from __future__ import annotations

import json as _json
import fastapi
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# EMI business logic
# ---------------------------------------------------------------------------

def round_money(value):
    return round(value, 2)


def calculate_emi(principal, annual_interest_rate, tenure_months):
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


def build_schedule(principal, annual_interest_rate, tenure_months, schedule_months):
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


def select_tool(requested_tool=None, loan_type=None):
    if requested_tool:
        return requested_tool, "Tool explicitly selected by caller."
    if loan_type == "home":
        return "calculate_home_emi", "Selected home-loan tool from loan_type."
    if loan_type == "vehicle":
        return "calculate_vehicle_emi", "Selected vehicle-loan tool from loan_type."
    return "calculate_generic_emi", "Using the generic EMI tool."


def run_tool(tool_name, principal, annual_interest_rate, tenure_months, schedule_months, loan_type):
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
        result["schedule"] = build_schedule(
            principal, annual_interest_rate, tenure_months, int(schedule_months)
        )
    return result


def _run_emi_task(params: dict) -> dict:
    """Shared validation + execution used by both /invoke and A2A tasks/send."""
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
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="EMI Agent (A2A + legacy)", version="0.2.0")

# ---------------------------------------------------------------------------
# A2A Agent Card info
# ---------------------------------------------------------------------------

AGENT_CARD = {
    "name": "EMI Calculator Agent",
    "description": (
        "Calculates EMI, total payment, total interest, and amortization schedule "
        "for home, vehicle, and generic loans. Supports the A2A JSON-RPC 2.0 protocol."
    ),
    "url": "http://localhost:8001/",
    "version": "0.2.0",
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
            "examples": ["Calculate home loan EMI for 45,00,000 at 8.5% for 20 years"],
        },
        {
            "id": "calculate_vehicle_emi",
            "name": "Vehicle Loan EMI",
            "description": "Calculate EMI and amortization schedule for a vehicle loan.",
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
            "examples": ["Calculate vehicle loan EMI for 8,00,000 at 9% for 5 years"],
        },
        {
            "id": "calculate_generic_emi",
            "name": "Generic Loan EMI",
            "description": "Calculate EMI and amortization schedule for any loan.",
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
            "examples": ["Calculate EMI for 5,00,000 at 10% for 3 years"],
        },
    ],
}

# In-memory task store (POC; replace with persistent storage in production)
_tasks: dict = {}


# ---------------------------------------------------------------------------
# A2A helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id, code: int, message: str):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _extract_params_from_message(message: dict) -> dict:
    """Pull EMI parameters from A2A message parts (data part or JSON-encoded text part)."""
    params: dict = {}
    for part in message.get("parts", []):
        if part.get("type") == "data":
            params.update(part.get("data", {}))
        elif part.get("type") == "text":
            try:
                data = _json.loads(part.get("text", ""))
                if isinstance(data, dict):
                    params.update(data)
            except Exception:
                pass
    return params


# ---------------------------------------------------------------------------
# A2A endpoints
# ---------------------------------------------------------------------------

@app.get("/.well-known/agent.json", include_in_schema=False)
def agent_card():
    """A2A Agent Card — discovery endpoint used by Pega Agent2Agent."""
    return AGENT_CARD


@app.post("/")
async def a2a_rpc(request: Request):
    """
    JSON-RPC 2.0 endpoint for the A2A protocol.

    Supported methods:
      tasks/send   — submit a task and get a synchronous result
      tasks/get    — retrieve a previously submitted task by id
      tasks/cancel — cancel a task by id
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            _rpc_error(None, -32700, "Parse error: request body is not valid JSON"),
            status_code=400,
        )

    if not isinstance(body, dict):
        return JSONResponse(_rpc_error(None, -32600, "Invalid Request"), status_code=400)

    if body.get("jsonrpc") != "2.0":
        return JSONResponse(
            _rpc_error(body.get("id"), -32600, "Invalid Request: jsonrpc must be '2.0'"),
            status_code=400,
        )

    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params", {})

    # ── tasks/send ──────────────────────────────────────────────────────────
    if method == "tasks/send":
        task_id = params.get("id") or str(uuid.uuid4())
        message = params.get("message", {})
        emi_params = _extract_params_from_message(message)

        task: dict = {
            "id": task_id,
            "status": {"state": "working", "timestamp": _now()},
            "artifacts": [],
        }
        _tasks[task_id] = task

        try:
            emi_result = _run_emi_task(emi_params)
            task["status"] = {"state": "completed", "timestamp": _now()}
            task["artifacts"] = [
                {
                    "name": "emi_result",
                    "parts": [{"type": "data", "data": emi_result}],
                }
            ]
        except ValueError as exc:
            task["status"] = {"state": "failed", "timestamp": _now(), "message": str(exc)}

        return JSONResponse(_rpc_result(rpc_id, task))

    # ── tasks/get ───────────────────────────────────────────────────────────
    if method == "tasks/get":
        task_id = params.get("id")
        if not task_id or task_id not in _tasks:
            return JSONResponse(
                _rpc_error(rpc_id, -32001, f"Task not found: {task_id}"),
                status_code=404,
            )
        return JSONResponse(_rpc_result(rpc_id, _tasks[task_id]))

    # ── tasks/cancel ────────────────────────────────────────────────────────
    if method == "tasks/cancel":
        task_id = params.get("id")
        if not task_id or task_id not in _tasks:
            return JSONResponse(
                _rpc_error(rpc_id, -32001, f"Task not found: {task_id}"),
                status_code=404,
            )
        _tasks[task_id]["status"] = {"state": "cancelled", "timestamp": _now()}
        return JSONResponse(_rpc_result(rpc_id, _tasks[task_id]))

    # ── unknown method ──────────────────────────────────────────────────────
    return JSONResponse(
        _rpc_error(rpc_id, -32601, f"Method not found: {method}"),
        status_code=404,
    )


# ---------------------------------------------------------------------------
# Legacy endpoints (backward compatible with Postman / Bruno / curl)
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "available_tools": ["calculate_generic_emi", "calculate_home_emi", "calculate_vehicle_emi"],
    }


@app.post("/invoke")
async def invoke_agent(request: Request):
    content_type = request.headers.get("content-type", "").lower()

    if "application/json" in content_type or not content_type:
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Invalid request body. Send valid JSON, or use form-data/x-www-form-urlencoded.",
            )
    elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        payload = dict(form)
    else:
        raise HTTPException(
            status_code=400,
            detail="Unsupported content type. Use application/json, form-data, or x-www-form-urlencoded.",
        )

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object.")

    try:
        return _run_emi_task(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))