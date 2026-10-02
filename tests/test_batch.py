from __future__ import annotations

import pytest

import tomlkit

from tomlkit import BatchError


def test_batch_applies_sequential_set_append_and_test() -> None:
    doc = tomlkit.document()
    operations: list[object] = [
        ["set", ["items"], []],
        ["append", ["items"], "first"],
        ("append", ["items"], "second"),
        {"op": "test", "path": ["items", 1], "expected": "second"},
        ["delete", ["items", 0]],
        {"op": "test", "path": ["items", 0], "value": "second"},
    ]

    result = doc.batch(operations)

    assert result is doc
    assert doc["items"] == ["second"]


def test_failed_batch_leaves_document_and_existing_views_unchanged() -> None:
    content = '''[server]
host = "localhost" # host comment
ports = [8000, 8001]
'''
    doc = tomlkit.parse(content)
    server = doc["server"]

    with pytest.raises(BatchError) as exc_info:
        doc.batch(
            [
                {"op": "set", "path": ["server", "host"], "value": "changed"},
                {"op": "set", "path": ["server", "ports", 0], "value": 8080},
                {"op": "delete", "path": ["server", "missing"]},
            ]
        )

    error = exc_info.value
    assert error.operation_index == 2
    assert error.path == ("server", "missing")
    assert doc.as_string() == content
    assert server["host"] == "localhost"
    assert server["ports"] == [8000, 8001]


def test_batch_distinguishes_booleans_from_integers() -> None:
    doc = tomlkit.parse('number = 1\nflag = true\n')

    with pytest.raises(BatchError, match="operation 0"):
        doc.batch([{"op": "test", "path": ["flag"], "value": 1}])

    with pytest.raises(BatchError, match="operation 0"):
        doc.batch([{"op": "test", "path": ["number"], "value": True}])

    with pytest.raises(BatchError, match="operation 0"):
        doc.batch([{"op": "test", "path": ["number"], "value": 1.0}])

    assert doc["number"] == 1
    assert doc["flag"] is True


def test_test_rejects_values_without_toml_type() -> None:
    doc = tomlkit.parse('value = 1\n')

    with pytest.raises(BatchError, match="test failed"):
        doc.batch([{"op": "test", "path": ["value"], "value": None}])


def test_batch_appends_to_array_of_tables_and_updates_new_index() -> None:
    content = '''[[people]]
name = "Ada" # preserved
'''
    doc = tomlkit.parse(content)

    doc.batch(
        [
            {"op": "append", "path": ["people"], "value": {"name": "Grace"}},
            {"op": "set", "path": ["people", 1, "name"], "value": "Hopper"},
            {"op": "test", "path": ["people", 0, "name"], "value": "Ada"},
        ]
    )

    assert doc["people"][0]["name"] == "Ada"
    assert doc["people"][1]["name"] == "Hopper"
    assert doc.as_string() == '''[[people]]
name = "Ada" # preserved
[[people]]
name = "Hopper"
'''


def test_batch_deletes_dotted_key_and_rebuilds_it() -> None:
    content = '''a.b = 1 # old dotted value

[c]
x = 2 # untouched
'''
    doc = tomlkit.parse(content)

    doc.batch(
        [
            {"op": "delete", "path": ["a", "b"]},
            {"op": "set", "path": ["a", "b"], "value": 2},
        ]
    )

    assert doc.unwrap() == {"a": {"b": 2}, "c": {"x": 2}}
    rendered = doc.as_string()
    assert rendered.startswith("a.b = 2\n")
    assert "# old dotted value" not in rendered
    assert "x = 2 # untouched" in rendered


def test_successful_batch_keeps_old_views_as_snapshots() -> None:
    content = '''[server]
host = "localhost"

[other]
name = "untouched" # keep formatting
'''
    doc = tomlkit.parse(content)
    old_server = doc["server"]

    doc.batch([{"op": "set", "path": ["server", "host"], "value": "example.com"}])

    assert doc["server"]["host"] == "example.com"
    assert old_server["host"] == "localhost"
    assert doc["server"] is not old_server
    assert "# keep formatting" in doc.as_string()


def test_batch_creates_missing_intermediate_tables() -> None:
    doc = tomlkit.document()

    doc.batch([{"op": "set", "path": ["a", "b", "c"], "value": 1}])

    assert doc.unwrap() == {"a": {"b": {"c": 1}}}


def test_append_requires_an_array() -> None:
    doc = tomlkit.parse('name = "toml"\n')

    with pytest.raises(BatchError) as exc_info:
        doc.batch([{"op": "append", "path": ["name"], "value": "x"}])

    assert exc_info.value.operation_index == 0
    assert exc_info.value.path == ("name",)


def test_array_of_tables_rejects_non_table_values() -> None:
    doc = tomlkit.parse("[[people]]\nname = 'Ada'\n")

    with pytest.raises(BatchError, match="table values"):
        doc.batch([{"op": "append", "path": ["people"], "value": "Grace"}])

    assert doc["people"] == [{"name": "Ada"}]
