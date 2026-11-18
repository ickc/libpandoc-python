"""The directory sandbox ("sandbox": {"read": [...], "write": [...]}):
libpandoc-rs's tests (libpandoc/tests/sandbox.rs, pandocrs/tests/sandbox.rs)
ported: the attack suite of the project's sandbox study, as data, run in
libpandoc.wasm with one directory mounted, and the rules of the mounts.

Needs wasmtime and libpandoc.wasm ($LIBPANDOC_WASM); the wasm filter tests
libpandoc-rs's filters ($LIBPANDOC_WASM_FILTERS). The lab, made once: `work`
(mounted, and the working directory), `secret` beside it (canaries,
harmless files whose text says CANARY), `outside` (files the options name,
mounted in some tests), `out` (written to), and user data directories.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import libpandoc as pandoc
from libpandoc import _sandbox

pytest.importorskip("wasmtime")
WASM = os.environ.get("LIBPANDOC_WASM", "")
FILTERS = os.environ.get("LIBPANDOC_WASM_FILTERS", "")
pytestmark = pytest.mark.skipif(
    sys.platform == "emscripten" or not os.path.isfile(WASM),
    reason="set LIBPANDOC_WASM to libpandoc.wasm")
needs_filters = pytest.mark.skipif(
    not FILTERS, reason="set LIBPANDOC_WASM_FILTERS to libpandoc-rs's filters built for wasm32-wasip1")
unix = pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need a privilege")

NOT_PERMITTED = "permission denied (Operation not permitted)"
DOES_NOT_EXIST = "does not exist (No such file or directory)"


class Lab:
    root: Path
    work: Path
    secret: Path
    outside: Path
    out: Path
    data: Path
    url: str
    connections = 0


def svg(text: str) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><text>{text}</text></svg>'


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    """The lab, and the working directory `work`; the module compiled once."""
    lab = Lab()
    root = tmp_path_factory.mktemp("sandbox-lab")
    # the real path (macOS's /var is /private/var)
    lab.root = root = Path(os.path.realpath(root))
    for d in ("work", "secret", "outside", "out", "data/pandoc/templates", "dirs/templates"):
        (root / d).mkdir(parents=True)
    lab.work, lab.secret, lab.outside, lab.out = (root / d for d in ("work", "secret", "outside", "out"))
    lab.data = root / "data"
    files = {
        "secret.txt": "CANARY-TXT-OUTSIDE\n",
        "secret.tex": "CANARY-TEX-OUTSIDE\n",
        "secret2.sty": "\\newcommand{\\leak}{CANARY-STY-OUTSIDE}\n",
        "secret.svg": svg("CANARY-SVG-OUTSIDE"),
        "probe.png": "not really a png",
        "secret.csv": "a,b\nCANARY-CSV-OUTSIDE,x\n",
        "secret.json": '{"a": "CANARY-JSON-OUTSIDE"}',
        "secret.typ": "CANARY-TYP-OUTSIDE\n",
        "secret.org": "CANARY-ORG-OUTSIDE\n",
        "secret.man": ".PP\nCANARY-MAN-OUTSIDE\n",
        "secret.t2t": "CANARY-T2T-OUTSIDE\n",
        "secret.js": "/* CANARY-JS-OUTSIDE */\n",
        "secret.css": "/* CANARY-CSS-OUTSIDE */\n",
        "secret.bib": "@book{canary, title={CANARY-BIB-OUTSIDE}, author={A}, year={2000}}\n",
        "secret.csl": "CANARY-CSL-OUTSIDE\n",
        "x/typst.toml": "CANARY-TOML-OUTSIDE\n",
    }
    for name, text in files.items():
        (lab.secret / name).parent.mkdir(parents=True, exist_ok=True)
        (lab.secret / name).write_text(text)
    (lab.work / "inside.svg").write_text(svg("INSIDE-OK"))
    (lab.work / "inside.txt").write_text("INSIDE-OK\n")
    (lab.work / "inside.tex").write_text("INSIDE-OK\n")
    (lab.work / "doc.md").write_text("*hi*\n")
    (root / "data/pandoc/templates/mine.html").write_text("DATA-DIR-OK $body$\n")
    (root / "dirs/templates/theirs.html").write_text("DATA-DIRS-OK $body$\n")
    if sys.platform != "win32":
        for ext in ("txt", "svg", "tex"):
            (lab.work / f"link-out.{ext}").symlink_to(f"../secret/secret.{ext}")
        (lab.work / "link-dir").symlink_to("../secret")
        for f in ("template.html", "style.css", "refs.bib", "input.md"):
            (lab.work / f"link-{f}").symlink_to(f"../outside/{f}")
    # what the options name: a template, a stylesheet, a bibliography,
    # outside and (`-in`) in `work`
    for d, tag in ((lab.outside, ""), (lab.work, "-in")):
        (d / f"template{tag}.html").write_text(
            'TEMPLATE-OK $for(css)$<link rel="stylesheet" href="$css$">$endfor$ $body$\n')
        (d / f"style{tag}.css").write_text('p::after { content: "CSS-OK" }\n')
        (d / f"refs{tag}.bib").write_text("@book{ok, title={BIB-OK}, author={A}, year={2000}}\n")
    (lab.outside / "input.md").write_text("INPUT-OK")
    # a local web server: how many connections it got
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    lab.url = f"http://127.0.0.1:{listener.getsockname()[1]}"

    def serve() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            Lab.connections += 1
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    _sandbox._module()  # compiled (or loaded from wasmtime's cache) once
    cwd = os.getcwd()
    # relative paths resolve from `work`, as pandoc run there would
    os.chdir(lab.work)
    yield lab
    os.chdir(cwd)
    listener.close()


def mounted(options: dict, read: list, write: list = ()) -> dict:
    return {**options, "sandbox": {"read": [str(d) for d in read], "write": [str(d) for d in write]}}


def convert(options: dict, input: bytes | None = None) -> tuple:
    """A conversion as the host gives it: (status, output, kind, message, log)."""
    dirs = _sandbox.from_options(options)
    return _sandbox.convert(options, input, dirs) if dirs is not None else pandoc._core.convert(
        json.dumps(options).encode(), input)


def named(p: Path | str) -> str:
    """An absolute path as the module names it: on Windows `C:\\x` is `/c/x`."""
    s = str(p).replace("\\", "/").removeprefix("//?/")
    if sys.platform == "win32" and s[1:2] == ":":
        return f"/{s[0].lower()}{s[2:]}"
    return s


# -- what a document could observe -------------------------------------------------


class Seen:
    def __init__(self, output: str, messages: str, everything: str) -> None:
        self.output, self.messages, self.everything = output, messages, everything


def unzip(b: bytes) -> list[tuple[str, bytes]]:
    """The members of a zip file (docx, odt, epub); none if it isn't one."""
    try:
        z = zipfile.ZipFile(io.BytesIO(b))
    except zipfile.BadZipFile:
        return []
    return [(i.filename, z.read(i)) for i in z.infolist()]


_UUID = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_TIME = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[0-9.:+\-Z]*"


def timeless(s: str) -> str:
    """ISO 8601 times and UUIDs written {TIME} and {UUID}: nothing else."""
    return re.sub(f"({_UUID})|{_TIME}", lambda m: "{UUID}" if m[1] else "{TIME}", s)


