"""Consecutive filters of this process: one exchange with pandoc (a run),
by default; "separate-filters" / LIBPANDOC_SEPARATE_FILTERS makes each its
own."""

from __future__ import annotations

import os
import sys
import textwrap
import threading
from pathlib import Path

import panir
import pytest
from panir import Filter, Pandoc, Para

import libpandoc
from libpandoc import PandocCancelledError, PandocError, _run, _scripts, cli

VAR = _run.VAR
HINT = "(its input is what filters[{}] wrote, unchecked: LIBPANDOC_SEPARATE_FILTERS=1 has pandoc check it)"

WASM = os.environ.get("LIBPANDOC_WASM_FILTERS", "")
try:
    import wasmtime  # noqa: F401

    _has_wasmtime = True
except ImportError:
    _has_wasmtime = False
needs_filters = pytest.mark.skipif(
    not (WASM and _has_wasmtime),
    reason="wasm filters: wasmtime, and LIBPANDOC_WASM_FILTERS set to libpandoc-rs's filters")
SANDBOX = os.environ.get("LIBPANDOC_WASM", "")
needs_sandbox = pytest.mark.skipif(
    not (SANDBOX and _has_wasmtime),
    reason="the directory sandbox: wasmtime, and LIBPANDOC_WASM set to libpandoc.wasm")


def wasm(name: str) -> str:
    return str(Path(WASM) / f"{name}.wasm")


