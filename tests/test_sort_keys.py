from textwrap import dedent

import pytest

import tomlkit

from tomlkit import parse
from tomlkit.exceptions import KeySortError


def dumps(doc) -> str:
    return tomlkit.dumps(doc)


def assert_preserves_values(original: str, sorted_content: str) -> None:
    """The sorted document re-parses to exactly the same values."""
    assert parse(sorted_content).unwrap() == parse(original).unwrap()


def assert_idempotent(doc, **kwargs) -> None:
    """Running the same sort again does not change any byte."""
    once = dumps(doc)
    doc.sort_keys(**kwargs)
    assert dumps(doc) == once
    # and neither does re-parsing the result and sorting that
    reparsed = parse(once)
    reparsed.sort_keys(**kwargs)
    assert dumps(reparsed) == once


def test_sort_keys_keeps_document_header_comment_on_top() -> None:
    content = dedent(
        """\
        # Package configuration
        # Do not edit by hand

        zebra = 1
        apple = 2
        """
    )
    doc = parse(content)
    doc.sort_keys()

    expected = dedent(
        """\
        # Package configuration
        # Do not edit by hand

        apple = 2
        zebra = 1
        """
    )
    assert dumps(doc) == expected
    assert_preserves_values(content, dumps(doc))
    assert_idempotent(doc)


def test_sort_keys_moves_attached_comments_and_whitespace_with_key() -> None:
    content = dedent(
        """\
        [tool]
        # about zebra
        zebra = 1

        # about apple
        apple = 2
        """
    )
    doc = parse(content)
    doc["tool"].sort_keys()

    expected = dedent(
        """\
        [tool]
        # about apple
        apple = 2

        # about zebra
        zebra = 1
        """
    )
    assert dumps(doc) == expected
    assert_preserves_values(content, dumps(doc))
    assert_idempotent(doc)


def test_sort_keys_with_custom_order_keeps_remaining_keys_relative_order() -> None:
    content = dedent(
        """\
        delta = 4
        bravo = 2
        alpha = 1
        charlie = 3
        """
    )
    doc = parse(content)
    doc.sort_keys(["charlie", "alpha"])

    expected = dedent(
        """\
        charlie = 3
        alpha = 1
        delta = 4
        bravo = 2
        """
    )
    assert dumps(doc) == expected
    assert_preserves_values(content, dumps(doc))
    assert_idempotent(doc, key_order=["charlie", "alpha"])


def test_sort_keys_ignores_unknown_keys_in_custom_order() -> None:
    content = "b = 2\na = 1\n"
    doc = parse(content)
    doc.sort_keys(["missing", "b", "a"])

    assert dumps(doc) == "b = 2\na = 1\n"
    assert_preserves_values(content, dumps(doc))


def test_sort_keys_accepts_key_objects_in_custom_order() -> None:
    content = "b = 2\na = 1\n"
    doc = parse(content)
    doc.sort_keys([tomlkit.key("b"), tomlkit.key("a")])

    assert dumps(doc) == "b = 2\na = 1\n"


def test_sort_keys_with_quoted_keys() -> None:
    content = dedent(
        """\
        "z b" = 1
        "a b" = 2
        """
    )
    doc = parse(content)
    doc.sort_keys()

    expected = dedent(
        """\
        "a b" = 2
        "z b" = 1
        """
    )
    assert dumps(doc) == expected
    assert_preserves_values(content, dumps(doc))
    assert_idempotent(doc)


def test_sort_keys_out_of_order_table_keeps_fragments_in_order() -> None:
    content = dedent(
        """\
        [a]
        x = 1
        [b]
        y = 2
        [a.c]
        z = 3
        """
    )
    doc = parse(content)
    doc.sort_keys(["b", "a"])

    expected = dedent(
        """\
        [b]
        y = 2
        [a]
        x = 1
        [a.c]
        z = 3
        """
    )
    assert dumps(doc) == expected
    assert_preserves_values(content, dumps(doc))
    assert_idempotent(doc, key_order=["b", "a"])


