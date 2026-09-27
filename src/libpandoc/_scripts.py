"""pandocpy: Python filter scripts, run in pandocpy's own process.

A filter named with ``-F`` that is a Python script (``.py``, or a Python
``#!`` line) runs in pandocpy's process instead of a new one: as its own
``__main__``, with the document as its standard input, the output format as
its argument, pandoc's environment variables, and its standard output as the
result. That serves any framework (panflute, pandocfilters, pandom, plain
JSON) exactly as its own code runs, minus starting a Python process and
importing it all again. A pandom script's ``f.main()`` hands its ``Filter``
over instead, which then runs on the document directly, knowing the whole
conversion.

Either way it runs with pandocpy's own Python and its packages, rather than
whichever ``python`` is first on ``PATH`` (as pandoc would). Not in process,
a script runs as a subprocess:

- when its author says so, with a line ``# pandocpy: subprocess`` near its
  top (for scripts that need a process of their own);
- when the user says so: ``PANDOCPY_SUBPROCESS=foo.py,bar`` (paths, file
  names or names without ``.py``; ``*`` for all);
- when it fails in process: it is run again as a subprocess, on the same
  document, with a warning.

This is for pandocpy, which runs one conversion: running a script swaps the
process's standard streams, ``sys.argv`` and environment while it runs.
"""

from __future__ import annotations

import io
import json
import os
import re
import runpy
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pandom
import pandom.filter

ENV = "PANDOCPY_SUBPROCESS"
_MARKER = re.compile(r"^#\s*pandocpy:\s*subprocess\b", re.MULTILINE)
_HEAD = 4096  # the marker is looked for in the first bytes of the script


def is_python_script(path: str) -> bool:
    """A file ending in ``.py``, or with a Python ``#!`` line."""
    p = Path(path)
    if not p.is_file():
        return False
    if p.suffix.lower() == ".py":
        return True
    try:
        with p.open("rb") as f:
            first = f.readline(256)
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first


def opted_out(path: str, env: Mapping[str, str] | None = None) -> str | None:
    """Why this script runs as a subprocess, if it does."""
    env = os.environ if env is None else env
    names = {n.strip() for n in env.get(ENV, "").split(",") if n.strip()}
    p = Path(path)
    if names & {"*", path, p.name, p.stem}:
        return f"${ENV}"
    try:
        with p.open("rb") as f:
            head = f.read(_HEAD).decode("utf-8", "replace")
    except OSError:
        return None
    if _MARKER.search(head):
        return "# pandocpy: subprocess"
    return None


class ScriptFailed(Exception):
    pass


def callback(
    path: str, pandoc_version: str, conversion: Callable[..., Any], *, in_process: bool = True
) -> Any:
    """A libpandoc callback running the script ``path`` in this process
    (falling back to a subprocess), or as a subprocess; with this Python
    either way, rather than whichever ``python`` is on ``PATH``."""

    def run(doc: bytes, context: bytes) -> bytes:
        ctx = json.loads(context)
        env = {
            "PANDOC_VERSION": pandoc_version,
            "PANDOC_READER_OPTIONS": json.dumps(ctx.get("reader-options") or {}),
            "PANDOC_INPUT_FORMAT": ctx.get("input-format") or "",
            "PANDOC_OUTPUT_FORMAT": ctx.get("output-format") or "",
        }
        fmt = ctx.get("format") or ""
        if not in_process:
            return subprocess_run(path, doc, fmt, env)
        try:
            return _in_process(path, doc, fmt, env, lambda f: _run_filter(f, doc, ctx, conversion))
        except Exception as e:  # noqa: BLE001  whatever the script raised
            print(
                f"[WARNING] {path} failed in pandocpy's process ({type(e).__name__}: {e}); "
                f"running it as a subprocess. If it needs a process of its own, add the "
                f"line '# pandocpy: subprocess' to it, or set {ENV}={Path(path).name}.",
                file=sys.stderr,
            )
            return subprocess_run(path, doc, fmt, env)

    return run


def _run_filter(f: pandom.Filter, doc: bytes, ctx: Mapping[str, Any], conversion: Any) -> bytes:
    """A pandom script's Filter, handed over: run on the document directly."""
    out = f(pandom.loads(doc), conversion=conversion(ctx, None))
    return pandom.dumps(out).encode()


def _in_process(
    path: str,
    doc: bytes,
    fmt: str,
    env: Mapping[str, str],
    handed: Callable[[pandom.Filter], bytes],
) -> bytes:
    filters: list[pandom.Filter] = []
    out = io.BytesIO()
    saved = (sys.stdin, sys.stdout, sys.argv, {k: os.environ.get(k) for k in env})
    sys.stdin = io.TextIOWrapper(io.BytesIO(doc), encoding="utf-8")
    sys.stdout = io.TextIOWrapper(out, encoding="utf-8", write_through=True)
    sys.argv = [path, fmt]
    os.environ.update(env)
    pandom.filter.handoff = filters.append
    try:
        try:
            runpy.run_path(path, run_name="__main__")
        except SystemExit as e:
            if e.code not in (None, 0):
                raise ScriptFailed(f"it exited with {e.code}") from None
        try:
            sys.stdout.flush()
        except ValueError:
            pass  # the script closed or detached it
        result = out.getvalue()
    finally:
        sys.stdin, sys.stdout, sys.argv, old = saved
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        pandom.filter.handoff = None
    if filters:
        return handed(filters[-1])
    try:
        json.loads(result)
    except ValueError:
        raise ScriptFailed("its output isn't a document (pandoc's JSON)") from None
    return result


def subprocess_run(path: str, doc: bytes, fmt: str, env: Mapping[str, str]) -> bytes:
    """As pandoc runs a Python JSON filter (with this Python)."""
    proc = subprocess.run(
        [sys.executable, path, fmt],
        input=doc,
        capture_output=True,
        env={**os.environ, **env},
        check=False,
    )
    sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
    if proc.returncode != 0:
        raise RuntimeError(f"{path} returned error status {proc.returncode}")
    return proc.stdout
