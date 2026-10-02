from __future__ import annotations

import copy
import math

from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tomlkit.container import Container
from tomlkit.container import OutOfOrderTableProxy
from tomlkit.exceptions import BatchError
from tomlkit.exceptions import BatchTestFailed
from tomlkit.items import AoT
from tomlkit.items import AbstractTable
from tomlkit.items import Array
from tomlkit.items import Item
from tomlkit.items import Table
from tomlkit.items import Trivia
from tomlkit.items import item as _item
from tomlkit.toml_document import TOMLDocument


_OP_KINDS = ("set", "delete", "append", "test")


def _normalize_path(path: str | int | Iterable[str | int]) -> tuple[str | int, ...]:
    if isinstance(path, bool):
        raise TypeError("Invalid path segment: booleans are not valid indices")
    if isinstance(path, (str, int)):
        segments: tuple[str | int, ...] = (path,)
    else:
        segments = tuple(path)

    for segment in segments:
        if isinstance(segment, bool) or not isinstance(segment, (str, int)):
            raise TypeError(
                f"Invalid path segment {segment!r}: "
                "segments must be strings (keys) or integers (array indices)"
            )

    return segments


@dataclass(frozen=True)
class Op:
    """
    A single batch operation for apply_batch().

    The path is a sequence of key segments (str) and array indices (int),
    interpreted against the result of all previous operations in the batch.
    Use the Op.set(), Op.delete(), Op.append() and Op.test() constructors.
    """

    kind: str
    path: tuple[str | int, ...]
    value: Any = None

    def __post_init__(self) -> None:
        if self.kind not in _OP_KINDS:
            raise ValueError(
                f"Invalid operation {self.kind!r}: expected one of {_OP_KINDS}"
            )
        object.__setattr__(self, "path", _normalize_path(self.path))
        if not self.path:
            raise ValueError("Operation path must not be empty")

    @classmethod
    def set(cls, path: str | int | Iterable[str | int], value: Any) -> Op:
        """Set the value at path, replacing any existing value."""
        return cls("set", _normalize_path(path), value)

    @classmethod
    def delete(cls, path: str | int | Iterable[str | int]) -> Op:
        """Delete the key or array element at path."""
        return cls("delete", _normalize_path(path))

    @classmethod
    def append(cls, path: str | int | Iterable[str | int], value: Any) -> Op:
        """Append a value to the array or array of tables at path."""
        return cls("append", _normalize_path(path), value)

    @classmethod
    def test(cls, path: str | int | Iterable[str | int], value: Any) -> Op:
        """Require the current value at path to equal the given value.

        Comparison follows TOML value type semantics: a boolean is never
        equal to an integer, and an integer is never equal to a float.
        """
        return cls("test", _normalize_path(path), value)


def _normalize_op(op: Op | Mapping[str, Any], index: int) -> Op:
    if isinstance(op, Op):
        return op

    if isinstance(op, Mapping):
        try:
            kind = op["op"]
            path = op["path"]
        except KeyError as e:
            raise BatchError(
                index, str(op.get("op")), (), f"missing operation field {e}"
            ) from e
        value = op.get("value")
        try:
            return Op(kind, _normalize_path(path), value)
        except (TypeError, ValueError) as e:
            raise BatchError(index, str(kind), (), str(e)) from e

    raise BatchError(index, "?", (), f"invalid operation {op!r}")


def _container_of(node: Any) -> Container | None:
    if isinstance(node, Container):
        return node
    if isinstance(node, AbstractTable):
        return node.value
    if isinstance(node, OutOfOrderTableProxy):
        return node._internal_container
    return None


def _new_super_table() -> Table:
    return Table(Container(True), Trivia(), False, is_super_table=True)


def _descend(node: Any, segment: str | int, create: bool = False) -> Any:
    if isinstance(segment, str):
        container = _container_of(node)
        if container is None:
            raise TypeError(
                f"cannot look up key {segment!r} in {type(node).__name__}"
            )
        if create and segment not in container:
            # Auto-vivify a missing intermediate table so that a deleted
            # subtree can be rebuilt by a later set operation.
            node[segment] = _new_super_table()
        return container.item(segment)

    if isinstance(node, (Array, AoT)):
        return node[segment]

    raise TypeError(
        f"cannot index {type(node).__name__} with [{segment}]: "
        "only arrays and arrays of tables accept integer indices"
    )


def _resolve(root: Container, path: tuple[str | int, ...]) -> Any:
    node: Any = root
    for segment in path:
        node = _descend(node, segment)
    return node


def _resolve_parent(root: Container, path: tuple[str | int, ...], create: bool) -> Any:
    node: Any = root
    for segment in path[:-1]:
        node = _descend(node, segment, create=create)
    return node