def base64_runs(s: str) -> str:
    """Runs of base64 (as in data: URIs), decoded."""
    out = b""
    for run in re.findall("[A-Za-z0-9+/]{24,}", s):
        whole = len(run) // 4 * 4
        rest = run[whole:]
        out += base64.b64decode(run[:whole] + (rest + "=" * (4 - len(rest)) if len(rest) > 1 else ""))
        out += b"\n"
    return out.decode("utf-8", "replace")


def seen(r: tuple, media: Path | None = None) -> Seen:
    """What a conversion gave: its output, its messages (the error, or the
    log), and everything (both, zip members inflated, the files extracted
    under `media`, runs of base64 decoded), where a canary would show."""
    status, out, kind, message, log = r
    output, messages = ((out.decode("utf-8", "replace"), log.decode("utf-8", "replace"))
                        if status == 0 else ("", f"{kind}: {message}"))
    everything = f"{output}\n{messages}\n"
    entries = unzip(out) if status == 0 else []
    for _, entry in entries:
        everything += entry.decode("utf-8", "replace")
    # what a document could observe of the output: a zip's members by name
    # and content (not its bytes, which hold its time), times and random
    # identifiers set aside
    if entries:
        output = "".join(f"{name}:\n{entry.decode('utf-8', 'replace')}\n" for name, entry in entries)
    if media is not None and media.exists():
        for f in sorted(media.rglob("*")):
            if f.is_file():
                everything += f.read_bytes().decode("utf-8", "replace")
    return Seen(timeless(output), messages, everything + base64_runs(everything))


# -- the attacks ------------------------------------------------------------------


def cases(lab: Lab) -> list[tuple[str, str, str, str, dict]]:
    """An attack: its name, the document's extension, the document (with
    {P} where the path it tries goes), the path ({S} the secret directory,
    {W} the work one, {URL} the local server), the options."""
    latex = {"from": "latex", "to": "plain"}

    def embed(f: str) -> dict:
        return {"from": f, "to": "html", "embed-resources": True}

    def md(to: str) -> dict:
        return {"from": "markdown", "to": to}

    def p(f: str, t: str = "plain") -> dict:
        return {"from": f, "to": t}

    cite = {"from": "markdown", "to": "plain", "citeproc": True}
    bib = "---\nbibliography: {P}\nnocite: \"@*\"\n---\n\nText.\n"
    xxe = '<?xml version="1.0"?><!DOCTYPE article [<!ENTITY x SYSTEM "file://{P}">]><article>'
    v = [
        ("latex-input-rel", "tex", r"\input{{P}}", "../secret/secret", latex),
        ("latex-input-abs", "tex", r"\input{{P}}", "{S}/secret.tex", latex),
        ("latex-include", "tex", r"\include{{P}}", "../secret/secret", latex),
        ("latex-subfile", "tex", r"\subfile{{P}}", "../secret/secret.tex", latex),
        ("latex-lstinput", "tex", r"\lstinputlisting{{P}}", "../secret/secret.txt", latex),
        ("latex-lst-abs", "tex", r"\lstinputlisting{{P}}", "{S}/secret.txt", latex),
        ("latex-minted", "tex", r"\inputminted{text}{{P}}", "../secret/secret.txt", latex),
        ("latex-usepackage", "tex", r"\usepackage{{P}}\leak", "../secret/secret2", latex),
        ("latex-graphics-probe", "tex", r"\includegraphics{{P}}", "../secret/probe", p("latex", "native")),
        ("latex-graphics-embed", "tex", r"\includegraphics{{P}}", "../secret/secret.svg", embed("latex")),
        ("latex-bibliography", "tex", r"\bibliography{{P}}\nocite{*}", "../secret/secret",
         {**latex, "citeproc": True}),
        ("latex-graphicspath", "tex", r"\graphicspath{{{P}}}\includegraphics{secret.svg}", "../secret/",
         embed("latex")),
        ("rst-include", "rst", ".. include:: {P}", "../secret/secret.txt", p("rst")),
        ("rst-include-abs", "rst", ".. include:: {P}", "{S}/secret.txt", p("rst")),
        ("rst-include-literal", "rst", ".. include:: {P}\n   :literal:", "../secret/secret.txt", p("rst")),
        ("rst-raw-file", "rst", ".. raw:: html\n   :file: {P}", "../secret/secret.txt", p("rst", "html")),
        ("rst-csv-file", "rst", ".. csv-table:: T\n   :file: {P}", "../secret/secret.csv", p("rst")),
        ("rst-csv-url", "rst", ".. csv-table:: T\n   :url: {P}", "{URL}/x.csv", p("rst")),
        ("rst-literalinclude", "rst", ".. literalinclude:: {P}", "../secret/secret.txt", p("rst")),
        ("rst-image-embed", "rst", ".. image:: {P}", "../secret/secret.svg", embed("rst")),
        ("org-include", "org", '#+INCLUDE: "{P}" src text', "../secret/secret.txt", p("org")),
        ("org-include-plain", "org", '#+INCLUDE: "{P}"', "../secret/secret.org", p("org")),
        ("org-setupfile", "org", "#+SETUPFILE: {P}\n\ntext", "../secret/secret.org", p("org")),
        ("adoc-include", "adoc", "include::{P}[]", "../secret/secret.txt", p("asciidoc")),
        ("adoc-include-abs", "adoc", "include::{P}[]", "{S}/secret.txt", p("asciidoc")),
        ("man-so", "man", ".TH T 1\n.so {P}\n", "../secret/secret.man", p("man")),
        ("man-so-abs", "man", ".TH T 1\n.so {P}\n", "{S}/secret.man", p("man")),
        ("t2t-include", "t2t", "\n\n\n%!include: {P}\n", "../secret/secret.t2t", p("t2t")),
        ("typst-include", "typ", '#include "{P}"', "../secret/secret.typ", p("typst")),
        ("typst-read", "typ", '#read("{P}")', "../secret/secret.txt", p("typst")),
        ("typst-read-abs", "typ", '#read("{P}")', "{S}/secret.txt", p("typst")),
        ("typst-json", "typ", '#json("{P}").a', "../secret/secret.json", p("typst")),
        ("typst-csv", "typ", '#csv("{P}").at(1).at(0)', "../secret/secret.csv", p("typst")),
        ("typst-image", "typ", '#image("{P}")', "../secret/secret.svg", embed("typst")),
        ("typst-import-pkg", "typ", '#import "@preview/../../../../../../../../../../../..{P}:1": *\n#leak',
         "{S}/x", p("typst")),
        ("djot-image", "dj", "![x]({P})", "../secret/secret.svg", embed("djot")),
        ("mediawiki-transclude", "wiki", "{{:{P}}}", "../secret/secret.txt", p("mediawiki")),
        ("dokuwiki-include", "dw", "{{page>{P}}}", "../secret/secret.txt", p("dokuwiki")),
        ("creole-image", "creole", "{{{P}|x}}", "../secret/secret.svg", embed("creole")),
        ("md-image-embed", "md", "![x]({P})", "../secret/secret.svg", embed("markdown")),
        ("md-image-abs", "md", "![x]({P})", "{S}/secret.svg", embed("markdown")),
        ("md-image-abs-dotdot", "md", "![x]({P})", "{W}/../secret/secret.svg", embed("markdown")),
        ("md-image-file-url", "md", "![x](file://{P})", "{S}/secret.svg", embed("markdown")),
        ("md-image-pct", "md", "![x]({P})", "..%2Fsecret%2Fsecret.svg", embed("markdown")),
        ("md-image-http", "md", "![x]({P})", "{URL}/x.svg", embed("markdown")),
        ("md-image-docx", "md", "![x]({P})", "../secret/secret.svg", md("docx")),
        ("md-image-odt", "md", "![x]({P})", "../secret/secret.svg", md("odt")),
        ("md-image-epub", "md", "![x]({P})", "../secret/secret.svg", md("epub")),
        ("md-image-rtf", "md", "![x]({P})", "../secret/secret.svg", md("rtf")),
        ("md-image-ipynb", "md", "![x]({P})", "../secret/secret.svg", md("ipynb")),
        ("md-image-fb2", "md", "![x]({P})", "../secret/secret.svg", md("fb2")),
        ("md-image-icml", "md", "![x]({P})", "../secret/secret.svg", md("icml")),
        ("md-extract-media", "md", "![x]({P})", "../secret/secret.svg",
         {"from": "markdown", "to": "html", "extract-media": "media-md-extract-media"}),
        ("md-raw-html", "md",
         '<script src="{P}.js"></script><link rel="stylesheet" href="{P}.css"><img src="{P}.svg">',
         "../secret/secret", {"from": "markdown", "to": "html", "embed-resources": True,
                              "standalone": True, "metadata": {"title": "x"}}),
        ("html-embed", "html",
         '<p><img src="{P}.svg"></p><script src="{P}.js"></script><link rel="stylesheet" href="{P}.css">',
         "../secret/secret", {"from": "html", "to": "html", "embed-resources": True,
                              "standalone": True, "metadata": {"title": "x"}}),
        ("html-iframe-http", "html", '<iframe src="{P}"></iframe>', "{URL}/x.html", p("html")),
        ("html-iframe-file", "html", '<iframe src="file://{P}"></iframe>', "{S}/secret.txt", p("html")),
        ("html-iframe-rel", "html", '<iframe src="{P}"></iframe>', "../secret/secret.txt", p("html")),
        ("docbook-xxe", "xml", xxe + "<para>&x;</para></article>", "{S}/secret.txt", p("docbook")),
        ("docbook-xinclude", "xml",
         ('<article xmlns:xi="http://www.w3.org/2001/XInclude"><para><xi:include href="{P}" '
          'parse="text"/></para></article>'), "../secret/secret.txt", p("docbook")),
        ("jats-xxe", "xml", xxe + "<body><p>&x;</p></body></article>", "{S}/secret.txt", p("jats")),
        ("opml-ext", "opml",
         '<?xml version="1.0"?><opml version="2.0"><head/><body><outline text="a" url="{P}"/></body></opml>',
         "../secret/secret.txt", p("opml")),
        ("meta-bibliography", "md", bib, "../secret/secret.bib", cite),
        ("meta-bibliography-abs", "md", bib, "{S}/secret.bib", cite),
        ("meta-bibliography-http", "md", bib, "{URL}/x.bib", cite),
        ("meta-csl", "md", "---\ncsl: {P}\n---\n\nText [@x].\n", "../secret/secret.csl", cite),
        ("meta-abbrev", "md",
         "---\ncitation-abbreviations: {P}\nreferences: [{id: x, title: T, type: book}]\n---\n\nText [@x].\n",
         "../secret/secret.txt", cite),
        ("meta-lang", "md", "---\nlang: {P}\n---\n\nText.\n", "../../secret/secret",
         {"from": "markdown", "to": "latex", "standalone": True}),
        ("meta-header-includes", "md", "---\nheader-includes: {P}\ntitle: x\n---\n\nText.\n",
         "../secret/secret.txt", {"from": "markdown", "to": "html", "standalone": True}),
        ("meta-bib-in-latex", "tex", r"\addbibresource{{P}}\nocite{*}", "../secret/secret.bib",
         {**latex, "citeproc": True}),
    ]
    if sys.platform != "win32":  # (a symbolic link needs a privilege on Windows)
        v += [
            ("latex-input-symlink", "tex", r"\input{{P}}", "link-out", latex),
            ("latex-input-symlinkdir", "tex", r"\input{{P}}", "link-dir/secret", latex),
            ("typst-read-symlink", "typ", '#read("{P}")', "link-out.txt", p("typst")),
            ("md-image-symlink", "md", "![x]({P})", "link-out.svg", embed("markdown")),
            ("md-image-symlink-abs", "md", "![x]({P})", "{W}/link-out.svg", embed("markdown")),
        ]
    return v


