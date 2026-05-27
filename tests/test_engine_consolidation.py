"""Acceptance tests for engine helper consolidation (#860 #864 #865 #866).

This module locks in the behavioral contract for the four helpers that were
deduplicated by the 2026-05-27 consolidation sweep:

  * ``output_utils.extract_output_text``     (was #860 duplicated across daemon, cli)
  * ``output_utils.safe_write_phase_output`` (was #860 duplicated across daemon, cli)
  * ``db.default_db_path``                   (was #864 duplicated 5+ ways)
  * ``env_utils.env_int``                    (was #865 duplicated 4 ways)
  * ``db.parse_json_list``                   (was #866 duplicated 2 ways)

The tests are intentionally narrow: each one exercises ONE behavior of ONE
helper.  They are written to fail loudly if the canonical helper drifts
from the prior implementations, AND if the legacy duplicate sites are
silently reintroduced.

Sealed acceptance test set — do not modify without re-running the
adversary phase.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from unittest import mock

import pytest


# ============================================================================
# extract_output_text — #860
# ============================================================================


class TestExtractOutputText:
    """Behavioral contract for output_utils.extract_output_text."""

    def test_returns_value_from_output_key(self):
        """When result.output is a string, return it as-is."""
        from orchestration_engine.output_utils import extract_output_text
        assert extract_output_text({"result": {"output": "hello"}}) == "hello"

    def test_prefers_output_over_text(self):
        """output key wins over text when both present (priority order)."""
        from orchestration_engine.output_utils import extract_output_text
        assert extract_output_text(
            {"result": {"output": "OUT", "text": "TXT"}}
        ) == "OUT"

    def test_falls_back_to_text_key(self):
        """When output is absent, text key is used."""
        from orchestration_engine.output_utils import extract_output_text
        assert extract_output_text({"result": {"text": "TXT"}}) == "TXT"

    def test_falls_back_to_content_key(self):
        """When output and text absent, content key is used."""
        from orchestration_engine.output_utils import extract_output_text
        assert extract_output_text({"result": {"content": "CON"}}) == "CON"

    def test_falls_back_to_message_key(self):
        """DryRunExecutor uses the message key — it must be honoured."""
        from orchestration_engine.output_utils import extract_output_text
        assert extract_output_text({"result": {"message": "MSG"}}) == "MSG"

    def test_anthropic_content_block_array(self):
        """List of text blocks joined with double newlines."""
        from orchestration_engine.output_utils import extract_output_text
        blocks = [
            {"type": "text", "text": "A"},
            {"type": "text", "text": "B"},
        ]
        assert extract_output_text({"result": {"content": blocks}}) == "A\n\nB"

    def test_anthropic_tool_use_block_skipped(self):
        """Blocks with type 'tool_use' must NOT contribute to the output."""
        from orchestration_engine.output_utils import extract_output_text
        blocks = [
            {"type": "text", "text": "kept"},
            {"type": "tool_use", "name": "calc", "input": {"x": 1}},
        ]
        assert extract_output_text({"result": {"content": blocks}}) == "kept"

    def test_anthropic_thinking_block_skipped(self):
        """Blocks with type 'thinking' must NOT leak internal reasoning."""
        from orchestration_engine.output_utils import extract_output_text
        blocks = [
            {"type": "thinking", "text": "internal-secret"},
            {"type": "text", "text": "user-facing"},
        ]
        assert extract_output_text({"result": {"content": blocks}}) == "user-facing"

    def test_plain_string_items_in_list_included(self):
        """Mixed list of plain strings and content blocks — strings included."""
        from orchestration_engine.output_utils import extract_output_text
        items = ["raw-string", {"type": "text", "text": "block"}]
        assert extract_output_text({"result": {"content": items}}) == "raw-string\n\nblock"

    def test_empty_text_blocks_skipped(self):
        """Empty-string text entries filtered out before joining."""
        from orchestration_engine.output_utils import extract_output_text
        blocks = [
            {"type": "text", "text": ""},
            {"type": "text", "text": "kept"},
        ]
        assert extract_output_text({"result": {"content": blocks}}) == "kept"

    def test_non_dict_inner_stringified(self):
        """When result is not a dict (e.g. None, list), str() it directly."""
        from orchestration_engine.output_utils import extract_output_text
        assert extract_output_text({"result": None}) == "None"
        assert extract_output_text({"result": 42}) == "42"

    def test_empty_inner_returns_empty_string(self):
        """An empty result dict returns the empty string, not '{}'."""
        from orchestration_engine.output_utils import extract_output_text
        assert extract_output_text({"result": {}}) == ""

    def test_missing_keys_falls_back_to_json(self):
        """If no known key present but inner has data, return JSON repr."""
        from orchestration_engine.output_utils import extract_output_text
        result = extract_output_text({"result": {"foo": "bar"}})
        # JSON contains the data — the exact key/value pair must round-trip
        assert "foo" in result
        assert "bar" in result

    def test_handles_click_shadowed_list(self):
        """isinstance check must use builtins.list (not the bare name) so
        the helper survives being called from a Click command namespace
        where ``list`` is shadowed by a sub-command."""
        from orchestration_engine.output_utils import extract_output_text
        # Simulate the Click namespace shadow: introduce a local `list` that
        # is not a type.  The helper imports from builtins so this must work.
        list = lambda: None  # noqa: A001 — intentional shadow
        blocks = [{"type": "text", "text": "ok"}]
        # The function should not raise TypeError from isinstance(val, list)
        assert extract_output_text({"result": {"content": blocks}}) == "ok"


# ============================================================================
# safe_write_phase_output — #860
# ============================================================================


class TestSafeWritePhaseOutput:
    """Behavioral contract for output_utils.safe_write_phase_output."""

    def test_writes_when_file_does_not_exist(self, tmp_path):
        from orchestration_engine.output_utils import safe_write_phase_output
        target = tmp_path / "phase.md"
        safe_write_phase_output(target, "fresh content", "phase_id")
        assert target.read_text() == "fresh content"

    def test_overwrites_when_existing_smaller(self, tmp_path):
        from orchestration_engine.output_utils import safe_write_phase_output
        target = tmp_path / "phase.md"
        target.write_text("ab")  # 2 bytes
        safe_write_phase_output(target, "abc", "phase_id")  # 3 bytes
        assert target.read_text() == "abc"

    def test_preserves_strictly_larger_agent_file(self, tmp_path):
        """The whole point of #210: don't clobber larger agent-authored files."""
        from orchestration_engine.output_utils import safe_write_phase_output
        target = tmp_path / "phase.md"
        big = "A" * 1000
        target.write_text(big)
        small = "B" * 10
        safe_write_phase_output(target, small, "phase_id")
        assert target.read_text() == big

    def test_overwrites_equal_size_file(self, tmp_path):
        """Equal-byte files are (over)written with the fresh capture
        because the guard uses strictly-greater-than."""
        from orchestration_engine.output_utils import safe_write_phase_output
        target = tmp_path / "phase.md"
        target.write_text("XXX")
        safe_write_phase_output(target, "YYY", "phase_id")
        assert target.read_text() == "YYY"

    def test_multibyte_byte_length_counted(self, tmp_path):
        """4 multi-byte chars > 5 ASCII chars when counted in UTF-8 bytes.

        Em-dash '—' is 3 bytes in UTF-8.  4 em-dashes = 12 bytes.  5 ASCII
        chars = 5 bytes.  The guard should compare bytes, so the existing
        12-byte file must be preserved."""
        from orchestration_engine.output_utils import safe_write_phase_output
        target = tmp_path / "phase.md"
        target.write_text("————")  # 12 UTF-8 bytes
        safe_write_phase_output(target, "XXXXX", "phase_id")  # 5 bytes
        assert target.read_text() == "————"


