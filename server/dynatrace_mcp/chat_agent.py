"""
ADK-based Conversational Chat Agent for multi-turn incident management.

Uses Google ADK (Agent Development Kit) with a multi-agent orchestrator
to handle role-aware queries against Dynatrace incident data.
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .agents import incident_management_orchestrator, rotate_api_key, _API_KEY_POOL
from .dynatrace_client import DynatraceClient
from .mock_data import MockDynatraceClient
from .slack_notifier import SlackNotifier

logger = logging.getLogger(__name__)

APP_NAME = "dynatrace_incident_mgmt"

# Severity levels that trigger an automatic Slack critical-incident alert
_CRITICAL_SEVERITIES = {"CRITICAL", "HIGH"}


class ChatAgent:
    """
    ADK-powered conversational agent.

    Wraps the ADK Runner + InMemorySessionService to provide a simple
    `chat()` coroutine compatible with the existing FastAPI endpoints.
    """

    def __init__(self):
        self.session_service = InMemorySessionService()
        self.runner = self._build_runner()
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite")
        self.dynatrace_client = self._build_dynatrace_client()
        self.slack = SlackNotifier()
        self.conversation_history: List[Dict[str, Any]] = []
        self._alerted_incidents: set = set()

    def _build_runner(self) -> Runner:
        """Create a fresh ADK Runner (called on init and after each key rotation)."""
        return Runner(
            agent=incident_management_orchestrator,
            app_name=APP_NAME,
            session_service=self.session_service,
        )

    def _rotate_and_rebuild(self) -> None:
        """Rotate to the next API key and rebuild the runner."""
        rotate_api_key()
        self.runner = self._build_runner()

    @staticmethod
    def _build_dynatrace_client() -> Any:
        if os.getenv("USE_MOCK_RESPONSES", "false").lower() == "true":
            return MockDynatraceClient()

        required = ("DYNATRACE_ENVIRONMENT_ID", "DYNATRACE_API_TOKEN", "DYNATRACE_API_URL")
        if not all(os.getenv(key) for key in required):
            logger.info("Dynatrace credentials missing; chat agent using mock data source")
            return MockDynatraceClient()

        return DynatraceClient()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def chat(
        self,
        user_message: str,
        incident_context: Optional[Dict[str, Any]] = None,
        conversation_id: Optional[str] = None,
        role: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Process a user message through the ADK multi-agent orchestrator.

        Args:
            user_message:     The user's chat message.
            incident_context: Optional incident data for context.
            conversation_id:  Optional session identifier for multi-turn conversations.
            role:             Caller role — 'L1', 'L2', or 'L3'.

        Returns:
            Dict with response, used_agents, and metadata.
        """
        user_id = "default_user"
        session_id = conversation_id or f"session_{uuid.uuid4().hex[:12]}"

        try:
            await self._ensure_session(user_id, session_id)

            context_summary = await self._build_context(incident_context)
            enriched_message = self._build_message(
                user_message, context_summary, incident_context, role
            )

            # If mock responses are enabled, bypass external Gemini calls
            use_mock = os.getenv("USE_MOCK_RESPONSES", "false").lower() == "true"
            if use_mock:
                # Create a deterministic mock response based on severity/role
                severity = (incident_context.get("severity") or "").upper() if incident_context else ""
                if severity in {"CRITICAL", "HIGH"}:
                    response_text = (
                        "MOCK: Critical incident analysis — immediate actions:\n"
                        "1) Isolate affected service\n2) Restart failing pods\n3) Notify on-call\n\nRemedy: Rollback recent deployment; investigate DB connections."
                    )
                    used_agents = ["incident_analyzer", "remedy_identifier"]
                else:
                    response_text = "MOCK: Analysis complete. Suggested remediation: follow standard runbook."
                    used_agents = ["incident_analyzer"]

                # Mock usage metadata
                usage_meta = {"prompt_token_count": 10, "total_token_count": 50}

                # Trigger mock Slack events if applicable
                if incident_context and self.slack.enabled:
                    # Simulate critical incident slack post
                    if (incident_context.get("severity") or "").upper() in _CRITICAL_SEVERITIES:
                        await self.slack.notify_custom(f"MOCK SLACK: critical incident {incident_context.get('id')}", color="#FF0000")
                    # Simulate remedy identified post
                    await self.slack.notify_custom(f"MOCK SLACK: remedy identified for {incident_context.get('id')} by {role}", color="#36A64F")

                now = datetime.now(timezone.utc).isoformat()
                self.conversation_history.append(
                    {
                        "timestamp": now,
                        "user_message": user_message,
                        "assistant_response": response_text,
                        "agents_used": used_agents,
                        "role": role,
                        "session_id": session_id,
                    }
                )
                return {
                    "response": response_text,
                    "timestamp": now,
                    "status": "success",
                    "used_agents": used_agents,
                    "metadata": {
                        "status": "success",
                        "model": "MOCK",
                        "conversation_id": session_id,
                        "usage": usage_meta,
                        "framework": "mock",
                    },
                }

            # normal path: build content for ADK
            content = types.Content(
                role="user",
                parts=[types.Part(text=enriched_message)],
            )

            response_text = ""
            used_agents: List[str] = []
            usage_meta: Dict[str, Any] = {}

            # Retry across all available API keys on quota exhaustion
            max_attempts = len(_API_KEY_POOL)
            last_exc: Optional[Exception] = None

            for attempt in range(max_attempts):
                try:
                    async for event in self.runner.run_async(
                        user_id=user_id,
                        session_id=session_id,
                        new_message=content,
                    ):
                        author = getattr(event, "author", None)
                        if author and author not in used_agents and author != "user":
                            used_agents.append(author)

                        if getattr(event, "usage_metadata", None) and not usage_meta:
                            usage_meta = self._extract_usage(event)

                        if event.is_final_response():
                            try:
                                if event.content and event.content.parts:
                                    response_text = event.content.parts[0].text or ""
                            except (AttributeError, IndexError):
                                response_text = ""
                    break  # success — exit retry loop

                except Exception as exc:
                    error_str = str(exc)
                    if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                        last_exc = exc
                        if attempt < max_attempts - 1:
                            logger.warning(
                                "Quota exhausted on attempt %d/%d — rotating API key",
                                attempt + 1, max_attempts,
                            )
                            self._rotate_and_rebuild()
                            # Re-ensure the session exists on the new runner
                            await self._ensure_session(user_id, session_id)
                            continue
                    raise  # non-quota error or all keys exhausted

            if not response_text and last_exc:
                raise last_exc

            now = datetime.now(timezone.utc).isoformat()

            # ── Slack notifications ───────────────────────────────────────
            if incident_context and self.slack.enabled:
                severity = (
                    incident_context.get("severity") or ""
                ).upper()
                incident_id = incident_context.get("id", "")

                # 1. Critical-incident alert (de-duplicated per incident ID)
                if (
                    severity in _CRITICAL_SEVERITIES
                    and incident_id not in self._alerted_incidents
                ):
                    sent = await self.slack.notify_critical_incident(
                        incident_context
                    )
                    if sent and incident_id:
                        self._alerted_incidents.add(incident_id)

                # 2. Remedy-identified alert when remedy agent contributed
                remedy_agents = {"remedy_identifier", "root_cause_analyzer"}
                if remedy_agents.intersection(used_agents) and response_text:
                    await self.slack.notify_remedy_identified(
                        incident=incident_context,
                        remedy_summary=response_text,
                        role=role,
                        agents_used=used_agents,
                    )
            # ─────────────────────────────────────────────────────────────

            self.conversation_history.append(
                {
                    "timestamp": now,
                    "user_message": user_message,
                    "assistant_response": response_text,
                    "agents_used": used_agents,
                    "role": role,
                    "session_id": session_id,
                }
            )

            logger.info(
                "ADK chat completed: session=%s agents=%s",
                session_id,
                used_agents,
            )

            return {
                "response": response_text,
                "timestamp": now,
                "status": "success",
                "used_agents": used_agents,
                "metadata": {
                    "status": "success",
                    "model": self.model_name,
                    "conversation_id": session_id,
                    "usage": usage_meta,
                    "framework": "google-adk",
                    "adk_agent": incident_management_orchestrator.name,
                },
            }

        except Exception as exc:
            error_str = str(exc)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                logger.warning(
                    "Gemini quota exhausted (429) — falling back to mock response: %s",
                    error_str[:200],
                )
                # ── Mock fallback on quota exhaustion ─────────────────────
                severity = (
                    (incident_context.get("severity") or "").upper()
                    if incident_context
                    else ""
                )
                if severity in _CRITICAL_SEVERITIES:
                    response_text = (
                        "⚠️ [Quota Fallback] Critical incident detected.\n\n"
                        "Recommended immediate actions:\n"
                        "1) Isolate the affected service\n"
                        "2) Restart failing pods / instances\n"
                        "3) Notify the on-call engineer\n\n"
                        "Suggested remedy: Roll back the most recent deployment "
                        "and investigate database connection pool exhaustion."
                    )
                    used_agents = ["incident_analyzer", "remedy_identifier"]
                else:
                    response_text = (
                        "ℹ️ [Quota Fallback] Analysis complete.\n\n"
                        "No critical indicators found. "
                        "Suggested remediation: follow the standard runbook "
                        "and monitor for further degradation."
                    )
                    used_agents = ["incident_analyzer"]

                now = datetime.now(timezone.utc).isoformat()
                self.conversation_history.append({
                    "timestamp": now,
                    "user_message": user_message,
                    "assistant_response": response_text,
                    "agents_used": used_agents,
                    "role": role,
                    "session_id": session_id,
                })
                return {
                    "response": response_text,
                    "timestamp": now,
                    "status": "success",
                    "used_agents": used_agents,
                    "metadata": {
                        "status": "success",
                        "model": "MOCK_FALLBACK",
                        "conversation_id": session_id,
                        "usage": {"prompt_token_count": 0, "total_token_count": 0},
                        "framework": "mock-quota-fallback",
                    },
                }
                # ──────────────────────────────────────────────────────────
            else:
                logger.exception("Error in ADK chat processing")
                return {
                    "response": f"Error processing request: {exc}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "error",
                    "error": error_str,
                    "metadata": {"status": "error", "framework": "google-adk"},
                }

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    async def _ensure_session(self, user_id: str, session_id: str) -> None:
        """Create the ADK session if it does not already exist."""
        try:
            existing = await self.session_service.get_session(
                app_name=APP_NAME,
                user_id=user_id,
                session_id=session_id,
            )
            if existing is None:
                raise ValueError("session not found")
        except Exception:
            await self.session_service.create_session(
                app_name=APP_NAME,
                user_id=user_id,
                session_id=session_id,
            )

    # ------------------------------------------------------------------
    # Message construction
    # ------------------------------------------------------------------

    def _build_message(
        self,
        user_message: str,
        context_summary: str,
        incident_context: Optional[Dict[str, Any]],
        role: Optional[str],
    ) -> str:
        """Enrich the raw user message with role and incident context."""
        parts = []

        role_label = (role or "").upper()
        if role_label:
            parts.append(f"[Caller Role: {role_label}]")

        parts.append(f"[Context: {context_summary}]")

        if incident_context:
            incident_lines = ["\n[Incident Details]"]
            for key, value in incident_context.items():
                if value not in (None, "", []):
                    incident_lines.append(f"  {key}: {value}")
            parts.append("\n".join(incident_lines))

        parts.append(f"\n{user_message}")
        return "\n".join(parts)

    async def _build_context(
        self, incident_context: Optional[Dict[str, Any]]
    ) -> str:
        if not incident_context:
            try:
                incidents = await self.dynatrace_client.get_incidents(
                    status="open", hours_back=24, limit=5
                )
                return f"Current open incidents: {len(incidents)} total"
            except Exception as exc:
                logger.error("Error fetching incidents for context: %s", exc)
                return "No incident context available"

        title = incident_context.get("title", "Unknown")
        severity = (incident_context.get("severity") or "Unknown").upper()
        status = (incident_context.get("status") or "Unknown").upper()
        return f"Incident: {title} | Severity: {severity} | Status: {status}"

    # ------------------------------------------------------------------
    # Usage metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_usage(event: Any) -> Dict[str, Any]:
        meta = getattr(event, "usage_metadata", None)
        if meta is None:
            return {}
        return {
            "prompt_token_count": getattr(meta, "prompt_token_count", None),
            "candidates_token_count": getattr(meta, "candidates_token_count", None),
            "total_token_count": getattr(meta, "total_token_count", None),
        }

    # ------------------------------------------------------------------
    # Conversation management
    # ------------------------------------------------------------------

    def get_conversation_history(self) -> List[Dict[str, Any]]:
        return self.conversation_history

    def clear_conversation_history(self) -> None:
        self.conversation_history = []
        logger.info("Conversation history cleared")

    async def close(self) -> None:
        if self.dynatrace_client:
            await self.dynatrace_client.close()
