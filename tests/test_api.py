"""The high-level API behaves like the pandoc CLI."""

import inspect
import pathlib
import pickle
import sys
import sysconfig
import threading
import time

import panir
import pytest

import libpandoc as pandoc


def test_versions():
    assert pandoc.pandoc_version().startswith("3.")
    assert pandoc.pandoc_api_version()[:2] == panir.PANDOC_API_VERSION[:2]
    # libpandoc.h's rule, checked at import (natively and in Pyodide)
    abi, need = pandoc._core.abi_version(), pandoc._core.MIN_ABI_VERSION
    assert need == 1011 and abi // 1000 == need // 1000 and abi >= need
    # (_wasm.py, Pyodide's _core, has its own copy of the number)
    wasm = (pathlib.Path(pandoc.__file__).parent / "_wasm.py").read_text(encoding="utf-8")
    assert f"\nMIN_ABI_VERSION = {need}\n" in wasm


@pytest.mark.skipif(not sysconfig.get_config_var("Py_GIL_DISABLED"),
                    reason="free-threaded CPython only")
def test_free_threaded_python_stays_so():
    """Importing libpandoc (and panir) doesn't turn the GIL back on."""
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


def test_paths_as_text_bytes_or_path_objects(tmp_path):
    src = tmp_path / "café.md"
    src.write_text("*x*")
    for p in (src, str(src), bytes(src)):
        assert pandoc.convert(input_files=[p]) == "<p><em>x</em></p>\n"
        assert pandoc.run([p]) == b"<p><em>x</em></p>\n"
    # in read_many's options too
    assert pandoc.read_many(["x"], data_dir=tmp_path, resource_path=[tmp_path])[0].blocks


def test_relative_paths(tmp_path, monkeypatch):
    # relative to Python's working directory, in Pyodide too
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel.md").write_text("# rel\n")
    assert pandoc.convert(input_files=["rel.md"], output_file="rel.html") == ""
    assert (tmp_path / "rel.html").read_text() == '<h1 id="rel">rel</h1>\n'


# Every public function that takes options as keywords, with an example
# call: its first argument, its named argument that is also an option (and
# that option's name and pandoc's other name for it), a value and the
# result expected with it.
DOC = panir.Pandoc(panir.Para(panir.Emph("x")))
NAMED = {
    "convert": None,  # (only the source, which is no option)
    "query": None,  # (its name, which is no parameter of the query)
    "read": (r"\emph{x}", "from_", "from", "reader", "latex", DOC),
    "read_many": ([r"\emph{x}"], "from_", "from", "reader", "latex", [DOC]),
    "write": (DOC, "to", "to", "writer", "rst", "*x*\n"),
}


def test_no_argument_falls_into_the_options():
    """A named argument given by keyword reaches its parameter, not the
    options, where the default overwrote it: read(text, from_="latex") read
    markdown, write(doc, to="rst") wrote HTML. The format is also taken
    from the options, by either of pandoc's names."""
    with_options = [
        name for name in pandoc.__all__
        if inspect.isfunction(f := getattr(pandoc, name))
        and any(p.kind is p.VAR_KEYWORD for p in inspect.signature(f).parameters.values())
    ]
    assert sorted(with_options) == sorted(NAMED)  # (a new one: add it above)
    for name in with_options:
        params = inspect.signature(getattr(pandoc, name)).parameters.values()
        assert not [p.name for p in params if p.kind is p.POSITIONAL_ONLY], name
    for name, example in NAMED.items():
        if example is None:
            continue
        f = getattr(pandoc, name)
        first, param, option, alias, value, want = example
        assert f(first, value) == want, name
        assert f(first, **{param: value}) == want, name
        assert f(first, options={option: value}) == want, name
        assert f(first, **{alias: value}) == want, name


def test_options_a_function_overrides_are_refused():
    doc = pandoc.read("*x*")
    for call in (lambda: pandoc.read("x", to="rst"),
                 lambda: pandoc.read("x", output_file="o.json"),
                 lambda: pandoc.write(doc, from_="latex"),
                 lambda: pandoc.write(doc, input_files=["a.md"]),
                 lambda: pandoc.read_many(["x"], filters=["f.lua"]),  # (libpandoc ignores these)
                 lambda: pandoc.read_many(["x"], standalone=True)):
        with pytest.raises(TypeError, match="doesn't take"):
            call()


