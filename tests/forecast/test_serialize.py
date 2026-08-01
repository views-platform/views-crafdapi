"""Phase 4a (#112): the extracted serialize/json_contract formatters lay numpy results onto
the served `{var}_*` column contract."""

import numpy as np
import pandas as pd
import pytest

from views_crafdapi.forecast.serialize.json_contract import hdi_dataframe, map_dataframe

pytestmark = pytest.mark.layer2_data

_T = pd.Index([600, 601])
_E = pd.Index([100, 101])


def test_map_dataframe_columns_and_values():
    vals = np.array([[1.0, 2.0], [3.0, 4.0]])  # (time, entity)
    out = map_dataframe("pred_x", vals, "month_id", "priogrid_id", _T, _E)
    assert list(out.columns) == ["pred_x_map"]
    assert out.index.names == ["month_id", "priogrid_id"]
    assert out.loc[(600, 100), "pred_x_map"] == 1.0
    assert out.loc[(601, 101), "pred_x_map"] == 4.0


def test_hdi_dataframe_columns_and_values():
    lower = np.array([1.0, 2.0])
    upper = np.array([3.0, 4.0])
    out = hdi_dataframe("pred_x", lower, upper, "month_id", "priogrid_id", pd.Index([600]), _E)
    assert list(out.columns) == ["pred_x_hdi_lower", "pred_x_hdi_upper"]
    assert out.index.names == ["month_id", "priogrid_id"]
    assert out.loc[(600, 100), "pred_x_hdi_lower"] == 1.0
    assert out.loc[(600, 101), "pred_x_hdi_upper"] == 4.0


def test_explicit_ids_override_defaults():
    out = map_dataframe(
        "v", np.array([[9.0]]), "month_id", "priogrid_id", _T, _E,
        time_ids=700, entity_ids=999,
    )
    assert out.loc[(700, 999), "v_map"] == 9.0
