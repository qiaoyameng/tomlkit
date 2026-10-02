from __future__ import annotations

import copy
import math

from collections.abc import Iterator
from collections.abc import Sequence
from typing import TYPE_CHECKING
from typing import Any


if TYPE_CHECKING:
    from typing import Self

from tomlkit._compat import decode
from tomlkit._types import _CustomDict
from tomlkit._utils import merge_dicts
from tomlkit.exceptions import KeyAlreadyPresent
from tomlkit.exceptions import InvalidKeyOrderError
from tomlkit.exceptions import NonExistentKey
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import AoT
from tomlkit.items import Array
from tomlkit.items import Comment
from tomlkit.items import InlineTable
from tomlkit.items import Item
from tomlkit.items import Key
from tomlkit.items import Null
from tomlkit.items import SingleKey
from tomlkit.items import Table
from tomlkit.items import Trivia
from tomlkit.items import Whitespace
from tomlkit.items import item as _item


_NOT_SET = object()


def _normalize_key_order(key_order: Sequence[str]) -> list[str]:
    order = list(key_order)
    if any(not isinstance(key, str) for key in order):
        raise TypeError("Key order must contain strings")
    if len(order) != len(set(order)):
        raise ValueError("Key order must not contain duplicate keys")
    return order


def _parse_key_path(path: str) -> list[str]:
    from tomlkit.parser import Parser

    try:
        key = Parser(f"{path} = 0")._parse_key_value(False)[0]
    except TOMLKitError:
        raise NonExistentKey(path)
    return [part.key for part in key]


def _first_key_part(key: Key) -> str:
    return next(iter(key)).key


def _contains_blank_line(item: Item) -> bool:
    if isinstance(item, Whitespace):
        return item.s.count("\n") >= 1
    return item.trivia.trail.count("\n") >= 2


def _split_trivia_run(
    run: list[tuple[Key | None, Item]],
) -> tuple[list[tuple[Key | None, Item]], list[tuple[Key | None, Item]]]:
    for index, (_, item) in enumerate(run):
        if isinstance(item, Whitespace) and _contains_blank_line(item):
            return run[: index + 1], run[index + 1 :]
    return [], run


def _build_groups(
    body: list[tuple[Key | None, Item]],
) -> tuple[
    list[tuple[Key | None, Item]],
    list[dict[str, Any]],
    list[tuple[Key | None, Item]],
]:
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    first_key_index = next(
        (index for index, (key, _) in enumerate(body) if key is not None),
        len(body),
    )
    header_end = first_key_index
    leading = body[:header_end]

    for index, (key, item) in enumerate(body):
        if index < header_end:
            continue
        if key is None:
            continue

        run_start = 0 if current is None else current["end"]
        run = body[run_start:index]
        before: list[tuple[Key | None, Item]] = []
        extracted_indent: list[tuple[Key | None, Item]] = []
        if current is not None:
            leading_item = item.body[0] if isinstance(item, AoT) else item
            if isinstance(leading_item, Table):
                indent = leading_item.trivia.indent
                newline_count = len(indent) - len(indent.lstrip("\n"))
                if newline_count:
                    extracted_indent = [
                        (None, Whitespace(indent[:newline_count]))
                    ]
                    leading_item.trivia.indent = indent[newline_count:]
            after, before = _split_trivia_run(run)
            current["after"].extend(after)
            before = extracted_indent + before

        current = {
            "key": key,
            "item": item,
            "start": index,
            "end": index + 1,
            "before": list(before),
            "after": [],
        }
        groups.append(current)

    if current is None:
        return body, [], []

    final_run = body[current["end"] :]
    after, before = _split_trivia_run(final_run)
    current["after"].extend(after)
    return leading, groups, before


def _flatten_groups(
    leading: list[tuple[Key | None, Item]],
    groups: list[dict[str, Any]],
    final: list[tuple[Key | None, Item]],
) -> list[tuple[Key | None, Item]]:
    body = list(leading)
    for group in groups:
        before = list(group["before"])
        after = list(group["after"])
        body.extend(before)
        body.append((group["key"], group["item"]))
        body.extend(after)
    body.extend(final)
    return body


def _reposition_plain_blank_lines(groups: list[dict[str, Any]]) -> None:
    if not groups:
        return
    blank_by_target: dict[int, list[tuple[Key | None, Item]]] = groups[0].get(
        "boundary_blanks", {}
    )
    for index, group in enumerate(groups):
        if index in blank_by_target:
            group["after"].extend(blank_by_target[index])


def _index_body(container: Container, body: list[tuple[Key | None, Item]]) -> None:
    container._map.clear()
    container._table_keys.clear()
    container._out_of_order_keys.clear()
    container._validation_cache.clear()

    for index, (key, item) in enumerate(body):
        if key is None:
            continue
        if key in container._map:
            current = container._map[key]
            indices = current if isinstance(current, tuple) else (current,)
            container._map[key] = (*indices, index)
            container._out_of_order_keys.add(key)
        else:
            container._map[key] = index

        if isinstance(item, AoT):
            item._parent_container = container
            item._parent_key = key
            item.parent_item = None

        if isinstance(item, (Table, InlineTable)):
            item.parent_item = container.parent_item

        if item.is_table():
            container._table_keys.append(key)


def _sort_item(item: Item, order: list[str], recursive: bool) -> None:
    if isinstance(item, (Table, InlineTable)):
        item.value.parent_item = item
        _sort_container(item.value, order, recursive, validate_keys=False)
    elif isinstance(item, AoT):
        for table in item.body:
            table.value.parent_item = item
            _sort_container(
                table.value,
                order,
                recursive,
                sort_body=True,
                validate_keys=False,
                pin_trailing_blank=True,
            )
    elif recursive and isinstance(item, Array):
        for value in item._value:
            if value.value is not None:
                _sort_item(value.value, order, recursive)


def _rendered_group_key(key: Key) -> str:
    if not key.is_dotted():
        return _first_key_part(key)
    return ".".join(part.key for part in key)


def _first_item_path(key: Key, item: Item) -> str:
    rendered = _rendered_group_key(key)
    if isinstance(item, Table) and key.is_dotted():
        for child_key, child_item in item.value.body:
            if child_key is not None:
                suffix = _first_item_path(child_key, child_item)
                return f"{_first_key_part(key)}.{suffix}"
    return rendered


def _group_rank(group: dict[str, Any], order: list[str]) -> tuple[int, int, int]:
    rendered_key = _first_item_path(group["key"], group["item"])
    direct_key = _first_key_part(group["key"])
    if rendered_key in order:
        return 0, order.index(rendered_key), group["original_index"]
    if not group["key"].is_dotted() and direct_key in order:
        return 0, order.index(direct_key), group["original_index"]
    return 1, group["first_seen"], group["original_index"]


def _first_fragment_key(table: Table) -> str | None:
    for key, item in table.value.body:
        if key is not None:
            return _first_item_path(key, item)
    return None


def _sort_fragment_groups(
    groups: list[dict[str, Any]],
    logical_key: Key,
    order: list[str],
) -> None:
    positions = [
        index
        for index, group in enumerate(groups)
        if group["key"] == logical_key and isinstance(group["item"], Table)
    ]
    if len(positions) < 2:
        return

    fragments = [groups[index] for index in positions]
    ranked = []
    unspecified = []
    for index, group in enumerate(fragments):
        direct_key = _first_fragment_key(group["item"])
        if direct_key is not None and direct_key in order:
            ranked.append((order.index(direct_key), index, group))
        else:
            unspecified.append((index, group))

    ordered = [group for _, _, group in sorted(ranked)]
    ordered.extend(group for _, group in unspecified)
    if ordered:
        first_original = groups[positions[0]]["item"]
        assert isinstance(first_original, Table)
        first_ordered = ordered[0]["item"]
        assert isinstance(first_ordered, Table)
        first_ordered.trivia.indent = first_original.trivia.indent
    for position, group in zip(positions, ordered):
        groups[position] = group


