"""Git integration module for coding pipeline feature-branch lifecycle.

Provides :class:`GitContext` which manages the full git lifecycle for a
pipeline run: branch creation → stage/commit after each code phase →
diff injection for review → push → merge gate.

This module is intentionally executor-agnostic — it runs in the
orchestrator process (CLI/sequencer level), not inside sub-agent sessions.

Safety rules enforced by this module:
- All git commands run via :func:`subprocess.run` with ``check=True``.
- ``git push --force`` and ``git push -f`` are **statically rejected**.
  Any call to :meth:`GitContext._run_git` that includes ``--force`` or
  a bare ``-f`` flag alongside ``push`` raises :class:`ValueError` before
  any subprocess is created.
- Dirty working directories cause an abort (no auto-stash).
- Merge conflicts on push are reported clearly; never auto-resolved.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class GitConfig:
    """Parsed from the ``git:`` section of a pipeline YAML template.

    All fields are optional — absent fields use the listed defaults.
    When ``enabled`` is ``False`` (or the ``git:`` key is absent entirely),
    no git operations are performed and full backward compatibility is
    preserved.
    """

    enabled: bool = False
    """Master switch — set to ``True`` to activate git lifecycle management."""

    branch_pattern: str = "feat/{pipeline_id}-{run_id}"
    """Python format-string for the feature branch name.

    Supported placeholders: ``{pipeline_id}``, ``{run_id}``.
    """

    auto_commit: bool = True
    """If ``True``, stage+commit after each phase listed in ``commit_phases``."""

    commit_phases: List[str] = field(default_factory=list)
    """Phase IDs whose output should trigger a git commit."""

    working_dir: str = "."
    """Repo root relative to the process CWD."""

    push: bool = True
    """Push the feature branch to ``origin`` after commits."""

    merge_gate: bool = True
    """Pause after the last phase and wait for human approval before merging."""

    create_pr: bool = False
    """Opt-in: run ``gh pr create`` when the gate is approved (requires ``gh`` CLI)."""

    base_branch: Optional[str] = None
    """Target branch to branch from.  ``None`` = auto-detect from ``HEAD``."""


@dataclass
class BranchInfo:
    """Metadata about the feature branch created for this pipeline run."""

    branch_name: str
    """The name of the feature branch."""

    base_branch: str
    """The branch the feature branch was created from."""

    created_at: datetime
    """UTC timestamp of branch creation."""


@dataclass
class CommitInfo:
    """Metadata about a commit made during a pipeline phase."""

    sha: str
    """Full commit SHA."""

    message: str
    """Commit message used."""

    files_changed: int
    """Number of files included in the commit."""

    phase_id: str
    """Pipeline phase that triggered this commit."""


@dataclass
class MergeGateResult:
    """Result of the merge gate decision."""

    status: Literal["approved", "rejected", "timeout", "skipped", "awaiting_approval"]
    """Current gate status."""

    approved_by: Optional[str] = None
    """Identity of approver (populated on approval)."""

    message: Optional[str] = None
    """Human-readable context message."""


# ---------------------------------------------------------------------------
# GitError
# ---------------------------------------------------------------------------


class GitError(RuntimeError):
    """Raised when a git operation fails with a useful diagnostic message.

    Attributes:
        command:  The git command list that was attempted.
        stderr:   Captured stderr output from git, if available.
    """

    def __init__(self, message: str, command: Optional[List[str]] = None,
                 stderr: Optional[str] = None) -> None:
        super().__init__(message)
        self.command = command or []
        self.stderr = stderr or ""

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.command:
            parts.append(f"  Command: git {' '.join(self.command[1:] if self.command[0] == 'git' else self.command)}")
        if self.stderr:
            parts.append(f"  git stderr: {self.stderr.strip()}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# GitContext
# ---------------------------------------------------------------------------


class GitContext:
    """Manages the git lifecycle for a single pipeline run.

    Create one instance per pipeline run and call the lifecycle methods in order:

    1. :meth:`on_pipeline_start` — validate repo, create feature branch.
    2. :meth:`on_phase_complete` — stage+commit if the phase is in ``commit_phases``.
    3. :meth:`get_branch_diff` — three-dot diff injected into review phases.
    4. :meth:`on_pipeline_complete` — push, write gate file, optionally create PR.
    5. :meth:`cleanup` — restore original branch on failure (no-op on success).

    Args:
        config:       Parsed :class:`GitConfig` from the template.
        pipeline_id:  Template ``id`` field (used in branch name / commit messages).
        run_id:       Short unique run identifier (e.g. UUID prefix).
        output_dir:   Directory where phase outputs and gate files are written.
    """

    # Gates directory (central registry for ``orch gate`` commands)
    GATES_DIR: Path = Path.home() / ".orch" / "gates"

    def __init__(
        self,
        config: GitConfig,
        pipeline_id: str,
        run_id: str,
        output_dir: Optional[Path] = None,
    ) -> None:
        self.config = config
        self.pipeline_id = pipeline_id
        self.run_id = run_id
        self.output_dir = output_dir or Path(".")

        # Resolved lazily in on_pipeline_start
        self._repo_root: Optional[Path] = None
        self._branch_info: Optional[BranchInfo] = None
        self._commits: List[CommitInfo] = []

    # ------------------------------------------------------------------
    # Lifecycle methods
    # ------------------------------------------------------------------

    def on_pipeline_start(self) -> BranchInfo:
        """Validate the repository and create the feature branch.

        Checks performed:
        - git executable is available.
        - Current directory is inside a git repository.
        - Working directory is clean (no uncommitted changes).
        - HEAD is not detached.

        Returns:
            :class:`BranchInfo` describing the created branch.

        Raises:
            GitError: On any validation or branch-creation failure.
        """
        self._verify_git_installed()

        working_dir = Path(self.config.working_dir).resolve()
        self._repo_root = working_dir

        self._verify_is_git_repo(working_dir)
        self._verify_clean_working_dir(working_dir)

        base_branch = self._resolve_base_branch(working_dir)
        branch_name = self._make_branch_name()

        actual_branch_name = self._create_branch(working_dir, branch_name, base_branch)

        self._branch_info = BranchInfo(
            branch_name=actual_branch_name,
            base_branch=base_branch,
            created_at=datetime.now(tz=timezone.utc),
        )
        logger.info(
            f"Git: created branch '{actual_branch_name}' from '{base_branch}'"
        )
        return self._branch_info

    def on_phase_complete(
        self, phase_id: str, phase_output: Dict[str, Any]
    ) -> Optional[CommitInfo]:
        """Stage and commit working-directory changes after a code phase.

        If ``phase_id`` is not in :attr:`GitConfig.commit_phases`, or if
        ``auto_commit`` is ``False``, this is a no-op.

        Args:
            phase_id:     The ID of the phase that just completed.
            phase_output: The phase result dict (not used directly — files are
                          discovered via ``git add -A``).

        Returns:
            :class:`CommitInfo` if a commit was made, ``None`` otherwise.

        Raises:
            GitError: If the commit command fails for reasons other than
                      "nothing to commit".
        """
        if not self.config.auto_commit:
            return None
        if phase_id not in self.config.commit_phases:
            return None
        if self._branch_info is None:
            logger.warning(
                "Git: on_phase_complete called before on_pipeline_start — skipping commit"
            )
            return None

        working_dir = self._repo_root or Path(self.config.working_dir).resolve()

        # Stage everything (MVP decision: git add -A)
        self._run_git(["git", "add", "-A"], cwd=working_dir)

        # Check whether there is actually anything staged
        result = self._run_git(
            ["git", "diff", "--cached", "--quiet"],
            cwd=working_dir,
            check=False,
        )
        if result.returncode == 0:
            # Nothing staged — skip silently
            logger.info(f"Git: phase '{phase_id}' — nothing to commit, skipping")
            return None

        message = (
            f"[orch] Phase '{phase_id}' — {self.pipeline_id} (run {self.run_id})"
        )
        self._run_git(["git", "commit", "-m", message], cwd=working_dir)

        # Retrieve the commit SHA
        sha_result = self._run_git(
            ["git", "rev-parse", "HEAD"], cwd=working_dir
        )
        sha = sha_result.stdout.strip()

        # Count files changed in last commit
        diff_result = self._run_git(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            cwd=working_dir,
            check=False,
        )
        files_changed = len(
            [l for l in diff_result.stdout.strip().splitlines() if l.strip()]
        ) if diff_result.returncode == 0 else 0

        commit_info = CommitInfo(
            sha=sha,
            message=message,
            files_changed=files_changed,
            phase_id=phase_id,
        )
        self._commits.append(commit_info)
        logger.info(
            f"Git: committed phase '{phase_id}' → {sha[:8]}  "
            f"({files_changed} file(s))"
        )
        return commit_info

    def get_branch_diff(self) -> str:
        """Return the three-dot diff between base and feature branch.

        This is used to inject ``{context.git_diff}`` into review phase prompts.

        Returns:
            The raw unified diff output as a string.  Returns an empty string
            if no branch has been created yet or no commits exist on the feature
            branch.
        """
        if self._branch_info is None:
            return ""

        working_dir = self._repo_root or Path(self.config.working_dir).resolve()
        base = self._branch_info.base_branch
        branch = self._branch_info.branch_name

        result = self._run_git(
            ["git", "diff", f"{base}...{branch}"],
            cwd=working_dir,
            check=False,
        )
        return result.stdout if result.returncode == 0 else ""

    def on_pipeline_complete(self, success: bool = True) -> MergeGateResult:
        """Push the feature branch and enter the merge gate.

        Behaviour:
        - If ``push`` is ``True`` and a remote exists, push the branch.
        - If ``merge_gate`` is ``True``, write a gate file and return
          ``status="awaiting_approval"``.
        - If ``create_pr`` is ``True`` *and* approval has been given, run
          ``gh pr create`` — but for the async gate model, PR creation is
          deferred to :meth:`approve_gate`.
        - If ``merge_gate`` is ``False``, return ``status="skipped"``.

        Args:
            success: Whether the pipeline itself succeeded.

        Returns:
            :class:`MergeGateResult` describing the gate outcome.
        """
        if self._branch_info is None:
            return MergeGateResult(status="skipped", message="No branch was created")

        if not success:
            logger.info("Git: pipeline failed — skipping push and merge gate")
            return MergeGateResult(status="skipped", message="Pipeline did not succeed")

        working_dir = self._repo_root or Path(self.config.working_dir).resolve()

        # Push branch to remote (if configured and remote exists)
        if self.config.push:
            self._push_branch(working_dir)

        if not self.config.merge_gate:
            return MergeGateResult(
                status="skipped",
                message="merge_gate is disabled in template config",
            )

        # Write gate file
        diff_stats = self._get_diff_stats(working_dir)
        gate_data = self._write_gate_file(diff_stats)

        logger.info(
            f"Git: merge gate created (run_id={self.run_id}).  "
            f"Approve with: orch gate approve {self.run_id}"
        )
        return MergeGateResult(
            status="awaiting_approval",
            message=(
                f"Branch '{self._branch_info.branch_name}' is ready for review.\n"
                f"  Approve:  orch gate approve {self.run_id}\n"
                f"  Reject:   orch gate reject {self.run_id}\n"
                f"  Info:     orch gate info {self.run_id}"
            ),
        )

    def cleanup(self, success: bool) -> None:
        """Restore the original base branch on failure.

        On success this is a no-op (we stay on the feature branch).
        On failure we check out the base branch to leave the repo in a
        usable state.  The feature branch is **never deleted** — it may
        contain useful partial work.

        Args:
            success: Whether the pipeline completed without errors.
        """
        if success:
            return

        if self._branch_info is None:
            return

        working_dir = self._repo_root or Path(self.config.working_dir).resolve()
        base = self._branch_info.base_branch
        logger.info(
            f"Git: pipeline failed — checking out base branch '{base}'"
        )
        try:
            self._run_git(["git", "checkout", base], cwd=working_dir)
        except GitError as exc:
            # Best-effort — don't raise during cleanup
            logger.warning(f"Git: cleanup failed to checkout '{base}': {exc}")

    # ------------------------------------------------------------------
    # Gate management helpers (used by ``orch gate`` CLI commands)
    # ------------------------------------------------------------------

    def _write_gate_file(self, diff_stats: str) -> Dict[str, Any]:
        """Write the gate JSON file and return its content.

        Files are written to both the run output directory and the central
        ``~/.orch/gates/`` registry so ``orch gate list`` can find them.
        """
        if self._branch_info is None:
            raise GitError(
                "Cannot write gate file: no branch info (on_pipeline_start not called)",
                command=[], stderr="",
            )

        gate_data: Dict[str, Any] = {
            "run_id": self.run_id,
            "pipeline_id": self.pipeline_id,
            "status": "awaiting_approval",
            "branch": self._branch_info.branch_name,
            "base_branch": self._branch_info.base_branch,
            "diff_stats": diff_stats,
            "commits": [
                {
                    "sha": c.sha[:12],
                    "message": c.message,
                    "files_changed": c.files_changed,
                    "phase_id": c.phase_id,
                }
                for c in self._commits
            ],
            "output_dir": str(self.output_dir),
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "approve_command": f"orch gate approve {self.run_id}",
            "reject_command": f"orch gate reject {self.run_id}",
            "create_pr": self.config.create_pr,
        }

        gate_json = json.dumps(gate_data, indent=2)

        # Write to output dir
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            (self.output_dir / "_gate.json").write_text(gate_json)
        except OSError as exc:
            logger.warning(f"Git: could not write gate file to output_dir: {exc}")

        # Write to central gates registry
        try:
            self.GATES_DIR.mkdir(parents=True, exist_ok=True)
            (self.GATES_DIR / f"{self.run_id}.json").write_text(gate_json)
        except OSError as exc:
            logger.warning(f"Git: could not write gate file to ~/.orch/gates/: {exc}")

        return gate_data

    @classmethod
    def load_gate(cls, run_id: str) -> Optional[Dict[str, Any]]:
        """Load a gate file by run_id from the central registry.

        Args:
            run_id: The pipeline run identifier.

        Returns:
            Gate data dict or ``None`` if not found.
        """
        gate_file = cls.GATES_DIR / f"{run_id}.json"
        if not gate_file.exists():
            return None
        try:
            return json.loads(gate_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"Git: could not read gate file {gate_file}: {exc}")
            return None

    @classmethod
    def list_gates(cls) -> List[Dict[str, Any]]:
        """Return all gate files from the central registry.

        Returns:
            List of gate data dicts, sorted by ``created_at`` descending.
        """
        if not cls.GATES_DIR.exists():
            return []
        gates = []
        for gate_file in sorted(cls.GATES_DIR.glob("*.json"), reverse=True):
            try:
                data = json.loads(gate_file.read_text())
                gates.append(data)
            except (OSError, json.JSONDecodeError):
                pass
        return gates

    @classmethod
    def update_gate_status(
        cls,
        run_id: str,
        status: str,
        message: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update the status of a gate file.

        Args:
            run_id:  The pipeline run identifier.
            status:  New status string (e.g. ``"approved"``, ``"rejected"``).
            message: Optional human-readable note.

        Returns:
            Updated gate data dict, or ``None`` if the gate was not found.
        """
        gate_file = cls.GATES_DIR / f"{run_id}.json"
        if not gate_file.exists():
            return None
        try:
            data = json.loads(gate_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise GitError(f"Could not read gate file: {exc}") from exc

        data["status"] = status
        data["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
        if message:
            data["message"] = message

        try:
            gate_file.write_text(json.dumps(data, indent=2))
        except OSError as exc:
            raise GitError(f"Could not write gate file: {exc}") from exc

        # Also update output_dir copy if present
        output_dir = data.get("output_dir")
        if output_dir:
            out_gate = Path(output_dir) / "_gate.json"
            if out_gate.exists():
                try:
                    out_gate.write_text(json.dumps(data, indent=2))
                except OSError:
                    pass

        return data

    # ------------------------------------------------------------------
    # PR creation
    # ------------------------------------------------------------------

    def create_pr(self, gate_data: Dict[str, Any]) -> Optional[str]:
        """Create a GitHub PR via ``gh pr create``.

        Only called when ``create_pr: true`` in the git config.  Requires the
        ``gh`` CLI to be installed and authenticated.

        Args:
            gate_data: The gate data dict (provides branch/base/pipeline info).

        Returns:
            The PR URL as a string, or ``None`` on failure.
        """
        branch = gate_data.get("branch", "")
        base = gate_data.get("base_branch", "main")
        pipeline_id = gate_data.get("pipeline_id", "pipeline")
        run_id = gate_data.get("run_id", "")

        title = f"[orch] {pipeline_id} — run {run_id}"
        body = (
            f"Automated PR created by orchestration-engine.\n\n"
            f"**Pipeline:** {pipeline_id}\n"
            f"**Run ID:** {run_id}\n"
            f"**Branch:** {branch}\n\n"
            f"Commits:\n"
            + "\n".join(
                f"- {c['sha'][:8]} {c['message']}"
                for c in gate_data.get("commits", [])
            )
        )

        try:
            result = subprocess.run(
                ["gh", "pr", "create",
                 "--title", title,
                 "--body", body,
                 "--base", base,
                 "--head", branch],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                pr_url = result.stdout.strip()
                logger.info(f"Git: PR created → {pr_url}")
                return pr_url
            else:
                logger.warning(
                    f"Git: gh pr create failed (rc={result.returncode}): "
                    f"{result.stderr.strip()}"
                )
                return None
        except FileNotFoundError:
            logger.warning(
                "Git: 'gh' CLI not found.  Install GitHub CLI or set create_pr: false."
            )
            return None
        except subprocess.TimeoutExpired:
            logger.warning("Git: gh pr create timed out")
            return None

    # ------------------------------------------------------------------
    # Internal git helpers
    # ------------------------------------------------------------------

    def _run_git(
        self,
        args: List[str],
        cwd: Optional[Path] = None,
        check: bool = True,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess:
        """Run a git command via subprocess.

        **Safety:** This method statically rejects any invocation of
        ``git push`` that includes a force flag (``--force`` or standalone
        ``-f``).  This guard runs *before* any subprocess is created.

        Args:
            args:   Full command list, e.g. ``["git", "commit", "-m", "msg"]``.
            cwd:    Working directory (defaults to process CWD).
            check:  Whether to raise on non-zero exit (passed to subprocess).
            **kwargs: Extra kwargs forwarded to :func:`subprocess.run`.

        Returns:
            :class:`subprocess.CompletedProcess` instance.

        Raises:
            ValueError: If a force-push flag is detected.
            GitError:   If the git command exits non-zero (when ``check=True``).
        """
        # ---- SAFETY: reject force-push --------------------------------
        _is_push = "push" in args
        if _is_push:
            for _arg in args:
                if _arg == "--force":
                    raise ValueError(
                        "Force push (--force) is not allowed by this tool. "
                        f"Refusing command: {' '.join(args)}"
                    )
                # Reject standalone -f only when doing a push
                # (we allow -f in other git contexts like git diff -f or
                #  git checkout -f where it means something different,
                #  but for push it would be force)
                # We match "-f" as a standalone arg, not as part of a value.
                if _arg == "-f":
                    raise ValueError(
                        "Force push (-f) is not allowed by this tool. "
                        f"Refusing command: {' '.join(args)}"
                    )
                if _arg == "--force-with-lease":
                    raise ValueError(
                        "Force push (--force-with-lease) is not allowed by this tool. "
                        f"Refusing command: {' '.join(args)}"
                    )

        try:
            proc = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=check,
                **kwargs,
            )
            return proc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr or ""
            raise GitError(
                f"git command failed (exit {exc.returncode}): "
                f"{' '.join(args[1:] if args[0] == 'git' else args)}",
                command=args,
                stderr=stderr,
            ) from exc

    def _verify_git_installed(self) -> None:
        """Raise :class:`GitError` if git is not available in PATH."""
        try:
            subprocess.run(
                ["git", "--version"],
                capture_output=True,
                check=True,
            )
        except FileNotFoundError:
            raise GitError(
                "Git executable not found.  Install git or disable "
                "`git.enabled` in the pipeline template."
            )
        except subprocess.CalledProcessError as exc:
            raise GitError(
                f"git --version failed: {exc.stderr}",
                command=["git", "--version"],
                stderr=exc.stderr,
            ) from exc

    def _verify_is_git_repo(self, working_dir: Path) -> None:
        """Raise :class:`GitError` if ``working_dir`` is not inside a git repo."""
        result = self._run_git(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=working_dir,
            check=False,
        )
        if result.returncode != 0:
            raise GitError(
                f"'{working_dir}' is not inside a git repository.  "
                "Run `git init` or disable `git.enabled` in the template.",
                command=["git", "rev-parse", "--is-inside-work-tree"],
                stderr=result.stderr,
            )

    def _verify_clean_working_dir(self, working_dir: Path) -> None:
        """Raise :class:`GitError` if there are uncommitted changes."""
        result = self._run_git(
            ["git", "status", "--porcelain"],
            cwd=working_dir,
        )
        dirty_lines = [l for l in result.stdout.splitlines() if l.strip()]
        if dirty_lines:
            dirty_summary = "\n  ".join(dirty_lines[:10])
            extra = (
                f"\n  ... and {len(dirty_lines) - 10} more"
                if len(dirty_lines) > 10
                else ""
            )
            raise GitError(
                f"Working directory is dirty.  Commit or stash your changes "
                f"before running a git-enabled pipeline.\n\n"
                f"Dirty files:\n  {dirty_summary}{extra}"
            )

    def _resolve_base_branch(self, working_dir: Path) -> str:
        """Return the current branch name (used as base_branch).

        Raises:
            GitError: If HEAD is detached.
        """
        if self.config.base_branch:
            # Verify the configured base branch exists
            result = self._run_git(
                ["git", "rev-parse", "--verify", self.config.base_branch],
                cwd=working_dir,
                check=False,
            )
            if result.returncode != 0:
                raise GitError(
                    f"Configured base_branch '{self.config.base_branch}' "
                    f"does not exist in this repository."
                )
            return self.config.base_branch

        # Auto-detect current branch
        result = self._run_git(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=working_dir,
            check=False,
        )
        if result.returncode != 0:
            raise GitError(
                "Cannot determine base branch — HEAD is detached.  "
                "Checkout a named branch first (e.g. `git checkout main`)."
            )
        return result.stdout.strip()

    def _make_branch_name(self) -> str:
        """Generate a sanitised branch name from ``branch_pattern``.

        Returns:
            A valid git branch name string.
        """
        raw = self.config.branch_pattern.format(
            pipeline_id=self.pipeline_id,
            run_id=self.run_id,
        )
        # Sanitize: replace characters that are invalid in git branch names
        # with hyphens.  Keep alphanumeric, hyphen, underscore, slash, dot.
        sanitized = re.sub(r"[^\w/\-.]", "-", raw)
        # Remove consecutive hyphens / dots
        sanitized = re.sub(r"-{2,}", "-", sanitized)
        sanitized = re.sub(r"\.{2,}", ".", sanitized)
        # Strip leading/trailing hyphens or dots
        sanitized = sanitized.strip("-.")
        return sanitized

    def _create_branch(
        self, working_dir: Path, branch_name: str, base_branch: str
    ) -> str:
        """Create the feature branch, retrying with a numeric suffix on collision.

        Args:
            working_dir:  Repo root.
            branch_name:  Desired branch name.
            base_branch:  Branch to create from.

        Returns:
            The actual branch name created (may differ from *branch_name* on collision).

        Raises:
            GitError: If creation fails after 5 retries.
        """
        max_retries = 5
        attempt_name = branch_name
        for attempt in range(max_retries + 1):
            result = self._run_git(
                ["git", "checkout", "-b", attempt_name],
                cwd=working_dir,
                check=False,
            )
            if result.returncode == 0:
                return attempt_name
            # Branch likely already exists
            if attempt < max_retries:
                attempt_name = f"{branch_name}-{attempt + 2}"
                logger.warning(
                    f"Git: branch '{branch_name}' already exists — "
                    f"trying '{attempt_name}'"
                )
            else:
                raise GitError(
                    f"Could not create branch '{branch_name}' after {max_retries} attempts.  "
                    f"All candidate names are already taken.",
                    command=["git", "checkout", "-b", branch_name],
                    stderr=result.stderr,
                )

    def _push_branch(self, working_dir: Path) -> None:
        """Push the feature branch to ``origin``.

        Skips gracefully if no remote is configured.

        Raises:
            GitError: If the push fails for reasons other than missing remote.
        """
        if self._branch_info is None:
            raise GitError(
                "Cannot push: no branch info (on_pipeline_start not called)",
                command=[], stderr="",
            )

        # Check whether a remote named 'origin' exists
        remote_result = self._run_git(
            ["git", "remote", "get-url", "origin"],
            cwd=working_dir,
            check=False,
        )
        if remote_result.returncode != 0:
            logger.warning(
                "Git: no remote 'origin' configured — branch created locally only."
            )
            return

        branch_name = self._branch_info.branch_name
        try:
            self._run_git(
                ["git", "push", "-u", "origin", branch_name],
                cwd=working_dir,
            )
            logger.info(f"Git: pushed branch '{branch_name}' to origin")
        except GitError as exc:
            # Check if the error is a conflict / non-fast-forward
            if "rejected" in exc.stderr.lower() or "non-fast-forward" in exc.stderr.lower():
                raise GitError(
                    f"Push rejected — branch has diverged from remote.  "
                    f"Pull and resolve manually:\n"
                    f"  git pull origin {branch_name}\n"
                    f"Check your git credentials and remote permissions.",
                    command=exc.command,
                    stderr=exc.stderr,
                ) from exc
            # Permission / auth errors
            if "permission denied" in exc.stderr.lower() or "authentication" in exc.stderr.lower():
                raise GitError(
                    f"Push failed — check your git credentials and remote permissions.",
                    command=exc.command,
                    stderr=exc.stderr,
                ) from exc
            raise

    def _get_diff_stats(self, working_dir: Path) -> str:
        """Return a one-line summary of changes between base and feature branch.

        Returns:
            Human-readable diff stats string, e.g. ``"+142 -12 across 5 files"``.
        """
        if self._branch_info is None:
            return "no changes"

        result = self._run_git(
            ["git", "diff", "--stat",
             f"{self._branch_info.base_branch}...{self._branch_info.branch_name}"],
            cwd=working_dir,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return "no changes"

        # The last line of git diff --stat is the summary line
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        return lines[-1].strip() if lines else "no changes"
