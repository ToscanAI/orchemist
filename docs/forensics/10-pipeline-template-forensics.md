# Pipeline Template Forensics (v3)

**Date:** 2026-03-13 — updated after remediation via new versioned templates
**Scope:** All 20 YAML pipeline templates (9 bundled in `templates/`, 11 in `examples/`)
**Method:** Cross-reference every template field, phase configuration, and prompt placeholder against the template schema (`PhaseDefinition`, `PipelineTemplate` dataclasses in `templates.py`) and the sequencer's interpolation engine (`sequencer.py`).

**Deleted since v1 audit:** `content-pipeline.yaml` (v2.4), `content-pipeline-v25.yaml`, `content-pipeline-v26.yaml`, `content-pipeline-gemini-v2.3.1.yaml` — these carried Findings 1, 2, 6, 8, and 10 from the v1 report and are now resolved by removal.

**Remediated via new versioned templates (2026-03-13):**
- Finding 1 → `templates/research-competitive-v2.yaml` (bare `{var}` → `{config[var]}`)
- Finding 2 → `examples/code-review-pipeline-v2.yaml` (ghost `scenario:` line removed)
- Finding 3 → `templates/content-pipeline-v28.yaml` (hyphenated phase IDs → underscores + file refs)
- Finding 4 → `examples/content-pipeline-v3.yaml` (hyphenated phase IDs → underscores + `previous_output` refs)

Original templates are preserved for reference. New versions should be used for all future runs.

---

## 1. Inventory

### Bundled Templates (`templates/`)

| File | ID | Category | Phases | Version | Scenario | Status |
|---|---|---|---|---|---|---|
| `coding-pipeline-v1.yaml` | `coding-pipeline-v1` | code | 8 | 1.6.0 | `scenarios/coding-pipeline-v1-smoke.yaml` | OK |
| `content-pipeline-v27.yaml` | `content-pipeline-v27` | content | 7 | 2.7.0 | — | **SUPERSEDED** by v28 |
| **`content-pipeline-v28.yaml`** | `content-pipeline-v28` | content | 7 | 2.8.0 | — | **NEW** — Finding 3 fix |
| `editorial-rewrite.yaml` | `editorial_rewrite` | content | 8 | 1.0.0 | — | OK |
| `research-competitive.yaml` | `research-competitive` | research | 3 | 1.0.0 | — | **SUPERSEDED** by v2 |
| **`research-competitive-v2.yaml`** | `research-competitive-v2` | research | 3 | 2.0.0 | — | **NEW** — Finding 1 fix |
| `sprint-runner-v1.yaml` | `sprint-runner-v1` | code | 5 | 1.1.0 | `scenarios/sprint-runner-v1-smoke.yaml` | OK |
| `sprint-runner-step-v1.yaml` | `sprint-runner-step-v1` | code | 5 | 1.1.0 | `scenarios/sprint-runner-step-v1-smoke.yaml` | OK |
| `greenfield-project-v1.yaml` | `greenfield-project-v1` | content | 4 | 1.0.0 | — | OK |

### Example Templates (`examples/`)

| File | ID | Category | Phases | Scenario | Status |
|---|---|---|---|---|---|
| `hello-pipeline.yaml` | `hello-pipeline` | example | 2 | — | OK |
| `linear-transitions.yaml` | `linear-transitions` | example | 3 | — | OK |
| `code-development-pipeline.yaml` | `code-development-pipeline` | code | 5 | — | OK |
| `code-review-pipeline.yaml` | `code-review-pipeline` | code | 5 | `scenarios/code-review-pipeline-scenario.yaml` (**MISSING**) | **SUPERSEDED** by v2 |
| **`code-review-pipeline-v2.yaml`** | `code-review-pipeline-v2` | code | 5 | — | **NEW** — Finding 2 fix |
| `content-pipeline-v2.yaml` | `content-pipeline-v2` | content | 7 | — | **SUPERSEDED** by v3 |
| **`content-pipeline-v3.yaml`** | `content-pipeline-v3` | content | 7 | — | **NEW** — Finding 4 fix |
| `docs-pipeline.yaml` | `docs-pipeline` | content | 3 | — | OK |
| `research-pipeline.yaml` | `research-pipeline` | research | 6 | — | OK |
| `security-audit-pipeline.yaml` | `security-audit-pipeline` | analysis | 3 | — | OK |
| `ux-audit-pipeline.yaml` | `ux-audit-pipeline` | analysis | 3 | — | OK |

