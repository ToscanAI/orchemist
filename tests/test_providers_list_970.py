"""Unit coverage for `orch providers list` + the providers_info registry (#970).

The SEALED acceptance suite (`.runs/run-970/acceptance_tests.py`) is the
red/green gate; this file is the house-style unit layer following the precedent
of recent runs (e.g. `tests/test_per_phase_provider_969.py`): focused tests for
the registry contents, the presence logic, the JSON schema, leak-safety, the
side-effect/network negatives, and the drift guards.

Conventions copied from `tests/test_cli_batch2.py:32-34` (`_invoke`) and
`tests/test_per_phase_provider_969.py` (the `urllib.request.urlopen` transport
seal). The `[NEW]` `providers_info` symbols are imported at module scope here
because, post-implementation, the module exists; the sealed suite imports them
lazily because it must collect against HEAD where the module is absent.
"""

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from orchestration_engine.cli import main
from orchestration_engine.executors.openrouter_executor import (
    DEFAULT_MODEL_MAP,
    OpenRouterExecutor,
)
from orchestration_engine.model_registry import bare_id
from orchestration_engine.providers_info import PROVIDERS_INFO, ProviderInfo
from orchestration_engine.templates import TemplateEngine

SIX_PROVIDERS = {"anthropic", "openrouter", "claudecode", "gemini", "openclaw", "dryrun"}
CREDENTIALED = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openclaw": "OPENCLAW_GATEWAY_TOKEN",
}
KEYLESS = {"claudecode", "gemini", "dryrun"}
JSON_KEYS = {
    "name",
    "mode",
    "per_phase",
    "credential_env",
    "configured",
    "default_models",
    "maturity",
    "notes",
}

_NET_SEAL = patch(
    "urllib.request.urlopen",
    side_effect=AssertionError("providers list must make zero network calls"),
)


def _invoke(args, env=None):
    """CliRunner helper (byte-faithful to tests/test_cli_batch2.py:32-34)."""
    with _NET_SEAL:
        return CliRunner().invoke(main, args, env=env or {}, catch_exceptions=False)


def _json(args, env=None):
    """Run `providers list --json` (sealed), parse, and index by provider name."""
    result = _invoke(args, env=env)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    return data, {obj["name"]: obj for obj in data}


