"""
Mock incident data for end-to-end testing of dashboard, ADK agents, and Slack flows.

Use these with the server endpoints (recommended: set USE_MOCK_RESPONSES=true
in server/.env and restart the server to avoid external Dynatrace/Gemini calls).
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Dict, List, Optional

critical_incident = {
    "id": "INC-0001",
    "title": "Payment processing failures — high error rate",
    "description": (
        "Multiple customers reporting payment failures. Error rate spiked from 0.1% to 12% "
        "after the recent deployment. Transaction service returning 502s."
    ),
    "affectedEntity": "service-payment-gateway",
    "severity": "CRITICAL",
    "status": "OPEN",
    "startTime": "1781052000000",  # mock epoch-ms
    "duration": "00:42:00",
    "errorRate": 12.4,
    "p99LatencyMs": 4820,
    "baselineLatencyMs": 320,
    "impactedUsers": 14230,
    "impactedServices": [
        "checkout-service",
        "payment-gateway",
        "fraud-detection",
        "order-management",
    ],
    "impact": (
        "Checkout payments are failing for a large customer segment. Payment retries "
        "are increasing load on payment-gateway and payments-db-primary."
    ),
    "url": "https://dynatrace.example/incidents/INC-0001",
}

minor_incident = {
    "id": "INC-0002",
    "title": "Increased latency on search API",
    "description": (
        "Search requests have a 20% P95 latency increase over the last hour. No errors observed, "
        "but customer-facing latency is degraded."
    ),
    "affectedEntity": "service-search",
    "severity": "MEDIUM",
    "status": "OPEN",
    "startTime": "1781055600000",
    "duration": "00:15:00",
    "errorRate": 0.0,
    "p99LatencyMs": 1180,
    "baselineLatencyMs": 850,
    "impactedUsers": 430,
    "impactedServices": ["search-api", "catalog-service"],
    "impact": (
        "Search results remain available, but customers are seeing slower response "
        "times on product discovery flows."
    ),
    "url": "https://dynatrace.example/incidents/INC-0002",
}


mock_services = [
    {
        "id": "service-payment-gateway",
        "name": "payment-gateway",
        "health": "unhealthy",
        "errorRate": 12.4,
        "responseTime": 4820,
        "throughput": 820,
        "lastUpdate": "2026-06-10T08:42:00Z",
    },
    {
        "id": "service-checkout",
        "name": "checkout-service",
        "health": "degraded",
        "errorRate": 8.1,
        "responseTime": 2360,
        "throughput": 1480,
        "lastUpdate": "2026-06-10T08:42:00Z",
    },
    {
        "id": "service-search",
        "name": "search-api",
        "health": "degraded",
        "errorRate": 0.0,
        "responseTime": 1180,
        "throughput": 3200,
        "lastUpdate": "2026-06-10T09:15:00Z",
    },
    {
        "id": "service-catalog",
        "name": "catalog-service",
        "health": "healthy",
        "errorRate": 0.2,
        "responseTime": 410,
        "throughput": 5100,
        "lastUpdate": "2026-06-10T09:15:00Z",
    },
]

mock_slos = [
    {
        "id": "slo-checkout-availability",
        "name": "Checkout availability",
        "target": 99.9,
        "current": 97.8,
        "status": "violated",
        "trend": "down",
    },
    {
        "id": "slo-payment-latency",
        "name": "Payment p95 latency",
        "target": 99.0,
        "current": 98.2,
        "status": "at-risk",
        "trend": "down",
    },
    {
        "id": "slo-search-latency",
        "name": "Search p95 latency",
        "target": 99.5,
        "current": 99.1,
        "status": "at-risk",
        "trend": "stable",
    },
]

mock_events_by_incident = {
    "INC-0001": [
        {
            "timestamp": "2026-06-10T08:14:02Z",
            "title": "Error-rate anomaly detected on payment-gateway",
            "severity": "CRITICAL",
        },
        {
            "timestamp": "2026-06-10T08:14:18Z",
            "title": "P99 latency breached 4.8 seconds",
            "severity": "HIGH",
        },
        {
            "timestamp": "2026-06-10T08:15:44Z",
            "title": "payments-db-primary connection pool exhausted",
            "severity": "CRITICAL",
        },
        {
            "timestamp": "2026-06-10T08:20:00Z",
            "title": "L1 escalation opened for payment-gateway",
            "severity": "INFO",
        },
        {
            "timestamp": "2026-06-10T08:27:00Z",
            "title": "Deployment v3.8.2 correlated with connection leak",
            "severity": "HIGH",
        },
    ],
    "INC-0002": [
        {
            "timestamp": "2026-06-10T09:00:00Z",
            "title": "Search API p95 latency increased by 20 percent",
            "severity": "MEDIUM",
        },
        {
            "timestamp": "2026-06-10T09:08:30Z",
            "title": "Catalog dependency showing elevated queue time",
            "severity": "MEDIUM",
        },
        {
            "timestamp": "2026-06-10T09:15:00Z",
            "title": "Cache hit rate recovered after warming cycle",
            "severity": "INFO",
        },
    ],
}

MOCK_INCIDENTS = [critical_incident, minor_incident]


def _clone(value: Any) -> Any:
    return copy.deepcopy(value)


def get_mock_incident(incident_id: Optional[str]) -> Dict[str, Any]:
    """Return a copy of the requested mock incident, defaulting to the critical case."""
    for incident in MOCK_INCIDENTS:
        if incident.get("id") == incident_id:
            return _clone(incident)
    return _clone(critical_incident)


class MockDynatraceClient:
    """Small in-memory client matching the DynatraceClient methods used by tools."""

    async def close(self) -> None:
        return None

    async def get_incidents(
        self,
        status: str = "open",
        hours_back: int = 24,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        status_key = (status or "open").lower()
        incidents = _clone(MOCK_INCIDENTS)
        if status_key not in {"all", "*"}:
            incidents = [
                incident
                for incident in incidents
                if str(incident.get("status", "")).lower() == status_key
            ]
        return incidents[:limit]

    async def get_incident_details(self, problem_id: str) -> Dict[str, Any]:
        return get_mock_incident(problem_id)

    async def get_events(
        self,
        entity_id: str,
        hours_back: int = 24,
        event_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        return _clone(mock_events_by_incident.get(entity_id, mock_events_by_incident["INC-0001"]))

    async def get_metrics(
        self,
        metric_key: str,
        hours_back: int = 24,
        resolution: str = "1m",
    ) -> Dict[str, Any]:
        return {
            "metricKey": metric_key,
            "resolution": resolution,
            "series": [
                {"timestamp": "2026-06-10T08:10:00Z", "value": 0.4},
                {"timestamp": "2026-06-10T08:20:00Z", "value": 12.4},
                {"timestamp": "2026-06-10T08:30:00Z", "value": 9.7},
            ],
        }

    async def get_top_services(self, limit: int = 10) -> List[Dict[str, Any]]:
        return _clone(mock_services[:limit])

    async def get_service_details(self, service_id: str) -> Dict[str, Any]:
        for service in mock_services:
            if service.get("id") == service_id:
                return _clone(service)
        return _clone(mock_services[0])

    async def get_slo_status(self, slo_id: str) -> Dict[str, Any]:
        for slo in mock_slos:
            if slo.get("id") == slo_id:
                return _clone(slo)
        return _clone(mock_slos[0])

    async def get_slos(self) -> List[Dict[str, Any]]:
        return _clone(mock_slos)

    async def create_event(
        self,
        entity_id: str,
        event_type: str,
        title: str,
        description: str,
        severity: str = "INFORMATIONAL",
    ) -> bool:
        mock_events_by_incident.setdefault(entity_id, []).append(
            {
                "timestamp": datetime.utcnow().isoformat(),
                "title": title,
                "severity": severity,
                "eventType": event_type,
                "description": description,
            }
        )
        return True
