"""Cancelling calls (``CancelToken``, ``cancel=``; libpandoc 1.13's cancel
tokens, inside ``"untrusted"`` for untrusted options from 1.14) and
Ctrl-C: before, during and after a call, from another thread, with a
filter running, in the directory sandbox, and SIGINT sent to a process
converting a slow document.

The slow input is libpandoc's test/cancel.c's: 100,000 ``>`` read as
markdown, minutes of pandoc's time (a ``timeout`` bounds each use here, so
that a cancel that doesn't work fails the test rather than hanging it).
"""

from __future__ import annotations

import contextvars
import importlib.util
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

import panir
import pytest

import libpandoc as pandoc
from libpandoc import CancelToken, PandocCancelledError, PandocError

SLOW = ">" * 100_000
# a document slow to write as docx (seconds)
LONG = panir.Pandoc(*[panir.Para(panir.Str("x"), panir.Space(), panir.Emph(panir.Str("y")))
                      for _ in range(200_000)])
LIMIT = 60  # seconds: a call not cancelled ends here, as a Timeout

needs_threads = pytest.mark.skipif(sys.platform == "emscripten", reason="no threads")
needs_processes = pytest.mark.skipif(sys.platform == "emscripten", reason="no processes")
needs_signals = pytest.mark.skipif(sys.platform in ("emscripten", "win32"),
                                   reason="SIGINT to a child process: POSIX")
WASM = os.environ.get("LIBPANDOC_WASM", "")
needs_wasm = pytest.mark.skipif(
    sys.platform == "emscripten" or not os.path.isfile(WASM)
    or importlib.util.find_spec("wasmtime") is None,
    reason="the directory sandbox: wasmtime, and LIBPANDOC_WASM set to libpandoc.wasm")


def cancel_after(token: CancelToken, seconds: float) -> threading.Timer:
    t = threading.Timer(seconds, token.cancel)
    t.start()
    return t


def timed(f, *args, **kwargs) -> tuple[BaseException | None, float]:
    """What ``f`` raised, and how long it took."""
    started = time.monotonic()
    try:
        f(*args, **kwargs)
    except BaseException as e:  # noqa: BLE001
        return e, time.monotonic() - started
    return None, time.monotonic() - started


def test_a_token():
    t = CancelToken()
    assert not t.cancelled and repr(t) == "CancelToken(cancelled=False)"
    t.cancel()
    t.cancel()  # (again: nothing more)
    assert t.cancelled
    with pytest.raises(TypeError, match="CancelToken"):
        pandoc.convert("x", cancel=1)  # type: ignore[arg-type]


def test_cancelled_before():
    """A token cancelled before the call: it fails at once, and every call
    after it too."""
    t = CancelToken()
    t.cancel()
    for call in (lambda: pandoc.convert(SLOW, timeout=LIMIT, cancel=t),
                 lambda: pandoc.read("x", cancel=t),
                 lambda: pandoc.write(pandoc.read("x"), "html", cancel=t),
                 lambda: pandoc.read_many(["a", "b"], cancel=t),
                 lambda: pandoc.convert(SLOW, timeout=LIMIT, cancel=t, untrusted=True),
                 lambda: pandoc.read_many(["a", "b"], cancel=t, untrusted=True)):
        e, took = timed(call)
        assert isinstance(e, PandocCancelledError) and e.kind == "Cancelled", e
        assert isinstance(e, PandocError) and took < 1


@needs_threads
@pytest.mark.parametrize("how", ["convert", "read", "write", "read_many", "filters", "untrusted",
                                 "read_many untrusted"])
def test_cancelled_during(how):
    """From another thread, while pandoc runs: within about a second (the
    runtime switches threads every 20 ms), and the library goes on."""
    t = CancelToken()
    calls = {
        "convert": lambda: pandoc.convert(SLOW, timeout=LIMIT, cancel=t),
        "read": lambda: pandoc.read(SLOW, timeout=LIMIT, cancel=t),
        "write": lambda: pandoc.write(LONG, "docx", timeout=LIMIT, cancel=t),
        "read_many": lambda: pandoc.read_many([SLOW, "x"], timeout=LIMIT, cancel=t),
        "filters": lambda: pandoc.convert(SLOW, timeout=LIMIT, cancel=t,
                                          filters=[lambda doc: None]),
        # (the token inside "untrusted", which the host writes)
        "untrusted": lambda: pandoc.convert(SLOW, timeout=LIMIT, cancel=t, untrusted=True),
        "read_many untrusted": lambda: pandoc.read_many([SLOW, "x"], timeout=LIMIT, cancel=t,
                                                        untrusted=True),
    }
    timer = cancel_after(t, 1)
    e, took = timed(calls[how])
    timer.join()
    assert isinstance(e, PandocCancelledError), e
    assert took < 4, took
    assert pandoc.convert("*x*", to="html") == "<p><em>x</em></p>\n"