def test_sort_keys_out_of_order_table_alphabetical() -> None:
    content = dedent(
        """\
        [a]
        x = 1
        [b]
        y = 2
        [a.c]
        z = 3
        """
    )
    doc = parse(content)
    doc.sort_keys()

    expected = dedent(
        """\
        [a]
        x = 1
        [a.c]
        z = 3
        [b]
        y = 2
        """
    )
    assert dumps(doc) == expected
    assert_preserves_values(content, dumps(doc))
    assert_idempotent(doc)


def test_sort_keys_multiple_arrays_of_tables_preserve_record_order() -> None:
    content = dedent(
        """\
        [[veggie]]
        name = "kale"

        [[fruit]]
        name = "apple"

        [[fruit]]
        name = "banana"
        """
    )
    doc = parse(content)
    doc.sort_keys()

    expected = dedent(
        """\
        [[fruit]]
        name = "apple"

        [[fruit]]
        name = "banana"

        [[veggie]]
        name = "kale"
        """
    )
    assert dumps(doc) == expected
    assert_preserves_values(content, dumps(doc))
    assert_idempotent(doc)


def test_sort_keys_array_of_tables_with_extending_sub_table() -> None:
    content = dedent(
        """\
        [[fruit]]
        name = "apple"
        [fruit.physical]
        color = "red"
        [[fruit]]
        name = "banana"
        [veggie]
        x = 1
        """
    )
    doc = parse(content)
    doc.sort_keys(["veggie", "fruit"])

    expected = dedent(
        """\
        [veggie]
        x = 1
        [[fruit]]
        name = "apple"
        [fruit.physical]
        color = "red"
        [[fruit]]
        name = "banana"
        """
    )
    assert dumps(doc) == expected
    assert_preserves_values(content, dumps(doc))
    assert_idempotent(doc, key_order=["veggie", "fruit"])


def test_sort_keys_refuses_to_move_plain_key_below_table_header() -> None:
    content = dedent(
        """\
        z = 1

        [a]
        x = 1
        """
    )
    doc = parse(content)
    with pytest.raises(KeySortError):
        doc.sort_keys(["a", "z"])

    # the document is left untouched
    assert dumps(doc) == content

    # an alphabetical sort is equally impossible here
    with pytest.raises(KeySortError):
        doc.sort_keys()
    assert dumps(doc) == content


def test_sort_keys_refuses_to_move_header_before_dotted_key() -> None:
    content = dedent(
        """\
        a.b = 1
        [m]
        q = 1
        [a.c]
        d = 2
        """
    )
    doc = parse(content)
    with pytest.raises(KeySortError):
        doc.sort_keys(["m", "a"])

    assert dumps(doc) == content


def test_sort_keys_dotted_key_and_scattered_table_sorts_legally() -> None:
    content = dedent(
        """\
        a.b = 1
        [m]
        q = 1
        [a.c]
        d = 2
        """
    )
    doc = parse(content)
    doc.sort_keys()

    expected = dedent(
        """\
        a.b = 1
        [a.c]
        d = 2
        [m]
        q = 1
        """
    )
    assert dumps(doc) == expected
    assert_preserves_values(content, dumps(doc))
    assert_idempotent(doc)


def test_sort_keys_recursive_sorts_sub_tables() -> None:
    content = dedent(
        """\
        [tool.b]
        z = 1
        y = 2

        [tool.a]
        w = 3
        v = 4
        """
    )
    doc = parse(content)
    doc.sort_keys(recursive=True)

    expected = dedent(
        """\
        [tool.a]
        v = 4
        w = 3

        [tool.b]
        y = 2
        z = 1
        """
    )
    assert dumps(doc) == expected
    assert_preserves_values(content, dumps(doc))
    assert_idempotent(doc, recursive=True)


def test_sort_keys_recursive_sorts_array_of_tables_elements() -> None:
    content = dedent(
        """\
        [[item]]
        b = 1
        a = 2
        """
    )
    doc = parse(content)
    doc.sort_keys(recursive=True)

    expected = dedent(
        """\
        [[item]]
        a = 2
        b = 1
        """
    )
    assert dumps(doc) == expected
    assert_preserves_values(content, dumps(doc))
    assert_idempotent(doc, recursive=True)


