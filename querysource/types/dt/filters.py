"""Functions for FilterRows.

Condition builders (``build_condition``, ``create_filter``, ``create_filter_chain``,
``combine_conditions``) return boolean ``pd.Series`` masks evaluated directly with
pandas/``operator`` callables. No predicate string is ever built or ``eval``uated
(FEAT-166). Regex expressions run on RE2 only (``querysource.types.dt.regex``).
"""
import datetime
import operator as _op
import re
from collections.abc import Callable
from decimal import Decimal
from functools import reduce
from typing import Optional

import numpy as np
import pandas as pd
from navconfig.logging import logging

from ...exceptions import QueryException
from .regex import check_regex_safety, re2_search

valid_operators = ['+', '-', '*', '/', '%', '==', '!=', '>', '<', '>=', '<=', '/', '//']

# Operators that produce a boolean mask suitable for filtering rows.
comparison_operators = frozenset({'==', '!=', '>', '<', '>=', '<='})

_COMPARISONS: dict[str, Callable] = {
    '==': _op.eq, '!=': _op.ne, '>': _op.gt, '<': _op.lt, '>=': _op.ge, '<=': _op.le,
}
_LOGICAL: dict[str, Callable] = {'&': _op.and_, '|': _op.or_}
_REGEX_EXPRESSIONS = frozenset({'regex', 'not_regex', 'fullmatch'})


def _text(series: pd.Series) -> pd.Series:
    """Return ``series`` as the pandas ``string`` dtype for ``.str`` accessors."""
    return series.astype("string")


def _bool(mask: pd.Series) -> pd.Series:
    """Normalise a (possibly nullable) boolean mask: ``NA`` never selects a row."""
    return mask.fillna(False).astype(bool)


def _regex_pattern(expression: str, value: str) -> str:
    """Anchor a scalar regex per the historical ``str.match``/``str.fullmatch`` semantics."""
    check_regex_safety(value)
    if expression == 'fullmatch':
        return f"^(?:{value})$"
    return f"^(?:{value})"


def _str_condition(expression: str, col: pd.Series, value: str) -> pd.Series:
    """Mask for a scalar string ``value`` (see FEAT-166 spec §2)."""
    if expression in _REGEX_EXPRESSIONS:
        mask = re2_search(col, _regex_pattern(expression, value))
        return ~mask if expression == 'not_regex' else mask
    text = _text(col)
    if expression in ('contains', 'not_contains'):
        mask = text.str.contains(re.escape(value), case=False, na=False, regex=True)
        return ~mask if expression == 'not_contains' else mask
    if expression in ('startswith', 'not_startswith'):
        mask = text.str.startswith(value, na=False)
        return ~mask if expression == 'not_startswith' else mask
    if expression in ('endswith', 'not_endswith'):
        mask = text.str.endswith(value, na=False)
        return ~mask if expression == 'not_endswith' else mask
    if expression in _COMPARISONS:
        return _COMPARISONS[expression](col, value)
    raise QueryException(f"Invalid expression: {expression}")


def _list_condition(expression: str, col: pd.Series, value: list) -> pd.Series:
    """Mask for a list ``value`` (see FEAT-166 spec §2)."""
    if expression in ('startswith', 'not_startswith', 'endswith', 'not_endswith'):
        text = _text(col)
        items = tuple(str(v) for v in value)
        if expression.endswith('startswith'):
            mask = text.str.startswith(items, na=False)
        else:
            mask = text.str.endswith(items, na=False)
        return ~mask if expression.startswith('not_') else mask
    if expression in ('contains', 'not_contains'):
        pattern = "|".join(re.escape(str(v)) for v in value)
        mask = re2_search(col, pattern, ignore_case=True)
        return ~mask if expression == 'not_contains' else mask
    if expression in ('regex', 'not_regex'):
        for item in value:
            check_regex_safety(item)
        mask = re2_search(col, "|".join(f"(?:{item})" for item in value))
        return ~mask if expression == 'not_regex' else mask
    if expression == 'fullmatch':
        pattern = "|".join(re.escape(str(v)) for v in value)
        return re2_search(col, f"^(?:{pattern})$")
    if expression == '==':
        return col.isin(value)
    if expression == '!=':
        return ~col.isin(value)
    if expression in ('>', '>='):
        return _COMPARISONS[expression](col, min(value))
    if expression in ('<', '<='):
        return _COMPARISONS[expression](col, max(value))
    raise QueryException(f"tFilter: Invalid expression: {expression}")


