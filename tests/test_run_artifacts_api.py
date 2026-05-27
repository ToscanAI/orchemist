"""Tests for the run artifact endpoints introduced for the Harness redesign.

Three sibling endpoints under ``/api/v1/runs/{run_id}/``:
    - ``artifacts``           — list files in output_dir
    - ``artifacts/{filename}`` — read one file (path-traversal guarded)
    - ``phase0``              — parse existing_symbols.md into structured form
    - ``dialogue``            — parse cross-model dialogue artifact if present

All four are read-only — no file writes, no DB writes.
"""

from __future__ import annotations

import uuid as _uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestration_engine.db import Database
from orchestration_engine.web.api import create_api_app


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client_and_db(tmp_path: Path):
    db_file = tmp_path / "engine.db"
    app = create_api_app(db_path=db_file)
    client = TestClient(app)
    return client, db_file


# #862: route through the canonical helper.
def _insert_run_with_output(db_file: Path, output_dir: Path) -> str:
    """Insert a minimal pipeline_run record pointing at *output_dir*."""
    from tests._helpers import insert_pipeline_run as _impl
    run_id = str(_uuid.uuid4())[:8]
    db = Database(db_file)
    _impl(
        db,
        run_id=run_id,
        status="running",
        template_path="/tmp/fake.yaml",
        template_id="fake-template",
        output_dir=str(output_dir),
    )
    return run_id


# ── /api/v1/runs/{id}/artifacts ──────────────────────────────────────────────


class TestListArtifacts:
    def test_returns_files_sorted_by_name(self, client_and_db, tmp_path):
        client, db_file = client_and_db
        out = tmp_path / "out"
        out.mkdir()
        (out / "2_behavioral.md").write_text("contracts")
        (out / "0_existing_symbols.md").write_text("inventory")
        (out / "1_spec.md").write_text("spec")
        run_id = _insert_run_with_output(db_file, out)

        res = client.get(f"/api/v1/runs/{run_id}/artifacts")
        assert res.status_code == 200
        data = res.json()
        assert data["run_id"] == run_id
        names = [f["name"] for f in data["files"]]
        # Alphabetical sort matches phase order because of the numeric prefix
        assert names == ["0_existing_symbols.md", "1_spec.md", "2_behavioral.md"]
        # File entries carry size + mtime
        assert all("size_bytes" in f and "mtime" in f for f in data["files"])

    def test_excludes_hidden_files(self, client_and_db, tmp_path):
        client, db_file = client_and_db
        out = tmp_path / "out"
        out.mkdir()
        (out / "spec.md").write_text("spec")
        (out / ".orch-daemon.log").write_text("daemon")
        run_id = _insert_run_with_output(db_file, out)

        names = [f["name"] for f in client.get(f"/api/v1/runs/{run_id}/artifacts").json()["files"]]
        assert ".orch-daemon.log" not in names
        assert "spec.md" in names

    def test_returns_404_for_unknown_run(self, client_and_db):
        client, _ = client_and_db
        res = client.get("/api/v1/runs/nosuchrun/artifacts")
        assert res.status_code == 404

    def test_returns_404_when_output_dir_missing(self, client_and_db, tmp_path):
        client, db_file = client_and_db
        run_id = _insert_run_with_output(db_file, tmp_path / "nonexistent")
        res = client.get(f"/api/v1/runs/{run_id}/artifacts")
        assert res.status_code == 404


# ── /api/v1/runs/{id}/artifacts/{filename} ───────────────────────────────────


class TestGetArtifact:
    def test_returns_content_of_named_artifact(self, client_and_db, tmp_path):
        client, db_file = client_and_db
        out = tmp_path / "out"
        out.mkdir()
        (out / "spec.md").write_text("# Spec\n\nHello world.")
        run_id = _insert_run_with_output(db_file, out)

        res = client.get(f"/api/v1/runs/{run_id}/artifacts/spec.md")
        assert res.status_code == 200
        data = res.json()
        assert data["filename"] == "spec.md"
        assert data["content"] == "# Spec\n\nHello world."
        assert data["size_bytes"] == len("# Spec\n\nHello world.")

    def test_rejects_path_traversal_dotdot(self, client_and_db, tmp_path):
        client, db_file = client_and_db
        out = tmp_path / "out"
        out.mkdir()
        (tmp_path / "secret.txt").write_text("should not be readable")
        run_id = _insert_run_with_output(db_file, out)

        res = client.get(f"/api/v1/runs/{run_id}/artifacts/..%2Fsecret.txt")
        assert res.status_code in (400, 404)

    def test_rejects_absolute_path(self, client_and_db, tmp_path):
        client, db_file = client_and_db
        out = tmp_path / "out"
        out.mkdir()
        run_id = _insert_run_with_output(db_file, out)

        res = client.get(f"/api/v1/runs/{run_id}/artifacts/%2Fetc%2Fpasswd")
        assert res.status_code in (400, 404)

    def test_rejects_hidden_file(self, client_and_db, tmp_path):
        client, db_file = client_and_db
        out = tmp_path / "out"
        out.mkdir()
        (out / ".env").write_text("SECRET=xyz")
        run_id = _insert_run_with_output(db_file, out)

        res = client.get(f"/api/v1/runs/{run_id}/artifacts/.env")
        assert res.status_code == 400

    def test_truncates_oversize_artifact(self, client_and_db, tmp_path):
        client, db_file = client_and_db
        out = tmp_path / "out"
        out.mkdir()
        big = "x" * (2 * 1024 * 1024)  # 2 MiB > 1 MiB cap
        (out / "huge.md").write_text(big)
        run_id = _insert_run_with_output(db_file, out)

        data = client.get(f"/api/v1/runs/{run_id}/artifacts/huge.md").json()
        assert len(data["content"]) <= (1024 * 1024) + 200  # cap + truncation marker
        assert "truncated" in data["content"]


