"""Missing values are never numeric zero. Helpers shared by WB adapters."""
from decimal import Decimal, InvalidOperation
from math import isfinite


def nm_id(value):
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
        return int(number) if number.is_finite() and number > 0 and number == number.to_integral_value() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def number(value):
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        parsed = float(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))
        return parsed if isfinite(parsed) and parsed >= 0 else None
    except (TypeError, ValueError):
        return None


def count(value):
    parsed = number(value)
    return int(parsed) if parsed is not None and parsed.is_integer() else None


def complete_sum(values):
    values = list(values)
    return sum(values) if values and all(v is not None for v in values) else None


def percent(numerator, denominator):
    return numerator / denominator * 100 if numerator is not None and denominator is not None and denominator > 0 else None
