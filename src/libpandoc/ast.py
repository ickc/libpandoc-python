"""pandoc's document AST: the ``libpandoc-ast`` package, re-exported.

See https://github.com/ickc/libpandoc-ast. ``read`` returns, and ``write``
takes, its ``Pandoc``; ``convert(filters=[...])`` runs its ``Filter``s.
"""

from libpandoc_ast import *  # noqa: F403
from libpandoc_ast import __all__  # noqa: F401
