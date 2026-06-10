"""Tool definitions, sandbox validators, and handlers for the OpenRouter executor.

Implements the day-one toolset from ToscanAI/orchemist#794:
    read_file, write_file, edit_file, bash, grep, glob

Every public handler returns a JSON-serialisable dict that the executor
stringifies into a ``role: "tool"`` message for the model. Errors are
returned as ``{error: <code>, message: <str>}`` — handlers MUST NOT raise
past the dispatcher; uncaught exceptions are wrapped as
``tool_internal_error`` by the caller.

Security note: the bash deny-list is a UX guardrail to catch obvious
mistakes (typos, hallucinated ``sudo``, copy-pasted ``curl | sh``). It is
NOT a security boundary. For adversarial isolation, run the engine inside
``firejail``, a container, or a user namespace with restricted capabilities.
"""

from __future__ import annotations

import fnmatch
import os
import re
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

READ_FILE_BINARY_PROBE_BYTES = 8 * 1024
READ_FILE_UTF8_ERRORS_POLICY = "replace"

# Default bash-tool timeout in seconds (OpenRouter tool schema).
BASH_DEFAULT_TIMEOUT_SECONDS = 120
BASH_MAX_TIMEOUT_S = 600
BASH_SIGTERM_GRACE_S = 5
BASH_STREAM_CAP_BYTES = 256 * 1024

GREP_MATCH_CAP = 1000
GLOB_PATH_CAP = 500

JSONL_ARG_STR_TRUNCATE_CHARS = 200
JSONL_ARG_STR_MARKER = "…[truncated]"

# Issue #800: cap on the JSON-stringified tool-result CONTENT stored in the
# message history (chars, not bytes). Distinct from BASH_STREAM_CAP_BYTES, which
# caps raw bash stdout/stderr in BYTES before the result dict is built.
# NOTE: no leading "\n" — the marker is appended directly after the cap-length
# prefix so that stripping the marker leaves EXACTLY the first `cap` chars
# (behavioral Contract 1.4: total stored length == cap + len(marker)).
TOOL_RESULT_TRUNCATION_MARKER = "[... truncated from {n} chars. Full output at {path}]"

# Bash deny-list. UX guardrail only. See module docstring.
BASH_DENY_PATTERNS: list[str] = [
    r"rm\s+-rf\s+/",
    r"\bsudo\s+",
    r"curl[^|]*\|\s*(sh|bash)",
    r"wget[^|]*\|\s*(sh|bash)",
    r"\beval\s+",
    r"chmod\s+777\s+/",
    r">\s*/dev/sd[a-z]",
    r"mkfs\.",
    r"dd\s+if=.*of=/dev/",
    r":\(\)\{",
]
_BASH_DENY_COMPILED = [re.compile(p, re.IGNORECASE) for p in BASH_DENY_PATTERNS]


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI-compatible)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a text file from disk. Rejects binary files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or overwrite a text file on disk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace occurrences of old_string with new_string in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a shell command. Subject to deny-list and cwd sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "default": BASH_DEFAULT_TIMEOUT_SECONDS},
                    "cwd": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search for a regex pattern across files. Results capped at 1000 matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "glob": {"type": "string"},
                    "ignore_case": {"type": "boolean", "default": False},
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches", "count"],
                        "default": "content",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Match file paths against a glob pattern. Results capped at 500 paths (sorted).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Sandbox roots normalisation
# ---------------------------------------------------------------------------


def normalise_sandbox_roots(raw: Any) -> tuple[dict[str, str], bool]:
    """Normalise a ``task.payload["sandbox_roots"]`` value.

    Returns ``(normalised_dict, fallback_triggered)``. ``normalised_dict``
    contains only keys with truthy absolute-path-string values. ``fallback_triggered``
    is True when the caller-supplied input normalises to an empty set of roots
    (meaning the executor should fall back to ``{tmp_dir: tempfile.gettempdir()}``
    and log a single warning).

    Treats as absent:
    - ``None``
    - empty dict
    - empty string
    - the literal string ``"None"`` (Python ``str(None)`` gotcha)
    - any value whose ``bool()`` is False
    """
    if not isinstance(raw, dict):
        return ({"tmp_dir": tempfile.gettempdir()}, True)

    normalised: dict[str, str] = {}
    for key in ("repo_path", "output_dir", "tmp_dir"):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            value = str(value)
        if not value or value == "None":
            continue
        normalised[key] = value

    if not normalised:
        return ({"tmp_dir": tempfile.gettempdir()}, True)

    # Always ensure tmp_dir is present as a backstop even when caller only gave repo_path/output_dir.
    normalised.setdefault("tmp_dir", tempfile.gettempdir())
    return (normalised, False)


