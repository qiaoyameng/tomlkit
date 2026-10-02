import decimal
import pickle

from decimal import Decimal as PyDecimal

import pytest

import tomlkit

from tomlkit import parse
from tomlkit.exceptions import InvalidNumberError
from tomlkit.items import Decimal
from tomlkit.items import Float
from tomlkit.items import Integer


tomllib = pytest.importorskip("tomllib")


def test_decimal_mode_parses_finite_floats_as_decimal():
    doc = parse("a = 1.2300000000000000000001\nb = 1e2\n", parse_float=PyDecimal)

    assert isinstance(doc["a"], Decimal)
    assert isinstance(doc["a"].unwrap(), PyDecimal)
    assert doc["a"].unwrap() == PyDecimal("1.2300000000000000000001")
    assert doc["b"].unwrap() == PyDecimal("1e2")
    # the item itself compares like a Decimal
    assert doc["a"] == PyDecimal("1.2300000000000000000001")


def test_decimal_mode_preserves_original_literal():
    src = "a = +1.5e1_0\nb = 1_000.000_1\nc = 6.02e+23\nd = -0.0\n"
    doc = parse(src, parse_float=PyDecimal)

    assert tomlkit.dumps(doc) == src


def test_decimal_mode_unaffected_by_context_precision():
    src = "a = 1.234567890123456789012345678901234567890123456789\nb = 2.5e+5000\n"
    with decimal.localcontext(prec=1):
        doc = parse(src, parse_float=PyDecimal)

        assert doc["a"].unwrap() == PyDecimal(
            "1.234567890123456789012345678901234567890123456789"
        )
        assert doc["b"].unwrap() == PyDecimal("2.5e+5000")
        assert tomlkit.dumps(doc) == src

    # a float would have lost the exact value entirely
    assert parse(src)["b"].unwrap() == float("inf")


def test_decimal_mode_output_is_valid_toml_for_tomllib():
    src = "a = 1.234567890123456789\nb = 2.5e+5000\nc = -0.0\n"
    with decimal.localcontext(prec=1):
        doc = parse(src, parse_float=PyDecimal)
        doc["d"] = PyDecimal("3")
        doc["e"] = PyDecimal("1E+10")
        out = tomlkit.dumps(doc)

    parsed = tomllib.loads(out)
    assert isinstance(parsed["d"], float)
    assert parsed["d"] == 3.0
    assert parsed["e"] == 1e10
    assert parsed["b"] == float("inf")


def test_decimal_mode_negative_zero():
    doc = parse("a = -0.0\n", parse_float=PyDecimal)

    assert doc["a"].unwrap().is_signed()
    # comparison follows Decimal semantics: negative zero equals zero
    assert doc["a"] == PyDecimal("0.0")
    assert tomlkit.dumps(doc) == "a = -0.0\n"

    doc["b"] = PyDecimal("-0")
    assert "b = -0.0" in tomlkit.dumps(doc)


def test_decimal_mode_inf_and_nan_stay_floats():
    src = "a = inf\nb = -inf\nc = nan\n"
    doc = parse(src, parse_float=PyDecimal)

    for key, expected in (("a", float("inf")), ("b", float("-inf"))):
        assert isinstance(doc[key], Float)
        assert doc[key].unwrap() == expected

    assert isinstance(doc["c"], Float)
    assert doc["c"].unwrap() != doc["c"].unwrap()  # nan != nan
    assert tomlkit.dumps(doc) == src


def test_decimal_mode_integral_decimal_is_written_as_float():
    doc = tomlkit.document()
    doc["a"] = PyDecimal("3")
    doc["b"] = PyDecimal("1E+10")
    doc["c"] = PyDecimal("0.00001")

    out = tomlkit.dumps(doc)
    assert "a = 3.0" in out
    assert "b = 1E+10" in out

    # the values must not come back as integers
    reparsed = parse(out)
    assert isinstance(reparsed["a"], Float)
    assert not isinstance(reparsed["a"], Integer)
    assert isinstance(reparsed["b"], Float)
    tomllib.loads(out)


