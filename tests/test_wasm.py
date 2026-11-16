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
    out = json.loads(pandoc.convert("x", from_="commonmark_x", to="json", number_sections=True,
                                    filters=[wasm("conversion")]))
    told = json.loads(out["blocks"][-1]["c"][1])
    assert told["number-sections"] is True  # (PANDOC_WRITER_OPTIONS)
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
    # (the memory limit with time enough to reach it: on a slow machine the
    # 0.3 s came first)
    monkeypatch.setenv("LIBPANDOC_WASM_TIMEOUT", "30")
    assert "memory limit: 67108864 bytes" in failure(wasm("misbehave"), "```hog\n```")
    assert failure(wasm("upper"), "hi") is None
    # 0: no limit, whatever the environment says
    assert failure(WasmFilter(wasm("upper"), timeout=0, max_memory=0), "hi") is None


@needs_filters
def test_a_filters_time_limit_covers_its_calls_to_pandoc():
    """Each call runs with what the filter has left as its "timeout", so one
    call on a slow document (the review's 100,000 nested block quotes, over
    15 s natively, minutes from a filter) can't outlast it."""
    slow = ">" * 100_000
    for request in ({"convert": [{"from": "markdown", "to": "html"}, slow]},
                    {"read_many": [[slow], {"from": "markdown"}]}):
        f = WasmFilter(wasm("calls"), timeout=1)
        started = time.monotonic()
        msg = failure(f, f"```call\n{json.dumps(request)}\n```")
        assert msg is not None and "its time limit" in msg, msg
        assert time.monotonic() - started < 4, request
    # the filter's own shorter "timeout" is kept: its call fails, it goes on
    request = {"convert": [{"from": "markdown", "to": "html", "timeout": 0.2}, slow]}
    out = json.loads(pandoc.convert(f"```call\n{json.dumps(request)}\n```", to="json",
                                    filters=[WasmFilter(wasm("calls"), timeout=30)]))
    assert json.loads(out["blocks"][0]["c"][1])["error"][0] == "Timeout"


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


def test_limits_out_of_range():
    """A negative or NaN limit was no limit (wasmtime's -1); one past what
    wasmtime takes wrapped around: 1e300 s stopped a filter at once, 2**64
    bytes and a MiB was a MiB."""
    for kw in ({"timeout": -1}, {"timeout": float("nan")}, {"timeout": float("inf")},
               {"max_memory": -1}, {"max_memory": 1.5}):
        with pytest.raises(ValueError, match="0 or more"):
            WasmFilter(wasm("upper") if WASM else "x.wasm", **kw)
    for bad in ("nan", "1e999"):
        with pytest.raises(ValueError):
            parse_timeout(bad)
    assert parse_timeout("1e300") == 1e300 and parse_memory(f"{2**40}g") == 2**62


@needs_filters
def test_huge_limits_are_no_limits(monkeypatch):
    big = "\n\n".join(f"para *{i}*" for i in range(2000))
    assert failure(WasmFilter(wasm("upper"), timeout=1e300, max_memory=2**64 + (1 << 20)), big) is None
    monkeypatch.setenv("LIBPANDOC_WASM_TIMEOUT", "1e300")
    assert failure(wasm("upper"), big) is None


# A WASI command writing 64 MiB to a descriptor, in blocks of 64 KiB; it
# exits with status 7 if a write fails.
FLOOD = """
(module
  (import "wasi_snapshot_preview1" "fd_write" (func $w (param i32 i32 i32 i32) (result i32)))
  (import "wasi_snapshot_preview1" "proc_exit" (func $exit (param i32)))
  (memory (export "memory") 2)
  (func (export "_start") (local $i i32)
    (i32.store (i32.const 0) (i32.const 65536))
    (i32.store (i32.const 4) (i32.const 65536))
    (loop $l
      (if (call $w (i32.const FD) (i32.const 0) (i32.const 1) (i32.const 8))
        (then (call $exit (i32.const 7))))
      (local.set $i (i32.add (local.get $i) (i32.const 1)))
      (br_if $l (i32.lt_u (local.get $i) (i32.const 1024))))))
"""


@pytest.mark.parametrize("fd, stream", [(1, "output"), (2, "error")])
def test_its_output_is_capped_as_its_memory(tmp_path, capfd, fd, stream):
    """A filter limited to 16 MiB kept 512 MiB of standard output in this
    process (its peak 1.7 GiB), and wrote freely to its standard error."""
    f = tmp_path / "flood.wat"
    f.write_text(FLOOD.replace("FD", str(fd)))
    msg = failure(WasmFilter(f, max_memory=16 << 20), "x")
    assert msg is not None and f"to its standard {stream}" in msg, msg
    assert len(capfd.readouterr().err) <= 16 << 20


@needs_filters
def test_the_clock_waits_while_no_timed_filter_runs():
    """It woke 100 times a second for the rest of the process's life."""
    from libpandoc import _wasmfilter

    assert failure(WasmFilter(wasm("upper"), timeout=5), "x") is None
    engine, ticks = _wasmfilter._engine(), []
    tick = engine.increment_epoch
    engine.increment_epoch = lambda: (ticks.append(1), tick())
    try:
        time.sleep(0.3)
        assert ticks == []
        assert failure(WasmFilter(wasm("misbehave"), timeout=0.3), "```spin\n```") is not None
        assert ticks
    finally:
        del engine.increment_epoch


@needs_filters
def test_pandocpy_runs_wasm_filters(tmp_path, capfd):
    from libpandoc import cli

    doc = tmp_path / "in.md"
    doc.write_text("hi")
    status = cli.main(["-t", "plain", "-F", str(Path(WASM) / "upper.wasm"), str(doc)])
    assert status == 0
    assert capfd.readouterr().out.splitlines() == ["HI"]  # CRLF on Windows, as pandoc
    status = cli.main(["-t", "plain", "-F", "nonesuch.wasm", str(doc)])
    assert status == cli.FILTER_FAILED
    assert "nonesuch.wasm not found" in capfd.readouterr().err


if sys.platform == "emscripten":  # no wasmtime in Pyodide
    pytestmark = pytest.mark.skip
