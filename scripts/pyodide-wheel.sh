#!/usr/bin/env bash
# The wheel for Pyodide: the package without its extension, with
# libpandoc.wasm and libpandoc's wasm/pyodide.mjs bundled
# (libpandoc-pyodide.mjs, from the same release) in libpandoc/_pyodide/,
# tagged pyemscripten_2026_0_wasm32 (PEP 783: Pyodide 314, Python 3.14), the
# only place it runs. micropip prefers it there; pip never takes it.
#
#   scripts/pyodide-wheel.sh <dir with both files> [out dir, default wheelhouse]
#
# Needs `pip` and `wheel` in $PYTHON (default python3).
set -euo pipefail
cd "$(dirname "$0")/.."
src=$1
out=${2:-wheelhouse}
py=${PYTHON:-python3}
data=src/libpandoc/_pyodide
tmp=$(mktemp -d)
trap 'rm -rf "$data" "$tmp"' EXIT
rm -rf "$data" build
mkdir -p "$data" "$out"
cp "$src/libpandoc.wasm" "$src/libpandoc-pyodide.mjs" "$data/"
LIBPANDOC_PYODIDE=1 "$py" -m pip wheel --no-deps -q -w "$tmp" .
"$py" -m wheel tags --remove --platform-tag pyemscripten_2026_0_wasm32 "$tmp"/libpandoc-*-py3-none-any.whl >/dev/null
mv "$tmp"/libpandoc-*-pyemscripten_2026_0_wasm32.whl "$out/"
rm -rf build
ls "$out"/libpandoc-*-pyemscripten_2026_0_wasm32.whl
