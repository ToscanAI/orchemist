"""Tests for the harness aggregate endpoints (items 4, 6, 7 from the 2026-05-25 audit).

Endpoints under test:
    GET /api/v1/regressions
    GET /api/v1/stale-findings
    GET /api/v1/trust-profiles
    GET /api/v1/decisions
    GET /api/v1/admin/state
    PUT /api/v1/admin/feature-flags
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestration_engine.db import Database
from orchestration_engine.web.api import (
    create_api_app,
    _ADMIN_DEFAULTS,
    _coerce_admin_doc,
    _merge_feature_flags_with_passthrough,
    _strict_coerce_bool,
)


@pytest.fixture
def client_and_db(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    db_file = tmp_path / "engine.db"
    app = create_api_app(db_path=db_file)
    client = TestClient(app)
    return client, db_file, fake_home


# ── Shared helpers ───────────────────────────────────────────────────────────


def _insert_pipeline_run(db, run_id: str) -> None:
    with db._locked():
        c = db.get_connection()
        c.execute(
            "INSERT INTO pipeline_runs(run_id, template_path, template_id, input_json, mode, output_dir) "
            "VALUES(?,?,?,?,?,?)",
            (run_id, "/tmp/fake.yaml", "fake", "{}", "dry-run", "/tmp"),
        )
        c.commit()


def _insert_regression(db, regression_id: str, status: str = "detected") -> None:
    with db._locked():
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO regressions (id, commit_sha, ci_run_url, failure_type, affected_files, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (regression_id, "deadbeef" + regression_id, "https://ci/run/1",
             "AssertionError in test_foo", '["src/foo.py"]', status),
        )
        conn.commit()


def _insert_review_outcome(db, review_id: str, run_id: str, *, verdict: str = "APPROVE",
                           issues: str = '[]', model: str = "claude-opus-4-7") -> None:
    with db._locked():
        c = db.get_connection()
        c.execute(
            "INSERT INTO review_outcomes (review_id, run_id, phase_id, reviewer_model, verdict, issues_found) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (review_id, run_id, "review", model, verdict, issues),
        )
        c.commit()


# ── /api/v1/regressions ──────────────────────────────────────────────────────


class TestListRegressions:
    def test_empty_db_returns_empty_list(self, client_and_db):
        client, _, _ = client_and_db
        body = client.get("/api/v1/regressions").json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["limit"] == 50
        assert body["offset"] == 0

    def test_returns_inserted_regression_with_full_shape(self, client_and_db):
        """Lock down the full key set the harness depends on (BLOCKER #2 fix)."""
        client, db_file, _ = client_and_db
        db = Database(db_file)
        _insert_regression(db, "r1")
        body = client.get("/api/v1/regressions").json()
        assert body["total"] == 1
        item = body["items"][0]
        # Every key the RegressionRecord TS interface declares MUST be present.
        expected_keys = {
            "id", "commit_sha", "ci_run_url", "failure_type", "affected_files",
            "diagnosis", "fix_run_id", "status", "fix_attempt_count", "created_at",
        }
        assert expected_keys.issubset(set(item.keys())), (
            f"missing keys: {expected_keys - set(item.keys())}"
        )
        assert item["affected_files"] == ["src/foo.py"]

    def test_status_filter(self, client_and_db):
        client, db_file, _ = client_and_db
        db = Database(db_file)
        for status in ("detected", "fixing", "resolved"):
            _insert_regression(db, f"reg-{status}", status=status)
        assert client.get("/api/v1/regressions").json()["total"] == 3
        assert client.get("/api/v1/regressions?status=detected").json()["total"] == 1
        assert client.get("/api/v1/regressions?status=resolved").json()["total"] == 1
        assert client.get("/api/v1/regressions?status=zzzzz").json()["total"] == 0

    def test_clamps_limit(self, client_and_db):
        client, _, _ = client_and_db
        assert client.get("/api/v1/regressions?limit=999").json()["limit"] == 200
        assert client.get("/api/v1/regressions?limit=-5").json()["limit"] == 1

    def test_pagination_is_stable_with_tiebreaker(self, client_and_db):
        """Without a secondary sort key, ties at created_at can repeat/skip rows
        across pages. The endpoint now sorts by `created_at DESC, id DESC` —
        verify two consecutive pages contain disjoint IDs."""
        client, db_file, _ = client_and_db
        db = Database(db_file)
        # Insert 5 regressions in the same SQL transaction so their
        # `created_at` values are identical at second-resolution.
        for i in range(5):
            _insert_regression(db, f"tie-{i:02d}")
        page1 = client.get("/api/v1/regressions?limit=2&offset=0").json()["items"]
        page2 = client.get("/api/v1/regressions?limit=2&offset=2").json()["items"]
        ids_p1 = {r["id"] for r in page1}
        ids_p2 = {r["id"] for r in page2}
        assert len(page1) == 2 and len(page2) == 2
        # Pages must be disjoint — proves the tiebreaker is doing its job.
        assert ids_p1.isdisjoint(ids_p2), (
            f"pagination repeated rows: page1={ids_p1}, page2={ids_p2}"
        )

    def test_timestamps_are_z_suffixed(self, client_and_db):
        """SQLite's CURRENT_TIMESTAMP writes naive strings; the endpoint
        appends Z so JS doesn't interpret them as local time."""
        client, db_file, _ = client_and_db
        db = Database(db_file)
        _insert_regression(db, "tz-test")
        ts = client.get("/api/v1/regressions").json()["items"][0]["created_at"]
        # Either ends with Z, or already has a +HH:MM offset.
        assert ts.endswith("Z") or "+" in ts or ts.endswith("00:00")


# ── /api/v1/stale-findings ───────────────────────────────────────────────────


class TestStaleFindings:
    def test_returns_empty_with_status_marker(self, client_and_db):
        client, _, _ = client_and_db
        body = client.get("/api/v1/stale-findings").json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["scan_status"] == "no_scanner_yet"


# ── /api/v1/trust-profiles ───────────────────────────────────────────────────


class TestTrustProfiles:
    def test_empty_db_returns_empty(self, client_and_db):
        client, _, _ = client_and_db
        body = client.get("/api/v1/trust-profiles").json()
        assert body == {"items": [], "total": 0}

    def test_returns_inserted_profile_with_full_shape(self, client_and_db):
        client, db_file, _ = client_and_db
        db = Database(db_file)
        with db._locked():
            db.get_connection().execute(
                "INSERT INTO trust_profiles "
                "(repo, template_id, task_type, auto_merge_threshold, "
                "human_review_threshold, trust_score, total_runs, "
                "successful_merges, regressions, reverted_prs, last_run_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("ToscanAI/orchemist", "coding-pipeline-standard", "feature",
                 0.90, 0.70, 0.91, 42, 38, 0, 0, "2026-05-25T11:00:00Z"),
            )
            db.get_connection().commit()
        body = client.get("/api/v1/trust-profiles").json()
        assert body["total"] == 1
        p = body["items"][0]
        # Full key set the TrustProfileRecord TS interface depends on.
        expected = {
            "id", "repo", "template_id", "task_type", "auto_merge_threshold",
            "human_review_threshold", "trust_score", "total_runs",
            "successful_merges", "regressions", "reverted_prs", "last_run_at",
            "created_at", "updated_at",
        }
        assert expected.issubset(set(p.keys())), f"missing: {expected - set(p.keys())}"
        assert p["repo"] == "ToscanAI/orchemist"
        assert p["trust_score"] == 0.91

    def test_active_profiles_ordered_first(self, client_and_db):
        """Profiles with a `last_run_at` come before NULL ones (audit
        recommendation: stale profiles shouldn't crowd out active ones)."""
        client, db_file, _ = client_and_db
        db = Database(db_file)
        with db._locked():
            c = db.get_connection()
            c.execute(
                "INSERT INTO trust_profiles (repo, template_id, task_type, last_run_at) "
                "VALUES (?,?,?,?)", ("a/a", "tA", "feature", None),
            )
            c.execute(
                "INSERT INTO trust_profiles (repo, template_id, task_type, last_run_at) "
                "VALUES (?,?,?,?)", ("b/b", "tB", "feature", "2026-05-25T12:00:00Z"),
            )
            c.commit()
        items = client.get("/api/v1/trust-profiles").json()["items"]
        assert items[0]["repo"] == "b/b"  # active one first
        assert items[1]["repo"] == "a/a"


