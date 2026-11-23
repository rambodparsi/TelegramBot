"""Pure-Python stand-in for ``ormsgpack`` (a Rust extension with no Pyodide wheel).

Backed by ``msgpack`` (which Pyodide ships). Unlike the stub in Cloudflare's official
example, this one honours the ``default=`` and ``ext_hook=`` hooks and defines the
exception classes that LangGraph's ``JsonPlusSerializer`` relies on -- without them,
checkpointing any state that contains LangChain messages fails.
"""

from __future__ import annotations

from typing import Any, Callable

import msgpack

__version__ = "1.12.1"


class MsgpackEncodeError(TypeError):
    """Raised when an object cannot be serialized."""


class MsgpackDecodeError(ValueError):
    """Raised when bytes cannot be deserialized."""


class Ext:
    """Mirror of ``ormsgpack.Ext``: an extension type code plus payload bytes."""

    __slots__ = ("tag", "data")

    def __init__(self, tag: int, data: bytes) -> None:
        self.tag = tag
        self.data = data


# Option flags (values match ormsgpack 1.12.x). The msgpack backend ignores most of
# them; they exist so callers can OR them together without AttributeError.
OPT_NAIVE_UTC = 1
OPT_NON_STR_KEYS = 2
OPT_OMIT_MICROSECONDS = 4
OPT_PASSTHROUGH_BIG_INT = 8
OPT_PASSTHROUGH_DATACLASS = 16
OPT_PASSTHROUGH_DATETIME = 32
OPT_PASSTHROUGH_SUBCLASS = 64
OPT_PASSTHROUGH_TUPLE = 128
OPT_SERIALIZE_NUMPY = 256
OPT_SERIALIZE_PYDANTIC = 512
OPT_SORT_KEYS = 1024
OPT_UTC_Z = 2048
OPT_PASSTHROUGH_UUID = 4096
OPT_PASSTHROUGH_ENUM = 8192
OPT_DATETIME_AS_TIMESTAMP_EXT = 16384
OPT_REPLACE_SURROGATES = 32768


def _wrap_default(default: Callable[[Any], Any] | None):
    if default is None:
        return None

    def _default(obj: Any) -> Any:
        result = default(obj)
        if isinstance(result, Ext):
            return msgpack.ExtType(result.tag, result.data)
        return result

    return _default


def packb(obj: Any, default: Callable[[Any], Any] | None = None, option: int | None = None) -> bytes:
    try:
        return msgpack.packb(obj, default=_wrap_default(default), use_bin_type=True)
    except MsgpackEncodeError:
        raise
    except Exception as exc:  # msgpack raises TypeError/ValueError/OverflowError
        raise MsgpackEncodeError(str(exc)) from exc


def unpackb(
    data: bytes | bytearray | memoryview,
    ext_hook: Callable[[int, bytes], Any] | None = None,
    option: int | None = None,
) -> Any:
    kwargs: dict[str, Any] = {"raw": False, "strict_map_key": False}
    if ext_hook is not None:
        kwargs["ext_hook"] = ext_hook
    try:
        return msgpack.unpackb(bytes(data), **kwargs)
    except Exception as exc:
        raise MsgpackDecodeError(str(exc)) from exc
