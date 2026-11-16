"""What a filter reads through pandoc with its conversion (``read(text,
ctx.conversion)``, ``read_many``) is read in pandoc's own sandbox when the
conversion is sandboxed, however the sandbox was asked for and however the
filter was given; and when that can't be told (a command line's conversion,
one a script makes from its environment).

The probe: a filter reading LaTeX, RST and Org fragments that include a
canary inside a directory (``INSIDE-OK``) and one outside it (``CANARY``),
and saying which it got ("latex:IO": both).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
import warnings
from pathlib import Path

import panir
import pytest

import libpandoc as pandoc

WASM = os.environ.get("LIBPANDOC_WASM", "")
needs_wasm = pytest.mark.skipif(
    sys.platform == "emscripten" or not os.path.isfile(WASM)
    or importlib.util.find_spec("wasmtime") is None,
    reason="the directory sandbox: wasmtime, and LIBPANDOC_WASM set to libpandoc.wasm")
needs_processes = pytest.mark.skipif(sys.platform == "emscripten", reason="no processes")

OPEN = "latex:IO rst:IO org:IO"
CONFINED = "latex:-- rst:-- org:--"

# what a probe does, as a script's text (the functions below run the same)
SEEN = """\
import warnings
import libpandoc, panir

FRAGMENTS = {fragments!r}

def seen(conversion):
    out = []
    for fmt, text in FRAGMENTS.items():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # (an include not found)
            s = panir.dumps(libpandoc.read(text, conversion, {{"from": fmt}}))
        out.append(f"{{fmt}}:" + ("I" if "INSIDE-OK" in s else "-") + ("O" if "CANARY" in s else "-"))
    return " ".join(out)
"""

# a panir script, handing its filter over in process
HANDOFF = """\
from panir import Filter, Pandoc, Para, Str

f = Filter()

@f.on(Pandoc)
def probe(doc, ctx):
    doc.blocks = [Para(Str(seen(ctx.conversion)))]
    return doc

if __name__ == "__main__":
    f.main()
"""

# a plain JSON filter, its conversion made from its environment
PLAIN = """\
import json, sys
doc = json.load(sys.stdin)
doc["blocks"] = [{"t": "Para", "c": [{"t": "Str", "c": seen(panir.Conversion.from_environment())}]}]
json.dump(doc, sys.stdout)
"""


class Lab:
    def __init__(self, root: Path) -> None:
        self.root, self.inside, self.outside = root, root / "inside", root / "outside"
        for d in (self.inside, self.outside):
            d.mkdir()
        for ext in ("tex", "rst", "org"):
            (self.inside / f"in.{ext}").write_text("INSIDE-OK\n")
            (self.outside / f"out.{ext}").write_text("CANARY\n")
        i, o = (p.as_posix() for p in (self.inside, self.outside))
        self.fragments = {
            "latex": f"\\input{{{i}/in.tex}}\n\n\\input{{{o}/out.tex}}\n",
            "rst": f".. include:: {i}/in.rst\n\n.. include:: {o}/out.rst\n",
            "org": f'#+INCLUDE: "{i}/in.org"\n\n#+INCLUDE: "{o}/out.org"\n',
        }
        seen = SEEN.format(fragments=self.fragments)
        self.namespace: dict = {}
        exec(seen, self.namespace)  # noqa: S102  (the probe, as the scripts run it)
        self.scripts = {}
        for name, body, head in (("handoff", HANDOFF, ""), ("plain", PLAIN, ""),
                                 ("subprocess", PLAIN, "# pandocpy: subprocess\n")):
            p = root / f"probe_{name}.py"
            p.write_text(head + seen + body)
            self.scripts[name] = str(p)

    def seen(self, conversion: panir.Conversion) -> str:
        return self.namespace["seen"](conversion)

    @property
    def directory(self) -> dict:
        return {"read": [str(self.inside)]}


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    return Lab(Path(os.path.realpath(tmp_path_factory.mktemp("confined"))))


def plain(out: str | bytes) -> str:
    assert isinstance(out, str)
    return out.strip()


def by_function(lab: Lab, options: dict) -> str:
    got = []
    pandoc.convert("x", to="plain", filters=[lambda doc, c: got.append(lab.seen(c))], **options)
    return got[0]


def by_panir_filter(lab: Lab, options: dict) -> str:
    f = panir.Filter()

    @f.on(panir.Pandoc)
    def probe(doc, ctx):
        doc.blocks = [panir.Para(panir.Str(lab.seen(ctx.conversion)))]
        return doc

    return plain(pandoc.convert("x", to="plain", filters=[f], **options))


def by_script(kind: str):
    def run(lab: Lab, options: dict) -> str:
        return plain(pandoc.convert("x", to="plain", filters=[lab.scripts[kind]], **options))
    return run


def nested(lab: Lab, options: dict) -> str:
    """A filter's own conversion, with the options to read as its
    conversion does, whose filter reads."""
    got = []

    def outer(doc, c):
        pandoc.convert("x", {**pandoc.read_options(c), "to": "json"},
                       filters=[lambda d, inner: got.append(lab.seen(inner))])

    pandoc.convert("x", to="plain", filters=[outer], **options)
    return got[0]


WAYS = {
    "function": by_function,
    "panir.Filter": by_panir_filter,
    "script handing over": by_script("handoff"),
    "script from its environment": by_script("plain"),
    "script as a subprocess": by_script("subprocess"),
    "a filter's convert": nested,
}


def ways():
    return pytest.mark.parametrize(
        "way", [pytest.param(w, marks=[needs_processes] if "subprocess" in w else [])
                for w in WAYS])


@ways()
def test_unsandboxed_a_filter_reads_files(lab, way):
    """The probe works: unsandboxed, what a filter of `convert` reads may
    include files, as Lua's pandoc.read; but a script making its
    conversion from its environment can't tell, and is confined."""
    expected = CONFINED if "script from" in way or "subprocess" in way else OPEN
    assert WAYS[way](lab, {}) == expected


