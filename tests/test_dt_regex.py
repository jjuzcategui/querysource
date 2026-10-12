"""FEAT-166 TASK-867: RE2-only regex helper for DataFrame filters."""
import pandas as pd
import pytest

from querysource.exceptions import QueryException
from querysource.types.dt import regex as dtregex


@pytest.fixture
def col():
    return pd.Series(["abc", "xab", None, "ABC", 12], index=[10, 11, 12, 13, 14])


def test_re2_search_literal_and_anchor(col):
    mask = dtregex.re2_search(col, "^(?:ab)")
    assert list(mask.index) == [10, 11, 12, 13, 14]
    assert mask.dtype == bool
    # anchored at start, case-sensitive, null never matches
    assert list(mask) == [True, False, False, False, False]


def test_re2_search_unanchored_and_non_string(col):
    mask = dtregex.re2_search(col, "ab")
    assert list(mask) == [True, True, False, False, False]
    numbers = dtregex.re2_search(col, "^1")
    assert list(numbers) == [False, False, False, False, True]


def test_re2_search_ignore_case(col):
    mask = dtregex.re2_search(col, "^(?:ab)", ignore_case=True)
    assert list(mask) == [True, False, False, True, False]


def test_re2_search_rejects_lookaround(col):
    with pytest.raises(QueryException, match="invalid regex"):
        dtregex.re2_search(col, "(?=a)(a|a)+$")


def test_re2_search_rejects_backreference(col):
    with pytest.raises(QueryException, match="invalid regex"):
        dtregex.re2_search(col, r"(a)\1")


def test_check_regex_safety_caps():
    with pytest.raises(QueryException, match="too long"):
        dtregex.check_regex_safety("a" * 201)
    with pytest.raises(QueryException, match="nested quantifier"):
        dtregex.check_regex_safety("(a+)+")
    with pytest.raises(QueryException, match="must be a string"):
        dtregex.check_regex_safety(["a"])
    dtregex.check_regex_safety("^[a-z]+$")  # must not raise


def test_re2_search_fails_closed_without_pyarrow(col, monkeypatch):
    monkeypatch.setattr(dtregex, "pc", None)
    with pytest.raises(QueryException, match="require pyarrow"):
        dtregex.re2_search(col, "a")


def test_module_never_uses_python_re_on_patterns():
    """Only the static nested-quantifier screen may use ``re``."""
    from pathlib import Path

    source = Path(dtregex.__file__).read_text(encoding="utf-8")
    assert "str.contains" not in source
    assert "re.search(pattern" not in source
    assert "re.match(" not in source
