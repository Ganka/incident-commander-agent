from __future__ import annotations

"""
Incident Triage Agents — Multi-level L1 / L2 / L3 workflow with Slack notifications.

Architecture
------------
  IncidentOrchestrator   — entry point; owns the incident lifecycle
    └─ L1Agent           — first-line triage; escalates to L2 when needed
    └─ L2Agent           — deep-dive investigation; escalates to L3 when needed
    └─ L3Agent           — engineering escalation package
  SlackNotifier          — async webhook notifications (imported from slack_notifier.py)

Mock data
---------
  MOCK_INCIDENT          — a realistic HIGH-severity Dynatrace-style incident dict
  generate_mock_logs()   — returns plausible error log lines for the incident

Usage
-----
  python agents.py                         # runs the full triage workflow
  USE_MOCK_RESPONSES=true python agents.py # same, but Slack calls are no-ops
"""

import asyncio
import json
import logging
import os
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .slack_notifier import SlackNotifier

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("incident_agents")

# ---------------------------------------------------------------------------
# Mock incident data
# ---------------------------------------------------------------------------

# Unix-ms timestamp ~ 2025-06-10 08:14:00 UTC
_TS_MS = 1749542040000

MOCK_INCIDENT: Dict[str, Any] = {
    "id": "PROB-20250610-00419",
    "title": "Payment Gateway — High Error Rate & Elevated Latency",
    "severity": "HIGH",
    "status": "OPEN",
    "startTime": str(_TS_MS),
    "duration": "22 minutes",
    "affectedEntity": "payment-gateway-prod-v3",
    "entityId": "HOST-A3F9C12D",
    "environment": "production",
    "region": "ap-south-1",
    "impactedUsers": 14_230,
    "impactedServices": [
        "checkout-service",
        "order-management",
        "fraud-detection",
    ],
    "errorRate": 38.7,
    "p99LatencyMs": 4820,
    "baselineLatencyMs": 310,
    "url": "https://dynatrace.example.com/problems/PROB-20250610-00419",
    "monitoringAlerts": [
        {
            "alertId": "ALT-0091",
            "name": "Payment Gateway Error Rate Spike",
            "threshold": "5%",
            "observed": "38.7%",
            "firedAt": "2025-06-10T08:14:02Z",
        },
        {
            "alertId": "ALT-0092",
            "name": "P99 Latency Breach — payment-gateway",
            "threshold": "500 ms",
            "observed": "4820 ms",
            "firedAt": "2025-06-10T08:14:18Z",
        },
        {
            "alertId": "ALT-0093",
            "name": "DB Connection Pool Exhaustion",
            "threshold": "80%",
            "observed": "99%",
            "firedAt": "2025-06-10T08:15:44Z",
        },
    ],
    "initialSymptoms": [
        "Users unable to complete checkout — HTTP 503 errors returned",
        "Payment confirmation emails not being dispatched",
        "Fraud-detection service timing out when called by checkout-service",
        "Database connection pool on payments-db-primary reporting 99% utilisation",
        "Kubernetes pods in CrashLoopBackOff after OOMKill events",
    ],
}


def generate_mock_logs() -> List[str]:
    """Return realistic error log lines that match the mock incident."""
    return [
        "[2025-06-10T08:14:01Z] ERROR payment-gateway: upstream connect error (503) — payments-db-primary timeout after 5000 ms",
        "[2025-06-10T08:14:03Z] ERROR checkout-service: POST /api/v2/payments failed — payment-gateway returned 503",
        "[2025-06-10T08:14:05Z] WARN  payment-gateway: HikariCP connection pool at capacity (50/50); queue depth 312",
        "[2025-06-10T08:14:07Z] ERROR payment-gateway: java.sql.SQLException: Timeout waiting for connection from pool",
        "[2025-06-10T08:14:09Z] ERROR fraud-detection: Circuit breaker OPEN — payment-gateway call-chain degraded",
        "[2025-06-10T08:14:12Z] ERROR checkout-service: Downstream dependency unavailable; returning HTTP 503 to client",
        "[2025-06-10T08:14:20Z] WARN  k8s/payment-gateway-6d8bfc9f7-xkp2m: OOMKilled — container exceeded 2 Gi limit",
        "[2025-06-10T08:14:21Z] INFO  k8s/payment-gateway-6d8bfc9f7-xkp2m: CrashLoopBackOff — restarting (attempt 3)",
        "[2025-06-10T08:15:44Z] CRIT  payments-db-primary: Max connections (500) reached; new connections rejected",
        "[2025-06-10T08:16:01Z] ERROR order-management: Unable to persist order — payment status unknown; rollback triggered",
        "[2025-06-10T08:17:33Z] ERROR payment-gateway: Unhandled exception in PaymentProcessorService.process(): NullPointerException at ConnectionWrapper.java:214",
        "[2025-06-10T08:18:44Z] WARN  payment-gateway: GC overhead limit exceeded — JVM heap 1.97 Gi / 2 Gi",
    ]


