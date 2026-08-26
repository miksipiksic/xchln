"""The deterministic mock provider - the scored rule set.

Rules apply to added lines only ("+", never the "+++" header, which the parser
never surfaces as hunk content). "line" is the line number in the new file, and
one finding is produced per matching line per rule.

Where the brief leaves room for interpretation the choice is recorded inline and
in SUBMISSION.md; those calls are:

  MOCK-003  "concatenated with +" is read literally - a template literal with
            ${} interpolation does not fire, a quote adjacent to a + does.
  MOCK-005  "=== null" / "!== null" do NOT fire. The rule is titled "loose null
            comparison"; a strict comparison is the obvious negative case, and a
            false positive costs more than a missed literal substring match.
  MOCK-008  case-sensitive TODO/FIXME. MOCK-INJ explicitly says
            "case-insensitive" and this row does not; the contrast is deliberate.
"""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor

from app.config import MAX_CONCURRENT_JOBS
from app.models import DiffChunk, DiffFile, Finding, Hunk
from app.providers.base import Provider

# --------------------------------------------------------------------------- #
# Rule metadata (severity / category / title come straight from the table)
# --------------------------------------------------------------------------- #
RULE_META: dict[str, tuple[str, str, str]] = {
    "MOCK-001": ("critical", "security", "eval usage"),
    "MOCK-002": ("critical", "security", "hardcoded credential"),
    "MOCK-003": ("high", "security", "SQL string concatenation"),
    "MOCK-004": ("high", "correctness", "swallowed exception"),
    "MOCK-005": ("medium", "correctness", "loose null comparison"),
    "MOCK-006": ("medium", "performance", "deep-clone via JSON"),
    "MOCK-007": ("low", "style", "console.log left in"),
    "MOCK-008": ("low", "style", "unresolved marker"),
    "MOCK-INJ": ("critical", "security", "prompt-injection content"),
}

# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #
# MOCK-002: the regex is transcribed verbatim from the brief.
_CREDENTIAL_RE = re.compile(
    r"(api[_-]?key|secret|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]",
    re.IGNORECASE,
)

# MOCK-003 support: string literals, SQL keywords, and concatenation evidence.
_STRING_LITERAL_RE = re.compile(
    r"\"(?:[^\"\\]|\\.)*\""     # "double quoted"
    r"|'(?:[^'\\]|\\.)*'"       # 'single quoted'
    r"|`(?:[^`\\]|\\.)*`"       # `backtick`
)
_SQL_KEYWORD_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
_CONCAT_RE = re.compile(r"[\"'`]\s*\+|\+\s*[\"'`]")

# MOCK-005: exclude the strict forms.
_LOOSE_NULL_RE = re.compile(r"(?<![=!])[=!]=\s*null\b")

# MOCK-INJ: three phrases, case-insensitive.
_INJECTION_RE = re.compile(
    r"ignore previous instructions|disregard all prior|you are now",
    re.IGNORECASE,
)

# MOCK-004: "catch" with an optional binding; the brace may sit on a later line.
_CATCH_RE = re.compile(r"\bcatch\b\s*(?:\([^)]*\))?")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def _finding(rule_id: str, path: str, line: int, evidence: str) -> Finding:
    severity, category, title = RULE_META[rule_id]
    return Finding(
        rule_id=rule_id,
        path=path,
        line=line,
        severity=severity,
        category=category,
        title=title,
        evidence=evidence,
    )


# --------------------------------------------------------------------------- #
# Single-line rules
# --------------------------------------------------------------------------- #
def _sql_concatenation(text: str) -> bool:
    """A SQL keyword inside a string literal that is concatenated with "+"."""
    if not _CONCAT_RE.search(text):
        return False
    for literal in _STRING_LITERAL_RE.finditer(text):
        if literal.group(0).startswith("`"):
            # Template literal: interpolation, not "+" concatenation.
            continue
        if _SQL_KEYWORD_RE.search(literal.group(0)):
            return True
    return False


