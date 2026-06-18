"""Template engine — loads YAML pipeline templates and creates execution plans."""

import logging
from pathlib import Path

from ._composition import _CompositionMixin
from ._config import (  # noqa: F401  (re-exported for the package facade)
    AutoMergeConfig,
    BudgetConfig,
    LifecycleHook,
    LifecycleHooksConfig,
    OnCompleteConfig,
    OnCompleteEntry,
    _is_within_dir,
    _parse_adversary_config,
    _parse_auto_merge_config,
    _parse_budget_config,
    _parse_dialogue_config,
    _parse_git_config,
    _parse_lifecycle_hooks_config,
    _parse_on_complete_config,
)
from ._dag import _DagMixin
from ._discovery import TemplateNotFoundError, _DiscoveryMixin  # noqa: F401  (facade re-export)
from ._loader import _LoaderMixin
from ._models import PhaseDefinition, PipelineTemplate  # noqa: F401  (facade re-export)
from ._validation import _ValidationMixin

logger = logging.getLogger(__name__)


class TemplateEngine(_DiscoveryMixin, _CompositionMixin, _LoaderMixin, _DagMixin, _ValidationMixin):
    """Loads YAML templates and creates execution plans.

    Template search order (first match wins):
    1. Paths from ``ORCH_TEMPLATES_PATH`` env var (colon-separated) — prepended
    2. ``project_dir`` (default: ``./templates/``)
    3. ``user_dir``    (default: ``~/.orch/templates/``)
    4. Bundled package templates (``<package>/../../templates/``)

    Pass ``project_dir`` or ``user_dir`` to the constructor to override the
    defaults — useful in tests.
    """

    pass


def load_template(template_path: str) -> "PipelineTemplate":
    """Module-level convenience wrapper around TemplateEngine.load_template().

    Allows callers to do::

        from orchestration_engine.templates import load_template
        template = load_template("/path/to/template.yaml")

    instead of instantiating TemplateEngine explicitly.

    Args:
        template_path: Path to the YAML template file (str or Path).

    Returns:
        PipelineTemplate instance.
    """
    return TemplateEngine().load_template(Path(template_path))
