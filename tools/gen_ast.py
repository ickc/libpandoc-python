#!/usr/bin/env python3
"""Generate src/libpandoc/ast.py from libpandoc's AST schema.

The schema (schema/ast-schema.json) is what libpandoc reports for
``{"query": "ast-schema"}``: pandoc-types' declarations, reified at compile
time. This script turns it into Python dataclasses plus JSON codecs that
match pandoc-types' aeson encoding:

- a type with several constructors is ``{"t": Con}`` / ``{"t": Con, "c": x}``
  / ``{"t": Con, "c": [x, y]}``; if every constructor is nullary it becomes
  an Enum;
- a single-constructor type is its field, or an array of its fields, or for
  a record an object keyed by field name;
- a newtype or type alias is the underlying type;
- ``Pandoc`` itself is ``{"pandoc-api-version", "meta", "blocks"}``.

Field names are derived from field types (``Attr`` -> ``attr``, ``[Inline]``
-> ``content``, ``[Citation]`` -> ``citations``). ``FIELD_NAMES`` names the
fields that can't be derived; generation fails, rather than guesses, when a
new pandoc-types needs another entry there.

    python tools/gen_ast.py schema/ast-schema.json src/libpandoc/ast.py
"""

from __future__ import annotations

import json
import keyword
import re
import sys
from pathlib import Path

# Constructor -> positional field names, where types don't determine them.
FIELD_NAMES: dict[str, list[str]] = {
    "Pandoc": ["meta", "blocks"],
    "Header": ["level", "attr", "content"],
    "TableBody": ["attr", "row_head_columns", "head", "body"],
    "ColWidth": ["width"],
    "MetaBool": ["value"],
}

# Tuple aliases that get a dataclass with these field names (still encoded
# as JSON arrays). Other aliases stay plain type aliases.
ALIAS_FIELDS: dict[str, list[str]] = {
    "Attr": ["identifier", "classes", "attributes"],
    "Target": ["url", "title"],
    "ListAttributes": ["start", "style", "delimiter"],
    "ColSpec": ["alignment", "width"],
}

# Defaults for the dataclass aliases, so that e.g. ``Attr()`` is nullAttr.
ALIAS_DEFAULTS: dict[str, list[str]] = {
    "Attr": ['""', "field(default_factory=list)", "field(default_factory=list)"],
    "Target": ['""', '""'],
}

CONTENT_TYPES = {"Inline", "Block", "MetaValue"}

PRIM_PY = {"string": "str", "int": "int", "double": "float", "bool": "bool"}


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def plural(name: str) -> str:
    return name[:-1] + "ies" if name.endswith("y") else name + "s"


