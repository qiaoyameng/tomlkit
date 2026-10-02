from __future__ import annotations

import copy

from collections.abc import Mapping
from collections.abc import Sequence
from datetime import date
from datetime import datetime
from datetime import time
from typing import Any
from typing import cast

from tomlkit.container import Container
from tomlkit.container import OutOfOrderTableProxy
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import AoT
from tomlkit.items import Array
from tomlkit.items import InlineTable
from tomlkit.items import Item
from tomlkit.items import Key
from tomlkit.items import Table
from tomlkit.items import Trivia
from tomlkit.items import item as _item


PathFragment = str | int | Key
PathLike = str | Sequence[PathFragment]
Operation = Mapping[str, Any] | Sequence[Any]


def _render_path(path: tuple[PathFragment, ...]) -> str:
    rendered = "$"
    for fragment in path:
        if isinstance(fragment, int) and not isinstance(fragment, bool):
            rendered += f"[{fragment}]"
        else:
            rendered += f".{fragment}"
    return rendered


class BatchError(TOMLKitError):
    """An operation in an atomic batch failed."""

    def __init__(
        self,
        operation_index: int,
        path: tuple[PathFragment, ...],
        message: str,
    ) -> None:
        self.operation_index = operation_index
        self.path = path
        super().__init__(
            f"Batch operation {operation_index} failed at "
            f"{_render_path(path)}: {message}"
        )


BatchOperationError = BatchError


def _coerce_operation(
    operation: Any, index: int
) -> tuple[str, PathLike, Any]:
    value: Any = None
    if isinstance(operation, Mapping):
        name = operation.get("op", operation.get("operation"))
        path = operation.get("path")
        if name in {"set", "append"}:
            if "value" not in operation:
                raise BatchError(index, (), f"{name!r} operation requires a value")
            value = operation["value"]
        elif name == "test":
            if "value" in operation:
                value = operation["value"]
            elif "expected" in operation:
                value = operation["expected"]
            else:
                raise BatchError(index, (), "'test' operation requires a value")
        elif name == "delete":
            if "value" in operation:
                raise BatchError(
                    index, (), "'delete' operation does not take a value"
                )
        else:
            raise BatchError(index, (), f"unknown operation {name!r}")
    elif isinstance(operation, Sequence) and not isinstance(
        operation, (str, bytes)
    ):
        sequence_operation = cast(tuple[Any, ...], tuple(operation))
        if len(operation) < 2:
            raise BatchError(index, (), "an operation tuple requires op and path")
        name, path, *rest = sequence_operation
        if name in {"set", "append"}:
            if len(rest) != 1:
                raise BatchError(index, (), f"{name!r} operation requires a value")
            value = rest[0]
        elif name == "test":
            if len(rest) != 1:
                raise BatchError(index, (), "'test' operation requires a value")
            value = rest[0]
        elif name == "delete":
            if len(rest) > 1:
                raise BatchError(
                    index, (), "'delete' operation does not take a value"
                )
        else:
            raise BatchError(index, (), f"unknown operation {name!r}")
    else:
        raise BatchError(index, (), "an operation must be a mapping or tuple")

    if not isinstance(name, str):
        raise BatchError(index, (), "operation name must be a string")
    if path is None:
        raise BatchError(index, (), "operation path is required")
    return name, path, value


def _normalize_path(
    path: PathLike, index: int
) -> tuple[PathFragment, ...]:
    if isinstance(path, (str, Key)):
        fragments = (path,)
    elif isinstance(path, Sequence):
        fragments = cast(tuple[PathFragment, ...], tuple(path))
    else:
        raise BatchError(index, (), "path must be a string or sequence of fragments")

    if not fragments:
        raise BatchError(index, (), "path must not be empty")

    normalized: list[PathFragment] = []
    for fragment in fragments:
        if isinstance(fragment, bool) or not isinstance(
            fragment, (str, int, Key)
        ):
            raise BatchError(
                index, fragments, "path fragments must be strings or ints"
            )
        if isinstance(fragment, Key):
            fragment = (
                next(iter(fragment)).key
                if fragment.is_multi()
                else fragment.key
            )
        normalized.append(fragment)
    return tuple(normalized)


