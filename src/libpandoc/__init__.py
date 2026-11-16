"""pandoc as a Python library, in process, through libpandoc's C ABI.

    >>> import libpandoc as pandoc
    >>> pandoc.convert("*hi*", from_="markdown", to="html")
    '<p><em>hi</em></p>\\n'
    >>> pandoc.run(["-f", "markdown", "-t", "latex"], input="*hi*")
    b'\\\\emph{hi}\\n'
    >>> doc = pandoc.read("*hi*")            # the AST, as panir objects
    >>> pandoc.write(doc, to="rst")
    '*hi*\\n'

Options are pandoc's defaults-file keys (https://pandoc.org/MANUAL.html#defaults-files),
given as keyword arguments with ``_`` for ``-`` (``reference_doc=``,
``from_=`` for ``from``) or as a dict, and libpandoc's own: ``timeout``
(seconds; ``PandocTimeoutError``) and ``untrusted``. Warnings pandoc
reports are issued as ``PandocWarning``; failures raise ``PandocError``.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import re
import sys
import warnings
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import panir
from panir import Conversion, Filter, Pandoc

if sys.platform == "emscripten":  # Pyodide: libpandoc.wasm
    from . import _wasm as _core
else:
    from . import _core
from ._wasmfilter import WasmFilter

__all__ = [
    "PandocError",
    "PandocTimeoutError",
    "PandocWarning",
    "WasmFilter",
    "convert",
    "default_template",
    "extensions",
    "input_formats",
    "load",
    "num_threads",
    "output_formats",
    "pandoc_api_version",
    "pandoc_version",
    "query",
    "read",
    "read_many",
    "read_options",
    "run",
    "set_num_threads",
    "write",
]

logger = logging.getLogger(__name__)

Source = str | bytes | None


class PandocError(Exception):
    """A pandoc failure.

    ``kind`` is pandoc's error constructor, e.g. ``"PandocParseError"`` or
    ``"PandocUnknownReaderError"``; ``message`` is what the pandoc CLI would
    print.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}"

    def __reduce__(self) -> tuple[Any, ...]:  # (pickled, e.g. by multiprocessing)
        return type(self), (self.kind, self.message)


class PandocTimeoutError(PandocError, TimeoutError):
    """A call stopped by its time limit (the option ``timeout``, seconds):
    libpandoc's error kind ``"Timeout"``. Both a ``PandocError`` and a
    ``TimeoutError`` (so an ``OSError``, as Python's other time limits are),
    so that either catches it."""


def _error(kind: str | None, message: str | None) -> PandocError:
    kind = kind or "Exception"
    return (PandocTimeoutError if kind == "Timeout" else PandocError)(kind, message or "")


class PandocWarning(UserWarning):
    """A warning from pandoc, e.g. a missing image or unresolved citation."""


_HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "")


def _caller() -> int:
    """``warnings.warn``'s stacklevel, called from ``_check``, for the
    first frame outside this package: the line that called it, through
    however many of its functions (write calls convert, ...)."""
    level, frame = 2, sys._getframe(2)
    while frame is not None and frame.f_code.co_filename.startswith(_HERE):
        level, frame = level + 1, frame.f_back
    return level


def _check(status: int, output: bytes, kind: str | None, message: str | None,
           log: bytes) -> bytes:
    for msg in json.loads(log or b"[]"):
        level = msg.get("verbosity", "WARNING")
        text = _render_log_message(msg)
        if level == "WARNING":
            warnings.warn(text, PandocWarning, stacklevel=_caller())
        elif level == "ERROR":
            logger.error(text)
        else:
            logger.info(text)
    if status != 0:
        raise _error(kind, message)
    return output


def _render_log_message(msg: dict[str, Any]) -> str:
    if "pretty" in msg:
        return f"[{msg.get('type')}] {msg['pretty']}"
    rest = {k: v for k, v in msg.items() if k not in ("type", "verbosity")}
    return f"[{msg.get('type')}] " + ", ".join(f"{k}: {v}" for k, v in rest.items())


def _encode(source: Source) -> bytes | None:
    if source is None or isinstance(source, bytes):
        return source
    if isinstance(source, str):
        return source.encode("utf-8")
    raise TypeError(
        f"the source is text (str) or bytes, not {type(source).__name__} "
        f"(files are named in input_files)"
    )


