# Forensic Finding 05 — License & Metadata Inconsistencies

> **Severity:** 🔴 Critical  
> **Impact:** Conflicting license claims and repository URLs will cause problems at open-source release  
> **Generated:** 2026-03-13

---

## Summary

There are three categories of metadata inconsistency: license type, repository URL/ownership, and project naming.

---

## Finding 5.1: License Conflict — MIT vs Apache 2.0

Three different sources claim three different things:

| Source | License Claimed |
|--------|----------------|
| `LICENSE` file | **MIT License** — "Copyright (c) 2026 Conny Lazo" |
| `pyproject.toml` | `license = {text = "MIT"}` |
| `README.md` badge | `[![License: MIT](...)` |
| `ROADMAP.md` | **"License: Apache 2.0"** |

**The conflict:** The actual `LICENSE` file, `pyproject.toml`, and README all say **MIT**. The ROADMAP says **Apache 2.0**. These are fundamentally different licenses:
- MIT: permissive, minimal restrictions
- Apache 2.0: permissive but includes explicit patent grant and contributor license terms

**Impact:** If the intent is Apache 2.0 (as ROADMAP suggests for the open-source release), then the LICENSE file, pyproject.toml, and README are all wrong. If the intent is MIT, the ROADMAP is wrong.

**Recommendation:** Decide on a license **before** the open-source release targeted for May 2026. Update all four locations to match. If Apache 2.0 is chosen, replace the LICENSE file contents, update pyproject.toml to `"Apache-2.0"`, and update the README badge.

---

## Finding 5.2: Repository URL Conflict — ToscanAI vs connylazo

| Source | Repository URL |
|--------|----------------|
| `README.md` badges | `https://github.com/ToscanAI/orchestration-engine` |
| `ROADMAP.md` | `ToscanAI/orchemist-v2` (for Go rewrite) |
| `pyproject.toml` [project.urls] | `https://github.com/connylazo/orchestration-engine` |
| `GETTING_STARTED.md` clone URL | `https://github.com/ToscanAI/orchestration-engine.git` |
| `coding-pipeline-v1.yaml` | `repo_url` field references `https://github.com/ToscanAI/orchestration-engine` |

**The conflict:** `pyproject.toml` points to `github.com/connylazo/orchestration-engine` while every other reference points to `github.com/ToscanAI/orchestration-engine`.

**Impact:** `pip install` from PyPI will show the `connylazo` URL in package metadata. Users following that link may find a non-existent repo if the canonical repo is under `ToscanAI`.

**Recommendation:** Unify all URLs to the canonical repository location. Update `pyproject.toml` [project.urls] to use the same org/user as all other references.

---

## Finding 5.3: Project Name Inconsistency — "Orchestration Engine" vs "Orchemist"

| Source | Name Used |
|--------|-----------|
| `pyproject.toml` | `orchestration-engine` |
| `README.md` | "Orchestration Engine" |
| `ROADMAP.md` title | "Orchemist Roadmap" |
| `ROADMAP.md` body | "Orchemist's issue watcher", "Orchemist detects the regression" |
| `ROADMAP.md` Level 5 | "$ORCHEMIST_TEST_STORE", "orch factory status", "orch migrate-validation" |
| CLI prefix | `orch` |

**The conflict:** The project packages as `orchestration-engine` and the CLI is `orch`, but the ROADMAP exclusively uses "Orchemist" as the product name. The README never mentions "Orchemist."

**Impact:** Naming confusion. Is it `orch`, `orchemist`, or `orchestration-engine`? Contributors reading the roadmap will search for "Orchemist" references in the code and find none.

**Recommendation:** Choose one name and use it consistently. If "Orchemist" is the brand name for the open-source release, update README and PyPI package name. If "Orchestration Engine" is the name, update the ROADMAP.

---

## Finding 5.4: Maintainer Attribution

| Source | Attribution |
|--------|-------------|
| `ROADMAP.md` | `Maintainer: @ToscanAI` |
| `pyproject.toml` | `authors = [{name = "Conny Lazo", email = "contact@renerivera.net"}]` |
| `LICENSE` | "Copyright (c) 2026 Conny Lazo" |
| `coding-pipeline-v1.yaml` | `author: "Toscan"` |

This is minor but worth noting: the author name varies between "Conny Lazo", "@ToscanAI", and "Toscan" across different files. For open-source release, consider standardizing.

---

## Finding 5.5: Version Discrepancy

| Source | Version |
|--------|---------|
| `pyproject.toml` | `0.3.0` |
| `frontend/package.json` | `0.1.0` |
| `coding-pipeline-v1.yaml` | `1.6.0` (template version, different scope) |

The frontend version `0.1.0` and engine version `0.3.0` are not synchronized. This is not inherently wrong (frontend and backend can version independently), but if the intent is a unified release version, they should match.

**Recommendation:** Decide if frontend and engine share a version number. If yes, synchronize them.

---

## Pre-Release Checklist

Before the open-source release (ROADMAP targets ~May 2026):

- [ ] Resolve MIT vs Apache 2.0 across all 4 locations
- [ ] Unify repository URLs to one canonical location
- [ ] Choose canonical project name (Orchestration Engine vs Orchemist)
- [ ] Standardize author/maintainer references
- [ ] Decide on version synchronization strategy
- [ ] Verify all badge URLs point to the correct repo
- [ ] Update GETTING_STARTED.md clone URL to match pyproject.toml
