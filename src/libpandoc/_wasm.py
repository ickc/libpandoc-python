"""libpandoc.wasm as ``_core``, in Pyodide: the same functions as the C
extension (``_core.c``), calling libpandoc's C ABI in a WebAssembly module
that the browser's (or Node's) engine runs.

``await libpandoc.load()`` loads the module the wheel for Pyodide carries
(``_pyodide/``: libpandoc.wasm, and libpandoc's ``wasm/pyodide.mjs``
bundled), or one from a URL, and registers its byte-level interface as the
JS module ``libpandoc_wasm``. A host may register one itself before
``import libpandoc`` instead::

    import { load } from "libpandoc/wasm/browser.mjs";
    const pandoc = await load(wasmUrl);
    pyodide.registerJsModule("libpandoc_wasm", pandoc.abi);

No threads, JSON filters or ``pandoc_main`` in wasm.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from pyodide.ffi import JsException, create_proxy, to_js

_DATA = os.path.join(os.path.dirname(__file__), "_pyodide")


class _NotLoaded:
    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(
            "libpandoc.wasm is not loaded: `await libpandoc.load()` first"
        )


try:
    import libpandoc_wasm as _abi  # type: ignore[import-not-found]
except ImportError:
    _abi = _NotLoaded()


def loaded() -> bool:
    return not isinstance(_abi, _NotLoaded)


async def load(source: str | bytes | None = None) -> None:
    """Load libpandoc.wasm: from ``source`` (a URL, fetched and compiled as
    it streams, or the module's bytes), else from the wheel. Pyodide's
    filesystem is pandoc's. Once loaded, calls are synchronous; loading
    again replaces the instance (after a ``StoppedError``, for one)."""
    global _abi
    import pyodide_js  # type: ignore[import-not-found]
    from pyodide.code import run_js

    with open(os.path.join(_DATA, "libpandoc-pyodide.mjs"), encoding="utf-8") as f:
        glue = f.read()
    if source is None:
        path = os.path.join(_DATA, "libpandoc.wasm")
        if not os.path.exists(path):
            raise FileNotFoundError(
                "this libpandoc has no libpandoc.wasm: give load() its URL"
            )
        # read by JS, so that 60 MB don't cross as Python bytes
        js_source = pyodide_js.FS.readFile(path)
    elif isinstance(source, bytes):
        js_source = to_js(source)
    else:
        js_source = source
    imp = run_js(
        "(text) => import('data:text/javascript;charset=utf-8,' + encodeURIComponent(text))"
    )
    glue_module = await imp(glue)
    _abi = (await glue_module.loadForPyodide(pyodide_js, js_source)).abi


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
