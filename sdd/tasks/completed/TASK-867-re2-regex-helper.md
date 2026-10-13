# TASK-867: RE2-only regex helper for DataFrame filters (`types/dt/regex.py`)

**Feature**: FEAT-166 — MultiQuery Filter without `eval` (eval-free DataFrame masks)
**Spec**: `sdd/specs/filter-eval-hardening.spec.md`
**Status**: pending
**Priority**: high
**Estimated effort**: S (< 2h)
**Depends-on**: none
**Assigned-to**: unassigned

---

## Context

Spec §3 Module 1. The new mask builder (TASK-869) must evaluate `regex`,
`not_regex` and `fullmatch` on a linear-time engine. The repository already has
such a helper for the qsurl residual stage (`querysource/qsurl/residual.py`,
FEAT-164) but `types/dt` must not import from `qsurl` (spec D8), so this task
ports it into a small standalone module that raises `QueryException`.

---

## Scope

- Create `querysource/types/dt/regex.py` with `MAX_REGEX_PATTERN_LENGTH`,
  `check_regex_safety(pattern)` and `re2_search(col, pattern, *, ignore_case=False)`.
- Fail closed (raise `QueryException`) when pyarrow is not importable.
- Write `tests/test_dt_regex.py` covering anchoring, nulls, lookaround rejection,
  the length cap, the nested-quantifier screen and the no-pyarrow path.

**NOT in scope**: changing `filters.py` (TASK-869), touching
`querysource/qsurl/residual.py` (spec non-goal), case-insensitive expressions.

---

## Files to Create / Modify

| File | Action | Description |
|---|---|---|
| `querysource/types/dt/regex.py` | CREATE | RE2 helper + safety screen |
| `tests/test_dt_regex.py` | CREATE | Unit tests |

---

## Codebase Contract (Anti-Hallucination)

### Verified Imports
```python
from querysource.exceptions import QueryException   # verified: querysource/exceptions.py:6
import pyarrow as pa                                 # verified installed: pyarrow 25.0.1 (optional `parquet` extra, pyproject.toml:170-171)
import pyarrow.compute as pc                         # verified: querysource/qsurl/residual.py:12
```

### Existing Signatures to Use
```python
# querysource/qsurl/residual.py  (REFERENCE ONLY — port, do not import)
_MAX_REGEX_PATTERN_LENGTH = 200                                            # line 40
_NESTED_QUANTIFIER_RE = re.compile(r"\([^()]*[+*][^()]*\)[+*]")             # line 41
def _check_regex_safety(pattern: str) -> None                              # line 44-60  (raises QSUrlError("lower", ...))
def _re2_contains(col: pd.Series, pattern: str, ignore_case: bool) -> pd.Series   # line 63-85
#   arrow = pa.array(col.astype(pd.StringDtype("pyarrow")))
#   matched = pc.match_substring_regex(arrow, pattern=pattern, ignore_case=ignore_case)
#   return pd.Series(matched.fill_null(False).to_numpy(zero_copy_only=False), index=col.index, dtype=bool)

# querysource/exceptions.py
class QueryException(Exception)                                            # line 6
```

### Does NOT Exist
- ~~`querysource.types.dt.regex`~~ — does not exist yet; this task creates it.
- ~~`pd.Series.str.contains(..., engine="re2")`~~ — pandas has no engine selector; RE2 is only reachable through `pyarrow.compute`.
- ~~`querysource.qsurl.residual.re2_search`~~ — the residual helper is private (`_re2_contains`) and raises `QSUrlError`; do not import it.

---

## Complexity Contract

```json
{
  "schema_version": 1,
  "targets": [
    {"path": "querysource/types/dt/regex.py", "action": "CREATE"},
    {"path": "tests/test_dt_regex.py", "action": "CREATE"}
  ],
  "contract_symbols": [
    "sym:querysource/exceptions.py#QueryException",
    "sym:querysource/qsurl/residual.py#_re2_contains",
    "sym:querysource/qsurl/residual.py#_check_regex_safety"
  ]
}
```

---

## Implementation Notes

### Pattern to Follow
`querysource/qsurl/residual.py:40-85` verbatim, with `QueryException(msg)` in
place of `QSUrlError("lower", msg)`.

### Key Constraints
- Anchoring is the caller's job: `re2_search` never adds `^`/`$` itself.
- Nulls never match (`fill_null(False)`).
- No Python `re` evaluation of the user pattern anywhere (`re` is used only
  for the static nested-quantifier screen).

### References in Codebase
- `querysource/qsurl/residual.py` — reference implementation
- `tests/qsurl/test_residual.py` — test style for RE2 rejection cases

---

## Implementation Blueprint

### Steps (in order)
1. Create `querysource/types/dt/regex.py` from the block below — *why*: it is a
   verbatim port of the FEAT-164 helper so behaviour matches qsurl.
2. Create `tests/test_dt_regex.py` and complete the `FILL IN` test bodies —
   *why*: AC8 of the spec needs proof that lookaround patterns are rejected and
   that the no-pyarrow path fails closed.
