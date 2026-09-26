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
    # as with pandoc: citeproc=True runs citeproc before the filters, and
    # "citeproc" among the filters runs it there
    out = pandoc.convert(src, to="plain", citeproc=True, metadata=bib, filters=[upper_filter()])
    assert "(DOE 2000)" in out
    out = pandoc.convert(src, to="plain", metadata=bib, filters=[upper_filter(), "citeproc"])
    assert "(Doe 2000)" in out


def test_python_filter_to_a_file(tmp_path):
    dst = tmp_path / "out.html"
    assert pandoc.convert("x", output_file=dst, filters=[upper_filter()]) == ""
    assert dst.read_text() == "<p>X</p>\n"


def test_read_and_write():
    doc = pandoc.read("Hello *world*")
    assert doc == pandom.Pandoc(pandom.Para(pandom.Str("Hello"), pandom.Space(), pandom.Emph(pandom.Str("world"))))
    assert pandoc.write(upper_filter()(doc), "plain") == "HELLO WORLD\n"


# Python filters run inside the one pandoc conversion (libpandoc callbacks)

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def test_media_in_the_input_survive_a_python_filter(tmp_path):
    """An image embedded in a docx reaches the writer through a Python
    filter, as through a Lua one: it's one pandoc run."""
    import zipfile

    (tmp_path / "dot.png").write_bytes(PNG)
    docx = pandoc.convert("![a dot](dot.png)", to="docx", resource_path=[str(tmp_path)])
    src = tmp_path / "in.docx"
    src.write_bytes(docx)
    seen = []
    out = pandoc.convert(
        options={"input-files": [str(src)], "to": "docx"},
        filters=[lambda doc: seen.append(pandom.stringify(doc))],
    )
    media = [n for n in zipfile.ZipFile(__import__("io").BytesIO(out)).namelist()
             if n.startswith("word/media/")]
    assert len(seen) == 1 and "a dot" in seen[0]
    assert len(media) == 1


def test_a_python_filters_exception_is_raised_as_it_is():
    class Boom(Exception):
        pass

    f = pandom.Filter()

    @f.on(pandom.Str)
    def explode(s):
        raise Boom("in the filter")

    with pytest.raises(Boom, match="in the filter") as info:
        pandoc.convert("*hi*", to="html", filters=[f])
    assert any(tb.name == "explode" for tb in info.traceback)


def test_a_python_filter_can_call_pandoc():
    """Nested conversions, from inside a conversion."""
    f = pandom.Filter()

    @f.on(pandom.Code)
    def render(code):
        return pandom.RawInline("html", pandoc.convert(code.text, to="html").strip()[3:-4])

    assert pandoc.convert("`*x*`", to="html", filters=[f]) == "<p><em>x</em></p>\n"


def test_a_python_filter_runs_on_the_calling_thread():
    import threading

    threads = []
    pandoc.convert("hi", to="html", filters=[lambda doc: threads.append(threading.get_ident())])
    assert threads == [threading.get_ident()]


def test_ctx_read_uses_the_reader_pandoc_decided_on(tmp_path):
    """The context knows the input format (from the file name here), and
    ctx.read parses fragments with it."""
    src = tmp_path / "in.rst"
    src.write_text(".. code::\n\n   *emph* and ``code``\n")
    seen = []
    f = pandom.Filter()

    @f.on(pandom.CodeBlock)
    def cell(code, ctx):
        seen.append((ctx.conversion.input_format, ctx.conversion.output_format, ctx.format))
        return ctx.read(code.text)

    out = pandoc.convert(options={"input-files": [str(src)], "to": "html5+smart"}, filters=[f])
    assert seen == [("rst", "html5+smart", "html5")]
    assert out == "<p><em>emph</em> and <code>code</code></p>\n"


def test_a_function_filter_may_take_the_conversion():
    seen = []
    pandoc.convert("hi", from_="commonmark_x", to="latex",
                   filters=[lambda doc, conversion: seen.append(conversion.input_format)])
    assert seen == ["commonmark_x"]


def test_a_filter_must_return_a_document():
    with pytest.raises(TypeError, match="not a Pandoc"):
        pandoc.convert("hi", to="html", filters=[lambda doc: 42])
