"""Amounts as they cross a boundary: plain decimal strings, never floats or exponent notation."""

from decimal import Decimal


def plain_decimal(value: Decimal) -> str:
    """``Decimal("5E+6")`` becomes ``"5000000"``; trailing zeros are dropped."""
    return format(value.normalize(), "f")
