
from __future__ import annotations

import json
import re
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# EMI business logic
# ---------------------------------------------------------------------------

def round_money(value: float) -> float:
    return round(value, 2)


def calculate_emi(principal: float, annual_interest_rate: float, tenure_months: int) -> Dict[str, Any]:
    monthly_rate = annual_interest_rate / 12 / 100
    if monthly_rate == 0:
        emi = principal / tenure_months
    else:
        factor = (1 + monthly_rate) ** tenure_months
        emi = principal * monthly_rate * factor / (factor - 1)

    total_payment = emi * tenure_months
    total_interest = total_payment - principal
    return {
        "monthly_rate": monthly_rate,
        "emi": round_money(emi),
        "total_payment": round_money(total_payment),
        "total_interest": round_money(total_interest),
    }


def build_schedule(
    principal: float,
    annual_interest_rate: float,
    tenure_months: int,
    schedule_months: int,
) -> list[Dict[str, Any]]:
    monthly_rate = annual_interest_rate / 12 / 100
    emi = calculate_emi(principal, annual_interest_rate, tenure_months)["emi"]
    balance = principal
    rows = []

    for month in range(1, min(schedule_months, tenure_months) + 1):
        interest_paid = balance * monthly_rate
        principal_paid = emi - interest_paid if monthly_rate > 0 else emi
        closing_balance = max(balance - principal_paid, 0)

        rows.append(
            {
                "month": month,
                "opening_balance": round_money(balance),
                "principal_paid": round_money(principal_paid),
                "interest_paid": round_money(interest_paid),
                "emi": emi,
                "closing_balance": round_money(closing_balance),
            }
        )
        balance = closing_balance

    return rows


def select_tool(requested_tool: Optional[str] = None, loan_type: Optional[str] = None) -> tuple[str, str]:
    if requested_tool:
        return requested_tool, "Tool explicitly selected by caller."
    if loan_type == "home":
        return "calculate_home_emi", "Selected home-loan tool from loan_type."
    if loan_type == "vehicle":
        return "calculate_vehicle_emi", "Selected vehicle-loan tool from loan_type."
    return "calculate_generic_emi", "Using the generic EMI tool."


def run_tool(
    tool_name: str,
    principal: float,
    annual_interest_rate: float,
    tenure_months: int,
    schedule_months: Optional[int],
    loan_type: str,
) -> Dict[str, Any]:
    result = calculate_emi(principal, annual_interest_rate, tenure_months)
    result.update(
        {
            "loan_type": loan_type,
            "principal": round_money(principal),
            "annual_interest_rate": annual_interest_rate,
            "tenure_months": tenure_months,
            "tool_name": tool_name,
        }
    )
    if schedule_months:
        result["schedule"] = build_schedule(principal, annual_interest_rate, tenure_months, int(schedule_months))
    return result


def _run_emi_task(params: dict) -> dict:
    """Shared validation + execution used by message/send and /invoke."""
    missing = [k for k in ("principal", "annual_interest_rate", "tenure_months") if params.get(k) is None]
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    try:
        principal = float(params["principal"])
        annual_interest_rate = float(params["annual_interest_rate"])
        tenure_months = int(params["tenure_months"])
        schedule_months = int(params["schedule_months"]) if params.get("schedule_months") is not None else None
    except (TypeError, ValueError):
        raise ValueError("principal, annual_interest_rate, tenure_months, schedule_months must be numeric")

    if principal <= 0 or tenure_months <= 0 or annual_interest_rate < 0:
        raise ValueError("principal and tenure_months must be > 0; annual_interest_rate must be >= 0")

    loan_type = params.get("loan_type", "generic")
    tool_name, reasoning = select_tool(params.get("tool"), loan_type)
    result = run_tool(tool_name, principal, annual_interest_rate, tenure_months, schedule_months, loan_type)

    return {
        "selected_tool": tool_name,
        "reasoning": reasoning,
        "execution": {
            "tool_name": tool_name,
            "arguments": {
                "loan_type": loan_type,
                "principal": principal,
                "annual_interest_rate": annual_interest_rate,
                "tenure_months": tenure_months,
                "schedule_months": schedule_months,
            },
            "result": result,
        },
    }


