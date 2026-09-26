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

from libpandoc.ast import Filter, Header

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
  different threads run in parallel.
- **Filters**: `filters=` takes pandoc's (Lua or JSON filter paths) and
  Python ones, mixed, in order. A Python filter is a
  [libpandoc-ast](https://github.com/ickc/libpandoc-ast) `Filter`, or a
  function that takes a document and changes it; the same `Filter` also
  runs under `pandoc --filter`. Python filters run between pandoc passes
  (read to JSON, filter, write from JSON), so resources pandoc keeps in
  memory, such as images embedded in a docx, need `extract_media` to
  survive them.

## The AST

`libpandoc.ast` is [libpandoc-ast](https://github.com/ickc/libpandoc-ast):
pandoc's types as Python classes, generated from pandoc-types, with checked
fields, pandoc's JSON, and filters. It is a separate, pure-Python package (it
needs no libpandoc), so filters written with it also run under the `pandoc`
executable. At import, this package checks that libpandoc-ast and the loaded
pandoc speak the same API version.

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
