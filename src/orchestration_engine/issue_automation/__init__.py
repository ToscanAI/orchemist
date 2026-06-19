"""IssueClassifier — LLM-based GitHub issue classification (Issue #5.1.1).

Classifies a GitHub issue into one of six categories using Claude Haiku
(fast, cheap) and persists the result to the ``issue_pipeline_map`` DB table.

Categories:
    ``bug``       — defects, errors, unexpected behaviour.
    ``feature``   — new functionality, enhancements.
    ``docs``      — documentation-only changes (README, docstrings, wiki).
    ``refactor``  — code quality / structure improvements, no behaviour change.
    ``research``  — investigation, spike, or feasibility study.
    ``content``   — blog posts, articles, marketing copy, non-code writing.

The classification result is available as an :class:`IssueClassification`
dataclass.  The :class:`IssueClassifier` class exposes a single
:meth:`~IssueClassifier.classify` method that builds the prompt, calls the
LLM, parses the JSON response, and persists the result to the DB.

Typical usage::

    from orchestration_engine.issue_automation import IssueClassifier

    classifier = IssueClassifier(executor=my_executor)
    result = classifier.classify(
        issue_number=42,
        repo="owner/repo",
        title="Fix null pointer in pipeline runner",
        body="When the pipeline runner receives an empty task list...",
        labels=["bug", "urgent"],
        db=db,
    )
    print(result.classification_type, result.confidence)
"""

from __future__ import annotations

from ..notifications import NotificationDispatcher  # noqa: F401

__all__ = [
    "IssueClassification",
    "IssueClassifier",
    "VALID_CLASSIFICATION_TYPES",
    "CLASSIFICATION_TEMPLATE_MAP",
    "DEFAULT_TEMPLATE_MAPPING",
    "TemplateSelector",
    "InputExtractor",
    "IssueAutomation",
    "post_github_comment",
    "slugify_branch",
    "generate_pipeline_input",
    "remove_github_label",
    "add_github_label",
    "get_github_issue_labels",
    "create_pr_for_issue",
    "create_content_pr",
    # Re-exports from github_fetcher (Issue #507)
    "GitHubIssueData",
    "GitHubIssueFetcher",
    "fetch_github_issue",
]

# Re-exports from github_fetcher
from ..github_fetcher import (  # noqa: E402
    GitHubIssueData,
    GitHubIssueFetcher,
    fetch_github_issue,
)

# slugify_branch — re-export for backward compat (Issue #511)
from ..text_utils import slugify_branch  # noqa: E402, F401

# Re-imports from extracted sub-modules — these form the facade re-export
# surface.  ``__init__`` holds no inline defs; every function and class lives in
# a sub-module (classifier / extractor / github_labels / pr_dispatch /
# orchestrator) and is re-exported here.
from .classifier import (  # noqa: E402, F401
    CLASSIFICATION_TEMPLATE_MAP,
    DEFAULT_TEMPLATE_MAPPING,
    VALID_CLASSIFICATION_TYPES,
    IssueClassification,
    IssueClassifier,
)
from .extractor import InputExtractor, TemplateSelector  # noqa: E402, F401
from .github_labels import (  # noqa: E402, F401
    add_github_label,
    generate_pipeline_input,
    get_github_issue_labels,
    post_github_comment,
    remove_github_label,
)
from .orchestrator import IssueAutomation  # noqa: E402, F401
from .pr_dispatch import (  # noqa: E402, F401
    _truncate_title,
    create_content_pr,
    create_pr_for_issue,
    post_failure_summary_comment,
    post_pipeline_result_comment,
    post_result_to_issue,
)