# ---------------------------------------------------------------------------
# The registry module itself (no CLI)
# ---------------------------------------------------------------------------
class TestProvidersInfoRegistry:
    """Direct assertions on the PROVIDERS_INFO frozen registry."""

    def test_exactly_six_records_unique_names(self):
        names = [p.name for p in PROVIDERS_INFO]
        assert set(names) == SIX_PROVIDERS
        assert len(names) == 6  # no duplicates

    def test_records_are_frozen(self):
        """ProviderInfo is a frozen dataclass — fields cannot be mutated."""
        with pytest.raises(Exception):  # FrozenInstanceError (a dataclasses.* subclass)
            PROVIDERS_INFO[0].name = "mutated"  # type: ignore[misc]
        assert isinstance(PROVIDERS_INFO[0], ProviderInfo)

    def test_credential_env_names(self):
        by_name = {p.name: p for p in PROVIDERS_INFO}
        for prov, var in CREDENTIALED.items():
            assert by_name[prov].credential_env == var
        for prov in KEYLESS:
            assert by_name[prov].credential_env is None

    def test_registry_stores_no_env_values(self):
        """The registry must store env-var NAMES, never presence/values (import-pure)."""
        for p in PROVIDERS_INFO:
            assert not hasattr(p, "configured")
            # credential_env is either a known var NAME or None — never a value.
            assert p.credential_env in (None, *CREDENTIALED.values())

    def test_import_is_env_independent(self, monkeypatch):
        """Re-importing providers_info reads NO environment (env is read in the CLI body).

        Functional proof: if any credential var changes value, a freshly reloaded
        registry is byte-identical — the records never capture the environment.
        """
        import dataclasses  # noqa: PLC0415
        import importlib  # noqa: PLC0415

        import orchestration_engine.providers_info as mod  # noqa: PLC0415

        # Compare by field-tuples (reload creates a new dataclass *type*, so
        # instance equality would differ even when the data is identical).
        before = [dataclasses.astuple(p) for p in mod.PROVIDERS_INFO]
        for var in CREDENTIALED.values():
            monkeypatch.setenv(var, "sentinel-value-should-not-be-captured")
        reloaded = importlib.reload(mod)
        after = [dataclasses.astuple(p) for p in reloaded.PROVIDERS_INFO]
        assert after == before
        for p in reloaded.PROVIDERS_INFO:
            assert "sentinel-value" not in (p.credential_env or "")
            assert "sentinel-value" not in p.notes


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------
class TestProvidersListTable:
    """Tests for the default human-readable table output."""

    def test_lists_six_and_names_env_vars(self):
        result = _invoke(["providers", "list"])
        assert result.exit_code == 0, result.output
        for name in SIX_PROVIDERS:
            assert name in result.output
        for var in CREDENTIALED.values():
            assert var in result.output

    def test_status_set_when_present(self):
        result = _invoke(["providers", "list"], env={"ANTHROPIC_API_KEY": "dummy"})
        assert result.exit_code == 0
        assert "set" in result.output

    def test_status_missing_when_empty(self):
        result = _invoke(["providers", "list"], env={"ANTHROPIC_API_KEY": ""})
        assert result.exit_code == 0
        assert "missing" in result.output

    def test_keyless_rows_show_na_not_a_var(self):
        """Keyless providers show an 'n/a' marker, never a fabricated env-var name."""
        result = _invoke(["providers", "list"], env={var: "" for var in CREDENTIALED.values()})
        assert result.exit_code == 0
        assert "n/a" in result.output

    def test_openrouter_base_url_surfaced(self):
        result = _invoke(["providers", "list"])
        assert result.exit_code == 0
        assert "openrouter.ai/api/v1" in result.output
        assert OpenRouterExecutor.DEFAULT_BASE_URL.split("//", 1)[1] in result.output

    def test_per_phase_column_consistent(self):
        """anthropic/openrouter render 'yes', the rest 'no' (table token = impl choice)."""
        result = _invoke(["providers", "list"])
        assert result.exit_code == 0
        assert "yes" in result.output
        assert "no" in result.output


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------
class TestProvidersListJson:
    """Tests for the --json machine-readable output."""

    def test_schema_exact_keys_and_types(self):
        data, by_name = _json(["providers", "list", "--json"])
        assert isinstance(data, list)
        assert len(data) == 6
        assert set(by_name) == SIX_PROVIDERS
        for obj in data:
            assert set(obj.keys()) == JSON_KEYS
            assert isinstance(obj["per_phase"], bool)
            assert isinstance(obj["configured"], bool)
            assert isinstance(obj["default_models"], dict)
            assert isinstance(obj["maturity"], str) and obj["maturity"]

    def test_credential_env_null_for_keyless(self):
        _, by_name = _json(["providers", "list", "--json"])
        for prov, var in CREDENTIALED.items():
            assert by_name[prov]["credential_env"] == var
        for prov in KEYLESS:
            assert by_name[prov]["credential_env"] is None

    def test_configured_tracks_env_per_row(self):
        env = {"ANTHROPIC_API_KEY": "dummy", "OPENROUTER_API_KEY": ""}
        _, by_name = _json(["providers", "list", "--json"], env=env)
        assert by_name["anthropic"]["configured"] is True
        assert by_name["openrouter"]["configured"] is False
        # keyless providers are never configured (no var)
        for prov in KEYLESS:
            assert by_name[prov]["configured"] is False

    def test_per_phase_membership(self):
        _, by_name = _json(["providers", "list", "--json"])
        true_set = {n for n, o in by_name.items() if o["per_phase"]}
        assert true_set == {"anthropic", "openrouter"}

    def test_tier_defaults_exact(self):
        _, by_name = _json(["providers", "list", "--json"])
        anth = by_name["anthropic"]["default_models"]
        assert anth == {
            "haiku": bare_id("haiku"),
            "sonnet": bare_id("sonnet"),
            "opus": bare_id("opus"),
        }
        assert by_name["openrouter"]["default_models"] == DEFAULT_MODEL_MAP
        for prov in SIX_PROVIDERS - {"anthropic", "openrouter"}:
            assert by_name[prov]["default_models"] == {}

    def test_maturity_labels(self):
        _, by_name = _json(["providers", "list", "--json"])
        expected = {
            "anthropic": "Production",
            "openrouter": "Production",
            "claudecode": "Limited",
            "gemini": "Experimental",
            "openclaw": "Deprecated",
            "dryrun": "Stable",
        }
        for prov, label in expected.items():
            assert by_name[prov]["maturity"] == label


