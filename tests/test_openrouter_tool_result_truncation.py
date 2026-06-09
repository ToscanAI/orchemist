"""Committed regression tests for issue #800 — truncate large tool results
before appending to the OpenRouter message history.

Two layers, mirroring the reviewer's ask (review.md MAJOR finding):

  (a) ``truncate_tool_content`` — the pure char-cap helper in
      ``orchestration_engine.executors.openrouter_tools``. Over-cap returns the
      first ``cap`` chars + the marker (with ``N == len(full)`` reported, and a
      total length of ``cap + len(marker)``); under-cap AND exactly-at-cap pass
      through unchanged.

  (b) the OpenRouter executor's ``role: "tool"`` append site — driven offline by
      mocking the API (``_do_post``) to emit one tool call then a plain-text
      terminator, and patching ``_execute_tool`` to return a controlled result
      dict. Option-A behaviour: over-cap WITH a writable ``output_dir`` →
      truncated content carrying the marker AND a spill file holding the FULL
      output; no ``output_dir`` OR a spill-write failure → full content, NO
      marker, no exception.

Deterministic and offline — no network. Uses the real ``tool_result_cap`` kwarg
with a small cap so payloads go "over-cap" cheaply.

Mirrors the imports/fixtures/mocking style of ``test_openrouter_executor.py``
(``sys.path`` shim, ``OpenRouterExecutor`` construction, ``patch``-based mocking).
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestration_engine.executors.openrouter_executor import OpenRouterExecutor
from orchestration_engine.executors.openrouter_tools import (
    TOOL_RESULT_TRUNCATION_MARKER,
    truncate_tool_content,
)
from orchestration_engine.schemas import TaskSpec, TaskState, TaskType


# ---------------------------------------------------------------------------
# (a) Pure helper: truncate_tool_content
# ---------------------------------------------------------------------------


class TestTruncateToolContentHelper:
    """Contract 1 / 3: the pure char-cap helper."""

    def test_over_cap_truncates_with_marker(self):
        """Over-cap → first ``cap`` chars + marker; ``truncated`` flag is True.

        The kept prefix is EXACTLY ``content[:cap]`` and the marker is appended
        as a suffix (not counted within the cap).
        """
        cap = 50
        full = "x" * 200  # len 200 > cap 50
        spill_path = "/tmp/out/phase_toolcall_1.txt"

        out, truncated = truncate_tool_content(full, cap, spill_path)

        assert truncated is True
        # prefix is the first `cap` chars of the full content
        assert out[:cap] == full[:cap]
        # marker is appended verbatim after the prefix
        expected_marker = TOOL_RESULT_TRUNCATION_MARKER.format(n=len(full), path=spill_path)
        assert out == full[:cap] + expected_marker
        assert out.endswith(expected_marker)

    def test_marker_reports_N_equal_to_full_length(self):
        """The marker's N is the PRE-truncation total length (``len(full)``),
        not the kept-prefix length and not the cap."""
        cap = 30
        full = "a" * 137  # N must be reported as 137, not 30
        spill_path = "/somewhere/spill.txt"

        out, _ = truncate_tool_content(full, cap, spill_path)

        # N == len(full) appears literally in the marker.
        assert "137" in out
        assert f"truncated from {len(full)} chars" in out
        # And the path is named so the model can read the full output back.
        assert spill_path in out

    def test_total_stored_length_is_cap_plus_marker(self):
        """Total appended length == cap + len(marker) — the marker is additive,
        the cap bounds only the retained prefix (Contract 1.4)."""
        cap = 64
        full = "z" * 5000
        spill_path = "/o/p_toolcall_3.txt"

        out, truncated = truncate_tool_content(full, cap, spill_path)

        marker = TOOL_RESULT_TRUNCATION_MARKER.format(n=len(full), path=spill_path)
        assert truncated is True
        assert len(out) == cap + len(marker)
        # Stripping the marker leaves EXACTLY the first `cap` chars.
        assert out[: -len(marker)] == full[:cap]

    def test_under_cap_passthrough_unchanged(self):
        """Under-cap → returned unchanged, ``truncated`` is False, no marker."""
        cap = 100
        full = "short content"  # len << cap
        out, truncated = truncate_tool_content(full, cap, "/unused/path.txt")

        assert truncated is False
        assert out == full
        assert "truncated from" not in out

    def test_exactly_at_cap_passthrough_unchanged(self):
        """Boundary: ``len(content) == cap`` is passthrough (strict-greater
        trigger — truncation fires only when ``len > cap``)."""
        cap = 42
        full = "q" * cap  # exactly at the cap
        out, truncated = truncate_tool_content(full, cap, "/unused/path.txt")

        assert truncated is False
        assert out == full
        assert len(out) == cap
        assert "truncated from" not in out


# ---------------------------------------------------------------------------
# (b) Executor append-site (Option A), driven offline
# ---------------------------------------------------------------------------


def _tool_call_response(tool_name="read_file", args=None, call_id="call_abc"):
    """An OpenRouter API response that requests exactly one tool call."""
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(args or {"path": "big.txt"}),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _final_text_response(text="done"):
    """A plain-text (no tool_calls) response that terminates the loop."""
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 4},
    }


def _tool_task():
    """A tool-enabled (multi-turn loop) TaskSpec.

    ``TaskType.CODE`` so the COMMAND/ACCEPTANCE_RUN local fast-path is NOT taken;
    ``disable_tools=False`` so the tool loop runs.
    """
    return TaskSpec(
        type=TaskType.CODE,
        payload={"prompt": "do a thing", "disable_tools": False, "phase_id": "myphase"},
    )


def _run_loop_with_one_tool_call(executor, task, tool_result):
    """Drive the executor's tool loop offline for exactly one tool call.

    ``_do_post`` emits a tool-call response first, then a plain-text terminator.
    ``_execute_tool`` returns ``tool_result`` (bypassing real file/shell access).
    Returns the captured ``messages`` list appended during the loop.
    """
    responses = [_tool_call_response(), _final_text_response()]
    captured = {}

    real_run = executor._run_tool_loop

    # Capture the messages list by wrapping the append site indirectly: we patch
    # _execute_tool to return our controlled dict and read the resulting
    # role:"tool" message off the loop via a side-effect recorder.
    def fake_do_post(body):
        # body["messages"] is the live list the loop mutates; snapshot a ref so
        # the final state (post-append) is observable after the loop returns.
        captured["messages"] = body["messages"]
        return responses.pop(0)

    with patch.object(executor, "_do_post", side_effect=fake_do_post), patch.object(
        executor, "_execute_tool", return_value=tool_result
    ):
        result = real_run(
            task=task,
            worker_id="test",
            model="anthropic/claude-sonnet-4-6",
            prompt="do a thing",
            use_thinking=False,
            thinking_level="off",
            start_time=0.0,
            roots=task.payload["_roots"],
            jsonl_path=task.payload.get("_jsonl_path"),
            phase_id="myphase",
        )
    return result, captured["messages"]


def _tool_message(messages):
    """Return the single role:"tool" message appended during the loop."""
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1, f"expected exactly one tool message, got {len(tool_msgs)}"
    return tool_msgs[0]


class TestExecutorAppendSiteOptionA:
    """Contract 2 / 6 / 7: the role:"tool" append-site Option-A behaviour."""

    def test_over_cap_with_writable_output_dir_truncates_and_spills(self, tmp_path):
        """Over-cap WITH a writable ``output_dir`` → stored content is truncated +
        carries the marker, AND the spill file holds the COMPLETE content."""
        cap = 100
        executor = OpenRouterExecutor(api_key="sk-or-test", tool_result_cap=cap)
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        # A result whose json.dumps() length comfortably exceeds the cap.
        big = "B" * 5000
        tool_result = {"stdout": big}
        full_content = json.dumps(tool_result)
        assert len(full_content) > cap  # guard: this is genuinely over-cap

        task = _tool_task()
        task.payload["_roots"] = {"output_dir": str(output_dir), "tmp_dir": str(tmp_path)}

        result, messages = _run_loop_with_one_tool_call(executor, task, tool_result)
        assert result.state == TaskState.SUCCESS

        tool_msg = _tool_message(messages)
        stored = tool_msg["content"]

        # Stored content is truncated to cap + marker, prefix == first `cap` chars.
        marker = TOOL_RESULT_TRUNCATION_MARKER.format(n=len(full_content), path="X")
        # marker text varies by path, so assert structurally instead of exact:
        assert "truncated from" in stored
        assert f"truncated from {len(full_content)} chars" in stored
        assert stored[:cap] == full_content[:cap]
        assert len(stored) < len(full_content)  # genuinely shortened

        # The spill file exists and holds the COMPLETE, untruncated content.
        spill_path = output_dir / "myphase_toolcall_1.txt"
        assert spill_path.exists()
        assert spill_path.read_text(encoding="utf-8") == full_content
        # And the marker names that very path so the model can read it back.
        assert str(spill_path) in stored

    def test_under_cap_with_output_dir_unchanged_no_spill(self, tmp_path):
        """Under-cap WITH a writable ``output_dir`` → stored content is the full
        json.dumps unchanged, NO marker, and NO spill file is written."""
        cap = 100_000  # cap far above the small result → passthrough
        executor = OpenRouterExecutor(api_key="sk-or-test", tool_result_cap=cap)
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        tool_result = {"stdout": "small"}
        full_content = json.dumps(tool_result)
        assert len(full_content) <= cap

        task = _tool_task()
        task.payload["_roots"] = {"output_dir": str(output_dir), "tmp_dir": str(tmp_path)}

        result, messages = _run_loop_with_one_tool_call(executor, task, tool_result)
        assert result.state == TaskState.SUCCESS

        tool_msg = _tool_message(messages)
        assert tool_msg["content"] == full_content  # byte-identical passthrough
        assert "truncated from" not in tool_msg["content"]
        # No spill file is created for an under-cap call.
        assert not (output_dir / "myphase_toolcall_1.txt").exists()
        assert list(output_dir.glob("*_toolcall_*.txt")) == []

    def test_no_output_dir_stores_full_content_no_marker(self, tmp_path):
        """No ``output_dir`` (nowhere to spill) → full content stored, NO marker,
        no exception — even for an over-cap result (Option A / Outcome #6)."""
        cap = 100
        executor = OpenRouterExecutor(api_key="sk-or-test", tool_result_cap=cap)

        big = "C" * 5000
        tool_result = {"stdout": big}
        full_content = json.dumps(tool_result)
        assert len(full_content) > cap  # over-cap, but nowhere to spill

        task = _tool_task()
        # roots WITHOUT output_dir (tmp-only) — the no-place-to-spill case.
        task.payload["_roots"] = {"tmp_dir": str(tmp_path)}

        result, messages = _run_loop_with_one_tool_call(executor, task, tool_result)
        assert result.state == TaskState.SUCCESS  # no exception raised

        tool_msg = _tool_message(messages)
        assert tool_msg["content"] == full_content  # FULL content, untruncated
        assert "truncated from" not in tool_msg["content"]  # NO marker

    def test_spill_write_failure_stores_full_content_no_marker(self, tmp_path):
        """Over-cap WITH an ``output_dir`` but the spill write FAILS → full content
        stored, NO marker, no exception (Option A / Outcome #7).

        The failure is forced via a real I/O error: ``output_dir`` is a path whose
        parent is a regular FILE, so ``_write_spill_file``'s ``mkdir(parents=True)``
        raises ``NotADirectoryError`` (an ``OSError`` subclass) and returns False.
        """
        cap = 100
        executor = OpenRouterExecutor(api_key="sk-or-test", tool_result_cap=cap)

        # Create a regular file, then point output_dir at a child path UNDER it.
        blocker = tmp_path / "iam_a_file"
        blocker.write_text("not a directory", encoding="utf-8")
        bad_output_dir = blocker / "nested" / "out"  # parent is a file → mkdir fails

        big = "D" * 5000
        tool_result = {"stdout": big}
        full_content = json.dumps(tool_result)
        assert len(full_content) > cap  # over-cap → spill attempted

        task = _tool_task()
        task.payload["_roots"] = {
            "output_dir": str(bad_output_dir),
            "tmp_dir": str(tmp_path),
        }

        # Sanity: confirm the spill write genuinely fails for this destination
        # (so the test exercises the real failure path, not a mocked stub).
        from orchestration_engine.executors.openrouter_executor import _write_spill_file

        assert _write_spill_file(bad_output_dir / "probe.txt", "x") is False

        # The loop must not raise despite the unwritable spill destination.
        result, messages = _run_loop_with_one_tool_call(executor, task, tool_result)
        assert result.state == TaskState.SUCCESS  # no exception propagated

        tool_msg = _tool_message(messages)
        assert tool_msg["content"] == full_content  # FULL content, untruncated
        assert "truncated from" not in tool_msg["content"]  # NO marker
        # No usable spill file exists for read-back.
        assert not bad_output_dir.exists()