3. Run `ruff check querysource/types/dt/regex.py` and the validation command.

### `querysource/types/dt/regex.py` (CREATE)
```python
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

from ...exceptions import QueryException  # verified: querysource/exceptions.py:6

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
        raise QueryException("regex filters require pyarrow (RE2 engine), which is not installed")
    arrow = pa.array(col.astype(pd.StringDtype("pyarrow")))
    try:
        matched = pc.match_substring_regex(arrow, pattern=pattern, ignore_case=ignore_case)
    except pa.ArrowInvalid as err:
        raise QueryException(f"invalid regex `{pattern}`: {err}") from err
    return pd.Series(
        matched.fill_null(False).to_numpy(zero_copy_only=False), index=col.index, dtype=bool
    )
```
**Why this shape**: identical mechanics to `residual.py:63-85` so both in-memory
filter paths behave the same; the only differences are the public names fixed by
spec §3 M1 and the exception type. Nothing here may call `Series.str.contains`.

### `tests/test_dt_regex.py` (CREATE)
```python
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
    assert list(mask.index) == [10, 11, 12, 13, 14] and mask.dtype == bool
    # FILL IN: assert only index 10 is True (anchored, case-sensitive, null → False) — bounded by spec §2 regex row


def test_re2_search_ignore_case(col):
    # FILL IN: ignore_case=True makes index 13 ("ABC") match "^(?:ab)" too — bounded by AC8
    raise NotImplementedError


def test_re2_search_rejects_lookaround(col):
    with pytest.raises(QueryException, match="invalid regex"):
        dtregex.re2_search(col, "(?=a)(a|a)+$")


def test_check_regex_safety_caps():
    with pytest.raises(QueryException, match="too long"):
        dtregex.check_regex_safety("a" * 201)
    with pytest.raises(QueryException, match="nested quantifier"):
        dtregex.check_regex_safety("(a+)+")
    dtregex.check_regex_safety("^[a-z]+$")  # must not raise


def test_re2_search_fails_closed_without_pyarrow(col, monkeypatch):
    monkeypatch.setattr(dtregex, "pc", None)
    with pytest.raises(QueryException, match="require pyarrow"):
        dtregex.re2_search(col, "a")
```
**Why this shape**: each test maps to one AC8 clause; the fixture includes a null
and a non-string value so the `fill_null(False)` and `astype` paths are exercised.

### FILL IN checklist
- [ ] `test_dt_regex.py::test_re2_search_literal_and_anchor` — exact expected mask; bounded by spec §2 regex row (anchored, case-sensitive).
- [ ] `test_dt_regex.py::test_re2_search_ignore_case` — body; bounded by AC8.

---

## Acceptance Criteria

- [ ] `from querysource.types.dt.regex import re2_search, check_regex_safety` works
- [ ] All tests pass: `pytest tests/test_dt_regex.py -v`
- [ ] No linting errors: `ruff check querysource/types/dt/regex.py tests/test_dt_regex.py`
- [ ] No reference to `Series.str.contains` / `re.search` on the user pattern in `regex.py` (spec AC8)

---

## Validation Commands

- `pytest tests/test_dt_regex.py -q`

---

## Test Specification

See the `tests/test_dt_regex.py` block in the blueprint.

---

## Agent Instructions

When you pick up this task:

1. **Work in the feature worktree** — never on `base_branch`
   (`python -m scripts.sdd.ensure_worktree --slug filter-eval-hardening --feature-id FEAT-166`)
2. **Read the spec** at the path listed above for full context
3. **Check dependencies** — none for this task
4. **Verify the Codebase Contract** — before writing ANY code:
   - Confirm every import in "Verified Imports" still exists (`grep` or `read` the source)
   - If anything has changed, update the contract FIRST, then implement
5. **Update status** in `sdd/tasks/index/filter-eval-hardening.json` → `"in-progress"`
   (set `started_at`) and commit only that index file
6. **Implement** — start from the Implementation Blueprint blocks, complete every
   `# FILL IN:` marker, and never change a signature or path the blueprint fixes
7. **Verify** all acceptance criteria are met — run the Validation Commands
8. **Commit the code** — stage only the files this task lists (never `git add .` / `-A`)
9. **Close the task** with `scripts/sdd/close_task.sh TASK-867 filter-eval-hardening verified`
10. **Fill in the Completion Note** below, then commit the staged SDD state

---

## Completion Note

*(Agent fills this in when done)*

**Completed by**: Claude Code (interactive /sdd-start, Jesus Lara)
**Date**: 2026-10-12
**Notes**: Created `querysource/types/dt/regex.py` (`check_regex_safety`, `re2_search`, `MAX_REGEX_PATTERN_LENGTH`) as a port of `qsurl/residual.py:40-85` raising `QueryException`; `check_regex_safety` additionally rejects non-string patterns. 8 tests in `tests/test_dt_regex.py` (anchoring, nulls, non-string cells, ignore_case, lookaround/backreference rejection, caps, fail-closed without pyarrow, source guard).

**Deviations from spec**: none
