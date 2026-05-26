
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List

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
) -> List[Dict[str, Any]]:
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


def select_tool(requested_tool: Optional[str] = None, loan_type: Optional[str] = None) -> Tuple[str, str]:
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
        raise ValueError(
            f"Missing required fields: {missing}. "
            f"Provide structured fields, or natural language containing principal, rate/interest, and tenure/term."
        )

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

app = FastAPI(title="Mortgage/EMI Agent (Pega A2A)", version="1.1.0")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rpc_result(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# In-memory task store
_tasks: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Natural language parsing helpers
# ---------------------------------------------------------------------------

_SCALE = {
    "k": 1_000,
    "thousand": 1_000,
    "m": 1_000_000,
    "million": 1_000_000,
    "b": 1_000_000_000,
    "bn": 1_000_000_000,
    "billion": 1_000_000_000,
    "lakh": 100_000,
    "lakhs": 100_000,
    "crore": 10_000_000,
    "crores": 10_000_000,
}

def _clean_number_str(s: str) -> str:
    # Remove currency symbols and commas
    s = s.strip()
    s = re.sub(r"[₹,$]", "", s)
    s = s.replace(",", "")
    return s

def _parse_scaled_number(text: str) -> Optional[float]:
    """
    Parses things like:
      500000
      5,00,000
      500k
      2.5m
      5 lakh
      1.2 crore
    """
    t = text.lower().strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k|thousand|m|million|b|bn|billion|lakh|lakhs|crore|crores)\b", t)
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        return val * _SCALE[unit]

    # plain number
    m2 = re.search(r"\d[\d,]*(?:\.\d+)?", t)
    if m2:
        try:
            return float(_clean_number_str(m2.group(0)))
        except Exception:
            return None
    return None

def _infer_loan_type(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["home", "house", "housing", "mortgage"]):
        return "home"
    if any(w in t for w in ["car", "vehicle", "auto", "automobile", "bike", "two-wheeler", "two wheeler"]):
        return "vehicle"
    return "generic"

def _parse_tenure_months(text: str) -> Optional[int]:
    t = text.lower()

    # explicit "xx years" or "xx months"
    m_years = re.search(r"(\d+(?:\.\d+)?)\s*(years|year|yrs|yr)\b", t)
    if m_years:
        years = float(m_years.group(1))
        return int(round(years * 12))

    m_months = re.search(r"(\d+(?:\.\d+)?)\s*(months|month|mos|mo)\b", t)
    if m_months:
        months = float(m_months.group(1))
        return int(round(months))

    # patterns like tenure_months = 460
    m = re.search(r"(tenure|term|duration)\s*(months|month|mos|mo)?\s*=?\s*(\d+)", t)
    if m:
        return int(m.group(3))

    return None

def _parse_interest_rate(text: str) -> Optional[float]:
    t = text.lower()

    # explicit "8%" or "8.5 %"
    m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%+", t)
    if m_pct:
        return float(m_pct.group(1))

    # "interest rate = 8" or "annual_interest_rate = 8.5"
    m = re.search(r"(interest|rate|apr|annual_interest_rate)\s*=?\s*(\d+(?:\.\d+)?)", t)
    if m:
        return float(m.group(2))

    # "at 8 percent"
    m2 = re.search(r"at\s*(\d+(?:\.\d+)?)\s*(percent|percentage)\b", t)
    if m2:
        return float(m2.group(1))

    return None

def _schedule_requested(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in ["amortization", "amortisation", "schedule", "breakdown", "table"])

def _parse_schedule_months(text: str) -> Optional[int]:
    """
    If user asks: "first 12 months schedule" or "schedule for 24 months"
    """
    t = text.lower()
    m = re.search(r"(first|next)\s*(\d+)\s*(months|month|mos|mo)\b", t)
    if m:
        return int(m.group(2))
    m2 = re.search(r"(schedule|amortization|breakdown)\s*(for)?\s*(\d+)\s*(months|month|mos|mo)\b", t)
    if m2:
        return int(m2.group(3))
    return None

