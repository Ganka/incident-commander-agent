"""
Slack Notifier — async webhook-based Slack notifications.

Two notification types:
  1. Critical incident alert  — fired when a CRITICAL/HIGH incident is detected.
  2. Remedy identified alert  — fired when an L1/L2/L3 analysis produces a remedy.

Configure via .env:
  SLACK_WEBHOOK_URL   — Incoming Webhook URL from Slack App settings (required)
  SLACK_CHANNEL       — Override channel, e.g. #incidents  (optional)
  SLACK_NOTIFY_LEVELS — Comma-separated severity levels to alert on (default: CRITICAL)
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

_SEVERITY_COLORS = {
    "CRITICAL": "#FF0000",
    "HIGH":     "#FF6600",
    "MEDIUM":   "#FFC107",
    "LOW":      "#36A64F",
}

_ROLE_EMOJI = {"L1": "🟢", "L2": "🔵", "L3": "🟣"}


class SlackNotifier:
    """Sends formatted messages to a Slack channel via Incoming Webhook."""

    def __init__(self):
        self.webhook_url: Optional[str] = os.getenv("SLACK_WEBHOOK_URL")
        self.channel: Optional[str] = os.getenv("SLACK_CHANNEL")
        notify_levels = os.getenv("SLACK_NOTIFY_LEVELS", "CRITICAL")
        self.notify_levels: List[str] = [
            lvl.strip().upper() for lvl in notify_levels.split(",")
        ]
        self._enabled = bool(self.webhook_url)
        # When USE_MOCK_RESPONSES=true, do not perform network calls — just log
        self._mock = os.getenv("USE_MOCK_RESPONSES", "false").lower() == "true"
        if not (self._enabled or self._mock):
            logger.warning(
                "SLACK_WEBHOOK_URL not set and mock mode disabled — Slack notifications are disabled."
            )

    @property
    def enabled(self) -> bool:
        # Enabled if webhook is configured OR mock mode is active
        return bool(self._enabled or self._mock)

    # ------------------------------------------------------------------
    # Public notification methods
    # ------------------------------------------------------------------

    async def notify_critical_incident(
        self, incident: Dict[str, Any]
    ) -> bool:
        """
        Post a critical-incident alert to Slack.

        Args:
            incident: Dynatrace incident dict with title, severity, status, etc.

        Returns:
            True if the message was sent successfully.
        """
        if not self.enabled:
            return False

        severity = (incident.get("severity") or "UNKNOWN").upper()
        if severity not in self.notify_levels:
            return False

        color = _SEVERITY_COLORS.get(severity, "#888888")
        title = incident.get("title", "Unknown Incident")
        status = (incident.get("status") or "UNKNOWN").upper()
        entity = incident.get("affectedEntity") or incident.get("entityId", "N/A")
        incident_id = incident.get("id", "N/A")
        start_time = incident.get("startTime", "")
        duration = incident.get("duration", "Ongoing")
        error_rate = incident.get("errorRate")

        try:
            ts = (
                datetime.fromtimestamp(int(start_time) / 1000, tz=timezone.utc)
                .strftime("%Y-%m-%d %H:%M UTC")
                if start_time
                else "Unknown"
            )
        except (TypeError, ValueError):
            ts = str(start_time) if start_time else "Unknown"

        fields = [
            {"title": "Severity",       "value": f"*{severity}*",  "short": True},
            {"title": "Status",         "value": status,            "short": True},
            {"title": "Affected Entity","value": entity,            "short": True},
            {"title": "Duration",       "value": str(duration),    "short": True},
            {"title": "Started",        "value": ts,               "short": True},
        ]
        if error_rate is not None:
            fields.append({"title": "Error Rate", "value": f"{error_rate}%", "short": True})

        payload = self._build_payload(
            text=f"🚨 *CRITICAL INCIDENT DETECTED* — {title}",
            color=color,
            title=title,
            title_link=incident.get("url"),
            fields=fields,
            footer=f"Incident ID: {incident_id} • Dynatrace MCP",
        )
        return await self._post(payload)

    async def notify_remedy_identified(
        self,
        incident: Dict[str, Any],
        remedy_summary: str,
        role: Optional[str],
        agents_used: List[str],
    ) -> bool:
        """
        Post a remedy-identified notification to Slack.

        Args:
            incident:       Incident context dict.
            remedy_summary: First ~400 chars of the AI-generated remedy text.
            role:           Caller role (L1/L2/L3).
            agents_used:    List of ADK agent names that contributed.

        Returns:
            True if the message was sent successfully.
        """
        if not self.enabled:
            return False

        severity = (incident.get("severity") or "UNKNOWN").upper()
        color = _SEVERITY_COLORS.get(severity, "#36A64F")
        title = incident.get("title", "Unknown Incident")
        role_label = (role or "General").upper()
        role_emoji = _ROLE_EMOJI.get(role_label, "👤")
        agents_str = ", ".join(agents_used) if agents_used else "orchestrator"

        snippet = remedy_summary[:400].strip()
        if len(remedy_summary) > 400:
            snippet += "…"

        fields = [
            {"title": "Incident",       "value": title,         "short": True},
            {"title": "Severity",       "value": severity,      "short": True},
            {"title": "Analysed By",    "value": role_label,    "short": True},
            {"title": "Agents Used",    "value": agents_str,    "short": True},
            {"title": "Remedy Preview", "value": snippet,       "short": False},
        ]

        payload = self._build_payload(
            text=f"{role_emoji} *REMEDY IDENTIFIED* for {title} ({role_label})",
            color=color,
            title=f"Remedy: {title}",
            fields=fields,
            footer="Dynatrace MCP — AI Incident Analysis",
        )
        return await self._post(payload)

    async def notify_custom(self, text: str, color: str = "#36A64F") -> bool:
        """Post a plain text message to Slack."""
        if not self.enabled:
            return False
        payload = {"text": text, "attachments": [{"color": color, "text": text}]}
        if self.channel:
            payload["channel"] = self.channel
        return await self._post(payload)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_payload(
        self,
        text: str,
        color: str,
        title: str,
        fields: List[Dict[str, Any]],
        title_link: Optional[str] = None,
        footer: str = "Dynatrace MCP",
    ) -> Dict[str, Any]:
        attachment: Dict[str, Any] = {
            "color": color,
            "title": title,
            "fields": fields,
            "footer": footer,
            "ts": int(datetime.now(timezone.utc).timestamp()),
        }
        if title_link:
            attachment["title_link"] = title_link

        payload: Dict[str, Any] = {"text": text, "attachments": [attachment]}
        if self.channel:
            payload["channel"] = self.channel
        return payload

    async def _post(self, payload: Dict[str, Any]) -> bool:
        """POST the payload to the Slack Incoming Webhook."""
        # If mock mode is enabled, log the payload and return success
        if self._mock:
            logger.info("MOCK Slack payload (not sent): %s", payload)
            return True

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        logger.info("Slack notification sent successfully.")
                        return True
                    body = await resp.text()
                    logger.error(
                        "Slack webhook returned %s: %s", resp.status, body
                    )
                    return False
        except Exception as exc:
            logger.error("Failed to send Slack notification: %s", exc)
            return False
