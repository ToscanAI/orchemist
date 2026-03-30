# CI Failure Investigation — Run #23286850209

**Repo:** `ToscanAI/orchestration-engine` | **Branch:** `main` | **Date:** 2026-03-19  
**Result:** ❌ Python 3.10 and 3.12 both fail (3.11 not in matrix)

---

## Executive Summary

The CI run has **5 independent failure categories** affecting **64 unique test cases** (180 total failures across 2 Python versions). The root causes are: (1) `fastapi` is an optional `[web]` dependency but test files import it unconditionally — CI only installs `[dev]`, (2) `api.py` line 629-631 still has the f-string backslash pattern that commit `c2dc447` fixed in `cli.py` but missed in `api.py`, (3) the `--repo` Click option reads `envvar='GITHUB_REPOSITORY'` which is set in GitHub Actions, causing `_fetch_issue_strict()` to run before `_infer_git_context()` error messages are reached, (4) Click 8.3.1 (installed in CI) removed `mix_stderr` from `CliRunner`, and (5) the f-string issue in `api.py` is **only visible on Python 3.10/3.11** (pre-PEP 701). These are all independent bugs.

---

## Category 1: `ModuleNotFoundError: No module named 'fastapi'`

### Affected Tests (40 tests, 4 files)
- `test_issue_automation_e2e.py` — 19 FAILED
- `test_issue_webhook.py` — 15 FAILED
- `test_issue_result_posting.py` — 2 FAILED
- `test_issue_label_trigger.py` — 10 ERRORS

### Root Cause
`fastapi` is declared as an **optional dependency** under `[project.optional-dependencies.web]` in `pyproject.toml`. The CI workflow installs only `pip install -e ".[dev]"` — it does **not** install `.[web]`.

The test files do hard `from fastapi.testclient import TestClient` imports at the top level without any `pytest.importorskip()` guard. When fastapi isn't installed, every test in those files fails at import time.

Note: `starlette` *is* transitively installed (via `sse-starlette` → `mcp`), but `fastapi` itself is not.

`test_github_app.py` uses `pytest.importorskip("starlette.testclient")` correctly but then calls `from orchestration_engine.web.api import create_api_app` at test time, which triggers the `api.py` SyntaxError (see Category 2).

### File:Line
- `pyproject.toml` line ~44: `web = ["fastapi>=0.100.0", ...]` (optional, not in `dev`)
- `.github/workflows/ci.yml` line 27: `pip install -e ".[dev]"` (missing `.[web]`)

### Introduced By
Pre-existing since the web/webhook tests were added (commits `609438b`, `7617d47`, `5473f66`). The tests have always required fastapi but CI never installed it — they may have been passing when `mcp` or another transitive dep pulled in fastapi, and broke when that transitive path changed.

---

## Category 2: `SyntaxError` in `api.py` — f-string backslash (Python 3.10/3.11 only)

### Affected Tests (4 tests, 1 file — **on Python 3.10 only**)
- `test_github_app.py::TestHandleGithubIssuesSignatureVerification` — 4 FAILED

### Root Cause
`src/orchestration_engine/web/api.py` lines 629-630:
```python
f"./output/{re.sub(r'[^\w\-]', '_', template.id)}"
f"-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{run_id}"
```
This uses `\w` and `\-` inside an f-string expression — a `SyntaxError` on Python 3.10/3.11 (PEP 701 only permits this in 3.12+).

Commit `c2dc447` ("fix: rewrite f-string backslash expressions for Python 3.10/3.11 compatibility") fixed the **identical pattern** in `cli.py` but **missed the same pattern in `api.py`**. The commit message even says "Two locations fixed" — but both were in `cli.py`.

On Python 3.12, this parses fine, so the 4 tests in `test_github_app.py` fail for a *different* reason there (fastapi missing → Category 1).

### File:Line
- `src/orchestration_engine/web/api.py` lines 629-631

### Introduced By
The pattern existed since `api.py` got the `_launch_from_api()` function. Commit `c2dc447` was supposed to fix all occurrences but missed this file.

---

## Category 3: CLI ordering — `_fetch_issue_strict` runs before git-repo error messages

### Affected Tests (2 tests, 1 file)
- `test_cli_launch_shorthand.py::TestGitAutoInference::test_not_in_git_repo_without_repo_flag_exits_1`
- `test_cli_launch_shorthand.py::TestGitAutoInference::test_inside_git_no_origin_shows_distinct_error`

