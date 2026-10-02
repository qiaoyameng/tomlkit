from __future__ import annotations

import dataclasses
import re

from collections.abc import Iterator

from tomlkit.exceptions import TOMLKitError


@dataclasses.dataclass
class ParseDiagnostic:
    """
    A single recovery diagnostic produced by ``parse_with_recovery()``.

    ``start``/``end`` are character offsets delimiting the unparseable
    fragment in the original input; ``line``/``col`` locate the error itself
    (1-based line, 0-based column, matching ``ParseError``). ``incomplete``
    is True when no safe recovery point could be determined, meaning the
    remainder of the input was swallowed into a single unparsed region and
    further errors inside it may have gone unreported.
    """

    message: str
    line: int
    col: int
    start: int
    end: int
    error: TOMLKitError
    incomplete: bool = False


# Lexer states for the recovery scanner.
_NORMAL = "normal"
_COMMENT = "comment"
_SQ = "sq"  # inside a '...' literal string
_DQ = "dq"  # inside a "..." basic string
_ML_SQ = "ml_sq"  # inside a '''...''' literal string
_ML_DQ = "ml_dq"  # inside a """...""" basic string

# A line holding nothing but a complete table header, e.g. "[foo]" or
# "[[foo.bar]]  # comment".
_HEADER_LINE_RE = re.compile(r"[ \t]*\[\[?[^\[\]\n]+\]\]?[ \t]*(?:#[^\n]*)?")

# A line looking like a table header whose closing bracket is missing,
# e.g. "[foo" -- used so a broken header does not swallow the rest of the
# document.
_UNTERMINATED_HEADER_RE = re.compile(r"[ \t]*\[\[?[^\[\]\n]*")

# One TOML key fragment: bare, "basic" or 'literal'.
_KEY_PART = r"[A-Za-z0-9_-]+|\"[^\"\n]*\"|'[^'\n]*'"
_KEY_PART_RE = re.compile(_KEY_PART)

# A key/value line, e.g. "foo.bar = ..." (the value itself is not matched).
_KV_LINE_RE = re.compile(
    r"[ \t]*(?P<key>(?:"
    + _KEY_PART
    + r")(?:[ \t]*\.[ \t]*(?:"
    + _KEY_PART
    + r"))*)[ \t]*="
)

# A (possibly unterminated) table header line, e.g. "[foo]", "[[foo.bar]]"
# or "[foo".
_TABLE_LINE_RE = re.compile(
    r"[ \t]*\[\[?[ \t]*(?P<key>[^\[\]#\n]+?)[ \t]*(?:\]\]?[ \t]*(?:#[^\n]*)?)?"
)


def find_fragment_end(src: str, start: int) -> tuple[int, bool]:
    """
    Finds the end of the unparseable fragment that starts at ``start``.

    Returns ``(end, complete)``: ``end`` is the offset at which the next
    statement begins (the fragment spans ``src[start:end]``) and ``complete``
    is False when no safe resynchronization point could be determined, in
    which case the rest of the input is consumed as a single unparsed region.

    The scanner understands TOML lexical structure -- comments, basic and
    literal strings (single- and multi-line), arrays and inline tables -- so
    that, for example, a ``[header]``-looking line inside an unterminated
    multi-line string is not mistaken for a new table.
    """
    i = start
    n = len(src)
    bracket = 0  # unmatched [ ... ]
    brace = 0  # unmatched { ... }
    state = _NORMAL
    line_start = src.rfind("\n", 0, start) + 1

    while i < n:
        c = src[i]

        if state == _COMMENT:
            if c == "\n":
                state = _NORMAL
            else:
                i += 1
                continue
        elif state == _SQ or state == _DQ:
            quote = "'" if state == _SQ else '"'
            if state == _DQ and c == "\\":
                i += 2
                continue
            if c == quote:
                state = _NORMAL
                i += 1
                continue
            if c != "\n":
                i += 1
                continue
            # An unterminated single-line string ends at the newline; fall
            # through and treat the newline as a normal-state boundary.
            state = _NORMAL
        elif state == _ML_SQ or state == _ML_DQ:
            quote = "'" if state == _ML_SQ else '"'
            if state == _ML_DQ and c == "\\":
                i += 2
                continue
            if c == quote:
                run = 0
                while i + run < n and src[i + run] == quote:
                    run += 1
                if run >= 3:
                    state = _NORMAL
                    i += 3
                else:
                    i += run
                continue
            i += 1
            continue

        # Normal (statement) state.
        if c == "#":
            state = _COMMENT
            i += 1
        elif c == "'" or c == '"':
            if src.startswith(c * 3, i):
                state = _ML_SQ if c == "'" else _ML_DQ
                i += 3
            else:
                state = _SQ if c == "'" else _DQ
                i += 1
        elif c == "[":
            bracket += 1
            i += 1
        elif c == "]":
            bracket = max(0, bracket - 1)
            i += 1
        elif c == "{":
            brace += 1
            i += 1
        elif c == "}":
            brace = max(0, brace - 1)
            i += 1
        elif c == "\n":
            if brace > 0:
                # Inline tables cannot span lines: a newline always ends a
                # statement with an unclosed "{".
                return i + 1, True
            if bracket == 0:
                return i + 1, True
            # Inside an unclosed array: only resume at a line that clearly
            # starts a new table, or after a broken table header.
            if _UNTERMINATED_HEADER_RE.fullmatch(src[line_start:i]):
                return i + 1, True
            nl = src.find("\n", i + 1)
            next_line = src[i + 1 : n if nl == -1 else nl]
            if _HEADER_LINE_RE.fullmatch(next_line):
                return i + 1, True
            line_start = i + 1
            i += 1
        else:
            i += 1

    complete = state in (_NORMAL, _COMMENT, _SQ, _DQ) and bracket == 0 and brace == 0
    return n, complete


