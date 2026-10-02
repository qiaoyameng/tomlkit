import pytest

from tomlkit import apply_batch
from tomlkit import dumps
from tomlkit import parse
from tomlkit.batch import Op
from tomlkit.exceptions import BatchError
from tomlkit.exceptions import BatchTestFailed


def test_batch_append_to_aot_then_set_by_index() -> None:
    doc = parse("""\
# servers configuration
[[servers]]
name = "alpha"  # primary
""")

    apply_batch(
        doc,
        [
            Op.append("servers", {"name": "beta", "port": 8080}),
            Op.set(["servers", 1, "port"], 9090),
            Op.test(["servers", 1, "name"], "beta"),
            Op.test(["servers", 0, "name"], "alpha"),
        ],
    )

    assert doc["servers"][1]["port"] == 9090
    assert doc["servers"][1]["name"] == "beta"
    # untouched nodes keep their comments and formatting
    assert dumps(doc) == """\
# servers configuration
[[servers]]
name = "alpha"  # primary
[[servers]]
name = "beta"
port = 9090
"""


def test_batch_delete_dotted_key_then_rebuild() -> None:
    doc = parse("""\
a.b = 1
c = 2
""")

    apply_batch(
        doc,
        [
            Op.delete(["a", "b"]),
            Op.set(["a", "b"], "rebuilt"),
        ],
    )

    assert doc["a"]["b"] == "rebuilt"
    assert dumps(doc) == """\
a.b = "rebuilt"
c = 2
"""


def test_batch_delete_whole_dotted_key_then_rebuild_deeper() -> None:
    doc = parse("""\
a.b = 1
c = 2
""")

    apply_batch(
        doc,
        [
            Op.delete("a"),
            Op.set(["a", "b", "c"], 3),
        ],
    )

    assert doc["a"]["b"]["c"] == 3
    assert parse(dumps(doc))["a"]["b"]["c"] == 3


def test_batch_failure_at_last_step_leaves_document_untouched() -> None:
    content = """\
flag = true
count = 1
"""
    doc = parse(content)
    view = doc["count"]

    with pytest.raises(BatchError) as error:
        apply_batch(
            doc,
            [
                Op.set("count", 2),
                Op.delete("missing"),
            ],
        )

    assert error.value.index == 1
    assert error.value.op == "delete"
    assert error.value.path == ("missing",)
    # nothing leaked into the original document
    assert dumps(doc) == content
    assert doc["count"] == 1
    # views fetched before the batch are untouched, not half-updated
    assert doc["count"] is view
    assert view == 1


def test_batch_test_failure_reports_index_and_path() -> None:
    doc = parse("flag = true\ncount = 1\n")

    with pytest.raises(BatchTestFailed) as error:
        apply_batch(
            doc,
            [
                Op.set("count", 2),
                Op.test("flag", 1),  # true is not the integer 1
            ],
        )

    assert error.value.index == 1
    assert error.value.path == ("flag",)
    assert error.value.expected == 1
    assert error.value.actual is True
    assert dumps(doc) == "flag = true\ncount = 1\n"


def test_batch_test_uses_toml_type_semantics() -> None:
    doc = parse("""\
flag = true
int = 1
float = 1.0
""")

    # a boolean is not an integer and vice versa
    with pytest.raises(BatchTestFailed):
        apply_batch(doc, [Op.test("flag", 1)])
    with pytest.raises(BatchTestFailed):
        apply_batch(doc, [Op.test("int", True)])
    # an integer is not a float
    with pytest.raises(BatchTestFailed):
        apply_batch(doc, [Op.test("int", 1.0)])
    with pytest.raises(BatchTestFailed):
        apply_batch(doc, [Op.test("float", 1)])

    # exact type and value matches pass
    apply_batch(
        doc,
        [
            Op.test("flag", True),
            Op.test("int", 1),
            Op.test("float", 1.0),
        ],
    )
    assert dumps(doc) == "flag = true\nint = 1\nfloat = 1.0\n"


def test_batch_test_on_arrays_and_tables() -> None:
    doc = parse("""\
[point]
x = 1
y = 2
tags = ["a", "b"]
""")

    apply_batch(
        doc,
        [
            Op.test("point", {"x": 1, "y": 2, "tags": ["a", "b"]}),
            Op.test(["point", "tags", 1], "b"),
        ],
    )

    with pytest.raises(BatchTestFailed):
        apply_batch(doc, [Op.test("point", {"x": 1, "y": 2, "tags": ["a"]})])