def test_cancelled_after():
    """A call that finished keeps its result; the next one fails."""
    t = CancelToken()
    assert pandoc.convert("*x*", to="html", cancel=t) == "<p><em>x</em></p>\n"
    t.cancel()
    with pytest.raises(PandocCancelledError):
        pandoc.convert("*x*", to="html", cancel=t)


@needs_threads
def test_one_token_for_many_calls():
    """A token serves calls at once, and cancelled, stops them all."""
    t = CancelToken()
    errors: list[BaseException | None] = []

    def one() -> None:
        errors.append(timed(pandoc.convert, SLOW, timeout=LIMIT, cancel=t)[0])

    threads = [threading.Thread(target=one) for _ in range(3)]
    for th in threads:
        th.start()
    time.sleep(0.5)
    t.cancel()
    for th in threads:
        th.join(10)
    assert len(errors) == 3 and all(isinstance(e, PandocCancelledError) for e in errors), errors


@needs_threads
@pytest.mark.parametrize("sandbox", [
    None,
    # (no time limit: only the cancel advances the module's epoch, and the
    # module is stopped when the filter returns, not at a tick)
    pytest.param({"read": [], "timeout": 0}, marks=needs_wasm, id="directory-sandbox"),
])
def test_a_python_filter_runs_to_its_end(sandbox):
    """A Python filter running when the token is cancelled runs to its
    end; the call then fails. A filter may cancel its own call's token.
    Natively and in the directory sandbox."""
    # The token is cancelled once the filter is running; the filter waits
    # to see it cancelled, then goes on to its end, and the call fails
    # (once ``cancelled`` is true, pandoc knows: the order inside
    # ``cancel``). (Not cancelled after a fixed time: on a busy CI runner a
    # call had not reached its filter by then, and was cancelled before
    # it; seen in libpandoc-rs's twin.)
    if sandbox is not None:
        # the module loaded first: a new libpandoc.wasm's first compilation
        # took over 60 s on CI runners, longer than the wait below
        pandoc.convert("x", to="html", sandbox=sandbox)
    t = CancelToken()
    started, ran = threading.Event(), []

    def slow_filter(doc):
        started.set()
        end = time.monotonic() + LIMIT
        while not t.cancelled and time.monotonic() < end:
            time.sleep(0.005)
        time.sleep(0.1)
        ran.append(True)

    def cancel_once_running():
        started.wait(LIMIT)
        t.cancel()

    canceller = threading.Thread(target=cancel_once_running)
    canceller.start()
    e, _ = timed(pandoc.convert, "x", to="html", filters=[slow_filter], cancel=t, sandbox=sandbox)
    canceller.join()
    assert isinstance(e, PandocCancelledError) and ran == [True], e

    t2 = CancelToken()
    e, _ = timed(pandoc.convert, "x", to="html", filters=[lambda doc: t2.cancel()], cancel=t2,
                 sandbox=sandbox)
    assert isinstance(e, PandocCancelledError), e


@pytest.mark.skipif(sys.platform == "emscripten", reason="no libpandoc tokens in Pyodide")
def test_pandoc_is_told_first(monkeypatch):
    """``cancel`` tells pandoc before ``cancelled`` says so: a filter that
    sees it and returns can't let a short conversion end well (that failed
    libpandoc-rs's twin of the test above on a slow macOS runner)."""
    from libpandoc import _cancel

    t, seen, core = CancelToken(), [], _cancel._core

    class Core:
        def __getattr__(self, name):
            return getattr(core, name)

        def cancel(self, id):
            seen.append(t.cancelled)
            core.cancel(id)

    monkeypatch.setattr(_cancel, "_core", Core())
    t.cancel()
    assert seen == [False] and t.cancelled


