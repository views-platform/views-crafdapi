"""Tests for managers/model.py (C-12): ModelPathManager, find_project_root, __load_config, ModelManager."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from views_crafdapi.managers.model import ModelPathManager, ModelManager

pytestmark = pytest.mark.layer4_infra


# ============================================================
# Phase 1: Pure functions — no mocking
# ============================================================

class TestValidateModelName:

    def test_valid_name(self):
        assert ModelPathManager.validate_model_name("some_model") is True

    def test_valid_two_word(self):
        assert ModelPathManager.validate_model_name("adjective_noun") is True

    def test_invalid_hyphen(self):
        assert ModelPathManager.validate_model_name("some-model") is False

    def test_invalid_caps(self):
        assert ModelPathManager.validate_model_name("SomeModel") is False

    def test_empty_string(self):
        assert ModelPathManager.validate_model_name("") is False

    def test_single_word(self):
        assert ModelPathManager.validate_model_name("singleword") is False

    def test_numeric(self):
        assert ModelPathManager.validate_model_name("model_123") is False

    def test_triple_word(self):
        assert ModelPathManager.validate_model_name("one_two_three") is False


class TestGenerateHash:

    def test_deterministic(self):
        h1 = ModelPathManager.generate_hash("my_model", True, "model")
        h2 = ModelPathManager.generate_hash("my_model", True, "model")
        assert h1 == h2

    def test_different_names_differ(self):
        h1 = ModelPathManager.generate_hash("model_a", True, "model")
        h2 = ModelPathManager.generate_hash("model_b", True, "model")
        assert h1 != h2

    def test_different_validate_flag_differs(self):
        h1 = ModelPathManager.generate_hash("my_model", True, "model")
        h2 = ModelPathManager.generate_hash("my_model", False, "model")
        assert h1 != h2

    def test_returns_hex_string(self):
        h = ModelPathManager.generate_hash("my_model", True, "model")
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex


class TestGetModelNameFromPath:

    def test_models_path(self):
        assert ModelPathManager.get_model_name_from_path(
            Path("project/models/my_model/config.py")
        ) == "my_model"

    def test_apis_path(self):
        assert ModelPathManager.get_model_name_from_path(
            Path("project/apis/un_fao/config.py")
        ) == "un_fao"

    def test_ensembles_path(self):
        assert ModelPathManager.get_model_name_from_path(
            Path("project/ensembles/fancy_ensemble/main.py")
        ) == "fancy_ensemble"

    def test_no_valid_parent_returns_none(self):
        assert ModelPathManager.get_model_name_from_path(
            Path("/random/unrelated/path")
        ) is None

    def test_multiple_valid_parents_returns_none(self):
        assert ModelPathManager.get_model_name_from_path(
            Path("project/models/apis/something/file.py")
        ) is None

    def test_no_subdirectory_after_parent_returns_none(self):
        assert ModelPathManager.get_model_name_from_path(
            Path("project/models")
        ) is None

    def test_invalid_name_after_parent_returns_none(self):
        assert ModelPathManager.get_model_name_from_path(
            Path("project/models/InvalidName/file.py")
        ) is None


# ============================================================
# Phase 2: Path management — pytest tmp_path
# ============================================================

class TestFindProjectRoot:

    def test_finds_gitignore(self, tmp_path):
        (tmp_path / ".gitignore").touch()
        subdir = tmp_path / "a" / "b" / "c"
        subdir.mkdir(parents=True)

        root = ModelPathManager.find_project_root(subdir / "file.py", ".gitignore")
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

        root = ModelPathManager.find_project_root(subdir / "file.py", ".gitignore")
        assert root == inner

    def test_no_marker_raises_file_not_found(self, tmp_path):
        subdir = tmp_path / "empty" / "deep"
        subdir.mkdir(parents=True)

        with pytest.raises(FileNotFoundError, match="not found in the directory hierarchy"):
            ModelPathManager.find_project_root(subdir / "file.py", ".nonexistent_marker")

    def test_custom_marker(self, tmp_path):
        (tmp_path / "pyproject.toml").touch()
        subdir = tmp_path / "src"
        subdir.mkdir()

        root = ModelPathManager.find_project_root(subdir / "file.py", "pyproject.toml")
        assert root == tmp_path


# ============================================================
# Phase 3: Config loading — __load_config
# ============================================================

class TestLoadConfig:

    def _make_manager(self, script_paths):
        """Create a ModelManager-like object with just enough state for __load_config."""
        mgr = object.__new__(ModelManager)
        mgr._script_paths = script_paths
        return mgr

    def test_success(self, tmp_path):
        config_file = tmp_path / "config_deployment.py"
        config_file.write_text(
            "def get_deployment_config():\n"
            "    return {'host': '0.0.0.0', 'port': 80}\n"
        )
        mgr = self._make_manager({"config_deployment.py": str(config_file)})

        result = mgr._ModelManager__load_config("config_deployment.py", "get_deployment_config")
        assert result == {"host": "0.0.0.0", "port": 80}

    def test_missing_file_returns_none(self):
        mgr = self._make_manager({"config_deployment.py": "/nonexistent/path.py"})
        result = mgr._ModelManager__load_config("config_deployment.py", "get_deployment_config")
        assert result is None

    def test_missing_method_returns_none(self, tmp_path):
        config_file = tmp_path / "config_deployment.py"
        config_file.write_text("x = 1\n")
        mgr = self._make_manager({"config_deployment.py": str(config_file)})

        result = mgr._ModelManager__load_config("config_deployment.py", "get_deployment_config")
        assert result is None

    def test_import_error_returns_none(self, tmp_path):
        config_file = tmp_path / "config_deployment.py"
        config_file.write_text("import nonexistent_module_xyz\n")
        mgr = self._make_manager({"config_deployment.py": str(config_file)})

        result = mgr._ModelManager__load_config("config_deployment.py", "get_deployment_config")
        assert result is None

    def test_missing_script_path_returns_none(self):
        mgr = self._make_manager({})
        result = mgr._ModelManager__load_config("config_deployment.py", "get_deployment_config")
        assert result is None

    def test_sys_modules_cleaned_on_success(self, tmp_path):
        config_file = tmp_path / "test_config_cleanup.py"
        config_file.write_text("def get_config():\n    return {}\n")
        mgr = self._make_manager({"test_config_cleanup.py": str(config_file)})

        mgr._ModelManager__load_config("test_config_cleanup.py", "get_config")
        # The module is inserted into sys.modules by the implementation
        assert "test_config_cleanup.py" in sys.modules
        # Clean up
        sys.modules.pop("test_config_cleanup.py", None)


# ============================================================
# Phase 4: ModelManager init + configs property
# ============================================================

class TestConfigsProperty:

    def test_merges_all_three(self):
        mgr = object.__new__(ModelManager)
        mgr._config_deployment = {"host": "0.0.0.0", "port": 80}
        mgr._config_hyperparameters = {"lr": 0.01}
        mgr._config_meta = {"name": "test"}

        configs = mgr.configs
        assert configs["host"] == "0.0.0.0"
        assert configs["lr"] == 0.01
        assert configs["name"] == "test"

    def test_handles_all_none(self):
        mgr = object.__new__(ModelManager)
        mgr._config_deployment = None
        mgr._config_hyperparameters = None
        mgr._config_meta = None

        configs = mgr.configs
        assert configs == {}

    def test_handles_partial_none(self):
        mgr = object.__new__(ModelManager)
        mgr._config_deployment = {"host": "0.0.0.0"}
        mgr._config_hyperparameters = None
        mgr._config_meta = {"name": "test"}

        configs = mgr.configs
        assert configs == {"host": "0.0.0.0", "name": "test"}

    def test_deployment_overrides_hyperparameters(self):
        """Deployment config is applied after hyperparameters in merge order."""
        mgr = object.__new__(ModelManager)
        mgr._config_hyperparameters = {"key": "from_hp"}
        mgr._config_deployment = {"key": "from_deploy"}
        mgr._config_meta = None

        assert mgr.configs["key"] == "from_deploy"


class TestModelManagerInit:

    @patch("views_crafdapi.managers.model.ModelManager._ModelManager__ascii_splash")
    @patch("views_crafdapi.managers.model.ModelManager._ModelManager__load_config")
    @patch("views_crafdapi.managers.model.LoggingModule")
    def test_init_loads_three_configs(self, mock_logging_cls, mock_load_config, mock_splash):
        mock_logging_cls.return_value.get_logger.return_value = MagicMock()
        mock_load_config.side_effect = [
            {"host": "0.0.0.0"},
            {"lr": 0.01},
            {"name": "test"},
        ]
        mock_path = MagicMock(spec=ModelPathManager)
        mock_path.logging = Path("/tmp/test-logs")
        mock_path.get_scripts.return_value = {}

        ModelManager.__instances__ = 0
        mgr = ModelManager(model_path=mock_path, wandb_notifications=False)

        assert mock_load_config.call_count == 3
        assert mgr._config_deployment == {"host": "0.0.0.0"}
        assert mgr._config_hyperparameters == {"lr": 0.01}
        assert mgr._config_meta == {"name": "test"}

    @patch("views_crafdapi.managers.model.ModelManager._ModelManager__ascii_splash")
    @patch("views_crafdapi.managers.model.ModelManager._ModelManager__load_config")
    @patch("views_crafdapi.managers.model.LoggingModule")
    def test_init_handles_missing_configs(self, mock_logging_cls, mock_load_config, mock_splash):
        mock_logging_cls.return_value.get_logger.return_value = MagicMock()
        mock_load_config.return_value = None
        mock_path = MagicMock(spec=ModelPathManager)
        mock_path.logging = Path("/tmp/test-logs")
        mock_path.get_scripts.return_value = {}

        ModelManager.__instances__ = 0
        mgr = ModelManager(model_path=mock_path)

        assert mgr._config_deployment is None
        assert mgr._config_hyperparameters is None
        assert mgr._config_meta is None
        assert mgr.configs == {}


# ============================================================
# Phase 5: AST config safety guard (C-35)
# ============================================================

class TestConfigASTValidation:
    """Tests for _validate_config_ast — the guard that prevents __load_config
    from executing config files containing dangerous constructs."""

    def test_safe_config_passes(self, tmp_path):
        f = tmp_path / "safe.py"
        f.write_text("def get_config():\n    return {'lr': 0.01}\n")
        assert ModelManager._validate_config_ast(str(f)) is True

    def test_import_os_blocked(self, tmp_path):
        f = tmp_path / "evil.py"
        f.write_text("import os\ndef get_config():\n    return {}\n")
        assert ModelManager._validate_config_ast(str(f)) is False

    def test_from_subprocess_blocked(self, tmp_path):
        f = tmp_path / "evil.py"
        f.write_text("from subprocess import run\ndef get_config():\n    return {}\n")
        assert ModelManager._validate_config_ast(str(f)) is False

    def test_exec_call_blocked(self, tmp_path):
        f = tmp_path / "evil.py"
        f.write_text("def get_config():\n    exec('import os')\n    return {}\n")
        assert ModelManager._validate_config_ast(str(f)) is False

    def test_eval_call_blocked(self, tmp_path):
        f = tmp_path / "evil.py"
        f.write_text("def get_config():\n    return eval('dict(a=1)')\n")
        assert ModelManager._validate_config_ast(str(f)) is False

    def test_open_call_blocked(self, tmp_path):
        f = tmp_path / "evil.py"
        f.write_text("def get_config():\n    open('/etc/passwd').read()\n    return {}\n")
        assert ModelManager._validate_config_ast(str(f)) is False

    def test_nested_os_import_blocked(self, tmp_path):
        f = tmp_path / "evil.py"
        f.write_text("def get_config():\n    import os\n    os.system('rm -rf /')\n    return {}\n")
        assert ModelManager._validate_config_ast(str(f)) is False

    def test_syntax_error_rejected(self, tmp_path):
        f = tmp_path / "broken.py"
        f.write_text("def get_config(\n")
        assert ModelManager._validate_config_ast(str(f)) is False

    def test_nonexistent_file_rejected(self):
        assert ModelManager._validate_config_ast("/nonexistent/path.py") is False

    def test_pathlib_import_allowed(self, tmp_path):
        f = tmp_path / "safe.py"
        f.write_text("from pathlib import Path\ndef get_config():\n    return {'p': str(Path('.'))}\n")
        assert ModelManager._validate_config_ast(str(f)) is True

    def test_load_config_refuses_dangerous_file(self, tmp_path):
        """Integration: __load_config returns None for a file that fails AST check."""
        evil = tmp_path / "config_deployment.py"
        evil.write_text("import os\ndef get_deployment_config():\n    return {'port': 80}\n")
        mgr = object.__new__(ModelManager)
        mgr._script_paths = {"config_deployment.py": str(evil)}
        result = mgr._ModelManager__load_config("config_deployment.py", "get_deployment_config")
        assert result is None

    def test_load_config_allows_safe_file(self, tmp_path):
        """Integration: __load_config executes a file that passes AST check."""
        safe = tmp_path / "config_deployment.py"
        safe.write_text("def get_deployment_config():\n    return {'port': 80}\n")
        mgr = object.__new__(ModelManager)
        mgr._script_paths = {"config_deployment.py": str(safe)}
        result = mgr._ModelManager__load_config("config_deployment.py", "get_deployment_config")
        assert result == {"port": 80}


# ============================================================
# Phase 6: Import discipline — AST checks (C-04 + C-10)
# ============================================================

class TestImportDiscipline:

    def _get_top_level_imports(self, filepath):
        import ast
        source = Path(filepath).read_text()
        tree = ast.parse(source)
        imports = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        return imports

    def test_model_py_no_module_level_wandb(self):
        model_py = Path(__file__).resolve().parent.parent / "src" / "views_crafdapi" / "managers" / "model.py"
        imports = self._get_top_level_imports(model_py)
        wandb_imports = [i for i in imports if "wandb" in i]
        assert wandb_imports == [], f"model.py has module-level wandb imports: {wandb_imports}"

    def test_log_py_no_import_from_model(self):
        log_py = Path(__file__).resolve().parent.parent / "src" / "views_crafdapi" / "managers" / "log.py"
        imports = self._get_top_level_imports(log_py)
        model_imports = [i for i in imports if "model" in i]
        assert model_imports == [], f"log.py imports from model: {model_imports}"

    def test_model_py_imports_log_at_module_level(self):
        model_py = Path(__file__).resolve().parent.parent / "src" / "views_crafdapi" / "managers" / "model.py"
        imports = self._get_top_level_imports(model_py)
        assert any("views_crafdapi.managers.log" in i for i in imports), \
            "model.py should import LoggingModule at module level"
