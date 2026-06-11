#!/usr/bin/env python3
"""
Dynatrace MCP Server Entry Point
"""

import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# Add server directory to path
sys.path.insert(0, str(Path(__file__).parent))

# Load environment variables from .env (if present)
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

from dynatrace_mcp import DynastraceMCPServer
from dynatrace_mcp.dynatrace_client import DynatraceClient
from dynatrace_mcp.chat_agent import ChatAgent
from dynatrace_mcp.mock_data import (
    MockDynatraceClient,
    critical_incident,
    get_mock_incident,
    minor_incident,
)
from dynatrace_mcp.slack_notifier import SlackNotifier

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

server = DynastraceMCPServer()
chat_agent: Optional[ChatAgent] = None

DEFAULT_CORS_ORIGINS = [
    "https://incident-commander-app-production.up.railway.app",
    "https://incident-commander-agent-production.up.railway.app",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


def _csv_env(name: str, defaults: list[str]) -> list[str]:
    values = os.getenv(name)
    if not values:
        return defaults
    return [value.strip().rstrip("/") for value in values.split(",") if value.strip()]


# ── Lifespan (replaces deprecated @app.on_event) ────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown."""
    global chat_agent
    logger.info("Starting Dynatrace HTTP server")
    try:
        chat_agent = ChatAgent()
        logger.info("Chat agent initialised (model: %s)", chat_agent.model_name)
    except Exception as exc:
        logger.error("Failed to initialise chat agent: %s", exc)
        chat_agent = None

    yield  # ← server is running

    if server.dynatrace_client:
        await server.close()
    if chat_agent:
        await chat_agent.close()
        logger.info("Chat agent closed")


# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_csv_env("CORS_ORIGINS", DEFAULT_CORS_ORIGINS),
    allow_origin_regex=os.getenv("CORS_ORIGIN_REGEX", r"https://.*\.up\.railway\.app"),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _serialize_tool_result(result: Any) -> Any:
    if hasattr(result, "structuredContent") and result.structuredContent is not None:
        return result.structuredContent

    if hasattr(result, "content"):
        content = list(result.content)
        if len(content) == 1 and getattr(content[0], "type", None) == "text":
            text_value = getattr(content[0], "text", None)
            if isinstance(text_value, str):
                try:
                    return json.loads(text_value)
                except json.JSONDecodeError:
                    return {"text": text_value}
        return [c.model_dump() if hasattr(c, "model_dump") else c for c in content]

    return result


def _use_mock_data() -> bool:
    """Use mock dashboard data when requested or when Dynatrace is not configured."""
    if os.getenv("USE_MOCK_RESPONSES", "false").lower() == "true":
        return True

    required = ("DYNATRACE_ENVIRONMENT_ID", "DYNATRACE_API_TOKEN", "DYNATRACE_API_URL")
    return not all(os.getenv(key) for key in required)


def _ensure_data_client() -> None:
    if server.dynatrace_client:
        return

    if _use_mock_data():
        logger.info("Using mock Dynatrace data source for dashboard tools")
        server.dynatrace_client = MockDynatraceClient()  # type: ignore[assignment]
    else:
        server.dynatrace_client = DynatraceClient()


# ── Routes ───────────────────────────────────────────────────────────────────
@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "incident-commander-agent",
        "model": chat_agent.model_name if chat_agent else None,
    }


@app.get("/")
async def root_health_check():
    return await health_check()


@app.get("/ready")
async def readiness_check():
    return {
        "status": "ready",
        "service": "incident-commander-agent",
        "chatAgentAvailable": chat_agent is not None,
        "mockDataEnabled": _use_mock_data(),
    }


@app.post("/tools/{tool_name}")
async def call_tool(tool_name: str, payload: dict[str, Any] = Body(default_factory=dict)):
    tool_names = [tool.name for tool in server.tools]
    if tool_name not in tool_names:
        raise HTTPException(status_code=404, detail="Tool not found")

    _ensure_data_client()

    result = await server._handle_tool_call(tool_name, payload or {})
    return _serialize_tool_result(result)