# ---------------------------------------------------------------------------
# FastAPI app + A2A (Pega-friendly)
# ---------------------------------------------------------------------------

app = FastAPI(title="Mortgage/EMI Agent (Pega A2A)", version="1.0.0")

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _rpc_result(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}

def _rpc_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

# In-memory task store
_tasks: Dict[str, Dict[str, Any]] = {}

def _part_kind(part: dict) -> Optional[str]:
    # Some clients use "type", others use "kind"
    return part.get("type") or part.get("kind")

def _extract_params_from_message(message: dict) -> dict:
    """
    Extract EMI params from message.parts.
    Supports:
      - data parts: {"type"/"kind":"data","data":{...}}
      - text parts containing JSON: {"type"/"kind":"text","text":"{...json...}"}
    """
    params = {}
    
    for part in message.get("parts", []):
        kind = part.get("type") or part.get("kind")

        # ✅ 1. Structured JSON input (preferred for Pega)
        if kind == "data":
            params.update(part.get("data", {}))

        # ✅ 2. Text input fallback (your "question" case)
        elif kind == "text":
            text = part.get("text", "").lower()

            # extract numbers using regex
            principal = re.search(r'principal\s*=?\s*(\d+)', text)
            interest = re.search(r'(interest|annual_interest_rate)\s*=?\s*(\d+)', text)
            tenure = re.search(r'(tenure|months)\s*=?\s*(\d+)', text)

            if principal:
                params["principal"] = float(principal.group(1))
            if interest:
                params["annual_interest_rate"] = float(interest.group(2))
            if tenure:
                params["tenure_months"] = int(tenure.group(2))

    return params


def _make_task(task_id: str, state: str, *, context_id: Optional[str] = None, error_message: Optional[str] = None,
              artifacts: Optional[list] = None) -> Dict[str, Any]:
    task = {
        "kind": "task",
        "id": task_id,
        "status": {"state": state, "timestamp": _now()},
        "artifacts": artifacts or [],
    }
    if context_id:
        task["contextId"] = context_id
    if error_message:
        task["status"]["message"] = error_message
    return task


# ---------------------------------------------------------------------------
# Agent Card (Pega expects /.well-known/agent.json) 【3-d0b4d9】
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("BASE_URL")  # set this in Render for accuracy (recommended)

def _effective_base_url(request: Optional[Request] = None) -> str:
    if BASE_URL:
        return BASE_URL.rstrip("/")
    if request is not None:
        # derive from incoming request host (works behind Render)
        return str(request.base_url).rstrip("/")
    return "http://localhost:8001"

AGENT_CARD_TEMPLATE = {
    "name": "Mortgage/EMI Calculator Agent",
    "description": "Calculates EMI, total payment, total interest, and amortization schedule.",
    "version": "1.0.0",
    # "url" will be filled dynamically
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": False,
    },
    "skills": [
        {
            "id": "calculate_home_emi",
            "name": "Home Loan EMI",
            "description": "Calculate EMI and amortization schedule for a home loan.",
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        },
        {
            "id": "calculate_vehicle_emi",
            "name": "Vehicle Loan EMI",
            "description": "Calculate EMI and amortization schedule for a vehicle loan.",
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        },
        {
            "id": "calculate_generic_emi",
            "name": "Generic Loan EMI",
            "description": "Calculate EMI and amortization schedule for any loan.",
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        },
    ],
}

@app.get("/.well-known/agent.json", include_in_schema=False)
async def agent_card(request: Request):
    card = dict(AGENT_CARD_TEMPLATE)
    card["url"] = _effective_base_url(request) + "/"
    return card

