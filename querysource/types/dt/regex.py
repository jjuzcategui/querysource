"""RE2-only regex evaluation for in-memory DataFrame filters.

User-supplied patterns are evaluated exclusively with
``pyarrow.compute.match_substring_regex`` (RE2, linear time). Python's
backtracking ``re`` engine never sees the pattern (FEAT-164 / FEAT-166).
"""
from __future__ import annotations

import re

import pandas as pd

try:
    import pyarrow as pa
    import pyarrow.compute as pc
except ImportError:  # pragma: no cover - pyarrow ships with the `parquet` extra
    pa = None
    pc = None

from ...exceptions import QueryException

MAX_REGEX_PATTERN_LENGTH: int = 200
_NESTED_QUANTIFIER_RE = re.compile(r"\([^()]*[+*][^()]*\)[+*]")


def check_regex_safety(pattern: str) -> None:
    """Reject a pattern that is too long or has the classic nested-quantifier shape.

    Args:
        pattern: user-supplied regex.

    Raises:
        QueryException: ``"regex pattern too long (N > 200 chars)"`` or
            ``"regex pattern `<p>` has a nested quantifier that risks catastrophic backtracking"``.
    """
    if not isinstance(pattern, str):
        raise QueryException(
            f"regex pattern must be a string, got {type(pattern).__name__}"
        )
    if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
        raise QueryException(
            f"regex pattern too long ({len(pattern)} > {MAX_REGEX_PATTERN_LENGTH} chars)"
        )
    if _NESTED_QUANTIFIER_RE.search(pattern):
        raise QueryException(
            f"regex pattern `{pattern}` has a nested quantifier that risks catastrophic backtracking"
        )


def re2_search(col: pd.Series, pattern: str, *, ignore_case: bool = False) -> pd.Series:
    """Search ``pattern`` in ``col`` with RE2 only (never Python ``re``).

    Anchoring is the caller's responsibility (prefix ``^(?:`` … ``)``, suffix ``$``).

    Args:
        col: column to match; non-string values are matched on their string form.
        pattern: user-supplied regex (RE2 syntax), already screened by ``check_regex_safety``.
        ignore_case: case-insensitive match when True.

    Returns:
        Boolean ``pd.Series`` aligned to ``col.index`` (dtype ``bool``); nulls never match.

    Raises:
        QueryException: pyarrow missing (``"regex filters require pyarrow (RE2 engine), which is not installed"``)
            or RE2 rejecting the pattern (``"invalid regex `<pattern>`: <arrow error>"``).
    """
    if pc is None:
        raise QueryException(
            "regex filters require pyarrow (RE2 engine), which is not installed"
        )
    arrow = pa.array(col.astype(pd.StringDtype("pyarrow")))
    try:
        matched = pc.match_substring_regex(arrow, pattern=pattern, ignore_case=ignore_case)
    except pa.ArrowInvalid as err:
        raise QueryException(f"invalid regex `{pattern}`: {err}") from err
    return pd.Series(
        matched.fill_null(False).to_numpy(zero_copy_only=False),
        index=col.index,
        dtype=bool,
    )
