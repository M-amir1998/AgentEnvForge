"""Archives, fingerprints and diffs of workspace trees."""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from agentenvforge.workspace.archive import (
    ArchiveTooLargeError,
    Ownership,
    create_directory_archive,
    create_files_archive,
    extract_archive,
    read_single_file,
)
from agentenvforge.workspace.diff import diff_trees
from agentenvforge.workspace.fingerprint import hash_directory


def make_tree(root: Path, files: dict[str, str | bytes]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="")
    return root


# --------------------------------------------------------------------- archives


def test_directory_archive_round_trip_with_ownership(tmp_path: Path) -> None:
    source = make_tree(tmp_path / "src", {"a.txt": "A", "pkg/b.py": "B"})
    with create_directory_archive(
        source, arcname="workspace", owner=Ownership(1000, 1001)
    ) as archive:
        with tarfile.open(fileobj=archive) as reader:
            members = reader.getmembers()
        assert {member.name for member in members} >= {
            "workspace",
            "workspace/a.txt",
            "workspace/pkg/b.py",
        }
        assert all((member.uid, member.gid) == (1000, 1001) for member in members)
        archive.seek(0)
        extract_archive(archive, tmp_path / "out", strip_components=1)
    assert (tmp_path / "out" / "pkg" / "b.py").read_text() == "B"


def test_files_archive_creates_parent_directories(tmp_path: Path) -> None:
    archive = create_files_archive({"x/y/z.txt": b"z"}, owner=Ownership(0, 0))
    extract_archive(archive, tmp_path)
    assert (tmp_path / "x" / "y" / "z.txt").read_bytes() == b"z"


def _tar_with(*members: tuple[tarfile.TarInfo, bytes | None]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    buffer.seek(0)
    return buffer


def test_unsafe_entries_are_skipped_not_extracted(tmp_path: Path) -> None:
    escape = tarfile.TarInfo("../escape.txt")
    escape.size = 1
    link = tarfile.TarInfo("passwd")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    good = tarfile.TarInfo("good.txt")
    good.size = 2

    skipped = extract_archive(
        _tar_with((escape, b"x"), (link, None), (good, b"ok")), tmp_path / "dest"
    )
    assert set(skipped) == {"../escape.txt", "passwd"}
    assert (tmp_path / "dest" / "good.txt").read_bytes() == b"ok"
    assert not (tmp_path / "escape.txt").exists()


def test_extraction_enforces_total_size_limit(tmp_path: Path) -> None:
    big = tarfile.TarInfo("big.bin")
    big.size = 100
    with pytest.raises(ArchiveTooLargeError):
        extract_archive(_tar_with((big, b"0" * 100)), tmp_path, max_total_bytes=10)


def test_read_single_file_respects_limit() -> None:
    info = tarfile.TarInfo("report.xml")
    info.size = 5
    assert read_single_file(_tar_with((info, b"hello")), max_bytes=10) == b"hello"
    with pytest.raises(ArchiveTooLargeError):
        read_single_file(_tar_with((info, b"hello")), max_bytes=4)


# ------------------------------------------------------------------ fingerprints


def test_hash_depends_on_content_not_location_or_caches(tmp_path: Path) -> None:
    first = make_tree(tmp_path / "one", {"a.py": "x", "b/c.txt": "y"})
    second = make_tree(tmp_path / "two", {"a.py": "x", "b/c.txt": "y", "__pycache__/a.pyc": b"\0"})
    assert hash_directory(first) == hash_directory(second)
    (second / "a.py").write_text("changed")
    assert hash_directory(first) != hash_directory(second)


def test_hash_distinguishes_renames(tmp_path: Path) -> None:
    first = make_tree(tmp_path / "one", {"a.txt": "same"})
    second = make_tree(tmp_path / "two", {"b.txt": "same"})
    assert hash_directory(first) != hash_directory(second)


# ------------------------------------------------------------------------- diffs


def test_diff_classifies_changes(tmp_path: Path) -> None:
    before = make_tree(
        tmp_path / "before", {"keep.txt": "same\n", "edit.txt": "old\n", "gone.txt": "x\n"}
    )
    after = make_tree(
        tmp_path / "after", {"keep.txt": "same\n", "edit.txt": "new\n", "added.txt": "y\n"}
    )
    diff = diff_trees(before, after)
    assert diff.added == ("added.txt",)
    assert diff.modified == ("edit.txt",)
    assert diff.deleted == ("gone.txt",)
    assert diff.changed_files == ("added.txt", "edit.txt", "gone.txt")
    assert "-old\n+new\n" in diff.patch


def test_identical_trees_produce_empty_diff(tmp_path: Path) -> None:
    before = make_tree(tmp_path / "before", {"a.txt": "a"})
    after = make_tree(tmp_path / "after", {"a.txt": "a", ".git/HEAD": "ref", "x.pyc": b"\0"})
    diff = diff_trees(before, after)
    assert diff.is_empty
    assert diff.patch == ""


def test_binary_changes_are_noted_not_inlined(tmp_path: Path) -> None:
    before = make_tree(tmp_path / "before", {"data.bin": b"\0\1"})
    after = make_tree(tmp_path / "after", {"data.bin": b"\0\2"})
    assert "Binary files a/data.bin and b/data.bin differ" in diff_trees(before, after).patch


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_patch_replays_exactly_with_git_apply(tmp_path: Path) -> None:
    files_before: dict[str, str | bytes] = {
        "edit.txt": "line 1\nline 2\nline 3\n",
        "no-newline.txt": "a\nb",
        "gone.txt": "bye\n",
    }
    files_after: dict[str, str | bytes] = {
        "edit.txt": "line 1\nline two\nline 3\n",
        "no-newline.txt": "a\nB",
        "nested/new.txt": "hello\n",
    }
    before = make_tree(tmp_path / "before", files_before)
    after = make_tree(tmp_path / "after", files_after)
    patch = tmp_path / "solution.patch"
    patch.write_text(diff_trees(before, after).patch, encoding="utf-8", newline="")

    replay = make_tree(tmp_path / "replay", files_before)
    # Pin line-ending handling: a global core.autocrlf=true would rewrite files on apply.
    git = ["git", "-c", "core.autocrlf=false"]
    subprocess.run([*git, "init", "-q"], cwd=replay, check=True)
    subprocess.run([*git, "apply", "--whitespace=nowarn", str(patch)], cwd=replay, check=True)
    assert diff_trees(after, replay).is_empty
