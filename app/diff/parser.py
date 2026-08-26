"""Unified-diff parser.

Produces, per file, the *reconstructed new-file view*: every context and added
line with its real line number in the new file. Single-line rules only need the
added lines, but the multi-line rule (empty catch block) has to see the context
around them, so both are kept.

The parser is a small state machine rather than a regex sweep because the only
reliable way to know where a hunk body ends - and therefore whether a line
starting with "---" is a file header or just a removed line whose content
begins with "--" - is to count the lines the hunk header promised.
"""

from __future__ import annotations

import re

from app.models import DiffFile, Hunk, NewLine

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_GIT_RE = re.compile(r"^diff --git (.+)$")
_BINARY_RE = re.compile(r"^(GIT binary patch|Binary files .* differ)")


class DiffParseError(ValueError):
    """The payload is not usable as a unified diff."""


def _strip_eol(line: str) -> str:
    if line.endswith("\n"):
        line = line[:-1]
    if line.endswith("\r"):
        line = line[:-1]
    return line


def _unquote(path: str) -> str:
    """Git C-quotes paths containing spaces or non-ASCII bytes."""
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        inner = path[1:-1]
        try:
            return inner.encode("latin-1", "backslashreplace").decode("unicode_escape")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return inner
    return path


def _clean_path(raw: str) -> str:
    """A path header value such as "b/src/db.ts<TAB>2024-01-01" -> "src/db.ts"."""
    raw = raw.strip()
    # A tab separates the path from an optional timestamp in POSIX diffs.
    raw = raw.split("\t", 1)[0].rstrip()
    raw = _unquote(raw)
    if raw == "/dev/null":
        return raw
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    return raw


def _split_git_header(rest: str) -> tuple[str | None, str | None]:
    """Best-effort split of the "diff --git" paths (which may contain spaces)."""
    if rest.startswith('"'):
        return None, None  # quoted form: rely on the ---/+++ lines instead
    parts = rest.split(" ")
    if len(parts) == 2:
        return _clean_path(parts[0]), _clean_path(parts[1])
    # Ambiguous (spaces in the path). Try the a/... b/... midpoint heuristic.
    if len(parts) % 2 == 0 and parts:
        half = len(parts) // 2
        return _clean_path(" ".join(parts[:half])), _clean_path(" ".join(parts[half:]))
    return None, None


class _Block:
    """A single file's slice of the diff, accumulated verbatim."""

    __slots__ = (
        "raw", "old_path", "new_path", "git_old", "git_new",
        "hunks", "binary", "has_header",
    )

    def __init__(self) -> None:
        self.raw: list[str] = []
        self.old_path: str | None = None
        self.new_path: str | None = None
        self.git_old: str | None = None
        self.git_new: str | None = None
        self.hunks: list[Hunk] = []
        self.binary = False
        # True once a file header has been seen. A bare hunk with no header is
        # not a unified diff - there is no path to attribute findings to.
        self.has_header = False

    def resolve_path(self) -> str:
        for candidate in (self.new_path, self.git_new, self.old_path, self.git_old):
            if candidate and candidate != "/dev/null":
                return candidate
        return "unknown"

    def to_file(self) -> DiffFile:
        return DiffFile(
            path=self.resolve_path(),
            raw="".join(self.raw),
            hunks=self.hunks,
            binary=self.binary,
        )


def parse_diff(text: str) -> list[DiffFile]:
    """Parse a unified diff into per-file structures.

    Raises DiffParseError when the text cannot be read as a unified diff.
    The concatenation of every returned DiffFile.raw reproduces the input
    exactly, which is what lets the chunker do precise size accounting.
    """
    if not text or not text.strip():
        raise DiffParseError("diff is empty")

    lines = text.splitlines(keepends=True)
    blocks: list[_Block] = []
    preamble: list[str] = []
    current: _Block | None = None

    in_hunk = False
    remaining_old = 0
    remaining_new = 0
    new_lineno = 0
    hunk: Hunk | None = None

    def start_block() -> _Block:
        nonlocal current
        block = _Block()
        blocks.append(block)
        current = block
        return block

    i = 0
    while i < len(lines):
        line = lines[i]
        body = _strip_eol(line)

        # ---------------- inside a hunk body ----------------
        if in_hunk and current is not None and hunk is not None:
            if remaining_old <= 0 and remaining_new <= 0:
                in_hunk = False
                hunk = None
                continue  # re-dispatch this line as a header

            prefix = line[:1]
            if prefix == "\\":                      # "\ No newline at end of file"
                current.raw.append(line)
                i += 1
                continue
            if prefix == "+":
                hunk.lines.append(NewLine(new_lineno, body[1:], True))
                new_lineno += 1
                remaining_new -= 1
            elif prefix == "-":
                remaining_old -= 1
            elif prefix == " " or body == "":
                # A bare empty line is context for an empty source line; some
                # tools emit it without the leading space.
                text_value = body[1:] if prefix == " " else ""
                hunk.lines.append(NewLine(new_lineno, text_value, False))
                new_lineno += 1
                remaining_old -= 1
                remaining_new -= 1
            else:
                # Malformed body - the hunk stopped early. Re-dispatch.
                in_hunk = False
                hunk = None
                continue
            current.raw.append(line)
            i += 1
            continue

        # ---------------- headers / preamble ----------------
        git_match = _DIFF_GIT_RE.match(body)
        if git_match:
            block = start_block()
            block.git_old, block.git_new = _split_git_header(git_match.group(1))
            block.has_header = True
            block.raw.append(line)
            i += 1
            continue

        if body.startswith("--- "):
            # A new plain (non-git) file block begins here when the current one
            # already carries content. We are provably not inside a hunk.
            if current is None or current.hunks or current.old_path is not None:
                start_block()
            assert current is not None
            current.old_path = _clean_path(body[4:])
            current.has_header = True
            current.raw.append(line)
            i += 1
            continue

        if body.startswith("+++ "):
            if current is None:
                start_block()
            assert current is not None
            current.new_path = _clean_path(body[4:])
            current.has_header = True
            current.raw.append(line)
            i += 1
            continue

        hunk_match = _HUNK_RE.match(body)
        if hunk_match:
            if current is None:
                start_block()
            assert current is not None
            remaining_old = int(hunk_match.group(2) or 1)
            new_start = int(hunk_match.group(3))
            remaining_new = int(hunk_match.group(4) or 1)
            new_lineno = new_start
            hunk = Hunk(new_start=new_start)
            current.hunks.append(hunk)
            current.raw.append(line)
            in_hunk = True
            i += 1
            continue

        if _BINARY_RE.match(body):
            if current is None:
                start_block()
            assert current is not None
            current.binary = True

        # Any other line: index/mode/rename metadata, or leading preamble.
        if current is None:
            preamble.append(line)
        else:
            current.raw.append(line)
        i += 1

    if not blocks:
        raise DiffParseError("no file headers found; not a unified diff")

    # Keep byte accounting exact: anything before the first file header belongs
    # to the first block.
    if preamble:
        blocks[0].raw[:0] = preamble

    if not any(block.has_header and (block.hunks or block.binary) for block in blocks):
        raise DiffParseError("no file header with hunks found; not a unified diff")
    return [block.to_file() for block in blocks]
