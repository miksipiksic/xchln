from __future__ import annotations

import pytest

from app.diff.parser import DiffParseError, parse_diff
from tests import diffs


def added(file):
    return [(line.number, line.text) for line in file.added_lines()]


def test_line_numbers_follow_the_new_file():
    (file,) = parse_diff(diffs.ALL_RULES)
    assert file.path == "src/db.ts"
    numbers = [number for number, _ in added(file)]
    # Hunk starts at new-file line 10; one context line precedes the additions,
    # and the removed line must not advance the counter.
    assert numbers == list(range(11, 24))


def test_raw_text_round_trips_exactly():
    """Byte accounting for the chunker depends on this."""
    for source in (
        diffs.ALL_RULES,
        diffs.TWO_FILES_UNSORTED,
        diffs.NEW_FILE,
        diffs.PLAIN_DIFF_NO_GIT_HEADER,
        diffs.CRLF_DIFF,
        diffs.NO_NEWLINE_AT_EOF,
        diffs.BINARY,
    ):
        assert "".join(f.raw for f in parse_diff(source)) == source


def test_multiple_files_are_separated():
    files = parse_diff(diffs.TWO_FILES_UNSORTED)
    assert [f.path for f in files] == ["zeta/last.js", "alpha/first.js"]


def test_new_file_uses_the_plus_path():
    (file,) = parse_diff(diffs.NEW_FILE)
    assert file.path == "added.js"
    assert [n for n, _ in added(file)] == [1, 2]


def test_deleted_file_falls_back_to_the_minus_path():
    (file,) = parse_diff(diffs.DELETED_FILE)
    assert file.path == "gone.js"
    assert added(file) == []


def test_removed_line_starting_with_dashes_is_not_a_file_header():
    files = parse_diff(diffs.TRICKY_REMOVAL)
    assert len(files) == 1
    assert files[0].path == "sql.sql"
    assert [text for _, text in added(files[0])] == ['console.log("replacement");']


def test_plain_diff_strips_timestamp_and_prefix():
    (file,) = parse_diff(diffs.PLAIN_DIFF_NO_GIT_HEADER)
    assert file.path == "new/app.js"
    assert added(file) == [(2, 'eval("plain");')]


def test_quoted_paths_are_unquoted():
    (file,) = parse_diff(diffs.QUOTED_PATH)
    assert file.path == "src/my file.js"


def test_crlf_is_stripped_from_line_text():
    (file,) = parse_diff(diffs.CRLF_DIFF)
    assert added(file) == [(2, 'console.log("crlf");')]


def test_no_newline_marker_is_ignored():
    (file,) = parse_diff(diffs.NO_NEWLINE_AT_EOF)
    assert added(file) == [(2, 'console.log("no newline");')]


def test_binary_patch_parses_but_adds_nothing():
    (file,) = parse_diff(diffs.BINARY)
    assert file.binary is True
    assert added(file) == []


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "   \n\t ",
        "just some prose, not a diff at all",
        "@@ -1,1 +1,1 @@\n+orphan hunk with no file header\n",
        "--- a/only-headers.js\n+++ b/only-headers.js\n",
    ],
)
def test_unparseable_payloads_are_rejected(payload):
    with pytest.raises(DiffParseError):
        parse_diff(payload)


def test_hunk_header_without_counts_defaults_to_one():
    diff = "--- a/x.js\n+++ b/x.js\n@@ -5 +5 @@\n+eval(\"single\");\n"
    (file,) = parse_diff(diff)
    assert added(file) == [(5, 'eval("single");')]