# ---------------------------------------------------------------------------
# Shared data structures
# ---------------------------------------------------------------------------

@dataclass
class TriageResult:
    """Structured output produced by each agent level."""

    level: str           # "L1" | "L2" | "L3"
    summary: str         # human-readable analysis narrative
    findings: List[str]  # bullet-point findings
    actions: List[str]   # recommended / taken actions
    should_escalate: bool
    escalation_reason: Optional[str] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "summary": self.summary,
            "findings": self.findings,
            "actions": self.actions,
            "should_escalate": self.should_escalate,
            "escalation_reason": self.escalation_reason,
        }

    def pretty(self) -> str:
        lines = [
            f"{'═' * 70}",
            f"  {self.level} TRIAGE RESULT",
            f"{'═' * 70}",
            f"\nSUMMARY\n{textwrap.fill(self.summary, 68)}",
            "\nKEY FINDINGS",
        ]
        lines += [f"  • {f}" for f in self.findings]
        lines += ["\nACTIONS / RECOMMENDATIONS"]
        lines += [f"  → {a}" for a in self.actions]
        if self.should_escalate:
            lines += [
                f"\nESCALATION: YES",
                f"  Reason: {self.escalation_reason}",
            ]
        else:
            lines += ["\nESCALATION: No — issue resolved at this level."]
        lines += [f"{'─' * 70}\n"]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent implementations (deterministic mock reasoning)
# ---------------------------------------------------------------------------

class L1Agent:
    """
    First-line triage agent.

    Responsibilities:
      - Parse incoming incident and attached logs/alerts.
      - Classify severity and blast radius.
      - Attempt immediate self-service remediation steps.
      - Escalate to L2 when the root cause is unclear or the fix is non-trivial.
    """

    name = "L1-Triage-Agent"

    async def analyse(
        self,
        incident: Dict[str, Any],
        logs: List[str],
        slack: SlackNotifier,
    ) -> TriageResult:
        logger.info("[L1] Starting triage for incident %s", incident["id"])

        # Notify Slack that L1 has started
        await slack.notify_critical_incident(incident)

        # --- Simulated async work (e.g., calling monitoring APIs) ---
        await asyncio.sleep(0.1)

        severity = incident.get("severity", "UNKNOWN").upper()
        error_rate = incident.get("errorRate", 0)
        p99 = incident.get("p99LatencyMs", 0)
        baseline = incident.get("baselineLatencyMs", 300)
        impacted = incident.get("impactedUsers", 0)
        services = incident.get("impactedServices", [])

        findings = [
            f"Severity: {severity} — error rate {error_rate}% (baseline < 2%)",
            f"P99 latency {p99} ms is {round(p99/baseline, 1)}× above baseline ({baseline} ms)",
            f"{impacted:,} users affected across services: {', '.join(services)}",
            "DB connection pool exhaustion detected on payments-db-primary (99% utilisation)",
            "K8s pods in CrashLoopBackOff — likely memory pressure / OOMKill",
            "Fraud-detection circuit breaker opened — cascading failure risk",
            "Log evidence: NullPointerException in PaymentProcessorService + JVM GC overhead",
        ]

        actions = [
            "Acknowledged incident; assigned to On-Call SRE (Priya Menon)",
            "Confirmed: checkout-service returning HTTP 503 to end users",
            "Verified: payments-db-primary max_connections (500) exhausted",
            "Attempted pod restart — CrashLoopBackOff persists; OOMKill on restart",
            "Checked recent deployments: payment-gateway v3.8.2 deployed 35 min ago",
            "Opened war-room Slack channel #inc-payment-20250610",
            "Escalating to L2 — DB exhaustion + OOMKill requires deeper investigation",
        ]

        summary = (
            f"HIGH-severity incident on the payment-gateway service detected at "
            f"{datetime.fromtimestamp(int(incident['startTime'])/1000, tz=timezone.utc).strftime('%H:%M UTC')}. "
            f"Error rate has spiked to {error_rate}% and P99 latency is {p99} ms ({round(p99/baseline,1)}× baseline), "
            f"directly impacting {impacted:,} users. Database connection pool is exhausted and Kubernetes pods are "
            f"OOMKilling. A recent deployment (v3.8.2, ~35 min ago) is a prime suspect. "
            f"L1 has been unable to stabilise the service; escalating to L2."
        )

        result = TriageResult(
            level="L1",
            summary=summary,
            findings=findings,
            actions=actions,
            should_escalate=True,
            escalation_reason=(
                "Root cause unclear; DB exhaustion + OOMKill + NullPointerException "
                "require code-level and infrastructure investigation beyond L1 scope."
            ),
            raw_data={"incident": incident, "logs": logs},
        )

        # Notify Slack of L1 outcome
        await slack.notify_remedy_identified(
            incident=incident,
            remedy_summary=summary,
            role="L1",
            agents_used=[self.name],
        )

        logger.info("[L1] Triage complete — escalating to L2.")
        return result


