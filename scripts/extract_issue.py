#!/usr/bin/env python3
"""
extract_issue.py — Extract behavioral contracts and spec from a GitHub issue body.

Usage:
    python3 scripts/extract_issue.py --issue 540 --repo ToscanAI/orchemist --output-dir /tmp/pipeline-540/

Reads a GitHub issue body via `gh issue view --json body`, splits it into sections
using the standardized feature request template structure, and writes spec.md and
behavioral.md to the output directory. Zero LLM involvement — deterministic extraction.

Section-to-file mapping:
  ## User Story          -> spec.md
  ## Context             -> spec.md
  ## Behavioral Contracts -> behavioral.md
  ## Integration points  -> spec.md
  ## Acceptance Criteria -> behavioral.md
  ## <unknown>           -> spec.md (default, with warning)

Behavioral rules:
  - Section headers are included in output (verbatim)
  - Subsections (### ...) inherit parent section's mapping
  - HTML comments are stripped (except inside code blocks)
  - CRLF normalized to LF
  - Code-fence-aware parsing (## inside fences not treated as headers)
  - Missing Behavioral Contracts section -> exit 1 (fatal)
  - Behavioral Contracts section with <3 non-empty lines -> exit 1 (placeholder)
  - Missing Context section -> warning only, exit 0
  - Unknown sections -> spec.md with warning
  - Duplicate sections -> concatenated with warning
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Section-to-file mapping (lowercase keys for case-insensitive matching)
SECTION_MAP = {
    "user story": "spec.md",
    "context": "spec.md",
    "integration points": "spec.md",
    "behavioral contracts": "behavioral.md",
    "acceptance criteria": "behavioral.md",
}

# Sections that, if missing, cause a fatal error (exit 1, no files written)
REQUIRED_SECTIONS = {"behavioral contracts"}

# Sections that, if missing, produce a warning only (exit 0)
WARN_MISSING_SECTIONS = {"context"}


# ─────────────────────────────────────────────────────────────────────────────
# ARGUMENT VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def validate_issue_number(value: str) -> int:
    """Validate and return issue number as int. Exits 1 on failure."""
    try:
        n = int(value)
    except (ValueError, TypeError):
        print(f"Error: invalid issue number: {value!r}", file=sys.stderr)
        sys.exit(1)
    # Must be a positive integer (GitHub issues start at 1)
    if n <= 0 or str(n) != value.strip():
        print(f"Error: invalid issue number: {value!r}", file=sys.stderr)
        sys.exit(1)
    return n


def validate_repo_format(value: str) -> str:
    """Validate owner/repo format. Exits 1 on failure."""
    pattern = r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$'
    if not re.match(pattern, value):
        print(f"Error: invalid repo format: {value!r} (expected owner/repo)", file=sys.stderr)
        sys.exit(1)
    return value


# ─────────────────────────────────────────────────────────────────────────────
# GH CLI INVOCATION
# ─────────────────────────────────────────────────────────────────────────────

def fetch_issue_body(issue_number: int, repo: str) -> str:
    """
    Fetch issue body via `gh issue view --json body`.
    Returns the body string.
    Exits 1 if gh fails or body is null/empty.
    """
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "body"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("Error: `gh` CLI not found. Install GitHub CLI and ensure it is in PATH.", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        # Propagate gh's stderr
        err = result.stderr.strip() or result.stdout.strip()
        print(err, file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"Error: could not parse gh output as JSON: {e}", file=sys.stderr)
        sys.exit(1)

    body = data.get("body")
    if body is None or body == "":
        print("Error: empty issue body", file=sys.stderr)
        sys.exit(1)

    return body


# ─────────────────────────────────────────────────────────────────────────────
# HTML COMMENT STRIPPING (code-fence-aware)
# ─────────────────────────────────────────────────────────────────────────────

def strip_html_comments(text: str) -> str:
    """
    Strip HTML comments (<!-- ... -->) from text, but NOT inside code fences.
    Multi-line comments are handled. Code-fence-aware.
    """
    lines = text.split("\n")
    result_lines = []
    in_fence = False
    fence_char_count = 0

    i = 0
    while i < len(lines):
        line = lines[i]

        if not in_fence:
            # Check if this line opens a code fence
            fence_match = re.match(r'^(`{3,}|~{3,})', line)
            if fence_match:
                in_fence = True
                fence_char_count = len(fence_match.group(1))
                result_lines.append(line)
                i += 1
                continue

            # Not in fence: strip HTML comments from this line
            # We need to handle multi-line comments
            result_lines.append(line)
            i += 1
        else:
            # Inside a code fence: check if this line closes it
            close_match = re.match(r'^(`{3,}|~{3,})\s*$', line)
            if close_match and len(close_match.group(1)) >= fence_char_count:
                in_fence = False
                fence_char_count = 0
            result_lines.append(line)
            i += 1

    # Now strip HTML comments from the non-fence parts
    # We need to rebuild with fence awareness
    # Better approach: process the whole text with regex but preserve fences

    return _strip_comments_preserving_fences(text)


def _strip_comments_preserving_fences(text: str) -> str:
    """
    Strip HTML comments from text while preserving code fence content.
    Uses a state-machine approach.
    """
    result = []
    i = 0
    n = len(text)
    in_fence = False
    fence_marker = None  # the fence characters (e.g. "```" or "````")

    while i < n:
        # Check if we're at a line start (i == 0 or previous char was \n)
        at_line_start = (i == 0 or text[i - 1] == "\n")

        if at_line_start and not in_fence:
            # Check for fence open
            m = re.match(r'(`{3,}|~{3,})', text[i:])
            if m:
                in_fence = True
                fence_marker = m.group(1)
                # Copy this line verbatim
                end = text.find("\n", i)
                if end == -1:
                    result.append(text[i:])
                    i = n
                else:
                    result.append(text[i:end + 1])
                    i = end + 1
                continue

        if at_line_start and in_fence:
            # Check for fence close
            m = re.match(r'(`{3,}|~{3,})\s*(\n|$)', text[i:])
            if m and len(m.group(1)) >= len(fence_marker):
                in_fence = False
                fence_marker = None
                # Copy this line verbatim
                end = text.find("\n", i)
                if end == -1:
                    result.append(text[i:])
                    i = n
                else:
                    result.append(text[i:end + 1])
                    i = end + 1
                continue

        if in_fence:
            # Inside fence: copy char verbatim
            result.append(text[i])
            i += 1
            continue

        # Not in fence: look for HTML comment start
        if text[i:i + 4] == "<!--":
            # Find comment end
            end = text.find("-->", i + 4)
            if end == -1:
                # Unclosed comment: remove to end of text
                i = n
            else:
                # Skip the comment
                i = end + 3
            continue

        result.append(text[i])
        i += 1

    return "".join(result)


# ─────────────────────────────────────────────────────────────────────────────
# MARKDOWN SECTION PARSER (code-fence-aware)
# ─────────────────────────────────────────────────────────────────────────────

def parse_sections(text: str) -> list[tuple[str | None, str]]:
    """
    Parse markdown text into sections.

    Returns a list of (header, content) tuples where:
    - header is None for preamble (text before first ## header)
    - header is the full header line (e.g. "## User Story") for sections
    - content is the section body (including trailing newline, not including header line)

    Only ## (h2) headers create new sections. ### and deeper are subsections.
    Code-fence-aware: ## patterns inside code fences are NOT treated as headers.
    """
    lines = text.split("\n")
    sections = []
    current_header = None
    current_lines = []
    in_fence = False
    fence_char_count = 0

    for line in lines:
        if not in_fence:
            # Check for fence open
            fence_match = re.match(r'^(`{3,}|~{3,})', line)
            if fence_match:
                in_fence = True
                fence_char_count = len(fence_match.group(1))
                current_lines.append(line)
                continue

            # Check for h2 header (but NOT h3+)
            h2_match = re.match(r'^## (?!#)', line)
            if h2_match:
                # Save current section
                sections.append((current_header, "\n".join(current_lines)))
                current_header = line
                current_lines = []
                continue

            current_lines.append(line)
        else:
            # Inside fence: check for close
            close_match = re.match(r'^(`{3,}|~{3,})\s*$', line)
            if close_match and len(close_match.group(1)) >= fence_char_count:
                in_fence = False
                fence_char_count = 0
            current_lines.append(line)

    # Don't forget the last section
    sections.append((current_header, "\n".join(current_lines)))

    return sections


# ─────────────────────────────────────────────────────────────────────────────
# SECTION ROUTING
# ─────────────────────────────────────────────────────────────────────────────

def get_section_name(header: str) -> str:
    """Extract section name from header line (e.g. '## User Story' -> 'User Story')."""
    return header.lstrip("#").strip()


def route_section(section_name: str) -> str:
    """Return target filename for a section name (case-insensitive)."""
    return SECTION_MAP.get(section_name.lower(), "spec.md")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN EXTRACTION LOGIC
# ─────────────────────────────────────────────────────────────────────────────

def extract_and_write(body: str, output_dir: Path) -> int:
    """
    Parse body, route sections, write output files.
    Returns exit code (0 on success, 1 on fatal error).
    """
    # Normalize CRLF -> LF
    body = body.replace("\r\n", "\n").replace("\r", "\n")

    # Strip HTML comments (preserving code blocks)
    body = _strip_comments_preserving_fences(body)

    # Parse sections
    raw_sections = parse_sections(body)

    # Track seen section names for duplicate detection
    seen_sections: dict[str, int] = {}  # lowercase name -> count

    # Files: spec.md and behavioral.md
    file_contents: dict[str, list[str]] = {"spec.md": [], "behavioral.md": []}

    # Warnings to print later (after validation, before writing)
    warnings: list[str] = []

    # Track which known sections we've seen
    known_section_names_seen: set[str] = set()

    # Current parent section (for ### subsection tracking)
    # Note: subsections are already included in parent's content by parse_sections
    # (since we only split on ##, not ###)

    # Process preamble (first section with header=None)
    for i, (header, content) in enumerate(raw_sections):
        if header is None:
            # Preamble: goes to spec.md
            preamble_stripped = content.strip()
            if preamble_stripped:
                file_contents["spec.md"].append(preamble_stripped)
        else:
            section_name = get_section_name(header)
            section_key = section_name.lower()

            # Check for duplicates
            if section_key in seen_sections:
                seen_sections[section_key] += 1
                warnings.append(f"Warning: duplicate section '{section_name}'")
            else:
                seen_sections[section_key] = 1

            # Determine target file
            target_file = route_section(section_name)
            if section_key not in SECTION_MAP:
                warnings.append(f"Warning: unknown section '{section_name}' — routing to spec.md")
            else:
                known_section_names_seen.add(section_key)

            # Build section text: header + content
            section_text = header
            if content.strip():
                # Preserve content as-is (content already has leading \n from split)
                # content starts with "" (empty) since we split at the header line
                # We need to include the body lines
                # content is the lines AFTER the header, joined with \n
                # Strip only trailing blank lines, preserve internal structure
                section_text = header + "\n" + content.rstrip("\n")
            else:
                section_text = header

            file_contents[target_file].append(section_text)

    # ─── Validation ───────────────────────────────────────────────────────────

    # Check required sections
    for req in REQUIRED_SECTIONS:
        if req not in known_section_names_seen and req not in seen_sections:
            print(f"Error: missing Behavioral Contracts section", file=sys.stderr)
            return 1

    # Check if behavioral contracts section is a placeholder (< 3 non-empty lines)
    # Find the behavioral contracts content
    behavioral_content = "\n".join(file_contents["behavioral.md"])
    # Count non-empty lines in behavioral contracts section(s)
    behavioral_lines = [l for l in behavioral_content.split("\n") if l.strip()]
    # Subtract header lines (lines starting with ## or ###)
    non_header_lines = [l for l in behavioral_lines if not l.startswith("#")]

    # Check if behavioral contracts section itself exists and count its content lines
    # We need to count lines specifically in the behavioral contracts section(s)
    behavioral_section_content_lines = []
    for header, content in raw_sections:
        if header is not None:
            section_name = get_section_name(header)
            if section_name.lower() == "behavioral contracts":
                lines = [l for l in content.split("\n") if l.strip()]
                behavioral_section_content_lines.extend(lines)

    if "behavioral contracts" in seen_sections and len(behavioral_section_content_lines) < 3:
        print(
            "Error: Behavioral Contracts section appears to be a placeholder (fewer than 3 non-empty lines)",
            file=sys.stderr,
        )
        return 1

    # ─── Warnings for missing non-required sections ───────────────────────────
    for warn_section in WARN_MISSING_SECTIONS:
        if warn_section not in known_section_names_seen and warn_section not in seen_sections:
            print(f"Warning: missing Context section", file=sys.stderr)

    # Print all accumulated warnings
    for w in warnings:
        print(w, file=sys.stderr)

    # ─── Write output files ───────────────────────────────────────────────────

    # Ensure output directory exists
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, NotADirectoryError) as e:
        print(f"Error: cannot create output directory: {e}", file=sys.stderr)
        return 1

    # Check it's actually a directory
    if not output_dir.is_dir():
        print(f"Error: cannot create output directory: {output_dir} is not a directory", file=sys.stderr)
        return 1

    for filename, sections in file_contents.items():
        if sections:
            # Join sections with a single blank line between them
            content = "\n\n".join(sections)
            # Ensure single trailing newline
            content = content.rstrip("\n") + "\n"
            (output_dir / filename).write_text(content, encoding="utf-8")
        else:
            # Write empty file with just newline? Or skip?
            # For spec.md, write empty; for behavioral.md this shouldn't happen
            # (we'd have exited already if behavioral contracts was missing)
            (output_dir / filename).write_text("\n", encoding="utf-8")

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT DIR VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def validate_output_dir(path_str: str) -> Path:
    """
    Validate output directory path.
    If it already exists as a file (not dir), prints error and exits 1.
    Returns a Path object (directory may or may not exist yet).
    """
    p = Path(path_str)
    if p.exists() and not p.is_dir():
        print(f"Error: cannot create output directory: {path_str} already exists as a file", file=sys.stderr)
        sys.exit(1)
    return p


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract spec.md and behavioral.md from a GitHub issue body.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--issue", required=True, help="GitHub issue number (positive integer)")
    parser.add_argument("--repo", required=True, help="GitHub repository in owner/repo format")
    parser.add_argument("--output-dir", required=True, help="Directory to write spec.md and behavioral.md")

    args = parser.parse_args()

    # Validate inputs
    issue_number = validate_issue_number(args.issue)
    repo = validate_repo_format(args.repo)
    output_dir = validate_output_dir(args.output_dir)

    # Fetch issue body
    body = fetch_issue_body(issue_number, repo)

    # Extract and write
    exit_code = extract_and_write(body, output_dir)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
