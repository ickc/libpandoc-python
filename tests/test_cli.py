"""pandocpy: the pandoc command in process, installed Python filters in it."""

import os
import shutil
import subprocess
import sys
import textwrap

import pytest

import libpandoc as pandoc
from libpandoc import cli


class FakeEntryPoint:
    def __init__(self, obj):
        self.obj = obj

    def load(self):
        return self.obj


def demo(doc):
    return doc


def test_plan_groups_consecutive_python_filters():
    filters = pandoc.query("parse-args", args=["-L", "a.lua", "-F", "demo", "-F", "demo",
                                               "--citeproc", "-F", "demo", "-F", "other"])
    entries, callbacks = cli.plan(filters["filters"], {"demo": FakeEntryPoint(demo)})
    assert entries == [
        {"type": "lua", "path": "a.lua"},
        {"type": "callback", "index": 0},
        {"type": "citeproc"},
        {"type": "callback", "index": 1},
        {"type": "json", "path": "other"},
    ]
    assert len(callbacks) == 2


def test_pandoc_parses_the_arguments(tmp_path):
    """Abbreviated options and defaults files, as pandoc reads them."""
    d = tmp_path / "d.yaml"
    d.write_text("filters: [demo, x.lua]\n")
    parsed = pandoc.query("parse-args", args=["--filt", "a", "-d", str(d)])
    assert parsed == {"filters": [
        {"type": "json", "path": "a"},
        {"type": "json", "path": "demo"},
        {"type": "lua", "path": "x.lua"},
    ]}
    assert pandoc.query("parse-args", args=["-t", "html", "--version"]) == {
        "informational": "VersionInfo"}


@pytest.fixture
def site(tmp_path):
    """A 'cells' filter installed as the panir.filters entry point "cells":
    code blocks of class cells become their lines, read as the document is."""
    site = tmp_path / "site"
    info = site / "cells_filter-0.1.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: cells-filter\nVersion: 0.1\n")
    (info / "entry_points.txt").write_text("[panir.filters]\ncells = cells_filter:f\n")
    (site / "cells_filter.py").write_text(textwrap.dedent("""\
        import os
        import libpandoc
        from panir import CodeBlock, Filter, Para, Str

        f = Filter()

        @f.on(CodeBlock)
        def cells(code, ctx):
            if "cells" in code.attr.classes:
                docs = libpandoc.read_many(code.text.splitlines(), ctx.conversion)
                seen = Para(Str(f"{ctx.conversion.input_format}|{os.getpid()}"))
                return [*(b for d in docs for b in d.blocks), seen]
        """))
    return site


def run(args, input, site=None):
    env = dict(os.environ)
    if site is not None:
        env["PYTHONPATH"] = os.pathsep.join([str(site), *sys.path])
    return subprocess.run([sys.executable, "-m", "libpandoc", *args],
                          input=input.encode(), capture_output=True, env=env, check=False)


def test_an_installed_filter_runs_in_process(site):
    md = "``` cells\n*a*\n~~b~~\n```\n"
    proc = run(["-f", "commonmark_x", "-t", "html", "-F", "cells"], md, site)
    assert proc.returncode == 0, proc.stderr.decode()
    lines = proc.stdout.decode().splitlines()
    assert lines[:2] == ["<p><em>a</em></p>", "<p><del>b</del></p>"]
    assert lines[2].startswith("<p>commonmark_x|")


def test_a_filter_in_a_defaults_file(site, tmp_path):
    d = tmp_path / "d.yaml"
    d.write_text("from: commonmark_x\nto: html\nfilters: [cells]\n")
    proc = run(["-d", str(d)], "``` cells\n*a*\n```\n", site)
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout.decode().startswith("<p><em>a</em></p>")


def test_filters_run_in_this_process(site, tmp_path, capfd, monkeypatch):
    monkeypatch.syspath_prepend(str(site))
    src = tmp_path / "in.md"
    src.write_text("``` cells\nx\n```\n")
    assert cli.main(["-t", "html", "-F", "cells", str(src)]) == 0
    assert f"|{os.getpid()}</p>" in capfd.readouterr().out


def test_help_lists_installed_filters(site):
    proc = run(["--help"], "", site)
    assert proc.returncode == 0
    out = proc.stdout.decode()
    assert "--filter" in out
    assert "run in process by -F NAME: cells" in out


def test_a_filters_exception(tmp_path, monkeypatch, capfd):
    def boom(doc):
        raise RuntimeError("boom in the filter")

    monkeypatch.setattr(cli, "installed_filters", lambda: {"boom": FakeEntryPoint(boom)})
    src = tmp_path / "in.md"
    src.write_text("x")
    assert cli.main(["-F", "boom", str(src)]) == cli.FILTER_FAILED
    err = capfd.readouterr().err
    assert "boom in the filter" in err and "Traceback" in err


def test_pandoc_lua(tmp_path, capfd):
    script = tmp_path / "s.lua"
    script.write_text('print(table.concat(arg, ","))\n'
                      'print(pandoc.write(pandoc.read("*hi*"), "html"))\n')
    assert cli.main(["lua", str(script), "a", "b"]) == 0
    assert capfd.readouterr().out == "a,b\n<p><em>hi</em></p>\n"
    assert cli.main(["lua", "-e", "error('on purpose')"]) == 84
    assert "on purpose" in capfd.readouterr().err
    # as pandoc's, but for build hashes in the backtrace
    assert cli.main(["lua", "--no-such-option"]) == 1
    assert capfd.readouterr().err.replace("\r\n", "\n").startswith(
        "pandocpy: user error (unrecognized option `--no-such-option'\n"
        "Usage: pandocpy lua [options] [script [args]]\n")


def test_pandoc_server_is_unsupported(capfd):
    assert cli.main(["server"]) == 4
    assert "Server mode unsupported" in capfd.readouterr().err


def _same_pandoc():
    exe = shutil.which("pandoc")
    if exe is None:
        return False
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, check=False).stdout
    return out.split()[1] == pandoc.pandoc_version()


@pytest.mark.skipif(not _same_pandoc(), reason="needs the pandoc command of the same version")
@pytest.mark.parametrize("args, input", [
    (["-t", "nonesuch"], "x"),
    (["-f", "nonesuch"], "x"),
    (["--no-such-option"], "x"),
    (["-t", "latex", "-s", "--toc", "-V", "documentclass=book"], "# A\n\n# B\n"),
    (["--list-input-formats"], ""),
    (["--list-extensions=gfm"], ""),
    (["-D", "html"], ""),
    (["-t", "html", "-M", "title=T", "-s", "--metadata=lang:de"], "x\n"),
    (["-t", "native", "--columns=20", "-t", "plain"], "a b c d e f g h i j k l m\n"),
    (["lua", "-e", "print(pandoc.write(pandoc.read('*x*'), 'html'))"], ""),
    (["lua", "-", "a"], "print(#arg, arg[1])\n"),
    (["lua", "-e", "error('x')"], ""),
])
def test_same_as_pandoc(args, input):
    """Output, errors and exit status as the pandoc command's."""
    ours = run(args, input)
    theirs = subprocess.run(["pandoc", *args], input=input.encode(), capture_output=True,
                            check=False)
    assert (ours.returncode, ours.stdout, ours.stderr.replace(b"pandocpy", b"pandoc")) == (
        theirs.returncode, theirs.stdout, theirs.stderr)
