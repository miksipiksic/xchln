"""Internal domain models and their wire serialisation.

Plain dataclasses rather than pydantic models: the wire shapes are fixed by the
contract and hand-written ``to_dict`` keeps the emitted JSON exactly - and only -
what the contract documents.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal

Severity = Literal["critical", "high", "medium", "low"]
Category = Literal["security", "correctness", "performance", "style"]
JobStatus = Literal["queued", "running", "done", "failed"]

SEVERITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
CATEGORIES: tuple[str, ...] = ("security", "correctness", "performance", "style")


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    path: str
    line: int
    severity: str
    category: str
    title: str
    evidence: str

    @property
    def id(self) -> str:
        return f"{self.rule_id}:{self.path}:{self.line}"

    @property
    def sort_key(self) -> tuple[str, int, str]:
        """Ordering is by path (lexicographic), then line, then ruleId."""
        return (self.path, self.line, self.rule_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ruleId": self.rule_id,
            "path": self.path,
            "line": self.line,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "evidence": self.evidence,
        }


def normalize(findings: list[Finding], max_findings: int | None = None) -> list[Finding]:
    """Dedup by id, order by (path, line, ruleId), then truncate.

    The single chokepoint for ordering. Both the GET result and the SSE emitter
    call this, so the two representations cannot drift.
    """
    seen: set[str] = set()
    unique: list[Finding] = []
    for finding in sorted(findings, key=lambda f: f.sort_key):
        if finding.id in seen:
            continue
        seen.add(finding.id)
        unique.append(finding)
    if max_findings is not None and max_findings >= 0:
        return unique[:max_findings]
    return unique


# --------------------------------------------------------------------------- #
# Parsed diff
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class NewLine:
    """A line as it appears in the *new* file: context lines and added lines."""

    number: int
    text: str
    added: bool


@dataclass(slots=True)
class Hunk:
    new_start: int
    lines: list[NewLine] = field(default_factory=list)

    def added(self) -> Iterator[NewLine]:
        return (line for line in self.lines if line.added)


@dataclass(slots=True)
class DiffFile:
    path: str
    raw: str          # exact original text of this file's block, for chunking
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False

    @property
    def byte_size(self) -> int:
        return len(self.raw.encode("utf-8"))

    def added_lines(self) -> Iterator[NewLine]:
        for hunk in self.hunks:
            yield from hunk.added()


@dataclass(slots=True)
class DiffChunk:
    index: int
    files: list[DiffFile]

    @property
    def byte_size(self) -> int:
        return sum(f.byte_size for f in self.files)


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Usage:
    input_bytes: int
    chunks: int
    cache_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        # Exactly the three documented keys - no extras, in case a probe
        # compares the object strictly.
        return {
            "inputBytes": self.input_bytes,
            "chunks": self.chunks,
            "cacheHit": self.cache_hit,
        }


@dataclass(slots=True)
class Event:
    seq: int
    type: str                     # "status" | "finding" | "done"
    data: dict[str, Any]

    @property
    def terminal(self) -> bool:
        return self.type == "done" or (
            self.type == "status" and self.data.get("status") == "failed"
        )


@dataclass(slots=True)
class Job:
    id: str
    provider: str
    max_findings: int
    content_key: str
    status: JobStatus = "queued"
    findings: list[Finding] = field(default_factory=list)
    usage: Usage = field(default_factory=lambda: Usage(0, 0, False))
    error: dict[str, str] | None = None
    created_at: float = 0.0

    # event log + live subscribers (see app/events/bus.py)
    events: list[Event] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    finished: asyncio.Event | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in ("done", "failed")

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"jobId": self.id, "status": self.status}
        if self.status == "done":
            body["findings"] = [f.to_dict() for f in self.findings]
        body["usage"] = self.usage.to_dict()
        if self.status == "failed" and self.error:
            # The contract demands "a clear error" but never says where; this
            # mirrors the error envelope so clients parse one shape everywhere.
            body["error"] = self.error
        return body
