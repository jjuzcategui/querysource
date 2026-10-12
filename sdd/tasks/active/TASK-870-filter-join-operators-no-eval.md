# TASK-870: `Filter` and `Join` operators without `eval`; `filter_conditions` allowlist

**Feature**: FEAT-166 — MultiQuery Filter without `eval` (eval-free DataFrame masks)
**Spec**: `sdd/specs/filter-eval-hardening.spec.md`
**Status**: pending
**Priority**: high
**Estimated effort**: M (2-4h)
**Depends-on**: TASK-869
**Assigned-to**: unassigned

---

## Context

Spec §3 Module 4. With TASK-869 the builders return masks, so the two `eval()`
call sites — `Filter.run` (`operators/filter/flt.py:147-152`) and
`Join._apply_join_conditions` (`operators/Join.py:194-198`) — become
`df.loc[combine_conditions(...)]`. The same `Filter.run` loop resolves
`filter_conditions` keys with `getattr(dffunctions, ft)` / `globals()[ft]`
(`flt.py:121-124`) and swallows errors with `print()` (`flt.py:127-128`); this
task enforces the `Filter.filter_functions()` allowlist that already feeds the
catalog and makes errors propagate (spec G5, D7).

---

## Scope

- `flt.py`: import `combine_conditions`; replace the `filter_conditions` loop
  (allowlist + `QueryException`, module logger); replace the `eval` block;
  keep `self.condition` as descriptive text (D6).
- `Join.py`: import `combine_conditions`; replace the `eval` line.
- Append operator-level tests to `tests/test_filter_eval_hardening.py`
  (file created by TASK-869).

**NOT in scope**: `types/dt/filters.py` (TASK-869), `getfunc.py` (TASK-868),
`handlers/multi.py`, `queries/multi/__init__.py` (unchanged call sites), the
`fields` equality block (`flt.py:135-142`, already eval-free).

---

## Files to Create / Modify

| File | Action | Description |
|---|---|---|
| `querysource/queries/multi/operators/filter/flt.py` | MODIFY | allowlist, no eval, logger |
| `querysource/queries/multi/operators/Join.py` | MODIFY | no eval |
| `tests/test_filter_eval_hardening.py` | MODIFY | append operator-level tests |

---

## Codebase Contract (Anti-Hallucination)

### Verified Imports
```python
from .....types.dt.filters import combine_conditions, create_filter   # flt.py:7 today imports only create_filter; combine_conditions provided by TASK-869
from ....types.dt.filters import combine_conditions, create_filter    # Join.py:9 today imports only create_filter
from .....exceptions import DataNotFound, DriverError                 # verified: flt.py:4 (add QueryException — exceptions.py:6)
from .....types.dt import filters as dffunctions                      # verified: flt.py:6
from querysource.queries.multi.operators.filter.flt import Filter     # verified: tests/test_catalog_filter.py:15
from querysource.queries.multi.operators.Join import Join             # verified: querysource/queries/multi/operators/Join.py:12
```

### Existing Signatures to Use
```python
# querysource/queries/multi/operators/filter/flt.py  (base 6e85f734)
class Filter(AbstractOperator):                                   # line 11
    @classmethod
    def filter_functions(cls) -> list[str]                        # line 42-60 (public functions of dffunctions minus builders)
    def __init__(self, data: dict, **kwargs) -> None              # line 62; self.conditions, self.fields, self._filter, self.filter_conditions, self._applied, self._operator
    async def run(self)                                           # line 83
#       it = self.data.copy()                                      # line 114
#       for ft, args in self.filter_conditions.items():            # line 115  ← loop to replace (115-128)
#       filter_specs = list(self._filter or []) + list(self.conditions or [])   # line 145
#       conditions = create_filter(filter_specs, df)               # line 147
#       self.condition = f" {self._operator} ".join(conditions)    # line 149
#       df = df.loc[eval(self.condition)]                          # line 150-152 ← replace

# querysource/queries/multi/operators/Join.py
class Join(AbstractOperator):                                     # line 12
    def _apply_join_conditions(self, df: DataFrame) -> DataFrame  # line 150
#       conditions = create_filter(filter_specs, df)               # line 194
#       condition_str = " & ".join(conditions)                     # line 197 ← remove
#       df_filtered = df.loc[eval(condition_str)]                  # line 198 ← replace

# querysource/queries/multi/abstract.py
logger = logging.getLogger(__name__)                              # line 17 (module-level; NOT an attribute)
class AbstractMulti: def __init__(self, data, **kwargs)           # line 33; setattr for every kwarg (41-42)

# tests/test_catalog_filter.py:74-81 — Filter(data=df.copy(), filter_conditions={"drop_rows": {"status": ["inactive"]}}) must keep working
```