def fill(lab: Lab, s: str) -> str:
    return s.replace("{S}", named(lab.secret)).replace("{W}", named(lab.work)).replace("{URL}", lab.url)


def missing(lab: Lab, attack: str) -> str:
    """A path that doesn't exist, inside the directory, as long as the
    attack's and with its extension (its other characters `q`): what a
    refusal must look like."""
    w = named(lab.work) + "/"
    if attack.startswith(lab.url):
        prefix, rest = lab.url, attack[len(lab.url):]
    elif attack.startswith("/") or ":/" in attack:
        prefix, rest = w, attack[min(len(w), len(attack)):]
    else:
        prefix, rest = "", attack
    last = rest.rsplit("/", 1)[-1]
    ext = last[last.rfind("."):] if "." in last else ""
    stem = rest[:len(rest) - len(ext)]
    return prefix + "".join(c if c == "/" else "q" for c in stem) + ext


def attempt(lab: Lab, case: tuple, p: str, tag: str) -> Seen:
    """Convert the case with the document naming `p`; what came out, with
    `p` written {P}."""
    name, ext, doc, _, options = case
    stem = f"doc-{name}-{tag}"
    (lab.work / f"{stem}.{ext}").write_text(doc.replace("{P}", p))
    options = mounted({**options, "input-files": [f"{stem}.{ext}"]}, [lab.work])
    media = None
    if "extract-media" in options:
        options["extract-media"] = m = f"{options['extract-media']}-{tag}"
        media = lab.work / m
    s = seen(convert(options), media)

    def general(t: str) -> str:
        return t.replace(p, "{P}").replace(p.replace("%2F", "/"), "{P}").replace(stem, "{FILE}")

    return Seen(general(s.output), general(s.messages), s.everything)


def check(lab: Lab, case: tuple) -> tuple[str, list[str]]:
    problems = []
    path = fill(lab, case[3])
    attack = attempt(lab, case, path, "attack")
    if "CANARY" in attack.everything:
        problems.append(f"LEAK: {attack.everything}")
    control = attempt(lab, case, missing(lab, path), "missing")
    same = attack.output == control.output
    # `..` or a symbolic link leading out of the directory is "not
    # permitted" in WASI, a file outside every directory "does not exist":
    # documented (README, "Security"), not hidden
    if same and attack.messages == control.messages:
        pass
    elif same and attack.messages.replace(NOT_PERMITTED, DOES_NOT_EXIST) == control.messages:
        problems.append("ERRNO")
    else:
        problems.append(f"DISTINGUISHABLE:\n  refused: {attack.messages}\n{attack.output}\n"
                        f"  missing: {control.messages}\n{control.output}")
    return case[0], problems


def test_the_attacks_leak_nothing(lab):
    """The study's cases: nothing of `secret` comes out, no connection is
    made, and a refusal reads as a missing file does."""
    all_cases = cases(lab)
    with ThreadPoolExecutor(min(8, os.cpu_count() or 2)) as pool:
        results = list(pool.map(lambda c: check(lab, c), all_cases))
    errno = [name for name, problems in results if "ERRNO" in problems]
    report = "".join(f"{name}:\n  " + "\n  ".join(p for p in problems if p != "ERRNO") + "\n"
                     for name, problems in results if any(p != "ERRNO" for p in problems))
    print(f'refused as "not permitted", not "does not exist": {errno}')
    print(f"{len(results)} cases; root {lab.root}")
    assert len(results) == (70 if sys.platform == "win32" else 75)
    assert Lab.connections == 0, "a connection was made"
    assert not report, report


