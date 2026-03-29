# Implementation Spec — Generic Adversary Parser (#701)

## Section B: Implementation Guidance

### Overview
Create a generic adversary parser that replaces per-phase parser modules. Three deliverables: a new parser module, a verdict_parser enhancement, and PhaseDefinition/template parsing changes.

### Deliverable 1: Generic Parser Module

#### New file: `src/orchestration_engine/adversary_parser.py`

```python
@dataclass
class AdversaryConfig:
    valid_categories: List[str]           # e.g. ["coverage", "trivial_satisfaction", "leakage", "specificity"]
    fallback_category: Optional[str]      # defaults to first category if None
    verdict_scan: str = "last"            # "first" or "last" — passed to verdict_parser
    reward_enabled: bool = False          # parsed but NOT acted on in this phase
    reward_filename: str = "adversary_reward.json"  # parsed but NOT acted on

@dataclass
class AdversaryFinding:
    category: str       # always lowercase, must be in valid_categories
    description: str    # preserved verbatim from input

@dataclass
class AdversaryVerdict:
    verdict: str        # "APPROVE" or "REQUEST_CHANGES"
    findings: List[AdversaryFinding]
    raw_text: str       # original input preserved verbatim

def parse_adversary_output(text: Any, config: AdversaryConfig) -> AdversaryVerdict:
    """Generic adversary output parser — behavior driven by config."""
```

#### Algorithm (mirrors spec_adversary.py / acceptance_test_adversary.py):
1. Coerce non-string input via `str()`, fallback to `""`
2. Extract verdict using `verdict_parser.extract_verdict(text, scan_order=config.verdict_scan, allowed_verdicts={"approve", "request_changes"})`
3. Parse all `[category] description` lines — accept only categories in `config.valid_categories`, silently skip others
4. On no verdict found: return REQUEST_CHANGES with one finding using `config.fallback_category` (or first category if fallback is None)
5. Findings parsed independently of verdict (APPROVE with finding lines still populates findings)

#### Imports
- `from .verdict_parser import extract_verdict` (uses enhanced version with scan_order)
- stdlib only

### Deliverable 2: verdict_parser Enhancement

#### Modified: `src/orchestration_engine/verdict_parser.py`

Add `scan_order` parameter to `extract_verdict()`:
```python
def extract_verdict(
    text: Optional[str] = None,
    file_path: Optional[str] = None,
    allowed_verdicts: Optional[set] = None,
    scan_order: str = "last",  # NEW: "first" or "last"
) -> Optional[str]:
```

- `scan_order="last"` (default): current behavior — Pass 1 scans in reverse, last match wins. Backward compatible.
- `scan_order="first"`: Pass 1 scans forward, first match wins. Used by acceptance_test_adversary today.
- Pass 2 (fallback regex) behavior unchanged regardless of scan_order — it uses priority ordering.

### Deliverable 3: PhaseDefinition + Template Parsing

#### Modified: `src/orchestration_engine/templates.py`

- Add `adversary_config: Optional[AdversaryConfig] = None` to `PhaseDefinition` dataclass
- Add `"adversary_config"` to `known_fields` set
- Add `_parse_adversary_config(raw_dict) -> Optional[AdversaryConfig]` helper:
  - Returns None if key not present
  - Validates: non-empty categories, fallback in categories (if specified), verdict_scan in ("first", "last")
  - Deduplicates valid_categories silently (preserve order)
  - Logs warning on unknown sub-fields

#### Validation (in `orch validate`):
- Empty `valid_categories` → error
- `fallback_category` not in `valid_categories` → error  
- `verdict_scan` not "first" or "last" → error
- Unknown fields inside `adversary_config` → warning

### What is NOT in this implementation
- No sequencer changes (Phase 2, #702)
- No escalation_partner field (Phase 2, #702)
- No reward computation or persistence (Phase 2/3)
- No template YAML changes (Phase 3, #703)
- No changes to spec_adversary.py or acceptance_test_adversary.py

### Test Strategy
- Unit tests for `adversary_parser.py`: all behavioral contracts from the issue
- Unit tests for `verdict_parser.py`: scan_order="first" vs "last", backward compat
- Unit tests for `templates.py`: PhaseDefinition parsing, validation edge cases
- No integration/pipeline tests (no engine wiring in this phase)
