"""Git-based phase handoff for the spec-loop (Issue #674).

Encapsulates all git operations for commit-based phase output tracking.
Isolated from ``git_integration.py`` (which handles feature-branch lifecycle).

When git is unavailable or operations fail, the class degrades gracefully:
``active`` becomes ``False`` and all methods return ``None`` / ``""``.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional

from .errors import GitHandoffError

logger = logging.getLogger(__name__)

_DIFF_TRUNCATION_LIMIT: int = 2500


class GitHandoff:
    """Commit-based handoff for spec-loop phases.

    Each phase's output is committed to a temporary branch
    ``spec-loop/{run_id}``.  The sequencer uses commit SHAs and diffs
    instead of inlining full text into prompts.

    All git commands run via ``subprocess.run`` with ``capture_output=True``.
    No ``--force`` flags.  No push (temp branch is local only).
    """

    def __init__(self, repo_path: Path, run_id: str) -> None:
        self.repo_path = Path(repo_path)
        self.run_id = run_id
        self.branch_name = f"spec-loop/{run_id}"
        self.original_branch: str = ""
        self.commit_log: Dict[str, Dict[int, str]] = {}
        self.active: bool = False
        self.run_dir: Path = self.repo_path / ".orchemist" / "runs" / run_id

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run a git command in the repo directory."""
        return subprocess.run(
            ["git"] + list(args),
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            check=check,
        )

    @staticmethod
    def _safe_filename(phase_id: str) -> str:
        """Sanitise a phase ID for use as a filename."""
        return re.sub(r"[^\w\-]", "_", phase_id) + ".md"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> bool:
        """Prepare the temp branch and run directory.

        Returns ``True`` on success, ``False`` on any failure (caller should
        fall back to file-based mode).
        """
        try:
            # Check git is available and we're inside a repo
            result = self._git("rev-parse", "--is-inside-work-tree", check=False)
            if result.returncode != 0:
                logger.warning(
                    "Git handoff: not inside a git work tree at %s — falling back",
                    self.repo_path,
                )
                return False

            # Check for dirty working tree
            status = self._git("status", "--porcelain")
            if status.stdout.strip():
                logger.warning(
                    "Git handoff: working tree is dirty — refusing to activate, "
                    "falling back to file-based mode"
                )
                return False

            # Record the current branch
            branch_result = self._git("rev-parse", "--abbrev-ref", "HEAD")
            self.original_branch = branch_result.stdout.strip()

            # Check .gitignore for .orchemist/
            self._check_gitignore()

            # Create run directory
            self.run_dir.mkdir(parents=True, exist_ok=True)

            # Create temp branch from current HEAD
            self._git("checkout", "-b", self.branch_name)

            self.active = True
            logger.info(
                "GitHandoff initialised for run_id=%s on branch %s",
                self.run_id,
                self.branch_name,
            )
            return True

        except FileNotFoundError:
            logger.warning("Git handoff: git binary not found — falling back to file-based mode")
            return False
        except subprocess.CalledProcessError as exc:
            logger.warning("Git handoff: initialisation failed — %s", exc)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("Git handoff: unexpected error during init — %s", exc)
            return False

    def _check_gitignore(self) -> None:
        """Warn if ``.orchemist/`` is not in ``.gitignore``."""
        gitignore_path = self.repo_path / ".gitignore"
        if gitignore_path.exists():
            content = gitignore_path.read_text()
            if ".orchemist/" in content or ".orchemist" in content.splitlines():
                return
        logger.warning(
            ".orchemist/ is not in .gitignore — add it to prevent "
            "spec-loop artifacts from persisting on feature branches."
        )

    # ------------------------------------------------------------------
    # Commit operations
    # ------------------------------------------------------------------

    def commit_phase_output(self, phase_id: str, round_num: int, output_text: str) -> Optional[str]:
        """Write phase output to the run dir, commit it, return the SHA.

        Returns ``None`` if the handoff is inactive or the commit fails.
        """
        if not self.active:
            return None

        try:
            filename = self._safe_filename(phase_id)
            file_path = self.run_dir / filename

            # Write (overwrite) the stable-named file
            file_path.write_text(output_text)

            # Stage only this file (use -f to override .gitignore for .orchemist/)
            rel_path = file_path.relative_to(self.repo_path)
            self._git("add", "-f", str(rel_path))

            # Commit with descriptive message
            msg = f"[spec-loop] {phase_id} round {round_num}"
            self._git("commit", "-m", msg, "--allow-empty")

            # Record SHA
            sha_result = self._git("rev-parse", "HEAD")
            sha = sha_result.stdout.strip()

            self.commit_log.setdefault(phase_id, {})[round_num] = sha
            return sha

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Git handoff: commit failed for %s round %d — deactivating. %s",
                phase_id,
                round_num,
                exc,
            )
            self.active = False
            return None

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def get_commit(self, phase_id: str, round_num: int) -> Optional[str]:
        """Return the commit SHA for a phase+round, or ``None``."""
        return self.commit_log.get(phase_id, {}).get(round_num)

    def get_diff(self, phase_id: str, from_round: int, to_round: int) -> str:
        """Return the git diff between two rounds for a phase.

        Truncates to ``_DIFF_TRUNCATION_LIMIT`` characters.
        Returns empty string on any failure.
        """
        if not self.active:
            return ""

        from_sha = self.get_commit(phase_id, from_round)
        to_sha = self.get_commit(phase_id, to_round)
        if not from_sha or not to_sha:
            return ""

        try:
            filename = self._safe_filename(phase_id)
            rel_path = self.run_dir.relative_to(self.repo_path) / filename
            result = self._git(
                "diff",
                "--no-color",
                from_sha,
                to_sha,
                "--",
                str(rel_path),
                check=False,
            )
            diff = result.stdout
            if not diff:
                return ""
            if len(diff) <= _DIFF_TRUNCATION_LIMIT:
                return diff
            # Truncate at a line boundary if possible
            truncated = diff[:_DIFF_TRUNCATION_LIMIT]
            last_nl = truncated.rfind("\n")
            if last_nl > _DIFF_TRUNCATION_LIMIT // 2:
                truncated = truncated[: last_nl + 1]
            return truncated
        except Exception:  # noqa: BLE001
            return ""

    def get_diff_for_member(self, member_id: str, current_round: int) -> str:
        """Convenience: diff between round ``current_round - 1`` and ``current_round``.

        Returns empty string if either commit is missing or ``current_round <= 1``.
        """
        if current_round <= 1:
            return ""
        return self.get_diff(member_id, current_round - 1, current_round)

    def is_active(self) -> bool:
        """Whether git handoff is operational."""
        return self.active

    # ------------------------------------------------------------------
    # Finalize and cleanup
    # ------------------------------------------------------------------

    def finalize(self, output_dir: Path, target_branch: str) -> None:
        """On APPROVE: copy final files to output_dir, commit on target branch.

        Raises ``GitHandoffError`` on failure (non-fatal to pipeline).
        """
        try:
            # Copy final files from run_dir to output_dir
            output_dir = Path(output_dir)
            if not output_dir.exists():
                logger.warning(
                    "finalize: output_dir does not exist, skipping copy: %s",
                    output_dir,
                )
                return
            for src_file in self.run_dir.glob("*.md"):
                dest_file = output_dir / src_file.name
                if dest_file.exists() and dest_file.stat().st_size > src_file.stat().st_size:
                    logger.info(
                        f"finalize: keeping larger agent file {dest_file.name} "
                        f"({dest_file.stat().st_size} bytes) over git version "
                        f"({src_file.stat().st_size} bytes)"
                    )
                    continue  # skip — keep the larger agent-written file
                shutil.copy2(src_file, dest_file)

            # Checkout target branch
            self._git("checkout", target_branch)

            # Copy deliverables from output_dir into repo
            for md_file in output_dir.glob("*.md"):
                dest = self.repo_path / md_file.name
                shutil.copy2(md_file, dest)
                self._git("add", "-f", md_file.name)

            # Commit on target branch
            self._git(
                "commit",
                "-m",
                f"[spec-loop] finalise spec artifacts (run {self.run_id})",
                "--allow-empty",
            )

            # Delete temp branch
            self._git("branch", "-D", self.branch_name, check=False)

        except Exception as exc:
            raise GitHandoffError(f"Finalize failed for run {self.run_id}: {exc}") from exc

    def cleanup(self, preserve: bool = False) -> None:
        """Checkout the original branch and optionally delete the temp branch.

        Args:
            preserve: If ``True`` (failure case), leave the branch for debugging.
        """
        try:
            # Checkout original branch (may already be on it)
            self._git("checkout", self.original_branch, check=False)

            if not preserve:
                self._git("branch", "-D", self.branch_name, check=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Git handoff cleanup warning: %s", exc)