def test_an_image_inside_is_embedded(lab):
    r = convert(mounted({"from": "markdown", "to": "html", "embed-resources": True}, [lab.work]),
                b"![x](inside.svg)")
    assert "INSIDE-OK" in seen(r).everything, r


def test_without_the_sandbox_they_leak(lab):
    """The canaries are found where they are: without the sandbox, the same
    documents leak (so that the suite's "nothing leaked" means something)."""
    by_name = {c[0]: c for c in cases(lab)}
    for name in ("latex-input-rel", "md-image-embed", "md-image-docx", "rst-include"):
        _, ext, doc, path, options = by_name[name]
        (lab.work / f"doc-{name}-native.{ext}").write_text(doc.replace("{P}", fill(lab, path)))
        r = convert({**options, "input-files": [f"doc-{name}-native.{ext}"]})
        assert "CANARY" in seen(r).everything, (name, r)


def test_outputs_compare_across_seconds(lab):
    """The suite compares outputs as a document could observe them: two
    conversions a second apart differ in their bytes (ODT and docx hold the
    time, EPUB a fresh identifier) and not in what `seen` keeps."""
    def run(to: str) -> tuple[bytes, str]:
        r = convert(mounted({"from": "markdown", "to": to, "metadata": {"title": "t"}}, [lab.work]),
                    b"*x*")
        return r[1], seen(r).output

    for to in ("odt", "docx", "epub"):
        a = run(to)
        time.sleep(1.1)
        b = run(to)
        assert a[0] != b[0], f"{to}: the same bytes"
        assert a[1] == b[1], to
    assert (timeless("at 2026-11-15T04:51:42.5Z, urn:uuid:0b2f4a8e-1c3d-4e5f-8a9b-0c1d2e3f4a5b.")
            == "at {TIME}, urn:uuid:{UUID}.")


# -- the rules ----------------------------------------------------------------------


def as_missing(m: str) -> str:
    return m.replace(NOT_PERMITTED, DOES_NOT_EXIST)


def test_what_the_options_name_must_be_mounted(lab):
    """The files the options name are read only inside the mounts: a
    template, a stylesheet, a bibliography, an input file. Outside, each
    reads as a missing file named the same way does, or (for an input file
    reached by `..` or a symbolic link) "not permitted": documented."""
    def options(template: str, css: str, bib: str) -> dict:
        return {"from": "markdown", "to": "html", "standalone": True, "embed-resources": True,
                "citeproc": True, "template": template, "css": [css], "bibliography": [bib]}

    doc = b'---\nnocite: "@*"\n---\n\nText.'

    def ok(r: tuple) -> bool:
        return all(x in seen(r).everything for x in ("TEMPLATE-OK", "CSS-OK", "BIB-OK"))

    def abs_(f: str) -> str:
        return named(lab.outside / f)

    # inside: relative to the working directory, and absolute
    r = convert(mounted(options("template-in.html", "style-in.css", "refs-in.bib"), [lab.work]), doc)
    assert ok(r), r
    r = convert(mounted(options(abs_("template.html"), abs_("style.css"), abs_("refs.bib")),
                        [lab.work, lab.outside]), doc)
    assert ok(r), r

    def o(what: str, p: str) -> dict:
        if what == "template":
            return {"from": "markdown", "to": "html", "standalone": True, "template": p}
        if what == "css":
            return {"from": "markdown", "to": "html", "standalone": True, "embed-resources": True,
                    "css": [p], "metadata": {"title": "t"}}
        if what == "bib":
            return {"from": "markdown", "to": "plain", "citeproc": True, "bibliography": [p]}
        return {"from": "markdown", "to": "plain", "input-files": [p]}

    # outside, each way of naming it, against a missing file inside (`qqq`
    # for the directory); `..` from one mount into another fails too
    table = []
    for what, file in (("template", "template.html"), ("css", "style.css"), ("bib", "refs.bib"),
                       ("input", "input.md")):
        ways = [("relative", f"../outside/{file}", f"qqqqqqqqqq/{file}"),
                ("absolute", abs_(file), named(lab.work / "qqqqqqq" / file)),
                ("dotdot-between", f"../outside/{file}", f"qqqqqqqqqq/{file}")]
        if sys.platform != "win32":
            ways.append(("symlink", f"link-{file}", f"qqqqq{file}"))
        for way, path, miss in ways:
            dirs = [lab.work, lab.outside] if way == "dotdot-between" else [lab.work]
            given = None if what == "input" else doc
            refused = convert(mounted(o(what, path), dirs), given)
            s = seen(refused)
            assert "-OK" not in s.everything, f"{what} {way}: read: {refused}"
            a = s.messages.replace(path, "{P}")
            b = seen(convert(mounted(o(what, miss), dirs), given)).messages.replace(miss, "{P}")
            how = "missing" if a == b else "not permitted" if as_missing(a) == b else "other"
            table.append(f"{what} {way}: {how}")
    expected = [
        "template relative: missing", "template absolute: missing",
        "template dotdot-between: missing", "template symlink: missing",
        "css relative: missing", "css absolute: missing", "css dotdot-between: missing",
        "css symlink: missing",
        "bib relative: missing", "bib absolute: missing", "bib dotdot-between: missing",
        "bib symlink: missing",
        "input relative: not permitted", "input absolute: missing",
        "input dotdot-between: not permitted", "input symlink: not permitted",
    ]
    assert table == [e for e in expected if sys.platform != "win32" or "symlink" not in e]


def walk(d: Path) -> list[Path]:
    return sorted(f for f in d.rglob("*") if f.is_file()) if d.exists() else []


def test_outputs_go_only_into_writable_mounts(lab):
    """pandoc writes only inside the write list: `output-file`,
    `extract-media` and `log-file` there work; in a read-only mount or
    outside every mount they fail, and nothing is written."""
    def o(out: Path, media: Path) -> dict:
        return {"from": "markdown", "to": "html", "standalone": True, "metadata": {"title": "t"},
                "output-file": named(out / "doc.html"), "extract-media": named(media / "media"),
                "log-file": named(out / "log.json")}

    doc = b"![x](inside.svg)"
    d = lab.out / "writable"
    d.mkdir()
    r = convert(mounted(o(d, d), [lab.work], [d]), doc)
    assert r[0] == 0 and r[1] == b"", r
    assert "media/" in (d / "doc.html").read_text()
    assert (d / "log.json").read_text().startswith("[")
    media = walk(d / "media")
    assert len(media) == 1 and "INSIDE-OK" in media[0].read_text(), media
    # relative to the working directory, when it is writable (the public API)
    assert pandoc.convert("x", to="html", output_file="doc-relative.html",
                          sandbox={"write": [str(lab.work)]}) == ""
    assert (lab.work / "doc-relative.html").exists()
    # a read-only mount, and a directory that isn't mounted
    ro, unmounted = lab.out / "read-only", lab.out / "unmounted"
    ro.mkdir()
    unmounted.mkdir()
    for out, med, read, why in ((ro, d, [lab.work, ro], NOT_PERMITTED),
                                (d, ro, [lab.work, ro], NOT_PERMITTED),
                                (unmounted, d, [lab.work], DOES_NOT_EXIST),
                                (d, unmounted, [lab.work], DOES_NOT_EXIST)):
        r = convert(mounted(o(out, med), read, [d]), doc)
        assert r[0] != 0 and why in r[3], r
    assert walk(ro) == [] and walk(unmounted) == []


