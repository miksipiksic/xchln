"""The provider interface.

Both providers see exactly the same input - one chunk of a parsed diff - and
return findings without ids or ordering applied. Everything downstream (dedup,
ordering, truncation, streaming, caching) is provider-agnostic, which is the
point: the mock provider proves the pipeline, the llm provider swaps only the
analysis step.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import DiffChunk, Finding


class ProviderError(RuntimeError):
    """A provider could not complete. The job fails cleanly; nothing crashes."""

    def __init__(self, message: str, *, code: str = "provider_error") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Provider(ABC):
    name: str

    @abstractmethod
    async def review(self, chunk: DiffChunk) -> list[Finding]:
        """Return findings for one chunk. Order does not matter here."""
        raise NotImplementedError