# ── /api/v1/decisions ────────────────────────────────────────────────────────


class TestDecisions:
    def test_empty_db_returns_empty_items(self, client_and_db):
        client, _, _ = client_and_db
        body = client.get("/api/v1/decisions").json()
        assert body["items"] == []
        assert body["total"] == 0

    def test_clamps_limit(self, client_and_db):
        client, _, _ = client_and_db
        assert client.get("/api/v1/decisions?limit=500").json()["limit"] == 100

    def test_returns_canonical_shape(self, client_and_db):
        """Lock the shape DecisionRecord TS interface depends on.
        Previously the frontend assumed `id` + `confidence` which don't exist."""
        client, db_file, _ = client_and_db
        db = Database(db_file)
        run_id = "run-" + uuid.uuid4().hex[:6]
        rev_id = "dec-" + uuid.uuid4().hex[:6]
        _insert_pipeline_run(db, run_id)
        _insert_review_outcome(
            db, rev_id, run_id,
            verdict="APPROVE",
            issues='[{"id": 1, "text": "looks good"}]',
        )
        body = client.get("/api/v1/decisions").json()
        assert body["total"] == 1
        d = body["items"][0]
        expected = {
            "review_id", "run_id", "phase_id", "reviewer_model",
            "verdict", "issues_found", "fix_verified", "created_at",
        }
        assert expected.issubset(set(d.keys())), f"missing: {expected - set(d.keys())}"
        # `issues_found` is deserialised — list of dicts, not strings.
        assert isinstance(d["issues_found"], list)
        assert d["issues_found"][0]["text"] == "looks good"
        # Timestamp must be Z-suffixed (or otherwise tz-aware).
        ts = d["created_at"]
        assert ts.endswith("Z") or "+" in ts or ts.endswith("00:00")

    def test_pagination_stable(self, client_and_db):
        client, db_file, _ = client_and_db
        db = Database(db_file)
        run_id = "run-" + uuid.uuid4().hex[:6]
        _insert_pipeline_run(db, run_id)
        for i in range(4):
            _insert_review_outcome(db, f"d{i:02d}-{run_id}", run_id)
        p1 = client.get("/api/v1/decisions?limit=2&offset=0").json()["items"]
        p2 = client.get("/api/v1/decisions?limit=2&offset=2").json()["items"]
        ids1 = {x["review_id"] for x in p1}
        ids2 = {x["review_id"] for x in p2}
        assert ids1.isdisjoint(ids2)