def _parse_natural_language(text: str) -> dict:
    """
    Try to parse principal, interest, tenure from natural language.
    Returns dict with any fields found.
    """
    if not text or not isinstance(text, str):
        return {}

    t = text.strip()
    low = t.lower()

    out: dict = {}

    # loan type inference
    out["loan_type"] = _infer_loan_type(low)

    # principal
    # Prefer explicit labels; otherwise first "large" number
    principal = None
    m_pr = re.search(r"(principal|loan amount|amount|borrow|borrowing)\s*[:=]?\s*([₹,$]?\s*[\d,]+(?:\.\d+)?(?:\s*(?:k|m|bn|lakh|crore))?)", low)
    if m_pr:
        principal = _parse_scaled_number(m_pr.group(2))
    if principal is None:
        # fallback: pick the largest numeric-looking value (often principal)
        nums = re.findall(r"[₹,$]?\s*\d[\d,]*(?:\.\d+)?\s*(?:k|m|bn|lakh|crore)?", low)
        parsed = []
        for n in nums:
            val = _parse_scaled_number(n)
            if val is not None:
                parsed.append(val)
        if parsed:
            principal = max(parsed)  # principal tends to be the largest
    if principal is not None:
        out["principal"] = float(principal)

    # interest
    rate = _parse_interest_rate(low)
    if rate is not None:
        out["annual_interest_rate"] = float(rate)

    # tenure
    tenure_m = _parse_tenure_months(low)
    if tenure_m is not None:
        out["tenure_months"] = int(tenure_m)

    # schedule intent
    if _schedule_requested(low):
        sm = _parse_schedule_months(low)
        if sm is not None:
            out["schedule_months"] = int(sm)
        else:
            # default to first 12 months when schedule requested but not specified
            out["schedule_months"] = 12

    # explicit tool hint
    if "home" in low or "mortgage" in low:
        out.setdefault("tool", "calculate_home_emi")
    elif any(w in low for w in ["vehicle", "car", "auto", "bike"]):
        out.setdefault("tool", "calculate_vehicle_emi")
    else:
        out.setdefault("tool", "calculate_generic_emi")

    return out


# ---------------------------------------------------------------------------
# A2A input extraction (structured + natural language)
# ---------------------------------------------------------------------------

def _extract_params_from_message(message: dict) -> dict:
    """
    Extract EMI params from A2A message.parts.

    Supports:
      - data parts: {"type"/"kind":"data","data":{...}}
      - text parts: {"type"/"kind":"text","text":"..."} -> natural language parsing
      - text parts containing JSON: {"text":"{...json...}"}
    """
    params: dict = {}

    # Collect text across all parts for better parsing
    text_blobs: List[str] = []

    for part in (message.get("parts") or []):
        kind = part.get("type") or part.get("kind")

        if kind == "data":
            data = part.get("data") or {}
            if isinstance(data, dict):
                # If someone sends {"question": "..."} inside data, parse it too
                q = data.get("question") or data.get("query") or data.get("input")
                if isinstance(q, str) and q.strip():
                    params.update(_parse_natural_language(q))
                params.update(data)

        elif kind == "text":
            txt = part.get("text", "")
            if isinstance(txt, str) and txt.strip():
                text_blobs.append(txt)

                # If text is JSON object, merge it
                try:
                    maybe = json.loads(txt)
                    if isinstance(maybe, dict):
                        params.update(maybe)
                except Exception:
                    pass

    # If we have any text, parse it as natural language and fill missing fields
    if text_blobs:
        merged_text = " ".join(text_blobs)
        nl = _parse_natural_language(merged_text)

        # Merge without overwriting already-provided structured values
        for k, v in nl.items():
            params.setdefault(k, v)

    return params


