"""The include step built into libpandoc (1.15, panclude): "panclude" among
the filters and its roots, natively, under pandoc's sandbox, for untrusted
options and in the directory sandbox; wasm filters given no directory after
it, which see the files' content, while their own calls to pandoc read
nothing; and pandocpy's -F panclude, never a filter of that name."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import textwrap
import threading
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

import libpandoc as pandoc
from libpandoc import cli

WASM = os.environ.get("LIBPANDOC_WASM_FILTERS", "")
needs_filters = pytest.mark.skipif(
    not WASM, reason="set LIBPANDOC_WASM_FILTERS to libpandoc-rs's filters built for wasm32-wasip1")
needs_sandbox = pytest.mark.skipif(
    sys.platform == "emscripten" or not os.path.isfile(os.environ.get("LIBPANDOC_WASM", "")),
    reason="set LIBPANDOC_WASM to libpandoc.wasm")
unix = pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need a privilege")

# a file inside the document's directory, one outside it, and (on Unix) a
# link inside it that leads out
DOC = textwrap.dedent("""\
    ```{include="part.txt"}
    OWN-PART
    ```

    ```{include="../secret.txt"}
    OWN-OUT
    ```

    ```{include="link.txt"}
    OWN-LINK
    ```
    """)
NO_ROOTS = "there are no roots, so no file is included"


@pytest.fixture
def tree(tmp_path):
    """`doc/` (doc.md, part.txt, a link to the canary), `other/`, and the
    canary `secret.txt` beside them; the real path (macOS's /var)."""
    base = Path(os.path.realpath(tmp_path))
    (base / "doc").mkdir()
    (base / "other").mkdir()
    (base / "doc" / "doc.md").write_text(DOC)
    (base / "doc" / "part.txt").write_text("hello *from the part*\n")
    (base / "other" / "other.txt").write_text("OTHER-OK\n")
    (base / "secret.txt").write_text("CANARY-PANCLUDE\n")
    if sys.platform != "win32":
        (base / "doc" / "link.txt").symlink_to("../secret.txt")
    return base


def named(p: Path | str) -> str:
    """A path as the directory sandbox's module names it: on Windows `C:\\x`
    is `/c/x`."""
    s = str(p).replace("\\", "/").removeprefix("//?/")
    if sys.platform == "win32" and s[1:2] == ":":
        return f"/{s[0].lower()}{s[2:]}"
    return s


@contextlib.contextmanager
def recorded() -> Iterator[list[str]]:
    """pandoc's warnings, as PandocWarning messages."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        said: list[str] = []
        yield said
    said += [str(x.message) for x in w if issubclass(x.category, pandoc.PandocWarning)]


def converted(tree: Path, filters: list, **options) -> tuple[str, list[str]]:
    """doc/doc.md to plain text: the output and pandoc's warnings; never
    the canary."""
    options.setdefault("input_files", [tree / "doc" / "doc.md"])
    with recorded() as said:
        out = pandoc.convert(None, to="plain", filters=filters, **options)
    assert "CANARY" not in out and "CANARY" not in " ".join(said), out
    return out, said


def cannot(path: str, file: Path | str, reason: str) -> str:
    """The warning for an include that failed, as pandoc's log gives it."""
    return f'[ScriptingWarning] Scripting warning: panclude: cannot include "{path}" in {file}: {reason}'


def test_by_default_inside_the_documents_directory(tree):
    doc = tree / "doc" / "doc.md"
    out, said = converted(tree, ["panclude"])
    assert "hello *from the part*" in out and "OWN-OUT" in out
    assert cannot("../secret.txt", doc, "outside the roots") in said
    if sys.platform != "win32":
        assert "OWN-LINK" in out
        assert cannot("link.txt", doc, "a symbolic link leads outside the roots") in said
    # off unless named
    out, said = converted(tree, [])
    assert "OWN-PART" in out and "hello" not in out and said == []


def test_roots_given(tree):
    doc = tree / "doc" / "doc.md"
    other = tree / "other"
    out, said = converted(tree, [{"type": "panclude", "roots": [other]}])
    assert "OWN-PART" in out and "hello" not in out
    assert cannot("part.txt", doc, "outside the roots") in said
    out, _ = converted(tree, [{"type": "panclude", "roots": [str(tree / "doc"), other]}])
    assert "hello *from the part*" in out
    out, said = converted(tree, [{"type": "panclude", "roots": []}])
    assert "OWN-PART" in out and "hello" not in out and cannot("part.txt", doc, NO_ROOTS) in said
    # a root that isn't a directory, a field it hasn't: errors
    for bad in ({"type": "panclude", "roots": [tree / "secret.txt"]},
                {"type": "panclude", "root": [other]}):
        with pytest.raises(pandoc.PandocError) as e:
            converted(tree, [bad])
        assert e.value.kind == "PandocOptionError"


def test_under_pandocs_sandbox_only_roots_named(tree):
    doc = tree / "doc" / "doc.md"
    out, said = converted(tree, ["panclude"], sandbox=True)
    assert "OWN-PART" in out and "hello" not in out and cannot("part.txt", doc, NO_ROOTS) in said
    out, said = converted(tree, [{"type": "panclude", "roots": [tree / "doc"]}], sandbox=True)
    assert "hello *from the part*" in out
    assert cannot("../secret.txt", doc, "outside the roots") in said


def test_it_ends_a_run(tree):
    """One of pandoc's own entries: a Python filter before it sees the
    element as the reader made it, one after it the file's text."""
    seen = []

    def look(doc):
        seen.append("from the part" in json.dumps(doc.to_json()))

    converted(tree, [look, "panclude", look, look])
    assert seen == [False, True, True]


def test_untrusted_options_read_only_the_hosts_roots(tree):
    """Untrusted options may name the step, which reads only inside the
    roots the host gives (inside "untrusted"), on any thread, with a cancel
    token too; without them nothing; their own roots are refused."""
    part = (tree / "doc" / "part.txt").as_posix()
    text = f'```{{include="{part}"}}\nOWN-PART\n```\n'
    roots = [str(tree / "doc")]

    def untrusted(host, **kw):
        with recorded() as said:
            out = pandoc.convert(text, to="plain", filters=["panclude"], untrusted=host, **kw)
        return out, said

    out, said = untrusted(True)
    assert "OWN-PART" in out and "hello" not in out and said[0].endswith(NO_ROOTS)
    # the host's roots: on the main thread (whose calls get a token of their
    # own for Ctrl-C), with a token given, and on another thread
    host = {"panclude-roots": roots}
    assert "hello *from the part*" in untrusted(host)[0]
    assert "hello *from the part*" in untrusted(host, cancel=pandoc.CancelToken())[0]
    got = []
    t = threading.Thread(target=lambda: got.append(untrusted(host)[0]))
    t.start()
    t.join()
    assert "hello *from the part*" in got[0]
    # the code's own roots: refused
    for theirs in ({"panclude_roots": roots}, {"filters": [{"type": "panclude", "roots": roots}]},
                   {"filters": ["panclude", "x.lua"]}):
        opts = {"filters": ["panclude"], **theirs}
        with pytest.raises(pandoc.PandocError) as e:
            pandoc.convert(text, to="plain", untrusted=True, **opts)
        assert e.value.kind == "PandocOptionError"
    # citeproc may be named too (libpandoc 1.15)
    assert pandoc.convert("x", to="plain", filters=["citeproc"], untrusted=True) == "x\n"


def test_a_file_named_panclude_is_not_run(tree, monkeypatch):
    """"panclude" is the step, also when the working directory has a Python
    script, or a program, of that name."""
    monkeypatch.chdir(tree / "doc")
    script = tree / "doc" / "panclude"
    script.write_text("#!/usr/bin/env python3\nopen('ran.txt', 'w').write('RAN')\n"
                      "import sys; sys.stdout.write(sys.stdin.read())\n")
    script.chmod(0o755)
    for filters in (["panclude"], [{"type": "json", "path": "panclude"}]):
        out, _ = converted(tree, filters, input_files=["doc.md"])
        assert "hello *from the part*" in out
        assert not (tree / "doc" / "ran.txt").exists()


# -- pandocpy -----------------------------------------------------------------------


def pandocpy(cwd: Path, args: list[str], env: dict | None = None,
             site: Path | None = None) -> tuple[int, str, str]:
    e = {k: v for k, v in os.environ.items()
         if k not in ("LIBPANDOC_SANDBOX", "LIBPANDOC_SANDBOX_WRITE", "LIBPANDOC_WASM_DIRS")}
    if site is not None:
        e["PYTHONPATH"] = os.pathsep.join([str(site), *sys.path])
    e.update(env or {})
    r = subprocess.run([sys.executable, "-m", "libpandoc", *args], capture_output=True, cwd=cwd,
                       env=e, timeout=300, check=False, stdin=subprocess.DEVNULL)
    text = lambda b: b.decode("utf-8", "replace").replace("\r\n", "\n")
    return r.returncode, text(r.stdout), text(r.stderr)


@pytest.fixture
def site(tmp_path):
    """A Python filter installed as the panir.filters entry point "panclude",
    which must never run."""
    site = tmp_path / "site"
    info = site / "panclude_filter-0.1.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: panclude-filter\nVersion: 0.1\n")
    (info / "entry_points.txt").write_text("[panir.filters]\npanclude = panclude_filter:f\n")
    (site / "panclude_filter.py").write_text("def f(doc):\n    raise RuntimeError('RAN-INSTALLED')\n")
    return site


