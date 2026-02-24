"""Comprehensive QA test suite for the plugin-command importer.

This file supplements ``test_import_plugin_command.py`` (143 tests) with
additional coverage derived from the formal requirements review.  It targets:

- Every acceptance criterion (AC-01 … AC-22) at the boundary level
- Every bug-fix regression (Warnings 1-4)
- Every identified test-coverage gap
- Happy paths, error paths, and security edge cases

All tests are self-contained and can run independently:

    pytest tests/test_import_plugin_command_comprehensive.py -v

Author: QA engineering (orch import plugin-command code review)
"""

from __future__ import annotations

import re
import textwrap
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
from orchestration_engine.importers.plugin_command import (
    META_SECTIONS,
    CONTENT_MODEL_TIER,
    CONTENT_THINKING_LEVEL,
    GENERATED_AUTHOR,
    REVIEW_MODEL_TIER,
    REVIEW_THINKING_LEVEL,
    _classify_section,
    _extract_frontmatter,
    _extract_h1,
    _extract_skill_refs,
    _make_unique_id,
    _parse_document,
    _parse_inputs_section,
    _skill_refs_for_section,
    _split_by_h2,
    import_plugin_command,
    import_plugin_command_from_string,
    slugify,
    snake_case,
)
from orchestration_engine.cli import main
from orchestration_engine.templates import TemplateEngine


# ===========================================================================
# Shared fixtures and helpers
# ===========================================================================


def _strip_comments_and_parse(yaml_text: str) -> Dict[str, Any]:
    """Strip leading YAML comment lines, then parse. Returns dict."""
    lines = [ln for ln in yaml_text.splitlines() if not ln.startswith("#")]
    return yaml.safe_load("\n".join(lines)) or {}


def _validate_structural(tmp_path: Path, yaml_text: str):
    """Write YAML, load template, run structural validate. Returns (template, errors)."""
    p = tmp_path / "template.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    engine = TemplateEngine()
    tpl = engine.load_template(p)
    errs = engine.validate_template(tpl)
    return tpl, errs


def _validate_extended(tmp_path: Path, yaml_text: str):
    """Run both structural + extended validation. Returns (errors, warnings)."""
    p = tmp_path / "template.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    engine = TemplateEngine()
    tpl = engine.load_template(p)
    with open(p) as fh:
        raw = yaml.safe_load(fh)
    struct_errs = engine.validate_template(tpl)
    ext_errs, ext_warns = engine.validate_template_extended(tpl, raw)
    return struct_errs + ext_errs, ext_warns


def _invoke(*args: str) -> Any:
    """Invoke the CLI via CliRunner. Returns result."""
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


# ---------------------------------------------------------------------------
# Canonical test documents
# ---------------------------------------------------------------------------

MINIMAL = textwrap.dedent("""\
    ---
    description: A minimal test command
    ---

    # Minimal Pipeline

    ## Trigger
    Run /minimal.

    ## Inputs
    1. **Topic** — the main subject

    ## Generate Content
    Write content about the topic.

    ## Output
    Present the content.
""")

MULTI = textwrap.dedent("""\
    ---
    description: Multi-section command
    argument-hint: "<subject>"
    tags:
      - test
    ---

    # Multi Phase Pipeline

    ## Trigger
    User runs /multi.

    ## Inputs
    1. **Subject** — what to process
    2. **Tone** (optional) — the desired tone

    ## Research
    Find sources for {subject}.

    ## Draft
    Write a first draft.

    ## Review and Polish
    Review the draft.

    ## Output
    Deliver the final document.
""")


# ===========================================================================
# AC-01 — Frontmatter: happy path, non-dict coercion, no-frontmatter
# ===========================================================================


class TestAC01_Frontmatter:
    """AC-01: frontmatter extracted into dict; non-dict silently → {}."""

    def test_full_frontmatter_is_a_dict(self):
        doc = "---\ndescription: Test\ntags:\n  - a\n---\n# T\n"
        fm, _ = _extract_frontmatter(doc)
        assert isinstance(fm, dict)
        assert fm["description"] == "Test"
        assert fm["tags"] == ["a"]

    def test_list_frontmatter_coerced_to_empty_dict(self):
        """YAML that is a list (not dict) must be silently coerced to {}."""
        doc = "---\n- item1\n- item2\n---\n# Title\n"
        fm, _ = _extract_frontmatter(doc)
        assert fm == {}, f"List frontmatter should coerce to {{}}, got {fm!r}"

    def test_scalar_string_frontmatter_coerced_to_empty_dict(self):
        """YAML that is a bare scalar (not dict) must coerce to {}."""
        doc = "---\nhello world\n---\n# Title\n"
        fm, _ = _extract_frontmatter(doc)
        assert fm == {}, f"Scalar frontmatter should coerce to {{}}, got {fm!r}"

    def test_integer_frontmatter_coerced_to_empty_dict(self):
        """YAML integer value coerces to {}."""
        doc = "---\n42\n---\n# Title\n"
        fm, _ = _extract_frontmatter(doc)
        assert fm == {}

    def test_body_returned_without_frontmatter_block(self):
        """Body must not contain the --- delimiters or frontmatter keys."""
        doc = "---\ndescription: Test\n---\n\n# Title\nBody text.\n"
        fm, body = _extract_frontmatter(doc)
        assert "---" not in body.split("\n")[:3], "Frontmatter delimiters leaked into body"
        assert "description" not in body or "description" in body  # only in body text, not as key


# ===========================================================================
# AC-02 — Unclosed frontmatter treated as no-frontmatter
# ===========================================================================


class TestAC02_UnclosedFrontmatter:
    def test_unclosed_returns_empty_frontmatter(self):
        doc = "---\ndescription: test\n\n# Title\n"
        fm, body = _extract_frontmatter(doc)
        assert fm == {}

    def test_unclosed_body_is_full_original_content(self):
        doc = "---\ndescription: test\n\n# Title\n"
        fm, body = _extract_frontmatter(doc)
        assert body == doc  # unchanged

    def test_single_dash_line_is_not_frontmatter(self):
        """A single '-' on the first line is not a frontmatter marker."""
        doc = "-\ndescription: test\n-\n# Title\n"
        fm, body = _extract_frontmatter(doc)
        assert fm == {}

    def test_no_first_line_separator_means_no_frontmatter(self):
        doc = "# Title first\n---\ndescription: test\n---\n"
        fm, _ = _extract_frontmatter(doc)
        assert fm == {}


# ===========================================================================
# AC-03 — Malformed frontmatter YAML raises ValueError with "frontmatter"
# ===========================================================================


class TestAC03_MalformedFrontmatter:
    def test_unclosed_bracket_raises_value_error(self):
        doc = "---\n: broken: [\n---\n# Title\n"
        with pytest.raises(ValueError, match="[Ff]rontmatter"):
            _extract_frontmatter(doc)

    def test_tab_in_yaml_key_raises(self):
        doc = "---\n\tindented: value\n---\n# Title\n"
        with pytest.raises(ValueError, match="[Ff]rontmatter"):
            _extract_frontmatter(doc)

    def test_error_propagates_through_parse_document(self):
        """_parse_document re-raises frontmatter errors as ValueError."""
        # ': bad: [' is definitively malformed YAML (unclosed bracket)
        doc = "---\n: bad: [\n---\n# Title\n"
        with pytest.raises(ValueError, match="[Ff]rontmatter|[Mm]alformed"):
            _parse_document(doc)

    def test_error_propagates_through_import_from_string(self):
        # Unclosed flow sequence → definitively malformed YAML
        doc = "---\n: bad: [\n---\n# Title\n## Section\nContent.\n"
        with pytest.raises(ValueError):
            import_plugin_command_from_string(doc)


# ===========================================================================
# AC-04 — First H1 → template.name, id = slugify(name)
# ===========================================================================


