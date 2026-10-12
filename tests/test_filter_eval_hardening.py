"""FEAT-166: builder-level regression tests — values are data, never code."""
import datetime as _dt
from decimal import Decimal

import pandas as pd
import pytest

from querysource.exceptions import QueryException
from querysource.types.dt.filters import (
    build_condition,
    combine_conditions,
    create_filter,
    create_filter_chain,
)


@pytest.fixture
def text_df():
    return pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "name": ["alice", "bob's", 'c"d', "x.*y", None],
        "status": ["active", "inactive", "active", "active')", None],
        "amount": [10, 20, 30, 40, 50],
        "when": pd.to_datetime(["2024-01-10", "2024-02-15", "2024-03-20", "2024-04-01", "2024-05-05"]),
    })


def _ids(df, mask):
    assert isinstance(mask, pd.Series)
    assert mask.dtype == bool
    assert list(mask.index) == list(df.index)
    return list(df.loc[mask, "id"])


# --------------------------------------------------------------------------- #
# AC4 — quoting / metacharacters are literal data
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("column,expression,value,expected_ids", [
    ("name", "==", "bob's", [2]),
    ("status", "==", "active')", [4]),
    ("status", "!=", "active')", [1, 2, 3, 5]),
    ("name", "contains", "'", [2]),
    ("name", "contains", '"', [3]),
    ("name", "contains", ".*", [4]),
    ("name", "not_contains", ".*", [1, 2, 3, 5]),
    ("name", "startswith", "x.*", [4]),
    ("status", "endswith", "')", [4]),
    ("name", "==", "') | (df['amount'] > 0) | ('", []),
    ("name", "contains", "') or True or ('", []),
    ("name", "startswith", "__import__('os')", []),
])
def test_quote_in_value_is_literal(text_df, column, expression, value, expected_ids):
    mask = build_condition(expression, column, value, {}, text_df)
    assert _ids(text_df, mask) == expected_ids


def test_contains_is_case_insensitive_and_null_safe(text_df):
    assert _ids(text_df, build_condition("contains", "name", "ALICE", {}, text_df)) == [1]
    # null rows never match, and never raise
    assert _ids(text_df, build_condition("startswith", "name", "a", {}, text_df)) == [1]
    assert _ids(text_df, build_condition("not_startswith", "name", "a", {}, text_df)) == [2, 3, 4, 5]


# --------------------------------------------------------------------------- #
# AC9 / AC6 — expression validation and func removal
# --------------------------------------------------------------------------- #
def test_column_ref_invalid_expression_raises(text_df):
    with pytest.raises(QueryException, match="column reference"):
        build_condition("contains", "name", {"$column": "status"}, {}, text_df)


def test_column_ref_valid_expression(text_df):
    mask = build_condition("==", "name", {"$column": "status"}, {}, text_df)
    assert _ids(text_df, mask) == []


@pytest.mark.parametrize("expression", ["func", "function"])
def test_func_expression_rejected(text_df, expression):
    with pytest.raises(QueryException, match="not supported"):
        create_filter([{"column": "name", "expression": expression, "value": "today"}], text_df)


@pytest.mark.parametrize("expression", ["+", "-", "*", "/", "//", "%", "nope", "__class__"])
def test_non_boolean_operators_rejected_for_strings(text_df, expression):
    with pytest.raises(QueryException, match="Invalid expression"):
        build_condition(expression, "name", "alice", {}, text_df)


# --------------------------------------------------------------------------- #
# AC2 — every expression returns a bool Series aligned to the index
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("column,expression,value,expected_ids", [
    ("amount", "==", 30, [3]),
    ("amount", "!=", 30, [1, 2, 4, 5]),
    ("amount", ">", 30, [4, 5]),
    ("amount", ">=", 30, [3, 4, 5]),
    ("amount", "<", 30, [1, 2]),
    ("amount", "<=", 30, [1, 2, 3]),
    ("amount", ">=", Decimal("30.5"), [4, 5]),
    ("amount", "==", True, []),
    ("when", ">=", pd.Timestamp("2024-03-01"), [3, 4, 5]),
    ("when", "<", _dt.date(2024, 2, 16), [1, 2]),
    ("when", "<", _dt.datetime(2024, 2, 16), [1, 2]),  # noqa: DTZ001 — naive column
    ("name", "is_null", None, [5]),
    ("name", "not_null", None, [1, 2, 3, 4]),
    ("name", "is_empty", None, []),
    ("name", "gt", 4, [1, 2]),
    ("name", "lt", 4, [3]),
    ("name", "regex", "^a", [1]),
    ("name", "regex", "a", [1]),            # str.match semantics: anchored at start
    ("name", "not_regex", "a", [2, 3, 4, 5]),
    ("name", "fullmatch", "ali", []),
    ("name", "fullmatch", "ali.*", [1]),
    ("name", "regex", "[a-z]+'s", [2]),
])
def test_build_condition_returns_series_for_every_expression(text_df, column, expression, value, expected_ids):
    mask = build_condition(expression, column, value, {}, text_df)
    assert _ids(text_df, mask) == expected_ids


