"""Every code cell in every shipped notebook must at least parse.

Why this exists: on 2026-08-15 an edit to `01`/`02`'s preflight cell moved the `:` that ends
an `if` to the end of a trailing comment. Both live notebooks raised `SyntaxError` on their
first executed cell — a worse failure than the 401 the cell was written to replace — and it
was merged.

Nothing could have caught it:

* `.github/workflows/run_pytest.yml` runs nbmake on `notebooks/03_offline_demo.ipynb` **only**,
  because `01`/`02` need live credentials.
* `pyproject.toml` sets `extend-exclude = ["notebooks", "_siblings"]`, so ruff never reads them.

So the two notebooks an external analyst actually follows had no syntax gate at all. This test
is deliberately the weakest possible one — it compiles, it does not execute — so it needs no
API key, no network and no fixtures, and therefore runs everywhere `03` cannot.
"""
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.layer4_infra

NOTEBOOKS = sorted((Path(__file__).parent.parent / "notebooks").glob("*.ipynb"))


def test_there_are_notebooks_to_check():
    """Guard the guard: a glob that silently matches nothing would pass forever."""
    assert NOTEBOOKS, "no notebooks found — this test would be vacuous"


@pytest.mark.parametrize("notebook", NOTEBOOKS, ids=lambda p: p.name)
def test_every_code_cell_parses(notebook: Path):
    doc = json.loads(notebook.read_text())
    failures = []
    for index, cell in enumerate(doc["cells"]):
        if cell.get("cell_type") != "code":
            continue
        source = "".join(cell["source"])
        # IPython magics/shell escapes are not Python; skip those cells rather than
        # teach this test a dialect it does not need to know.
        if any(line.lstrip().startswith(("%", "!")) for line in source.splitlines()):
            continue
        try:
            compile(source, f"{notebook.name}:cell{index}", "exec")
        except SyntaxError as exc:
            failures.append(f"  cell {index}: {exc.msg} (line {exc.lineno})")

    assert not failures, (
        f"{notebook.name} has code cells that cannot be parsed — a reader running from the "
        f"top hits this before anything else:\n" + "\n".join(failures)
    )