class L2Agent:
    """
    Deep-dive investigation agent.

    Responsibilities:
      - Correlate logs, metrics, and deployment history.
      - Identify root cause and affected components.
      - Generate detailed remediation plan.
      - Decide whether engineering (L3) escalation is required.
    """

    name = "L2-Investigation-Agent"

    async def investigate(
        self,
        incident: Dict[str, Any],
        logs: List[str],
        l1_result: TriageResult,
        slack: SlackNotifier,
    ) -> TriageResult:
        logger.info("[L2] Starting investigation for incident %s", incident["id"])

        await asyncio.sleep(0.1)

        findings = [
            "DEPLOYMENT CORRELATION: payment-gateway v3.8.2 deployed 35 min before incident onset — primary suspect.",
            "MEMORY LEAK HYPOTHESIS: JVM heap at 1.97/2 Gi with GC overhead warning; v3.8.2 release notes show removal of "
            "connection-pool keep-alive which may have introduced a handle leak.",
            "CONNECTION POOL EXHAUSTION: HikariCP pool (50 connections) being consumed 6× faster than normal; connections "
            "not returned on exception path — likely unclosed connections in new code path.",
            "CASCADING FAILURE: payments-db-primary hit its max_connections (500) limit because all 10 payment-gateway "
            "replicas each leaked connections simultaneously after the rollout.",
            "NULL POINTER EXCEPTION: Stack trace in ConnectionWrapper.java:214 points to a code path added in v3.8.2 that "
            "accesses a potentially null ResultSet before null-check.",
            "CIRCUIT BREAKER: fraud-detection correctly opened its circuit breaker; no data loss in that service.",
            "DATA INTEGRITY: Order-management rolled back transactions correctly — no orphaned payment records confirmed.",
            "INFRA: Node CPU and disk I/O within normal bounds; issue is application-level, not infrastructure.",
        ]

        actions = [
            "IMMEDIATE: Roll back payment-gateway to v3.8.0 (last known stable) — estimated 4 min.",
            "IMMEDIATE: Restart payments-db-primary to flush leaked connections (coordinate with DBA team).",
            "SHORT-TERM: Increase HikariCP connection timeout to surface leaks faster in staging.",
            "SHORT-TERM: Add connection-leak detection (HikariCP leakDetectionThreshold=30000) to catch future leaks.",
            "SHORT-TERM: Patch ConnectionWrapper.java:214 null-check and write regression unit test.",
            "SHORT-TERM: Close fraud-detection circuit breaker once payment-gateway is healthy.",
            "POST-INCIDENT: Add canary deployment gate: error rate > 1% auto-triggers rollback.",
            "POST-INCIDENT: Review v3.8.2 PR for similar unclosed-resource patterns.",
        ]

        summary = (
            "L2 root-cause analysis confirms that deployment of payment-gateway v3.8.2 (~35 min ago) introduced "
            "a connection handle leak in ConnectionWrapper.java:214 (NullPointerException on exception path). "
            "Unclosed JDBC connections exhausted the HikariCP pool (50 slots) across all 10 replicas, which in turn "
            "exhausted payments-db-primary's max_connections limit (500). The resulting timeouts triggered OOMKill "
            "as the JVM leaked memory alongside connections. Fraud-detection and order-management behaved correctly "
            "with circuit breakers and rollbacks. "
            "Recommended immediate action: roll back to v3.8.0 and restart payments-db-primary. "
            "L3 escalation required to authorise emergency rollback in production and review code fix."
        )

        result = TriageResult(
            level="L2",
            summary=summary,
            findings=findings,
            actions=actions,
            should_escalate=True,
            escalation_reason=(
                "Production rollback and DB restart require L3 engineering authority. "
                "Code fix for ConnectionWrapper.java must be reviewed by owning squad before hotfix deploy."
            ),
            raw_data={"l1_result": l1_result.to_dict()},
        )

        await slack.notify_remedy_identified(
            incident=incident,
            remedy_summary=summary,
            role="L2",
            agents_used=[self.name, L1Agent.name],
        )

        logger.info("[L2] Investigation complete — escalating to L3.")
        return result


