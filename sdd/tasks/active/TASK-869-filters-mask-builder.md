# TASK-869: Mask builder in `types/dt/filters.py` (no eval strings)

**Feature**: FEAT-166 — MultiQuery Filter without `eval` (eval-free DataFrame masks)
**Spec**: `sdd/specs/filter-eval-hardening.spec.md`
**Status**: pending
**Priority**: high
**Estimated effort**: L (4-8h)
**Depends-on**: TASK-867
**Assigned-to**: unassigned

---

## Context

Spec §3 Module 2 — the core of FEAT-166. `build_condition`
(`querysource/types/dt/filters.py:22-171`) returns Python source text with the
request value interpolated unescaped, and `create_filter` (`filters.py:205-248`)
returns a list of such strings that `Filter.run` and `Join` pass to `eval()`.
This task makes the builders return boolean `pd.Series` masks evaluated
directly with pandas, following the expression table in spec §2, removes the
`func`/`function` expression (spec D4) and adds `combine_conditions`. The two
existing test files that call `eval(" & ".join(conditions))` are ported in the
same task because the return type changes under them.

---

## Scope

- Rewrite `build_condition`, `create_filter_chain`, `create_filter` to return
  masks; add `combine_conditions(conditions, operator='&')`.
- Regex expressions go through `re2_search` / `check_regex_safety` (TASK-867).
- Validate `expression` in the `$column` branch against `comparison_operators`.
- Remove the `getFunction` import and the `func`/`function` branch; raise
  `QueryException("Filter: expression 'func' is not supported")`.
- Port `tests/test_column_filters.py` (15 `eval` sites) and
  `tests/test_join_with_column_filter.py` (10 `eval` sites) to
  `df.loc[combine_conditions(conditions, '&')]`.
- Create `tests/test_filter_eval_hardening.py` with the builder-level cases
  from spec §4 (operator-level cases are appended by TASK-870).

**NOT in scope**: `flt.py` / `Join.py` call sites (TASK-870), the catalog enum
(TASK-871), `drop_columns`/`drop_rows`/`drop_duplicates`/… helpers below line 260
(unchanged).

---

## Files to Create / Modify

| File | Action | Description |
|---|---|---|
| `querysource/types/dt/filters.py` | MODIFY | masks instead of strings; `combine_conditions`; `func` removed |
| `tests/test_column_filters.py` | MODIFY | port 15 `eval` sites to mask reduction |
| `tests/test_join_with_column_filter.py` | MODIFY | port 10 `eval` sites to mask reduction |
| `tests/test_filter_eval_hardening.py` | CREATE | builder-level regression tests |

---

## Codebase Contract (Anti-Hallucination)

### Verified Imports
```python
from querysource.exceptions import QueryException                        # verified: querysource/exceptions.py:6 (already imported at filters.py:9)
from querysource.types.dt.regex import check_regex_safety, re2_search    # provided by TASK-867 (querysource/types/dt/regex.py)
from querysource.types.dt.filters import create_filter, build_condition, create_filter_chain   # verified: filters.py:22,173,205
import pandas as pd; import numpy as np                                   # verified: filters.py:6-7
```

