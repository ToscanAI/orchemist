# Context Guide — Writing Effective `files_context`

The `files_context` field in pipeline input is the **single most important factor** in pipeline success. A thin context forces the spec agent to explore the codebase — which burns time, produces file dumps instead of specs, and often fails entirely.

## Why This Matters

| Context Quality | Spec Behavior | Success Rate |
|-----------------|--------------|--------------|
| **Rich** (actual signatures, code patterns) | Writes spec immediately | ~95% (#579, #576, #600) |
| **Thin** (descriptions like "Database class with methods") | Explores codebase, dumps files | ~20% (#468, #571) |

The pattern is clear: **the more the spec agent already knows, the better it performs.**

## What Good `files_context` Looks Like

### 1. Actual Code, Not Descriptions

❌ Bad:
```
Database class with methods: get_pipeline_run(), list_pipeline_runs(), etc.
```

✅ Good:
```python
class Database:
    def get_pipeline_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Return a pipeline_runs row as a dict, or None."""
    
    def list_pipeline_runs(self, status: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """List pipeline runs ordered by created_at DESC."""
```

### 2. Function Signatures with Types and Docstrings

Include the exact interface the agent will code against. Skip implementation bodies.

### 3. The Pattern to Follow

If the new code should follow an existing pattern (error handling, test structure, imports), include a concrete example:

```python
# Error handling pattern in this codebase:
if run is None:
    click.echo(f"✗ Run '{run_id}' not found.", err=True)
    sys.exit(1)

# Import pattern:
from orchestration_engine.db import Database
from pathlib import Path
```

### 4. Line Numbers for Key Sections

When the agent needs to modify existing code, tell it exactly where:
```
### src/orchestration_engine/verdict_parser.py
Contains `extract_verdict()` function and the canonical `_VERDICT_KEYWORDS = {"approve", "request_changes", "abort"}` set (lowercase per the documented output contract; `src/orchestration_engine/transitions.py` re-exports both for legacy callers). When adding a new verdict keyword, edit `verdict_parser.py` ONLY — the re-export in `transitions.py` and the callsite in `sequencer.py` (lowercase comparison) pick it up automatically.
```

### 5. What to Include

For each relevant file:
- Full path relative to repo root
- Whether it's CREATE, MODIFY, or READ-ONLY
- Relevant class/function signatures (actual code)
- Line numbers if modifying existing code
- Purpose in one line

Also include:
- Import patterns the codebase uses
- Error handling conventions
- Test structure and patterns
- Any constants or config the new code needs

### 6. What to Skip

- Full file contents (the agent doesn't need 5000 lines of cli.py)
- Implementation bodies (signatures are enough)
- Unrelated modules
- History or changelog

## How to Extract Context

Run these from the repo root:

```bash
# Get class/function signatures from a file
grep -n "def \|class " src/path/to/file.py

# Get a specific function signature with docstring
sed -n '/def function_name/,/"""/p' src/path/to/file.py

# Get imports
head -30 src/path/to/file.py

# Get test patterns
head -60 tests/test_relevant.py
```

## Size Guidelines

- **Minimum viable:** ~500 chars (surgical fix, 1-2 files)
- **Typical feature:** ~2000-4000 chars (3-5 files with signatures)
- **Maximum useful:** ~6000 chars (beyond this, focus is lost)

## The Rule

**If the spec agent would need to `cat` a file to understand the interface, put that interface in `files_context` instead.**
