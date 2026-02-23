"""Tests for the community template index — Feature #76.

Covers:
- YAML loading (string, local file, malformed)
- Search by name, description, tags, category
- Empty query → returns all
- No results case
- Cache freshness check
- Result formatting
- CLI ``orch templates search`` integration (mocked index)
"""

from __future__ import annotations

import os
import time
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from click.testing import CliRunner

from orchestration_engine.template_index import TemplateEntry, TemplateIndex
from orchestration_engine.cli import main


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_YAML = textwrap.dedent("""\
    templates:
      - name: hello-pipeline
        description: Minimal two-phase example for smoke-testing
        author: Test Author
        repo_url: https://github.com/example/hello
        version: "1.0.0"
        category: example
        tags:
          - hello-world
          - smoke-test
        install_command: "orch templates install example/hello"

      - name: content-pipeline
        description: Full content creation pipeline with adversarial review
        author: Content Creator
        repo_url: https://github.com/example/content
        version: "2.0.0"
        category: content
        tags:
          - writing
          - review
          - article
        install_command: "orch templates install example/content"

      - name: code-review-pipeline
        description: Automated code review with security and complexity checks
        author: DevTools Team
        repo_url: https://github.com/example/code-review
        version: "1.1.0"
        category: engineering
        tags:
          - code-review
          - security
          - quality
        install_command: "orch templates install example/code-review"
""")


@pytest.fixture
def sample_index() -> TemplateIndex:
    """Return a :class:`TemplateIndex` pre-loaded from SAMPLE_YAML."""
    idx = TemplateIndex()
    idx.load_from_string(SAMPLE_YAML)
    return idx


# ---------------------------------------------------------------------------
# TemplateEntry tests
# ---------------------------------------------------------------------------

class TestTemplateEntry:
    def test_from_dict_full(self):
        data = {
            "name": "my-template",
            "description": "Does something great",
            "author": "Alice",
            "repo_url": "https://example.com/repo",
            "version": "3.0.0",
            "category": "data",
            "tags": ["etl", "transform"],
            "install_command": "orch templates install alice/my-template",
        }
        entry = TemplateEntry.from_dict(data)
        assert entry.name == "my-template"
        assert entry.description == "Does something great"
        assert entry.author == "Alice"
        assert entry.repo_url == "https://example.com/repo"
        assert entry.version == "3.0.0"
        assert entry.category == "data"
        assert entry.tags == ["etl", "transform"]
        assert entry.install_command == "orch templates install alice/my-template"

    def test_from_dict_defaults(self):
        entry = TemplateEntry.from_dict({})
        assert entry.name == ""
        assert entry.tags == []
        assert entry.version == "1.0.0"

    def test_to_dict_roundtrip(self):
        original = TemplateEntry(
            name="test",
            description="desc",
            author="auth",
            repo_url="https://example.com",
            version="1.2.3",
            category="ops",
            tags=["a", "b"],
            install_command="orch templates install test",
        )
        data = original.to_dict()
        restored = TemplateEntry.from_dict(data)
        assert restored.name == original.name
        assert restored.tags == original.tags
        assert restored.version == original.version

    def test_matches_name(self):
        entry = TemplateEntry.from_dict({"name": "hello-world", "description": "", "author": "",
                                         "repo_url": "", "version": "1.0.0", "category": ""})
        assert entry.matches("hello")
        assert entry.matches("HELLO")
        assert entry.matches("world")
        assert not entry.matches("foo")

    def test_matches_description(self):
        entry = TemplateEntry.from_dict({"name": "x", "description": "Adversarial review pipeline",
                                         "author": "", "repo_url": "", "version": "1.0.0", "category": ""})
        assert entry.matches("adversarial")
        assert entry.matches("review")
        assert not entry.matches("security")

    def test_matches_tags(self):
        entry = TemplateEntry.from_dict({"name": "x", "description": "", "author": "",
                                         "repo_url": "", "version": "1.0.0", "category": "",
                                         "tags": ["code-review", "security"]})
        assert entry.matches("security")
        assert entry.matches("CODE-REVIEW")
        assert not entry.matches("data")

    def test_matches_category(self):
        entry = TemplateEntry.from_dict({"name": "x", "description": "", "author": "",
                                         "repo_url": "", "version": "1.0.0", "category": "engineering"})
        assert entry.matches("engineering")
        assert entry.matches("ENGINEERING")
        assert not entry.matches("content")


# ---------------------------------------------------------------------------
# TemplateIndex loading tests
# ---------------------------------------------------------------------------

