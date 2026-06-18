"""``python -m orchestration_engine.daemon`` entry point.

Mirrors the behavior of the pre-package ``daemon.py`` module's
``if __name__ == "__main__"`` block so the spawn in
``cli/pipeline_cmds.py`` —
``[sys.executable, "-m", "orchestration_engine.daemon", run_id, db_path]`` —
keeps working now that ``daemon`` is a package (``python -m <pkg>`` runs
``<pkg>/__main__.py``, not ``<pkg>/__init__.py``'s ``__main__`` guard).
"""

import sys

from . import logger, run_daemon

if __name__ == "__main__":
    if len(sys.argv) != 3:
        logger.error("Usage: python -m orchestration_engine.daemon <run_id> <db_path>")
        sys.exit(1)

    _run_id = sys.argv[1]
    _db_path = sys.argv[2]
    run_daemon(_run_id, _db_path)
