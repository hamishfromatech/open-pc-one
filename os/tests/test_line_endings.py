"""A CRLF checkout must fail loudly, in one place, with a useful message.

``.gitattributes`` pins ``eol=lf`` for the whole tree, but that only protects
clones made after it existed. A Windows checkout with ``core.autocrlf=true``, a
source archive zipped on Windows, or a later ``.gitattributes`` edit can still
put CR bytes into the tree. When that happens the symptoms are six unrelated
test failures -- bash rejects ``set -euo pipefail<CR>`` as an option name, and
the SHA-256-pinned OpenAPI spec hashes differently -- which sends the reader
looking for bugs that are not there.

This test names the real cause. It checks shell scripts byte for byte, plus the
files whose bytes are load-bearing (the pinned REST spec, the systemd units,
mkosi config and the JSON/YAML manifests). Python sources are deliberately not
scanned: a fixture may legitimately embed a CR in a string.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Never part of the published tree, and never worth scanning.
_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    ".devstate",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}
_EXCLUDED_PREFIXES = (
    "os/image/wheels",
    "os/image/artifacts",
    "build",
    "dist",
)

_SHELL_SUFFIXES = {".sh", ".bash"}
_LF_SENSITIVE_SUFFIXES = {
    ".json",
    ".yaml",
    ".yml",
    ".service",
    ".target",
    ".mount",
    ".conf",
    ".ini",
    ".txt",
}
_LF_SENSITIVE_NAMES = {"Makefile", "Dockerfile", "mkosi.postinst", "pytest.ini", "ruff.toml"}

_REMEDY = """\
These files were checked out with CRLF line endings. CR bytes break bash
option parsing (os/scripts/*) and change the bytes of files whose hash is
pinned (contract/openapi.json), which is what produces the confusing
unrelated failures. Fix the checkout; do not edit the files:

    git config core.autocrlf false
    git rm --cached -r . && git reset --hard

or clone with `git -c core.autocrlf=false clone <url>`. A fresh clone is
unaffected: .gitattributes pins `* text=auto eol=lf` and a test asserts it
stays there."""


def _relative_parts(relative: str) -> tuple[str, ...]:
    return Path(relative).parts


def _excluded(relative: str) -> bool:
    parts = _relative_parts(relative)
    if any(part in _EXCLUDED_DIRS for part in parts):
        return True
    return any(relative == prefix or relative.startswith(prefix + "/") for prefix in _EXCLUDED_PREFIXES)


def _tracked_files() -> list[Path]:
    """Tracked files when git is available, otherwise the working tree.

    The dev container has no git and no .git directory, so it falls back to a
    walk. Both paths yield repository-relative paths under ``REPO``.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "-z"],
            capture_output=True,
            check=False,
        )
    except OSError:
        result = None

    if result is not None and result.returncode == 0:
        names = [name for name in result.stdout.decode("utf-8", "replace").split("\0") if name]
        if names:
            return [REPO / name for name in names]

    return sorted(
        path
        for path in REPO.rglob("*")
        if path.is_file() and not _excluded(path.relative_to(REPO).as_posix())
    )


def _is_shell_script(relative: str, data: bytes) -> bool:
    path = Path(relative)
    if path.suffix in _SHELL_SUFFIXES:
        return True
    if path.parts[:2] == ("os", "scripts"):
        return True
    first_line = data.split(b"\n", 1)[0]
    return first_line.startswith(b"#!") and b"sh" in first_line


def _is_lf_sensitive(relative: str) -> bool:
    path = Path(relative)
    return path.suffix in _LF_SENSITIVE_SUFFIXES or path.name in _LF_SENSITIVE_NAMES


def cr_byte_offenders(files: list[Path], root: Path = REPO) -> list[str]:
    """Return one line per file that contains a CR where it must not."""
    problems: list[str] = []
    for file in files:
        try:
            data = file.read_bytes()
        except OSError:
            continue
        if b"\r" not in data:
            continue
        relative = file.relative_to(root).as_posix()
        if not (_is_shell_script(relative, data) or _is_lf_sensitive(relative)):
            continue
        first_line = data[: data.index(b"\r")].count(b"\n") + 1
        count = data.count(b"\r")
        problems.append(f"{relative}: {count} CR byte(s), first on line {first_line}")
    return problems


def test_no_shell_script_contains_a_carriage_return() -> None:
    scripts = [
        path
        for path in _tracked_files()
        if _is_shell_script(path.relative_to(REPO).as_posix(), path.read_bytes()[:512])
    ]
    assert scripts, "no shell scripts found -- the scan itself is broken"
    problems = cr_byte_offenders(scripts)
    assert not problems, "CRLF shell scripts:\n  " + "\n  ".join(problems) + "\n\n" + _REMEDY


def test_no_hash_or_config_file_contains_a_carriage_return() -> None:
    candidates = [
        path
        for path in _tracked_files()
        if _is_lf_sensitive(path.relative_to(REPO).as_posix())
    ]
    assert candidates, "no JSON/YAML/systemd files found -- the scan itself is broken"
    problems = cr_byte_offenders(candidates)
    assert not problems, "CRLF in byte-sensitive files:\n  " + "\n  ".join(problems) + "\n\n" + _REMEDY


def test_the_scan_finds_a_crlf_script_and_a_crlf_contract(tmp_path: Path) -> None:
    # The guard has to be demonstrated against a broken tree, otherwise a
    # matcher that never matches would pass the tests above.
    (tmp_path / "os" / "scripts").mkdir(parents=True)
    (tmp_path / "contract").mkdir()
    (tmp_path / "os" / "scripts" / "pin-artifacts").write_bytes(
        b"#!/bin/bash\r\nset -euo pipefail\r\necho ok\r\n"
    )
    (tmp_path / "contract" / "openapi.json").write_bytes(b'{\r\n  "openapi": "3.1.0"\r\n}\r\n')
    # An LF script and an unrelated Markdown file with a CR must not be flagged.
    (tmp_path / "os" / "scripts" / "ota-update").write_bytes(b"#!/bin/bash\nset -eu\necho ok\n")
    (tmp_path / "README.md").write_bytes(b"harmless\r\n")

    problems = cr_byte_offenders(
        sorted(path for path in tmp_path.rglob("*") if path.is_file()), tmp_path
    )

    joined = "\n".join(problems)
    assert "os/scripts/pin-artifacts: 3 CR byte(s), first on line 1" in joined
    assert "contract/openapi.json: 3 CR byte(s), first on line 1" in joined
    assert "ota-update" not in joined
    assert "README.md" not in joined


def test_gitattributes_pins_lf_for_the_whole_tree() -> None:
    attributes = REPO / ".gitattributes"
    assert attributes.is_file(), ".gitattributes is what keeps a Windows checkout byte-identical"
    rules = [
        line.strip()
        for line in attributes.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "* text=auto eol=lf" in rules
    for pattern in ("os/scripts/*", "*.json", "*.yaml", "*.yml"):
        assert any(rule.split()[0] == pattern and "eol=lf" in rule for rule in rules), (
            f".gitattributes lost the LF rule for {pattern}"
        )
