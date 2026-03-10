"""Community template index — search and discovery.

Provides :class:`TemplateIndex` and :class:`TemplateEntry` for fetching,
caching, and searching community-contributed pipeline templates.

Usage::

    index = TemplateIndex()
    index.load_remote("https://raw.githubusercontent.com/.../index.yaml")
    results = index.search("content")
    print(index.format_results(results))
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from urllib import request as urllib_request
from urllib.error import URLError

import yaml


# Default URL for the community template index
DEFAULT_INDEX_URL = (
    "https://raw.githubusercontent.com/ToscanAI/orchestration-engine/main/"
    "community-templates/index.yaml"
)

# Default cache location
DEFAULT_CACHE_PATH = Path.home() / ".orch" / "cache" / "template-index.yaml"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TemplateEntry:
    """One entry in the community template index."""

    name: str
    description: str
    author: str
    repo_url: str
    version: str
    category: str
    tags: List[str] = field(default_factory=list)
    install_command: str = ""

    # ---- helpers -----------------------------------------------------------

    def matches(self, query: str) -> bool:
        """Return True if *query* appears (case-insensitively) in any searchable field."""
        q = query.lower()
        haystack = " ".join(
            [
                self.name,
                self.description,
                self.author,
                self.category,
                " ".join(self.tags),
            ]
        ).lower()
        return q in haystack

    @classmethod
    def from_dict(cls, data: dict) -> "TemplateEntry":
        """Construct a :class:`TemplateEntry` from a raw YAML dict."""
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            author=data.get("author", ""),
            repo_url=data.get("repo_url", ""),
            version=data.get("version", "1.0.0"),
            category=data.get("category", ""),
            tags=list(data.get("tags") or []),
            install_command=data.get("install_command", ""),
        )

    def to_dict(self) -> dict:
        """Serialise to a plain dict (for YAML round-trip)."""
        return {
            "name": self.name,
            "description": self.description,
            "author": self.author,
            "repo_url": self.repo_url,
            "version": self.version,
            "category": self.category,
            "tags": self.tags,
            "install_command": self.install_command,
        }


# ---------------------------------------------------------------------------
# Index class
# ---------------------------------------------------------------------------

class TemplateIndex:
    """Container for a collection of :class:`TemplateEntry` objects.

    Supports remote fetch, local cache, search, and formatted display.
    """

    def __init__(self) -> None:
        self.entries: List[TemplateEntry] = []

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_remote(self, url: str) -> None:
        """Fetch and parse a YAML index from *url* (HTTP/HTTPS).

        Raises :class:`urllib.error.URLError` on network errors or
        :class:`ValueError` on malformed YAML / unexpected structure.
        """
        try:
            with urllib_request.urlopen(url, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
        except URLError as exc:
            raise URLError(f"Failed to fetch template index from {url!r}: {exc}") from exc

        self._parse_yaml_string(raw)

    def load_local(self, path) -> None:
        """Load and parse a YAML index from a local *path* (str or Path)."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Template index not found: {path}")
        raw = path.read_text(encoding="utf-8")
        self._parse_yaml_string(raw)

    def load_from_string(self, yaml_string: str) -> None:
        """Parse a YAML index from a raw string (useful for testing)."""
        self._parse_yaml_string(yaml_string)

    def _parse_yaml_string(self, raw: str) -> None:
        """Internal: parse YAML and populate :attr:`entries`."""
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ValueError(f"Failed to parse template index YAML: {exc}") from exc

        if data is None:
            self.entries = []
            return

        # Support both a bare list and a dict with a "templates" key
        if isinstance(data, list):
            raw_entries = data
        elif isinstance(data, dict):
            raw_entries = data.get("templates", []) or []
        else:
            raise ValueError(
                f"Unexpected template index format: expected a list or dict, got {type(data).__name__}"
            )

        self.entries = [TemplateEntry.from_dict(e) for e in raw_entries if isinstance(e, dict)]

    # ------------------------------------------------------------------
    # Caching
    # ------------------------------------------------------------------

    def save_cache(self, path=None) -> None:
        """Serialise entries to *path* (default: ``~/.orch/cache/template-index.yaml``)."""
        cache_path = Path(path) if path is not None else DEFAULT_CACHE_PATH
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"templates": [e.to_dict() for e in self.entries]}
        cache_path.write_text(yaml.dump(payload, allow_unicode=True), encoding="utf-8")

    @staticmethod
    def is_cache_fresh(path=None, ttl_hours: float = 24) -> bool:
        """Return True if *path* exists and its mtime is within *ttl_hours*."""
        cache_path = Path(path) if path is not None else DEFAULT_CACHE_PATH
        if not cache_path.exists():
            return False
        age_seconds = time.time() - cache_path.stat().st_mtime
        return age_seconds < ttl_hours * 3600

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str) -> List[TemplateEntry]:
        """Return entries matching *query* (case-insensitive substring).

        If *query* is empty or whitespace, return all entries.
        """
        if not query or not query.strip():
            return list(self.entries)
        return [e for e in self.entries if e.matches(query.strip())]

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def format_results(self, entries: List[TemplateEntry]) -> str:
        """Return a human-readable string listing *entries*.

        Each entry is rendered as a compact block::

            ┌─ name (vX.Y.Z)  [category]
            │  Description text
            │  Author: …   Tags: …
            └  Install: orch templates install …
        """
        if not entries:
            return "No matching templates found."

        lines: List[str] = []
        for entry in entries:
            tags_str = ", ".join(entry.tags) if entry.tags else "—"
            install = entry.install_command or f"orch templates install {entry.name}"
            lines.append(
                f"┌─ {entry.name} (v{entry.version})  [{entry.category}]\n"
                f"│  {entry.description}\n"
                f"│  Author: {entry.author}   Tags: {tags_str}\n"
                f"└  Install: {install}\n"
            )
        return "\n".join(lines)