### Existing Signatures to Use
```python
# querysource/types/dt/filters.py  (base 6e85f734)
from ...utils.getfunc import getFunction                                              # line 10  → REMOVE
valid_operators = ['+', '-', '*', '/', '%', '==', '!=', '>', '<', '>=', '<=', '/', '//'] # line 13 (keep)
comparison_operators = frozenset({'==', '!=', '>', '<', '>=', '<='})                    # line 19 (keep)
def build_condition(expression: str, column: str, value, condition: dict, df: pd.DataFrame = None) -> str   # line 22
#   $column branch 37-46 (expression NOT validated) · gt/lt 48-54 · is_null/not_null/is_empty 55-60
#   numeric 61-69 · str 70-109 (regex 74-79, contains/startswith/endswith/==/!= 80-108)
#   datetime 110-123 · list 124-171 (startswith/endswith tuples, contains escaped alternation,
#   regex joined with "|", fullmatch escaped, ==/!= isin, >/>= min, </<= max)
def create_filter_chain(expression: list, column: str, df: pd.DataFrame) -> list       # line 173 (np.logical_and.reduce over strings — broken)
def create_filter(_filter: list, df: pd.DataFrame) -> list                            # line 205; column checks 219-228; func branch 238-242; chain 243-247

# tests/test_column_filters.py — 15× `result = df.loc[eval(" & ".join(conditions))]` (first at line 32)
# tests/test_join_with_column_filter.py — 10× same line (first at line 60), each preceded by `df = joined_df  # Make df available for eval`
```

### Does NOT Exist
- ~~`querysource.types.dt.filters.combine_conditions`~~ — created by THIS task.
- ~~`pd.Series.str.len()` on non-string dtype~~ — raises `AttributeError`; cast with `.astype("string")` first.
- ~~`str.startswith(..., case=False)`~~ — pandas `startswith`/`endswith` have no `case` kwarg; only `na`.
- ~~`Series.str.match(..., engine=...)`~~ — no RE2 selector in pandas; use `re2_search` with an explicit `^(?:…)` anchor.
- ~~`getFunction` in filters.py after this task~~ — import removed; nothing in this file may resolve functions by name.

---

## Complexity Contract

```json
{
  "schema_version": 1,
  "targets": [
    {"path": "querysource/types/dt/filters.py", "action": "MODIFY"},
    {"path": "tests/test_column_filters.py", "action": "MODIFY"},
    {"path": "tests/test_join_with_column_filter.py", "action": "MODIFY"},
    {"path": "tests/test_filter_eval_hardening.py", "action": "CREATE"}
  ],
  "contract_symbols": [
    "sym:querysource/types/dt/filters.py#build_condition",
    "sym:querysource/types/dt/filters.py#create_filter",
    "sym:querysource/types/dt/filters.py#create_filter_chain",
    "sym:querysource/types/dt/regex.py#re2_search",
    "sym:querysource/types/dt/regex.py#check_regex_safety",
    "sym:querysource/exceptions.py#QueryException"
  ]
}
```

---

## Implementation Notes

### Pattern to Follow
`querysource/qsurl/residual.py:21-28` (`_OPS` dict of `operator.*`) and
`residual.py:101-144` (`_leaf_mask`) — one `if` per expression, each returning a
`pd.Series`.

### Key Constraints
- Preserve today's row selection for every expression (spec G2 and §2 table):
  `contains` is case-insensitive and literal; scalar `regex` is start-anchored
  (`str.match`), `fullmatch` fully anchored, list `regex` unanchored.
- `df` is now required by `build_condition` (masks are eager).
- Always pass `na=False` to `str.*` accessors (spec §7 gotcha).
- Error messages keep today's prefixes (`"Invalid expression: …"`,
  `"tFilter: Column … not found in DataFrame."`, `"Referenced column '…' not found in DataFrame."`).

### References in Codebase
- `querysource/qsurl/residual.py` — mask-building pattern
- `tests/test_column_filters.py:376-400` — existing error-message assertions that must keep passing

---

## Implementation Blueprint

### Steps (in order)
1. Replace the import at `filters.py:10` and add the `operator`/`functools`
   imports and the `_COMPARISONS`/`_LOGICAL` tables — *why*: masks are combined
   with real callables, never with operator text.
2. Rewrite `build_condition` per the spec §2 table (block below) — *why*: every
   row of that table is a decided semantic; do not add or drop expressions.
3. Add `combine_conditions`; rewrite `create_filter_chain` and `create_filter`
   (second block) — *why*: callers in TASK-870 rely on these exact signatures.
4. Port the two existing test files: replace each
   `result = df.loc[eval(" & ".join(conditions))]` with
   `result = df.loc[combine_conditions(conditions, '&')]`, add
   `combine_conditions` to their `from querysource.types.dt.filters import …`
   line, and delete the now-useless `df = joined_df  # Make df available for eval`
   lines in the join file (use `joined_df.loc[...]`) — *why*: the return type
   changed; the assertions themselves must not change (G2).
5. Create `tests/test_filter_eval_hardening.py` and complete the `FILL IN`s.
6. Run `ruff check querysource/types/dt/filters.py` and the validation commands.

