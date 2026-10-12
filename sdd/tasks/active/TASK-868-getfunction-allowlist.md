# TASK-868: Strict allowlist resolver in `utils/getfunc.py`

**Feature**: FEAT-166 — MultiQuery Filter without `eval` (eval-free DataFrame masks)
**Spec**: `sdd/specs/filter-eval-hardening.spec.md`
**Status**: pending
**Priority**: high
**Estimated effort**: S (< 2h)
**Depends-on**: none
**Assigned-to**: unassigned

---

## Context

Spec §3 Module 3 / goal G4. `getFunction` (`querysource/utils/getfunc.py:8-48`)
documents a whitelist but falls back to module `globals()`, to `builtins` and to
`importlib.import_module()` on any dotted name. It is reached from request-controlled
names in `Map._call_fn` (`transformations/Map.py:142`), `transforms.apply_function`
(`types/dt/transforms.py:154`), `utils/fn.py:63` and (until TASK-869 removes it)
`create_filter`'s `func` expression. This task makes it resolve **only** public
callables of `querysource.utils.functions`, keeping the signature and the `None`
contract so no caller changes.

---

## Scope

- Rewrite `getFunction` to resolve only from `qsfunctions`; dotted names,
  `builtins` names, private names, non-`str` input and non-callables return `None`
  (warning logged).
- Remove the `importlib`, `contextlib` and `builtins` imports from `getfunc.py`.
- Add `tests/test_getfunc_allowlist.py`.

**NOT in scope**: changing `Map.py`, `transforms.py`, `fn.py` (callers already
handle `None`); the `globals()[...]` lookups in `utils/validators.py` and
`transforms.py:696` (spec non-goal); removing the `func` filter expression (TASK-869).

---

## Files to Create / Modify

| File | Action | Description |
|---|---|---|
| `querysource/utils/getfunc.py` | MODIFY | allowlist-only resolver |
| `tests/test_getfunc_allowlist.py` | CREATE | Unit tests |

---

## Codebase Contract (Anti-Hallucination)

### Verified Imports
```python
from navconfig.logging import logging                 # verified: querysource/utils/getfunc.py:4
from querysource.utils import functions as qsfunctions  # verified: querysource/utils/getfunc.py:5 (Cython module querysource/utils/functions.pyx)
from querysource.utils.getfunc import getFunction      # verified: querysource/utils/getfunc.py:8
```

### Existing Signatures to Use
```python
# querysource/utils/getfunc.py
def getFunction(fname: str) -> callable:   # line 8; current fallbacks: qsfunctions 21-22, globals() 23-28, builtins 29-32, importlib dotted 34-44

# querysource/utils/functions.pyx — public names that MUST still resolve (sample)
today, now, yesterday, current_date, current_year, fdom, ldom, to_date, format_date, trim, to_uuid, to_udf

# Callers (unchanged; all already treat None as "not found"):
# querysource/queries/multi/transformations/Map.py:142-147   func = getFunction(fname); if not callable(func): return it
# querysource/types/dt/transforms.py:154                     func = getFunction(fname)
# querysource/utils/fn.py:63-66                              func = getFunction(fname); if not func: warning + return None
```

### Does NOT Exist
- ~~`qsfunctions.__all__`~~ — the Cython module exposes no `__all__`; use `getattr(qsfunctions, name, None)`.
- ~~`getFunction(fname, module=...)`~~ — no extra parameters; keep the one-argument signature.
- ~~a shared `ALLOWED_FUNCTIONS` constant~~ — none exists; the allowlist IS the module's public callables.

---

## Complexity Contract

```json
{
  "schema_version": 1,
  "targets": [
    {"path": "querysource/utils/getfunc.py", "action": "MODIFY"},
    {"path": "tests/test_getfunc_allowlist.py", "action": "CREATE"}
  ],
  "contract_symbols": [
    "sym:querysource/utils/getfunc.py#getFunction"
  ]
}
```

---

## Implementation Notes

### Key Constraints
- Same signature; return `None` (never raise) for unresolvable names — `fn.py:63-66`
  depends on the falsy return.
- `str` check first: `Map._call_fn` may pass non-string values through.
- Log at warning level via `navconfig.logging.logging` (already imported); no `print`.

### References in Codebase
- `querysource/utils/fn.py:63-66` — the `None` contract
- `querysource/queries/multi/transformations/Map.py:142-147` — `callable()` guard

---

## Implementation Blueprint

### Steps (in order)
1. Replace the whole body of `querysource/utils/getfunc.py` with the block below —
   *why*: the dangerous fallbacks are the entire second half of the file; a
   rewrite is smaller and clearer than surgical deletes.