# ── /api/v1/admin/state + PUT /admin/feature-flags ──────────────────────────


class TestAdminState:
    def test_returns_defaults_when_no_file(self, client_and_db):
        client, _, _ = client_and_db
        body = client.get("/api/v1/admin/state").json()
        assert body["source"] == "default"
        assert body["autonomy_level"] == "4.3"
        assert body["feature_flags"]["phase0_hard_gate"] is False
        assert body["modes"]["openrouter"] is True
        # extra forward-compat key always present.
        assert body["extra"] == {}

    def test_round_trip_flag_update(self, client_and_db):
        client, _, fake_home = client_and_db
        res = client.put("/api/v1/admin/feature-flags", json={"phase0_hard_gate": True})
        assert res.status_code == 200
        assert res.json()["feature_flags"]["phase0_hard_gate"] is True
        admin_path = fake_home / ".orchestration-engine" / "admin.json"
        assert admin_path.exists()
        state = client.get("/api/v1/admin/state").json()
        assert state["source"] == "file"
        assert state["feature_flags"]["phase0_hard_gate"] is True

    def test_rejects_unknown_flag(self, client_and_db):
        client, _, _ = client_and_db
        res = client.put("/api/v1/admin/feature-flags", json={"made_up_flag": True})
        assert res.status_code == 400
        assert "Unknown" in res.json()["detail"]

    def test_rejects_non_dict_body(self, client_and_db):
        client, _, _ = client_and_db
        res = client.put("/api/v1/admin/feature-flags", json=["not", "a", "dict"])
        assert res.status_code == 400

    def test_accepts_canonical_string_spellings(self, client_and_db):
        client, _, _ = client_and_db
        body = client.put("/api/v1/admin/feature-flags",
                          json={"phase0_hard_gate": "true", "extend_verdict": "false"}).json()
        assert body["feature_flags"]["phase0_hard_gate"] is True
        assert body["feature_flags"]["extend_verdict"] is False

    def test_rejects_ambiguous_value(self, client_and_db):
        """Audit finding: bool('false') was True. We now reject anything
        that isn't an explicit bool, 0/1, or one of the canonical strings."""
        client, _, _ = client_and_db
        res = client.put("/api/v1/admin/feature-flags", json={"phase0_hard_gate": "maybe"})
        assert res.status_code == 400
        assert "boolean" in res.json()["detail"]

    def test_partial_file_falls_back_to_defaults(self, client_and_db):
        client, _, fake_home = client_and_db
        admin_dir = fake_home / ".orchestration-engine"
        admin_dir.mkdir(parents=True)
        (admin_dir / "admin.json").write_text(json.dumps({"autonomy_level": "5"}))
        body = client.get("/api/v1/admin/state").json()
        assert body["autonomy_level"] == "5"
        assert body["feature_flags"]["phase0_hard_gate"] is False

    def test_unreadable_file_falls_back_with_default_source(self, client_and_db):
        client, _, fake_home = client_and_db
        admin_dir = fake_home / ".orchestration-engine"
        admin_dir.mkdir(parents=True)
        (admin_dir / "admin.json").write_text("not valid json {{{")
        body = client.get("/api/v1/admin/state").json()
        assert body["source"] == "default"
        assert body["autonomy_level"] == "4.3"

    def test_non_dict_root_falls_back(self, client_and_db):
        """Audit BLOCKER #2: JSON parses but isn't a dict — must not 500."""
        client, _, fake_home = client_and_db
        admin_dir = fake_home / ".orchestration-engine"
        admin_dir.mkdir(parents=True)
        (admin_dir / "admin.json").write_text('"a string at root"')
        # GET must not 500
        state = client.get("/api/v1/admin/state").json()
        assert state["feature_flags"]["phase0_hard_gate"] is False
        # PUT must not 500 either — it should ignore the malformed file and start fresh
        res = client.put("/api/v1/admin/feature-flags", json={"phase0_hard_gate": True})
        assert res.status_code == 200

    def test_nested_feature_flags_not_dict_falls_back(self, client_and_db):
        """Audit BLOCKER #2: top-level dict but feature_flags is wrong type."""
        client, _, fake_home = client_and_db
        admin_dir = fake_home / ".orchestration-engine"
        admin_dir.mkdir(parents=True)
        (admin_dir / "admin.json").write_text(json.dumps({"feature_flags": "broken"}))
        state = client.get("/api/v1/admin/state").json()
        # Defaults kick in for the malformed inner key.
        assert isinstance(state["feature_flags"], dict)
        assert state["feature_flags"]["phase0_hard_gate"] is False
        # PUT must succeed even though feature_flags on disk was a string.
        res = client.put("/api/v1/admin/feature-flags", json={"phase0_hard_gate": True})
        assert res.status_code == 200

    def test_autonomy_level_coerced_to_str(self, client_and_db):
        """Audit MAJOR: integer or other types in admin.json must come
        back as string per the AdminState TS contract."""
        client, _, fake_home = client_and_db
        admin_dir = fake_home / ".orchestration-engine"
        admin_dir.mkdir(parents=True)
        (admin_dir / "admin.json").write_text(json.dumps({"autonomy_level": 5}))
        body = client.get("/api/v1/admin/state").json()
        assert isinstance(body["autonomy_level"], str)
        assert body["autonomy_level"] == "5"

    def test_extra_keys_preserved_in_extra_namespace(self, client_and_db):
        """Audit MAJOR: unknown top-level keys must round-trip via `extra`."""
        client, _, fake_home = client_and_db
        admin_dir = fake_home / ".orchestration-engine"
        admin_dir.mkdir(parents=True)
        (admin_dir / "admin.json").write_text(json.dumps({
            "autonomy_level": "4.3",
            "future_setting_x": "preserved",
        }))
        body = client.get("/api/v1/admin/state").json()
        assert body["extra"] == {"future_setting_x": "preserved"}

    @pytest.mark.asyncio
    async def test_two_simultaneous_puts_no_loss(self, tmp_path, monkeypatch):
        """Round-3 audit BLOCKER: the round-2 'concurrent PUT' test was
        trivial-satisfaction because the handler has zero `await` points
        inside its critical section — asyncio can't preempt mid-handler
        without an await, so two `asyncio.gather`'d PUTs ran sequentially
        whether the lock was present or not. The lock was deleted.

        What we actually need to guarantee: when two PUTs target distinct
        flags, both patches land and the on-disk file is well-formed
        regardless of interleaving. `os.replace` atomicity + the single
        event loop's serialisation of await-free handlers already gives us
        this property; verify with real asyncio.gather concurrency.
        """
        import asyncio as _asyncio
        from httpx import AsyncClient, ASGITransport
        fake_home = tmp_path / "home"; fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        db_file = tmp_path / "engine.db"
        app = create_api_app(db_path=db_file)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            results = await _asyncio.gather(
                ac.put("/api/v1/admin/feature-flags", json={"phase0_hard_gate": True}),
                ac.put("/api/v1/admin/feature-flags", json={"extend_verdict": False}),
                ac.put("/api/v1/admin/feature-flags", json={"cross_repo": True}),
            )
            for r in results:
                assert r.status_code == 200
            state = (await ac.get("/api/v1/admin/state")).json()
        # All three patches must land — proves the natural serialisation works.
        assert state["feature_flags"]["phase0_hard_gate"] is True
        assert state["feature_flags"]["extend_verdict"] is False
        assert state["feature_flags"]["cross_repo"] is True
        # And the on-disk file must be valid JSON (os.replace atomicity).
        admin_path = fake_home / ".orchestration-engine" / "admin.json"
        assert admin_path.exists()
        json.loads(admin_path.read_text())  # raises if mid-write corruption

    def test_put_response_returns_canonical_shape(self, client_and_db):
        """Round-2 audit MAJOR: PUT response previously returned only the
        keys present on disk (incomplete) and could leak non-bool values
        from a hand-edited file. The response now matches
        AdminState['feature_flags'] exactly — all 4 keys, all booleans."""
        client, _, _ = client_and_db
        body = client.put("/api/v1/admin/feature-flags",
                          json={"phase0_hard_gate": True}).json()
        expected_keys = {"phase0_hard_gate", "extend_verdict", "dialogue_phase", "cross_repo"}
        assert set(body["feature_flags"].keys()) == expected_keys
        # Every value is a real Python bool, not a string.
        for k, v in body["feature_flags"].items():
            assert isinstance(v, bool), f"{k}={v!r} is {type(v).__name__}, expected bool"

    def test_put_canonicalises_disk_when_prior_garbage(self, client_and_db):
        """Round-3 audit MAJOR: PUT used to canonicalise the response but
        leave the disk file dirty. A subsequent reader of admin.json (CLI,
        daemon, another tool) saw `"maybe"` while the API claimed
        `False`. Now the on-disk feature_flags are also normalised on every
        PUT so disk and response agree."""
        client, _, fake_home = client_and_db
        admin_dir = fake_home / ".orchestration-engine"
        admin_dir.mkdir(parents=True)
        (admin_dir / "admin.json").write_text(json.dumps({
            "feature_flags": {"phase0_hard_gate": "maybe", "extend_verdict": "no_idea"},
        }))
        res = client.put("/api/v1/admin/feature-flags", json={"dialogue_phase": True})
        assert res.status_code == 200
        on_disk = json.loads((admin_dir / "admin.json").read_text())
        # Disk and API both report canonical bools for ALL flags.
        for k, v in on_disk["feature_flags"].items():
            assert isinstance(v, bool), f"disk still has non-bool {k}={v!r}"
        # Canonicalised back to per-flag defaults (False/True/False/False per
        # _ADMIN_DEFAULTS). The patch (dialogue_phase=True) lands.
        assert on_disk["feature_flags"]["phase0_hard_gate"] is False  # was "maybe" → default False
        assert on_disk["feature_flags"]["extend_verdict"] is True     # was "no_idea" → default True
        assert on_disk["feature_flags"]["dialogue_phase"] is True     # the patch
        assert on_disk["feature_flags"]["cross_repo"] is False        # untouched → default False

    def test_coerce_admin_doc_does_not_share_default_mutables(self):
        """Round-4 audit MAJOR: the round-3 lock-in test was
        trivial-satisfaction. TestClient JSON-serialises responses, so
        client-side mutation cannot reach the server-side dict the bug
        actually corrupted. The real bug is in the in-process helper; the
        test must touch it directly.

        Now imports `_coerce_admin_doc` from module scope and calls it
        twice in-process. Mutating the first result must not affect the
        second — which only holds if every call constructs the inner dicts
        fresh (as the round-3 fix did).
        """
        # Fast-path: not-a-dict input.
        r1 = _coerce_admin_doc(None)
        r1["feature_flags"]["phase0_hard_gate"] = True
        r2 = _coerce_admin_doc(None)
        assert r2["feature_flags"]["phase0_hard_gate"] is False, (
            "_coerce_admin_doc not-a-dict path aliases module-level _ADMIN_DEFAULTS"
        )
        # Module-level default must still be intact.
        assert _ADMIN_DEFAULTS["feature_flags"]["phase0_hard_gate"] is False
        # Slow-path: well-formed input.
        r3 = _coerce_admin_doc({"feature_flags": {"phase0_hard_gate": True}})
        r3["modes"]["openrouter"] = False
        r4 = _coerce_admin_doc({"feature_flags": {"phase0_hard_gate": True}})
        assert r4["modes"]["openrouter"] is True, (
            "_coerce_admin_doc slow-path aliases module-level _ADMIN_DEFAULTS modes"
        )
        assert _ADMIN_DEFAULTS["modes"]["openrouter"] is True

    def test_unknown_nested_feature_flag_preserved_through_put(self, client_and_db):
        """Round-4 audit MAJOR: round-3's pre-write canonicalisation
        silently dropped any flag inside ``feature_flags`` that wasn't in
        ``_ADMIN_KNOWN_FLAGS``. A forward-compat operator (or beta build)
        that put `"experimental_speculation": True` on disk would lose it
        on every PUT.

        Now ``_merge_feature_flags_with_passthrough`` preserves unknown
        nested keys verbatim, mirroring the `extra` top-level behaviour.
        """
        client, _, fake_home = client_and_db
        admin_dir = fake_home / ".orchestration-engine"
        admin_dir.mkdir(parents=True)
        (admin_dir / "admin.json").write_text(json.dumps({
            "feature_flags": {
                "phase0_hard_gate": True,
                "experimental_speculation": True,
            },
        }))
        res = client.put("/api/v1/admin/feature-flags", json={"cross_repo": True})
        assert res.status_code == 200
        # Response only exposes known flags (the AdminState TS contract).
        assert "experimental_speculation" not in res.json()["feature_flags"]
        assert res.json()["feature_flags"]["cross_repo"] is True
        # On-disk file MUST preserve the unknown flag for forward-compat.
        on_disk = json.loads((admin_dir / "admin.json").read_text())
        assert on_disk["feature_flags"]["experimental_speculation"] is True, (
            "unknown nested flag was silently dropped by canonicalisation"
        )

    def test_merge_with_passthrough_helper_direct(self):
        """Lock-in for ``_merge_feature_flags_with_passthrough`` semantics:
        unknown keys verbatim, known keys canonicalised to bool, conflicts
        resolved in favour of canonical (operator-edited junk doesn't
        shadow engine-managed flags)."""
        out = _merge_feature_flags_with_passthrough({
            "phase0_hard_gate": "maybe",        # known, garbage → default
            "extend_verdict": "yes",            # known, canonical truthy
            "experimental_speculation": True,   # unknown, preserved
            "weird_passthrough_value": [1, 2],  # unknown, preserved verbatim
        })
        assert out["phase0_hard_gate"] is False   # default (round-2 strictness)
        assert out["extend_verdict"] is True      # "yes" → True
        assert out["experimental_speculation"] is True
        assert out["weird_passthrough_value"] == [1, 2]
        # Non-dict input still returns canonical-only (no crash).
        out2 = _merge_feature_flags_with_passthrough("not a dict")  # type: ignore[arg-type]
        assert set(out2.keys()) == {"phase0_hard_gate", "extend_verdict", "dialogue_phase", "cross_repo"}

    def test_strict_coerce_bool_direct(self):
        """Direct unit tests for the module-scope helper, replacing what
        used to need an HTTP round-trip."""
        assert _strict_coerce_bool(True) is True
        assert _strict_coerce_bool(False) is False
        assert _strict_coerce_bool(0) is False
        assert _strict_coerce_bool(1) is True
        assert _strict_coerce_bool(0.0) is False
        assert _strict_coerce_bool(1.0) is True
        assert _strict_coerce_bool("True") is True
        assert _strict_coerce_bool("FALSE") is False
        assert _strict_coerce_bool("yes") is True
        assert _strict_coerce_bool("no") is False
        assert _strict_coerce_bool("") is False
        # Anything else → None.
        assert _strict_coerce_bool("maybe") is None
        assert _strict_coerce_bool(2) is None
        assert _strict_coerce_bool(0.5) is None
        assert _strict_coerce_bool(None) is None
        assert _strict_coerce_bool([1, 2]) is None
        assert _strict_coerce_bool({"a": "b"}) is None

    def test_get_strict_coerces_garbage_in_disk_file(self, client_and_db):
        """Round-2 audit MAJOR: `_coerce_bool` previously fell through to
        `bool(value)` for unrecognised inputs — `bool("maybe")` = True.
        The new `_strict_coerce_bool` returns None on unrecognised input,
        and `_coerce_admin_doc` substitutes the per-flag default."""
        client, _, fake_home = client_and_db
        admin_dir = fake_home / ".orchestration-engine"
        admin_dir.mkdir(parents=True)
        # 'maybe' is not a canonical bool string; should fall back to the
        # default for phase0_hard_gate (False), NOT silently become True.
        (admin_dir / "admin.json").write_text(json.dumps({
            "feature_flags": {"phase0_hard_gate": "maybe", "extend_verdict": "yes"},
        }))
        body = client.get("/api/v1/admin/state").json()
        assert body["feature_flags"]["phase0_hard_gate"] is False  # ← default, NOT bool("maybe")
        assert body["feature_flags"]["extend_verdict"] is True     # ← "yes" → True (canonical)
