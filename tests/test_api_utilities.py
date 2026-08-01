"""Unit tests for api.py utility functions: parameter parsing, numpy conversion, DataFrame serialization."""

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException

from views_faoapi.managers.serialization import (
    convert_numpy_types,
    dataframe_to_dict,
    flatten_numeric_list_columns,
    parse_list_param,
    parse_string_list_param,
)

pytestmark = pytest.mark.layer2_data


class TestParseListParam:

    def test_valid_comma_separated(self):
        assert parse_list_param("530,531,532") == [530, 531, 532]

    def test_none_returns_none(self):
        assert parse_list_param(None) is None

    def test_empty_representations(self):
        for empty in ("", "[]", "null", "None"):
            assert parse_list_param(empty) is None

    def test_single_value(self):
        assert parse_list_param("530") == [530]

    def test_whitespace_handling(self):
        assert parse_list_param(" 530 , 531 ") == [530, 531]

    def test_non_integer_raises_422(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_list_param("abc")
        assert exc_info.value.status_code == 422


class TestParseStringListParam:

    def test_valid_comma_separated(self):
        assert parse_string_list_param("pred_sb,pred_ns") == ["pred_sb", "pred_ns"]

    def test_none_returns_none(self):
        assert parse_string_list_param(None) is None

    def test_empty_representations(self):
        for empty in ("", "[]", "null", "None"):
            assert parse_string_list_param(empty) is None

    def test_single_value(self):
        assert parse_string_list_param("pred_sb") == ["pred_sb"]

    def test_whitespace_handling(self):
        assert parse_string_list_param(" pred_sb , pred_ns ") == ["pred_sb", "pred_ns"]


class TestConvertNumpyTypes:

    def test_numpy_int(self):
        result = convert_numpy_types(np.int64(42))
        assert result == 42
        assert isinstance(result, int)

    def test_numpy_float(self):
        result = convert_numpy_types(np.float64(3.14))
        assert result == 3.14
        assert isinstance(result, float)

    def test_numpy_array(self):
        result = convert_numpy_types(np.array([1, 2, 3]))
        assert result == [1, 2, 3]

    def test_nan_becomes_none(self):
        assert convert_numpy_types(np.float64(np.nan)) is None

    def test_inf_becomes_none(self):
        assert convert_numpy_types(np.float64(np.inf)) is None

    def test_native_nan_becomes_none(self):
        assert convert_numpy_types(float("nan")) is None

    def test_nested_dict(self):
        obj = {"a": np.int64(1), "b": [np.float64(2.0)]}
        result = convert_numpy_types(obj)
        assert result == {"a": 1, "b": [2.0]}
        assert isinstance(result["a"], int)
        assert isinstance(result["b"][0], float)


class TestFlattenNumericListColumns:

    def test_single_element_flattened(self):
        df = pd.DataFrame({"col": [[5.2], [3.1]]})
        result = flatten_numeric_list_columns(df)
        assert result["col"].tolist() == [5.2, 3.1]

    def test_multi_element_preserved(self):
        df = pd.DataFrame({"col": [[1, 2, 3], [4, 5, 6]]})
        result = flatten_numeric_list_columns(df)
        assert result["col"].tolist() == [[1, 2, 3], [4, 5, 6]]

    def test_string_column_skipped(self):
        df = pd.DataFrame({"col": ["hello", "world"]})
        result = flatten_numeric_list_columns(df)
        assert result["col"].tolist() == ["hello", "world"]

    def test_empty_dataframe(self):
        df = pd.DataFrame({"col": pd.Series([], dtype=object)})
        result = flatten_numeric_list_columns(df)
        assert result.empty

    def test_mixed_columns(self):
        df = pd.DataFrame({
            "single": [[5.2], [3.1]],
            "multi": [[1, 2], [3, 4]],
            "text": ["a", "b"],
        })
        result = flatten_numeric_list_columns(df)
        assert result["single"].tolist() == [5.2, 3.1]
        assert result["multi"].tolist() == [[1, 2], [3, 4]]
        assert result["text"].tolist() == ["a", "b"]


class TestDataframeToDict:

    def test_basic_conversion(self):
        index = pd.MultiIndex.from_tuples(
            [(500, 100001)], names=["month_id", "priogrid_id"]
        )
        df = pd.DataFrame({"value": [42.0]}, index=index)
        result = dataframe_to_dict(df)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["month_id"] == 500
        assert result[0]["priogrid_id"] == 100001
        assert result[0]["value"] == 42.0

    def test_numpy_types_converted(self):
        df = pd.DataFrame({"col": [np.int64(42)]})
        result = dataframe_to_dict(df)
        assert isinstance(result[0]["col"], int)

    def test_nan_values_become_none(self):
        df = pd.DataFrame({"col": [float("nan")]})
        result = dataframe_to_dict(df)
        assert result[0]["col"] is None
