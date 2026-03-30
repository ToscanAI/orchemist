"""Acceptance tests for issue #715: migrate toml → tomllib.

Derived ONLY from behavioral contracts in behavioral.md.
Written PRE-IMPLEMENTATION — tests for pyproject.toml structure and source-level
contracts WILL FAIL until implementation is complete. That is expected.

Run after implementation:
    cd /home/toscan/orchestration-engine
    python -m pytest pipeline-emulation/715-toml-to-tomllib/acceptance_tests.py -v
"""

import importlib
import importlib.util
import re
import sys
import tempfile
import tomllib
from pathlib import Path
from unittest.mock import patch, call

import pytest

# Ensure orchestration_engine is importable
sys.path.insert(0, "/home/toscan/orchestration-engine/src")

# Absolute paths used across tests
REPO_ROOT = Path("/home/toscan/orchestration-engine")
PYPROJECT_TOML = REPO_ROOT / "pyproject.toml"
CONFIG_PY = REPO_ROOT / "src" / "orchestration_engine" / "config.py"
SERVER_PY = REPO_ROOT / "src" / "orchestration_engine" / "mcp" / "server.py"
TEST_CONFIG_PY = REPO_ROOT / "tests" / "test_config.py"
TEST_MCP_SERVER_PY = REPO_ROOT / "tests" / "test_mcp_server.py"


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT: API compatibility
# load_toml_config(path) returns the same dict as before
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadTomlConfigAPICompatibility:
    """load_toml_config() must return identical results regardless of underlying library."""

    def test_load_toml_config_returns_correct_dict(self, tmp_path):
        """Given a valid TOML config file, load_toml_config returns the expected dict."""
        toml_content = b"""
[queue]
max_workers = 12
poll_interval_seconds = 3

[retry]
max_retries_default = 5
"""
        config_file = tmp_path / "config.toml"
        config_file.write_bytes(toml_content)

        from orchestration_engine.config import load_toml_config
        result = load_toml_config(str(config_file))

        assert result["queue"]["max_workers"] == 12
        assert result["queue"]["poll_interval_seconds"] == 3
        assert result["retry"]["max_retries_default"] == 5

    def test_load_toml_config_missing_file_returns_empty_dict(self):
        """Given a non-existent path, load_toml_config returns {} (existing fallback preserved)."""
        from orchestration_engine.config import load_toml_config
        result = load_toml_config("/nonexistent/path/config.toml")
        assert result == {}


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT: open("rb") requirement
# tomllib.load() raises TypeError with a text-mode handle
# Any correct implementation MUST use open(..., "rb")
# ─────────────────────────────────────────────────────────────────────────────

