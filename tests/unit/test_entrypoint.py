"""entry.py only runs inside the Worker runtime, so check its handler contract statically.

The Python Workers runtime calls handlers with extra positional arguments
(e.g. queue(batch, env, ctx), scheduled(controller, env, ctx)); a handler that does
not accept them crashes before any of our code runs.
"""

import ast
from pathlib import Path

ENTRY = Path(__file__).resolve().parents[2] / "src" / "entry.py"


def _handlers():
    tree = ast.parse(ENTRY.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Default")
    return {f.name: f for f in cls.body if isinstance(f, ast.AsyncFunctionDef)}


def test_runtime_handlers_accept_extra_positional_args():
    handlers = _handlers()
    for name in ("queue", "scheduled"):
        assert name in handlers, f"missing {name} handler"
        assert handlers[name].args.vararg is not None, f"{name}() must accept *args"


def test_fetch_takes_request():
    assert [a.arg for a in _handlers()["fetch"].args.args] == ["self", "request"]
