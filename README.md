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
  A value that is `None` is an absent option, as Lua's `nil` in a table
  (`timeout=None`: no time limit; in a sandbox, `"timeout": None`: the
  environment's); a list with `None` in it is an error.
  `read(text, "latex")` and `write(doc, "rst")` take the format as their
  second argument (or `from_=`, `to=`, or in the options); an option a
  function would override or ignore (`read(text, to=...)`, `read_many`'s
  options that don't affect reading) is a `TypeError`, as an unknown one
  is a `PandocError`. `run` takes command-line arguments, parsed by pandoc
  itself.
- **Errors** raise `PandocError`, with `kind` set to pandoc's error
  constructor (`"PandocParseError"`, ...). **Warnings** are issued as
  `PandocWarning` (`read_many`'s too).
- **Time limit:** `timeout=` (seconds) on `convert`, `read`, `write` and
  `read_many` stops a call still running after that long with a
  `PandocTimeoutError` (a `PandocError` and a `TimeoutError`); the process
  goes on. `read_many(..., return_exceptions=True)` keeps the texts read in
  time, each other one's error in its place. It stops pandoc's own code and
  the JSON filters pandoc runs (and Python scripts run as subprocesses),
  not code in this process (a Python filter runs to its end, and the call
  then fails) nor Lua filters. `run` and
  `pandocpy` have none, as pandoc has none.
- **Cancelling:** `cancel=` a `CancelToken` on the same functions stops a
  call when `token.cancel()` is called, from another thread or from a
  filter: `PandocCancelledError` (a `PandocError`), the process going on.
  It stops what `timeout` stops (in the directory sandbox, the whole
  conversion, Lua included). A token stays cancelled, and serves any number
  of calls, one after another or at once. With `untrusted=True` the token
  goes inside the key the host writes, `"untrusted": {"cancel-token": N}`
  (libpandoc 1.14), not beside it: a top-level `"cancel-token"` with
  `"untrusted"` is refused, as the code could have written it.

  ```python
  token = pandoc.CancelToken()
  threading.Timer(5, token.cancel).start()      # a cancel button, a deadline
  pandoc.convert(text, to="html", cancel=token)
  ```

  **Ctrl-C** (`KeyboardInterrupt`) stops a call made on the main thread
  within milliseconds (it cancels the call's token, or one of its own),
  natively and in the directory sandbox, `untrusted=True` too; not `run`.
  For that,
  a call on the main thread runs on a helper thread, one for the process,
  while the main thread waits, where Python delivers Ctrl-C: Python
  filters pandoc calls back then run on that thread, in a copy of the
  caller's `contextvars` context (about 8 µs a call, 2.5% of a tiny
  conversion; a new thread for each call cost 70 µs). Measured: 35 to 60
  ms from the signal to the process's end, natively and in the sandbox.
  Loading libpandoc.wasm can't be interrupted: Ctrl-C (or a cancel) while
  the sandbox loads it stops the call once it has loaded, up to 0.25 s
  later from wasmtime's cache, up to its first compilation's 20 s
  otherwise. `pandocpy` dies on Ctrl-C at once, as
  pandoc does.
- **Threads:** the GIL is released while pandoc runs, so conversions in
  different threads run in parallel. A Python filter runs on the thread
  that called pandoc (for the main thread, the helper thread above). pandoc runs on one thread per logical
  core this process may use, or `$LIBPANDOC_NUM_THREADS` (read at start,
  like `$OMP_NUM_THREADS`); `num_threads()` tells the number. pandoc
  starts on one thread and grows to that number the first time it runs
  something in parallel; `set_num_threads(n)` changes the number only
  before that (after, it changes nothing and returns the number in
  effect). 2000 small conversions from a pool of Python threads run 5
  times as fast on 16 threads as on one; on a 16-core, 32-thread CPU, 16
  was as fast as 32.
- **Untrusted input:** `untrusted=True` is about options: it accepts only
  those that read no files, write none, fetch nothing and run nothing, with
  pandoc's sandbox on and nothing read from the user data directory
  (libpandoc's list; anything else raises a `PandocError` naming it), for
  options from code you don't trust: citeproc with the metadata's
  references or files given as their contents (`citeproc_files=`, below
  "Wasm filters"), not `bibliography`. It bounds neither a document's time
  nor its memory, and there is no memory limit for a call in this process
  (running out of memory ends it). For documents from someone you don't
  trust: the **directory sandbox** (below), or `sandbox=True` (pandoc's
  `--sandbox`; citeproc too) or `untrusted=True` so that pandoc reads
  nothing the document names, with a `timeout` and memory of its own (a
  separate process, or a container with a memory limit).
- **Filters**: `filters=` takes pandoc's (Lua or JSON filter paths,
  `"citeproc"`, the include step `"panclude"`: below) and Python ones,
  mixed, in order. A Python filter is a
  [panir](https://github.com/ickc/panir) `Filter`, or a function that
  takes a document (and optionally the `panir.Conversion`) and changes it.
  Python filters run in this process, inside the one pandoc conversion, as
  pandoc runs a Lua filter: what the reader keeps in memory (images embedded
  in a docx) reaches the writer, and a filter's exception is raised from
  `convert` as it is. A filter that takes the conversion
  (`ctx.conversion`, a `panir.Conversion`) knows the formats pandoc decided
  on, the reader's and the writer's options (`writer_options`: `-N`,
  `--top-level-division`, ..., as Lua's `PANDOC_WRITER_OPTIONS`) and, from
  `convert`, the options themselves. A filter may call pandoc itself
  (`convert`, or `read(text, ctx.conversion)` to parse a fragment the way
  the document was read, in pandoc's sandbox when the conversion is
  sandboxed: "Reading fragments, from filters"). Many small reads should
  go through one `read_many`: 10,011 small markdown texts take 0.20 s
  batched against 3.50 s with a call each (0.35 ms a call). Filter scripts and wasm filters get
  `PANDOC_WRITER_OPTIONS` in their environment, as libpandoc's JSON
  filters do. Consecutive filters of this process are one exchange with
  pandoc (below).

## Consecutive filters

pandoc hands a filter in this process the whole document as its JSON and
reads back the one it returns: an exchange, about 0.3 s each on a 9.8 MB
textbook, most of it pandoc decoding what comes back, and for Python
filters 0.2 s more, panir parsing and writing it. **Consecutive
filters of this process share one exchange, by default**: Python filters
(`panir.Filter`s, functions), Python filter scripts (in this process or as
a subprocess) and wasm filters (`WasmFilter`, `.wasm` paths). One of
pandoc's own (a Lua filter, a JSON filter program, `citeproc`, the include
step) ends a run.
The same in `convert`, `read`, `write`, `pandocpy` (`-F a.py -F b.py`) and
the directory sandbox. Within a run, from one filter to the next:

- from a Python filter to another: the same `panir.Pandoc` object, as it
  is;
- from a Python filter to one on bytes (a wasm filter, a script reading
  its standard input, a script run as a subprocess): written once;
- from one on bytes to a Python filter: parsed once;
- from one on bytes to another: the bytes exactly as written, unchecked.

A script run in this process is either, as it turns out when it runs: a
panir script whose `main()` hands its `Filter`s over is a Python filter
(given the document the filter before it left, parsed, and leaving it
parsed: two such scripts in a row parse the document once and write it
once), and one that reads its standard input (panflute, pandocfilters,
plain JSON) is on bytes, which are written from the document only when it
reads them. A wasm filter keeps its own limits and directories, and its
calls into pandoc are as they were.

On the textbook, JSON to HTML (`convert`, best and median of 7 runs, in
seconds; a 32-thread machine, otherwise idle):

| filters | one exchange (the default) | each its own |
|---|---|---|
| none | 2.18 / 2.18 | |
| 1 wasm `identity` | 2.48 / 2.48 | |
| 3 wasm `identity` | 2.52 / 2.53 | 3.11 / 3.11 |
| 2 Python filters doing nothing | 2.68 / 2.69 | 3.18 / 3.21 |
| 3 Python filters doing nothing | 2.67 / 2.69 | 3.72 / 3.72 |
| 2 panir scripts doing nothing | 2.68 / 2.69 | 3.21 / 3.22 |
| 3 panir scripts doing nothing | 2.68 / 2.69 | 3.72 / 3.74 |

**The switch:** `separate_filters=True` (the option `"separate-filters"`)
makes every filter its own exchange, Python filters in a row too, so that
pandoc reads and checks the document between any two. It is this
package's option, not pandoc's: taken out of the options before pandoc
sees them, and not among those filters are told. A value that isn't
`True` or `False` is a `PandocError` (`PandocOptionError`); `read_many`
takes it and ignores it. When the options don't say, it is
`LIBPANDOC_SEPARATE_FILTERS`, read for a call with filters: `1` on; unset,
empty or `0` off; anything else a `ValueError`, `LIBPANDOC_SEPARATE_FILTERS:
1 (each filter its own exchange with pandoc), or 0 or empty, not "yes"`
(a safety switch is not to be ignored). `pandocpy` obeys the variable
(a bad value: that message, exit status 1).

**The safety side.** In a run, pandoc doesn't read the document between
two filters: a later filter is given exactly what the earlier one left,
and after one on bytes, its bytes. pandoc always checks the final
document. The switch is for finding which filter broke a document, and for
not letting one filter's bytes reach the next unchecked.

What changes for a filter's author, against the same filters apart (each
seen in this package's tests or by hand):

- The next filter gets what the one before left, not the document pandoc
  read and wrote again: the same `Pandoc` object. A filter that keeps a
  reference to the document after it returns sees what later filters of
  the run do to it. The metadata is a dict in the order the filter left
  it (apart: pandoc's, sorted by key). A document nested deeper than panir
  can write (20,000 block quotes, past Python's recursion limit) reaches
  the next filter, which may undo it, where apart writing it fails
  (panir's `ASTDepthError`) after the filter that made it. After a filter
  on bytes, the next is given its bytes as they are.
- A script reading its standard input after a Python filter reads what
  panir wrote, not pandoc: the same document (byte for byte pandoc's, on
  the documents tried). Its standard input is read once and isn't
  seekable, as a pipe (`sys.stdin.buffer` is an `io.BufferedReader`).
- A document pandoc would refuse is refused when the run ends, not after
  the filter that made it (or a filter of the run that can't read it
  fails first, below).
- pandoc's log (`pandocpy --verbose`) counts a run as one filter: one
  `Running filter libpandoc:callback/0` for it.
- State behaves the same either way: the same process; a script runs
  afresh for each conversion, and a function or a `Filter` is the same
  object for every one.
- A failing filter fails the run, and those after it don't run. Its
  exception is raised as it is, its type unchanged, with a note naming the
  filter by its place in the `filters` given, counted from 0 (in
  `pandocpy`, in pandoc's list of filters, `-F`, `-L` and `--citeproc` in
  order): `filters[2]: this filter failed`, or `filters[2]: what it was
  given could not be read` when panir could not parse its input.
  When that went unchecked from a filter on bytes before it, the note
  says whose it was: `(its input is what filters[1] wrote, unchecked:
  LIBPANDOC_SEPARATE_FILTERS=1 has pandoc check it)`. A traceback shows the
  note (`pandocpy` prints it; a `WasmFilterError` it prints with its
  note); pandoc's own message names the callback, which is the run. A time
  limit's `PandocTimeoutError` (a script run as a subprocess) gets the note
  too.
- Cancelling: between two filters of a run, the call's `CancelToken` (or
  Ctrl-C's) is looked at, and a cancelled one stops the run before the
  next (`PandocCancelledError`). A `timeout` is not: a run goes to its end,
  as one filter does, and the call fails then.

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
not filters); a wasm filter by itself can't (through the document it
returns it can: below the list):

- **files:** **no directory**, as a wasm filter is for code you don't
  trust, unless `LIBPANDOC_WASM_DIRS` gives some: directories as `PATH`
  lists them (`:`, `;` on Windows; unset or empty: none), read-only, the
  same for every wasm filter, in the directory sandbox too, read when the
  filter runs (as its limits are). One that doesn't exist or isn't a
  directory fails the filter before it runs, with a `WasmFilterError`
  (`LIBPANDOC_WASM_DIRS: X does not exist`, `... is not a directory`). A
  filter that fails while it was given none says so: `(it was given no
  directory: LIBPANDOC_WASM_DIRS)`. A program gives one filter its own
  instead, whatever the variable says: `WasmFilter(path, dirs=[...])`,
  directories seen as the variable's are (`dirs=[]`: none, the hint then
  `(it was given no directory)`), and `preopens=[(host, guest,
  writable)]`, a directory seen as `guest` exactly, writable if so.

  ```sh
  LIBPANDOC_WASM_DIRS=.:../common pandocpy -F include.wasm input.md
  ```

  The filter sees each directory under its absolute path (`C:\x` as `/c/x`
  on Windows; its real path too, if a link leads to it) and under its
  path relative to the working directory (`.`, `..`, `../common`, `img`;
  none on another drive), and the working directory as `.` when it lies
  inside one: so `here.txt`, `../common/part.txt` and absolute names work
  as for a native filter. The filter's own libc (wasi-libc) matches the
  beginning of a path against those names, without normalising it:
  `img/../../common/x` doesn't work ("Operation not permitted"), and a
  leading `./` is not to be relied on (`./../common/x`; `./img/x` when
  only `img` is given): the same Rust filter drops it built without
  link-time optimisation, and keeps it, so fails, built with `lto = true`.
  A symbolic link is refused when it leaves the directory its name is
  opened through, even into another one given. No network, no programs.
  In WASI an empty
  name is the working directory: `open("")` opens a directory, not a
  missing file;
- **environment:** the variables pandoc gives filters (`PANDOC_VERSION`,
  `PANDOC_READER_OPTIONS`, and libpandoc's `PANDOC_WRITER_OPTIONS`,
  `PANDOC_INPUT_FORMAT`, `PANDOC_OUTPUT_FORMAT`, `PANDOC_SANDBOX`) and
  **none of this process's** unless named. Environment variables configure
  filters (pandoc-ext's pagebreak reads `PANDOC_PAGEBREAK_HTML_CLASS` and
  `PANDOC_PAGEBREAK_ODT_STYLE`), and they also hold tokens and passwords,
  which a filter run without auditing it is not to have.
  `LIBPANDOC_WASM_ENV` names some to give every wasm filter, with this
  process's values: names separated by commas (empty entries skipped; one
  the process doesn't have is left out), read when the filter runs, as
  its directories are:

  ```sh
  LIBPANDOC_WASM_ENV=MYFILTER_STYLE,MYFILTER_LANG pandocpy -F myfilter.wasm input.md
  ```

  An entry that can't be a variable's name (with `=`, a NUL or white
  space) fails the filter before it runs, with a `WasmFilterError` naming
  it up to an `=` (what follows may be a value):
  `LIBPANDOC_WASM_ENV: not a variable's name: "A B"`, `...
  "TOKEN=..."`; so does a value that isn't UTF-8 (`LIBPANDOC_WASM_ENV:
  NAME: its value is not UTF-8`). No message ever shows a value. The
  variables pandoc gives filters are the host's: a grant of one of those
  names is not passed. A program gives one filter its own instead,
  whatever the variable says: `WasmFilter(path, env=["NAME", ...])`, names
  whose values are this process's when the filter runs, or
  `env={"NAME": "value"}`, names with values (`env=[]`: none). A bad name
  there is a `ValueError` when the filter is made (`PATH: not a variable's
  name: "A B"`), as is a value with a NUL.

  A filter can't tell a variable not set from one not given, and the host
  can't see what a filter looks up: nothing says a filter missed one. So a
  filter does better to read its configuration from the document's
  metadata, which is in the document for all to see and works in every
  host, with the environment as an override at most. All this is about
  wasm filters only: in the directory sandbox pandoc and its Lua filters
  get the environment the sandbox gives them, whatever
  `LIBPANDOC_WASM_ENV` says, and Python filters and JSON filters
  (programs) see this process's whole environment;
- **calls to pandoc** (libpandoc-rs's `libpandoc` crate, built for wasm):
  in pandoc's sandbox, with no options that read or write files, fetch or
  run anything, and nothing read from the user data directory (libpandoc
  checks them: `"untrusted"`). So they read no file, not even one the
  filter can read itself: for citeproc, the filter reads the
  bibliography, style and abbreviations itself and gives them as their
  contents (below); the include step, named in a call, reads nothing. A filter can't name a cancel token (`"untrusted"` is
  written over what it wrote); its calls stop with the conversion they
  run in;
- **time and memory**, which pandoc limits for no filter:
  `$LIBPANDOC_WASM_TIMEOUT` (seconds, as pandoc-server's `--timeout`) and
  `$LIBPANDOC_WASM_MAX_MEMORY` (bytes, or with `k`, `m`, `g`, as pandoc's
  `+RTS -M`) stop a filter past them, with a `WasmFilterError`; none by
  default, as pandoc. `WasmFilter(path, timeout=, max_memory=)` for one
  filter (0: none). The time limit covers the filter's calls to pandoc:
  each runs with the time the filter has left as its `timeout`, one made
  after that fails at once, and the filter is then stopped. Its memory
  limit is the filter's own memory, not pandoc's, which its calls use, and
  also caps what it writes to its standard output and to its standard
  error (passed on to `sys.stderr`), each.

**What these bounds don't cover: the document the filter returns.** pandoc
writes it with your access, as it writes any document: a writer that
embeds files (docx, ODT, EPUB, PPTX, HTML with `--embed-resources`) reads
the images the document names, citeproc run after the filter reads the
`bibliography` in its metadata, and a PDF engine reads what raw LaTeX
names. A filter given no directory at all can name a file it couldn't
open and find it in the output (tested in libpandoc-rs). So for a filter
you don't trust, confine the conversion too, as for a document you don't
trust: the directory sandbox below, where pandoc reads only what you
listed (pandoc's own, `sandbox=True` or `--sandbox`, covers its writers
and citeproc, not a PDF engine).

**citeproc in a wasm filter** (multibib and the like): its files are read
by the filter, with the access its host gave it, and given to pandoc as
their contents, `"citeproc-files"` (libpandoc 1.14): options `{"from":
"json", "to": "json", "citeproc": true, "citeproc-files": {"NAME":
"TEXT", ...}}`, each file under the name pandoc looks up: a bibliography
as the metadata names it (`refs.bib`, `../common/refs.bib`, an absolute
path), `csl: style` as `style.csl` and `citation-abbreviations: abbr` as
`abbr.json` (a name with a dot as it is), TEXT the file as UTF-8 text.
libpandoc-rs's `libpandoc::citeproc` does it for filters in Rust (its
example filter `citeproc`); a file the filter can't read (outside its
directories) is left out, and pandoc reports it missing.
libpandoc's header has the rules (a dependent style's parent, LaTeX's
`\bibliography`); URLs are never fetched. From Python the option is
`citeproc_files=` (a dict), for a sandboxed conversion (`sandbox=True` or
`untrusted=True`; without, it is refused, as citeproc would read the disk
instead): a Python filter's own citeproc
(`convert(..., citeproc=True)`) reads the files itself, with this
process's access.

A filter that reads files reads what the document asks for, within its
directories (an include filter; citeproc on the metadata's
`bibliography`): when the document isn't trusted either, give the filter
no more than the document's own list (`LIBPANDOC_SANDBOX`).

## The include step: panclude

[panclude](https://github.com/ickc/panclude)'s `include` attribute is
built into libpandoc (1.15): a code block, raw block or div naming a file
is filled with it inside the conversion, so that every filter after the
step sees the content as if the document held it. It is off unless named
among the filters: `filters=["panclude", ...]`, or `pandocpy -F
panclude`; first, before the filters that are to see the content.

````markdown
```{.parse include="part.md"}
placeholder
```
````

```sh
pandocpy -F panclude -F parse.wasm -F upper.wasm doc.md
```

libpandoc-rs's example filters `parse.wasm` (the block's text read as
markdown) and `upper.wasm` (upper-cased) are given no directory, yet get
`part.md`'s text: the step put it into the document. Without `-F
panclude` they get `placeholder`.

**Roots.** The step reads only inside its roots: by default the main
document's directory (its first input file's; the working directory when
the source is given to `convert`), or those an entry names instead,
`{"type": "panclude", "roots": ["chapters", Path("/srv/shared")]}`
(relative ones from the working directory; one that isn't a directory, or
another key, is a `PandocError`). A path outside them (`../x`, an
absolute path elsewhere, a symbolic link leading out) is not read: the
element stays as it was, with a `PandocWarning`, `[ScriptingWarning]
Scripting warning: panclude: cannot include "../x" in doc.md: outside the
roots` (`fail_if_warnings=True` makes it an error).

**Sandboxes.** With `sandbox=True` (`--sandbox`) only roots named count:
`"panclude"` alone reads nothing. With `untrusted=True` the options may
name the step, `filters=["panclude"]`, not its roots, and it reads
nothing; the host gives roots in `untrusted`'s value,
`untrusted={"panclude-roots": [...]}`, which the code's options can't
name (with `cancel=`, and on the main thread, the call's token goes in
beside them). A wasm filter's calls to pandoc are given none: a filter
naming the step in a call reads nothing. In the directory sandbox the step
reads only inside the mounted directories, its roots named as the module
sees them, untouched like the options' other paths (the same paths on
Unix; on Windows relative ones with `/`, or `/c/...` for `C:\...`); a root
not mounted is no directory there, a `PandocError`.

**The name** is reserved: `"panclude"` is never a Python script or a
program in the working directory (`./panclude` is), nor an installed
filter of that name in `pandocpy`.

The syntax (line ranges, `raw=`, a div filled with a document, nested
includes) and the warnings: [panclude](https://github.com/ickc/panclude).

## Security: documents you don't trust

pandoc reads what a document asks for: LaTeX's `\input`, RST, Org,
AsciiDoc, man and Typst includes, images (embedded with
`--embed-resources`, in a docx or an EPUB), a `bibliography:` or `csl:`
in its metadata. Run on a document from elsewhere, it can put any file
you can read into its output. pandoc's own `--sandbox` (`sandbox=True`)
is all or nothing: the document reads no file at all, not even its own
images.

A **directory sandbox** is a container at the level of mounts: pandoc runs
whole inside libpandoc.wasm under wasmtime (`pip install libpandoc[wasm]`)
and sees only the directories you list, as a container sees only its
mounts. It is libpandoc-rs's, with the same settings and rules.

```sh
LIBPANDOC_SANDBOX=. LIBPANDOC_SANDBOX_WRITE=out pandocpy paper.tex -s -o out/paper.html
```

```python
html = pandoc.convert(None, input_files=["paper.tex"], to="html",
                      sandbox={"read": ["."], "write": ["out"]})
```

**What is mounted.** The read list (`"read"`, `LIBPANDOC_SANDBOX`) and the
write list (`"write"`, `LIBPANDOC_SANDBOX_WRITE`, readable too), each
directory at its own absolute path; the variables are lists as `PATH` is
(`:`, `;` on Windows), for `run` and `pandocpy`; the option is for
`convert`, `read` and `write`. A directory that doesn't exist is an error;
a variable set but empty, or an empty list, mounts nothing. A private
temporary directory. No network, no programs. Of the environment, only
`SOURCE_DATE_EPOCH` (for a reproducible docx, odt or epub). Nothing else:
**not the user data directory, not `PANDOC_DATA_DIRS`**: list them if
pandoc is to find your templates and filters there. Without wasmtime the
setting is refused, never ignored.

**Everything pandoc reads must be in a mounted directory**: what the
document names, and what you name too (input files, template, CSS,
bibliography, defaults files, Lua filters). Outside, a file is missing.
Output goes to standard output (the returned value), or into a directory
of the write list (`output_file`, `extract_media`, `log_file`; `-o`,
`--extract-media`, `--log`). The options and arguments reach pandoc
untouched. Listing a directory opens all of it to the document: list the
document's own directory, not your home.

**Filters.** Lua filters run inside, confined as the document is (in
plain pandoc a Lua filter can do anything you can). Filters that are
programs (JSON filters) fail (status 83 in `pandocpy`): wasm starts no
process, and a filter run outside would read whatever the document names.
Python filters (functions, `panir.Filter`s) and Python filter scripts
(`-F foo.py`) are your code, as trusted as the command line: they run in
this process as they do without the sandbox, called back by the module,
unconfined. Wasm filters run in this process too, seeing their own
directories (none, unless `LIBPANDOC_WASM_DIRS` or the program gives
some), not the mounts: the document's list and the filters' are different
lists.
`pandocpy lua` and `pandoc server` are not in the module (status 4). A
filter that reads fragments with `read(text, ctx.conversion)` or
`read_many` reads them in pandoc's own sandbox: no files, not even in the
directories listed ("Reading fragments, from filters"): every filter
running in this process is told the conversion is sandboxed (the
conversion's `sandbox`, and `PANDOC_SANDBOX=true` for scripts and wasm
filters), where the module tells of pandoc's own sandbox only.

**Limits.** `LIBPANDOC_WASM_TIMEOUT` (seconds) and
`LIBPANDOC_WASM_MAX_MEMORY` (bytes, or with `k`, `m`, `g`), or for one
call the sandbox's `"timeout"` and `"max-memory"`, stop a conversion past
them, counted from the module's start (as `cancel=`'s token and Ctrl-C
stop it: from outside, wasmtime's epoch, which also advances other wasm
runs' clocks by one tick, 10 ms); none by default, wasm's 4 GiB at
most. A negative, infinite or NaN one is a `ValueError`. A conversion
stopped by the time limit raises `PandocTimeoutError`, as one past
libpandoc's `timeout=` (which works inside too, but doesn't stop Lua);
one out of memory a `PandocError` saying so. A document can be built to
take time or memory (Typst's `#while true {}`: 3 GB in 4.5 s natively;
100,000 nested `>`: over 15 s): set **both** for documents from the
internet. The memory limit alone does not stop every such document: a
loop that grows slowly, or not at all, runs on until the time limit. The
time limit covers pandoc and Lua inside the module, not filters run in
this process (a Python filter runs to its end; a wasm filter has its own
limits).

**What to know.**

- `..` from one mounted directory into another fails (each mount resolves
  alone), in an absolute path too; a path without `..` works.
- Relative names resolve from the working directory, which must be in a
  mounted directory; if it isn't, they find nothing and the error says so.
- On Windows a path with a drive letter means nothing inside: use relative
  names with `/`, or `/c/...` for `C:\...`.
- A path leaving a mount by `..` or a symbolic link fails as "permission
  denied", where a missing file "does not exist": a document can tell the
  two apart where pandoc shows the error (an input file, an AsciiDoc or
  Typst include), though it reads nothing. A template, CSS or bibliography
  reads as missing either way.
- In WASI an empty file name is the working directory: a Lua filter's
  `io.open("")` opens it, where natively it is a missing file, and reading
  it then fails (include-code-files' `include=""` fails the conversion
  inside, where natively it is an empty block); a wasm filter's likewise.
- PDF output needs a program and fails, saying so: write LaTeX, HTML or
  Typst and make the PDF outside.
- `pandocpy --version` says what runs: the module's pandoc
  (`Features: -server -lua`).
- `read_many` reads in this process: a directory sandbox there is an error.

**What it doesn't protect from:** a hard link inside a mounted directory
to a file elsewhere (it is that file), and anything else you put in the
directories; documents built to use time or memory beyond the limits you
set; your own filters; bugs in wasmtime or in this host.

**The module.** `LIBPANDOC_WASM=/path/to/libpandoc.wasm`, else libpandoc's
`continuous` release's, downloaded once (about 60 MB) into the user's
cache (`~/.cache/libpandoc/continuous`; `~/Library/Caches` on macOS,
`%LOCALAPPDATA%` on Windows) and checked against the release's
`SHA256SUMS`. A cached module older than this package needs fails, naming
the file to remove. Its **first compilation takes about 20 s and 8 GB of
memory**, RAM and swap together (no setting of wasmtime's lowers that;
with less, the process is killed); wasmtime's cache (`~/.cache/wasmtime`)
keeps the result (64 MB), and later processes load it in 0.25 s.

**What it costs** (pandoc's MANUAL, markdown to HTML, one run of
`pandocpy`): 2.1 s and 1.07 GB at most against 0.64 s and 0.36 GB
natively; in a process that has loaded the module, 1.85 s a conversion
against 0.25 s, and a small one takes 6 ms. The first run: 19.5 s and
7.9 GB.

**For real isolation**, run the conversion where only the directory
exists, a container at least, a virtual machine better:

```sh
podman run --rm --network=none --read-only --tmpfs /tmp --cap-drop=all \
  --security-opt=no-new-privileges --memory=2g --cpus=1 \
  -v "$PWD/doc:/work:ro,Z" -w /work IMAGE \
  timeout 60 pandocpy main.tex -t html > out.html
```

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
`read_options(conversion)` gives the options used. Read so, a text keeps
its tabs, as Lua's `pandoc.read` reads it; the `pandoc` command, and so
panflute's `convert_text` and pypandoc, first replace them by spaces
(`preserve_tabs=False` does). A call each costs about 0.3 ms, six times
`pandoc.read`'s: batch them. amsthm's 738 notes take 0.25 s read one by
one and 0.015 s in one `read_many`.

A text `read_many` can't read fails as `reading input N: ...`, N counting
from 0: the text's place in the list given, not in the document. With
`return_exceptions=True` each error is in its text's place in the result,
so that a filter can map a failure back to where the text came from (a
table's cell) and say so.

**Confined when the conversion is.** Document text read this way may name
files (LaTeX's `\input`, RST's and Org's includes). When the conversion
the filter runs in is sandboxed, by pandoc's sandbox (`sandbox=True`,
`--sandbox`, a defaults file, `untrusted=True`) or a directory sandbox
(`sandbox={...}`, `LIBPANDOC_SANDBOX`), the fragments are read in pandoc's
own sandbox: they read no files, not even in the directories listed (the
filter runs in this process, outside them). libpandoc tells every filter
whether pandoc's sandbox is on (the conversion's `sandbox`, from a
callback's context; `PANDOC_SANDBOX` for scripts, which
`Conversion.from_environment()` reads), and the directory sandbox tells
its filters it is. The rule: a read is confined when the conversion's
`sandbox` is true, or its options ask for a sandbox, or when nothing can
tell (`sandbox` unknown and no options: a conversion made by hand, or a
script's under plain pandoc). A filter that wants the files read gives its
own options, `read(text, ctx.conversion, sandbox=False)`: its code, its
choice.

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
their standard input (a panir script hands its filters over instead:
"Consecutive filters"), saving a Python start and its imports per filter
(about 30 ms each). The same holds for `convert(filters=["foo.py"])`, from
any number of threads: while scripts run, `sys.stdin`, `sys.stdout`,
`sys.argv` and `os.environ` are each thread's script's own there, and the
real ones elsewhere. A script imports what `python foo.py` would let it,
modules and packages beside it included (its folder is first on
`sys.path` while it runs, and what it imported from there is forgotten
after, so another script's `util.py` is that script's own); what still
differs is in `libpandoc/_scripts.py`'s docstring. Scripts run with this
Python and its packages either way. A script runs as a subprocess instead:

- if its author says so, with a line `# pandocpy: subprocess` near its top;
- if the user says so: `PANDOCPY_SUBPROCESS=foo.py,bar` (paths, file names,
  names without `.py`, or `*` for all).

A script runs afresh for each document, in process too: its globals start
anew each time (a fresh module namespace), as a JSON filter's would in a
process of its own; only the modules it imports stay imported, with any
state they keep. (LibPandoc.jl loads a Julia script once per process, and
its globals live on: reset them for each document there.) A filter given
to `convert` as a function or a `panir.Filter` is the same object for
every conversion: what it keeps lives as long as this process.

A script that fails in process fails the conversion (`ScriptFailed`, its
error chained, saying how to opt out); it is not run again, which would do
twice what it did. A script that changes the working directory has it
changed back when it ends. As a subprocess, a script writes to this
process's standard error, and `timeout=` stops it.

`python -m libpandoc` is the same as `pandocpy`, which is pandoc's command
line exactly (pandoc's own command tests pass), file names that aren't
UTF-8 and `pandocpy lua` (pandoc as a Lua interpreter) included. Only
`pandoc server` is not supported. With `LIBPANDOC_SANDBOX` or
`LIBPANDOC_SANDBOX_WRITE` set, it is the pandoc command of libpandoc.wasm
in the directory sandbox (above), on this process's standard input, output
and error: pandoc's own messages, `--quiet`, `--verbose`, `--log` and
exit statuses.

## The AST

Documents are [panir](https://github.com/ickc/panir) objects: pandoc's
types as Python classes, generated from pandoc-types, with checked fields,
pandoc's JSON, and filters. It is a separate, pure-Python package (it needs no
libpandoc), so filters written with it also run under the `pandoc`
executable. At import, this package checks that panir and the loaded pandoc
speak the same API version.

## Installing

Wheels are `abi3`: one per platform serves every CPython ≥ 3.11 (and one
more for free-threaded 3.14t). Each bundles libpandoc (about 40 MB to
download, 200 MB installed; Pyodide's 16 MB): Linux
(glibc ≥ 2.28, and musl ≥ 1.2 such as Alpine; x86-64 and aarch64), macOS
(Intel and Arm), Windows x64, and Pyodide (below).

To build against an installed libpandoc (for example a conda environment,
or a staged `dist/` prefix):

```sh
LIBPANDOC_PREFIX=/path/to/prefix pip install .
```

It needs libpandoc 1.15 or a later 1.x (libpandoc's rule: the same major
version, a minor at least as large), checked when the package is imported,
for libpandoc.wasm in Pyodide too. The extension then finds the library in
that prefix when it runs (an RPATH). `LIBPANDOC_RPATH` sets another, e.g. `$ORIGIN/...` relative to the
extension, or empty for none (a wheel whose repair tool bundles the
library). libpandoc-rs reads both variables the same way.

### In the browser (Pyodide)

On Pyodide, the package runs pandoc as libpandoc.wasm, which the browser's
engine runs (libpandoc's `wasm/`), instead of the native library: the same
API, Python filters included. Its wheel for Pyodide
(`py3-none-pyemscripten_2026_0_wasm32`, built by `scripts/pyodide-wheel.sh`
from a libpandoc release's `libpandoc.wasm` and `libpandoc-pyodide.mjs`)
carries the module; load it once before the first call:

```python
import micropip
await micropip.install("libpandoc")
import libpandoc
await libpandoc.load()            # from the wheel; or load(url), load(bytes)
libpandoc.convert("*hi*", to="html")
```

Pyodide's filesystem is pandoc's, and so is Python's working directory
(set before each call): relative paths mean the same to both. A host may instead
load libpandoc.wasm itself and register it before `import libpandoc`:

```js
import { load } from "libpandoc/wasm/browser.mjs";
import { emscriptenDirectory } from "libpandoc/wasm/emscripten-fs.mjs";
const pandoc = await load(wasmUrl, { tmp: emscriptenDirectory(pyodide.FS, "/tmp") });
pyodide.registerJsModule("libpandoc_wasm", pandoc.abi);
await pyodide.runPythonAsync("import libpandoc; print(libpandoc.convert('*hi*', to='html'))");
```

The API and AST tests pass there but the 3 that need threads, which
wasm doesn't have. A document too large for wasm32's 4 GiB (about 80 MB of
markdown) stops the libpandoc.wasm instance for good: calls then raise a
`JsException` named `StoppedError` (libpandoc's `wasm/README.md`, "When an
instance stops") until `await libpandoc.load()` loads it again. It needs a
browser with wasm's exnref exception handling (Chromium 138, Firefox 132,
Safari 18.2 or later). Pyodide has no pandoc command (`pandocpy`), no
threads, and no JSON filters (no programs).

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
checked Python objects, costs about 15% over a Lua filter. Every filter
but a Lua one is handed the whole document as pandoc's JSON and hands it
back, whatever it changes: on a 9.4 MB textbook a filter that does next to
nothing adds 0.6 s in Python (0.35 s in Rust, 0.37 s as a JSON filter), where a
Lua filter, given elements as it asks for them, adds 0.09 s; pandoc's own
encoding and decoding of the JSON is most of it. Consecutive filters of
this process pay it once ("Consecutive filters").

## License

The package's own code is GPL-2.0-or-later (`COPYING.md`), as pandoc's:
it links pandoc. What it loads is libpandoc, under the GPL, version 2, as
a whole (pandoc and about 200 Haskell packages, GHC's runtime and a few C
libraries, each under its own licence). A wheel bundles it, and carries
among its licence files (`*.dist-info/licenses/libpandoc-licenses/`)
libpandoc's `COPYING.md` and `THIRD-PARTY-NOTICES` from the release it was
built with, which also cover what the wheel tools graft beside it (GMP and
libffi; on musl libstdc++ and libgcc); the Pyodide wheel, libpandoc.wasm's
`libpandoc-wasm-THIRD-PARTY-NOTICES.txt`. The directory sandbox downloads
libpandoc.wasm, whose notices are beside it in the release.
