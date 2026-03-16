"""MCP (Model Context Protocol) package for the Orchestration Engine.

Provides the server scaffold and transport layer for IDE integration
(Claude Code, Cursor) via MCP protocol.
"""

from .server import run_mcp_server

__all__ = ["run_mcp_server"]
