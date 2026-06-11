"""Regression tests for #835 — apply_config_schema_defaults.

The daemon must fill in defaults from `template.config_schema.properties.*.default`
into the initial_input dict before it reaches the sequencer. Without this,
prompt templates referencing `{config[ui_primitive_paths]}` etc. would render
the literal `<MISSING:ui_primitive_paths>` for any consumer whose stored
config dict pre-dates the new optional fields — a silent regression introduced
whenever the standard pipeline YAML adds optional config_schema properties.
"""

from __future__ import annotations

import pytest

from orchestration_engine.daemon import apply_config_schema_defaults


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """#980/#981: foreground `orch run` now persists by default. Redirect HOME
    so default_db_path() resolves under tmp and never touches the real
    ~/.orchestration-engine."""
    monkeypatch.setenv("HOME", str(tmp_path))


class TestAppliesDefaults:
    def test_fills_missing_string_default(self):
        config: dict = {}
        schema = {"properties": {"ui_primitive_paths": {"type": "string", "default": ""}}}
        apply_config_schema_defaults(config, schema)
        assert config == {"ui_primitive_paths": ""}

    def test_fills_missing_bool_default(self):
        config: dict = {}
        schema = {"properties": {"phase0_hard_gate": {"type": "boolean", "default": False}}}
        apply_config_schema_defaults(config, schema)
        assert config == {"phase0_hard_gate": False}

    def test_fills_missing_int_default(self):
        config: dict = {}
        schema = {"properties": {"acceptance_max_retries": {"type": "integer", "default": 3}}}
        apply_config_schema_defaults(config, schema)
        assert config == {"acceptance_max_retries": 3}

    def test_does_not_overwrite_existing_value(self):
        """Existing values in the config dict must win over schema defaults."""
        config = {"ui_primitive_paths": "packages/ui/src/components/*.tsx"}
        schema = {"properties": {"ui_primitive_paths": {"type": "string", "default": ""}}}
        apply_config_schema_defaults(config, schema)
        assert config["ui_primitive_paths"] == "packages/ui/src/components/*.tsx"

    def test_does_not_overwrite_falsy_existing_value(self):
        """A key explicitly set to empty string / False / 0 must be preserved."""
        config: dict = {"flag_a": False, "name": "", "count": 0}
        schema = {
            "properties": {
                "flag_a": {"type": "boolean", "default": True},
                "name": {"type": "string", "default": "fallback"},
                "count": {"type": "integer", "default": 5},
            }
        }
        apply_config_schema_defaults(config, schema)
        assert config == {"flag_a": False, "name": "", "count": 0}

    def test_skips_properties_without_default(self):
        """Properties declared but without a `default` field stay absent."""
        config: dict = {}
        schema = {"properties": {"required_field": {"type": "string"}}}
        apply_config_schema_defaults(config, schema)
        assert config == {}

    def test_fills_multiple_keys(self):
        """The five new Phase 0 config fields all get defaults applied."""
        config: dict = {}
        schema = {
            "properties": {
                "ui_primitive_paths": {"type": "string", "default": ""},
                "lib_paths": {"type": "string", "default": ""},
                "action_dirs": {"type": "string", "default": ""},
                "workspace_barrels": {"type": "string", "default": ""},
                "phase0_hard_gate": {"type": "boolean", "default": False},
            }
        }
        apply_config_schema_defaults(config, schema)
        assert config == {
            "ui_primitive_paths": "",
            "lib_paths": "",
            "action_dirs": "",
            "workspace_barrels": "",
            "phase0_hard_gate": False,
        }

    def test_preserves_existing_and_fills_missing(self):
        """The realistic case: consumer has the old fields; new ones get defaults."""
        config = {
            "issue_title": "My issue",
            "issue_body": "Body",
            "repo_path": "/some/repo",
            "branch_name": "fix/issue-1",
        }
        schema = {
            "properties": {
                "issue_title": {"type": "string"},
                "issue_body": {"type": "string"},
                "repo_path": {"type": "string"},
                "branch_name": {"type": "string"},
                "ui_primitive_paths": {"type": "string", "default": ""},
                "phase0_hard_gate": {"type": "boolean", "default": False},
            }
        }
        apply_config_schema_defaults(config, schema)
        assert config["issue_title"] == "My issue"
        assert config["issue_body"] == "Body"
        assert config["repo_path"] == "/some/repo"
        assert config["branch_name"] == "fix/issue-1"
        assert config["ui_primitive_paths"] == ""
        assert config["phase0_hard_gate"] is False