# ── /api/v1/runs/{id}/phase0 ─────────────────────────────────────────────────


_PHASE0_FIXTURE = """\
# Existing-symbols inventory for issue #42

Inventory date: 2026-05-25

## 1. UI primitives (consume — do NOT re-author)

- `Badge` ← `packages/ui/src/badge.tsx:14`
- `Button` ← `packages/ui/src/button.tsx:8`
- `Spinner` ← `packages/ui/src/spinner.tsx:5`

## 2. Project shared libraries

- `verdict_parser.extract_verdict` ← `src/.../verdict_parser.py:48`

## 3. Adjacent action / hook / route patterns (mirror byte-shape)

(empty — consumer did not provide inventory inputs for this category)

## 4. Workspace barrels (consumable cross-package imports)

- `saveResponse` ← `apps/web/lib/responses.ts:12`
- `loadResponses` ← `apps/web/lib/responses.ts:30`

## 5. Consume-vs-author guidance (sub-check 7d enforcement)

Verdict labels (CONSUME / EXTEND / DIVERGENT / BLOCKED).

## 6. SPEC's proposed new symbols

- **fooHelper** (verdict: NEW-OK)
- **barHelper** (verdict: EXTEND)
- **bazHelper** (verdict: CONSUME)
"""


class TestPhase0:
    def test_parses_section_counts(self, client_and_db, tmp_path):
        client, db_file = client_and_db
        out = tmp_path / "out"
        out.mkdir()
        (out / "existing_symbols.md").write_text(_PHASE0_FIXTURE)
        run_id = _insert_run_with_output(db_file, out)

        data = client.get(f"/api/v1/runs/{run_id}/phase0").json()
        assert data["sections"]["ui_primitives"]["count"] == 3
        assert data["sections"]["shared_libs"]["count"] == 1
        assert data["sections"]["adjacent_patterns"]["count"] == 0  # explicit empty stub
        assert data["sections"]["workspace_barrels"]["count"] == 2

    def test_parses_verdict_label_counts(self, client_and_db, tmp_path):
        client, db_file = client_and_db
        out = tmp_path / "out"
        out.mkdir()
        (out / "existing_symbols.md").write_text(_PHASE0_FIXTURE)
        run_id = _insert_run_with_output(db_file, out)

        data = client.get(f"/api/v1/runs/{run_id}/phase0").json()
        verdicts = data["verdicts"]
        # The literal word "CONSUME" appears in §5 once + §6 once = 2
        assert verdicts["CONSUME"] >= 1
        assert verdicts["EXTEND"] >= 1
        assert verdicts["NEW_OK"] >= 1

    def test_returns_404_when_no_phase0_artifact(self, client_and_db, tmp_path):
        client, db_file = client_and_db
        out = tmp_path / "out"
        out.mkdir()
        (out / "spec.md").write_text("spec only, no phase0")
        run_id = _insert_run_with_output(db_file, out)

        res = client.get(f"/api/v1/runs/{run_id}/phase0")
        assert res.status_code == 404


# ── /api/v1/runs/{id}/dialogue ───────────────────────────────────────────────


_DIALOGUE_FIXTURE = """\
# Spec review dialogue

## Round 1 · DRAFTER (claude-sonnet-4-6)

Proposes 4 new files.

## Round 1 · REVIEWER (gemini-3-pro) · REVISE

Two findings. Jaccard 0.0 (first round).

## Round 2 · DRAFTER (claude-sonnet-4-6)

Revised: 1 EXTEND, 2 CONSUME, 0 new files.

## Round 2 · REVIEWER (gemini-3-pro) · APPROVE

Looks good. Jaccard 0.93.
"""


class TestDialogue:
    def test_parses_rounds(self, client_and_db, tmp_path):
        client, db_file = client_and_db
        out = tmp_path / "out"
        out.mkdir()
        (out / "dialogue.md").write_text(_DIALOGUE_FIXTURE)
        run_id = _insert_run_with_output(db_file, out)

        data = client.get(f"/api/v1/runs/{run_id}/dialogue").json()
        rounds = data["rounds"]
        assert len(rounds) == 4
        assert rounds[0]["side"] == "drafter"
        assert rounds[0]["model"] == "claude-sonnet-4-6"
        assert rounds[1]["side"] == "reviewer"
        assert rounds[1]["verdict"] == "revise"
        assert rounds[3]["verdict"] == "approve"
        # Jaccard extracted from round-2 reviewer body
        assert rounds[3]["jaccard"] == 0.93

    def test_returns_404_when_no_dialogue_artifact(self, client_and_db, tmp_path):
        client, db_file = client_and_db
        out = tmp_path / "out"
        out.mkdir()
        (out / "spec.md").write_text("no dialogue here")
        run_id = _insert_run_with_output(db_file, out)

        res = client.get(f"/api/v1/runs/{run_id}/dialogue")
        assert res.status_code == 404
