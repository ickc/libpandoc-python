"""Wasm filters: libpandoc-rs's example filters (filters/, built for
wasm32-wasip1), given as $LIBPANDOC_WASM_FILTERS, run by wasmtime."""

from __future__ import annotations

import json
import os
import subprocess
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


def named(p: str | os.PathLike[str]) -> str:
    """An absolute path as a wasm filter names it: with `/`; on Windows
    `C:\\x` is `/c/x`."""
    s = os.fspath(p).replace("\\", "/")
    if sys.platform == "win32" and s[1:2] == ":":
        s = f"/{s[0].lower()}{s[2:]}"
    return s


@pytest.fixture
def tree(tmp_path, monkeypatch) -> Path:
    """What a wasm filter sees: `work` (the working directory, with `img`),
    `common` beside it, `secret` (a canary) and a file; on POSIX a link
    leaving `work`, and `linked`, a link to `common`. LIBPANDOC_WASM_DIRS
    unset."""
    base = Path(os.path.realpath(tmp_path))  # (macOS's /var is /private/var)
    for d in ("work/img", "common", "secret"):
        (base / d).mkdir(parents=True)
    for f, text in (("work/here.txt", "HERE-OK"), ("work/img/in.txt", "IMG-OK"),
                    ("common/part.txt", "COMMON-OK"), ("secret/secret.txt", "CANARY-WASM-DIRS"),
                    ("a-file.txt", "not a directory")):
        (base / f).write_text(text)
    if sys.platform != "win32":
        (base / "work/link-out.txt").symlink_to("../secret/secret.txt")
        (base / "linked").symlink_to("common")
    monkeypatch.chdir(base / "work")
    monkeypatch.delenv("LIBPANDOC_WASM_DIRS", raising=False)
    return base


def include(file: str, f=None) -> tuple[str | None, str | None]:
    """(output, error) of `include` (or the filter ``f``) on ``file``."""
    try:
        return pandoc.convert(f"```include\n{file}\n```\n", to="plain",
                              filters=[f or wasm("include")]).strip(), None
    except WasmFilterError as e:
        return None, str(e)


@needs_filters
def test_a_filter_names_no_cancel_token():
    """A filter can't name a cancel token: the host writes "untrusted" over
    what it wrote (its calls stop with the conversion they run in), and a
    top-level "cancel-token" is refused."""
    t = pandoc.CancelToken()
    t.cancel()
    a = call({"convert": [{"to": "html", "untrusted": {"cancel-token": t._id}}, "*x*"]},
             {"convert": [{"to": "html", "cancel-token": t._id}, "*x*"]},
             {"read_many": [["x"], {"untrusted": {"cancel-token": t._id}}]})
    assert a[0] == {"ok": "<p><em>x</em></p>\n"}, a
    assert a[1]["error"][0] == "PandocOptionError", a
    assert len(a[2]["ok"]) == 1, a


@needs_filters
def test_it_sees_only_what_it_is_given(tree, capfd):
    common = tree / "common"
    # a directory under a name of the program's: here as the filter's "."
    f = WasmFilter(wasm("include"), preopens=[(common, ".", False)])
    assert include("part.txt", f) == ("COMMON-OK", None)
    # as the variable gives it: by its absolute path (and, through a link,
    # its real one too)
    f = WasmFilter(wasm("include"), dirs=[common])
    assert include(named(common / "part.txt"), f) == ("COMMON-OK", None)
    if sys.platform != "win32":
        f = WasmFilter(wasm("include"), dirs=[tree / "linked"])
        for part in (tree / "linked/part.txt", common / "part.txt"):
            assert include(named(part), f) == ("COMMON-OK", None), part
    # what is outside is missing
    out, err = include(named(tree / "secret/secret.txt"), WasmFilter(wasm("include"), dirs=[common]))
    assert out is None and err.endswith("exited with status 3"), err
    # no directory: the filter's failure says it had none
    out, err = include("here.txt", WasmFilter(wasm("include"), dirs=[]))
    assert err.endswith("exited with status 3 (it was given no directory)"), err
    # a directory that doesn't exist, or isn't one: it fails before it runs
    for d, says in ((tree / "nope", "does not exist"), (tree / "a-file.txt", "is not a directory")):
        out, err = include("here.txt", WasmFilter(wasm("include"), dirs=[d]))
        assert err is not None and err.endswith(f"include.wasm: {d} {says}"), err
    assert "CANARY" not in capfd.readouterr().err


@needs_filters
def test_none_by_default(tree):
    """No directory unless LIBPANDOC_WASM_DIRS gives some (empty: none);
    the failure says so."""
    for value in (None, "", os.pathsep):
        with pytest.MonkeyPatch.context() as m:
            if value is not None:
                m.setenv("LIBPANDOC_WASM_DIRS", value)
            _, err = include("here.txt")
            assert err is not None and err.endswith(
                "exited with status 3 (it was given no directory: LIBPANDOC_WASM_DIRS)"), err