def test_decimal_mode_non_finite_decimal_written_as_toml_literals():
    doc = tomlkit.document()
    doc["a"] = PyDecimal("Infinity")
    doc["b"] = PyDecimal("-Infinity")
    doc["c"] = PyDecimal("NaN")
    doc["d"] = PyDecimal("-NaN")

    out = tomlkit.dumps(doc)
    assert "a = inf" in out
    assert "b = -inf" in out
    assert "c = nan" in out
    assert "d = -nan" in out

    parsed = tomllib.loads(out)
    assert parsed["a"] == float("inf")
    assert parsed["b"] == float("-inf")
    assert parsed["c"] != parsed["c"]


def test_decimal_mode_unwraps_containers_to_decimal():
    doc = parse(
        "arr = [1.5, 2.50]\nt = {x = 0.25, y = 3}\n[tbl]\nv = 1.1\n",
        parse_float=PyDecimal,
    )
    unwrapped = doc.unwrap()

    assert unwrapped["arr"] == [PyDecimal("1.5"), PyDecimal("2.50")]
    assert unwrapped["t"] == {"x": PyDecimal("0.25"), "y": 3}
    assert unwrapped["tbl"]["v"] == PyDecimal("1.1")
    assert isinstance(doc["arr"][0], Decimal)
    assert isinstance(doc["t"]["x"], Decimal)
    assert isinstance(doc["tbl"]["v"], Decimal)


def test_decimal_mode_arithmetic_and_reassignment():
    doc = parse("a = 0.1\narr = [1.5]\nt = {x = 0.25}\n", parse_float=PyDecimal)

    doc["a"] = doc["a"] + PyDecimal("0.2")
    assert isinstance(doc["a"], Decimal)
    assert doc["a"].unwrap() == PyDecimal("0.3")

    doc["arr"][0] = doc["arr"][0] * PyDecimal("2")
    doc["t"]["x"] = doc["t"]["x"] + PyDecimal("0.75")

    out = tomlkit.dumps(doc)
    assert "a = 0.3" in out
    assert "arr = [3.0]" in out
    assert "t = {x = 1.00}" in out
    tomllib.loads(out)


def test_decimal_mode_arithmetic_keeps_explicit_sign():
    doc = parse("a = +1.5\n", parse_float=PyDecimal)
    doc["a"] = doc["a"] + PyDecimal("1")

    assert "a = +2.5" in tomlkit.dumps(doc)


def test_decimal_mode_item_from_decimal():
    it = tomlkit.item(PyDecimal("1.5"))

    assert isinstance(it, Decimal)
    assert it.as_string() == "1.5"
    assert it.unwrap() == PyDecimal("1.5")


def test_decimal_mode_item_pickle_roundtrip():
    doc = parse("a = 1.234567890123456789\n", parse_float=PyDecimal)
    restored = pickle.loads(pickle.dumps(doc["a"]))

    assert isinstance(restored, Decimal)
    assert restored.unwrap() == PyDecimal("1.234567890123456789")
    assert restored.as_string() == "1.234567890123456789"


def test_decimal_mode_rejects_exponent_beyond_decimal_range():
    with pytest.raises(InvalidNumberError):
        parse("a = 1e999999999999999999999\n", parse_float=PyDecimal)


def test_default_float_mode_is_unchanged():
    src = "a = 1.2300000000000000000001\nb = +1.5e1_0\nc = inf\n"
    doc = parse(src)

    assert isinstance(doc["a"], Float)
    assert isinstance(doc["a"].unwrap(), float)
    assert doc["a"].unwrap() == float("1.2300000000000000000001")
    assert isinstance(doc["c"].unwrap(), float)
    assert tomlkit.dumps(doc) == src

    # loads/load accept the same keyword
    assert isinstance(tomlkit.loads("a = 1.5\n")["a"], Float)
    assert isinstance(
        tomlkit.loads("a = 1.5\n", parse_float=PyDecimal)["a"], Decimal
    )