def build_condition(expression: str, column: str, value, condition: dict, df: pd.DataFrame) -> pd.Series:
    """Build one boolean mask for ``df[column] <expression> value``.

    See the FEAT-166 spec §2 for the expression table. No string is ever built
    or evaluated; the mask is computed with pandas/``operator`` callables.

    Args:
        expression: Comparison, text, null or regex operator.
        column: Column to test (must exist in ``df``).
        value: Scalar, list, datetime or ``{"$column": "other"}`` reference.
        condition: The raw condition dict (kept for signature compatibility).
        df: DataFrame the mask is computed against.

    Returns:
        A boolean ``pd.Series`` aligned to ``df.index``.

    Raises:
        QueryException: invalid expression for the value type, ``$column``
            reference missing from ``df``, non-numeric ``gt``/``lt`` value,
            unsafe or invalid regex.
    """
    col = df[column]
    if isinstance(value, dict) and "$column" in value:
        ref_column = value["$column"]
        if ref_column not in df.columns:
            raise QueryException(
                f"Referenced column '{ref_column}' not found in DataFrame."
            )
        if expression not in _COMPARISONS:
            raise QueryException(
                f"Invalid expression '{expression}' for column reference on "
                f"'{column}'. Allowed: {sorted(comparison_operators)}"
            )
        return _bool(_COMPARISONS[expression](col, df[ref_column]))
    if expression in ("gt", "lt"):
        if not isinstance(value, (int, float)):
            raise QueryException(
                "Value must be numeric for column length filtering."
            )
        lengths = _text(col).str.len()
        return _bool(lengths > value if expression == "gt" else lengths < value)
    if expression == "is_null":
        return _bool(col.isnull() | (col == ''))
    if expression == "not_null":
        return ~_bool(col.isnull() | (col == ''))
    if expression == "is_empty":
        return _bool(col == '')
    if isinstance(value, (int, float, Decimal)):
        if expression not in comparison_operators:
            raise QueryException(
                f"Invalid expression '{expression}' for numeric value on column "
                f"'{column}'. Allowed: {sorted(comparison_operators)}"
            )
        right = float(value) if isinstance(value, Decimal) else value
        return _bool(_COMPARISONS[expression](col, right))
    if isinstance(value, str):
        return _bool(_str_condition(expression, col, value))
    if isinstance(value, (np.datetime64, np.timedelta64, datetime.date)):
        # datetime.date catches datetime.datetime and pd.Timestamp via subclassing.
        if expression not in comparison_operators:
            raise QueryException(
                f"Invalid expression '{expression}' for datetime value on column "
                f"'{column}'. Allowed: {sorted(comparison_operators)}"
            )
        right = pd.Timestamp(value) if isinstance(value, datetime.date) else value
        return _bool(_COMPARISONS[expression](col, right))
    if isinstance(value, list):
        return _bool(_list_condition(expression, col, value))
    raise QueryException(f"Invalid expression: {expression}")


def combine_conditions(conditions: list[pd.Series], operator: str = '&') -> pd.Series:
    """Reduce boolean masks with ``&`` (AND) or ``|`` (OR).

    Args:
        conditions: Masks produced by ``create_filter``/``build_condition``.
        operator: ``'&'`` or ``'|'``.

    Returns:
        A single boolean ``pd.Series``.

    Raises:
        QueryException: ``operator`` not in ``('&', '|')`` or ``conditions`` empty.
    """
    if operator not in _LOGICAL:
        raise QueryException(
            f"Filter: invalid logical operator '{operator}', expected '&' or '|'"
        )
    if not conditions:
        raise QueryException("Filter: no conditions to combine")
    return reduce(_LOGICAL[operator], conditions)


