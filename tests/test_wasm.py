"""Wasm filters: libpandoc-rs's example filters (filters/, built for
wasm32-wasip1), given as $LIBPANDOC_WASM_FILTERS, run by wasmtime."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

import libpandoc as pandoc
from libpandoc import WasmFilter
from libpandoc._wasmfilter import WasmFilterError, parse_memory, parse_timeout

pytest.importorskip("wasmtime")
WASM = os.environ.get("LIBPANDOC_WASM_FILTERS", "")
needs_filters = pytest.mark.skipif(
    not WASM, reason="set LIBPANDOC_WASM_FILTERS to libpandoc-rs's filters built for wasm32-wasip1"
)


def wasm(name: str) -> str:
    return str(Path(WASM) / f"{name}.wasm")


def failure(f, text: str) -> str | None:
    try:
        pandoc.convert(text, to="plain", filters=[f])
    except WasmFilterError as e:
        return str(e)
    return None


def call(*requests):
    text = "\n\n".join(f"```call\n{json.dumps(r)}\n```" for r in requests)
    doc = json.loads(pandoc.convert(text, to="json", filters=[wasm("calls")]))
    return [json.loads(b["c"][1]) for b in doc["blocks"]]


def test_not_a_wasm_file(tmp_path):
    bad = tmp_path / "bad.wasm"
    bad.write_text("not wasm")
    with pytest.raises(ValueError, match="not a wasm filter"):
        WasmFilter(bad)


@needs_filters
def test_a_wasm_filter_runs_in_the_conversion():
    assert pandoc.convert("hello *world*", to="plain", filters=[wasm("upper")]) == "HELLO WORLD\n"
    assert pandoc.convert("hi", to="plain", filters=[WasmFilter(wasm("upper"))]) == "HI\n"


@needs_filters
def test_it_is_told_the_conversion():
    out = json.loads(pandoc.convert("x", from_="commonmark_x", to="json",
                                    filters=[wasm("conversion")]))
    told = json.loads(out["blocks"][-1]["c"][1])
    assert told["format"] == "json"
    assert told["input-format"].startswith("commonmark_x")
    assert told["pandoc-version"] == pandoc.pandoc_version()


@needs_filters
def test_a_filter_calls_pandoc_as_the_document_is_read():
    out = pandoc.convert("```parse\n*a*\n```\n\n```parse\n# b\n```", to="html",
                         filters=[wasm("parse")])
    assert out == '<p><em>a</em></p>\n<h1 id="b">b</h1>\n'


@needs_filters
def test_a_filters_calls_get_no_files_programs_or_lua(tmp_path):
    ok, many, version = call(
        {"convert": [{"from": "markdown", "to": "html"}, "*x*"]},
        {"read_many": [["a", "*b*"], {"from": "markdown"}]},
        {"query": ["version", None]},
    )
    assert ok["ok"] == "<p><em>x</em></p>\n"
    assert many["ok"][1]["blocks"][0]["c"][0]["t"] == "Emph"
    assert version["ok"] == pandoc.pandoc_version()
    refused = call(
        {"convert": [{"from": "markdown", "to": "html", "filters": ["/bin/sh"]}, "x"]},
        {"convert": [{"from": "markdown", "to": "html", "output-file": "out.html"}, "x"]},
        {"convert": [{"from": "markdown", "to": "writer.lua"}, "x"]},
        {"convert": [{"from": "markdown", "to": "pdf"}, "x"]},
        {"convert": [{"from": "markdown", "to": "html"}, None]},
        {"read_many": [["x"], {"from": "markdown", "data-dir": "/"}]},
        {"query": ["parse-args", {"args": ["-d", "x.yaml"]}]},
    )
    assert all(r["error"][0] == "PandocOptionError" for r in refused), refused
    assert "not allowed for untrusted code: filters" in refused[0]["error"][1]
    secret = tmp_path / "secret.tex"
    secret.write_text("SECRET")
    tex = f"\\input{{{secret}}}"
    answers = call({"read_many": [[tex], {"from": "latex"}]},
                   {"convert": [{"from": "latex", "to": "plain"}, tex]})
    assert "SECRET" not in json.dumps(answers)


@needs_filters
def test_it_sees_the_current_directory_read_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.txt").write_text("included")
    assert "included" in pandoc.convert("```include\nin.txt\n```", to="plain",
                                        filters=[wasm("include")])
    assert "exited with status 3" in failure(wasm("include"), "```include\n/etc/passwd\n```")
    assert "exited with status 3" in failure(wasm("misbehave"), "```write\nout.txt\n```")
    assert not (tmp_path / "out.txt").exists()
    pandoc.convert("```write\nout.txt\n```", to="plain",
                   filters=[WasmFilter(wasm("misbehave"), writable=True)])
    assert (tmp_path / "out.txt").exists()


@needs_filters
def test_limits(monkeypatch):
    started = time.monotonic()
    msg = failure(WasmFilter(wasm("misbehave"), timeout=0.3), "```spin\n```")
    assert "its time limit" in msg and time.monotonic() - started < 10
    msg = failure(WasmFilter(wasm("misbehave"), max_memory=64 << 20), "```hog\n```")
    assert "memory limit: 67108864 bytes" in msg
    monkeypatch.setenv("LIBPANDOC_WASM_TIMEOUT", "0.3")
    monkeypatch.setenv("LIBPANDOC_WASM_MAX_MEMORY", "64m")
    assert "its time limit" in failure(wasm("misbehave"), "```spin\n```")
    assert "memory limit: 67108864 bytes" in failure(wasm("misbehave"), "```hog\n```")
    assert failure(wasm("upper"), "hi") is None
    # 0: no limit, whatever the environment says
    assert failure(WasmFilter(wasm("upper"), timeout=0, max_memory=0), "hi") is None


def test_limits_are_read_as_pandoc_writes_them():
    assert parse_timeout("2") == 2.0 and parse_timeout("0.5") == 0.5
    assert parse_timeout("") is None and parse_timeout("0") is None
    for bad in ("2s", "-1", "inf"):
        with pytest.raises(ValueError):
            parse_timeout(bad)
    assert parse_memory("512M") == 512 << 20 and parse_memory("1g") == 1 << 30
    assert parse_memory("4096") == 4096 and parse_memory("64k") == 64 << 10
    assert parse_memory("") is None and parse_memory("0") is None
    for bad in ("12x", "M", "-1"):
        with pytest.raises(ValueError):
            parse_memory(bad)


@needs_filters
def test_pandocpy_runs_wasm_filters(capfd):
    from libpandoc import cli

    src = Path(WASM)
    status = cli.main(["-t", "plain", "-F", str(src / "upper.wasm"), os.devnull])
    assert status == 0
    status = cli.main(["-t", "plain", "-F", "nonesuch.wasm", os.devnull])
    assert status == cli.FILTER_FAILED
    assert "nonesuch.wasm not found" in capfd.readouterr().err


if sys.platform == "emscripten":  # no wasmtime in Pyodide
    pytestmark = pytest.mark.skip