def test_pandocpy_panclude_is_the_step(tree, site):
    """-F panclude is the step, not an installed filter of that name nor a
    Python script in the working directory; under --sandbox it has no
    roots."""
    work = tree / "doc"
    (work / "panclude").write_text("open('ran.txt', 'w').write('RAN')\n")
    outside = 'panclude: cannot include "../secret.txt" in doc.md: outside the roots'
    status, out, err = pandocpy(work, ["doc.md", "-t", "plain", "-F", "panclude"], site=site)
    assert status == 0, err
    assert "hello *from the part*" in out and outside in err
    status, out, err = pandocpy(work, ["doc.md", "-t", "plain", "--sandbox", "-F", "panclude"],
                                site=site)
    assert status == 0 and "OWN-PART" in out and "hello" not in out and NO_ROOTS in err, err
    assert not (work / "ran.txt").exists()
    assert "RAN-INSTALLED" not in err and "CANARY" not in out + err
    # not listed among the installed filters either
    assert "panclude" not in cli.installed_filters()


# -- wasm filters ---------------------------------------------------------------------


def wasm(name: str) -> str:
    return str(Path(WASM) / f"{name}.wasm")


@needs_filters
def test_a_wasm_filter_given_no_directory_sees_the_content(tree, monkeypatch):
    """The step first, then wasm filters given no directory: parse reads the
    code block's text as the document was read, upper upper-cases it. They
    see the file's text, after the block's own; without the step, never."""
    pytest.importorskip("wasmtime")
    monkeypatch.delenv("LIBPANDOC_WASM_DIRS", raising=False)
    (tree / "doc" / "doc.md").write_text('```{.parse include="part.txt"}\nbefore the part:\n```\n')
    after = [wasm("parse"), wasm("upper")]
    assert converted(tree, ["panclude", *after])[0] == "BEFORE THE PART: HELLO FROM THE PART\n"
    assert converted(tree, after)[0] == "BEFORE THE PART:\n"
    roots = [{"type": "panclude", "roots": [tree / "doc"]}]
    assert converted(tree, roots + after, sandbox=True)[0] == "BEFORE THE PART: HELLO FROM THE PART\n"
    # pandocpy, the same
    status, out, err = pandocpy(tree / "doc", ["doc.md", "-t", "plain", "-F", "panclude",
                                               "-F", wasm("parse"), "-F", wasm("upper")])
    assert (status, out) == (0, "BEFORE THE PART: HELLO FROM THE PART\n"), err


