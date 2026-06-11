"""
Dynatrace MCP Server - Main Implementation
"""

import logging
import json
from typing import Any, Dict, List, Optional, Callable
from datetime import datetime
import asyncio
from mcp.server import Server
from mcp.types import Tool, TextContent, CallToolResult

from .dynatrace_client import DynatraceClient
from .agents import (
    IncidentAnalyzerAgent,
    RootCauseAnalysisAgent,
    SLAMonitorAgent,
    IncidentTimelineAgent,
    NotificationAgent,
    IncidentEscalationAgent,
    PerformanceOptimizationAgent,
)

logger = logging.getLogger(__name__)


class DynastraceMCPServer:
    """Main MCP Server for Dynatrace Incident Management"""

    def __init__(self, name: str = "dynatrace-mcp-server"):
        self.name = name
        self.server = Server(name)
        self.dynatrace_client: Optional[DynatraceClient] = None
        self.tools: List[Tool] = []
        
        # Initialize agents
        self.incident_analyzer = IncidentAnalyzerAgent()
        self.root_cause_analyzer = RootCauseAnalysisAgent()
        self.sla_monitor = SLAMonitorAgent()
        self.timeline_analyzer = IncidentTimelineAgent()
        self.notification_agent = NotificationAgent()
        self.escalation_agent = IncidentEscalationAgent()
        self.performance_agent = PerformanceOptimizationAgent()
        
        # Register tools and handlers
        self._register_tools()
        self._setup_handlers()

    def _register_tools(self):
        """Register available tools with the MCP server"""
        self.tools = [
            Tool(
                name="get_incidents",
                description="Fetch current incidents from Dynatrace",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "description": "'open', 'resolved', or 'all'",
                            "default": "open",
                        },
                        "hours_back": {
                            "type": "integer",
                            "description": "Hours to look back",
                            "default": 24,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max incidents to return",
                            "default": 100,
                        },
                    },
                },
            ),
            Tool(
                name="analyze_incident",
                description="Analyze incident using AI",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "incident_id": {
                            "type": "string",
                            "description": "Incident ID",
                        },
                    },
                    "required": ["incident_id"],
                },
            ),
            Tool(
                name="root_cause_analysis",
                description="Perform root cause analysis on an incident",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "incident_id": {
                            "type": "string",
                            "description": "Incident ID",
                        },
                        "hours_back": {
                            "type": "integer",
                            "description": "Hours of context to analyze",
                            "default": 6,
                        },
                    },
                    "required": ["incident_id"],
                },
            ),
            Tool(
                name="check_sla_status",
                description="Check SLA/SLO status",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "slo_id": {
                            "type": "string",
                            "description": "SLO ID (optional, gets all if not provided)",
                        },
                    },
                },
            ),
            Tool(
                name="generate_notifications",
                description="Generate notifications for incident stakeholders",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "incident_id": {
                            "type": "string",
                            "description": "Incident ID",
                        },
                        "severity": {
                            "type": "string",
                            "description": "Incident severity",
                        },
                    },
                    "required": ["incident_id"],
                },
            ),
            Tool(
                name="check_escalation",
                description="Check if incident should be escalated",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "incident_id": {
                            "type": "string",
                            "description": "Incident ID",
                        },
                    },
                    "required": ["incident_id"],
                },
            ),
            Tool(
                name="get_top_services",
                description="Get top services by health status",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Number of services",
                            "default": 10,
                        },
                    },
                },
            ),
            Tool(
                name="get_service_metrics",
                description="Get service performance metrics",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "service_id": {
                            "type": "string",
                            "description": "Service ID",
                        },
                        "hours_back": {
                            "type": "integer",
                            "description": "Hours of metrics",
                            "default": 24,
                        },
                    },
                    "required": ["service_id"],
                },
            ),
            Tool(
                name="get_incident_timeline",
                description="Get timeline of incident events",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "incident_id": {
                            "type": "string",
                            "description": "Incident ID",
                        },
                    },
                    "required": ["incident_id"],
                },
            ),
            Tool(
                name="dashboard_summary",
                description="Get comprehensive dashboard summary",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "hours_back": {
                            "type": "integer",
                            "description": "Hours to summarize",
                            "default": 24,
                        },
                    },
                },
            ),
        ]
        
    def _setup_handlers(self):
        """Setup request handlers"""

        @self.server.list_tools()
        async def handle_list_tools():
            return self.tools

        @self.server.call_tool()
        async def handle_tool_call(name: str, arguments: Dict[str, Any]):
            return await self._handle_tool_call(name, arguments)

    async def _handle_tool_call(self, name: str, arguments: Dict[str, Any]) -> CallToolResult:
        """Handle tool calls"""
        try:
            if name == "get_incidents":
                return await self._tool_get_incidents(arguments)
            elif name == "analyze_incident":
                return await self._tool_analyze_incident(arguments)
            elif name == "root_cause_analysis":
                return await self._tool_root_cause_analysis(arguments)
            elif name == "check_sla_status":
                return await self._tool_check_sla_status(arguments)
            elif name == "generate_notifications":
                return await self._tool_generate_notifications(arguments)
            elif name == "check_escalation":
                return await self._tool_check_escalation(arguments)
            elif name == "get_top_services":
                return await self._tool_get_top_services(arguments)
            elif name == "get_service_metrics":
                return await self._tool_get_service_metrics(arguments)
            elif name == "get_incident_timeline":
                return await self._tool_get_incident_timeline(arguments)
            elif name == "dashboard_summary":
                return await self._tool_dashboard_summary(arguments)
            else:
                return CallToolResult(content=[TextContent(type="text", text=f"Unknown tool: {name}")])
        except Exception as e:
            logger.error(f"Error handling tool {name}: {e}", exc_info=True)
            return CallToolResult(content=[TextContent(type="text", text=f"Error: {str(e)}")])

    async def _tool_get_incidents(self, args: Dict[str, Any]) -> CallToolResult:
        """Get incidents tool"""
        incidents = await self.dynatrace_client.get_incidents(
            status=args.get("status", "open"),
            hours_back=args.get("hours_back", 24),
            limit=args.get("limit", 100),
        )
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(incidents, indent=2, default=str))]
        )

    async def _tool_analyze_incident(self, args: Dict[str, Any]) -> CallToolResult:
        """Analyze incident tool"""
        incident_id = args.get("incident_id")
        incident_details = await self.dynatrace_client.get_incident_details(incident_id)
        
        analysis = await self.incident_analyzer.process(incident_details)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(analysis, indent=2, default=str))])

    async def _tool_root_cause_analysis(self, args: Dict[str, Any]) -> CallToolResult:
        """Root cause analysis tool"""
        incident_id = args.get("incident_id")
        hours_back = args.get("hours_back", 6)
        
        incident_details = await self.dynatrace_client.get_incident_details(incident_id)
        # In a real scenario, get more context (events, metrics, etc.)
        
        analysis = await self.root_cause_analyzer.process(incident_details)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(analysis, indent=2, default=str))])

    async def _tool_check_sla_status(self, args: Dict[str, Any]) -> CallToolResult:
        """Check SLA status tool"""
        slo_id = args.get("slo_id")
        
        if slo_id:
            slo_data = await self.dynatrace_client.get_slo_status(slo_id)
            slos = [slo_data]
        else:
            slos = await self.dynatrace_client.get_slos()
        
        analyses = []
        for slo in slos:
            analysis = await self.sla_monitor.process(slo)
            analyses.append(analysis)
        
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(analyses, indent=2, default=str))])

    async def _tool_generate_notifications(self, args: Dict[str, Any]) -> CallToolResult:
        """Generate notifications tool"""
        incident_id = args.get("incident_id")
        requested_channel = args.get("channel")
        
        incident_details = await self.dynatrace_client.get_incident_details(incident_id)
        notifications = await self.notification_agent.process(incident_details)
        if requested_channel:
            notifications["requested_channel"] = requested_channel
            if requested_channel not in notifications.get("channels", []):
                notifications.setdefault("channels", []).insert(0, requested_channel)
                notifications.setdefault("notifications", []).insert(
                    0,
                    {
                        "channel": requested_channel,
                        "message": (
                            f"[{notifications.get('severity', 'UNKNOWN')}] "
                            f"{incident_details.get('title', 'Unknown incident')} - "
                            f"{incident_details.get('impact') or incident_details.get('description', '')}"
                        ),
                        "sent": False,
                    },
                )
        
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(notifications, indent=2, default=str))])

    async def _tool_check_escalation(self, args: Dict[str, Any]) -> CallToolResult:
        """Check escalation tool"""
        incident_id = args.get("incident_id")
        
        incident_details = await self.dynatrace_client.get_incident_details(incident_id)
        escalation = await self.escalation_agent.process(incident_details)
        
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(escalation, indent=2, default=str))])

    async def _tool_get_top_services(self, args: Dict[str, Any]) -> CallToolResult:
        """Get top services tool"""
        services = await self.dynatrace_client.get_top_services(args.get("limit", 10))
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(services, indent=2, default=str))])

    async def _tool_get_service_metrics(self, args: Dict[str, Any]) -> CallToolResult:
        """Get service metrics tool"""
        service_id = args.get("service_id")
        service_details = await self.dynatrace_client.get_service_details(service_id)
        
        # Get performance metrics
        analysis = await self.performance_agent.process(service_details)
        
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(analysis, indent=2, default=str))])

    async def _tool_get_incident_timeline(self, args: Dict[str, Any]) -> CallToolResult:
        """Get incident timeline tool"""
        incident_id = args.get("incident_id")
        
        # Get events for the incident
        events = await self.dynatrace_client.get_events(incident_id, hours_back=24)
        timeline = await self.timeline_analyzer.process(events)
        
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(timeline, indent=2, default=str))])

    async def _tool_dashboard_summary(self, args: Dict[str, Any]) -> CallToolResult:
        """Get dashboard summary tool"""
        hours_back = args.get("hours_back", 24)
        
        # Gather data for dashboard
        incidents = await self.dynatrace_client.get_incidents(
            status="all", hours_back=hours_back, limit=50
        )
        services = await self.dynatrace_client.get_top_services(limit=10)
        slos = await self.dynatrace_client.get_slos()
        
        summary = {
            "timestamp": datetime.utcnow().isoformat(),
            "total_incidents": len(incidents),
            "open_incidents": len([i for i in incidents if i.get("status") == "open"]),
            "top_services": services,
            "slo_count": len(slos),
            "hours_analyzed": hours_back,
        }
        
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(summary, indent=2, default=str))])

    async def run(self):
        """Run the MCP server"""
        self.dynatrace_client = DynatraceClient()
        logger.info(f"Starting {self.name}")
        # Provide a compatibility mode for standalone execution using stdio
        # If the caller provides streams and initialization options, forward
        # them to the underlying MCP Server. Otherwise run with the stdio
        # transport.
        try:
            from mcp.server.stdio import stdio_server

            async with stdio_server() as (read_stream, write_stream):
                init_opts = self.server.create_initialization_options(
                    notification_options=None, experimental_capabilities=None
                )
                await self.server.run(read_stream, write_stream, init_opts)
        except Exception:
            # In case stdio transport isn't appropriate, re-raise
            raise

    async def close(self):
        """Close the server"""
        if self.dynatrace_client:
            await self.dynatrace_client.close()