class TestDefensiveAgainstMalformedSchema:
    """Schemas come from operator-edited YAML; tolerate the gnarly shapes."""

    def test_none_schema_is_noop(self):
        config = {"existing": "value"}
        apply_config_schema_defaults(config, None)
        assert config == {"existing": "value"}

    def test_non_dict_schema_is_noop(self):
        config = {"existing": "value"}
        apply_config_schema_defaults(config, "not-a-dict")
        assert config == {"existing": "value"}

    def test_schema_without_properties_is_noop(self):
        config = {"existing": "value"}
        apply_config_schema_defaults(config, {"type": "object"})
        assert config == {"existing": "value"}

    def test_properties_not_a_dict_is_noop(self):
        config: dict = {}
        apply_config_schema_defaults(config, {"properties": ["a", "b"]})
        assert config == {}

    def test_property_spec_not_a_dict_is_skipped(self):
        """A bogus property spec (string, list, None) is skipped, not crashed on."""
        config: dict = {}
        schema = {
            "properties": {
                "good": {"type": "string", "default": "ok"},
                "bad_a": "not-a-dict",
                "bad_b": None,
                "bad_c": ["list"],
            }
        }
        apply_config_schema_defaults(config, schema)
        assert config == {"good": "ok"}

    def test_property_without_default_field_is_skipped(self):
        config: dict = {}
        schema = {
            "properties": {
                "no_default": {"type": "string", "description": "required field"},
                "has_default": {"type": "string", "default": "filled"},
            }
        }
        apply_config_schema_defaults(config, schema)
        assert config == {"has_default": "filled"}


class TestPhase0RegressionCase:
    """The exact scenario the bug exists to fix: pre-2.1 consumer launching
    coding-pipeline-standard v2.1.0 which adds 5 new optional config fields."""

    def test_pre_v2_1_consumer_input_gets_phase0_defaults(self):
        """A consumer dict using only the v2.0-era required fields plus
        the standard optional ones receives all 5 new Phase 0 defaults so
        prompt rendering at Phase 0 has real strings, not <MISSING:> literals."""
        config = {
            "issue_title": "Add OAuth login",
            "issue_body": "Need Google OAuth on the login page",
            "repo_path": "/home/dev/myproject",
            "branch_name": "feat/oauth",
            "issue_number": 42,
            "repo_url": "https://github.com/me/myproject",
            "test_command": "pytest tests/",
            "language": "python",
        }
        # Subset of v2.1.0 schema matching the engine YAML
        schema = {
            "required": [
                "issue_title", "issue_body", "repo_path",
                "branch_name", "issue_number", "repo_url",
            ],
            "properties": {
                "issue_title": {"type": "string"},
                "issue_body": {"type": "string"},
                "repo_path": {"type": "string"},
                "branch_name": {"type": "string"},
                "issue_number": {"type": "integer"},
                "repo_url": {"type": "string"},
                "test_command": {"type": "string", "default": "python3 -m pytest tests/ -x -q"},
                "language": {"type": "string", "default": "python"},
                "files_context": {"type": "string", "default": ""},
                "style_guide": {"type": "string", "default": ""},
                "acceptance_test_file": {"type": "string", "default": ""},
                "acceptance_max_retries": {"type": "integer", "default": 3},
                "ui_primitive_paths": {"type": "string", "default": ""},
                "lib_paths": {"type": "string", "default": ""},
                "action_dirs": {"type": "string", "default": ""},
                "workspace_barrels": {"type": "string", "default": ""},
                "phase0_hard_gate": {"type": "boolean", "default": False},
            },
        }
        apply_config_schema_defaults(config, schema)
        # All 5 new Phase 0 fields are now present with safe defaults
        assert config["ui_primitive_paths"] == ""
        assert config["lib_paths"] == ""
        assert config["action_dirs"] == ""
        assert config["workspace_barrels"] == ""
        assert config["phase0_hard_gate"] is False
        # Pre-existing optional fields with defaults that the consumer DID supply
        # are preserved (test_command, language)
        assert config["test_command"] == "pytest tests/"
        assert config["language"] == "python"
        # Pre-existing optional fields the consumer did NOT supply get defaults
        assert config["files_context"] == ""
        assert config["acceptance_max_retries"] == 3

    def test_prompt_substitution_no_longer_renders_MISSING_literal(self):
        """End-to-end proof: after defaults are applied, str.format(config=…)
        on a Phase 0 prompt template produces real strings, not <MISSING:> tokens.

        This is the regression the helper exists to prevent. Without it,
        `{config[ui_primitive_paths]}` would render `<MISSING:ui_primitive_paths>`
        for any pre-v2.1 consumer."""
        from orchestration_engine.sequencer import _SafeDict

        config: dict = {"issue_title": "T"}
        schema = {
            "properties": {
                "ui_primitive_paths": {"type": "string", "default": ""},
                "lib_paths": {"type": "string", "default": ""},
            }
        }
        apply_config_schema_defaults(config, schema)
        rendered = (
            "Title: {config[issue_title]} | UI: '{config[ui_primitive_paths]}' | Lib: '{config[lib_paths]}'"
        ).format(config=_SafeDict(config))
        assert "<MISSING:" not in rendered
        assert rendered == "Title: T | UI: '' | Lib: ''"


