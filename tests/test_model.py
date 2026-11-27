"""Tests for managers/model.py (the service's folders) and the manager's merged settings."""

import pytest

from views_crafdapi.managers.api import CrafdApiManager
from views_crafdapi.managers.model import find_project_root

pytestmark = pytest.mark.layer4_infra


class TestFindProjectRoot:

    def test_finds_gitignore(self, tmp_path):
        (tmp_path / ".gitignore").touch()
        subdir = tmp_path / "a" / "b" / "c"
        subdir.mkdir(parents=True)

        root = find_project_root(subdir / "file.py", ".gitignore")
        assert root == tmp_path

    def test_nested_repos_finds_first(self, tmp_path):
        """C-18 edge case: inner .gitignore found before outer."""
        outer = tmp_path / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        (outer / ".gitignore").touch()
        (inner / ".gitignore").touch()

        subdir = inner / "src"
        subdir.mkdir()

        root = find_project_root(subdir / "file.py", ".gitignore")
        assert root == inner

    def test_no_marker_raises_file_not_found(self, tmp_path):
        subdir = tmp_path / "empty" / "deep"
        subdir.mkdir(parents=True)

        with pytest.raises(FileNotFoundError, match="not found in the directory hierarchy"):
            find_project_root(subdir / "file.py", ".nonexistent_marker")

    def test_custom_marker(self, tmp_path):
        (tmp_path / "pyproject.toml").touch()
        subdir = tmp_path / "src"
        subdir.mkdir()

        root = find_project_root(subdir / "file.py", "pyproject.toml")
        assert root == tmp_path


class TestConfigsProperty:

    def test_merges_all_three(self):
        mgr = object.__new__(CrafdApiManager)
        mgr._config_deployment = {"host": "0.0.0.0", "port": 80}
        mgr._config_hyperparameters = {"lr": 0.01}
        mgr._config_meta = {"name": "test"}

        configs = mgr.configs
        assert configs["host"] == "0.0.0.0"
        assert configs["lr"] == 0.01
        assert configs["name"] == "test"

    def test_handles_all_none(self):
        mgr = object.__new__(CrafdApiManager)
        mgr._config_deployment = None
        mgr._config_hyperparameters = None
        mgr._config_meta = None

        configs = mgr.configs
        assert configs == {}

    def test_handles_partial_none(self):
        mgr = object.__new__(CrafdApiManager)
        mgr._config_deployment = {"host": "0.0.0.0"}
        mgr._config_hyperparameters = None
        mgr._config_meta = {"name": "test"}

        configs = mgr.configs
        assert configs == {"host": "0.0.0.0", "name": "test"}

    def test_deployment_overrides_hyperparameters(self):
        """Deployment config is applied after hyperparameters in merge order."""
        mgr = object.__new__(CrafdApiManager)
        mgr._config_hyperparameters = {"key": "from_hp"}
        mgr._config_deployment = {"key": "from_deploy"}
        mgr._config_meta = None

        assert mgr.configs["key"] == "from_deploy"