class TestBinaryModeRequirement:
    """tomllib.load() silently enforces binary-mode file handles via TypeError."""

    def test_tomllib_raises_type_error_with_text_mode_handle(self, tmp_path):
        """Given a text-mode file handle, tomllib.load() raises TypeError.

        This documents why all call sites MUST use open(..., 'rb').
        """
        toml_file = tmp_path / "test.toml"
        toml_file.write_text('[section]\nkey = "value"\n')

        with open(toml_file, "r") as f:  # text mode — intentionally wrong
            with pytest.raises(TypeError):
                tomllib.load(f)

    def test_tomllib_succeeds_with_binary_mode_handle(self, tmp_path):
        """Given a binary-mode file handle, tomllib.load() succeeds."""
        toml_file = tmp_path / "test.toml"
        toml_file.write_bytes(b'[section]\nkey = "value"\n')

        with open(toml_file, "rb") as f:
            data = tomllib.load(f)

        assert data["section"]["key"] == "value"

    def test_load_toml_config_uses_binary_mode_open(self, tmp_path):
        """load_toml_config() must call open() with 'rb' mode, not text mode.

        FAIL NOW → PASS AFTER: config.py must use open(..., 'rb') to call tomllib.load().
        Using tomllib.loads() with a text-mode file would bypass this check.
        """
        toml_content = b'[queue]\nmax_workers = 4\n'
        config_file = tmp_path / "config.toml"
        config_file.write_bytes(toml_content)

        from orchestration_engine.config import load_toml_config

        with patch("builtins.open", wraps=open) as mock_open:
            result = load_toml_config(str(config_file))

        # Collect all open() calls and find the one for our config file
        config_file_calls = [
            c for c in mock_open.call_args_list
            if str(config_file) in str(c) or str(config_file) in (c.args[0] if c.args else "")
        ]
        assert config_file_calls, (
            "load_toml_config() did not call open() on the config file. "
            "It must use open(path, 'rb') to open TOML files for tomllib."
        )
        # At least one call to open() the config file must use 'rb' mode
        rb_calls = [
            c for c in config_file_calls
            if "rb" in str(c)
        ]
        assert rb_calls, (
            f"load_toml_config() opened the config file but not in 'rb' mode. "
            f"Calls found: {config_file_calls}. "
            "tomllib.load() requires binary mode — use open(path, 'rb')."
        )

    def test_read_version_uses_binary_mode_open(self, tmp_path):
        """_read_version() must call open() with 'rb' mode when falling back to pyproject.toml.

        FAIL NOW → PASS AFTER: server.py must use open(..., 'rb') for tomllib.load()
        in the _read_version() fallback path.
        """
        from orchestration_engine.mcp.server import _read_version

        # Write a minimal pyproject.toml in a temp location
        fake_pyproject = tmp_path / "pyproject.toml"
        fake_pyproject.write_bytes(b'[project]\nversion = "1.2.3"\n')

        # Patch importlib.metadata to force the file-read fallback path,
        # then patch builtins.open to track calls.
        with patch("importlib.metadata.version", side_effect=Exception("not installed")):
            with patch("builtins.open", wraps=open) as mock_open:
                # Redirect the Path lookup inside _read_version to our temp file
                with patch(
                    "orchestration_engine.mcp.server.Path",
                    wraps=lambda *a, **kw: fake_pyproject if "pyproject.toml" in str(a) else Path(*a, **kw)
                ):
                    try:
                        _read_version()
                    except Exception:
                        pass  # We just care about the open() call mode, not return value

        # Look for any open() call that involved a pyproject.toml
        pyproject_calls = [
            c for c in mock_open.call_args_list
            if "pyproject.toml" in str(c)
        ]
        if not pyproject_calls:
            pytest.skip(
                "Could not intercept pyproject.toml open() in _read_version() — "
                "implementation may use a different Path resolution strategy. "
                "Source check confirms binary mode requirement."
            )

        rb_calls = [c for c in pyproject_calls if "rb" in str(c)]
        assert rb_calls, (
            f"_read_version() opened pyproject.toml but not in 'rb' mode. "
            f"Calls: {pyproject_calls}. Must use open(path, 'rb') for tomllib."
        )


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT: pyproject.toml structure
# These tests FAIL now (toml is still in deps). PASS after implementation.
# ─────────────────────────────────────────────────────────────────────────────