def _new_table(parent: Any) -> Table | InlineTable:
    if isinstance(parent, InlineTable):
        return InlineTable(Container(), Trivia(), new=True)
    return Table(Container(), Trivia(), False)


def _get_child(
    parent: Any,
    fragment: PathFragment,
    index: int,
    path: tuple[PathFragment, ...],
) -> Any:
    if isinstance(parent, (Container, Table, InlineTable)):
        if not isinstance(fragment, str):
            raise BatchError(index, path, "table keys must be strings")
        try:
            return parent.item(fragment)
        except KeyError as ex:
            message = str(ex.args[0]) if ex.args else "key not found"
            raise BatchError(index, path, message) from ex

    if isinstance(parent, OutOfOrderTableProxy):
        if not isinstance(fragment, str):
            raise BatchError(index, path, "table keys must be strings")
        try:
            return parent._internal_container.item(fragment)
        except KeyError as ex:
            message = str(ex.args[0]) if ex.args else "key not found"
            raise BatchError(index, path, message) from ex

    if isinstance(parent, (Array, AoT)):
        if not isinstance(fragment, int) or isinstance(fragment, bool):
            raise BatchError(index, path, "array indexes must be ints")
        try:
            return parent[fragment]
        except IndexError as ex:
            raise BatchError(index, path, str(ex)) from ex

    raise BatchError(index, path, f"cannot navigate into {type(parent).__name__}")


def _navigate(
    root: Container,
    path: tuple[PathFragment, ...],
    index: int,
    *,
    create_missing_tables: bool = False,
) -> tuple[Any, PathFragment]:
    current: Any = root
    for depth, fragment in enumerate(path[:-1]):
        current_path = path[: depth + 1]
        try:
            current = _get_child(current, fragment, index, current_path)
        except BatchError as ex:
            if (
                not create_missing_tables
                or not isinstance(fragment, str)
                or not isinstance(
                    current,
                    (Container, Table, InlineTable, OutOfOrderTableProxy),
                )
            ):
                raise
            table = _new_table(current)
            current[fragment] = table
            current = _get_child(current, fragment, index, current_path)

    return current, path[-1]


def _convert_value(value: Any, parent: Any) -> Item:
    if isinstance(value, Item):
        value = copy.deepcopy(value)
    conversion_parent = parent if isinstance(parent, Item) else None
    converted = _item(value, _parent=conversion_parent)
    return copy.deepcopy(converted)


def _aot_value(value: Any, path: tuple[PathFragment, ...], index: int) -> Table:
    converted = _convert_value(value, None)
    if not isinstance(converted, Table):
        raise BatchError(index, path, "arrays of tables only accept table values")
    return converted


def _set_value(
    parent: Any,
    fragment: PathFragment,
    value: Any,
    path: tuple[PathFragment, ...],
    index: int,
) -> None:
    if isinstance(parent, AoT):
        if not isinstance(fragment, int) or isinstance(fragment, bool):
            raise BatchError(index, path, "array indexes must be ints")
        try:
            parent[fragment] = _aot_value(value, path, index)
        except IndexError as ex:
            raise BatchError(index, path, str(ex)) from ex
        return

    if isinstance(parent, Array):
        if not isinstance(fragment, int) or isinstance(fragment, bool):
            raise BatchError(index, path, "array indexes must be ints")
        try:
            parent[fragment] = _convert_value(value, parent)
        except IndexError as ex:
            raise BatchError(index, path, str(ex)) from ex
        return

    if isinstance(
        parent, (Container, Table, InlineTable, OutOfOrderTableProxy)
    ) and isinstance(fragment, str):
        parent[fragment] = _convert_value(value, parent)
        return

    raise BatchError(
        index, path, "set path does not point to a table key or array index"
    )


def _delete_value(
    parent: Any,
    fragment: PathFragment,
    path: tuple[PathFragment, ...],
    index: int,
) -> None:
    if isinstance(parent, (Array, AoT)):
        if not isinstance(fragment, int) or isinstance(fragment, bool):
            raise BatchError(index, path, "array indexes must be ints")
        try:
            del parent[fragment]
        except IndexError as ex:
            raise BatchError(index, path, str(ex)) from ex
    elif isinstance(
        parent, (Container, Table, InlineTable, OutOfOrderTableProxy)
    ) and isinstance(fragment, str):
        try:
            del parent[fragment]
        except KeyError as ex:
            message = str(ex.args[0]) if ex.args else "key not found"
            raise BatchError(index, path, message)
    else:
        raise BatchError(
            index, path, "delete path does not point to a table key or array index"
        )


