"""Tests for TriggerMatcher and InputMapper (Issue #329.3).

Covers:
  - Group A: TriggerMatcher — empty filters (always pass)
  - Group B: TriggerMatcher — branch filter
  - Group C: TriggerMatcher — labels filter
  - Group D: TriggerMatcher — action filter
  - Group E: TriggerMatcher — event_type filter
  - Group F: TriggerMatcher — AND combination of multiple filters
  - Group G: TriggerMatcher — unknown filter keys (warn + ignore)
  - Group H: InputMapper — template resolution
  - Group I: InputMapper — mixed (template + literal) values
  - Group J: InputMapper — edge cases (missing paths, non-string values)
"""

import logging

import pytest

from orchestration_engine.webhooks import InputMapper, TriggerMatcher


# ---------------------------------------------------------------------------
# Group A: TriggerMatcher — Empty / trivial cases
# ---------------------------------------------------------------------------


class TestTriggerMatcherEmpty:
    """Empty or minimal filter lists should always return True."""

    def test_empty_filters_returns_true(self):
        """No filters → always match."""
        assert TriggerMatcher.matches([], {"ref": "refs/heads/main"}) is True

    def test_empty_filters_empty_payload(self):
        """No filters, no payload → still True."""
        assert TriggerMatcher.matches([], {}) is True

    def test_single_empty_filter_dict(self):
        """A filter dict with no keys has nothing to check → True."""
        assert TriggerMatcher.matches([{}], {"action": "opened"}) is True


# ---------------------------------------------------------------------------
# Group B: TriggerMatcher — branch filter
# ---------------------------------------------------------------------------


class TestTriggerMatcherBranch:
    """Branch filter extracts branch name from 'ref' and compares."""

    def test_branch_match_with_refs_prefix(self):
        """refs/heads/main ref matches branch='main'."""
        filters = [{"branch": "main"}]
        payload = {"ref": "refs/heads/main"}
        assert TriggerMatcher.matches(filters, payload) is True

    def test_branch_no_match_with_refs_prefix(self):
        """refs/heads/develop does not match branch='main'."""
        filters = [{"branch": "main"}]
        payload = {"ref": "refs/heads/develop"}
        assert TriggerMatcher.matches(filters, payload) is False

    def test_branch_match_without_refs_prefix(self):
        """Plain ref value (no refs/heads/) is compared as-is."""
        filters = [{"branch": "main"}]
        payload = {"ref": "main"}
        assert TriggerMatcher.matches(filters, payload) is True

    def test_branch_missing_ref_key(self):
        """Missing 'ref' in payload → empty string → no match."""
        filters = [{"branch": "main"}]
        payload = {}
        assert TriggerMatcher.matches(filters, payload) is False

    def test_branch_case_sensitive(self):
        """Branch matching is case-sensitive."""
        filters = [{"branch": "Main"}]
        payload = {"ref": "refs/heads/main"}
        assert TriggerMatcher.matches(filters, payload) is False

    def test_branch_feature_branch(self):
        """Feature branch path with slashes matches exactly."""
        filters = [{"branch": "feature/my-feature"}]
        payload = {"ref": "refs/heads/feature/my-feature"}
        assert TriggerMatcher.matches(filters, payload) is True


# ---------------------------------------------------------------------------
# Group C: TriggerMatcher — labels filter
# ---------------------------------------------------------------------------


