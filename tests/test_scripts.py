"""pandocpy: Python filter scripts in its own process, and the opt-outs."""

import os
import textwrap

import pytest

from libpandoc import _scripts, cli

PLAIN = """\
import json, os, sys
doc = json.load(sys.stdin)
doc["blocks"].append({"t": "Para", "c": [{"t": "Str", "c":
    f"pid={os.getpid()}|{sys.argv[1]}|{os.environ.get('PANDOC_INPUT_FORMAT')}"}]})
json.dump(doc, sys.stdout)
"""

PANDOM = """\
import os
from pandom import Filter, Pandoc, Para

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


def test_a_pandom_script_hands_its_filter_over(tmp_path, md, capfd, monkeypatch):
    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "pd.py", PANDOM)
    status, out, err = pandocpy(capfd, "-f", "gfm", "-t", "html", "-F", s, md)
    assert status == 0, err
    assert f"pid={os.getpid()}|gfm" in out


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


def test_a_script_failing_in_process_runs_as_a_subprocess(tmp_path, md, capfd, monkeypatch):
    """Here it prints to stdout as well as the document, which only works
    with a process of its own... not really, but it fails in process."""
    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "fragile.py", "import sys\nif 'libpandoc' in sys.modules:\n"
               "    raise RuntimeError('not here')\n" + PLAIN)
    status, out, err = pandocpy(capfd, "-t", "html", "-F", s, md)
    assert status == 0, err
    assert "failed in process (RuntimeError: not here)" in err
    assert "pid=" in out and f"pid={os.getpid()}|" not in out


def test_a_script_failing_everywhere(tmp_path, md, capfd, monkeypatch):
    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "broken.py", "import sys\nsys.exit(3)\n")
    status, _, err = pandocpy(capfd, "-t", "html", "-F", s, md)
    assert status == cli.FILTER_FAILED
    assert "returned error status 3" in err


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


def test_convert_runs_a_pandom_script_knowing_the_options(tmp_path, monkeypatch):
    import libpandoc

    monkeypatch.delenv(_scripts.ENV, raising=False)
    s = script(tmp_path, "opts.py", """\
        from pandom import Filter, Pandoc, Para

        f = Filter()

        @f.on(Pandoc)
        def mark(doc, ctx):
            doc.blocks.append(Para(str(ctx.conversion.options.get("tab-stop"))))

        if __name__ == "__main__":
            f.main()
        """)
    assert "<p>7</p>" in libpandoc.convert("hi", to="html", tab_stop=7, filters=[s])


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