# ---------------------------------------------------------------------------
# Security: no key material ever rendered
# ---------------------------------------------------------------------------
class TestProvidersListLeakSafety:
    """The raw env-var VALUE must never appear in any output mode."""

    def test_no_value_leak_table_or_json(self):
        env = {
            "ANTHROPIC_API_KEY": "sk-test-CANARY-AAA",
            "OPENROUTER_API_KEY": "sk-test-CANARY-OOO",
            "OPENCLAW_GATEWAY_TOKEN": "sk-test-CANARY-GGG",
        }
        for args in (["providers", "list"], ["providers", "list", "--json"]):
            result = _invoke(args, env=env)
            assert result.exit_code == 0
            assert "CANARY" not in result.output
            for value in env.values():
                assert value not in result.output


# ---------------------------------------------------------------------------
# Read-only: zero side effects, zero network
# ---------------------------------------------------------------------------
class TestProvidersListReadOnly:
    """The command constructs no DB, writes no files, and makes no network calls."""

    def test_no_orchestration_engine_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        for args in (["providers", "list"], ["providers", "list", "--json"]):
            result = _invoke(args)
            assert result.exit_code == 0, result.output
            assert not (tmp_path / ".orchestration-engine").exists()

    def test_no_db_construction(self, monkeypatch):
        import orchestration_engine.db as db_mod  # noqa: PLC0415

        def _boom(*_a, **_k):
            raise AssertionError("providers list must not construct Database")

        monkeypatch.setattr(db_mod.Database, "__init__", _boom)
        monkeypatch.setattr(db_mod, "default_db_path", _boom)
        result = _invoke(["providers", "list"])
        assert result.exit_code == 0, result.output

    def test_zero_network(self):
        """Explicit urlopen-raises seal on both render modes (default path is network-free)."""
        for args in (["providers", "list"], ["providers", "list", "--json"]):
            with patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("no network on providers list"),
            ):
                result = CliRunner().invoke(main, args, catch_exceptions=False)
                assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Drift guards (code<->code and code<->doc)
# ---------------------------------------------------------------------------
class TestProvidersInfoDriftGuards:
    """The stored literals stay bound to the live constant and the doc."""

    def test_per_phase_matches_known_providers(self):
        per_phase_names = {p.name for p in PROVIDERS_INFO if p.per_phase}
        assert per_phase_names == set(TemplateEngine.KNOWN_PROVIDERS)

    def test_maturity_matches_current_state_doc(self):
        """Each maturity string appears in the CURRENT-STATE 'Executor maturity' region."""
        from pathlib import Path  # noqa: PLC0415

        doc = Path(__file__).resolve().parent.parent / "docs" / "CURRENT-STATE.md"
        lines = doc.read_text(encoding="utf-8").splitlines()

        start = next(i for i, ln in enumerate(lines) if ln.strip() == "## Executor maturity")
        region, in_body = [], False
        for ln in lines[start + 1 :]:
            if not in_body:
                if ln.strip() == "":
                    continue
                in_body = True
            elif ln.strip() == "":
                break
            region.append(ln)
        region_text = "\n".join(region)

        assert "DryRunExecutor" in region_text
        for p in PROVIDERS_INFO:
            assert p.maturity in region_text, f"{p.maturity!r} ({p.name}) missing from doc region"
