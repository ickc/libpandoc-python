# libpandoc (Python)

[pandoc](https://pandoc.org) as a Python library. Calls run pandoc in
process, through [libpandoc](https://github.com/ickc/libpandoc)'s C ABI and
a CPython extension, not by running the `pandoc` executable. Documents come
back as typed Python objects generated from pandoc's own type definitions.

```python
import libpandoc as pandoc

pandoc.convert("*hi*", from_="markdown", to="html")      # '<p><em>hi</em></p>\n'
pandoc.convert("# Report", to="docx")                    # bytes
pandoc.convert(input_files=["a.md"], output_file="a.pdf", pdf_engine="typst")
pandoc.run(["-f", "markdown", "-t", "latex", "--citeproc"], input=src)  # the CLI, exactly

from panir import Filter, Header

f = Filter()

@f.on(Header)
def demote(h):
    h.level += 1

pandoc.convert("# Hi", to="html", filters=[f])           # '<h2 id="hi">Hi</h2>\n'
doc = pandoc.read("Hello *world*")    # Pandoc(Para(Str('Hello'), Space(), Emph(Str('world'))))
pandoc.write(f(doc), "plain")
```

- **Options** are pandoc's [defaults-file](https://pandoc.org/MANUAL.html#defaults-files)
  keys, as keyword arguments (`_` for `-`, `from_` for `from`) or a dict.
  `run` takes command-line arguments, parsed by pandoc itself.
- **Errors** raise `PandocError`, with `kind` set to pandoc's error
  constructor (`"PandocParseError"`, ...). **Warnings** are issued as
  `PandocWarning`.
- **Threads:** the GIL is released while pandoc runs, so conversions in
  different threads run in parallel. pandoc runs on one thread per logical
  core this process may use, or `$LIBPANDOC_NUM_THREADS` (read at start,
  like `$OMP_NUM_THREADS`); `num_threads()` and `set_num_threads(n)` query
  and change it. 2000 small conversions from a pool of Python threads run
  5 times as fast on 16 threads as on one; on a 16-core, 32-thread CPU, 16
  was as fast as 32.
- **Untrusted input:** `untrusted=True` accepts only options that read no
  files, write none, fetch nothing and run nothing, with pandoc's sandbox
  on (libpandoc's list; anything else raises a `PandocError` naming it):
  for documents or options from someone you don't trust.
- **Filters**: `filters=` takes pandoc's (Lua or JSON filter paths,
  `"citeproc"`) and Python ones, mixed, in order. A Python filter is a
  [panir](https://github.com/ickc/panir) `Filter`, or a function that
  takes a document (and optionally the `panir.Conversion`) and changes it.
  Python filters run in this process, inside the one pandoc conversion, as
  pandoc runs a Lua filter: what the reader keeps in memory (images embedded
  in a docx) reaches the writer, and a filter's exception is raised from
  `convert` as it is. A filter may call pandoc itself (`convert`, or
  `ctx.read(text)` to parse a fragment the way the document was read).

## Wasm filters

`filters=["foo.wasm"]` (or `WasmFilter(path)`), and `pandocpy -F
foo.wasm`, run a filter compiled to WebAssembly in this process, with
wasmtime (`pip install libpandoc[wasm]`): a pandoc JSON filter built for
WASI (`wasm32-wasip1`), such as any [panir](https://github.com/ickc/panir)
Rust filter; see [libpandoc-rs](https://github.com/ickc/libpandoc-rs). The
same file runs in pandocrs, pandocjl and libpandoc.wasm (browsers
included), and under pandoc itself through a wrapper.

It runs sandboxed, whatever pandoc's options. Python, Lua and JSON filters
can do anything you can (pandoc's `--sandbox` limits readers and writers,
not filters); a wasm filter can't:

- **files:** it sees the current directory, read-only (`dirs=`,
  `writable=` for others); no network, no programs;
- **calls to pandoc** (libpandoc-rs's `libpandoc` crate, built for wasm):
  in pandoc's sandbox, with no options that read or write files, fetch or
  run anything (libpandoc checks them: `"untrusted"`, 1.7);
- **time and memory**, which pandoc limits for no filter:
  `$LIBPANDOC_WASM_TIMEOUT` (seconds, as pandoc-server's `--timeout`; its
  calls to pandoc included) and `$LIBPANDOC_WASM_MAX_MEMORY` (bytes, or with
  `k`, `m`, `g`, as pandoc's `+RTS -M`) stop a filter past them, with a
  `WasmFilterError`; none by default, as pandoc. `WasmFilter(path,
  timeout=, max_memory=)` for one filter (0: none).

## Reading fragments, from filters

`read_many` parses many texts at once, each on its own, in parallel: for
filters that parse fragments such as table cells. For 2000 cells, pandoc's
part takes 138 ms on one thread, as long as one document joining them all
(155 ms, which also lets the cells affect each other), and 38 ms on 16;
turning the result into panir objects takes another 26 ms. A `read` each
takes 725 ms. Given the conversion a filter
runs in, it reads them the way that conversion reads its input:

```python
@f.on(CodeBlock)
def cells(code, ctx):
    docs = pandoc.read_many(code.text.splitlines(), ctx.conversion)
    return [b for d in docs for b in d.blocks]
```

`read(text, ctx.conversion)` does the same for one text, and
`read_options(conversion)` gives the options used.

## pandocpy

`pandocpy` is pandoc's command line, run in process through libpandoc: it
takes pandoc's arguments and does what pandoc does. The difference is that a
filter named with `-F` that is an installed Python filter runs in this
process, inside the conversion, where it knows how the document is read and
can call pandoc cheaply:

```toml
# pyproject.toml of a filter package
[project.entry-points."panir.filters"]
pantable = "pantable:filter"    # a panir.Filter, or a function
```

```sh
pandocpy -F pantable input.md -o output.html   # pantable in process
pandocpy -F other-filter input.md              # anything else: as pandoc does
```

Python filter scripts (`-F foo.py`, panflute, pandocfilters, panir or plain
JSON) also run in this process, as their own `__main__` with the document as
their standard input, saving a Python start and its imports per filter
(about 30 ms each). The same holds for `convert(filters=["foo.py"])`, from
any number of threads: while scripts run, `sys.stdin`, `sys.stdout`,
`sys.argv` and `os.environ` are each thread's script's own there, and the
real ones elsewhere. Scripts run with this Python and its packages either
way. A script runs as a subprocess instead:

- if its author says so, with a line `# pandocpy: subprocess` near its top;
- if the user says so: `PANDOCPY_SUBPROCESS=foo.py,bar` (paths, file names,
  names without `.py`, or `*` for all);
- if it fails in process: it is run again as a subprocess, with a warning.

`python -m libpandoc` is the same as `pandocpy`, which is pandoc's command
line exactly (pandoc's own command tests pass), `pandocpy lua` included:
pandoc as a Lua interpreter. Only `pandoc server` is not supported.

## The AST

Documents are [panir](https://github.com/ickc/panir) objects: pandoc's
types as Python classes, generated from pandoc-types, with checked fields,
pandoc's JSON, and filters. It is a separate, pure-Python package (it needs no
libpandoc), so filters written with it also run under the `pandoc`
executable. At import, this package checks that panir and the loaded pandoc
speak the same API version.

## Installing

Wheels are `abi3`: one per platform serves every CPython ≥ 3.10. Each
bundles libpandoc (about 60 MB).

To build against an installed libpandoc (for example a conda environment,
or a staged `dist/` prefix):

```sh
LIBPANDOC_PREFIX=/path/to/prefix pip install .
```

The extension then finds the library in that prefix when it runs (an
RPATH). `LIBPANDOC_RPATH` sets another, e.g. `$ORIGIN/...` relative to the
extension, or empty for none (a wheel whose repair tool bundles the
library). libpandoc-rs reads both variables the same way.

### In the browser (Pyodide): a prototype

On Pyodide, the package runs pandoc as libpandoc.wasm, which the browser's
engine runs (libpandoc's `wasm/`), instead of the native library: the same
API, Python filters included. The host loads libpandoc.wasm and registers
it before `import libpandoc`; how the package will load it itself is to be
decided with its packaging.

```js
import { load } from "libpandoc/wasm/browser.mjs";
import { emscriptenDirectory } from "libpandoc/wasm/emscripten-fs.mjs";
const pandoc = await load(wasmUrl, { tmp: emscriptenDirectory(pyodide.FS, "/tmp") });
pyodide.registerJsModule("libpandoc_wasm", pandoc.abi);
await pyodide.runPythonAsync("import libpandoc; print(libpandoc.convert('*hi*', to='html'))");
```

46 of the 49 API and AST tests pass there; the other 3 need threads, which
wasm doesn't have. A document too large for wasm32's 4 GiB (about 80 MB of
markdown) stops the libpandoc.wasm instance for good: calls then raise a
`JsException` named `StoppedError` (libpandoc's `wasm/README.md`, "When an
instance stops"); for now, restart Pyodide. It needs a browser with wasm's exnref exception
handling (Chromium 138, Firefox 132, Safari 18.2 or later).

## Performance

`bench/roundtrip.py`, on pandoc's test suite repeated 50 times (0.46 MB of
markdown):

| | ms |
|---|---|
| markdown → html | 1097 |
| same, with a Lua filter uppercasing every `Str` | 1352 |
| same filter in Python (`convert(filters=[f])`) | 1551 |
| of which Python's share (json.loads, from_json, to_json, json.dumps) | 145 |

pandoc's reader and writer dominate. Exchanging the AST as JSON, into
checked Python objects, costs about 15% over a Lua filter.

## License

GPL-2.0-or-later, as pandoc: the package links pandoc.
