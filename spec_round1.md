# Phase: spec

Now let me look at the `variables` field in `PipelineTemplate` and how it's parsed, plus the existing tests for reference:

Now I understand the structure. The `variables` field mentioned in the issue exists in templates (YAML) but is NOT currently a parsed field in `PipelineTemplate` at all. Let me verify:

So `variables` as a top-level YAML section doesn't exist in current templates — this is a future-forward feature described in the issue. Let me also look at the existing test structure:

Now I have enough context to write a comprehensive implementation spec. Let me also verify the exact `_SafeDict` pattern used and how phases are dispatched:

Now I have enough information to write a comprehensive implementation spec. Let me also look at a small part of the test file to understand the helper pattern to follow:

Now I have a comprehensive understanding of the codebase. Let me write the implementation spec:

Here is my complete implementation spec:

---

# Section B: Implementation Guidance

## 1. Problem Statement

The `PhaseSequencer` class in `src/orchestration_engine/sequencer.py` has three gaps that cause agent phases to run with unresolved `[MISSING:key>` strings in their prompts and commands:

**Gap 1 — No default merging.** `PhaseSequencer.__init__()` (line ~122) sets `self.config = config or {}` directly from raw pipeline input. The template exposes defaults in two places: `template.config_schema.properties.X.default` (a flat dict already stored in `template.config_schema`) and `template.variables.X.default` (a top-level YAML section that is **not yet parsed** into any `PipelineTemplate` field). Neither source of defaults is ever merged into `self.config` before interpolation begins.

**Gap 2 — Silent placeholder substitution with wrong format.** `_SafeDict.__missing__()` (line ~2338) already returns `<MISSING:key>` (angle brackets, not square brackets). The issue text references `[MISSING:key>` as the format — this is the **intended new format** for placeholder rejection detection. The `__missing__` method must be updated to produce `[MISSING:key>` (left square bracket, colon, key, right angle bracket) so that the post-interpolation scanner has a single known pattern to match. This also applies to `__getattr__`. Additionally, no JIT type coercion happens here today — booleans render as Python `"True"`/`"False"`, `None` renders as `"None"`, which breaks downstream shell commands.

**Gap 3 — No post-interpolation validation.** After `_build_phase_input()` and `_build_command_extras()` produce their interpolated strings, there is no check for unresolved `[MISSING:...>` markers before the task is submitted to the runner. This allows ghost agents to spawn with useless prompts.

Additionally, `templates.py` does not parse a `variables:` top-level YAML section into `PipelineTemplate` at all. This section needs to be added so the sequencer can read defaults from it.

## 2. Files to Modify

### `src/orchestration_engine/templates.py`

1. Add `variables: Dict[str, Any]` field to `PipelineTemplate` dataclass with `field(default_factory=dict)`.
2. In `PipelineTemplate.__post_init__()`, add normalization: `if self.variables is None: self.variables = {}`.
3. In `TemplateEngine.load_template()`, parse `data.get("variables") or {}` and pass it to `PipelineTemplate(variables=...)`.
4. Add module-level helper function `extract_config_defaults(template: PipelineTemplate) -> Dict[str, Any]` that builds a merged defaults dict with lowest-to-highest precedence: `config_schema` properties defaults, then `variables` defaults. Returns a flat dict of `{key: *** for top-level properties only (no recursive traversal of nested JSON Schema structures).

### `src/orchestration_engine/sequencer.py`

1. In `PhaseSequencer.__init__()`: after `self.config = config or {}`, call `extract_config_defaults(template)` (imported from `templates.py`) to build the defaults dict, then construct the merged config as `{**defaults_dict, **self.config}` and reassign to `self.config`. Original Python types are preserved (`bool`, `int`, `float`, `None`, `list`, `dict`) — no coercion at init.

2. In `PhaseSequencer.__init__()`: immediately after the default merge, perform required-field validation. Read `template.config_schema.get("required", [])`. For each field name in that list, check if the key is absent from the merged `self.config` (i.e., `key not in self.config`). If any are missing, raise `ValueError` with a consolidated message listing all missing field names: `"Pipeline initialization failed: required config fields missing after applying defaults: topic, author"`.

3. In `_SafeDict.__missing__()` (line ~2338): change the placeholder format from `<MISSING:{key}>` to `[MISSING:{key}>`. Add JIT type coercion: if the key *is* present (change `__getitem__` instead to intercept on access), coerce the retrieved value: `None` → `""`, `bool` → `"true"` or `"false"` (lowe
…(truncated)…
