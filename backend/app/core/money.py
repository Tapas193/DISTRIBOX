"""Deterministic money handling for D6 DISTRIBOX.

Every monetary amount in this platform is a :class:`decimal.Decimal`.
``float`` is never used for money, anywhere. Floating point cannot
represent 0.1 exactly, and a revenue-management system that is off by a
paisa on 5,000 bookings is a revenue-management system that cannot be
trusted.

This module also owns *coercion* of messy external values (CSV cells such
as ``"Rs. 4,000"``, ``"(600)"``, ``""``) into clean ``Decimal`` or
``None``. Coercion is intentionally separate from validation: parsing is
about shape, validation is about business rules.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional

#: Money is rounded to paise (2 dp).
MONEY_EXPONENT = Decimal("0.01")
#: Rates (ADR, RevPAR, percentages-of-aggregate) keep 4 dp for precision
#: while staying exact enough for currency presentation.
RATE_EXPONENT = Decimal("0.0001")

#: Characters that commonly wrap or decorate currency in exported files.
_CURRENCY_NOISE = re.compile(r"[₹$€£]|rs\.?|\s|usd|inr|eur|gbp", re.IGNORECASE)

ZERO = Decimal("0.00")


class MoneyParseError(ValueError):
    """Raised when a raw value cannot be interpreted as money at all."""


def quantize_money(value: Decimal) -> Decimal:
    """Round to paise using banker-safe HALF_UP (what finance expects)."""
    return Decimal(value).quantize(MONEY_EXPONENT, rounding=ROUND_HALF_UP)


def quantize_rate(value: Decimal) -> Decimal:
    """Round an ADR/RevPAR-style rate to 4 decimal places."""
    return Decimal(value).quantize(RATE_EXPONENT, rounding=ROUND_HALF_UP)


def to_decimal(value: Any) -> Optional[Decimal]:
    """Best-effort conversion of an arbitrary value to ``Decimal``.

    Returns ``None`` when the value is absent or blank, so that callers
    can distinguish "not supplied" from "supplied as zero". Raises
    :class:`MoneyParseError` for values that are present but unparseable.
    """
    if value is None:
        return None

    if isinstance(value, Decimal):
        return value

    if isinstance(value, bool):
        # bool is an int subclass; treating True as Rs.1 would be a silent
        # data-corruption bug rather than a helpful error.
        raise MoneyParseError(f"boolean is not a monetary value: {value!r}")

    if isinstance(value, int):
        return Decimal(value)

    if isinstance(value, float):
        # Reached only for third-party inputs. Decimal(float) is exact to
        # the float, so str() first to get the shortest intended repr.
        return Decimal(str(value))

    text = str(value).strip()
    if text == "" or text.lower() in {"na", "n/a", "null", "none", "-"}:
        return None

    # Accounting notation: (600) means -600.
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    text = _CURRENCY_NOISE.sub("", text)
    text = text.replace(",", "").strip()

    if text.startswith("-"):
        negative = True
        text = text[1:].strip()

    if text in {"", "+"}:
        raise MoneyParseError(f"not a monetary value: {value!r}")

    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise MoneyParseError(f"not a monetary value: {value!r}") from exc

    return -result if negative else result


def require_money(value: Any, field: str) -> Decimal:
    """Coerce to Decimal, treating absent values as zero.

    Missing *cost* fields legitimately mean "no cost incurred" (a direct
    booking has no commission line at all). Missing *revenue* fields do
    not, and are caught by the engine's validation stage.
    """
    parsed = to_decimal(value)
    return ZERO if parsed is None else parsed


def money_or_none(value: Any) -> Optional[Decimal]:
    """Coerce to Decimal, propagating absence as ``None``."""
    return to_decimal(value)


def to_int(value: Any, *, field: str = "value") -> int:
    """Coerce an integer-ish external value (may arrive as ``"2"``)."""
    if value is None:
        raise MoneyParseError(f"{field} is required")
    if isinstance(value, bool):
        raise MoneyParseError(f"{field} must be an integer, got boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise MoneyParseError(f"{field} must be a whole number, got {value}")
        return int(value)
    if isinstance(value, float):
        if not float(value).is_integer():
            raise MoneyParseError(f"{field} must be a whole number, got {value}")
        return int(value)

    text = str(value).strip()
    if text == "":
        raise MoneyParseError(f"{field} is required")
    try:
        return int(Decimal(text))
    except (InvalidOperation, ValueError) as exc:
        raise MoneyParseError(f"{field} must be an integer, got {value!r}") from exc


def decimal_to_float(value: Optional[Decimal]) -> Optional[float]:
    """Convert to float for JSON serialisation only.

    Never feed the result back into a financial calculation.
    """
    if value is None:
        return None
    return float(value)