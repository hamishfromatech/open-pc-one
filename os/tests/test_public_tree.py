"""The published tree must not contain internal material or documentation.

This runs the same checker CI runs (``scripts/check_public_tree.py``) against the
repository, and tests the checker itself against temporary trees so a broken
matcher cannot silently pass.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_public_tree.py"


def run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_repository_is_clean() -> None:
    result = run(REPO)
    assert result.returncode == 0, result.stderr
    assert "public tree clean" in result.stdout


def test_sources_directory_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "docs" / "source").mkdir(parents=True)
    (tmp_path / "docs" / "source" / "anything.md").write_text("harmless\n", encoding="utf-8")
    result = run(tmp_path)
    assert result.returncode == 1
    assert "forbidden path" in result.stderr


def test_any_documentation_directory_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "architecture.md").write_text("harmless\n", encoding="utf-8")
    result = run(tmp_path)
    assert result.returncode == 1
    assert "docs/architecture.md" in result.stderr
    assert "forbidden path" in result.stderr


def test_a_staged_document_is_rejected(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        return  # the container has no git; the filesystem check covers that path
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("harmless\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "docs/notes.md"], check=True)
    # Remove the working copy so only the index can catch it.
    (tmp_path / "docs" / "notes.md").unlink()
    (tmp_path / "docs").rmdir()
    result = run(tmp_path)
    assert result.returncode == 1
    assert "tracked path" in result.stderr


def test_commercial_file_names_are_rejected(tmp_path: Path) -> None:
    for name in ("unit-BOM.csv", "supplier-list.md", "pricing.md", "competitor-teardown.md"):
        (tmp_path / name).write_text("harmless\n", encoding="utf-8")
    result = run(tmp_path)
    assert result.returncode == 1
    for name in ("unit-BOM.csv", "supplier-list.md", "pricing.md", "competitor-teardown.md"):
        assert name in result.stderr


def test_identifiers_and_cost_figures_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text(
        "\n".join(
            [
                "host 192" + ".168.4.10",
                "see the Paper" + "clip task",
                "board prefix pcp_" + "board_x",
                "cost A" + "$699 and landed US" + "$281",
                "the FO" + "B price and the MO" + "Q",
                "gross mar" + "gin 60%",
                "api at atech" + ".industries",
            ]
        ),
        encoding="utf-8",
    )
    result = run(tmp_path)
    assert result.returncode == 1
    for needle in ("private LAN", "internal task system", "internal API prefix", "retail price",
                   "landed cost", "supplier price term", "commercial gross-profit figure",
                   "internal hostname"):
        assert needle in result.stderr


def test_binary_files_do_not_crash_the_checker(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