# ---------------------------------------------------------------------------
# Path sandbox
# ---------------------------------------------------------------------------


def _sandbox_root_paths(roots: dict[str, str]) -> list[Path]:
    return [Path(p).resolve() for p in roots.values() if p]


def _validate_path(raw_path: str, roots: dict[str, str]) -> tuple[Path | None, dict | None]:
    """Validate a tool-arg path against the sandbox roots.

    Returns ``(resolved_path, None)`` on success or ``(None, error_dict)`` on rejection.
    Distinguishes ``path_outside_sandbox`` (raw path outside all roots — pure-lexical check)
    from ``symlink_escape`` (raw path lexically inside a root, but the resolved target —
    after following symlinks — is outside).
    """
    if not isinstance(raw_path, str) or raw_path == "":
        return (None, {"error": "invalid_tool_call", "message": "path argument must be non-empty"})

    root_paths = _sandbox_root_paths(roots)
    if not root_paths:
        return (
            None,
            {"error": "no_sandbox_configured", "message": "no sandbox roots are configured"},
        )

    raw = Path(raw_path).expanduser()
    if not raw.is_absolute():
        raw = Path(os.getcwd()) / raw
    raw_normalised = Path(os.path.normpath(str(raw)))
    resolved = raw.resolve(strict=False)

    raw_inside_lexical = any(_path_within_lexical(raw_normalised, r) for r in root_paths)
    resolved_inside = any(_path_within_lexical(resolved, r) for r in root_paths)

    if resolved_inside:
        return (resolved, None)
    if raw_inside_lexical:
        return (
            None,
            {
                "error": "symlink_escape",
                "resolved_path": str(resolved),
                "message": f"symlink at {raw_path} resolves to {resolved} which is outside sandbox",
            },
        )
    return (
        None,
        {
            "error": "path_outside_sandbox",
            "resolved_path": str(resolved),
            "message": f"{resolved} is outside the allowed sandbox roots",
        },
    )


def _path_within_lexical(child: Path, parent: Path) -> bool:
    """Pure-lexical check: is `child` inside `parent`? No symlink resolution on `child`."""
    try:
        Path(os.path.normpath(str(child))).relative_to(parent)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def handle_read_file(args: dict, roots: dict[str, str], **_kwargs: Any) -> dict:
    path_arg = args.get("path", "")
    resolved, err = _validate_path(path_arg, roots)
    if err is not None:
        return err
    assert resolved is not None
    if not resolved.exists():
        return {"error": "not_found", "message": f"{resolved} does not exist"}
    if not resolved.is_file():
        return {"error": "not_a_file", "message": f"{resolved} is not a regular file"}

    # Binary probe + read.  Any uncaught OSError here bubbles to the dispatcher
    # which wraps it as `tool_internal_error` — that's the spec's contract for
    # "handler raises unexpectedly" (as opposed to our deliberate not_found /
    # binary_file / not_a_file paths above).
    with open(resolved, "rb") as fh:
        head = fh.read(READ_FILE_BINARY_PROBE_BYTES)
    if b"\x00" in head:
        return {
            "error": "binary_file",
            "message": (
                f"{resolved} appears to be binary (null bytes detected in first "
                f"{READ_FILE_BINARY_PROBE_BYTES} bytes); tool returns text only"
            ),
        }
    raw_bytes = resolved.read_bytes()
    content = raw_bytes.decode("utf-8", errors=READ_FILE_UTF8_ERRORS_POLICY)
    return {"content": content, "size_bytes": len(raw_bytes)}


def handle_write_file(args: dict, roots: dict[str, str], **_kwargs: Any) -> dict:
    path_arg = args.get("path", "")
    content = args.get("content")
    if not isinstance(path_arg, str) or path_arg == "":
        return {"error": "invalid_tool_call", "message": "path argument must be non-empty"}
    if content is None:
        return {"error": "invalid_tool_call", "message": "missing required argument content"}
    if not isinstance(content, str):
        content = str(content)

    resolved, err = _validate_path(path_arg, roots)
    if err is not None:
        return err
    assert resolved is not None
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        resolved.write_bytes(data)
        return {"bytes_written": len(data)}
    except OSError as exc:
        return {"error": "write_error", "message": f"{exc}"}


