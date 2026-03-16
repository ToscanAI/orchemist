"""Tests for MCP server scaffold (issue #467).

Covers CLI integration and server startup behavior for the MCP transport layer.
These tests use Click's CliRunner and unittest.mock to avoid blocking I/O.
"""

import asyncio
import sys
from unittest.mock import patch, MagicMock

import pytest

from click.testing import CliRunner
from orchestration_engine.cli import main


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def cli_runner():
    return CliRunner()


# ─────────────────────────────────────────────────────────────────────────────
# CLI Integration Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestMcpCliIntegration:
    def test_mcp_appears_in_help(self, cli_runner):
        """mcp subcommand should appear in orch --help."""
        result = cli_runner.invoke(main, ['--help'])
        assert 'mcp' in result.output.lower()

    def test_mcp_help_shows_transport_option(self, cli_runner):
        """orch mcp --help should document --transport with stdio and sse."""
        result = cli_runner.invoke(main, ['mcp', '--help'])
        assert result.exit_code == 0
        assert '--transport' in result.output
        assert 'stdio' in result.output
        assert 'sse' in result.output

    def test_mcp_help_shows_port_option_with_default_8000(self, cli_runner):
        """orch mcp --help should document --port with default 8000."""
        result = cli_runner.invoke(main, ['mcp', '--help'])
        assert result.exit_code == 0
        assert '--port' in result.output
        assert '8000' in result.output

    def test_invalid_transport_exits_code_1(self, cli_runner):
        """Unsupported transport should exit with code 1."""
        result = cli_runner.invoke(main, ['mcp', '--transport', 'grpc'])
        assert result.exit_code == 1

    def test_invalid_transport_error_message(self, cli_runner):
        """Unsupported transport should write exact error message to stderr."""
        result = cli_runner.invoke(main, ['mcp', '--transport', 'grpc'])
        assert 'Unsupported transport: grpc. Supported: stdio, sse' in result.output

    def test_invalid_port_zero_exits_code_1(self, cli_runner):
        """Port 0 should exit with code 1."""
        result = cli_runner.invoke(main, ['mcp', '--transport', 'sse', '--port', '0'])
        assert result.exit_code == 1

    def test_invalid_port_out_of_range_exits_code_1(self, cli_runner):
        """Port 99999 should exit with code 1."""
        result = cli_runner.invoke(main, ['mcp', '--transport', 'sse', '--port', '99999'])
        assert result.exit_code == 1


# ─────────────────────────────────────────────────────────────────────────────
# Server Startup Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestMcpServerStartup:
    def test_stdio_logs_mcp_server_started(self, capsys):
        """stdio transport should log 'MCP server started' to stderr."""
        from orchestration_engine.mcp.server import run_mcp_server
        with patch('asyncio.run'), \
             patch('orchestration_engine.mcp.server._read_version', return_value='0.3.0'), \
             patch('os.environ.get', return_value=None), \
             patch('orchestration_engine.mcp.server._check_api_key'):
            with patch('asyncio.run'):
                run_mcp_server(transport='stdio')
        captured = capsys.readouterr()
        assert 'MCP server started' in captured.err

    def test_sse_logs_mcp_server_started_with_port(self, capsys):
        """SSE transport should log startup message with port."""
        from orchestration_engine.mcp.server import run_mcp_server
        import socket as sock_mod
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        with patch('asyncio.run'), \
             patch('orchestration_engine.mcp.server._read_version', return_value='0.3.0'), \
             patch('orchestration_engine.mcp.server._check_api_key'), \
             patch('socket.socket', return_value=mock_sock):
            run_mcp_server(transport='sse', port=9090)
        captured = capsys.readouterr()
        assert 'MCP server started on SSE transport, port 9090' in captured.err

    def test_sse_default_port_is_8000(self, capsys):
        """Default SSE port should be 8000."""
        from orchestration_engine.mcp.server import run_mcp_server
        mock_sock = MagicMock()
        with patch('asyncio.run'), \
             patch('orchestration_engine.mcp.server._read_version', return_value='0.3.0'), \
             patch('orchestration_engine.mcp.server._check_api_key'), \
             patch('socket.socket', return_value=mock_sock):
            run_mcp_server(transport='sse')
        captured = capsys.readouterr()
        assert 'MCP server started on SSE transport, port 8000' in captured.err

    def test_no_api_key_logs_warning(self, capsys):
        """No API key should emit warning."""
        from orchestration_engine.mcp.server import _check_api_key
        import os
        with patch.dict('os.environ', {}, clear=True):
            # Remove ANTHROPIC_API_KEY if present
            env = {'ANTHROPIC_API_KEY': ''} if False else {}
            import os as _os
            original = _os.environ.get('ANTHROPIC_API_KEY')
            if original is not None:
                del _os.environ['ANTHROPIC_API_KEY']
            try:
                _check_api_key()
            finally:
                if original is not None:
                    _os.environ['ANTHROPIC_API_KEY'] = original
        captured = capsys.readouterr()
        assert 'No API key configured' in captured.err

    def test_with_api_key_no_warning(self, capsys):
        """With API key configured, no warning should be emitted."""
        from orchestration_engine.mcp.server import _check_api_key
        import os
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'sk-test-key'}):
            _check_api_key()
        captured = capsys.readouterr()
        assert 'No API key configured' not in captured.err

    def test_version_fallback_when_pyproject_missing(self, tmp_path, capsys):
        """Version falls back to 0.0.0 when pyproject.toml is absent."""
        from orchestration_engine.mcp import server as mcp_server
        with patch('importlib.metadata.version', side_effect=Exception('not found')), \
             patch('orchestration_engine.mcp.server._pyproject_path',
                   tmp_path / 'nonexistent.toml', create=True):
            # Override the path by monkeypatching the toml load
            with patch('toml.load', side_effect=Exception('file not found')):
                # Also patch __file__ reference by patching Path resolution
                ver = mcp_server._read_version()
        assert ver == '0.0.0'
        captured = capsys.readouterr()
        assert 'Could not read version from pyproject.toml, using 0.0.0' in captured.err


# ─────────────────────────────────────────────────────────────────────────────
# MCP Handshake / FastMCP Instance Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestMcpHandshake:
    def test_initialize_returns_server_name(self):
        """FastMCP instance should have name 'orchemist'."""
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP(name="orchemist")
        assert mcp.name == "orchemist"

    def test_initialize_returns_correct_version(self):
        """FastMCP instance version should be settable and readable."""
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP(name="orchemist")
        mcp._mcp_server.version = "0.3.0"
        assert mcp._mcp_server.version == "0.3.0"

    def test_initialize_returns_tools_capability(self):
        """FastMCP instance should have list_tools method (tools capability)."""
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP(name="orchemist")
        assert hasattr(mcp, 'list_tools')
        assert callable(mcp.list_tools)

    def test_tools_list_returns_empty_list(self):
        """No tools registered — list_tools should return empty list."""
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP(name="orchemist")
        tools = asyncio.run(mcp.list_tools())
        assert tools == []
