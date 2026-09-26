"""pandocpy: pandoc's command line, installed Python filters in process."""

import os
import subprocess
import sys
import textwrap

import pytest

from libpandoc import cli


class FakeEntryPoint:
    def __init__(self, obj):
        self.obj = obj

    def load(self):
        return self.obj


def demo(doc):
    return doc


@pytest.mark.parametrize(
    "args, expected",
    [
        (["-F", "demo"], ["--lua-filter=libpandoc:callback/0"]),
        (["--filter", "demo"], ["--lua-filter=libpandoc:callback/0"]),
        (["--filter=demo"], ["--lua-filter=libpandoc:callback/0"]),
        (["-Fdemo"], ["--lua-filter=libpandoc:callback/0"]),
        (["-F", "other", "-t", "html"], ["-F", "other", "-t", "html"]),
        (["--filter=other.py"], ["--filter=other.py"]),
        (["-F"], ["-F"]),
    ],
)
def test_python_filters_are_named_as_callbacks(args, expected):
    argv, filters = cli.with_python_filters(args, {"demo": FakeEntryPoint(demo)})
    assert argv == expected
    assert filters == ([demo] if "demo" in "".join(args) else [])


def test_order_among_other_filters():
    argv, filters = cli.with_python_filters(
        ["-L", "a.lua", "-F", "demo", "--citeproc", "-F", "demo"],
        {"demo": FakeEntryPoint(demo)},
    )
    assert argv == [
        "-L", "a.lua",
        "--lua-filter=libpandoc:callback/0",
        "--citeproc",
        "--lua-filter=libpandoc:callback/1",
    ]
    assert filters == [demo, demo]


@pytest.fixture
def installed_filter(tmp_path):
    """A 'cells' filter installed as the pandom.filters entry point "cells":
    code blocks of class cells become their text, read as the document is."""
    site = tmp_path / "site"
    (site / "cells_filter-0.1.dist-info").mkdir(parents=True)
    (site / "cells_filter-0.1.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: cells-filter\nVersion: 0.1\n"
    )
    (site / "cells_filter-0.1.dist-info" / "entry_points.txt").write_text(
        "[pandom.filters]\ncells = cells_filter:f\n"
    )
    (site / "cells_filter.py").write_text(textwrap.dedent("""\
        import os
        from pandom import CodeBlock, Filter, Para, Str

        f = Filter()

        @f.on(CodeBlock)
        def cells(code, ctx):
            if "cells" in code.attr.classes:
                seen = Para(Str(f"{ctx.conversion.input_format}|{os.getpid()}"))
                return [*ctx.read(code.text), seen]
        """))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(site), *sys.path])}
    return env


def pandocpy(args, input, env):
    proc = subprocess.Popen(
        [sys.executable, "-m", "libpandoc", *args],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    out, err = proc.communicate(input.encode())
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err), proc.pid


def test_an_installed_filter_runs_in_process(installed_filter):
    md = "``` cells\n*a* ~~b~~\n```\n"
    proc, pid = pandocpy(["-f", "commonmark_x", "-t", "html", "-F", "cells"], md, installed_filter)
    assert proc.returncode == 0, proc.stderr.decode()
    lines = proc.stdout.decode().splitlines()
    # read as commonmark_x (strikeout), by pandoc in the filter's own process
    assert lines[0] == "<p><em>a</em> <del>b</del></p>"
    fmt, _, filter_pid = lines[1].removeprefix("<p>").removesuffix("</p>").partition("|")
    assert fmt == "commonmark_x"
    assert int(filter_pid) == pid  # pandocpy's own process


def test_help_lists_installed_filters(installed_filter):
    proc, _ = pandocpy(["--help"], "", installed_filter)
    assert proc.returncode == 0
    assert "Installed Python filters: cells" in proc.stdout.decode()


def test_informational_options():
    assert cli._informational(["--version"]).startswith("pandocpy")
    assert "+smart" in cli._informational(["--list-extensions=markdown"])
    assert "markdown" in cli._informational(["--list-input-formats"])
    assert "$body$" in cli._informational(["-D", "html"])
    assert cli._informational(["-t", "html"]) is None


def test_errors_exit_nonzero(capsys):
    assert cli.main(["-t", "nonesuch"]) == 1
    assert "nonesuch" in capsys.readouterr().err