@needs_filters
def test_the_variables_directories(tree, monkeypatch, capfd):
    """Each directory LIBPANDOC_WASM_DIRS gives, under its absolute path and
    its path relative to the working directory (and the working directory
    as `.` inside one); paths are not normalised."""

    def run(dirs: list[str], file: str) -> tuple[str | None, str | None]:
        monkeypatch.setenv("LIBPANDOC_WASM_DIRS", os.pathsep.join(dirs))
        return include(file)

    work, common = named(tree / "work"), named(tree / "common")
    reads = [
        # the working directory, as `.`, and by its absolute path
        (["."], "here.txt", "HERE-OK"),
        (["."], "img/in.txt", "IMG-OK"),
        (["."], f"{work}/here.txt", "HERE-OK"),
        ([str(tree / "work")], "here.txt", "HERE-OK"),
        # its parent, as `..`, and the working directory as `.` inside it
        ([".."], "../common/part.txt", "COMMON-OK"),
        ([".."], "here.txt", "HERE-OK"),
        ([".."], f"{common}/part.txt", "COMMON-OK"),
        # a sibling, and a list
        (["../common"], "../common/part.txt", "COMMON-OK"),
        ([".", "../common"], "here.txt", "HERE-OK"),
        ([".", "../common"], "../common/part.txt", "COMMON-OK"),
        # a subdirectory
        (["img"], "img/in.txt", "IMG-OK"),
    ]
    for dirs, file, text in reads:
        assert run(dirs, file) == (text, None), (dirs, file)
    fails = [
        (["."], "../common/part.txt"),
        (["../common"], "here.txt"),
        # only below a subdirectory, and not normalised
        (["img"], "here.txt"),
        (["img"], "./img/in.txt"),
        ([".", "../common"], "./../common/part.txt"),
        ([".", "../common"], "img/../../common/part.txt"),
        # outside what was given
        ([".", "../common"], named(tree / "secret/secret.txt")),
        ([".", "../common"], "../secret/secret.txt"),
    ]
    if sys.platform != "win32":
        fails.append((["."], "link-out.txt"))
    for dirs, file in fails:
        capfd.readouterr()
        out, err = run(dirs, file)
        assert out is None and err.endswith("exited with status 3"), (dirs, file, err)
        said = capfd.readouterr().err  # (the filter's standard error)
        assert "CANARY" not in said, said
        if file in ("./../common/part.txt", "img/../../common/part.txt"):
            assert "Operation not permitted" in said, said
    # a directory that doesn't exist, a file: the filter fails, naming it
    _, err = run([".", "nope"], "here.txt")
    assert err is not None and err.endswith("include.wasm: LIBPANDOC_WASM_DIRS: nope does not exist")
    file = os.path.join("..", "a-file.txt")
    _, err = run([file], "here.txt")
    assert err is not None and err.endswith(f"LIBPANDOC_WASM_DIRS: {file} is not a directory"), err
    # a program's own choice replaces the variable: none, or its own
    monkeypatch.setenv("LIBPANDOC_WASM_DIRS", ".")
    _, err = include("here.txt", WasmFilter(wasm("include"), dirs=[]))
    assert err is not None and err.endswith("exited with status 3 (it was given no directory)"), err
    _, err = include("here.txt", WasmFilter(wasm("include"), dirs=["../common"]))
    assert err is not None and err.endswith("exited with status 3"), err
    _, err = include("here.txt", WasmFilter(wasm("include"), preopens=[("../common", "/c", False)]))
    assert err is not None and err.endswith("exited with status 3"), err
    assert include("here.txt") == ("HERE-OK", None)
    assert "CANARY" not in capfd.readouterr().err


@needs_filters
def test_read_only_unless_given_writable(tree, monkeypatch):
    write = "```write\nout.txt\n```"
    monkeypatch.setenv("LIBPANDOC_WASM_DIRS", ".")
    assert failure(wasm("misbehave"), write).endswith("exited with status 3")
    assert failure(WasmFilter(wasm("misbehave"), dirs=["."]), write).endswith("exited with status 3")
    assert not (tree / "work/out.txt").exists()
    assert failure(WasmFilter(wasm("misbehave"), preopens=[(".", ".", True)]), write) is None
    assert (tree / "work/out.txt").exists()


def test_dirs_are_paths():
    """``dirs`` lists directories; a name of one's own is ``preopens``'s."""
    for dirs, says in (([(".", ".")], "preopens"), (".", "are lists")):
        with pytest.raises(TypeError, match=says):
            WasmFilter(wasm("upper") if WASM else "x.wasm", dirs=dirs)
    with pytest.raises(TypeError, match="host, guest, writable"):
        WasmFilter(wasm("upper") if WASM else "x.wasm", preopens=[(".", ".")])