def _sort_container(
    container: Container,
    order: list[str],
    recursive: bool = True,
    sort_body: bool = True,
    validate_keys: bool = True,
    pin_trailing_blank: bool = False,
) -> None:
    leading, groups, final = _build_groups(container._body)

    for group in groups:
        group["original_index"] = group["start"]
        if recursive:
            _sort_item(group["item"], order, recursive)

    direct_keys: list[str] = []
    for group in groups:
        direct_key = _first_key_part(group["key"])
        if direct_key not in direct_keys:
            direct_keys.append(direct_key)
        group["first_seen"] = direct_keys.index(direct_key)

    if sort_body:
        boundary_blanks: dict[int, list[tuple[Key | None, Item]]] = {}
        if not pin_trailing_blank and not isinstance(
            container.parent_item, InlineTable
        ):
            for index, group in enumerate(groups[:-1]):
                after = group["after"]
                for item_index, (_, item) in enumerate(after):
                    if isinstance(item, Whitespace) and _contains_blank_line(item):
                        boundary_blanks[index] = after[item_index:]
                        group["after"] = after[:item_index]
                        break

        ranked_groups = sorted(
            enumerate(groups),
            key=lambda entry: _group_rank(entry[1], order),
        )
        groups[:] = [group for _, group in ranked_groups]
        for index, group in enumerate(groups):
            group["sorted_index"] = index
            group["boundary_blanks"] = boundary_blanks

        if pin_trailing_blank:
            trailing: list[tuple[Key | None, Item]] = []
            for group in groups:
                after = group["after"]
                for item_index, (_, item) in enumerate(after):
                    if isinstance(item, Whitespace) and _contains_blank_line(item):
                        trailing.extend(after[item_index:])
                        group["after"] = after[:item_index]
                        break
            groups[-1]["after"].extend(trailing)

        if isinstance(container.parent_item, InlineTable):
            for index, group in enumerate(groups):
                group["before"] = []
                group["after"] = (
                    [(None, Whitespace(", "))]
                    if index < len(groups) - 1
                    else []
                )
        elif not pin_trailing_blank:
            _reposition_plain_blank_lines(groups)

        for key, index in container._map.items():
            if isinstance(index, tuple):
                _sort_fragment_groups(groups, key, order)

    container._body = _flatten_groups(
        leading, groups, final
    )
    _index_body(container, container._body)


def _sort_proxy_container(
    owner: Container, indices: tuple[int, ...], order: list[str], recursive: bool
) -> None:
    logical_key = next(
        key for key, value in owner._map.items() if value == indices
    )
    leading, groups, final = _build_groups(owner._body)

    fragment_groups: list[tuple[int, dict[str, Any]]] = []
    for index, group in enumerate(groups):
        if group["key"] == logical_key and isinstance(group["item"], Table):
            fragment_groups.append((index, group))

    for _, group in fragment_groups:
        if recursive:
            _sort_item(group["item"], order, recursive)
        else:
            group["item"].value.parent_item = group["item"]
            _sort_container(
                group["item"].value,
                order,
                False,
                sort_body=True,
                validate_keys=False,
            )

    positions = [index for index, _ in fragment_groups]

    def trailing_blank(table: Table) -> Whitespace | None:
        last_key_index = max(
            (
                index
                for index, (key, _) in enumerate(table.value.body)
                if key is not None
            ),
            default=-1,
        )
        for index in range(len(table.value.body) - 1, last_key_index, -1):
            key, item = table.value.body[index]
            if isinstance(item, Whitespace):
                del table.value.body[index]
                return item
        if last_key_index >= 0:
            last_item = table.value.body[last_key_index][1]
            if last_item.trivia.trail.count("\n") > 1:
                last_item.trivia.trail = last_item.trivia.trail[:-1]
                return Whitespace("\n")
        return None

    boundary_blanks: dict[int, Whitespace] = {}
    boundary_targets: dict[int, int] = {}
    for position in positions:
        boundary_position = position + 1
        while (
            boundary_position < len(groups)
            and groups[boundary_position]["key"] == logical_key
        ):
            boundary_position += 1
        if boundary_position >= len(groups):
            continue

        boundary_table = groups[boundary_position]["item"]
        if not isinstance(boundary_table, Table):
            continue
        source_table = groups[position]["item"]
        blank = None
        if boundary_position not in positions and isinstance(source_table, Table):
            blank = trailing_blank(source_table)
        if blank is None:
            blank = trailing_blank(boundary_table)
        if blank is not None:
            boundary_blanks[boundary_position] = blank
            boundary_targets[boundary_position] = position

    ranked = []
    unspecified = []
    for original_index, (_, group) in enumerate(fragment_groups):
        direct_key = _first_fragment_key(group["item"])
        if direct_key is not None and direct_key in order:
            ranked.append((order.index(direct_key), original_index, group))
        else:
            unspecified.append((original_index, group))

    ordered = [group for _, _, group in sorted(ranked)]
    ordered.extend(group for _, group in unspecified)
    original_positions = list(positions)
    for position, group in zip(original_positions, ordered):
        groups[position] = group
        table = group["item"]
        if isinstance(table, Table) and position in boundary_targets.values():
            source_boundary = next(
                boundary
                for boundary, target in boundary_targets.items()
                if target == position
            )
            if source_boundary not in positions:
                table.value.body.append(
                    (None, boundary_blanks[source_boundary])
                )
        elif isinstance(table, Table) and position in boundary_blanks:
            table.trivia.indent = (
                boundary_blanks[position].s + table.trivia.indent
            )

    owner._body = _flatten_groups(leading, groups, final)
    _index_body(owner, owner._body)


def _container_key_strings(container: Container) -> set[str]:
    keys = {key.key for key in container._map}
    for key, _ in container._body:
        if key is not None:
            keys.add(_rendered_group_key(key))
    return keys


def _item_contains_key(item: Item, parts: list[str], recursive: bool) -> bool:
    if not parts:
        return True
    key = parts[0]
    if isinstance(item, (Table, InlineTable)):
        return _container_contains_path(item.value, parts, recursive)
    if isinstance(item, AoT):
        return any(
            _container_contains_path(table.value, parts, recursive)
            for table in item.body
        )
    if recursive and isinstance(item, Array):
        return any(
            group.value is not None
            and _item_contains_key(group.value, parts, recursive)
            for group in item._value
        )
    return False


def _container_contains_path(
    container: Container, parts: list[str], recursive: bool
) -> bool:
    key = parts[0]
    try:
        value = container.item(key)
    except NonExistentKey:
        value = None

    if value is not None:
        if len(parts) == 1:
            return True
        if isinstance(value, OutOfOrderTableProxy):
            return _container_contains_path(
                value._internal_container, parts[1:], recursive
            )
        if isinstance(value, Item):
            return _item_contains_key(value, parts[1:], recursive)

    if recursive:
        return any(
            _item_contains_key(item, parts, recursive)
            for body_key, item in container.body
            if body_key is not None
        )
    return False


def _ensure_order_keys_exist(
    container: Container, order: list[str], recursive: bool = False
) -> None:
    for key in order:
        if key in _container_key_strings(container):
            continue

        parts = _parse_key_path(key)
        if not _container_contains_path(container, parts, recursive):
            raise NonExistentKey(key)


def _invalidate_display_names(container: Container) -> None:
    for _, item in container.body:
        if isinstance(item, (Table, AoT)):
            item.invalidate_display_name()


def _assert_container_order(
    container: Container,
    order: list[str],
    recursive: bool,
    validate_keys: bool = True,
) -> None:
    keys: list[str] = []
    for key, item in container.body:
        if key is None:
            continue
        rendered = _first_item_path(key, item)
        if rendered not in keys:
            keys.append(rendered)

    selected = [key for key in keys if key in order]
    expected = [key for key in order if key in selected]
    if selected != expected:
        raise InvalidKeyOrderError(
            "Requested key order cannot be represented without changing semantics"
        )

    if recursive:
        for _, item in container.body:
            _assert_item_order(item, order, validate_keys)