def create_filter_chain(expression: list, column: str, df: pd.DataFrame) -> pd.Series:
    """Build and AND-reduce the masks of a chained expression list for one column.

    Args:
        expression: List of ``{expression, value}`` dicts applied to ``column``.
        column: The column every chained condition tests.
        df: DataFrame the masks are computed against.

    Returns:
        A boolean ``pd.Series`` (logical AND of every chained condition).

    Raises:
        QueryException: invalid expression or empty chain.
    """
    masks = []
    for condition in expression:
        expr = condition.get('expression', '==')
        value = condition.get('value', None)
        masks.append(build_condition(expr, column, value, condition, df))
    return combine_conditions(masks, '&')


def create_filter(_filter: list, df: pd.DataFrame) -> list[pd.Series]:
    """Return one boolean mask per filter spec.

    ``func``/``function`` expressions are rejected (FEAT-166): a filter value is
    data, never a function name.

    Args:
        _filter: Filter dicts, each with ``column``, ``expression`` and ``value``.
        df: DataFrame the masks are computed against.

    Returns:
        A list of boolean ``pd.Series`` aligned to ``df.index``.

    Raises:
        QueryException: missing/unknown column or unsupported expression.
    """
    conditions: list[pd.Series] = []
    for condition in _filter:
        column = condition.get('column')
        if not column:
            raise QueryException(
                "Column name is required for filtering."
            )
        if column not in df.columns:
            raise QueryException(
                f"tFilter: Column {column} not found in DataFrame."
            )
        expression = condition.get('expression', '==')
        value = condition.get('value', None)
        if expression in ('func', 'function'):
            raise QueryException(
                f"Filter: expression '{expression}' is not supported"
            )
        if isinstance(expression, list):
            conditions.append(create_filter_chain(expression, column, df))
            continue
        conditions.append(build_condition(expression, column, value, condition, df))
    return conditions


def drop_columns(df: pd.DataFrame, columns: list = None, endswith: list = None, startswith: list = None):
    """
    This function drops specified columns from a DataFrame based on exact names, suffixes, or prefixes.

    :param dataframe: The DataFrame from which columns will be dropped.
    :param columns: List of exact column names to drop.
    :param endswith: List of suffixes; columns ending with these will be dropped.
    :param startswith: List of prefixes; columns starting with these will be dropped.
    :return: The DataFrame with specified columns dropped.
    """
    if columns and isinstance(columns, list):
        df = df.drop(columns=columns, errors="ignore")
    elif endswith and isinstance(endswith, list):
        cols_to_drop = [col for col in df.columns if str(col).endswith(tuple(endswith))]
        df = df.drop(columns=cols_to_drop)
    elif startswith and isinstance(startswith, list):
        cols_to_drop = [col for col in df.columns if str(col).startswith(tuple(startswith))]
        df = df.drop(columns=cols_to_drop)
    return df


def drop_rows(df: pd.DataFrame, **kwargs):
    """
    This function drops rows from a DataFrame based on specified column values.

    :param df: The DataFrame from which rows will be dropped.
    :param kwargs: Column names and their corresponding values to drop rows.
    :return: The DataFrame with specified rows dropped.
    """
    for column, expression in kwargs.items():
        if isinstance(expression, list):
            mask = df[column].isin(expression)
            df = df[~mask]
            df.head()
    return df

def drop_duplicates(df: pd.DataFrame, columns: Optional[list] = None, **kwargs):
    """
    This function drops duplicate rows from a DataFrame based on specified columns.

    :param dataframe: The DataFrame from which duplicates will be dropped.
    :param columns: List of columns to consider for identifying duplicates.
    :param kwargs: Additional keyword arguments for drop_duplicates method.
    :return: The DataFrame with duplicates dropped.
    """
    if columns and isinstance(columns, list):
        # NOTE: do NOT sort before dropping. ``sort_values(by=columns)`` is
        # unnecessary for ``keep='first'`` (drop_duplicates already keeps the
        # first occurrence in the DataFrame's existing order) and it is fragile:
        # with a multi-column subset, if any column holds mixed/unorderable
        # values (e.g. an object column mixing str and NaN, or ints and str),
        # ``sort_values`` raises ``TypeError: '<' not supported between
        # instances of ...`` — which the Filter operator swallows, so the rows
        # are never de-duplicated and the downstream upsert then fails with a
        # CardinalityViolation. Dropping directly on the subset is order-stable
        # and type-agnostic.
        df = df.drop_duplicates(subset=columns, **kwargs)
    return df


