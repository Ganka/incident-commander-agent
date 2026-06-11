"""
Dynatrace API Client for MCP Server
"""

import os
import logging
import aiohttp
import httpx
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta
import json

logger = logging.getLogger(__name__)


_PROBLEM_STATUS_SELECTOR = {
    "open": 'status("open")',
    "resolved": 'status("closed")',
    "closed": 'status("closed")',
}

_PROBLEM_SEVERITY_TO_DASHBOARD = {
    "AVAILABILITY": "critical",
    "ERROR": "high",
    "RESOURCE_CONTENTION": "high",
    "MONITORING_UNAVAILABLE": "high",
    "CUSTOM_ALERT": "medium",
    "PERFORMANCE": "medium",
}


class DynatraceClient:
    """Client for Dynatrace API interactions"""

    def __init__(
        self,
        environment_id: Optional[str] = None,
        api_token: Optional[str] = None,
        api_url: Optional[str] = None,
    ):
        self.environment_id = environment_id or os.getenv("DYNATRACE_ENVIRONMENT_ID")
        self.api_token = api_token or os.getenv("DYNATRACE_API_TOKEN")
        self.api_url = api_url or os.getenv("DYNATRACE_API_URL")

        if not all([self.environment_id, self.api_token, self.api_url]):
            raise ValueError(
                "Missing required Dynatrace configuration: "
                "environment_id, api_token, api_url"
            )

        self.api_url = self.api_url.rstrip('/')
        if not self.api_url.endswith('/api/v2'):
            self.api_url = f"{self.api_url}/api/v2"

        self.headers = {
            "Authorization": f"Api-Token {self.api_token}",
            "Content-Type": "application/json",
        }
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def close(self):
        """Close any persistent session/resources."""
        if self.session:
            await self.session.close()

    async def get_incidents(
        self,
        status: str = "open",
        hours_back: int = 24,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Fetch incidents from Dynatrace
        
        Args:
            status: 'open', 'resolved', or 'all'
            hours_back: Number of hours to look back
            limit: Maximum number of incidents to return
        """
        try:
            endpoint = f"{self.api_url}/problems"
            params = {
                "problemSelector": self._build_problem_selector(status),
                "from": int((datetime.utcnow() - timedelta(hours=hours_back)).timestamp() * 1000),
                "to": int(datetime.utcnow().timestamp() * 1000),
                "pageSize": limit,
            }
            params = {k: v for k, v in params.items() if v is not None}

            logger.info("Dynatrace get_incidents request %s params=%s", endpoint, params)
            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, headers=self.headers, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        problems = data.get("problems", [])
                        logger.info("Dynatrace get_incidents response count=%s", len(problems))
                        return [self._normalize_problem(problem) for problem in problems]
                    else:
                        text = await resp.text()
                        logger.error("Error fetching incidents: %s %s", resp.status, text)
                        return []
        except Exception as e:
            logger.error(f"Exception fetching incidents: {e}")
            return []

    async def get_incident_details(self, problem_id: str) -> Dict[str, Any]:
        """Get detailed information about a specific incident"""
        try:
            endpoint = f"{self.api_url}/problems/{problem_id}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, headers=self.headers) as resp:
                    if resp.status == 200:
                        return self._normalize_problem(await resp.json())
                    else:
                        logger.error(f"Error fetching incident details: {resp.status}")
                        return {}
        except Exception as e:
            logger.error(f"Exception fetching incident details: {e}")
            return {}

    async def get_events(
        self,
        entity_id: str,
        hours_back: int = 24,
        event_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Get events for a specific entity"""
        try:
            endpoint = f"{self.api_url}/events"
            start_time = int((datetime.utcnow() - timedelta(hours=hours_back)).timestamp() * 1000)
            end_time = int(datetime.utcnow().timestamp() * 1000)
            
            params = {
                "entityId": entity_id,
                "from": start_time,
                "to": end_time,
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, headers=self.headers, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("events", [])
                    else:
                        logger.error(f"Error fetching events: {resp.status}")
                        return []
        except Exception as e:
            logger.error(f"Exception fetching events: {e}")
            return []

    async def get_metrics(
        self,
        metric_key: str,
        hours_back: int = 24,
        resolution: str = "1m",
    ) -> Dict[str, Any]:
        """Get metric data from Dynatrace"""
        try:
            endpoint = f"{self.api_url}/metrics/query"
            start_time = int((datetime.utcnow() - timedelta(hours=hours_back)).timestamp() * 1000)
            end_time = int(datetime.utcnow().timestamp() * 1000)
            
            params = {
                "metricKey": metric_key,
                "from": start_time,
                "to": end_time,
                "resolution": resolution,
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, headers=self.headers, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        logger.error(f"Error fetching metrics: {resp.status}")
                        return {}
        except Exception as e:
            logger.error(f"Exception fetching metrics: {e}")
            return {}

    async def get_top_services(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get top services by error rate"""
        try:
            endpoint = f"{self.api_url}/entities"
            params = {
                "entitySelector": "type(SERVICE)",
                "pageSize": limit,
                "sort": "-healthStatus",
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, headers=self.headers, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return [self._normalize_service(entity) for entity in data.get("entities", [])]
                    else:
                        logger.error(f"Error fetching services: {resp.status}")
                        return []
        except Exception as e:
            logger.error(f"Exception fetching services: {e}")
            return []

    async def get_service_details(self, service_id: str) -> Dict[str, Any]:
        """Get detailed information about a service"""
        try:
            endpoint = f"{self.api_url}/entities/{service_id}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, headers=self.headers) as resp:
                    if resp.status == 200:
                        return self._normalize_service(await resp.json())
                    else:
                        logger.error(f"Error fetching service details: {resp.status}")
                        return {}
        except Exception as e:
            logger.error(f"Exception fetching service details: {e}")
            return {}

    async def get_slo_status(self, slo_id: str) -> Dict[str, Any]:
        """Get SLO status information"""
        try:
            endpoint = f"{self.api_url}/slo/{slo_id}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, headers=self.headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        logger.error(f"Error fetching SLO: {resp.status}")
                        return {}
        except Exception as e:
            logger.error(f"Exception fetching SLO: {e}")
            return {}

    async def get_slos(self) -> List[Dict[str, Any]]:
        """Get all SLOs"""
        try:
            endpoint = f"{self.api_url}/slo"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, headers=self.headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("slos", [])
                    else:
                        logger.error(f"Error fetching SLOs: {resp.status}")
                        return []
        except Exception as e:
            logger.error(f"Exception fetching SLOs: {e}")
            return []

    async def create_event(
        self,
        entity_id: str,
        event_type: str,
        title: str,
        description: str,
        severity: str = "INFORMATIONAL",
    ) -> bool:
        """Create a custom event in Dynatrace"""
        try:
            endpoint = f"{self.api_url}/events"
            payload = {
                "entityId": entity_id,
                "eventType": event_type,
                "title": title,
                "description": description,
                "severity": severity,
                "timestamp": int(datetime.utcnow().timestamp() * 1000),
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint, headers=self.headers, json=payload
                ) as resp:
                    return resp.status == 201
        except Exception as e:
            logger.error(f"Exception creating event: {e}")
            return False

    @staticmethod
    def _build_problem_selector(status: str) -> Optional[str]:
        status_key = (status or "open").strip().lower()
        if status_key == "all":
            return None
        return _PROBLEM_STATUS_SELECTOR.get(status_key)

    def _normalize_problem(self, problem: Dict[str, Any]) -> Dict[str, Any]:
        """Convert Dynatrace problem payloads into the dashboard incident shape."""
        if not problem:
            return {}

        problem_id = problem.get("problemId") or problem.get("id")
        display_id = problem.get("displayId") or problem_id
        start_ms = self._coerce_millis(problem.get("startTime"))
        end_ms = self._coerce_millis(problem.get("endTime"))
        entity = self._first_entity(problem)
        severity_level = problem.get("severityLevel") or problem.get("severity")
        status = self._normalize_problem_status(problem.get("status"))
        impacted_entities = problem.get("impactedEntities") or problem.get("affectedEntities") or []
        impact_level = problem.get("impactLevel") or "UNKNOWN"
        affected_name = entity.get("name") or "Unknown entity"
        title = problem.get("title") or "Dynatrace problem"

        normalized = {
            "id": problem_id,
            "problemId": problem_id,
            "displayId": display_id,
            "title": title,
            "description": problem.get("description") or self._problem_description(problem),
            "affectedEntity": affected_name,
            "affectedEntityId": self._entity_id(entity),
            "severity": self._normalize_problem_severity(severity_level, impact_level),
            "severityLevel": severity_level,
            "status": status,
            "startTime": self._iso_from_millis(start_ms),
            "startTimeMs": start_ms,
            "endTimeMs": end_ms,
            "duration": self._format_duration(start_ms, end_ms),
            "errorRate": float(problem.get("errorRate") or 0),
            "impact": self._problem_impact(impact_level, impacted_entities),
            "impactLevel": impact_level,
            "raw": problem,
        }

        problem_url = problem.get("url")
        if problem_url:
            normalized["url"] = problem_url

        return normalized

    @staticmethod
    def _normalize_problem_status(status: Any) -> str:
        status_value = str(status or "open").strip().upper()
        if status_value in {"CLOSED", "RESOLVED"}:
            return "resolved"
        if status_value in {"ACKNOWLEDGED", "ACKNOWLEDGED_OPEN"}:
            return "acknowledged"
        return "open"

    @staticmethod
    def _normalize_problem_severity(severity_level: Any, impact_level: Any) -> str:
        severity = str(severity_level or "").strip().upper()
        if severity in _PROBLEM_SEVERITY_TO_DASHBOARD:
            return _PROBLEM_SEVERITY_TO_DASHBOARD[severity]

        impact = str(impact_level or "").strip().upper()
        if impact in {"APPLICATION", "SERVICE"}:
            return "high"
        if impact in {"INFRASTRUCTURE", "ENVIRONMENT"}:
            return "medium"
        return "low"

    @staticmethod
    def _first_entity(problem: Dict[str, Any]) -> Dict[str, Any]:
        for key in ("affectedEntities", "impactedEntities"):
            entities = problem.get(key)
            if entities:
                return entities[0] or {}
        root_cause = problem.get("rootCauseEntity")
        if root_cause:
            return root_cause
        return {}

    @staticmethod
    def _entity_id(entity: Dict[str, Any]) -> Optional[str]:
        entity_id = entity.get("entityId")
        if isinstance(entity_id, dict):
            return entity_id.get("id")
        if isinstance(entity_id, str):
            return entity_id
        return entity.get("id")

    @staticmethod
    def _coerce_millis(value: Any) -> Optional[int]:
        if value in (None, "", -1, "-1"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _iso_from_millis(value: Optional[int]) -> str:
        if value is None:
            return datetime.utcnow().isoformat()
        return datetime.utcfromtimestamp(value / 1000).isoformat()

    @classmethod
    def _format_duration(cls, start_ms: Optional[int], end_ms: Optional[int]) -> str:
        if start_ms is None:
            return "Unknown"

        effective_end = end_ms or int(datetime.utcnow().timestamp() * 1000)
        total_seconds = max(0, int((effective_end - start_ms) / 1000))
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    @staticmethod
    def _problem_description(problem: Dict[str, Any]) -> str:
        display_id = problem.get("displayId") or problem.get("problemId") or "Problem"
        impact_level = str(problem.get("impactLevel") or "unknown").lower()
        severity_level = str(problem.get("severityLevel") or "unknown").lower()
        return f"{display_id}: {impact_level} impact with {severity_level} severity."

    @staticmethod
    def _problem_impact(impact_level: Any, impacted_entities: List[Dict[str, Any]]) -> str:
        entity_count = len(impacted_entities)
        label = str(impact_level or "unknown").lower().replace("_", " ")
        if entity_count == 1:
            entity_name = impacted_entities[0].get("name", "one entity")
            return f"{label.title()} impact affecting {entity_name}."
        if entity_count > 1:
            return f"{label.title()} impact affecting {entity_count} entities."
        return f"{label.title()} impact reported by Dynatrace."

    @staticmethod
    def _normalize_service(entity: Dict[str, Any]) -> Dict[str, Any]:
        entity_id = entity.get("entityId") or entity.get("id")
        display_name = entity.get("displayName") or entity.get("name") or entity_id or "Unknown service"
        health_status = str(entity.get("healthStatus") or "HEALTHY").upper()
        health = {
            "HEALTHY": "healthy",
            "UNHEALTHY": "unhealthy",
            "DEGRADED": "degraded",
        }.get(health_status, "healthy")

        return {
            "id": entity_id,
            "name": display_name,
            "health": health,
            "errorRate": float(entity.get("errorRate") or 0),
            "responseTime": int(entity.get("responseTime") or 0),
            "throughput": int(entity.get("throughput") or 0),
            "lastUpdate": datetime.utcnow().isoformat(),
            "raw": entity,
        }
