from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tomlkit._compat import decode
from tomlkit.container import Container
from tomlkit.exceptions import EmptyKeyError
from tomlkit.exceptions import EmptyTableNameError
from tomlkit.exceptions import ParseError
from tomlkit.items import AoT
from tomlkit.items import Item
from tomlkit.items import Key
from tomlkit.items import Null
from tomlkit.items import Table
from tomlkit.items import Trivia
from tomlkit.items import UnparsedNode
from tomlkit.parser import _KV
from tomlkit.parser import Parser
from tomlkit.toml_document import TOMLDocument


@dataclass(frozen=True)
class Diagnostic:
    """A single problem found while recovery-parsing a document."""

    message: str
    line: int
    col: int
    start: int
    end: int
    incomplete: bool = False

    def __str__(self) -> str:
        return f"{self.message} at line {self.line} col {self.col}"


@dataclass
class RecoveryResult:
    """Result of a recovery parse.

    ``document`` is an editable :class:`TOMLDocument`. Fragments that could not
    be parsed are kept verbatim inside opaque :class:`UnparsedNode` items; the
    parts around them still expose the normal container interface. When a safe
    resynchronization point cannot be found, ``complete`` is ``False`` and the
    remaining text is stored as a single unparsed region.
    """

    document: TOMLDocument
    diagnostics: list[Diagnostic]
    complete: bool

    @property
    def ok(self) -> bool:
        return not self.diagnostics


@dataclass(frozen=True)
class _Record:
    start: int
    end: int
    terminated: bool


def scan_records(source: str) -> list[_Record]:
    """Split *source* into boundary-aware top-level records.

    A record ends at a line boundary reached while no bracket/enclosure is open.
    Comments, single- and triple-quoted strings (basic and literal) are
    understood, so a ``[`` or ``[table]``-looking token inside a string never
    starts a new record. A record whose last construct stays open (an
    unterminated multiline string or bracket cluster) is reported as
    non-terminated and extends to the end of the input.
    """
    n = len(source)
    records: list[_Record] = []
    rec_start = 0
    i = 0
    depth = 0
    while i < n:
        c = source[i]
        if c == "#":
            i += 1
            while i < n and source[i] != "\n":
                i += 1
            continue
        if c in "\"'":
            quote = c
            if source.startswith(quote * 3, i):
                close = source.find(quote * 3, i + 3)
                if close == -1:
                    records.append(_Record(rec_start, n, False))
                    return records
                i = close + 3
                continue
            i += 1
            while i < n and source[i] != quote and source[i] != "\n":
                if quote == '"' and source[i] == "\\" and i + 1 < n:
                    i += 2
                else:
                    i += 1
            if i < n and source[i] == quote:
                i += 1
            # An unterminated single-line string falls through and the newline
            # at depth 0 closes the record, which is a safe resync point.
            continue
        if c in "[{":
            depth += 1
            i += 1
            continue
        if c in "]}":
            if depth > 0:
                depth -= 1
            i += 1
            continue
        if c == "\n" and depth == 0:
            end = i + 1
            records.append(_Record(rec_start, end, True))
            rec_start = end
        i += 1

    if rec_start < n:
        records.append(_Record(rec_start, n, depth == 0))
    return records