---

## 2. Findings

### Finding 1 — ~~CRITICAL~~ RESOLVED: `research-competitive.yaml` uses bare placeholders

**Status:** RESOLVED (2026-03-13) — Fixed in `templates/research-competitive-v2.yaml`
**Affected template:** `templates/research-competitive.yaml`
**Severity:** CRITICAL — prompts are returned raw (uninterpolated) at runtime

The `market_scan` phase prompt uses bare `{research_topic}`, `{competitors}`, `{additional_research}`. The `competitive_analysis` phase uses `{our_product}`, `{focus_areas}`. None of these are wrapped in `{config[...]}` or `{input[...]}`.

The sequencer's `format()` call passes only named kwargs: `input`, `config`, `previous_output`, `context`, `output_dir`, `phase_summary`, plus `**phase_kwargs` (keyed by phase IDs like `market_scan`). Bare names like `{research_topic}` match **nothing** — Python raises `KeyError`, caught by the sequencer, which then returns the **entire raw template uninterpolated**:

```python
except (KeyError, IndexError, AttributeError) as exc:
    logger.warning(f"Phase '{phase.id}': format error in prompt template — {exc}. Returning raw template.")
    prompt = phase.prompt_template
```

**Impact:** The LLM receives the literal template text with `{research_topic}` etc. visible. It may attempt to interpret the placeholders, but no actual user input reaches the prompt. The template is functionally **non-operational**.

**Fix:** Replace all bare placeholders with `{config[key]}` syntax:
- `{research_topic}` → `{config[research_topic]}`
- `{competitors}` → `{config[competitors]}`
- `{additional_research}` → `{config[additional_research]}`
- `{our_product}` → `{config[our_product]}`
- `{focus_areas}` → `{config[focus_areas]}`

**Resolution:** All 5 bare placeholders replaced with `{config[key]}` syntax in `templates/research-competitive-v2.yaml`. Original file preserved.

---

### Finding 2 — ~~HIGH~~ RESOLVED: Ghost scenario reference

**Status:** RESOLVED (2026-03-13) — Fixed in `examples/code-review-pipeline-v2.yaml`
**Affected template:** `examples/code-review-pipeline.yaml`
**Severity:** HIGH — scoring fails on run

The template declares `scenario: "scenarios/code-review-pipeline-scenario.yaml"` (line 19). This file **does not exist** in the workspace. When `orch run` is invoked with auto-scoring, it will fail trying to load the scenario file.

The comment says `# Issue #295: required for code pipelines`, but the file was never created.

Existing scenarios in `scenarios/`:
```
coding-pipeline-v1-smoke.yaml   ✓
sprint-runner-v1-smoke.yaml     ✓
sprint-runner-step-v1-smoke.yaml ✓
code-development-smoke.yaml     ✓
docs-pipeline-smoke.yaml        ✓
security-audit-smoke.yaml       ✓
ux-audit-smoke.yaml             ✓
e2e-autonomous.yaml             ✓
e2e-test-template.yaml          ✓
code-review-pipeline-scenario.yaml  ✗ MISSING
```