def handle_edit_file(args: dict, roots: dict[str, str], **_kwargs: Any) -> dict:
    path_arg = args.get("path", "")
    old_string = args.get("old_string")
    new_string = args.get("new_string")
    replace_all = bool(args.get("replace_all", False))

    if not isinstance(path_arg, str) or path_arg == "":
        return {"error": "invalid_tool_call", "message": "path argument must be non-empty"}
    if old_string is None:
        return {"error": "invalid_tool_call", "message": "missing required argument old_string"}
    if not isinstance(old_string, str) or old_string == "":
        return {"error": "invalid_tool_call", "message": "old_string must be non-empty"}
    if new_string is None:
        return {"error": "invalid_tool_call", "message": "missing required argument new_string"}
    if not isinstance(new_string, str):
        new_string = str(new_string)

    resolved, err = _validate_path(path_arg, roots)
    if err is not None:
        return err
    assert resolved is not None
    if not resolved.exists():
        return {"error": "not_found", "message": f"{resolved} does not exist"}

    try:
        raw = resolved.read_bytes()
        text = raw.decode("utf-8", errors=READ_FILE_UTF8_ERRORS_POLICY)
    except OSError as exc:
        return {"error": "read_error", "message": f"{exc}"}

    count = text.count(old_string)
    if count == 0:
        return {
            "error": "old_string_not_found",
            "message": f"old_string does not appear in {resolved}",
        }
    if count > 1 and not replace_all:
        return {
            "error": "ambiguous_edit",
            "matches": count,
            "message": (
                f"old_string appears {count} times in {resolved}; "
                "set replace_all: true or include more surrounding context to disambiguate"
            ),
        }

    # No-op edit: old == new → don't rewrite disk (preserves mtime).
    if old_string == new_string:
        return {"replacements": count}

    new_text = (
        text.replace(old_string, new_string)
        if replace_all
        else text.replace(old_string, new_string, 1)
    )
    try:
        resolved.write_bytes(new_text.encode("utf-8"))
    except OSError as exc:
        return {"error": "write_error", "message": f"{exc}"}
    return {"replacements": count if replace_all else 1}