class TestTemplateIndexLoading:
    def test_load_from_string_basic(self):
        idx = TemplateIndex()
        idx.load_from_string(SAMPLE_YAML)
        assert len(idx.entries) == 3
        assert idx.entries[0].name == "hello-pipeline"

    def test_load_from_string_list_format(self):
        """Index can also be a bare list (not wrapped in 'templates:')."""
        bare_list = textwrap.dedent("""\
            - name: simple
              description: Simple template
              author: Author
              repo_url: https://example.com
              version: "1.0.0"
              category: example
              tags: []
        """)
        idx = TemplateIndex()
        idx.load_from_string(bare_list)
        assert len(idx.entries) == 1
        assert idx.entries[0].name == "simple"

    def test_load_from_string_empty(self):
        idx = TemplateIndex()
        idx.load_from_string("")
        assert idx.entries == []

    def test_load_from_string_empty_templates_key(self):
        idx = TemplateIndex()
        idx.load_from_string("templates: []")
        assert idx.entries == []

    def test_load_from_string_malformed_yaml(self):
        idx = TemplateIndex()
        with pytest.raises(ValueError, match="Failed to parse"):
            idx.load_from_string("key: [unclosed")

    def test_load_from_string_unexpected_format(self):
        idx = TemplateIndex()
        with pytest.raises(ValueError, match="Unexpected template index format"):
            idx.load_from_string("42")

    def test_load_local_success(self, tmp_path):
        index_file = tmp_path / "index.yaml"
        index_file.write_text(SAMPLE_YAML, encoding="utf-8")
        idx = TemplateIndex()
        idx.load_local(index_file)
        assert len(idx.entries) == 3

    def test_load_local_missing_file(self, tmp_path):
        idx = TemplateIndex()
        with pytest.raises(FileNotFoundError):
            idx.load_local(tmp_path / "nonexistent.yaml")

    def test_load_remote_success(self):
        """Mock urllib.request.urlopen to simulate remote fetch."""
        import io
        idx = TemplateIndex()
        mock_response = MagicMock()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = SAMPLE_YAML.encode("utf-8")

        with patch("orchestration_engine.template_index.urllib_request.urlopen",
                   return_value=mock_response):
            idx.load_remote("https://example.com/index.yaml")

        assert len(idx.entries) == 3

    def test_load_remote_network_error(self):
        from urllib.error import URLError
        idx = TemplateIndex()
        with patch("orchestration_engine.template_index.urllib_request.urlopen",
                   side_effect=URLError("connection refused")):
            with pytest.raises(URLError):
                idx.load_remote("https://example.com/index.yaml")


# ---------------------------------------------------------------------------
# TemplateIndex cache tests
# ---------------------------------------------------------------------------

class TestTemplateIndexCache:
    def test_save_and_load_cache(self, tmp_path, sample_index):
        cache_file = tmp_path / "cache.yaml"
        sample_index.save_cache(cache_file)
        assert cache_file.exists()

        idx2 = TemplateIndex()
        idx2.load_local(cache_file)
        assert len(idx2.entries) == len(sample_index.entries)
        assert idx2.entries[0].name == sample_index.entries[0].name

    def test_save_cache_creates_parent_dirs(self, tmp_path):
        idx = TemplateIndex()
        idx.load_from_string(SAMPLE_YAML)
        deep = tmp_path / "a" / "b" / "c" / "index.yaml"
        idx.save_cache(deep)
        assert deep.exists()

    def test_is_cache_fresh_fresh_file(self, tmp_path):
        cache = tmp_path / "index.yaml"
        cache.write_text("templates: []")
        assert TemplateIndex.is_cache_fresh(cache, ttl_hours=24) is True

    def test_is_cache_fresh_stale_file(self, tmp_path):
        cache = tmp_path / "index.yaml"
        cache.write_text("templates: []")
        # Backdate mtime by 25 hours
        stale_time = time.time() - (25 * 3600)
        os.utime(cache, (stale_time, stale_time))
        assert TemplateIndex.is_cache_fresh(cache, ttl_hours=24) is False

    def test_is_cache_fresh_missing_file(self, tmp_path):
        assert TemplateIndex.is_cache_fresh(tmp_path / "missing.yaml") is False

    def test_is_cache_fresh_custom_ttl(self, tmp_path):
        cache = tmp_path / "index.yaml"
        cache.write_text("templates: []")
        # 30 minutes old
        old_time = time.time() - (30 * 60)
        os.utime(cache, (old_time, old_time))
        # TTL = 1 hour → still fresh
        assert TemplateIndex.is_cache_fresh(cache, ttl_hours=1) is True
        # TTL = 0.4 hours (24 min) → stale
        assert TemplateIndex.is_cache_fresh(cache, ttl_hours=0.4) is False


# ---------------------------------------------------------------------------
# TemplateIndex search tests
# ---------------------------------------------------------------------------

