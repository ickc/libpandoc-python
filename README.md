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

from libpandoc import ast

doc = pandoc.read("Hello *world*")    # Pandoc(meta={}, blocks=[Para(content=[Str(text='Hello'), ...
def shout(node):
    if isinstance(node, ast.Str):
        return ast.Str(node.text.upper())
pandoc.write(pandoc.walk(doc, shout), "plain")           # 'HELLO WORLD\n'
```

- **Options** are pandoc's [defaults-file](https://pandoc.org/MANUAL.html#defaults-files)
  keys, as keyword arguments (`_` for `-`, `from_` for `from`) or a dict.
  `run` takes command-line arguments, parsed by pandoc itself.
- **Errors** raise `PandocError`, with `kind` set to pandoc's error
  constructor (`"PandocParseError"`, ...). **Warnings** are issued as
  `PandocWarning`.
- **Threads:** the GIL is released while pandoc runs, so conversions in
  different threads run in parallel.
- **Lua filters** work as usual (`filters=["f.lua"]`). **Python filters**
  work on the typed AST: `read`, change it, `write`.

## The typed AST

`libpandoc.ast` has one dataclass per pandoc-types constructor, with the
fields in Haskell order: `Header(level, attr, content)`,
`Link(attr, content, target)`, `Attr(identifier, classes, attributes)`.
Types whose constructors all have no fields are enums (`Alignment.AlignLeft`).
Every node has `to_json()` and `from_json()` for pandoc's JSON.

`src/libpandoc/ast.py` is generated. libpandoc reifies pandoc-types'
declarations at compile time and reports them as a schema;
`tools/gen_ast.py` turns the schema into classes and JSON codecs. After a
pandoc upgrade:

```sh
pixi run gen      # from schema/ast-schema.json, as reported by the new libpandoc
```

CI regenerates from the libpandoc it tests against and fails on any
difference. Field names come from field types (`Attr` → `attr`, `[Inline]`
→ `content`). A few that can't be derived are listed in `FIELD_NAMES` in
`tools/gen_ast.py`. If a pandoc-types change needs a new entry, generation
fails and says so; it never guesses.

At import, the package checks that the loaded pandoc speaks the API
version `ast.py` was generated for.

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
| markdown → html | 1099 |
| same, with a Lua filter uppercasing every `Str` | 1337 |
| same filter in Python: `read`, `walk`, `write` | 1493 |
| of which Python's share (json.loads, from_json, to_json, json.dumps) | 91 |

pandoc's reader and writer dominate. Exchanging the AST as JSON costs about
12% over a Lua filter.

## License

GPL-2.0-or-later, as pandoc: the package links pandoc.
