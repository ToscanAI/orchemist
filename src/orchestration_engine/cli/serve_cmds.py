"""Web/server launcher command group for the orchestration-engine CLI.

Behavior-neutral extraction (EPIC #942 / issue #1005, 950e). The ``serve`` /
``ui`` / ``api-server`` / ``mcp`` commands previously lived inline in
``cli/__init__.py``; their bodies are moved here VERBATIM. Each command
self-registers on the shared ``main`` Click group (imported from ``._root``) at
import time via its ``@main.command`` decorator, so the facade only needs to
import this module for the registration side effect.

The heavy web app factory ``..web.api.create_api_app`` (and ``uvicorn``,
``fastapi``, the std-lib HTTP server, ``..mcp.run_mcp_server``, etc.) is — and
remains — a *function-local lazy import*: these launchers intentionally defer the
expensive web/uvicorn import so it is NOT paid at plain ``orch <cmd>`` startup.
Do NOT hoist it to module top.

All dependencies the commands touch are either module-locals brought in below
(``sys`` / ``Path`` / ``Optional`` / ``_get_persistent_db_path``) or
function-local lazy imports, NONE of which the test-suite patches on the
``orchestration_engine.cli`` facade, so the 950b/950c ``_cli.<dep>`` call-time
indirection is NOT needed here (the web tests invoke ``serve --help`` through the
``main`` group and import ``create_app`` from its source module).
"""

import sys
from pathlib import Path
from typing import Optional

import click

from ._helpers import _get_persistent_db_path
from ._root import main

# ---------------------------------------------------------------------------
# orch serve — local web UI server  (Feature #79)
# ---------------------------------------------------------------------------


