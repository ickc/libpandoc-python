"""Builds libpandoc._core against an installed libpandoc.

libpandoc (include/libpandoc.h and the shared library) is looked for in
$LIBPANDOC_PREFIX, then $CONDA_PREFIX / $PREFIX (conda builds), then
sys.prefix. The extension uses the limited API, so the wheel is abi3,
except on free-threaded CPython, which has none (a cp314t wheel).
"""

import os
import sys
import sysconfig
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
# Where the extension finds libpandoc at run time, as libpandoc-rs's
# build script decides: $LIBPANDOC_RPATH if set ("$ORIGIN/..." relative to
# the extension; empty: none, for a wheel whose repair tool bundles the
# library), else the directory it was found in. Conda builds need nothing:
# rattler-build makes RPATHs into $PREFIX relative.
rpath = os.environ.get("LIBPANDOC_RPATH")
if rpath is None or rpath == "1":  # "1": what this variable used to take
    rpath = str(prefix / "lib")
elif sys.platform == "darwin":
    rpath = rpath.replace("$ORIGIN", "@loader_path")
runtime_dirs = [rpath] if rpath and sys.platform.startswith("linux") else []
# (setuptools gives macOS -L, not an rpath, for runtime_library_dirs)
link_args = [f"-Wl,-rpath,{rpath}"] if rpath and sys.platform == "darwin" else []
abi3 = not sysconfig.get_config_var("Py_GIL_DISABLED")

setup(
    ext_modules=[
        Extension(
            "libpandoc._core",
            sources=["src/libpandoc/_core.c"],
            include_dirs=[str(prefix / "include")],
            library_dirs=[str(prefix / "lib")],
            libraries=["pandoc"],
            runtime_library_dirs=runtime_dirs,
            extra_link_args=link_args,
            define_macros=[("Py_LIMITED_API", "0x030A0000")] if abi3 else [],
            py_limited_api=abi3,
        )
    ],
    options={"bdist_wheel": {"py_limited_api": "cp310"}} if abi3 else {},
)