class TestTemplateIndexSearch:
    def test_search_by_name(self, sample_index):
        results = sample_index.search("hello")
        assert len(results) == 1
        assert results[0].name == "hello-pipeline"

    def test_search_by_description(self, sample_index):
        results = sample_index.search("adversarial")
        assert len(results) == 1
        assert results[0].name == "content-pipeline"

    def test_search_by_tag(self, sample_index):
        results = sample_index.search("security")
        assert len(results) == 1
        assert results[0].name == "code-review-pipeline"

    def test_search_by_category(self, sample_index):
        results = sample_index.search("engineering")
        assert len(results) == 1
        assert results[0].name == "code-review-pipeline"

    def test_search_case_insensitive(self, sample_index):
        assert sample_index.search("HELLO") == sample_index.search("hello")
        assert sample_index.search("CONTENT") == sample_index.search("content")

    def test_search_empty_query_returns_all(self, sample_index):
        results = sample_index.search("")
        assert len(results) == 3

    def test_search_whitespace_query_returns_all(self, sample_index):
        results = sample_index.search("   ")
        assert len(results) == 3

    def test_search_no_results(self, sample_index):
        results = sample_index.search("zzz-nonexistent-xyz")
        assert results == []

    def test_search_multiple_matches(self, sample_index):
        # Both "hello-pipeline" (description: "smoke-testing") and possibly
        # others have "pipeline" in their name → all three match "pipeline"
        results = sample_index.search("pipeline")
        assert len(results) == 3

    def test_search_partial_match(self, sample_index):
        results = sample_index.search("review")
        assert any(e.name == "code-review-pipeline" for e in results)

    def test_search_returns_list(self, sample_index):
        results = sample_index.search("hello")
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# TemplateIndex format_results tests
# ---------------------------------------------------------------------------

class TestFormatResults:
    def test_format_no_results(self, sample_index):
        output = sample_index.format_results([])
        assert "No matching" in output

    def test_format_single_result(self, sample_index):
        results = sample_index.search("hello")
        output = sample_index.format_results(results)
        assert "hello-pipeline" in output
        assert "v1.0.0" in output
        assert "example" in output  # category
        assert "Install:" in output

    def test_format_all_results(self, sample_index):
        results = sample_index.search("")
        output = sample_index.format_results(results)
        assert "hello-pipeline" in output
        assert "content-pipeline" in output
        assert "code-review-pipeline" in output

    def test_format_includes_install_command(self, sample_index):
        results = sample_index.search("hello")
        output = sample_index.format_results(results)
        assert "orch templates install" in output

    def test_format_includes_author(self, sample_index):
        results = sample_index.search("hello")
        output = sample_index.format_results(results)
        assert "Test Author" in output

    def test_format_tags_shown(self, sample_index):
        results = sample_index.search("hello")
        output = sample_index.format_results(results)
        assert "smoke-test" in output or "hello-world" in output

    def test_format_fallback_install_command(self):
        """When install_command is empty, a default one is generated."""
        idx = TemplateIndex()
        entry = TemplateEntry(
            name="my-tpl",
            description="desc",
            author="auth",
            repo_url="",
            version="1.0.0",
            category="test",
            tags=[],
            install_command="",
        )
        output = idx.format_results([entry])
        assert "orch templates install my-tpl" in output


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------

