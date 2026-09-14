"""diffn — annotate a unified diff with old/new line numbers.

Output format (same as the predecessor project):
    Added lines:    "      42   :+code"
    Deleted lines:  "41        :-code"
    Context lines:  "41   , 42   : code"

Noise files (lockfiles, assets, generated code) keep their header but have
their content replaced by a one-line stub, see ``omit_noise_content``.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_GIT_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
# glab's default output has no `diff --git` line: a file opens with `--- path` / `+++ path`,
# and those get numbered like content lines when they follow a hunk.
_OLD_PATH = re.compile(r"^(?:\d+\s+:)?--- (.+)$")
_NEW_PATH = re.compile(r"^(?: {6}\d+\s*:)?\+\+\+ (.+)$")
_AFTER_HEADER = ("@@", "Binary files ", "(content omitted, ")
_BINARY_MARKERS = ("Binary files ", "GIT binary patch")
_KEPT_METADATA = ("new file mode", "deleted file mode", "rename from ", "rename to ")
_NOISE_PATTERNS = (
    re.compile(
        r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|"
        r"bun\.lockb?|poetry\.lock|uv\.lock|Pipfile\.lock|Cargo\.lock|go\.sum|"
        r"Gemfile\.lock|composer\.lock|mix\.lock|pubspec\.lock|Podfile\.lock|"
        r"flake\.lock|packages\.lock\.json)$"
    ),
    re.compile(r"\.min\.(js|css)$|\.(js|css)\.map$"),
    re.compile(r"(^|/)__snapshots__/|\.snap$"),
    re.compile(r"\.pb\.go$|_pb2(_grpc)?\.py$"),
    re.compile(
        r"\.(png|jpg|jpeg|gif|webp|avif|bmp|svg|ico|woff2?|ttf|otf|eot|pdf|"
        r"mp3|mp4|webm|zip|gz|tar|jar)$"
    ),
)
_NOISE_STUB = "(content omitted, lockfile, asset or generated file: +{additions} -{deletions})"
_NOISE_STUB_NO_COUNTS = "(content omitted, lockfile, asset or generated file)"
_NOISE_STUB_RE = re.compile(r"^\(content omitted, .*?(?:: \+(\d+) -(\d+))?\)$")
_ANNOTATED_ADDED = re.compile(r"^ {6}\d+\s*:\+")
_ANNOTATED_DELETED = re.compile(r"^\d+\s+:-")


class DiffFile(BaseModel):
    path: str
    old_path: str = ""
    status: Literal["added", "modified", "deleted", "renamed"] = "modified"
    additions: int = 0
    deletions: int = 0
    noise: bool = False


def is_noise_path(path: str) -> bool:
    return any(p.search(path) for p in _NOISE_PATTERNS)


def _is_git_format(lines: list[str]) -> bool:
    return any(line.startswith("diff --git ") for line in lines)


def _opens_file(lines: list[str], i: int, *, git: bool) -> bool:
    if git:
        return lines[i].startswith("diff --git ")
    if i + 1 >= len(lines) or not _OLD_PATH.match(lines[i]) or not _NEW_PATH.match(lines[i + 1]):
        return False
    if i + 2 >= len(lines):
        return True
    after = lines[i + 2]
    return after.startswith(_AFTER_HEADER) or bool(_OLD_PATH.match(after))


def _split_files(lines: list[str]) -> list[list[str]]:
    git = _is_git_format(lines)
    chunks: list[list[str]] = []
    for i, line in enumerate(lines):
        if not chunks or _opens_file(lines, i, git=git):
            chunks.append([])
        chunks[-1].append(line)
    return chunks


def _chunk_paths(chunk: list[str]) -> tuple[str, str] | None:
    """``(old_path, new_path)`` from a file chunk's header."""
    if m := _GIT_HEADER.match(chunk[0]):
        return m.group(1), m.group(2)
    old = _OLD_PATH.match(chunk[0])
    new = _NEW_PATH.match(chunk[1]) if len(chunk) > 1 else None
    if not old or not new:
        return None
    old_path, new_path = old.group(1), new.group(1)
    return (
        new_path if old_path == "/dev/null" else old_path,
        old_path if new_path == "/dev/null" else new_path,
    )


