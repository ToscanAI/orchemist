# Phase: spec

The spec file is truncated in storage. I need to reconstruct what exists and apply the adversary's targeted fixes. Let me read the adversary findings carefully and produce the complete revised spec with surgical edits addressing only the flagged issues.

The adversary raised these findings:
1. **[divergence]** `behavioral.md` is truncated — Gaps 2 and 3 have no visible behavioral contracts
2. **[divergence]** No contract for JIT type coercion (spec Gap 2) — need clear observable outcomes in spec
3. **[divergence]** No contract for post-interpolation validation (spec Gap 3) — need observable outcomes specified
4. **[vague]** Contract 1.8 — need positive observable outputs, not just "no errors"
5. **[missing_edge_case]** `variables` key with no `default` sub-key (e.g., flat string instead of nested `{default: "foo"}`)
6. **[missing_edge_case]** Interaction between required-field validation and `None` values — does `None` satisfy `required`?

As the spec agent, I need to:
- Add/clarify Observable Outcomes for Gaps 2 and 3 (items 2, 3)
- Add observable outcome for the no-variables/no-config_schema case (item 4)
- Add implementation steps for the two missing edge cases (items 5, 6)

Here is the complete revised spec (reconstructing the full document with targeted additions):

---

# Section B: Implementation Guidance

## 1. Problem Statement

The `PhaseSequencer` class in `src/orchestration_engine/sequencer.py` has three gaps that cause agent phases to run with unresolved `[MISSING:key>` strings in their prompts and commands:

**Gap 1 — No default merging.** `PhaseSequencer.__init__()` (line ~122) sets `self.config = config or {}` directly from raw pipeline input. The template exposes defaults in two places: `template.config_schema.properties.X.default` (a flat dict already stored in `template.config_schema`) and `template.variables.X.default` (a top-level YAML section that is **not yet parsed** into any `PipelineTemplate` field). Neither source of defaults is ever merged into `self.config` before interpolation begins.

**Gap 2 — Silent placeholder substitution + missing JIT coercion.** `_SafeDict.__missing__()` (line ~2338) returns a placeholder string for any missing key. Additionally, no JIT type coercion happens — booleans render as Python `"True"`/`"False"`, `None` renders as `"None"`, which breaks downstream shell commands. The coercion rules must be applied at `__getitem__` access time inside `_SafeDict`, not at init time, to preserve raw Python types in `self.config` for programmatic use.

**Gap 3 — No post-interpolation validation.** After `_build_phase_input()` and `_build_command_extras()` produce their interpolated strings, there is no check for unresolved `[MISSING:...>` markers before the task is submitted to the runner. This allows ghost agents to spawn with useless prompts.

Additionally, `templates.py` does not parse a `variables:` top-level YAML section into `PipelineTemplate` at all. This section needs to be added so the sequencer can read defaults from it.

## 2. Files to Modify

### `src/orchestration_engine/templates.py`

1. Add `variables: Dict[str, Any]` field to `PipelineTemplate` dataclass with `field(default_factory=dict)`.
2. In `PipelineTemplate.__post_init__()`, add normalization: `if self.variables is None: self.variables = {}`.
3. In `TemplateEngine.load_template()`, parse `data.get("variables") or {}` and pass it to `PipelineTemplate(variables=...)`.
4. Add module-level helper function `extract_config_defaults(template: PipelineTemplate) -> Dict[str, Any]` that builds a merged defaults dict with lowest-to-highest precedence: `config_schema` properties defaults, then `variables` defaults. Returns a flat dict for top-level properties only (no recursive traversal of nested JSON Schema structures).
   - For `config_schema`: iterate `template.config_schema.get("properties", {}).items()`. For each `(key, prop_def)`, if `prop_def` is a dict and `"default"` is in `prop_def`, include `key: ***"default"]`.
  
…(truncated)…