class RecoveryParser(Parser):
    """A :class:`Parser` variant that recovers from syntax errors.

    Instead of raising on the first error, the broken fragment is stored as an
    opaque :class:`UnparsedNode` (preserving its original text and character
    span), while well-formed parts of the document remain fully editable.
    """

    def __init__(self, string: str | bytes) -> None:
        super().__init__(string)
        self._diagnostics: list[Diagnostic] = []
        self._complete = True
        text = decode(string)
        self._text = text
        self._records = scan_records(text)
        self._step_container: Any = None
        starts = [0]
        for idx, ch in enumerate(text):
            if ch == "\n":
                starts.append(idx + 1)
        self._line_starts = starts

    # ----- public entry point ---------------------------------------------

    def parse_recovery(self) -> RecoveryResult:
        body = TOMLDocument(True)

        # Phase 1: root-level key/value pairs, up to the first table header.
        while not self.end() and self._current != "[":
            if not self._recover_step(body, self._parse_root_item):
                break

        # Phase 2: tables/AoTs (and any stray junk between or after them).
        while not self.end():
            if not self._recover_step(body, self._parse_table_step):
                break

        body.parsing(False)
        return RecoveryResult(body, list(self._diagnostics), self._complete)

    # ----- top-level steps -------------------------------------------------

    def _parse_root_item(self) -> tuple[Key | None, Item] | None:
        return self._parse_item()

    def _parse_table_step(self) -> tuple[Key | None, Item]:
        if self._current != "[":
            # A recovered table header may have left us on a key/value line or
            # whitespace; parse it as a root-level item (possibly returning
            # None for pure whitespace).
            return self._parse_root_item()  # type: ignore[return-value]
        key, value = self._parse_table_recovery()
        if isinstance(value, Table) and value.is_aot_element():
            value = self._parse_aot_recovery(value, key, self._step_container)
        return key, value

    def _recover_step(
        self,
        container: Any,
        step: Any,
        *,
        step_start: int | None = None,
    ) -> bool:
        """Run one parsing *step* into *container*, recovering from errors.

        Returns ``False`` once the end of input is reached.
        """
        if step_start is None:
            step_start = self._idx
        self._step_container = container
        try:
            result = step()
        except ParseError as e:
            self._recover_at(e, container, step_start)
            return not self.end()

        if result is None:
            # Whitespace/comment item (already consumed) or a table header
            # encountered from a non-table position: continue the loop.
            self.mark()
            return not self.end()

        key, value = result
        if key is not None and key.is_multi():
            try:
                container.append(key, value)
            except Exception as e:
                self._recover_at(e, container, step_start, error_position=step_start)
                return not self.end()
        elif not self._merge_ws(value, container):
            try:
                container.append(key, value)
            except Exception as e:
                self._recover_at(e, container, step_start, error_position=step_start)
                return not self.end()

        self.mark()
        return not self.end()

    # ----- recovery core ---------------------------------------------------

    def _index_from_error(self, error: ParseError) -> int:
        line = error.line - 1
        col = error.col
        if line >= len(self._line_starts):
            return len(self._text)
        idx = self._line_starts[line] + col
        return max(0, min(idx, len(self._text)))

    def _linecol_from_index(self, idx: int) -> tuple[int, int]:
        idx = max(0, min(idx, len(self._text)))
        line = 0
        for start in self._line_starts:
            if start <= idx:
                line += 1
            else:
                break
        col = idx - self._line_starts[line - 1]
        return line, col

    def _record_at(self, idx: int) -> _Record:
        for record in self._records:
            if record.start <= idx < record.end or (
                idx == len(self._text) and record.end == len(self._text)
            ):
                return record
        # Fall back to the last record.
        return self._records[-1]

    def _recover_at(
        self,
        error: BaseException,
        container: Any,
        step_start: int,
        *,
        error_position: int | None = None,
    ) -> tuple[Key | None, UnparsedNode]:
        if isinstance(error, ParseError):
            err_idx = self._index_from_error(error)
            err_line = error.line
            err_col = error.col
            err_message = str(error)
        else:
            err_idx = error_position if error_position is not None else self._idx
            err_line, err_col = self._linecol_from_index(err_idx)
            err_message = str(error)

        record = self._record_at(err_idx)
        start = record.start
        end = record.end

        if not record.terminated:
            end = len(self._text)
            self._complete = False

        raw = self._text[start:end]
        node = UnparsedNode(raw, start, end)

        err_end = min(err_idx + 1, end)
        self._diagnostics.append(
            Diagnostic(
                err_message,
                err_line,
                err_col,
                err_idx if err_idx <= end else start,
                err_end if err_end > err_idx else end,
            )
        )
        if not record.terminated:
            tail_line = self._text.count("\n", 0, start) + 1
            self._diagnostics.append(
                Diagnostic(
                    "No safe resynchronization point found; the remaining text is "
                    "kept as an unparsed region",
                    tail_line,
                    0,
                    start,
                    end,
                    incomplete=True,
                )
            )

        key: Key | None = None
        extracted = self._extract_key(raw)
        if extracted is not None:
            key, value_raw, indent, value_start_in_raw = extracted
            if key.key not in container._map and not key.is_multi():
                node = UnparsedNode(value_raw, start + value_start_in_raw, end)
                node._trivia.indent = indent
                if value_raw.endswith("\n"):
                    node._trivia.trail = ""
            else:
                key = None

        if key is None:
            container._raw_append(None, node)
        else:
            container._raw_append(key, node)

        self._seek(end)
        return key, node

    def _seek(self, idx: int) -> None:
        self._src._idx = idx
        if idx < len(self._src):
            self._src._current = self._src[idx]
        else:
            self._src._current = self._src.EOF
        self._src.mark()

    def _extract_key(
        self, raw: str
    ) -> tuple[Key, str, str, int] | None:
        """Best-effort extraction of a leading ``key =`` from a bad fragment.

        Returns ``(key, value_raw, indent, value_start_in_raw)`` or ``None``
        when no key/value shape can be recognized (e.g. a table header).
        """
        try:
            probe = Parser(raw)
            probe.mark()
            probe._src.advance_while(frozenset(" \t\r\n"))
            body_start = probe._idx
            if probe.end() or probe._current == "[":
                return None
            key = probe._parse_key()
            if key.is_multi():
                return None
            probe.mark()
            found_equals = probe._current == "="

            while probe._current in _KV and probe.inc():
                if probe._current == "=":
                    if found_equals:
                        return None
                    found_equals = True
            if not found_equals:
                return None
            key.sep = probe.extract()
            indent = raw[:body_start]
            value_start = probe._idx
            return key, raw[value_start:], indent, value_start
        except ParseError:
            return None

    # ----- tables ----------------------------------------------------------

    def _parse_table_recovery(
        self,
        parent_name: Key | None = None,
        parent: Table | None = None,
    ) -> tuple[Key, Table | AoT]:
        """Recovery variant of :meth:`Parser._parse_table`."""
        if self._current != "[":
            raise self.parse_error(
                ParseError, "_parse_table() called on non-bracket character."
            )

        indent = self.extract()
        self.inc()  # Skip opening bracket

        if self.end():
            raise self.parse_error(ParseError, "Unexpected end of file")

        is_aot = False
        if self._current == "[":
            if not self.inc():
                raise self.parse_error(ParseError, "Unexpected end of file")
            is_aot = True
        try:
            key = self._parse_key()
        except EmptyKeyError:
            raise self.parse_error(EmptyTableNameError) from None
        if self.end():
            raise self.parse_error(ParseError, "Unexpected end of file")
        elif self._current != "]":
            raise self.parse_error(
                ParseError, f"Unexpected character: {self._current!r}"
            )

        key.sep = ""
        full_key = key
        name_parts = tuple(key)
        if any(" " in part.key.strip() and part.is_bare() for part in name_parts):
            raise self.parse_error(
                ParseError, f'Invalid table name "{full_key.as_string()}"'
            )

        missing_table = False
        if parent_name:
            parent_name_parts = tuple(parent_name)
        else:
            parent_name_parts = ()

        if len(name_parts) > len(parent_name_parts) + 1:
            missing_table = True

        name_parts = name_parts[len(parent_name_parts) :]

        values = Container(True)

        self.inc()  # Skip closing bracket
        if is_aot:
            if self.end():
                raise self.parse_error(ParseError, "Unexpected end of file")
            elif self._current != "]":
                raise self.parse_error(
                    ParseError, f"Unexpected character: {self._current!r}"
                )
            self.inc()  # Skip second closing bracket

        cws, comment, trail = self._parse_comment_trail()

        result: Table | AoT = Null()  # type: ignore[assignment]
        table = Table(
            values,
            Trivia(indent, cws, comment, trail),
            is_aot,
            name=name_parts[0].key if name_parts else key.key,
            display_name=full_key.as_string(),
            is_super_table=False,
        )

        if len(name_parts) > 1:
            if missing_table:
                table = Table(
                    Container(True),
                    Trivia("", cws, comment, trail),
                    is_aot and name_parts[0] in self._aot_stack,
                    is_super_table=True,
                    name=name_parts[0].key,
                )

            result = table
            key = name_parts[0]

            for i, _name in enumerate(name_parts[1:]):
                child = table.get(
                    _name,
                    Table(
                        Container(True),
                        Trivia(indent, cws, comment, trail),
                        is_aot and i == len(name_parts) - 2,
                        is_super_table=i < len(name_parts) - 2,
                        name=_name.key,
                        display_name=(
                            full_key.as_string() if i == len(name_parts) - 2 else None
                        ),
                    ),
                )

                if is_aot and i == len(name_parts) - 2:
                    table.raw_append(_name, AoT([child], name=table.name, parsed=True))
                else:
                    table.raw_append(_name, child)

                table = child
                values = table.value
        else:
            if name_parts:
                key = name_parts[0]

        outer = parent if parent is not None else None
        self._parse_table_body_recovery(table, values, full_key, outer)

        table.value._validate_out_of_order_table()
        if isinstance(result, Null):
            result = table

            if is_aot and (not self._aot_stack or full_key != self._aot_stack[-1]):
                result = self._parse_aot_recovery(
                    result,
                    full_key,
                    outer if outer is not None else self._step_container,
                )

        return key, result

    def _parse_table_body_recovery(
        self,
        table: Table,
        values: Container,
        full_key: Key,
        outer: Any,
    ) -> None:
        """Parse key/value lines and nested child tables inside one table."""
        while not self.end():
            item_start = self._idx
            try:
                parsed = self._parse_item()
            except ParseError as e:
                self._recover_at(e, values, item_start)
                continue

            if parsed:
                _key, _val = parsed
                if not self._merge_ws(_val, values):
                    try:
                        table.raw_append(_key, _val)
                    except Exception as e:
                        self._recover_at(
                            e, values, item_start, error_position=item_start
                        )
                continue

            # parsed is None: whitespace/comment or the start of a table header.
            if self._current == "[":
                try:
                    _, key_next = self._peek_table()
                except ParseError as e:
                    self._recover_at(e, values, item_start)
                    continue

                if self._is_child(full_key, key_next):
                    if not self._parse_child_tables(table, full_key):
                        break
                break
            else:
                # Defensive: _parse_item() returned None without a bracket.
                # Recover the current record instead of escaping to the caller.
                self._recover_at(
                    self.parse_error(
                        ParseError,
                        "_parse_item() returned None on a non-bracket character.",
                    ),
                    values,
                    item_start,
                )
                continue

    def _parse_child_tables(self, table: Table, full_key: Key) -> bool:
        """Parse a direct child table plus any siblings following it.

        Returns ``False`` when an unrecoverable header was consumed as a fragment
        and the caller should leave this table's body.
        """
        item_start = self._idx
        try:
            key_next, table_next = self._parse_table_recovery(full_key, table)
        except ParseError as e:
            self._recover_at(e, table.value, item_start)
            return False
        table.raw_append(key_next, table_next)

        while not self.end():
            peek_start = self._idx
            try:
                _, key_next = self._peek_table()
            except ParseError as e:
                self._recover_at(e, table.value, peek_start)
                return False
            if not self._is_child(full_key, key_next):
                break
            child_start = self._idx
            try:
                key_next, table_next = self._parse_table_recovery(full_key, table)
            except ParseError as e:
                self._recover_at(e, table.value, child_start)
                return False
            table.raw_append(key_next, table_next)
        return True

    def _parse_aot_recovery(
        self, first: Table, name_first: Key, outer: Any
    ) -> AoT:
        payload: list[Table] = [first]
        self._aot_stack.append(name_first)
        while not self.end():
            peek_start = self._idx
            try:
                is_aot_next, name_next = self._peek_table()
            except ParseError as e:
                # A broken header between AoT siblings: recover it into the
                # surrounding document and stop this AoT.
                self._recover_at(e, outer, peek_start)
                break
            if is_aot_next and name_next == name_first:
                elem_start = self._idx
                try:
                    _, tbl = self._parse_table_recovery(name_first)
                except ParseError as e:
                    self._recover_at(e, outer, elem_start)
                    break
                if not isinstance(tbl, Table):
                    break
                payload.append(tbl)
            else:
                break

        self._aot_stack.pop()
        return AoT(payload, parsed=True)


def parse_recovery(string: str | bytes) -> RecoveryResult:
    """Parse *string* in recovery mode.

    Returns a :class:`RecoveryResult` carrying an editable document and a list
    of :class:`Diagnostic` objects. The strict :func:`tomlkit.parse` behavior
    is unchanged; this entry point never drops broken text.
    """
    return RecoveryParser(string).parse_recovery()