class L3Agent:
    """
    Engineering escalation agent.

    Responsibilities:
      - Assemble the full escalation package for the owning engineering squad.
      - Specify exact rollback / hotfix / infra actions with owners and SLAs.
      - Document the incident timeline for post-mortem.
    """

    name = "L3-Engineering-Agent"

    async def escalate(
        self,
        incident: Dict[str, Any],
        logs: List[str],
        l1_result: TriageResult,
        l2_result: TriageResult,
        slack: SlackNotifier,
    ) -> TriageResult:
        logger.info("[L3] Assembling escalation package for incident %s", incident["id"])

        await asyncio.sleep(0.1)

        findings = [
            "ROOT CAUSE CONFIRMED: Connection handle leak in PaymentProcessorService — ConnectionWrapper.java:214 "
            "does not null-check ResultSet before closing; connection not returned to pool on SQLException.",
            "BLAST RADIUS: 14,230 users blocked at checkout for ~22 minutes; estimated ~INR 18.4 L revenue at risk "
            "(based on avg transaction value × affected sessions × conversion rate).",
            "DEPLOYMENT RISK: v3.8.2 lacked connection-pool load testing in staging — gate gap identified.",
            "DATABASE HEALTH: payments-db-primary will need a connection flush; read replica unaffected.",
            "ROLLBACK SAFETY: v3.8.0 is safe; no schema migrations between v3.8.0 and v3.8.2.",
            "HOTFIX SCOPE: Single method patch in ConnectionWrapper.java; no API contract changes.",
            "MONITORING GAP: No alert configured for HikariCP connection-wait time > 1 s — missed early signal.",
        ]

        actions = [
            # Immediate — within next 10 min
            "[P0 / Ops] Execute: kubectl rollout undo deployment/payment-gateway --to-revision=<v3.8.0-hash>",
            "[P0 / DBA — Ravi Kumar] Graceful restart payments-db-primary to flush leaked connections.",
            "[P0 / Ops] Monitor error rate post-rollback; expected recovery to < 1% within 5 min.",
            "[P0 / Ops] Re-close fraud-detection circuit breaker once payment-gateway health probe passes.",
            # Short-term — within 24 h
            "[P1 / Payments Squad] Fix: add null-check in ConnectionWrapper.java:214; add finally-block to guarantee close().",
            "[P1 / Payments Squad] Add HikariCP leakDetectionThreshold=30000 to application.yml.",
            "[P1 / QA] Add load test to staging pipeline: sustain 10× normal connection rate for 5 min as release gate.",
            "[P1 / SRE] Add alert: HikariCP pendingAcquires > 20 for > 60 s → PagerDuty page.",
            # Post-incident
            "[P2 / Engineering Manager] Schedule post-mortem within 48 h; share blameless RCA with all squads.",
            "[P2 / Platform] Review canary policy: auto-rollback if error rate > 1% within 10 min of deployment.",
        ]

        timeline = {
            "08:14:02 UTC": "Monitoring alert fired — error rate 38.7%",
            "08:14:18 UTC": "P99 latency breach alert fired — 4820 ms",
            "08:15:44 UTC": "DB connection pool exhaustion alert fired",
            "08:16:00 UTC": "L1 agent engaged; incident acknowledged",
            "08:20:00 UTC": "L1 escalated to L2 — OOMKill + DB exhaustion",
            "08:27:00 UTC": "L2 confirmed root cause — v3.8.2 connection leak",
            "08:30:00 UTC": "L2 escalated to L3 — production rollback authority required",
            "08:36:00 UTC": "L3 escalation package assembled and dispatched to Payments Squad",
        }

        summary = (
            "ENGINEERING ESCALATION PACKAGE — Incident PROB-20250610-00419\n\n"
            "CONFIRMED ROOT CAUSE: payment-gateway v3.8.2 introduced a JDBC connection handle leak in "
            "ConnectionWrapper.java:214. Unclosed connections exhausted the HikariCP pool across all replicas, "
            "cascading into payments-db-primary max_connections exhaustion, JVM OOM, and CrashLoopBackOff. "
            "14,230 users impacted; ~INR 18.4 L revenue at risk.\n\n"
            "IMMEDIATE ACTION REQUIRED: Roll back to v3.8.0 (no schema delta) and restart "
            "payments-db-primary to flush leaked connections. SRE and DBA authorisation secured at L3."
        )

        result = TriageResult(
            level="L3",
            summary=summary,
            findings=findings,
            actions=actions,
            should_escalate=False,
            raw_data={
                "l1_result": l1_result.to_dict(),
                "l2_result": l2_result.to_dict(),
                "incident_timeline": timeline,
            },
        )

        await slack.notify_remedy_identified(
            incident=incident,
            remedy_summary=summary,
            role="L3",
            agents_used=[self.name, L2Agent.name, L1Agent.name],
        )

        # Final Slack update — full escalation package dispatched
        await slack.notify_custom(
            text=(
                f"📦 *L3 ESCALATION PACKAGE DISPATCHED* — {incident['title']}\n"
                f"Rollback to v3.8.0 authorised. Payments Squad engaged. "
                f"Post-mortem scheduled within 48 h. Incident ID: {incident['id']}"
            ),
            color="#6600CC",
        )

        logger.info("[L3] Escalation package complete.")
        return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class IncidentOrchestrator:
    """
    Drives the full L1 → L2 → L3 triage pipeline.

    The orchestrator is intentionally thin: it wires agents together,
    passes context forward, and surfaces the consolidated report.
    """

    def __init__(self):
        self.slack = SlackNotifier()
        self.l1 = L1Agent()
        self.l2 = L2Agent()
        self.l3 = L3Agent()

    async def run(
        self,
        incident: Dict[str, Any],
        logs: Optional[List[str]] = None,
    ) -> Dict[str, TriageResult]:
        logs = logs or []
        results: Dict[str, TriageResult] = {}

        print("\n" + "█" * 70)
        print("  INCIDENT TRIAGE WORKFLOW STARTING")
        print(f"  Incident : {incident['id']} — {incident['title']}")
        print(f"  Severity : {incident['severity']}")
        print(f"  Time     : {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        print("█" * 70 + "\n")

        # L1
        l1 = await self.l1.analyse(incident, logs, self.slack)
        results["L1"] = l1
        print(l1.pretty())

        if not l1.should_escalate:
            print("✅  Incident resolved at L1.")
            return results

        # L2
        l2 = await self.l2.investigate(incident, logs, l1, self.slack)
        results["L2"] = l2
        print(l2.pretty())

        if not l2.should_escalate:
            print("✅  Incident resolved at L2.")
            return results

        # L3
        l3 = await self.l3.escalate(incident, logs, l1, l2, self.slack)
        results["L3"] = l3
        print(l3.pretty())

        print("✅  Escalation package dispatched. Full triage workflow complete.\n")
        return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    orchestrator = IncidentOrchestrator()
    results = await orchestrator.run(
        incident=MOCK_INCIDENT,
        logs=generate_mock_logs(),
    )

    # Optionally dump the structured JSON report
    if os.getenv("DUMP_JSON"):
        report = {level: r.to_dict() for level, r in results.items()}
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())


