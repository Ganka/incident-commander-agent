#!/usr/bin/env python3
\"\"\"Post mock incident requests to the running MCP server.

This script helps exercise the full endpoint flow (chat, notify) using the
mock incidents in `dynatrace_mcp.mock_data`. For end-to-end offline testing,
set `USE_MOCK_RESPONSES=true` in server/.env and restart the server so the
server will avoid contacting Gemini and will also mock Slack posts.
\"\"\"

import argparse
import os
import sys
import httpx
from typing import Dict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')) )

from dynatrace_mcp.mock_data import critical_incident, minor_incident


SERVER_URL = os.getenv('MCP_SERVER_URL', 'http://localhost:3002')


def post_chat(incident: Dict, role: str = 'L2') -> None:
    payload = {
        'message': f\"Please analyze incident {incident.get('id')}\",
        'incident_context': incident,
        'role': role,
    }
    url = f\"{SERVER_URL}/chat\"
    print(f\"POST {url} -> payload keys: {list(payload.keys())}\")
    with httpx.Client(timeout=20.0) as client:
        r = client.post(url, json=payload)
        print('Status:', r.status_code)
        try:
            print('JSON:', r.json())
        except Exception:
            print('Text:', r.text[:1000])


def post_notify(incident_id: str) -> None:
    url = f\"{SERVER_URL}/notify/critical\"
    payload = { 'incident_id': incident_id }
    print(f\"POST {url} -> {payload}\")
    with httpx.Client(timeout=15.0) as client:
        r = client.post(url, json=payload)
        print('Status:', r.status_code)
        try:
            print('JSON:', r.json())
        except Exception:
            print('Text:', r.text[:1000])


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--which', choices=['critical', 'minor', 'both'], default='critical')
    p.add_argument('--role', choices=['L1','L2','L3'], default='L2')
    args = p.parse_args()

    if args.which in ('critical', 'both'):
        print('\\n--- Chat (critical incident) ---')
        post_chat(critical_incident, role=args.role)
        print('\\n--- Notify (critical incident manual trigger) ---')
        post_notify(critical_incident['id'])

    if args.which in ('minor', 'both'):
        print('\\n--- Chat (minor incident) ---')
        post_chat(minor_incident, role=args.role)


if __name__ == '__main__':
    main()