### `querysource/types/dt/filters.py` (MODIFY — header + `build_condition`)
```python
# occurrences: 1 (verified: grep -c 'from ...utils.getfunc import getFunction' querysource/types/dt/filters.py)
# REPLACE `from ...utils.getfunc import getFunction` (filters.py:10) with:
import operator as _op
from collections.abc import Callable
from functools import reduce
from .regex import check_regex_safety, re2_search

# AFTER `comparison_operators = frozenset({...})` (filters.py:19), add:
_COMPARISONS: dict[str, Callable] = {
    '==': _op.eq, '!=': _op.ne, '>': _op.gt, '<': _op.lt, '>=': _op.ge, '<=': _op.le,
}
_LOGICAL: dict[str, Callable] = {'&': _op.and_, '|': _op.or_}
_REGEX_EXPRESSIONS = frozenset({'regex', 'not_regex', 'fullmatch'})


def _text(series: pd.Series) -> pd.Series:
    """Return ``series`` as the pandas ``string`` dtype for ``.str`` accessors."""
    return series.astype("string")


# occurrences: 1 (verified: grep -c 'def build_condition(' querysource/types/dt/filters.py)
# REPLACE the whole function at filters.py:22-171 with:
def build_condition(expression: str, column: str, value, condition: dict, df: pd.DataFrame) -> pd.Series:
    """Build one boolean mask for ``df[column] <expression> value``.

    See FEAT-166 spec §2 for the expression table. No string is ever built
    or evaluated; the mask is computed with pandas/operator callables.

    Raises:
        QueryException: invalid expression for the value type, ``$column``
            reference missing from ``df``, non-numeric ``gt``/``lt`` value,
            unsafe or invalid regex.
    """
    col = df[column]
    if isinstance(value, dict) and "$column" in value:
        ref_column = value["$column"]
        if ref_column not in df.columns:
            raise QueryException(f"Referenced column '{ref_column}' not found in DataFrame.")
        if expression not in _COMPARISONS:
            raise QueryException(
                f"Invalid expression '{expression}' for column reference on '{column}'. "
                f"Allowed: {sorted(comparison_operators)}"
            )
        return _COMPARISONS[expression](col, df[ref_column])
    if expression in ("gt", "lt"):
        if not isinstance(value, (int, float)):
            raise QueryException("Value must be numeric for column length filtering.")
        lengths = _text(col).str.len()
        return lengths > value if expression == "gt" else lengths < value
    if expression == "is_null":
        return col.isnull() | (col == '')
    if expression == "not_null":
        return ~(col.isnull() | (col == ''))
    if expression == "is_empty":
        return col == ''
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        if expression not in comparison_operators:
            raise QueryException(
                f"Invalid expression '{expression}' for numeric value on column "
                f"'{column}'. Allowed: {sorted(comparison_operators)}"
            )
        return _COMPARISONS[expression](col, value)
    if isinstance(value, str):
        # FILL IN: str branch — regex/not_regex/fullmatch via check_regex_safety + re2_search
        #   (regex → "^(?:" + value + ")", not_regex → ~, fullmatch → "^(?:" + value + ")$");
        #   contains/not_contains → _text(col).str.contains(re.escape(value), case=False, na=False, regex=True);
        #   startswith/endswith (+not_) → _text(col).str.startswith(value, na=False) …;
        #   ==/!= and the other comparison_operators → _COMPARISONS; anything else →
        #   QueryException(f"Invalid expression: {expression}") — bounded by spec §2 table, AC4, AC8
        raise NotImplementedError
    if isinstance(value, (np.datetime64, np.timedelta64, datetime.date)):
        if expression not in comparison_operators:
            raise QueryException(
                f"Invalid expression '{expression}' for datetime value on column "
                f"'{column}'. Allowed: {sorted(comparison_operators)}"
            )
        right = pd.Timestamp(value) if isinstance(value, datetime.date) else value
        return _COMPARISONS[expression](col, right)
    if isinstance(value, list):
        # FILL IN: list branch — startswith/endswith (+not_) with tuple(value) and na=False;
        #   contains/not_contains → re2_search(col, "|".join(re.escape(v) for v in value), ignore_case=True);
        #   regex/not_regex → check_regex_safety on each item, re2_search(col, "|".join(f"(?:{p})" …)) unanchored;
        #   fullmatch → re2_search(col, "^(?:" + "|".join(map(re.escape, value)) + ")$");
        #   == → col.isin(value); != → ~col.isin(value); >/>= → vs min(value); </<= → vs max(value);
        #   anything else → QueryException(f"tFilter: Invalid expression: {expression}") — bounded by spec §2 table
        raise NotImplementedError
    raise QueryException(f"Invalid expression: {expression}")
```
**Why this shape**: each branch mirrors the old string branch one-to-one but
returns the evaluated mask; the `$column` branch gains the `comparison_operators`
check (AC9); `bool` is excluded from the numeric branch because `isinstance(True, int)`
is true in Python. Signatures are fixed by spec §3 M2.