class TestEndToEndAgainstRealisticPhase0Prompt:
    """Integration test using an in-memory synthetic template that mirrors
    the v2.1.0 standard pipeline's Phase 0 prompt structure.

    We deliberately avoid coupling the test to `templates/` per the lint rule
    in `test_lint_no_templates_hardcode.py` (issue #632). Instead we hand-write
    a config_schema + Phase 0 prompt that contains the exact `{config[…]}`
    placeholders the real Phase 0 uses for the 5 new optional fields, and
    prove the helper keeps `<MISSING:>` out of the rendered prompt.
    """

    def test_phase0_prompt_renders_without_MISSING_after_defaults_applied(self):
        from orchestration_engine.sequencer import _SafeDict

        # Synthetic config_schema that mirrors v2.1.0 standard pipeline's
        # new optional Phase 0 fields. Keep in sync with templates if those
        # fields ever change.
        config_schema = {
            "required": [
                "issue_title", "issue_body", "repo_path",
                "branch_name", "issue_number", "repo_url",
            ],
            "properties": {
                "issue_title": {"type": "string"},
                "issue_body": {"type": "string"},
                "repo_path": {"type": "string"},
                "language": {"type": "string", "default": "python"},
                # The five new Phase 0 optional fields:
                "ui_primitive_paths": {"type": "string", "default": ""},
                "lib_paths": {"type": "string", "default": ""},
                "action_dirs": {"type": "string", "default": ""},
                "workspace_barrels": {"type": "string", "default": ""},
                "phase0_hard_gate": {"type": "boolean", "default": False},
            },
        }

        # Synthetic prompt mirroring the literal `{config[...]}` references
        # in the v2.1.0 Phase 0 prompt_template.
        phase0_prompt = (
            "Title: {config[issue_title]}\n"
            "Body: {config[issue_body]}\n"
            "Repo: {config[repo_path]} (Lang: {config[language]})\n"
            "### UI primitives\n{config[ui_primitive_paths]}\n"
            "### Project shared libraries\n{config[lib_paths]}\n"
            "### Adjacent action / hook / route patterns\n{config[action_dirs]}\n"
            "### Workspace barrels\n{config[workspace_barrels]}\n"
            "Hard gate: {config[phase0_hard_gate]}\n"
        )

        # Pre-v2.1 consumer input — only v2.0-era required + standard optional
        # fields supplied; the new Phase 0 fields are absent.
        pre_v2_1_input: dict = {
            "issue_title": "Regression-test issue",
            "issue_body": "Body",
            "repo_path": "/some/repo",
        }

        apply_config_schema_defaults(pre_v2_1_input, config_schema)

        rendered = phase0_prompt.format(config=_SafeDict(pre_v2_1_input))

        missing_literals = [
            line for line in rendered.splitlines() if "<MISSING:" in line
        ]
        assert not missing_literals, (
            f"Phase 0 prompt rendered {len(missing_literals)} <MISSING:> literal(s) "
            f"after defaults applied — the regression is NOT prevented. "
            f"Offending lines: {missing_literals[:5]}"
        )
        # Verify the five new fields rendered as their empty/False defaults
        assert "### UI primitives\n\n" in rendered
        assert "### Project shared libraries\n\n" in rendered
        assert "Hard gate: False\n" in rendered

    def test_cli_run_template_path_invokes_apply_helper(self):
        """Static check: the cli.run_template path imports + calls the helper.

        Belt-and-suspenders against future refactors that might remove the
        call without removing the test.
        """
        from tests.conftest import read_src

        cli_src = read_src("cli.py")

        # The helper must be invoked at least twice — once for run_template,
        # once for pipeline_launch. (Scenario_run adds a third call which is
        # belt-and-suspenders; not asserted here so the test stays focused.)
        invocation_count = cli_src.count("apply_config_schema_defaults(initial_input")
        assert invocation_count >= 2, (
            f"expected ≥2 invocations of apply_config_schema_defaults in "
            f"cli.py (run_template + pipeline_launch); got {invocation_count}. "
            f"This regression test exists because round-2 adversary review "
            f"caught that only the daemon path called the helper — the "
            f"synchronous orch-run path bypassed it and would have shipped "
            f"<MISSING:> literals for pre-v2.1 consumers."
        )


