"""The high-level API behaves like the pandoc CLI."""

import sys
import sysconfig
import threading

import pandom
import pytest

import libpandoc as pandoc


def test_versions():
    assert pandoc.pandoc_version().startswith("3.")
    assert pandoc.pandoc_api_version()[:2] == pandom.PANDOC_API_VERSION[:2]


@pytest.mark.skipif(not sysconfig.get_config_var("Py_GIL_DISABLED"),
                    reason="free-threaded CPython only")
def test_free_threaded_python_stays_so():
    """Importing libpandoc (and pandom) doesn't turn the GIL back on."""
    assert not sys._is_gil_enabled()


def test_convert_text():
    assert pandoc.convert("*hi*", from_="markdown", to="html") == "<p><em>hi</em></p>\n"


def test_convert_defaults_to_markdown_and_html():
    assert pandoc.convert("*hi*") == "<p><em>hi</em></p>\n"


def test_convert_options_dict_and_kwargs():
    out = pandoc.convert("# A\n\n# B", {"to": "html"}, number_sections=True, standalone=True,
                         metadata={"title": "T"})
    assert "<title>T</title>" in out and 'data-number="1"' in out


def test_convert_binary():
    out = pandoc.convert("x", to="docx")
    assert isinstance(out, bytes) and out[:2] == b"PK"


def test_convert_files(tmp_path):
    src = tmp_path / "in.md"
    src.write_text("*x*")
    dst = tmp_path / "out.html"
    assert pandoc.convert(input_files=[src], output_file=dst) == ""
    assert dst.read_text() == "<p><em>x</em></p>\n"


def test_run_is_the_cli(tmp_path):
    assert pandoc.run(["-f", "markdown", "-t", "latex"], input="*hi*") == b"\\emph{hi}\n"
    (tmp_path / "d.yaml").write_text("to: rst\n")
    assert pandoc.run(["--defaults", str(tmp_path / "d.yaml")], input="*hi*") == b"*hi*\n"


def test_run_rejects_informational_options():
    with pytest.raises(pandoc.PandocError, match="informational"):
        pandoc.run(["--version"])


def test_errors_are_typed():
    with pytest.raises(pandoc.PandocError) as e:
        pandoc.convert("x", from_="nonesuch")
    assert e.value.kind == "PandocUnknownReaderError"
    with pytest.raises(pandoc.PandocError) as e:
        pandoc.convert("x", {"not-an-option": 1})
    assert e.value.kind == "PandocOptionError" or "not-an-option" in str(e.value)


def test_warnings():
    with pytest.warns(pandoc.PandocWarning, match="CouldNotFetchResource"):
        pandoc.convert("![](does-not-exist.png)", to="docx")


def test_lua_filter(tmp_path):
    f = tmp_path / "upper.lua"
    f.write_text("function Str(s) return pandoc.Str(s.text:upper()) end\n")
    assert pandoc.convert("hi", to="plain", filters=[str(f)]) == "HI\n"


def test_queries():
    assert "markdown" in pandoc.input_formats()
    assert "docx" in pandoc.output_formats()
    assert pandoc.extensions("markdown")["smart"] is True
    assert "$body$" in pandoc.default_template("html")


def test_threads():
    results = {}

    def work(i):
        results[i] = pandoc.convert(f"*{i}*", to="html")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == {i: f"<p><em>{i}</em></p>\n" for i in range(16)}


def test_num_threads():
    n = pandoc.num_threads()
    assert n >= 1
    try:
        assert pandoc.set_num_threads(2) == 2
        assert pandoc.num_threads() == 2
        assert pandoc.convert("*x*") == "<p><em>x</em></p>\n"
        assert pandoc.set_num_threads(0) == 1
    finally:
        pandoc.set_num_threads(n)


def test_num_threads_from_the_environment():
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c", "import libpandoc; print(libpandoc.num_threads())"],
        env={**__import__("os").environ, "LIBPANDOC_NUM_THREADS": "3"},
        capture_output=True, text=True, check=True,
    ).stdout
    assert out.strip() == "3"