@main.command("serve")
@click.option("--port", default=8374, show_default=True, help="Port to serve on.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to bind to.")
@click.option("--no-open", is_flag=True, help="Do not auto-open browser.")
@click.option("--db-path", default=None, help="SQLite DB path for pipeline runs.")
@click.option("--reload", is_flag=True, help="Enable uvicorn auto-reload (dev mode).")
def serve(  # noqa: C901
    port: int, host: str, no_open: bool, db_path: Optional[str], reload: bool
) -> None:
    """Launch the unified web UI + REST API on a single port.

    Serves the Next.js static frontend and the /api/v1/ REST API together.
    No CORS, no proxy, no separate servers needed.

    Requires the optional [web] extra:

      pip install orchestration-engine[web]

    Examples:

      orch serve                    # http://127.0.0.1:8374
      orch serve --port 9000
      orch serve --no-open          # start without opening browser
      orch serve --db-path /tmp/my.db
    """
    try:
        import uvicorn  # noqa: PLC0415

        from ..web.api import create_api_app  # noqa: PLC0415
    except ImportError:
        click.echo("Web UI requires extra dependencies. Install with:", err=True)
        click.echo("  pip install orchestration-engine[web]", err=True)
        sys.exit(1)

    app = create_api_app(db_path=db_path)

    # Mount static frontend if available
    frontend_out = Path(__file__).resolve().parent.parent.parent / "frontend" / "out"
    if frontend_out.exists():
        from fastapi.responses import FileResponse  # noqa: PLC0415

        index_html = frontend_out / "index.html"

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            """SPA fallback: serve static files or route-specific HTML for client-side routing.

            Next.js static export generates dynamic route pages as `_.html`
            (e.g. `templates/_.html` for `/templates/[id]`). We must serve
            the correct HTML shell so the client-side router hydrates the
            right page component.
            """
            # 1. Exact static file match (JS, CSS, images, etc.)
            static_file = frontend_out / full_path
            if static_file.is_file() and static_file.resolve().is_relative_to(
                frontend_out.resolve()
            ):
                return FileResponse(str(static_file))

            # 2. Try .html extension (e.g. /runs → runs.html)
            html_file = frontend_out / f"{full_path}.html"
            if html_file.is_file() and html_file.resolve().is_relative_to(frontend_out.resolve()):
                return FileResponse(str(html_file))

            # 3. Dynamic route: /templates/xyz/edit → templates/_/edit.html
            #    Try replacing dynamic segments with '_' (most-specific first)
            parts = full_path.strip("/").split("/")

            # 3a. Try substituting each path segment with '_' from right to left
            #     e.g. /templates/xyz/edit → templates/_/edit.html
            for i in range(len(parts) - 1, 0, -1):
                trial = [*parts]
                trial[i] = "_"
                # Try as .html
                candidate_html = frontend_out / ("/".join(trial) + ".html")
                if candidate_html.is_file() and candidate_html.resolve().is_relative_to(
                    frontend_out.resolve()
                ):
                    return FileResponse(str(candidate_html))
                # Try as directory with index.html
                candidate_index = frontend_out / "/".join(trial) / "index.html"
                if candidate_index.is_file() and candidate_index.resolve().is_relative_to(
                    frontend_out.resolve()
                ):
                    return FileResponse(str(candidate_index))

            # 3b. Walk up the path to find the nearest _.html
            for i in range(len(parts), 0, -1):
                candidate = frontend_out / "/".join(parts[:i]) / "_.html"
                if candidate.is_file() and candidate.resolve().is_relative_to(
                    frontend_out.resolve()
                ):
                    return FileResponse(str(candidate))

            # 4. Ultimate fallback: index.html (dashboard)
            return FileResponse(str(index_html))

        click.echo(f"Frontend: {frontend_out}")
    else:
        click.echo(
            "Warning: Frontend not built. Run 'cd frontend && npm run build'. "
            "API endpoints are still available at /api/v1/",
            err=True,
        )

    if not no_open:
        import threading  # noqa: PLC0415
        import webbrowser  # noqa: PLC0415

        threading.Timer(1.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()

    click.echo("Orchestration Engine (unified)")
    click.echo(f"  UI:  http://{host}:{port}")
    click.echo(f"  API: http://{host}:{port}/api/v1/docs")
    click.echo("  Press Ctrl+C to stop.")

    uvicorn.run(app, host=host, port=port, reload=reload)


# ---------------------------------------------------------------------------
# orch ui — Serve static Next.js frontend export (Issue #310)
# ---------------------------------------------------------------------------


@main.command("ui")
@click.option("--port", default=8080, show_default=True, help="Port to serve the frontend on.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to bind to.")
@click.option("--no-open", is_flag=True, help="Skip auto-opening the browser.")
def ui(port: int, host: str, no_open: bool) -> None:
    """Serve the static Next.js frontend export and open the browser.

    Serves the pre-built frontend from frontend/out/ using Python's built-in
    HTTP server.  Build the frontend first if the directory is missing:

      cd frontend && npm run build

    Examples:

      orch ui                    # http://localhost:8080
      orch ui --port 9090
      orch ui --no-open          # start without opening browser
      orch ui --host 0.0.0.0    # bind to all interfaces
    """
    import http.server  # noqa: PLC0415
    import socketserver  # noqa: PLC0415
    import threading  # noqa: PLC0415
    import webbrowser  # noqa: PLC0415

    frontend_out = Path(__file__).parent.parent.parent / "frontend" / "out"

    if not frontend_out.exists():
        click.echo(
            "✗ frontend/out/ not found. Run 'cd frontend && npm run build' first.",
            err=True,
        )
        sys.exit(1)

    class _QuietHandler(http.server.SimpleHTTPRequestHandler):
        """Serve from frontend/out/ and suppress request logs."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(frontend_out), **kwargs)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # silence per-request output

    url = f"http://{host}:{port}"

    # socketserver.TCPServer with allow_reuse_address so re-runs don't fail
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer((host, port), _QuietHandler)

    if not no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    click.echo("✓ Orchestration Engine frontend (static)")
    click.echo(f"  Serving:  {frontend_out}")
    click.echo(f"  URL:      {url}")
    click.echo("  Press Ctrl+C to stop.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        click.echo("\n✓ Server stopped.")


# ---------------------------------------------------------------------------
# orch api-server — REST API server (Issue #257)
# ---------------------------------------------------------------------------


@main.command("api-server")
@click.option("--port", default=8375, show_default=True, help="Port to serve on.")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to bind to.")
@click.option("--reload", is_flag=True, default=False, help="Enable auto-reload (dev only).")
@click.option(
    "--db-path",
    default=None,
    help="Override path to the persistent pipeline-runs DB.",
)
def api_server(port: int, host: str, reload: bool, db_path: Optional[str]) -> None:
    """Launch the REST API server for programmatic pipeline control.

    Starts a FastAPI REST API at /api/v1/ backed by the same daemon-based
    async execution infrastructure used by ``orch launch``.  Intended for
    CI/CD pipelines, OpenClaw, and other programmatic consumers.

    Requires the optional [web] extra:

      pip install orchestration-engine[web]

    Endpoints:

    \b
      GET  /api/v1/health                — health check
      GET  /api/v1/templates             — list all templates
      GET  /api/v1/templates/{name}      — template detail
      POST /api/v1/runs                  — launch a pipeline run
      GET  /api/v1/runs                  — list runs (with filtering/pagination)
      GET  /api/v1/runs/{run_id}         — run status
      GET  /api/v1/runs/{run_id}/logs    — daemon log output
      DELETE /api/v1/runs/{run_id}       — cancel a run

    Examples:

      orch api-server                    # http://127.0.0.1:8375/api/v1/
      orch api-server --port 9000
      orch api-server --reload           # dev mode with auto-reload
    """
    try:
        import uvicorn  # noqa: PLC0415

        from ..web.api import create_api_app  # noqa: PLC0415
    except ImportError:
        click.echo("REST API server requires extra dependencies. Install with:", err=True)
        click.echo("  pip install orchestration-engine[web]", err=True)
        sys.exit(1)

    effective_db_path = db_path or _get_persistent_db_path()
    app = create_api_app(db_path=effective_db_path)

    click.echo("✓ Orchestration Engine REST API server")
    click.echo(f"  Listening on http://{host}:{port}")
    click.echo(f"  Docs:      http://{host}:{port}/api/v1/docs")
    click.echo(f"  DB:        {effective_db_path}")
    click.echo("  Press Ctrl+C to stop.")

    uvicorn.run(app, host=host, port=port, reload=reload)


@main.command("mcp")
@click.option(
    "--transport",
    default="stdio",
    help="Transport protocol: stdio or sse",
)
@click.option(
    "--port",
    default=8000,
    type=int,
    show_default=True,
    help="Port for SSE transport (default: 8000)",
)
def mcp_server(transport: str, port: int) -> None:
    """Start the MCP server for IDE integration (Claude Code, Cursor)."""
    supported = ["stdio", "sse"]
    if transport not in supported:
        click.echo(
            f"Unsupported transport: {transport}. Supported: stdio, sse",
            err=True,
        )
        sys.exit(1)
    if not (1 <= port <= 65535):
        click.echo(
            f"Invalid port: {port}. Port must be between 1 and 65535",
            err=True,
        )
        sys.exit(1)
    from ..mcp import run_mcp_server  # noqa: PLC0415

    run_mcp_server(transport=transport, port=port)
