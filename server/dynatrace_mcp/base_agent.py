"""
ADK-based base agent wrapper.

Provides a thin compatibility shim that runs a single ADK LlmAgent
programmatically — used by the specialized agent classes in agents.py.
"""

import logging
import os
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite")
_APP_NAME = "dynatrace_base_agent"


class BaseADKAgent(ABC):
    """
    Base class for ADK-powered specialist agents.

    Subclasses define `agent_name`, `agent_description`, and `agent_instruction`,
    then implement `process()`. The `analyze()` coroutine handles the full
    ADK Runner lifecycle (session creation → run → extract final response).
    """

    agent_name: str = "base_agent"
    agent_description: str = "Base ADK agent"
    agent_instruction: str = "You are a helpful assistant."
    model: str = DEFAULT_MODEL

    def __init__(self):
        self._llm_agent = LlmAgent(
            name=self.agent_name,
            model=self.model,
            description=self.agent_description,
            instruction=self.agent_instruction,
        )
        self._session_service = InMemorySessionService()
        self._runner = Runner(
            agent=self._llm_agent,
            app_name=_APP_NAME,
            session_service=self._session_service,
        )

    async def analyze(
        self, prompt: str, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Send `prompt` (optionally enriched with `context`) to the ADK agent
        and return the response as a dict with 'text', 'status', and 'metadata'.
        """
        full_prompt = prompt
        if context:
            full_prompt += f"\n\nContext:\n{self._format_context(context)}"

        user_id = "agent_user"
        session_id = f"run_{uuid.uuid4().hex[:10]}"

        await self._session_service.create_session(
            app_name=_APP_NAME,
            user_id=user_id,
            session_id=session_id,
        )

        content = types.Content(
            role="user",
            parts=[types.Part(text=full_prompt)],
        )

        response_text = ""
        usage_meta: Dict[str, Any] = {}

        try:
            async for event in self._runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=content,
            ):
                if getattr(event, "usage_metadata", None) and not usage_meta:
                    usage_meta = self._extract_usage(event)

                if event.is_final_response():
                    try:
                        if event.content and event.content.parts:
                            response_text = event.content.parts[0].text or ""
                    except (AttributeError, IndexError):
                        response_text = ""

            logger.info(
                "ADK agent %s completed: tokens=%s",
                self.agent_name,
                usage_meta.get("total_token_count"),
            )

            return {
                "status": "success",
                "text": response_text,
                "metadata": {
                    "status": "success",
                    "model": self.model,
                    "usage": usage_meta,
                },
            }

        except Exception as exc:
            error_str = str(exc)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                logger.warning(
                    "Gemini quota exhausted (429) in ADK agent %s", self.agent_name
                )
            else:
                logger.exception(
                    "Error in ADK agent %s", self.agent_name
                )
            return {
                "status": "error",
                "text": "",
                "error": error_str,
                "metadata": {"status": "error", "model": self.model, "usage": {}},
            }

    async def generate_summary(self, data: Dict[str, Any]) -> str:
        """Generate a concise summary of the provided data."""
        context_text = self._format_context(data)
        prompt = f"Provide a concise summary of the following data:\n{context_text}"
        result = await self.analyze(prompt)
        return result.get("text", "")

    @staticmethod
    def _format_context(data: Dict[str, Any]) -> str:
        lines = []
        for key, value in data.items():
            if isinstance(value, (list, dict)):
                lines.append(f"{key}:\n{value}")
            else:
                lines.append(f"{key}: {value}")
        return "\n".join(lines)

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

    @abstractmethod
    async def process(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Process data and return results. Must be implemented by subclasses."""
        pass