def _unwrap(value: Any) -> Any:
    if isinstance(value, Item):
        return value.unwrap()
    return value


def _toml_equal(actual: Any, expected: Any) -> bool:
    actual = _unwrap(actual)
    expected = _unwrap(expected)

    if expected is None or isinstance(
        expected, (bytes, bytearray, set, frozenset)
    ):
        return False

    actual_is_bool = isinstance(actual, bool)
    expected_is_bool = isinstance(expected, bool)
    if actual_is_bool or expected_is_bool:
        return actual_is_bool and expected_is_bool and actual == expected

    if isinstance(actual, int) or isinstance(expected, int):
        return (
            type(actual) is int
            and type(expected) is int
            and actual == expected
        )

    if isinstance(actual, float) or isinstance(expected, float):
        return type(actual) is float and type(expected) is float and actual == expected

    if isinstance(actual, Mapping) or isinstance(expected, Mapping):
        if not (isinstance(actual, Mapping) and isinstance(expected, Mapping)):
            return False
        if set(actual) != set(expected):
            return False
        return all(_toml_equal(actual[key], expected[key]) for key in actual)

    if isinstance(actual, (list, tuple)) or isinstance(expected, (list, tuple)):
        if not (
            isinstance(actual, (list, tuple))
            and isinstance(expected, (list, tuple))
        ):
            return False
        return len(actual) == len(expected) and all(
            _toml_equal(left, right) for left, right in zip(actual, expected)
        )

    for value_type in (datetime, date, time):
        if isinstance(actual, value_type) or isinstance(expected, value_type):
            return type(actual) is type(expected) and actual == expected

    if actual is None or isinstance(actual, (bytes, bytearray, set, frozenset)):
        return False

    return (
        type(actual) is type(expected) and actual == expected
    )


def _resolve(
    root: Container, path: tuple[PathFragment, ...], index: int
) -> Any:
    current: Any = root
    for depth, fragment in enumerate(path):
        current_path = path[: depth + 1]
        current = _get_child(current, fragment, index, current_path)
    return current


def batch(document: Container, operations: Sequence[Operation]) -> Container:
    """Apply operations atomically and return the updated document.

    Supported operations are ``set``, ``delete``, ``append`` and ``test``.
    They run on a deep copy, so failure leaves the original document and any
    previously obtained container views unchanged. On success, the document
    object keeps its identity but replaces its internal state atomically;
    older nested views remain accessible as independent pre-batch snapshots,
    while new views must be fetched from the returned document.
    """
    working = copy.deepcopy(document)

    for index, raw_operation in enumerate(operations):
        path: tuple[PathFragment, ...] = ()
        try:
            name, raw_path, value = _coerce_operation(raw_operation, index)
            path = _normalize_path(raw_path, index)
            if name == "test":
                target = _resolve(working, path, index)
                if not _toml_equal(target, value):
                    raise BatchError(index, path, "test failed")
            else:
                parent, fragment = _navigate(
                    working,
                    path,
                    index,
                    create_missing_tables=name == "set",
                )
                if name == "set":
                    _set_value(parent, fragment, value, path, index)
                elif name == "delete":
                    _delete_value(parent, fragment, path, index)
                else:
                    target = _resolve(working, path, index)
                    if not isinstance(target, (Array, AoT)):
                        raise BatchError(
                            index, path, "append path must point to an array"
                        )
                    if isinstance(target, AoT):
                        target.append(_aot_value(value, path, index))
                    else:
                        target.append(_convert_value(value, target))
        except BatchError:
            raise
        except Exception as ex:
            raise BatchError(index, path, str(ex)) from ex

    document._replace_state(working)
    return document


def apply_batch(document: Container, operations: Sequence[Operation]) -> Container:
    """Alias for :func:`batch`."""
    return batch(document, operations)