class TestTriggerMatcherLabels:
    """Labels filter supports both single-label and multi-label payload shapes."""

    def test_labels_match_single_label_object(self):
        """payload['label']['name'] is in filter list → match."""
        filters = [{"labels": ["bug", "enhancement"]}]
        payload = {"label": {"name": "bug"}}
        assert TriggerMatcher.matches(filters, payload) is True

    def test_labels_no_match_single_label_object(self):
        """payload['label']['name'] not in filter list → no match."""
        filters = [{"labels": ["bug", "enhancement"]}]
        payload = {"label": {"name": "question"}}
        assert TriggerMatcher.matches(filters, payload) is False

    def test_labels_match_multi_label_list(self):
        """payload['labels'] list contains at least one matching label → match."""
        filters = [{"labels": ["bug"]}]
        payload = {"labels": [{"name": "enhancement"}, {"name": "bug"}]}
        assert TriggerMatcher.matches(filters, payload) is True

    def test_labels_no_match_multi_label_list(self):
        """payload['labels'] list has no matching label → no match."""
        filters = [{"labels": ["bug"]}]
        payload = {"labels": [{"name": "enhancement"}, {"name": "question"}]}
        assert TriggerMatcher.matches(filters, payload) is False

    def test_labels_empty_payload_labels(self):
        """Empty labels list in payload → no match."""
        filters = [{"labels": ["bug"]}]
        payload = {"labels": []}
        assert TriggerMatcher.matches(filters, payload) is False

    def test_labels_missing_label_key(self):
        """No 'label' or 'labels' in payload → no match."""
        filters = [{"labels": ["bug"]}]
        payload = {"action": "labeled"}
        assert TriggerMatcher.matches(filters, payload) is False

    def test_labels_any_one_of_multiple_allowed(self):
        """ANY label match in the filter list suffices."""
        filters = [{"labels": ["bug", "critical", "hotfix"]}]
        payload = {"labels": [{"name": "critical"}]}
        assert TriggerMatcher.matches(filters, payload) is True

    def test_labels_both_sources_checked(self):
        """Both 'label' (single) and 'labels' (list) sources are checked."""
        filters = [{"labels": ["feature"]}]
        # Single-label payload without a list
        payload = {"label": {"name": "feature"}}
        assert TriggerMatcher.matches(filters, payload) is True

        # List payload without a single label
        payload2 = {"labels": [{"name": "feature"}]}
        assert TriggerMatcher.matches(filters, payload2) is True


# ---------------------------------------------------------------------------
# Group D: TriggerMatcher — action filter
# ---------------------------------------------------------------------------


class TestTriggerMatcherAction:
    """Action filter does an exact-match against payload['action']."""

    def test_action_match(self):
        """Exact action match → True."""
        filters = [{"action": "opened"}]
        payload = {"action": "opened"}
        assert TriggerMatcher.matches(filters, payload) is True

    def test_action_no_match(self):
        """Different action → False."""
        filters = [{"action": "opened"}]
        payload = {"action": "closed"}
        assert TriggerMatcher.matches(filters, payload) is False

    def test_action_missing_key(self):
        """Missing 'action' in payload → no match."""
        filters = [{"action": "opened"}]
        payload = {}
        assert TriggerMatcher.matches(filters, payload) is False

    def test_action_case_sensitive(self):
        """Action matching is case-sensitive."""
        filters = [{"action": "Opened"}]
        payload = {"action": "opened"}
        assert TriggerMatcher.matches(filters, payload) is False


# ---------------------------------------------------------------------------
# Group E: TriggerMatcher — event_type filter
# ---------------------------------------------------------------------------


class TestTriggerMatcherEventType:
    """event_type filter does an exact-match against payload['event_type']."""

    def test_event_type_match(self):
        """Exact event_type match → True."""
        filters = [{"event_type": "push"}]
        payload = {"event_type": "push"}
        assert TriggerMatcher.matches(filters, payload) is True

    def test_event_type_no_match(self):
        """Different event_type → False."""
        filters = [{"event_type": "push"}]
        payload = {"event_type": "pull_request"}
        assert TriggerMatcher.matches(filters, payload) is False

    def test_event_type_missing_key(self):
        """Missing 'event_type' in payload → no match."""
        filters = [{"event_type": "push"}]
        payload = {}
        assert TriggerMatcher.matches(filters, payload) is False


# ---------------------------------------------------------------------------
# Group F: TriggerMatcher — AND combination
# ---------------------------------------------------------------------------


