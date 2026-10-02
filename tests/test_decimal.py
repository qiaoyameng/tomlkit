import decimal
import io
import pickle
import tomllib

from decimal import Decimal as PyDecimal

import pytest

import tomlkit

from tomlkit.items import Decimal
from tomlkit.items import Float


@pytest.fixture
def low_precision():
    """Run a test with an extremely low decimal context precision."""
    old_prec = decimal.getcontext().prec
    decimal.getcontext().prec = 1
    try:
        yield
    finally:
        decimal.getcontext().prec = old_prec


def test_decimal_mode_exposes_decimal_values():
    doc = tomlkit.parse("price = 19.99\n", parse_float=PyDecimal)

    assert isinstance(doc["price"], Decimal)
    assert isinstance(doc["price"].unwrap(), PyDecimal)
    assert doc["price"].unwrap() == PyDecimal("19.99")
    assert doc.unwrap() == {"price": PyDecimal("19.99")}


def test_decimal_mode_preserves_original_lexemes():
    src = (
        "a = +1.5e10\n"
        "b = 1_000.000_1\n"
        "c = 6.022_140_76E23\n"
        "d = -0.0\n"
        "e = 3.14159265358979323846264338327950288419\n"
    )
    doc = tomlkit.parse(src, parse_float=PyDecimal)

    assert doc["a"].as_string() == "+1.5e10"
    assert doc["b"].as_string() == "1_000.000_1"
    assert doc["c"].as_string() == "6.022_140_76E23"
    assert doc["d"].as_string() == "-0.0"
    assert doc["e"].as_string() == "3.14159265358979323846264338327950288419"
    assert tomlkit.dumps(doc) == src


def test_decimal_mode_parse_unaffected_by_context_precision(low_precision):
    src = "pi = 3.14159265358979323846264338327950288419\nbig = 1.7976931348623157e308\n"
    doc = tomlkit.parse(src, parse_float=PyDecimal)

    assert doc["pi"].unwrap() == PyDecimal(
        "3.14159265358979323846264338327950288419"
    )
    assert doc["big"].unwrap() == PyDecimal("1.7976931348623157e308")
    # Unmodified values keep their original literal form on round-trip.
    assert tomlkit.dumps(doc) == src


def test_decimal_mode_editing_other_field_keeps_float_lexemes(low_precision):
    src = (
        "price = 19.9999999999999999999999\n"
        'name = "old"\n'
        "ratio = 1.5e-300\n"
    )
    doc = tomlkit.parse(src, parse_float=PyDecimal)

    doc["name"] = "new"

    assert tomlkit.dumps(doc) == src.replace('"old"', '"new"')


def test_decimal_mode_load_and_loads_accept_parse_float():
    src = "x = 0.1\n"

    assert tomlkit.loads(src, parse_float=PyDecimal)["x"].unwrap() == PyDecimal(
        "0.1"
    )
    assert tomlkit.load(io.StringIO(src), parse_float=PyDecimal)[
        "x"
    ].unwrap() == PyDecimal("0.1")


def test_writing_integral_decimal_stays_a_float():
    doc = tomlkit.parse("a = 1.5\n", parse_float=PyDecimal)
    doc["b"] = PyDecimal("3")
    doc["c"] = PyDecimal("1E+3")

    out = tomlkit.dumps(doc)
    assert "b = 3.0\n" in out
    assert "c = 1E+3\n" in out

    # The generated lexemes are valid TOML and re-parse as floats.
    parsed = tomllib.loads(out)
    assert isinstance(parsed["b"], float)
    assert isinstance(parsed["c"], float)
    assert parsed["b"] == 3.0
    assert parsed["c"] == 1000.0

    # Re-parsing in decimal mode yields the exact same values.
    reparsed = tomlkit.parse(out, parse_float=PyDecimal)
    assert reparsed["b"].unwrap() == PyDecimal("3")
    assert reparsed["c"].unwrap() == PyDecimal("1E+3")


def test_writing_decimal_under_low_context_precision(low_precision):
    doc = tomlkit.document()
    doc["long"] = PyDecimal("3.14159265358979323846264338327950288419")
    doc["big"] = PyDecimal("1.23456789e300")
    doc["tiny"] = PyDecimal("1.5e-320")

    out = tomlkit.dumps(doc)
    assert "long = 3.14159265358979323846264338327950288419\n" in out

    # Output is syntactically valid TOML.
    parsed = tomllib.loads(out)
    assert parsed["long"] == float("3.14159265358979323846264338327950288419")
    assert parsed["big"] == float("1.23456789e300")
    assert parsed["tiny"] == float("1.5e-320")

    # Exact values survive a decimal-mode round-trip.
    reparsed = tomlkit.parse(out, parse_float=PyDecimal)
    assert reparsed["long"].unwrap() == PyDecimal(
        "3.14159265358979323846264338327950288419"
    )
    assert reparsed["big"].unwrap() == PyDecimal("1.23456789e300")
    assert reparsed["tiny"].unwrap() == PyDecimal("1.5e-320")