def scan_line(path: str, line_no: int, text: str) -> list[Finding]:
    """Every rule that needs only the one added line."""
    out: list[Finding] = []

    if "eval(" in text:
        out.append(_finding("MOCK-001", path, line_no, text))
    if _CREDENTIAL_RE.search(text):
        out.append(_finding("MOCK-002", path, line_no, text))
    if _sql_concatenation(text):
        out.append(_finding("MOCK-003", path, line_no, text))
    if _LOOSE_NULL_RE.search(text):
        out.append(_finding("MOCK-005", path, line_no, text))
    if "JSON.parse(JSON.stringify(" in text:
        out.append(_finding("MOCK-006", path, line_no, text))
    if "console.log(" in text:
        out.append(_finding("MOCK-007", path, line_no, text))
    if "TODO" in text or "FIXME" in text:
        out.append(_finding("MOCK-008", path, line_no, text))
    if _INJECTION_RE.search(text):
        # Reported as a finding and treated as inert text - it never reaches an
        # instruction position anywhere in this service.
        out.append(_finding("MOCK-INJ", path, line_no, text))

    return out


# --------------------------------------------------------------------------- #
# MOCK-004: empty catch block, possibly spanning lines
# --------------------------------------------------------------------------- #
def _body_is_empty(body: str) -> bool:
    body = _BLOCK_COMMENT_RE.sub("", body)
    body = _LINE_COMMENT_RE.sub("", body)
    return body.strip() == ""


def _scan_empty_catches(path: str, hunk: Hunk) -> list[Finding]:
    """Report the "catch" line when its block body holds nothing but blanks.

    Only lines *within a single hunk* are contiguous in the new file, so the
    walk never crosses a hunk boundary. If the closing brace is not visible in
    the hunk the rule stays silent - a false positive costs more than a miss.
    """
    out: list[Finding] = []
    lines = hunk.lines

    for idx, entry in enumerate(lines):
        match = _CATCH_RE.search(entry.text)
        if not match or not entry.added:
            continue

        # Find the opening brace: same line, or the next line(s) if only
        # whitespace intervenes.
        row, col = idx, match.end()
        found_brace = False
        while row < len(lines) and not found_brace:
            text = lines[row].text
            while col < len(text):
                char = text[col]
                if char == "{":
                    found_brace = True
                    break
                if not char.isspace():
                    break
                col += 1
            if found_brace:
                break
            if col < len(text):
                break          # hit real code before the brace: not a block
            row, col = row + 1, 0
        if not found_brace:
            continue

        # Walk the block, collecting its body until the brace depth returns to 0.
        depth = 0
        body: list[str] = []
        closed = False
        while row < len(lines) and not closed:
            text = lines[row].text
            while col < len(text):
                char = text[col]
                if char == "{":
                    depth += 1
                    if depth > 1:
                        body.append(char)
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        closed = True
                        break
                    body.append(char)
                elif depth >= 1:
                    body.append(char)
                col += 1
            if closed:
                break
            body.append("\n")
            row, col = row + 1, 0

        if closed and _body_is_empty("".join(body)):
            out.append(_finding("MOCK-004", path, entry.number, entry.text))

    return out


# --------------------------------------------------------------------------- #
# File / chunk entry points
# --------------------------------------------------------------------------- #
def scan_file(file: DiffFile) -> list[Finding]:
    if file.binary:
        return []
    findings: list[Finding] = []
    for hunk in file.hunks:
        for line in hunk.lines:
            if line.added:
                findings.extend(scan_line(file.path, line.number, line.text))
        findings.extend(_scan_empty_catches(file.path, hunk))
    return findings


def scan_chunk(chunk: DiffChunk) -> list[Finding]:
    findings: list[Finding] = []
    for file in chunk.files:
        findings.extend(scan_file(file))
    return findings


# Rule matching is CPU work (regex over up to 1 MiB of text). Running it on the
# event loop would stall SSE heartbeats and /health under load, so it is
# offloaded to a small thread pool sized to the declared job concurrency.
_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="mock-scan"
)


class MockProvider(Provider):
    name = "mock"

    async def review(self, chunk: DiffChunk) -> list[Finding]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, scan_chunk, chunk)
