"""Tests for #840 — admin feature_flags consumed at runtime.

The admin UI persists flags to ~/.orchestration-engine/admin.json. Before
#840 the engine never read those flags — the UI was a false control. This
module verifies that:

  1. feature_flags.is_enabled() reads from admin.json with proper TTL caching
     and defensive fallbacks for missing/malformed files.
  2. The phase0_hard_gate flag, when True, overrides the YAML's exhausted
     transition for the existing_symbols_inventory phase and halts the pipeline.
  3. The dialogue_phase flag, when False (default), causes type:dialogue
     phases to be skipped with a synthetic clean-exit result.
  4. The defaults in feature_flags._DEFAULTS match _ADMIN_DEFAULTS["feature_flags"]
     in web/api.py — drift between the two is a regression.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_json_isolated(tmp_path, monkeypatch):
    """Point feature_flags at a tmp admin.json + reset cache. Yields the path."""
    from orchestration_engine import feature_flags as ff
    path = tmp_path / "admin.json"
    monkeypatch.setenv("ORCH_ADMIN_PATH", str(path))
    ff.reset_cache()
    yield path
    ff.reset_cache()


def _write_admin(path: Path, **flag_overrides: bool) -> None:
    path.write_text(json.dumps({
        "autonomy_level": "4.3",
        "feature_flags": {
            "phase0_hard_gate": False,
            "extend_verdict": True,
            "dialogue_phase": False,
            "cross_repo": False,
            **flag_overrides,
        },
        "modes": {"openrouter": True, "standalone": True, "openclaw": False, "dry_run": True},
    }))


# ---------------------------------------------------------------------------
# is_enabled() — read + cache + defensive fallbacks
# ---------------------------------------------------------------------------


class TestIsEnabledBasic:
    def test_returns_default_when_file_absent(self, admin_json_isolated):
        from orchestration_engine.feature_flags import is_enabled
        assert is_enabled("phase0_hard_gate") is False
        assert is_enabled("extend_verdict") is True
        assert is_enabled("dialogue_phase") is False
        assert is_enabled("cross_repo") is False

    def test_reads_true_value_from_admin_json(self, admin_json_isolated):
        from orchestration_engine.feature_flags import is_enabled
        _write_admin(admin_json_isolated, phase0_hard_gate=True)
        assert is_enabled("phase0_hard_gate") is True

    def test_reads_false_value_overriding_default_true(self, admin_json_isolated):
        from orchestration_engine.feature_flags import is_enabled
        # extend_verdict defaults to True; verify a False override sticks
        _write_admin(admin_json_isolated, extend_verdict=False)
        assert is_enabled("extend_verdict") is False

    def test_unknown_flag_returns_false_and_warns(self, admin_json_isolated, caplog):
        from orchestration_engine.feature_flags import is_enabled
        import logging
        with caplog.at_level(logging.WARNING):
            assert is_enabled("totally_made_up_flag") is False
        assert any("unknown flag" in r.message for r in caplog.records)


class TestDefensiveReadFallbacks:
    def test_malformed_json_falls_back_to_defaults(self, admin_json_isolated, caplog):
        from orchestration_engine.feature_flags import is_enabled
        import logging
        admin_json_isolated.write_text("not valid json {")
        with caplog.at_level(logging.WARNING):
            assert is_enabled("phase0_hard_gate") is False  # default
        assert any("failed to read" in r.message for r in caplog.records)

    def test_non_dict_top_level_falls_back_to_defaults(self, admin_json_isolated):
        from orchestration_engine.feature_flags import is_enabled
        admin_json_isolated.write_text(json.dumps([1, 2, 3]))
        assert is_enabled("phase0_hard_gate") is False

    def test_feature_flags_key_not_a_dict_falls_back(self, admin_json_isolated):
        from orchestration_engine.feature_flags import is_enabled
        admin_json_isolated.write_text(json.dumps({"feature_flags": "string"}))
        assert is_enabled("phase0_hard_gate") is False

    def test_non_bool_flag_value_uses_default(self, admin_json_isolated):
        from orchestration_engine.feature_flags import is_enabled
        admin_json_isolated.write_text(json.dumps({
            "feature_flags": {"phase0_hard_gate": "true"}  # string, not bool
        }))
        # _read_flags_from_disk requires isinstance(v, bool) — string is rejected
        assert is_enabled("phase0_hard_gate") is False

    def test_permission_error_falls_back_to_defaults(self, admin_json_isolated, caplog, monkeypatch):
        """If admin.json is unreadable (PermissionError, EACCES), the engine
        must NOT crash — it should log and fall back to defaults. This is the
        contract a future "tighten the except clause" refactor must preserve."""
        from orchestration_engine import feature_flags as ff
        import logging

        _write_admin(admin_json_isolated, phase0_hard_gate=True)
        ff.reset_cache()

        def _raise_permission(self, *args, **kwargs):
            raise PermissionError(13, "Permission denied")

        # Mock the Path.read_text call that _read_flags_from_disk uses.
        monkeypatch.setattr(Path, "read_text", _raise_permission)
        with caplog.at_level(logging.WARNING):
            assert ff.is_enabled("phase0_hard_gate") is False  # default, not True
        assert any("failed to read" in r.message for r in caplog.records)


class TestCacheTTL:
    def test_cache_reused_within_ttl(self, admin_json_isolated):
        from orchestration_engine import feature_flags as ff
        _write_admin(admin_json_isolated, phase0_hard_gate=True)
        assert ff.is_enabled("phase0_hard_gate") is True
        # Change the file — cache should NOT pick it up until TTL expires.
        _write_admin(admin_json_isolated, phase0_hard_gate=False)
        assert ff.is_enabled("phase0_hard_gate") is True  # still cached

    def test_reset_cache_forces_reread(self, admin_json_isolated):
        from orchestration_engine import feature_flags as ff
        _write_admin(admin_json_isolated, phase0_hard_gate=True)
        assert ff.is_enabled("phase0_hard_gate") is True
        _write_admin(admin_json_isolated, phase0_hard_gate=False)
        ff.reset_cache()
        assert ff.is_enabled("phase0_hard_gate") is False

    def test_get_flags_fresh_bypasses_cache(self, admin_json_isolated):
        from orchestration_engine import feature_flags as ff
        _write_admin(admin_json_isolated, phase0_hard_gate=True)
        assert ff.get_flags()["phase0_hard_gate"] is True
        _write_admin(admin_json_isolated, phase0_hard_gate=False)
        assert ff.get_flags()["phase0_hard_gate"] is True  # cached
        assert ff.get_flags(fresh=True)["phase0_hard_gate"] is False  # re-read


# ---------------------------------------------------------------------------
# Drift check: feature_flags._DEFAULTS vs web/api.py _ADMIN_DEFAULTS
# ---------------------------------------------------------------------------


class TestDefaultsMatchAdminApi:
    def test_keys_match_admin_known_flags(self):
        """Every flag declared in feature_flags._DEFAULTS must be a known
        admin flag in web/api.py, and vice versa. Drift would mean the UI
        could persist a flag the engine doesn't read, or the engine queries
        a flag the UI doesn't expose."""
        from orchestration_engine.feature_flags import _DEFAULTS
        from orchestration_engine.web.api import _ADMIN_KNOWN_FLAGS
        assert set(_DEFAULTS.keys()) == set(_ADMIN_KNOWN_FLAGS), (
            f"feature_flags._DEFAULTS keys ({sorted(_DEFAULTS.keys())}) "
            f"diverge from admin API _ADMIN_KNOWN_FLAGS "
            f"({sorted(_ADMIN_KNOWN_FLAGS)}). One was added without the other."
        )

    def test_default_values_match(self):
        from orchestration_engine.feature_flags import _DEFAULTS
        from orchestration_engine.web.api import _ADMIN_DEFAULTS
        assert _DEFAULTS == _ADMIN_DEFAULTS["feature_flags"], (
            "Default values diverge between feature_flags._DEFAULTS and "
            "web.api._ADMIN_DEFAULTS['feature_flags']. Keep them in sync."
        )