def _statement_lines(text: str) -> Iterator[str]:
    """
    Yields the lines of ``text`` that begin a new statement, i.e. lines
    starting outside of any string, comment, array or inline table.
    """
    i = 0
    n = len(text)
    bracket = 0
    brace = 0
    state = _NORMAL
    line_start = 0

    while i < n:
        c = text[i]

        if state == _COMMENT:
            if c == "\n":
                state = _NORMAL
                line_start = i + 1
            i += 1
            continue
        if state == _SQ or state == _DQ:
            quote = "'" if state == _SQ else '"'
            if state == _DQ and c == "\\":
                i += 2
                continue
            if c == quote:
                state = _NORMAL
            elif c == "\n":
                state = _NORMAL
                line_start = i + 1
            i += 1
            continue
        if state == _ML_SQ or state == _ML_DQ:
            quote = "'" if state == _ML_SQ else '"'
            if state == _ML_DQ and c == "\\":
                i += 2
                continue
            if c == quote:
                run = 0
                while i + run < n and text[i + run] == quote:
                    run += 1
                if run >= 3:
                    state = _NORMAL
                    i += 3
                else:
                    i += run
                continue
            i += 1
            continue

        # Normal (statement) state.
        if i == line_start and bracket == 0 and brace == 0:
            nl = text.find("\n", i)
            yield text[i : n if nl == -1 else nl]

        if c == "#":
            state = _COMMENT
        elif c == "'" or c == '"':
            if text.startswith(c * 3, i):
                state = _ML_SQ if c == "'" else _ML_DQ
                i += 3
                continue
            state = _SQ if c == "'" else _DQ
        elif c == "[":
            bracket += 1
        elif c == "]":
            bracket = max(0, bracket - 1)
        elif c == "{":
            brace += 1
        elif c == "}":
            brace = max(0, brace - 1)
        elif c == "\n":
            line_start = i + 1
        i += 1


def _add_key_claims(claims: set, dotted: str) -> None:
    parts = []
    for match in _KEY_PART_RE.finditer(dotted):
        part = match.group(0)
        if len(part) >= 2 and part[0] == part[-1] and part[0] in "'\"":
            part = part[1:-1]
        parts.append(part)
    for i in range(1, len(parts) + 1):
        claims.add(".".join(parts[:i]))


def extract_claimed_keys(raw: str) -> frozenset:
    """
    Best-effort extraction of the key names a broken fragment appears to
    define (keys of key/value pairs and table headers found at the top
    lexical level of ``raw``). Used to reject edits that would silently
    shadow content still held by the opaque fragment.
    """
    claims: set = set()
    for line in _statement_lines(raw):
        match = _TABLE_LINE_RE.fullmatch(line)
        if match:
            _add_key_claims(claims, match.group("key"))
            continue
        match = _KV_LINE_RE.match(line)
        if match:
            _add_key_claims(claims, match.group("key"))
    return frozenset(claims)
