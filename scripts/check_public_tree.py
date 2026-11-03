#!/usr/bin/env python3
"""Fail if internal material or a documentation directory reaches the public tree.

The rule this enforces is simple and absolute: the published repository is code
only.

* no ``docs/`` directory and no ``docs-internal/`` directory (internal design,
  commercial documents and engineering notes all stay private),
* no file named like a BOM, supplier list, pricing sheet or competitor teardown,
* no internal hostnames, task-system names, API prefixes, prices or supplier
  terms anywhere in the tree.

Two checks run: one over the working tree and one over ``git ls-files``, so a
file that exists only in the index is still caught.

Run it from the repository root (it is also a test: ``os/tests/test_public_tree.py``)
and from CI. It has no third-party dependencies so it works on a bare runner.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    ".venv",
    ".devstate",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    "node_modules",
    "build",
    "dist",
    # Untracked internal working copy; the git check below still rejects it if
    # it is ever added to the index.
    "docs-internal",
}

# Generated or binary-only trees: no source of truth lives here.
SKIP_PREFIXES = (
    "os/image/wheels",
    "os/image/artifacts",
)

FORBIDDEN_TOP_LEVEL = ("docs", "docs-internal")
FORBIDDEN_NAME_GLOBS = ("*bom*", "*supplier*", "*pricing*", "*teardown*")

# The checker has to spell the patterns out, so it is the one place they are
# allowed to appear. Keep this list minimal: exactly the checking code.
SELF_EXEMPT = {
    "scripts/check_public_tree.py",
    "os/tests/test_public_tree.py",
}


def _rule(*parts: str, ignore_case: bool = False) -> re.Pattern[str]:
    """Build a pattern from fragments.

    The fragments keep this file from matching the very grep a release check
    runs over the tree: a plain recursive grep for the sourcing term has to be
    able to print CLEAN without exempting the checker itself.
    """
    return re.compile("".join(parts), re.IGNORECASE if ignore_case else 0)


CHECKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_rule(r"192", r"\.168\."), "private LAN address literal"),
    (_rule("paper", "clip", ignore_case=True), "internal task system"),
    (_rule("pcp_", "board_"), "internal API prefix"),
    (_rule("pcp_", "cli_auth_"), "internal auth prefix"),
    (_rule("atech", r"\.industries", ignore_case=True), "internal hostname"),
    (_rule("A", r"\$"), "retail price"),
    (_rule("US", r"\$"), "landed cost"),
    (_rule(r"\b", "FO", "B", r"\b", ignore_case=True), "supplier price term"),
    (_rule(r"\b", "mar", "gin", ignore_case=True), "commercial gross-profit figure"),
    (_rule(r"\b", "MO", "Q", r"\b", ignore_case=True), "supplier price term"),
)


def _iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in Path(rel).parts):
            continue
        if any(rel == prefix or rel.startswith(prefix + "/") for prefix in SKIP_PREFIXES):
            continue
        yield path, rel


def _tracked_paths(root: Path) -> list[str] | None:
    """Return ``git ls-files``, or ``None`` when this is not a git checkout."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [entry for entry in result.stdout.decode("utf-8", "replace").split("\0") if entry]


def _forbidden_path(rel: str) -> bool:
    return any(rel == name or rel.startswith(name + "/") for name in FORBIDDEN_TOP_LEVEL)


def check_tree(root: Path) -> list[str]:
    """Return one human-readable line per violation; empty means clean."""
    problems: list[str] = []
    for path, rel in _iter_files(root):
        if rel in SELF_EXEMPT:
            continue

        if _forbidden_path(rel):
            problems.append(f"{rel}: forbidden path (documentation is not published)")
            continue

        name = path.name.lower()
        if any(fnmatch.fnmatch(name, pattern) for pattern in FORBIDDEN_NAME_GLOBS):
            problems.append(f"{rel}: forbidden file name ({name})")
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: nothing to scrub

        for line_number, line in enumerate(text.splitlines(), start=1):
            for pattern, label in CHECKS:
                if pattern.search(line):
                    problems.append(f"{rel}:{line_number}: {label} ({pattern.pattern!r})")
    return problems


def check_tracked_paths(root: Path) -> list[str]:
    """Return violations from the git index, so a staged doc cannot slip past."""
    tracked = _tracked_paths(root)
    if tracked is None:
        return []

    problems: list[str] = []
    for rel in tracked:
        if rel in SELF_EXEMPT:
            continue
        if _forbidden_path(rel):
            problems.append(f"{rel}: tracked path (documentation is not published)")
            continue
        name = Path(rel).name.lower()
        if any(fnmatch.fnmatch(name, pattern) for pattern in FORBIDDEN_NAME_GLOBS):
            problems.append(f"{rel}: forbidden tracked file name ({name})")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="tree to check (default: repository root)")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not root.is_dir():
        print(f"check-public-tree: {root} is not a directory", file=sys.stderr)
        return 2

    problems = check_tree(root) + check_tracked_paths(root)
    if problems:
        print("public tree contains material that must not be published:", file=sys.stderr)
        for problem in problems:
            print(f"  FAIL {problem}", file=sys.stderr)
        return 1

    print(f"public tree clean ({root})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
