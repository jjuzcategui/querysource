---
# SDD flow type and base branch (FEAT-145).
type: feature
base_branch: dev
projects: [multiquery]
tags: [multiquery, filter, join, security, eval, pandas, re2]
---

# Feature Specification: MultiQuery Filter without `eval` (eval-free DataFrame masks)

**Feature ID**: FEAT-166
**Date**: 2026-10-12
**Author**: Jesus Lara (spec drafted with Claude Code)
**Status**: approved
**Target version**: 5.2.4
**Origin**: private security report on the `Filter` operator (code-reading only,
no issue/PR opened). Verified against dev `6e85f734` on 2026-10-12.

---

## 1. Motivation & Business Requirements

### Problem Statement
The MultiQuery `Filter` operator builds its row predicate as **Python source
text** and executes it with `eval()`:

- `querysource/types/dt/filters.py::build_condition` (`filters.py:22-171`)
  returns strings such as `df['{column}'].str.contains(r'{value}', ...)` and
  `(df['{column}'] {expression} {value})`. In the `str` branch the request
  value is wrapped as `'{value}'` with **no escaping** (`filters.py:80-100`);
  in the regex branches it is wrapped as `r'{value}'` (`filters.py:74-79`);
  in the `$column` branch `expression` is interpolated **without validation**
  (`filters.py:46`). The column name is also interpolated as text everywhere.
- `querysource/queries/multi/operators/filter/flt.py:147-152` joins those
  strings with `self._operator` and runs `df.loc[eval(self.condition)]`.
- `querysource/queries/multi/operators/Join.py:194-198` reuses
  `create_filter` for `join_conditions` and also runs `eval()`.

The strings come from the request: `POST /api/v3/queries` receives the whole
pipeline in the body and `MultiQS` instantiates `Filter(data=result, **step)`
(`querysource/queries/multi/__init__.py:1012`); the slug variant of the same
handler turns leftover body keys into a `Filter` too
(`querysource/handlers/multi.py:689-712`). The PBAC pre-flight
(`handlers/multi.py:31-119`) authorises slugs, files, raw queries and write
destinations — it does not inspect operator steps, and it is a no-op when no
`security` guardian is installed on the app. Column-name validation
(`create_filter`, `filters.py:224-228`) and the closed operator list in the
`str` branch do **not** close the hole: the *value* is the injection point,
and a column name can be produced by an earlier `Rename`/`Map` step of the
same request.

Two sibling weaknesses live on the same path:

- `querysource/utils/getfunc.py::getFunction` (`getfunc.py:8-48`) claims a
  whitelist but falls back to `globals()`, to `builtins`, and to
  `importlib.import_module()` of any dotted name. It is reached from
  `create_filter`'s `func`/`function` expression (`filters.py:238-242`),
  from `Map._call_fn` (`transformations/Map.py:142`), from
  `transforms.apply_function` (`types/dt/transforms.py:154`) and from
  `utils/fn.py:63`. The resolved callable is applied to every cell of a
  request-named column. Note that the `func` branch of `create_filter` is
  functionally a **no-op** today (it filters a local `df` that is never
  returned) — it only executes the function.
- `Filter.run` resolves `filter_conditions` keys with
  `getattr(dffunctions, ft)` then `globals()[ft]` (`flt.py:121-124`), never
  checking the `Filter.filter_functions()` allowlist that already exists for
  the catalog, and swallows errors with `print()` (`flt.py:127-128`).

The repository already decided this pattern is unacceptable for the qsurl
residual stage (FEAT-152, 2026-09-24; `querysource/qsurl/residual.py` builds
boolean masks and uses RE2 via pyarrow — see FEAT-164). This spec applies the
same design to `Filter`, `Join` and the shared helpers.

### Goals
- G1. No `eval()` / `exec()` and no string-built predicates remain in
  `types/dt/filters.py`, `operators/filter/flt.py` or `operators/Join.py`.
  `build_condition` returns a boolean `pd.Series`; `create_filter` returns a
  `list[pd.Series]`; masks are combined with `&` / `|` on Series objects.
- G2. Every expression accepted today by `Filter`/`Join` keeps its documented
  semantics (same rows selected) — except `func`/`function`, which is removed
  (G4) — and the existing tests in `tests/test_column_filters.py`,
  `tests/test_join_with_column_filter.py` and `tests/test_catalog_filter.py`
  pass after being ported from `eval(" & ".join(conditions))` to mask
  reduction.
- G3. `regex` / `not_regex` / `fullmatch` run **only** on RE2
  (`pyarrow.compute.match_substring_regex`), with the same length cap and
  nested-quantifier screen as `qsurl/residual.py`; without pyarrow they fail
  closed with `QueryException`.
- G4. `getFunction` resolves names **only** from `querysource.utils.functions`
  (`qsfunctions`); dotted names, builtins and module globals return `None`.
  The `func`/`function` filter expressions are removed from `build_condition`,
  `create_filter` and the `Filter.catalog.yaml` enum.
- G5. `Filter.filter_conditions` keys are validated against
  `Filter.filter_functions()`; unknown keys and function errors raise
  `QueryException` (logged with the module logger, no `print`).
