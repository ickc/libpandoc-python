"""pandoc as a Python library, in process, through libpandoc's C ABI.

    >>> import libpandoc as pandoc
    >>> pandoc.convert("*hi*", from_="markdown", to="html")
    '<p><em>hi</em></p>\\n'
    >>> pandoc.run(["-f", "markdown", "-t", "latex"], input="*hi*")
    b'\\\\emph{hi}\\n'
    >>> doc = pandoc.read("*hi*")            # the AST, as pandom objects
    >>> pandoc.write(doc, to="rst")
    '*hi*\\n'

Options are pandoc's defaults-file keys (https://pandoc.org/MANUAL.html#defaults-files),
given as keyword arguments with ``_`` for ``-`` (``reference_doc=``,
``from_=`` for ``from``) or as a dict. Warnings pandoc reports are issued as
``PandocWarning``; failures raise ``PandocError``.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import warnings
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import pandom
from pandom import Conversion, Filter, Pandoc

from . import _core

__all__ = [
    "PandocError",
    "PandocWarning",
    "convert",
    "default_template",
    "extensions",
    "input_formats",
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


class PandocWarning(UserWarning):
    """A warning from pandoc, e.g. a missing image or unresolved citation."""


def _check(status: int, output: bytes, kind: str | None, message: str | None,
           log: bytes) -> bytes:
    for msg in json.loads(log or b"[]"):
        level = msg.get("verbosity", "WARNING")
        text = _render_log_message(msg)
        if level == "WARNING":
            warnings.warn(text, PandocWarning, stacklevel=4)
        elif level == "ERROR":
            logger.error(text)
        else:
            logger.info(text)
    if status != 0:
        raise PandocError(kind or "Exception", message or "")
    return output


def _render_log_message(msg: dict[str, Any]) -> str:
    if "pretty" in msg:
        return f"[{msg.get('type')}] {msg['pretty']}"
    rest = {k: v for k, v in msg.items() if k not in ("type", "verbosity")}
    return f"[{msg.get('type')}] " + ", ".join(f"{k}: {v}" for k, v in rest.items())


def _encode(source: Source) -> bytes | None:
    if source is None or isinstance(source, bytes):
        return source
    return source.encode("utf-8")


def _options(options: Mapping[str, Any] | None, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    opts = dict(options or {})
    for key, value in kwargs.items():
        key = key.rstrip("_").replace("_", "-")
        opts[key] = value
    return opts


def _dumps(opts: Mapping[str, Any]) -> bytes:
    # paths may appear anywhere, e.g. in input_files lists
    return json.dumps(opts, default=os.fspath).encode()


def query(name: str, **params: Any) -> Any:
    """Answer one of libpandoc's queries (see libpandoc.h), as parsed JSON."""
    raw = json.dumps({"query": name, **params}).encode()
    return json.loads(_check(*_core.query(raw)))


def num_threads() -> int:
    """How many threads pandoc runs on: one per logical core this process
    may use (its CPU affinity), unless ``$LIBPANDOC_NUM_THREADS`` (read at
    start, like ``$OMP_NUM_THREADS``) or ``set_num_threads`` says otherwise.
    Conversions from different Python threads, and ``read_many``, run in
    parallel on them."""
    return int(query("num-threads"))


def set_num_threads(n: int) -> int:
    """Run pandoc on ``n`` threads from now on (at least 1); returns the new
    number."""
    return _core.set_num_threads(n)


