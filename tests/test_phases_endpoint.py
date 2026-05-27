"""Tests for GET /api/v1/phases (#842).

Used by frontend to hydrate phase metadata at boot, replacing the
hardcoded PHASES/PHASE_CARDS arrays in RunDetailClient.tsx and
skills/page.tsx that had drifted from the canonical YAML.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """FastAPI TestClient against the engine's app (real templates loaded)."""
    from orchestration_engine.web.api import create_api_app
    return TestClient(create_api_app())


class TestGetPhasesHappyPath:
    def test_default_pipeline_returns_standard(self, client):
        """No query param defaults to the standard coding pipeline."""
        resp = client.get("/api/v1/phases")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pipeline"] == "coding-pipeline-standard"
        assert "version" in body
        assert isinstance(body["phases"], list)
        assert len(body["phases"]) > 0

    def test_explicit_pipeline_query_works(self, client):
        resp = client.get("/api/v1/phases?pipeline=coding-pipeline-standard")
        assert resp.status_code == 200
        assert resp.json()["pipeline"] == "coding-pipeline-standard"

    def test_phase_entries_have_required_keys(self, client):
        """Every phase entry must have id, name, model_tier, task_type,
        depends_on, order — these are the contract for frontend hydration."""
        resp = client.get("/api/v1/phases")
        for phase in resp.json()["phases"]:
            for key in ("id", "name", "model_tier", "task_type", "depends_on", "order"):
                assert key in phase, f"missing key {key!r} in phase {phase!r}"

    def test_phase_order_is_zero_indexed_and_sequential(self, client):
        """Frontend relies on `order` being 0-based and matching YAML
        declaration order — drift here would scramble the Phase Rail."""
        phases = client.get("/api/v1/phases").json()["phases"]
        orders = [p["order"] for p in phases]
        assert orders == list(range(len(phases))), (
            f"order field is not 0-indexed sequential: {orders}"
        )

    def test_phase_0_is_first_for_standard_pipeline(self, client):
        """Post-#835: existing_symbols_inventory must be the first phase
        in the standard pipeline."""
        first = client.get("/api/v1/phases").json()["phases"][0]
        assert first["id"] == "existing_symbols_inventory"
        assert first["order"] == 0

    def test_model_tier_values_are_constrained(self, client):
        """Frontend's PhaseDef.tier type is `'sonnet' | 'opus' | 'engine'`
        — backend returns the YAML's model_tier (sonnet/opus) or None for
        engine phases. Verify all values fit this constrained set."""
        valid = {"sonnet", "opus", None}
        for phase in client.get("/api/v1/phases").json()["phases"]:
            assert phase["model_tier"] in valid, (
                f"unexpected model_tier {phase['model_tier']!r} for phase "
                f"{phase['id']!r} — frontend cannot render it"
            )


class TestGetPhasesEdgeCases:
    def test_unknown_pipeline_returns_404(self, client):
        resp = client.get("/api/v1/phases?pipeline=does-not-exist-xyz")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_pipeline_id_traversal_attempt_404s(self, client):
        """No path traversal: id is resolved through TemplateEngine which
        only accepts known template ids from its scan; an unknown id is
        404'd before any filesystem touch."""
        # URL-encode the slashes so FastAPI's path routing doesn't reject
        # the request before we reach the handler.
        resp = client.get("/api/v1/phases", params={"pipeline": "..%2F..%2Fetc%2Fpasswd"})
        assert resp.status_code == 404


class TestStandardPipelinePhaseList:
    """The canonical phase list for coding-pipeline-standard must be
    stable across version bumps — frontend tests + e2e screenshots anchor
    on these specific phase ids."""

    def test_full_phase_id_set(self, client):
        phases = client.get("/api/v1/phases").json()["phases"]
        ids = [p["id"] for p in phases]
        expected = {
            "existing_symbols_inventory", "spec", "behavioral", "spec_adversary",
            "postmortem_spec", "acceptance_test", "implement", "acceptance_run",
            "review", "fix", "postmortem_review", "test",
        }
        assert set(ids) == expected, (
            f"Standard pipeline phase set has drifted. Missing: "
            f"{expected - set(ids)}. Extra: {set(ids) - expected}. "
            f"If intentional, update both this test AND the frontend phase rail."
        )

    def test_only_review_is_opus_today(self, client):
        """Frozen-state drift sentinel. Today's engine YAML has
        spec_adversary AND review at opus tier per VISION pillar 8
        (max-effort adversary + review phases). When the YAML is updated
        to bump or regress any phase to/from opus, this test will fail —
        re-set the expected set to match the new state and add a CHANGELOG
        entry. See VISION.md §8."""
        phases = client.get("/api/v1/phases").json()["phases"]
        opus_phase_ids = {p["id"] for p in phases if p["model_tier"] == "opus"}
        assert opus_phase_ids == {"spec_adversary", "review"}, (
            f"Opus phase set has changed to {opus_phase_ids}. Expected "
            f"{{'spec_adversary', 'review'}} per VISION pillar 8. If this "
            f"change is intentional, update this test's expected set to "
            f"match and document the change in CHANGELOG. See VISION.md §8."
        )

    def test_task_type_marks_engine_phases(self, client):
        """acceptance_run has task_type='acceptance_run' (engine phase, no
        LLM). The frontend should derive 'engine' badge from task_type
        rather than from model_tier (which currently says 'sonnet' on
        engine phases — pre-existing YAML data quirk)."""
        phases = client.get("/api/v1/phases").json()["phases"]
        by_id = {p["id"]: p for p in phases}
        assert by_id["acceptance_run"]["task_type"] == "acceptance_run"


class TestDocumentedDuplicateGroupResolved:
    """Anchor for DUPLICATES_REFRESHED.md NEW Group A: this endpoint exists
    so the frontend doesn't need to hardcode phase metadata. The test
    documents the resolution in code — a refactor that removes the endpoint
    must also update the duplicate-audit doc."""

    def test_endpoint_is_reachable(self, client):
        """Functional probe: the endpoint exists, returns 200, returns
        a 'phases' list. A 404 here means the route registration regressed
        and frontend PhaseRail hydration would break."""
        resp = client.get("/api/v1/phases")
        assert resp.status_code == 200, (
            "/api/v1/phases endpoint returned non-200 — frontend "
            "PhaseRail hydration is broken; DUPLICATES NEW Group A reopens."
        )
        assert "phases" in resp.json()