### Root Cause
The `--repo` Click option has `envvar='GITHUB_REPOSITORY'`:
```python
@click.option('--repo', default=None, envvar='GITHUB_REPOSITORY', ...)
```

In GitHub Actions, `GITHUB_REPOSITORY` is **always set** (to `ToscanAI/orchestration-engine`). This means `repo` is never `None` in CI, so `effective_repo` is populated from the env var, and the code **never reaches** the `if not effective_repo:` branch that produces "Not inside a git repository" or "Cannot determine GitHub repository" errors.

Instead, execution falls through to `_fetch_issue_strict(effective_repo, issue_number)`, which checks for a GitHub token, fails (no token in test env), and exits with "Error: No GitHub token found."

The tests mock `_infer_git_context` but don't mock `_fetch_issue_strict` and don't unset `GITHUB_REPOSITORY`, because they assume `repo` will be `None`. The tests pass locally (no `GITHUB_REPOSITORY` env var) but fail in CI.

### File:Line
- `src/orchestration_engine/cli.py` line 1758-1760: `envvar='GITHUB_REPOSITORY'`
- `tests/test_cli_launch_shorthand.py` lines 284-312: missing `monkeypatch.delenv('GITHUB_REPOSITORY', raising=False)`

### Introduced By
Commit `e47b178` ("Pipeline result for #591") which added both the `--repo` envvar binding and these tests, but didn't account for CI having `GITHUB_REPOSITORY` set.

---

## Category 4: `mix_stderr` removed in Click 8.3.x (NEW — not in original evidence)

### Affected Tests (18 tests, 3 files)
- `test_rubric_generator_comprehensive.py::TestCliDeep` — 15 FAILED
- `test_post_pipeline_scoring.py::TestScoreOnlyFlag` — 2 FAILED
- `test_template_validation_suite_extended.py::TestOrchTemplatesTestSmoke` — 1 FAILED

### Root Cause
Click 8.3.1 (installed in CI) removed the `mix_stderr` parameter from `CliRunner.__init__()`. The tests use `CliRunner(mix_stderr=False)` or `CliRunner(mix_stderr=True)`, which raises `TypeError`.

`pyproject.toml` specifies `click>=8.0.0` — no upper bound. CI pulls the latest (8.3.1).

### File:Line
- `tests/test_rubric_generator_comprehensive.py` line 1266: `CliRunner(mix_stderr=False)`
- `tests/test_post_pipeline_scoring.py` line 448: `CliRunner(mix_stderr=True)`

### Introduced By
External — Click 8.3.0/8.3.1 release removed the deprecated `mix_stderr` parameter. The tests were written against Click 8.1.x.

---

## Blast Radius

| Category | Unique Tests | Files | Python 3.10 | Python 3.12 |
|----------|-------------|-------|-------------|-------------|
| 1. fastapi missing | 40 | 4 | ❌ | ❌ |
| 2. api.py SyntaxError | 4 | 1 | ❌ | ✅ (masked by #1) |
| 3. CLI env var ordering | 2 | 1 | ❌ | ❌ |
| 4. mix_stderr Click 8.3 | 18 | 3 | ❌ | ❌ |
| **Total** | **64** | **8** | | |

Total failure count: 64 unique tests × ~3 Python versions = ~180 FAILED lines in log (some overlap where Cat 2 and Cat 1 hit the same test on 3.10).

---

## Are They Related?

**No.** These are 4 fully independent bugs:
1. **fastapi** — CI install config gap (never included `[web]`)
2. **api.py SyntaxError** — missed file in commit `c2dc447`'s fix
3. **CLI env var** — test environment assumption (`GITHUB_REPOSITORY` not unset)
4. **mix_stderr** — Click 8.3 breaking change + unbounded dependency

---

## Recommended Fix Approach

1. **fastapi missing:** Change CI install to `pip install -e ".[dev,web,github]"` — or add `fastapi` and `starlette` to `[dev]` dependencies. Also add `pytest.importorskip("fastapi")` guards to the 4 test files for robustness.

2. **api.py SyntaxError:** Apply the same fix pattern as `c2dc447` — extract `re.sub()` and `strftime()` into local variables before the f-string on line 629-630 of `api.py`.

3. **CLI env var ordering:** Add `monkeypatch.delenv('GITHUB_REPOSITORY', raising=False)` to the two failing tests, so they don't accidentally pick up CI's env var.

4. **mix_stderr:** Either pin `click<8.3` in `pyproject.toml`, or remove `mix_stderr` from the 3 test files (it's the default behavior in Click 8.3+ anyway — stderr is always mixed).
