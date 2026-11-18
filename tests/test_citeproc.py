"""citeproc's files given as their contents (``citeproc_files=``,
libpandoc 1.14's ``"citeproc-files"``): for a sandboxed conversion, from
Python, and from a wasm filter, whose calls to pandoc read no file:
libpandoc-rs's example filter ``citeproc`` ($LIBPANDOC_WASM_FILTERS) reads
the files the metadata names itself, in the directories it is given, and
hands them to pandoc. A canary bibliography outside them, named by the
metadata, never shows. libpandoc-rs's libpandoc/tests/citeproc.rs and
pandocrs/tests/cli.rs, ported.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

import libpandoc as pandoc
from libpandoc._wasmfilter import WasmFilterError

FILTERS = os.environ.get("LIBPANDOC_WASM_FILTERS", "")
needs_filters = pytest.mark.skipif(
    sys.platform == "emscripten" or not FILTERS or importlib.util.find_spec("wasmtime") is None,
    reason="wasm filters: wasmtime, and LIBPANDOC_WASM_FILTERS set to libpandoc-rs's filters")
WASM = os.environ.get("LIBPANDOC_WASM", "")
needs_wasm = pytest.mark.skipif(
    sys.platform == "emscripten" or not os.path.isfile(WASM)
    or importlib.util.find_spec("wasmtime") is None,
    reason="the directory sandbox: wasmtime, and LIBPANDOC_WASM set to libpandoc.wasm")

BIB = ("@article{key, title={BIB-OK}, journal={Journal of Long Names}, "
       "author={Author, A.}, year={2000}}\n")
# A style of its own: "(STYLE-OK title in journal)", the journal's short
# form (from the abbreviations).
STYLE = """<?xml version="1.0" encoding="utf-8"?>
<style xmlns="http://purl.org/net/xbiblio/csl" class="in-text" version="1.0">
  <info><title>Test</title><id>test</id><updated>2026-01-01T00:00:00+00:00</updated></info>
  <citation><layout prefix="(" suffix=")"><text value="STYLE-OK "/><text variable="title"/><text value=" in "/><text variable="container-title" form="short"/></layout></citation>
  <bibliography><layout><text variable="title"/></layout></bibliography>
