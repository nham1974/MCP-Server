from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional

app = FastAPI()

# ✅ Request model (Pega friendly)
class AgentRequest(BaseModel):
    request_id: Optional[str] = None
    action: str
    input: Optional[dict] = {}

# ✅ Response model
class AgentResponse(BaseModel):
    request_id: Optional[str]
    status: str
    output: dict


# ✅ Health check
@app.get("/")
def root():
    return {"status": "A2A agent running"}


# ✅ MAIN AGENT ENDPOINT
@app.post("/agent")
def agent_handler(req: AgentRequest):

    if req.action == "ping":
        return AgentResponse(
            request_id=req.request_id,
            status="success",
            output={"message": "pong"}
        )

    elif req.action == "echo":
        return AgentResponse(
            request_id=req.request_id,
            status="success",
            output={"echo": req.input}
        )

    else:
        return AgentResponse(
            request_id=req.request_id,
            status="error",
            output={"error": "Unsupported action"}
        )


# ✅ AGENT CARD (WELL-KNOWN)
@app.get("/.well-known/agent-card.json")
def agent_card():
    return {
        "name": "Simple POC Agent",
        "description": "Minimal Agent2Agent service for POC",
        "url": "https://mcp-server-9h5q.onrender.com/agent",
        "version": "1.0.0",
        "authentication": {
            "type": "none"
        },
        "capabilities": {
            "streaming": False
        },
        "skills": [
            {
                "id": "ping",
                "name": "Ping",
                "description": "Health check",
                "input": {},
                "output": {"message": "pong"}
            },
            {
                "id": "echo",
                "name": "Echo",
                "description": "Returns input",
                "input": {"any": "json"},
                "output": {"echo": "json"}
            }
        ]
    }