### `querysource/types/dt/filters.py` (MODIFY — `combine_conditions`, chain, `create_filter`)
```python
# occurrences: 1 (verified: grep -c 'def create_filter_chain(' querysource/types/dt/filters.py)
# REPLACE filters.py:173-203 (`create_filter_chain`) with the two functions below:
def combine_conditions(conditions: list[pd.Series], operator: str = '&') -> pd.Series:
    """Reduce boolean masks with ``&`` (AND) or ``|`` (OR).

    Raises:
        QueryException: ``operator`` not in ``('&', '|')`` or ``conditions`` empty.
    """
    if operator not in _LOGICAL:
        raise QueryException(f"Filter: invalid logical operator '{operator}', expected '&' or '|'")
    if not conditions:
        raise QueryException("Filter: no conditions to combine")
    return reduce(_LOGICAL[operator], conditions)


def create_filter_chain(expression: list, column: str, df: pd.DataFrame) -> pd.Series:
    """Build and AND-reduce the masks of a chained expression list for one column."""
    masks = []
    for condition in expression:
        expr = condition.get('expression', '==')
        value = condition.get('value', None)
        masks.append(build_condition(expr, column, value, condition, df))
    return combine_conditions(masks, '&')


# occurrences: 1 (verified: grep -c 'def create_filter(' querysource/types/dt/filters.py)
# REPLACE filters.py:205-248 (`create_filter`) with:
def create_filter(_filter: list, df: pd.DataFrame) -> list[pd.Series]:
    """Return one boolean mask per filter spec (``func``/``function`` are rejected).

    Raises:
        QueryException: missing/unknown column, unsupported expression.
    """
    conditions: list[pd.Series] = []
    for condition in _filter:
        column = condition.get('column')
        if not column:
            raise QueryException("Column name is required for filtering.")
        if column not in df.columns:
            raise QueryException(f"tFilter: Column {column} not found in DataFrame.")
        expression = condition.get('expression', '==')
        value = condition.get('value', None)
        if expression in ('func', 'function'):
            raise QueryException(f"Filter: expression '{expression}' is not supported")
        if isinstance(expression, list):
            conditions.append(create_filter_chain(expression, column, df))
            continue
        conditions.append(build_condition(expression, column, value, condition, df))
    return conditions
```
**Why**: spec D1 keeps `create_filter`'s name and arity with a new return type;
D4 removes `func`; `create_filter_chain` finally works (it reduced strings before).

### `tests/test_filter_eval_hardening.py` (CREATE)
```python
"""FEAT-166: builder-level regression tests — values are data, never code."""
import pandas as pd
import pytest

from querysource.exceptions import QueryException
from querysource.types.dt.filters import build_condition, combine_conditions, create_filter


@pytest.fixture
def text_df():
    return pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "name": ["alice", "bob's", 'c"d', "x.*y", None],
        "status": ["active", "inactive", "active", "active')", None],
        "amount": [10, 20, 30, 40, 50],
    })


@pytest.mark.parametrize("expression,value,expected_ids", [
    ("==", "bob's", [2]),
    ("==", "active')", [4]),
    ("contains", "'", [2]),
    ("contains", '"', [3]),
    ("contains", ".*", [4]),
    ("startswith", "x.*", [4]),
    ("endswith", "')", [4]),
])
def test_quote_in_value_is_literal(text_df, expression, value, expected_ids):
    column = "status" if value in ("active')", "')") else "name"
    mask = build_condition(expression, column, value, {}, text_df)
    assert isinstance(mask, pd.Series) and mask.dtype == bool
    assert list(text_df.loc[mask, "id"]) == expected_ids


def test_column_ref_invalid_expression_raises(text_df):
    with pytest.raises(QueryException, match="column reference"):
        build_condition("contains", "name", {"$column": "status"}, {}, text_df)


def test_func_expression_rejected(text_df):
    with pytest.raises(QueryException, match="not supported"):
        create_filter([{"column": "name", "expression": "func", "value": "today"}], text_df)


def test_combine_conditions_and_or(text_df):
    a = text_df["amount"] > 15
    b = text_df["status"] == "active"
    assert list(text_df.loc[combine_conditions([a, b], '&'), "id"]) == [3, 4]
    assert list(text_df.loc[combine_conditions([a, b], '|'), "id"]) == [1, 2, 3, 4, 5]
    with pytest.raises(QueryException):
        combine_conditions([a, b], 'and')
    with pytest.raises(QueryException):
        combine_conditions([], '&')


def test_build_condition_returns_series_for_every_expression(text_df):
    # FILL IN: iterate the spec §2 table (scalar, list, datetime, is_null/not_null/is_empty, gt/lt,
    #   regex/not_regex/fullmatch) and assert each returns a bool pd.Series aligned to text_df.index — bounded by AC2
    raise NotImplementedError


def test_list_values_isin_min_max(text_df):
    # FILL IN: list == → isin, != → ~isin, >= → min(value), <= → max(value); assert selected ids — bounded by spec §2 list rows
    raise NotImplementedError


def test_regex_expressions_use_re2(text_df):
    # FILL IN: regex "^al" vs "al" anchoring, fullmatch, lookaround pattern raises QueryException("invalid regex") — bounded by AC8
    raise NotImplementedError
```
**Why this shape**: AC4 is the headline security property; the parametrised
literal cases cover every quoting path the old string builder interpolated.