def test_a_notebook_attachment_stays_inside_the_media_directory(lab):
    doc = ('{"cells":[{"cell_type":"markdown","metadata":{},"attachments":{"../../../x.svg":'
           '{"image/svg+xml":"PHN2Zy8+"}},"source":["![x](attachment:../../../x.svg)"]}],'
           '"metadata":{},"nbformat":4,"nbformat_minor":5}')
    d = lab.out / "ipynb"
    d.mkdir()
    r = convert(mounted({"from": "ipynb", "to": "html", "extract-media": named(d / "media")}, [], [d]),
                doc.encode())
    assert r[0] == 0, r
    files = walk(d)
    assert len(files) == 1 and files[0].is_relative_to(d / "media"), files


def test_what_is_refused(lab):
    def refused(sandbox, why: str) -> None:
        with pytest.raises(pandoc.PandocError) as e:
            pandoc.convert("x", to="html", sandbox=sandbox)
        assert e.value.kind == "PandocOptionError" and why in e.value.message, e.value

    refused({"read": ["no-such-directory"]}, "no-such-directory")
    refused({"write": ["no-such-directory"]}, "no-such-directory")
    refused({"read": [str(lab.work / "inside.txt")]}, "not a directory")
    refused({"read": [], "network": True}, '"network"')
    refused({"read": "."}, "lists directories")
    refused({"read": [1]}, "lists directories")
    # the limits: as a wasm filter's
    for bad in (-1, float("nan"), float("inf"), "1", True):
        with pytest.raises(ValueError):
            pandoc.convert("x", to="html", sandbox={"read": [], "timeout": bad})
        with pytest.raises(ValueError):
            pandoc.convert("x", to="html", sandbox={"read": [], "max-memory": bad})
    # read_many reads in this process: a directory sandbox is refused
    with pytest.raises(pandoc.PandocError, match="for conversions"):
        pandoc.read_many(["x"], sandbox={"read": []})
    # pandoc's own sandbox stays the native library's
    assert pandoc.convert("*x*", to="html", sandbox=True) == "<p><em>x</em></p>\n"


def test_none_is_an_absent_limit(lab, monkeypatch):
    """A sandbox's value None is absent: a limit the environment's, a list
    empty; a list with None in it is refused."""
    sandbox = {"read": None, "write": None, "timeout": None, "max-memory": None}
    for var in ("LIBPANDOC_WASM_TIMEOUT", "LIBPANDOC_WASM_MAX_MEMORY"):
        monkeypatch.setenv(var, "-1")  # (the environment's is read, and refused)
        with pytest.raises(ValueError, match=var):
            pandoc.convert("x", to="html", sandbox=sandbox)
        monkeypatch.delenv(var)
    assert pandoc.convert("x", to="html", sandbox=sandbox) == "<p>x</p>\n"
    with pytest.raises(pandoc.PandocError, match="lists directories"):
        pandoc.convert("x", to="html", sandbox={"read": [None]})


def test_the_limits_from_the_environment_are_checked(lab, monkeypatch):
    for var, bad in (("LIBPANDOC_WASM_TIMEOUT", "-1"), ("LIBPANDOC_WASM_TIMEOUT", "nan"),
                     ("LIBPANDOC_WASM_TIMEOUT", "inf"), ("LIBPANDOC_WASM_MAX_MEMORY", "-1"),
                     ("LIBPANDOC_WASM_MAX_MEMORY", "1x")):
        monkeypatch.setenv(var, bad)
        with pytest.raises(ValueError, match=var):
            pandoc.convert("x", to="html", sandbox={"read": []})
        monkeypatch.delenv(var)


def test_pdf_needs_a_program(lab):
    """A PDF needs a program: it fails, naming it."""
    r = convert(mounted({"from": "markdown", "to": "pdf"}, [lab.work]), b"x")
    assert r[0] != 0 and "pdflatex" in r[3] and "unsupported operation" in r[3], r


def test_a_call_has_a_time_limit(lab):
    """libpandoc's "timeout" works in the module too, and so does the
    sandbox's own (the module's clock: it stops Lua as well)."""
    for options in ({"timeout": 1}, {"sandbox": {"read": [str(lab.work)], "timeout": 1}}):
        started = time.monotonic()
        with pytest.raises(pandoc.PandocTimeoutError):
            pandoc.convert(">" * 100_000, **{"sandbox": {"read": [str(lab.work)]}, **options})
        assert time.monotonic() - started < 10
    (lab.work / "forever.lua").write_text("function Pandoc(d) while true do end end\n")
    with pytest.raises(pandoc.PandocTimeoutError, match='"timeout"'):
        pandoc.convert("x", filters=["forever.lua"], sandbox={"read": [str(lab.work)], "timeout": 1})


def test_no_directory_at_all(lab):
    """No directory at all: the input given, and nothing else."""
    r = convert({"from": "markdown", "to": "html", "embed-resources": True, "sandbox": {}},
                b"*hi* ![x](inside.svg)")
    assert r[0] == 0 and b"<em>hi</em>" in r[1] and "INSIDE-OK" not in seen(r).everything, r


def test_the_working_directory(lab):
    """Relative names resolve from the process's working directory when it
    is mounted; when it isn't they find nothing, and the error says why."""
    o = {"from": "markdown", "to": "plain", "input-files": ["inside.txt"]}
    assert pandoc.convert(None, o, sandbox={"read": [str(lab.work)]}) == "INSIDE-OK\n"
    with pytest.raises(pandoc.PandocError) as e:
        pandoc.convert(None, o, sandbox={"read": [str(lab.outside)]})
    assert "does not exist" in e.value.message and "is not in the sandbox" in e.value.message


def test_dot_dot_inside_a_mount(lab):
    """`..` that stays inside one mounted directory resolves; one that
    leaves it does not, even towards another mounted directory."""
    (lab.work / "sub").mkdir()
    (lab.work / "sub/t.html").write_text("DOTDOT-OK $body$")
    out = pandoc.convert(None, input_files=["sub/../doc.md"], standalone=True,
                         template="sub/../sub/t.html", sandbox={"read": [str(lab.work)]})
    assert "DOTDOT-OK" in out
    with pytest.raises(pandoc.PandocError):
        pandoc.convert(None, input_files=["../outside/input.md"],
                       sandbox={"read": [str(lab.work), str(lab.outside)]})


