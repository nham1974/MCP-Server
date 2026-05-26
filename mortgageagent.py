from __future__ import annotations
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
# ---------------------------------------------------------------------------
# SIMPLE CONFIG (Hardcoded values)
# ---------------------------------------------------------------------------
DEFAULT_INTEREST = 8        # 8%
DEFAULT_TENURE = 360       # 30 years (months)
# ---------------------------------------------------------------------------
# EMI Calculation
# ---------------------------------------------------------------------------
def calculate_emi(principal: float) -> Dict[str, Any]:
    monthly_rate = DEFAULT_INTEREST / 12 / 100
    if monthly_rate == 0:
        emi = principal / DEFAULT_TENURE
    else:
        factor = (1 + monthly_rate) ** DEFAULT_TENURE
        emi = principal * monthly_rate * factor / (factor - 1)
    return {
        "principal": principal,
        "interest": DEFAULT_INTEREST,
        "tenure_months": DEFAULT_TENURE,
        "emi": round(emi, 2)
    }
# ---------------------------------------------------------------------------
# App + helpers
# ---------------------------------------------------------------------------
app = FastAPI(title="Simple Mortgage Agent", version="1.0")
_tasks: Dict[str, Dict[str, Any]] = {}
def _now():
    return datetime.now(timezone.utc).isoformat()
def _rpc_result(rpc_id, result):
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
def _rpc_error(rpc_id, message):
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"message": message}}
def _make_task(task_id, state, *, data=None, error=None):
    task = {
        "kind": "task",
        "id": task_id,
        "status": {
            "state": state,
            "timestamp": _now()
        },
        "artifacts": []
    }
    if data:
        task["artifacts"].append({
            "name": "result",
            "parts": [
                {"type": "data", "data": data}
            ]
        })
    if error:
        task["status"]["message"] = error
    return task
# ---------------------------------------------------------------------------
# ✅ SIMPLE NATURAL LANGUAGE PARSER
# ---------------------------------------------------------------------------
def _extract_params(message: dict):
    text = ""
    params = {}
    for part in message.get("parts", []):
        if part.get("type") == "text":
            text += part.get("text", "")
        elif part.get("type") == "data":
            data = part.get("data", {})
            params.update(data)
    text = text.lower()
    # ✅ 1. Loan type detection
    if "home" in text or "house" in text:
        params["loan_type"] = "home"
    elif "car" in text or "vehicle" in text:
        params["loan_type"] = "vehicle"
    else:
        params.setdefault("loan_type", "generic")
    # ✅ 2. Principal = first number
    numbers = re.findall(r'\d+', text)
    if numbers and "principal" not in params:
        params["principal"] = float(numbers[0])
    return params
# ---------------------------------------------------------------------------
# JSON-RPC (A2A)
# ---------------------------------------------------------------------------
@app.post("/")
async def a2a_handler(request: Request):
    body = await request.json()
    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params", {})
    if method != "message/send":
        return JSONResponse(_rpc_error(rpc_id, "Unsupported method"))
    msg = params.get("message", {})
    task_id = str(uuid.uuid4())
    try:
        extracted = _extract_params(msg)
        principal = extracted.get("principal")
        loan_type = extracted.get("loan_type", "generic")
        if not principal:
            raise ValueError("Principal not found in input")
        result = calculate_emi(principal)
        result["loan_type"] = loan_type
        task = _make_task(task_id, "completed", data=result)
        _tasks[task_id] = task
        return JSONResponse(_rpc_result(rpc_id, task))
    except Exception as e:
        task = _make_task(task_id, "failed", error=str(e))
        _tasks[task_id] = task
        return JSONResponse(_rpc_result(rpc_id, task))
# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}
