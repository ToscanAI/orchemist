# Behavioral Contracts — Generic Adversary Parser (#701)

## Section A: Observable Behavioral Contracts

### 1. Generic Parser — Verdict Extraction

- When `parse_adversary_output(text, config)` receives text containing `VERDICT: APPROVE` and config has `verdict_scan: "last"`, the system returns a verdict with `verdict="APPROVE"` and an empty findings list (if no finding lines present)
- When `parse_adversary_output(text, config)` receives text containing `VERDICT: REQUEST_CHANGES` followed by `[coverage] Missing test for contract X`, and `"coverage"` is in `config.valid_categories`, the system returns `verdict="REQUEST_CHANGES"` with one finding having `category="coverage"` and description containing `"Missing test for contract X"`
- When text contains multiple tagged finding lines across categories all in `config.valid_categories`, the system returns all findings with their respective categories preserved in order
- When text has no recognizable verdict (no APPROVE or REQUEST_CHANGES token), the system returns `verdict="REQUEST_CHANGES"` with one finding using `config.fallback_category` as its category
- When `config.fallback_category` is None and no verdict is found, the system uses the first entry in `config.valid_categories` as the fallback category

### 2. Generic Parser — Category Filtering

- When text contains `[coverage] description` and `"coverage"` is in `config.valid_categories`, the system includes it in findings with `category="coverage"`
- When text contains `[unknown_cat] description` and `"unknown_cat"` is NOT in `config.valid_categories`, the system silently skips it — no exception, finding does not appear in results
- When config has `valid_categories: ["vague", "trivial"]` and text contains `[coverage] desc`, the system skips `[coverage]` since it's not in this config's valid set
- When text contains findings in both valid and invalid categories, the system returns only valid-category findings and silently skips the rest

### 3. Generic Parser — Input Coercion

- When `parse_adversary_output` receives `None`, the system returns `verdict="REQUEST_CHANGES"` with an explanatory finding — never raises
- When it receives an empty string, the system returns `verdict="REQUEST_CHANGES"` with an explanatory finding
- When it receives a non-string (int, dict, list, bool, float), the system coerces via `str()` and returns `verdict="REQUEST_CHANGES"` — never raises regardless of input type

### 4. Generic Parser — Data Structures

- When a verdict is parsed, the verdict object exposes: `verdict` (str, "APPROVE" or "REQUEST_CHANGES"), `findings` (list of finding objects), `raw_text` (str, original input preserved verbatim — byte-identical to what was passed in, or `str(input)` if coerced)
- When a finding is parsed, the finding object exposes: `category` (str, always lowercase), `description` (str, preserved verbatim from input line after `[category] ` prefix)
- When an APPROVE verdict has finding lines in the text, the findings list is still populated (findings are parsed independently of verdict)

### 5. Generic Parser — Config-Driven Behavior

- When `config.verdict_scan` is `"first"` and text contains `VERDICT: APPROVE` before `VERDICT: REQUEST_CHANGES`, the parser returns `verdict="APPROVE"` (first wins)
- When `config.verdict_scan` is `"last"` and text contains `VERDICT: APPROVE` before `VERDICT: REQUEST_CHANGES`, the parser returns `verdict="REQUEST_CHANGES"` (last wins)
- When two configs have different `valid_categories`, the same input text produces different findings for each config (only categories in that config's set are included)

### 6. verdict_parser Enhancement — scan_order

- When `extract_verdict(text, scan_order="last")` is called (default), behavior is identical to the current implementation — last structured `VERDICT:` line wins (backward compatible)
- When `extract_verdict(text, scan_order="first")` is called, the first structured `VERDICT:` line wins
- When `extract_verdict` is called without `scan_order`, it defaults to `"last"` — all existing callers get identical behavior without code changes
- When Pass 1 (structured VERDICT: lines) finds no match regardless of scan_order, Pass 2 (fallback regex with priority ordering) is used — Pass 2 behavior is unchanged by scan_order

### 7. PhaseDefinition Parsing

- When a YAML phase contains `adversary_config:` with `valid_categories`, `fallback_category`, and `verdict_scan`, the system populates `phase.adversary_config` as an `AdversaryConfig` object with all fields set
- When a YAML phase has no `adversary_config` key, `phase.adversary_config` is `None`
- When `adversary_config` contains only `valid_categories` (other fields omitted), the system uses defaults: `fallback_category=None` (→ first category), `verdict_scan="last"`, `reward_enabled=False`

### 8. Validation

- When `adversary_config.valid_categories` is an empty list, `orch validate` rejects the template with a clear error message
- When `adversary_config.fallback_category` is set to a value NOT in `valid_categories`, `orch validate` rejects the template with a clear error message
- When `adversary_config.verdict_scan` is set to a value other than `"first"` or `"last"`, `orch validate` rejects the template with a clear error message
- When `adversary_config.valid_categories` contains duplicate entries, the system deduplicates silently preserving order (first occurrence kept)
- When `adversary_config` contains an unknown field (e.g. `unknown_key: true`), the system logs a warning (consistent with existing unknown-field handling in templates.py)
