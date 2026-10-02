from __future__ import annotations

import pytest

import tomlkit

from tomlkit import parse_recovery
from tomlkit.exceptions import ParseError
from tomlkit.exceptions import UnparsedRegionEditError
from tomlkit.items import UnparsedNode
from tomlkit.recovery import Diagnostic
from tomlkit.recovery import RecoveryResult
from tomlkit.recovery import RecoveryParser
from tomlkit.recovery import scan_records


def test_strict_parse_still_raises_on_first_error() -> None:
    content = "good = 1\nbad = = 2\n"
    with pytest.raises(ParseError):
        tomlkit.parse(content)


def test_recovery_returns_result_type_with_document_and_diagnostics() -> None:
    content = "good = 1\nbad = = 2\n"
    result = parse_recovery(content)
    assert isinstance(result, RecoveryResult)
    assert result.complete is True
    assert len(result.diagnostics) == 1
    diag = result.diagnostics[0]
    assert isinstance(diag, Diagnostic)
    assert diag.line == 2
    assert not diag.incomplete


def test_valid_document_has_no_diagnostics_and_round_trips() -> None:
    content = (
        "# comment\n"
        "title = \"demo\"\n"
        "\n"
        "[owner]\n"
        "name = \"Tom\"\n"
        "ports = [8001, 8002]\n"
        "\n"
        "[[products]]\n"
        "name = \"Hammer\"\n"
        "\n"
        "[[products]]\n"
        "name = \"Nail\"\n"
    )
    result = parse_recovery(content)
    assert result.ok
    assert tomlkit.dumps(result.document) == content
    assert result.document["owner"]["ports"] == [8001, 8002]


def test_two_independent_errors_sandwiching_a_legal_table() -> None:
    content = (
        "root_key = 1\n"
        "@broken top = 1\n"
        "[good]\n"
        "a = 1\n"
        "[other]\n"
        "b = 2\n"
        "bad = value_no_eq\n"
        "[last]\n"
        "c = 3\n"
    )
    result = parse_recovery(content)
    assert result.complete is True
    assert len(result.diagnostics) == 2

    # Legal nodes keep the ordinary container interface.
    assert result.document["good"]["a"] == 1
    assert result.document["other"]["b"] == 2
    assert result.document["last"]["c"] == 3

    # dumps must preserve both broken fragments verbatim.
    assert tomlkit.dumps(result.document) == content

    # Editing a legal field keeps every error fragment untouched.
    result.document["good"]["a"] = 42
    expected = content.replace("a = 1", "a = 42")
    assert tomlkit.dumps(result.document) == expected

    # Once the fragments are fixed, a strict parse yields the expected values.
    fixed = (
        tomlkit.dumps(result.document)
        .replace("@broken top = 1", "broken_top = 1")
        .replace("bad = value_no_eq", 'bad = "fixed"')
    )
    strict = tomlkit.parse(fixed)
    assert strict["root_key"] == 1
    assert strict["broken_top"] == 1
    assert strict["good"]["a"] == 42
    assert strict["other"]["b"] == 2
    assert strict["other"]["bad"] == "fixed"
    assert strict["last"]["c"] == 3


def test_unparsed_node_is_opaque_and_preserves_span_and_text() -> None:
    content = "bad = 42abc\n"
    result = parse_recovery(content)
    node = result.document["bad"]
    assert isinstance(node, UnparsedNode)
    start, end = node.span
    assert content[start:end] == node.raw
    assert node.raw == "42abc\n"

    with pytest.raises(UnparsedRegionEditError):
        result.document["bad"] = 1
    with pytest.raises(UnparsedRegionEditError):
        del result.document["bad"]
    with pytest.raises(UnparsedRegionEditError):
        node.unwrap()
    with pytest.raises(UnparsedRegionEditError):
        node["whatever"]

    # The rejected edits did not mutate or drop the fragment.
    assert tomlkit.dumps(result.document) == content


