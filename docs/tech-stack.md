# Tech Stack — Implementation Decisions

> **Audience:** Contributors and curious readers who want to understand *why* the engine is built the way it is — not just *how*.

---

## Design Philosophy

Every dependency in this project was an explicit choice, not a default. The engine is designed to run on a **Raspberry Pi** (or any low-resource Linux host) as well as developer laptops. That constraint shapes everything: no heavyweight frameworks, no mandatory network services, no background daemons required. The entire system is a Python package you install and run.

---

## Python 3.10+

**Why 3.10 specifically?**

- `match`/`case` (structural pattern matching) for cleaner state machine code
- `X | Y` union type syntax (`str | None` instead of `Optional[str]`)
- `ParamSpec` and improved `typing` ergonomics for generic utilities
- Still broadly available on Raspberry Pi OS and common Linux distributions as of 2025–2026

The project targets 3.10, 3.11, and 3.12 (see `pyproject.toml` classifiers). We do not use 3.9 or earlier because the `match` statement and union syntax would require workarounds that add noise without adding value.

---

## Pydantic V2 for Schemas

All task inputs, outputs, and metadata are **Pydantic V2 models** (`pydantic>=2.0`).

**Why Pydantic over plain dataclasses?**

- **Runtime validation**: field constraints (`ge=0.0, le=1.0`), type coercion, and validators run automatically on construction — no manual sanitisation code scattered everywhere
- **Serialisation**: `.model_dump()` and `.model_dump_json()` give consistent JSON output for SQLite storage and IPC without custom serialisers
- **Self-documenting schemas**: field types, defaults, and `Field(description=...)` annotations serve as living documentation
- **`@model_validator`**: computed fields like `confidence_level` (derived from numeric `confidence`) are kept co-located with the model they belong to

**Why not V1?** V2 is significantly faster (written in Rust), has cleaner validator syntax (`@model_validator(mode='after')` vs `@validator`), and is the actively maintained branch.

---

## SQLite + WAL Mode for the Task Queue

The task queue is backed by **SQLite** stored at `~/.orchestration-engine/engine.db`. No separate database server is required.

**WAL (Write-Ahead Logging) mode** is enabled on every connection:

```python
conn.execute("PRAGMA journal_mode = WAL")
conn.execute("PRAGMA busy_timeout = 5000")
conn.execute("PRAGMA synchronous = NORMAL")
```

**Why not PostgreSQL?**

| SQLite | PostgreSQL |
|---|---|
| Zero installation — file on disk | Requires a running server process |
| Works on Raspberry Pi, CI, developer laptops identically | Heavy RAM baseline (~50–100 MB idle) |
| WAL mode allows concurrent readers + 1 writer | Full MVCC but overkill for single-host queue |
| Tests spin up in-memory instances instantly | Test setup requires a live Postgres instance or Docker |
| No network stack, no auth, no connection pool | Connection pool adds latency and complexity |

The engine processes tasks sequentially within a single host. SQLite's WAL mode is more than sufficient for the concurrency level (multiple worker threads reading, one writer at a time). If the queue grows to multi-host scale, the abstraction layer (`Database` class) is designed to be swapped out.

---

## Click for CLI

The `orch` CLI is built with **Click** (`click>=8.0`).

**Why Click over argparse?**

- **Composable commands**: `@main.command()` decorators compose naturally into a command group without boilerplate
- **Type coercion**: `click.Choice`, `click.Path`, `type=int` handle validation and error messages automatically
- **`--help` generation**: comprehensive help text is derived from docstrings and option annotations
- **Confirmation prompts**: `click.confirm()` for destructive operations (like `cancel`) with `--force` bypass
- **POSIX-compliant**: supports both `--flag` and `-f` short forms with minimal code

argparse would work but requires significantly more boilerplate for the same feature set. FastAPI or Typer were not considered — the CLI doesn't need an HTTP server or async runtime.

---

## PyYAML for Pipeline Templates

Pipeline templates are written in **YAML** and loaded with **PyYAML** (`pyyaml>=6.0`).

**Why YAML over JSON or TOML?**

