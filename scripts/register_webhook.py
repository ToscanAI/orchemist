#!/usr/bin/env python3
"""One-shot setup script to register a GitHub webhook and create the matching
in-engine regression trigger.

Usage::

    python scripts/register_webhook.py \\
      --repo ToscanAI/orchestration-engine \\
      --url https://<your-public-host>/api/v1/webhooks/regression-ci-trigger \\
      [--trigger-id regression-ci-trigger] \\
      [--template-id regression-pipeline-v1]

The script is **idempotent**: if the trigger ID already exists in the DB it
logs a warning and skips DB insertion, but will still attempt to register the
GitHub webhook if ``--force-github`` is passed.

Environment variables
----------------------
``WEBHOOK_PUBLIC_URL``
    Fallback when ``--url`` is not supplied on the CLI.

``REGRESSION_TRIGGER_ID``
    Default trigger ID when ``--trigger-id`` is not supplied.

``REGRESSION_TEMPLATE_ID``
    Default template ID when ``--template-id`` is not supplied.

``ORCHESTRATION_ENGINE_DB``
    Path to the SQLite DB file.  Defaults to
    ``~/.orchestration-engine/engine.db``.

Issue: #429.2
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("register_webhook")


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_REPO = "ToscanAI/orchestration-engine"
_DEFAULT_TRIGGER_ID = "regression-ci-trigger"
_DEFAULT_TEMPLATE_ID = "regression-pipeline-v1"
_SECRET_FILENAME = "webhook-secret"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list.  Defaults to ``sys.argv[1:]`` when ``None``.

    Returns:
        Parsed :class:`argparse.Namespace`.
    """
    parser = argparse.ArgumentParser(
        description="Register a GitHub webhook and create the matching in-engine trigger.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--repo",
        default=_DEFAULT_REPO,
        help="GitHub repo slug (owner/repo).",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("WEBHOOK_PUBLIC_URL", ""),
        help=(
            "Public URL for the webhook endpoint.  "
            "Reads WEBHOOK_PUBLIC_URL env var as fallback."
        ),
    )
    parser.add_argument(
        "--trigger-id",
        default=os.environ.get("REGRESSION_TRIGGER_ID", _DEFAULT_TRIGGER_ID),
        help="Trigger ID to register in the orchestration engine.",
    )
    parser.add_argument(
        "--template-id",
        default=os.environ.get("REGRESSION_TEMPLATE_ID", _DEFAULT_TEMPLATE_ID),
        help="Pipeline template ID to associate with the trigger.",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "ORCHESTRATION_ENGINE_DB",
            str(Path.home() / ".orchestration-engine" / "engine.db"),
        ),
        help="Path to the SQLite DB file.",
    )
    parser.add_argument(
        "--force-github",
        action="store_true",
        default=False,
        help="Re-register the GitHub webhook even if the engine trigger already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would be done without making any changes.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Secret generation
# ---------------------------------------------------------------------------


def generate_and_store_secret(secret_path: Path) -> str:
    """Generate a cryptographically random HMAC secret and store it on disk.

    The secret file is written with mode ``0600`` (owner read/write only).
    If the file already exists it is silently overwritten.

    Args:
        secret_path: Filesystem path where the secret should be stored.

    Returns:
        The generated 64-character hex string (32 bytes of entropy).
    """
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    webhook_secret = secrets.token_hex(32)
    secret_path.write_text(webhook_secret, encoding="utf-8")
    # Restrict to owner read/write only
    secret_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    logger.info("Generated webhook secret and stored at %s (mode 0600)", secret_path)
    return webhook_secret


# ---------------------------------------------------------------------------
# GitHub webhook registration
# ---------------------------------------------------------------------------


def register_github_webhook(repo: str, payload_url: str, secret: str) -> int:
    """Register a ``check_suite.completed`` webhook on a GitHub repository.

    Uses the ``gh api`` CLI (GitHub CLI) to call the GitHub REST API.  The
    caller must have a valid ``gh auth`` session with ``admin:org`` or ``repo``
    scope.

    Args:
        repo:        GitHub repo slug (``owner/repo``).
        payload_url: The public HTTPS URL that GitHub will POST events to.
        secret:      HMAC-SHA256 webhook secret shared with the engine.

    Returns:
        The integer webhook ID returned by the GitHub API.

    Raises:
        RuntimeError: When ``gh api`` exits with a non-zero return code or
            returns invalid JSON.
        FileNotFoundError: When the ``gh`` CLI is not installed.
    """
    # Build the webhook config payload
    hook_data = {
        "name": "web",
        "active": True,
        "events": ["check_suite"],
        "config": {
            "url": payload_url,
            "content_type": "json",
            "secret": secret,
            "insecure_ssl": "0",
        },
    }
    input_json = json.dumps(hook_data)

    logger.info("Registering GitHub webhook on %s → %s", repo, payload_url)
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/hooks",
                "--method",
                "POST",
                "--input",
                "-",
            ],
            input=input_json,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "GitHub CLI ('gh') is not installed or not in PATH.  "
            "Install it from https://cli.github.com/ and run 'gh auth login'."
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"gh api returned non-zero exit code {result.returncode}.\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh api returned invalid JSON: {result.stdout!r}"
        ) from exc

    webhook_id = response.get("id")
    if webhook_id is None:
        raise RuntimeError(
            f"GitHub API response missing 'id' field: {response!r}"
        )

    logger.info(
        "GitHub webhook registered: id=%s, active=%s",
        webhook_id,
        response.get("active"),
    )
    return int(webhook_id)


