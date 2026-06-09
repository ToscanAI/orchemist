"""§6.C — timeout-constant naming normalization (#929 / #942 timeout).

Part C is a RENAME-only normalization: the four executor default-timeout
constants are unified onto the public ``*_SECONDS`` convention (with
``openclaw_executor.DEFAULT_TIMEOUT_SECONDS`` as the already-conforming
template, left unchanged). Values and semantics are UNCHANGED and the constants
stay SEPARATE (1200 task / 120 shell / 120 bash-tool / 600 gemini).

These tests pin that the post-rename names import and carry the unchanged
values, AND that the OLD names are gone (no leftover alias) — so the rename
provably happened and no value/semantic drift occurred.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestration_engine import command_executor
from orchestration_engine import openclaw_executor
from orchestration_engine.executors import gemini_cli_executor, openrouter_tools


def test_c1_openclaw_constant_unchanged():
    """openclaw is the naming template — name + value unchanged (1200)."""
    assert openclaw_executor.DEFAULT_TIMEOUT_SECONDS == 1200


def test_c2_renamed_constants_importable_and_value_stable():
    """The renamed constants resolve to their unchanged values."""
    from orchestration_engine.command_executor import (
        DEFAULT_TIMEOUT_SECONDS as command_default,
    )
    from orchestration_engine.executors.openrouter_tools import (
        BASH_DEFAULT_TIMEOUT_SECONDS as bash_default,
    )
    from orchestration_engine.executors.gemini_cli_executor import (
        DEFAULT_TIMEOUT_SECONDS as gemini_default,
    )

    assert command_default == 120
    assert bash_default == 120
    assert gemini_default == 600


def test_c2_old_names_are_gone():
    """Regression guard: the pre-rename names must no longer exist."""
    assert not hasattr(command_executor, "DEFAULT_TIMEOUT")
    assert not hasattr(openrouter_tools, "BASH_DEFAULT_TIMEOUT_S")
    assert not hasattr(gemini_cli_executor, "_DEFAULT_TIMEOUT_SECONDS")


def test_c3_bash_tool_schema_default_unchanged():
    """The bash-tool JSON schema still defaults timeout_seconds to 120 — no
    value drift from the rename."""
    bash_schema = next(
        s
        for s in openrouter_tools.TOOL_SCHEMAS
        if s["function"]["name"] == "bash"
    )
    props = bash_schema["function"]["parameters"]["properties"]
    assert props["timeout_seconds"]["default"] == 120
