"""The generated AST codecs reproduce pandoc's JSON exactly."""

import json
from pathlib import Path

import pytest

import libpandoc as pandoc
from libpandoc import ast

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


def pandoc_json(name: str) -> dict:
    out = pandoc.convert((DATA / name).read_bytes(), from_=INPUTS[name], to="json")
    return json.loads(out)


@pytest.mark.parametrize("name", INPUTS)
def test_json_round_trip(name):
    j = pandoc_json(name)
    doc = ast.from_json(j)
    assert isinstance(doc, ast.Pandoc)
    assert ast.to_json(doc) == j


@pytest.mark.parametrize("name", INPUTS)
def test_read_write_matches_convert(name):
    source = (DATA / name).read_bytes()
    doc = pandoc.read(source, INPUTS[name])
    assert pandoc.write(doc, "native") == pandoc.convert(source, from_=INPUTS[name], to="native")


def test_every_constructor_is_exercised():
    seen: set[type] = set()

    def visit(node):
        seen.add(type(node))

    for name in INPUTS:
        pandoc.walk(ast.from_json(pandoc_json(name)), visit)
    constructors = {
        cls for cls in vars(ast).values()
        if isinstance(cls, type) and issubclass(cls, (ast.Block, ast.Inline)) and cls
        not in (ast.Block, ast.Inline)
    }
    assert constructors <= seen, constructors - seen


def test_node_round_trip():
    attr = ast.Attr("id", ["a"], [("k", "v")])
    h = ast.Header(2, attr, [ast.Str("x"), ast.Space(), ast.Emph([ast.Str("y")])])
    assert ast.Header.from_json(h.to_json()) == h
    assert h.to_json() == {
        "t": "Header",
        "c": [2, ["id", ["a"], [["k", "v"]]], [
            {"t": "Str", "c": "x"}, {"t": "Space"}, {"t": "Emph", "c": [{"t": "Str", "c": "y"}]}
        ]],
    }


def test_incompatible_api_version():
    with pytest.raises(ValueError, match="incompatible"):
        ast.from_json({"pandoc-api-version": [1, 22], "meta": {}, "blocks": []})