def test_what_convert_returns(tmp_path):
    assert pandoc.convert("x", writer="docx")[:2] == b"PK"  # (pandoc's other name for to)
    assert pandoc.convert("x", output_file=None) == "<p>x</p>\n"  # (None, "-": no file)
    assert pandoc.convert("x", to="docx", output_file="-")[:2] == b"PK"
    writer = tmp_path / "my-writer.lua"  # (a "-" in a Lua writer's name)
    writer.write_text('function Writer(doc, opts) return "WROTE" end\n')
    assert pandoc.convert("x", to=str(writer)) == "WROTE\n"


def test_arguments_of_the_wrong_kind():
    with pytest.raises(TypeError, match="not one text"):
        pandoc.read_many("abc")  # (three texts, "a", "b" and "c")
    with pytest.raises(TypeError, match="not one string"):
        pandoc.run("-t rst")
    with pytest.raises(TypeError, match="not one filter"):
        pandoc.convert("x", filters="f.lua")
    with pytest.raises(TypeError, match="not PosixPath|not WindowsPath"):
        pandoc.convert(pathlib.Path("a.md"))


def test_no_input_is_refused():
    """With neither a source nor input files, pandoc would read this
    process's standard input and close it (sys.stdin then fails)."""
    for call in (pandoc.convert, pandoc.read, lambda: pandoc.convert(None, input_files=[])):
        with pytest.raises(TypeError, match="needs a source or input_files"):
            call()


def test_read_runs_filters_in_python():
    def emph(doc):
        doc.blocks[0].content[0] = panir.Emph("x")

    assert pandoc.read("x", filters=[emph]) == DOC


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


def test_a_warning_names_the_line_that_called():
    """Each function's warnings point at its caller, however many of the
    package's functions are in between (it was the caller's caller)."""
    doc = pandoc.read("![](nope.png)")
    calls = [
        lambda: pandoc.convert("![](nope.png)", to="docx"),
        lambda: pandoc.write(doc, "docx"),
        lambda: pandoc.read(r"\input{nope}", "latex"),
        lambda: pandoc.read_many([r"\input{nope}"], "latex"),
    ]
    for call in calls:
        with pytest.warns(pandoc.PandocWarning) as w:
            call()
        assert (w[0].filename, w[0].lineno) == (__file__, call.__code__.co_firstlineno)


# The review's 100,000 nested block quotes: over 15 s without a time limit.
SLOW = ">" * 100_000


def test_a_call_has_a_time_limit():
    started = time.monotonic()
    with pytest.raises(pandoc.PandocTimeoutError) as e:
        pandoc.convert(SLOW, to="html", timeout=0.5)
    assert time.monotonic() - started < 5
    assert e.value.kind == "Timeout" and isinstance(e.value, pandoc.PandocError)
    assert isinstance(e.value, TimeoutError)
    assert repr(pickle.loads(pickle.dumps(e.value))) == repr(e.value)
    with pytest.raises(TimeoutError):
        pandoc.read(SLOW, timeout=0.5)
    # read_many: the texts not read in time, in their places
    started = time.monotonic()
    a, slow, b = pandoc.read_many(["*a*", SLOW, "b"], timeout=0.5, return_exceptions=True)
    assert time.monotonic() - started < 5
    assert isinstance(slow, pandoc.PandocTimeoutError), slow
    assert a.blocks and b.blocks
    with pytest.raises(pandoc.PandocTimeoutError, match="reading input 1"):
        pandoc.read_many(["a", SLOW], timeout=0.5)
    # and the next call works
    assert pandoc.convert("*hi*") == "<p><em>hi</em></p>\n"


def test_a_python_filter_runs_to_its_end_past_the_time_limit():
    """A Python filter is code in this process, which pandoc's time limit
    can't stop: the conversion fails when it returns."""
    ran = []

    def slow(doc):
        time.sleep(1)
        ran.append(True)

    started = time.monotonic()
    with pytest.raises(pandoc.PandocTimeoutError):
        pandoc.convert("x", filters=[slow], timeout=0.2)
    assert ran == [True] and time.monotonic() - started >= 1


def test_read_many_warns_as_convert_does():
    """Its reader's messages are in its log (libpandoc 1.9; before, on
    standard error), issued as PandocWarnings."""
    with pytest.warns(pandoc.PandocWarning, match="Could not load include file") as w:
        docs = pandoc.read_many(["\\input{no-such-file} x", "y"], "latex")
    assert len(docs) == 2 and len(w) == 1