@needs_filters
def test_a_wasm_filters_calls_read_nothing(tree):
    """A wasm filter's own calls are untrusted, with no roots from the host:
    a call naming the step reads nothing, whatever it writes; roots of its
    own are refused. Natively and in the directory sandbox, with the
    conversion's own step given a root holding the canary."""
    pytest.importorskip("wasmtime")
    doc = tree / "doc" / "doc.md"
    runs = [False]
    if os.path.isfile(os.environ.get("LIBPANDOC_WASM", "")) and sys.platform != "emscripten":
        runs.append(True)
    for sandboxed in runs:
        # (paths natively, and as the module sees them)
        path = named if sandboxed else (lambda p: Path(p).as_posix())
        secret, root = path(tree / "secret.txt"), path(tree)
        part = f'```{{include="{secret}"}}\nOWN\n```\n'
        requests = [
            {"convert": [{"to": "plain", "filters": ["panclude"]}, part]},
            {"convert": [{"to": "plain", "filters": ["panclude"],
                          "untrusted": {"panclude-roots": [root]}}, part]},
            {"convert": [{"to": "plain", "filters": ["panclude"], "untrusted": False}, part]},
            {"convert": [{"to": "plain", "filters": ["panclude"], "panclude-roots": [root]}, part]},
            {"convert": [{"to": "plain", "filters": [{"type": "panclude", "roots": [root]}]}, part]},
        ]
        doc.write_text("\n\n".join(f"```call\n{json.dumps(r)}\n```" for r in requests))
        options = {"input_files": [path(doc)]}
        if sandboxed:
            options["sandbox"] = {"read": [str(tree)]}
        # the conversion's own step has a root holding the canary
        step = {"type": "panclude", "roots": [root]}
        out = pandoc.convert(None, to="json", filters=[step, wasm("calls")], **options)
        assert "CANARY" not in out
        answers = [json.loads(b["c"][1]) for b in json.loads(out)["blocks"]]
        for a in answers[:3]:
            assert a["ok"].strip() == "OWN", (options, a)
        for a in answers[3:]:
            assert a["error"][0] == "PandocOptionError", (options, a)