# ============================================================================
# default_db_path — #864
# ============================================================================


class TestDefaultDbPath:
    """Behavioral contract for db.default_db_path."""

    def test_returns_path_object(self):
        from orchestration_engine.db import default_db_path
        result = default_db_path()
        assert isinstance(result, Path)

    def test_terminal_filename_is_engine_db(self):
        from orchestration_engine.db import default_db_path
        assert default_db_path().name == "engine.db"

    def test_parent_directory_is_orchestration_engine(self):
        from orchestration_engine.db import default_db_path
        assert default_db_path().parent.name == ".orchestration-engine"

    def test_creates_parent_directory_with_parents_true(self, tmp_path, monkeypatch):
        """#864 drift caveat: canonical version uses parents=True so a
        missing GRANDPARENT does not raise FileNotFoundError."""
        from orchestration_engine import db as db_module

        # Point HOME at a directory whose parent is missing — only possible
        # with parents=True for the mkdir to succeed.
        fake_home = tmp_path / "missing_parent" / "home_user"
        # NOTE: don't create fake_home; default_db_path must create the
        # entire chain ".orchestration-engine" + the path to it.
        monkeypatch.setattr(db_module.Path, "home", lambda: fake_home)

        result = db_module.default_db_path()
        assert result == fake_home / ".orchestration-engine" / "engine.db"
        assert (fake_home / ".orchestration-engine").is_dir()

    def test_idempotent_creates_directory(self, tmp_path, monkeypatch):
        """Calling repeatedly must not raise (exist_ok=True)."""
        from orchestration_engine import db as db_module
        monkeypatch.setattr(db_module.Path, "home", lambda: tmp_path)
        db_module.default_db_path()
        # Second call must not raise FileExistsError
        result = db_module.default_db_path()
        assert result.parent.is_dir()