### Does NOT Exist
- ~~`self.logger` on `Filter` / `AbstractOperator`~~ — verified absent (`operators/abstract.py` defines none; only `AbstractTransform` sets one at `transformations/abstract.py:29`). Use a module-level `_logger = logging.getLogger(__name__)` in `flt.py`.
- ~~`Filter.condition` as an evaluable expression~~ — after this task it is descriptive text only; nothing may `eval` it.
- ~~`globals()[ft]`~~ — removed; the only resolver is `getattr(dffunctions, ft)` after the allowlist check.
- ~~`create_filter(...)` returning `list[str]`~~ — returns `list[pd.Series]` after TASK-869.

---

## Complexity Contract

```json
{
  "schema_version": 1,
  "targets": [
    {"path": "querysource/queries/multi/operators/filter/flt.py", "action": "MODIFY"},
    {"path": "querysource/queries/multi/operators/Join.py", "action": "MODIFY"},
    {"path": "tests/test_filter_eval_hardening.py", "action": "MODIFY"}
  ],
  "contract_symbols": [
    "sym:querysource/queries/multi/operators/filter/flt.py#Filter",
    "sym:querysource/queries/multi/operators/filter/flt.py#Filter.run",
    "sym:querysource/queries/multi/operators/filter/flt.py#Filter.filter_functions",
    "sym:querysource/queries/multi/operators/Join.py#Join._apply_join_conditions",
    "sym:querysource/types/dt/filters.py#create_filter",
    "sym:querysource/types/dt/filters.py#combine_conditions"
  ]
}
```

---

## Implementation Notes

### Key Constraints
- `Filter(**step)` public API unchanged — `MultiQS` (`queries/multi/__init__.py:1012`)
  and `handlers/multi.py:711` instantiate it with request kwargs.
- `self._operator` validation is delegated to `combine_conditions` (raises
  `QueryException` for anything but `&`/`|`).
- `DataNotFound` semantics for empty results are unchanged (`flt.py:130-133, 153-156`).
- No `print` anywhere in `flt.py` after this task.

### References in Codebase
- `querysource/queries/multi/transformations/Map.py:127-163` — logger + error style for function calls

---

## Implementation Blueprint

### Steps (in order)
1. Edit `flt.py` imports (`combine_conditions`, `QueryException`, module logger) —
   *why*: operators have no `self.logger`.
2. Replace the `filter_conditions` loop with the allowlisted version — *why*: G5/AC7.
3. Replace the `create_filter` + `eval` block with mask reduction — *why*: G1/AC1.
4. Edit `Join.py` import and the `eval` line — *why*: same sink, same fix.
5. Append the operator-level tests to `tests/test_filter_eval_hardening.py`.
6. Run `ruff check` on both operator files and the validation commands.