**Fix:** Either create the missing scenario file, or remove the `scenario:` line (since the template is in `examples/`, not `templates/`, #295 enforcement may not apply).

**Resolution:** Ghost `scenario:` line removed in `examples/code-review-pipeline-v2.yaml`. Original file preserved.

---

### Finding 3 — ~~HIGH~~ RESOLVED: Hyphenated phase IDs in `content-pipeline-v27.yaml`

**Status:** RESOLVED (2026-03-13) — Fixed in `templates/content-pipeline-v28.yaml`
**Affected template:** `templates/content-pipeline-v27.yaml` (the only surviving content pipeline)
**Severity:** HIGH — now elevated because this is the sole content pipeline

Phase IDs with hyphens: `fact-check`, `red-team`, `apply-fixes`, `voice-check`, `final-polish`.

The CHANGELOG documents: *"Phase IDs must use underscores — hyphens break `str.format` interpolation."* The sequencer has workarounds:
- `phase_kwargs` keys: `safe_pid = pid.replace("-", "_")`  
- Disc lookup: `{output_dir}/fact_check.md` (underscore form)

But the v2.7 template's prompts instruct agents to write to `{output_dir}/fact-check.md` (hyphen form matching the phase ID). The disc lookup looks for `fact_check.md`. If the agent follows the template instruction, the disc lookup **misses the file**, and subsequent phases get truncated in-memory output instead of the full on-disc version.

Example from the `fact-check` phase:
```yaml
2. Write your complete output to: {output_dir}/fact-check.md
```

The sequencer's disc lookup:
```python
disk_path = Path(self.output_dir) / f"{safe_pid}.md"  # "fact_check.md"
```

**Impact:** Token-expensive inline fallback instead of efficient file-path handoff — the core innovation of v2.7 is partially undermined.

**Fix:** Rename all hyphenated phase IDs to underscores (`fact_check`, `red_team`, `apply_fixes`, `voice_check`, `final_polish`) and update all `{output_dir}/` file references in the prompts to match.

**Resolution:** All 5 phase IDs renamed to underscores, all `{output_dir}/` file references and `depends_on` lists updated in `templates/content-pipeline-v28.yaml`. Original v27 preserved.

---

### Finding 4 — ~~MEDIUM~~ RESOLVED: Hyphenated phase IDs in `examples/content-pipeline-v2.yaml`

**Status:** RESOLVED (2026-03-13) — Fixed in `examples/content-pipeline-v3.yaml`
**Affected template:** `examples/content-pipeline-v2.yaml`
**Severity:** MEDIUM — example template, same hyphen issue

Phase IDs: `flow-review`, `red-team`, `apply-fixes`, `final-review`. Same workaround-dependent behavior as Finding 3, but since this is an example template using inline `{previous_output[...]}` rather than file-handoff, the practical impact is lower.

**Fix:** Rename to underscores for consistency: `flow_review`, `red_team`, `apply_fixes`, `final_review`.

**Resolution:** All 4 phase IDs renamed to underscores, all `{previous_output[...]}` references and `depends_on` lists updated in `examples/content-pipeline-v3.yaml`. Original v2 preserved.

---

### Finding 5 — MEDIUM: `{phase_id.output}` vs `{previous_output[phase_id]}` dual syntax

**Affected templates:** `editorial-rewrite.yaml`, `research-competitive.yaml`, `code-development-pipeline.yaml` vs. `hello-pipeline.yaml`, `linear-transitions.yaml`, `content-pipeline-v2.yaml`, `code-review-pipeline.yaml`, `research-pipeline.yaml`
**Severity:** MEDIUM — both work, but inconsistency creates confusion

Two interpolation syntaxes for referencing prior phase outputs:

| Syntax | Mechanism | Used by (surviving templates) |
|---|---|---|
| `{previous_output[research]}` | `_PreviousOutputProxy.__getitem__` | hello-pipeline, linear-transitions, content-pipeline-v2, code-review-pipeline, research-pipeline |
| `{research.output}` | `_PhaseOutput.__getattr__` | editorial-rewrite, research-competitive, code-development-pipeline |
| `{output_dir}/research.md` (file-handoff) | Agent reads from disk | content-pipeline-v27, coding-pipeline-v1, sprint-runner-v1/step-v1, greenfield-project-v1 |

All three patterns are valid. The most modern templates (v2.7, coding) use file-handoff. The older inline approach works in both syntaxes.

**Recommendation:** Document all three syntaxes in the template authoring guide and recommend file-handoff for new templates, `{previous_output[id]}` for simple inline cases.

---

### Finding 6 — MEDIUM: No `scenario` on any content/research template

**Affected templates:** `content-pipeline-v27.yaml`, `editorial-rewrite.yaml`, `research-competitive.yaml`, `greenfield-project-v1.yaml`
**Severity:** MEDIUM — no automated quality gate for content pipelines

The `scenario:` field (Issue #172, #295) enables post-pipeline auto-scoring. All 3 coding templates declare scenarios. **Zero content or research templates do.**

Content pipelines complete without any automated quality check. The `scenarios/content-pipeline/` directory exists but contains no scenario files for v2.7.

**Recommendation:** Create a scenario YAML for `content-pipeline-v27` at minimum. A basic scenario could check: article word count ≥ 80% of target, Sources section present, no `<MISSING:` placeholders in output.

---

### Finding 7 — MEDIUM: `thinking_level` omitted in several templates

**Affected templates:** `research-competitive.yaml` (all 3 phases), `editorial-rewrite.yaml` (all 8 phases), `greenfield-project-v1.yaml` (2 of 4 phases)
**Severity:** LOW — defaults to `"low"`, which is acceptable

The `PhaseDefinition` default is `thinking_level: "low"`. Omitting it is not a bug, but authors may not realize the default versus explicitly choosing `off`, `medium`, or `high`.

The `greenfield-project-v1` template explicitly sets `thinking_level: high` on `research` and `architecture` phases but omits it on `scaffold` and `issues` — so those run at `low`, which is intentional (scaffold is mechanical, issues is generation).

**Recommendation:** No fix needed, but document the default in the template authoring guide.

---

### Finding 8 — MEDIUM: `output_schema` declared but never consumed

**Affected template:** `examples/docs-pipeline.yaml` (review phase)
**Severity:** LOW — dead config, no runtime effect

The `review` phase declares:
```yaml
output_schema:
  type: object
  properties:
    result:
      type: string
```

The engine does not validate or enforce `output_schema` at runtime. This field exists on `PhaseDefinition` as a passthrough, possibly intended for future structured output parsing (Issue #188). Currently, it has no effect and may mislead template authors.

---

### Finding 9 — LOW: Sprint runner uses Opus for spec, coding-pipeline uses Sonnet

**Affected templates:** `sprint-runner-v1.yaml`, `sprint-runner-step-v1.yaml` vs. `coding-pipeline-v1.yaml`
**Severity:** LOW — design difference, not a bug

| Phase | coding-pipeline-v1 | sprint-runner-v1/step-v1 |
|---|---|---|
| spec | `sonnet` + `thinking: high` | `opus` + `thinking: medium` |
| implement | `sonnet` + `thinking: high` | `sonnet` + `thinking: high` |
| review | `opus` + `thinking: high` | `opus` + `thinking: high` |

The sprint runner uses Opus for spec (single-shot accuracy matters more in batch). The coding pipeline uses Sonnet for spec but has `spec_adversary` as a safety net. Defensible design choice but undocumented.

**Cost impact:** Sprint runs are ~2-3× more expensive per spec phase (Opus vs Sonnet).

---

### Finding 10 — LOW: `greenfield-project-v1` hardcoded GitHub org default

**Affected template:** `templates/greenfield-project-v1.yaml`
**Severity:** LOW — configurable, but default is `ToscanAI`

The `repo_org` config default is `"ToscanAI"`. A community user running `orch run greenfield-project-v1` without overriding `repo_org` would attempt to create a repo under the `ToscanAI` org (fails without permission).

**Recommendation:** Change default to empty string or a placeholder like `"your-org"`.

---

### Finding 11 — LOW: `code-development-pipeline.yaml` git context placeholders

**Affected template:** `examples/code-development-pipeline.yaml`
**Severity:** LOW — works when git integration is enabled

The `code_review` phase uses `{context.branch_name}`, `{context.base_branch}`, and `{context.git_diff}`, populated by the git integration module. If git integration is disabled, these resolve to `<MISSING:...>` placeholders that confuse the reviewing agent.

**Recommendation:** Add a header comment that git integration must be enabled for this template to work correctly.

---

### Finding 12 — LOW: `task_type` values not validated

**Affected:** All templates
**Severity:** LOW — engine is permissive by design

The `PhaseDefinition.task_type` field defaults to `"content"` and documents valid values as `content, research, review, code, translation`. However, templates also use `analysis` (security-audit, ux-audit, docs-pipeline), `command` (coding-pipeline, sprint-runner), and `acceptance_run` (coding-pipeline). None of these are validated against a known set.

The sequencer only checks for `command` and `acceptance_run` as special dispatch types (the `_NO_PROMPT_TASK_TYPES` set). All other values pass through to the executor unchanged — they're effectively labels for human readability.

**Recommendation:** Document all valid `task_type` values in the template authoring guide, including the engine-dispatched types (`command`, `acceptance_run`) vs. the label-only types (`content`, `research`, `review`, `code`, `analysis`).

---

### Finding 13 — INFO: Sprint runner chain design is well-architected

The `sprint-runner-v1` ↔ `sprint-runner-step-v1` ping-pong chain is a strong design:
- `sprint-runner-v1.on_complete.success` → launch `sprint-runner-step-v1` with `parent_output_dir: "{{output_dir}}"`
- `sprint-runner-step-v1.on_complete.success` → launch `sprint-runner-v1` with same mapping
- Natural termination: `prepare` phase exits code 1 when `remaining_issues` is empty
- `max_chain_depth: 12` prevents runaway chains
- Fail-fast: `failed: []` stops the chain on any issue failure

The `prepare` phase as a `task_type: command` with inline Python is a practical workaround for the lack of a built-in "issue queue" phase type. The validation (`positive integer` checks, `gh` CLI timeout) is solid.

---

### Finding 14 — INFO: coding-pipeline-v1 acceptance loop is well-designed

The `coding-pipeline-v1.yaml` introduces an innovative acceptance-first pattern:
1. `spec` → `spec_adversary` (APPROVE/REQUEST_CHANGES loop, `max_iterations: 4`)
2. `acceptance_test` → writes tests *before* code exists, with `protected_outputs: [acceptance_tests.py]`
3. `implement` → constrained by immutable acceptance tests
4. `acceptance_run` (engine-executed, `task_type: acceptance_run`) → `implement` on failure, `review` on success
5. `review` (Opus + supervisor) → `fix` on REQUEST_CHANGES → `acceptance_run` (re-verify)
6. `test` (command phase) → runs full test suite as final gate

The `auto_merge` and `routing_config` blocks at template level configure confidence-based merge/review/retry decisions. This is the most sophisticated template in the collection.

---

## 3. Summary Matrix

| # | Severity | Template | Finding | Status |
|---|---|---|---|---|
| 1 | ~~CRITICAL~~ | research-competitive | Bare `{var}` placeholders — prompt returned raw | **RESOLVED** → `research-competitive-v2.yaml` |
| 2 | ~~HIGH~~ | code-review-pipeline | Ghost scenario file reference | **RESOLVED** → `code-review-pipeline-v2.yaml` |
| 3 | ~~HIGH~~ | content-pipeline-v27 | Hyphenated phase IDs break file-handoff disc lookup | **RESOLVED** → `content-pipeline-v28.yaml` |
| 4 | ~~MEDIUM~~ | content-pipeline-v2 (example) | Hyphenated phase IDs — convention violation | **RESOLVED** → `content-pipeline-v3.yaml` |
| 5 | **MEDIUM** | Multiple | Dual output-reference syntax — consistency gap | Open |
| 6 | **MEDIUM** | All content/research | No scenario for quality gate | Open |
| 7 | **LOW** | research, editorial, greenfield | Missing `thinking_level` — relies on default | Open |
| 8 | **LOW** | docs-pipeline | `output_schema` not consumed | Open |
| 9 | **LOW** | sprint-runner vs coding | Opus spec cost difference — undocumented | Open |
| 10 | **LOW** | greenfield | Hardcoded GitHub org default | Open |
| 11 | **LOW** | code-dev example | `{context.*}` requires git enabled | Open |
| 12 | **LOW** | All | `task_type` values not validated | Open |
| 13 | **INFO** | sprint-runner pair | Well-architected chain design | — |
| 14 | **INFO** | coding-pipeline-v1 | Well-designed acceptance loop | — |

---

## 4. Recommended Remediation Order

### Tier 1 — Fix now (functional breakage) — ALL RESOLVED
1. ~~**Fix `research-competitive.yaml` placeholders**~~ — DONE → `templates/research-competitive-v2.yaml` (Finding 1)
2. ~~**Fix `content-pipeline-v27.yaml` phase IDs**~~ — DONE → `templates/content-pipeline-v28.yaml` (Finding 3)
3. ~~**Create or remove ghost scenario**~~ — DONE → `examples/code-review-pipeline-v2.yaml` (Finding 2)

### Tier 2 — Standardize (consistency) — PARTIALLY RESOLVED
4. ~~**Fix `content-pipeline-v2.yaml` phase IDs**~~ — DONE → `examples/content-pipeline-v3.yaml` (Finding 4)
5. **Document interpolation syntaxes** — add a reference section to template-authoring.md covering `{config[key]}`, `{phase_id.output}`, `{previous_output[id]}`, and file-handoff (Finding 5)
6. **Document `task_type` values** — list all valid types and which are engine-dispatched (Finding 12)

### Tier 3 — Improve (quality/usability)
7. **Create content pipeline scenarios** for automated quality gating (Finding 6)
8. **Change greenfield default org** to a placeholder (Finding 10)
9. **Add git-required note** to code-development-pipeline header (Finding 11)