@pytest.fixture(autouse=True)
def _no_switch(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    monkeypatch.delenv(_scripts.ENV, raising=False)


def adding(text):
    """A Python filter appending a paragraph."""
    def f(doc):
        doc.blocks.append(Para(text))
    f.__qualname__ = f"adding({text!r})"
    return f


def script(tmp_path, name, code):
    p = tmp_path / name
    p.write_text(textwrap.dedent(code))
    return str(p)


# a script on bytes: reads its standard input, appends a paragraph
PLAIN = """\
import json, sys
doc = json.load(sys.stdin)
doc["blocks"].append({"t": "Para", "c": [{"t": "Str", "c": "%s"}]})
json.dump(doc, sys.stdout)
"""

# a panir script, handing its filter over
PANIR = """\
from panir import Filter, Pandoc, Para

f = Filter()

@f.on(Pandoc)
def add(doc):
    doc.blocks.append(Para("%s"))

if __name__ == "__main__":
    f.main()
"""

GARBLE = """\
import sys
sys.stdout.write('{"not": "a document"}')
"""

LUA = """\
function Pandoc(doc)
  doc.blocks:insert(pandoc.Para("lua"))
  return doc
end
"""


def paras(out: str) -> list[str]:
    return out.split()


class Seen:
    """Typed filters recording the documents they were given, kept alive
    (so that two parses can't share an address)."""

    def __init__(self):
        self.docs: list[Pandoc] = []

    def __call__(self, doc):
        self.docs.append(doc)


@pytest.fixture
def counted(monkeypatch):
    """How many times this package parsed and wrote a document."""
    n = {"loads": 0, "dumps": 0}
    loads, dumps = panir.loads, panir.dumps

    def counting_loads(*a, **k):
        n["loads"] += 1
        return loads(*a, **k)

    def counting_dumps(*a, **k):
        n["dumps"] += 1
        return dumps(*a, **k)

    monkeypatch.setattr(panir, "loads", counting_loads)
    monkeypatch.setattr(panir, "dumps", counting_dumps)
    return n


# -- one exchange --------------------------------------------------------------------


@pytest.mark.parametrize("separate", [False, True])
def test_typed_filters_share_the_document_unless_separate(separate):
    seen = Seen()
    libpandoc.convert("hi", to="plain", filters=[seen, seen], separate_filters=separate)
    assert (seen.docs[0] is seen.docs[1]) is not separate


def test_a_lua_filter_ends_a_run(tmp_path):
    lua = tmp_path / "f.lua"
    lua.write_text(LUA)
    seen = Seen()
    out = libpandoc.convert("hi", to="plain", filters=[seen, str(lua), seen])
    assert seen.docs[0] is not seen.docs[1] and paras(out) == ["hi", "lua"]


def test_typed_lua_typed_typed_keeps_the_order(tmp_path):
    lua = tmp_path / "f.lua"
    lua.write_text(LUA)
    seen = Seen()

    def record(doc):
        seen(doc)
        doc.blocks.append(Para(f"t{len(seen.docs)}"))

    for separate in (False, True):
        seen.docs.clear()
        out = libpandoc.convert("hi", to="plain", filters=[record, str(lua), record, record],
                                separate_filters=separate)
        assert paras(out) == ["hi", "t1", "lua", "t2", "t3"]
        assert (seen.docs[1] is seen.docs[2]) is not separate


def test_two_panir_scripts_share_one_parse(tmp_path, counted):
    """Each hands its filter over without reading its standard input: the
    run parses pandoc's document once and writes it once."""
    a = script(tmp_path, "a.py", PANIR % "a")
    b = script(tmp_path, "b.py", PANIR % "b")
    out = libpandoc.convert("hi", to="plain", filters=[a, b])
    assert paras(out) == ["hi", "a", "b"] and counted == {"loads": 1, "dumps": 1}
    counted.update(loads=0, dumps=0)
    out = libpandoc.convert("hi", to="plain", filters=[a, b], separate_filters=True)
    assert paras(out) == ["hi", "a", "b"] and counted == {"loads": 2, "dumps": 2}


def test_a_script_reading_its_input_gets_it_written_once(tmp_path, counted):
    """two typed filters, a script reading stdin, a typed one: the
    document parsed once for the first two, written once for the script,
    its bytes parsed once for the last."""
    s = script(tmp_path, "plain.py", PLAIN % "s")
    filters = [adding("a"), adding("b"), s, adding("c")]
    out = libpandoc.convert("hi", to="plain", filters=filters)
    assert paras(out) == ["hi", "a", "b", "s", "c"] and counted == {"loads": 2, "dumps": 2}
    counted.update(loads=0, dumps=0)
    out = libpandoc.convert("hi", to="plain", filters=filters, separate_filters=True)
    assert paras(out) == ["hi", "a", "b", "s", "c"] and counted == {"loads": 3, "dumps": 3}


def test_a_panir_script_after_a_typed_filter_gets_the_same_document(tmp_path):
    seen = Seen()
    s = script(tmp_path, "keep.py", """\
        import panir
        def keep(doc):
            doc.blocks.append(panir.Para("s"))
        f = panir.Filter()
        f.on(panir.Pandoc)(keep)
        f.main()
        """)
    out = libpandoc.convert("hi", to="plain", filters=[seen, s, seen])
    assert seen.docs[0] is seen.docs[1] and paras(out) == ["hi", "s"]


# -- mixed runs ------------------------------------------------------------------------


@needs_filters
def test_a_mixed_run_keeps_its_order_chained_and_separate(tmp_path):
    """typed, wasm, typed, script, wasm, typed: upper uppercases what came
    before it only."""
    s = script(tmp_path, "plain.py", PLAIN % "s")
    p = script(tmp_path, "panir.py", PANIR % "p")
    filters = [adding("a"), wasm("upper"), adding("b"), s, wasm("identity"), adding("c"), p]
    want = ["HI", "A", "b", "s", "c", "p"]
    assert paras(libpandoc.convert("hi", to="plain", filters=filters)) == want
    assert paras(libpandoc.convert("hi", to="plain", filters=filters,
                                   separate_filters=True)) == want


@needs_filters
def test_wasm_filters_share_one_exchange(tmp_path):
    seen = Seen()
    for separate in (False, True):
        seen.docs.clear()
        libpandoc.convert("hi", to="plain", filters=[seen, wasm("identity"), seen],
                          separate_filters=separate)
        assert seen.docs[0] is not seen.docs[1]  # (the wasm filter wrote bytes)
    # a WasmFilter and a path alike
    out = libpandoc.convert("hi", to="plain",
                            filters=[libpandoc.WasmFilter(wasm("upper")), wasm("identity")])
    assert out == "HI\n"


# -- failures ------------------------------------------------------------------------------


def test_a_failing_filter_is_named_and_the_later_ones_dont_run():
    ran = []

    def bad(doc):
        raise KeyError("boom")

    with pytest.raises(KeyError, match="boom") as e:
        libpandoc.convert("hi", to="plain", filters=[adding("a"), bad, lambda d: ran.append(d)])
    assert e.value.__notes__ == ["filters[1]: this filter failed"]
    assert ran == []


def test_a_failing_filter_is_named_by_its_place_in_the_list(tmp_path):
    lua = tmp_path / "f.lua"
    lua.write_text(LUA)

    def bad(doc):
        raise ValueError("boom")

    with pytest.raises(ValueError) as e:
        libpandoc.convert("hi", to="plain", filters=[adding("a"), str(lua), adding("b"), bad])
    assert e.value.__notes__ == ["filters[3]: this filter failed"]
    with pytest.raises(ValueError) as e:
        libpandoc.convert("hi", to="plain", filters=[adding("a"), str(lua), adding("b"), bad],
                          separate_filters=True)
    assert e.value.__notes__ == ["filters[3]: this filter failed"]


def test_a_time_limit_is_named_too(tmp_path):
    slow = script(tmp_path, "slow.py", "# pandocpy: subprocess\nimport sys, time\n"
                  "sys.stdin.read()\ntime.sleep(20)\n")
    with pytest.raises(libpandoc.PandocTimeoutError) as e:
        libpandoc.convert("hi", to="plain", timeout=1, filters=[adding("a"), slow])
    assert e.value.__notes__ == ["filters[1]: this filter failed"]


def test_a_failing_script_keeps_its_advice(tmp_path):
    s = script(tmp_path, "bad.py", "raise RuntimeError('a bug')\n")
    with pytest.raises(_scripts.ScriptFailed, match="It ran in this process") as e:
        libpandoc.convert("hi", to="plain", filters=[adding("a"), s])
    assert e.value.__notes__ == ["filters[1]: this filter failed"]


def test_unchecked_bytes_give_the_hint(tmp_path):
    g = script(tmp_path, "garble.py", GARBLE)
    ran = []
    with pytest.raises(ValueError) as e:  # (panir's ASTDecodeError)
        libpandoc.convert("hi", to="plain", filters=[g, lambda d: ran.append(d)])
    assert e.value.__notes__ == [
        "filters[1]: what it was given could not be read " + HINT.format(0)]
    assert ran == []
    # a script reading them, failing on them
    s = script(tmp_path, "reads.py", PLAIN % "s")
    with pytest.raises(_scripts.ScriptFailed, match="KeyError") as e:
        libpandoc.convert("hi", to="plain", filters=[g, s])
    assert e.value.__notes__ == ["filters[1]: this filter failed " + HINT.format(0)]
    # a panir script handing over: its input parsed
    p = script(tmp_path, "panir.py", PANIR % "p")
    with pytest.raises(ValueError) as e:
        libpandoc.convert("hi", to="plain", filters=[g, p])
    assert e.value.__notes__ == [
        "filters[1]: what it was given could not be read " + HINT.format(0)]


def test_with_the_switch_pandoc_refuses_them_after_the_first(tmp_path):
    g = script(tmp_path, "garble.py", GARBLE)
    ran = []
    with pytest.raises(PandocError, match="invalid document") as e:
        libpandoc.convert("hi", to="plain", filters=[g, lambda d: ran.append(d)],
                          separate_filters=True)
    assert "callback 0" in str(e.value) and ran == []


def test_pandoc_checks_the_last_answer_of_a_run(tmp_path):
    g = script(tmp_path, "garble.py", GARBLE)
    with pytest.raises(PandocError, match="invalid document"):
        libpandoc.convert("hi", to="plain", filters=[adding("a"), g])


@needs_filters
def test_a_wasm_filter_given_unchecked_bytes(tmp_path):
    g = script(tmp_path, "garble.py", GARBLE)
    with pytest.raises(libpandoc._wasmfilter.WasmFilterError) as e:
        libpandoc.convert("hi", to="plain", filters=[g, wasm("upper")])
    assert e.value.__notes__ == ["filters[1]: this filter failed " + HINT.format(0)]
    assert "(it was given no directory" in str(e.value)


# -- the switch ------------------------------------------------------------------------------


def exchanges(**kw) -> bool:
    """Whether two typed filters had one exchange."""
    seen = Seen()
    libpandoc.convert("hi", to="plain", filters=[seen, seen], **kw)
    return seen.docs[0] is seen.docs[1]


@pytest.mark.parametrize(("value", "one"), [(None, True), ("", True), ("0", True), ("1", False)])
def test_the_variable(monkeypatch, value, one):
    if value is not None:
        monkeypatch.setenv(VAR, value)
    assert exchanges() is one


@pytest.mark.parametrize("value", ["yes", "true", "2", " 1", "1 "])
def test_a_bad_value_of_the_variable_is_an_error(monkeypatch, value):
    monkeypatch.setenv(VAR, value)
    with pytest.raises(ValueError) as e:
        exchanges()
    assert str(e.value) == (f"{VAR}: 1 (each filter its own exchange with pandoc), or 0 or "
                            f'empty, not "{value}"')
    # read only for a call with filters
    assert libpandoc.convert("hi", to="plain") == "hi\n"
    # the options' value wins
    assert exchanges(separate_filters=False) is True
    assert exchanges(options={"separate-filters": True}) is False


def test_the_options_value_wins_over_the_variable(monkeypatch):
    monkeypatch.setenv(VAR, "1")
    assert exchanges(separate_filters=False) is True
    monkeypatch.setenv(VAR, "0")
    assert exchanges(separate_filters=True) is False


def test_a_value_not_a_boolean_is_an_option_error():
    with pytest.raises(PandocError) as e:
        exchanges(separate_filters="yes")
    assert e.value.kind == "PandocOptionError"
    assert e.value.message == "\"separate-filters\": True or False, not 'yes'"
    with pytest.raises(PandocError, match="separate-filters"):
        libpandoc.convert("hi", to="plain", separate_filters=1)


def test_its_not_passed_to_pandoc_and_read_many_ignores_it():
    """pandoc refuses an option it doesn't know: the key is this package's."""
    assert libpandoc.convert("hi", to="plain", separate_filters=True) == "hi\n"
    assert libpandoc.read("hi", separate_filters=True) == libpandoc.read("hi")
    assert libpandoc.write(libpandoc.read("hi"), "plain", separate_filters=False) == "hi\n"
    [doc] = libpandoc.read_many(["hi"], separate_filters=True)
    assert doc == libpandoc.read("hi")


def test_filters_are_told_the_options_without_it():
    told = []

    def f(doc, conversion):
        told.append(conversion.options)

    libpandoc.convert("hi", to="plain", tab_stop=3, separate_filters=False, filters=[f])
    assert told[0]["tab-stop"] == 3 and "separate-filters" not in told[0]


# -- the command line ----------------------------------------------------------------------


def pandocpy(capfd, *args):
    status = cli.main(list(args))
    out = capfd.readouterr()
    return status, out.out, out.err


def ran(err: str) -> list[str]:
    return [line.removeprefix("[INFO] Running filter ") for line in err.splitlines()
            if line.startswith("[INFO] Running filter ")]


def test_pandocpy_runs_one_callback_per_run(tmp_path, capfd, monkeypatch):
    md = tmp_path / "in.md"
    md.write_text("hi\n")
    lua = tmp_path / "f.lua"
    lua.write_text(LUA)
    a = script(tmp_path, "a.py", PANIR % "a")
    b = script(tmp_path, "b.py", PLAIN % "b")
    c = script(tmp_path, "c.py", PANIR % "c")
    args = ["--verbose", "-t", "plain", "-F", a, "-F", b, "-L", str(lua), "-F", c, "-F", a, str(md)]
    status, out, err = pandocpy(capfd, *args)
    assert status == 0, err
    assert paras(out) == ["hi", "a", "b", "lua", "c", "a"]
    assert ran(err) == ["libpandoc:callback/0", str(lua), "libpandoc:callback/1"]
    monkeypatch.setenv(VAR, "1")
    status, out, err = pandocpy(capfd, *args)
    assert status == 0, err
    assert paras(out) == ["hi", "a", "b", "lua", "c", "a"]
    assert ran(err) == ["libpandoc:callback/0", "libpandoc:callback/1", str(lua),
                        "libpandoc:callback/2", "libpandoc:callback/3"]


def test_pandocpy_names_the_failing_filter(tmp_path, capfd):
    md = tmp_path / "in.md"
    md.write_text("hi\n")
    g = script(tmp_path, "garble.py", GARBLE)
    s = script(tmp_path, "reads.py", PLAIN % "s")
    status, _, err = pandocpy(capfd, "-t", "plain", "-F", g, "-F", s, str(md))
    assert status == cli.FILTER_FAILED
    assert "filters[1]: this filter failed " + HINT.format(0) in err


@pytest.mark.parametrize("value", ["yes", "on"])
def test_pandocpy_a_bad_value_of_the_variable(tmp_path, capfd, monkeypatch, value):
    md = tmp_path / "in.md"
    md.write_text("hi\n")
    monkeypatch.setenv(VAR, value)
    status, _, err = pandocpy(capfd, "-t", "plain", "-F", script(tmp_path, "a.py", PANIR % "a"),
                              str(md))
    assert status == 1
    assert f'pandocpy: {VAR}: 1 (each filter its own exchange with pandoc), or 0 or empty, ' \
           f'not "{value}"' in err
    # without filters, not read
    status, out, _ = pandocpy(capfd, "-t", "plain", str(md))
    assert (status, out) == (0, "hi\n")


@needs_filters
def test_pandocpy_names_a_failing_wasm_filter(tmp_path, capfd):
    md = tmp_path / "in.md"
    md.write_text("hi\n")
    g = script(tmp_path, "garble.py", GARBLE)
    status, _, err = pandocpy(capfd, "-t", "plain", "-F", g, "-F", wasm("upper"), str(md))
    assert status == cli.FILTER_FAILED
    assert err.splitlines()[-1] == "filters[1]: this filter failed " + HINT.format(0)


# -- cancelling ------------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "emscripten", reason="no libpandoc tokens in Pyodide")
@pytest.mark.parametrize("main_thread", [True, False])
def test_a_cancel_stops_the_run_before_the_next_filter(main_thread):
    token = libpandoc.CancelToken()
    ran = []

    def cancelling(doc):
        token.cancel()

    def call():
        with pytest.raises(PandocCancelledError):
            libpandoc.convert("hi", to="plain", cancel=token,
                              filters=[cancelling, lambda d: ran.append(d)])

    if main_thread:
        call()
    else:
        t = threading.Thread(target=call)
        t.start()
        t.join()
    assert ran == []


# -- the directory sandbox --------------------------------------------------------------------


@needs_sandbox
def test_the_directory_sandbox_runs_them_alike(tmp_path):
    seen = Seen()
    p = script(tmp_path, "panir.py", PANIR % "p")
    box = {"read": [str(tmp_path)]}
    out = libpandoc.convert("hi", to="plain", sandbox=box, filters=[seen, p, seen])
    assert paras(out) == ["hi", "p"] and seen.docs[0] is seen.docs[1]
    seen.docs.clear()
    out = libpandoc.convert("hi", to="plain", sandbox=box, filters=[seen, p, seen],
                            separate_filters=True)
    assert paras(out) == ["hi", "p"] and seen.docs[0] is not seen.docs[1]
    g = script(tmp_path, "garble.py", GARBLE)
    with pytest.raises(ValueError) as e:
        libpandoc.convert("hi", to="plain", sandbox=box, filters=[g, seen])
    assert e.value.__notes__ == [
        "filters[1]: what it was given could not be read " + HINT.format(0)]


@needs_sandbox
def test_pandocpy_in_the_directory_sandbox(tmp_path, capfd, monkeypatch):
    md = tmp_path / "in.md"
    md.write_text("hi\n")
    a = script(tmp_path, "a.py", PANIR % "a")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LIBPANDOC_SANDBOX", str(tmp_path))
    status, out, err = pandocpy(capfd, "--verbose", "-t", "plain", "-F", a, "-F", a, "in.md")
    assert status == 0, err
    assert paras(out) == ["hi", "a", "a"] and ran(err) == ["libpandoc:callback/0"]
    monkeypatch.setenv(VAR, "1")
    status, out, err = pandocpy(capfd, "--verbose", "-t", "plain", "-F", a, "-F", a, "in.md")
    assert status == 0, err
    assert ran(err) == ["libpandoc:callback/0", "libpandoc:callback/1"]


def test_the_cli_plan_chains_installed_filters_scripts_and_wasm():
    """(the plan alone: entries and runs)"""
    entries, runs = cli.plan([{"type": "json", "path": "x"}, {"type": "lua", "path": "f.lua"}],
                             {"x": _EntryPoint(adding("x"))})
    assert entries == [{"type": "callback", "index": 0}, {"type": "lua", "path": "f.lua"}]
    assert [i for i, _ in runs[0].members] == [0]


class _EntryPoint:
    def __init__(self, f):
        self.f = f

    def load(self):
        return self.f


def test_a_filter_object_is_typed():
    f = Filter()
    f.on(Pandoc)(lambda doc: doc.blocks.append(Para("f")))
    seen = Seen()
    out = libpandoc.convert("hi", to="plain", filters=[seen, f, seen])
    assert paras(out) == ["hi", "f"] and seen.docs[0] is seen.docs[1]
