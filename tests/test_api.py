"""The high-level API behaves like the pandoc CLI."""

import threading

import pytest

import libpandoc as pandoc
from libpandoc import ast


def test_versions():
    assert pandoc.pandoc_version().startswith("3.")
    assert pandoc.pandoc_api_version()[:2] == ast.PANDOC_API_VERSION[:2]


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