def _apply_set(root: Container, op: Op) -> None:
    parent = _resolve_parent(root, op.path, create=True)
    last = op.path[-1]

    if isinstance(last, str):
        if not isinstance(parent, (Container, AbstractTable, OutOfOrderTableProxy)):
            raise TypeError(f"cannot set key {last!r} on {type(parent).__name__}")
        parent[last] = op.value
        return

    if isinstance(parent, Array):
        parent[last] = op.value
        return

    if isinstance(parent, AoT):
        converted = _item(op.value)
        if not isinstance(converted, Table):
            raise TypeError(
                "elements of an array of tables must be tables, "
                f"got {type(converted).__name__}"
            )
        parent[last] = converted
        return

    raise TypeError(
        f"cannot set index [{last}] on {type(parent).__name__}: "
        "only arrays and arrays of tables accept integer indices"
    )


def _apply_delete(root: Container, op: Op) -> None:
    parent = _resolve_parent(root, op.path, create=False)
    last = op.path[-1]

    if isinstance(last, str):
        if not isinstance(parent, (Container, AbstractTable, OutOfOrderTableProxy)):
            raise TypeError(
                f"cannot delete key {last!r} from {type(parent).__name__}"
            )
        del parent[last]
        return

    if isinstance(parent, (Array, AoT)):
        del parent[last]
        return

    raise TypeError(
        f"cannot delete index [{last}] from {type(parent).__name__}: "
        "only arrays and arrays of tables accept integer indices"
    )


def _apply_append(root: Container, op: Op) -> None:
    target = _resolve(root, op.path)

    if isinstance(target, Array):
        target.insert(len(target), op.value)
        return

    if isinstance(target, AoT):
        target.insert(len(target), op.value)
        return

    raise TypeError(
        f"cannot append to {type(target).__name__}: "
        "append target must be an array or an array of tables"
    )


def _unwrap(value: Any) -> Any:
    if isinstance(value, (Item, Container, OutOfOrderTableProxy)):
        return value.unwrap()
    return value


def _toml_value_equal(actual: Any, expected: Any) -> bool:
    actual = _unwrap(actual)
    expected = _unwrap(expected)

    # TOML type semantics: booleans, integers and floats are distinct types,
    # so Python's bool-is-an-int and 1 == 1.0 coercions must not apply.
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected

    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        if type(actual) is not type(expected):
            return False
        if isinstance(actual, float) and math.isnan(actual) and math.isnan(expected):
            return True
        return bool(actual == expected)

    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _toml_value_equal(a, e) for a, e in zip(actual, expected)
        )

    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _toml_value_equal(actual[k], expected[k]) for k in actual
        )

    return type(actual) is type(expected) and actual == expected


def _apply_test(root: Container, op: Op, index: int) -> None:
    actual = _resolve(root, op.path)
    if not _toml_value_equal(actual, op.value):
        raise BatchTestFailed(index, op.path, _unwrap(op.value), _unwrap(actual))


def _apply(root: Container, op: Op, index: int) -> None:
    if op.kind == "set":
        _apply_set(root, op)
    elif op.kind == "delete":
        _apply_delete(root, op)
    elif op.kind == "append":
        _apply_append(root, op)
    else:
        _apply_test(root, op, index)


def _commit(document: TOMLDocument, working: TOMLDocument) -> None:
    """Swap the validated state of the working copy into the document.

    Item objects previously obtained from the document keep referencing the
    pre-batch state: they stay usable as detached snapshots but no longer
    reflect, or affect, the document.
    """
    for key in list(dict.keys(document)):
        dict.__delitem__(document, key)
    document.__setstate__(
        (working._map, working._body, working._parsed, working._table_keys)
    )
    document._validation_cache.clear()


def apply_batch(
    document: TOMLDocument, operations: Iterable[Op | Mapping[str, Any]]
) -> TOMLDocument:
    """Apply a batch of operations to a TOML document atomically.

    Each operation is an Op (see Op.set(), Op.delete(), Op.append() and
    Op.test()) or an equivalent mapping such as
    {'op': 'set', 'path': ['a', 'b'], 'value': 1}. Paths are sequences
    of key segments (str) and array indices (int); every operation is
    interpreted against the result of the previous ones, and a test
    operation checks the current value with TOML value type semantics
    (a boolean is never equal to an integer).

    The batch runs on an internal copy of the document, so comments and
    formatting of untouched nodes are preserved. Only if every operation
    succeeds is the original document updated; otherwise it is left
    completely unchanged and a BatchError (BatchTestFailed for a failed
    test) reports the index and path of the failing operation.

    View contract: container views (tables, arrays, ...) fetched from the
    document before the call are never partially updated. After a failed
    batch they remain fully valid. After a successful batch they become
    detached snapshots of the pre-batch state: they can still be read and
    rendered, but they no longer reflect the document and mutating them
    does not affect it; fetch fresh views from the document instead.

    Returns the (updated) document.
    """
    ops = [_normalize_op(op, index) for index, op in enumerate(operations)]

    working = copy.deepcopy(document)
    for index, op in enumerate(ops):
        try:
            _apply(working, op, index)
        except BatchError:
            raise
        except Exception as e:
            raise BatchError(index, op.kind, op.path, str(e)) from e

    _commit(document, working)

    return document