</style>
"""
ABBR = '{"default": {"container-title": {"Journal of Long Names": "ABBR-OK"}}}'
CANARY = "CANARY-CITEPROC-PY-5c1e"
OKS = ("STYLE-OK", "BIB-OK", "ABBR-OK")


def named(p: Path) -> str:
    """An absolute path as YAML metadata names it for a wasm filter (with
    `/`; on Windows `C:\\x` is `/c/x`)."""
    s = os.fspath(p).replace("\\", "/")
    if sys.platform == "win32" and s[1:2] == ":":
        s = f"/{s[0].lower()}{s[2:]}"
    return s


def doc(**meta: str) -> str:
    """A document citing `key`, with this metadata."""
    lines = "".join(f"{k.replace('_', '-')}: '{v}'\n" for k, v in meta.items())
    return f"---\n{lines}---\n\nSee [@key].\n"


@pytest.fixture
def lab(tmp_path, monkeypatch) -> Path:
    """`files` (a bibliography, a CSL style and abbreviations, and the
    working directory) and `secret` beside it (a canary bibliography);
    LIBPANDOC_WASM_DIRS unset."""
    root = Path(os.path.realpath(tmp_path))  # (macOS's /var is /private/var)
    for d in ("files", "secret"):
        (root / d).mkdir()
    canary = f"@book{{key, title={{{CANARY}}}, author={{B}}, year={{2001}}}}\n"
    for f, text in (("files/refs.bib", BIB), ("files/style.csl", STYLE), ("files/abbr.json", ABBR),
                    ("secret/canary.bib", canary)):
        (root / f).write_text(text)
    monkeypatch.chdir(root / "files")
    monkeypatch.delenv("LIBPANDOC_WASM_DIRS", raising=False)
    return root


def test_the_files_given_as_their_contents():
    """For a sandboxed conversion (pandoc's sandbox, or untrusted options):
    nothing is read from the disk for them; without the sandbox the option
    is refused (citeproc would read the disk instead)."""
    files = {"refs.bib": BIB, "style.csl": STYLE, "abbr.json": ABBR}
    text = doc(bibliography="refs.bib", csl="style", citation_abbreviations="abbr")
    for opts in ({"sandbox": True}, {"untrusted": True}):
        out = pandoc.convert(text, to="plain", citeproc=True, citeproc_files=files, **opts)
        assert all(ok in out for ok in OKS), (opts, out)
    with pytest.raises(pandoc.PandocError) as e:
        pandoc.convert(text, to="plain", citeproc=True, citeproc_files=files)
    assert e.value.kind == "PandocOptionError", e.value


def citeproc(**kwargs) -> pandoc.WasmFilter:
    return pandoc.WasmFilter(Path(FILTERS) / "citeproc.wasm", **kwargs)


# The conversions it runs in: plain, in pandoc's sandbox, and in the
# directory sandbox (listing the canary's directory: pandoc could read it
# there, the filter can't).
CONVERSIONS = [
    pytest.param({}, id="plain"),
    pytest.param({"sandbox": True}, id="sandbox"),
    pytest.param({"sandbox": "secret"}, id="directory-sandbox", marks=needs_wasm),
]


def run(f: pandoc.WasmFilter, options: dict, text: str, lab: Path) -> tuple[str | None, str | None]:
    """(output, error) of a conversion to plain text with the filter."""
    if options.get("sandbox") == "secret":
        options = {**options, "sandbox": {"read": [str(lab / "secret")], "timeout": 120}}
    try:
        return pandoc.convert(text, to="plain", filters=[f], **options), None
    except WasmFilterError as e:  # (its own error)
        return None, str(e)


@needs_filters
@pytest.mark.parametrize("options", CONVERSIONS)
def test_a_wasm_filter_reads_them_itself(lab, options):
    files = lab / "files"
    # by relative names, `csl: style` looked up as `style.csl`
    relative = doc(bibliography="refs.bib", csl="style", citation_abbreviations="abbr")
    # by absolute names, given as the variable gives them
    absolute = doc(bibliography=named(files / "refs.bib"), csl=named(files / "style.csl"),
                   citation_abbreviations=named(files / "abbr.json"))
    for f, text in ((citeproc(preopens=[(files, ".", False)]), relative),
                    (citeproc(dirs=[files]), absolute), (citeproc(dirs=["."]), relative)):
        out, err = run(f, options, text, lab)
        assert out is not None and all(ok in out for ok in OKS), (out, err)


@needs_filters
@pytest.mark.parametrize("options", CONVERSIONS)
def test_without_its_directory_they_are_missing(lab, options, capfd):
    _, err = run(citeproc(dirs=[]), options, doc(bibliography=named(lab / "files/refs.bib")), lab)
    assert err is not None and err.endswith("exited with status 1 (it was given no directory)"), err
    assert "not found" in capfd.readouterr().err  # (pandoc's error, the filter's standard error)


@needs_filters
@pytest.mark.parametrize("options", CONVERSIONS)
def test_the_canary_never_shows(lab, options, capfd):
    """The canary, outside the filter's directory, named by the metadata
    (absolute, and relative through `..`): never in the output, the error,
    or what the filter writes to its standard error."""
    files = lab / "files"
    for text in (doc(bibliography=named(lab / "secret/canary.bib")),
                 doc(bibliography="../secret/canary.bib")):
        for f in (citeproc(dirs=[files]), citeproc(preopens=[(files, ".", False)])):
            out, err = run(f, options, text, lab)
            assert out is None and CANARY not in err, (out, err)
    assert CANARY not in capfd.readouterr().err


@needs_filters
def test_pandocpy(lab):
    """On pandocpy's command line: the files read by the filter in the
    directories LIBPANDOC_WASM_DIRS gives; without them they are missing,
    and a canary outside them never shows, however the conversion is
    sandboxed (not, pandoc's --sandbox, the directory sandbox listing the
    canary's directory)."""
    text = doc(bibliography="refs.bib", csl="style", citation_abbreviations="abbr")
    canary = "---\nbibliography: ../secret/canary.bib\nnocite: '@*'\n---\n\n[@key]\n"

    def pandocpy(args: list[str], env: dict[str, str], text: str) -> subprocess.CompletedProcess:
        e = {k: v for k, v in os.environ.items()
             if k not in ("LIBPANDOC_WASM_DIRS", "LIBPANDOC_SANDBOX", "LIBPANDOC_SANDBOX_WRITE")}
        return subprocess.run(
            [sys.executable, "-m", "libpandoc", "-t", "plain", "-F",
             str(Path(FILTERS) / "citeproc.wasm"), *args],
            input=text.encode(), capture_output=True, cwd=lab / "files", env={**e, **env},
            timeout=180, check=False)

    conversions = [([], {}), (["--sandbox"], {})]
    if os.path.isfile(WASM):
        conversions.append(([], {"LIBPANDOC_SANDBOX": "..", "LIBPANDOC_WASM_TIMEOUT": "120"}))
    for args, env in conversions:
        r = pandocpy(args, {**env, "LIBPANDOC_WASM_DIRS": "."}, text)
        assert r.returncode == 0 and all(ok.encode() in r.stdout for ok in OKS), (args, env, r)
        r = pandocpy(args, env, text)
        assert r.returncode == 83 and b"not found" in r.stderr, (args, env, r)
        assert b"(it was given no directory: LIBPANDOC_WASM_DIRS)" in r.stderr, (args, env, r)
        r = pandocpy(args, {**env, "LIBPANDOC_WASM_DIRS": "."}, canary)
        assert r.returncode == 83 and CANARY.encode() not in r.stdout + r.stderr, (args, env, r)
        assert b"canary.bib' not found" in r.stderr, (args, env, r)


if sys.platform == "emscripten":  # no wasmtime, no processes in Pyodide
    pytestmark = pytest.mark.skip