@pytest.mark.parametrize("column,expression,value,expected_ids", [
    ("amount", "==", [10, 50], [1, 5]),
    ("amount", "!=", [10, 50], [2, 3, 4]),
    ("amount", ">=", [40, 20], [2, 3, 4, 5]),   # min(value)
    ("amount", "<=", [40, 20], [1, 2, 3, 4]),   # max(value)
    ("amount", ">", [40, 20], [3, 4, 5]),
    ("amount", "<", [40, 20], [1, 2, 3]),
    ("name", "startswith", ["al", "bo"], [1, 2]),
    ("name", "not_startswith", ["al", "bo"], [3, 4, 5]),
    ("name", "endswith", ["'s", "y"], [2, 4]),
    ("name", "not_endswith", ["'s", "y"], [1, 3, 5]),
    ("name", "contains", ["LICE", ".*"], [1, 4]),     # literal, case-insensitive
    ("name", "not_contains", ["LICE", ".*"], [2, 3, 5]),
    ("name", "regex", ["^al", "y$"], [1, 4]),          # unanchored alternation
    ("name", "not_regex", ["^al", "y$"], [2, 3, 5]),
    ("name", "fullmatch", ["alice", "x.*y"], [1, 4]),  # literals
])
def test_list_values_isin_min_max(text_df, column, expression, value, expected_ids):
    mask = build_condition(expression, column, value, {}, text_df)
    assert _ids(text_df, mask) == expected_ids


def test_list_invalid_expression_raises(text_df):
    with pytest.raises(QueryException, match="Invalid expression"):
        build_condition("nope", "name", ["a"], {}, text_df)


# --------------------------------------------------------------------------- #
# AC8 — regex only through RE2
# --------------------------------------------------------------------------- #
def test_regex_expressions_use_re2(text_df, monkeypatch):
    with pytest.raises(QueryException, match="invalid regex"):
        build_condition("regex", "name", "(?=a)(a|a)+$", {}, text_df)
    with pytest.raises(QueryException, match="nested quantifier"):
        build_condition("regex", "name", "(a+)+$", {}, text_df)
    with pytest.raises(QueryException, match="too long"):
        build_condition("fullmatch", "name", "a" * 201, {}, text_df)
    with pytest.raises(QueryException, match="nested quantifier"):
        build_condition("regex", "name", ["ok", "(a+)+$"], {}, text_df)

    from querysource.types.dt import regex as dtregex
    monkeypatch.setattr(dtregex, "pc", None)
    with pytest.raises(QueryException, match="require pyarrow"):
        build_condition("regex", "name", "a", {}, text_df)
    with pytest.raises(QueryException, match="require pyarrow"):
        build_condition("contains", "name", ["a"], {}, text_df)


# --------------------------------------------------------------------------- #
# combine_conditions / chains / create_filter
# --------------------------------------------------------------------------- #
def test_combine_conditions_and_or(text_df):
    a = text_df["amount"] > 15
    b = text_df["status"] == "active"
    assert list(text_df.loc[combine_conditions([a, b], '&'), "id"]) == [3]
    assert list(text_df.loc[combine_conditions([a, b], '|'), "id"]) == [1, 2, 3, 4, 5]
    assert list(text_df.loc[combine_conditions([a]), "id"]) == [2, 3, 4, 5]
    with pytest.raises(QueryException, match="logical operator"):
        combine_conditions([a, b], 'and')
    with pytest.raises(QueryException, match="logical operator"):
        combine_conditions([a, b], '& (df["amount"] > 0) | ')
    with pytest.raises(QueryException, match="no conditions"):
        combine_conditions([], '&')


def test_create_filter_returns_masks_and_chain_ands(text_df):
    masks = create_filter([
        {"column": "amount", "expression": ">", "value": 15},
        {"column": "status", "expression": "==", "value": "active"},
    ], text_df)
    assert len(masks) == 2 and all(isinstance(m, pd.Series) and m.dtype == bool for m in masks)
    assert list(text_df.loc[combine_conditions(masks, '&'), "id"]) == [3]

    chain = create_filter_chain(
        [{"expression": ">", "value": 15}, {"expression": "<", "value": 45}], "amount", text_df
    )
    assert _ids(text_df, chain) == [2, 3, 4]

    via_filter = create_filter(
        [{"column": "amount", "expression": [{"expression": ">", "value": 15}, {"expression": "<", "value": 45}]}],
        text_df,
    )
    assert _ids(text_df, via_filter[0]) == [2, 3, 4]


def test_create_filter_column_errors(text_df):
    with pytest.raises(QueryException, match="required"):
        create_filter([{"expression": "==", "value": 1}], text_df)
    with pytest.raises(QueryException, match="not found"):
        create_filter([{"column": "nope", "expression": "==", "value": 1}], text_df)
    with pytest.raises(QueryException, match="Referenced column"):
        create_filter([{"column": "name", "expression": "==", "value": {"$column": "nope"}}], text_df)


def test_no_eval_in_filters_source():
    from pathlib import Path

    import querysource.types.dt.filters as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "eval(" not in source
    assert "getFunction" not in source
