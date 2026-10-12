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
    "importlib", "logging", "qsfunctions", "builtins",         # module globals → None
    "_private", "", None, 42, ["today"],                        # malformed → None
])
def test_disallowed_names_return_none(name):
    assert getFunction(name) is None


def test_non_callable_attribute_returns_none():
    # module-level non-callable attributes of qsfunctions must not be returned
    non_callables = [
        n for n in dir(qsfunctions)
        if not n.startswith('_') and not callable(getattr(qsfunctions, n))
    ]
    for name in non_callables:
        assert getFunction(name) is None, name


def test_getfunc_module_has_no_dynamic_import_machinery():
    import querysource.utils.getfunc as mod

    for forbidden in ("importlib", "builtins", "contextlib"):
        assert not hasattr(mod, forbidden), forbidden
