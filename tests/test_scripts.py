"""pandocpy: Python filter scripts in its own process, and the opt-outs."""

import os
import textwrap
import time

import pytest

import libpandoc
from libpandoc import _scripts, cli

PLAIN = """\
import json, os, sys
doc = json.load(sys.stdin)
doc["blocks"].append({"t": "Para", "c": [{"t": "Str", "c":
    f"pid={os.getpid()}|{sys.argv[1]}|{os.environ.get('PANDOC_INPUT_FORMAT')}"}]})
json.dump(doc, sys.stdout)
"""

PANIR = """\
import os
from panir import Filter, Pandoc, Para

f = Filter()

@f.on(Pandoc)
def mark(doc, ctx):
    doc.blocks.append(Para(f"pid={os.getpid()}|{ctx.conversion.input_format}"))

if __name__ == "__main__":
    f.main()
"""


def script(tmp_path, name, code):
    p = tmp_path / name
    p.write_text(textwrap.dedent(code))
    return str(p)


def pandocpy(capfd, *args):
    status = cli.main(list(args))
    out = capfd.readouterr()
    return status, out.out, out.err


@pytest.fixture
def md(tmp_path):
    p = tmp_path / "in.md"
    p.write_text("hi\n")
    return str(p)


def test_a_plain_json_script_runs_in_process(tmp_path, md, capfd, monkeypatch):
    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "plain.py", PLAIN)
    status, out, err = pandocpy(capfd, "-f", "commonmark_x", "-t", "html", "-F", s, md)
    assert status == 0, err
    assert f"pid={os.getpid()}|html|commonmark_x" in out


def test_a_panir_script_hands_its_filter_over(tmp_path, md, capfd, monkeypatch):
    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "pd.py", PANIR)
    status, out, err = pandocpy(capfd, "-f", "gfm", "-t", "html", "-F", s, md)
    assert status == 0, err
    assert f"pid={os.getpid()}|gfm" in out


TWO = """\
import os
import panir
from panir import Filter, Pandoc, Para

first, second = Filter(), Filter()

@first.on(Pandoc)
def one(doc):
    doc.blocks.append(Para("one"))

@second.on(Pandoc)
def two(doc):
    doc.blocks.append(Para(f"two|pid={os.getpid()}|after {len(doc.blocks)}"))

if __name__ == "__main__":
    panir.main([first, second])
"""


def test_a_panir_script_hands_over_each_of_its_filters(tmp_path, md, capfd, monkeypatch):
    """A script's main([f, g]): both run here, in order, as a Lua file's {f, g}."""
    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "two.py", TWO)
    status, out, err = pandocpy(capfd, "-t", "html", "-F", s, md)
    assert status == 0, err
    assert "<p>one</p>" in out and f"two|pid={os.getpid()}|after 2" in out


def test_the_author_opts_out(tmp_path, md, capfd, monkeypatch):
    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "own.py", "# pandocpy: subprocess\n" + PLAIN)
    assert _scripts.opted_out(s) == "# pandocpy: subprocess"
    status, out, err = pandocpy(capfd, "-t", "html", "-F", s, md)
    assert status == 0, err
    assert "pid=" in out and f"pid={os.getpid()}|" not in out


@pytest.mark.parametrize("value", ["plain2.py", "plain2", "*", "x,plain2.py"])
def test_the_user_opts_out(tmp_path, md, capfd, monkeypatch, value):
    s = script(tmp_path, "plain2.py", PLAIN)
    monkeypatch.setenv(_scripts.ENV, value)
    status, out, err = pandocpy(capfd, "-t", "html", "-F", s, md)
    assert status == 0, err
    assert "pid=" in out and f"pid={os.getpid()}|" not in out


COUNTED = """\
import json, os, sys
with open(os.path.join(os.path.dirname(__file__), "runs"), "a") as f:
    f.write("ran\\n")
"""