class TestTriggerMatcherAndCombination:
    """Multiple filters are AND-combined: all must pass."""

    def test_two_filters_both_match(self):
        """Both filters match → True."""
        filters = [
            {"branch": "main"},
            {"action": "opened"},
        ]
        payload = {"ref": "refs/heads/main", "action": "opened"}
        assert TriggerMatcher.matches(filters, payload) is True

    def test_two_filters_first_fails(self):
        """First filter fails → False (short-circuit)."""
        filters = [
            {"branch": "main"},
            {"action": "opened"},
        ]
        payload = {"ref": "refs/heads/develop", "action": "opened"}
        assert TriggerMatcher.matches(filters, payload) is False

    def test_two_filters_second_fails(self):
        """Second filter fails → False."""
        filters = [
            {"branch": "main"},
            {"action": "opened"},
        ]
        payload = {"ref": "refs/heads/main", "action": "closed"}
        assert TriggerMatcher.matches(filters, payload) is False

    def test_multiple_keys_in_one_filter_dict(self):
        """Multiple keys within a single filter dict are also AND-combined."""
        filters = [{"branch": "main", "action": "opened"}]
        # Both match
        payload = {"ref": "refs/heads/main", "action": "opened"}
        assert TriggerMatcher.matches(filters, payload) is True
        # action doesn't match
        payload2 = {"ref": "refs/heads/main", "action": "closed"}
        assert TriggerMatcher.matches(filters, payload2) is False

    def test_three_filters_all_match(self):
        """Three filters, all pass → True."""
        filters = [
            {"branch": "release"},
            {"action": "labeled"},
            {"event_type": "pull_request"},
        ]
        payload = {
            "ref": "refs/heads/release",
            "action": "labeled",
            "event_type": "pull_request",
        }
        assert TriggerMatcher.matches(filters, payload) is True


# ---------------------------------------------------------------------------
# Group G: TriggerMatcher — unknown filter keys
# ---------------------------------------------------------------------------


class TestTriggerMatcherUnknownKeys:
    """Unknown filter keys emit a warning and are ignored (not a failure)."""

    def test_unknown_key_does_not_fail(self):
        """A filter with an unknown key still returns True when other keys match."""
        filters = [{"unknown_key": "value"}]
        payload = {"ref": "refs/heads/main"}
        # Should not raise and should not fail the match
        result = TriggerMatcher.matches(filters, payload)
        assert result is True

    def test_unknown_key_emits_warning(self, caplog):
        """Unknown filter key must log a WARNING."""
        filters = [{"mystery_field": "abc"}]
        payload = {}
        with caplog.at_level(logging.WARNING, logger="orchestration_engine.webhooks"):
            TriggerMatcher.matches(filters, payload)
        assert any("mystery_field" in msg for msg in caplog.messages)

    def test_unknown_key_alongside_known_key_fail(self):
        """Unknown key is ignored; known key (branch mismatch) still fails the filter."""
        filters = [{"branch": "main", "mystery_key": "whatever"}]
        payload = {"ref": "refs/heads/develop"}
        assert TriggerMatcher.matches(filters, payload) is False

    def test_unknown_key_alongside_known_key_pass(self):
        """Unknown key is ignored; known key (branch match) passes the filter."""
        filters = [{"branch": "main", "mystery_key": "whatever"}]
        payload = {"ref": "refs/heads/main"}
        assert TriggerMatcher.matches(filters, payload) is True


# ---------------------------------------------------------------------------
# Group H: InputMapper — template resolution
# ---------------------------------------------------------------------------


class TestInputMapperTemplateResolution:
    """InputMapper resolves {{payload.x.y}} templates."""

    def test_simple_top_level_key(self):
        """{{payload.action}} resolves to payload['action']."""
        payload = {"action": "push"}
        input_map = {"event": "{{payload.action}}"}
        result = InputMapper.apply(payload, input_map)
        assert result == {"event": "push"}

    def test_nested_path(self):
        """{{payload.repository.full_name}} traverses nested dicts."""
        payload = {"repository": {"full_name": "org/repo"}}
        input_map = {"repo": "{{payload.repository.full_name}}"}
        result = InputMapper.apply(payload, input_map)
        assert result == {"repo": "org/repo"}

    def test_deeply_nested_path(self):
        """Three-level nesting resolves correctly."""
        payload = {"a": {"b": {"c": "deep_value"}}}
        input_map = {"val": "{{payload.a.b.c}}"}
        result = InputMapper.apply(payload, input_map)
        assert result == {"val": "deep_value"}

    def test_missing_path_returns_none(self):
        """Unresolvable path returns None."""
        payload = {"action": "push"}
        input_map = {"sha": "{{payload.missing.path}}"}
        result = InputMapper.apply(payload, input_map)
        assert result == {"sha": None}

    def test_missing_intermediate_key_returns_none(self):
        """Intermediate key missing returns None."""
        payload = {"repository": None}
        input_map = {"name": "{{payload.repository.full_name}}"}
        result = InputMapper.apply(payload, input_map)
        assert result == {"name": None}

    def test_multiple_templates(self):
        """Multiple template values are each resolved independently."""
        payload = {
            "repository": {"full_name": "org/repo"},
            "ref": "refs/heads/main",
        }
        input_map = {
            "repo": "{{payload.repository.full_name}}",
            "branch_ref": "{{payload.ref}}",
        }
        result = InputMapper.apply(payload, input_map)
        assert result["repo"] == "org/repo"
        assert result["branch_ref"] == "refs/heads/main"