### `querysource/queries/multi/operators/filter/flt.py` (MODIFY — imports)
```python
# occurrences: 1 (verified: grep -c 'from .....types.dt.filters import create_filter' querysource/queries/multi/operators/filter/flt.py)
# REPLACE `from .....types.dt.filters import create_filter` (flt.py:7) with:
from .....types.dt.filters import combine_conditions, create_filter
# REPLACE `from .....exceptions import DataNotFound, DriverError` (flt.py:4) with:
from .....exceptions import DataNotFound, DriverError, QueryException
# AFTER the import block (below `from ..abstract import AbstractOperator`, flt.py:8), add:
import logging

_logger = logging.getLogger(__name__)
```
**Why**: `QueryException` is the error type for user-facing filter failures; the
module logger replaces `print`.

### `querysource/queries/multi/operators/filter/flt.py` (MODIFY — `filter_conditions` loop)
```python
# occurrences: 1 (verified: grep -c 'func = globals()\[ft\]' querysource/queries/multi/operators/filter/flt.py)
# REPLACE flt.py:115-128 (from `for ft, args in self.filter_conditions.items():` through `print(f"Error on {ft}: {err}")`) with:
        allowed = set(self.filter_functions())
        for ft, args in self.filter_conditions.items():
            if ft not in allowed:
                raise QueryException(f"Filter: unknown filter function '{ft}'")
            self._applied.append(f"Filter: {ft!s} args: {args}")
            func = getattr(dffunctions, ft)
            try:
                it = func(it, **(args or {}))
            except QueryException:
                raise
            except Exception as err:
                _logger.error("Filter: error applying %s: %s", ft, err)
                raise QueryException(f"Filter: error applying '{ft}': {err}") from err
```
**Why**: the allowlist is the same list the catalog publishes (`filter_functions()`),
so the runtime can no longer reach names the schema does not advertise; errors
propagate as `QueryException` so `MultiQS` reports them (D7).

### `querysource/queries/multi/operators/filter/flt.py` (MODIFY — eval block)
```python
# occurrences: 1 (verified: grep -c 'eval(self.condition)' querysource/queries/multi/operators/filter/flt.py)
# REPLACE flt.py:147-152 (`conditions = create_filter(filter_specs, df)` … `]  # pylint: disable=W0123`) with:
            masks = create_filter(filter_specs, df)
            # Descriptive text only (never evaluated) — kept for _applied/debug parity.
            self.condition = f" {self._operator} ".join(
                f"{spec.get('column')} {spec.get('expression', '==')} {spec.get('value')!r}"
                for spec in filter_specs
            )
            self._applied.append(f"Filter: conditions: {self.condition}")
            df = df.loc[combine_conditions(masks, self._operator)]
```
**Why**: D6 keeps `Filter.condition` as a string for logs; `combine_conditions`
validates `self._operator`.

### `querysource/queries/multi/operators/Join.py` (MODIFY)
```python
# occurrences: 1 (verified: grep -c 'from ....types.dt.filters import create_filter' querysource/queries/multi/operators/Join.py)
# REPLACE `from ....types.dt.filters import create_filter` (Join.py:9) with:
from ....types.dt.filters import combine_conditions, create_filter

# occurrences: 1 (verified: grep -c 'df_filtered = df.loc\[eval(condition_str)\]' querysource/queries/multi/operators/Join.py)
# REPLACE Join.py:196-198 (`# Apply conditions with AND logic` / `condition_str = " & ".join(conditions)` / `df_filtered = df.loc[eval(condition_str)]`) with:
            # Apply conditions with AND logic
            df_filtered = df.loc[combine_conditions(conditions, '&')]
```
**Why**: join conditions are always AND-combined today (`Join.py:197`).

### `tests/test_filter_eval_hardening.py` (MODIFY — append)
```python
# occurrences: 1 (verified after TASK-869: grep -c 'def test_regex_expressions_use_re2' tests/test_filter_eval_hardening.py)
# APPEND at end of file:
import asyncio
from pathlib import Path

from querysource.queries.multi.operators.filter.flt import Filter
from querysource.queries.multi.operators.Join import Join