def test_missing_array_bracket_becomes_one_unparsed_region() -> None:
    content = (
        "[section]\n"
        "ok = 1\n"
        "nums = [1, 2,\n"
        "       3, 4\n"
        "[other]\n"
        "x = 5\n"
    )
    result = parse_recovery(content)
    assert result.complete is False
    assert any(d.incomplete for d in result.diagnostics)

    node = result.document["section"]["nums"]
    assert isinstance(node, UnparsedNode)
    start, end = node.span
    assert content[start:end] == node.raw
    assert "[other]" in node.raw and "x = 5" in node.raw

    # The legal field before the region stays editable, raw text is preserved.
    result.document["section"]["ok"] = 7
    edited = tomlkit.dumps(result.document)
    assert edited == content.replace("ok = 1", "ok = 7")

    # The swallowed table cannot be reached through an edit path.
    with pytest.raises(KeyError):
        result.document["other"]

    # Close the bracket as text and re-run the strict parser.
    fixed = edited.replace(
        "nums = [1, 2,\n       3, 4\n[other]",
        "nums = [1, 2,\n       3, 4]\n[other]",
    )
    strict = tomlkit.parse(fixed)
    assert list(strict["section"]["nums"]) == [1, 2, 3, 4]
    assert strict["section"]["ok"] == 7
    assert strict["other"]["x"] == 5


def test_unterminated_multiline_string_does_not_create_a_fake_table() -> None:
    content = (
        "[a]\n"
        "v = 1\n"
        'text = """hello\n'
        "# not a comment\n"
        "[fake.header]\n"
        "still inside\n"
        "[b]\n"
        "w = 2\n"
    )
    result = parse_recovery(content)
    assert result.complete is False
    assert any(d.incomplete for d in result.diagnostics)

    # The pseudo-headers inside the open string must not have been parsed.
    assert "fake.header" not in result.document
    assert "b" not in result.document

    node = result.document["a"]["text"]
    assert isinstance(node, UnparsedNode)
    assert "[fake.header]" in node.raw
    assert "[b]" in node.raw

    # Legal data survives and is editable; raw fragment is byte-identical.
    result.document["a"]["v"] = 99
    edited = tomlkit.dumps(result.document)
    assert edited == content.replace("v = 1", "v = 99")

    fixed = edited.replace("still inside\n[b]", 'still inside"""\n[b]')
    strict = tomlkit.parse(fixed)
    assert strict["a"]["v"] == 99
    assert strict["a"]["text"] == "hello\n# not a comment\n[fake.header]\nstill inside"
    assert strict["b"]["w"] == 2


def test_scanner_understands_strings_comments_and_brackets() -> None:
    source = (
        'a = "[x]"\n'
        "# [comment]\n"
        "b = [\n"
        "  1,\n"
        "]\n"
        'c = """multi\n'
        "[fake]\n"
        'tail"""\n'
        "d = 1\n"
    )
    records = scan_records(source)
    # Every terminated record ends on a newline boundary; the multiline string
    # is contained inside one record even though it contains a "[fake]" line.
    joined = "".join(source[r.start : r.end] for r in records)
    assert joined == source
    assert all(source[r.end - 1] == "\n" for r in records if r.terminated)


def test_scanner_marks_open_multiline_string_as_unterminated() -> None:
    source = 'x = """open\n[fake]\nmore\n'
    records = scan_records(source)
    assert len(records) == 1
    assert records[0].terminated is False


def test_scanner_marks_open_brackets_as_unterminated() -> None:
    source = "x = [1,\n2\n[fake]\n"
    records = scan_records(source)
    assert len(records) == 1
    assert records[0].terminated is False


def test_recovery_after_bad_table_header_keeps_later_tables() -> None:
    content = "[bad..name]\nx = 1\n[good]\ny = 2\n"
    result = parse_recovery(content)
    assert result.complete is True
    assert len(result.diagnostics) == 1
    assert result.document["good"]["y"] == 2
    assert tomlkit.dumps(result.document) == content


def test_single_line_unterminated_string_resynchronizes_on_next_line() -> None:
    content = 'x = "open\ny = 2\n'
    result = parse_recovery(content)
    assert result.complete is True
    assert result.document["y"] == 2
    assert tomlkit.dumps(result.document) == content


def test_empty_input_produces_empty_document() -> None:
    result = parse_recovery("")
    assert result.ok and result.complete
    assert tomlkit.dumps(result.document) == ""


def test_recovery_parser_is_a_parser_with_strict_parse() -> None:
    parser = RecoveryParser("a = 1\n")
    assert isinstance(parser.parse(), tomlkit.TOMLDocument)