@needs_wasm
def test_the_directory_sandbox_stops_when_a_filter_returns(monkeypatch):
    """In the sandbox, a filter that returns after its token was cancelled
    ends the call as cancelled, though ``cancel`` hasn't advanced the epoch
    yet (here: never), which a short conversion could outrun."""
    from libpandoc import _cancel

    monkeypatch.setattr(_cancel, "interrupt", lambda: None)
    t = CancelToken()
    e, _ = timed(pandoc.convert, "x", to="html", filters=[lambda doc: t.cancel()], cancel=t,
                 sandbox={"read": [], "timeout": 0})
    assert isinstance(e, PandocCancelledError), e


def test_untrusted_options():
    """Untrusted options take the token inside "untrusted", which the host
    writes over whatever was there (libpandoc 1.14); a top-level
    "cancel-token" beside "untrusted" is refused, as the code could have
    written it. (On the main thread every call has a token: Ctrl-C's.)"""
    from libpandoc._cancel import options

    t = CancelToken()
    assert pandoc.convert("*x*", to="html", untrusted=True) == "<p><em>x</em></p>\n"
    assert pandoc.convert("*x*", to="html", untrusted=True, cancel=t) == "<p><em>x</em></p>\n"
    assert pandoc.read_many(["*x*"], untrusted=True, cancel=t)[0] == pandoc.read("*x*")
    for untrusted in (True, {}, {"cancel-token": 1}, "yes"):
        assert options({"to": "html", "untrusted": untrusted}, t) == {
            "to": "html", "untrusted": {"cancel-token": t._id}}
    assert options({"untrusted": False}, t) == {"untrusted": False, "cancel-token": t._id}
    assert options({"untrusted": True}, None) == {"untrusted": True}
    for untrusted in (True, {}, {"cancel-token": t._id}):
        with pytest.raises(PandocError) as e:
            pandoc.convert("x", {"to": "html", "untrusted": untrusted, "cancel-token": t._id})
        assert e.value.kind == "PandocOptionError", e.value


def test_a_filter_sees_the_callers_context():
    """A call on the main thread runs on the helper thread, in a copy of the
    caller's context: a ContextVar the caller set is the filter's."""
    var: contextvars.ContextVar[str] = contextvars.ContextVar("var", default="unset")
    var.set("the caller's")
    seen = []
    pandoc.convert("x", to="html", filters=[lambda doc: seen.append(var.get())])
    assert seen == ["the caller's"]


@needs_wasm
@needs_threads
def test_the_directory_sandbox(tmp_path):
    """In the sandbox the token stops libpandoc.wasm from outside (wasmtime's
    epoch): pandoc, and Lua, which nothing stops natively."""
    loop = tmp_path / "forever.lua"
    loop.write_text("function Pandoc(doc) while true do end end\n")
    sandbox = {"read": [str(tmp_path)], "timeout": LIMIT}
    # (the filter as the module names it: on Windows C:\\x is /c/x)
    inside = loop.as_posix()
    if sys.platform == "win32":
        inside = f"/{inside[0].lower()}{inside[2:]}"
    pandoc.convert("x", to="html", sandbox=sandbox)  # (the module compiled)
    for call in (lambda t: pandoc.convert(SLOW, to="html", sandbox=sandbox, cancel=t),
                 lambda t: pandoc.convert("x", to="html", filters=[inside], sandbox=sandbox,
                                          cancel=t),
                 # untrusted options: the token is never in the module's options
                 lambda t: pandoc.convert(SLOW, to="html", sandbox=sandbox, cancel=t,
                                          untrusted=True)):
        t = CancelToken()
        timer = cancel_after(t, 1)
        e, took = timed(call, t)
        timer.join()
        assert isinstance(e, PandocCancelledError), e
        assert took < 3, took
    t = CancelToken()
    t.cancel()
    e, _ = timed(pandoc.convert, "x", to="html", sandbox=sandbox, cancel=t)
    assert isinstance(e, PandocCancelledError)
    assert pandoc.convert("x", to="html", sandbox=sandbox) == "<p>x</p>\n"
    assert pandoc.convert("x", to="html", sandbox=sandbox, untrusted=True,
                          cancel=CancelToken()) == "<p>x</p>\n"