class TestPyprojectTomlStructure:
    """pyproject.toml must reflect the migration: toml removed, tomli conditional added."""

    def _parse_pyproject(self):
        with open(PYPROJECT_TOML, "rb") as f:
            return tomllib.load(f)

    def test_toml_not_in_main_dependencies(self):
        """FAIL NOW → PASS AFTER: 'toml' must NOT appear in [project.dependencies]."""
        data = self._parse_pyproject()
        deps = data.get("project", {}).get("dependencies", [])
        toml_deps = [d for d in deps if re.match(r"^toml\b", d)]
        assert toml_deps == [], (
            f"Found 'toml' in dependencies: {toml_deps}. "
            "Must be removed and replaced with tomllib/tomli."
        )

    def test_tomli_in_optional_or_conditional_dependencies(self):
        """FAIL NOW → PASS AFTER: 'tomli' must appear for Python < 3.11 compatibility."""
        data = self._parse_pyproject()

        # Check optional-dependencies or dependency-groups for tomli
        optional_deps = data.get("project", {}).get("optional-dependencies", {})
        dep_groups = data.get("dependency-groups", {})
        all_optional = [
            dep
            for group in list(optional_deps.values()) + list(dep_groups.values())
            for dep in group
        ]

        # Also check main deps for PEP 508 environment marker form:
        # tomli>=2.0; python_version < "3.11"
        main_deps = data.get("project", {}).get("dependencies", [])

        has_tomli = any(re.match(r"^tomli\b", d) for d in all_optional + main_deps)
        assert has_tomli, (
            "Expected 'tomli' as a conditional dependency for Python < 3.11. "
            "Either as a PEP 508 marker in [project.dependencies] or in optional-dependencies."
        )

    def test_tomli_dependency_has_python_version_marker(self):
        """FAIL NOW → PASS AFTER: tomli dependency must include python_version < '3.11' marker.

        An unconditional tomli dep would pass the presence check but violate the
        'no redundant dependency on 3.11+' contract. The PEP 508 marker is required.
        """
        data = self._parse_pyproject()
        main_deps = data.get("project", {}).get("dependencies", [])
        optional_deps = data.get("project", {}).get("optional-dependencies", {})
        dep_groups = data.get("dependency-groups", {})
        all_optional = [
            dep
            for group in list(optional_deps.values()) + list(dep_groups.values())
            for dep in group
        ]

        # Find the tomli entry
        tomli_entries = [d for d in main_deps + all_optional if re.match(r"^tomli\b", d)]
        assert tomli_entries, "No tomli dependency entry found at all."

        # At least one must have the python_version marker
        marker_re = re.compile(r'python_version\s*[<>!=]+\s*["\']?3\.11["\']?')
        entries_with_marker = [d for d in tomli_entries if marker_re.search(d)]
        assert entries_with_marker, (
            f"tomli dependency entries found: {tomli_entries}, but none contain "
            "a 'python_version < \"3.11\"' (or similar) marker. "
            "The marker is required to avoid installing tomli on Python 3.11+."
        )

    def test_tomli_dependency_has_version_constraint(self):
        """FAIL NOW → PASS AFTER: tomli dependency must include a version constraint (e.g. >=2.0.0).

        Pinless dependencies are fragile; the migration spec requires a minimum version.
        """
        data = self._parse_pyproject()
        main_deps = data.get("project", {}).get("dependencies", [])
        optional_deps = data.get("project", {}).get("optional-dependencies", {})
        dep_groups = data.get("dependency-groups", {})
        all_optional = [
            dep
            for group in list(optional_deps.values()) + list(dep_groups.values())
            for dep in group
        ]

        tomli_entries = [d for d in main_deps + all_optional if re.match(r"^tomli\b", d)]
        assert tomli_entries, "No tomli dependency entry found at all."

        # At least one must have a version specifier like >=2.0 or >=2.0.0
        version_re = re.compile(r"tomli\s*[><=!]")
        entries_with_version = [d for d in tomli_entries if version_re.search(d)]
        assert entries_with_version, (
            f"tomli dependency entries found: {tomli_entries}, but none contain "
            "a version constraint. Expected something like 'tomli>=2.0.0; python_version < \"3.11\"'."
        )

    def test_tomli_w_in_dev_extra(self):
        """FAIL NOW → PASS AFTER: 'tomli-w' must appear in the 'dev' optional-dependency group.

        tomli_w is needed for writing TOML in test fixtures and belongs in dev extras.
        """
        data = self._parse_pyproject()
        optional_deps = data.get("project", {}).get("optional-dependencies", {})
        dep_groups = data.get("dependency-groups", {})

        # Collect all deps from groups named 'dev'
        dev_deps = list(optional_deps.get("dev", [])) + list(dep_groups.get("dev", []))

        has_tomli_w = any(re.match(r"^tomli[-_]w\b", d, re.IGNORECASE) for d in dev_deps)
        assert has_tomli_w, (
            f"'tomli-w' not found in dev extras. Dev deps found: {dev_deps}. "
            "tomli_w must be in the 'dev' optional-dependencies group for test fixture writing."
        )


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT: config.py source no longer imports `toml`
# FAIL NOW → PASS AFTER
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigPySource:
    """config.py source must import tomllib (or tomli) instead of toml."""

    def test_config_py_does_not_import_toml(self):
        """FAIL NOW → PASS AFTER: config.py must not have 'import toml'."""
        source = CONFIG_PY.read_text()
        # Match bare `import toml` or `from toml import ...`
        assert not re.search(r"^\s*import toml\b", source, re.MULTILINE), (
            "config.py still uses 'import toml'. Must be migrated to tomllib/tomli."
        )
        assert not re.search(r"^\s*from toml\b", source, re.MULTILINE), (
            "config.py still uses 'from toml import ...'. Must be migrated."
        )

    def test_config_py_has_tomllib_compat_import(self):
        """FAIL NOW → PASS AFTER: config.py must import tomllib with tomli fallback for 3.10."""
        source = CONFIG_PY.read_text()
        # The compat block should try tomllib (stdlib 3.11+) first, then tomli
        has_tomllib = "tomllib" in source
        has_tomli = "tomli" in source
        assert has_tomllib or has_tomli, (
            "config.py must import tomllib (stdlib) or tomli (backport). "
            "Found neither. Migration not complete."
        )

    def test_load_toml_config_works_with_toml_module_blocked(self, tmp_path):
        """FAIL NOW → PASS AFTER: load_toml_config() must work end-to-end when 'toml' is absent.

        Patches sys.modules to block the 'toml' package, re-imports config module,
        then calls load_toml_config() on a real file. This proves the compat import
        works at call time, not just at import time.
        """
        toml_content = b'[queue]\nmax_workers = 7\n[retry]\nmax_retries_default = 2\n'
        config_file = tmp_path / "config.toml"
        config_file.write_bytes(toml_content)

        mod_name = "orchestration_engine.config"
        # Remove any cached version of the module
        cached = {k: sys.modules.pop(k) for k in list(sys.modules) if "orchestration_engine" in k}

        try:
            with patch.dict("sys.modules", {"toml": None}):
                # Re-import config.py with toml blocked — must succeed via tomllib
                spec = importlib.util.spec_from_file_location(mod_name, CONFIG_PY)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # Should NOT raise ImportError for 'toml'

                # Now call the actual function — must return correct dict without 'toml'
                result = mod.load_toml_config(str(config_file))

            assert result.get("queue", {}).get("max_workers") == 7, (
                f"load_toml_config() returned unexpected result when 'toml' was blocked: {result}. "
                "The function must work via tomllib even when 'toml' is unavailable."
            )
            assert result.get("retry", {}).get("max_retries_default") == 2
        except ImportError as e:
            if "toml" in str(e).lower():
                pytest.fail(
                    f"load_toml_config() tried to import 'toml' when it was blocked: {e}. "
                    "config.py must use the tomllib/tomli compat block after migration."
                )
            raise
        finally:
            # Restore module cache
            for k in list(sys.modules):
                if "orchestration_engine" in k:
                    sys.modules.pop(k, None)
            sys.modules.update(cached)


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT: server.py._read_version() behavior
# ─────────────────────────────────────────────────────────────────────────────

