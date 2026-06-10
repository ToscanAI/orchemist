"""command_security — shared command security primitives.

Single-source truth for the command denylist and the shell-aware security gate
used by both :class:`CommandExecutor` (denylist only) and
``OpenRouterExecutor._execute_command_locally`` (full gate).

Threat model & honest framing
-----------------------------
The two layers here are NOT equal in strength:

1. **The denylist (:data:`DANGEROUS_PATTERNS`) is the always-on security floor.**
   It scans the RAW command string in BOTH denylist-only mode and allowlist
   mode, *before* any allowlist logic. It runs whether or not an allowlist is
   present. This is what catches ``bash -c 'rm -rf /'`` even when ``bash`` is
   itself allowlisted.

2. **The allowlist is BEST-EFFORT defense-in-depth over a shell string.** It is
   *intentionally bypassable* when the allowlist itself includes a shell
   interpreter (``bash``/``sh``): ``check_shell_command("bash -c '<anything>'",
   ["bash", ...])`` extracts the binary ``bash``, finds it in the allowlist, and
   passes. An allowlist that contains a shell interpreter is therefore
   effectively allow-all (the denylist floor still applies). This is ACCEPTED
   under the threat model: the real risk for Orchemist pipeline phases is
   *template misconfiguration*, not active adversaries with shell access. If the
   threat model ever includes untrusted template authors, the correct fix is
   shell-free execution (Strategy B) or a strict command-chain parser
   (Strategy C), not patching this gate.

Known false-rejects when an allowlist is ACTIVE (non-empty)
-----------------------------------------------------------
These are fail-closed (rejected, never crashes) and do NOT affect the shipped
gates (whose operators are space-delimited and which contain no substitutions):

* ``$((arithmetic))`` — matches the ``$(`` substitution marker → rejected.
* env-assignment prefixes, e.g. ``FOO=bar cmd`` — ``shlex`` yields ``FOO=bar``
  as the segment-start token → not a builtin / not in allowlist → rejected.
* operators glued without spaces, e.g. ``a&&b`` — ``shlex`` yields ``a&&b`` as a
  single token (not a standalone operator) → rejected. A subtler variant,
  ``pnpm build&&pnpm test``, tokenises to ``['pnpm', 'build&&pnpm', 'test']``:
  the leading binary is extracted and checked, but the *trailing* ``pnpm test``
  is silently skipped (no standalone operator token to start a new segment).
  This is not a security hole — the always-on denylist floor already scanned the
  full raw string — and operators are space-delimited in every shipped gate.
* subshell grouping ``( ... )`` and brace groups ``{ ...; }`` — the leading
  ``(`` / ``{`` becomes the segment-start token → rejected.
* ``[`` / ``test`` — these are genuine binaries (``/usr/bin/[``); they are NOT
  in the builtins exemption and require explicit allowlisting.

Design note: ``CommandExecutor`` uses ``shell=False`` and shlex-splits the
command before its allowlist check, so it does not need the shell-operator
splitting algorithm here — it imports only :data:`DANGEROUS_PATTERNS`. The
shell-aware allowlist gate (:func:`check_shell_command`) is used only by the
OpenRouter ``shell=True`` path.
"""

from __future__ import annotations

import re
import shlex
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Denylist — unconditionally blocks these patterns regardless of allowlist.
# Matched against the RAW command string before any other logic. This is the
# ALWAYS-ON security floor: it runs in both denylist-only and allowlist modes.
# ---------------------------------------------------------------------------
DANGEROUS_PATTERNS: List[re.Pattern] = [
    re.compile(r"\brm\s+(-\w*f\w*|-\w*r\w*){1,}"),  # rm -rf, rm -fr, rm -f, rm -r
    re.compile(r"\bsudo\b"),  # privilege escalation
    re.compile(r"\bsu\s"),  # switch user
    re.compile(r"\bcurl\b.*\|\s*(?:sh|bash|zsh|fish)"),  # curl | sh (RCE pattern)
    re.compile(r"\bwget\b.*\|\s*(?:sh|bash|zsh|fish)"),  # wget | sh (RCE pattern)
    re.compile(r">\s*/dev/sd[a-z]"),  # disk overwrite
    re.compile(r"\bdd\b.*of=/dev/"),  # dd to device
    re.compile(r"\bmkfs\b"),  # format filesystem
    re.compile(r"\bchmod\s+777\b"),  # world-writable
    re.compile(r"\bchown\s+root\b"),  # chown to root
    re.compile(r":\(\)\{.*\}"),  # fork bomb
    re.compile(r"\bpkill\b|\bkillall\b"),  # mass process kill
]