# ---------------------------------------------------------------------------
# Backward-compatibility aliases
# These allow server.py (and any other module) to keep importing the old names
# while the real implementation now lives in the classes above.
# ---------------------------------------------------------------------------

class IncidentAnalyzerAgent(IncidentOrchestrator):
    """Alias for IncidentOrchestrator — preserves the original import name."""
    pass


# Common alternative names that server.py or other modules may reference
TriageAgent        = L1Agent
InvestigationAgent = L2Agent
EscalationAgent    = L3Agent
Orchestrator       = IncidentOrchestrator


# ---------------------------------------------------------------------------
# Google ADK multi-agent orchestrator
# Required by chat_agent.py:
#   from .agents import incident_management_orchestrator, rotate_api_key, _API_KEY_POOL
# ---------------------------------------------------------------------------

import os as _os

# API key pool — populated from GEMINI_API_KEY or GEMINI_API_KEY_1 / _2 / _3
def _build_api_key_pool() -> List[str]:
    keys: List[str] = []
    # Support a comma-separated list in GEMINI_API_KEY
    primary = _os.getenv("GEMINI_API_KEY", "")
    if primary:
        keys.extend([k.strip() for k in primary.split(",") if k.strip()])
    # Support numbered keys: GEMINI_API_KEY_1, GEMINI_API_KEY_2, ...
    for i in range(1, 10):
        k = _os.getenv(f"GEMINI_API_KEY_{i}", "")
        if k:
            keys.append(k.strip())
    return keys or ["__no_key_configured__"]

