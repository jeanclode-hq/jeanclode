"""diffn — annotate a unified diff with old/new line numbers.

Output format (same as the predecessor project):
    Added lines:    "      42   :+code"
    Deleted lines:  "41        :-code"
    Context lines:  "41   , 42   : code"
"""

from __future__ import annotations

import re

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_BINARY_MARKERS = ("Binary files ", "GIT binary patch")
_NOISE_FILE_RE = re.compile(r"^diff --git a/(.+?) b/", re.MULTILINE)
_NOISE_PATTERNS = (
    re.compile(
        r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|"
        r"uv\.lock|Cargo\.lock|go\.sum)$"
    ),
    re.compile(r"\.min\.(js|css)$"),
    re.compile(r"\.(png|jpg|jpeg|gif|svg|ico|woff2?|ttf|otf|eot|pdf)$"),
)


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


def all_files_are_noise(diff: str) -> bool:
    """True if every file changed is a lockfile/asset (i.e. nothing to review)."""
    files = _NOISE_FILE_RE.findall(diff)
    if not files:
        return False
    return all(any(p.search(f) for p in _NOISE_PATTERNS) for f in files)


def is_binary_only(diff: str) -> bool:
    """True if the diff is entirely binary changes."""
    return any(marker in diff for marker in _BINARY_MARKERS) and all_files_are_noise(diff)