def test_a_script_failing_in_process_is_not_run_again(tmp_path, md, monkeypatch):
    """Its error is the conversion's (it was run again as a subprocess,
    doing twice what it did, its error then a warning)."""
    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "fragile.py", COUNTED + "raise RuntimeError('a bug')\n")
    with pytest.raises(_scripts.ScriptFailed, match="fragile.py failed .RuntimeError: a bug.") as e:
        libpandoc.convert(input_files=[md], filters=[s])
    assert isinstance(e.value.__cause__, RuntimeError)
    assert "# pandocpy: subprocess" in str(e.value)
    assert (tmp_path / "runs").read_text() == "ran\n"


def test_a_script_exiting_with_an_error(tmp_path, md, capfd, monkeypatch):
    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "broken.py", COUNTED + "print('broken: bad input', file=sys.stderr)\n"
               "sys.exit(3)\n")
    status, _, err = pandocpy(capfd, "-t", "html", "-F", s, md)
    assert status == cli.FILTER_FAILED
    assert "broken.py failed (it exited with 3)" in err
    assert err.count("broken: bad input") == 1 and (tmp_path / "runs").read_text() == "ran\n"


def test_a_script_writing_no_document(tmp_path, md, monkeypatch):
    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "quiet.py", "import sys\nsys.stdin.read()\n")
    with pytest.raises(_scripts.ScriptFailed, match="it wrote no document"):
        libpandoc.convert(input_files=[md], filters=[s])


def test_a_script_changing_the_working_directory(tmp_path, md, capfd, monkeypatch):
    """The output file is written where it was named, as with pandoc,
    where the filter's process has its own working directory."""
    monkeypatch.delenv(_scripts.ENV, raising=False)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path)
    s = script(tmp_path, "cd.py", "import os\nos.chdir('elsewhere')\n" + PLAIN)
    status, _, err = pandocpy(capfd, "-F", s, md, "-o", "out.html")
    assert status == 0, err
    assert (tmp_path / "out.html").exists() and not (tmp_path / "elsewhere" / "out.html").exists()
    assert os.getcwd() == str(tmp_path)


def test_a_subprocess_script_is_stopped_by_the_time_limit(tmp_path, md, monkeypatch):
    """As libpandoc stops a JSON filter (a script in process runs to its
    end: Python can't be stopped from outside)."""
    s = script(tmp_path, "slow.py", "# pandocpy: subprocess\nimport time\ntime.sleep(60)\n")
    started = time.monotonic()
    with pytest.raises(libpandoc.PandocTimeoutError, match="slow.py stopped after 0.5 s"):
        libpandoc.convert(input_files=[md], filters=[s], timeout=0.5)
    assert time.monotonic() - started < 30


def test_panflute_script(tmp_path, md, capfd, monkeypatch):
    pytest.importorskip("panflute")
    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "pf.py", """\
        import os
        import panflute as pf

        def action(elem, doc):
            if isinstance(elem, pf.Str):
                return pf.Str(elem.text.upper())

        def finalize(doc):
            doc.content.append(pf.Para(pf.Str(f"pid={os.getpid()}")))

        def main(doc=None):
            return pf.run_filter(action, finalize=finalize, doc=doc)

        if __name__ == "__main__":
            main()
        """)
    status, out, err = pandocpy(capfd, "-t", "html", "-F", s, md)
    assert status == 0, err
    assert "<p>HI</p>" in out and f"pid={os.getpid()}" in out


def test_pandocfilters_script(tmp_path, md, capfd, monkeypatch):
    pytest.importorskip("pandocfilters")
    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "pdf.py", """\
        import os
        from pandocfilters import Str, toJSONFilter

        def caps(key, value, format, meta):
            if key == "Str":
                return Str(value.upper() + f"@{os.getpid()}")

        if __name__ == "__main__":
            toJSONFilter(caps)
        """)
    status, out, err = pandocpy(capfd, "-t", "html", "-F", s, md)
    assert status == 0, err
    assert f"<p>HI@{os.getpid()}</p>" in out