def test_a_lua_filter_is_confined(lab):
    """A Lua filter runs inside, confined as the document is: it reads and
    writes only in the mounts, runs no program, and is found only inside."""
    d = lab.out / "lua"
    d.mkdir()
    mark = {k: named(d / k) for k in ("written.txt", "executed", "popen", "pipe")}
    lua = f"""function Pandoc(doc)
  local out = {{}}
  local function read(f)
    local h = io.open(f)
    out[#out + 1] = f .. "=" .. (h and h:read("a"):sub(1, 9) or "none")
  end
  local function write(f)
    local h = io.open(f, "w")
    out[#out + 1] = f .. "=" .. (h and "written" or "refused")
    if h then h:close() end
  end
  read("inside.txt")
  read("../secret/secret.txt")
  read("{named(lab.secret / 'secret.txt')}")
  write("written-by-lua.txt")
  write("{mark['written.txt']}")
  os.execute("touch {mark['executed']}")
  pcall(io.popen, "touch {mark['popen']}")
  pcall(pandoc.pipe, "touch", {{"{mark['pipe']}"}}, "")
  return pandoc.Pandoc({{pandoc.Para(table.concat(out, " "))}})
end
"""
    (lab.work / "confined.lua").write_text(lua)
    text = pandoc.convert("x", to="plain", filters=["confined.lua"], wrap="none",
                          sandbox={"read": [str(lab.work)], "write": [str(d)]})
    assert "CANARY" not in text
    for x in ("inside.txt=INSIDE-OK", "../secret/secret.txt=none", "secret.txt=none",
              "written-by-lua.txt=refused", "written.txt=written"):
        assert x in text, (x, text)
    assert sorted(p.name for p in d.iterdir()) == ["written.txt"]
    # a Lua filter outside the mounts isn't found
    (lab.outside / "confined.lua").write_text(lua)
    with pytest.raises(pandoc.PandocError):
        pandoc.convert("x", to="plain", filters=[str(lab.outside / "confined.lua")],
                       sandbox={"read": [str(lab.work)]})


def test_python_filters_run_here(lab):
    """Python filters run in this process, on the thread running the module
    (the calling thread; the helper thread for the main thread's calls),
    inside the conversion; their exception is raised as it is, as
    natively."""
    threads = []

    def upper(doc, conversion):
        threads.append((threading.get_ident(), conversion.format))
        return pandoc.read(pandoc.write(doc, "plain").replace("hello", "HELLO"))

    sandbox = {"read": [str(lab.work)]}
    results = []
    other = threading.Thread(
        target=lambda: results.append(pandoc.convert("hello", to="plain", filters=[upper],
                                                     sandbox=sandbox)))
    other.start()
    other.join()
    assert results == ["HELLO\n"] and threads == [(other.ident, "plain")]

    def fails(doc):
        raise KeyError("no")

    with pytest.raises(KeyError, match="no"):
        pandoc.convert("hello", to="plain", filters=[fails], sandbox=sandbox)

    def fragment(doc, conversion):  # read as the conversion reads: in pandoc's sandbox here
        assert pandoc.read_options(conversion)["sandbox"] is True
        (frag,) = pandoc.read_many([r"\input{inside} y"], conversion)
        return frag

    with pytest.warns(pandoc.PandocWarning, match="Could not load include file"):
        out = pandoc.convert("x", from_="latex", to="plain", filters=[fragment], sandbox=sandbox)
    assert out == "y\n"


def test_conversions_in_threads(lab):
    """Conversions from several threads at once, each with a Python filter."""
    def one(i: int) -> str:
        return pandoc.convert(f"n{i}", to="plain", filters=[lambda d: d],
                              sandbox={"read": [str(lab.work)]})

    with ThreadPoolExecutor(8) as pool:
        assert list(pool.map(one, range(32))) == [f"n{i}\n" for i in range(32)]


def test_a_deep_document_converts(lab):
    """GHC keeps its stack in the module's memory: a deeply nested document
    needs no more of this thread's (here a thread with 1 MiB of stack, as
    small as any Python's)."""
    out = []
    old = threading.stack_size(1 << 20)
    try:
        t = threading.Thread(target=lambda: out.append(pandoc.convert(
            "> " * 2000 + "x", to="html", sandbox={"read": [str(lab.work)]})))
        t.start()
        t.join()
    finally:
        threading.stack_size(old)
    assert out and out[0].count("<blockquote>") == 2000


def test_a_json_filter_is_refused(lab):
    """A JSON filter (a program) is refused: wasm starts no process, and the
    message says so, whether the program is a file or on PATH."""
    f = lab.work / "filter.sh"
    ran = lab.out / "json-filter-ran"
    f.write_text(f"#!/bin/sh\ntouch '{ran}'\ncat\n")
    f.chmod(0o755)
    for filter_ in (str(f), "cat"):
        with pytest.raises(pandoc.PandocError) as e:
            pandoc.convert("hello", to="plain", filters=[filter_], sandbox={"read": [str(lab.work)]})
        assert e.value.kind == "PandocFilterError" and "runs no programs" in e.value.message
    assert not ran.exists()


@needs_filters
def test_a_wasm_filter_sees_its_own_directories(lab, monkeypatch):
    """A wasm filter sees its own directories, not the sandbox's mounts: the
    document's list and the filter's are different lists."""
    from libpandoc._wasmfilter import WasmFilterError

    include = Path(FILTERS) / "include.wasm"
    monkeypatch.delenv("LIBPANDOC_WASM_DIRS", raising=False)

    def run(f: pandoc.WasmFilter, file: str) -> str:
        return pandoc.convert(f"```{{.include}}\n{file}\n```\n", to="plain", filters=[f],
                              sandbox={"read": [str(lab.work)], "write": [str(lab.out)]})

    # none: not the mounts (the working directory, `work`, is mounted)
    for f, says in ((pandoc.WasmFilter(include), "(it was given no directory: LIBPANDOC_WASM_DIRS)"),
                    (pandoc.WasmFilter(include, dirs=[]), "(it was given no directory)")):
        with pytest.raises(WasmFilterError) as e:
            run(f, "inside.txt")
        assert str(e.value).endswith(f"exited with status 3 {says}"), e.value
    # its own: `work` (here `.`, the working directory), not `out`
    own = pandoc.WasmFilter(include, dirs=[lab.work])
    assert "INSIDE-OK" in run(own, "inside.txt")
    (lab.out / "input.md").write_text((lab.outside / "input.md").read_text())
    with pytest.raises(WasmFilterError):
        run(own, named(lab.out / "input.md"))
    assert "INPUT-OK" in run(pandoc.WasmFilter(include, dirs=[lab.out]), named(lab.out / "input.md"))
    # the variable's
    monkeypatch.setenv("LIBPANDOC_WASM_DIRS", ".")
    assert "INSIDE-OK" in run(pandoc.WasmFilter(include), "inside.txt")
    for outside in ("../secret/secret.txt", named(lab.secret / "secret.txt"),
                    named(lab.outside / "input.md")):
        with pytest.raises(WasmFilterError) as e:
            run(own, outside)
        assert "CANARY" not in str(e.value)


def test_without_wasmtime_it_is_refused(lab, monkeypatch):
    """Without wasmtime the setting is refused, never ignored."""
    monkeypatch.setitem(sys.modules, "wasmtime", None)
    with pytest.raises(pandoc.PandocError, match="wasmtime"):
        pandoc.convert("x", to="html", sandbox={"read": []})
    monkeypatch.setenv("LIBPANDOC_SANDBOX", ".")
    with pytest.raises(pandoc.PandocError, match="wasmtime"):
        pandoc.run(["-t", "html"], input="x")