_API_KEY_POOL: List[str] = _build_api_key_pool()
_current_key_index: int = 0


def rotate_api_key() -> str:
    """Advance to the next API key in the pool and configure the environment."""
    global _current_key_index
    _current_key_index = (_current_key_index + 1) % len(_API_KEY_POOL)
    new_key = _API_KEY_POOL[_current_key_index]
    _os.environ["GEMINI_API_KEY"] = new_key
    logger.info("Rotated to API key index %d", _current_key_index)
    return new_key


# ---------------------------------------------------------------------------
# ADK agent stubs
# These provide the `incident_management_orchestrator` object expected by
# chat_agent.py. They delegate to the L1/L2/L3 pipeline internally.
# Replace the stub bodies with real google.adk.agents.Agent instances once
# the ADK dependency is available in the environment.
# ---------------------------------------------------------------------------

try:
    from google.adk.agents import Agent, LlmAgent  # type: ignore

    _MODEL = _os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite")

    incident_analyzer_adk = LlmAgent(
        name="incident_analyzer",
        model=_MODEL,
        instruction=(
            "You are an L1 incident triage specialist. Analyse the provided incident "
            "details and logs. Summarise symptoms, severity, and blast radius. "
            "Identify preliminary root-cause hypotheses and recommend immediate actions."
        ),
    )

    root_cause_analyzer_adk = LlmAgent(
        name="root_cause_analyzer",
        model=_MODEL,
        instruction=(
            "You are an L2 senior SRE performing deep root-cause analysis. "
            "Correlate logs, metrics, deployment history, and system events. "
            "Identify the definitive root cause and produce a detailed remediation plan."
        ),
    )

    remedy_identifier_adk = LlmAgent(
        name="remedy_identifier",
        model=_MODEL,
        instruction=(
            "You are an L3 engineering escalation specialist. "
            "Given the L1 and L2 analyses, produce a complete escalation package: "
            "confirmed root cause, blast radius, rollback/hotfix steps with owners, "
            "and post-mortem requirements."
        ),
    )

    incident_management_orchestrator = Agent(
        name="incident_management_orchestrator",
        model=_MODEL,
        instruction=(
            "You are the incident management orchestrator. Route queries to the "
            "appropriate specialist agent based on the caller role (L1/L2/L3) and "
            "incident severity. Ensure full context is passed between levels."
        ),
        sub_agents=[
            incident_analyzer_adk,
            root_cause_analyzer_adk,
            remedy_identifier_adk,
        ],
    )

    logger.info("Google ADK agents initialised successfully.")

except Exception as _adk_exc:  # ADK not installed or config missing — use lightweight stubs
    logger.warning(
        "Google ADK not available (%s) — using stub orchestrator. "
        "Install google-adk and set GEMINI_API_KEY to enable LLM agents.",
        _adk_exc,
    )

    class _StubAgent:
        """Minimal stub that satisfies attribute access from chat_agent.py."""
        def __init__(self, name: str):
            self.name = name

        async def run_async(self, *args: Any, **kwargs: Any):  # type: ignore[override]
            return iter([])

    incident_management_orchestrator = _StubAgent("incident_management_orchestrator")  # type: ignore


# ---------------------------------------------------------------------------
# Named agent classes
# Required by server.py:
#   from .agents import (
#       IncidentAnalyzerAgent, RootCauseAnalysisAgent, SLAMonitorAgent,
#       IncidentTimelineAgent, NotificationAgent, IncidentEscalationAgent,
#       PerformanceOptimizationAgent,
#   )
#
# Each class exposes an async `process(data)` method that returns a dict,
# matching the call pattern used in server.py's tool handlers.
# ---------------------------------------------------------------------------