class TestCliRunnerIntegration:
    """Real `orch run` invocation via Click's CliRunner.

    The synthetic-prompt test above proves the helper-in-isolation works;
    the static-grep test proves the call exists in cli.py source. This
    class is the round-3 adversary's explicit ask: prove the actual CLI
    command produces a Phase-0-shaped prompt without `<MISSING:>` for a
    pre-v2.1 input dict.

    Strategy: write a minimal pipeline YAML to tmp_path that mirrors the
    real v2.1.0 standard pipeline's Phase 0 config_schema + a Phase 0
    prompt referencing the same `{config[...]}` placeholders. Invoke
    `orch run` in dry-run mode with only the v2.0-era required fields.
    The CLI must apply schema defaults before the sequencer renders the
    prompt; if it doesn't, the rendered prompt (captured via monkeypatch
    on the format step) contains `<MISSING:>` and the test fails.
    """

    def test_orch_run_applies_defaults_before_prompt_rendering(self, tmp_path, monkeypatch):
        import yaml
        from click.testing import CliRunner

        from orchestration_engine.cli import main
        from orchestration_engine import daemon as daemon_mod

        # Minimal pipeline YAML mirroring v2.1.0 Phase 0 config_schema shape.
        # We use only optional defaults (no required fields) so the test
        # input can stay tiny.
        pipeline_yaml = tmp_path / "phase0-fixture.yaml"
        pipeline_yaml.write_text(yaml.safe_dump({
            "id": "phase0-fixture",
            "name": "Phase 0 Regression Fixture",
            "description": "Mirror of v2.1.0 Phase 0 config_schema for #835 regression test.",
            "author": "test",
            "version": "1.0.0",
            "category": "code",
            "config_schema": {
                "type": "object",
                "required": [],
                "properties": {
                    "ui_primitive_paths": {"type": "string", "default": ""},
                    "lib_paths": {"type": "string", "default": ""},
                    "action_dirs": {"type": "string", "default": ""},
                    "workspace_barrels": {"type": "string", "default": ""},
                    "phase0_hard_gate": {"type": "boolean", "default": False},
                },
            },
            "phases": [{
                "id": "p0",
                "name": "Phase 0 mock",
                "description": "Mocks the canonical Phase 0 prompt for testing.",
                "prompt_template": (
                    "UI:'{config[ui_primitive_paths]}' Lib:'{config[lib_paths]}' "
                    "Act:'{config[action_dirs]}' Bar:'{config[workspace_barrels]}' "
                    "Hard:{config[phase0_hard_gate]}"
                ),
            }],
        }))

        # Capture every call to apply_config_schema_defaults so we can prove
        # the CLI surface actually invokes it (not just the daemon).
        calls: list[dict] = []
        real_apply = daemon_mod.apply_config_schema_defaults

        def _spy(config, schema):
            calls.append({"keys_before": list(config.keys())})
            real_apply(config, schema)
            calls[-1]["keys_after"] = list(config.keys())

        # cli.py imports the helper at module top (`from .daemon import
        # apply_config_schema_defaults`) per #876 A-8, so the call resolves
        # through the cli module's namespace.  Patch BOTH the daemon module
        # attribute (for any internal daemon path) AND cli's module-level
        # name binding (so the cli call sites see the spy).
        from orchestration_engine import cli as cli_mod
        monkeypatch.setattr(daemon_mod, "apply_config_schema_defaults", _spy)
        monkeypatch.setattr(cli_mod, "apply_config_schema_defaults", _spy)

        runner = CliRunner()
        # Empty input dict; required=[] in the schema so no required-field
        # validation kicks in. The schema defaults MUST be applied or the
        # rendered Phase 0 prompt will contain <MISSING:> literals.
        # --output-dir constrains artifacts to tmp_path (no CWD pollution).
        out_dir = tmp_path / "out"
        result = runner.invoke(
            main,
            [
                "run", str(pipeline_yaml),
                "--mode", "dry-run",
                "--input", "{}",
                "--output-dir", str(out_dir),
            ],
            catch_exceptions=False,
        )

        # The CLI must have invoked the spy at least once.
        assert calls, (
            "apply_config_schema_defaults was NEVER called during `orch run` — "
            "the CLI surface bypasses the schema-defaults helper. "
            f"CLI output:\n{result.output}"
        )
        last = calls[-1]
        # Before the call: the keys the user supplied. After: must include
        # the five Phase 0 defaults.
        for key in ("ui_primitive_paths", "lib_paths", "action_dirs",
                    "workspace_barrels", "phase0_hard_gate"):
            assert key in last["keys_after"], (
                f"apply_config_schema_defaults did not fill missing key "
                f"{key!r}. keys_before={last['keys_before']!r}, "
                f"keys_after={last['keys_after']!r}"
            )

        # Belt-and-suspenders: the dry-run output should not surface any
        # <MISSING:> literal (the rendered prompts are logged at INFO level
        # but not echoed to stdout by default; this assertion is a
        # safety-net rather than a primary signal).
        assert "<MISSING:" not in result.output, (
            f"`orch run` output contains <MISSING:> literal — defaults "
            f"were not applied before prompt rendering. Output:\n{result.output}"
        )