_SRC = Path(__file__).resolve().parents[1] / "querysource"


def test_no_eval_or_globals_in_filter_sources():
    for rel in ("types/dt/filters.py", "queries/multi/operators/filter/flt.py", "queries/multi/operators/Join.py"):
        text = (_SRC / rel).read_text(encoding="utf-8")
        assert "eval(" not in text and "globals()" not in text, rel


def test_filter_run_quote_value_is_literal(text_df):
    op = Filter(data=text_df.copy(), conditions=[{"column": "status", "expression": "==", "value": "active')"}])
    out = asyncio.run(op.run())
    assert list(out["id"]) == [4]
    assert isinstance(op.condition, str)


def test_filter_conditions_unknown_key_raises(text_df):
    op = Filter(data=text_df.copy(), filter_conditions={"not_a_fn": {}})
    with pytest.raises(QueryException, match="unknown filter function"):
        asyncio.run(op.run())


def test_filter_conditions_known_fn_applied(text_df):
    op = Filter(data=text_df.copy(), filter_conditions={"drop_rows": {"status": ["inactive"]}})
    out = asyncio.run(op.run())
    assert "inactive" not in set(out["status"].dropna())


def test_filter_operator_or(text_df):
    # FILL IN: two conditions with operator='|' select the union; operator='and' raises QueryException — bounded by AC2/D6
    raise NotImplementedError


def test_join_conditions_no_eval():
    # FILL IN: build two small DataFrames, Join(data={...}, join_conditions=[{"left": ..., "expression": ">=", "right": ...}], ...)
    #   per Join.__init__ kwargs (read Join.py:12-150 first), run it and assert the filtered rows — bounded by spec §4 test_join_conditions_no_eval
    raise NotImplementedError
```
**Why**: the source-grep test is AC1 made executable; the `Filter.run` cases prove
the operator path (not just the builder) treats values as data.

### FILL IN checklist
- [ ] `test_filter_eval_hardening.py::test_filter_operator_or` — bounded by AC2/D6.
- [ ] `test_filter_eval_hardening.py::test_join_conditions_no_eval` — read `Join.__init__` kwargs first; bounded by spec §4.

---

## Acceptance Criteria

- [ ] `grep -n "eval(\|globals()\|print(" querysource/queries/multi/operators/filter/flt.py querysource/queries/multi/operators/Join.py` → nothing (AC1)
- [ ] `filter_conditions` with an unknown key raises `QueryException`; `drop_rows` case from `tests/test_catalog_filter.py:74` still passes (AC7)
- [ ] All tests pass: `pytest tests/test_filter_eval_hardening.py tests/test_catalog_filter.py tests/test_join_with_column_filter.py -v`
- [ ] No linting errors: `ruff check querysource/queries/multi/operators/filter/flt.py querysource/queries/multi/operators/Join.py`

---

## Validation Commands

- `pytest tests/test_filter_eval_hardening.py -q`
- `pytest tests/test_catalog_filter.py -q`
- `pytest tests/test_join_with_column_filter.py -q`

---

## Test Specification

See the appended block for `tests/test_filter_eval_hardening.py` in the blueprint.

---

## Agent Instructions

When you pick up this task:

1. **Work in the feature worktree** — never on `base_branch`
   (`python -m scripts.sdd.ensure_worktree --slug filter-eval-hardening --feature-id FEAT-166`)
2. **Read the spec** at the path listed above for full context
3. **Check dependencies** — TASK-869 must be `"done"` in `sdd/tasks/index/filter-eval-hardening.json`
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
9. **Close the task** with `scripts/sdd/close_task.sh TASK-870 filter-eval-hardening verified`
10. **Fill in the Completion Note** below, then commit the staged SDD state

---

## Completion Note

*(Agent fills this in when done)*

**Completed by**: <session or agent ID>
**Date**: YYYY-MM-DD
**Notes**:

**Deviations from spec**: none
