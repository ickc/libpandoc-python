"""pandocpy against pandoc's own command tests.

    python tests/conformance.py PANDOC_SOURCE [--pandoc PATH] [--jobs N]

PANDOC_SOURCE is pandoc's source (the release tarball, or a checkout) of the
same version as libpandoc's pandoc: its ``test/command/*.md`` hold ~1200
tests, each a command (``% pandoc ...``), its input, and its expected output
(stderr lines as ``2> ...``, a nonzero status as ``=> N``). They run as
pandoc's own runner (test/Tests/Command.hs) runs them: from ``test/``, with
its environment, through a shell, ``pandoc`` standing for the command
tested.

Some tests depend on pandoc's source tree or build (data files, the test
executable's features), so the suite is run twice: with the pandoc command
(``--pandoc``, the same version) and with pandocpy. What fails only with
pandocpy is a deviation; the exit status is their number.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandom

import libpandoc


def load_tests(command_dir: Path) -> list[tuple[str, int, str, str, str]]:
    """(file, number, command, input, expected) for each command test."""
    tests = []
    paths = sorted(command_dir.glob("*.md"))
    # as the runner reads them: pandoc's markdown reader, tabs kept, the
    # top-level code blocks
    docs = libpandoc.read_many([p.read_text(encoding="utf-8") for p in paths],
                               "markdown", preserve_tabs=True)
    for path, doc in zip(paths, docs):
        blocks = [b.text for b in doc.blocks if isinstance(b, pandom.CodeBlock)]
        num = 0
        for code in blocks:
            lines = hs_lines(code)
            i = 0
            joined = []
            while i < len(lines) and lines[i].endswith("\\"):
                joined.append(lines[i][:-1])
                i += 1
            if i >= len(lines):
                continue
            joined.append(lines[i])
            first = " ".join(joined)
            if not first.startswith("%"):
                continue
            num += 1
            rest = lines[i + 1:]
            end = rest.index("^D") if "^D" in rest else len(rest)
            expected = []
            for line in rest[end + 1:]:
                if line == ".":
                    break
                expected.append(line)
            tests.append((
                path.name, num, first[1:].lstrip(" "),
                "".join(line + "\n" for line in rest[:end]),
                "".join(line + "\n" for line in expected),
            ))
    return tests


def hs_lines(s: str) -> list[str]:
    """Haskell's ``lines``: no empty last line for a trailing newline."""
    out = s.split("\n")
    return out[:-1] if out and out[-1] == "" else out


def emulate(cmd: str, pandoc: str) -> str:
    """``pandoc`` at the start and after ``| ``, as pandocToEmulate."""
    if cmd.startswith("pandoc"):
        cmd = pandoc + cmd[len("pandoc"):]
    return re.sub(r"\| pandoc", "| " + pandoc.replace("\\", "\\\\"), cmd)


def run(test_dir: Path, pandoc: str, cmd: str, stdin: str) -> str:
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "SYSTEMROOT", "PYTHONPATH")}
    env.update({"TMP": ".", "LANG": "en_US.UTF-8", "HOME": "./"})
    try:
        p = subprocess.run(emulate(cmd, pandoc), shell=True, cwd=test_dir, env=env,
                           input=stdin.encode(), capture_output=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        return "(timed out)\n"
    err = "".join("2> " + line + "\n" for line in p.stderr.decode("utf-8", "replace").splitlines())
    out = (err + p.stdout.decode("utf-8", "replace")).replace("\r", "")
    out = out.replace("pandocpy", "pandoc")
    if p.returncode:
        out += f"=> {p.returncode}\n"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", type=Path)
    ap.add_argument("--pandoc", default="pandoc", help="the pandoc command to compare with")
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    ap.add_argument("--only", help="run the tests of this file only")
    args = ap.parse_args()
    test_dir = args.source / "test"
    tests = load_tests(test_dir / "command")
    if args.only:
        tests = [t for t in tests if t[0] == args.only]
    pandocpy = f"{shlex.quote(sys.executable)} -m libpandoc"

    def fails(pandoc: str) -> dict[tuple[str, int], str]:
        def one(t):
            return t, run(test_dir, pandoc, t[2], t[3])
        with ThreadPoolExecutor(args.jobs) as ex:
            return {(t[0], t[1]): out for t, out in ex.map(one, tests) if out != t[4]}

    theirs = fails(args.pandoc)
    ours = fails(pandocpy)
    only_ours = sorted(set(ours) - set(theirs))
    print(f"{len(tests)} tests: pandoc fails {len(theirs)}, pandocpy {len(ours)}, "
          f"pandocpy only {len(only_ours)}")
    by_key = {(t[0], t[1]): t for t in tests}
    for key in only_ours:
        t = by_key[key]
        print(f"\n--- {t[0]} #{t[1]}: % {t[2]}\nexpected:\n{t[4]}got:\n{ours[key]}")
    return len(only_ours)


if __name__ == "__main__":
    sys.exit(main())
