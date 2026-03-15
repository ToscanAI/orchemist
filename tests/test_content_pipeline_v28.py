import pytest
import yaml
from pathlib import Path
from src.orchestration_engine.templates import TemplateEngine

TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "content-pipeline-v28.yaml"


class TestContentPipelineV28:
    @pytest.fixture(autouse=True, scope="class")
    def loaded_template(self, request):
        """Class-scoped fixture: load engine + template once for all tests."""
        engine = TemplateEngine()
        tmpl = engine.load_template(TEMPLATE_PATH)
        request.cls.engine = engine
        request.cls.tmpl = tmpl

    def test_loads_successfully(self):
        assert self.tmpl.id == "content-pipeline-v28"
        assert self.tmpl.name == "Content Pipeline v2.8"
        assert self.tmpl.version == "2.8.0"

    def test_has_7_phases(self):
        assert len(self.tmpl.phases) == 7

    def test_phase_ids(self):
        ids = [p.id for p in self.tmpl.phases]
        assert ids == [
            "research", "draft", "fact_check", "red_team",
            "apply_fixes", "voice_check", "final_polish"
        ]

    def test_phase_dependencies(self):
        phases = {p.id: p for p in self.tmpl.phases}
        # draft depends on research
        assert "research" in phases["draft"].depends_on
        # fact_check depends on draft and research
        assert "draft" in phases["fact_check"].depends_on
        assert "research" in phases["fact_check"].depends_on
        # red_team depends on draft
        assert "draft" in phases["red_team"].depends_on
        # apply_fixes depends on draft, fact_check, red_team
        assert "draft" in phases["apply_fixes"].depends_on
        assert "fact_check" in phases["apply_fixes"].depends_on
        assert "red_team" in phases["apply_fixes"].depends_on
        # voice_check depends on draft and apply_fixes
        assert "draft" in phases["voice_check"].depends_on
        assert "apply_fixes" in phases["voice_check"].depends_on
        # final_polish depends on apply_fixes and voice_check
        assert "apply_fixes" in phases["final_polish"].depends_on
        assert "voice_check" in phases["final_polish"].depends_on

    def test_no_human_review_phases(self):
        """v28 has no human_review fields — all phases return False."""
        for phase in self.tmpl.phases:
            assert getattr(phase, "human_review", False) == False

    def test_config_schema_fields(self):
        required = set(self.tmpl.config_schema.get("required", []))
        assert required == {"topic", "author_name", "author_facts", "voice_style", "source_material"}
        props = self.tmpl.config_schema.get("properties", {})
        # Optional fields are in properties but not required
        for optional in ("audience", "tone", "word_count", "publication"):
            assert optional in props
            assert optional not in required

    def test_doc_fields(self):
        assert self.tmpl.author == "Toscan"
        assert self.tmpl.category == "content"
        assert len(self.tmpl.tags) >= 3
        assert len(self.tmpl.use_cases) >= 2
        # v28 YAML has no example_input — value is absent, None, or empty dict
        assert getattr(self.tmpl, "example_input", None)  # v28 has example_input since #589

    def test_validates_clean(self):
        raw = yaml.safe_load(TEMPLATE_PATH.read_text())
        errors, warnings = self.engine.validate_template_extended(self.tmpl, raw)
        assert len(errors) == 0

    def test_model_tiers(self):
        phases = {p.id: p for p in self.tmpl.phases}
        assert phases["research"].model_tier == "sonnet"
        assert phases["draft"].model_tier == "opus"
        assert phases["fact_check"].model_tier == "sonnet"
        assert phases["red_team"].model_tier == "opus"
        assert phases["apply_fixes"].model_tier == "sonnet"
        assert phases["voice_check"].model_tier == "sonnet"
        assert phases["final_polish"].model_tier == "opus"
