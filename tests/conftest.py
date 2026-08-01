import io

import numpy as np
import pandas as pd
import pytest
from pathlib import Path
from unittest.mock import Mock

from views_faoapi.data.handlers import FAO_PGMDataset


_METADATA_COLS = FAO_PGMDataset._METADATA_COLS

# Root of the views-platform monorepo checkout (…/views_platform), the parent
# of this repo. Cross-repo falsification probes read sibling-repo source under it.
PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent


def pytest_runtest_setup(item):
    """Skip tests marked ``requires_sibling_repos`` when a named sibling repo
    directory is absent under :data:`PLATFORM_ROOT`.

    Cross-repo falsification probes (the ``test_falsify_*`` suite) inspect the
    source of sibling repos (views-pipeline-core, views-datafactory, …). Those
    siblings exist in a full monorepo checkout but not in a clean CI checkout of
    this repo alone. This one hook lets a probe declare its sibling dependency
    *once* as a marker, so it SKIPS (not errors, not xfails) when the sibling is
    absent — keeping the suite green-by-default everywhere (register C-74; the
    xfail-vs-skip convention is documented in TESTING.md).
    """
    for marker in item.iter_markers(name="requires_sibling_repos"):
        missing = [repo for repo in marker.args if not (PLATFORM_ROOT / repo).exists()]
        if missing:
            pytest.skip(
                f"sibling repo(s) not available under {PLATFORM_ROOT}: {', '.join(missing)}"
            )


def make_fao_df(n_cells=4, n_months=2, n_samples=10, seed=42, targets=("pred_test", "pred_other")):
    """Synthetic forecast frame. ``targets`` names the per-cell sample columns; the default
    ``pred_test``/``pred_other`` are deliberately series-less (they exercise the reduction, which
    is naming-agnostic). Endpoint / consumer-naming tests pass real stems, e.g.
    ``targets=("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os")``, so the boundary rename
    (schema.series_of) resolves sb/ns/os."""
    rng = np.random.default_rng(seed)
    rows = n_cells * n_months
    data = {
        "pg_xcoord": [10.0, 11.0, 12.0, 13.0][:n_cells] * n_months,
        "pg_ycoord": [20.0, 21.0, 22.0, 23.0][:n_cells] * n_months,
        "country_iso_a3": ["AAA", "AAA", "BBB", "BBB"][:n_cells] * n_months,
        "admin1_gaul1_code": [1, 1, 2, 2][:n_cells] * n_months,
        "admin1_gaul1_name": ["Region1", "Region1", "Region2", "Region2"][:n_cells] * n_months,
        "admin1_gaul0_code": [10, 10, 20, 20][:n_cells] * n_months,
        "admin1_gaul0_name": ["Province1", "Province1", "Province2", "Province2"][:n_cells] * n_months,
        "admin2_gaul2_code": [100, 101, 200, 201][:n_cells] * n_months,
        "admin2_gaul2_name": ["District1", "District2", "District3", "District4"][:n_cells] * n_months,
        **{t: [rng.random(n_samples) for _ in range(rows)] for t in targets},
    }
    df = pd.DataFrame(data)
    month_ids = []
    cell_ids = list(range(100, 100 + n_cells))
    for m in range(600, 600 + n_months):
        month_ids.extend([m] * n_cells)
    df.index = pd.MultiIndex.from_arrays(
        [month_ids, cell_ids * n_months],
        names=["month_id", "priogrid_id"],
    )
    return df


@pytest.fixture
def rng():
    """Deterministic random number generator for reproducible tests."""
    return np.random.default_rng(42)


@pytest.fixture
def mock_path_manager():
    """Mock ModelPathManager for tests that need path management."""
    manager = Mock()
    manager.cache = Path("/tmp/test_cache")
    manager.model_name = "test_model"
    return manager


@pytest.fixture
def fao_dataset():
    """Minimal FAO_PGMDataset for aggregation and subset testing."""
    df = make_fao_df()
    return FAO_PGMDataset(df)


@pytest.fixture
def parquet_bytes():
    """Valid parquet bytes from a FAO-compatible DataFrame."""
    df = make_fao_df()
    buf = io.BytesIO()
    df.to_parquet(buf)
    return buf.getvalue()
