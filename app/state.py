"""Process-wide service state, created once per app and hung off app.state."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.jobs.cache import ContentCache, IdempotencyIndex
from app.jobs.queue import JobRunner
from app.jobs.store import JobStore


@dataclass
class AppState:
    store: JobStore = field(default_factory=JobStore)
    cache: ContentCache = field(default_factory=ContentCache)
    idempotency: IdempotencyIndex = field(default_factory=IdempotencyIndex)
    runner: JobRunner = field(init=False)

    def __post_init__(self) -> None:
        self.runner = JobRunner(self.cache)