@needs_wasm
def test_cancelled_while_the_module_loads(tmp_path, monkeypatch):
    """A token cancelled while libpandoc.wasm loads (as Ctrl-C during its
    first compilation, 20 s) stops the call once it has loaded. Its epoch
    came before the store's deadline, which nothing else would reach
    without a time limit: the whole conversion ran (16 s here, of a
    document that runs for minutes)."""
    from libpandoc import _sandbox

    monkeypatch.delenv("LIBPANDOC_WASM_TIMEOUT", raising=False)  # (no clock ticking)
    t = CancelToken()
    load = _sandbox._module

    def cancelled_while_loading():
        t.cancel()
        return load()

    monkeypatch.setattr(_sandbox, "_module", cancelled_while_loading)
    e, took = timed(pandoc.convert, ">" * 10_000, to="html", sandbox={"read": [str(tmp_path)]},
                    cancel=t)
    assert isinstance(e, PandocCancelledError), e
    assert took < 3, took


SLOW_SCRIPT = """\
import sys, time
import libpandoc
print("started", flush=True)
started = time.monotonic()
try:
    libpandoc.convert({source}, to="html", timeout=60{more})
finally:
    print("ended after", round(time.monotonic() - started, 2), flush=True)
"""


def interrupted(script: str, wait: float = 1.0) -> tuple[subprocess.CompletedProcess, float]:
    """Run ``script``, send it SIGINT once it has started its conversion
    (and ``wait`` more seconds), and wait for it: its result, and how long
    it took to end after the signal."""
    p = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    assert p.stdout is not None
    assert p.stdout.readline().strip() == "started"
    time.sleep(wait)
    sent = time.monotonic()
    p.send_signal(signal.SIGINT)
    out, err = p.communicate(timeout=120)
    took = time.monotonic() - sent
    return subprocess.CompletedProcess(p.args, p.returncode, out, err), took


@needs_signals
@pytest.mark.parametrize("more", ["", ", filters=[lambda doc: None]", ", untrusted=True"])
def test_ctrl_c(more):
    """Ctrl-C during a long conversion on the main thread: the call stops at
    once, and Python's usual KeyboardInterrupt ends the process (by SIGINT,
    as Python does when one isn't caught). Untrusted options too (the token
    inside "untrusted": libpandoc 1.14)."""
    r, took = interrupted(SLOW_SCRIPT.format(source=repr(SLOW), more=more))
    assert "KeyboardInterrupt" in r.stderr and "ended after" in r.stdout, r
    assert r.returncode == -signal.SIGINT, r
    assert took < 3, took


@needs_signals
@needs_wasm
def test_ctrl_c_in_the_directory_sandbox(tmp_path):
    more = f", sandbox={{'read': [{str(tmp_path)!r}]}}"
    # (the module loaded before the clock starts: loading it is not
    # interruptible, and took more than the wait on a slow runner)
    script = SLOW_SCRIPT.format(source=repr(SLOW), more=more).replace(
        'print("started"', f'libpandoc.convert("x", to="html"{more})\nprint("started"', 1)
    r, took = interrupted(script)
    assert "KeyboardInterrupt" in r.stderr and "ended after" in r.stdout, r
    assert took < 3, took


@needs_signals
def test_ctrl_c_caught():
    """Caught, KeyboardInterrupt leaves the library usable."""
    script = textwrap.dedent(f"""\
        import libpandoc
        print("started", flush=True)
        try:
            libpandoc.convert({SLOW!r}, timeout=60)
        except KeyboardInterrupt:
            print(libpandoc.convert("*x*", to="html"), end="")
        """)
    r, took = interrupted(script)
    assert r.returncode == 0 and r.stdout.endswith("<p><em>x</em></p>\n"), r
    assert took < 3, took


@needs_signals
def test_pandocpy_ctrl_c(tmp_path):
    """pandocpy dies by SIGINT, as pandoc does (its default action)."""
    slow = tmp_path / "slow.md"
    slow.write_text(SLOW)
    p = subprocess.Popen([sys.executable, "-m", "libpandoc", str(slow), "-o", os.devnull],
                         stderr=subprocess.PIPE)
    time.sleep(3)  # (Python, then pandoc reading)
    sent = time.monotonic()
    p.send_signal(signal.SIGINT)
    p.communicate(timeout=120)
    assert p.returncode == -signal.SIGINT and time.monotonic() - sent < 3