def _assert_item_order(
    item: Item, order: list[str], validate_keys: bool = True
) -> None:
    if isinstance(item, (Table, InlineTable)):
        _assert_container_order(item.value, order, True, validate_keys)
    elif isinstance(item, AoT):
        for table in item.body:
            _assert_container_order(
                table.value, order, True, validate_keys
            )
    elif isinstance(item, Array):
        for group in item._value:
            if group.value is not None:
                _assert_item_order(group.value, order, validate_keys)


def _validate_container(
    container: Container,
    order: list[str],
    recursive: bool,
    validate_keys: bool = True,
    inline: bool = False,
) -> None:
    from tomlkit.parser import Parser
    from tomlkit.toml_document import TOMLDocument

    validation_container = container
    standalone = not isinstance(container, TOMLDocument)
    if standalone:
        validation_container = Container(True)
        wrapper: Item
        if inline:
            wrapper = InlineTable(container, Trivia(), new=False)
        else:
            wrapper = Table(container, Trivia(), False, is_super_table=False)
        validation_container._raw_append(SingleKey("__tomlkit_sort__"), wrapper)

    _invalidate_display_names(container)
    rendered = validation_container.as_string()
    try:
        reparsed = Parser(rendered).parse()
    except TOMLKitError as exc:
        raise InvalidKeyOrderError(str(exc)) from exc

    if reparsed.unwrap() != validation_container.unwrap():
        raise InvalidKeyOrderError("Requested key order changes the TOML value")
    if standalone:
        _assert_container_order(container, order, recursive, validate_keys)
    else:
        _assert_container_order(reparsed, order, recursive, validate_keys)


def _keyed_entries(
    container: Container,
) -> dict[str, list[tuple[Key, Item]]]:
    entries: dict[str, list[tuple[Key, Item]]] = {}
    for key, item in container.body:
        if key is not None:
            entries.setdefault(key.key, []).append((key, item))
    return entries


def _collect_items(container: Container) -> list[Any]:
    objects: list[Any] = [container]
    for _, item in container.body:
        objects.append(item)
        if isinstance(item, (Table, InlineTable)):
            objects.extend(_collect_items(item.value))
        elif isinstance(item, AoT):
            for table in item.body:
                objects.append(table)
                objects.extend(_collect_items(table.value))
        elif isinstance(item, Array):
            for group in item._value:
                if group.value is not None:
                    nested_items = _collect_item(group.value)
                    objects.extend(nested_items)
    return objects


def _collect_item(item: Item) -> list[Any]:
    objects: list[Any] = [item]
    if isinstance(item, (Table, InlineTable)):
        objects.extend(_collect_items(item.value))
    elif isinstance(item, AoT):
        for table in item.body:
            objects.append(table)
            objects.extend(_collect_items(table.value))
    elif isinstance(item, Array):
        for group in item._value:
            if group.value is not None:
                objects.extend(_collect_item(group.value))
    return objects


def _source_map(original: Any, memo: dict[int, Any]) -> dict[int, Item | Container]:
    containers = (
        _collect_items(original)
        if isinstance(original, Container)
        else _collect_item(original)
    )
    return {
        id(memo[id(source)]): source
        for source in containers
        if id(source) in memo
    }


def _adopt_item(original: Item, candidate: Item) -> None:
    if hasattr(candidate, "_trivia"):
        original._trivia = candidate._trivia

    if isinstance(original, (Table, InlineTable)) and isinstance(
        candidate, (Table, InlineTable)
    ):
        _adopt_container(original.value, candidate.value)
        if isinstance(original, Table) and isinstance(candidate, Table):
            original.name = candidate.name
            original.display_name = candidate.display_name
            original._is_aot_element = candidate._is_aot_element
            original._is_super_table = candidate._is_super_table
        elif isinstance(original, InlineTable):
            original._new = candidate._new
    elif isinstance(original, AoT) and isinstance(candidate, AoT):
        original._body = candidate._body
        original._parsed = candidate._parsed
        list.clear(original)
        list.extend(original, candidate._body)
        for original_table, candidate_table in zip(original.body, candidate.body):
            _adopt_item(original_table, candidate_table)
    elif isinstance(original, Array) and isinstance(candidate, Array):
        original._value = candidate._value
        original._index_map = dict(candidate._index_map)
        original._multiline = candidate._multiline
        list.clear(original)
        list.extend(original, candidate)


def _adopt_container(
    original: Container,
    candidate: Container,
    source_map: dict[int, Item | Container] | None = None,
) -> None:
    item_by_id = {
        id(item): (key, item)
        for key, item in original.body
        if key is not None
    }
    used: dict[str, int] = {}
    body: list[tuple[Key | None, Item]] = []

    for candidate_key, candidate_item in candidate.body:
        if candidate_key is None:
            body.append((None, candidate_item))
            continue

        mapped = source_map.get(id(candidate_item)) if source_map else None
        if mapped is not None and isinstance(mapped, Item):
            original_item = mapped
            original_key, _ = item_by_id[id(original_item)]
        else:
            original_entries = _keyed_entries(original)
            occurrence = used.get(candidate_key.key, 0)
            used[candidate_key.key] = occurrence + 1
            original_key, original_item = original_entries[candidate_key.key][
                occurrence
            ]
        _adopt_item(original_item, candidate_item)
        body.append((original_key, original_item))

    original._body = body
    original._parsed = candidate._parsed
    _index_body(original, body)

    dict.clear(original)
    for key, item in body:
        if key is not None:
            dict.__setitem__(original, key.key, item.value)