def test_decimal_negative_zero():
    doc = tomlkit.parse("x = -0.0\n", parse_float=PyDecimal)

    assert doc["x"].unwrap().is_signed()
    assert tomlkit.dumps(doc) == "x = -0.0\n"

    # Writing a negative zero keeps the sign and stays a float lexeme.
    doc["y"] = PyDecimal("-0")
    out = tomlkit.dumps(doc)
    assert "y = -0.0\n" in out
    assert tomllib.loads(out)["y"] == -0.0

    # Comparison follows decimal semantics: -0 == 0, but the sign is kept.
    assert PyDecimal("-0") == PyDecimal("0")
    assert PyDecimal("-0").is_signed()
    assert not PyDecimal("0").is_signed()


def test_decimal_inf_and_nan():
    src = "a = inf\nb = -inf\nc = nan\n"
    doc = tomlkit.parse(src, parse_float=PyDecimal)

    assert doc["a"].unwrap() == PyDecimal("Infinity")
    assert doc["b"].unwrap() == PyDecimal("-Infinity")
    assert doc["c"].unwrap().is_nan()
    assert tomlkit.dumps(doc) == src

    # Writing non-finite decimals produces TOML inf/nan spellings.
    doc["d"] = PyDecimal("Infinity")
    doc["e"] = PyDecimal("-Infinity")
    doc["f"] = PyDecimal("NaN")
    out = tomlkit.dumps(doc)
    assert "d = inf\n" in out
    assert "e = -inf\n" in out
    assert "f = nan\n" in out
    tomllib.loads(out)

    # Comparison follows decimal semantics: nan is never equal to itself.
    assert PyDecimal("Infinity") == PyDecimal("Infinity")
    assert PyDecimal("NaN") != PyDecimal("NaN")


def test_decimal_arithmetic_and_reassignment():
    doc = tomlkit.parse("x = 0.1\n", parse_float=PyDecimal)

    doc["x"] = doc["x"] + PyDecimal("0.2")

    # Exact decimal arithmetic, free of binary float error.
    assert doc["x"].unwrap() == PyDecimal("0.3")
    assert tomlkit.dumps(doc) == "x = 0.3\n"

    doc["x"] = doc["x"] * 2
    assert tomlkit.dumps(doc) == "x = 0.6\n"

    # Integral results are still written as float lexemes.
    doc["x"] = doc["x"] * 10
    out = tomlkit.dumps(doc)
    assert out == "x = 6.0\n"
    assert isinstance(tomllib.loads(out)["x"], float)


def test_decimal_in_arrays_and_inline_tables():
    src = "arr = [1.1, 2.2]\ntbl = {v = 3.3}\n"
    doc = tomlkit.parse(src, parse_float=PyDecimal)

    unwrapped = doc.unwrap()
    assert unwrapped["arr"] == [PyDecimal("1.1"), PyDecimal("2.2")]
    assert unwrapped["tbl"] == {"v": PyDecimal("3.3")}
    assert tomlkit.dumps(doc) == src

    # New decimals inside containers follow the same lexeme rules.
    doc["arr"].append(PyDecimal("4"))
    doc["tbl"]["w"] = PyDecimal("5.5")
    out = tomlkit.dumps(doc)
    assert "4.0" in out
    assert "w = 5.5" in out
    tomllib.loads(out)

    # Arithmetic on nested values re-assigns through the same rules.
    doc["tbl"]["v"] = doc["tbl"]["v"] + PyDecimal("0.1")
    assert doc["tbl"]["v"].unwrap() == PyDecimal("3.4")
    assert "v = 3.4" in tomlkit.dumps(doc)


def test_decimal_item_pickles():
    doc = tomlkit.parse("x = 1.5\n", parse_float=PyDecimal)

    restored = pickle.loads(pickle.dumps(doc["x"]))

    assert isinstance(restored, Decimal)
    assert restored.unwrap() == PyDecimal("1.5")
    assert restored.as_string() == "1.5"


def test_decimal_item_unaffected_by_context_precision(low_precision):
    item = tomlkit.item(PyDecimal("1.23456789"))

    assert isinstance(item, Decimal)
    assert item.unwrap() == PyDecimal("1.23456789")
    assert item.as_string() == "1.23456789"


def test_default_float_mode_is_unchanged():
    src = "x = 0.1\n"
    doc = tomlkit.parse(src)

    assert isinstance(doc["x"], Float)
    assert isinstance(doc["x"].unwrap(), float)
    assert doc.unwrap() == {"x": 0.1}

    doc["x"] = doc["x"] + 0.2
    assert doc["x"].unwrap() == 0.1 + 0.2
    assert tomlkit.dumps(doc) == f"x = {0.1 + 0.2}\n"


def test_decimal_still_dispatches_to_custom_encoders():
    seen = []

    @tomlkit.register_encoder
    def encode_decimal(obj):
        if isinstance(obj, PyDecimal):
            seen.append(obj)
            return tomlkit.float_(str(obj))
        raise TypeError

    try:
        result = tomlkit.item(PyDecimal("1.23"))
        assert seen == [PyDecimal("1.23")]
        assert isinstance(result, Float)
        assert result.as_string() == "1.23"
    finally:
        tomlkit.unregister_encoder(encode_decimal)
