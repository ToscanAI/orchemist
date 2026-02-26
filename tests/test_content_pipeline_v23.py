import pytest
from pathlib import Path
from src.orchestration_engine.templates import TemplateEngine

TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "content-pipeline.yaml"

class TestContentPipelineV23:
    def test_loads_successfully(self):
        engine = TemplateEngine()
        tmpl = engine.load_template(TEMPLATE_PATH)
        assert tmpl.id == "content-pipeline-v24"
        assert tmpl.version == "2.4.0"

    def test_has_10_phases(self):
        engine = TemplateEngine()
        tmpl = engine.load_template(TEMPLATE_PATH)
        assert len(tmpl.phases) == 10

    def test_phase_ids(self):
        engine = TemplateEngine()
        tmpl = engine.load_template(TEMPLATE_PATH)
        ids = [p.id for p in tmpl.phases]
        assert ids == ["research", "outline", "draft", "flow-review", "red-team", "consistency", "final-7a", "final-7b", "final-7c", "select-best"]

    def test_parallel_finals(self):
        engine = TemplateEngine()
        tmpl = engine.load_template(TEMPLATE_PATH)
        phases = {p.id: p for p in tmpl.phases}
        # All three finals depend on draft + their reviewer, not on each other
        assert "draft" in phases["final-7a"].depends_on
        assert "flow-review" in phases["final-7a"].depends_on
        assert "draft" in phases["final-7b"].depends_on
        assert "red-team" in phases["final-7b"].depends_on
        assert "draft" in phases["final-7c"].depends_on
        assert "consistency" in phases["final-7c"].depends_on
        # No cross-dependencies between finals
        for fid in ["final-7a", "final-7b", "final-7c"]:
            deps = phases[fid].depends_on
            others = [f for f in ["final-7a", "final-7b", "final-7c"] if f != fid]
            for o in others:
                assert o not in deps

    def test_select_best_depends_on_all_finals(self):
        engine = TemplateEngine()
        tmpl = engine.load_template(TEMPLATE_PATH)
        phases = {p.id: p for p in tmpl.phases}
        assert set(phases["select-best"].depends_on) == {"final-7a", "final-7b", "final-7c"}

    def test_human_review_phases(self):
        engine = TemplateEngine()
        tmpl = engine.load_template(TEMPLATE_PATH)
        phases = {p.id: p for p in tmpl.phases}
        assert getattr(phases["outline"], "human_review", False) == True
        assert getattr(phases["red-team"], "human_review", False) == True

    def test_config_schema_fields(self):
        engine = TemplateEngine()
        tmpl = engine.load_template(TEMPLATE_PATH)
        props = tmpl.config_schema.get("properties", {})
        assert "topic" in props
        assert "audience" in props
        assert "tone" in props
        assert "word_count" in props
        assert "publication" in props

    def test_doc_fields(self):
        engine = TemplateEngine()
        tmpl = engine.load_template(TEMPLATE_PATH)
        assert tmpl.author == "Toscan"
        assert tmpl.category == "content"
        assert len(tmpl.tags) >= 3
        assert len(tmpl.use_cases) >= 2
        assert tmpl.example_input is not None

    def test_validates_clean(self):
        engine = TemplateEngine()
        tmpl = engine.load_template(TEMPLATE_PATH)
        import yaml
        raw = yaml.safe_load(TEMPLATE_PATH.read_text())
        errors, warnings = engine.validate_template_extended(tmpl, raw)
        assert len(errors) == 0

    def test_model_tiers(self):
        engine = TemplateEngine()
        tmpl = engine.load_template(TEMPLATE_PATH)
        phases = {p.id: p for p in tmpl.phases}
        assert phases["red-team"].model_tier == "opus"
        assert phases["select-best"].model_tier == "opus"
        assert phases["final-7a"].model_tier == "haiku"
        assert phases["final-7c"].model_tier == "haiku"
