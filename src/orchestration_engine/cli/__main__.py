"""``python -m orchestration_engine.cli`` entry point.

Mirrors the behavior of the pre-package ``cli.py`` module so callers that spawn
``python -m orchestration_engine.cli ...`` (e.g. regression.py) keep working.
"""

from . import main

if __name__ == "__main__":
    main()
