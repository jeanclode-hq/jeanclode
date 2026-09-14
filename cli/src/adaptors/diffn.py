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
_FILE_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
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
_NOISE_STUB_BINARY = "(content omitted, binary lockfile, asset or generated file)"
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


def _split_files(lines: list[str]) -> list[list[str]]:
    chunks: list[list[str]] = []
    for line in lines:
        if line.startswith("diff --git ") or not chunks:
            chunks.append([])
        chunks[-1].append(line)
    return chunks


def omit_noise_content(diff_text: str) -> str:
    """Replace the content of every noise file with a +/- count stub.

    Agents gain nothing from thousands of lockfile lines, and they push
    real PRs over the review size limit.
    """
    out: list[str] = []
    for chunk in _split_files(diff_text.splitlines()):
        m = _FILE_HEADER.match(chunk[0])
        if not m or not is_noise_path(m.group(2)):
            out.extend(chunk)
            continue
        out.append(chunk[0])
        out.extend(line for line in chunk[1:] if line.startswith(_KEPT_METADATA))
        if any(line.startswith(_BINARY_MARKERS) for line in chunk):
            out.append(_NOISE_STUB_BINARY)
            continue
        additions = deletions = 0
        in_hunks = False
        for line in chunk[1:]:
            if line.startswith("@@"):
                in_hunks = True
            elif in_hunks and line.startswith("+"):
                additions += 1
            elif in_hunks and line.startswith("-"):
                deletions += 1
        out.append(_NOISE_STUB.format(additions=additions, deletions=deletions))
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
    """The diff as agents see it: noise content omitted, lines numbered."""
    return format_diff_with_line_numbers(omit_noise_content(raw_diff))


def parse_diff_files(diff: str) -> list[DiffFile]:
    """List the files a ``prepare_diff`` output touches, with +/- counts."""
    files: list[DiffFile] = []
    for chunk in _split_files(diff.splitlines()):
        m = _FILE_HEADER.match(chunk[0])
        if not m:
            continue
        file = DiffFile(path=m.group(2), noise=is_noise_path(m.group(2)))
        in_hunks = False
        for line in chunk[1:]:
            if line.startswith("@@"):
                in_hunks = True
            elif line.startswith("new file mode"):
                file.status = "added"
            elif line.startswith("deleted file mode"):
                file.status = "deleted"
            elif line.startswith("rename from "):
                file.status = "renamed"
                file.old_path = line.removeprefix("rename from ")
            elif stub := _NOISE_STUB_RE.match(line):
                file.additions = int(stub.group(1) or 0)
                file.deletions = int(stub.group(2) or 0)
            elif in_hunks and (_ANNOTATED_ADDED.match(line) or line.startswith("+")):
                file.additions += 1
            elif in_hunks and (_ANNOTATED_DELETED.match(line) or line.startswith("-")):
                file.deletions += 1
        files.append(file)
    return files


def all_files_are_noise(diff: str) -> bool:
    """True if every file changed is a lockfile/asset (i.e. nothing to review)."""
    paths = [m.group(2) for line in diff.splitlines() if (m := _FILE_HEADER.match(line))]
    return bool(paths) and all(is_noise_path(p) for p in paths)
