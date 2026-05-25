from __future__ import annotations

import json
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
    params: dict = {}
    for part in message.get("parts", []):
        kind = _part_kind(part)
        if kind == "data":
            data = part.get("data") or {}
            if isinstance(data, dict):
                params.update(data)
        elif kind == "text":
            text = part.get("text", "")
            try:
                maybe = json.loads(text)
                if isinstance(maybe, dict):
                    params.update(maybe)
            except Exception:
                # If text isn't JSON, we don't guess numbers—return empty and let validation fail.
                pass
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
