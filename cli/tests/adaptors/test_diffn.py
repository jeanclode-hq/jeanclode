"""Tests for diff preparation: noise omission and per-file stats."""

from __future__ import annotations

import pytest

from src.adaptors.diffn import (
    DiffFile,
    all_files_are_noise,
    format_diff_with_line_numbers,
    is_noise_path,
    parse_diff_files,
    prepare_diff,
)

_CODE = "diff --git a/src/app.py b/src/app.py\nindex 1..2 100644\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,3 @@\n keep\n-old\n+new\n+more\n"
_LOCK = "diff --git a/uv.lock b/uv.lock\nindex 1..2 100644\n--- a/uv.lock\n+++ b/uv.lock\n@@ -1,3 +1,2 @@\n ctx\n-pkg==1\n-dep==1\n+pkg==2\n"
_ICON = "diff --git a/web/logo.png b/web/logo.png\nnew file mode 100644\nindex 0..1\nBinary files /dev/null and b/web/logo.png differ\n"

# `glab mr diff` without --raw: no `diff --git` lines, and GitLab sends no content for
# collapsed files (the pnpm lockfile here).
_GLAB = (
    "--- src/app.py\n+++ src/app.py\n@@ -1,2 +1,3 @@\n keep\n-old\n+new\n+more\n"
    "--- uv.lock\n+++ uv.lock\n@@ -1,3 +1,2 @@\n ctx\n-pkg==1\n-dep==1\n+pkg==2\n"
    "--- web/logo.png\n+++ web/logo.png\nBinary files /dev/null and b/web/logo.png differ\n"
    "--- pnpm-lock.yaml\n+++ pnpm-lock.yaml\n"
    "--- src/new.py\n+++ src/new.py\n@@ -0,0 +1,1 @@\n+x = 1\n"
)
_STUB = "(content omitted, lockfile, asset or generated file: +1 -2)"
_STUB_NO_COUNTS = "(content omitted, lockfile, asset or generated file)"


def test_is_noise_path() -> None:
    assert is_noise_path("frontend/pnpm-lock.yaml")
    assert is_noise_path("uv.lock")
    assert is_noise_path("tests/__snapshots__/view.test.ts.snap")
    assert is_noise_path("dist/app.min.js")
    assert not is_noise_path("src/lock.py")
    assert not is_noise_path("docs/uv.lock.md")


@pytest.mark.parametrize("raw", [_CODE, _GLAB.split("--- uv.lock")[0]])
def test_prepare_diff_is_unchanged_without_noise(raw: str) -> None:
    assert prepare_diff(raw) == format_diff_with_line_numbers(raw)


def test_prepare_diff_stubs_git_noise_files() -> None:
    out = prepare_diff(_CODE + _LOCK + _ICON).splitlines()
    assert out == [
        *format_diff_with_line_numbers(_CODE).splitlines(),
        "diff --git a/uv.lock b/uv.lock",
        _STUB,
        "diff --git a/web/logo.png b/web/logo.png",
        "new file mode 100644",
        _STUB_NO_COUNTS,
    ]


def test_prepare_diff_stubs_glab_noise_files_and_keeps_every_other_line() -> None:
    numbered = format_diff_with_line_numbers(_GLAB).splitlines()
    # Headers that follow a hunk come out numbered, like content lines.
    lock = next(i for i, line in enumerate(numbered) if line.endswith(":--- uv.lock"))
    logo = next(i for i, line in enumerate(numbered) if line.endswith(":--- web/logo.png"))
    pnpm = numbered.index("--- pnpm-lock.yaml")
    new = numbered.index("--- src/new.py")

    assert prepare_diff(_GLAB).splitlines() == [
        *numbered[: lock + 2],
        _STUB,
        *numbered[logo : logo + 2],
        _STUB_NO_COUNTS,
        *numbered[pnpm : pnpm + 2],
        _STUB_NO_COUNTS,
        *numbered[new:],
    ]


def test_parse_diff_files_counts_prepared_git_diff() -> None:
    rename = "diff --git a/a.py b/b.py\nsimilarity index 90%\nrename from a.py\nrename to b.py\n"
    files = parse_diff_files(prepare_diff(_CODE + _LOCK + _ICON + rename))
    assert files == [
        DiffFile(path="src/app.py", additions=2, deletions=1),
        DiffFile(path="uv.lock", additions=1, deletions=2, noise=True),
        DiffFile(path="web/logo.png", status="added", noise=True),
        DiffFile(path="b.py", old_path="a.py", status="renamed"),
    ]


def test_parse_diff_files_counts_prepared_glab_diff() -> None:
    assert parse_diff_files(prepare_diff(_GLAB)) == [
        DiffFile(path="src/app.py", additions=2, deletions=1),
        DiffFile(path="uv.lock", additions=1, deletions=2, noise=True),
        DiffFile(path="web/logo.png", noise=True),
        DiffFile(path="pnpm-lock.yaml", noise=True),
        DiffFile(path="src/new.py", status="added", additions=1),
    ]


def test_all_files_are_noise() -> None:
    assert all_files_are_noise(prepare_diff(_LOCK + _ICON))
    assert not all_files_are_noise(prepare_diff(_LOCK + _CODE))
    assert all_files_are_noise(prepare_diff("--- uv.lock\n+++ uv.lock\n@@ -1 +1 @@\n-a\n+b\n"))
    assert not all_files_are_noise(prepare_diff(_GLAB))
    assert not all_files_are_noise("")
