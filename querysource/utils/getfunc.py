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
    module globals, Python built-in names or dynamic module imports (FEAT-166).

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