# ---------------------------------------------------------------------------
# Group I: InputMapper — mixed values (templates + literals)
# ---------------------------------------------------------------------------


class TestInputMapperMixedValues:
    """Literal values pass through unchanged; templates are resolved."""

    def test_literal_string_unchanged(self):
        """Non-template string values are returned as literals."""
        payload = {"action": "push"}
        input_map = {"env": "production"}
        result = InputMapper.apply(payload, input_map)
        assert result == {"env": "production"}

    def test_dollar_path_is_literal(self):
        """'$.x.y' values are NOT processed by InputMapper — returned as-is."""
        payload = {"ref": "refs/heads/main"}
        input_map = {"branch": "$.ref"}
        result = InputMapper.apply(payload, input_map)
        # InputMapper treats $.* as a literal string
        assert result == {"branch": "$.ref"}

    def test_mixed_template_and_literal(self):
        """Mix of template and literal values in one call."""
        payload = {"sender": {"login": "alice"}}
        input_map = {
            "user": "{{payload.sender.login}}",
            "env": "staging",
        }
        result = InputMapper.apply(payload, input_map)
        assert result["user"] == "alice"
        assert result["env"] == "staging"

    def test_empty_input_map(self):
        """Empty input_map returns empty dict."""
        payload = {"action": "push"}
        result = InputMapper.apply(payload, {})
        assert result == {}

    def test_empty_payload(self):
        """Template resolution against empty payload returns None."""
        input_map = {"repo": "{{payload.repository.full_name}}"}
        result = InputMapper.apply({}, input_map)
        assert result == {"repo": None}


# ---------------------------------------------------------------------------
# Group J: InputMapper — edge cases
# ---------------------------------------------------------------------------


class TestInputMapperEdgeCases:
    """Edge cases: non-string values, partial template strings."""

    def test_integer_value_passthrough(self):
        """Non-string values pass through unchanged."""
        input_map = {"count": 42, "flag": True}
        result = InputMapper.apply({}, input_map)
        assert result == {"count": 42, "flag": True}

    def test_none_value_passthrough(self):
        """None values pass through unchanged."""
        input_map = {"nothing": None}
        result = InputMapper.apply({}, input_map)
        assert result == {"nothing": None}

    def test_dict_value_passthrough(self):
        """Dict values pass through unchanged."""
        input_map = {"config": {"key": "value"}}
        result = InputMapper.apply({}, input_map)
        assert result == {"config": {"key": "value"}}

    def test_partial_template_not_resolved(self):
        """Partial template (embedded in larger string) is NOT resolved."""
        payload = {"action": "push"}
        # The whole value is "prefix-{{payload.action}}" — not a full match
        input_map = {"label": "prefix-{{payload.action}}"}
        result = InputMapper.apply(payload, input_map)
        # Returned as literal because it doesn't fullmatch the pattern
        assert result == {"label": "prefix-{{payload.action}}"}

    def test_resolve_path_list_value(self):
        """A path that resolves to a list returns the list as-is."""
        payload = {"labels": ["bug", "enhancement"]}
        input_map = {"tags": "{{payload.labels}}"}
        result = InputMapper.apply(payload, input_map)
        assert result == {"tags": ["bug", "enhancement"]}

    def test_resolve_path_numeric_value(self):
        """A path that resolves to a number returns the number."""
        payload = {"stats": {"count": 7}}
        input_map = {"num": "{{payload.stats.count}}"}
        result = InputMapper.apply(payload, input_map)
        assert result == {"num": 7}
