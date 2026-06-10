"""Rubric generator — parse skill markdown files, emit LLM Judge rubric YAML.

Public API:  parse_skill · generate_rubric_text · generate_yaml · generate_rubric_file
Constraints: stdlib + yaml + re only.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from .timestamps import now_utc

# ---------------------------------------------------------------------------
# Pre-compiled patterns
# ---------------------------------------------------------------------------
_FM_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_FM_KV_RE = re.compile(r"^([\w][\w-]*):\s*(.+)$", re.MULTILINE)
_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_CHECKLIST_RE = re.compile(r"^\s*-\s+\[[ xX]\]\s+(.+)$")
_BOLD_RE = re.compile(r"^\*\*(.+?)\*\*:\s*")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_BULLET_RE = re.compile(r"^\s*-\s+(?!\[[ xX\s]\])(.+)$")
_DO_RE = re.compile(r"\b(DO|ALWAYS|BEST PRACTICE)\b", re.IGNORECASE)
_DONT_RE = re.compile(r"\b(DON'?T|AVOID|NOT|NEVER)\b", re.IGNORECASE)
_SEP_RE = re.compile(r"^:?-+:?$")
_WE_ARE_RE = re.compile(r"^\s*-\s+\*\*We are\*\*:\s*(.+)$", re.IGNORECASE)
_WE_ARE_NOT_RE = re.compile(r"^\s*-\s+\*\*We are not\*\*:\s*(.+)$", re.IGNORECASE)
_ATTR_HDG_RE = re.compile(r"^\*\*[^*]+\*\*\s*$")  # **Name** on its own line


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CriteriaTable:
    """A parsed markdown criteria table (non-terminology)."""

    name: str
    columns: List[str]
    rows: List[List[str]]


@dataclass
class SkillData:
    """All structured data extracted from a SKILL.md file."""

    skill_name: str
    description: str
    source_file: str
    # AC-5: checklist items with section tagging
    checklist_items: List[Dict[str, str]] = field(default_factory=list)
    # AC-6/AC-7: DO/DON'T pairs from terminology tables and attribute blocks
    do_dont_pairs: List[Dict[str, str]] = field(default_factory=list)
    # AC-8: regular (non-terminology) criteria tables
    criteria_tables: List[CriteriaTable] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Low-level text helpers
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Return (fields, body).  Safe on malformed or absent frontmatter."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fields = {km.group(1): km.group(2).strip() for km in _FM_KV_RE.finditer(m.group(1))}
    return fields, text[m.end() :]


def _strip_code_blocks(text: str) -> str:
    """Replace code block interiors with blank lines (preserves line numbers)."""
    return _CODE_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def _clean_bold(raw: str) -> str:
    """**Label**: text  →  Label: text."""
    m = _BOLD_RE.match(raw)
    if not m:
        return raw
    rest = raw[m.end() :].strip()
    return f"{m.group(1)}: {rest}" if rest else raw


def _classify_heading(text: str) -> str:
    """Return 'do', 'dont', or 'neutral'."""
    clean = re.sub(r"[^\w '\-]", " ", text)
    if _DONT_RE.search(clean):
        return "dont"
    if _DO_RE.search(clean):
        return "do"
    return "neutral"


def _split_row(line: str) -> Optional[List[str]]:
    """Parse one markdown table row → list of cell strings, or None."""
    s = line.strip()
    if not s.startswith("|"):
        return None
    inner = s[1:-1] if s.endswith("|") else s[1:]
    return [c.strip() for c in inner.split("|")]


def _is_sep(cells: List[str]) -> bool:
    """True if every non-empty cell is a markdown separator (---, :---:, etc.)."""
    return all(_SEP_RE.match(c.replace(" ", "")) for c in cells if c)


# ---------------------------------------------------------------------------
# Extraction functions (public so tests can import them)
# ---------------------------------------------------------------------------


def _extract_checklist_items(text: str) -> List[Dict[str, str]]:
    """Return all ``- [ ]`` / ``- [x]`` items as ``{"text", "section"}`` dicts.

    AC-5: items are tagged with the most-recently-seen H2/H3 heading.
    Sub-section items (4+ space indent) are included and tagged accordingly.
    """
    items: List[Dict[str, str]] = []
    current_section = ""
    for line in text.splitlines():
        hm = _HEADING_RE.match(line)
        if hm:
            level = len(hm.group(1))
            if 2 <= level <= 3:
                current_section = hm.group(2).strip()
            continue
        m = _CHECKLIST_RE.match(line)
        if m:
            items.append(
                {
                    "text": _clean_bold(m.group(1).strip()),
                    "section": current_section,
                }
            )
    return items


def _extract_do_dont_from_sections(text: str) -> Tuple[List[str], List[str]]:
    """Return (do_items, dont_items) as flat string lists from classified headings.

    Renamed from the original ``_extract_do_dont`` to match the test import name.
    """
    do_items: List[str] = []
    dont_items: List[str] = []
    kind = "neutral"
    for line in text.splitlines():
        hm = _HEADING_RE.match(line)
        if hm:
            kind = _classify_heading(hm.group(2))
            continue
        if kind in ("do", "dont"):
            bm = _BULLET_RE.match(line)
            if bm:
                (do_items if kind == "do" else dont_items).append(bm.group(1).strip())
    return do_items, dont_items


def _extract_we_are_blocks(text: str) -> List[Dict[str, str]]:
    """Return DO/DON'T pairs extracted from ``**We are**:`` / ``**We are not**:`` blocks.

    AC-7: each returned dict has keys ``do``, ``dont``, ``source="attribute_block"``.
    Only yields a pair when both halves appear (unpaired "We are" is discarded).
    """
    pairs: List[Dict[str, str]] = []
    pending_we_are: Optional[str] = None

    for line in text.splitlines():
        stripped = line.strip()

        # Reset pending on empty line or bold attribute heading (e.g. **Approachable**)
        if not stripped or _ATTR_HDG_RE.match(stripped):
            pending_we_are = None
            continue

        m_do = _WE_ARE_RE.match(line)
        if m_do:
            pending_we_are = m_do.group(1).strip()
            continue

        m_dont = _WE_ARE_NOT_RE.match(line)
        if m_dont and pending_we_are is not None:
            pairs.append(
                {
                    "do": pending_we_are,
                    "dont": m_dont.group(1).strip(),
                    "source": "attribute_block",
                }
            )
            pending_we_are = None

    return pairs


def _extract_tables(  # noqa: C901
    text: str,
    warnings_out: List[str],
) -> Tuple[List[CriteriaTable], List[Dict[str, str]]]:
    """Parse all markdown tables.

    Returns:
        (criteria_tables, terminology_pairs)

    - Terminology tables (columns "Use This" / "Not This") are NOT added to
      ``criteria_tables``; their rows become DO/DON'T pairs with
      ``source="terminology_table"``.
    - All other tables become :class:`CriteriaTable` objects.
    """
    criteria_tables: List[CriteriaTable] = []
    terminology_pairs: List[Dict[str, str]] = []
    name_counts: Dict[str, int] = {}
    current_heading = ""
    lines = text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]
        hm = _HEADING_RE.match(line)
        if hm:
            current_heading = hm.group(2).strip()
            i += 1
            continue

        if not line.strip().startswith("|"):
            i += 1
            continue

        # Collect contiguous table rows
        raw_rows: List[str] = []
        while i < len(lines) and lines[i].strip().startswith("|"):
            raw_rows.append(lines[i])
            i += 1

        if len(raw_rows) < 2:
            continue

        try:
            header_cells = _split_row(raw_rows[0])
            if not header_cells:
                continue
            n = len(header_cells)

            # Collect data rows (skip separator rows)
            data_rows: List[List[str]] = []
            for rl in raw_rows[1:]:
                cells = _split_row(rl)
                if cells is None or _is_sep(cells):
                    continue
                if len(cells) < n:
                    cells += [""] * (n - len(cells))
                data_rows.append(cells[:n])

            if not data_rows:
                continue

            # --- Detect terminology table ("Use This" / "Not This") ---
            normalized_cols = [c.strip().lower() for c in header_cells]
            use_this_idx = next((j for j, c in enumerate(normalized_cols) if c == "use this"), None)
            not_this_idx = next((j for j, c in enumerate(normalized_cols) if c == "not this"), None)

            if use_this_idx is not None and not_this_idx is not None:
                for row in data_rows:
                    do_val = row[use_this_idx] if use_this_idx < len(row) else ""
                    dont_val = row[not_this_idx] if not_this_idx < len(row) else ""
                    if do_val or dont_val:
                        terminology_pairs.append(
                            {
                                "do": do_val,
                                "dont": dont_val,
                                "source": "terminology_table",
                            }
                        )
                continue  # terminology table → not added to criteria_tables

            # --- Regular criteria table ---
            base = current_heading or "Criteria"
            cnt = name_counts.get(base, 0) + 1
            name_counts[base] = cnt
            name = base if cnt == 1 else f"{base} ({cnt})"
            criteria_tables.append(CriteriaTable(name=name, columns=header_cells, rows=data_rows))

        except Exception as exc:  # pragma: no cover  # noqa: BLE001
            warnings_out.append(f"Skipping malformed table near '{current_heading}': {exc}")

    return criteria_tables, terminology_pairs


# ---------------------------------------------------------------------------
# build_criteria_list — AC-10 helper (public for tests)
# ---------------------------------------------------------------------------


def _build_criteria_list(data: SkillData) -> List[Dict]:
    """Merge all extracted checks into a single unified ``criteria`` list.

    Each item has a ``type`` key:
    * ``"checklist"``  — from ``data.checklist_items``
    * ``"do_dont"``    — from ``data.do_dont_pairs``
    * ``"table_row"``  — from ``data.criteria_tables``
    """
    criteria: List[Dict] = []

    for item in data.checklist_items:
        criteria.append(  # noqa: PERF401
            {
                "type": "checklist",
                "text": item["text"],
                "section": item.get("section", ""),
            }
        )

    for pair in data.do_dont_pairs:
        criteria.append(  # noqa: PERF401
            {
                "type": "do_dont",
                "do": pair.get("do", ""),
                "dont": pair.get("dont", ""),
                "source": pair.get("source", ""),
            }
        )

    for tbl in data.criteria_tables:
        for row in tbl.rows:
            criteria.append(  # noqa: PERF401
                {
                    "type": "table_row",
                    "table": tbl.name,
                    "values": dict(zip(tbl.columns, row)),
                }
            )

    return criteria


# ---------------------------------------------------------------------------
# parse_skill — main entry point
# ---------------------------------------------------------------------------


def parse_skill(source_path: Path) -> SkillData:
    """Parse a skill markdown file and return structured :class:`SkillData`.

    Raises:
        ValueError: if the file is unreadable or empty.
    """
    path = Path(source_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except PermissionError as exc:
        raise ValueError(f"Cannot read file: {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"Cannot read file (encoding error): {path}: {exc}") from exc

    if not raw.strip():
        raise ValueError(f"Skill file is empty: {path}")

    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    warns: List[str] = []

    frontmatter, body = _parse_frontmatter(text)

    if "name" in frontmatter:
        skill_name = frontmatter["name"].strip()
    else:
        skill_name = path.stem
        warns.append(f"No 'name' in frontmatter; derived from filename: '{skill_name}'")

    clean = _strip_code_blocks(body)

    criteria_tables, terminology_pairs = _extract_tables(clean, warns)
    attribute_pairs = _extract_we_are_blocks(clean)
    do_dont_pairs = terminology_pairs + attribute_pairs

    return SkillData(
        skill_name=skill_name,
        description=frontmatter.get("description", "").strip(),
        source_file=str(path.resolve()),
        checklist_items=_extract_checklist_items(clean),
        do_dont_pairs=do_dont_pairs,
        criteria_tables=criteria_tables,
        warnings=warns,
    )


# ---------------------------------------------------------------------------
# Rubric text generation (AC-9)
# ---------------------------------------------------------------------------


def _make_scale(data: SkillData) -> str:
    """Generate 6-band scoring scale text."""
    if data.checklist_items:
        n = len(data.checklist_items)
        p90 = max(1, round(n * 0.9))
        p60 = max(1, round(n * 0.6))
        half = max(1, n // 2)
        bands = [
            f"**1.0 — Excellent:** All {n} checklist items satisfied. No quality gaps detected.",
            f"**0.8 — Good:** At least {p90} of {n} items satisfied. Minor gaps; core conclusions unaffected.",  # noqa: E501
            "**0.6 — Acceptable:** Most major checks pass. 1–2 significant items missed.",
            f"**0.4 — Poor:** {p60} or fewer items satisfied. Multiple significant checks missing.",
            f"**0.2 — Very Poor:** Fewer than {half} items satisfied. High risk of material errors.",  # noqa: E501
            "**0.0 — Unacceptable:** Critical quality checks ignored. Content should not be shared.",  # noqa: E501
        ]
    elif data.criteria_tables:
        names = ", ".join(t.name for t in data.criteria_tables[:2])
        bands = [
            f"**1.0 — Excellent:** Fully satisfies all criteria in {names}.",
            "**0.8 — Good:** Satisfies the large majority of criteria. Minor gaps only.",
            "**0.6 — Acceptable:** Satisfies most criteria but misses 1–2 notable items.",
            "**0.4 — Poor:** Multiple criteria unmet. Quality noticeably below expectations.",
            "**0.2 — Very Poor:** Few criteria satisfied. Significant rework required.",
            "**0.0 — Unacceptable:** Does not meet the criteria. Should not be delivered.",
        ]
    else:
        bands = [
            "**1.0 — Excellent:** Fully meets all quality expectations.",
            "**0.8 — Good:** Mostly meets expectations; minor issues only.",
            "**0.6 — Acceptable:** Partially meets expectations; some gaps.",
            "**0.4 — Poor:** Significant quality gaps present.",
            "**0.2 — Very Poor:** Barely meets minimum expectations.",
            "**0.0 — Unacceptable:** Does not meet quality expectations.",
        ]
    return "\n\n".join(f"- {b}" for b in bands)


def generate_rubric_text(data: SkillData) -> str:
    """Render SkillData into a rubric markdown string for LLMJudgeGrader.

    AC-9: Contains four sections — Preamble, Scoring Scale, Specific Checks,
    Output Format.  ``Score: [0.0-1.0]`` appears verbatim so that
    ``LLMJudgeGrader._SCORE_RE`` can match it.
    """
    pretty = data.skill_name.replace("-", " ").replace("_", " ").title()
    parts: List[str] = [f"# {pretty} Quality Rubric", ""]

    # --- Preamble ---
    if data.description:
        parts += [
            f"You are evaluating content quality against the {pretty} skill.",
            "",
            data.description,
        ]
    else:
        parts.append(f"You are evaluating content quality against the {pretty} skill checklist.")

    # --- Scoring Scale ---
    parts += ["", "## Scoring Scale (0.0 to 1.0)", "", _make_scale(data), ""]

    # --- Specific Checks ---
    parts += ["## Specific Checks", ""]

    has_checks = data.checklist_items or data.do_dont_pairs or data.criteria_tables

    if data.checklist_items:
        parts += ["### Checklist Items", ""]
        for i, item in enumerate(data.checklist_items, 1):
            section_tag = f" [{item['section']}]" if item.get("section") else ""
            parts.append(f"{i}. {item['text']}{section_tag}")
        parts.append("")

    if data.do_dont_pairs:
        parts += ["### DO / DON'T Guidelines", ""]
        for pair in data.do_dont_pairs:
            parts.append(f"- ✓ **DO:** {pair['do']}")
            parts.append(f"  ✗ **DON'T:** {pair['dont']}")
        parts.append("")

    if data.criteria_tables:
        parts += ["### Criteria Tables", ""]
        for tbl in data.criteria_tables:
            parts += [f"#### {tbl.name}", ""]
            parts.append("| " + " | ".join(tbl.columns) + " |")
            parts.append("| " + " | ".join("---" for _ in tbl.columns) + " |")
            for row in tbl.rows:
                cells = [(c[:150] + "…") if len(c) > 200 else c for c in row]
                parts.append("| " + " | ".join(cells) + " |")
            parts.append("")

    if not has_checks:
        parts += [
            "**Note:** No specific checklist items or criteria tables were found.",
            "Apply your best judgment based on the skill description above.",
            "",
        ]

    # --- Output Format ---
    parts += [
        "## Output Format",
        "",
        "Score: [0.0-1.0]",
        "Reasoning: [2-3 sentences explaining the score]",
        'Failed checks: [list each failed checklist item, or "None"]',
    ]

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# YAML generation (AC-10)
# ---------------------------------------------------------------------------


def generate_yaml(data: SkillData) -> str:
    """Render SkillData as a YAML string with the AC-10 required keys.

    Top-level keys: ``name``, ``generated_from``, ``generated_at``,
    ``rubric``, ``criteria``.
    """
    rubric_text = generate_rubric_text(data)
    doc = {
        "name": data.skill_name,
        "generated_from": data.source_file,
        "generated_at": now_utc().isoformat(timespec="seconds"),
        "rubric": rubric_text,
        "criteria": _build_criteria_list(data),
    }
    header = (
        "# Generated by: orch rubric generate\n"
        f"# Source: {data.source_file}\n"
        "# Compatible with: LLMJudgeGrader\n\n"
    )
    return header + yaml.dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------------
# File I/O (AC-2/3/4)
# ---------------------------------------------------------------------------


def generate_rubric_file(
    skill_file: Path,
    output: Optional[Path] = None,
    force: bool = False,
) -> Path:
    """Parse a skill file and write rubric YAML to disk.

    Returns:
        Path of the written output file.

    Raises:
        ValueError: on any validation or I/O error.
    """
    skill_path = Path(skill_file).resolve()
    if not skill_path.exists():
        raise ValueError(f"File not found: {skill_file}")
    if skill_path.is_dir():
        raise ValueError(f"Expected a file, got a directory: {skill_file}")

    data = parse_skill(skill_path)

    for w in data.warnings:
        print(f"⚠ {w}", file=sys.stderr)

    if not data.checklist_items and not data.criteria_tables:
        print(
            "⚠ No checklist items or criteria tables found; "
            "generated rubric uses generic scoring.",
            file=sys.stderr,
        )

    if output is None:
        output = Path(f"{data.skill_name}-rubric.yaml")

    out_path = Path(output)
    if out_path.is_dir():
        raise ValueError(f"Output path is a directory: {output}")
    if out_path.exists() and not force:
        raise ValueError(f"Output already exists: {out_path} (use --force to overwrite)")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(generate_yaml(data), encoding="utf-8")
    return out_path
