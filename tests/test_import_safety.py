"""ADR-012: Importing modules must not trigger application initialization or side effects."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.layer4_infra

SRC = Path(__file__).resolve().parent.parent / "src"


class TestModuleImportSafety:

    def test_api_module_importable_without_side_effects(self):
        """Importing api.py must not call create_app() at module level."""
        result = subprocess.run(
            [sys.executable, "-c",
             "import views_faoapi.managers.api; print('OK')"],
            capture_output=True, text=True, timeout=10,
            env={**__import__("os").environ, "PYTHONPATH": str(SRC)},
        )
        assert result.returncode == 0, (
            f"Importing api.py failed:\nstderr: {result.stderr}"
        )
        assert "OK" in result.stdout

    def test_log_module_does_not_import_from_api(self):
        """log.py must not import from api.py — use model.py instead (ADR-002 topology)."""
        log_path = SRC / "views_faoapi" / "managers" / "log.py"
        tree = ast.parse(log_path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "views_faoapi.managers.api":
                pytest.fail(
                    f"log.py line {node.lineno}: imports from api.py. "
                    "Must import from model.py instead (ADR-012)."
                )

    def test_prediction_module_no_module_level_dotenv(self):
        """No module in the prediction package may call dotenv.load_dotenv() at
        module level (ADR-012). The package was split from a single prediction.py
        (S10); every file is checked so the invariant can't slip into a submodule."""
        pred_pkg = SRC / "views_faoapi" / "managers" / "prediction"
        for pred_path in sorted(pred_pkg.glob("*.py")):
            tree = ast.parse(pred_path.read_text())
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                    func_src = ast.dump(node.value.func)
                    if "load_dotenv" in func_src:
                        pytest.fail(
                            f"prediction/{pred_path.name} line {node.lineno}: "
                            "module-level load_dotenv() call. Defer to runtime (ADR-012)."
                        )

    def test_model_py_no_module_level_wandb(self):
        """model.py must not import wandb at module level (ADR-002)."""
        model_path = SRC / "views_faoapi" / "managers" / "model.py"
        tree = ast.parse(model_path.read_text())
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "wandb" in alias.name:
                        pytest.fail(
                            f"model.py line {node.lineno}: module-level "
                            f"'import {alias.name}'. Move to point of use (ADR-002)."
                        )
            elif isinstance(node, ast.ImportFrom) and node.module and "wandb" in node.module:
                pytest.fail(
                    f"model.py line {node.lineno}: module-level "
                    f"'from {node.module} import ...'. Move to point of use (ADR-002)."
                )
