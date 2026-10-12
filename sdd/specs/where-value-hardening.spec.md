---
# SDD flow type and base branch (FEAT-145).
type: feature
base_branch: dev
projects: [parsers, rust-parsers]
tags: [parsers, validators, is-valid, sql-injection, quoting, security, rust]
---

# Feature Specification: WHERE value hardening — `is_valid()` closure and idempotent literal quoting

**Feature ID**: FEAT-167
**Date**: 2026-10-12
**Author**: Jesus Lara (spec drafted with Claude Code)
**Status**: draft
**Target version**: 5.2.5

---

## 1. Motivation & Business Requirements

> Why does this feature exist? What problem does it solve?

### Problem Statement

FEAT-166 (`filter-eval-hardening`, PR #661) removed `eval()` from the
MultiQuery `Filter`/`Join` operators and put `querysource/utils/getfunc.py`
behind an allowlist. It explicitly scoped out the SQL-side value pipeline
(spec Non-Goals: "SQL-layer value escaping audit", "`validators.py`
`globals()` lookups"). That pipeline is `is_valid()` in
`querysource/types/validators.pyx:625`, which every dialect parser runs on
every request-supplied filter value through `AbstractParser._where_element`
(`querysource/parsers/abstract.pyx:583`) and `_handle_keys`/`_process_element`
(`abstract.pyx:487-535`) during `set_options()`. Verified on `dev` at
`82214b25` with the real `pgSQLParser.set_options()` + `build_query()`
pipeline, three request-controlled gaps are still open:

1. **Configuration-variable substitution** (`validators.pyx:427,664`):
   `is_valid()` falls back to `config.get(value)`. A filter value that equals
   a `navconfig` key is replaced by the configuration value and lands in the
   SQL text: `{"username": "DBPWD"}` renders `WHERE username='12345678'`
   (the real local DB password). Through a comparison dict
   (`{"amount": {">": "DBPWD"}}` → `amount > '12345678'`) this is a boolean
   oracle over any secret the process holds. All dialects are affected.
2. **Function-name execution** (`validators.pyx:673` →
   `querysource/utils/functions.pyx:821 to_udf()`): after the config lookup,
   `is_valid()` calls `to_udf(value)` for *any* string. `to_udf` resolves the
   name with `globals()[name]` over the whole `functions.pyx` namespace
   (79 callables, 54 of them invocable with no arguments) and executes it:
   `{"created": "midnight"}` renders `created='2026-10-12T00:00:00'`.
   `docs/QUERYSOURCE_DIALECT.md` §13.1 currently *documents* this
   ("other zero-argument helpers of the same module can be named"), while
   `querysource/utils/vocabulary.py` declares those helpers
   `invocable: False`. FEAT-166 hardened `getFunction` but not `to_udf`.
3. **Raw pass-through of values containing `(`** (`validators.pyx:209,658`):
   `is_pg_function()` returns `True` for any value containing `(`, and
   `is_valid()` then returns the value verbatim, unquoted. The Rust mirror
   `rust/src/validators.rs:92` does the same and is used by
   `rust/src/safe_dict.rs:162` on the raw-query path. The PostgreSQL, generic
   SQL and CQL builders happen to re-quote the value, but `BigQueryParser`
   comparison dicts emit it verbatim:
   `{"amount": {">": "x) OR (1=1"}}` → `WHERE amount > x) OR (1=1`
   (SQL injection).

**Collateral defect — double quoting.** The ledger issue
`issue:48c9b3050a0c` (module-level `quoteString()` not escaping) was fixed
in `acb6bdf5`, which exposed the next layer: `is_valid()` now pre-quotes a
string as `'O''Brien'`, and the dialect builders quote it *again* without an
agreed contract. Verified renders of `{"name": "O'Brien"}` at `82214b25`:

| Path | Rendered | Status |
|---|---|---|
| pgsql Cython (`Entity.quoteString`, `pgsql.pyx:408,416,564`) | `name='O''''Brien'` | double-escaped (matches `O''Brien`) |
| cql Cython (`cql.pyx:92,135`) | `name = 'O''''Brien'` | double-escaped |
| pgsql Rust (`quote_string(&escape_string(v))`, `pgsql_parser.rs:499,520`) | `name='OBrien'` | lossy (apostrophe deleted) |
| generic sql Rust (`sql_parser.rs:64,405`) | `name='OBrien'` | lossy |
| typed `string` placeholder (`to_string` = `quoteString(escape_string(v))`, `validators.pyx:430`) | `'OBrien'` | lossy |
| BigQuery (`bq_quote_string`, unwrap + undo doubling) | `name="O'Brien"` | correct — the reference pattern |

Only the FEAT-152 ILIKE branch and `bq_quote_string` implement the correct
"unwrap the pre-quoted literal, undo `''`, re-escape from scratch" shape.
Every other equality/comparison/IN/negation branch either doubles twice or
strips the quote. Legitimate data containing an apostrophe cannot be
filtered on today through the Rust path, and matches the wrong value
through the Cython path.

### Goals

- G1 — `is_valid()` never consults `navconfig` and never executes a function
  by name outside the explicit relative-date keyword allowlist (`UDF_LIST`).
- G2 — `is_valid()` never returns a request-supplied string unquoted because
  it contains `(`; only exact `PG_UDF` members (default `now()`) pass raw.
  Cython and Rust `is_valid` agree.
- G3 — `to_udf()` resolves only `UDF_LIST` members (env-overridable, same
  list `is_udf()` uses) and raises for anything else; the `builtins`
  fallback and the duplicate dispatcher in `querysource/utils/validators.py`
  are gone. The default list gains `FDOW`, `LDOW`, `LDOPM` (the helpers
  §13.1 already advertises) in both engines and in the vocabulary registry.
- G4 — Literal quoting is **idempotent** in both engines:
  `quote(quote(x)) == quote(x)`, and `quote(x)` round-trips any `x` without
  deleting characters. A value containing `'` renders as `'O''Brien'` under
  every dialect builder (Rust and Cython) and for typed `string` placeholders.
- G5 — No `print()` in `validators.pyx`; errors go through `logging`.
- G6 — Docs (`QUERYSOURCE_DIALECT.md` §13.1/§13.2) describe the enforced
  contract, and the vocabulary endpoint stays consistent with it.
- G7 — The `UDF_LIST`, `PG_CONSTANTS` and `PG_UDF` environment overrides are
  honoured by the Rust extension exactly as by `validators.pyx` (parsed once,
  at first use).

### Non-Goals (explicitly out of scope)

- `querysource/types/dt/transforms.py:696` and `querysource/queries/executor.py:159`
  `globals()`/`eval` lookups (FEAT-166 follow-ups, separate ledger issues).
- Backslash-escaping policy for non-PostgreSQL targets (MySQL
  `NO_BACKSLASH_ESCAPES`): the generic `sql` builders keep the existing
  Cython policy (single-quote doubling only); Rust is aligned to it, not the
  other way round.
- The ILIKE / partial-matching branches (FEAT-152/FEAT-180) — already
  idempotent; they are regression-tested, not changed.
- The `@variable` replacement path (`QS_VARIABLES`, `abstract.pyx:444`) —
  already an explicit registry.
- JSONB element filters, BETWEEN grammar (`filter_values.py`) — unchanged.

---

## 2. Architectural Design

### Overview

Two independent corrections on the shared value pipeline:

**A. Close `is_valid()` (M1 + M2).** `is_valid()` keeps its signature and
its typed-hint dispatch, and its generic branch becomes a closed decision
list: `null` → `'null'`; boolean/integer → verbatim; `UDF_LIST` keyword →
`quoteString(to_udf(value))`; `PG_CONSTANTS` member → upper-cased raw;
exact `PG_UDF` member → raw; list/dict → verbatim; **everything else →
quoted literal** (`quoteString(value)`, or verbatim when `noquote=True`).
The `config.get()` lookup and the `to_udf(value)` fallback are deleted,
`is_pg_function()` becomes an exact-membership test (both engines), and
`to_udf()` itself is restricted to `UDF_LIST` so no other caller can use it
as a name-based dispatcher. `UDF_LIST` moves to one pure-Python source
(`querysource/utils/udf_list.py`) imported by both `types/validators.pyx`
and `utils/functions.pyx`, which also removes the circular-import reason
`to_udf` currently uses the broken pure-Python `is_udf` from
`utils/validators.py`. The default list is extended with `FDOW`, `LDOW` and
`LDOPM` (Cython, Rust and `vocabulary.KEYWORD_REGISTRY`), and the three Rust
allowlist statics read the same environment variables, so an operator
override behaves identically on both paths.

**B. Idempotent literal quoting (M3).** The builders remain the owners of
final quoting (FEAT-165 contract), and `is_valid()` keeps pre-quoting plain
strings (FEAT-152 pinned invariant, `tests/qsurl/test_pg_ilike.py:112`). The
fix is to make the builders' quoting function tolerant of its own output:
a shared *unwrap* step (`unquote_literal`) recognises a well-formed
pre-quoted literal — starts and ends with `'`, and every interior `'` is part
of a `''` pair — and returns its inner text with the doubling undone; the
quoting function then escapes from scratch. `Entity.quoteString` (Cython)
and `quote_string` (Rust) gain that step, Rust builders stop passing values
through `escape_string` (which deletes quotes) before quoting, and
`to_string` (typed `string`/`varchar`/`field` hints) stops using
`escape_string` too. The ILIKE branches and `bq_quote_string` already do
exactly this and are untouched.

### Component Diagram
```
request filter ──► AbstractParser._where_element / _handle_keys (abstract.pyx)
                        │
                        ▼
                 is_valid()  (types/validators.pyx)  ◄── M1 closed decision list
                   ├─ UDF_LIST keyword ──► to_udf()  (utils/functions.pyx) ◄── M2 allowlist
                   │                         └── udf_list()  (utils/udf_list.py, NEW, M2)
                   ├─ PG_UDF exact ──► raw
                   └─ else ──► quoteString(value)  → "'O''Brien'"
                        │
                        ▼
      dialect builders (pgsql.pyx / sql.pyx / cql.pyx / sqlserver.pyx / sosql.pyx
                        pgsql_parser.rs / sql_parser.rs)
                        │
                        ▼
      Entity.quoteString / quote_string  ◄── M3: unquote_literal() first, then escape
                        │
                        ▼
                  WHERE name='O''Brien'

raw-query placeholders ──► safe_dict.rs ──► validators.rs::is_valid ◄── M1 (Rust mirror)
```

### Integration Points

| Existing Component | Integration Type | Notes |
|---|---|---|
| `is_valid()` `querysource/types/validators.pyx:625` | modifies | generic branch becomes closed; signature unchanged |
| `is_pg_function()` `validators.pyx:209` / `rust/src/validators.rs:92` | modifies | exact `PG_UDF` membership (Rust gains an env-aware `PG_UDF` static, default `now()`; `UDF_LIST`/`PG_CONSTANTS` statics become env-aware too) |
| `KEYWORD_REGISTRY` / `FUNCTION_REGISTRY` `querysource/utils/vocabulary.py:43,88` | modifies | `FDOW`, `LDOW`, `LDOPM` become keyword rows; `fdow`/`ldow` leave the informational list |
| `tests/unit/test_vocabulary.py:47-52` | updates | pins the registry sizes/order |
| `to_udf()` `querysource/utils/functions.pyx:821` | modifies | allowlist-only; raises `KeyError` otherwise |
| `is_udf()` `querysource/utils/validators.py:77` | removes | dead dispatcher; `functions.pyx:26` import replaced by `udf_list` |
| `Entity.quoteString` `validators.pyx:590`, `quote_string` `validators.rs:164` | modifies | idempotent via `unquote_literal` |
| `to_string` `validators.pyx:430` / `validators.rs` `to_string` | modifies | no `escape_string` quote-stripping |
| Rust builders `pgsql_parser.rs` (7 sites), `sql_parser.rs` (6 sites) | modifies | `quote_string(&escape_string(v), true)` → `quote_string(v, true)` |
| `resolve_udf_conditions` `validators.pyx:233`, `filter_values._normalise_bound` `filter_values.py:69` | uses (unchanged) | already guard with `is_udf` before `to_udf` |
| `vocabulary.keyword_example` `querysource/utils/vocabulary.py:154` | uses (unchanged) | only called with `udf_list` names; catches exceptions |
| `safe_dict.rs:162` | uses (unchanged) | benefits from Rust `is_valid` fix |
| `docs/QUERYSOURCE_DIALECT.md` §13.1/§13.2 | updates | documented contract |

### Data Models

No new data models. One new pure-Python helper module (M2) and one new
pure-Python/Rust helper function (M3); see §3 skeletons.

### New Public Interfaces
```python
# querysource/utils/udf_list.py (new, M2)
DEFAULT_UDF_LIST: tuple[str, ...]          # 10 names: today's 7 + FDOW, LDOW, LDOPM
def udf_list() -> list[str]: ...          # effective UDF_LIST (env-overridable), upper-cased copy
def is_udf_keyword(value: str) -> bool: ...  # value.strip().upper() in udf_list()

# querysource/parsers/filter_values.py (M3)
def unquote_literal(value: str) -> str | None: ...  # inner text of a well-formed pre-quoted literal, else None
```
```rust
// rust/src/validators.rs (M1)
fn env_list(name: &str, default: &[&str], upper: bool) -> Vec<String>;  // mirrors validators.pyx:30 `_env_list`
// rust/src/validators.rs (M3)
pub fn unquote_literal(value: &str) -> Option<String>;
```

---

## 3. Module Breakdown

> Define the discrete modules that will be implemented.
> These directly map to Task Artifacts in Phase 2.

#### Delegation-eligible modules

| Module | Eligible? | Decided patterns / exact contracts | Why not (if no) |
|---|---|---|---|
| M1: `is_valid()` closure (Cython + Rust) | yes | decision list fixed in §2; `is_pg_function` = exact membership; `print` → `logging.getLogger(__name__)`; AC1–AC3 renders fixed | — |
| M2: `to_udf` allowlist + `udf_list.py` | yes | module path, function names, `KeyError` contract fixed; dead dispatcher deleted | — |
| M3: idempotent quoting | yes | `unquote_literal` grammar fixed (§3 M3); Rust call-site replacement is mechanical; AC4 renders fixed | — |
| M4: tests + docs | yes | test file path, harness (`tests/test_dialect_filter_preprocessing.py:33 preprocess`), doc sections fixed | — |

### Module 1: `is_valid()` closure (Cython and Rust mirror)
- **Path**: `querysource/types/validators.pyx`, `rust/src/validators.rs`
- **Responsibility**: remove the config lookup and the `to_udf` fallback;
  make `is_pg_function` an exact `PG_UDF` membership test in both engines;
  replace `print` with logging; make the three Rust allowlist statics
  env-aware and give the Rust `UDF_LIST` the same 10-name default as M2's
  `DEFAULT_UDF_LIST`. Behaviour for keywords, constants, lists, dicts,
  booleans, integers and typed hints is unchanged.
- **Depends on**: nothing (M2 only changes where `UDF_LIST` is read from).
- **Interface Skeleton** *(signatures + docstrings only — bodies belong to task blueprints, FEAT-545)*:
  ```python
  # querysource/types/validators.pyx  (modifies validators.pyx:209, :427, :625-682)
  cdef bool_t is_pg_function(object value):   # verified: validators.pyx:209
      """True only when ``value`` is an exact member of PG_UDF (default ['now()']).
      A value merely containing '(' is NOT a function."""

  # DELETE: cdef object get_config_var(object value)   # verified: validators.pyx:427
  # DELETE: `from navconfig import config` once unused  # verified: validators.pyx:18

  cpdef object is_valid(object key, object value, str T = None, bint noquote = False):  # verified: validators.pyx:625
      """Validate/normalise one WHERE value. Generic branch (no/unknown hint), in order:
      null-like -> 'null'; bool/int -> value; UDF_LIST keyword -> quoteString(to_udf(value));
      PG_CONSTANTS -> value.upper(); exact PG_UDF -> value; list/dict -> value;
      otherwise -> value when noquote else quoteString(value).
      Never reads configuration; never calls to_udf for a non-keyword.
      Errors from to_udf are logged via logging.getLogger(__name__).warning (no print)."""
  ```
  ```rust
  // rust/src/validators.rs  (modifies validators.rs:92, :269-345; adds PG_UDF static next to UDF_LIST at :21)
  fn env_list(name: &str, default: &[&str], upper: bool) -> Vec<String>; // mirrors validators.pyx:30 _env_list: comma-split, trim, drop blanks, upper-case when `upper`
  static UDF_LIST: Lazy<Vec<String>>;         // env_list("UDF_LIST", &DEFAULT_UDF_LIST, true) — verified: validators.rs:21 (today a static 7-name &str vec)
  static PG_CONSTANTS: Lazy<Vec<String>>;     // env_list("PG_CONSTANTS", &["CURRENT_DATE", "CURRENT_TIMESTAMP"], true) — verified: validators.rs:34
  static PG_UDF: Lazy<Vec<String>>;           // env_list("PG_UDF", &["now()"], false) — same default as validators.pyx:42
  pub fn is_udf(value: &str) -> bool;         // unchanged signature (already exported, lib.rs:44); membership over the env-aware list
  pub fn is_pg_function(value: &str) -> bool; // exact PG_UDF membership (no `contains('(')`)
  pub fn is_valid(key: &str, value: &str, type_hint: Option<&str>, noquote: bool) -> String; // unchanged signature; `(` no longer bypasses quote_string
  ```

### Module 2: `to_udf` allowlist and single `UDF_LIST` source
- **Path**: `querysource/utils/udf_list.py` (new), `querysource/utils/functions.pyx`,
  `querysource/utils/validators.py`, `querysource/types/validators.pyx:27-40`,
  `querysource/utils/vocabulary.py`, `tests/unit/test_vocabulary.py`
- **Responsibility**: one env-aware definition of the keyword list, consumed
  by `types/validators.pyx` (replacing its local `_env_list('UDF_LIST', udf)`)
  and by `to_udf`, which resolves **only** those names and raises otherwise.
  The default list gains `FDOW`, `LDOW`, `LDOPM`; `vocabulary.py` promotes
  them to `KeywordEntry` rows (and drops `fdow`/`ldow` from the informational
  `FUNCTION_REGISTRY`). Delete the dead pure-Python dispatcher `is_udf` (and
  its `UDF` list) from `utils/validators.py` and the `builtins` fallback from
  `to_udf`. The Rust default list is updated by M1 (same file as its env rewrite).
- **Depends on**: nothing.
- **Interface Skeleton**:
  ```python
  # querysource/utils/udf_list.py  (new)
  DEFAULT_UDF_LIST: tuple[str, ...] = ("CURRENT_YEAR", "CURRENT_MONTH", "TODAY", "YESTERDAY", "LAST_YEAR", "FDOM", "LDOM", "FDOW", "LDOW", "LDOPM")  # first 7 verified: validators.pyx:27; fdow/ldow/ldopm verified: functions.pyx:147,150,206

  def udf_list() -> list[str]:
      """Effective keyword list: ``UDF_LIST`` env (comma-separated, stripped, upper-cased)
      or DEFAULT_UDF_LIST. Returns a fresh list each call (same semantics as validators.pyx:_env_list, :30)."""

  def is_udf_keyword(value: object) -> bool:
      """True when ``value`` is a str and ``value.strip().upper()`` is in udf_list()."""

  # querysource/utils/functions.pyx  (modifies functions.pyx:26 import, :821-844)
  def to_udf(str value, *args, **kwargs):  # verified: functions.pyx:821
      """Execute the relative-date keyword ``value`` (case-insensitive) and return its result.
      Raises KeyError(value) when ``value`` is not in udf_list(); resolves the implementation
      as globals()[value.lower()] ONLY after the allowlist check. No builtins fallback."""

  # querysource/types/validators.pyx  (modifies validators.pyx:27-40)
  # UDF_LIST = udf_list()  -- the cdef list keeps its name and type; udf_keywords() (validators.pyx:219) unchanged.

  # querysource/utils/validators.py  (modifies validators.py:73-100)
  # DELETE: UDF, is_an_udf, is_udf (dispatcher). No importer remains after functions.pyx:26 is changed.

  # querysource/utils/vocabulary.py  (modifies vocabulary.py:43-86 KEYWORD_REGISTRY, :88-150 FUNCTION_REGISTRY)
  # ADD KeywordEntry rows "FDOW", "LDOW", "LDOPM" (category "date", returns "date-string",
  #   descriptions: Monday of the current week / Sunday of the current week / last day of the
  #   previous month, all YYYY-MM-DD) after "CURRENT_MONTH" (vocabulary.py:80).
  # REMOVE the FunctionEntry rows name="fdow" (vocabulary.py:132) and name="ldow" (vocabulary.py:142);
  #   FUNCTION_REGISTRY keeps date_diff, date_sum, days_ago, previous_month (4 entries).
  # tests/unit/test_vocabulary.py:47-52 — pinned size 6 -> 4 and order without fdow/ldow.
  ```

### Module 3: Idempotent literal quoting (Cython + Rust)
- **Path**: `querysource/parsers/filter_values.py`, `querysource/types/validators.pyx:430,590`,
  `rust/src/validators.rs:164`, `rust/src/pgsql_parser.rs`, `rust/src/sql_parser.rs`
- **Responsibility**: `unquote_literal` grammar in both engines;
  `Entity.quoteString`/`quote_string` call it before escaping; `to_string`
  (both engines) delegates to the idempotent quoter; Rust builder sites stop
  calling `escape_string` before `quote_string`.
- **Depends on**: nothing (M1/M2 only share the `.pyx` file).
- **Grammar (decided)**: `unquote_literal(v)` returns `Some(inner)` iff
  `len(v) >= 2`, `v[0] == v[-1] == "'"`, and `v[1:-1]` contains no single
  `'` that is not part of a `''` pair; `inner = v[1:-1].replace("''", "'")`.
  Otherwise `None`. Quoting then is: `inner = unquote_literal(v) or v`;
  `"'" + inner.replace("'", "''") + "'"`. Existing special cases of
  `Entity.quoteString` (`'None'` → `''`, `null`/`NULL` passthrough, bool,
  leading `"` → `'` when `no_dblquoting`) are kept in front of the new step.
- **Interface Skeleton**:
  ```python
  # querysource/parsers/filter_values.py  (adds after _quote, filter_values.py:52)
  def unquote_literal(value: str) -> str | None:
      """Inner text of a well-formed pre-quoted SQL literal (outer ' pair, every interior
      ' doubled) with the doubling undone; None when ``value`` is not such a literal."""

  # querysource/types/validators.pyx
  @classmethod
  def quoteString(cls, value, bool_t no_dblquoting=True):   # verified: validators.pyx:590
      """Idempotent: quoteString(quoteString(x)) == quoteString(x); round-trips x."""
  cdef str to_string(object value):                           # verified: validators.pyx:430
      """Entity.quoteString(value) — no escape_string quote-stripping."""
  ```
  ```rust
  // rust/src/validators.rs
  pub fn unquote_literal(value: &str) -> Option<String>;              // same grammar as the Python helper
  pub fn quote_string(value: &str, no_dblquoting: bool) -> String;    // verified: validators.rs:164 — idempotent
  pub fn to_string(value: &str) -> String;                            // quote_string(value, true)
  // rust/src/pgsql_parser.rs:499,520,581,589-590,602,667,674 and rust/src/sql_parser.rs:64,332,368,398,402,405:
  //   quote_string(&escape_string(v), true)  ->  quote_string(v, true)
  //   (lines 589-590 feed BETWEEN bounds: use quote_string on each bound instead of escape_string)
  ```

### Module 4: Tests and documentation
- **Path**: `tests/unit/test_where_value_hardening.py` (new),
  `rust/src/validators.rs` `#[cfg(test)]`, `docs/QUERYSOURCE_DIALECT.md`
- **Responsibility**: unit tests for `is_valid`, `to_udf`, `unquote_literal`,
  `Entity.quoteString`; end-to-end renders through the real
  `set_options()`/`build_query()` pipeline for pgsql (Rust and Cython via
  `monkeypatch.setattr(pgsql, "HAS_RUST", False)`), `SQLParser`, `CQLParser`,
  `BigQueryParser`; Rust unit tests; docs §13.1/§13.2 rewrite.
- **Depends on**: M1, M2, M3.
- **Interface Skeleton**:
  ```python
  # tests/unit/test_where_value_hardening.py (new) — reuse the harness shape of
  # tests/test_dialect_filter_preprocessing.py:13-38 (_Connection/_Redis/preprocess)
  async def test_config_key_is_a_plain_literal() -> None: ...        # AC1
  async def test_function_name_is_a_plain_literal() -> None: ...     # AC2
  def test_to_udf_rejects_non_keywords() -> None: ...                # AC2
  async def test_parenthesis_value_is_quoted_all_dialects() -> None: ...  # AC3 (incl. BigQuery comparison dict)
  async def test_apostrophe_roundtrip_all_dialects() -> None: ...    # AC4
  def test_quote_string_idempotent() -> None: ...                    # AC4 (Entity.quoteString, rust quote_string)
  def test_validators_has_no_print() -> None: ...                    # AC5 (source grep)
  ```

---

## 4. Test Specification

### Unit Tests
| Test | Module | Description |
|---|---|---|
| `test_config_key_is_a_plain_literal` | M1 | `is_valid('u', 'DBHOST')` → `"'DBHOST'"`; `is_valid('d', 'DBPWD', 'date')` → `"'DBPWD'"`; `validators.pyx` source has no `navconfig` import |
| `test_function_name_is_a_plain_literal` | M1/M2 | `is_valid('c', 'midnight')` → `"'midnight'"`; `'today'`/`'TODAY'` still resolve to a quoted date |
| `test_to_udf_rejects_non_keywords` | M2 | `to_udf('midnight')`, `to_udf('uri_exists')`, `to_udf('print')` raise `KeyError`; `to_udf('fdom')` returns a `YYYY-MM-DD` string; `UDF_LIST` env override honoured by both `udf_list()` and `to_udf` |
| `test_default_udf_list_has_week_helpers` | M2 | `udf_keywords()` equals the 10-name default; `to_udf('fdow')`, `to_udf('LDOW')`, `to_udf('ldopm')` return `YYYY-MM-DD` strings; `rs.is_udf('LDOPM')` is True |
| `test_vocabulary_week_keywords` | M2 | `build_vocabulary(udf_keywords(), pg_constants(), pg_udfs())['keywords']` lists `FDOW`, `LDOW`, `LDOPM` with description and example; `['functions']` has 4 entries |
| `test_rust_env_override_parity` | M1 | fresh interpreter with `UDF_LIST="today, fdom"`, `PG_UDF="now(),my_fn()"`: `rs.is_udf('FDOM')` True, `rs.is_udf('LDOM')` False, `rs.is_pg_function('my_fn()')` True, `rs.is_valid('x', 'my_fn()', None, False)` == `my_fn()`; same values from `udf_keywords()`/`pg_udfs()` (shape of `tests/unit/test_udf_keyword_resolution.py:90`) |
| `test_is_pg_function_exact` | M1 | Cython and Rust: `now()` → True; `pg_sleep(5)`, `(select 1)`, `lower('x')` → False; `PG_UDF` env override honoured (Cython) |
| `test_parenthesis_value_is_quoted_all_dialects` | M1 | `{"name": "pg_sleep(5)"}` and `{"amount": {">": "x) OR (1=1"}}` render as quoted literals under pgsql Rust, pgsql Cython, sql, cql; BigQuery renders `amount > "x) OR (1=1"` |
| `test_rust_is_valid_parenthesis` | M1 | `_qs_parsers.is_valid('k', 'pg_sleep(5)', None, False)` → `"'pg_sleep(5)'"` |
| `test_unquote_literal_grammar` | M3 | `"'O''Brien'"` → `O'Brien`; `"'abc'"` → `abc`; `"O'Brien"` → None; `"'a'b'"` → None; `"''"` → `""`; `"'"` → None |
| `test_quote_string_idempotent` | M3 | for x in [`O'Brien`, `plain`, `it''s`, `'pre'`, `50%`, `a\b`]: `q(q(x)) == q(x)` and `q(x) == "'" + x.replace("'", "''") + "'"` for raw x — Cython `Entity.quoteString` and Rust `quote_string` |
| `test_typed_string_keeps_apostrophe` | M3 | `is_valid('n', "O'Brien", 'string')` → `"'O''Brien'"` (Cython and Rust) |
| `test_apostrophe_roundtrip_all_dialects` | M3 | `{"name": "O'Brien"}`, `{"name": {">": "O'Brien"}}`, `{"name": ["O'Brien", "x"]}`, `{"name": "!O'Brien"}` render `'O''Brien'` under pgsql Rust, pgsql Cython, sql, cql; `"O'Brien"` under BigQuery |
| `test_ilike_branch_unchanged` | M3 | existing `tests/qsurl/test_pg_ilike.py` suite passes untouched |
| `test_validators_has_no_print` | M1 | `grep -c "print(" querysource/types/validators.pyx` == 0 |

### Integration Tests
| Test | Description |
|---|---|
| `tests/test_dialect_filter_preprocessing.py` | existing FEAT-165 pre-processing suite stays green |
| `tests/unit/test_udf_keyword_resolution.py` | Rust/Cython keyword parity and env override stay green after M2 |
| `tests/unit/test_cython_filter_fallback.py` | Cython fallback gates stay green; its pure-Python `Entity` stub (used only when the extension is missing) is updated to the idempotent shape |
| `cargo test --no-default-features` (see memory: linking recipe) | Rust unit tests for `unquote_literal`, `quote_string` idempotency, `is_pg_function`, `is_valid` |

### Test Data / Fixtures
```python
# Reuse verbatim from tests/test_dialect_filter_preprocessing.py:13-38
class _Connection: ...   # async no-op context manager
class _Redis: ...        # .connection() -> _Connection()
async def preprocess(conditions: dict, query: str = SQL, parser_cls=pgSQLParser): ...
# Force the Cython path: monkeypatch.setattr(querysource.parsers.pgsql, "HAS_RUST", False)
```

---

## 5. Acceptance Criteria

> This feature is complete when ALL of the following are true:

- [ ] AC1 — `is_valid('u', 'DBHOST')` returns `"'DBHOST'"` and `is_valid('d', 'DBPWD', 'date')` returns `"'DBPWD'"`; `querysource/types/validators.pyx` no longer imports `navconfig.config` and has no `get_config_var`.
- [ ] AC2 — `is_valid('c', 'midnight')` returns `"'midnight'"`; `to_udf('midnight')` raises `KeyError`; `to_udf('TODAY')`, `to_udf('today')`, `to_udf('fdom')` still return dates; `udf_keywords()` returns `['CURRENT_YEAR', 'CURRENT_MONTH', 'TODAY', 'YESTERDAY', 'LAST_YEAR', 'FDOM', 'LDOM', 'FDOW', 'LDOW', 'LDOPM']`, `to_udf('ldopm')` returns a `YYYY-MM-DD` date, and `build_vocabulary(udf_keywords(), pg_constants(), pg_udfs())` lists those 10 keywords and 4 informational functions; `querysource/utils/validators.py` has no `globals()[` and no `builtins` lookup.
- [ ] AC3 — `is_valid('k', 'pg_sleep(5)')` returns `"'pg_sleep(5)'"` and `is_valid('k', 'now()')` returns `now()` in both Cython and Rust (`_qs_parsers.is_valid`); `BigQueryParser` renders `{"amount": {">": "x) OR (1=1"}}` as `amount > "x) OR (1=1"`.
- [ ] AC4 — `{"name": "O'Brien"}` renders `name='O''Brien'` under pgsql (Rust and Cython), `SQLParser` and `CQLParser`, and `name="O'Brien"` under `BigQueryParser`; the same holds for comparison dicts, IN lists and `!`-negation; `Entity.quoteString` and Rust `quote_string` are idempotent per `test_quote_string_idempotent`; `is_valid('n', "O'Brien", 'string')` returns `"'O''Brien'"`.
- [ ] AC5 — `grep -c "print(" querysource/types/validators.pyx` is `0`; `grep -c "quote_string(&escape_string(" rust/src/pgsql_parser.rs rust/src/sql_parser.rs` is `0` for both files.
- [ ] AC6 — `docs/QUERYSOURCE_DIALECT.md` §13.1 states that only `UDF_LIST` keywords are resolved (no "other zero-argument helpers" sentence), its table gains `FDOW`, `LDOW`, `LDOPM` rows, and §13.2 states that any other value, including one containing `(`, is a quoted literal; `docs/FILTER_OPERATORS.md` unchanged.
- [ ] AC7 — `pytest tests/unit/test_where_value_hardening.py tests/qsurl/test_pg_ilike.py tests/unit/test_udf_keyword_resolution.py tests/test_dialect_filter_preprocessing.py tests/unit/test_cython_filter_fallback.py tests/unit/test_vocabulary.py tests/test_validators_array_type.py -v` passes after `python setup.py build_ext --inplace` and `make build-rust && make stage-rust`.
- [ ] AC8 — `cargo test --no-default-features` (from `rust/`) passes the new tests and introduces no new failures beyond the 4 pre-existing ones recorded on 2026-10-07.
- [ ] AC9 — `ruff check querysource/utils/udf_list.py querysource/parsers/filter_values.py querysource/utils/validators.py tests/unit/test_where_value_hardening.py` is clean.
- [ ] AC11 — In a fresh interpreter with `UDF_LIST="today, fdom"` and `PG_UDF="now(),my_fn()"`, `rs.is_udf('FDOM')` is True, `rs.is_udf('LDOM')` is False and `rs.is_pg_function('my_fn()')` is True, matching `udf_keywords()`/`pg_udfs()` on the Cython side.
- [ ] AC10 — No public signature changes: `is_valid`, `to_udf`, `Entity.quoteString`, `quote_string`, `is_pg_function` keep their parameters; `udf_keywords()`, `pg_udfs()`, `pg_constants()` (`validators.pyx:219-229`) unchanged.

---

## 6. Codebase Contract

> **CRITICAL — Anti-Hallucination Anchor**
> This section is the single source of truth for what exists in the codebase.
> Implementation agents MUST NOT reference imports, attributes, or methods
> not listed here without first verifying they exist via `grep` or `read`.

### Verified Imports
```python
from querysource.types.validators import is_valid, Entity, is_udf, udf_keywords, pg_udfs, pg_constants, resolve_udf_conditions  # verified: validators.pyx:625, :489, :206, :219, :227, :223, :233
from querysource.utils.functions import to_udf           # verified: functions.pyx:821 (compiled module)
from querysource.parsers.filter_values import is_udf, to_udf, _quote, base_key, is_comparison_dict, parse_between  # verified: filter_values.py:12-13, :52, :41, :46, :83
from querysource.parsers.pgsql import pgSQLParser        # verified: tests/test_dialect_filter_preprocessing.py:9
from querysource.parsers.sql import SQLParser            # verified: querysource/parsers/sql.pyx
from querysource.parsers.cql import CQLParser            # verified: querysource/parsers/cql.pyx
from querysource.parsers.bigquery import BigQueryParser  # verified: querysource/parsers/bigquery.pyx
from querysource.models import QueryObject               # verified: tests/test_dialect_filter_preprocessing.py:7
from querysource.qs_parsers import _qs_parsers as rs     # exposes is_udf, is_pgconstant, is_pg_function, escape_string, quote_string, to_string, is_valid — verified: rust/src/lib.rs:44-51
from querysource.utils.vocabulary import keyword_example, build_vocabulary, KEYWORD_REGISTRY, FUNCTION_REGISTRY, KeywordEntry  # verified: vocabulary.py:154, :166, :43, :88, :18
```

### Existing Class Signatures
```python
# querysource/types/validators.pyx
cdef list udf = ["CURRENT_YEAR", "CURRENT_MONTH", "TODAY", "YESTERDAY", "LAST_YEAR", "FDOM", "LDOM"]  # line 27
cdef list _env_list(str name, list default, bint upper = True)   # line 30 — env override helper
cdef list UDF_LIST = _env_list('UDF_LIST', udf)                 # line 40
cdef list PG_CONSTANTS = _env_list('PG_CONSTANTS', [...])       # line 41
cdef list PG_UDF = _env_list('PG_UDF', ["now()"], False)        # line 42
cdef str quoteString(object value)                              # line 155 — module-level; used by is_valid; ''-escapes (issue 48c9b3050a0c fixed)
cpdef bool_t is_udf(object value)                               # line 206 — `value in UDF_LIST` (caller upper-cases)
cdef bool_t is_pg_function(object value)                        # line 209 — BUG: `'(' in value` → True
cpdef dict resolve_udf_conditions(dict conditions, dict cond_definition = None)  # line 233 — guards with UDF_LIST before to_udf
cdef object get_config_var(object value)                        # line 427 — `config.get(value)` — TO DELETE
cdef str to_string(object value)                                # line 430 — quoteString(escape_string(value)) — lossy
cpdef object escape_string(object value)                        # line 433 — translate table deletes ' and "
cdef dict type_validators                                        # line 463 — "udf": [is_udf, to_udf], "string": [is_string, to_string], "literal": [is_object, None]
cdef class Entity                                                # line 489
    @classmethod def escapeString(cls, value)                    # line 583
    @classmethod def quoteString(cls, value, bool_t no_dblquoting=True)  # line 590 — strip outer ' then double ' (no undo of '')
cpdef object is_valid(object key, object value, str T = None, bint noquote = False)  # line 625
    # line 654: elif is_udf(str(value).upper()): return quoteString(to_udf(value))
    # line 656: elif is_pgconstant(str(value).upper()): return value.upper()
    # line 658: elif is_pg_function(value): return value           # raw pass-through — gap 3
    # line 664: val = get_config_var(value)                        # gap 1
    # line 673: val = to_udf(value)                                # gap 2
    # line 680: print(f'Valid Error on {key}:{value}, error: {ex}') # G5

# querysource/utils/functions.pyx
cpdef str fdom(object value = None, str mask = "%Y-%m-%d", str zone = None)   # line 133
cpdef str fdow(object value=None, str mask="%Y-%m-%d", str zone=None)         # line 147
cpdef str ldow(object value=None, str mask="%Y-%m-%d", str zone=None)         # line 150
cpdef str ldom(object value = None, str mask = "%Y-%m-%d", str zone = None)   # line 183
cpdef str ldopm(object value = None, str mask = "%Y-%m-%d", str zone = None)  # line 206
from .validators import is_udf        # line 26 — the pure-Python dispatcher in utils/validators.py (executes, returns result or None)
def to_udf(str value, *args, **kwargs)  # line 821 — globals()[f] (line 827/829), builtins fallback (line 832)

# querysource/utils/validators.py
UDF = [...]                            # line 74 — stale copy (lacks LAST_YEAR)
def is_an_udf(value)                   # line 75
def is_udf(value: str, *args, **kwargs) -> Callable  # line 77 — globals()/builtins dispatcher, try/finally swallows errors; only importer: functions.pyx:26

# querysource/parsers/abstract.pyx
cdef object _get_function_replacement(self, object function, str key, object val)  # line 444 — QS_VARIABLES registry (@variable path, unchanged)
cdef bint _handle_keys(self, str key, object val, dict _filter)   # line 487 — is_valid(key, value, _type) at :495, :503
async def _process_element(self, name, value, connection)         # line 510 — is_valid(key, value, _type) at :527
async def _where_element(self, key, value, connection)            # line 583 — is_valid(key, v, noquote=self.string_literal) at :611, :634, :643

# querysource/parsers/pgsql.pyx (Cython fallback, HAS_RUST at :26-28)
Entity.quoteString(...) call sites: 408 (comparison dict), 416 (COMPARISON_TOKENS), 452 (negated list), 465/483 (IN lists), 516/520 (!= negation), 564 (equality)
# querysource/parsers/sql.pyx: 200, 209, 219, 225, 254, 258 (+2) ; cql.pyx: 92, 100, 105, 128, 131, 135 (+1)
# sqlserver.pyx (3) and sosql.pyx (3) also call Entity.quoteString — fixed transitively by M3

# querysource/parsers/bigquery.pyx
cdef str bq_quote_string(object value)   # line 40 — unwrap pre-quoted + undo '' + wrap in " — the reference idempotent pattern
# comparison dict / COMPARISON_TOKENS at :247, :253 — emits bq_quote_string(str(v)); raw `(` values arrive unquoted from is_valid

# querysource/parsers/filter_values.py
def _quote(text: str) -> str           # line 52 — "'" + text.replace("'", "''") + "'"
def _normalise_bound(key, bound)       # line 62 — is_udf(bound.upper()) guard before to_udf (line 77-78)

# querysource/utils/vocabulary.py
KEYWORD_REGISTRY: dict[str, KeywordEntry]   # line 43 — 7 rows TODAY…CURRENT_MONTH (last row "CURRENT_MONTH" at line 80)
FUNCTION_REGISTRY: tuple[FunctionEntry, ...]  # line 88 — date_diff, date_sum, days_ago, previous_month, fdow (line 132), ldow (line 142); all invocable=False
def keyword_example(name: str) -> Any  # line 154 — to_udf(name.strip()) inside try/except
def build_vocabulary(udf_list, pg_constants, pg_udfs) -> dict  # line 166 — keywords follow udf_list order

# rust/src/validators.rs
static UDF_LIST: Lazy<Vec<&str>>        // line 21 — static, NOT env-overridable
static PG_CONSTANTS: Lazy<Vec<&str>>    // line 34
pub fn is_udf(value: &str) -> bool      // line 78
pub fn is_pg_function(value: &str) -> bool   // line 92 — `value.contains('(') || value == "now()"` — gap 3 mirror
pub fn escape_string(value: &str) -> String  // line 138 — strips ' and " ('"' | '\'' => {})
pub fn quote_string(value: &str, no_dblquoting: bool) -> String  // line 164 — strip outer ', double interior '
pub fn bq_quote_string(value: &str) -> String   // line 214 — unwrap + undo '' (reference pattern)
pub fn to_string(value: &str) -> String         // line 232 — quote_string(&escape_string(value), true)
pub fn is_valid(key, value, type_hint, noquote) -> String  // line 269 — `if is_pg_function(value) { return value.to_string(); }` at :335; no config/to_udf
// #[cfg(test)] mod tests at line 348 (test_quote_string_* at 379-405, test_is_valid_* at 412-470, test_bq_quote_string_* at 478-494)

# rust/src/pgsql_parser.rs — `quote_string(&escape_string(...), true)` at 499, 520, 581, 602, 667, 674; escape_string alone at 589-590 (BETWEEN bounds)
# rust/src/sql_parser.rs   — same pattern at 64, 332, 368, 398, 402, 405
# rust/src/safe_dict.rs:162 — calls validators::is_valid(key, value, type_hint, false) for raw-query placeholders
# rust/src/lib.rs:46,51 — exposes is_pg_function and is_valid as pyfunctions
```

### Integration Points
| New Component | Connects To | Via | Verified At |
|---|---|---|---|
| `udf_list()` | `validators.pyx` `UDF_LIST` | replaces `_env_list('UDF_LIST', udf)` | `validators.pyx:40` |
| `udf_list()` | `functions.pyx::to_udf` | replaces `from .validators import is_udf` | `functions.pyx:26,826` |
| `unquote_literal` (py) | `Entity.quoteString` | called before escaping | `validators.pyx:590` |
| `unquote_literal` (rs) | `quote_string` | called before escaping | `validators.rs:164` |
| `quote_string(v, true)` | pgsql/sql Rust builders | replaces `quote_string(&escape_string(v), true)` | `pgsql_parser.rs:499…674`, `sql_parser.rs:64…405` |
| `PG_UDF` static (rs) | `is_pg_function` | exact membership | `validators.rs:92` |
| closed `is_valid` | `AbstractParser._where_element/_handle_keys/_process_element` | unchanged calls | `abstract.pyx:495,503,527,611,634,643` |

### Does NOT Exist (Anti-Hallucination)
- ~~`querysource.utils.udf_list`~~ — does not exist yet (M2 creates it).
- ~~`unquote_literal`~~ — exists in neither engine (M3 creates it); `bq_quote_string` (`bigquery.pyx:40`, `validators.rs`) inlines the equivalent logic.
- ~~`PG_UDF` in `rust/src/validators.rs`~~ — only `UDF_LIST` and `PG_CONSTANTS` statics exist; Rust has no env override for any of them and no `env_list` helper (M1 creates both; `std::env::var` is not used anywhere in `rust/src/` today).
- ~~`KEYWORD_REGISTRY["FDOW"]` / `["LDOW"]` / `["LDOPM"]`~~ — `fdow`/`ldow` are `FunctionEntry` rows today (`vocabulary.py:132,142`); `ldopm` appears nowhere in `vocabulary.py`.
- ~~`querysource.types.validators.get_config_var` as a Python-visible name~~ — it is `cdef` (module-private); nothing outside `validators.pyx` references it.
- ~~`querysource.utils.getfunc.getFunction` on the SQL path~~ — `is_valid` never calls it; FEAT-166's allowlist does not cover `to_udf`.
- ~~`Entity.unquote`/`Entity.pg_literal`~~ — `pg_literal` is a function in `pgsql.pyx`/`pgsql_parser.rs` (ILIKE branch only), not an `Entity` method.
- ~~`tests/unit/test_where_value_hardening.py`~~ — does not exist (M4 creates it).
- ~~`validators.pyx` `logging`/`logger`~~ — the module has no logger today; M1 adds `import logging` and a module-level `logger = logging.getLogger(__name__)`.
- ~~`self.logger` in `validators.pyx`~~ — these are module functions, not methods; use the module logger.

### Edit Sites (Blueprint Anchors)

Verified against: `82214b25` (dev; `8f9339b2` adds only the id ledger).

| File | Action | Verbatim anchor line | Verified at | Occurrences |
|---|---|---|---|---|
| `querysource/types/validators.pyx` | MODIFY | `cdef list udf = ["CURRENT_YEAR", "CURRENT_MONTH", "TODAY", "YESTERDAY", "LAST_YEAR", "FDOM", "LDOM"]` | `validators.pyx:27` | 1 |
| `querysource/types/validators.pyx` | MODIFY | `cdef bool_t is_pg_function(object value):` | `validators.pyx:209` | 1 |
| `querysource/types/validators.pyx` | MODIFY (delete) | `cdef object get_config_var(object value):` | `validators.pyx:427` | 1 |
| `querysource/types/validators.pyx` | MODIFY | `cdef str to_string(object value):` | `validators.pyx:430` | 1 |
| `querysource/types/validators.pyx` | MODIFY | `    def quoteString(cls, value, bool_t no_dblquoting=True):` | `validators.pyx:590` | 1 |
| `querysource/types/validators.pyx` | MODIFY | `cpdef object is_valid(object key, object value, str T = None, bint noquote = False):` | `validators.pyx:625` | 1 |
| `querysource/types/validators.pyx` | MODIFY (delete) | `print(f'Valid Error on {key}:{value}, error: {ex}')` | `validators.pyx:680` | 1 |
| `querysource/utils/functions.pyx` | MODIFY | `from .validators import is_udf` | `functions.pyx:26` | 1 |
| `querysource/utils/functions.pyx` | MODIFY | `def to_udf(str value, *args, **kwargs):` | `functions.pyx:821` | 1 |
| `querysource/utils/validators.py` | MODIFY (delete block) | `def is_udf(value: str, *args, **kwargs) -> Callable:` | `validators.py:77` | 1 |
| `querysource/utils/udf_list.py` | CREATE | — | — | — |
| `querysource/parsers/filter_values.py` | MODIFY (add after) | `def _quote(text: str) -> str:` | `filter_values.py:52` | 1 |
| `rust/src/validators.rs` | MODIFY | `static UDF_LIST: Lazy<Vec<&str>> = Lazy::new(\|\| {` | `validators.rs:21` | 1 |
| `rust/src/validators.rs` | MODIFY | `static PG_CONSTANTS: Lazy<Vec<&str>> =` | `validators.rs:34` | 1 |
| `rust/src/validators.rs` | MODIFY | `pub fn is_pg_function(value: &str) -> bool {` | `validators.rs:92` | 1 |
| `rust/src/validators.rs` | MODIFY | `pub fn quote_string(value: &str, no_dblquoting: bool) -> String {` | `validators.rs:164` | 1 |
| `rust/src/validators.rs` | MODIFY | `    if is_pg_function(value) {` (inside `pub fn is_valid`, after the `is_pgconstant` block) | `validators.rs:335` | 1 |
| `rust/src/pgsql_parser.rs` | MODIFY | `quote_string(&escape_string(` — ambiguous; sites: `:499` (comparison-dict map), `:520` (`let safe_v = ...` COMPARISON_TOKENS), `:581` (negated list), `:589-590` (`let v0 = escape_string(`/`let v1 = escape_string(` BETWEEN), `:602` (IN list map), `:667` (`format!("{} != {}"`), `:674` (`&value[1..]`) | `pgsql_parser.rs` | 7 |
| `rust/src/sql_parser.rs` | MODIFY | `quote_string(&escape_string(` — ambiguous; sites: `:64` (`_ => quote_string(&escape_string(value), true),`), `:332` (IN list map), `:368` (`&s_str`), `:398` (`!=`), `:402` (`&value[1..]`), `:405` (`{key}=`) | `sql_parser.rs` | 6 |
| `docs/QUERYSOURCE_DIALECT.md` | MODIFY | `### 13.1 Value functions (date keywords)` | `QUERYSOURCE_DIALECT.md:696` | 1 |
| `docs/QUERYSOURCE_DIALECT.md` | MODIFY (add rows after) | `\| \`LDOM\` \| last day of the current month (\`YYYY-MM-DD\`) \|` | `QUERYSOURCE_DIALECT.md:711` | 1 |
| `docs/QUERYSOURCE_DIALECT.md` | MODIFY | `### 13.2 Database constants and functions` | `QUERYSOURCE_DIALECT.md:732` | 1 |
| `querysource/utils/vocabulary.py` | MODIFY (add rows after block) | `    "CURRENT_MONTH": KeywordEntry(` | `vocabulary.py:80` | 1 |
| `querysource/utils/vocabulary.py` | MODIFY (delete blocks) | `        name="fdow",` and `        name="ldow",` | `vocabulary.py:132`, `:142` | 1 each |
| `tests/unit/test_vocabulary.py` | MODIFY | `    assert len(FUNCTION_REGISTRY) == 6` | `test_vocabulary.py:51` | 1 |
| `tests/unit/test_where_value_hardening.py` | CREATE | — | — | — |
| `tests/unit/test_cython_filter_fallback.py` | MODIFY (stub only) | `from querysource.types.validators import Entity as _Entity` | `test_cython_filter_fallback.py:44` | 1 |

- `/sdd-task` MUST re-run `grep -c` for every row it uses.
- The two Rust rows carry their per-site context in the anchor cell because the
  one-line pattern is ambiguous (7 and 6 occurrences).

---

## 7. Implementation Notes & Constraints

### Patterns to Follow
- **Builders own final quoting; `is_valid` pre-quotes** (FEAT-152 pinned
  invariant, `tests/qsurl/test_pg_ilike.py:112`; FEAT-165 `_where_element`
  comments at `abstract.pyx:613-619`). Do not remove the pre-quoting; make
  the second pass idempotent instead.
- **Reference implementation for idempotent quoting**: `bq_quote_string`
  (`bigquery.pyx:40`, `validators.rs`) and the ILIKE strip-then-requote
  branch (`pgsql.pyx:418-441`, `pgsql_parser.rs:528-556`).
- **Allowlists, never dispatch by arbitrary name** (FEAT-166 decision,
  wiki `mem-230a6c102dcf`): `to_udf` must check membership *before*
  `globals()` and must not fall back to `builtins`.
- **Env-override semantics** stay exactly those of `_env_list`
  (`validators.pyx:30`): comma-separated, stripped, upper-cased for
  `UDF_LIST`, case-preserved for `PG_UDF`.
- **Logging**: module-level `logging.getLogger(__name__)` in `validators.pyx`
  (no `print`, no `navconfig.logging` dependency needed).
- **Cython rules** (`.claude/rules/cython-development.md`): keep `cdef`/`cpdef`
  signatures; `udf_list` is imported as a Python module (no `.pxd`).
- **Rebuild discipline**: `.pyx` changes need `python setup.py build_ext --inplace`;
  Rust changes need `make build-rust && make stage-rust` (memory:
  `rust-extension-stage-rust`; a stale `.so` silently passes old behaviour).
- **No `git stash`** for comparisons (memory `no-git-stash-for-comparisons`).

### Known Risks / Gotchas
- **Documented behaviour change** (gap 2): `QUERYSOURCE_DIALECT.md` §13.1
  promises `fdow`/`ldow`/`ldopm` by name; they stay available because they
  join the default `UDF_LIST` (§8 Q1). Any *other* helper (`midnight`,
  `now`, `days_ago`, …) stops resolving. The only extension mechanism is
  `UDF_LIST=...,NAME` per deployment (a zero-argument helper of
  `functions.pyx`), documented in §13.1 by M4.
- **Config-var substitution** (gap 1) may be silently relied upon by some
  slug (e.g. a filter whose value is a config key name on purpose).
  No in-repo consumer exists (`grep -rn get_config_var` → only
  `validators.pyx`). Resolved in §8 Q2 (no consumer); removal is
  unconditional (no flag), because a flag would keep the secret-oracle
  primitive alive.
- **Ambiguity of user values that look pre-quoted**: a literal request value
  `'abc'` (with its own quotes) is unwrapped to `abc` by the idempotent
  quoter. This is today's behaviour too (strip-then-requote), so it is not a
  regression; documented in §13.2.
- **`escape_string` still exists** and is still used by the `literal` type
  hint (`validators.pyx:641`, `validators.rs` `"literal"` arm) and by
  `filter_common.rs:201`. Out of scope; do not delete it.
- **Rust statics are initialised once** (`once_cell::Lazy`): after M1 the
  env override is read at first use of the extension, exactly as
  `validators.pyx` reads it at import. A test that needs a different list
  must spawn a fresh interpreter (`tests/unit/test_udf_keyword_resolution.py:90`
  pattern). Changing `UDF_LIST` at runtime after import stays unsupported on
  both paths (unchanged).
- **`tests/unit/test_cython_filter_fallback.py`** carries a pure-Python
  `Entity` stub used only when the extension cannot be imported; keep it in
  sync or its simulation diverges from the compiled behaviour.
- **`tests/qsurl/test_pg_ilike.py::test_pinned_is_valid_prequoting_shape`**
  pins `is_valid` pre-quoting; it must stay green (it does under this design).
- **Blast radius of `Entity.quoteString`**: 36 call sites across
  `pgsql.pyx` (9), `sql.pyx` (8), `cql.pyx` (7), `sqlserver.pyx` (3),
  `sosql.pyx` (3), `handlers/_pagination.py` (2), `repositories/definitions.py`
  (2), `handlers/manager.py` (1), `utils/fn.py` (1). Idempotency only
  changes outputs for inputs that were already pre-quoted — i.e. exactly the
  double-escaped cases — so non-filter call sites are unaffected, but M4 must
  run the full `tests/` suite, not only the listed files.
- **Cargo baseline**: 4 pre-existing failures on dev as of 2026-10-07
  (memory `rust-cargo-test-no-default-features`); AC8 compares against that
  baseline.

### External Dependencies
| Package | Version | Reason |
|---|---|---|
| — | — | No new dependencies (pure Python, Cython, Rust std + existing `once_cell`) |

---

## 8. Open Questions

- [x] Feature or hotfix flow? — *Resolved at spec time*: `feature` on `dev`,
  like FEAT-166 (no Jira key; multi-file change across Cython and Rust).
- [x] Q1 — Should the default `UDF_LIST` gain `FDOW`, `LDOW`, `LDOPM` (the
  helpers §13.1 currently advertises), with matching `KeywordEntry` rows in
  `vocabulary.py` and the Rust static? — *Resolved by Jesus Lara
  (2026-10-12)*: **yes, incorporate them** — default list in Cython
  (`udf_list.py`), Rust (`validators.rs`) and `KEYWORD_REGISTRY` (M1/M2, AC2).
- [x] Q2 — Is any deployed slug relying on config-variable substitution in a
  filter value? — *Resolved by Jesus Lara (2026-10-12)*: **no** — removed
  without a feature flag (M1, AC1).
- [x] Q3 — Should a value containing `(` be *rejected* (`ParserError`) rather
  than quoted as a literal? — *Resolved by Jesus Lara (2026-10-12)*: **quote
  it as a literal** (M1, AC3; `Acme (US)` keeps working).
- [x] Q4 — Open a ledger issue for the Rust-side env-override gap
  (`UDF_LIST`/`PG_UDF` static in `validators.rs`) rather than fixing it here?
  — *Resolved by Jesus Lara (2026-10-12)*: **fix it in this feature** — the
  three Rust statics become env-aware (M1, G7, AC11).

---

## 9. Design Research Cross-Check

> Independent design opinion from the `codex` seat over the **accepted exploration
> doc** (never over this spec). Model: `—` · Status: skipped (no accepted
> exploration document: this spec was derived directly from the in-session
> verification of FEAT-166's non-goals; `codex` is installed but the §3b
> precondition is not met) · Transcript: —

| # | Suggestion (kind) | Disposition | Reason | Landed in |
|---|---|---|---|---|

Summary: **0** confirmed · **0** rejected · **0** escalated.

---

## Worktree Strategy

- **Isolation**: one feature worktree
  `.claude/worktrees/feat-FEAT-167-where-value-hardening` from `dev`; the
  `sdd-coder` engine gives each task its own sub-worktree inside it.
- **Module dependency graph**:
  - M1 has no import edge to M2 or M3 (it deletes code and changes a
    membership test).
  - M2 → nothing (new module + two import rewrites). M1 and M2 both edit
    `validators.pyx` (different line ranges: M1 `:209,:427,:625-682`;
    M2 `:27-40`) — serialized through the shared file, not a dependency.
  - M3 → nothing; it edits `validators.pyx:430,:590` (shared file with M1/M2),
    `filter_values.py`, and the Rust files.
  - M4 → M1, M2, M3 (tests assert the combined behaviour; docs describe it).
- **Shared files**: `querysource/types/validators.pyx` (M1, M2, M3) and
  `rust/src/validators.rs` (M1, M3) — their tasks are serialized.
  `vocabulary.py` and `tests/unit/test_vocabulary.py` are M2-only.
- **Exclusive resources**: Cython rebuild (`build_ext --inplace`) and Rust
  rebuild + stage (`make build-rust && make stage-rust`) mutate shared
  artefacts → tasks that rebuild are `parallel: false`.
- **Cross-feature dependencies**: FEAT-166 (merged, `23a03c91`) — none
  pending.

---

## Revision History

| Version | Date | Author | Change |
|---|---|---|---|
| 0.1 | 2026-10-12 | Jesus Lara (with Claude Code) | Initial draft from in-session verification of FEAT-166 residual gaps and the double-quoting defect |
| 0.2 | 2026-10-12 | Jesus Lara (with Claude Code) | §8 Q1–Q4 resolved: FDOW/LDOW/LDOPM join the default keyword list (vocabulary promoted), config substitution removed unconditionally, `(` values quoted, Rust allowlists env-aware (G7, AC11) |
