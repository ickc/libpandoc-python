"""pandoc as a Python library, in process, through libpandoc's C ABI.

    >>> import libpandoc as pandoc
    >>> pandoc.convert("*hi*", from_="markdown", to="html")
    '<p><em>hi</em></p>\\n'
    >>> pandoc.run(["-f", "markdown", "-t", "latex"], input="*hi*")
    b'\\\\emph{hi}\\n'
    >>> doc = pandoc.read("*hi*")            # the AST, libpandoc.ast
    >>> pandoc.write(doc, to="rst")
    '*hi*\\n'

Options are pandoc's defaults-file keys (https://pandoc.org/MANUAL.html#defaults-files),
given as keyword arguments with ``_`` for ``-`` (``reference_doc=``,
``from_=`` for ``from``) or as a dict. Warnings pandoc reports are issued as
``PandocWarning``; failures raise ``PandocError``.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import warnings
from collections.abc import Mapping, Sequence
from typing import Any

from . import _core, ast
from .ast import Filter, Pandoc

__all__ = [
    "PandocError",
    "PandocWarning",
    "ast",
    "convert",
    "default_template",
    "extensions",
    "input_formats",
    "output_formats",
    "pandoc_api_version",
    "pandoc_version",
    "query",
    "read",
    "run",
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
    /,
    **kwargs: Any,
) -> str | bytes:
    """Convert ``source`` as ``pandoc`` would with these defaults-file options.

    ``source`` is standard input (str or bytes); None means the options'
    ``input_files``. Returns the output as ``str`` for text formats and
    ``bytes`` for binary ones (docx, pdf, ...); if ``output_file`` is set the
    output is written there and ``""`` is returned.

    ``filters`` may mix pandoc's (Lua or JSON filter paths) with Python ones:
    ``libpandoc.ast.Filter``s, or functions that take a ``Pandoc`` and change
    it or return a new one. They run in order, as ``--filter`` would.

        convert("# Hi", from_="markdown", to="docx")
        convert(options={"input-files": ["a.md"], "output-file": "a.pdf"})
    """
    opts = _options(options, kwargs)
    if any(_is_python_filter(f) for f in opts.get("filters") or ()):
        out = _convert_with_python_filters(_encode(source), opts)
    else:
        out = _check(*_core.convert(_dumps(opts), _encode(source)))
    if "output-file" in opts:
        return ""
    return out.decode("utf-8") if _is_text_format(opts.get("to", "html")) else out


def _is_python_filter(f: Any) -> bool:
    return isinstance(f, Filter) or callable(f)


# Options that act while reading, or before filters: given to the first pass
# only, so that the later passes, which read pandoc's JSON, don't repeat them
# (and don't undo what a filter changed, as --metadata would).
_READING = frozenset({
    "from", "reader", "input-file", "input-files", "file-scope",
    "shift-heading-level-by", "base-header-level", "abbreviations",
    "default-image-extension", "indented-code-classes", "track-changes",
    "strip-comments", "tab-stop", "preserve-tabs", "extract-media",
    "metadata", "metadata-file", "metadata-files", "bibliography", "csl",
    "citation-abbreviations",
})


def _convert_with_python_filters(source: bytes | None, opts: dict[str, Any]) -> bytes:
    """Run a conversion whose filters include Python ones.

    pandoc can't call back into Python mid-conversion, so this runs as
    ``pandoc -t json | filter | pandoc -f json`` would, in process: the first
    pass reads (with the filters before the first Python one), each Python
    filter runs on the document, pandoc filters between them run on JSON, and
    the last pass writes (with the filters after the last Python one, then
    citeproc). One difference from a single pandoc run: resources pandoc
    keeps in memory, such as images embedded in a docx, don't survive the
    passes; set ``extract_media`` to keep them.
    """
    filters = list(opts.get("filters") or ())
    groups: list[tuple[bool, list[Any]]] = []
    for f in filters:
        python = _is_python_filter(f)
        if groups and groups[-1][0] == python:
            groups[-1][1].append(f)
        else:
            groups.append((python, [f]))
    later = {k: v for k, v in opts.items() if k not in _READING and k != "filters"}
    later.pop("citeproc", None)
    writing = {**later, "from": "json", "citeproc": opts.get("citeproc", False)}
    fmt = opts.get("to")
    fmt = fmt.split("+")[0].split("-")[0] if isinstance(fmt, str) else None

    first = {k: v for k, v in opts.items() if k not in ("output-file", "citeproc")}
    first.update({"to": "json", "filters": groups.pop(0)[1] if not groups[0][0] else []})
    data = _check(*_core.convert(_dumps(first), source))
    last_pandoc = groups.pop()[1] if not groups[-1][0] else []
    doc = None
    for python, group in groups:
        if python:
            doc = Pandoc.from_json(json.loads(data)) if doc is None else doc
            for f in group:
                if isinstance(f, Filter):
                    doc = f(doc, fmt)
                else:
                    result = f(doc)
                    doc = doc if result is None else result
        else:
            middle = {**later, "from": "json", "to": "json", "filters": group}
            middle.pop("output-file", None)
            data = _check(*_core.convert(_dumps(middle), ast.dumps(doc).encode()))
            doc = None
    if doc is not None:
        data = ast.dumps(doc).encode()
    return _check(*_core.convert(_dumps({**writing, "filters": last_pandoc}), data))


def run(args: Sequence[str], input: Source = None) -> bytes:
    """Run pandoc with command-line arguments, as ``pandoc ARGS``.

    ``input`` is standard input; the result is what pandoc writes to standard
    output. Arguments are exactly the CLI's (``--defaults`` included), except
    that informational options such as ``--version`` are rejected: use
    ``pandoc_version()`` etc. instead.
    """
    argv = tuple(os.fsencode(a) if isinstance(a, os.PathLike) else a.encode() for a in args)
    return _check(*_core.convert_args(argv, _encode(input)))


def read(source: Source = None, from_: str = "markdown", /,
         options: Mapping[str, Any] | None = None, **kwargs: Any) -> Pandoc:
    """Parse ``source`` into a document (``libpandoc.ast.Pandoc``).

    Other options (``input_files``, reader extensions in ``from_``, Lua
    ``filters``, ...) apply as in ``convert``.
    """
    opts = _options(options, kwargs)
    opts.update({"from": from_, "to": "json"})
    opts.pop("output-file", None)
    out = _check(*_core.convert(_dumps(opts), _encode(source)))
    return Pandoc.from_json(json.loads(out))


def write(doc: Pandoc, to: str = "html", /,
          options: Mapping[str, Any] | None = None, **kwargs: Any) -> str | bytes:
    """Render a typed document, as ``convert`` from JSON would."""
    opts = _options(options, kwargs)
    opts.update({"from": "json", "to": to})
    return convert(ast.dumps(doc).encode(), opts)


def _check_versions() -> None:
    if tuple(ast.PANDOC_API_VERSION[:2]) != pandoc_api_version()[:2]:
        raise ImportError(
            f"libpandoc-ast is for pandoc API {ast.PANDOC_API_VERSION}, "
            f"but the loaded pandoc library speaks {pandoc_api_version()}"
        )


_check_versions()
