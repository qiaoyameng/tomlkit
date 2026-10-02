from __future__ import annotations

import pytest

from tomlkit import dumps
from tomlkit import parse
from tomlkit.exceptions import KeyAlreadyPresent
from tomlkit.exceptions import NonExistentKey
from tomlkit.exceptions import TOMLKitError


def test_move_value_between_tables_keeps_attached_comments() -> None:
    doc = parse(
        """\
# paragraph about the server (blank-line separated, stays)

# TLS settings
[server]
host = "localhost"
# whether TLS is enabled
tls = true  # required in production

# paragraph about the port (stays)
port = 8080

[security]
level = "high"
"""
    )

    doc.move(["server", "tls"], ["security", "tls"])

    assert dumps(doc) == """\
# paragraph about the server (blank-line separated, stays)

# TLS settings
[server]
host = "localhost"

# paragraph about the port (stays)
port = 8080

[security]
level = "high"
# whether TLS is enabled
tls = true  # required in production
"""

    reparsed = parse(dumps(doc))
    assert reparsed["security"]["tls"] is True
    assert "tls" not in reparsed["server"]
    assert reparsed["server"]["port"] == 8080


def test_move_value_creates_missing_intermediate_tables() -> None:
    doc = parse("[server]\ntls = true\n")

    doc.move(["server", "tls"], ["security", "transport", "tls"])

    assert dumps(doc) == "[server]\n\n[security.transport]\ntls = true\n"
    reparsed = parse(dumps(doc))
    assert reparsed["security"]["transport"]["tls"] is True


def test_move_table_with_quoted_key_and_header_comment() -> None:
    doc = parse(
        """\
[server]
host = "x"

# comment attached to the inner table
[server."inner.part"]  # trailing header comment
a = 1

[other]
b = 2

[server.late]
c = 3
"""
    )

    doc.move(["server", "inner.part"], ["security", "inner"])

    assert dumps(doc) == """\
[server]
host = "x"

[other]
b = 2

[server.late]
c = 3

# comment attached to the inner table
[security.inner]  # trailing header comment
a = 1

"""

    reparsed = parse(dumps(doc))
    assert reparsed["security"]["inner"]["a"] == 1
    assert "inner.part" not in reparsed["server"]
    assert reparsed["server"]["late"]["c"] == 3


def test_move_array_of_tables() -> None:
    doc = parse(
        """\
[server]
# endpoints of the server
[[server.endpoints]]
url = "https://a"

[[server.endpoints]]
url = "https://b"
"""
    )

    doc.move(["server", "endpoints"], ["security", "endpoints"])

    assert dumps(doc) == """\
[server]

# endpoints of the server
[[security.endpoints]]
url = "https://a"

[[security.endpoints]]
url = "https://b"
"""

    reparsed = parse(dumps(doc))
    assert [e["url"] for e in reparsed["security"]["endpoints"]] == [
        "https://a",
        "https://b",
    ]
    assert "endpoints" not in reparsed["server"]


def test_move_dotted_key_implicit_table_keeps_inline_rendering() -> None:
    doc = parse(
        """\
[server]
# c1
tls.enabled = true
host = "x"
# c2
tls.version = "1.3"
"""
    )

    doc.move(["server", "tls"], ["security", "tls"])

    assert dumps(doc) == """\
[server]
host = "x"

[security]
# c1
# c2
tls.enabled = true
tls.version = "1.3"
"""

    reparsed = parse(dumps(doc))
    assert reparsed["security"]["tls"] == {"enabled": True, "version": "1.3"}
    assert "tls" not in reparsed["server"]


def test_move_nested_dotted_key_table() -> None:
    doc = parse("[server]\ntls.a.b = 1\ntls.a.c = 2\n")

    doc.move(["server", "tls"], ["security", "tls"])

    assert dumps(doc) == "[server]\n\n[security]\ntls.a.b = 1\ntls.a.c = 2\n"
    reparsed = parse(dumps(doc))
    assert reparsed["security"]["tls"] == {"a": {"b": 1, "c": 2}}


def test_move_renames_node_and_preserves_dotted_style() -> None:
    doc = parse("[server]\ntls.enabled = true\n")

    doc.move(["server", "tls"], ["security", "ssl"])

    assert dumps(doc) == "[server]\n\n[security]\nssl.enabled = true\n"
    reparsed = parse(dumps(doc))
    assert reparsed["security"]["ssl"] == {"enabled": True}


def test_move_segment_with_dot_is_a_single_quoted_key() -> None:
    doc = parse('[server]\n"tls.cert" = 1\n[security]\n')

    doc.move(["server", "tls.cert"], ["security", "tls.cert"])

    assert dumps(doc) == '[server]\n[security]\n"tls.cert" = 1\n'
    reparsed = parse(dumps(doc))
    assert reparsed["security"]["tls.cert"] == 1
    assert "tls" not in reparsed["security"]


