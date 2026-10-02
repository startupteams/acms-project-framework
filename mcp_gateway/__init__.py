"""MIAM MCP Gateway — thin internal gateway for Startup Teams managed agents.

Approved direction: STEA-004 plan 2026-10-02 (Jira Workflow Recovery and Internal
MCP Gateway), Window 1. The gateway is a logically separate service: it
authenticates agent/assignment tokens, enforces roles/scopes, exposes the ACMS
adapter as MCP resources/tools, writes a durable activity log, and FAILS CLOSED.
It never replaces REST/A2A/SSE and never owns domain business logic.

W1 scope: gateway shell + ACMS adapter only (resources/tools from plan §18,
risk classes §16, roles §17, tokens §12/§13, context manifest §14).
"""
from __future__ import annotations

__version__ = "0.3.0"

GATEWAY_NAME = "miam-mcp-gateway"