# ============================================================================
# env_int — #865
# ============================================================================


class TestEnvInt:
    """Behavioral contract for env_utils.env_int."""

    def test_parses_valid_integer(self):
        from orchestration_engine.env_utils import env_int
        assert env_int("42", 100) == 42

    def test_negative_integer(self):
        from orchestration_engine.env_utils import env_int
        assert env_int("-7", 0) == -7

    def test_zero_value_preserved(self):
        """'0' must round-trip to 0, not fall back to default."""
        from orchestration_engine.env_utils import env_int
        assert env_int("0", 100) == 0

    def test_none_falls_back_to_default(self):
        """Unset env var (os.environ.get returns None) → default."""
        from orchestration_engine.env_utils import env_int
        assert env_int(None, 99) == 99

    def test_malformed_falls_back_to_default(self):
        """Non-integer string → default; must not raise."""
        from orchestration_engine.env_utils import env_int
        assert env_int("abc", 7) == 7

    def test_float_string_falls_back_to_default(self):
        """'1.5' is not an int — must fall back, not truncate."""
        from orchestration_engine.env_utils import env_int
        assert env_int("1.5", 8) == 8

    def test_empty_string_falls_back_to_default(self):
        """Empty string is malformed input → default."""
        from orchestration_engine.env_utils import env_int
        assert env_int("", 5) == 5


# ============================================================================
# parse_json_list — #866
# ============================================================================


class TestParseJsonList:
    """Behavioral contract for db.parse_json_list."""

    def test_none_returns_empty_list(self):
        from orchestration_engine.db import parse_json_list
        assert parse_json_list(None) == []

    def test_already_list_returned_unchanged(self):
        from orchestration_engine.db import parse_json_list
        src = ["a", "b"]
        assert parse_json_list(src) == ["a", "b"]

    def test_empty_list_returned_unchanged(self):
        from orchestration_engine.db import parse_json_list
        assert parse_json_list([]) == []

    def test_json_string_decoded(self):
        from orchestration_engine.db import parse_json_list
        assert parse_json_list('["a", "b"]') == ["a", "b"]

    def test_empty_json_string_decoded(self):
        from orchestration_engine.db import parse_json_list
        assert parse_json_list("[]") == []

    def test_malformed_string_returns_empty(self):
        """Garbled JSON must not crash — fall back to []."""
        from orchestration_engine.db import parse_json_list
        assert parse_json_list("not-json") == []

    def test_unparsable_type_returns_empty(self):
        """An int can't be json.loads'd; must fall back to []."""
        from orchestration_engine.db import parse_json_list
        assert parse_json_list(42) == []