def test_the_module_is_checked(lab, tmp_path, monkeypatch):
    """A module this package can't use fails, naming what to do; a download
    is checked against the release's SHA256SUMS and written whole."""
    with pytest.raises(pandoc.PandocError, match="remove /x/libpandoc.wasm"):
        monkeypatch.delenv("LIBPANDOC_WASM")
        _sandbox._checked_abi(1001, "/x/libpandoc.wasm")
    monkeypatch.setenv("LIBPANDOC_WASM", str(tmp_path / "nope.wasm"))
    with pytest.raises(pandoc.PandocError, match="not libpandoc.wasm"):
        pandoc.convert("x", to="html", sandbox={"read": []})
    import hashlib

    served = tmp_path / "release"
    served.mkdir()
    (served / "libpandoc.wasm").write_bytes(b"\0asm-not-really")
    (served / _sandbox.NOTICES).write_bytes(b"notices\n")
    notices = hashlib.sha256(b"notices\n").hexdigest() + f"  {_sandbox.NOTICES}\n"
    (served / "SHA256SUMS").write_text(f"{'0' * 64}  libpandoc.wasm\n{notices}")
    monkeypatch.setattr(_sandbox, "RELEASE_URL", served.as_uri() + "/")
    target = tmp_path / "cache" / "libpandoc.wasm"
    with pytest.raises(pandoc.PandocError, match="SHA256SUMS says 0000"):
        _sandbox._download(str(target))
    assert not target.exists()
    digest = hashlib.sha256(b"\0asm-not-really").hexdigest()
    (served / "SHA256SUMS").write_text(f"{digest} *libpandoc.wasm\n{notices}")
    _sandbox._download(str(target))
    # the module, and its licence notices beside it
    assert target.read_bytes() == b"\0asm-not-really"
    assert sorted(p.name for p in target.parent.iterdir()) == [_sandbox.NOTICES, target.name]
    assert (target.parent / _sandbox.NOTICES).read_text() == "notices\n"


# -- pandocpy with LIBPANDOC_SANDBOX and LIBPANDOC_SANDBOX_WRITE ---------------------


def pandocpy(lab: Lab, args: list[str], input: str = "", env: dict | None = None,
             native: bool = False) -> tuple[int, str, str]:
    """pandocpy in `work` with the environment given (a user data directory
    of the lab's, a time limit of 60 s unless given); (status, out, err)."""
    e = {k: v for k, v in os.environ.items()
         if k not in ("LIBPANDOC_SANDBOX", "LIBPANDOC_SANDBOX_WRITE", "LIBPANDOC_WASM_MAX_MEMORY",
                      "LIBPANDOC_WASM_DIRS", "PANDOC_DATA_DIRS")}
    e.update(LIBPANDOC_WASM_TIMEOUT="60", XDG_DATA_HOME=str(lab.data), APPDATA=str(lab.data))
    e.update(env or {})
    r = subprocess.run([sys.executable, "-m", "libpandoc", *args], input=input.encode(),
                       capture_output=True, cwd=lab.work, env=e, timeout=180, check=False)
    text = lambda b: b.decode("utf-8", "replace").replace("\r\n", "\n")
    return r.returncode, text(r.stdout), text(r.stderr)


HERE = {"LIBPANDOC_SANDBOX": "."}


def test_pandocpy_reads_only_inside(lab):
    (lab.work / "doc.tex").write_text("\\input{inside}\n\n\\input{../secret/secret}\n")
    status, out, err = pandocpy(lab, ["doc.tex", "-t", "plain"], env=HERE)
    assert status == 0 and "INSIDE-OK" in out and "CANARY" not in out, err
    assert "[WARNING] Could not load include file ../secret/secret" in err
    # standard input, as pandoc reads it
    status, out, _ = pandocpy(lab, ["-f", "latex", "-t", "plain"], "\\input{inside}", HERE)
    assert (status, out.strip()) == (0, "INSIDE-OK")
    # an input file outside: as pandoc fails for a file it can't read
    status, out, err = pandocpy(lab, [named(lab.secret / "secret.tex"), "-t", "plain"], env=HERE)
    assert status == 1 and out == "" and "does not exist" in err, err
    # no directory at all: the input only
    status, out, _ = pandocpy(lab, ["-f", "latex", "-t", "plain"], "\\input{inside} x",
                              {"LIBPANDOC_SANDBOX": ""})
    assert (status, out.strip()) == (0, "x")
    status, out, _ = pandocpy(lab, ["--version"], env=HERE)
    assert status == 0 and "-lua" in out, out


def test_pandocpy_writes_only_where_listed(lab):
    env = {**HERE, "LIBPANDOC_SANDBOX_WRITE": str(lab.out)}
    status, _, err = pandocpy(lab, ["doc.md", "-o", named(lab.out / "doc.html")], env=env)
    assert status == 0, err
    assert (lab.out / "doc.html").read_text() == "<p><em>hi</em></p>\n"
    status, _, err = pandocpy(lab, ["doc.md", "-o", "doc.html"], env={"LIBPANDOC_SANDBOX_WRITE": "."})
    assert status == 0 and (lab.work / "doc.html").exists(), err
    status, _, err = pandocpy(lab, ["doc.md", "-o", "x.html"], env=HERE)
    assert status != 0 and "permission denied" in err and not (lab.work / "x.html").exists(), err
    # `..` from one mount into another: an absolute path works, this not
    status, _, err = pandocpy(lab, ["doc.md", "-o", "../out/rel.html"], env=env)
    assert status != 0 and "permission denied" in err, err


def test_pandocpy_the_working_directory_must_be_listed(lab):
    status, _, err = pandocpy(lab, ["doc.md"], env={"LIBPANDOC_SANDBOX": str(lab.out)})
    assert status != 0 and "is not in the sandbox" in err, err


def test_pandocpy_the_data_directories(lab):
    """The user data directory and PANDOC_DATA_DIRS are not mounted by
    themselves: pandoc finds nothing there unless they are listed, and then
    they are readable and not writable."""
    dirs = str(lab.root / "dirs")
    templates = (("mine.html", "DATA-DIR-OK"), ("theirs.html", "DATA-DIRS-OK"))
    for template, ok in templates:
        status, out, err = pandocpy(lab, ["doc.md", "-s", "--template", template],
                                    env={**HERE, "PANDOC_DATA_DIRS": dirs})
        assert status != 0 and ok not in out, (template, out, err)
    listed = os.pathsep.join([".", str(lab.data / "pandoc"), dirs])
    env = {"LIBPANDOC_SANDBOX": listed, "PANDOC_DATA_DIRS": dirs}
    for template, ok in templates:
        status, out, err = pandocpy(lab, ["doc.md", "-s", "--template", template], env=env)
        assert status == 0 and ok in out, (template, out, err)
    targets = (named(lab.data / "pandoc/new.txt"), named(lab.root / "dirs/new.txt"))
    (lab.work / "write-data.lua").write_text(
        f"function Pandoc(d)\n  for _, f in ipairs({{'{targets[0]}', '{targets[1]}'}}) do\n"
        "    if io.open(f, 'w') then error('wrote ' .. f) end\n  end\nend\n")
    status, _, err = pandocpy(lab, ["doc.md", "-L", "write-data.lua"], env=env)
    assert status == 0, err
    assert not (lab.data / "pandoc/new.txt").exists() and not (lab.root / "dirs/new.txt").exists()


def test_pandocpy_source_date_epoch_makes_the_output_reproducible(lab):
    env = {**HERE, "LIBPANDOC_SANDBOX_WRITE": str(lab.out), "SOURCE_DATE_EPOCH": "1700000000"}
    for ext in ("docx", "odt"):
        made = []
        for n in range(2):
            status, _, err = pandocpy(lab, ["doc.md", "-o", named(lab.out / f"dated-{n}.{ext}")], env=env)
            assert status == 0, err
            made.append((lab.out / f"dated-{n}.{ext}").read_bytes())
            time.sleep(1.1)
        assert made[0] == made[1], f"{ext}: two runs differ"