# In a library: libpandoc.convert(filters=["foo.py"]), from several threads


def test_convert_runs_a_script_in_process(tmp_path, monkeypatch):
    import libpandoc

    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "plain.py", PLAIN)
    out = libpandoc.convert("hi", from_="commonmark_x", to="html", filters=[s])
    assert f"pid={os.getpid()}|html|commonmark_x" in out


def test_convert_runs_a_panir_script_knowing_the_options(tmp_path, monkeypatch):
    import libpandoc

    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "opts.py", """\
        from panir import Filter, Pandoc, Para

        f = Filter()

        @f.on(Pandoc)
        def mark(doc, ctx):
            doc.blocks.append(Para(str(ctx.conversion.options.get("tab-stop"))))

        if __name__ == "__main__":
            f.main()
        """)
    assert "<p>7</p>" in libpandoc.convert("hi", to="html", tab_stop=7, filters=[s])


WRITER = """\
import json, os, sys
doc = json.load(sys.stdin)
w = json.loads(os.environ["PANDOC_WRITER_OPTIONS"])
doc["blocks"].append({"t": "Para", "c": [{"t": "Str", "c":
    f"pid={os.getpid()}|{w['number-sections']}|{w['top-level-division']}"}]})
json.dump(doc, sys.stdout)
"""


@pytest.mark.parametrize("where", ["in process", "subprocess"])
def test_a_script_is_told_the_writer_options(tmp_path, md, capfd, monkeypatch, where):
    """PANDOC_WRITER_OPTIONS, as libpandoc gives the JSON filters it runs."""
    monkeypatch.setenv(_scripts.ENV, "*" if where == "subprocess" else "")
    s = script(tmp_path, "writer.py", WRITER)
    status, out, err = pandocpy(capfd, "-t", "html", "-N", "--top-level-division=part",
                                "-F", s, md)
    assert status == 0, err
    assert "|True|top-level-part" in out
    assert (f"pid={os.getpid()}|" in out) == (where == "in process")


def test_scripts_in_parallel_threads(tmp_path, monkeypatch):
    """Each conversion's script sees its own document, format and
    environment, and the process's own streams are left alone."""
    import sys
    from concurrent.futures import ThreadPoolExecutor

    import libpandoc

    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "echo.py", """\
        import json, os, sys, time
        doc = json.load(sys.stdin)
        time.sleep(0.01)  # overlap with the other threads
        word = doc["blocks"][0]["c"][0]["c"]
        doc["blocks"].append({"t": "Para", "c": [{"t": "Str", "c":
            f"{word}|{sys.argv[1]}|{os.environ['PANDOC_INPUT_FORMAT']}"}]})
        json.dump(doc, sys.stdout)
        """)
    real = sys.stdout, sys.stdin, sys.argv, os.environ
    formats = ["markdown", "commonmark_x", "gfm", "commonmark"]

    def one(i):
        fmt = formats[i % 4]
        out = libpandoc.convert(f"w{i}", from_=fmt, to="html", filters=[s])
        return out, f"w{i}|html|{fmt}"

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(one, range(40)))
    assert all(expected in out for out, expected in results)
    assert (sys.stdout, sys.stdin, sys.argv, os.environ) == real


# A script of several files: what it imports from beside it, as python script.py

USES_UTIL = """\
import json, os, sys
import util
doc = json.load(sys.stdin)
doc["blocks"].append({"t": "Para", "c": [{"t": "Str", "c": f"{util.WORD}|pid={os.getpid()}"}]})
json.dump(doc, sys.stdout)
"""


def folder_with(tmp_path, name, files):
    d = tmp_path / name
    for rel, code in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(textwrap.dedent(code))
    return d