2. Create `tests/test_getfunc_allowlist.py` and complete the `FILL IN` — *why*: AC5
   needs an explicit negative test for dotted and builtin names.
3. Run `ruff check querysource/utils/getfunc.py` and the validation commands
   (`tests/test_catalog_map.py` guards the Map catalog that lists transform functions).

### `querysource/utils/getfunc.py` (MODIFY — whole-file replacement)
```python
# occurrences: 1 (verified: grep -c 'def getFunction(fname: str) -> callable:' querysource/utils/getfunc.py)
# REPLACE the entire file (48 lines, anchor `def getFunction(fname: str) -> callable:` at getfunc.py:8)
"""Resolve a named helper function from the QuerySource function allowlist."""
from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from navconfig.logging import logging

from . import functions as qsfunctions


def getFunction(fname: str) -> Optional[Callable]:
    """Return the callable ``querysource.utils.functions.<fname>`` or ``None``.

    Only public attributes of ``querysource.utils.functions`` are resolvable.
    A name that is not a ``str``, starts with ``_``, contains ``.`` or does not
    resolve to a callable returns ``None``. There is deliberately no fallback to
    module globals, ``builtins`` or ``importlib`` (FEAT-166).

    Args:
        fname: Function name as written in a query/pipeline definition.

    Returns:
        The allowlisted callable, or ``None`` when the name is not allowed.
    """
    if not isinstance(fname, str) or not fname or fname.startswith('_') or '.' in fname:
        logging.warning("Function %r is not an allowed QuerySource function.", fname)
        return None
    func = getattr(qsfunctions, fname, None)
    if not callable(func):
        logging.warning("Function %r not found in querysource.utils.functions.", fname)
        return None
    return func
```
**Why this shape**: spec D5 keeps the signature and the `None` contract; the three
rejected categories (dotted, private, non-callable) are the exact fallbacks the
old code offered. `Optional[Callable]` replaces the invalid `-> callable` annotation.

### `tests/test_getfunc_allowlist.py` (CREATE)
```python
"""FEAT-166 TASK-868: getFunction resolves only querysource.utils.functions."""
import pytest

from querysource.utils import functions as qsfunctions
from querysource.utils.getfunc import getFunction


@pytest.mark.parametrize("name", ["today", "now", "to_date", "trim", "fdom"])
def test_allowlisted_names_resolve(name):
    assert getFunction(name) is getattr(qsfunctions, name)


@pytest.mark.parametrize("name", [
    "os.system", "subprocess.run", "importlib.import_module",   # dotted → None
    "eval", "exec", "open", "__import__", "getattr",           # builtins → None
    "importlib", "logging", "qsfunctions",                      # getfunc.py globals → None
    "_private", "", None, 42, ["today"],                        # malformed → None
])
def test_disallowed_names_return_none(name):
    assert getFunction(name) is None


def test_getfunc_module_has_no_dynamic_import_machinery():
    import querysource.utils.getfunc as mod
    # FILL IN: assert `importlib`, `builtins` and `contextlib` are not attributes of `mod` — bounded by AC5
    raise NotImplementedError
```
**Why this shape**: the parametrised negatives enumerate each removed fallback so a
regression in any one of them fails a named case.

### FILL IN checklist
- [ ] `test_getfunc_allowlist.py::test_getfunc_module_has_no_dynamic_import_machinery` — body; bounded by AC5.

---

## Acceptance Criteria

- [ ] `getFunction` returns `None` for dotted names, builtins, private names and non-strings (spec AC5)
- [ ] `grep -n "importlib\|builtins\|contextlib" querysource/utils/getfunc.py` returns nothing
- [ ] All tests pass: `pytest tests/test_getfunc_allowlist.py tests/test_catalog_map.py -v`
- [ ] No linting errors: `ruff check querysource/utils/getfunc.py tests/test_getfunc_allowlist.py`

---

## Validation Commands

- `pytest tests/test_getfunc_allowlist.py -q`
- `pytest tests/test_catalog_map.py -q`

---

## Test Specification

See the `tests/test_getfunc_allowlist.py` block in the blueprint.

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
9. **Close the task** with `scripts/sdd/close_task.sh TASK-868 filter-eval-hardening verified`
10. **Fill in the Completion Note** below, then commit the staged SDD state

---

## Completion Note

*(Agent fills this in when done)*

**Completed by**: <session or agent ID>
**Date**: YYYY-MM-DD
**Notes**:

**Deviations from spec**: none