def test_batch_operations_see_previous_results() -> None:
    doc = parse("a = 1\n")

    apply_batch(
        doc,
        [
            Op.set("a", 2),
            Op.test("a", 2),
            Op.delete("a"),
            Op.set(["b", "c"], [1, 2]),
            Op.append(["b", "c"], 3),
            Op.test(["b", "c"], [1, 2, 3]),
        ],
    )

    assert "a" not in doc
    assert doc["b"]["c"] == [1, 2, 3]


def test_batch_views_become_detached_snapshots_after_success() -> None:
    doc = parse("""\
[server]
host = "old"
""")
    view = doc["server"]

    apply_batch(doc, [Op.set(["server", "host"], "new")])

    # the document reflects the batch
    assert doc["server"]["host"] == "new"
    assert doc["server"] is not view
    # the old view is a stable snapshot of the pre-batch state
    assert view["host"] == "old"
    view["host"] = "mutated"
    assert doc["server"]["host"] == "new"


def test_batch_views_stay_valid_after_failure() -> None:
    content = "[server]\nhost = " + chr(34) + "old" + chr(34) + "\n"
    doc = parse(content)
    view = doc["server"]

    with pytest.raises(BatchError):
        apply_batch(
            doc,
            [Op.set(["server", "host"], "new"), Op.test("nope", 1)],
        )

    assert doc["server"] is view
    assert view["host"] == "old"
    assert dumps(doc) == content


def test_batch_accepts_mapping_operations() -> None:
    doc = parse("a = 1\narr = [1]\n")

    apply_batch(
        doc,
        [
            {"op": "test", "path": ["a"], "value": 1},
            {"op": "set", "path": ["a"], "value": 2},
            {"op": "append", "path": ["arr"], "value": 2},
            {"op": "delete", "path": ["arr", 0]},
        ],
    )

    assert doc["a"] == 2
    assert doc["arr"] == [2]


def test_batch_invalid_operation_reports_index() -> None:
    doc = parse("a = 1\n")

    with pytest.raises(BatchError) as error:
        apply_batch(doc, [{"op": "set", "path": ["a"]}])
    assert error.value.index == 0

    with pytest.raises(BatchError) as error:
        apply_batch(doc, [Op.set("a", 1), {"op": "nope", "path": ["a"]}])
    assert error.value.index == 1
    assert dumps(doc) == "a = 1\n"


def test_batch_array_index_errors_are_reported() -> None:
    doc = parse("arr = [1, 2]\n")

    with pytest.raises(BatchError) as error:
        apply_batch(doc, [Op.set(["arr", 5], 3)])
    assert error.value.index == 0
    assert error.value.path == ("arr", 5)
    assert doc["arr"] == [1, 2]

    with pytest.raises(BatchError):
        apply_batch(doc, [Op.append("missing", 1)])
    assert doc["arr"] == [1, 2]


def test_batch_delete_array_element() -> None:
    doc = parse("arr = [1, 2, 3]\n")

    apply_batch(doc, [Op.delete(["arr", 1])])

    assert doc["arr"] == [1, 3]
    assert dumps(doc) == "arr = [1, 3]\n"


def test_batch_returns_document() -> None:
    doc = parse("a = 1\n")

    assert apply_batch(doc, [Op.set("a", 2)]) is doc


def test_batch_test_on_missing_path_fails() -> None:
    doc = parse("a = 1\n")

    with pytest.raises(BatchError) as error:
        apply_batch(doc, [Op.test("missing", 1)])

    assert error.value.index == 0
    assert error.value.op == "test"
    assert error.value.path == ("missing",)
    assert dumps(doc) == "a = 1\n"


def test_batch_rejects_boolean_path_segments() -> None:
    with pytest.raises(TypeError):
        Op.set([True], 1)

    with pytest.raises(BatchError):
        apply_batch(parse("a = 1\n"), [{"op": "set", "path": [False], "value": 1}])


def test_batch_set_replaces_table_with_value_and_back() -> None:
    doc = parse("""\
[a]
x = 1

[b]
y = 2
""")

    apply_batch(doc, [Op.set("a", 5)])
    assert doc["a"] == 5
    assert parse(dumps(doc))["b"]["y"] == 2

    apply_batch(doc, [Op.set("a", {"z": 3})])
    assert doc["a"]["z"] == 3
    reparsed = parse(dumps(doc))
    assert reparsed["a"]["z"] == 3
    assert reparsed["b"]["y"] == 2


def test_batch_negative_array_index() -> None:
    doc = parse("arr = [1, 2, 3]\n")

    apply_batch(doc, [Op.set(["arr", -1], 9)])

    assert doc["arr"] == [1, 2, 9]
