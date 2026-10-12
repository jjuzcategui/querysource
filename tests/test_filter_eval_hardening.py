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


# --------------------------------------------------------------------------- #
# Operator level (TASK-870): Filter.run / Join without eval
# --------------------------------------------------------------------------- #
import asyncio  # noqa: E402
from pathlib import Path  # noqa: E402

from querysource.queries.multi.operators.filter.flt import Filter  # noqa: E402
from querysource.queries.multi.operators.Join import Join  # noqa: E402

_SRC = Path(__file__).resolve().parents[1] / "querysource"


def test_no_eval_or_globals_in_filter_sources():
    for rel in (
        "types/dt/filters.py",
        "queries/multi/operators/filter/flt.py",
        "queries/multi/operators/Join.py",
    ):
        text = (_SRC / rel).read_text(encoding="utf-8")
        assert "eval(" not in text, rel
        assert "globals()" not in text, rel
        assert "print(" not in text, rel


async def _run(op):
    async with op as o:
        return await o.run()


def test_filter_run_quote_value_is_literal(text_df):
    op = Filter(
        data=text_df.copy(),
        conditions=[{"column": "status", "expression": "==", "value": "active')"}],
    )
    out = asyncio.run(_run(op))
    assert list(out["id"]) == [4]
    assert isinstance(op.condition, str)
    assert "active')" in op.condition


def test_filter_run_injection_shaped_values_select_nothing(text_df):
    for value in ("') | (df['amount'] > 0) | ('", "__import__('os').system('true')"):
        op = Filter(
            data=text_df.copy(),
            filter=[{"column": "name", "expression": "contains", "value": value}],
        )
        # DataNotFound (a QueryException) — the literal text matches no row.
        with pytest.raises(QueryException, match="No Data was Found"):
            asyncio.run(_run(op))


def test_filter_conditions_unknown_key_raises(text_df):
    op = Filter(data=text_df.copy(), filter_conditions={"not_a_fn": {}})
    with pytest.raises(QueryException, match="unknown filter function"):
        asyncio.run(_run(op))


@pytest.mark.parametrize("name", ["getFunction", "build_condition", "create_filter", "re", "pd", "logging"])
def test_filter_conditions_module_attributes_are_not_functions(text_df, name):
    op = Filter(data=text_df.copy(), filter_conditions={name: {}})
    with pytest.raises(QueryException, match="unknown filter function"):
        asyncio.run(_run(op))


def test_filter_conditions_known_fn_applied(text_df):
    op = Filter(data=text_df.copy(), filter_conditions={"drop_rows": {"status": ["inactive"]}})
    out = asyncio.run(_run(op))
    assert "inactive" not in set(out["status"].dropna())
    assert list(out["id"]) == [1, 3, 4, 5]


def test_filter_conditions_function_error_propagates(text_df):
    op = Filter(data=text_df.copy(), filter_conditions={"drop_columns": {"columns": "not-a-list", "bogus": 1}})
    with pytest.raises(QueryException, match="error applying 'drop_columns'"):
        asyncio.run(_run(op))


def test_filter_operator_or(text_df):
    conds = [
        {"column": "amount", "expression": "<", "value": 15},
        {"column": "status", "expression": "==", "value": "active')"},
    ]
    out = asyncio.run(_run(Filter(data=text_df.copy(), conditions=conds, operator="|")))
    assert list(out["id"]) == [1, 4]
    with pytest.raises(QueryException, match="logical operator"):
        asyncio.run(_run(Filter(data=text_df.copy(), conditions=conds, operator="and")))


