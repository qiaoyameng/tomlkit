import pytest

import tomlkit

from tomlkit import dumps
from tomlkit import parse
from tomlkit import parse_with_recovery
from tomlkit.exceptions import ErrorFragmentEditError
from tomlkit.exceptions import ParseError
from tomlkit.items import ErrorFragment
from tomlkit.items import Table


# Two independent errors (a missing array bracket and an unclosed multi-line
# string) sandwiching a valid table.
BROKEN = """\
# top comment
broken_array = [1, 2

[good]
x = 1
name = "hi"

[bad]
description = \"\"\"unclosed
[fake]
trailing = 1
"""


def fragments(container):
    return [v for _, v in container.body if isinstance(v, ErrorFragment)]


def test_parse_with_recovery_recovers_valid_parts_around_errors():
    doc, diagnostics = parse_with_recovery(BROKEN)

    assert len(diagnostics) == 2
    # The missing array bracket has a well-defined boundary ...
    assert not diagnostics[0].incomplete
    # ... the unclosed multi-line string swallows the rest of the input, so
    # its diagnostics are explicitly marked as incomplete.
    assert diagnostics[1].incomplete

    # Valid nodes keep the existing container interface.
    assert doc["good"]["x"] == 1
    assert doc["good"]["name"] == "hi"
    assert isinstance(doc["bad"], Table)

    # The pseudo table header inside the unclosed string is not mistaken
    # for a real table.
    assert "fake" not in doc


def test_parse_with_recovery_keeps_original_text_and_spans():
    doc, diagnostics = parse_with_recovery(BROKEN)

    (fragment,) = fragments(doc)
    assert fragment.raw == "broken_array = [1, 2\n\n"
    assert BROKEN[fragment.start : fragment.end] == fragment.raw
    assert fragment.span == (diagnostics[0].start, diagnostics[0].end)
    assert fragment.claimed_keys == frozenset({"broken_array"})

    (inner,) = fragments(doc["bad"].value)
    assert inner.raw == 'description = """unclosed\n[fake]\ntrailing = 1\n'
    assert BROKEN[inner.start : inner.end] == inner.raw
    assert inner.claimed_keys == frozenset({"description"})

    # The document round-trips byte-for-byte.
    assert dumps(doc) == BROKEN


def test_parse_with_recovery_edits_preserve_error_fragments():
    doc, _ = parse_with_recovery(BROKEN)

    doc["good"]["x"] = 42

    out = dumps(doc)
    assert "x = 42" in out
    # All error fragments are preserved verbatim.
    assert "broken_array = [1, 2\n" in out
    assert 'description = """unclosed\n[fake]\ntrailing = 1\n' in out

    # Fixing the fragments yields a document that strict-parses cleanly.
    fixed = out.replace("broken_array = [1, 2\n", "broken_array = [1, 2]\n")
    fixed = fixed.replace(
        'description = """unclosed\n', 'description = """unclosed"""\n'
    )
    doc2 = parse(fixed)
    assert doc2["broken_array"] == [1, 2]
    assert doc2["good"]["x"] == 42
    assert doc2["good"]["name"] == "hi"
    assert doc2["bad"]["description"] == "unclosed"
    assert doc2["fake"]["trailing"] == 1


def test_parse_with_recovery_rejects_edits_inside_error_fragments():
    doc, _ = parse_with_recovery(BROKEN)

    with pytest.raises(ErrorFragmentEditError):
        doc["broken_array"] = [1, 2]

    with pytest.raises(ErrorFragmentEditError):
        doc["bad"]["description"] = "fixed"

    # Editing valid keys and adding new ones still works.
    doc["good"]["x"] = 2
    doc["good"]["new_key"] = "ok"
    assert doc["good"]["new_key"] == "ok"


def test_parse_remains_strict_by_default():
    with pytest.raises(ParseError):
        parse(BROKEN)


def test_recovery_understands_comments_quotes_and_arrays():
    # A "[" inside a comment must not disturb the boundary scan, and a
    # closed multi-line string containing a pseudo header parses normally.
    doc, diagnostics = parse_with_recovery(
        "x = 1 2  # [not a bracket\n"
        's = """\n[fake]\n"""\n'
        "y = 2\n"
    )

    assert len(diagnostics) == 1
    assert not diagnostics[0].incomplete
    assert doc["s"] == "[fake]\n"
    assert doc["y"] == 2
    assert "fake" not in doc


def test_recovery_marks_unclosed_array_at_eof_as_incomplete():
    doc, diagnostics = parse_with_recovery("a = 1\narr = [1, 2\n")

    assert doc["a"] == 1
    assert len(diagnostics) == 1
    assert diagnostics[0].incomplete
    (fragment,) = fragments(doc)
    assert fragment.raw == "arr = [1, 2\n"


def test_recovery_resumes_at_table_after_missing_array_bracket():
    doc, diagnostics = parse_with_recovery("[t]\narr = [1, 2\n\n[other]\nx = 1\n")

    assert len(diagnostics) == 1
    assert not diagnostics[0].incomplete
    assert doc["other"]["x"] == 1
    (fragment,) = fragments(doc["t"].value)
    assert fragment.raw == "arr = [1, 2\n\n"


def test_recovery_handles_broken_table_header():
    doc, diagnostics = parse_with_recovery("[ok]\nx = 1\n[bad header\ny = 2\n[good]\nz = 3\n")

    assert len(diagnostics) == 1
    assert doc["ok"]["x"] == 1
    assert doc["good"]["z"] == 3
    # The broken header appeared inside [ok]'s body, so its fragment is
    # stored in that table's container.
    (fragment,) = fragments(doc["ok"].value)
    assert fragment.raw == "[bad header\n"
    assert "bad" in fragment.claimed_keys
    with pytest.raises(ErrorFragmentEditError):
        doc["ok"]["bad"] = {}
    assert dumps(doc) == "[ok]\nx = 1\n[bad header\ny = 2\n[good]\nz = 3\n"


def test_recovery_rejects_edits_for_dotted_key_claims():
    doc, diagnostics = parse_with_recovery("a.b = \nc = 1\n")

    assert len(diagnostics) == 1
    (fragment,) = fragments(doc)
    assert fragment.claimed_keys == frozenset({"a", "a.b"})
    with pytest.raises(ErrorFragmentEditError):
        doc["a"] = {}
    with pytest.raises(ErrorFragmentEditError):
        doc["a.b"] = 1

    doc["c"] = 2
    assert doc["c"] == 2


def test_recovery_inside_array_of_tables():
    src = "[[fruit]]\nname = 'apple'\n[[fruit]]\nname = \n[[fruit]]\nname = 'cherry'\n"
    doc, diagnostics = parse_with_recovery(src)

    assert len(diagnostics) == 1
    assert [t.get("name") for t in doc["fruit"]] == ["apple", None, "cherry"]
    assert dumps(doc) == src