@needs_filters
def test_pandocpy_gives_the_variables_directories(tree):
    """pandocpy: none by default, the failure saying so (status 83); the
    variable's; one that doesn't exist (in a child process: the variable
    is the process's)."""

    def pandocpy(dirs: str | None, file: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "LIBPANDOC_WASM_DIRS"}
        if dirs is not None:
            env["LIBPANDOC_WASM_DIRS"] = dirs
        return subprocess.run([sys.executable, "-m", "libpandoc", "-t", "plain", "-F", wasm("include")],
                              input=f"```include\n{file}\n```\n".encode(), capture_output=True,
                              cwd=tree / "work", env=env, timeout=120, check=False)

    r = pandocpy(None, "here.txt")
    assert r.returncode == 83, r
    assert b"exited with status 3 (it was given no directory: LIBPANDOC_WASM_DIRS)" in r.stderr, r
    r = pandocpy(os.pathsep.join([".", "../common"]), "../common/part.txt")
    assert (r.returncode, r.stdout.strip()) == (0, b"COMMON-OK"), r
    r = pandocpy(os.pathsep.join([".", "nope"]), "here.txt")
    assert r.returncode == 83 and b"LIBPANDOC_WASM_DIRS: nope does not exist" in r.stderr, r


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
    engine, ticks = _wasmfilter.engine(), []
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
def test_a_filter_replaced_is_compiled_again(tmp_path):
    """Also when the new file has the old one's modification time (as at
    a file system's coarse resolution, or a copy keeping times): the old
    filter ran."""
    f = tmp_path / "f.wasm"
    f.write_bytes(Path(wasm("identity")).read_bytes())
    assert pandoc.convert("hi", to="plain", filters=[str(f)]) == "hi\n"
    st = f.stat()
    f.write_bytes(Path(wasm("upper")).read_bytes())
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert pandoc.convert("hi", to="plain", filters=[str(f)]) == "HI\n"


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


@needs_filters
def test_wasm_filters_in_threads():
    """Conversions with wasm filters in several threads at once (wasmtime-py
    49's tables of host functions, changed without a lock, broke)."""
    from concurrent.futures import ThreadPoolExecutor

    def one(i: int) -> str:
        return pandoc.convert(f"n{i}", to="plain", filters=[wasm("upper")])

    with ThreadPoolExecutor(8) as pool:
        assert list(pool.map(one, range(64))) == [f"N{i}\n" for i in range(64)]


@needs_filters
def test_pandocpy_with_a_failing_wasm_filter_exits(tmp_path):
    """As pandoc's status for a failed filter, the run's last call into
    Python made before the interpreter's exit (see the test below: it came
    after it in 1 process in 9 on macOS, SIGSEGV; 3.11 aborted)."""
    r = subprocess.run([sys.executable, "-m", "libpandoc", "-t", "plain", "-F", wasm("include")],
                       input=b"```include\nnonesuch.txt\n```\n", capture_output=True,
                       cwd=tmp_path, timeout=120, check=False)
    assert r.returncode == 83, r.stderr


FREED_SCRIPT = """\
import os, sys, weakref
import libpandoc, panir
from libpandoc import _wasmfilter

os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
held, refs, capped = 0, [], _wasmfilter._capped

def watched(*args):  # the callbacks wasmtime is given, watched
    sink, over = capped(*args)
    refs.append(weakref.ref(sink))
    return sink, over

_wasmfilter._capped = watched
f = libpandoc.WasmFilter(sys.argv[1])
doc = panir.dumps(libpandoc.read("```include\\nnonesuch.txt\\n```\\n")).encode()
for _ in range(20):
    refs.clear()
    try:
        f.run(doc, {"format": "plain"})
    except _wasmfilter.WasmFilterError:
        pass
    held += any(r() is not None for r in refs)
print(held)
"""


@needs_filters
@pytest.mark.skipif(not hasattr(os, "sched_setaffinity"), reason="one CPU for a process: Linux")
def test_a_run_ends_once_wasmtime_has_freed_its_callbacks(tmp_path):
    """wasmtime frees a run's standard output and error callbacks on a
    thread of its own (tokio's) after the store is closed, calling Python:
    after the interpreter's exit, that crashed it. A run waits for it. In a
    process on one CPU that thread runs late: without the wait, 19 runs in
    20 returned with the callbacks still held."""
    r = subprocess.run([sys.executable, "-c", FREED_SCRIPT, wasm("include")], capture_output=True,
                       text=True, cwd=tmp_path, timeout=120, check=False)
    assert r.returncode == 0 and r.stdout.strip() == "0", r


if sys.platform == "emscripten":  # no wasmtime in Pyodide
    pytestmark = pytest.mark.skip