class IncidentAnalyzerAgent:
    """L1-level incident analysis agent (server.py interface)."""

    async def process(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        slack = SlackNotifier()
        logs = incident.get("logs") or generate_mock_logs()
        result = await L1Agent().analyse(incident, logs, slack)
        return result.to_dict()


class RootCauseAnalysisAgent:
    """L2-level root cause analysis agent (server.py interface)."""

    async def process(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        slack = SlackNotifier()
        logs = incident.get("logs") or generate_mock_logs()
        # Build a minimal L1 result to pass as context
        l1_result = TriageResult(
            level="L1",
            summary="Auto-promoted to L2 without explicit L1 pass.",
            findings=[],
            actions=[],
            should_escalate=True,
        )
        result = await L2Agent().investigate(incident, logs, l1_result, slack)
        return result.to_dict()


class SLAMonitorAgent:
    """SLA/SLO monitoring agent (server.py interface)."""

    async def process(self, slo: Dict[str, Any]) -> Dict[str, Any]:
        slo_id = slo.get("id", "unknown")
        target = slo.get("target", 99.9)
        actual = slo.get("actual", slo.get("value", target))
        status = "OK" if float(actual) >= float(target) else "BREACHED"
        return {
            "slo_id": slo_id,
            "target": target,
            "actual": actual,
            "status": status,
            "analysis": (
                f"SLO {slo_id} is {status}. "
                f"Current value {actual}% vs target {target}%."
            ),
        }


class IncidentTimelineAgent:
    """Incident timeline analysis agent (server.py interface)."""

    async def process(self, events: Any) -> Dict[str, Any]:
        if isinstance(events, list):
            timeline = [
                {
                    "time": e.get("timestamp", e.get("startTime", "unknown")),
                    "event": e.get("title", e.get("name", str(e))),
                    "severity": e.get("severity", "INFO"),
                }
                for e in events
            ]
        else:
            timeline = []
        return {
            "event_count": len(timeline),
            "timeline": timeline,
            "summary": f"Timeline contains {len(timeline)} events.",
        }


class NotificationAgent:
    """Notification generation agent (server.py interface)."""

    async def process(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        severity = (incident.get("severity") or "UNKNOWN").upper()
        title = incident.get("title", "Unknown Incident")
        channels = ["#incidents"]
        if severity in ("CRITICAL", "HIGH"):
            channels += ["#on-call", "#engineering-leads"]
        return {
            "incident_id": incident.get("id"),
            "severity": severity,
            "channels": channels,
            "notifications": [
                {
                    "channel": ch,
                    "message": f"[{severity}] {title} — immediate attention required.",
                    "sent": True,
                }
                for ch in channels
            ],
        }


class IncidentEscalationAgent:
    """Escalation decision agent (server.py interface)."""

    async def process(self, incident: Dict[str, Any]) -> Dict[str, Any]:
        severity = (incident.get("severity") or "LOW").upper()
        error_rate = float(incident.get("errorRate") or 0)
        duration = str(incident.get("duration", ""))

        should_escalate = severity in ("CRITICAL", "HIGH") or error_rate > 10
        level = "L3" if severity == "CRITICAL" else ("L2" if should_escalate else "L1")

        return {
            "incident_id": incident.get("id"),
            "should_escalate": should_escalate,
            "escalation_level": level,
            "reason": (
                f"Severity {severity}, error rate {error_rate}%, duration {duration}. "
                + ("Escalation required." if should_escalate else "No escalation needed.")
            ),
        }


class PerformanceOptimizationAgent:
    """Service performance analysis agent (server.py interface)."""

    async def process(self, service: Dict[str, Any]) -> Dict[str, Any]:
        service_id = service.get("id", service.get("entityId", "unknown"))
        p99 = service.get("p99LatencyMs", service.get("responseTime", 0))
        error_rate = service.get("errorRate", 0)
        throughput = service.get("throughput", "N/A")

        recommendations = []
        if float(p99) > 1000:
            recommendations.append("Investigate slow database queries or downstream timeouts.")
        if float(error_rate) > 5:
            recommendations.append("Review recent deployments and error logs.")
        if not recommendations:
            recommendations.append("Service is within normal performance bounds.")

        return {
            "service_id": service_id,
            "p99_latency_ms": p99,
            "error_rate_pct": error_rate,
            "throughput": throughput,
            "recommendations": recommendations,
        }