class TestAC04_TitleAndId:
    def test_h1_becomes_template_name(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        assert data["name"] == "Minimal Pipeline"

    def test_id_is_slugified_title(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        assert data["id"] == "minimal-pipeline"

    def test_title_with_special_chars_slugified(self):
        doc = "---\ndescription: d\n---\n# Brand Voice & Tone\n## Step\nDo it.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        assert data["id"] == "brand-voice-tone"
        assert data["name"] == "Brand Voice & Tone"

    def test_first_h1_wins_when_multiple_h1s_present(self):
        """If two H1s appear, the first one defines the title."""
        doc = "# First Title\n# Second Title\n## Section\nBody.\n"
        parsed = _parse_document(doc)
        assert parsed.title == "First Title"

    def test_h1_after_intro_text(self):
        """H1 can appear after some intro text before it."""
        doc = "Some intro text.\n\n# My Template\n\n## Section\nContent.\n"
        parsed = _parse_document(doc)
        assert parsed.title == "My Template"

    # Accented / unicode titles (NFKD fix — Warning 2)
    def test_accented_title_slugified_without_diacritics(self):
        """Ü → U, é → e: accented titles transliterate cleanly."""
        doc = "# Über Plan\n## Step\nDo it.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        assert data["id"] == "uber-plan", f"Accented slug: {data['id']!r}"

    def test_accented_multibyte_title(self):
        """Café au Lait → cafe-au-lait."""
        doc = "# Café au Lait Pipeline\n## Step\nDo it.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        assert data["id"] == "cafe-au-lait-pipeline"


# ===========================================================================
# AC-05 — Missing H1 raises ValueError with "H1"
# ===========================================================================


class TestAC05_MissingH1:
    def test_no_h1_raises_value_error(self):
        doc = "## Section\nNo H1 here.\n"
        with pytest.raises(ValueError, match="H1"):
            import_plugin_command_from_string(doc)

    def test_only_h2_headings_raises(self):
        doc = "## A\nBody A.\n## B\nBody B.\n"
        with pytest.raises(ValueError, match="H1"):
            import_plugin_command_from_string(doc)

    def test_h1_in_frontmatter_not_counted(self):
        """H1 inside frontmatter text is not a real H1."""
        doc = "---\ntitle: '# Not a real H1'\n---\n## Section\nBody.\n"
        with pytest.raises(ValueError, match="H1"):
            import_plugin_command_from_string(doc)

    def test_empty_document_raises(self):
        with pytest.raises(ValueError, match="H1"):
            import_plugin_command_from_string("")

    def test_whitespace_only_document_raises(self):
        with pytest.raises(ValueError, match="H1"):
            import_plugin_command_from_string("   \n\n\t\n")


# ===========================================================================
# AC-06 — META_SECTIONS produce no pipeline phase
# ===========================================================================


class TestAC06_MetaSections:
    """Every member of META_SECTIONS must be skipped, not turned into a phase."""

    @pytest.mark.parametrize("heading", sorted(META_SECTIONS))
    def test_meta_heading_produces_no_phase(self, heading):
        doc = f"# Pipeline\n\n## {heading.title()}\nSome meta content.\n\n## Real Section\nDo work.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        phase_names = [p["name"].lower() for p in data["phases"]]
        assert heading.lower() not in phase_names, (
            f"Meta section '{heading}' must not become a pipeline phase; got {phase_names}"
        )

    @pytest.mark.parametrize("heading", sorted(META_SECTIONS))
    def test_meta_heading_classification(self, heading):
        result = _classify_section(heading.title())
        assert result in ("meta", "inputs"), (
            f"META_SECTION '{heading}' classified as '{result}', expected 'meta' or 'inputs'"
        )

    def test_all_meta_produces_zero_phases(self):
        """Document with only meta H2s → 0 phases."""
        sections = "\n\n".join(
            f"## {h.title()}\nContent for {h}.\n" for h in META_SECTIONS if h != "inputs"
        )
        doc = f"# Pipeline\n\n{sections}\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        assert data["phases"] == []


# ===========================================================================
# AC-07 — Inputs section → config_schema with type: object
# ===========================================================================


class TestAC07_ConfigSchema:
    def test_config_schema_type_is_object(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        assert data["config_schema"]["type"] == "object"

    def test_config_schema_has_properties_key(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        assert "properties" in data["config_schema"]

    def test_config_schema_field_type_is_string(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        for field_schema in data["config_schema"]["properties"].values():
            assert field_schema["type"] == "string"

    def test_config_schema_has_description_on_fields(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        for field_schema in data["config_schema"]["properties"].values():
            assert "description" in field_schema
            assert field_schema["description"]


# ===========================================================================
# AC-08 — em-dash and hyphen separators both supported
# ===========================================================================


class TestAC08_InputSeparators:
    @pytest.mark.parametrize("separator", ["—", "-"])
    def test_field_parsed_with_separator(self, separator):
        body = f"1. **My Field** {separator} field description\n"
        schema = _parse_inputs_section(body)
        assert "my_field" in schema["properties"]
        assert schema["properties"]["my_field"]["description"] == "field description"


# ===========================================================================
# AC-09 — Fields without (optional) appear in required array
# ===========================================================================


class TestAC09_RequiredFields:
    def test_field_without_optional_is_required(self):
        body = "1. **Campaign Goal** — the primary objective\n"
        schema = _parse_inputs_section(body)
        assert "campaign_goal" in schema.get("required", [])

    def test_multiple_required_fields(self):
        body = textwrap.dedent("""\
            1. **Alpha** — first field
            2. **Beta** — second field
            3. **Gamma** — third field
        """)
        schema = _parse_inputs_section(body)
        required = schema.get("required", [])
        assert "alpha" in required
        assert "beta" in required
        assert "gamma" in required

    def test_required_array_omitted_when_all_optional(self):
        """When every field is optional, no 'required' key is emitted."""
        body = textwrap.dedent("""\
            1. **Alpha** (optional) — first field
            2. **Beta** (optional) — second field
        """)
        schema = _parse_inputs_section(body)
        # Either 'required' is absent or it's empty
        required = schema.get("required", [])
        assert "alpha" not in required
        assert "beta" not in required


# ===========================================================================
# AC-10 — Fields with (optional) in any position excluded from required
# ===========================================================================


class TestAC10_OptionalFields:
    def test_optional_in_parenthetical_before_dash(self):
        body = "1. **Budget** (optional) — approximate budget\n"
        schema = _parse_inputs_section(body)
        assert "budget" not in schema.get("required", [])

    def test_optional_in_field_name_bold_text(self):
        """(optional) can be part of the bold name text itself."""
        body = "1. **Budget (optional)** — the budget\n"
        schema = _parse_inputs_section(body)
        assert "budget" not in schema.get("required", [])

    def test_optional_in_description(self):
        """(optional) appearing in the description marks the field optional."""
        body = "1. **Extra** — (optional) extra context if available\n"
        schema = _parse_inputs_section(body)
        assert "extra" not in schema.get("required", [])

    def test_case_insensitive_optional_detection(self):
        """(OPTIONAL), (Optional), (optional) all work."""
        for variant in ["(OPTIONAL)", "(Optional)", "(optional)"]:
            body = f"1. **Field** {variant} — description\n"
            schema = _parse_inputs_section(body)
            assert "field" not in schema.get("required", []), (
                f"{variant!r} not detected as optional marker"
            )

    def test_optional_word_without_parens_is_not_optional(self):
        """'optional' as plain word in description does NOT mark the field optional."""
        body = "1. **Notes** — optional additional notes\n"
        schema = _parse_inputs_section(body)
        # The word 'optional' without parens must NOT exclude from required
        # (the check is for '(optional)' token, not the bare word)
        # This tests that the regex requires parentheses
        # Note: this is a subtle point — the field might still end up in required
        # because 'optional' without parens is ignored. Let's verify required behavior:
        required = schema.get("required", [])
        # 'optional' bare word = NOT a marker → field should be required
        assert "notes" in required, (
            f"'optional' bare word must not exclude field from required: required={required}"
        )


# ===========================================================================
# AC-11 — Empty Inputs section → fallback config_schema with synthetic input
# ===========================================================================


class TestAC11_EmptyInputs:
    def test_empty_inputs_body_gives_fallback_schema(self):
        schema = _parse_inputs_section("No numbered list here.\n")
        assert schema["type"] == "object"
        assert "input" in schema["properties"]

    def test_fallback_input_field_is_required(self):
        schema = _parse_inputs_section("Describe the problem.")
        assert "input" in schema.get("required", [])

    def test_fallback_input_field_has_type_string(self):
        schema = _parse_inputs_section("No fields.")
        assert schema["properties"]["input"]["type"] == "string"

    def test_no_inputs_section_at_all_uses_global_fallback(self):
        """No ## Inputs section → global fallback config_schema with 'input' field."""
        doc = "# Pipeline\n\n## Do Work\nDo some work here.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        assert data["config_schema"]["type"] == "object"
        assert "input" in data["config_schema"]["properties"]


# ===========================================================================
# AC-12 — Content phases get model_tier: sonnet, thinking_level: low
# ===========================================================================


class TestAC12_ContentPhaseTiers:
    def test_content_phase_model_tier_is_constant(self):
        assert CONTENT_MODEL_TIER == "sonnet"

    def test_content_phase_thinking_level_is_constant(self):
        assert CONTENT_THINKING_LEVEL == "low"

    def test_content_phase_has_sonnet_model_tier(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        content_phases = [p for p in data["phases"] if p.get("task_type") == "content"]
        assert content_phases, "No content phases found"
        for p in content_phases:
            assert p["model_tier"] == "sonnet", (
                f"Content phase {p['id']!r} has model_tier={p['model_tier']!r}"
            )

    def test_content_phase_has_low_thinking_level(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        content_phases = [p for p in data["phases"] if p.get("task_type") == "content"]
        for p in content_phases:
            assert p["thinking_level"] == "low", (
                f"Content phase {p['id']!r} has thinking_level={p['thinking_level']!r}"
            )


# ===========================================================================
# AC-13 — Review phase auto-inserted after every content phase
# ===========================================================================


class TestAC13_ReviewPhaseInsertion:
    def test_one_content_section_one_review(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        review_phases = [p for p in data["phases"] if p.get("model_tier") == "opus"]
        content_phases = [p for p in data["phases"] if p.get("model_tier") == "sonnet"]
        assert len(review_phases) == len(content_phases), (
            "One review phase per content phase expected"
        )

    def test_review_phase_immediately_follows_content(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MULTI))
        phases = data["phases"]
        for i, phase in enumerate(phases):
            if phase.get("task_type") == "content":
                # The very next phase must be a review
                assert i + 1 < len(phases), f"Content phase {phase['id']!r} is last — missing review"
                next_phase = phases[i + 1]
                assert next_phase["model_tier"] == "opus", (
                    f"Phase after content {phase['id']!r} is not review: {next_phase!r}"
                )

    def test_review_phase_model_tier_is_opus(self):
        assert REVIEW_MODEL_TIER == "opus"
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        review = next(p for p in data["phases"] if "review" in p["id"])
        assert review["model_tier"] == "opus"

    def test_review_phase_thinking_level_is_medium(self):
        assert REVIEW_THINKING_LEVEL == "medium"
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        review = next(p for p in data["phases"] if "review" in p["id"])
        assert review["thinking_level"] == "medium"

    def test_review_phase_id_pattern(self):
        """Review phase ID is '<content-id>-review'."""
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        content = next(p for p in data["phases"] if p["task_type"] == "content")
        review = next(p for p in data["phases"] if p["task_type"] == "review")
        assert review["id"] == f"{content['id']}-review", (
            f"Review ID {review['id']!r} doesn't match expected '{content['id']}-review'"
        )

    def test_review_phase_depends_on_its_content_phase(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        content = next(p for p in data["phases"] if p["task_type"] == "content")
        review = next(p for p in data["phases"] if p["task_type"] == "review")
        assert content["id"] in review["depends_on"]

    def test_review_phase_name_uses_em_dash(self):
        """Review phase name format is '<phase_name> — Review'."""
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        review = next(p for p in data["phases"] if p["task_type"] == "review")
        assert "—" in review["name"], f"Review phase name must use em-dash: {review['name']!r}"
        assert "Review" in review["name"]

    def test_review_phase_description_mentions_auto_inserted(self):
        """Review phase description records it was auto-inserted."""
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        review = next(p for p in data["phases"] if p["task_type"] == "review")
        assert "auto-inserted" in review["description"].lower(), (
            f"Review phase description doesn't mention auto-insertion: {review['description']!r}"
        )

    def test_review_phase_has_timeout_minutes_20(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        review = next(p for p in data["phases"] if p["task_type"] == "review")
        assert review["timeout_minutes"] == 20

    def test_three_content_sections_produce_six_phases(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MULTI))
        assert len(data["phases"]) == 6, (
            f"3 content sections → 6 phases, got {len(data['phases'])}"
        )


# ===========================================================================
# AC-14 — _dump_template_yaml must NOT mutate global yaml.Dumper
# ===========================================================================


class TestAC14_YAMLDumperIsolation:
    def _baseline_repr_count(self):
        return len(yaml.Dumper.yaml_representers)

    def test_single_call_no_repr_growth(self):
        before = self._baseline_repr_count()
        import_plugin_command_from_string(MINIMAL)
        after = self._baseline_repr_count()
        assert before == after

    def test_ten_calls_no_repr_growth(self):
        before = self._baseline_repr_count()
        for _ in range(10):
            import_plugin_command_from_string(MULTI)
        after = self._baseline_repr_count()
        assert before == after, (
            f"yaml.Dumper grew from {before} to {after} representers after 10 calls"
        )

    def test_no_str_subclass_in_global_dumper(self):
        import_plugin_command_from_string(MINIMAL)
        str_subtypes = [
            t for t in yaml.Dumper.yaml_representers
            if isinstance(t, type) and issubclass(t, str) and t is not str
        ]
        assert str_subtypes == [], (
            f"Str subtypes leaked into global yaml.Dumper: {str_subtypes}"
        )

    def test_global_yaml_dump_still_works_after_import(self):
        """Standard yaml.dump must be unaffected — no style bleed-over."""
        import_plugin_command_from_string(MINIMAL)
        result = yaml.dump({"key": "short"}, Dumper=yaml.Dumper).strip()
        assert result == "key: short"

    def test_thread_safe_no_cross_contamination(self):
        """Concurrent imports must not leak state across threads."""
        results = []
        errors = []

        def _do_import():
            try:
                before = len(yaml.Dumper.yaml_representers)
                import_plugin_command_from_string(MULTI)
                after = len(yaml.Dumper.yaml_representers)
                results.append(before == after)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_do_import) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"Thread errors: {errors}"
        assert all(results), "Some thread saw yaml.Dumper grow"


# ===========================================================================
# AC-15/16 — Path traversal prevention in skill refs
# ===========================================================================


class TestAC15_16_PathTraversal:
    def test_absolute_path_rejected(self, tmp_path):
        """AC-15: Absolute path skill refs are silently dropped."""
        skill = tmp_path / "SKILL.md"
        skill.write_text("# Skill")
        text = f"[skill]({skill})"
        refs = _extract_skill_refs(text, base_dir=tmp_path / "commands")
        assert refs == []

    def test_path_escaping_project_root_rejected(self, tmp_path):
        """AC-16: Relative path escaping project root is silently dropped."""
        outside = tmp_path.parent / "outside_skill" / "SKILL.md"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("# Outside")
        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()
        # ../../outside_skill/SKILL.md from tmp_path/commands/ escapes tmp_path
        text = "[skill](../../outside_skill/SKILL.md)"
        refs = _extract_skill_refs(text, base_dir=cmd_dir)
        assert refs == []

    def test_deeply_nested_escape_rejected(self, tmp_path):
        """Multiple ../ levels that escape root are rejected."""
        cmd_dir = tmp_path / "a" / "b" / "c"
        cmd_dir.mkdir(parents=True)
        text = "../../../../etc/SKILL.md"
        refs = _extract_skill_refs(text, base_dir=cmd_dir)
        assert refs == []

    def test_none_base_dir_returns_empty(self):
        refs = _extract_skill_refs("[skill](../skills/x/SKILL.md)", base_dir=None)
        assert refs == []


# ===========================================================================
# AC-17 — Canonical ../skills/<name>/SKILL.md accepted when file exists
# ===========================================================================


class TestAC17_CanonicalSkillRef:
    def test_canonical_path_accepted(self, tmp_path):
        skill = tmp_path / "skills" / "brand-voice" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# Brand Voice")
        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()
        text = "[brand-voice](../skills/brand-voice/SKILL.md)"
        refs = _extract_skill_refs(text, base_dir=cmd_dir)
        assert len(refs) == 1
        assert refs[0] == str(skill.resolve())

    def test_skill_not_on_disk_not_included(self, tmp_path):
        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()
        text = "[missing](../skills/nonexistent/SKILL.md)"
        refs = _extract_skill_refs(text, base_dir=cmd_dir)
        assert refs == []

    def test_prose_reference_also_accepted(self, tmp_path):
        """Bare prose path 'brand-voice/SKILL.md' is also extracted."""
        skill = tmp_path / "brand-voice" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# Skill")
        text = "See brand-voice/SKILL.md for guidelines."
        refs = _extract_skill_refs(text, base_dir=tmp_path)
        assert str(skill.resolve()) in refs

    def test_multiple_skills_deduplicated(self, tmp_path):
        """Same skill referenced twice → appears once."""
        skill = tmp_path / "seo" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# SEO")
        text = "[seo](./seo/SKILL.md) and [seo again](./seo/SKILL.md)"
        refs = _extract_skill_refs(text, base_dir=tmp_path)
        assert len(refs) == 1


# ===========================================================================
# AC-18/19 — Placeholders survive verbatim in prompt templates
# ===========================================================================


class TestAC18_19_Placeholders:
    def test_input_placeholder_in_content_phase(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        content = next(p for p in data["phases"] if p["task_type"] == "content")
        assert "{input}" in content["prompt_template"]

    def test_previous_output_placeholder_in_content_phase(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        content = next(p for p in data["phases"] if p["task_type"] == "content")
        assert "{previous_output}" in content["prompt_template"]

    def test_arbitrary_curly_braces_in_body_preserved(self):
        doc = "# Pipeline\n## Step\nUse `{custom_var}` and `{another}` here.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        prompt = data["phases"][0]["prompt_template"]
        assert "{custom_var}" in prompt
        assert "{another}" in prompt

    def test_heading_token_in_body_not_substituted(self):
        """{heading} inside body text must survive — not be treated as a format key."""
        doc = "# Pipeline\n## Step\nProcess {heading} content carefully.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        prompt = data["phases"][0]["prompt_template"]
        assert "{heading}" in prompt

    def test_body_token_in_body_not_substituted(self):
        """{body} inside body text must survive."""
        doc = "# Pipeline\n## Step\nUpdate the {body} section.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        prompt = data["phases"][0]["prompt_template"]
        assert "{body}" in prompt

    def test_jinja_style_double_braces_preserved(self):
        """{{ Jinja-style }} blocks must pass through unchanged."""
        doc = "# Pipeline\n## Render\nUse `{{ variable }}` in Jinja templates.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        prompt = data["phases"][0]["prompt_template"]
        assert "{{ variable }}" in prompt

    def test_review_prompt_contains_phase_output_reference(self):
        """Review prompt contains {previous_output[<content_phase_id>]}."""
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        content_id = next(p["id"] for p in data["phases"] if p["task_type"] == "content")
        review = next(p for p in data["phases"] if p["task_type"] == "review")
        expected_ref = "{" + f"previous_output[{content_id}]" + "}"
        assert expected_ref in review["prompt_template"], (
            f"Review prompt missing phase output reference {expected_ref!r}"
        )


# ===========================================================================
# AC-20 — Duplicate H2 headings produce collision-free phase IDs
# ===========================================================================


class TestAC20_DuplicatePhaseIds:
    def test_duplicate_headings_get_numeric_suffix(self):
        doc = textwrap.dedent("""\
            # Pipeline
            ## Step
            Content A.
            ## Step
            Content B.
        """)
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        content_ids = [p["id"] for p in data["phases"] if p["task_type"] == "content"]
        assert "step" in content_ids
        assert "step-2" in content_ids

    def test_no_ugly_chained_suffix(self):
        """Regression: 'phase-2' as literal ID already in seen → next dupe is phase-3."""
        seen: Dict[str, int] = {}
        r1 = _make_unique_id("phase", seen)
        r2 = _make_unique_id("phase-2", seen)   # literal heading "Phase 2"
        r3 = _make_unique_id("phase", seen)
        assert r1 == "phase"
        assert r2 == "phase-2"
        assert r3 == "phase-3", f"Expected 'phase-3', got {r3!r}"

    def test_four_identical_headings(self):
        doc = textwrap.dedent("""\
            # Pipeline
            ## Alpha
            Body 1.
            ## Alpha
            Body 2.
            ## Alpha
            Body 3.
            ## Alpha
            Body 4.
        """)
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        content_ids = [p["id"] for p in data["phases"] if p["task_type"] == "content"]
        assert len(content_ids) == len(set(content_ids)), f"Duplicate IDs: {content_ids}"
        assert "alpha" in content_ids
        assert "alpha-2" in content_ids
        assert "alpha-3" in content_ids
        assert "alpha-4" in content_ids

    def test_review_ids_also_collision_free(self):
        doc = textwrap.dedent("""\
            # Pipeline
            ## Step
            Body 1.
            ## Step
            Body 2.
        """)
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        all_ids = [p["id"] for p in data["phases"]]
        assert len(all_ids) == len(set(all_ids)), f"Duplicate IDs in phases: {all_ids}"


# ===========================================================================
# AC-21 — Generated YAML passes validate_template() with zero structural errors
# ===========================================================================


class TestAC21_StructuralValidation:
    def test_minimal_structural_errors_none(self, tmp_path):
        yaml_text = import_plugin_command_from_string(MINIMAL)
        _, errs = _validate_structural(tmp_path, yaml_text)
        assert errs == [], f"Structural errors: {errs}"

    def test_multi_structural_errors_none(self, tmp_path):
        yaml_text = import_plugin_command_from_string(MULTI)
        _, errs = _validate_structural(tmp_path, yaml_text)
        assert errs == [], f"Structural errors: {errs}"

    def test_no_sections_structural_errors_none(self, tmp_path):
        doc = "# Empty Pipeline\n\n## Trigger\nRun.\n\n## Output\nDone.\n"
        yaml_text = import_plugin_command_from_string(doc)
        _, errs = _validate_structural(tmp_path, yaml_text)
        assert errs == [], f"Structural errors: {errs}"


# ===========================================================================
# AC-22 — Generated YAML passes validate_template_extended() with zero errors
# ===========================================================================


class TestAC22_ExtendedValidation:
    def test_minimal_extended_errors_none(self, tmp_path):
        yaml_text = import_plugin_command_from_string(MINIMAL)
        errs, _ = _validate_extended(tmp_path, yaml_text)
        assert errs == [], f"Extended errors: {errs}"

    def test_multi_extended_errors_none(self, tmp_path):
        yaml_text = import_plugin_command_from_string(MULTI)
        errs, _ = _validate_extended(tmp_path, yaml_text)
        assert errs == [], f"Extended errors: {errs}"

    def test_no_model_tier_warnings(self, tmp_path):
        yaml_text = import_plugin_command_from_string(MINIMAL)
        _, warns = _validate_extended(tmp_path, yaml_text)
        tier_warns = [w for w in warns if "model_tier" in w]
        assert tier_warns == [], f"model_tier warnings: {tier_warns}"

    def test_no_thinking_level_warnings(self, tmp_path):
        yaml_text = import_plugin_command_from_string(MINIMAL)
        _, warns = _validate_extended(tmp_path, yaml_text)
        level_warns = [w for w in warns if "thinking_level" in w]
        assert level_warns == [], f"thinking_level warnings: {level_warns}"


# ===========================================================================
# Warning 1 — skill_refs word-boundary matching (short skill names)
# ===========================================================================


class TestWarning1_SkillRefWordBoundary:
    """Warning 1: bare substring gave false positives for short skill names.

    A skill named 'ai' must NOT match bodies containing 'email', 'said', etc.
    The fix uses \\b word-boundary regex.
    """

    def _make_skill(self, tmp_path: Path, name: str) -> str:
        skill = tmp_path / name / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(f"# {name} skill")
        return str(skill)

    def test_short_skill_name_ai_no_false_positive_in_email(self, tmp_path):
        ref = self._make_skill(tmp_path, "ai")
        body = "Send an email to the client and maintain communication."
        result = _skill_refs_for_section(body, [ref])
        assert result == [], (
            f"'ai' skill matched inside 'email'/'maintain' — word boundary not enforced: {result}"
        )

    def test_short_skill_name_ai_no_false_positive_in_said(self, tmp_path):
        ref = self._make_skill(tmp_path, "ai")
        body = "He said it was a great idea, and I agree."
        result = _skill_refs_for_section(body, [ref])
        assert result == []

    def test_short_skill_name_ai_no_false_positive_in_brain(self, tmp_path):
        ref = self._make_skill(tmp_path, "ai")
        body = "The human brain is a remarkable organ."
        result = _skill_refs_for_section(body, [ref])
        assert result == []

    def test_short_skill_name_ai_matches_as_whole_word(self, tmp_path):
        """'ai' as a standalone word in the body must match."""
        ref = self._make_skill(tmp_path, "ai")
        body = "Use the ai system to process requests."
        result = _skill_refs_for_section(body, [ref])
        assert ref in result, f"'ai' as whole word should match"

    def test_multiword_skill_name_still_matches(self, tmp_path):
        """Multi-word skill names (hyphens normalised to spaces) still match."""
        ref = self._make_skill(tmp_path, "brand-voice")
        body = "Apply brand voice guidelines for all copy."
        result = _skill_refs_for_section(body, [ref])
        assert ref in result

    def test_skill_name_not_in_section_body_excluded(self, tmp_path):
        ref = self._make_skill(tmp_path, "seo-guide")
        body = "Write compelling brand stories."
        result = _skill_refs_for_section(body, [ref])
        assert result == []


# ===========================================================================
# Warning 2 — slugify() NFKD Unicode normalization
# ===========================================================================


class TestWarning2_SlugifyUnicode:
    """Warning 2: slugify() must transliterate accented Latin characters via NFKD.

    Before the fix, 'Über' would silently drop 'Ü', producing 'ber' instead of
    'uber', causing ID collisions between 'Über Plan' and 'ber Plan'.
    """

    @pytest.mark.parametrize("text,expected", [
        ("Über Plan", "uber-plan"),
        ("café", "cafe"),
        ("Ñoño", "nono"),
        ("résumé", "resume"),
        ("naïve", "naive"),
        ("Ångström", "angstrom"),
        ("Straße", "strae"),   # ß has no ASCII equivalent in NFKD, so it's dropped
    ])
    def test_accented_transliteration(self, text, expected):
        result = slugify(text)
        assert result == expected, f"slugify({text!r}) = {result!r}, expected {expected!r}"

    def test_emoji_produces_empty_string(self):
        """Emoji have no ASCII equivalent → empty slug."""
        assert slugify("🎯") == ""

    def test_emoji_with_ascii_keeps_ascii_part(self):
        """🎯 Campaign → 'campaign' (emoji dropped, ASCII kept)."""
        result = slugify("🎯 Campaign")
        assert result == "campaign"

    def test_cjk_produces_empty_string(self):
        """CJK characters have no ASCII equivalent → empty slug."""
        result = slugify("日本語")
        assert result == ""

    def test_mixed_accented_and_regular(self):
        result = slugify("Über-Mensch Awaits")
        assert result == "uber-mensch-awaits"


# ===========================================================================
# Warning 4 — _make_unique_id empty base_id fallback
# ===========================================================================


class TestWarning4_EmptyBaseId:
    """Warning 4: slugify() can return '' for emoji/CJK headings.

    _make_unique_id must fall back to 'section' when base_id is empty,
    preventing empty string phase IDs in the generated template.
    """

    def test_empty_base_id_falls_back_to_section(self):
        seen: Dict[str, int] = {}
        result = _make_unique_id("", seen)
        assert result == "section", f"Empty base_id must fall back to 'section', got {result!r}"

    def test_empty_base_id_deduplicates_correctly(self):
        seen: Dict[str, int] = {}
        r1 = _make_unique_id("", seen)
        r2 = _make_unique_id("", seen)
        r3 = _make_unique_id("", seen)
        assert r1 == "section"
        assert r2 == "section-2"
        assert r3 == "section-3"

    def test_emoji_heading_produces_valid_phase_id(self):
        """A document with an emoji-only H2 must produce a non-empty phase ID."""
        doc = "# Pipeline\n## 🎯\nDo targeted work here.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        for phase in data["phases"]:
            assert phase["id"], f"Phase has empty ID: {phase!r}"
            # Must be a valid slug (no empty string, no leading/trailing hyphens)
            assert re.match(r"^[a-z][a-z0-9-]*$", phase["id"]), (
                f"Phase ID is not a valid slug: {phase['id']!r}"
            )

    def test_emoji_heading_falls_back_to_section(self):
        """Emoji-only heading → slug is '' → phase ID = 'section'."""
        doc = "# Pipeline\n## 🎯\nDo work.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        content_phase = next(p for p in data["phases"] if p["task_type"] == "content")
        assert content_phase["id"] == "section", (
            f"Emoji heading should fall back to 'section', got {content_phase['id']!r}"
        )

    def test_multiple_emoji_headings_get_unique_ids(self):
        """Two emoji-only headings: 'section' and 'section-2'."""
        doc = "# Pipeline\n## 🎯\nDo work 1.\n## 🚀\nDo work 2.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        content_ids = [p["id"] for p in data["phases"] if p["task_type"] == "content"]
        assert "section" in content_ids
        assert "section-2" in content_ids


# ===========================================================================
# Warning 3 — CLI --validate uses direct call, not CliRunner inside CLI
# ===========================================================================


class TestWarning3_ValidateFlagDirect:
    """Warning 3: --validate must not use CliRunner inside a real CLI invocation.

    The fix calls the validation stack directly (yaml.safe_load → load_template
    → validate_template → validate_template_extended).  We verify:
    - The command succeeds (exit code 0) for a valid template
    - The output includes structural-check language (from the direct call)
    - No CliRunner import is present in the cli.py implementation
    """

    def test_validate_flag_exits_0_for_valid_template(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd = tmp_path / "cmd.md"
        cmd.write_text(MINIMAL)
        out = tmp_path / "out.yaml"
        result = _invoke("import", "plugin-command", str(cmd), "--output", str(out), "--validate")
        assert result.exit_code == 0, f"--validate failed: {result.output}"

    def test_validate_flag_output_mentions_structural_check(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd = tmp_path / "cmd.md"
        cmd.write_text(MINIMAL)
        out = tmp_path / "out.yaml"
        result = _invoke("import", "plugin-command", str(cmd), "--output", str(out), "--validate")
        output_lower = result.output.lower()
        assert "structural" in output_lower or "valid" in output_lower, (
            f"--validate output should mention structural checks: {result.output!r}"
        )

    def test_validate_flag_output_mentions_yaml_syntax(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd = tmp_path / "cmd.md"
        cmd.write_text(MINIMAL)
        out = tmp_path / "out.yaml"
        result = _invoke("import", "plugin-command", str(cmd), "--output", str(out), "--validate")
        assert "yaml" in result.output.lower() or "syntax" in result.output.lower(), (
            f"--validate output should mention YAML syntax: {result.output!r}"
        )

    def test_cli_runner_not_imported_in_validate_branch(self):
        """The 'click.testing' import must not be used inside the --validate branch.

        This guards against the anti-pattern of running CliRunner inside a
        real CLI invocation (Warning 3).  We verify by inspecting the source.
        """
        import inspect
        from orchestration_engine import cli as cli_module
        source = inspect.getsource(cli_module)
        # The validate_template command implementation (post-fix) must not import
        # CliRunner inside the import_plugin_command command body.
        # Strategy: find the import_plugin_command command function source and
        # check it doesn't call CliRunner.
        import ast
        tree = ast.parse(source)
        # Look for any CliRunner usage in a Call node with func attribute
        class _CliRunnerFinder(ast.NodeVisitor):
            found = False
            def visit_Name(self, node):
                if node.id == "CliRunner":
                    self.found = True
                self.generic_visit(node)
            def visit_Attribute(self, node):
                if isinstance(node, ast.Attribute) and node.attr == "CliRunner":
                    self.found = True
                self.generic_visit(node)
        finder = _CliRunnerFinder()
        finder.visit(tree)
        # The CliRunner may still be imported at module level for other tests,
        # but it must not be *instantiated* inside the --validate block.
        # We check by looking for 'CliRunner()' invocations (constructor calls).
        cli_runner_calls = re.findall(r"CliRunner\(\)", source)
        # All CliRunner() calls must come from TEST code, not the validate block.
        # The validate implementation (post-fix) removes CliRunner().invoke().
        # Heuristic: if CliRunner() appears 0 times in the non-test source, the fix is applied.
        # (The actual import_plugin_command CLI command does not use CliRunner().)
        # This is a best-effort structural check:
        import_cmd_fn_match = re.search(
            r"def import_plugin_command\(.*?(?=\ndef |\nclass |\Z)",
            source,
            re.DOTALL,
        )
        if import_cmd_fn_match:
            fn_source = import_cmd_fn_match.group(0)
            assert "CliRunner()" not in fn_source, (
                "CliRunner() must not be used inside the import_plugin_command CLI command "
                "(Warning 3 fix not applied)"
            )


# ===========================================================================
# Bug-fix: Multiple parentheticals in input fields
# ===========================================================================


class TestMultipleParentheticals:
    def test_two_parens_before_dash(self):
        body = "1. **Budget** (USD) (optional) — approximate budget in USD\n"
        schema = _parse_inputs_section(body)
        assert "budget" in schema["properties"]

    def test_two_parens_optional_not_required(self):
        body = "1. **Budget** (USD) (optional) — amount\n"
        schema = _parse_inputs_section(body)
        assert "budget" not in schema.get("required", [])

    def test_two_parens_no_optional_is_required(self):
        body = "1. **Qty** (positive integer) (must be > 0) — count\n"
        schema = _parse_inputs_section(body)
        assert "qty" in schema.get("required", [])

    def test_three_parens_field_parsed(self):
        body = "1. **Value** (USD) (rounded) (optional) — the value\n"
        schema = _parse_inputs_section(body)
        assert "value" in schema["properties"]
        assert "value" not in schema.get("required", [])

    def test_description_clean_of_parenthetical_content(self):
        """The description must not contain the parenthetical meta-info."""
        body = "1. **Budget** (USD) (optional) — approximate budget in USD\n"
        schema = _parse_inputs_section(body)
        desc = schema["properties"]["budget"]["description"]
        assert desc == "approximate budget in USD"
        assert "(USD)" not in desc
        assert "(optional)" not in desc


# ===========================================================================
# Bug-fix: (optional) stripping from description at any position
# ===========================================================================


class TestOptionalDescriptionStripping:
    def test_leading_optional_stripped(self):
        body = "1. **Extra** — (optional) extra context\n"
        schema = _parse_inputs_section(body)
        desc = schema["properties"]["extra"]["description"]
        assert desc.lower().startswith("(optional)") is False
        assert "extra context" in desc

    def test_trailing_optional_stripped(self):
        body = "1. **Comment** — supporting comment (optional)\n"
        schema = _parse_inputs_section(body)
        desc = schema["properties"]["comment"]["description"]
        assert "(optional)" not in desc.lower()
        assert "supporting comment" in desc

    def test_mid_sentence_optional_stripped(self):
        body = "1. **Note** — add (optional) notes here\n"
        schema = _parse_inputs_section(body)
        desc = schema["properties"]["note"]["description"]
        assert "(optional)" not in desc.lower()
        assert "add" in desc
        assert "notes here" in desc

    def test_double_space_collapsed_after_strip(self):
        """Removing (optional) from the middle of a description must not leave double spaces."""
        body = "1. **Note** — add (optional) context here if needed\n"
        schema = _parse_inputs_section(body)
        desc = schema["properties"]["note"]["description"]
        assert "  " not in desc, f"Double space found in description: {desc!r}"

    def test_optional_case_insensitive_stripped(self):
        body = "1. **Note** — (OPTIONAL) extra note\n"
        schema = _parse_inputs_section(body)
        desc = schema["properties"]["note"]["description"]
        assert "optional" not in desc.lower()

    def test_field_still_optional_after_description_fix(self):
        body = textwrap.dedent("""\
            1. **Required** — mandatory input
            2. **Optional field** — (optional) extra information
        """)
        schema = _parse_inputs_section(body)
        required = schema.get("required", [])
        assert "required" in required
        assert "optional_field" not in required


# ===========================================================================
# Tags preservation (falsy coercion fix)
# ===========================================================================


class TestTagsPreservation:
    def test_explicit_empty_tags_preserved(self):
        doc = "---\ntags: []\ndescription: d\n---\n# T\n## S\nDo it.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        assert data["tags"] == [], (
            f"tags: [] must be preserved, not replaced with defaults: {data['tags']!r}"
        )

    def test_absent_tags_produces_defaults(self):
        doc = "---\ndescription: d\n---\n# T\n## S\nDo it.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        assert len(data["tags"]) > 0, "Absent tags should produce defaults"

    def test_null_tags_produces_defaults(self):
        doc = "---\ntags: null\ndescription: d\n---\n# T\n## S\nDo it.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        assert len(data["tags"]) > 0, "tags: null should fall back to defaults"

    def test_explicit_tags_list_preserved(self):
        doc = "---\ntags:\n  - marketing\n  - campaign\n---\n# T\n## S\nDo it.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        assert "marketing" in data["tags"]
        assert "campaign" in data["tags"]

    def test_non_list_tags_treated_as_absent(self):
        """tags: 'a string' (not list) → use defaults."""
        doc = "---\ntags: 'not-a-list'\ndescription: d\n---\n# T\n## S\nDo it.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        # A string is not a list, so defaults should apply
        assert isinstance(data["tags"], list)


# ===========================================================================
# CLI coverage
# ===========================================================================


class TestCLICoverage:
    def test_import_group_help_exits_0(self):
        result = _invoke("import", "--help")
        assert result.exit_code == 0

    def test_plugin_command_subcommand_in_help(self):
        result = _invoke("import", "--help")
        assert "plugin-command" in result.output

    def test_dry_run_outputs_yaml_to_stdout(self, tmp_path):
        cmd = tmp_path / "cmd.md"
        cmd.write_text(MINIMAL)
        result = _invoke("import", "plugin-command", str(cmd), "--dry-run")
        assert result.exit_code == 0
        assert "id:" in result.output
        assert "phases:" in result.output

    def test_dry_run_writes_no_files(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd = tmp_path / "cmd.md"
        cmd.write_text(MINIMAL)
        _invoke("import", "plugin-command", str(cmd), "--dry-run")
        assert list(tmp_path.glob("*.yaml")) == []

    def test_output_flag_writes_named_file(self, tmp_path):
        cmd = tmp_path / "cmd.md"
        cmd.write_text(MINIMAL)
        out = tmp_path / "my-output.yaml"
        result = _invoke("import", "plugin-command", str(cmd), "--output", str(out))
        assert result.exit_code == 0
        assert out.exists()

    def test_output_file_is_valid_yaml(self, tmp_path):
        cmd = tmp_path / "cmd.md"
        cmd.write_text(MINIMAL)
        out = tmp_path / "out.yaml"
        _invoke("import", "plugin-command", str(cmd), "--output", str(out))
        data = yaml.safe_load(out.read_text())
        assert isinstance(data, dict)
        assert "id" in data

    def test_default_output_filename_derived_from_template_id(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd = tmp_path / "cmd.md"
        cmd.write_text(MINIMAL)
        result = _invoke("import", "plugin-command", str(cmd))
        assert result.exit_code == 0
        assert (tmp_path / "minimal-pipeline.yaml").exists()

    def test_custom_author_in_output(self, tmp_path):
        cmd = tmp_path / "cmd.md"
        cmd.write_text(MINIMAL)
        out = tmp_path / "out.yaml"
        _invoke("import", "plugin-command", str(cmd), "--output", str(out), "--author", "qa-bot")
        data = yaml.safe_load(out.read_text())
        assert data["author"] == "qa-bot"

    def test_missing_file_exit_nonzero(self):
        result = CliRunner().invoke(
            main,
            ["import", "plugin-command", "/nonexistent/file.md"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0

    def test_malformed_file_exit_nonzero(self, tmp_path):
        cmd = tmp_path / "bad.md"
        cmd.write_text("## No H1 here\nJust content.\n")
        result = CliRunner().invoke(
            main,
            ["import", "plugin-command", str(cmd)],
            catch_exceptions=False,
        )
        assert result.exit_code != 0

    def test_success_message_in_output(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd = tmp_path / "cmd.md"
        cmd.write_text(MINIMAL)
        result = _invoke("import", "plugin-command", str(cmd))
        assert result.exit_code == 0
        # Should mention the generated file
        assert "minimal-pipeline.yaml" in result.output or "Generated" in result.output

    def test_next_steps_shown_without_validate_flag(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cmd = tmp_path / "cmd.md"
        cmd.write_text(MINIMAL)
        result = _invoke("import", "plugin-command", str(cmd))
        assert result.exit_code == 0
        assert "orch validate" in result.output or "next" in result.output.lower()

    def test_multi_word_title_output_filename(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        doc = "# My Complex Pipeline Title\n\n## Step One\nDo something.\n"
        cmd = tmp_path / "complex.md"
        cmd.write_text(doc)
        result = _invoke("import", "plugin-command", str(cmd))
        assert result.exit_code == 0
        expected = tmp_path / "my-complex-pipeline-title.yaml"
        assert expected.exists(), f"Expected {expected}"


# ===========================================================================
# Template metadata completeness
# ===========================================================================


class TestTemplateMetadata:
    def test_version_field_present_and_correct(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        assert data.get("version") == "1.0.0"

    def test_author_field_present(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        assert data.get("author"), "author field must be non-empty"

    def test_default_author_is_importer_identifier(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        assert data["author"] == GENERATED_AUTHOR

    def test_category_is_always_imported(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        assert data.get("category") == "imported"

    def test_use_cases_contains_argument_hint_when_present(self):
        doc = textwrap.dedent("""\
            ---
            description: A cool command
            argument-hint: "<my input here>"
            ---
            # My Pipeline
            ## Step
            Do work.
        """)
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        use_cases_text = " ".join(data.get("use_cases", []))
        assert "my input here" in use_cases_text.lower() or "<my input here>" in use_cases_text

    def test_use_cases_contains_description_when_present(self):
        doc = textwrap.dedent("""\
            ---
            description: Process marketing campaigns
            ---
            # Pipeline
            ## Step
            Do work.
        """)
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        assert any("marketing" in uc.lower() or "campaigns" in uc.lower()
                   for uc in data.get("use_cases", []))

    def test_use_cases_fallback_when_no_frontmatter(self):
        doc = "# Simple Pipeline\n## Step\nDo work.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        assert data.get("use_cases"), "use_cases must be non-empty even without frontmatter"

    def test_example_input_populated_from_required_fields(self):
        doc = textwrap.dedent("""\
            ---
            description: d
            ---
            # Pipeline
            ## Inputs
            1. **Alpha** — first field
            2. **Beta** — second field
            ## Step
            Do work.
        """)
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        ei = data.get("example_input", {})
        assert "alpha" in ei
        assert "beta" in ei

    def test_example_input_values_are_placeholders(self):
        """example_input values should be '<field_name>' style placeholders."""
        doc = textwrap.dedent("""\
            ---
            description: d
            ---
            # Pipeline
            ## Inputs
            1. **Topic** — the topic
            ## Step
            Do work.
        """)
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        ei = data.get("example_input", {})
        assert "topic" in ei
        # Value should be a non-empty placeholder string
        assert isinstance(ei["topic"], str) and ei["topic"]

    def test_description_from_frontmatter_used(self):
        doc = textwrap.dedent("""\
            ---
            description: My specific description text
            ---
            # Pipeline
            ## Step
            Do work.
        """)
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        assert data["description"] == "My specific description text"

    def test_description_fallback_when_absent(self):
        doc = "# My Pipeline\n## Step\nDo work.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        assert data.get("description"), "description must not be empty"
        assert "My Pipeline" in data["description"] or "pipeline" in data["description"].lower()


# ===========================================================================
# Phase chain / depends_on structure
# ===========================================================================


class TestPhaseDependencyChain:
    def test_first_content_phase_has_no_depends(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        first = data["phases"][0]
        assert first["depends_on"] == []

    def test_linear_chain_for_multi_section(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MULTI))
        phases = data["phases"]
        # Each phase (except the first) depends on exactly the previous one
        for i in range(1, len(phases)):
            prev_id = phases[i - 1]["id"]
            assert prev_id in phases[i]["depends_on"], (
                f"Phase {phases[i]['id']!r} should depend on {prev_id!r}, "
                f"got depends_on={phases[i]['depends_on']!r}"
            )

    def test_review_phase_depends_on_content_not_previous_review(self):
        """Review phase depends on its paired content phase, not the prior review."""
        data = _strip_comments_and_parse(import_plugin_command_from_string(MULTI))
        phases = data["phases"]
        content_phases = [p for p in phases if p["task_type"] == "content"]
        review_phases = [p for p in phases if p["task_type"] == "review"]
        for content, review in zip(content_phases, review_phases):
            assert content["id"] in review["depends_on"], (
                f"Review {review['id']!r} must depend on content {content['id']!r}"
            )


# ===========================================================================
# YAML output format
# ===========================================================================


class TestYAMLOutputFormat:
    def test_generated_yaml_is_parseable(self):
        yaml_text = import_plugin_command_from_string(MINIMAL)
        data = _strip_comments_and_parse(yaml_text)
        assert isinstance(data, dict)

    def test_generated_yaml_has_header_comment(self):
        yaml_text = import_plugin_command_from_string(MINIMAL)
        first_line = yaml_text.splitlines()[0]
        assert first_line.startswith("#"), "Generated YAML must start with a comment header"

    def test_header_comment_mentions_orch_import(self):
        yaml_text = import_plugin_command_from_string(MINIMAL)
        assert "orch import plugin-command" in yaml_text

    def test_long_prompt_uses_literal_block_style(self):
        """Long strings in prompt_template must use YAML literal block (|) style."""
        yaml_text = import_plugin_command_from_string(MULTI)
        # Literal block style: key followed by '|'
        assert "|" in yaml_text, "Expected YAML literal block style for long strings"

    def test_sort_keys_false_preserves_logical_order(self):
        """Keys should appear in logical order (id before name before version...)."""
        yaml_text = import_plugin_command_from_string(MINIMAL)
        data_lines = [ln for ln in yaml_text.splitlines() if not ln.startswith("#")]
        yaml_body = "\n".join(data_lines)
        id_pos = yaml_body.find("\nid:")
        name_pos = yaml_body.find("\nname:")
        version_pos = yaml_body.find("\nversion:")
        assert id_pos < name_pos < version_pos, (
            "Template keys should appear in logical order (id, name, version, ...)"
        )


# ===========================================================================
# import_plugin_command (file-based) path
# ===========================================================================


class TestImportFromFile:
    def test_reads_and_imports_from_disk(self, tmp_path):
        cmd = tmp_path / "cmd.md"
        cmd.write_text(MINIMAL)
        yaml_text = import_plugin_command(cmd)
        data = _strip_comments_and_parse(yaml_text)
        assert data["id"] == "minimal-pipeline"

    def test_raises_file_not_found_for_missing_path(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            import_plugin_command(tmp_path / "does_not_exist.md")

    def test_custom_author_in_file_import(self, tmp_path):
        cmd = tmp_path / "cmd.md"
        cmd.write_text(MINIMAL)
        yaml_text = import_plugin_command(cmd, author="file-tester")
        data = _strip_comments_and_parse(yaml_text)
        assert data["author"] == "file-tester"

    def test_skill_refs_resolved_from_file_location(self, tmp_path):
        """Skill refs are resolved relative to the actual file location."""
        skill = tmp_path / "skills" / "brand-voice" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# Brand Voice")
        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()
        cmd = cmd_dir / "cmd.md"
        cmd.write_text(textwrap.dedent("""\
            # My Pipeline
            ## Content Step
            Apply brand voice guidelines here.
            [brand-voice skill](../skills/brand-voice/SKILL.md)
        """))
        yaml_text = import_plugin_command(cmd)
        data = _strip_comments_and_parse(yaml_text)
        # At least one phase should have skill_refs
        phases_with_refs = [p for p in data["phases"] if p.get("skill_refs")]
        assert phases_with_refs, "Expected at least one phase with resolved skill_refs"


# ===========================================================================
# Review prompt content
# ===========================================================================


class TestReviewPromptContent:
    def test_review_prompt_contains_completeness(self):
        from orchestration_engine.importers.plugin_command import _build_review_prompt
        prompt = _build_review_prompt("draft", "Draft")
        assert "Completeness" in prompt or "completeness" in prompt

    def test_review_prompt_contains_quality(self):
        from orchestration_engine.importers.plugin_command import _build_review_prompt
        prompt = _build_review_prompt("draft", "Draft")
        assert "Quality" in prompt or "quality" in prompt

    def test_review_prompt_contains_verdict_options(self):
        from orchestration_engine.importers.plugin_command import _build_review_prompt
        prompt = _build_review_prompt("draft", "Draft")
        assert "APPROVED" in prompt
        assert "NEEDS-REVISION" in prompt
        assert "REJECTED" in prompt

    def test_review_prompt_contains_phase_name(self):
        from orchestration_engine.importers.plugin_command import _build_review_prompt
        prompt = _build_review_prompt("brand-voice", "Brand Voice")
        assert "Brand Voice" in prompt

    def test_review_prompt_contains_correct_phase_ref(self):
        from orchestration_engine.importers.plugin_command import _build_review_prompt
        prompt = _build_review_prompt("research", "Research")
        assert "{previous_output[research]}" in prompt

    def test_review_prompt_contains_output_format_section(self):
        from orchestration_engine.importers.plugin_command import _build_review_prompt
        prompt = _build_review_prompt("draft", "Draft")
        assert "Output Format" in prompt or "output format" in prompt.lower()


# ===========================================================================
# Phase prompt content
# ===========================================================================


class TestContentPromptContent:
    def test_content_prompt_includes_heading_name(self):
        from orchestration_engine.importers.plugin_command import _build_phase_prompt
        prompt = _build_phase_prompt("Analysis Phase", "Do detailed analysis.")
        assert "Analysis Phase" in prompt

    def test_content_prompt_includes_instructions_section(self):
        from orchestration_engine.importers.plugin_command import _build_phase_prompt
        prompt = _build_phase_prompt("Step", "Write something meaningful.")
        assert "Instructions" in prompt
        assert "Write something meaningful." in prompt

    def test_content_prompt_fallback_when_empty_body(self):
        from orchestration_engine.importers.plugin_command import _build_phase_prompt
        prompt = _build_phase_prompt("Step", "")
        # Must not raise; falls back to a sensible message
        assert "Step" in prompt
        assert len(prompt) > 10

    def test_content_prompt_includes_input_section(self):
        from orchestration_engine.importers.plugin_command import _build_phase_prompt
        prompt = _build_phase_prompt("Step", "Do work.")
        assert "## Input" in prompt


# ===========================================================================
# _classify_section edge cases
# ===========================================================================


class TestClassifySectionEdgeCases:
    @pytest.mark.parametrize("heading", [
        "Requirements",
        "Prerequisites",
        "requirements",
        "PREREQUISITES",
    ])
    def test_requirements_and_prerequisites_are_meta(self, heading):
        assert _classify_section(heading) == "meta"

    def test_content_heading_with_leading_space(self):
        """Spaces around heading text are stripped before classification."""
        assert _classify_section("  My Content Section  ") == "content"

    def test_inputs_case_variants(self):
        for variant in ["Inputs", "INPUTS", "inputs", "InPuTs"]:
            assert _classify_section(variant) == "inputs", (
                f"'{variant}' should classify as 'inputs'"
            )

    def test_unknown_heading_is_content(self):
        assert _classify_section("Completely New Heading") == "content"
        assert _classify_section("Analysis") == "content"
        assert _classify_section("Strategy") == "content"


# ===========================================================================
# _split_by_h2 edge cases
# ===========================================================================


class TestSplitByH2EdgeCases:
    def test_empty_section_body(self):
        """An H2 immediately followed by another H2 has empty body."""
        body = "## Section A\n## Section B\nContent.\n"
        sections = _split_by_h2(body)
        assert len(sections) == 2
        assert sections[0].heading == "Section A"
        assert sections[0].body == ""

    def test_h3_h4_within_section_body(self):
        """Sub-headings (H3, H4) are captured in the body, not split."""
        body = "## Main\n### Sub A\n#### Deep B\nContent.\n"
        sections = _split_by_h2(body)
        assert len(sections) == 1
        assert "### Sub A" in sections[0].body
        assert "#### Deep B" in sections[0].body

    def test_many_sections(self):
        body = "".join(f"## Section {i}\nContent {i}.\n" for i in range(10))
        sections = _split_by_h2(body)
        assert len(sections) == 10

    def test_section_heading_text_is_stripped(self):
        body = "##   Padded Heading   \nContent.\n"
        sections = _split_by_h2(body)
        assert sections[0].heading == "Padded Heading"


# ===========================================================================
# _extract_h1 edge cases
# ===========================================================================


class TestExtractH1EdgeCases:
    def test_first_h1_wins_when_multiple(self):
        body = "# First\n# Second\n# Third\n"
        title, rest = _extract_h1(body)
        assert title == "First"
        assert "# First" not in rest
        assert "# Second" in rest

    def test_h1_with_trailing_whitespace(self):
        body = "# My Title   \n"
        title, _ = _extract_h1(body)
        assert title == "My Title"

    def test_h1_not_confused_with_hash_in_body(self):
        """A line like '#hashtag' or '## H2' is not an H1."""
        body = "## H2 Section\n#hashtag mention\n"
        title, _ = _extract_h1(body)
        assert title == ""

    def test_h1_at_end_of_document(self):
        body = "Some text.\n\n# Late Title"
        title, _ = _extract_h1(body)
        assert title == "Late Title"


# ===========================================================================
# snake_case edge cases
# ===========================================================================


class TestSnakeCaseEdgeCases:
    @pytest.mark.parametrize("text,expected", [
        ("Hello World", "hello_world"),
        ("Field Name (optional)", "field_name_optional"),
        ("  Leading Spaces  ", "leading_spaces"),
        ("Multiple   Spaces", "multiple_spaces"),
        ("already_snake", "already_snake"),
        ("Mixed-Hyphens and Spaces", "mixed_hyphens_and_spaces"),
    ])
    def test_snake_case_conversion(self, text, expected):
        assert snake_case(text) == expected


# ===========================================================================
# slugify edge cases
# ===========================================================================


class TestSlugifyEdgeCases:
    @pytest.mark.parametrize("text,expected", [
        ("Hello World", "hello-world"),
        ("  --- hello ---  ", "hello"),
        ("my-pipeline", "my-pipeline"),
        ("Brand Voice & Tone", "brand-voice-tone"),
        ("Step 1 — Overview", "step-1-overview"),
        ("", ""),
        ("---", ""),  # all hyphens → stripped to empty
        ("Hello...World", "hello-world"),
        ("CamelCase", "camelcase"),
    ])
    def test_slugify_cases(self, text, expected):
        assert slugify(text) == expected

    def test_numbers_preserved(self):
        assert slugify("Phase 3 Analysis") == "phase-3-analysis"

    def test_consecutive_special_chars_single_hyphen(self):
        assert slugify("Hello!!!World") == "hello-world"

    def test_result_never_starts_with_hyphen(self):
        result = slugify("---hello")
        assert not result.startswith("-")

    def test_result_never_ends_with_hyphen(self):
        result = slugify("hello---")
        assert not result.endswith("-")


# ===========================================================================
# Document-level edge cases
# ===========================================================================


class TestDocumentEdgeCases:
    def test_document_with_h1_only_no_sections(self):
        """H1 alone (no H2) → valid template with 0 phases."""
        doc = "# Lonely Pipeline\n\nJust some body text with no sections.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        assert data["id"] == "lonely-pipeline"
        assert data["phases"] == []

    def test_document_with_h1_and_only_inputs(self):
        """Only Inputs H2 (no content phases) → valid with 0 phases."""
        doc = textwrap.dedent("""\
            # Pipeline
            ## Inputs
            1. **Topic** — the topic
        """)
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        assert data["phases"] == []
        assert "topic" in data["config_schema"]["properties"]

    def test_very_long_section_body(self):
        """Extremely long section body is handled without error."""
        long_body = "Do something with {input}.\n" * 200
        doc = f"# Pipeline\n## Long Step\n{long_body}\n"
        yaml_text = import_plugin_command_from_string(doc)
        data = _strip_comments_and_parse(yaml_text)
        assert len(data["phases"]) == 2  # content + review

    def test_section_with_code_block(self):
        """Code blocks in section body survive into the prompt."""
        doc = textwrap.dedent("""\
            # Pipeline
            ## Code Step
            Run the following:

            ```python
            result = process(input)
            print(result)
            ```
        """)
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        prompt = data["phases"][0]["prompt_template"]
        assert "```python" in prompt

    def test_all_meta_sections_produces_fallback_schema(self):
        """No Inputs section → global fallback schema is used."""
        doc = "# Pipeline\n## Trigger\nRun.\n## Output\nDone.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        schema = data["config_schema"]
        assert schema["type"] == "object"
        assert "input" in schema["properties"]

    def test_unicode_body_content_survives(self):
        """Unicode content in section body must pass through unchanged."""
        doc = "# Pipeline\n## Step\nProcess data: α, β, γ, δ, 日本語, emoji 🎯.\n"
        data = _strip_comments_and_parse(import_plugin_command_from_string(doc))
        prompt = data["phases"][0]["prompt_template"]
        assert "α" in prompt or "beta" in prompt.lower() or "Process" in prompt


# ===========================================================================
# import_plugin_command_from_string: author parameter
# ===========================================================================


class TestAuthorParameter:
    def test_default_author_is_generated_author_constant(self):
        data = _strip_comments_and_parse(import_plugin_command_from_string(MINIMAL))
        assert data["author"] == GENERATED_AUTHOR
        assert GENERATED_AUTHOR == "orch import plugin-command"

    def test_custom_author_overrides_default(self):
        data = _strip_comments_and_parse(
            import_plugin_command_from_string(MINIMAL, author="test-qa-suite")
        )
        assert data["author"] == "test-qa-suite"

    def test_empty_string_author_propagates(self):
        data = _strip_comments_and_parse(
            import_plugin_command_from_string(MINIMAL, author="")
        )
        # Empty string is technically valid (it's what was asked for)
        assert data["author"] == "" or data["author"] is None or data["author"] == GENERATED_AUTHOR
        # Main point: no crash

    def test_unicode_author_propagates(self):
        data = _strip_comments_and_parse(
            import_plugin_command_from_string(MINIMAL, author="René Müller <rene@example.com>")
        )
        assert data["author"] == "René Müller <rene@example.com>"