def test_sort_keys_non_recursive_leaves_sub_tables_alone() -> None:
    content = dedent(
        """\
        [[item]]
        b = 1
        a = 2
        """
    )
    doc = parse(content)
    doc.sort_keys()

    assert dumps(doc) == content


def test_sort_keys_failure_inside_sub_table_leaves_document_untouched() -> None:
    content = dedent(
        """\
        [b]
        z = 1
        [b.c]
        q = 2
        """
    )
    doc = parse(content)
    # moving "z" below the [b.c] header would change which table it
    # belongs to, so the requested order is rejected
    with pytest.raises(KeySortError):
        doc["b"].sort_keys(["c", "z"])

    assert dumps(doc) == content


def test_sort_keys_on_empty_and_single_key_containers_is_a_no_op() -> None:
    content = "only = 1\n"
    doc = parse(content)
    doc.sort_keys()
    doc.sort_keys(recursive=True)

    assert dumps(doc) == content


def test_sort_keys_table_method_returns_table_for_chaining() -> None:
    doc = parse("[tool]\nb = 1\na = 2\n")
    table = doc["tool"]

    assert table.sort_keys() is table
    assert dumps(doc) == "[tool]\na = 2\nb = 1\n"


def test_sort_keys_merges_scattered_table_like_the_parser() -> None:
    content = dedent(
        """\
        [a]
        x = 1
        [b]
        y = 2
        [a.c]
        z = 3
        """
    )
    doc = parse(content)
    doc.sort_keys()

    expected = dedent(
        """\
        [a]
        x = 1
        [a.c]
        z = 3
        [b]
        y = 2
        """
    )
    assert dumps(doc) == expected
    # the fragments became one entry, exactly as the parser would read
    # the sorted document back
    assert list(doc.keys()) == list(parse(expected).keys())
    assert_preserves_values(content, dumps(doc))
    assert_idempotent(doc)


def test_sort_keys_recursive_refusal_is_consistent() -> None:
    # sorting recursively would merge the scattered fragments of "a" into
    # one table whose plain key "x" cannot move below the [a.c] header,
    # so the sort is refused - and refused again on the untouched input
    content = dedent(
        """\
        [a]
        x = 1
        [b]
        y = 1
        [a.c]
        z = 1
        """
    )
    for _ in range(2):
        doc = parse(content)
        with pytest.raises(KeySortError):
            doc.sort_keys(recursive=True)
        assert dumps(doc) == content


def test_sort_keys_comment_before_table_header_travels_with_table() -> None:
    # a comment in front of a table header belongs to the body of the
    # preceding table and moves along with it
    content = dedent(
        """\
        [b]
        y = 2
        # still about b

        [a]
        x = 1
        """
    )
    doc = parse(content)
    doc.sort_keys()

    expected = dedent(
        """\
        [a]
        x = 1

        [b]
        y = 2
        # still about b
        """
    )
    assert dumps(doc) == expected
    assert_preserves_values(content, dumps(doc))
    assert_idempotent(doc)


def test_sort_keys_inline_table() -> None:
    content = "x = {b = 1, a = 2, c = 3}\n"
    doc = parse(content)
    doc["x"].sort_keys()

    expected = "x = {a = 2, b = 1, c = 3}\n"
    assert dumps(doc) == expected
    assert_preserves_values(content, dumps(doc))
    doc["x"].sort_keys()
    assert dumps(doc) == expected


def test_sort_keys_after_key_deletion() -> None:
    content = "c = 3\nb = 2\na = 1\n"
    doc = parse(content)
    del doc["b"]
    doc.sort_keys()

    assert dumps(doc) == "a = 1\nc = 3\n"
    assert_preserves_values("c = 3\na = 1\n", dumps(doc))
    assert_idempotent(doc)


def test_sort_keys_blank_line_inside_table_stays_separator() -> None:
    content = dedent(
        """\
        [b]
        y = 2

        [a]
        x = 1
        """
    )
    doc = parse(content)
    doc.sort_keys()

    expected = dedent(
        """\
        [a]
        x = 1

        [b]
        y = 2
        """
    )
    assert dumps(doc) == expected
    assert_preserves_values(content, dumps(doc))
    assert_idempotent(doc)

