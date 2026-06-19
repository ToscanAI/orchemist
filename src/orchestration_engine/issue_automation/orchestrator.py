"""IssueAutomation — orchestrate classify → select → extract → launch.

:class:`IssueAutomation` chains the issue-automation steps: classify a GitHub
issue, select a pipeline template, extract pipeline inputs from the issue, and
launch a pipeline run via a caller-supplied launcher.  Dependencies are
injected at construction time to facilitate testing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..notifications import NotificationDispatcher

if TYPE_CHECKING:
    from ..db import Database
    from .classifier import IssueClassification, IssueClassifier
    from .extractor import InputExtractor, TemplateSelector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# IssueAutomation — orchestrates classify → select → extract → launch
# ---------------------------------------------------------------------------


class IssueAutomation:
    """Orchestrates the full GitHub issue → pipeline run flow.

    Chains four steps:

    1. **Classify** — :class:`IssueClassifier` assigns a type (bug/feature/…)
       and persists the result to the DB.
    2. **Select template** — :class:`TemplateSelector` maps the classification
       type to a pipeline template name.
    3. **Extract inputs** — :class:`InputExtractor` reads the template's
       ``config_schema`` and populates pipeline input variables from the issue.
    4. **Launch run** — the caller-supplied *launcher* callable spawns a
       daemon subprocess and returns the pipeline run dict.

    Dependencies are injected at construction time to facilitate testing.

    Args:
        classifier: :class:`IssueClassifier` instance.
        selector:   :class:`TemplateSelector` instance.
        extractor:  :class:`InputExtractor` instance.

    Example::

        automation = IssueAutomation(
            classifier=IssueClassifier(),
            selector=TemplateSelector(),
            extractor=InputExtractor(),
        )
        result = automation.process(
            issue_number=42,
            repo="owner/repo",
            title="Fix crash on empty input",
            body="When the list is empty the runner crashes.",
            labels=["bug"],
        )
        print(result["run_id"], result["comment_body"])
    """

    def __init__(
        self,
        classifier: "IssueClassifier",
        selector: "TemplateSelector",
        extractor: "InputExtractor",
        confidence_threshold: float = 0.70,
        notification_dispatcher: Optional[NotificationDispatcher] = None,
    ) -> None:
        self.classifier = classifier
        self.selector = selector
        self.extractor = extractor
        self._confidence_threshold = confidence_threshold
        self._dispatcher = notification_dispatcher

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(
        self,
        issue_number: int,
        repo: str,
        title: str,
        body: str = "",
        labels: Optional[List[str]] = None,
        db: Optional["Database"] = None,
        launcher: Optional[Any] = None,
        template_resolver: Optional[Any] = None,
        template_engine: Optional[Any] = None,
        mode: str = "standalone",
        gateway_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Classify an issue, select a template, extract inputs, and launch a pipeline.

        Steps:

        1. Classify the issue via :meth:`~IssueClassifier.classify` (persists to
           DB when *db* is provided).
        2. Select the pipeline template name via :meth:`~TemplateSelector.select`.
        3. Load the template's ``config_schema`` (via *template_resolver* /
           *template_engine*) and extract pipeline inputs via
           :meth:`~InputExtractor.extract`.  Falls back to empty schema on
           failure.
        4. Launch the pipeline via *launcher* when provided.  Updates the
           classification row status to ``"launched"`` in the DB on success.

        Args:
            issue_number:      GitHub issue number.
            repo:              Repository slug (e.g. ``"owner/repo"``).
            title:             Issue title.
            body:              Issue body / description.  Defaults to ``""``.
            labels:            List of GitHub label strings.  Defaults to ``[]``.
            db:                :class:`~db.Database` instance for persistence.
                               When ``None``, classification is not persisted
                               and the status is not updated.
            launcher:          Callable matching the ``_launch_pipeline_from_trigger``
                               signature: ``(template_file, template, input_data,
                               mode, gateway_url, db) → run_dict``.
                               When ``None``, no pipeline is launched.
            template_resolver: Callable that resolves a template name to a
                               :class:`~pathlib.Path`.  Used to load the template.
                               When ``None``, the config schema defaults to ``{}``.
            template_engine:   Template engine instance with a ``load_template``
                               method.  Used together with *template_resolver*.
            mode:              Daemon execution mode (``"standalone"``, ``"openclaw"``,
                               ``"dry-run"``).  Defaults to ``"standalone"``.
            gateway_url:       OpenClaw gateway URL.  ``None`` to use env var.

        Returns:
            Dict with keys::

                issue_number       int   — original issue number
                repo               str   — repository slug
                classification_type str  — LLM-assigned type
                confidence         float — LLM confidence [0, 1]
                template           str   — selected pipeline template name
                run_id             str   — launched run ID, or None
                comment_body       str   — pre-formatted GitHub comment text
        """
        labels = labels or []

        # Step 1: Classify
        classification = self.classifier.classify(
            issue_number=issue_number,
            repo=repo,
            title=title,
            body=body,
            labels=labels,
            db=db,
        )

        # Confidence threshold gate — escalate to human review when confidence
        # is too low to trust automatic pipeline selection.
        run_id: Optional[str] = None
        escalated: bool = False

        if classification.confidence < self._confidence_threshold:
            logger.warning(
                "IssueAutomation: confidence %.2f below threshold %.2f for issue #%d "
                "in %r — escalating to human review",
                classification.confidence,
                self._confidence_threshold,
                issue_number,
                repo,
            )
            if self._dispatcher is not None:
                self._dispatcher.dispatch(
                    event="human_review",
                    run_id=f"issue-{issue_number}",
                    issue_number=issue_number,
                    confidence=f"{classification.confidence:.0%}",
                    summary=(
                        f"Low-confidence classification: "
                        f"{classification.classification_type} "
                        f"({classification.confidence:.0%}). Manual review needed."
                    ),
                    tier="escalation",
                )
            escalated = True  # noqa: F841 — escalation outcome flag, kept intentional

            # Update DB status to 'escalated' so the row reflects the outcome.
            if db is not None and classification.id is not None:
                db.update_issue_classification_status(classification.id, "escalated")

            template_name = self.selector.select(classification.classification_type)

            comment_body = self._build_comment(
                issue_number=issue_number,
                classification=classification,
                template_name=template_name,
                run_id=None,
                escalated=True,
            )

            return {
                "issue_number": issue_number,
                "repo": repo,
                "classification_type": classification.classification_type,
                "confidence": classification.confidence,
                "template": template_name,
                "run_id": None,
                "comment_body": comment_body,
                "escalated": True,
            }

        # Step 2: Select template
        template_name = self.selector.select(classification.classification_type)

        # Step 3: Load config_schema and extract pipeline inputs
        config_schema: Dict[str, Any] = {}
        template_path_obj = None
        template_obj = None

        if template_resolver is not None and template_engine is not None:
            try:
                template_path_obj = template_resolver(template_name)
                template_obj = template_engine.load_template(template_path_obj)
                config_schema = template_obj.config_schema or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "IssueAutomation: could not load template schema for %r: %s",
                    template_name,
                    exc,
                )

        pipeline_inputs = self.extractor.extract(
            issue_title=title,
            issue_body=body,
            config_schema=config_schema,
        )
        # Always ensure issue context is in inputs as a fallback
        pipeline_inputs.setdefault("issue_number", issue_number)
        pipeline_inputs.setdefault("repo", repo)

        # Step 4: Launch pipeline
        if launcher is not None and template_path_obj is not None and template_obj is not None:
            try:
                run_dict = launcher(
                    template_file=template_path_obj,
                    template=template_obj,
                    input_data=pipeline_inputs,
                    mode=mode,
                    gateway_url=gateway_url,
                    db=db,
                )
                run_id = run_dict.get("run_id")
                # Update the classification row to reflect the pipeline was launched
                if db is not None and classification.id is not None:
                    db.update_issue_classification_status(classification.id, "launched")
                logger.info(
                    "IssueAutomation: launched pipeline %r for issue #%d in %r (run_id=%r)",
                    template_name,
                    issue_number,
                    repo,
                    run_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "IssueAutomation: failed to launch pipeline for issue #%d: %s",
                    issue_number,
                    exc,
                )

        comment_body = self._build_comment(
            issue_number=issue_number,
            classification=classification,
            template_name=template_name,
            run_id=run_id,
            escalated=False,
        )

        return {
            "issue_number": issue_number,
            "repo": repo,
            "classification_type": classification.classification_type,
            "confidence": classification.confidence,
            "template": template_name,
            "run_id": run_id,
            "comment_body": comment_body,
            "escalated": False,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_comment(
        self,
        issue_number: int,  # noqa: ARG002
        classification: "IssueClassification",
        template_name: str,
        run_id: Optional[str],
        escalated: bool = False,
    ) -> str:
        """Build the GitHub comment body for an issue automation result.

        Args:
            issue_number:    GitHub issue number (informational).
            classification:  The populated :class:`IssueClassification` instance.
            template_name:   Selected pipeline template name.
            run_id:          Pipeline run ID, or ``None`` when no run was launched.
            escalated:       When ``True``, the comment reflects that the issue
                             was escalated to human review due to low confidence.

        Returns:
            Markdown-formatted comment text suitable for posting as a GitHub issue comment.
        """
        lines: List[str] = [
            "🤖 **Orchemist** has picked up this issue.",
            "",
            f"**Classification:** `{classification.classification_type}` "
            f"(confidence: {classification.confidence:.0%})",
        ]
        if escalated:
            lines.append(
                f"⚠️ **Confidence too low for automatic launch** "
                f"(`{classification.confidence:.0%}` < threshold)"
            )
            lines.append("This issue has been escalated to human review via Telegram.")
        else:
            lines.append(f"**Pipeline:** `{template_name}`")
            if run_id:
                lines.append(f"**Run ID:** `{run_id}`")
            else:
                lines.append("**Run ID:** *(not launched — template unavailable or no launcher)*")
        if classification.reasoning:
            lines.append(f"**Reasoning:** {classification.reasoning}")
        return "\n".join(lines)