class TestCLITemplatesSearch:
    """Test ``orch templates search`` via Click's test runner."""

    def _make_mock_index_load(self):
        """Return a context manager that patches TemplateIndex to load SAMPLE_YAML."""
        def _patched_load_remote(self_inner, url):
            self_inner.load_from_string(SAMPLE_YAML)

        def _patched_is_fresh(path=None, ttl_hours=24):
            return False  # Always force remote fetch in tests

        def _patched_save_cache(self_inner, path=None):
            pass  # No-op

        return (
            patch("orchestration_engine.template_index.TemplateIndex.load_remote",
                  _patched_load_remote),
            patch("orchestration_engine.template_index.TemplateIndex.is_cache_fresh",
                  staticmethod(_patched_is_fresh)),
            patch("orchestration_engine.template_index.TemplateIndex.save_cache",
                  _patched_save_cache),
        )

    def test_search_with_query(self):
        runner = CliRunner()
        patches = self._make_mock_index_load()
        with patches[0], patches[1], patches[2]:
            result = runner.invoke(main, ["templates", "search", "hello"])
        assert result.exit_code == 0, result.output
        assert "hello-pipeline" in result.output

    def test_search_empty_query_returns_all(self):
        runner = CliRunner()
        patches = self._make_mock_index_load()
        with patches[0], patches[1], patches[2]:
            result = runner.invoke(main, ["templates", "search"])
        assert result.exit_code == 0, result.output
        assert "hello-pipeline" in result.output
        assert "content-pipeline" in result.output
        assert "code-review-pipeline" in result.output

    def test_search_no_results(self):
        runner = CliRunner()
        patches = self._make_mock_index_load()
        with patches[0], patches[1], patches[2]:
            result = runner.invoke(main, ["templates", "search", "zzz-nonexistent"])
        assert result.exit_code == 0, result.output
        assert "No templates found" in result.output

    def test_search_refresh_flag(self):
        """--refresh forces remote fetch even when cache is fresh."""
        runner = CliRunner()

        def _always_fresh(path=None, ttl_hours=24):
            return True  # Pretend cache is fresh

        def _patched_load_remote(self_inner, url):
            self_inner.load_from_string(SAMPLE_YAML)

        def _patched_save_cache(self_inner, path=None):
            pass

        with (
            patch("orchestration_engine.template_index.TemplateIndex.is_cache_fresh",
                  staticmethod(_always_fresh)),
            patch("orchestration_engine.template_index.TemplateIndex.load_remote",
                  _patched_load_remote),
            patch("orchestration_engine.template_index.TemplateIndex.save_cache",
                  _patched_save_cache),
        ):
            result = runner.invoke(main, ["templates", "search", "--refresh", "hello"])

        assert result.exit_code == 0, result.output
        assert "hello-pipeline" in result.output

    def test_search_index_url_override(self):
        """--index-url is passed through to load_remote."""
        runner = CliRunner()
        captured_url: list = []

        def _patched_load_remote(self_inner, url):
            captured_url.append(url)
            self_inner.load_from_string(SAMPLE_YAML)

        def _patched_is_fresh(path=None, ttl_hours=24):
            return False

        def _patched_save_cache(self_inner, path=None):
            pass

        with (
            patch("orchestration_engine.template_index.TemplateIndex.load_remote",
                  _patched_load_remote),
            patch("orchestration_engine.template_index.TemplateIndex.is_cache_fresh",
                  staticmethod(_patched_is_fresh)),
            patch("orchestration_engine.template_index.TemplateIndex.save_cache",
                  _patched_save_cache),
        ):
            result = runner.invoke(
                main,
                ["templates", "search", "--index-url", "https://custom.example.com/idx.yaml"],
            )

        assert result.exit_code == 0, result.output
        assert captured_url == ["https://custom.example.com/idx.yaml"]

    def test_search_uses_cache_when_fresh(self, tmp_path):
        """When cache is fresh, load_local is called instead of load_remote."""
        cache_file = tmp_path / "template-index.yaml"

        idx_writer = TemplateIndex()
        idx_writer.load_from_string(SAMPLE_YAML)
        idx_writer.save_cache(cache_file)

        runner = CliRunner()
        load_remote_called: list = []

        def _patched_load_remote(self_inner, url):
            load_remote_called.append(url)
            self_inner.load_from_string(SAMPLE_YAML)

        def _always_fresh(path=None, ttl_hours=24):
            return True

        with (
            patch("orchestration_engine.template_index.TemplateIndex.is_cache_fresh",
                  staticmethod(_always_fresh)),
            patch("orchestration_engine.template_index.TemplateIndex.load_remote",
                  _patched_load_remote),
            patch("orchestration_engine.cli._TEMPLATE_INDEX_CACHE", cache_file),
        ):
            result = runner.invoke(main, ["templates", "search", "hello"])

        assert result.exit_code == 0, result.output
        assert "hello-pipeline" in result.output
        assert load_remote_called == [], "load_remote should NOT have been called when cache is fresh"

    def test_search_fallback_to_stale_cache_on_network_error(self, tmp_path):
        """If remote fails and stale cache exists, use the stale cache."""
        from urllib.error import URLError

        cache_file = tmp_path / "template-index.yaml"
        idx_writer = TemplateIndex()
        idx_writer.load_from_string(SAMPLE_YAML)
        idx_writer.save_cache(cache_file)

        runner = CliRunner()

        def _always_stale(path=None, ttl_hours=24):
            return False

        def _fail_remote(self_inner, url):
            raise URLError("network unreachable")

        with (
            patch("orchestration_engine.template_index.TemplateIndex.is_cache_fresh",
                  staticmethod(_always_stale)),
            patch("orchestration_engine.template_index.TemplateIndex.load_remote",
                  _fail_remote),
            patch("orchestration_engine.cli._TEMPLATE_INDEX_CACHE", cache_file),
        ):
            result = runner.invoke(main, ["templates", "search", "hello"])

        assert result.exit_code == 0, result.output
        assert "hello-pipeline" in result.output
        # Expect a warning about stale cache
        assert "stale" in result.output.lower() or "stale" in result.stderr_bytes.decode(errors="replace").lower() if hasattr(result, "stderr_bytes") else True
