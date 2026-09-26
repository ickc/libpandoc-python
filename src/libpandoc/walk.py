"""Tree traversal for libpandoc.ast documents."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

from .ast import Node

__all__ = ["walk"]


def walk(node: Any, action: Callable[[Any], Any]) -> Any:
    """Apply ``action`` to every AST node, children before parents.

    ``action(node)`` returns None to keep the node, a node to replace it, or
    (for an element of a list, such as a ``Block`` in a document) a list of
    nodes to splice in its place, which may be empty to delete it. Returns
    the new tree; the input is modified in place where possible.
    """
    if isinstance(node, list):
        out: list[Any] = []
        for item in node:
            new = walk(item, action)
            if isinstance(new, list) and isinstance(item, Node):
                out.extend(new)
            else:
                out.append(new)
        node[:] = out
        return node
    if isinstance(node, tuple):
        return tuple(walk(item, action) for item in node)
    if isinstance(node, dict):
        for key, value in node.items():
            node[key] = walk(value, action)
        return node
    if isinstance(node, Node) and dataclasses.is_dataclass(node):
        for f in dataclasses.fields(node):
            setattr(node, f.name, walk(getattr(node, f.name), action))
        result = action(node)
        return node if result is None else result
    return node