class Generator:
    def __init__(self, schema: dict) -> None:
        self.schema = schema
        self.types = {t["name"]: t for t in schema["types"]}
        self.api_version = schema["pandoc-api-version"]
        # sum types whose name is also one of their constructors' names
        self.base_name = {}
        for t in schema["types"]:
            name = t["name"]
            self.base_name[name] = name
            if self.is_sum(t) and any(c["name"] == name for c in t["constructors"]):
                self.base_name[name] = name + "Base"

    # -- classification ------------------------------------------------

    @staticmethod
    def is_sum(t: dict) -> bool:
        return t["kind"] == "data" and len(t["constructors"]) > 1

    def is_enum(self, t: dict) -> bool:
        return self.is_sum(t) and all(not c["fields"] for c in t["constructors"])

    # -- names ---------------------------------------------------------

    def field_names(self, con: dict) -> list[str]:
        fields = con["fields"]
        if fields and all(f["name"] for f in fields):
            names = [self.record_field(con["name"], f["name"]) for f in fields]
        elif con["name"] in FIELD_NAMES:
            names = FIELD_NAMES[con["name"]]
            if len(names) != len(fields):
                sys.exit(f"FIELD_NAMES[{con['name']!r}] has {len(names)} names "
                         f"but the constructor has {len(fields)} fields")
        else:
            names = [self.derive_name(con["name"], f["type"]) for f in fields]
        if len(set(names)) != len(names):
            sys.exit(f"constructor {con['name']} has ambiguous field names {names}; "
                     "add it to FIELD_NAMES in tools/gen_ast.py")
        return [n + "_" if keyword.iskeyword(n) else n for n in names]

    @staticmethod
    def record_field(con: str, name: str) -> str:
        prefix = con[0].lower() + con[1:]
        if name.startswith(prefix) and len(name) > len(prefix):
            name = name[len(prefix):]
        return snake(name)

    def derive_name(self, con: str, ty: dict) -> str:
        if "maybe" in ty:
            return self.derive_name(con, ty["maybe"])
        if "ref" in ty:
            return snake(ty["ref"])
        if "map" in ty:
            return "content"
        if "list" in ty:
            inner = ty["list"]
            while "list" in inner:
                inner = inner["list"]
            if "tuple" in inner or inner.get("ref") in CONTENT_TYPES:
                return "content"
            if "ref" in inner:
                return plural(snake(inner["ref"]))
        if ty.get("prim") == "string":
            return "text"
        sys.exit(f"can't name a field of type {ty} in constructor {con}; "
                 "add it to FIELD_NAMES in tools/gen_ast.py")

    # -- type expressions ----------------------------------------------

    def py_type(self, ty: dict) -> str:
        if "prim" in ty:
            return PRIM_PY[ty["prim"]]
        if "ref" in ty:
            return self.base_name[ty["ref"]]
        if "list" in ty:
            return f"list[{self.py_type(ty['list'])}]"
        if "maybe" in ty:
            return f"{self.py_type(ty['maybe'])} | None"
        if "map" in ty:
            k, v = ty["map"]
            return f"dict[{self.py_type(k)}, {self.py_type(v)}]"
        if "tuple" in ty:
            return f"tuple[{', '.join(self.py_type(t) for t in ty['tuple'])}]"
        raise ValueError(ty)

    def dec(self, ty: dict, x: str, depth: int = 0) -> str:
        """Python expression decoding JSON value x of type ty."""
        v = f"x{depth}"
        if "prim" in ty:
            return f"float({x})" if ty["prim"] == "double" else x
        if "ref" in ty:
            return f"_dec_{ty['ref']}({x})"
        if "list" in ty:
            return f"[{self.dec(ty['list'], v, depth + 1)} for {v} in {x}]"
        if "maybe" in ty:
            return f"(None if {x} is None else {self.dec(ty['maybe'], x, depth)})"
        if "map" in ty:
            k, val = ty["map"]
            return f"{{k{depth}: {self.dec(val, v, depth + 1)} for k{depth}, {v} in {x}.items()}}"
        if "tuple" in ty:
            parts = [self.dec(t, f"{x}[{i}]", depth) for i, t in enumerate(ty["tuple"])]
            return f"({', '.join(parts)},)"
        raise ValueError(ty)

    def enc(self, ty: dict, x: str, depth: int = 0) -> str:
        """Python expression encoding value x of type ty as JSON."""
        v = f"x{depth}"
        if "prim" in ty:
            return x
        if "ref" in ty:
            return f"_enc_{ty['ref']}({x})"
        if "list" in ty:
            inner = self.enc(ty["list"], v, depth + 1)
            return x if inner == v else f"[{inner} for {v} in {x}]"
        if "maybe" in ty:
            inner = self.enc(ty["maybe"], x, depth)
            return x if inner == x else f"(None if {x} is None else {inner})"
        if "map" in ty:
            k, val = ty["map"]
            return f"{{k{depth}: {self.enc(val, v, depth + 1)} for k{depth}, {v} in {x}.items()}}"
        if "tuple" in ty:
            parts = [self.enc(t, f"{x}[{i}]", depth) for i, t in enumerate(ty["tuple"])]
            return f"[{', '.join(parts)}]"
        raise ValueError(ty)

    # -- declarations --------------------------------------------------

    def generate(self) -> str:
        out: list[str] = [HEADER.format(api=".".join(map(str, self.api_version)))]
        out.append(f"PANDOC_API_VERSION = {tuple(self.api_version)!r}\n")
        aliases: list[str] = []
        codecs: list[str] = []
        self.class_types: list[tuple[str, str]] = []
        for t in self.schema["types"]:
            name, kind = t["name"], t["kind"]
            if kind == "alias" and name in ALIAS_FIELDS:
                out.append(self.alias_class(t))
                self.class_types.append((name, name))
            elif kind in ("alias", "newtype"):
                ty = t["type"] if kind == "alias" else t["constructors"][0]["fields"][0]["type"]
                aliases.append(f"{name} = {self.runtime_type(ty)}")
            elif self.is_enum(t):
                out.append(self.enum_class(t))
                self.class_types.append((name, name))
            elif self.is_sum(t):
                self.class_types.append((self.base_name[name], name))
                out.append(f"class {self.base_name[name]}(Node):\n"
                           f'    """Any constructor of pandoc\'s ``{name}``."""\n'
                           f"    __slots__ = ()\n\n")
                for c in t["constructors"]:
                    out.append(self.con_class(c, self.base_name[name]))
                    self.class_types.append((c["name"], name))
            else:
                out.append(self.con_class(t["constructors"][0], "Node"))
                self.class_types.append((t["constructors"][0]["name"], name))
            codecs.append(self.codec(t))
        out.append("\n# type aliases and newtypes\n")
        out.extend(a + "\n" for a in aliases)
        out.append("\n\n# JSON codecs, following pandoc-types' aeson instances\n\n")
        out.extend(codecs)
        out.append("# pandoc type of each class (constructor classes map to their type)\n")
        out.append("_TYPE_OF: dict[type, str] = {\n")
        out.extend(f"    {cls}: {ty!r},\n" for cls, ty in self.class_types)
        out.append("}\n\n")
        out.append(FOOTER)
        return "".join(out)

    def runtime_type(self, ty: dict) -> str:
        """A type expression evaluable at import time (forward refs quoted)."""
        if "ref" in ty:
            return repr(self.base_name[ty["ref"]])
        if "list" in ty:
            return f"list[{self.runtime_type(ty['list'])}]"
        if "map" in ty:
            k, v = ty["map"]
            return f"dict[{self.runtime_type(k)}, {self.runtime_type(v)}]"
        if "tuple" in ty:
            return f"tuple[{', '.join(self.runtime_type(t) for t in ty['tuple'])}]"
        if "maybe" in ty:
            return f"typing.Optional[{self.runtime_type(ty['maybe'])}]"
        return self.py_type(ty)

    def alias_class(self, t: dict) -> str:
        name, names = t["name"], ALIAS_FIELDS[t["name"]]
        types = t["type"]["tuple"]
        defaults = ALIAS_DEFAULTS.get(name)
        lines = [f"@dataclass(slots=True)\nclass {name}(Node):",
                 f'    """pandoc\'s ``{name}`` (a tuple, encoded as a JSON array)."""']
        for i, (n, ty) in enumerate(zip(names, types)):
            default = f" = {defaults[i]}" if defaults else ""
            lines.append(f"    {n}: {self.py_type(ty)}{default}")
        return "\n".join(lines) + "\n\n\n"

    def enum_class(self, t: dict) -> str:
        lines = [f"class {t['name']}(str, enum.Enum):",
                 f'    """pandoc\'s ``{t["name"]}``."""']
        lines += [f"    {c['name']} = {c['name']!r}" for c in t["constructors"]]
        return "\n".join(lines) + "\n\n\n"

    def con_class(self, con: dict, base: str) -> str:
        names = self.field_names(con)
        lines = [f"@dataclass(slots=True)\nclass {con['name']}({base}):"]
        for n, f in zip(names, con["fields"]):
            lines.append(f"    {n}: {self.py_type(f['type'])}")
        if not con["fields"]:
            lines.append("    pass")
        return "\n".join(lines) + "\n\n\n"

    def codec(self, t: dict) -> str:
        name, kind = t["name"], t["kind"]
        if name == self.schema["root"]:
            con = t["constructors"][0]
            meta_t, blocks_t = (f["type"] for f in con["fields"])
            return (
                f"def _dec_{name}(j):\n"
                f"    _check_api_version(j.get('pandoc-api-version'))\n"
                f"    return {name}({self.dec(meta_t, 'j[\"meta\"]')}, "
                f"{self.dec(blocks_t, 'j[\"blocks\"]')})\n\n\n"
                f"def _enc_{name}(v):\n"
                f"    return {{'pandoc-api-version': list(PANDOC_API_VERSION), "
                f"'meta': {self.enc(meta_t, 'v.meta')}, "
                f"'blocks': {self.enc(blocks_t, 'v.blocks')}}}\n\n\n")
        if kind == "alias" and name in ALIAS_FIELDS:
            types = t["type"]["tuple"]
            names = ALIAS_FIELDS[name]
            decs = ", ".join(self.dec(ty, f"j[{i}]") for i, ty in enumerate(types))
            encs = ", ".join(self.enc(ty, f"v.{n}") for n, ty in zip(names, types))
            return (f"def _dec_{name}(j):\n    return {name}({decs})\n\n\n"
                    f"def _enc_{name}(v):\n    return [{encs}]\n\n\n")
        if kind in ("alias", "newtype"):
            ty = t["type"] if kind == "alias" else t["constructors"][0]["fields"][0]["type"]
            return (f"def _dec_{name}(j):\n    return {self.dec(ty, 'j')}\n\n\n"
                    f"def _enc_{name}(v):\n    return {self.enc(ty, 'v')}\n\n\n")
        if self.is_enum(t):
            return (f"def _dec_{name}(j):\n    return {name}(j['t'])\n\n\n"
                    f"def _enc_{name}(v):\n    return {{'t': v.value}}\n\n\n")
        if self.is_sum(t):
            decs, encs = [], []
            for c in t["constructors"]:
                cn, fields = c["name"], c["fields"]
                if any(f["name"] for f in fields):
                    sys.exit(f"record constructor {cn} in sum type {name} is not supported")
                names = self.field_names(c)
                if not fields:
                    decs.append(f"    {cn!r}: lambda c: {cn}(),")
                    encs.append(f"    {cn}: lambda v: {{'t': {cn!r}}},")
                elif len(fields) == 1:
                    ty = fields[0]["type"]
                    decs.append(f"    {cn!r}: lambda c: {cn}({self.dec(ty, 'c')}),")
                    encs.append(f"    {cn}: lambda v: {{'t': {cn!r}, 'c': {self.enc(ty, 'v.' + names[0])}}},")
                else:
                    d = ", ".join(self.dec(f["type"], f"c[{i}]") for i, f in enumerate(fields))
                    e = ", ".join(self.enc(f["type"], f"v.{n}") for n, f in zip(names, fields))
                    decs.append(f"    {cn!r}: lambda c: {cn}({d}),")
                    encs.append(f"    {cn}: lambda v: {{'t': {cn!r}, 'c': [{e}]}},")
            return (f"_DEC_{name} = {{\n" + "\n".join(decs) + "\n}\n\n\n"
                    f"def _dec_{name}(j):\n    return _DEC_{name}[j['t']](j.get('c'))\n\n\n"
                    f"_ENC_{name} = {{\n" + "\n".join(encs) + "\n}\n\n\n"
                    f"def _enc_{name}(v):\n    return _ENC_{name}[type(v)](v)\n\n\n")
        # single constructor
        con = t["constructors"][0]
        cn, fields = con["name"], con["fields"]
        names = self.field_names(con)
        if fields and all(f["name"] for f in fields):
            d = ", ".join(self.dec(f["type"], f"j[{f['name']!r}]") for f in fields)
            e = ", ".join(f"{f['name']!r}: {self.enc(f['type'], 'v.' + n)}"
                          for n, f in zip(names, fields))
            return (f"def _dec_{name}(j):\n    return {cn}({d})\n\n\n"
                    f"def _enc_{name}(v):\n    return {{{e}}}\n\n\n")
        if len(fields) == 1:
            ty = fields[0]["type"]
            return (f"def _dec_{name}(j):\n    return {cn}({self.dec(ty, 'j')})\n\n\n"
                    f"def _enc_{name}(v):\n    return {self.enc(ty, 'v.' + names[0])}\n\n\n")
        d = ", ".join(self.dec(f["type"], f"j[{i}]") for i, f in enumerate(fields))
        e = ", ".join(self.enc(f["type"], f"v.{n}") for n, f in zip(names, fields))
        return (f"def _dec_{name}(j):\n    return {cn}({d})\n\n\n"
                f"def _enc_{name}(v):\n    return [{e}]\n\n\n")


