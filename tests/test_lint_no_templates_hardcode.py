"""Lint rule: no test file may reference templates/ by hardcoded filesystem path.

This test enforces that test files do not couple themselves to the production
templates/ directory. Templates may be renamed; test fixtures live in examples/
and must not be moved.

Allowed: "/api/v1/templates/" in HTTP endpoint strings (detected by /api/ prefix).
Forbidden: Path construction that traverses into the templates/ directory
           (e.g., Path(...) / "templates", REPO_ROOT / "templates" / ...).

Issue #632: Decouple test fixtures from templates/.
"""

import re
from pathlib import Path

TESTS_DIR = Path(__file__).parent

# Matches Path construction that references the templates/ directory as a filesystem path.
# Examples matched (violations):
#   REPO_ROOT / "templates" / "coding-pipeline-v1.yaml"
#   Path(__file__).parent.parent / "templates"
# Examples NOT matched (allowed):
#   "/api/v1/templates/coding-pipeline-v1"  (HTTP route)
#   client.get("/api/v1/templates/")        (HTTP route)
_FORBIDDEN_PATH = re.compile(
    r'/\s*["\']templates["\']'   # / "templates" or / 'templates'
)


def test_no_test_file_references_templates_dir_by_hardcoded_path():
    """No file in tests/ may construct a filesystem path into templates/.

    After Issue #632, stable test fixtures live in examples/ and no test
    file should construct a Path that traverses into templates/.
    """
    violations = []
    skip_self = Path(__file__).name
    for py_file in sorted(TESTS_DIR.glob("*.py")):
        if py_file.name == skip_self:
            continue
        source = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            # Skip comment lines
            if stripped.startswith("#"):
                continue
            # Skip HTTP route strings (contain /api/ or /api/v1/)
            if "/api/" in line:
                continue
            if _FORBIDDEN_PATH.search(line):
                violations.append(f"{py_file.name}:{lineno}: {line.strip()}")
    assert not violations, (
        "Test files must not reference templates/ by hardcoded filesystem path.\n"
        "Move test fixtures to examples/ instead.\n"
        "Violations:\n" + "\n".join(violations)
    )
