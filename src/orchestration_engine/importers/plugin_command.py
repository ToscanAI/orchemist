"""Importer: knowledge-work-plugin command files → PipelineTemplate YAML.

A plugin command file is a Markdown document with an optional YAML frontmatter
block, one H1 title, and a series of H2 sections.  This module parses that
structure and emits a ``PipelineTemplate``-compatible YAML document that:

* Maps every non-meta H2 section to a pipeline phase (``sonnet`` tier).
* Inserts an independent ``review`` phase (``opus`` tier, ``medium`` thinking)
  after each content-producing phase.
* Derives ``config_schema`` from the ``## Inputs`` section.
* Collects skill file references and populates ``skill_refs`` on the relevant
  phases (resolved to absolute paths so ``orch validate`` can find them).
* Produces template-level metadata (id, name, version, description, author)
  that satisfies ``orch validate``'s extended checks.

Usage
-----
From Python::

    from orchestration_engine.importers.plugin_command import import_plugin_command
    yaml_text = import_plugin_command(Path("campaign-plan.md"))

From the CLI::

    orch import plugin-command campaign-plan.md [--output template.yaml]

Prompt-template placeholders
-----------------------------
Every generated phase prompt contains:

* ``{input}``          — the raw pipeline input dict (populated at runtime)
* ``{previous_output}``— output of the immediately preceding phase (if any)

These are passed through by the PhaseSequencer's ``_SafeDict`` substitution.

Section classification
-----------------------
H2 headings that match ``META_SECTIONS`` (case-insensitive exact match) are
skipped and produce no phase.  The ``Inputs`` heading is additionally parsed to
generate the template's ``config_schema``.  Everything else becomes a content
phase.

Edit the module-level ``META_SECTIONS`` frozenset to extend the skip list.
"""

from __future__ import annotations

import re  # noqa: F401  — still used by other helpers below
import textwrap
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# ---------------------------------------------------------------------------
# Module-level configuration constants
# ---------------------------------------------------------------------------

#: H2 headings that must not become pipeline phases.  Comparison is
#: case-insensitive.  Add entries here to extend the skip list without
#: touching any other code.
META_SECTIONS: frozenset = frozenset(
    {
        "trigger",
        "inputs",
        "output",
        "outputs",
        "notes",
        "instructions",
        "requirements",
        "prerequisites",
    }
)

#: Model tier used for generated content phases.
CONTENT_MODEL_TIER: str = "sonnet"

#: Model tier used for auto-inserted review phases.
REVIEW_MODEL_TIER: str = "opus"

#: Thinking level for content phases.
CONTENT_THINKING_LEVEL: str = "low"

#: Thinking level for auto-inserted review phases.
REVIEW_THINKING_LEVEL: str = "medium"

#: Author string stamped on every generated template.
GENERATED_AUTHOR: str = "orch import plugin-command"

#: Default template version.
DEFAULT_VERSION: str = "1.0.0"

# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------


@dataclass
class _Section:
    """Raw parsed H2 section from a plugin command file."""

    heading: str
    body: str  # Everything under this H2 until the next H2


@dataclass
class _ParsedCommand:
    """Intermediate representation of a fully-parsed plugin command file."""

    frontmatter: Dict[str, Any] = field(default_factory=dict)
    title: str = ""  # First H1 heading text
    sections: List[_Section] = field(default_factory=list)
    # Absolute paths to SKILL.md files found anywhere in the document
    skill_refs_all: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# String utilities
# ---------------------------------------------------------------------------