def handle_bash(
    args: dict,
    roots: dict[str, str],
    is_cancelled: Optional[Callable[[], bool]] = None,
    **_kwargs: Any,
) -> dict:
    command = args.get("command")
    if not isinstance(command, str) or command == "":
        return {"error": "invalid_tool_call", "message": "command argument must be non-empty"}

    for pat in _BASH_DENY_COMPILED:
        m = pat.search(command)
        if m:
            return {
                "error": "command_denied",
                "message": f"command matches guardrail pattern: {m.group(0)}",
            }

    timeout_seconds = args.get("timeout_seconds", BASH_DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        return {"error": "invalid_tool_call", "message": "timeout_seconds must be an integer"}
    if timeout_seconds <= 0 or timeout_seconds > BASH_MAX_TIMEOUT_S:
        return {
            "error": "invalid_tool_call",
            "message": f"timeout_seconds must be in [1, {BASH_MAX_TIMEOUT_S}]",
        }

    cwd_arg = args.get("cwd")
    if cwd_arg is not None:
        if not isinstance(cwd_arg, str) or cwd_arg == "":
            return {"error": "invalid_tool_call", "message": "cwd must be a non-empty string"}
        cwd_resolved, cwd_err = _validate_path(cwd_arg, roots)
        if cwd_err is not None:
            return cwd_err
        assert cwd_resolved is not None
        if not cwd_resolved.exists() or not cwd_resolved.is_dir():
            return {
                "error": "invalid_tool_call",
                "message": f"cwd {cwd_resolved} is not a directory",
            }
        cwd = str(cwd_resolved)
    else:
        cwd = roots.get("repo_path") or roots.get("tmp_dir") or tempfile.gettempdir()

    return _run_bash(command, cwd, timeout_seconds, is_cancelled)


def _run_bash(
    command: str,
    cwd: str,
    timeout_seconds: int,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> dict:
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    start = time.monotonic()
    poll_interval = 0.1
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=poll_interval)
                exit_code = proc.returncode
                timed_out = False
                break
            except subprocess.TimeoutExpired:
                if is_cancelled is not None and is_cancelled():
                    _terminate_tree(proc)
                    try:
                        stdout, stderr = proc.communicate(timeout=BASH_SIGTERM_GRACE_S)
                    except subprocess.TimeoutExpired:
                        _kill_tree(proc)
                        stdout, stderr = proc.communicate(timeout=1)
                    stdout_text_c, stdout_trunc_c = _truncate_stream(stdout or b"", "stdout")
                    stderr_text_c, stderr_trunc_c = _truncate_stream(stderr or b"", "stderr")
                    return {
                        "error": "aborted",
                        "message": "bash aborted by user cancel",
                        "stdout": stdout_text_c,
                        "stderr": stderr_text_c,
                        "exit_code": proc.returncode if proc.returncode is not None else -1,
                        "truncated": {"stdout": stdout_trunc_c, "stderr": stderr_trunc_c},
                    }
                elapsed = time.monotonic() - start
                if elapsed >= timeout_seconds:
                    timed_out = True
                    _terminate_tree(proc)
                    try:
                        stdout, stderr = proc.communicate(timeout=BASH_SIGTERM_GRACE_S)
                    except subprocess.TimeoutExpired:
                        _kill_tree(proc)
                        stdout, stderr = proc.communicate(timeout=1)
                    exit_code = proc.returncode
                    break
    except Exception as exc:
        try:
            _kill_tree(proc)
        except Exception:
            pass
        return {"error": "bash_failed", "message": f"{exc}"}

    stdout_text, stdout_truncated = _truncate_stream(stdout, "stdout")
    stderr_text, stderr_truncated = _truncate_stream(stderr, "stderr")

    if timed_out:
        return {
            "error": "timeout",
            "message": f"command exceeded {timeout_seconds} seconds",
            "stdout": stdout_text,
            "stderr": stderr_text,
            "exit_code": exit_code if exit_code is not None else -1,
            "truncated": {"stdout": stdout_truncated, "stderr": stderr_truncated},
        }

    return {
        "stdout": stdout_text,
        "stderr": stderr_text,
        "exit_code": exit_code,
        "truncated": {"stdout": stdout_truncated, "stderr": stderr_truncated},
    }


def _terminate_tree(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except Exception:
            pass


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except Exception:
            pass


def _truncate_stream(data: bytes, label: str) -> tuple[str, bool]:
    text = data.decode("utf-8", errors=READ_FILE_UTF8_ERRORS_POLICY)
    if len(text.encode("utf-8")) <= BASH_STREAM_CAP_BYTES:
        return (text, False)
    encoded = text.encode("utf-8")[:BASH_STREAM_CAP_BYTES]
    truncated = encoded.decode("utf-8", errors="ignore")
    dropped = len(data) - len(encoded)
    return (truncated + f"\n[... {label} truncated, {dropped} bytes dropped ...]\n", True)


def truncate_tool_content(content: str, cap: int, spill_path: str) -> tuple[str, bool]:
    """Cap the JSON-stringified tool-result CONTENT at ``cap`` CHARACTERS for the
    message-history append (issue #800).

    Returns ``(content_to_append, truncated)``:
      - if ``len(content) <= cap``: ``(content, False)`` — passthrough, byte-identical
        to the pre-#800 behaviour.
      - else: ``(content[:cap] + marker, True)``, where the marker names the full
        count ``N == len(content)`` (the PRE-truncation total) and the on-disk spill
        path the model can read back via ``read_file``.

    Char-scoped (``len()``), distinct from the byte-scoped :func:`_truncate_stream`.
    Pure function — no disk I/O; the caller writes the spill file separately.
    """
    if len(content) <= cap:
        return content, False
    marker = TOOL_RESULT_TRUNCATION_MARKER.format(n=len(content), path=spill_path)
    return content[:cap] + marker, True


def handle_grep(args: dict, roots: dict[str, str], **_kwargs: Any) -> dict:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or pattern == "":
        return {"error": "invalid_tool_call", "message": "pattern argument must be non-empty"}
    ignore_case = bool(args.get("ignore_case", False))
    output_mode = args.get("output_mode", "content")
    if output_mode not in {"content", "files_with_matches", "count"}:
        return {
            "error": "invalid_tool_call",
            "message": "output_mode must be one of content/files_with_matches/count",
        }

    supplied = args.get("path")
    if supplied is not None and (not isinstance(supplied, str) or supplied == ""):
        return {
            "error": "invalid_tool_call",
            "message": "path must be a non-empty string when provided",
        }
    path_arg = supplied or roots.get("repo_path") or roots.get("tmp_dir") or tempfile.gettempdir()
    resolved, err = _validate_path(path_arg, roots)
    if err is not None:
        return err
    assert resolved is not None
    if not resolved.exists():
        return {"error": "not_found", "message": f"{resolved} does not exist"}

    glob_filter = args.get("glob")
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return {"error": "invalid_tool_call", "message": f"invalid regex pattern: {exc}"}

    matches: list[dict] = []
    truncated = False
    files_with_match: set[str] = set()
    match_count = 0

    if resolved.is_file():
        files = [resolved]
    else:
        files = [p for p in resolved.rglob("*") if p.is_file()]
        if glob_filter:
            files = [p for p in files if fnmatch.fnmatch(p.name, glob_filter)]

    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8", errors=READ_FILE_UTF8_ERRORS_POLICY) as fh:
                for lineno, line in enumerate(fh, start=1):
                    if regex.search(line):
                        match_count += 1
                        files_with_match.add(str(file_path))
                        if output_mode == "content":
                            if len(matches) >= GREP_MATCH_CAP:
                                truncated = True
                                continue
                            matches.append(
                                {
                                    "path": str(file_path),
                                    "line": lineno,
                                    "text": line.rstrip("\n"),
                                }
                            )
        except (OSError, UnicodeError):
            continue

    if output_mode == "content":
        return {"matches": matches, "truncated": truncated}
    if output_mode == "files_with_matches":
        listed = sorted(files_with_match)
        truncated = len(listed) > GREP_MATCH_CAP
        return {"matches": listed[:GREP_MATCH_CAP], "truncated": truncated}
    # output_mode == "count"
    return {"count": match_count, "truncated": False}


def handle_glob(args: dict, roots: dict[str, str], **_kwargs: Any) -> dict:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or pattern == "":
        return {"error": "invalid_tool_call", "message": "pattern argument must be non-empty"}

    supplied = args.get("path")
    if supplied is not None and (not isinstance(supplied, str) or supplied == ""):
        return {
            "error": "invalid_tool_call",
            "message": "path must be a non-empty string when provided",
        }
    path_arg = supplied or roots.get("repo_path") or roots.get("tmp_dir") or tempfile.gettempdir()
    resolved, err = _validate_path(path_arg, roots)
    if err is not None:
        return err
    assert resolved is not None
    if not resolved.exists() or not resolved.is_dir():
        return {"error": "not_found", "message": f"{resolved} is not an existing directory"}

    collected = sorted(str(p) for p in resolved.rglob(pattern) if p.is_file())
    truncated = len(collected) > GLOB_PATH_CAP
    return {"paths": collected[:GLOB_PATH_CAP], "truncated": truncated}


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

TOOL_DISPATCH: dict[str, Callable[..., dict]] = {
    "read_file": handle_read_file,
    "write_file": handle_write_file,
    "edit_file": handle_edit_file,
    "bash": handle_bash,
    "grep": handle_grep,
    "glob": handle_glob,
}


# ---------------------------------------------------------------------------
# JSONL args / result summaries
# ---------------------------------------------------------------------------


def summarise_args(args: dict) -> dict:
    """Return a shallow copy of args with long/multi-line strings truncated + escaped."""
    out: dict = {}
    for key, value in args.items():
        if isinstance(value, str):
            escaped = (
                value.replace("\\", "\\\\")
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t")
            )
            if len(escaped) > JSONL_ARG_STR_TRUNCATE_CHARS:
                escaped = escaped[:JSONL_ARG_STR_TRUNCATE_CHARS] + JSONL_ARG_STR_MARKER
            out[key] = escaped
        else:
            out[key] = value
    return out


def summarise_result(tool_name: str, result: dict) -> str:
    """Short human-readable string for the JSONL line."""
    if "error" in result:
        return f"error: {result['error']}"
    if tool_name == "read_file":
        size = len(result.get("content", ""))
        return f"{size} chars read"
    if tool_name == "write_file":
        return f"{result.get('bytes_written', 0)} bytes written"
    if tool_name == "edit_file":
        return f"{result.get('replacements', 0)} replacements"
    if tool_name == "bash":
        stdout_len = len(result.get("stdout", ""))
        return f"exit {result.get('exit_code', '?')}, stdout {stdout_len} chars"
    if tool_name == "grep":
        if "count" in result:
            return f"{result['count']} matches"
        return f"{len(result.get('matches', []))} matches"
    if tool_name == "glob":
        return f"{len(result.get('paths', []))} paths"
    return "ok"


def iso_now() -> str:
    # Single datetime.now() call — avoids tick-boundary race between seconds and microseconds.
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(now.microsecond / 1000):03d}Z"