def test_two_scripts_each_with_a_module_beside_it_of_the_same_name(tmp_path, monkeypatch):
    """Each imports its own util.py, in process, one after the other; after,
    neither folder is on sys.path, and no util is left in sys.modules."""
    import sys

    import libpandoc

    monkeypatch.delenv(_scripts.ENV, raising=False)
    a = folder_with(tmp_path, "a", {"f.py": USES_UTIL, "util.py": "WORD = 'from-a'\n"})
    b = folder_with(tmp_path, "b", {"f.py": USES_UTIL, "util.py": "WORD = 'from-b'\n"})
    path = list(sys.path)
    out = libpandoc.convert("hi", to="html", filters=[str(a / "f.py"), str(b / "f.py")])
    pid = f"pid={os.getpid()}"  # (both in process: neither fell back to a subprocess)
    assert f"<p>from-a|{pid}</p>\n<p>from-b|{pid}</p>" in out
    assert sys.path == path and "util" not in sys.modules


def test_a_script_importing_a_package_beside_it(tmp_path, monkeypatch):
    import sys

    import libpandoc

    monkeypatch.delenv(_scripts.ENV, raising=False)
    d = folder_with(tmp_path, "p", {
        "f.py": """\
            import json, os, sys
            from helpers.words import WORD
            doc = json.load(sys.stdin)
            doc["blocks"].append({"t": "Para", "c": [{"t": "Str", "c": f"{WORD}|pid={os.getpid()}"}]})
            json.dump(doc, sys.stdout)
            """,
        "helpers/__init__.py": "",
        "helpers/words.py": "WORD = 'packaged'\n",
    })
    out = libpandoc.convert("hi", to="html", filters=[str(d / "f.py")])
    assert f"<p>packaged|pid={os.getpid()}</p>" in out
    assert not any(m == "helpers" or m.startswith("helpers.") for m in sys.modules)


def test_the_path_is_restored_after_the_script_fails(tmp_path, monkeypatch):
    """A script that imports from beside it, then raises: sys.path and
    sys.modules as they were (the subprocess it falls back to is another
    matter: here, the run in process alone)."""
    import sys

    monkeypatch.delenv(_scripts.ENV, raising=False)
    d = folder_with(tmp_path, "e", {"f.py": "import util\nraise RuntimeError(util.WORD)\n",
                                    "util.py": "WORD = 'boom'\n"})
    path = list(sys.path)
    with pytest.raises(RuntimeError, match="boom"):
        _scripts._in_process(str(d / "f.py"), lambda: b"{}", "html", {})
    assert sys.path == path and "util" not in sys.modules


def test_a_script_sees_what_python_script_py_gives_it(tmp_path, monkeypatch):
    """__file__ absolute, sys.path[0] its folder (symbolic links resolved),
    __name__ and __spec__, the working directory, as python script.py has
    them; sys.argv[0] absolute too (python script.py: as given)."""
    import json

    monkeypatch.delenv(_scripts.ENV, raising=False)
    real = folder_with(tmp_path, "real", {"f.py": """\
        import json, os, sys
        doc = json.load(sys.stdin)
        seen = [__file__, sys.argv[0], sys.path[0], __name__, repr(__spec__), os.getcwd()]
        doc["blocks"].append({"t": "Para", "c": [{"t": "Str", "c": json.dumps(seen)}]})
        json.dump(doc, sys.stdout)
        """})
    (tmp_path / "link").symlink_to(real)
    monkeypatch.chdir(tmp_path)
    doc = b'{"pandoc-api-version":[1,23,1,2],"meta":{},"blocks":[]}'
    out = _scripts._in_process("link/f.py", lambda: doc, "html", {})
    seen = json.loads(json.loads(out)["blocks"][-1]["c"][0]["c"])
    path = str(tmp_path / "link/f.py")
    assert seen == [path, path, os.path.realpath(real), "__main__", "None", str(tmp_path)]