class Container(_CustomDict):  # type: ignore[type-arg]
    """
    A container for items within a TOMLDocument.

    This class implements the `dict` interface with copy/deepcopy protocol.
    """

    def __init__(self, parsed: bool = False) -> None:
        self._map: dict[Key, int | tuple[int, ...]] = {}
        self._body: list[tuple[Key | None, Item]] = []
        self._parsed = parsed
        self._table_keys: list[Key] = []
        # number of already-validated fragments and the temp container they
        # were merged into, per out-of-order key; lets parse-time validation
        # resume where the previous pass stopped instead of re-merging every
        # fragment (quadratic) on each append
        self._validation_cache: dict[Key, tuple[int, Container]] = {}
        # superset of the keys mapped to an index tuple, so validating all
        # out-of-order tables doesn't have to scan every key in the map;
        # stale entries are filtered by the per-key isinstance check
        self._out_of_order_keys: set[Key] = set()
        self.parent_item: Item | None = None

    @property
    def body(self) -> list[tuple[Key | None, Item]]:
        return self._body

    def unwrap(self) -> dict[str, Any]:
        """Returns as pure python object (ppo)"""
        unwrapped: dict[str, Any] = {}
        # Resolve each key straight from _map, which already holds the parsed
        # Key objects and their body index, instead of via self.items(): the
        # inherited MutableMapping iteration goes through __getitem__, which
        # rebuilds a SingleKey from the bare string on every key only to throw
        # it away. Out-of-order keys (a tuple index) still go through
        # OutOfOrderTableProxy so their validation (and fragment merge) runs
        # exactly as before. _map is keyed for lookup, not ordered: replacing a
        # value re-inserts its key and moves it last, so take the order from the
        # body index, which is what dumps() and items() follow.
        for key, idx in sorted(
            self._map.items(),
            key=lambda item: item[1][0] if isinstance(item[1], tuple) else item[1],
        ):
            if isinstance(idx, tuple):
                value: Any = OutOfOrderTableProxy(self, idx)
            else:
                value = self._body[idx][1]
            unwrapped[key.key] = value.unwrap() if hasattr(value, "unwrap") else value

        return unwrapped

    @property
    def value(self) -> dict[str, Any]:
        """The wrapped dict value"""
        d: dict[str, Any] = {}
        for k, v in self._body:
            if k is None:
                continue

            key_str = k.key
            val: Any = v.value

            if isinstance(val, Container):
                val = val.value

            if key_str in d:
                merge_dicts(d[key_str], val)
            else:
                d[key_str] = val

        return d

    def parsing(self, parsing: bool) -> None:
        self._parsed = parsing
        self._validation_cache.clear()

        for _, v in self._body:
            if isinstance(v, Table):
                v.value.parsing(parsing)
            elif isinstance(v, AoT):
                for t in v.body:
                    t.value.parsing(parsing)

    def add(self, key: Key | Item | str, item: Any = None) -> Container:
        """
        Adds an item to the current Container.

        :Example:

        >>> # add a key-value pair
        >>> doc.add('key', 'value')
        >>> # add a comment or whitespace or newline
        >>> doc.add(comment('# comment'))
        """
        if item is None:
            if not isinstance(key, (Comment, Whitespace)):
                raise ValueError(
                    "Non comment/whitespace items must have an associated key"
                )

            return self.append(None, key)

        assert not isinstance(key, Item)
        return self.append(key, item)

    def _handle_dotted_key(self, key: Key, value: Item) -> None:
        if isinstance(value, (Table, AoT)):
            raise TOMLKitError("Can't add a table to a dotted key")
        name, *mid, last = key
        name._dotted = True
        table = current = Table(Container(True), Trivia(), False, is_super_table=True)
        for _name in mid:
            _name._dotted = True
            new_table = Table(Container(True), Trivia(), False, is_super_table=True)
            current.append(_name, new_table)
            current = new_table

        last.sep = key.sep
        current.append(last, value)

        self.append(name, table)
        return

    def _get_last_index_before_table(self) -> int:
        last_index = -1
        for i, (k, v) in enumerate(self._body):
            if isinstance(v, Null):
                continue  # Null elements are inserted after deletion

            if isinstance(v, Whitespace) and not v.is_fixed():
                continue

            if isinstance(v, (Table, AoT)) and k is not None and not k.is_dotted():
                break

            if (
                isinstance(v, Table)
                and k is not None
                and k.is_dotted()
                and self._renders_table_header(v)
            ):
                # A dotted-key super table renders inline (`a.b = 1`) only as
                # long as none of its children render a `[table]` header; once
                # one does, anything appended after it would land inside that
                # table's scope.
                break
            last_index = i
        return last_index + 1

    def _renders_table_header(self, table: Table) -> bool:
        for k, v in table.value.body:
            if isinstance(v, AoT):
                return True
            if isinstance(v, Table):
                if k is not None and k.is_dotted() and v.is_super_table():
                    if self._renders_table_header(v):
                        return True
                else:
                    return True
        return False

    def _validate_out_of_order_table(self, key: Key | None = None) -> None:
        if key is None:
            for k in list(self._out_of_order_keys):
                assert k is not None
                self._validate_out_of_order_table(k)
            return
        if key not in self._map:
            return
        current_idx = self._map[key]
        if not isinstance(current_idx, tuple):
            return
        if self._parsed:
            # while parsing, every fragment appended to an out-of-order key
            # triggers a validation pass; resume from the cached temp
            # container so each fragment is merged (and deep-copied) once
            # instead of on every later pass. Fragments are only ever
            # appended during parsing, so a count prefix stays valid; any
            # other mutation clears the cache.
            validated, temp = self._validation_cache.get(key, (0, None))
            if validated > len(current_idx):
                validated, temp = 0, None
            try:
                temp = OutOfOrderTableProxy.validate(
                    self, current_idx[validated:], temp
                )
            except Exception:
                # the temp container may be partially mutated; don't let a
                # caught-and-retried failure resume from a poisoned cache
                self._validation_cache.pop(key, None)
                raise
            self._validation_cache[key] = (len(current_idx), temp)
            return
        OutOfOrderTableProxy.validate(self, current_idx)

    def append(
        self, key: Key | str | None, item: Any, validate: bool = True
    ) -> Container:
        """Similar to :meth:`add` but both key and value must be given."""
        if not isinstance(key, Key) and key is not None:
            key = SingleKey(key)

        if not isinstance(item, Item):
            item = _item(item)

        if key is not None and key.is_multi():
            self._handle_dotted_key(key, item)
            return self

        if isinstance(item, (AoT, Table)) and item.name is None:
            assert isinstance(key, Key)
            item.name = key.key

        prev = self._previous_item()
        prev_ws = isinstance(prev, Whitespace) or ends_with_whitespace(prev)
        if isinstance(item, Table):
            if not self._parsed:
                item.invalidate_display_name()
            if (
                self._body
                and not (self._parsed or item.trivia.indent or prev_ws)
                and key is not None
                and not key.is_dotted()
            ):
                item.trivia.indent = "\n"

        if isinstance(item, AoT) and self._body and not self._parsed:
            item.invalidate_display_name()
            if item and not ("\n" in item[0].trivia.indent or prev_ws):
                item[0].trivia.indent = "\n" + item[0].trivia.indent

        if key is not None and key in self:
            current_idx = self._map[key]
            if isinstance(current_idx, tuple):
                current_body_element = self._body[current_idx[-1]]
            else:
                current_body_element = self._body[current_idx]

            current = current_body_element[1]

            if isinstance(item, Table):
                if not isinstance(current, (Table, AoT)):
                    raise KeyAlreadyPresent(key)

                if item.is_aot_element():
                    # New AoT element found later on
                    # Adding it to the current AoT
                    if not isinstance(current, AoT):
                        current = AoT([current, item], parsed=self._parsed)

                        self._replace(key, key, current)
                    else:
                        current.append(item)

                    return self
                elif isinstance(current, AoT):
                    if not item.is_aot_element():
                        if item.is_super_table() and len(current.body):
                            # A sub-table header such as `[fruit.apple.texture]`
                            # appearing after the array `[[fruit]]` (possibly with
                            # unrelated tables in between) extends the last element
                            # of the array, per the TOML spec.
                            last = current[-1]
                            for k, v in item.value.body:
                                last.value.append(k, v)

                            return self
                        # Tried to define a table after an AoT with the same name.
                        raise KeyAlreadyPresent(key)

                    current.append(item)

                    return self
                elif current.is_super_table():
                    if item.is_super_table():
                        # We need to merge both super tables
                        if (
                            key.is_dotted()
                            or (
                                current_body_element[0] is not None
                                and current_body_element[0].is_dotted()
                            )
                            or self._table_keys[-1] != current_body_element[0]
                        ):
                            if key.is_dotted() and not self._parsed:
                                idx = self._get_last_index_before_table()
                            else:
                                idx = len(self._body)

                            if idx < len(self._body):
                                self._insert_at(idx, key, item)
                            else:
                                self._raw_append(key, item)

                            if validate:
                                self._validate_out_of_order_table(key)

                            return self

                        # Merge the new super table's body into the existing one
                        # in place. Previously this deep-copied `current` before
                        # appending, which is O(size of current) on every merge
                        # and therefore O(n^2) when many subtables share a super
                        # table (e.g. consecutive `[a.b.c]` / `[a.b.d]` headers).
                        # Mutating in place is O(1) per merge. The defensive copy
                        # that protected the out-of-order validation pass has been
                        # moved into OutOfOrderTableProxy (its only consumer).
                        for k, v in item.value.body:
                            current.append(k, v)

                        return self
                    elif (
                        current_body_element[0] is not None
                        and current_body_element[0].is_dotted()
                    ):
                        raise TOMLKitError("Redefinition of an existing table")
                    else:
                        # Merging a concrete table into an existing implicit/super
                        # table is only valid if it does not redefine existing
                        # subtrees via dotted keys and does not change prior types.
                        assert isinstance(current, Table)
                        self._validate_table_candidate(current, item)
                elif not item.is_super_table():
                    raise KeyAlreadyPresent(key)
                else:
                    # An existing concrete table (current) is being extended by
                    # a super-table (item) — e.g. [a] b=1 then [a.b] c=2 out of
                    # order, or [a] b.c=1 then [a.b] d=2.  Validate that the
                    # super-table does not redefine any existing key, raising
                    # early at parse time.  When validation passes, fall through
                    # — _raw_append below will create an out-of-order entry and
                    # preserve table ordering in the document.
                    assert isinstance(current, Table)
                    self._validate_table_candidate(current, item)
            elif isinstance(item, AoT):
                if not isinstance(current, AoT):
                    # Tried to define an AoT after a table with the same name.
                    raise KeyAlreadyPresent(key)

                for table in item.body:
                    current.append(table)

                return self
            else:
                raise KeyAlreadyPresent(key)

        is_table = isinstance(item, (Table, AoT))
        if (
            key is not None
            and self._body
            and not self._parsed
            and (not is_table or key.is_dotted())
        ):
            # If there is already at least one table in the current container
            # and the given item is not a table, we need to find the last
            # item that is not a table and insert after it
            # If no such item exists, insert at the top of the table
            last_index = self._get_last_index_before_table()

            if last_index < len(self._body):
                after_item = self._body[last_index][1]
                if not (
                    isinstance(after_item, Whitespace)
                    or "\n" in after_item.trivia.indent
                ):
                    after_item.trivia.indent = "\n" + after_item.trivia.indent
                return self._insert_at(last_index, key, item)
            else:
                previous_item = self._body[-1][1]
                if isinstance(previous_item, Table) and previous_item.is_super_table():
                    previous_child = previous_item.value._previous_item()
                    if (
                        previous_child is not None
                        and not isinstance(previous_child, Whitespace)
                        and "\n" in previous_item.trivia.trail
                        and "\n" not in previous_child.trivia.trail
                    ):
                        previous_child.trivia.trail += previous_item.trivia.trail
                if not (
                    isinstance(previous_item, Whitespace)
                    or ends_with_whitespace(previous_item)
                    or "\n" in previous_item.trivia.trail
                ):
                    previous_item.trivia.trail += "\n"

        self._raw_append(key, item)
        if validate and key is not None:
            self._validate_out_of_order_table(key)
        return self

    def _validate_table_candidate(self, current: Table, candidate: Table) -> None:
        for k, v in candidate.value.body:
            if k is None:
                continue

            if k in current.value._map:
                existing = current.value.item(k)
                if isinstance(existing, (Table, AoT)) != isinstance(v, (Table, AoT)):
                    raise KeyAlreadyPresent(k)
                if k.is_dotted():
                    raise TOMLKitError("Redefinition of an existing table")
                if isinstance(existing, Table) and isinstance(v, Table):
                    if not existing.is_super_table() and not v.is_super_table():
                        # Both sides are concrete `[table]` definitions of the
                        # same name; the table is declared twice.
                        raise KeyAlreadyPresent(k)
                    # One side is still an implicit/super table, so a duplicate
                    # (if any) is nested deeper - keep checking the subtree.
                    self._validate_table_candidate(existing, v)
                continue

            if not k.is_dotted():
                # Even when the candidate key itself is not dotted, an
                # existing dotted key may already use it as a prefix —
                # e.g.  [a] b.c=1 then [a.b] d=2  (b prefixes b.c).
                for existing_key in current.value._map:
                    if existing_key.is_dotted() and next(iter(existing_key)) == k:
                        raise TOMLKitError("Redefinition of an existing table")
                continue

            head = next(iter(k))
            if head in current.value._map:
                raise TOMLKitError("Redefinition of an existing table")

    def _raw_append(self, key: Key | None, item: Item) -> None:
        if key is not None and key in self._map:
            current_idx = self._map[key]
            if not isinstance(current_idx, tuple):
                current_idx = (current_idx,)

            current = self._body[current_idx[-1]][1]
            if not isinstance(current, Table):
                raise KeyAlreadyPresent(key)

            self._map[key] = (*current_idx, len(self._body))
            self._out_of_order_keys.add(key)
        elif key is not None:
            self._map[key] = len(self._body)

        self._body.append((key, item))
        if item.is_table() and key is not None:
            self._table_keys.append(key)

        if key is not None:
            dict.__setitem__(self, key.key, item.value)

    def _remove_at(self, idx: int) -> None:
        key = self._body[idx][0]
        assert key is not None
        index = self._map.get(key)
        if index is None:
            raise NonExistentKey(key)
        self._validation_cache.clear()
        self._body[idx] = (None, Null())

        if isinstance(index, tuple):
            index_list = list(index)
            index_list.remove(idx)
            if len(index_list) == 1:
                self._map[key] = index_list.pop()
            else:
                self._map[key] = tuple(index_list)
        else:
            dict.__delitem__(self, key.key)
            self._map.pop(key)

    def remove(self, key: Key | str) -> Container:
        """Remove a key from the container."""
        if not isinstance(key, Key):
            key = SingleKey(key)

        idx = self._map.pop(key, None)
        if idx is None:
            raise NonExistentKey(key)

        self._validation_cache.clear()
        if isinstance(idx, tuple):
            for i in idx:
                self._body[i] = (None, Null())
        else:
            self._body[idx] = (None, Null())

        dict.__delitem__(self, key.key)

        return self

    def _insert_after(
        self, key: Key | str, other_key: Key | str, item: Any
    ) -> Container:
        if key is None:
            raise ValueError("Key cannot be null in insert_after()")

        if key not in self:
            raise NonExistentKey(key)

        if not isinstance(key, Key):
            key = SingleKey(key)

        if not isinstance(other_key, Key):
            other_key = SingleKey(other_key)

        item = _item(item)

        idx = self._map[key]
        # Insert after the max index if there are many.
        if isinstance(idx, tuple):
            idx = max(idx)
        current_item = self._body[idx][1]
        if "\n" not in current_item.trivia.trail:
            current_item.trivia.trail += "\n"

        # Increment indices after the current index
        for k, v in self._map.items():
            if isinstance(v, tuple):
                new_indices = []
                for v_ in v:
                    if v_ > idx:
                        v_ = v_ + 1

                    new_indices.append(v_)

                self._map[k] = tuple(new_indices)
            elif v > idx:
                self._map[k] = v + 1

        self._map[other_key] = idx + 1
        self._body.insert(idx + 1, (other_key, item))

        if key is not None:
            dict.__setitem__(self, other_key.key, item.value)

        return self

    def _insert_at(self, idx: int, key: Key | str, item: Any) -> Container:
        if idx > len(self._body) - 1:
            raise ValueError(f"Unable to insert at position {idx}")

        if not isinstance(key, Key):
            key = SingleKey(key)

        item = _item(item)

        if idx > 0:
            previous_item = self._body[idx - 1][1]
            if not (
                isinstance(previous_item, Whitespace)
                or ends_with_whitespace(previous_item)
                or isinstance(item, (AoT, Table))
                or "\n" in previous_item.trivia.trail
            ):
                previous_item.trivia.trail += "\n"

        # Increment indices after the current index
        for k, v in self._map.items():
            if isinstance(v, tuple):
                new_indices = []
                for v_ in v:
                    if v_ >= idx:
                        v_ = v_ + 1

                    new_indices.append(v_)

                self._map[k] = tuple(new_indices)
            elif v >= idx:
                self._map[k] = v + 1

        if key in self._map:
            current_idx = self._map[key]
            if not isinstance(current_idx, tuple):
                current_idx = (current_idx,)
            self._map[key] = (*current_idx, idx)
            self._out_of_order_keys.add(key)
        else:
            self._map[key] = idx
        self._body.insert(idx, (key, item))

        dict.__setitem__(self, key.key, item.value)

        return self

    def item(self, key: Key | str) -> Item | OutOfOrderTableProxy:
        """Get an item for the given key."""
        if not isinstance(key, Key):
            key = SingleKey(key)

        idx = self._map.get(key)
        if idx is None:
            raise NonExistentKey(key)

        if isinstance(idx, tuple):
            # The item we are getting is an out of order table
            # so we need a proxy to retrieve the proper objects
            # from the parent container
            return OutOfOrderTableProxy(self, idx)

        return self._body[idx][1]

    def last_item(self) -> Item | None:
        """Get the last item."""
        if self._body:
            return self._body[-1][1]
        return None

    def sort_keys(
        self,
        key_order: Sequence[str] = (),
        recursive: bool = False,
        _validate_keys: bool = True,
    ) -> Container:
        """Reorder keys in this container without reformatting the document."""
        order = _normalize_key_order(key_order)
        if _validate_keys:
            _ensure_order_keys_exist(self, order, recursive)

        memo: dict[int, Any] = {}
        candidate = copy.deepcopy(self, memo)
        _sort_container(candidate, order, recursive)
        _validate_container(candidate, order, recursive, _validate_keys)
        _adopt_container(self, candidate, _source_map(self, memo))
        return self

    def as_string(self) -> str:
        """Render as TOML string."""
        s = ""
        for k, v in self._body:
            if k is not None:
                if isinstance(v, Table):
                    if (
                        s.strip(" ")
                        and not s.strip(" ").endswith("\n")
                        and "\n" not in v.trivia.indent
                    ):
                        s += "\n"
                    s += self._render_table(k, v)
                elif isinstance(v, AoT):
                    if (
                        s.strip(" ")
                        and not s.strip(" ").endswith("\n")
                        and "\n" not in v.trivia.indent
                    ):
                        s += "\n"
                    s += self._render_aot(k, v)
                else:
                    s += self._render_simple_item(k, v)
            else:
                s += self._render_simple_item(k, v)

        return s

    def _render_table(self, key: Key, table: Table, prefix: str | None = None) -> str:
        cur = ""

        if table.display_name is not None:
            _key = table.display_name
        else:
            _key = key.as_string()

            if prefix is not None:
                _key = prefix + "." + _key

        if (
            not table.is_super_table()
            or (
                any(
                    not isinstance(v, (Table, AoT, Whitespace, Null))
                    for _, v in table.value.body
                )
                and not key.is_dotted()
            )
            or (
                any(
                    k is not None and k.is_dotted()
                    for k, v in table.value.body
                    if isinstance(v, Table)
                )
                and not key.is_dotted()
            )
        ):
            open_, close = "[", "]"
            if table.is_aot_element():
                open_, close = "[[", "]]"

            newline_in_table_trivia = (
                "\n" if "\n" not in table.trivia.trail and len(table.value) > 0 else ""
            )
            cur += (
                f"{table.trivia.indent}"
                f"{open_}"
                f"{decode(_key)}"
                f"{close}"
                f"{table.trivia.comment_ws}"
                f"{decode(table.trivia.comment)}"
                f"{table.trivia.trail}"
                f"{newline_in_table_trivia}"
            )
        elif table.trivia.indent == "\n":
            cur += table.trivia.indent

        for k, v in table.value.body:
            if isinstance(v, Table):
                if (
                    cur.strip(" ")
                    and not cur.strip(" ").endswith("\n")
                    and "\n" not in v.trivia.indent
                ):
                    cur += "\n"
                assert k is not None
                if v.is_super_table():
                    if k.is_dotted() and not key.is_dotted():
                        # Dotted key inside table
                        cur += self._render_table(k, v)
                    else:
                        cur += self._render_table(k, v, prefix=_key)
                else:
                    cur += self._render_table(k, v, prefix=_key)
            elif isinstance(v, AoT):
                if (
                    cur.strip(" ")
                    and not cur.strip(" ").endswith("\n")
                    and "\n" not in v.trivia.indent
                ):
                    cur += "\n"
                assert k is not None
                cur += self._render_aot(k, v, prefix=_key)
            else:
                cur += self._render_simple_item(
                    k, v, prefix=_key if key.is_dotted() else None
                )

        return cur

    def _render_aot(self, key: Key, aot: AoT, prefix: str | None = None) -> str:
        _key = key.as_string()
        if prefix is not None:
            _key = prefix + "." + _key

        cur = ""
        _key = decode(_key)
        for table in aot.body:
            cur += self._render_aot_table(table, prefix=_key)

        return cur

    def _render_aot_table(self, table: Table, prefix: str | None = None) -> str:
        cur = ""
        _key = prefix or ""
        open_, close = "[[", "]]"

        cur += (
            f"{table.trivia.indent}"
            f"{open_}"
            f"{decode(_key)}"
            f"{close}"
            f"{table.trivia.comment_ws}"
            f"{decode(table.trivia.comment)}"
            f"{table.trivia.trail}"
        )

        for k, v in table.value.body:
            if isinstance(v, Table):
                assert k is not None
                if v.is_super_table():
                    if k.is_dotted():
                        # Dotted key inside table
                        cur += self._render_table(k, v)
                    else:
                        cur += self._render_table(k, v, prefix=_key)
                else:
                    cur += self._render_table(k, v, prefix=_key)
            elif isinstance(v, AoT):
                assert k is not None
                cur += self._render_aot(k, v, prefix=_key)
            else:
                cur += self._render_simple_item(k, v)

        return cur

    def _render_simple_item(
        self, key: Key | None, item: Item, prefix: str | None = None
    ) -> str:
        if key is None:
            return item.as_string()

        _key = key.as_string()
        if prefix is not None:
            _key = prefix + "." + _key

        return (
            f"{item.trivia.indent}"
            f"{decode(_key)}"
            f"{key.sep}"
            f"{decode(item.as_string())}"
            f"{item.trivia.comment_ws}"
            f"{decode(item.trivia.comment)}"
            f"{item.trivia.trail}"
        )

    def __len__(self) -> int:
        return dict.__len__(self)

    def __iter__(self) -> Iterator[str]:
        return iter(dict.keys(self))

    # Dictionary methods
    def __getitem__(self, key: Key | str) -> Any:
        item = self.item(key)
        if isinstance(item, Item) and item.is_boolean():
            return item.value

        return item

    def __contains__(self, key: object) -> bool:
        # Native membership test. The inherited ``MutableMapping.__contains__``
        # resolves the value via ``__getitem__``/``item()`` (and builds a
        # ``NonExistentKey`` on every absent key) only to discard it. Resolve the
        # key the same way ``item()`` does -- ``str`` becomes a ``SingleKey``
        # (a non-str/non-``Key`` argument still raises ``TypeError``) -- then
        # probe ``_map`` directly. For an out-of-order table the proxy is still
        # built so its validation runs exactly as before.
        if not isinstance(key, Key):
            key = SingleKey(key)  # type: ignore[arg-type]
        idx = self._map.get(key)
        if idx is None:
            return False
        if isinstance(idx, tuple):
            # during parsing every fragment append re-probes membership;
            # if the validation cache covers exactly these fragments they
            # are already known to merge cleanly, so skip rebuilding the
            # proxy (which is linear in the number of fragments)
            validated, _ = self._validation_cache.get(key, (0, None))
            if not (self._parsed and validated == len(idx)):
                OutOfOrderTableProxy(self, idx)
        return True

    def __setitem__(self, key: Key | str, value: Any) -> None:
        if key in self:
            old_key = next(filter(lambda k: k == key, self._map))
            self._replace(old_key, key, value)
        else:
            self.append(key, value)

    def __delitem__(self, key: Key | str) -> None:
        self.remove(key)

    def setdefault(self, key: Key | str, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return self[key]

    def _replace(self, key: Key | str, new_key: Key | str, value: Item) -> None:
        if not isinstance(key, Key):
            key = SingleKey(key)

        idx = self._map.get(key)
        if idx is None:
            raise NonExistentKey(key)

        self._replace_at(idx, new_key, value)

    def _replace_at(
        self, idx: int | tuple[int, ...], new_key: Key | str, value: Item
    ) -> None:
        value = _item(value)
        self._validation_cache.clear()

        if isinstance(idx, tuple):
            for i in idx[1:]:
                self._body[i] = (None, Null())

            idx = idx[0]

        k, v = self._body[idx]
        assert k is not None
        # A dotted key renders its value inline (e.g. ``a.b = 1``), which is only
        # consistent with a super table. When the replacement value renders with
        # its own ``[header]`` instead (a non-super table, or an array of tables),
        # keeping the dotted key duplicates the prefix onto the header (#524) and
        # lets the header swallow following dotted siblings (#542). Drop the dotted
        # key so the replacement renders as a plain table/array of tables.
        dotted_to_header = k.is_dotted() and (
            isinstance(value, AoT)
            or (isinstance(value, Table) and not value.is_super_table())
        )
        # That new header also captures every sibling that renders inline -- plain
        # values and dotted keys -- if any still follow it (#513), so it must be
        # moved past them, exactly as a value-to-table change already is.
        reposition_dotted = dotted_to_header and any(
            not isinstance(cur_val, (Null, Whitespace))
            and not (
                isinstance(cur_val, (Table, AoT))
                and (cur_key is None or not cur_key.is_dotted())
            )
            for cur_key, cur_val in self._body[idx + 1 :]
        )
        if not isinstance(new_key, Key):
            if (
                isinstance(value, (AoT, Table)) != isinstance(v, (AoT, Table))
                or new_key != k.key
                or dotted_to_header
            ):
                new_key = SingleKey(new_key)
            else:  # Inherit the sep of the old key
                new_key = k

        del self._map[k]
        self._map[new_key] = idx
        if new_key != k:
            dict.__delitem__(self, k.key)

        if (
            isinstance(value, (AoT, Table)) != isinstance(v, (AoT, Table))
            or reposition_dotted
        ):
            self.remove(k)
            if isinstance(value, (AoT, Table)):
                # New tables must appear after all entries that render inline:
                # plain values and dotted keys (which are super tables). Skip
                # those and insert before the first real ``[header]`` so the new
                # table cannot swallow a following sibling on round-trip.
                for i in range(idx, len(self._body)):
                    cur_key, cur_val = self._body[i]
                    if isinstance(cur_val, (AoT, Table)) and not (
                        cur_key is not None and cur_key.is_dotted()
                    ):
                        self._insert_at(i, new_key, value)
                        idx = i
                        break
                else:
                    idx = -1
                    self.append(new_key, value)
            else:
                # the replaced table's slot lies in the table region, where a
                # plain value would be captured by the preceding table on
                # round-trip; append() puts it with the other root-level values
                idx = -1
                self.append(new_key, value)
        else:
            # Copying trivia
            if not isinstance(value, (Whitespace, AoT)):
                value.trivia.indent = v.trivia.indent
                value.trivia.comment_ws = value.trivia.comment_ws or v.trivia.comment_ws
                value.trivia.comment = value.trivia.comment or v.trivia.comment
                value.trivia.trail = v.trivia.trail
            self._body[idx] = (new_key, value)

        if value is not v and hasattr(value, "invalidate_display_name"):
            value.invalidate_display_name()

        if isinstance(value, Table):
            # Insert a cosmetic new line for tables if:
            # - it does not have it yet OR is not followed by one
            # - it is not the last item, or
            # - The table being replaced has a newline
            result = self._previous_item_with_index()
            assert result is not None
            last, _ = result
            idx = last if idx < 0 else idx
            has_ws = ends_with_whitespace(value)
            replace_has_ws = (
                isinstance(v, Table)
                and v.value.body
                and isinstance(v.value.body[-1][1], Whitespace)
            )
            next_ws = idx < last and isinstance(self._body[idx + 1][1], Whitespace)
            if (idx < last or replace_has_ws) and not (next_ws or has_ws):
                value.append(None, Whitespace("\n"))

            assert isinstance(new_key, Key)
            dict.__setitem__(self, new_key.key, value.value)

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return repr(self.value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, dict):
            return NotImplemented

        return bool(_equal_with_nan(self.value, other))

    def _getstate(self, protocol: int) -> tuple[bool]:
        return (self._parsed,)

    def __reduce__(self) -> tuple[type, tuple[bool], tuple[Any, ...]]:
        return self.__reduce_ex__(2)

    def __reduce_ex__(self, protocol: int) -> tuple[type, tuple[bool], tuple[Any, ...]]:  # type: ignore[override]
        return (
            self.__class__,
            self._getstate(protocol),
            (self._map, self._body, self._parsed, self._table_keys),
        )

    def __setstate__(self, state: tuple[Any, ...]) -> None:
        self._map = state[0]
        self._body = state[1]
        self._parsed = state[2]
        self._table_keys = state[3]
        self._out_of_order_keys = {
            k for k, v in self._map.items() if isinstance(v, tuple)
        }

        for key, item in self._body:
            if key is not None:
                dict.__setitem__(self, key.key, item.value)

    def copy(self) -> Self:
        return copy.copy(self)

    def __copy__(self) -> Self:
        c = self.__class__(self._parsed)
        for k, v in dict.items(self):
            dict.__setitem__(c, k, v)

        c._body += self.body
        c._map.update(self._map)
        c._out_of_order_keys |= self._out_of_order_keys

        return c

    def _previous_item_with_index(
        self, idx: int | None = None, ignore: tuple[type, ...] = (Null,)
    ) -> tuple[int, Item] | None:
        """Find the immediate previous item before index ``idx``"""
        if idx is None or idx > len(self._body):
            idx = len(self._body)
        for i in range(idx - 1, -1, -1):
            v = self._body[i][-1]
            if not isinstance(v, ignore):
                return i, v
        return None

    def _previous_item(
        self, idx: int | None = None, ignore: tuple[type, ...] = (Null,)
    ) -> Item | None:
        """Find the immediate previous item before index ``idx``.
        If ``idx`` is not given, the last item is returned.
        """
        prev = self._previous_item_with_index(idx, ignore)
        return prev[-1] if prev else None


class OutOfOrderTableProxy(_CustomDict):  # type: ignore[type-arg]
    @staticmethod
    def validate(
        container: Container,
        indices: tuple[int, ...],
        temp_container: Container | None = None,
    ) -> Container:
        """Validate out of order tables in the given container"""
        # Append all items to a temp container to see if there is any error.
        # We deep-copy each value before appending: appending a super table
        # merges it in place into a matching one already in temp_container, and
        # those values are the live subtables of `container`. Without the copy
        # the merge would mutate the caller's tables (and corrupt a later
        # validation pass). The container merge itself is now copy-free for
        # speed, so this is where the isolation lives.
        # Passing a previous pass's temp container back in (with the already
        # validated indices stripped from `indices`) resumes the validation
        # incrementally.
        if temp_container is None:
            temp_container = Container(True)
        for i in indices:
            _, item = container._body[i]

            if isinstance(item, Table):
                for k, v in item.value.body:
                    temp_container.append(k, copy.deepcopy(v), validate=True)

        temp_container._validate_out_of_order_table()
        return temp_container

    def __init__(
        self,
        container: Container,
        indices: tuple[int, ...],
        logical_key: Key | None = None,
    ) -> None:
        self._container = container
        self._internal_container = Container(True)
        self._tables: list[Table] = []
        self._tables_map: dict[Key, list[int]] = {}
        self._logical_key = logical_key

        if logical_key is None:
            for key, key_indices in container._map.items():
                if key_indices == indices:
                    self._logical_key = key
                    break

        for i in indices:
            _, _item = self._container._body[i]

            if isinstance(_item, Table):
                self._tables.append(_item)
                table_idx = len(self._tables) - 1
                for k, v in _item.value.body:
                    merged = self._merge_aot_fragment(k, v)
                    if merged is None:
                        self._internal_container._raw_append(k, v)
                    else:
                        v = merged
                    key_indices = self._tables_map.setdefault(k, [])  # type: ignore[arg-type]
                    if table_idx not in key_indices:
                        key_indices.append(table_idx)
                    if k is not None:
                        dict.__setitem__(self, k.key, v)

        self._internal_container._validate_out_of_order_table()

    def _merge_aot_fragment(self, key: Key | None, item: Item) -> AoT | None:
        """
        Merge an array-of-tables fragment from a later out-of-order table part.

        An AoT whose elements are split across out-of-order parts of the same
        table arrives here once per part; ``_raw_append`` only knows how to
        chain duplicate ``Table`` parts and would raise ``KeyAlreadyPresent``.
        The fragments are presented as a new merged ``AoT`` referencing the
        live element tables, without mutating either fragment (the parts keep
        rendering their own elements).

        Returns the merged ``AoT``, or ``None`` if this is not such a fragment.
        """
        internal = self._internal_container
        if key is None or not isinstance(item, AoT) or key not in internal._map:
            return None
        idx = internal._map[key]
        if isinstance(idx, tuple):
            # A tuple index means the key already resolved to several body
            # positions, which here only happens for a degenerate collision
            # (e.g. a non-AoT part sharing the key). Three or more genuine AoT
            # fragments do not reach this branch: each later fragment merges
            # into the single growing AoT below, so ``idx`` stays an int.
            return None
        existing = internal._body[idx][1]
        if not isinstance(existing, AoT):
            return None

        merged = AoT([*existing.body, *item.body], parsed=True)
        internal._body[idx] = (internal._body[idx][0], merged)
        dict.__setitem__(internal, key.key, merged.value)
        return merged

    def unwrap(self) -> dict[str, Any]:
        return self._internal_container.unwrap()

    @property
    def value(self) -> dict[str, Any]:
        return self._internal_container.value

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return repr(self.value)

    def __contains__(self, key: object) -> bool:
        # Native membership test. The inherited ``MutableMapping.__contains__``
        # resolves the value via ``__getitem__`` (and builds a ``NonExistentKey``
        # on every absent key) only to discard it. Probe the internal container
        # directly -- the same predicate ``__getitem__`` already uses -- which is
        # itself a native ``_map`` lookup that still rebuilds the proxy for an
        # out-of-order key so its validation runs exactly as before.
        return key in self._internal_container

    def __getitem__(self, key: Key | str) -> Any:
        if key not in self._internal_container:
            raise NonExistentKey(key)

        return self._internal_container[key]

    def __setitem__(self, key: Key | str, value: Any) -> None:
        from .items import item as _item_fn

        def _is_table_or_aot(it: Any) -> bool:
            return isinstance(_item_fn(it), (Table, AoT))

        _key: Key = key if isinstance(key, Key) else SingleKey(key)

        if _key in self._tables_map:
            # Overwrite the first table and remove others
            map_indices = self._tables_map[_key]
            while len(map_indices) > 1:
                table = self._tables[map_indices.pop()]
                self._remove_table(table)
            old_value = self._tables[map_indices[0]][key]
            if _is_table_or_aot(old_value) and not _is_table_or_aot(value):
                # Remove the entry from the map and set value again.
                del self._tables[map_indices[0]][key]
                del self._tables_map[_key]
                self[key] = value
                return
            self._tables[map_indices[0]][key] = value
        elif self._tables:
            if not _is_table_or_aot(value):  # if the value is a plain value
                for table in self._tables:
                    # find the first table that allows plain values
                    if any(not _is_table_or_aot(v) for _, v in table.items()):
                        table[key] = value
                        break
                else:
                    # No part holds a plain value yet, so the chosen part must
                    # start rendering its own ``[key]`` header. Prefer a part
                    # that is not a super table (it already renders that header):
                    # turning a header-less super part concrete here would emit a
                    # second, duplicate header next to an existing concrete part
                    # and produce TOML that no longer parses.
                    for table in self._tables:
                        if not table.is_super_table():
                            table[key] = value
                            break
                    else:
                        self._tables[0][key] = value
            else:
                self._tables[0][key] = value
        else:
            self._container[key] = value

        self._internal_container[key] = value
        if key is not None:
            dict.__setitem__(self, key, value)

    def _remove_table(self, table: Table) -> None:
        """Remove table from the parent container"""
        self._tables.remove(table)
        for idx, body_item in enumerate(self._container._body):
            if body_item[1] is table:
                self._container._remove_at(idx)
                break

    def __delitem__(self, key: Key | str) -> None:
        _key: Key = key if isinstance(key, Key) else SingleKey(key)
        if _key not in self._tables_map:
            raise NonExistentKey(key)

        for i in reversed(self._tables_map[_key]):
            table = self._tables[i]
            del table[key]
            if not table and len(self._tables) > 1:
                self._remove_table(table)

        del self._tables_map[_key]
        del self._internal_container[key]
        if key is not None:
            dict.__delitem__(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(dict.keys(self))

    def __len__(self) -> int:
        return dict.__len__(self)

    def setdefault(self, key: Key | str, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return self[key]

    def sort_keys(
        self, key_order: Sequence[str] = (), recursive: bool = False
    ) -> OutOfOrderTableProxy:
        """Reorder logical keys spread across multiple table fragments."""
        order = _normalize_key_order(key_order)
        _ensure_order_keys_exist(
            self._internal_container, order, recursive
        )

        assert self._logical_key is not None
        logical_key = self._logical_key
        memo: dict[int, Any] = {}
        candidate_owner = copy.deepcopy(self._container, memo)
        candidate_indices = candidate_owner._map[logical_key]
        if not isinstance(candidate_indices, tuple):
            candidate_indices = (candidate_indices,)

        _sort_proxy_container(candidate_owner, candidate_indices, order, recursive)
        _invalidate_display_names(candidate_owner)

        from tomlkit.parser import Parser

        try:
            reparsed = Parser(candidate_owner.as_string()).parse()
        except TOMLKitError as exc:
            raise InvalidKeyOrderError(str(exc)) from exc

        if reparsed.unwrap() != self._container.unwrap():
            raise InvalidKeyOrderError("Requested key order changes the TOML value")

        candidate_proxy = OutOfOrderTableProxy(
            candidate_owner, candidate_indices, logical_key
        )
        _assert_container_order(
            candidate_proxy._internal_container, order, recursive
        )
        _adopt_container(
            self._container, candidate_owner, _source_map(self._container, memo)
        )
        adopted_indices = self._container._map[logical_key]
        if not isinstance(adopted_indices, tuple):
            adopted_indices = (adopted_indices,)
        return OutOfOrderTableProxy(
            self._container, adopted_indices, logical_key
        )



def ends_with_whitespace(it: Any) -> bool:
    """Returns ``True`` if the given item ``it`` is a ``Table`` or ``AoT`` object
    ending with a ``Whitespace``.
    """
    if isinstance(it, Whitespace):
        return True
    if isinstance(it, Table):
        previous = it.value._previous_item()
        return previous is not None and ends_with_whitespace(previous)
    return isinstance(it, AoT) and len(it) > 0 and ends_with_whitespace(it[-1])


def _equal_with_nan(left: Any, right: Any) -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            return False
        return all(_equal_with_nan(left[k], right[k]) for k in left)

    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return False
        return all(_equal_with_nan(l, r) for l, r in zip(left, right))  # noqa: E741

    if isinstance(left, float) and isinstance(right, float):
        if math.isnan(left) and math.isnan(right):
            return True

    return bool(left == right)