HEADER = '''\
# GENERATED by tools/gen_ast.py from schema/ast-schema.json
# (pandoc-api-version {api}). Do not edit; regenerate instead.
"""pandoc's document AST as Python dataclasses.

One class per pandoc-types constructor, with fields in the Haskell order.
``Pandoc.from_json`` / ``Pandoc.to_json`` (and ``from_json`` / ``to_json``
for any node type) convert to and from pandoc's JSON.
"""

from __future__ import annotations

import enum
import typing
from dataclasses import dataclass, field


class Node:
    """Base class of every AST node."""

    __slots__ = ()

    def to_json(self) -> typing.Any:  # noqa: D102
        """This node as pandoc's JSON (Python lists, dicts, str, ...)."""
        return _ENCODERS[_type_of(self)](self)

    @classmethod
    def from_json(cls, j: typing.Any) -> typing.Self:
        """A node of this type from pandoc's JSON."""
        return _DECODERS[_type_of_class(cls)](j)


'''

FOOTER = '''\
def _check_api_version(v):
    if v is None or list(v[:2]) != list(PANDOC_API_VERSION[:2]):
        raise ValueError(
            f"pandoc-api-version {v} is incompatible with {PANDOC_API_VERSION}"
        )


_DECODERS = {n[5:]: f for n, f in globals().items() if n.startswith("_dec_")}
_ENCODERS = {n[5:]: f for n, f in globals().items() if n.startswith("_enc_")}


def _type_of_class(cls: type) -> str:
    try:
        return _TYPE_OF[cls]
    except KeyError:
        raise TypeError(f"{cls.__name__} is not a pandoc AST type") from None


def _type_of(v: object) -> str:
    return _type_of_class(type(v))


def from_json(j: typing.Any) -> Pandoc:
    """A document from pandoc's JSON (as parsed by json.loads)."""
    return _dec_Pandoc(j)


def to_json(doc: Pandoc) -> typing.Any:
    """A document as pandoc's JSON (for json.dumps)."""
    return _enc_Pandoc(doc)
'''


def main() -> None:
    schema_path, out_path = sys.argv[1:3]
    schema = json.loads(Path(schema_path).read_text())
    Path(out_path).write_text(Generator(schema).generate())


if __name__ == "__main__":
    main()