def _count_changes(chunk: list[str]) -> tuple[int, int, bool]:
    additions = deletions = 0
    in_hunks = False
    for line in chunk:
        if line.startswith("@@"):
            in_hunks = True
        elif not in_hunks:
            continue
        elif _ANNOTATED_ADDED.match(line) or line.startswith("+"):
            additions += 1
        elif _ANNOTATED_DELETED.match(line) or line.startswith("-"):
            deletions += 1
    return additions, deletions, in_hunks


def omit_noise_content(diff: str) -> str:
    """Replace the content of every noise file with a +/- count stub.

    Runs on the numbered diff and emits every other file's lines verbatim,
    so what an agent sees for real code is byte-identical either way.
    """
    out: list[str] = []
    for chunk in _split_files(diff.splitlines()):
        paths = _chunk_paths(chunk)
        if not paths or not is_noise_path(paths[1]):
            out.extend(chunk)
            continue
        if chunk[0].startswith("diff --git "):
            out.append(chunk[0])
            out.extend(line for line in chunk[1:] if line.startswith(_KEPT_METADATA))
        else:
            out.extend(chunk[:2])
        additions, deletions, has_hunks = _count_changes(chunk)
        if has_hunks and not any(line.startswith(_BINARY_MARKERS) for line in chunk):
            out.append(_NOISE_STUB.format(additions=additions, deletions=deletions))
        else:
            out.append(_NOISE_STUB_NO_COUNTS)
    return "\n".join(out)


def format_diff_with_line_numbers(diff_text: str) -> str:
    """Annotate each line of a unified diff with old/new line numbers."""
    out: list[str] = []
    old, new = 0, 0
    in_hunk = False
    for line in diff_text.splitlines():
        m = _HUNK_HEADER.match(line)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            in_hunk = True
            out.append(line)
            continue
        if not in_hunk or not line:
            out.append(line)
            continue
        prefix, content = line[0], line[1:]
        if prefix == "+":
            out.append(f"      {new:<5}:+{content}")
            new += 1
        elif prefix == "-":
            out.append(f"{old:<5}     :-{content}")
            old += 1
        elif prefix == " ":
            out.append(f"{old:<5}, {new:<5}: {content}")
            old += 1
            new += 1
        else:
            in_hunk = False
            out.append(line)
    return "\n".join(out)


def prepare_diff(raw_diff: str) -> str:
    """The diff as agents see it: lines numbered, then noise content omitted."""
    return omit_noise_content(format_diff_with_line_numbers(raw_diff))


def parse_diff_files(diff: str) -> list[DiffFile]:
    """List the files a ``prepare_diff`` output touches, with +/- counts."""
    files: list[DiffFile] = []
    for chunk in _split_files(diff.splitlines()):
        paths = _chunk_paths(chunk)
        if not paths:
            continue
        old_path, path = paths
        file = DiffFile(path=path, noise=is_noise_path(path))
        if chunk[0].startswith("diff --git "):
            for line in chunk[1:]:
                if line.startswith("new file mode"):
                    file.status = "added"
                elif line.startswith("deleted file mode"):
                    file.status = "deleted"
                elif line.startswith("rename from "):
                    file.status = "renamed"
                    file.old_path = line.removeprefix("rename from ")
        elif old_path != path:
            file.status = "renamed"
            file.old_path = old_path
        else:
            first_hunk = next((line for line in chunk if line.startswith("@@")), "")
            if first_hunk.startswith("@@ -0,0 "):
                file.status = "added"
            elif " +0,0 @@" in first_hunk:
                file.status = "deleted"
        stub = next((m for line in chunk if (m := _NOISE_STUB_RE.match(line))), None)
        if stub:
            file.additions, file.deletions = int(stub.group(1) or 0), int(stub.group(2) or 0)
        else:
            file.additions, file.deletions, _ = _count_changes(chunk)
        files.append(file)
    return files


def all_files_are_noise(diff: str) -> bool:
    """True if every file changed is a lockfile/asset (i.e. nothing to review)."""
    paths = [p[1] for chunk in _split_files(diff.splitlines()) if (p := _chunk_paths(chunk))]
    return bool(paths) and all(is_noise_path(p) for p in paths)