# -- the directory sandbox ---------------------------------------------------------------


@needs_sandbox
def test_in_the_directory_sandbox_nothing_outside_the_mounts(tree):
    """The step reads inside its roots as the module sees them (the same
    paths on Unix; /c/... on Windows), and nothing outside the mounted
    directories; a root wider than them is no directory there: an error."""
    pytest.importorskip("wasmtime")
    doc = tree / "doc"
    mounts = {"read": [str(doc)]}
    secret = named(tree / "secret.txt")
    (doc / "doc.md").write_text(f'```{{include="{secret}"}}\nOWN-ABS\n```\n\n{DOC}')
    file = named(doc / "doc.md")
    for filters in (["panclude"], [{"type": "panclude", "roots": [named(doc)]}]):
        out, said = converted(tree, filters, input_files=[file], sandbox=mounts)
        assert "hello *from the part*" in out and "OWN-ABS" in out and "OWN-OUT" in out
        assert cannot("../secret.txt", file, "outside the roots") in said
        if sys.platform != "win32":
            assert "OWN-LINK" in out
    with pytest.raises(pandoc.PandocError) as e:
        converted(tree, [{"type": "panclude", "roots": [named(tree)]}], input_files=[file],
                  sandbox=mounts)
    assert e.value.kind == "PandocOptionError" and "is not a directory" in e.value.message
    # untrusted, with the host's roots as the module names them
    text = f'```{{include="{named(doc / "part.txt")}"}}\nOWN-PART\n```\n'
    out = pandoc.convert(text, to="plain", filters=["panclude"], sandbox=mounts,
                         untrusted={"panclude-roots": [named(doc)]})
    assert "hello *from the part*" in out
    with recorded():
        out = pandoc.convert(text, to="plain", filters=["panclude"], sandbox=mounts, untrusted=True)
    assert "OWN-PART" in out and "hello" not in out
    # pandocpy, the working directory mounted
    status, out, err = pandocpy(doc, ["doc.md", "-t", "plain", "-F", "panclude"],
                                env={"LIBPANDOC_SANDBOX": ".", "LIBPANDOC_WASM_TIMEOUT": "120"})
    assert status == 0 and "hello *from the part*" in out and "CANARY" not in out + err, err
