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

The module is checked as the native library is (``_core.c``): the same
major version and at least the minor this package needs. No threads or
JSON filters here, and no pandoc command: libpandoc.wasm has
``pandoc_main`` from 1.11, but its standard streams would be the JS host's
WASI ones, not Python's.
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


# The libpandoc this package needs, as _core.c's MIN_ABI_VERSION.
MIN_ABI_VERSION = 1011


def _checked(abi: Any) -> Any:
    """``abi`` if its libpandoc.wasm is one this package works with (the
    same major version, a minor at least as large), else ImportError."""
    v = int(abi.abiVersion())
    need = MIN_ABI_VERSION
    if v // 1000 != need // 1000 or v % 1000 < need % 1000:
        raise ImportError(
            f"libpandoc.wasm {v // 1000}.{v % 1000} is loaded, and this module needs "
            f"libpandoc {need // 1000}.{need % 1000} or a later {need // 1000}.x"
        )
    return abi


try:
    import libpandoc_wasm  # type: ignore[import-not-found]
except ImportError:
    _abi: Any = _NotLoaded()
else:
    _abi = _checked(libpandoc_wasm)


def loaded() -> bool:
    return not isinstance(_abi, _NotLoaded)


_cwd = "/"  # pandoc's working directory (wasi-libc's), as last set


def _chdir() -> None:
    """pandoc's working directory as Python's (Pyodide's filesystem is
    pandoc's), so that relative paths mean the same to both."""
    global _cwd
    cwd = os.getcwd()
    if cwd != _cwd and getattr(_abi, "chdir", None) is not None:
        if _abi.chdir(cwd) == 0:
            _cwd = cwd


async def load(source: str | bytes | None = None) -> None:
    """Load libpandoc.wasm: from ``source`` (a URL, fetched and compiled as
    it streams, or the module's bytes), else from the wheel. Pyodide's
    filesystem is pandoc's. Once loaded, calls are synchronous; loading
    again replaces the instance (after a ``StoppedError``, for one)."""
    global _abi, _cwd
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
    _abi = _checked((await glue_module.loadForPyodide(pyodide_js, js_source)).abi)
    _cwd = "/"


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
    _chdir()
    return _result(_abi.convert(_js(options), _js(input)))


def convert_args(args: tuple[bytes, ...], input: bytes | None) -> Result:
    _chdir()
    return _result(_abi.convertArgs(to_js([a.decode() for a in args]), _js(input)))


def convert_filters(options: bytes, input: bytes | None, filters: tuple[Callback, ...]) -> Result:
    _chdir()
    return _call_filters(lambda fs: _abi.convertFilters(_js(options), _js(input), fs), filters)


def convert_args_filters(args: tuple[bytes, ...], input: bytes | None,
                         filters: tuple[Callback, ...]) -> Result:
    _chdir()
    argv = to_js([a.decode() for a in args])
    return _call_filters(lambda fs: _abi.convertArgsFilters(argv, _js(input), fs), filters)


def read_many(request: bytes) -> Result:
    _chdir()
    return _result(_abi.readMany(_js(request)))


def query(q: bytes) -> Result:
    return _result(_abi.query(_js(q)))


def set_num_threads(n: int) -> int:
    return 1  # one thread in wasm (libpandoc.wasm answers the query so too)


def main(argv: tuple[bytes, ...], filters: bytes | None, callbacks: tuple[Any, ...]) -> int:
    raise NotImplementedError("the pandoc command (pandoc_main) is not available in Pyodide")
