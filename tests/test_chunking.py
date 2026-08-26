from __future__ import annotations

from app.config import CHUNK_BYTES
from app.diff.chunker import chunk_files
from app.diff.parser import parse_diff
from app.models import DiffChunk, normalize
from app.providers.mock import scan_chunk
from tests import diffs
from tests.conftest import AUTH, review


def test_small_diff_is_a_single_chunk():
    files = parse_diff(diffs.ALL_RULES)
    assert len(chunk_files(files)) == 1


def test_no_chunk_exceeds_the_limit_unless_one_file_does():
    files = parse_diff(diffs.large_multifile_diff())
    chunks = chunk_files(files)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.byte_size <= CHUNK_BYTES or len(chunk.files) == 1


def test_a_file_never_spans_two_chunks():
    files = parse_diff(diffs.large_multifile_diff())
    chunks = chunk_files(files)
    seen = [f.path for chunk in chunks for f in chunk.files]
    assert seen == [f.path for f in files]      # every file exactly once, in order
    assert len(seen) == len(set(seen))


def test_oversized_single_file_becomes_its_own_chunk():
    huge = diffs.large_multifile_diff(file_count=1, lines_per_file=1200)
    files = parse_diff(huge)
    assert files[0].byte_size > CHUNK_BYTES
    chunks = chunk_files(files)
    assert len(chunks) == 1 and len(chunks[0].files) == 1


def test_byte_accounting_covers_the_whole_payload():
    source = diffs.large_multifile_diff()
    files = parse_diff(source)
    chunks = chunk_files(files)
    total = sum(f.byte_size for chunk in chunks for f in chunk.files)
    assert total == len(source.encode("utf-8"))


def test_chunked_scan_equals_unchunked_scan():
    """The property the whole chunking design exists to guarantee."""
    files = parse_diff(diffs.large_multifile_diff())

    chunked = normalize([f for c in chunk_files(files) for f in scan_chunk(c)])
    single = normalize(scan_chunk(DiffChunk(index=0, files=files)))

    assert len(chunk_files(files)) > 1          # the test would be vacuous otherwise
    assert [f.id for f in chunked] == [f.id for f in single]
    assert [f.to_dict() for f in chunked] == [f.to_dict() for f in single]


async def test_usage_reports_chunk_count_over_the_wire(client):
    body = await review(client, diffs.large_multifile_diff())
    assert body["status"] == "done"
    assert body["usage"]["chunks"] > 1
    assert body["usage"]["inputBytes"] > CHUNK_BYTES


async def test_findings_stay_ordered_across_chunks(client):
    body = await review(client, diffs.large_multifile_diff())
    keys = [(f["path"], f["line"], f["ruleId"]) for f in body["findings"]]
    assert keys == sorted(keys)
    ids = [f["id"] for f in body["findings"]]
    assert len(ids) == len(set(ids))


async def test_ordering_ignores_file_position_in_the_payload(client):
    body = await review(client, diffs.TWO_FILES_UNSORTED)
    # alpha/first.js appears second in the diff but must be reported first.
    assert [f["path"] for f in body["findings"]] == ["alpha/first.js", "zeta/last.js"]


async def test_max_findings_truncates_but_usage_reflects_the_full_scan(client):
    full = await review(client, diffs.large_multifile_diff())
    capped = await review(
        client, diffs.large_multifile_diff(), options={"maxFindings": 5}
    )

    assert len(capped["findings"]) == 5
    assert capped["findings"] == full["findings"][:5]
    # usage describes the scan, not the truncated view of it
    assert capped["usage"]["chunks"] == full["usage"]["chunks"]
    assert capped["usage"]["inputBytes"] == full["usage"]["inputBytes"]