@app.post("/chat")
async def chat(
    message: str = Body(..., embed=True),
    incident_id: str = Body(None, embed=True),
    incident_context: dict = Body(None, embed=True),
    conversation_id: str = Body(None, embed=True),
    role: str = Body(None, embed=True),
):
    """Conversational endpoint — multi-turn, Gemini-powered."""
    if not chat_agent:
        raise HTTPException(status_code=503, detail="Chat agent not available")

    try:
        context = incident_context
        role_up = (role or "").upper()
        if not context:
            if incident_id:
                context = get_mock_incident(incident_id)
            elif role_up == "L1":
                context = minor_incident
            else:
                # default to critical for L2/L3 and unknown roles for testing
                context = critical_incident

        return await chat_agent.chat(
            user_message=message,
            incident_context=context,
            conversation_id=conversation_id,
            role=role,
        )
    except Exception as exc:
        logger.error("Error in /chat: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/chat/history")
async def get_chat_history(conversation_id: str = None):
    if not chat_agent:
        raise HTTPException(status_code=503, detail="Chat agent not available")
    history = chat_agent.get_conversation_history()
    return {"messages": history, "count": len(history)}


@app.post("/chat/clear")
async def clear_chat_history():
    if not chat_agent:
        raise HTTPException(status_code=503, detail="Chat agent not available")
    chat_agent.clear_conversation_history()
    return {"status": "cleared"}


@app.post("/notify/critical")
async def notify_critical(
    incident_id: str = Body(..., embed=True),
    channel: str = Body(None, embed=True),
    incident_context: dict = Body(None, embed=True),
):
    """
    Manually trigger a Slack critical-incident alert for a given incident ID.
    Fetches the incident from Dynatrace and posts it to the configured Slack channel.
    """
    notifier = SlackNotifier()
    if channel:
        notifier.channel = channel
    if not notifier.enabled:
        raise HTTPException(
            status_code=503,
            detail="Slack notifications are not configured. Set SLACK_WEBHOOK_URL in .env",
        )

    try:
        _ensure_data_client()
        incident = incident_context or await server.dynatrace_client.get_incident_details(incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")

        sent = await notifier.notify_critical_incident(incident)
        return {
            "status": "sent" if sent else "failed",
            "incident_id": incident_id,
            "channel": notifier.channel,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error in /notify/critical: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/notify/slack")
async def notify_slack(
    incident_id: str = Body(..., embed=True),
    channel: str = Body("#incidents", embed=True),
    incident_context: dict = Body(None, embed=True),
    message: str = Body(None, embed=True),
):
    """Send or mock-send an incident update to a selected Slack channel."""
    notifier = SlackNotifier()
    notifier.channel = channel

    if not notifier.enabled:
        return {
            "status": "not_configured",
            "incident_id": incident_id,
            "channel": channel,
            "detail": "Slack webhook is not configured. Set SLACK_WEBHOOK_URL or enable USE_MOCK_RESPONSES.",
        }

    try:
        _ensure_data_client()
        incident = incident_context or await server.dynatrace_client.get_incident_details(incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")

        severity = str(incident.get("severity") or "UNKNOWN").upper()
        title = incident.get("title", "Unknown incident")
        impact = incident.get("impact") or incident.get("description") or "Impact is under investigation."
        text = message or (
            f"[{severity}] {title}\n"
            f"Incident: {incident_id}\n"
            f"Impact: {impact}\n"
            "Action: Incident response is active. Further updates will follow from Dynatrace MCP."
        )
        color = {
            "CRITICAL": "#FF0000",
            "HIGH": "#FF6600",
            "MEDIUM": "#FFC107",
            "LOW": "#36A64F",
        }.get(severity, "#36A64F")

        sent = await notifier.notify_custom(text=text, color=color)
        return {
            "status": "sent" if sent else "failed",
            "incident_id": incident_id,
            "channel": channel,
            "message": text,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error in /notify/slack: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Entry point ──────────────────────────────────────────────────────────────
def run():
    host = os.getenv("HOST") or ("0.0.0.0" if os.getenv("PORT") else os.getenv("MCP_SERVER_HOST", "0.0.0.0"))
    port = int(os.getenv("PORT") or os.getenv("MCP_SERVER_PORT", "3002"))
    uvicorn.run("main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