def test_pandocpy_refuses_what_it_cant_confine(lab):
    status, _, err = pandocpy(lab, ["-t", "plain"], "x", {"LIBPANDOC_SANDBOX": "no-such-dir"})
    assert status == 1 and "no-such-dir" in err, err
    # a program: a JSON filter, a PDF engine
    status, _, err = pandocpy(lab, ["-t", "plain", "-F", "some-program"], "x", HERE)
    assert status == 83 and "runs no programs" in err, err
    status, _, err = pandocpy(lab, ["-t", "pdf"], "x", HERE)
    assert status != 0 and "pdflatex" in err, err
    # pandoc's subcommands aren't in the module
    status, _, err = pandocpy(lab, ["lua", "-e", "print(1)"], env=HERE)
    assert status == 4, err


def test_pandocpy_python_filter_scripts_run_here(lab):
    """A Python filter script is the user's code, as the command line is: it
    runs in this process, as natively, unconfined."""
    outside = str(lab.outside / "input.md")
    (lab.work / "upper.py").write_text(
        "import json, sys\nd = json.load(sys.stdin)\n"
        f"d['blocks'] = [{{'t': 'Para', 'c': [{{'t': 'Str', 'c': open({outside!r}).read().strip()}}]}}]\n"
        "json.dump(d, sys.stdout)\n")
    status, out, err = pandocpy(lab, ["-t", "plain", "-F", "upper.py"], "x", HERE)
    assert (status, out.strip()) == (0, "INPUT-OK"), err


def test_pandocpy_pandocs_own_messages_and_statuses(lab):
    """The command is the module's pandoc: its messages, as the options ask,
    and its exit statuses, the native command's."""
    (lab.work / "warns.tex").write_text("\\input{../secret/secret}\n\nx\n")
    env = {**HERE, "LIBPANDOC_SANDBOX_WRITE": str(lab.out)}
    warning = "[WARNING] Could not load include file"
    status, _, err = pandocpy(lab, ["warns.tex", "-t", "plain"], env=env)
    assert status == 0 and warning in err, err
    status, _, err = pandocpy(lab, ["warns.tex", "-t", "plain", "--quiet"], env=env)
    assert status == 0 and "WARNING" not in err, err
    status, _, err = pandocpy(lab, ["warns.tex", "-s", "-t", "html", "--verbose"], env=env)
    assert status == 0 and "[INFO]" in err, err
    status, _, err = pandocpy(lab, ["warns.tex", "-t", "plain", "--log", named(lab.out / "warns.log")],
                              env=env)
    assert status == 0 and "CouldNotLoadIncludeFile" in (lab.out / "warns.log").read_text(), err
    status, _, err = pandocpy(lab, ["warns.tex", "-t", "plain", "--fail-if-warnings"], env=env)
    assert status == 3 and warning in err, err
    for args in (["missing.md"], ["-f", "nope", "doc.md"], ["-t", "nope", "doc.md"],
                 ["--no-such-option"]):
        native = pandocpy(lab, args)[0]
        status, _, err = pandocpy(lab, args, env=env)
        assert native != 0 and status == native, (args, native, status, err)


@needs_filters
def test_pandocpy_a_wasm_filter_on_the_command_line(lab):
    """It runs in this process, seeing its own directories
    (LIBPANDOC_WASM_DIRS's), not the mounts."""
    include, upper = (str(Path(FILTERS) / f) for f in ("include.wasm", "upper.wasm"))
    args = ["-t", "plain", "-F", include, "-F", upper]
    status, out, err = pandocpy(lab, args, "```include\ninside.tex\n```\n", HERE)
    assert status == 83 and "(it was given no directory: LIBPANDOC_WASM_DIRS)" in err, (out, err)
    env = {**HERE, "LIBPANDOC_WASM_DIRS": "."}
    status, out, err = pandocpy(lab, args, "```include\ninside.tex\n```\n", env)
    assert (status, out.strip()) == (0, "INSIDE-OK"), err
    secret = "```include\n../secret/secret.tex\n```\n"
    status, out, err = pandocpy(lab, ["-t", "plain", "-F", include], secret, env)
    assert status == 83 and "CANARY" not in out + err, (out, err)
    # the filter's directories, not the mounts: `secret`, given to the
    # filter alone, is read by it, though the sandbox lists only `.`
    env = {**HERE, "LIBPANDOC_WASM_DIRS": "../secret"}
    status, out, err = pandocpy(lab, ["-t", "plain", "-F", include], secret, env)
    assert (status, out.strip()) == (0, "CANARY-TEX-OUTSIDE"), err


def test_pandocpy_the_memory_limit_stops_a_loop(lab):
    """A string doubled without end is stopped by the memory limit (Typst's
    bare `#while true {}` is the time limit's: how fast its memory grows
    varies from machine to machine); within the same limit, an ordinary
    document converts."""
    env = {**HERE, "LIBPANDOC_WASM_MAX_MEMORY": "128m"}
    status, _, err = pandocpy(lab, ["-f", "typst", "-t", "plain"],
                              '#let s = "xxxxxxxx"\n#while true { s = s + s }\n', env)
    assert status != 0 and "out of memory" in err and "LIBPANDOC_WASM_MAX_MEMORY" in err, err
    status, out, err = pandocpy(lab, ["-f", "markdown", "-t", "html"], "*hi*", env)
    assert (status, out) == (0, "<p><em>hi</em></p>\n"), err


def test_the_sandboxs_memory_limit_names_itself(lab):
    """Out of memory, the message names where the limit was given, as
    libpandoc-rs's."""
    with pytest.raises(pandoc.PandocError, match='out of memory.*the sandbox\'s "max-memory"'):
        pandoc.convert('#let s = "xxxxxxxx"\n#while true { s = s + s }\n', from_="typst",
                       to="plain", sandbox={"read": [], "max-memory": 128 << 20, "timeout": 60})


def test_pandocpy_the_time_limit_stops_nested_quotes(lab):
    (lab.work / "quotes.md").write_text(">" * 100_000)
    status, _, err = pandocpy(lab, ["quotes.md", "-t", "html"], env={**HERE, "LIBPANDOC_WASM_TIMEOUT": "2"})
    after = re.search(r"stopped after ([0-9.]+) ", err)
    assert status != 0 and after and 2.0 <= float(after[1]) < 4.0, err
    status, out, err = pandocpy(lab, ["doc.md", "-t", "html"], env={**HERE, "LIBPANDOC_WASM_TIMEOUT": "10"})
    assert (status, out) == (0, "<p><em>hi</em></p>\n"), err


def test_pandocpy_a_large_standard_input(lab):
    """A document larger than a pipe holds, on standard input, read whole."""
    status, out, err = pandocpy(lab, ["-f", "markdown", "-t", "plain"],
                                "Some *words* here.\n\n" * 20_000, HERE)
    assert status == 0 and out.count("Some words here.") == 20_000, err


def test_pandocpy_without_wasmtime_it_is_refused(lab):
    code = ("import sys; sys.modules['wasmtime'] = None\n"
            "from libpandoc import cli; sys.exit(cli.main(sys.argv[1:]))")
    for var in ("LIBPANDOC_SANDBOX", "LIBPANDOC_SANDBOX_WRITE"):
        r = subprocess.run([sys.executable, "-c", code, "-t", "plain"], input=b"x", check=False,
                           capture_output=True, cwd=lab.work, env={**os.environ, var: "."}, timeout=60)
        assert r.returncode != 0 and r.stdout == b"" and b"wasmtime" in r.stderr, r