def test_move_value_out_of_out_of_order_table_fragment() -> None:
    doc = parse("[a]\nb = 1\n[c]\nd = 1\n[a.e]\nf = 2\n")

    doc.move(["a", "b"], ["c", "b"])

    assert dumps(doc) == "[a]\n[c]\nd = 1\nb = 1\n[a.e]\nf = 2\n"
    reparsed = parse(dumps(doc))
    assert reparsed["c"]["b"] == 1
    assert "b" not in reparsed["a"]
    assert reparsed["a"]["e"]["f"] == 2


def test_move_implicit_table_from_out_of_order_headers() -> None:
    doc = parse("[a.b]\nx = 1\n[c]\ny = 2\n[a.d]\nz = 3\n")

    doc.move(["a"], ["root_a"])

    assert dumps(doc) == "[c]\ny = 2\n\n[root_a.b]\nx = 1\n\n[root_a.d]\nz = 3\n"
    reparsed = parse(dumps(doc))
    assert reparsed["root_a"] == {"b": {"x": 1}, "d": {"z": 3}}
    assert "a" not in reparsed


def test_move_value_into_table_with_sub_tables_inserts_before_them() -> None:
    doc = parse("[a]\nb = 1\n[c]\nd = 1\n[c.sub]\ne = 2\n")

    doc.move(["a", "b"], ["c", "b"])

    assert dumps(doc) == "[a]\n[c]\nd = 1\nb = 1\n\n[c.sub]\ne = 2\n"
    reparsed = parse(dumps(doc))
    assert reparsed["c"]["b"] == 1
    assert reparsed["c"]["sub"]["e"] == 2


@pytest.mark.parametrize(
    "source,target,error",
    [
        (["a"], ["a", "c", "x"], TOMLKitError),  # into its own descendant
        (["a"], ["a", "b"], TOMLKitError),  # into itself
        (["a", "b"], ["c", "b"], KeyAlreadyPresent),  # target key exists
        (["a", "b"], ["a", "b"], KeyAlreadyPresent),  # same location
        (["a", "nope"], ["c", "x"], NonExistentKey),  # missing source key
        (["nope", "b"], ["c", "x"], NonExistentKey),  # missing source table
        (["a", "b", "c"], ["d", "x"], TOMLKitError),  # through a plain value
        (["a", "b"], ["c", "b", "x"], TOMLKitError),  # into a plain value
    ],
)
def test_failed_move_leaves_document_byte_identical(
    source: list[str], target: list[str], error: type[Exception]
) -> None:
    content = "[a]\nb = 1\n[a.c]\nd = 2\n\n[c]\nb = 2\n[c.d]\ne = 3\n"
    doc = parse(content)

    with pytest.raises(error):
        doc.move(source, target)

    assert dumps(doc) == content


def test_failed_move_with_dotted_source_leaves_document_byte_identical() -> None:
    content = "[server]\ntls.enabled = true\ntls.version = 1\n[security]\ntls = 1\n"
    doc = parse(content)

    with pytest.raises(KeyAlreadyPresent):
        doc.move(["server", "tls"], ["security", "tls"])

    assert dumps(doc) == content


def test_move_out_of_order_table_is_rejected_without_mutation() -> None:
    content = "[a]\nb = 1\n[c]\nd = 1\n[a.e]\nf = 2\n"
    doc = parse(content)

    with pytest.raises(TOMLKitError):
        doc.move(["a"], ["z"])

    assert dumps(doc) == content


def test_move_rejects_invalid_paths_without_mutation() -> None:
    content = "[a]\nb = 1\n"
    doc = parse(content)

    with pytest.raises(ValueError):
        doc.move([], ["x"])
    with pytest.raises(ValueError):
        doc.move(["a"], [])
    with pytest.raises(TypeError):
        doc.move("a.b", ["x"])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        doc.move(["a"], "x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        doc.move([1], ["x"])  # type: ignore[list-item]

    assert dumps(doc) == content


def test_move_in_document_mixing_quoted_dotted_and_out_of_order_tables() -> None:
    content = """\
# global settings

[server]
# quoted key below
"odd.key" = 1
# tls block
tls.enabled = true  # turn it on
tls.version = "1.3"

[other]
x = 1

[server.late]
y = 2

# attached to endpoints
[[server.endpoints]]
url = "https://a"
"""
    doc = parse(content)

    doc.move(["server", "tls"], ["security", "tls"])
    doc.move(["server", "odd.key"], ["security", "odd.key"])
    doc.move(["server", "endpoints"], ["security", "endpoints"])

    assert dumps(doc) == """\
# global settings

[server]

[other]
x = 1

[server.late]
y = 2


[security]
# tls block
tls.enabled = true  # turn it on
tls.version = "1.3"
# quoted key below
"odd.key" = 1

# attached to endpoints
[[security.endpoints]]
url = "https://a"
"""

    reparsed = parse(dumps(doc))
    assert reparsed["security"]["tls"] == {"enabled": True, "version": "1.3"}
    assert reparsed["security"]["odd.key"] == 1
    assert reparsed["security"]["endpoints"] == [{"url": "https://a"}]
    assert reparsed["server"]["late"]["y"] == 2
    assert "tls" not in reparsed["server"]
