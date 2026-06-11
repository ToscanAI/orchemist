"""Version parity guard (Issue #975).

Closes the drift-detection gap: the package literal ``__version__`` must equal
the source-of-truth ``[project].version`` in ``pyproject.toml``. The existing
health-endpoint tests only assert endpoint<->import consistency and are blind
to the pyproject literal drifting (which is exactly how #975 happened).
"""

from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11 fallback (mirrors config.py / mcp/server.py)
    import tomli as tomllib  # type: ignore[no-redef]

import orchestration_engine

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version_matches_pyproject():
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)

    project_version = data["project"]["version"]
    assert isinstance(project_version, str) and project_version, (
        "pyproject [project].version must be a non-empty string"
    )
    assert orchestration_engine.__version__ == project_version, (
        f"__version__ ({orchestration_engine.__version__!r}) is out of sync "
        f"with pyproject [project].version ({project_version!r}); bump both together."
    )
