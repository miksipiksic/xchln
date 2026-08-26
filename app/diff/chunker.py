"""Split a parsed diff into chunks of at most CHUNK_BYTES, on file boundaries.

One file's diff never spans two chunks; a single file larger than the limit
becomes a chunk of its own. Because every review rule is scoped to a single
file, chunked and unchunked scans are identical *by construction* rather than
by careful merging - which is the property tests/test_chunking.py asserts.
"""

from __future__ import annotations

from app.config import CHUNK_BYTES
from app.models import DiffChunk, DiffFile


def chunk_files(files: list[DiffFile], chunk_bytes: int = CHUNK_BYTES) -> list[DiffChunk]:
    chunks: list[DiffChunk] = []
    current: list[DiffFile] = []
    current_size = 0

    for file in files:
        size = file.byte_size
        if current and current_size + size > chunk_bytes:
            chunks.append(DiffChunk(index=len(chunks), files=current))
            current = []
            current_size = 0
        current.append(file)
        current_size += size
        # An oversized single file is its own chunk: close it out immediately
        # so it never picks up a neighbour.
        if current_size >= chunk_bytes:
            chunks.append(DiffChunk(index=len(chunks), files=current))
            current = []
            current_size = 0

    if current:
        chunks.append(DiffChunk(index=len(chunks), files=current))

    if not chunks:
        # A parsed, non-empty diff always reports at least one chunk.
        chunks.append(DiffChunk(index=0, files=[]))
    return chunks