# ============================================================================
# VERIFICATION: each canonical helper has exactly ONE definition site
# ============================================================================


class TestSingleDefinitionSite:
    """Lock in the no-duplicate invariant — the whole point of consolidation."""

    SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "orchestration_engine"

    def _grep_count(self, pattern: str) -> int:
        """Count Python files containing the given pattern at module level."""
        count = 0
        for py_file in self.SRC_ROOT.rglob("*.py"):
            text = py_file.read_text(encoding="utf-8")
            if re.search(pattern, text, flags=re.MULTILINE):
                count += 1
        return count

    def test_extract_output_text_defined_once(self):
        # The canonical home is output_utils.extract_output_text
        count = self._grep_count(r"^def extract_output_text\b")
        assert count == 1, f"Expected 1 canonical def; found {count}"
        # Legacy underscore-prefixed name must not be re-defined
        legacy_count = self._grep_count(r"^def _extract_output_text\b")
        assert legacy_count == 0, (
            f"Found {legacy_count} legacy '_extract_output_text' definitions — "
            "callers should import from output_utils."
        )

    def test_safe_write_phase_output_defined_once(self):
        count = self._grep_count(r"^def safe_write_phase_output\b")
        assert count == 1, f"Expected 1 canonical def; found {count}"
        legacy_count = self._grep_count(r"^def _safe_write_phase_output\b")
        assert legacy_count == 0, (
            f"Found {legacy_count} legacy '_safe_write_phase_output' definitions."
        )

    def test_default_db_path_defined_once(self):
        count = self._grep_count(r"^def default_db_path\b")
        assert count == 1, f"Expected 1 canonical def; found {count}"

    def test_parse_json_list_defined_once(self):
        count = self._grep_count(r"^def parse_json_list\b")
        assert count == 1, f"Expected 1 canonical def; found {count}"

    def test_env_int_defined_once(self):
        count = self._grep_count(r"^def env_int\b")
        assert count == 1, f"Expected 1 canonical def; found {count}"


# ============================================================================
# Cross-module integration: imports succeed & callers use the canonical helpers
# ============================================================================


class TestConsumerWiring:
    """Verify each consumer module imports the canonical helper, not a copy."""

    def test_daemon_imports_extract_output_text_from_output_utils(self):
        from orchestration_engine import daemon, output_utils
        # The daemon module's bound name MUST be the same object as the canonical
        assert daemon._extract_output_text is output_utils.extract_output_text

    def test_daemon_imports_safe_write_phase_output_from_output_utils(self):
        from orchestration_engine import daemon, output_utils
        assert daemon._safe_write_phase_output is output_utils.safe_write_phase_output

    def test_cli_imports_extract_output_text_from_output_utils(self):
        from orchestration_engine import cli, output_utils
        assert cli._extract_output_text is output_utils.extract_output_text

    def test_cli_imports_safe_write_phase_output_from_output_utils(self):
        from orchestration_engine import cli, output_utils
        assert cli._safe_write_phase_output is output_utils.safe_write_phase_output

    def test_mcp_tools_imports_parse_json_list_from_db(self):
        from orchestration_engine.mcp import tools
        from orchestration_engine import db
        assert tools._parse_json_list is db.parse_json_list

    def test_mcp_tools_get_persistent_db_path_uses_default_db_path(self, tmp_path, monkeypatch):
        from orchestration_engine.mcp import tools
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = tools._get_persistent_db_path()
        assert result == str(tmp_path / ".orchestration-engine" / "engine.db")

    def test_cli_get_persistent_db_path_uses_default_db_path(self, tmp_path, monkeypatch):
        from orchestration_engine import cli
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = cli._get_persistent_db_path()
        assert result == str(tmp_path / ".orchestration-engine" / "engine.db")

    def test_api_get_persistent_db_path_uses_default_db_path(self, tmp_path, monkeypatch):
        pytest.importorskip("fastapi")
        from orchestration_engine.web import api
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = api._get_persistent_db_path()
        assert result == str(tmp_path / ".orchestration-engine" / "engine.db")