@functools.cache
def pandoc_version() -> str:
    """The version of the pandoc library in use, e.g. ``"3.11"``."""
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
    if not isinstance(to, str):
        return True
    base = to.split("+")[0].split("-")[0]
    return base in _text_writers() or base.endswith(".lua")


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
    ``pandom.Filter``s, or functions that take a ``Pandoc`` (and optionally
    a ``pandom.Conversion``) and change it or return a new one. They run in
    order, in this process, within one pandoc run, as ``--filter`` would. A
    Python filter's exception is raised from ``convert`` as it is.

        convert("# Hi", from_="markdown", to="docx")
        convert(options={"input-files": ["a.md"], "output-file": "a.pdf"})
    """
    opts = _options(options, kwargs)
    filters = opts.get("filters") or ()
    callbacks, entries = _callbacks(filters, opts)
    if callbacks:
        pandoc_opts = {**opts, "filters": entries}
        out = _check(*_core.convert_filters(_dumps(pandoc_opts), _encode(source), callbacks))
    else:
        out = _check(*_core.convert(_dumps(opts), _encode(source)))
    if "output-file" in opts:
        return ""
    return out.decode("utf-8") if _is_text_format(opts.get("to", "html")) else out


def _is_python_filter(f: Any) -> bool:
    return isinstance(f, Filter) or callable(f)


def _callbacks(
    filters: Sequence[Any], opts: Mapping[str, Any] | None
) -> tuple[tuple[Any, ...], list[Any]]:
    """Python filters as libpandoc callbacks, and the filters list referring
    to them by index. Consecutive Python objects (Filters, functions) share
    one callback (one JSON round trip). Python filter scripts (a JSON
    filter's path, ``.py`` or a Python ``#!``) run in this process, each its
    own callback, unless opted out (see ``libpandoc._scripts``)."""
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
        if path is not None and _scripts.is_python_script(path):
            entries.append({"type": "callback", "index": len(callbacks)})
            callbacks.append(_scripts.callback(
                path, pandoc_version(), _conversion, options=opts,
                in_process=_scripts.opted_out(path) is None,
            ))
        else:
            entries.append(f)
    flush()
    return tuple(callbacks), entries


def _json_filter_path(f: Any) -> str | None:
    """The path of a JSON filter as pandoc reads the "filters" option: a
    string not ending in .lua (nor "citeproc"), or {"type": "json"}."""
    if isinstance(f, (str, os.PathLike)):
        s = os.fspath(f)
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
        doc = pandom.loads(doc_json)
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
        return pandom.dumps(doc).encode()

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
    ``pandoc_version()`` etc. instead.
    """
    argv = tuple(os.fsencode(a) if isinstance(a, os.PathLike) else a.encode() for a in args)
    return _check(*_core.convert_args(argv, _encode(input)))


def read(source: Source = None, from_: str | Conversion = "markdown", /,
         options: Mapping[str, Any] | None = None, **kwargs: Any) -> Pandoc:
    """Parse ``source`` into a document (``pandom.Pandoc``).

    ``from_`` is a format, or, in a filter, the conversion the filter runs
    in (``ctx.conversion``): then ``source`` is read the way that
    conversion reads its input, as with ``read_many``.

    Otherwise other options (``input_files``, reader extensions in
    ``from_``, Lua ``filters``, ...) apply as in ``convert``.
    """
    if isinstance(from_, Conversion):
        if not isinstance(source, (str, bytes)):
            raise TypeError("read(source, conversion) takes the text to read")
        text = source.decode("utf-8") if isinstance(source, bytes) else source
        return read_many([text], from_, options, **kwargs)[0]
    opts = _options(options, kwargs)
    opts.update({"from": from_, "to": "json"})
    opts.pop("output-file", None)
    out = _check(*_core.convert(_dumps(opts), _encode(source)))
    return Pandoc.from_json(json.loads(out))


def read_many(sources: Iterable[str], from_: str | Conversion = "markdown", /,
              options: Mapping[str, Any] | None = None, **kwargs: Any) -> list[Pandoc]:
    """Parse many texts, each on its own, in parallel: their documents.

    For filters that parse many fragments, such as table cells: one call
    into pandoc, which sets up the reader once and reads the texts on all
    cores, much cheaper than a ``read`` each. ``from_`` is a format, or the
    conversion a filter runs in (``ctx.conversion``), to read the way it
    reads its input. Options that affect reading apply (``tab_stop``,
    ``abbreviations``, ``resource_path``, ...).

        docs = libpandoc.read_many(cells, ctx.conversion)
    """
    texts = [s if isinstance(s, str) else s.decode("utf-8") for s in sources]
    opts = read_options(from_) if isinstance(from_, Conversion) else {"from": from_}
    opts.update(_options(options, kwargs))
    request = json.dumps({"options": opts, "inputs": texts}, ensure_ascii=False)
    out = json.loads(_check(*_core.read_many(request.encode("utf-8"))))
    docs = []
    for i, j in enumerate(out):
        if "error" in j:
            e = j["error"]
            raise PandocError(e["kind"], f"reading input {i}: {e['message']}")
        docs.append(Pandoc.from_json(j))
    return docs


# The conversion's options that also apply to reading a fragment of it.
_READ_OPTIONS = frozenset({
    "abbreviations", "data-dir", "default-image-extension", "indented-code-classes",
    "preserve-tabs", "resource-path", "sandbox", "strip-comments", "tab-stop",
    "track-changes",
})

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
        options=options,
    )


def write(doc: Pandoc, to: str = "html", /,
          options: Mapping[str, Any] | None = None, **kwargs: Any) -> str | bytes:
    """Render a typed document, as ``convert`` from JSON would."""
    opts = _options(options, kwargs)
    opts.update({"from": "json", "to": to})
    return convert(pandom.dumps(doc).encode(), opts)


def _check_versions() -> None:
    if tuple(pandom.PANDOC_API_VERSION[:2]) != pandoc_api_version()[:2]:
        raise ImportError(
            f"pandom is for pandoc API {pandom.PANDOC_API_VERSION}, "
            f"but the loaded pandoc library speaks {pandoc_api_version()}"
        )


_check_versions()
