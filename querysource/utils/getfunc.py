"""Resolve a named helper function from the QuerySource function allowlist."""
from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from navconfig.logging import logging

from . import functions as qsfunctions

# Module-owned helpers that must not be reachable by name from a request:
# ``uri_exists`` performs an outbound HTTP request to a caller-supplied URI
# (SSRF primitive); ``to_udf`` is itself a name-based dispatcher.
_DENIED: frozenset[str] = frozenset({'uri_exists', 'to_udf'})


def getFunction(fname: str) -> Optional[Callable]:
    """Return the callable ``querysource.utils.functions.<fname>`` or ``None``.

    Only public callables *defined by* ``querysource.utils.functions`` are
    resolvable (names it merely re-exports, such as ``Path`` or ``UUID``, are
    not), minus ``_DENIED``. A name that is not a ``str``, starts with ``_``,
    contains ``.`` or does not resolve returns ``None``. There is deliberately
    no fallback to module globals, Python built-in names or dynamic module
    imports (FEAT-166).

    Args:
        fname: Function name as written in a query/pipeline definition.

    Returns:
        The allowlisted callable, or ``None`` when the name is not allowed.
    """
    if not isinstance(fname, str) or not fname or fname.startswith('_') or '.' in fname:
        logging.warning("Function %r is not an allowed QuerySource function.", fname)
        return None
    if fname in _DENIED:
        logging.warning("Function %r is not allowed from a query definition.", fname)
        return None
    func = getattr(qsfunctions, fname, None)
    if not callable(func) or getattr(func, '__module__', None) != qsfunctions.__name__:
        logging.warning("Function %r not found in querysource.utils.functions.", fname)
        return None
    return func
