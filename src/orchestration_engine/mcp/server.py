"""MCP Server module for the Orchestration Engine.

Implements the Model Context Protocol server with stdio and SSE transports.
Used by IDE integrations (Claude Code, Cursor) to invoke orchemist pipelines.
"""

import os
import sys
from pathlib import Path


def _read_version() -> str:
    """Read the package version from importlib.metadata or pyproject.toml.

    Uses importlib.metadata as primary source, falls back to parsing
    pyproject.toml with the 'toml' package (Python 3.10-compatible).
    Returns '0.0.0' and emits a warning if both sources fail.

    Returns:
        Version string (e.g. "0.3.0") or "0.0.0" on failure.
    """
    try:
        import importlib.metadata
        return importlib.metadata.version("orchemist")
    except Exception:
        pass

    try:
        import toml
        _pyproject = Path(__file__).parent.parent.parent.parent / "pyproject.toml"
        data = toml.load(str(_pyproject))
        return data["project"]["version"]
    except Exception:
        print("Could not read version from pyproject.toml, using 0.0.0", file=sys.stderr)
        return "0.0.0"


def _check_api_key() -> None:
    """Check if an API key is configured and emit a warning if not.

    Checks the ANTHROPIC_API_KEY environment variable. If absent,
    writes a warning to stderr.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("No API key configured — running without auth", file=sys.stderr)


def run_mcp_server(transport: str = "stdio", port: int = 8000) -> None:
    """Start the MCP server with the specified transport.

    Args:
        transport: Transport protocol to use. Must be 'stdio' or 'sse'.
        port: Port number for SSE transport (default: 8000).

    Raises:
        SystemExit: On OSError (port already in use) for SSE transport.
    """
    if transport == "stdio":
        print("MCP server started", file=sys.stderr)
        _check_api_key()
        from mcp.server.fastmcp import FastMCP
        from .tools import register_tools
        version = _read_version()
        mcp = FastMCP(name="orchemist")
        mcp._mcp_server.version = version
        register_tools(mcp)
        import asyncio
        asyncio.run(mcp.run_stdio_async())

    elif transport == "sse":
        print(f"MCP server started on SSE transport, port {port}", file=sys.stderr)
        _check_api_key()

        # Pre-check port availability before attempting to start the server.
        # uvicorn catches and logs OSErrors internally rather than re-raising,
        # so we must verify the port is free before handing off to the MCP SDK.
        import socket as _socket
        _check_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _check_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 0)
        try:
            _check_sock.bind(("0.0.0.0", port))
        except OSError:
            _check_sock.close()
            print(
                f"Address already in use: port {port} is already bound",
                file=sys.stderr,
            )
            sys.exit(1)
        finally:
            _check_sock.close()

        from mcp.server.fastmcp import FastMCP
        from .tools import register_tools
        version = _read_version()
        mcp = FastMCP(name="orchemist", host="0.0.0.0", port=port)
        mcp._mcp_server.version = version
        register_tools(mcp)
        import asyncio
        try:
            asyncio.run(mcp.run_sse_async())
        except OSError as e:
            if "Address already in use" in str(e):
                print(
                    f"Address already in use: port {port} is already bound",
                    file=sys.stderr,
                )
                sys.exit(1)
            raise
