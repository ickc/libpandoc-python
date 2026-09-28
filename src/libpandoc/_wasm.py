"""libpandoc.wasm as ``_core``, in Pyodide: the same functions as the C
extension (``_core.c``), calling libpandoc's C ABI in a WebAssembly module
that the browser's (or Node's) engine runs.

The host loads libpandoc.wasm and registers its byte-level interface as the
JS module ``libpandoc_wasm`` before ``import libpandoc``::

    import { load } from "libpandoc/wasm/browser.mjs";
    const pandoc = await load(wasmUrl);
    pyodide.registerJsModule("libpandoc_wasm", pandoc.abi);

(How the package finds and loads the module itself is to be decided with
its packaging.) No threads, JSON filters or ``pandoc_main`` in wasm.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyodide.ffi import JsException, create_proxy, to_js

try:
    import libpandoc_wasm as _abi  # type: ignore[import-not-found]
except ImportError as e:
    raise ImportError(
        "libpandoc in Pyodide needs libpandoc.wasm: load it and register "
        "its `abi` as the JS module libpandoc_wasm before importing libpandoc"
    ) from e

Result = tuple[int, bytes, "str | None", "str | None", bytes]
Callback = Callable[[bytes, bytes], bytes]


def _js(b: bytes | None) -> Any:
    return None if b is None else to_js(b)


def _result(r: Any) -> Result:
    status, output, kind, message, log = r
    return int(status), output.to_bytes(), kind, message, log.encode()


def _call_filters(call: Callable[[list[Any]], Any], fns: tuple[Callback, ...]) -> Result:
    """``call`` with the filters as JS functions: the first exception a
    filter raises is re-raised, as the C extension does."""
    error: list[BaseException] = []

    def wrap(fn: Callback) -> Callable[[Any, Any], Any]:
        def run(doc: Any, ctx: Any) -> Any:
            try:
                return to_js(fn(doc.to_bytes(), ctx.to_bytes()))
            except BaseException as e:
                if not error:
                    error.append(e)
                raise
        return run

    proxies = [create_proxy(wrap(f)) for f in fns]
    try:
        r = call(to_js(proxies))
    except JsException:
        if error:
            raise error[0] from None
        raise
    finally:
        for p in proxies:
            p.destroy()
    if error:
        raise error[0]
    return _result(r)


def abi_version() -> int:
    return int(_abi.abiVersion())


def convert(options: bytes, input: bytes | None) -> Result:
    return _result(_abi.convert(_js(options), _js(input)))


def convert_args(args: tuple[bytes, ...], input: bytes | None) -> Result:
    return _result(_abi.convertArgs(to_js([a.decode() for a in args]), _js(input)))


def convert_filters(options: bytes, input: bytes | None, filters: tuple[Callback, ...]) -> Result:
    return _call_filters(lambda fs: _abi.convertFilters(_js(options), _js(input), fs), filters)


def convert_args_filters(args: tuple[bytes, ...], input: bytes | None,
                         filters: tuple[Callback, ...]) -> Result:
    argv = to_js([a.decode() for a in args])
    return _call_filters(lambda fs: _abi.convertArgsFilters(argv, _js(input), fs), filters)


def read_many(request: bytes) -> Result:
    return _result(_abi.readMany(_js(request)))


def query(q: bytes) -> Result:
    return _result(_abi.query(_js(q)))


def set_num_threads(n: int) -> int:
    return 1  # one thread in wasm


def main(argv: tuple[bytes, ...], filters: bytes | None, callbacks: tuple[Any, ...]) -> int:
    raise NotImplementedError("the pandoc command (pandoc_main) is not in libpandoc.wasm")