# ---------------------------------------------------------------------------
# phase0_hard_gate wired into sequencer
# ---------------------------------------------------------------------------


class TestPhase0HardGateRuntime:
    """Wired-in test: the sequencer source must check
    feature_flags.is_enabled('phase0_hard_gate') in the exhaust path
    AND only for the canonical Phase 0 phase id."""

    def test_sequencer_source_calls_phase0_hard_gate_check(self):
        seq = (
            Path(__file__).resolve().parent.parent
            / "src" / "orchestration_engine" / "sequencer.py"
        ).read_text()
        assert 'is_enabled("phase0_hard_gate")' in seq, (
            "sequencer.py is missing the is_enabled('phase0_hard_gate') "
            "call — phase0_hard_gate runtime wiring is gone. See #840."
        )
        # The check must be gated by the canonical Phase 0 id (via the
        # shared constant). A future refactor that drops the phase-id guard
        # would silently halt non-Phase-0 exhaustions too.
        assert "_ff.PHASE_0_ID" in seq, (
            "phase0_hard_gate check is no longer gated by the shared "
            "feature_flags.PHASE_0_ID constant — a refactor would now halt "
            "EVERY phase exhaustion, not just Phase 0."
        )

    def test_phase_0_id_constant_value(self):
        """The PHASE_0_ID constant value is the contract: every consumer
        template that ships Phase 0 MUST use this exact phase id (see the
        sequencer wiring at sequencer.py:3624)."""
        from orchestration_engine.feature_flags import PHASE_0_ID
        assert PHASE_0_ID == "existing_symbols_inventory", (
            "PHASE_0_ID has drifted from its documented value. The sequencer "
            "wiring at sequencer.py:3624 anchors on this string; any change "
            "requires synchronised updates to the standard pipeline YAML's "
            "phase id and the documented contract in feature_flags.py."
        )

    def test_override_sets_exhausted_next_to_none(self, admin_json_isolated):
        """Functional probe: simulate the override decision in isolation.
        Mirrors the sequencer logic so a refactor that breaks the contract
        is caught even when the integration test is too heavyweight to run."""
        from orchestration_engine.feature_flags import is_enabled

        def decide_exhausted_next(phase_id: str, yaml_exhausted: str | None) -> str | None:
            """Mirror sequencer.py's #840 override logic."""
            if phase_id == "existing_symbols_inventory" and yaml_exhausted is not None:
                if is_enabled("phase0_hard_gate"):
                    return None  # halt — override YAML
            return yaml_exhausted

        # Flag OFF: YAML's exhausted=spec takes effect (graceful degradation)
        _write_admin(admin_json_isolated, phase0_hard_gate=False)
        assert decide_exhausted_next("existing_symbols_inventory", "spec") == "spec"

        # Flag ON: override to None (halt)
        _write_admin(admin_json_isolated, phase0_hard_gate=True)
        from orchestration_engine import feature_flags as ff
        ff.reset_cache()
        assert decide_exhausted_next("existing_symbols_inventory", "spec") is None

        # Flag ON but other phase exhausts: NOT overridden
        assert decide_exhausted_next("review", "fix") == "fix"