def test_join_conditions_no_eval():
    calls = pd.DataFrame({
        "user_id": [1, 2, 3],
        "called_at": pd.to_datetime(["2024-01-10", "2024-02-01", "2024-03-01"]),
        "note": ["ok", "x') | (df['user_id'] > 0) | ('", "ok"],
    })
    users = pd.DataFrame({
        "user_id": [1, 2, 3],
        "created_at": pd.to_datetime(["2024-01-01", "2024-02-15", "2024-02-01"]),
    })
    op = Join(
        data={"calls": calls, "users": users},
        left="calls", right="users", using="user_id", type="inner",
        join_conditions=[{"left": "called_at", "expression": ">=", "right": "created_at"}],
    )
    out = asyncio.run(_run(op))
    assert isinstance(out, pd.DataFrame)
    assert list(out["user_id"]) == [1, 3]

    bad = Join(
        data={"calls": calls.copy(), "users": users.copy()},
        left="calls", right="users", using="user_id",
        join_conditions=[{"left": "called_at", "expression": "contains", "right": "created_at"}],
    )
    with pytest.raises(QueryException):
        asyncio.run(_run(bad))


# --------------------------------------------------------------------------- #
# Review fixes (FEAT-166 adversarial review)
# --------------------------------------------------------------------------- #
def test_suppress_pattern_runs_on_re2(text_df):
    from querysource.types.dt.filters import suppress

    df = pd.DataFrame({"s": ["abc-123", "no-dash", None, "a" * 28 + "!"]})
    out = suppress(df.copy(), columns=["s"], pattern="-")
    assert list(out["s"].iloc[[0, 1, 3]]) == ["abc", "no", "a" * 28 + "!"]
    assert pd.isna(out["s"].iloc[2])
    with pytest.raises(QueryException, match="nested quantifier"):
        suppress(df.copy(), columns=["s"], pattern="(a+)+$")
    with pytest.raises(QueryException, match="invalid regex"):
        suppress(df.copy(), columns=["s"], pattern="(?=a)(a|a)+$")
    # through the operator path as well
    op = Filter(data=df.copy(), filter_conditions={"suppress": {"columns": ["s"], "pattern": "(a+)+$"}})
    with pytest.raises(QueryException, match="nested quantifier"):
        asyncio.run(_run(op))


def test_fullmatch_cannot_escape_anchoring_group(text_df):
    with pytest.raises(QueryException, match="invalid regex"):
        build_condition("fullmatch", "name", "zzz)|(abc", {}, text_df)
    with pytest.raises(QueryException, match="invalid regex"):
        build_condition("regex", "name", ["ok", "zzz)|(abc"], {}, text_df)
    assert _ids(text_df, build_condition("fullmatch", "name", "alice|x.*y", {}, text_df)) == [1, 4]


@pytest.mark.parametrize("expression,value", [
    ({"==": 1}, "alice"),
    (["=="], "alice"),
    ("==", {"$column": ["status"]}),
])
def test_malformed_expression_shapes_raise_query_exception(text_df, expression, value):
    with pytest.raises(QueryException):
        build_condition(expression, "name", value, {}, text_df)


def test_malformed_specs_raise_query_exception(text_df):
    with pytest.raises(QueryException, match="must be an object"):
        create_filter(["name == alice"], text_df)
    with pytest.raises(QueryException, match="required"):
        create_filter([{"column": ["name"], "expression": "==", "value": 1}], text_df)
    with pytest.raises(QueryException, match="not found"):
        build_condition("==", "nope", 1, {}, text_df)
    with pytest.raises(QueryException, match="non-empty list"):
        build_condition(">", "amount", [], {}, text_df)
    with pytest.raises(QueryException, match="mutually comparable"):
        build_condition(">", "amount", [1, "a"], {}, text_df)
    with pytest.raises(QueryException, match="requires a value"):
        build_condition("==", "name", None, {}, text_df)
    with pytest.raises(QueryException, match="cannot compare"):
        build_condition(">", "amount", "ten", {}, text_df)


def test_filter_functions_allowlist_is_module_owned():
    names = Filter.filter_functions()
    for forbidden in ("getFunction", "combine_conditions", "create_filter", "build_condition",
                      "create_filter_chain", "re2_search", "check_regex_safety", "re2_find"):
        assert forbidden not in names, forbidden
    assert {"drop_rows", "drop_na", "suppress", "fill_na"} <= set(names)