def test_read_many_keeps_each_texts_error():
    a, loop, c = pandoc.read_many(["a", "\\newcommand{\\a}{\\a}\\a", "c"], "latex",
                                  return_exceptions=True)
    assert a.blocks and c.blocks
    assert type(loop) is pandoc.PandocError and loop.message.startswith("reading input 1:")


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
    assert pandoc.num_threads() >= 1
    # Setting the number: in a process of its own, where pandoc's threads
    # haven't grown yet (it changes nothing after that, and another test
    # here may have run calls in parallel already).
    import subprocess
    import sys
    code = (
        "import libpandoc as p\n"
        "a = p.set_num_threads(2); b = p.num_threads(); c = p.convert('*x*')\n"
        "d = p.set_num_threads(0)\n"  # (clamped)
        "p.set_num_threads(2); p.read_many(['a', 'b'])\n"  # (two texts: grown)
        "print(a, b, d, c.strip(), p.set_num_threads(1), p.num_threads())"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    most = min(2, __import__("os").cpu_count() or 1)
    assert out.stdout.strip() == f"{most} {most} 1 <p><em>x</em></p> {most} {most}"


def test_num_threads_from_the_environment():
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c", "import libpandoc; print(libpandoc.num_threads())"],
        env={**__import__("os").environ, "LIBPANDOC_NUM_THREADS": "3"},
        capture_output=True, text=True, check=True,
    ).stdout
    assert out.strip() == "3"


def test_untrusted(tmp_path):
    # libpandoc's "untrusted": only options that read, write, fetch and run
    # nothing, with pandoc's sandbox on
    assert pandoc.convert("*hi*", to="html", untrusted=True) == "<p><em>hi</em></p>\n"
    for opts in ({"filters": ["x.lua"]}, {"output_file": str(tmp_path / "o")},
                 {"to": "pdf"}, {"data_dir": str(tmp_path)}):
        with pytest.raises(pandoc.PandocError, match="not allowed for untrusted code"):
            pandoc.convert("x", untrusted=True, **{"to": "html", **opts})
    secret = tmp_path / "secret.tex"
    secret.write_text("SECRET")
    tex = f"\\input{{{secret.as_posix()}}}"  # / on Windows too, for LaTeX
    assert "SECRET" in pandoc.convert(tex, from_="latex", to="plain")
    with pytest.warns(pandoc.PandocWarning, match="Could not load include file"):
        assert "SECRET" not in pandoc.convert(tex, from_="latex", to="plain", untrusted=True)


SHUTDOWN = """
import sys, threading, time, libpandoc as p
def slow(doc):
    time.sleep(1)
work = {"filter": lambda: p.convert("x", filters=[slow]), "long": lambda: p.convert(">" * 12000)}
threading.Thread(target=work[sys.argv[1]], daemon=True).start()
time.sleep(0.2)
print("exits")
"""


@pytest.mark.skipif(sys.platform == "emscripten", reason="no processes in Pyodide")
@pytest.mark.parametrize("mode", ["filter", "long"])
def test_exit_with_a_call_running_in_a_daemon_thread(mode):
    """The process ends at once and cleanly, the thread inside pandoc or
    inside a Python filter that will want the GIL back."""
    import subprocess
    started = time.monotonic()
    out = subprocess.run([sys.executable, "-c", SHUTDOWN, mode], capture_output=True, text=True,
                         timeout=60, check=False)
    assert (out.returncode, out.stdout) == (0, "exits\n"), out.stderr
    assert time.monotonic() - started < 30


@pytest.mark.skipif(sys.platform == "emscripten", reason="no subinterpreters in Pyodide")
@pytest.mark.skipif(sys.version_info < (3, 14), reason="concurrent.interpreters: 3.14")
def test_a_subinterpreter_is_refused_cleanly():
    """The extension has single-phase initialization: an isolated
    interpreter refuses it with an ImportError; this one goes on."""
    from concurrent import interpreters

    interp = interpreters.create()
    try:
        with pytest.raises(interpreters.ExecutionFailed, match="ImportError"):
            interp.exec("import libpandoc")
    finally:
        interp.close()
    assert pandoc.convert("*y*") == "<p><em>y</em></p>\n"
