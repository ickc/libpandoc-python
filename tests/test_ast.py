"""libpandoc with pandom: reading, writing, Python filters."""

import json
from pathlib import Path

import pandom
import pytest

import libpandoc as pandoc

DATA = Path(__file__).parent / "data"
INPUTS = {
    "testsuite.txt": "markdown",
    "tables.txt": "markdown",
    "pipe-tables.txt": "markdown",
    "markdown-citations.txt": "markdown",
    "html-reader.html": "html",
    "latex-reader.latex": "latex",
    "extras.md": "markdown",
}


@pytest.mark.parametrize("name", INPUTS)
def test_json_round_trip(name):
    out = pandoc.convert((DATA / name).read_bytes(), from_=INPUTS[name], to="json")
    j = json.loads(out)
    assert pandom.Pandoc.from_json(j).to_json() == j


@pytest.mark.parametrize("name", INPUTS)
def test_read_write_matches_convert(name):
    source = (DATA / name).read_bytes()
    doc = pandoc.read(source, INPUTS[name])
    assert pandoc.write(doc, "native") == pandoc.convert(source, from_=INPUTS[name], to="native")


def upper_filter():
    f = pandom.Filter()

    @f.on(pandom.Str)
    def upper(s):
        s.text = s.text.upper()

    return f


def test_python_filter():
    assert pandoc.convert("hello *world*", to="plain", filters=[upper_filter()]) == "HELLO WORLD\n"


def test_python_filter_sees_the_output_format():
    f = pandom.Filter()

    @f.on(pandom.Str)
    def tag(s, ctx):
        return pandom.Str(f"{s.text}@{ctx.format}")

    assert pandoc.convert("x", to="plain", filters=[f]) == "x@plain\n"


def test_function_as_filter():
    def number(doc):
        doc.blocks.insert(0, pandom.Para(pandom.Str("first")))

    assert pandoc.convert("x", to="plain", filters=[number]) == "first\n\nx\n"


def test_python_and_lua_filters_in_order(tmp_path):
    lua = tmp_path / "exclaim.lua"
    lua.write_text('function Str(s) return pandoc.Str(s.text .. "!") end\n')
    f = upper_filter()
    assert pandoc.convert("a", to="plain", filters=[str(lua), f]) == "A!\n"
    # upper before exclaim; then exclaim twice: before and after
    assert pandoc.convert("a", to="plain", filters=[f, str(lua)]) == "A!\n"
    assert pandoc.convert("a", to="plain", filters=[str(lua), f, str(lua)]) == "A!!\n"


def test_reading_options_apply_once():
    f = pandom.Filter()

    @f.on(pandom.Pandoc)
    def retitle(doc):
        doc.meta["title"] = "from the filter"

    out = pandoc.convert("# H", to="html", standalone=True, shift_heading_level_by=1,
                         metadata={"title": "from options"}, filters=[f])
    assert "<h2" in out and "<h3" not in out
    assert "<title>from the filter</title>" in out


def test_python_filter_with_citeproc():
    src = "[@doe]\n\n# References\n"
    bib = {"references": [{"id": "doe", "type": "book", "author": [{"family": "Doe"}],
                           "title": "T", "issued": {"date-parts": [[2000]]}}]}
    out = pandoc.convert(src, to="plain", citeproc=True, metadata=bib, filters=[upper_filter()])
    # citeproc runs after the filter: its output isn't upper-cased
    assert "(Doe 2000)" in out


def test_python_filter_to_a_file(tmp_path):
    dst = tmp_path / "out.html"
    assert pandoc.convert("x", output_file=dst, filters=[upper_filter()]) == ""
    assert dst.read_text() == "<p>X</p>\n"


def test_read_and_write():
    doc = pandoc.read("Hello *world*")
    assert doc == pandom.Pandoc(pandom.Para(pandom.Str("Hello"), pandom.Space(), pandom.Emph(pandom.Str("world"))))
    assert pandoc.write(upper_filter()(doc), "plain") == "HELLO WORLD\n"