- Multi-line prompt templates are human-readable in YAML (`|` block scalars), painful in JSON (escaped `\n`)
- YAML is the standard format for pipeline and CI configuration (Kubernetes, GitHub Actions, Ansible) — familiar to operators
- `yaml.safe_load()` is used exclusively — never `yaml.load()` — to prevent code execution via YAML tags

**Why not TOML?** TOML is excellent for configuration files but awkward for deeply nested structures like phase dependency graphs with inline prompt templates.

---

## stdlib `urllib` for AnthropicExecutor

The `AnthropicExecutor` calls the Anthropic Messages API using **Python's built-in `urllib.request`** — no `requests`, no `httpx`, no `aiohttp`.

```python
req = urllib.request.Request(
    self.API_URL,
    data=json.dumps(body).encode("utf-8"),
    headers={...},
    method="POST",
)
with urllib.request.urlopen(req, timeout=300) as resp:
    return json.loads(resp.read().decode("utf-8"))
```

**Why?**

- **Zero extra dependency**: `requests` alone adds ~150 KB and its own dependency tree (`certifi`, `charset-normalizer`, `idna`, `urllib3`). On a Raspberry Pi or a minimal container, every dependency is a liability
- **No version conflicts**: `urllib` ships with Python and has no version mismatch risk
- **Sufficient for the use case**: the API is a single POST endpoint with JSON in/JSON out; `urllib` handles this without ceremony

The trade-off is slightly more verbose error handling (`urllib.error.HTTPError` instead of `response.raise_for_status()`), but the executor is small enough that this is manageable.

---

## pytest for Testing

Tests use **pytest** (`pytest>=7.0`) with the `pytest-asyncio` and `pytest-cov` plugins.

**Why pytest over `unittest`?**

- Fixture injection (`@pytest.fixture`) is cleaner than `setUp`/`tearDown` class methods
- Parametrised tests (`@pytest.mark.parametrize`) reduce boilerplate for table-driven tests
- In-memory SQLite databases (`:memory:`) are created per-test via fixtures — tests are isolated and fast
- `pytest-cov` integrates cleanly for coverage reporting in CI

---

## No Framework Dependencies

The engine has **no FastAPI, Django, Flask, Celery, or similar framework dependencies**. This is deliberate.

**What was considered and rejected:**

| Framework | Why rejected |
|---|---|
| FastAPI | Adds async runtime, HTTP server, and 10+ transitive deps; unnecessary for a local CLI tool |
| Celery | Requires Redis or RabbitMQ as a broker; contradicts the zero-install-server goal |
| Django | Monolithic; designed for web applications, not CLI pipelines |
| SQLAlchemy | Powerful but large; the database access patterns here are simple enough for raw SQLite |

The engine is a **library + CLI**. Adding a framework would mean users need to run a server process, manage a message broker, and understand framework conventions — all for something that runs locally on a single host. Simplicity wins.

---

## Runs on Raspberry Pi

The Raspberry Pi is the **explicit lower bound** for resource usage:

- **RAM**: a Raspberry Pi 4 has 1–8 GB; the engine is designed to idle under 50 MB
- **CPU**: task execution is I/O-bound (waiting for API responses), not CPU-bound
- **Storage**: SQLite uses a single file; no separate data directory layout to manage
- **No background daemons**: the worker loop runs when `orch` is invoked; there is no persistent service to install (though systemd unit files can wrap it)

This constraint also makes the engine well-suited for **CI pipelines**, **GitHub Actions**, and **Docker containers** where resource limits are real and startup time matters.

---

## Summary

| Choice | Rationale |
|---|---|
| Python 3.10+ | Union types, `match`, broad availability |
| Pydantic V2 | Runtime validation, serialisation, self-documenting |
| SQLite + WAL | Zero-install, sufficient concurrency, fast test teardown |
| Click | Composable CLI commands, minimal boilerplate |
| PyYAML | Human-friendly multi-line templates |
| stdlib urllib | Zero extra HTTP dependency |
| pytest | Fixtures, parametrisation, coverage |
| No framework | Single-host tool; frameworks add overhead and server requirements |
| Raspberry Pi target | Forces minimal dependencies and low idle resource usage |