def clean_empty(df: pd.DataFrame, columns: Optional[list] = None):
    """
    This function drops rows from a DataFrame where specified columns are empty, NaN, or contain empty strings.

    :param dataframe: The DataFrame from which rows will be dropped.
    :param columns: List of columns to check for empty values.
    :return: The DataFrame with specified rows dropped.
    """
    if columns and isinstance(columns, list):
        for column in columns:
            condition = df[
                (df[column].empty) | (df[column] == "") | (df[column].isna())
            ].index
            df.drop(condition, inplace=True)
    return df


def suppress(df: pd.DataFrame, columns: Optional[list] = None, **kwargs):
    """
    This function suppresses parts of string values in specified columns based on a regex pattern.

    :param dataframe: The DataFrame containing the columns to be modified.
    :param columns: List of columns to apply the suppression.
    :param kwargs: Additional keyword arguments, including 'pattern' for the regex.
    :return: The DataFrame with suppressed string values.
    """
    pattern = kwargs.get('pattern', None)

    def clean_chars(field):
        name = str(field)
        if not pattern:
            return name
        if re.search(pattern, name):
            pos = re.search(pattern, name).start()
            # return str(name)[:pos]
            return name[:pos]
        else:
            return name

    if columns and isinstance(columns, list):
        for column in columns:
            df[column] = df[column].astype(str)
            df[column] = df[column].apply(clean_chars)
    return df


def fill_na(df: pd.DataFrame, columns: Optional[list] = None, fill_value="", **kwargs):
    """
    This function fills NaN values in specified columns with a given fill value.

    :param df: The DataFrame containing the columns to be filled.
    :param columns: List of columns to fill NaN values.
    :param fill_value: The value to replace NaN values with.
    :param kwargs: Additional keyword arguments.
    :return: The DataFrame with NaN values filled.
    """
    df[columns] = (
        df[columns].astype(str).replace(["nan", np.nan], fill_value, regex=True)
    )
    return df

def fill_nulls(df: pd.DataFrame, field: str, column: str):
    """
    Fills null values in a specified field with values from another column.

    :param df: The DataFrame containing the fields.
    :param field: The name of the field to fill nulls in.
    :param column: The name of the column to use for filling nulls.
    :return: The DataFrame with nulls filled.
    """
    # Check if field exists in DataFrame, if not, create it using the column's values
    if field not in df.columns:
        df[field] = df[column]
    else:
        # Replace empty strings with nulls, only if the field is of string type
        try:
            df[field] = df[field].apply(
                lambda x: x.strip() if isinstance(x, str) else x
            ).replace("", np.nan)
        except Exception as err:
            logging.error("drop_duplicates error: %s", err)

        # Ensure there are no duplicate index labels before filling nulls
        if df.index.duplicated().any():
            logging.warning("Duplicate index labels found. Resetting index to prevent issues.")
            df = df.reset_index(drop=True)

        # Fill null values in the field with values from the specified column
        try:
            df.loc[df[field].isnull(), field] = df[column]
        except KeyError:
            logging.error(f"Fill Nulls: Column {column} doesn't exist")
        except ValueError as e:
            logging.error(f"Error during fill_nulls: {e}")

    return df


def drop_na(df: pd.DataFrame, field: str, columns: Optional[list] = None) -> pd.DataFrame:
    """
    Drops rows with NaN values in a specified field or columns.

    :param df: The DataFrame containing the field or columns.
    :param field: The name of the field to drop NaN values in.
    :param columns: List of columns to drop NaN values in.
    :return: The DataFrame with NaN values dropped.
    """
    if field in df.columns:
        df = df.dropna(subset=[field])
    elif columns:
        df.dropna(subset=columns, inplace=True)
    return df