- G6. The `$column` branch validates `expression` against the comparison set.
- G7. Regression tests prove that values containing quote characters, regex
  metacharacters or Python syntax are treated as literal data, and that the
  function resolver rejects dotted / builtin names.

### Non-Goals (explicitly out of scope)
- `querysource/queries/executor.py:159` (RethinkDB `eval` with
  `safe_globals`) — behind `raw_query:execute`; separate ledger issue.
- `querysource/utils/validators.py:84-86` and
  `querysource/types/dt/transforms.py:696` (`globals()[...]` lookups) —
  separate ledger issue.
- PBAC changes (operator-level policies, pre-flight of `sources` keys) —
  compensating controls handled in navigator-api policies / a separate spec.
- Converging `qsurl/residual.py` onto the new RE2 helper (follow-up; residual
  stays untouched here to keep the diff focused).
- The SQL-layer value escaping audit (`parsers/*.pyx` `safe_v`) — separate.
- No new filter expressions; no polars/modin support.

---

## 2. Architectural Design

### Overview
Replace the string builder with a **mask builder**. `build_condition` takes
the same `(expression, column, value, condition, df)` arguments but returns
`df[column] <op> value` evaluated directly in Python:

| Expression | Mask (pandas, no eval) |
|---|---|
| `==`, `!=`, `<`, `<=`, `>`, `>=` with scalar | `_COMPARISONS[expr](df[column], value)` (`operator.eq`, …) |
| same with `{"$column": other}` | `_COMPARISONS[expr](df[column], df[other])`; `expr` **must** be in `_COMPARISONS` |
| same with `datetime.date` / `np.datetime64` | right side normalised with `pd.Timestamp(value)` |
| `==` / `!=` with list | `df[column].isin(value)` / `~…` |
| `>` / `>=` with list | compare against `min(value)`; `<` / `<=` against `max(value)` |
| `gt` / `lt` (numeric value) | `df[column].str.len() > value` / `< value` |
| `is_null` / `not_null` | `df[column].isnull() \| (df[column] == '')` / `~…` |
| `is_empty` | `df[column] == ''` |
| `contains` / `not_contains` (str) | `df[column].str.contains(re.escape(value), case=False, na=False, regex=True)` / `~…` |
| `contains` / `not_contains` (list) | alternation of `re.escape`d items, `case=False`, via RE2 |
| `startswith` / `endswith` (+ `not_`) | `df[column].str.startswith(value)` with `str` or `tuple(value)`; `na=False` |
| `regex` (str) | RE2 search of `^(?:value)` (preserves `str.match` start-anchoring) |
| `not_regex` (str) | `~` of the above |
| `fullmatch` (str) | RE2 search of `^(?:value)$` |
| `regex` / `not_regex` (list) | RE2 search of `(?:p1)\|(?:p2)…` unanchored (preserves today's `str.contains`) |
| `fullmatch` (list) | RE2 search of `^(?:` escaped alternation `)$` |
| anything else | `QueryException("Invalid expression: …")` |

The `str` branch keeps today's rule that the `+ - * / % //` arithmetic
operators of `valid_operators` are **not** accepted for string values (they
were interpolated before but produce no boolean mask); the numeric branch keeps
the `comparison_operators` check that already exists (`filters.py:16-19`).

Masks are combined by a new `combine_conditions(conditions, operator)` helper
with `functools.reduce` over `operator.and_` / `operator.or_`; `operator` is
validated against `{'&', '|'}`. `Filter.run` and `Join._apply_join_conditions`
call `df.loc[mask]`. `Filter.condition` is kept as a **descriptive string**
(for `_applied` / logging) built from the specs, never evaluated.

Regex evaluation lives in a new small module `querysource/types/dt/regex.py`
(`re2_search`, `check_regex_safety`) copied from the FEAT-164 helpers so that
`types/dt` does not import from `qsurl`.

`getFunction` becomes a strict allowlist resolver over `qsfunctions` and the
`func`/`function` expression is deleted. `Filter.run` validates
`filter_conditions` keys with `Filter.filter_functions()`.

### Component Diagram
```
POST /api/v3/queries ──→ MultiQS.run ──→ Filter(data, **step)
                                            │
                                            ├─ filter_conditions ──→ Filter.filter_functions() allowlist ──→ dffunctions.<fn>(df, **args)
                                            │
                                            └─ filter + conditions ──→ create_filter(specs, df) ──→ list[pd.Series]
                                                                              │                         │
                                                                              ├─ build_condition ─→ pd.Series (masks)
                                                                              │        └─ regex/fullmatch ─→ types/dt/regex.re2_search (pyarrow RE2)
                                                                              └─ combine_conditions(masks, '&'|'|') ──→ df.loc[mask]

Join._apply_join_conditions ──→ create_filter(specs, df) ──→ combine_conditions(masks, '&') ──→ df.loc[mask]

Map._call_fn / transforms.apply_function / utils.fn ──→ getFunction(name) ──→ qsfunctions only (else None)
```

### Integration Points
| Existing Component | Integration Type | Notes |
|---|---|---|
| `Filter.run` (`flt.py:83-158`) | modifies | eval removed; allowlist for `filter_conditions`; module logger instead of `print` |
| `Join._apply_join_conditions` (`Join.py:150-215`) | modifies | eval removed; uses `combine_conditions` |
| `create_filter` / `build_condition` / `create_filter_chain` (`filters.py`) | modifies | return masks; `func` removed; `$column` expression validated |
| `getFunction` (`getfunc.py`) | modifies | allowlist-only; same signature, same `None` contract |
| `Filter.catalog.yaml` | modifies | drop `func`/`function` from `filterExpression` enum and from the `value` description |
| `Map._call_fn`, `transforms.apply_function`, `utils/fn.py` | unchanged callers | already handle `None` from `getFunction` |
| `handlers/multi.py`, `queries/multi/__init__.py` | unchanged | `Filter(**step)` API is preserved |
| `tests/test_column_filters.py`, `tests/test_join_with_column_filter.py` | modifies | port `eval(" & ".join(...))` to `combine_conditions(..., '&')` |

### Data Models
No new data models. Condition specs stay `{"column": str, "expression": str | list[str], "value": Any}`.

### New Public Interfaces
```python
# querysource/types/dt/filters.py
def build_condition(expression: str, column: str, value, condition: dict, df: pd.DataFrame) -> pd.Series: ...
def create_filter(_filter: list, df: pd.DataFrame) -> list[pd.Series]: ...
def create_filter_chain(expression: list, column: str, df: pd.DataFrame) -> pd.Series: ...
def combine_conditions(conditions: list[pd.Series], operator: str = '&') -> pd.Series: ...

# querysource/types/dt/regex.py
def check_regex_safety(pattern: str) -> None: ...
def re2_search(col: pd.Series, pattern: str, *, ignore_case: bool = False) -> pd.Series: ...
```

---

## 3. Module Breakdown

#### Delegation-eligible modules

| Module | Eligible? | Decided patterns / exact contracts | Why not (if no) |
|---|---|---|---|
| M1: RE2 helper (`types/dt/regex.py`) | yes | verbatim port of `qsurl/residual.py:40-85` with `QueryException` instead of `QSUrlError` | — |
| M2: mask builder (`types/dt/filters.py`) | yes | expression table in §2, signatures in §3, error messages fixed below | — |
| M3: strict `getFunction` (`utils/getfunc.py`) | yes | allowlist = `qsfunctions` attributes that are callable; dotted/builtins → `None` | — |
| M4: Filter + Join operators | yes | eval sites replaced by `df.loc[combine_conditions(...)]`; `filter_conditions` allowlist | — |
| M5: catalog + docs + tests | yes | enum edit, test port, regression tests listed in §4 | — |

### Module 1: RE2 regex helper
- **Path**: `querysource/types/dt/regex.py` (new)
- **Responsibility**: evaluate user-supplied regex patterns against a Series
  using RE2 only (linear time), with the FEAT-164 safety screen; fail closed
  without pyarrow.
- **Depends on**: `pyarrow` (optional import, same guard as `residual.py:10-15`).
- **Interface Skeleton**:
  ```python
  # querysource/types/dt/regex.py  (new)
  """RE2-only regex evaluation for DataFrame filters (no Python ``re`` backtracking)."""
  from __future__ import annotations
  import re
  import pandas as pd
  try:
      import pyarrow as pa
      import pyarrow.compute as pc
  except ImportError:  # pragma: no cover - pyarrow ships with the `parquet` extra
      pa = None
      pc = None
  from ...exceptions import QueryException  # verified: querysource/exceptions.py:6

  MAX_REGEX_PATTERN_LENGTH: int = 200
  _NESTED_QUANTIFIER_RE = re.compile(r"\([^()]*[+*][^()]*\)[+*]")


  def check_regex_safety(pattern: str) -> None:
      """Reject patterns longer than ``MAX_REGEX_PATTERN_LENGTH`` or with a nested quantifier.

      Raises:
          QueryException: ``"regex pattern too long (...)"`` or ``"regex pattern `...` has a nested quantifier ..."``.
      """


  def re2_search(col: pd.Series, pattern: str, *, ignore_case: bool = False) -> pd.Series:
      """Search ``pattern`` in ``col`` with RE2 (``pyarrow.compute.match_substring_regex``).

      Non-string values are matched on their string form; nulls never match.
      Anchoring is the caller's job (prefix ``^(?:`` … ``)`` / suffix ``$``).

      Returns:
          Boolean ``pd.Series`` aligned to ``col.index`` (dtype ``bool``).

      Raises:
          QueryException: when pyarrow is not installed (``"regex filters require pyarrow (RE2 engine), which is not installed"``)
              or when RE2 rejects the pattern (``"invalid regex `<pattern>`: <arrow error>"``).
      """
  ```

### Module 2: Mask builder in `types/dt/filters.py`
- **Path**: `querysource/types/dt/filters.py`
- **Responsibility**: `build_condition` / `create_filter` /
  `create_filter_chain` return boolean masks; new `combine_conditions`; the
  `func`/`function` expressions and the `getFunction` import are removed.
- **Depends on**: Module 1.
- **Interface Skeleton**:
  ```python
  # querysource/types/dt/filters.py  (modifies filters.py:1-258)
  import operator as _op
  from functools import reduce
  from .regex import check_regex_safety, re2_search   # Module 1
  # REMOVE: from ...utils.getfunc import getFunction   # filters.py:10

  valid_operators = ['+', '-', '*', '/', '%', '==', '!=', '>', '<', '>=', '<=', '/', '//']  # kept, filters.py:13
  comparison_operators = frozenset({'==', '!=', '>', '<', '>=', '<='})                      # kept, filters.py:19
  _COMPARISONS: dict[str, Callable] = {'==': _op.eq, '!=': _op.ne, '>': _op.gt, '<': _op.lt, '>=': _op.ge, '<=': _op.le}
  _LOGICAL: dict[str, Callable] = {'&': _op.and_, '|': _op.or_}


  def build_condition(expression: str, column: str, value, condition: dict, df: pd.DataFrame) -> pd.Series:  # filters.py:22
      """Build one boolean mask for ``df[column] <expression> value`` (see spec §2 table).

      ``df`` is now required (it was Optional) because the mask is evaluated eagerly.

      Raises:
          QueryException: invalid expression for the value type; ``$column`` reference
              not in ``df``; non-numeric value for ``gt``/``lt``; unsafe/invalid regex.
      """


  def combine_conditions(conditions: list[pd.Series], operator: str = '&') -> pd.Series:  # new
      """Reduce masks with ``&`` or ``|``.

      Raises:
          QueryException: ``operator`` not in ``('&', '|')``, or ``conditions`` empty.
      """


  def create_filter_chain(expression: list, column: str, df: pd.DataFrame) -> pd.Series:  # filters.py:173
      """Build and AND-reduce the masks of a chained expression list for one column."""


  def create_filter(_filter: list, df: pd.DataFrame) -> list[pd.Series]:  # filters.py:205
      """Return one boolean mask per spec (``func``/``function`` are no longer accepted).

      Raises:
          QueryException: missing/unknown column; ``expression in ('func', 'function')``
              → ``"Filter: expression 'func' is not supported"``.
      """
  ```

### Module 3: Strict `getFunction`
- **Path**: `querysource/utils/getfunc.py`
- **Responsibility**: resolve a function name only from
  `querysource.utils.functions`; everything else returns `None`.
- **Depends on**: nothing new.
- **Interface Skeleton**:
  ```python
  # querysource/utils/getfunc.py  (modifies getfunc.py:1-48)
  from collections.abc import Callable
  from typing import Optional
  from navconfig.logging import logging              # kept, getfunc.py:4
  from . import functions as qsfunctions             # kept, getfunc.py:5
  # REMOVE: import importlib, import contextlib, import builtins  (getfunc.py:1-3)


  def getFunction(fname: str) -> Optional[Callable]:  # getfunc.py:8
      """Return the callable ``qsfunctions.<fname>`` or ``None``.

      Only public attributes of ``querysource.utils.functions`` are resolvable.
      Names that are not ``str``, start with ``_``, contain ``.`` or are not
      callable return ``None`` (logged at warning level). No fallback to module
      globals, ``builtins`` or ``importlib``.
      """
  ```

### Module 4: `Filter` and `Join` operators without eval
- **Path**: `querysource/queries/multi/operators/filter/flt.py`, `querysource/queries/multi/operators/Join.py`
- **Responsibility**: apply masks via `df.loc[...]`; enforce the
  `filter_conditions` allowlist; log with a module-level `_logger`.
- **Depends on**: Module 2.
- **Interface Skeleton**:
  ```python
  # querysource/queries/multi/operators/filter/flt.py  (modifies flt.py:7, 113-152)
  from .....types.dt.filters import combine_conditions, create_filter   # flt.py:7

  class Filter(AbstractOperator):
      async def run(self):                                                 # flt.py:83
          # filter_conditions (replaces flt.py:115-128):
          #   allowed = set(self.filter_functions())
          #   for ft, args in self.filter_conditions.items():
          #       if ft not in allowed: raise QueryException(f"Filter: unknown filter function '{ft}'")
          #       func = getattr(dffunctions, ft); it = func(it, **args)  — errors → QueryException, logged via module `_logger`
          # conditions (replaces flt.py:146-152):
          #   masks = create_filter(filter_specs, df)
          #   self.condition = f" {self._operator} ".join(_describe(spec) for spec in filter_specs)   # text only
          #   df = df.loc[combine_conditions(masks, self._operator)]
          ...

  # querysource/queries/multi/operators/Join.py  (modifies Join.py:9, 193-198)
  from ....types.dt.filters import combine_conditions, create_filter       # Join.py:9
  # in _apply_join_conditions: df_filtered = df.loc[combine_conditions(create_filter(filter_specs, df), '&')]
  ```

### Module 5: Catalog, docs and tests
- **Path**: `querysource/queries/multi/operators/filter/Filter.catalog.yaml`,
  `tests/test_column_filters.py`, `tests/test_join_with_column_filter.py`,
  `tests/test_catalog_filter.py`, new `tests/test_filter_eval_hardening.py`,
  new `tests/test_getfunc_allowlist.py`, `docs/FILTER_OPERATORS.md` (one
  paragraph noting `func`/`function` removal and RE2 for in-memory regex).
- **Responsibility**: enum update; port tests to masks; regression tests of §4.
- **Depends on**: Modules 1-4.

---

## 4. Test Specification

### Unit Tests
| Test | Module | Description |
|---|---|---|
| `test_re2_search_literal_and_anchor` | M1 | `^(?:ab)` matches only rows starting with `ab`; nulls → False |
| `test_re2_search_rejects_lookaround` | M1 | lookahead pattern raises `QueryException("invalid regex …")` |
| `test_check_regex_safety_caps` | M1 | 201-char pattern and `(a+)+` raise |
| `test_re2_search_fails_closed_without_pyarrow` | M1 | monkeypatch `pc=None` → `QueryException` |
| `test_build_condition_returns_series` | M2 | every §2 row returns a `pd.Series` of dtype bool aligned to `df.index` |
| `test_quote_in_value_is_literal` | M2 | value containing `'`, `"`, `)`, `#` for `==`, `contains`, `startswith`, `endswith` selects only rows equal/containing that literal text |
| `test_regex_metachars_in_contains_are_escaped` | M2 | `contains` with `.*` matches only rows containing the literal `.*` |
| `test_column_ref_invalid_expression_raises` | M2 | `{"$column": …}` with `expression: "contains"` raises `QueryException` |
| `test_func_expression_rejected` | M2 | `expression: "func"` raises `QueryException` and never calls any function |
| `test_combine_conditions_and_or` | M2 | `&` and `|` reduction; bad operator raises; empty list raises |
| `test_list_values_isin_min_max` | M2 | list `==`, `!=`, `>`, `<=` semantics preserved |
| `test_getfunction_allowlist_only` | M3 | `getFunction('today')` is `qsfunctions.today`; dotted names, builtin names, `_private`, non-str → `None` |
| `test_filter_conditions_unknown_key_raises` | M4 | `filter_conditions={"not_a_fn": {}}` → `QueryException` |
| `test_filter_conditions_known_fn_applied` | M4 | existing `drop_rows` case from `tests/test_catalog_filter.py:74` still passes |
| `test_filter_condition_attribute_is_text` | M4 | after `run()`, `Filter.condition` is a string and no `eval` symbol is referenced in `flt.py` / `Join.py` / `filters.py` (source grep assertion) |
| `test_join_conditions_no_eval` | M4 | `Join` with `join_conditions` filters correctly via masks |

### Integration Tests
| Test | Description |
|---|---|
| `tests/test_column_filters.py` (ported) | all 15 existing `create_filter` cases pass with `df.loc[combine_conditions(conditions, '&')]` |
| `tests/test_join_with_column_filter.py` (ported) | all existing cases pass with mask reduction |
| `tests/test_catalog_filter.py` | enum no longer contains `func`/`function`; `conditions`/`filter_conditions` wiring unchanged |

### Test Data / Fixtures
```python
@pytest.fixture
def text_df():
    return pd.DataFrame({
        "id": [1, 2, 3, 4],
        "name": ["alice", "bob's", 'c"d', "x.*y"],
        "status": ["active", "inactive", "active", None],
        "amount": [10, 20, 30, 40],
    })
```

---

## 5. Acceptance Criteria

- [ ] AC1. `grep -n "eval(" querysource/types/dt/filters.py querysource/queries/multi/operators/filter/flt.py querysource/queries/multi/operators/Join.py` returns nothing; `grep -n "globals()" querysource/queries/multi/operators/filter/flt.py querysource/utils/getfunc.py` returns nothing.
- [ ] AC2. `build_condition` returns `pd.Series` (bool) and `create_filter` returns `list[pd.Series]` for every expression in the §2 table; `combine_conditions` exists and validates its operator.
- [ ] AC3. `pytest tests/test_column_filters.py tests/test_join_with_column_filter.py tests/test_catalog_filter.py tests/test_catalog_map.py tests/test_filter_eval_hardening.py tests/test_getfunc_allowlist.py -q` passes (baseline: 64 passed on dev `6e85f734`).
- [ ] AC4. A `Filter` step whose string value contains quote characters, parentheses or regex metacharacters selects exactly the rows whose cell equals/contains that literal text (test `test_quote_in_value_is_literal`).
- [ ] AC5. `getFunction` returns `None` for any dotted name, any `builtins` name and any name not defined in `querysource.utils.functions`; `importlib`, `builtins` and `contextlib` are no longer imported by `getfunc.py`.
- [ ] AC6. `create_filter` raises `QueryException` for `expression in ('func', 'function')`; the catalog enum (`Filter.catalog.yaml`) no longer lists them and `ComponentRegistry.get_catalog()` reflects it.
- [ ] AC7. `Filter.run` raises `QueryException` for a `filter_conditions` key outside `Filter.filter_functions()`; errors inside a filter function propagate as `QueryException` (no `print`).
- [ ] AC8. `regex`/`not_regex`/`fullmatch` are evaluated through `re2_search`; a lookaround pattern raises `QueryException`; with `pc=None` the leaf raises instead of falling back to Python `re`.
- [ ] AC9. `$column` comparisons reject expressions outside `comparison_operators` with `QueryException`.
- [ ] AC10. `ruff check querysource/types/dt/filters.py querysource/types/dt/regex.py querysource/utils/getfunc.py querysource/queries/multi/operators/filter/flt.py querysource/queries/multi/operators/Join.py` passes.
- [ ] AC11. `docs/FILTER_OPERATORS.md` mentions that in-memory (`Filter`/`Join`) regex expressions run on RE2 and that `func`/`function` were removed; `tests/test_filter_operators_docs.py` still passes.
- [ ] AC12. Version bumped to `5.2.4` in `querysource/version.py` (release lane).

---

## 6. Codebase Contract

### Verified Imports
```python
from querysource.types.dt.filters import create_filter, build_condition, create_filter_chain  # verified: querysource/types/dt/filters.py:22,173,205
from querysource.types.dt import filters as dffunctions                 # verified: querysource/queries/multi/operators/filter/flt.py:6
from querysource.utils.getfunc import getFunction                       # verified: querysource/utils/getfunc.py:8
from querysource.utils import functions as qsfunctions                  # verified: querysource/utils/getfunc.py:5 (Cython module utils/functions.pyx)
from querysource.exceptions import QueryException, DataNotFound, DriverError  # verified: querysource/exceptions.py:6,53,74
from querysource.queries.multi.operators.filter.flt import Filter       # verified: tests/test_catalog_filter.py:15
from querysource.queries.multi.operators import Filter                  # verified: querysource/queries/multi/operators/__init__.py:1
from querysource.queries.multi.registry import ComponentRegistry        # verified: tests/test_catalog_filter.py:16
import pyarrow as pa; import pyarrow.compute as pc                      # verified: querysource/qsurl/residual.py:10-15 (pyarrow 25.0.1 installed in dev; optional `parquet` extra, pyproject.toml:170-171)
```

### Existing Class Signatures
```python
# querysource/types/dt/filters.py
valid_operators = ['+', '-', '*', '/', '%', '==', '!=', '>', '<', '>=', '<=', '/', '//']        # line 13
comparison_operators = frozenset({'==', '!=', '>', '<', '>=', '<='})                             # line 19
def build_condition(expression: str, column: str, value, condition: dict, df: pd.DataFrame = None) -> str   # line 22
def create_filter_chain(expression: list, column: str, df: pd.DataFrame) -> list                 # line 173  (returns np.logical_and.reduce over strings — broken today)
def create_filter(_filter: list, df: pd.DataFrame) -> list                                       # line 205  (func branch lines 238-242; chain branch 243-247)
def drop_columns(df, columns=None, endswith=None, startswith=None)                               # line 260 (first of the dffunctions used by filter_conditions)

# querysource/utils/getfunc.py
def getFunction(fname: str) -> callable      # line 8; fallbacks: qsfunctions (21-22), globals() (23-28), builtins (29-32), importlib dotted (34-44)

# querysource/queries/multi/abstract.py
class AbstractMulti:
    def __init__(self, data: Union[dict, pd.DataFrame], **kwargs) -> None   # line 33; `for k, v in kwargs.items(): setattr(self, k, v)` line 41-42
    def _print_info(self, df: pd.DataFrame) -> None                          # line 88
logger = logging.getLogger(__name__)                                         # line 17 (module logger)

# querysource/queries/multi/operators/abstract.py
class AbstractOperator(AbstractMulti):                                       # line 16
    def __init__(self, data: dict, **kwargs) -> None                         # line 27 (sets self._backend)

# querysource/queries/multi/operators/filter/flt.py
class Filter(AbstractOperator):                                              # line 11
    @classmethod
    def filter_functions(cls) -> list[str]                                   # line 42 (excludes getFunction, build_condition, create_filter_chain, create_filter)
    def __init__(self, data: dict, **kwargs) -> None                         # line 62; attrs: conditions, fields, _filter, filter_conditions, _applied, _operator
    async def start(self)                                                    # line 73
    async def run(self)                                                      # line 83; filter_conditions loop 115-128; fields 135-142; create_filter + eval 145-152

# querysource/queries/multi/operators/Join.py
class Join(AbstractOperator):                                                # line 12
    def _apply_join_conditions(self, df: DataFrame) -> DataFrame             # line 150; create_filter 194; eval 198

# querysource/queries/multi/transformations/Map.py
class Map(AbstractTransform):
    def _call_fn(self, field: str, fname: Callable, args: dict, it: pd.DataFrame) -> pd.DataFrame  # line 127; getFunction at 142, `if not callable(func): return it` 146

# querysource/types/dt/transforms.py
def apply_function(df, field, fname, column=None, **kwargs) -> pd.DataFrame  # line 134; getFunction at 154

# querysource/qsurl/residual.py (reference implementation to port, not to import)
_MAX_REGEX_PATTERN_LENGTH = 200                                               # line 40
_NESTED_QUANTIFIER_RE                                                         # line 41
def _check_regex_safety(pattern: str) -> None                                 # line 44
def _re2_contains(col: pd.Series, pattern: str, ignore_case: bool) -> pd.Series  # line 63
```

### Integration Points
| New Component | Connects To | Via | Verified At |
|---|---|---|---|
| `combine_conditions` | `Filter.run` | `df.loc[combine_conditions(masks, self._operator)]` | `flt.py:146-152` |
| `combine_conditions` | `Join._apply_join_conditions` | `df.loc[combine_conditions(masks, '&')]` | `Join.py:194-198` |
| `re2_search` | `build_condition` regex branches | `re2_search(df[column], f"^(?:{value})")` | `filters.py:74-79, 146-156` |
| `Filter.filter_functions()` | `Filter.run` | allowlist check before `getattr(dffunctions, ft)` | `flt.py:115-126` |
| strict `getFunction` | `Map._call_fn`, `apply_function`, `utils/fn.py` | unchanged call sites; `None` already handled | `Map.py:142-147`, `transforms.py:154`, `fn.py:63-66` |

### Does NOT Exist (Anti-Hallucination)
- ~~`querysource.types.dt.filters.combine_conditions`~~ — does not exist yet (created by M2).
- ~~`querysource.types.dt.regex`~~ — does not exist yet (created by M1).
- ~~`Filter.logger` / `self.logger` on operators~~ — verified absent: `AbstractOperator` (`operators/abstract.py`) defines no logger; only `AbstractTransform` sets `self.logger` (`transformations/abstract.py:29`). M4 uses a module-level `_logger = logging.getLogger(__name__)` in `flt.py`.
- ~~`pd.Series.str.contains(..., engine=...)`~~ — no RE2 selector in pandas; RE2 is reached only through `pyarrow.compute`.
- ~~`querysource.types.dt.filters.getFunction`~~ — it is an import, not a definition; it is removed by M2.
- ~~`Filter.filter_functions()` runtime enforcement~~ — the list exists but nothing enforces it today.
- ~~`create_filter` returning a filtered DataFrame for `func`~~ — it never did (local variable), see §1.

### Edit Sites (Blueprint Anchors)

Verified against: `6e85f734`

| File | Action | Verbatim anchor line | Verified at | Occurrences |
|---|---|---|---|---|
| `querysource/types/dt/filters.py` | MODIFY | `from ...utils.getfunc import getFunction` | `filters.py:10` | 1 |
| `querysource/types/dt/filters.py` | MODIFY | `def build_condition(expression: str, column: str, value, condition: dict, df: pd.DataFrame = None) -> str:` | `filters.py:22` | 1 |
| `querysource/types/dt/filters.py` | MODIFY | `def create_filter_chain(expression: list, column: str, df: pd.DataFrame) -> list:` | `filters.py:173` | 1 |
| `querysource/types/dt/filters.py` | MODIFY | `def create_filter(_filter: list, df: pd.DataFrame) -> list:` | `filters.py:205` | 1 |
| `querysource/types/dt/regex.py` | CREATE | — | — | — |
| `querysource/utils/getfunc.py` | MODIFY | `def getFunction(fname: str) -> callable:` | `getfunc.py:8` | 1 |
| `querysource/queries/multi/operators/filter/flt.py` | MODIFY | `from .....types.dt.filters import create_filter` | `flt.py:7` | 1 |
| `querysource/queries/multi/operators/filter/flt.py` | MODIFY | `func = globals()[ft]` | `flt.py:124` | 1 |
| `querysource/queries/multi/operators/filter/flt.py` | MODIFY | `eval(self.condition)` | `flt.py:151` | 1 |
| `querysource/queries/multi/operators/Join.py` | MODIFY | `from ....types.dt.filters import create_filter` | `Join.py:9` | 1 |
| `querysource/queries/multi/operators/Join.py` | MODIFY | `df_filtered = df.loc[eval(condition_str)]` | `Join.py:198` | 1 |
| `querysource/queries/multi/operators/filter/Filter.catalog.yaml` | MODIFY | `        - func` | `Filter.catalog.yaml:127` | 1 |
| `tests/test_column_filters.py` | MODIFY | `result = df.loc[eval(" & ".join(conditions))]` | `test_column_filters.py:32` | 15 (replace all) |
| `tests/test_join_with_column_filter.py` | MODIFY | `result = df.loc[eval(" & ".join(conditions))]` | `test_join_with_column_filter.py:60` | 10 (replace all) |
| `tests/test_filter_eval_hardening.py` | CREATE | — | — | — |
| `tests/test_getfunc_allowlist.py` | CREATE | — | — | — |
| `docs/FILTER_OPERATORS.md` | MODIFY | `## qsurl` | `docs/FILTER_OPERATORS.md` | 1 (append a `## In-memory Filter / Join` section before it) |

---

## 7. Implementation Notes & Constraints

### Decisions
- D1. **Masks, not strings.** `create_filter` keeps its name and argument
  order but changes its return type to `list[pd.Series]`. It is only used by
  `flt.py`, `Join.py` and the two test files (verified by grep); no public
  re-export in `querysource/types/dt/__init__.py`.
- D2. **`contains` stays case-insensitive** (`case=False`) and literal
  (`re.escape`) — that is today's behaviour for scalars and lists.
- D3. **`regex` keeps `str.match` semantics** (anchored at start, case
  sensitive) by prefixing `^(?:…)`; `fullmatch` wraps `^(?:…)$`. List `regex`
  stays unanchored (today it is `str.contains`). All go through RE2.
- D4. **`func`/`function` removed** rather than hardened: it was a no-op
  filter that only executed code. Removing it is the smallest correct change.
- D5. **`getFunction` keeps its signature** and `None` contract so Map,
  `apply_function` and `utils/fn.py` need no change.
- D6. **`Filter.condition` becomes descriptive text** (e.g.
  `status == 'active' & revenue > 1000`) for `_applied`/debugging; it is never
  passed to any evaluator.
- D7. **Errors propagate.** `filter_conditions` failures raise
  `QueryException` instead of being printed and ignored.
- D8. **`qsurl/residual.py` is not touched.** The RE2 helper is duplicated into
  `types/dt/regex.py`; a follow-up may make `residual.py` import from it.

### Patterns to Follow
- Mirror `querysource/qsurl/residual.py:21-28, 40-85, 101-144` for the mask
  and RE2 code; use `operator` module callables, never string operators.
- `from ...exceptions import QueryException` for all user-facing errors;
  message prefix `Filter:` for operator errors (matches existing
  `"tFilter: …"`/`"Filter: …"` strings).
- Google-style docstrings and type hints on every new/changed function.
- Tests: pandas DataFrames built inline; `pytest.raises(QueryException)`.

### Known Risks / Gotchas
- **Dtype of mask with nullable columns**: comparisons on `object`/`string`
  columns with `None` yield `False`; `str.*` accessors must pass `na=False`
  (`startswith`/`endswith` today do not — keep the new behaviour explicit and
  add a null row to the fixtures).
- **`str.len()` on non-string column** raises `AttributeError`; wrap `gt`/`lt`
  in `df[column].astype("string")` like residual does.
- **pyarrow optional**: CI installs the `parquet` extra; if a deployment lacks
  pyarrow, regex filters now fail closed (documented in AC8 / docs).
- **Filter in `handlers/multi.py:689-712`** passes `filter={...}` as a dict
  when leftover body keys exist; `create_filter` iterates it as before — do
  not change that contract here (pre-existing behaviour, out of scope).
- **No `self.logger` on operators** (see Does NOT Exist): use the module
  logger, never `print`.

### External Dependencies
| Package | Version | Reason |
|---|---|---|
| `pyarrow` | `>=25` (existing `parquet` extra) | RE2 engine for `regex`/`fullmatch` masks |
| `pandas` | `3.x` (existing) | mask arithmetic |

---

## 8. Open Questions

- [x] Keep `create_filter`'s name with a changed return type, or add a new function? — *Resolved (spec)*: keep the name, change the return type; only two internal callers plus tests.
- [x] Harden or remove `func`/`function`? — *Resolved (spec)*: remove (it was a no-op filter).
- [x] Should the RE2 helper be shared with `qsurl/residual.py` now? — *Resolved (spec)*: duplicate now, converge in a follow-up (D8).
- [ ] Should `pyarrow` move from the `parquet` extra to a core dependency now that two filter paths fail closed without it? — *Owner: Jesus Lara* (release decision, not blocking).
- [ ] Follow-up ledger issues to open after merge: `executor.py:159` rethink eval; `validators.py:84-86` / `transforms.py:696` `globals()` lookups; PBAC pre-flight ignores `sources` keys (`handlers/multi.py:469-480`). — *Owner: Jesus Lara*

---

## 9. Design Research Cross-Check

> Model: — · Status: skipped (no accepted exploration document — spec scaffolded
> from an inline security analysis; codex seat requires an accepted brainstorm/proposal)
> · Transcript: —

| # | Suggestion (kind) | Disposition | Reason | Landed in |
|---|---|---|---|---|

Summary: **0** confirmed · **0** rejected · **0** escalated.

---

## Worktree Strategy

- **Isolation**: one feature worktree
  `.claude/worktrees/feat-FEAT-166-filter-eval-hardening` from `dev`; the
  `sdd-coder` engine gives each task its own sub-worktree inside it.
- **Module dependency graph**:
  - M2 → M1 (`filters.py` imports `re2_search`, `check_regex_safety` from `types/dt/regex.py`).
  - M4 → M2 (`flt.py` and `Join.py` import `combine_conditions`, and depend on `create_filter` returning masks).
  - M5 → M2, M3, M4 (tests exercise the new return types and the allowlist).
  - M3 has no edge to M1/M2/M4 → runs concurrently with M1.
- **Shared files**: none between modules (each file is owned by exactly one
  module; `filters.py` is M2 only, `flt.py`/`Join.py` are M4 only).
- **Exclusive resources**: none (pure Python; no `.pyx`/Rust rebuild, no
  lockfile change, no migration).
- **Cross-feature dependencies**: none. FEAT-164 (RE2 in qsurl) is already
  merged and serves only as reference.

---

## Revision History

| Version | Date | Author | Change |
|---|---|---|---|
| 0.1 | 2026-10-12 | Jesus Lara / Claude Code | Initial draft from the private security analysis of `Filter`/`Join`/`getFunction` |
