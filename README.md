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

from pandom import Filter, Header

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
  different threads run in parallel, on all cores: 2000 small conversions
  run 6.6 times as fast on 8 threads as on one, 8.5 times on 32.
- **Filters**: `filters=` takes pandoc's (Lua or JSON filter paths,
  `"citeproc"`) and Python ones, mixed, in order. A Python filter is a
  [pandom](https://github.com/ickc/pandom) `Filter`, or a function that
  takes a document (and optionally the `pandom.Conversion`) and changes it.
  Python filters run in this process, inside the one pandoc conversion, as
  pandoc runs a Lua filter: what the reader keeps in memory (images embedded
  in a docx) reaches the writer, and a filter's exception is raised from
  `convert` as it is. A filter may call pandoc itself (`convert`, or
  `ctx.read(text)` to parse a fragment the way the document was read).

## Reading fragments, from filters

`read_many` parses many texts at once, each on its own, on all cores: for
filters that parse fragments such as table cells (2000 cells: 107 ms, against
725 ms for a `read` each, and 184 ms for joining them into one document,
which also lets the cells affect each other). Given the conversion a filter
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
[project.entry-points."pandom.filters"]
pantable = "pantable:filter"    # a pandom.Filter, or a function
```

```sh
pandocpy -F pantable input.md -o output.html   # pantable in process
pandocpy -F other-filter input.md              # anything else: as pandoc does
```

`python -m libpandoc` is the same. Informational options (`--version`,
`--list-*`, `-D`) are answered from libpandoc's queries.

## The AST

Documents are [pandom](https://github.com/ickc/pandom) objects: pandoc's
types as Python classes, generated from pandoc-types, with checked fields,
pandoc's JSON, and filters. It is a separate, pure-Python package (it needs no
libpandoc), so filters written with it also run under the `pandoc`
executable. At import, this package checks that pandom and the loaded pandoc
speak the same API version.

## Installing

Wheels are `abi3`: one per platform serves every CPython ≥ 3.10. Each
bundles libpandoc (about 60 MB).

To build against an installed libpandoc (for example a conda environment,
or a staged `dist/` prefix):

```sh
LIBPANDOC_PREFIX=/path/to/prefix pip install .
```

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
