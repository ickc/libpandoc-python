"""Where does the time go in a Python filter round trip?

    python bench/roundtrip.py [REPEAT]
"""

import json
import sys
import time
import warnings
from pathlib import Path

import panir

import libpandoc as pandoc

warnings.simplefilter("ignore", pandoc.PandocWarning)  # the repeated input duplicates notes

repeat = int(sys.argv[1]) if len(sys.argv) > 1 else 50
src = (Path(__file__).parent.parent / "tests/data/testsuite.txt").read_text() * repeat


def timed(label, f, n=3):
    best = min(_once(f) for _ in range(n))
    print(f"{label:<44} {best * 1000:8.1f} ms")
    return f()


def _once(f):
    t = time.perf_counter()
    f()
    return time.perf_counter() - t


print(f"input: {len(src) / 1e6:.2f} MB of markdown")
timed("markdown -> html (in pandoc, no Python AST)", lambda: pandoc.convert(src, to="html"))
timed("markdown -> markdown", lambda: pandoc.convert(src, to="markdown"))
js = timed("markdown -> json (read + aeson encode)", lambda: pandoc.convert(src, to="json"))
print(f"{'':<44} ({len(js) / 1e6:.1f} MB of JSON)")
j = timed("json.loads", lambda: json.loads(js))
doc = timed("Pandoc.from_json (build Python objects)", lambda: panir.Pandoc.from_json(j))
j2 = timed("to_json", lambda: doc.to_json())
js2 = timed("json.dumps", lambda: json.dumps(j2))
timed("json -> html (aeson decode + write)", lambda: pandoc.convert(js2, from_="json", to="html"))
timed("json -> json (aeson decode + encode)", lambda: pandoc.convert(js2, from_="json", to="json"))
timed("walk, no-op action on every node", lambda: panir.walk(doc, lambda n, ctx: None))

# The same filter, uppercasing every Str, as Lua (no JSON) and as Python.
lua = Path(__file__).parent / "upper.lua"
lua.write_text("function Str(s) return pandoc.Str(s.text:upper()) end\n")
timed("filter: Lua, markdown -> html", lambda: pandoc.convert(src, to="html", filters=[str(lua)]))


f = panir.Filter()


@f.on(panir.Str)
def upper(s):
    s.text = s.text.upper()


timed("filter: Python, markdown -> html", lambda: pandoc.convert(src, to="html", filters=[f]))