@app.get("/.well-known/agent-card.json", include_in_schema=False)
async def agent_card_alias(request: Request):
    # Helpful for non-Pega A2A clients too
    card = dict(AGENT_CARD_TEMPLATE)
    card["url"] = _effective_base_url(request) + "/"
    return card


# ---------------------------------------------------------------------------
# JSON-RPC endpoint (Pega calls message/send) 【1-ca1215】【2-e03f15】
# ---------------------------------------------------------------------------

@app.post("/", include_in_schema=False)
async def a2a_jsonrpc(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_rpc_error(None, -32700, "Parse error: request body is not valid JSON"), status_code=400)

    if not isinstance(body, dict):
        return JSONResponse(_rpc_error(None, -32600, "Invalid Request"), status_code=400)

    if body.get("jsonrpc") != "2.0":
        return JSONResponse(_rpc_error(body.get("id"), -32600, "Invalid Request: jsonrpc must be '2.0'"), status_code=400)

    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    # --- message/send (PRIMARY for Pega) ---
    if method == "message/send":
        msg = params.get("message") or {}
        context_id = msg.get("contextId")
        task_id = params.get("taskId") or params.get("id") or str(uuid.uuid4())

        # store initial
        _tasks[task_id] = _make_task(task_id, "working", context_id=context_id)

        try:
            emi_params = _extract_params_from_message(msg)
            emi_result = _run_emi_task(emi_params)

            # Return a completed Task with artifacts
            artifacts = [
                {
                    "name": "emi_result",
                    "parts": [
                        # include both keys for compatibility across clients
                        {"type": "data", "kind": "data", "data": emi_result}
                    ],
                }
            ]
            task = _make_task(task_id, "completed", context_id=context_id, artifacts=artifacts)
            _tasks[task_id] = task
            return JSONResponse(_rpc_result(rpc_id, task))

        except ValueError as exc:
            task = _make_task(task_id, "failed", context_id=context_id, error_message=str(exc))
            _tasks[task_id] = task
            return JSONResponse(_rpc_result(rpc_id, task))

    # --- message/stream (optional) ---
    # If Pega ever calls this, we return a clear error (you can implement SSE later).
    if method == "message/stream":
        return JSONResponse(_rpc_error(rpc_id, -32601, "Method not supported: message/stream"), status_code=400)

    # --- tasks/get ---
    if method == "tasks/get":
        task_id = params.get("id") or params.get("taskId")
        if not task_id or task_id not in _tasks:
            return JSONResponse(_rpc_error(rpc_id, -32001, f"Task not found: {task_id}"), status_code=404)
        return JSONResponse(_rpc_result(rpc_id, _tasks[task_id]))

    # --- tasks/cancel ---
    if method == "tasks/cancel":
        task_id = params.get("id") or params.get("taskId")
        if not task_id or task_id not in _tasks:
            return JSONResponse(_rpc_error(rpc_id, -32001, f"Task not found: {task_id}"), status_code=404)
        _tasks[task_id] = _make_task(task_id, "canceled")
        return JSONResponse(_rpc_result(rpc_id, _tasks[task_id]))

    # Unknown method
    return JSONResponse(_rpc_error(rpc_id, -32601, f"Method not found: {method}"), status_code=404)


# ---------------------------------------------------------------------------
# Legacy endpoints (optional but useful for curl/Postman)
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/invoke")
async def invoke_agent(request: Request):
    content_type = request.headers.get("content-type", "").lower()

    if "application/json" in content_type or not content_type:
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON.")
    elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        payload = dict(form)
    else:
        raise HTTPException(status_code=400, detail="Unsupported content type.")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object.")

    try:
        return _run_emi_task(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# Local run convenience (port 8001), Render uses $PORT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8001"))
    uvicorn.run("mortgage:app", host="0.0.0.0", port=port)