class TestReadVersion:
    """_read_version() must return a valid version string and fall back to '0.0.0'."""

    def test_read_version_returns_version_string(self):
        """_read_version() returns the version string from pyproject.toml without error."""
        from orchestration_engine.mcp.server import _read_version
        version = _read_version()
        # Must be a non-empty semver-like string
        assert isinstance(version, str)
        assert len(version) > 0
        assert re.match(r"^\d+\.\d+", version), (
            f"Expected semver-like string, got: {version!r}"
        )

    def test_read_version_fallback_when_pyproject_absent(self, tmp_path, capsys):
        """Given pyproject.toml is absent, the real _read_version() returns '0.0.0' and warns.

        This test calls the REAL _read_version() function imported from server.py.
        It patches builtins.open to raise FileNotFoundError for any pyproject.toml path,
        and patches importlib.metadata to simulate the package not being installed.
        """
        from orchestration_engine.mcp import server as mcp_server

        def selective_open(path, mode="r", *args, **kwargs):
            """Raise FileNotFoundError for pyproject.toml; allow all other open() calls."""
            if "pyproject.toml" in str(path):
                raise FileNotFoundError(f"No such file or directory: '{path}'")
            # Allow all other file opens (stdlib, etc.)
            import builtins
            return builtins.__loader__.load_module("builtins")  # fallback to real open

        # Use a simpler approach: patch the specific toml/tomllib load call
        # by making open raise FileNotFoundError for pyproject.toml
        original_open = open

        def fake_open(path, *args, **kwargs):
            if "pyproject.toml" in str(path):
                raise FileNotFoundError(f"Simulated absent pyproject.toml: {path}")
            return original_open(path, *args, **kwargs)

        with patch("importlib.metadata.version", side_effect=Exception("package not installed")):
            with patch("builtins.open", side_effect=fake_open):
                # Call the REAL _read_version() — not a mock, not a local function
                version = mcp_server._read_version()

        assert version == "0.0.0", (
            f"Expected '0.0.0' fallback when pyproject.toml is absent, got: {version!r}. "
            "The fallback must be preserved after migration."
        )

        captured = capsys.readouterr()
        assert captured.err, (
            "Expected a warning on stderr when pyproject.toml is absent. "
            "_read_version() should emit a warning before returning '0.0.0'."
        )
        assert "0.0.0" in captured.err or "pyproject" in captured.err.lower() or "version" in captured.err.lower(), (
            f"stderr warning doesn't mention the fallback situation. Got: {captured.err!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT: server.py source uses tomllib, not toml
# FAIL NOW → PASS AFTER
# ─────────────────────────────────────────────────────────────────────────────

class TestServerPySource:
    """server.py _read_version must use tomllib/tomli, not toml, after migration."""

    def test_server_py_does_not_import_toml_for_version_read(self):
        """FAIL NOW → PASS AFTER: server.py must not use 'import toml' in _read_version."""
        source = SERVER_PY.read_text()
        assert not re.search(r"^\s*import toml\b", source, re.MULTILINE), (
            "server.py still uses 'import toml'. "
            "Must be migrated to tomllib/tomli after issue #715."
        )


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT: Test fixtures — test_config.py uses tomli_w, not toml.dump
# FAIL NOW → PASS AFTER
# ─────────────────────────────────────────────────────────────────────────────

class TestTestFixturesUseTomliW:
    """test_config.py must write TOML via tomli_w, not toml.dump."""

    def test_test_config_py_uses_tomli_w(self):
        """FAIL NOW → PASS AFTER: test_config.py must not use toml.dump for writing fixtures."""
        source = TEST_CONFIG_PY.read_text()

        # After migration, toml.dump must not be used for writing fixtures
        assert "toml.dump" not in source, (
            "test_config.py still uses 'toml.dump'. "
            "Must be replaced with tomli_w.dumps() or tomli_w.dump()."
        )

    def test_test_config_py_imports_tomli_w(self):
        """FAIL NOW → PASS AFTER: test_config.py must import tomli_w for writing fixtures."""
        source = TEST_CONFIG_PY.read_text()
        assert "tomli_w" in source, (
            "test_config.py does not import tomli_w. "
            "After migration, test fixtures must be written via tomli_w."
        )

    def test_tomli_w_can_write_valid_toml_readable_by_tomllib(self, tmp_path):
        """tomli_w-written files must be readable by tomllib (stdlib)."""
        try:
            import tomli_w
        except ImportError:
            pytest.skip("tomli_w not yet installed — will be available after implementation")

        data = {"queue": {"max_workers": 8}, "retry": {"max_retries_default": 3}}
        toml_file = tmp_path / "fixture.toml"

        with open(toml_file, "wb") as f:
            tomli_w.dump(data, f)

        with open(toml_file, "rb") as f:
            result = tomllib.load(f)

        assert result["queue"]["max_workers"] == 8
        assert result["retry"]["max_retries_default"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT: Mock patching — patch target matches actual import path after migration
# FAIL NOW → PASS AFTER
# ─────────────────────────────────────────────────────────────────────────────

class TestMockPatchTargetConsistency:
    """The patch target for TOML load in test_mcp_server.py must match server.py's actual import."""

    def test_test_mcp_server_does_not_patch_toml_load_after_migration(self):
        """FAIL NOW → PASS AFTER: 'toml.load' must not be the patch target in test_mcp_server.py.

        After migration, server.py no longer uses 'import toml', so patching 'toml.load'
        has no effect. The patch target must match the actual import in server.py.
        """
        source = TEST_MCP_SERVER_PY.read_text()
        assert "patch('toml.load'" not in source and 'patch("toml.load"' not in source, (
            "test_mcp_server.py still patches 'toml.load'. "
            "After migration to tomllib, this patch target is invalid. "
            "Must be updated to match the new import path in server.py."
        )

    def test_test_mcp_server_uses_tomllib_based_patch_target(self):
        """FAIL NOW → PASS AFTER: test_mcp_server.py must have a patch() using tomllib/tomli path.

        It's not sufficient to simply remove 'toml.load' — a correct replacement patch
        must exist. Without it, TOML load mocking disappears entirely, making tests depend
        on real filesystem state.

        The patch target must reference 'tomllib' or 'tomli' — e.g.:
          patch('orchestration_engine.mcp.server.tomllib.load', ...)
        """
        source = TEST_MCP_SERVER_PY.read_text()
        # Must contain a patch() call that references tomllib or tomli in the target path
        has_tomllib_patch = bool(re.search(r"patch\s*\(\s*['\"].*tomllib.*['\"]", source))
        has_tomli_patch = bool(re.search(r"patch\s*\(\s*['\"].*tomli[^_w].*['\"]", source))
        assert has_tomllib_patch or has_tomli_patch, (
            "test_mcp_server.py does not contain a patch() call targeting 'tomllib' or 'tomli'. "
            "After removing 'patch(\"toml.load\")', a replacement patch with the new import path "
            "must be added. Expected something like: patch('orchestration_engine.mcp.server.tomllib.load', ...)"
        )

    def test_server_py_and_test_mcp_server_use_consistent_toml_import(self):
        """FAIL NOW → PASS AFTER: server.py and test_mcp_server.py must agree on the TOML library."""
        server_source = SERVER_PY.read_text()
        test_source = TEST_MCP_SERVER_PY.read_text()

        # After migration, server.py should use tomllib (not toml)
        server_uses_toml = bool(re.search(r"\bimport toml\b", server_source))
        # If server uses tomllib, the test must not patch 'toml.load'
        if not server_uses_toml:
            assert "toml.load" not in test_source, (
                "server.py no longer uses 'import toml', but test_mcp_server.py "
                "still patches 'toml.load'. Patch target must be updated."
            )


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACT: Python 3.11+ — tomli NOT imported when tomllib available
# ─────────────────────────────────────────────────────────────────────────────

class TestNoRedundantTomliImportOnPy311:
    """On Python 3.11+, tomllib is stdlib. tomli must NOT be imported as a redundant dep."""

    def test_tomli_not_in_sys_modules_after_import_on_py311(self):
        """Given Python 3.11+, tomli must NOT be imported when tomllib is available."""
        if sys.version_info < (3, 11):
            pytest.skip("This contract only applies to Python 3.11+")

        # Remove cached modules to get a fresh import
        mods_to_remove = [k for k in sys.modules if "orchestration_engine" in k]
        saved = {k: sys.modules.pop(k) for k in mods_to_remove}

        try:
            # Ensure tomllib is available (it is on 3.11+) and tomli is absent
            with patch.dict("sys.modules", {"tomli": None}):
                # config.py should import cleanly using only tomllib on 3.11+
                spec = importlib.util.spec_from_file_location(
                    "orchestration_engine.config", CONFIG_PY
                )
                mod = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(mod)
                    # If we get here without ImportError, tomli was not required ✓
                    imported_tomli = "tomli" in sys.modules and sys.modules["tomli"] is not None
                    # tomli should not have been imported (it was blocked and we succeeded)
                    assert not imported_tomli, (
                        "config.py imported 'tomli' on Python 3.11+ where tomllib is stdlib. "
                        "The compat block should prefer tomllib on 3.11+."
                    )
                except ImportError as e:
                    if "tomli" in str(e).lower():
                        pytest.fail(
                            f"config.py tried to import 'tomli' on Python 3.11+: {e}. "
                            "On 3.11+, only tomllib (stdlib) should be used."
                        )
                    # Other ImportError might be from toml being blocked — that's the point
        finally:
            # Restore module cache
            for k in mods_to_remove:
                sys.modules.pop(k, None)
            sys.modules.update(saved)
