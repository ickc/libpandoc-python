"""Builds libpandoc._core against an installed libpandoc.

libpandoc (include/libpandoc.h and the shared library) is looked for in
$LIBPANDOC_PREFIX, then $CONDA_PREFIX / $PREFIX (conda builds), then
sys.prefix. The extension uses the limited API, so the wheel is abi3.
"""

import os
import sys
from pathlib import Path

from setuptools import Extension, setup


def find_prefix() -> Path:
    for var in ("LIBPANDOC_PREFIX", "PREFIX", "CONDA_PREFIX"):
        if os.environ.get(var):
            prefix = Path(os.environ[var])
            if sys.platform == "win32" and (prefix / "Library").is_dir():
                prefix = prefix / "Library"
            if (prefix / "include" / "libpandoc.h").is_file():
                return prefix
    return Path(sys.prefix)


prefix = find_prefix()
runtime_dirs = []
if os.environ.get("LIBPANDOC_RPATH") and sys.platform != "win32":
    # for development against an uninstalled libpandoc
    runtime_dirs = [str(prefix / "lib")]

setup(
    ext_modules=[
        Extension(
            "libpandoc._core",
            sources=["src/libpandoc/_core.c"],
            include_dirs=[str(prefix / "include")],
            library_dirs=[str(prefix / "lib")],
            libraries=["pandoc"],
            runtime_library_dirs=runtime_dirs,
            define_macros=[("Py_LIMITED_API", "0x030A0000")],
            py_limited_api=True,
        )
    ],
    options={"bdist_wheel": {"py_limited_api": "cp310"}},
)
