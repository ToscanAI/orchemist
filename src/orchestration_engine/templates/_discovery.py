"""Template discovery — search paths, name resolution, and listing."""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class TemplateNotFoundError(FileNotFoundError):
    """Raised when a template name cannot be resolved in any search path."""

    def __init__(self, name: str, searched: Optional[List[Path]] = None) -> None:
        self.name = name
        self.searched = searched or []
        paths_str = ", ".join(str(p) for p in self.searched)
        super().__init__(f"Template '{name}' not found. Searched: [{paths_str}]")


class _DiscoveryMixin:
    """Search-path resolution and template listing for :class:`TemplateEngine`."""

    _SOURCE_CUSTOM = "custom"
    _SOURCE_PROJECT = "project"
    _SOURCE_USER = "user"
    _SOURCE_BUNDLED = "bundled"

    def __init__(
        self,
        templates_dir: Optional[Path] = None,
        project_dir: Optional[Path] = None,
        user_dir: Optional[Path] = None,
    ) -> None:
        # --- backward-compat: templates_dir sets the project dir -----------
        if templates_dir is not None:
            # Existing callers that pass templates_dir= still work.
            self._project_dir: Path = templates_dir
        else:
            self._project_dir = project_dir if project_dir is not None else Path.cwd() / "templates"

        self._user_dir: Path = (
            user_dir if user_dir is not None else Path.home() / ".orch" / "templates"
        )

        # Package-bundled templates live three levels up from this file:
        # src/orchestration_engine/templates/ → src/orchestration_engine/ →
        # src/ → repo-root/ → templates/
        self._bundled_dir: Path = Path(__file__).parent.parent.parent.parent / "templates"

        # Keep the old attribute for code that accessed engine.templates_dir
        self.templates_dir = self._project_dir

    # ------------------------------------------------------------------
    # Search-path helpers
    # ------------------------------------------------------------------

    def get_search_paths(self) -> List[Tuple[Path, str]]:
        """Return the ordered list of ``(path, source_label)`` pairs.

        Order:
        1. Paths from ``ORCH_TEMPLATES_PATH`` (labelled "custom")
        2. Project-local   (labelled "project")
        3. User-global     (labelled "user")
        4. Bundled          (labelled "bundled")
        """
        paths: List[Tuple[Path, str]] = []

        # 1. ORCH_TEMPLATES_PATH
        env_raw = os.environ.get("ORCH_TEMPLATES_PATH", "")
        if env_raw:
            for part in env_raw.split(":"):
                part = part.strip()
                if part:
                    paths.append((Path(part), self._SOURCE_CUSTOM))

        # 2. Project-local
        paths.append((self._project_dir, self._SOURCE_PROJECT))

        # 3. User-global
        paths.append((self._user_dir, self._SOURCE_USER))

        # 4. Bundled
        paths.append((self._bundled_dir, self._SOURCE_BUNDLED))

        return paths

    # ------------------------------------------------------------------
    # Name-based resolution
    # ------------------------------------------------------------------

    def resolve_template(self, name: str) -> Path:  # noqa: C901
        """Resolve a template *name* to an absolute :class:`Path`.

        Searches ``get_search_paths()`` in order.  The *name* is matched
        against ``<stem>.yaml`` and ``<stem>.yml`` files in each directory.

        Args:
            name: Bare template name (e.g. ``"content-pipeline"``).
                  ``.yaml`` / ``.yml`` extensions are stripped before matching.

        Returns:
            Absolute :class:`Path` to the first matching file.

        Raises:
            ValueError: If *name* contains path separators or ``..`` (path
                        traversal attempt).
            TemplateNotFoundError: When no match is found in any directory.
        """
        # Security: reject path traversal attempts before touching the filesystem
        if os.sep in name or "/" in name or "\\" in name or ".." in name:
            raise ValueError(f"Template name must not contain path separators or '..': {name!r}")

        # Strip extension so callers can pass "foo.yaml" or just "foo"
        stem = Path(name).stem if name.endswith((".yaml", ".yml")) else name

        searched: List[Path] = []
        for directory, _label in self.get_search_paths():
            if not directory.exists():
                searched.append(directory)
                continue
            for ext in (".yaml", ".yml"):
                candidate = directory / f"{stem}{ext}"
                if candidate.exists():
                    logger.debug("resolve_template(%r) → %s", name, candidate)
                    return candidate.resolve()
            searched.append(directory)

        # --- ID-based fallback: scan YAML files and match by id field -----
        for directory, _label in self.get_search_paths():
            if not directory.exists():
                continue
            for filepath in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
                try:
                    tpl = self.load_template(filepath)
                    if tpl.id == stem:
                        logger.debug(
                            "resolve_template(%r) → %s (matched by id)",
                            name,
                            filepath,
                        )
                        return filepath.resolve()
                except Exception:  # noqa: BLE001, PERF203
                    continue

        raise TemplateNotFoundError(name, searched)

    # ------------------------------------------------------------------
    # Template listing
    # ------------------------------------------------------------------

    def list_templates(self) -> List[Dict[str, Any]]:
        """Return all discoverable templates with metadata.

        Scans every directory in ``get_search_paths()``.  Each entry is a
        ``dict`` with the keys:

        * ``name``      — template display name
        * ``id``        — template id
        * ``version``   — template version string
        * ``phases``    — number of phases (int)
        * ``description`` — template description
        * ``source``    — source label (project / user / bundled / custom)
        * ``path``      — absolute path as ``str``

        A template file is included **only once** — the first time it is
        encountered (first-wins rule mirrors ``resolve_template``).  Deduplication
        is performed by **template id** (not filename stem), so two files with the
        same ``id`` field but different names are correctly treated as the same
        logical template.  Files in later directories with the same id are silently
        skipped (custom > project > user > bundled precedence order).

        Templates with a ``null`` or empty ``id`` field are skipped with a
        ``WARNING``-level log message.  Intra-directory duplicates (same ``id``
        in the same directory) are also silently skipped; alphabetical filename
        ordering determines which entry wins.
        """
        results: List[Dict[str, Any]] = []
        seen_ids: Dict[str, str] = {}  # template id → first source label

        for directory, source_label in self.get_search_paths():
            if not directory.exists():
                continue
            for filepath in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
                try:
                    template = self.load_template(filepath)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("list_templates: skipping %s — %s", filepath, exc)
                    continue

                if not template.id:
                    logger.warning(
                        "list_templates: skipping %s — template id is null or empty",
                        filepath,
                    )
                    continue

                if template.id in seen_ids:
                    logger.debug(
                        "list_templates: skipping %s (id %r shadowed by %s)",
                        filepath,
                        template.id,
                        seen_ids[template.id],
                    )
                    continue

                seen_ids[template.id] = source_label
                results.append(
                    {
                        "name": template.name,
                        "id": template.id,
                        "version": template.version,
                        "phases": len(template.phases),
                        "description": template.description,
                        "source": source_label,
                        "path": str(filepath.resolve()),
                    }
                )

        return results