### FILL IN checklist
- [ ] `filters.py::build_condition` str branch — per spec §2 table; bounded by AC4/AC8.
- [ ] `filters.py::build_condition` list branch — per spec §2 table; bounded by G2.
- [ ] `test_filter_eval_hardening.py::test_build_condition_returns_series_for_every_expression` — bounded by AC2.
- [ ] `test_filter_eval_hardening.py::test_list_values_isin_min_max` — bounded by spec §2.
- [ ] `test_filter_eval_hardening.py::test_regex_expressions_use_re2` — bounded by AC8.

---

## Acceptance Criteria

- [ ] `grep -c "eval(" querysource/types/dt/filters.py` → 0; `grep -c getFunction querysource/types/dt/filters.py` → 0
- [ ] `build_condition` returns `pd.Series` (bool) for every expression in spec §2; `create_filter` returns `list[pd.Series]` (AC2)
- [ ] All tests pass: `pytest tests/test_column_filters.py tests/test_join_with_column_filter.py tests/test_filter_eval_hardening.py -v` (the two ported files keep every original assertion)
- [ ] No linting errors: `ruff check querysource/types/dt/filters.py tests/test_filter_eval_hardening.py`
- [ ] `$column` with a non-comparison expression raises `QueryException` (AC9)

---

## Validation Commands

- `pytest tests/test_filter_eval_hardening.py -q`
- `pytest tests/test_column_filters.py -q`
- `pytest tests/test_join_with_column_filter.py -q`

---

## Test Specification

See the `tests/test_filter_eval_hardening.py` block in the blueprint; the ported
files keep their existing assertions.

---

## Agent Instructions

When you pick up this task:

1. **Work in the feature worktree** — never on `base_branch`
   (`python -m scripts.sdd.ensure_worktree --slug filter-eval-hardening --feature-id FEAT-166`)
2. **Read the spec** at the path listed above for full context
3. **Check dependencies** — TASK-867 must be `"done"` in `sdd/tasks/index/filter-eval-hardening.json`
4. **Verify the Codebase Contract** — before writing ANY code:
   - Confirm every import in "Verified Imports" still exists (`grep` or `read` the source)
   - Re-run the `grep -c` for each anchor; if a count changed, re-locate the anchor
   - If anything has changed, update the contract FIRST, then implement
5. **Update status** in `sdd/tasks/index/filter-eval-hardening.json` → `"in-progress"`
   (set `started_at`) and commit only that index file
6. **Implement** — start from the Implementation Blueprint blocks, complete every
   `# FILL IN:` marker, and never change a signature or path the blueprint fixes
7. **Verify** all acceptance criteria are met — run the Validation Commands
8. **Commit the code** — stage only the files this task lists (never `git add .` / `-A`)
9. **Close the task** with `scripts/sdd/close_task.sh TASK-869 filter-eval-hardening verified`
10. **Fill in the Completion Note** below, then commit the staged SDD state

---

## Completion Note

*(Agent fills this in when done)*

**Completed by**: <session or agent ID>
**Date**: YYYY-MM-DD
**Notes**:

**Deviations from spec**: none