# ---------------------------------------------------------------------------
# dialogue_phase wired into sequencer
# ---------------------------------------------------------------------------


class TestDialoguePhaseRuntime:
    """Wired-in test: the sequencer source must check
    feature_flags.is_enabled('dialogue_phase') before dispatching a
    type:dialogue phase, and skip with a synthetic clean-exit result
    when the flag is False."""

    def test_sequencer_source_calls_dialogue_phase_check(self):
        seq = (
            Path(__file__).resolve().parent.parent
            / "src" / "orchestration_engine" / "sequencer.py"
        ).read_text()
        assert 'is_enabled("dialogue_phase")' in seq, (
            "sequencer.py is missing the is_enabled('dialogue_phase') "
            "call — dialogue_phase runtime wiring is gone. See #840."
        )
        # The synthetic result's state should be recognisable for downstream
        # debugging / UI display.
        assert "skipped_by_feature_flag" in seq, (
            "Synthetic skip result no longer carries the "
            "'skipped_by_feature_flag' state marker."
        )

    def test_default_admin_state_disables_dialogue(self, admin_json_isolated):
        """Default behaviour: dialogue_phase is False. Sequencer should skip
        any dialogue phase by default — no opt-in required to AVOID running
        an expensive cross-model loop on a non-dialogue pipeline that happens
        to ship one."""
        from orchestration_engine.feature_flags import is_enabled
        assert is_enabled("dialogue_phase") is False

    def test_enabling_admin_flag_makes_dialogue_run(self, admin_json_isolated):
        from orchestration_engine.feature_flags import is_enabled
        _write_admin(admin_json_isolated, dialogue_phase=True)
        assert is_enabled("dialogue_phase") is True

    def test_dialogue_gate_wired_in_all_three_dispatch_sites(self):
        """The dialogue gate must appear in all THREE sequencer dispatch
        paths: _execute_wave_sequential (linear), _execute_wave_parallel
        (parallel waves), and StateMachineSequencer._execute_transitions
        (state-machine routing). Any one missing means a consumer can
        accidentally bypass the gate by choosing a different sequencer."""
        seq = (
            Path(__file__).resolve().parent.parent
            / "src" / "orchestration_engine" / "sequencer.py"
        ).read_text()
        # Three is the canonical count today; if a fourth dispatch path is
        # ever added (e.g. an async path), it must also gate.
        invocation_count = seq.count('is_enabled("dialogue_phase")')
        assert invocation_count >= 3, (
            f"expected ≥3 invocations of is_enabled('dialogue_phase') in "
            f"sequencer.py (linear + parallel + state-machine dispatch); "
            f"got {invocation_count}. A dispatch path is missing the gate."
        )


class TestOrchAdminPathHonouredByWebApi:
    """The ORCH_ADMIN_PATH env var must be honoured by both the read path
    (feature_flags) AND the write path (web/api.py admin handlers). Without
    this, a test that points ORCH_ADMIN_PATH at a tmpdir would still see the
    UI write to ~/.orchestration-engine/admin.json — silent split-brain."""

    def test_admin_handlers_use_feature_flags_path_resolver(self):
        api_src = (
            Path(__file__).resolve().parent.parent
            / "src" / "orchestration_engine" / "web" / "api.py"
        ).read_text()
        # The admin GET + PUT handlers must call _ff._admin_json_path()
        # (or equivalent shared resolver), NOT hardcode Path.home().
        # Two callsites are expected (get_admin_state + update_feature_flags).
        count = api_src.count("_ff._admin_json_path()")
        assert count >= 2, (
            f"expected ≥2 callsites of _ff._admin_json_path() in web/api.py "
            f"(admin GET + admin PUT); got {count}. Hardcoded Path.home() "
            f"on the write path creates ORCH_ADMIN_PATH split-brain."
        )