# ---------------------------------------------------------------------------
# Shell builtins exempt from allowlist checks. These tokens are not executable
# binaries; the shell handles them internally. Exactly these six — do NOT
# silently expand this set (see module docstring re: `[` / `test`).
# "echo" is included because, while /usr/bin/echo exists, it behaves as a
# builtin in bash/sh and is universally expected to be safe.
# ---------------------------------------------------------------------------
SHELL_BUILTINS: frozenset = frozenset(
    {
        "exit",
        "cd",
        ":",
        "true",
        "false",
        "echo",
    }
)

# ---------------------------------------------------------------------------
# Substitution tokens that bypass an allowlist over a shell string. When an
# allowlist is ENFORCED (non-empty), any command containing these substrings is
# rejected because the allowlist cannot be applied to the substituted content.
# (In denylist-only mode these are NOT rejected — only the denylist applies.)
# ---------------------------------------------------------------------------
SUBSTITUTION_MARKERS: Tuple[str, ...] = ("$(", "`", "<(", ">(")

# Top-level shell operators that separate command segments. The first token of
# each segment is the invoked binary.
_OPERATORS: frozenset = frozenset({"&&", "||", "|", ";"})


def check_shell_command(
    command: str,
    allowed_commands: Optional[List[str]],
) -> Optional[Tuple[str, str]]:
    """Security gate for ``shell=True`` commands.

    Returns ``None`` when the command passes all checks. Otherwise returns a
    ``(error_code, message)`` tuple where *message* is already ``[SECURITY]``-
    prefixed and suitable for direct use in a ``TaskError``.

    Control flow (order matters):

    1. **Denylist FIRST, ALWAYS (both modes).** Scan the raw string against
       :data:`DANGEROUS_PATTERNS`. A match → ``("security_blocked", ...)``. This
       is the always-on floor; it runs whether or not an allowlist is present.
    2. **Determine mode.** ``allowed = allowed_commands or None`` so both
       ``None`` and ``[]`` collapse to denylist-only. If denylist-only → return
       ``None`` (passed) now.
    3. **Allowlist mode (non-empty list):**
       a. Reject command substitution (``$(``, `` ` ``, ``<(``, ``>(``).
       b. ``shlex.split`` the string; on ``ValueError`` → FAIL CLOSED with
          ``("security_blocked", "[SECURITY] unparseable command")`` (do NOT
          naive-split — a wrong split could approve a forbidden binary).
       c. Extract the first token of each operator-delimited segment (the
          invoked binary); strip path prefixes via basename.
       d. Builtins (:data:`SHELL_BUILTINS`) are never rejected.
       e. Any non-builtin binary not in *allowed_commands* → blocked.
    """
    # ── Step 1: Denylist (always-on floor; both modes) ───────────────────────
    for pattern in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return (
                "security_blocked",
                f"[SECURITY] command matches dangerous pattern "
                f"'{pattern.pattern}' and is unconditionally blocked.",
            )

    # ── Step 2: Allowlist mode active? ([] and None → denylist-only) ─────────
    allowed = allowed_commands or None
    if allowed is None:
        return None  # denylist-only passed

    # ── Step 3: Reject command substitution (allowlist-active case) ──────────
    for marker in SUBSTITUTION_MARKERS:
        if marker in command:
            return (
                "security_blocked",
                "[SECURITY] command substitution not permitted with an allowlist",
            )

    # ── Step 4: Extract top-level binaries (fail closed on bad quoting) ──────
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes — cannot validate, so reject. Do NOT fall back to a
        # naive split (it ignores quoting and could approve a forbidden binary).
        return ("security_blocked", "[SECURITY] unparseable command")

    binaries = _extract_top_level_binaries(tokens)

    # ── Step 5: Allowlist check — each binary must be allowed or a builtin ────
    for binary in binaries:
        if binary in SHELL_BUILTINS:
            continue  # builtin; not subject to allowlist
        # Strip leading path components for portability (/usr/bin/git → git).
        basename = binary.split("/")[-1]
        if not any(basename == a or binary == a or binary.endswith(f"/{a}") for a in allowed):
            return (
                "security_blocked",
                f"[SECURITY] command '{binary}' not in allowlist",
            )

    return None


def _extract_top_level_binaries(tokens: List[str]) -> List[str]:
    """Return the first token of each operator-delimited segment.

    *tokens* is an already-``shlex.split`` token list. Segments are separated by
    the operator tokens in :data:`_OPERATORS` (``&&``, ``||``, ``|``, ``;``);
    the first non-empty token of each segment is the invoked binary.

    This is intentionally a linear scan, NOT a full shell parser. See the module
    docstring for the documented false-rejects (subshells, brace groups, glued
    operators, env-assignment prefixes) — all of which fail closed and none of
    which affect the shipped gates.
    """
    binaries: List[str] = []
    segment_start = True
    for token in tokens:
        if token in _OPERATORS:
            segment_start = True
        elif segment_start and token:
            binaries.append(token)
            segment_start = False
    return binaries
