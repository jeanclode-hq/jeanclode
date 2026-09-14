"""Tests for diff preparation: noise omission and per-file stats."""

from __future__ import annotations

from src.adaptors.diffn import (
    DiffFile,
    all_files_are_noise,
    is_noise_path,
    omit_noise_content,
    parse_diff_files,
    prepare_diff,
)

_CODE = "diff --git a/src/app.py b/src/app.py\nindex 1..2 100644\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,3 @@\n keep\n-old\n+new\n+more\n"
_LOCK = "diff --git a/uv.lock b/uv.lock\nindex 1..2 100644\n--- a/uv.lock\n+++ b/uv.lock\n@@ -1,3 +1,2 @@\n ctx\n-pkg==1\n-dep==1\n+pkg==2\n"
_ICON = "diff --git a/web/logo.png b/web/logo.png\nnew file mode 100644\nindex 0..1\nBinary files /dev/null and b/web/logo.png differ\n"


def test_is_noise_path() -> None:
    assert is_noise_path("frontend/pnpm-lock.yaml")
    assert is_noise_path("uv.lock")
    assert is_noise_path("tests/__snapshots__/view.test.ts.snap")
    assert is_noise_path("dist/app.min.js")
    assert not is_noise_path("src/lock.py")
    assert not is_noise_path("docs/uv.lock.md")


def test_omit_noise_content_replaces_lockfile_hunks_with_counts() -> None:
    out = omit_noise_content(_CODE + _LOCK)
    assert "pkg==" not in out
    assert "diff --git a/uv.lock b/uv.lock" in out
    assert "(content omitted, lockfile, asset or generated file: +1 -2)" in out
    assert "+more" in out


def test_omit_noise_content_stubs_binary_assets_and_keeps_status() -> None:
    out = omit_noise_content(_ICON)
    assert out.splitlines() == [
        "diff --git a/web/logo.png b/web/logo.png",
        "new file mode 100644",
        "(content omitted, binary lockfile, asset or generated file)",
    ]


def test_omit_noise_content_leaves_code_untouched() -> None:
    assert omit_noise_content(_CODE) == _CODE.rstrip("\n")


def test_parse_diff_files_counts_prepared_diff() -> None:
    rename = "diff --git a/a.py b/b.py\nsimilarity index 90%\nrename from a.py\nrename to b.py\n"
    files = parse_diff_files(prepare_diff(_CODE + _LOCK + _ICON + rename))
    assert files == [
        DiffFile(path="src/app.py", additions=2, deletions=1),
        DiffFile(path="uv.lock", additions=1, deletions=2, noise=True),
        DiffFile(path="web/logo.png", status="added", noise=True),
        DiffFile(path="b.py", old_path="a.py", status="renamed"),
    ]


def test_all_files_are_noise() -> None:
    assert all_files_are_noise(prepare_diff(_LOCK + _ICON))
    assert not all_files_are_noise(prepare_diff(_LOCK + _CODE))
    assert not all_files_are_noise("")