@ways()
def test_pandocs_sandbox(lab, way):
    assert WAYS[way](lab, {"sandbox": True}) == CONFINED


@needs_wasm
@ways()
def test_the_directory_sandbox(lab, way):
    """In process, outside the module: in pandoc's own sandbox, no files,
    not even inside the directory listed."""
    assert WAYS[way](lab, {"sandbox": lab.directory}) == CONFINED


def test_the_authors_own_options_win(lab):
    """A filter that wants files read gives its own options (its code)."""
    got = []

    def unconfined(doc, c):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            got.append(panir.dumps(pandoc.read(lab.fragments["latex"], c, {"from": "latex"},
                                               sandbox=False)))

    pandoc.convert("x", to="plain", filters=[unconfined], sandbox=True)
    assert "INSIDE-OK" in got[0] and "CANARY" in got[0]


def test_a_conversion_without_options_cant_tell(lab):
    """A conversion made by hand, or from the environment, has no options:
    read in pandoc's sandbox. Options without "sandbox" (or None) say it
    isn't sandboxed."""
    assert pandoc.read_options(panir.Conversion())["sandbox"] is True
    assert "sandbox" not in pandoc.read_options(panir.Conversion(options={}))
    assert pandoc.read_options(panir.Conversion(options={"sandbox": {"read": []}}))["sandbox"]
    assert lab.seen(panir.Conversion(options={})) == OPEN


def pandocpy(lab: Lab, args: list[str], env: dict) -> str:
    e = {k: v for k, v in os.environ.items() if not k.startswith("LIBPANDOC_SANDBOX")}
    e.setdefault("LIBPANDOC_WASM_TIMEOUT", "120")
    r = subprocess.run([sys.executable, "-m", "libpandoc", "-t", "plain", *args], input=b"x",
                       capture_output=True, env={**e, **env}, timeout=300, check=False)
    assert r.returncode == 0, r.stderr.decode()
    return r.stdout.decode().strip()


SCRIPTS = pytest.mark.parametrize("kind", ["handoff", "plain", "subprocess"])


@needs_processes
@SCRIPTS
def test_pandocpy_cant_tell(lab, kind):
    """pandoc doesn't tell its filters about --sandbox (in the arguments or
    a defaults file), so pandocpy's are always confined."""
    assert pandocpy(lab, ["-F", lab.scripts[kind]], {}) == CONFINED
    assert pandocpy(lab, ["--sandbox", "-F", lab.scripts[kind]], {}) == CONFINED


@needs_processes
@needs_wasm
@SCRIPTS
def test_pandocpy_in_the_directory_sandbox(lab, kind):
    env = {"LIBPANDOC_SANDBOX": str(lab.inside)}
    assert pandocpy(lab, ["-F", lab.scripts[kind]], env) == CONFINED


@needs_processes
def test_pandocpy_an_installed_filter(lab, tmp_path, monkeypatch):
    """A filter installed as an entry point (a function), in pandocpy."""
    from libpandoc import cli

    pkg = tmp_path / "probe_pkg"
    pkg.mkdir()
    (pkg / "probe_pkg.py").write_text(SEEN.format(fragments=lab.fragments) + textwrap.dedent("""
        def filter(doc, conversion):
            doc.blocks = [panir.Para(panir.Str(seen(conversion)))]
            return doc
        """))
    monkeypatch.syspath_prepend(str(pkg))
    import probe_pkg  # type: ignore[import-not-found]

    monkeypatch.setattr(cli, "installed_filters", lambda: {"probe-pkg": _Loaded(probe_pkg.filter)})
    entries, callbacks = cli.plan([{"type": "json", "path": "probe-pkg"}], cli.installed_filters())
    out = pandoc._core.convert_filters(
        pandoc._dumps({"to": "plain", "filters": entries}), b"x", callbacks)
    assert pandoc._check(*out).decode().strip() == CONFINED


class _Loaded:
    def __init__(self, f):
        self.f = f

    def load(self):
        return self.f