def _input(source: Source, opts: Mapping[str, Any], fn: str) -> bytes | None:
    """The input of a conversion: ``source``, or None for the options'
    input files. With neither, pandoc would read this process's standard
    input and close it, so that ``sys.stdin`` stops working: refused."""
    if source is None and not (opts.get("input-files") or opts.get("input-file")):
        raise TypeError(
            f"{fn}() needs a source or input_files (for standard input: "
            f"{fn}(sys.stdin.buffer.read(), ...))"
        )
    return _encode(source)


def _options(options: Mapping[str, Any] | None, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    opts = dict(options or {})
    for key, value in kwargs.items():
        key = key.rstrip("_").replace("_", "-")
        opts[key] = value
    return opts


def _format(opts: dict[str, Any], key: str, alias: str, value: Any, default: str) -> None:
    """Set the format a function reads or writes (read's ``from``, write's
    ``to``): ``value`` if given, else the options' (``key``, or pandoc's
    other name for it, ``alias``), else ``default``."""
    if value is None:
        value = opts.get(key) or opts.get(alias) or default
    opts.pop(alias, None)
    opts[key] = value


def _refuse(opts: Mapping[str, Any], keys: Iterable[str], fn: str, why: str) -> None:
    """A TypeError for options a function would override or ignore."""
    given = [k for k in keys if k in opts]
    if given:
        raise TypeError(f"{fn}() doesn't take {', '.join(map(repr, given))}: {why}")


def _dumps(value: Any) -> bytes:
    """JSON for libpandoc, with paths (``os.PathLike`` or bytes) anywhere,
    e.g. in ``input_files``, as their text. libpandoc's JSON is UTF-8, so a
    file name that isn't (bytes Python keeps as surrogate escapes) can't be
    named in it: a ValueError, rather than a name for another file."""
    try:
        return json.dumps(value, default=_path, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as e:
        near = e.object[max(e.start - 30, 0):e.end + 30]
        raise ValueError(
            f"not UTF-8, which libpandoc's options must be (a file name? pandocpy's "
            f"arguments may be any file name): ...{near!r}..."
        ) from None


def _path(o: Any) -> str:
    if isinstance(o, (os.PathLike, bytes)):
        return os.fsdecode(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def query(name: str, **params: Any) -> Any:
    """Answer one of libpandoc's queries (see libpandoc.h), as parsed JSON."""
    return json.loads(_check(*_core.query(_dumps({"query": name, **params}))))


def num_threads() -> int:
    """How many threads pandoc runs on, or, before they have grown (below),
    will run on: one per logical core this process may use (its CPU
    affinity), unless ``$LIBPANDOC_NUM_THREADS`` (read at start, like
    ``$OMP_NUM_THREADS``) or ``set_num_threads`` says otherwise; at most one
    per such core. Conversions from different Python threads, and
    ``read_many``, run in parallel on them.

    pandoc starts on one thread and grows to that number once, the first
    time it runs something in parallel: a call starting while another
    thread's runs, or ``read_many`` with more than one text. In Pyodide,
    always 1."""
    return int(query("num-threads"))


def set_num_threads(n: int) -> int:
    """Set the number of threads pandoc will grow to (``num_threads``),
    clamped to 1 to one per logical core this process may use; returns the
    number in effect.

    Only until pandoc's threads have grown: after that it changes nothing
    and returns the number they grew to (libpandoc does not resize a
    running pandoc, which hung it now and then). So a result other than
    ``n`` means that ``n`` was out of range or that it came too late: call
    it before pandoc first runs anything in parallel, or set
    ``$LIBPANDOC_NUM_THREADS``."""
    in_effect = _core.set_num_threads(n)
    if in_effect < 1:  # (0: libpandoc's runtime has stopped)
        raise RuntimeError("libpandoc's runtime is not running")
    return in_effect


@functools.cache
def pandoc_version() -> str:
    """The version of the pandoc library in use, e.g. ``"3.14"``."""
    return query("version")


@functools.cache
def pandoc_api_version() -> tuple[int, ...]:
    """The pandoc-types API version of its AST, e.g. ``(1, 23, 1)``."""
    return tuple(query("api-version"))


@functools.cache
def input_formats() -> list[str]:
    """Reader names, as ``pandoc --list-input-formats``."""
    return query("input-formats")


@functools.cache
def output_formats() -> list[str]:
    """Writer names, as ``pandoc --list-output-formats``."""
    return query("output-formats")


def extensions(format: str) -> dict[str, bool]:
    """The extensions a format supports, and whether each is on by default."""
    return query("extensions-for-format", format=format)


def default_template(format: str) -> str:
    """The default template for a format, as ``pandoc -D FORMAT``."""
    return query("default-template", format=format)


@functools.cache
def _text_writers() -> frozenset[str]:
    # writers producing text; docx, odt, epub, pptx, pdf, chunkedhtml etc. are binary
    binary = {"docx", "odt", "epub", "epub2", "epub3", "pptx", "pdf", "chunkedhtml", "xlsx"}
    return frozenset(f for f in output_formats() if f not in binary)


def _is_text_format(to: Any) -> bool:
    if not isinstance(to, str) or ".lua" in to.lower():  # (a Lua writer's path: my-writer.lua)
        return True
    return re.split("[+-]", to, maxsplit=1)[0] in _text_writers()


def _output(out: bytes, opts: Mapping[str, Any]) -> str | bytes:
    """What a conversion returns: ``""`` if it wrote a file, else its
    output, as text for a text format (``to``, or its other name
    ``writer``)."""
    target = opts.get("output-file")
    if target is not None and os.fsdecode(target) != "-":  # ("-", as None: standard output)
        return ""
    return out.decode("utf-8") if _is_text_format(opts.get("to") or opts.get("writer") or "html") else out


def convert(
    source: Source = None,
    options: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> str | bytes:
    """Convert ``source`` as ``pandoc`` would with these defaults-file options.

    ``source`` is standard input (str or bytes); None means the options'
    ``input_files``. Returns the output as ``str`` for text formats and
    ``bytes`` for binary ones (docx, pdf, ...); if ``output_file`` is set the
    output is written there and ``""`` is returned.

    ``filters`` may mix pandoc's (Lua or JSON filter paths) with Python ones:
    ``panir.Filter``s, or functions that take a ``Pandoc`` (and optionally
    a ``panir.Conversion``) and change it or return a new one. They run in
    order, in this process, within one pandoc run, as ``--filter`` would. A
    Python filter's exception is raised from ``convert`` as it is.

    ``timeout=`` (seconds; libpandoc's option, not pandoc's) stops a
    conversion still running after that long with a ``PandocTimeoutError``;
    the process goes on. It stops pandoc's own code (readers, writers,
    templates, citeproc) and a JSON filter pandoc runs, but not code in this
    process or in Lua: a Python filter (a function, or a script run in
    process or by this package as a subprocess) runs to its end, and the
    conversion then fails with ``PandocTimeoutError`` when it returns; a Lua
    filter isn't stopped at all. There is no memory limit: for documents
    from someone you don't trust, give the conversion a process of its own
    (or libpandoc.wasm) and a ``timeout``, with ``sandbox=True`` or
    ``untrusted=True`` so that it reads nothing the document names.

        convert("# Hi", from_="markdown", to="docx")
        convert(options={"input-files": ["a.md"], "output-file": "a.pdf"})
    """
    opts = _options(options, kwargs)
    return _output(_convert(_input(source, opts, "convert"), opts), opts)


def _convert(source: bytes | None, opts: Mapping[str, Any]) -> bytes:
    """pandoc's output for these options, filters in Python included."""
    filters = opts.get("filters") or ()
    if isinstance(filters, (str, bytes, os.PathLike)):
        raise TypeError(f"filters is a list, not one filter: filters=[{filters!r}]")
    callbacks, entries = _callbacks(filters, opts)
    if callbacks:
        pandoc_opts = {**opts, "filters": entries}
        return _check(*_core.convert_filters(_dumps(pandoc_opts), source, callbacks))
    return _check(*_core.convert(_dumps(opts), source))


def _is_python_filter(f: Any) -> bool:
    return isinstance(f, Filter) or (callable(f) and not isinstance(f, WasmFilter))


def _callbacks(
    filters: Sequence[Any], opts: Mapping[str, Any] | None, args: Sequence[str] = ()
) -> tuple[tuple[Any, ...], list[Any]]:
    """Python filters as libpandoc callbacks, and the filters list referring
    to them by index. Consecutive Python objects (Filters, functions) share
    one callback (one JSON round trip). Python filter scripts (a JSON
    filter's path, ``.py`` or a Python ``#!``) run in this process, each its
    own callback, unless opted out (see ``libpandoc._scripts``). Wasm
    filters (``WasmFilter``, or a JSON filter's path ending in ``.wasm``)
    run in this process too, each its own callback; ``args``, the command
    line if any, may name the user data directory where they are looked
    for, as pandoc does."""
    from . import _scripts

    callbacks: list[Any] = []
    entries: list[Any] = []
    group: list[Any] = []

    def flush() -> None:
        if group:
            entries.append({"type": "callback", "index": len(callbacks)})
            callbacks.append(_callback(tuple(group), opts))
            group.clear()

    for f in filters:
        if _is_python_filter(f):
            group.append(f)
            continue
        flush()
        path = _json_filter_path(f)
        if isinstance(f, WasmFilter):
            entries.append({"type": "callback", "index": len(callbacks)})
            callbacks.append(f)
        elif path is not None and path.lower().endswith(".wasm"):
            found = _find_filter(path, opts, args)
            if found is None:
                raise FileNotFoundError(f"wasm filter {path} not found")
            entries.append({"type": "callback", "index": len(callbacks)})
            callbacks.append(_wasm_filter(found))
        elif path is not None and _scripts.is_python_script(path):
            entries.append({"type": "callback", "index": len(callbacks)})
            callbacks.append(_scripts.callback(
                path, pandoc_version(), _conversion, options=opts,
                in_process=_scripts.opted_out(path) is None,
            ))
        else:
            entries.append(f)
    flush()
    return tuple(callbacks), entries


def _find_filter(path: str, opts: Mapping[str, Any] | None, args: Sequence[str]) -> str | None:
    """A filter's file as pandoc finds one: as given, else in the user data
    directory's ``filters/``."""
    if os.path.isfile(path):
        return path
    data_dir = _data_dir(opts, args)
    found = None if data_dir is None else os.path.join(data_dir, "filters", path)
    return found if found is not None and os.path.isfile(found) else None


def _data_dir(opts: Mapping[str, Any] | None, args: Sequence[str]) -> str | None:
    """pandoc's user data directory: ``--data-dir`` (or the option), else
    ``$XDG_DATA_HOME/pandoc`` if it exists, else ``~/.pandoc``."""
    if opts is not None and "data-dir" in opts:
        return os.fspath(opts["data-dir"])
    for i, a in enumerate(args):
        if a.startswith("--data-dir="):
            return a[len("--data-dir="):]
        if a == "--data-dir" and i + 1 < len(args):
            return args[i + 1]
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    xdg = os.path.join(xdg, "pandoc")
    return xdg if os.path.isdir(xdg) else os.path.expanduser("~/.pandoc")


_WASM_FILTERS: dict[str, tuple[float, WasmFilter]] = {}


def _wasm_filter(path: str) -> WasmFilter:
    """A wasm filter by path, compiled once per process (again if the file
    changes)."""
    key, mtime = os.path.abspath(path), os.path.getmtime(path)
    cached = _WASM_FILTERS.get(key)
    if cached is None or cached[0] != mtime:
        cached = _WASM_FILTERS[key] = (mtime, WasmFilter(path))
    return cached[1]


def _json_filter_path(f: Any) -> str | None:
    """The path of a JSON filter as pandoc reads the "filters" option: a
    string not ending in .lua (nor "citeproc"), or {"type": "json"}."""
    if isinstance(f, (str, bytes, os.PathLike)):
        s = os.fsdecode(f)
        return None if s == "citeproc" or s.lower().endswith(".lua") else s
    if isinstance(f, Mapping) and f.get("type") == "json":
        return f.get("path")
    return None


def _callback(group: tuple[Any, ...], opts: Mapping[str, Any] | None) -> Any:
    """A libpandoc callback running Python filters on the document (``opts``:
    the conversion's options, if known)."""
    user_opts = None if opts is None else {k: v for k, v in opts.items() if k != "filters"}

    def run(doc_json: bytes, context: bytes) -> bytes:
        conversion = _conversion(json.loads(context), user_opts)
        doc = panir.loads(doc_json)
        for f in group:
            if isinstance(f, Filter):
                doc = f(doc, conversion=conversion)
            else:
                result = f(doc, conversion) if _takes_conversion(f) else f(doc)
                doc = doc if result is None else result
            if not isinstance(doc, Pandoc):
                raise TypeError(
                    f"the filter {getattr(f, '__qualname__', f)!r} returned "
                    f"{type(doc).__name__}, not a Pandoc"
                )
        return panir.dumps(doc).encode()

    return run


def _takes_conversion(fn: Any) -> bool:
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        return True
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 2


def run(args: Sequence[str], input: Source = None) -> bytes:
    """Run pandoc with command-line arguments, as ``pandoc ARGS``.

    ``input`` is standard input; the result is what pandoc writes to standard
    output. Arguments are exactly the CLI's (``--defaults`` included), except
    that informational options such as ``--version`` are rejected: use
    ``pandoc_version()`` etc. instead. They are text, paths or bytes, and
    must be UTF-8 (libpandoc's ``pandoc_convert_args``): a file name that
    isn't is a ValueError (``pandocpy`` takes any). With no ``input`` and
    no input files in the arguments, pandoc reads this process's standard
    input, as the command does, and closes it.
    """
    if isinstance(args, (str, bytes)):
        raise TypeError("run() takes a list of arguments, not one string (shlex.split(s))")
    return _check(*_core.convert_args(tuple(map(_arg, args)), _encode(input)))


def _arg(a: str | bytes | os.PathLike[Any]) -> bytes:
    s = os.fsdecode(a)
    try:
        return s.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"not UTF-8, which run's arguments must be: {s!r}") from None


def read(source: Source = None, from_: str | Conversion | None = None,
         options: Mapping[str, Any] | None = None, **kwargs: Any) -> Pandoc:
    """Parse ``source`` into a document (``panir.Pandoc``).

    ``from_`` is a format (default: the options' ``from``, else markdown),
    or, in a filter, the conversion the filter runs in
    (``ctx.conversion``): then ``source`` is read the way that conversion
    reads its input, as with ``read_many``.

    Otherwise other options (``input_files``, reader extensions in
    ``from_``, ``filters``, ``timeout``, ...) apply as in ``convert``; those
    about the output (``to``, ``output_file``) are a TypeError.
    """
    if isinstance(from_, Conversion):
        if not isinstance(source, (str, bytes)):
            raise TypeError("read(source, conversion) takes the text to read")
        text = source.decode("utf-8") if isinstance(source, bytes) else source
        return read_many([text], from_, options, **kwargs)[0]
    opts = _options(options, kwargs)
    _refuse(opts, ("to", "writer", "output-file"), "read", "it gives the document")
    _format(opts, "from", "reader", from_, "markdown")
    opts["to"] = "json"
    return panir.loads(_convert(_input(source, opts, "read"), opts))


def read_many(sources: Iterable[str], from_: str | Conversion | None = None,
              options: Mapping[str, Any] | None = None, *, return_exceptions: bool = False,
              **kwargs: Any) -> list[Any]:
    """Parse many texts, each on its own, in parallel: their documents.

    For filters that parse many fragments, such as table cells: one call
    into pandoc, which sets up the reader once and reads the texts on all
    cores, much cheaper than a ``read`` each. ``from_`` is a format, or the
    conversion a filter runs in (``ctx.conversion``), to read the way it
    reads its input. Options that affect reading apply (``tab_stop``,
    ``abbreviations``, ``resource_path``, ``sandbox``, ...; others are a
    ``TypeError``), and
    ``timeout`` (seconds), the whole call's: it returns within about that
    long, a text not read by then failing with ``PandocTimeoutError``.
    Warnings (an include file not found) are issued as ``PandocWarning``, as
    ``convert``'s.

    A text that can't be read raises its ``PandocError``, the first one's;
    with ``return_exceptions=True`` (as ``asyncio.gather``'s) each such
    error is in the list in its text's place instead, the others' documents
    kept.

        docs = libpandoc.read_many(cells, ctx.conversion)
    """
    if isinstance(sources, (str, bytes)):
        raise TypeError("read_many() takes a list of texts, not one text (read() reads one)")
    texts = [s if isinstance(s, str) else s.decode("utf-8") for s in sources]
    conversion = isinstance(from_, Conversion)
    opts = read_options(from_) if conversion else {}
    opts.update(_options(options, kwargs))
    _refuse(opts, sorted(set(opts) - _READ_MANY_OPTIONS), "read_many",
            "it takes only the options that affect reading")
    _format(opts, "from", "reader", None if conversion else from_, "markdown")
    request = _dumps({"options": opts, "inputs": texts})
    out = json.loads(_check(*_core.read_many(request)))
    docs: list[Any] = []
    for i, j in enumerate(out):
        if "error" in j:
            e = _error(j["error"].get("kind"), f"reading input {i}: {j['error'].get('message')}")
            if not return_exceptions:
                raise e
            docs.append(e)
        else:
            docs.append(Pandoc.from_json(j))
    return docs


# The conversion's options that also apply to reading a fragment of it.
_READ_OPTIONS = frozenset({
    "abbreviations", "data-dir", "default-image-extension", "indented-code-classes",
    "preserve-tabs", "resource-path", "sandbox", "strip-comments", "tab-stop",
    "track-changes",
})
# What read_many's options may be (libpandoc's list): the others, which
# libpandoc ignores, are a TypeError here rather than silently ignored.
_READ_MANY_OPTIONS = _READ_OPTIONS | {"from", "reader", "timeout", "untrusted"}

# Reader options (as pandoc gives filters) that have a defaults-file key.
_READER_OPTIONS = ("default-image-extension", "indented-code-classes", "strip-comments",
                   "tab-stop")
_TRACK_CHANGES = {"accept-changes": "accept", "reject-changes": "reject", "all-changes": "all"}


def read_options(conversion: Conversion, format: str | None = None) -> dict[str, Any]:
    """Options (defaults-file keys) to read a fragment as ``conversion``
    reads its input: its input format (or ``format``) and the options that
    affect reading, not filters, templates, metadata or the output.

    The input format is the one pandoc decided on when known, else the
    options' ``from``, else markdown: pandoc doesn't yet tell JSON filters
    it runs (jgm/pandoc#11016), though libpandoc does.
    """
    opts: dict[str, Any] = {}
    if conversion.options is not None:
        opts.update((k, v) for k, v in conversion.options.items() if k in _READ_OPTIONS)
    elif conversion.reader_options is not None:
        ro = conversion.reader_options
        opts.update((k, ro[k]) for k in _READER_OPTIONS if k in ro)
        tc = _TRACK_CHANGES.get(ro.get("track-changes", ""))
        if tc:
            opts["track-changes"] = tc
    given = conversion.options or {}
    opts["from"] = (format or conversion.input_format or given.get("from")
                    or given.get("reader") or "markdown")
    return opts


def _conversion(context: Mapping[str, Any], options: Mapping[str, Any] | None) -> Conversion:
    """The conversion libpandoc describes to an in-process filter."""
    return Conversion(
        context.get("format"),
        input_format=context.get("input-format"),
        output_format=context.get("output-format"),
        reader_options=context.get("reader-options"),
        writer_options=context.get("writer-options"),
        options=options,
    )


def write(doc: Pandoc, to: str | None = None,
          options: Mapping[str, Any] | None = None, **kwargs: Any) -> str | bytes:
    """Render a typed document, as ``convert`` from JSON would, with its
    options (``timeout`` and the rest). ``to`` is the format (default: the
    options' ``to``, else html); options about the input (``from_``,
    ``input_files``) are a TypeError."""
    opts = _options(options, kwargs)
    _refuse(opts, ("from", "reader", "input-files", "input-file"), "write",
            "its input is the document")
    _format(opts, "to", "writer", to, "html")
    opts["from"] = "json"
    return convert(panir.dumps(doc).encode(), opts)


def _check_versions() -> None:
    if tuple(panir.PANDOC_API_VERSION[:2]) != pandoc_api_version()[:2]:
        raise ImportError(
            f"panir is for pandoc API {panir.PANDOC_API_VERSION}, "
            f"but the loaded pandoc library speaks {pandoc_api_version()}"
        )


async def load(source: str | bytes | None = None) -> None:
    """In Pyodide, load libpandoc.wasm before the first call: from
    ``source`` (a URL, or the module's bytes), else from the wheel.
    Elsewhere, nothing to do: pandoc is loaded with the package."""
    if sys.platform == "emscripten":
        await _core.load(source)
        _check_versions()


# in Pyodide, once libpandoc.wasm is loaded (load(), or the host before import)
if sys.platform != "emscripten" or _core.loaded():
    _check_versions()