# ---------------------------------------------------------------------------
# Engine trigger registration
# ---------------------------------------------------------------------------


def register_engine_trigger(
    db: Any,
    trigger_id: str,
    template_id: str,
    secret: str,
) -> Any:
    """Create and persist a regression trigger in the orchestration engine DB.

    Calls :func:`~orchestration_engine.regression.register_regression_trigger`
    to create the trigger row, then updates the ``secret`` field so HMAC
    verification is wired up.

    Args:
        db:          Open :class:`~orchestration_engine.db.Database` instance.
        trigger_id:  Unique trigger identifier.
        template_id: Pipeline template ID associated with the trigger.
        secret:      HMAC-SHA256 shared secret to store on the trigger.

    Returns:
        The created :class:`~orchestration_engine.webhooks.TriggerConfig`
        instance.

    Raises:
        sqlite3.IntegrityError: If a trigger with ``trigger_id`` already
            exists.  Callers should catch this and decide whether to skip or
            fail.
    """
    # Import here to keep the script self-contained when run from the repo root
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from orchestration_engine.regression import register_regression_trigger  # noqa: PLC0415

    trigger = register_regression_trigger(
        db=db,
        trigger_id=trigger_id,
        template_id=template_id,
    )
    # Store the HMAC secret (not set by register_regression_trigger)
    db.update_trigger(trigger_id, secret=secret)
    logger.info(
        "Engine trigger %r registered with secret (template=%r)", trigger_id, template_id
    )
    return trigger


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    """Entry point for the register_webhook script.

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    args = parse_args(argv)

    # Validate required arguments
    if not args.url:
        logger.error(
            "Webhook public URL is required.  "
            "Pass --url or set the WEBHOOK_PUBLIC_URL environment variable."
        )
        return 1

    if args.dry_run:
        logger.info("[DRY RUN] Would register webhook with the following settings:")
        logger.info("  repo:       %s", args.repo)
        logger.info("  url:        %s", args.url)
        logger.info("  trigger-id: %s", args.trigger_id)
        logger.info("  template:   %s", args.template_id)
        logger.info("  db:         %s", args.db)
        return 0

    # --- Ensure DB directory exists
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # --- Generate and store HMAC secret
    secret_path = db_path.parent / _SECRET_FILENAME
    webhook_secret = generate_and_store_secret(secret_path)
    print(f"\n[!] Webhook secret stored at: {secret_path}")
    print(f"[!] Set this environment variable on your API server:")
    print(f"    export WEBHOOK_SECRET={webhook_secret}\n")

    # --- Open DB
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from orchestration_engine.db import Database  # noqa: PLC0415

    db = Database(db_path=db_path)

    # --- Register engine trigger (idempotent)
    trigger_created = False
    try:
        register_engine_trigger(
            db=db,
            trigger_id=args.trigger_id,
            template_id=args.template_id,
            secret=webhook_secret,
        )
        trigger_created = True
        logger.info("Engine trigger %r created successfully.", args.trigger_id)
    except sqlite3.IntegrityError:
        logger.warning(
            "Trigger %r already exists in the DB — skipping creation.  "
            "Use --force-github to re-register the GitHub webhook anyway.",
            args.trigger_id,
        )

    # --- Register GitHub webhook
    should_register_github = trigger_created or args.force_github
    webhook_id: Optional[int] = None

    if should_register_github:
        try:
            webhook_id = register_github_webhook(
                repo=args.repo,
                payload_url=args.url,
                secret=webhook_secret,
            )
        except FileNotFoundError as exc:
            logger.error("GitHub CLI not available: %s", exc)
            logger.error(
                "Engine trigger was created successfully.  "
                "Register the GitHub webhook manually via the API or GitHub UI."
            )
            return 1
        except RuntimeError as exc:
            logger.error("Failed to register GitHub webhook: %s", exc)
            logger.error(
                "Engine trigger was created.  "
                "Retry with 'gh auth login' and re-run the script."
            )
            return 1
    else:
        logger.info(
            "Skipping GitHub webhook registration (trigger already exists and "
            "--force-github not set)."
        )

    # --- Summary
    print("\n=== Registration Summary ===")
    print(f"  Trigger ID:        {args.trigger_id}")
    print(f"  Template ID:       {args.template_id}")
    print(f"  GitHub webhook ID: {webhook_id or '(skipped)'}")
    print(f"  Payload URL:       {args.url}")
    print(f"  Secret file:       {secret_path}")
    print()
    print("Record the GitHub webhook ID — you will need it to monitor or delete the hook.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