# slugify + snake_case live in the shared text_utils module (see #813 dedup audit
# Group 3); re-exported here so existing callers keep working unchanged.
from ..text_utils import slugify, snake_case  # noqa: F401


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _extract_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Extract YAML frontmatter from *content*.

    Returns ``(frontmatter_dict, body_without_frontmatter)``.

    The frontmatter is the YAML block between the **first** ``---`` line and the
    **next** ``---`` line.  If no valid frontmatter is found, returns
    ``({}, content)`` unchanged.

    Raises:
        ValueError: If the frontmatter YAML is syntactically invalid.
    """
    lines = content.splitlines(keepends=True)
    if not lines:
        return {}, content

    # The very first line must be exactly "---" (with optional trailing whitespace)
    if not lines[0].strip() == "---":
        return {}, content

    # Find the closing "---"
    close_idx: Optional[int] = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            close_idx = i
            break

    if close_idx is None:
        return {}, content

    fm_text = "".join(lines[1:close_idx])
    body = "".join(lines[close_idx + 1 :])

    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid frontmatter YAML: {exc}") from exc

    if not isinstance(fm, dict):
        fm = {}

    return fm, body


def _extract_h1(body: str) -> Tuple[str, str]:
    """Extract the first H1 heading from *body*.

    Returns ``(title_text, body_without_h1_line)``.  If no H1 is found,
    returns ``("", body)`` unchanged.
    """
    lines = body.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = re.match(r"^#\s+(.+)$", line.rstrip())
        if m:
            title = m.group(1).strip()
            rest = "".join(lines[:i] + lines[i + 1 :])
            return title, rest
    return "", body


def _split_by_h2(body: str) -> List[_Section]:
    """Split *body* at every H2 heading (``## ...``).

    Returns a list of ``_Section`` objects.  Text before the first H2 is
    ignored (it is typically intro prose).  The section body includes all text
    from after the heading line up to (but not including) the next H2 heading.
    """
    sections: List[_Section] = []
    lines = body.splitlines(keepends=True)

    current_heading: Optional[str] = None
    current_body_lines: List[str] = []

    def _flush() -> None:
        if current_heading is not None:
            sections.append(
                _Section(
                    heading=current_heading,
                    body="".join(current_body_lines).strip(),
                )
            )

    for line in lines:
        m = re.match(r"^##\s+(.+)$", line.rstrip())
        if m:
            _flush()
            current_heading = m.group(1).strip()
            current_body_lines = []
        else:
            if current_heading is not None:
                current_body_lines.append(line)

    _flush()
    return sections


def _classify_section(heading: str) -> str:
    """Classify an H2 heading.

    Returns one of:
    - ``"inputs"``  — parse for config_schema
    - ``"meta"``    — skip entirely
    - ``"content"`` — generate a pipeline phase
    """
    key = heading.strip().lower()
    if key == "inputs":
        return "inputs"
    if key in META_SECTIONS:
        return "meta"
    return "content"


# ---------------------------------------------------------------------------
# config_schema parsing
# ---------------------------------------------------------------------------

# Matches: `1. **Field name** — description text`
# Also handles:
#   - ` - ` (hyphen) instead of ` — ` (em-dash)
#   - zero or more parentheticals between the bold name and the dash,
#     e.g.: `2. **Budget range** (optional) — description`
#           `3. **Budget** (USD) (optional) — approximate budget`
#           `4. **Qty** (positive integer) (required) — count`
#
# BUG FIX: previously used `(?:\([^)]*\)\s*)?` (exactly-one, optional),
# which caused fields with *two or more* parentheticals (e.g. "(USD) (optional)")
# to be silently dropped from the config_schema.  Changed to `*` (zero-or-more).
_INPUT_FIELD_RE = re.compile(
    r"^\d+\.\s+\*\*([^*]+)\*\*\s*(?:\([^)]*\)\s*)*(?:—|-)\s*(.*)$",
    re.MULTILINE,
)


def _parse_inputs_section(body: str) -> Dict[str, Any]:
    """Parse an ``## Inputs`` section body into a JSON Schema ``config_schema``.

    Input list items follow the pattern::

        1. **Field name** — description text
        2. **Another field** (optional) — description
        3. **Budget** (USD) (optional) — approximate budget

    Multiple parentheticals before the separator are fully supported; any of
    them may carry the ``(optional)`` marker.

    Rules:
    - Field name → snake_case property key.
    - If ``(optional)`` appears anywhere in the field entry (name,
      parenthetical, or description), the field is **not** added to the
      ``required`` array.
    - ``(optional)`` tokens are stripped from the stored description text
      (regardless of their position — leading, trailing, or mid-sentence).
    - All fields default to ``type: string``.
    - Returns a minimal JSON Schema object; at minimum ``{"type": "object",
      "properties": {}, "required": []}`` so ``orch validate`` never sees an
      empty schema.
    """
    properties: Dict[str, Any] = {}
    required: List[str] = []

    for m in _INPUT_FIELD_RE.finditer(body):
        raw_name = m.group(1).strip()
        description = m.group(2).strip()
        full_match = m.group(0)  # Entire matched line — used for (optional) detection

        # Strip parenthetical suffixes from the field name for the key
        clean_name = re.sub(r"\s*\(.*?\)\s*$", "", raw_name).strip()
        key = snake_case(clean_name)

        # Strip *all* occurrences of "(optional)" from the description (any position).
        # Previously only the trailing occurrence was stripped, leaving "(optional)"
        # visible in the stored description when it appeared at the start or middle.
        # E.g. "(optional) extra context" → "extra context"
        #      "some text (optional)"     → "some text"
        #      "use (optional) if needed" → "use if needed"
        desc_clean = re.sub(r"\(\s*optional\s*\)\s*", "", description, flags=re.IGNORECASE).strip()
        # Collapse any runs of whitespace created by the removal
        desc_clean = re.sub(r" {2,}", " ", desc_clean)

        # "optional" marker = a *complete* "(optional)" token anywhere in the full
        # matched line (name, parenthetical, or description).
        # Uses \( ... \) to require the closing paren, avoiding false positives
        # on open tokens like "(optional_extra)".  Case-insensitive.
        is_optional = bool(
            re.search(r"\(\s*optional\s*\)", full_match, re.IGNORECASE)
        )

        properties[key] = {"type": "string", "description": desc_clean}

        if not is_optional:
            required.append(key)

    schema: Dict[str, Any] = {
        "type": "object",
        "properties": properties if properties else {"input": {"type": "string", "description": "Primary input"}},
    }
    if required:
        schema["required"] = required
    elif not properties:
        # Fallback: mark the synthetic "input" field as required
        schema["required"] = ["input"]

    return schema


# ---------------------------------------------------------------------------
# Skill-ref extraction
# ---------------------------------------------------------------------------

# Matches Markdown links whose href ends in SKILL.md:
#   [some label](../skills/brand-voice/SKILL.md)
_SKILL_LINK_RE = re.compile(
    r"\[(?:[^\]]*)\]\(([^)]+SKILL\.md[^)]*)\)",
    re.IGNORECASE,
)

# Matches prose references: "brand-voice/SKILL.md" or just "SKILL.md"
_SKILL_PROSE_RE = re.compile(
    r"(?:[\w\-]+/)*SKILL\.md",
    re.IGNORECASE,
)


def _extract_skill_refs(text: str, base_dir: Optional[Path] = None) -> List[str]:
    """Extract skill file references from *text*.

    Looks for:
    1. Markdown links: ``[label](path/to/SKILL.md)``
    2. Bare path references: ``some-skill/SKILL.md``

    Returns a **deduplicated, ordered** list of absolute path strings for
    SKILL.md files that actually exist on disk (so ``orch validate`` passes).
    References that cannot be resolved are silently dropped.

    Args:
        text:     Full document text to scan.
        base_dir: Directory of the source command file, used to resolve
                  relative paths.  ``None`` disables path resolution and
                  returns an empty list.
    """
    if base_dir is None:
        return []

    seen: Dict[str, None] = {}  # Ordered set (insertion-order dict)

    def _try_resolve(raw_href: str) -> Optional[str]:
        """Resolve *raw_href* relative to *base_dir*; return abs path or None.

        Security — two layers of path-traversal prevention:

        1. Absolute paths are rejected unconditionally.  A skill reference
           like ``/etc/SKILL.md`` must never be accepted regardless of whether
           the file exists.

        2. Relative paths that resolve *outside* the project root are rejected.
           The project root is defined as ``base_dir.parent`` (one directory
           above the command file's folder).  This permits the canonical
           ``../skills/brand-voice/SKILL.md`` pattern (up from ``commands/``
           to the project root, then down into ``skills/``), while blocking
           references like ``../../etc/SKILL.md`` that escape the project tree
           entirely.

        Callers that genuinely need an out-of-tree skill file should place a
        symlink pointing to it from within the project directory.
        """
        p = Path(raw_href)
        if p.is_absolute():
            # Reject absolute path skill refs: they can reference arbitrary
            # locations on the filesystem, including sensitive system paths.
            return None
        resolved = (base_dir / p).resolve()
        # Reject relative paths that escape beyond the project root.
        # project_root == base_dir.parent  (e.g. /project/ when the command
        # file lives in /project/commands/).
        project_root = base_dir.resolve().parent
        try:
            resolved.relative_to(project_root)
        except ValueError:
            return None
        if resolved.exists() and resolved.name.upper() == "SKILL.MD":
            return str(resolved)
        return None

    # 1. Markdown links
    for m in _SKILL_LINK_RE.finditer(text):
        href = m.group(1).strip()
        r = _try_resolve(href)
        if r and r not in seen:
            seen[r] = None

    # 2. Prose references (only if they look like relative paths)
    for m in _SKILL_PROSE_RE.finditer(text):
        href = m.group(0).strip()
        r = _try_resolve(href)
        if r and r not in seen:
            seen[r] = None

    return list(seen)


# ---------------------------------------------------------------------------
# Phase helpers
# ---------------------------------------------------------------------------

def _make_unique_id(base_id: str, seen: Dict[str, int]) -> str:
    """Return a collision-free phase ID derived from *base_id*.

    If *base_id* has not been used, return it and record it.  Otherwise
    append ``-2``, ``-3``, … until a fresh name is found.

    Unlike a naive recursive approach, this iterative implementation correctly
    handles the case where a literal section heading like "Phase 2" has already
    added ``phase-2`` to *seen* independently — a subsequent duplicate "Phase"
    heading then produces ``phase-3`` rather than the ugly ``phase-2-2``.

    A loop limit of 10,000 guards against unbounded iteration on pathological
    documents (far beyond any realistic heading duplication).

    Args:
        base_id: Slugified candidate phase ID.  If empty (e.g. because the
                 heading consisted entirely of non-ASCII/non-alphanumeric
                 characters and ``slugify()`` produced ``""``), falls back to
                 the generic identifier ``"section"`` so that the generated
                 template always contains valid, non-empty phase IDs.
        seen:    Mutable mapping of ``{phase_id: 1}``.  Modified in place.
    """
    # Guard: slugify("🎯") == "" — use a safe fallback so callers never
    # end up with an empty string as a phase id.
    if not base_id:
        base_id = "section"
    if base_id not in seen:
        seen[base_id] = 1
        return base_id
    # Iterative counter: try base_id-2, base_id-3, … until we find a free slot.
    counter = 2
    while counter <= 10_000:
        candidate = f"{base_id}-{counter}"
        if candidate not in seen:
            seen[candidate] = 1
            return candidate
        counter += 1
    raise RuntimeError(
        f"Cannot generate a unique phase ID for base '{base_id}' after 10,000 "
        "attempts — check for pathological heading duplication."
    )


def _build_phase_prompt(heading: str, body: str) -> str:
    """Construct a prompt template for a content phase.

    The prompt:
    1. Declares what input is available (``{input}`` and ``{previous_output}``).
    2. States the section name.
    3. Appends the full section body as task instructions.

    Note: heading and body are embedded via f-string/concatenation rather than
    ``str.format()`` so that any ``{...}`` placeholders that legitimately appear
    in the body text (e.g., Jinja templates, JSON examples, or keys like
    ``{heading}``/``{body}`` itself) are passed through verbatim instead of
    being silently mangled.  The ``{input}`` and ``{previous_output}`` tokens
    are preserved as literal strings for PhaseSequencer's ``_SafeDict``
    substitution at runtime.
    """
    instructions = body if body else f"Complete the '{heading}' step."
    return (
        f"You are executing the '{heading}' step of this workflow.\n\n"
        "## Input\n\n"
        "{input}\n\n"
        "## Context from previous steps\n\n"
        "{previous_output}\n\n"
        "## Instructions\n\n"
        + instructions
    )


def _build_review_prompt(content_phase_id: str, content_phase_name: str) -> str:
    """Construct the prompt template for an auto-inserted review phase.

    The generated prompt uses ``{previous_output[<phase_id>]}`` which the
    PhaseSequencer resolves at runtime to the named phase's output text.
    """
    prev_ref = "{" + f"previous_output[{content_phase_id}]" + "}"
    return (
        f"You are a senior reviewer evaluating the output of the '{content_phase_name}' step.\n\n"
        f"## Content to Review\n\n"
        f"{prev_ref}\n\n"
        "## Review Criteria\n\n"
        "Evaluate the content against the following dimensions:\n\n"
        "1. **Completeness** — Does the output address all required points?\n"
        "2. **Quality** — Is the content well-structured, clear, and accurate?\n"
        "3. **Consistency** — Does it align with the stated inputs and goals?\n"
        "4. **Actionability** — Are the outputs specific enough to act on?\n\n"
        "## Output Format\n\n"
        "Provide:\n"
        "- An overall quality score (1–10)\n"
        "- Strengths (bullet list)\n"
        "- Issues found (bullet list with severity: high/medium/low)\n"
        "- Specific revision suggestions for any high-severity issues\n"
        "- A final verdict: APPROVED / NEEDS-REVISION / REJECTED\n"
    )


# ---------------------------------------------------------------------------
# Core parse → template conversion
# ---------------------------------------------------------------------------


def _parse_document(content: str, source_path: Optional[Path] = None) -> _ParsedCommand:
    """Parse raw Markdown *content* into a ``_ParsedCommand`` intermediate.

    Args:
        content:     Full text of the plugin command file.
        source_path: Path to the source file (used to resolve skill refs).

    Returns:
        A ``_ParsedCommand`` with all sections classified.
    """
    base_dir = source_path.parent if source_path else None

    try:
        frontmatter, body = _extract_frontmatter(content)
    except ValueError as exc:
        raise ValueError(f"Malformed frontmatter: {exc}") from exc

    title, body = _extract_h1(body)
    if not title:
        raise ValueError(
            "Plugin command file must have at least one H1 heading (# Title) "
            "to derive the template name."
        )

    sections = _split_by_h2(body)
    skill_refs_all = _extract_skill_refs(content, base_dir)

    return _ParsedCommand(
        frontmatter=frontmatter,
        title=title,
        sections=sections,
        skill_refs_all=skill_refs_all,
    )


def _build_template_dict(
    parsed: _ParsedCommand,
    author: str = GENERATED_AUTHOR,
) -> Dict[str, Any]:
    """Convert a ``_ParsedCommand`` into a ``PipelineTemplate``-compatible dict.

    This is the heart of the importer.  It:
    - Derives ``id``, ``name``, ``version``, ``description``, ``author``.
    - Builds ``config_schema`` from the ``## Inputs`` section.
    - Creates a pipeline phase for every content section.
    - Inserts a review phase after each content phase (linear chain).
    - Assigns skill refs to phases whose body mentions skill files.

    Args:
        parsed: Output of ``_parse_document``.
        author: Value for the ``author`` field in the generated template.

    Returns:
        A ``dict`` ready to be serialised with ``yaml.dump``.
    """
    fm = parsed.frontmatter

    template_id = slugify(parsed.title)
    template_name = parsed.title
    description = fm.get("description") or ""

    # BUG FIX: previously `list(fm.get("tags") or [])` coerced an *explicit*
    # `tags: []` in frontmatter to the empty list, then `tags or [defaults]`
    # replaced it with the hardcoded defaults — ignoring the user's intent.
    #
    # Correct semantics:
    #   - tags: [foo, bar]  → use [foo, bar]
    #   - tags: []          → use []  (user explicitly opted out of defaults)
    #   - tags: null        → use defaults (null == "not specified")
    #   - <key absent>      → use defaults
    _fm_tags = fm.get("tags")
    tags: List[str] = list(_fm_tags) if isinstance(_fm_tags, list) else []

    # config_schema: populated from the Inputs section (if present)
    config_schema: Dict[str, Any] = {}

    # Intermediate list of phase dicts
    phases: List[Dict[str, Any]] = []
    seen_ids: Dict[str, int] = {}
    prev_phase_id: Optional[str] = None  # For linear depends_on chain

    for section in parsed.sections:
        classification = _classify_section(section.heading)

        if classification == "inputs":
            config_schema = _parse_inputs_section(section.body)
            continue

        if classification == "meta":
            continue

        # ── Content phase ──────────────────────────────────────────────
        phase_id = _make_unique_id(slugify(section.heading), seen_ids)
        phase_name = section.heading

        # Determine which skill refs belong to this section
        # (skill_refs_all already contains resolved absolute paths)
        section_skill_refs = _skill_refs_for_section(
            section.body, parsed.skill_refs_all
        )

        content_phase: Dict[str, Any] = {
            "id": phase_id,
            "name": phase_name,
            "description": f"Execute the '{phase_name}' step from the plugin command.",
            "task_type": "content",
            "model_tier": CONTENT_MODEL_TIER,
            "thinking_level": CONTENT_THINKING_LEVEL,
            "depends_on": [prev_phase_id] if prev_phase_id else [],
            "timeout_minutes": 30,
            "prompt_template": _build_phase_prompt(phase_name, section.body),
        }
        if section_skill_refs:
            content_phase["skill_refs"] = section_skill_refs

        phases.append(content_phase)
        prev_phase_id = phase_id

        # ── Auto-inserted review phase ─────────────────────────────────
        review_id = _make_unique_id(f"{phase_id}-review", seen_ids)
        review_phase: Dict[str, Any] = {
            "id": review_id,
            "name": f"{phase_name} — Review",
            "description": (
                f"Senior review of the '{phase_name}' output "
                "(auto-inserted by orch import plugin-command)."
            ),
            "task_type": "review",
            "model_tier": REVIEW_MODEL_TIER,
            "thinking_level": REVIEW_THINKING_LEVEL,
            "depends_on": [phase_id],
            "timeout_minutes": 20,
            "prompt_template": _build_review_prompt(phase_id, phase_name),
        }
        phases.append(review_phase)
        prev_phase_id = review_id

    # Ensure we always have a valid config_schema
    if not config_schema:
        config_schema = {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "Primary input for the pipeline",
                }
            },
            "required": ["input"],
        }

    # Build example_input from required schema fields (helps extended validation)
    example_input: Dict[str, Any] = {}
    for key in config_schema.get("required", []):
        example_input[key] = f"<{key}>"

    # use_cases: derived from frontmatter argument-hint + description
    use_cases: List[str] = []
    if fm.get("description"):
        use_cases.append(fm["description"])
    if fm.get("argument-hint"):
        use_cases.append(f"Input: {fm['argument-hint']}")

    template: Dict[str, Any] = {
        "id": template_id,
        "name": template_name,
        "version": DEFAULT_VERSION,
        "description": description or f"Imported from plugin command: {template_name}",
        "author": author,
        "category": "imported",
        # Use whatever tags the frontmatter specified (even empty list).
        # Fall back to defaults only when tags was absent or null in frontmatter.
        "tags": tags if isinstance(_fm_tags, list) else ["imported", "plugin-command"],
        "use_cases": use_cases or [f"Execute the {template_name} workflow"],
        "example_input": example_input or {"input": "<primary input>"},
        "config_schema": config_schema,
        "phases": phases,
    }

    return template


def _skill_refs_for_section(section_body: str, all_refs: List[str]) -> List[str]:
    """Filter *all_refs* to those actually mentioned in *section_body*.

    A skill ref is considered "mentioned" in a section if the SKILL.md
    filename's parent directory name appears as a **whole word** in the section
    text (case-insensitive).  This is a best-effort heuristic — full path
    matching is impractical since the section body contains prose, not file
    paths.

    Word-boundary matching (``\\b``) is used instead of a bare substring check.
    The bare-substring approach produced false positives for short skill names:
    a skill named ``ai`` would match any section body containing words like
    "email", "said", "brain", "maintain", "detail", etc., silently injecting
    an irrelevant skill_ref into that phase.

    Because both *normalised* and *body_lower* have hyphens and underscores
    converted to spaces, ``\\b`` correctly delimits word boundaries around
    multi-word skill names (e.g. ``"brand voice"`` still matches
    ``"brand voice guidelines"``).

    Returns only refs whose skill name can be found in the section body.
    """
    if not all_refs:
        return []
    result: List[str] = []
    body_lower = section_body.lower().replace("-", " ").replace("_", " ")
    for ref in all_refs:
        skill_dir = Path(ref).parent.name  # e.g. "brand-voice"
        # Normalise: hyphens/underscores/spaces all equivalent
        normalised = skill_dir.lower().replace("-", " ").replace("_", " ")
        # Use word-boundary regex to avoid short names matching substrings.
        # re.escape handles any residual special characters in skill dir names.
        pattern = re.compile(r"\b" + re.escape(normalised) + r"\b")
        if pattern.search(body_lower):
            result.append(ref)
    return result


# ---------------------------------------------------------------------------
# YAML serialisation
# ---------------------------------------------------------------------------


def _dump_template_yaml(template_dict: Dict[str, Any]) -> str:
    """Serialise *template_dict* to a YAML string.

    Uses a custom representer to ensure multi-line prompt strings are written
    as YAML literal blocks (``|``), which are far more readable than quoted
    inline strings.
    """
    # Register a literal-block representer for long strings
    class _LiteralStr(str):
        pass

    def _literal_representer(dumper: yaml.Dumper, data: _LiteralStr) -> yaml.ScalarNode:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")

    def _prepare(obj: Any) -> Any:
        """Recursively convert long strings to _LiteralStr."""
        if isinstance(obj, dict):
            return {k: _prepare(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_prepare(v) for v in obj]
        if isinstance(obj, str) and ("\n" in obj or len(obj) > 80):
            return _LiteralStr(obj)
        return obj

    prepared = _prepare(template_dict)

    # Use a local subclass so we never mutate the global yaml.Dumper
    # representers dict.  Assigning ``dumper = yaml.Dumper`` is merely an
    # alias (not a copy) and would permanently pollute the global state.
    class _CustomDumper(yaml.Dumper):
        pass

    _CustomDumper.add_representer(_LiteralStr, _literal_representer)

    header = (
        "# Generated by: orch import plugin-command\n"
        "# Prompt placeholders:\n"
        "#   {input}           — pipeline config dict (set by --input / --input-file)\n"
        "#   {previous_output} — output of the previous phase in the chain\n"
        "#\n"
        "# Review phases (model_tier: opus) are auto-inserted after every\n"
        "# content phase.  Remove or adjust them as needed.\n"
        "#\n"
    )

    stream = StringIO()
    yaml.dump(
        prepared,
        stream,
        Dumper=_CustomDumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=120,
    )
    return header + stream.getvalue()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def import_plugin_command(
    source: Path,
    author: str = GENERATED_AUTHOR,
) -> str:
    """Parse *source* and return a valid ``PipelineTemplate`` YAML string.

    This is the primary public entry-point.

    Args:
        source: Path to the plugin command Markdown file.
        author: Value for the ``author`` field in the generated template.
                Defaults to ``"orch import plugin-command"``.

    Returns:
        A YAML string that passes ``orch validate``.

    Raises:
        FileNotFoundError: If *source* does not exist.
        ValueError:        If the file is malformed (no H1, bad frontmatter, etc.).
    """
    if not source.exists():
        raise FileNotFoundError(f"Plugin command file not found: {source}")

    content = source.read_text(encoding="utf-8")
    parsed = _parse_document(content, source_path=source)
    template_dict = _build_template_dict(parsed, author=author)
    return _dump_template_yaml(template_dict)


def import_plugin_command_from_string(
    content: str,
    source_path: Optional[Path] = None,
    author: str = GENERATED_AUTHOR,
) -> str:
    """Parse plugin command Markdown from a string and return YAML.

    Useful for testing without touching the filesystem.

    Args:
        content:     Full Markdown text of the plugin command.
        source_path: Optional path; used only for skill-ref resolution.
        author:      Value for the ``author`` field.

    Returns:
        A YAML string that passes ``orch validate``.

    Raises:
        ValueError: If the content is malformed.
    """
    parsed = _parse_document(content, source_path=source_path)
    template_dict = _build_template_dict(parsed, author=author)
    return _dump_template_yaml(template_dict)