def _make_task(
    task_id: str,
    state: str,
    *,
    context_id: Optional[str] = None,
    error_message: Optional[str] = None,
    artifacts: Optional[list] = None,
) -> Dict[str, Any]:
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
# Agent Card (Pega expects /.well-known/agent.json)
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("BASE_URL")  # set this in Render for accuracy (recommended)

def _effective_base_url(request: Optional[Request] = None) -> str:
    if BASE_URL:
        return BASE_URL.rstrip("/")
    if request is not None:
        return str(request.base_url).rstrip("/")
    return "http://localhost:8001"

AGENT_CARD_TEMPLATE = {
    "name": "Mortgage/EMI Calculator Agent",
    "description": (
        "Calculates EMI, total payment, total interest, and optional amortization schedule. "
        "Accepts structured JSON in data parts OR natural language in text parts."
    ),
    "version": "1.1.0",
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
            "inputModes": ["application/json", "text/plain"],
            "outputModes": ["application/json"],
        },
        {
            "id": "calculate_vehicle_emi",
            "name": "Vehicle Loan EMI",
            "description": "Calculate EMI and amortization schedule for a vehicle loan.",
            "inputModes": ["application/json", "text/plain"],
            "outputModes": ["application/json"],
        },
        {
            "id": "calculate_generic_emi",
            "name": "Generic Loan EMI",
            "description": "Calculate EMI and amortization schedule for any loan.",
            "inputModes": ["application/json", "text/plain"],
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
    card = dict(AGENT_CARD_TEMPLATE)
    card["url"] = _effective_base_url(request) + "/"
    return card


# ---------------------------------------------------------------------------
# JSON-RPC endpoint (Pega calls message/send)
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

    if method == "message/send":
        msg = params.get("message") or {}
        context_id = msg.get("contextId")
        task_id = params.get("taskId") or params.get("id") or str(uuid.uuid4())

        _tasks[task_id] = _make_task(task_id, "working", context_id=context_id)

        try:
            emi_params = _extract_params_from_message(msg)

            # If schedule requested but defaulted to 12 and tenure < 12, cap it
            if emi_params.get("schedule_months") is not None and emi_params.get("tenure_months") is not None:
                emi_params["schedule_months"] = min(int(emi_params["schedule_months"]), int(emi_params["tenure_months"]))

            emi_result = _run_emi_task(emi_params)

            artifacts = [
                {
                    "name": "emi_result",
                    "parts": [{"type": "data", "kind": "data", "data": emi_result}],
                }
            ]
            task = _make_task(task_id, "completed", context_id=context_id, artifacts=artifacts)
            _tasks[task_id] = task
            return JSONResponse(_rpc_result(rpc_id, task))

        except ValueError as exc:
            task = _make_task(task_id, "failed", context_id=context_id, error_message=str(exc))
            _tasks[task_id] = task
            return JSONResponse(_rpc_result(rpc_id, task))

    if method == "message/stream":
        return JSONResponse(_rpc_error(rpc_id, -32601, "Method not supported: message/stream"), status_code=400)

    if method == "tasks/get":
        task_id = params.get("id") or params.get("taskId")
        if not task_id or task_id not in _tasks:
            return JSONResponse(_rpc_error(rpc_id, -32001, f"Task not found: {task_id}"), status_code=404)
        return JSONResponse(_rpc_result(rpc_id, _tasks[task_id]))

    if method == "tasks/cancel":
        task_id = params.get("id") or params.get("taskId")
        if not task_id or task_id not in _tasks:
            return JSONResponse(_rpc_error(rpc_id, -32001, f"Task not found: {task_id}"), status_code=404)
        _tasks[task_id] = _make_task(task_id, "canceled")
        return JSONResponse(_rpc_result(rpc_id, _tasks[task_id]))

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

    # Support: {"question": "..."} in legacy calls
    if payload.get("question") and not any(payload.get(k) is not None for k in ("principal", "annual_interest_rate", "tenure_months")):
        payload.update(_parse_natural_language(str(payload["question"])))

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
