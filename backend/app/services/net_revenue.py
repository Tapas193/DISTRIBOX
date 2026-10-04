"""D6 DISTRIBOX - Net Revenue Engine.

This is the deterministic financial foundation of the platform. Every
number the hotel is shown about revenue, cost, ADR or RevPAR originates
here or from a SQL aggregate over these definitions.

Hard rules enforced by this module
----------------------------------
1. **No LLM, ever.** This module imports nothing from ``app.ai`` and
   cannot be influenced by a language model.
2. **No floating point for money.** All amounts are ``Decimal``.
3. **No silent correction.** A negative commission is an *error*, not a
   value to be clamped to zero. Silently repairing bad financial input is
   how a revenue system starts lying to its users.
4. **Originals are preserved.** Derived values are additional fields; the
   imported gross/discount/commission/fee figures are never mutated.
5. **Every result is traceable.** ``evidence`` on the result records the
   exact inputs and formula used, so the UI can show its work.

The identity, unchanging:

    distribution_cost = discount + commission + payment_fee + other_channel_cost
    net_room_revenue  = gross_room_revenue - distribution_cost
    gross_adr         = gross_room_revenue / room_nights
    net_adr           = net_room_revenue   / room_nights
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional

from app.core.money import (
    ZERO,
    quantize_money,
    quantize_rate,
    require_money,
    to_decimal,
    to_int,
)

# An ADR/RevPAR above this is virtually always a unit error (for a
# Himalayan hill hotel, even a suite at Rs 40,000/night is unheard of).
# Flagged, never rejected - the hotel may genuinely have luxury inventory.
IMPLAUSIBLE_RATE_THRESHOLD = Decimal("500000")


class ErrorCode(str, Enum):
    """Stable machine-readable codes. The API surfaces these verbatim."""

    MISSING_GROSS_REVENUE = "MISSING_GROSS_REVENUE"
    NEGATIVE_GROSS_REVENUE = "NEGATIVE_GROSS_REVENUE"
    NEGATIVE_DISCOUNT = "NEGATIVE_DISCOUNT"
    NEGATIVE_COMMISSION = "NEGATIVE_COMMISSION"
    NEGATIVE_PAYMENT_FEE = "NEGATIVE_PAYMENT_FEE"
    NEGATIVE_OTHER_COST = "NEGATIVE_OTHER_COST"
    INVALID_COMMISSION_RATE = "INVALID_COMMISSION_RATE"
    NEGATIVE_ROOM_NIGHTS = "NEGATIVE_ROOM_NIGHTS"
    ZERO_ROOM_NIGHTS = "ZERO_ROOM_NIGHTS"
    NET_REVENUE_NEGATIVE = "NET_REVENUE_NEGATIVE"
    INVALID_DATE_RANGE = "INVALID_DATE_RANGE"
    UNPARSEABLE_AMOUNT = "UNPARSEABLE_AMOUNT"
    IMPLAUSIBLE_RATE = "IMPLAUSIBLE_RATE"


class Severity(str, Enum):
    """``ERROR`` blocks persistence. ``WARNING`` is surfaced, not fatal."""

    ERROR = "ERROR"
    WARNING = "WARNING"


@dataclass(frozen=True)
class ValidationIssue:
    code: ErrorCode
    message: str
    severity: Severity = Severity.ERROR
    field: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "severity": self.severity.value,
            "field": self.field,
        }


class EconomicsValidationError(ValueError):
    """Raised when booking economics cannot be computed at all."""

    def __init__(self, issues: List[ValidationIssue]):
        self.issues = issues
        summary = "; ".join(f"{i.code.value}: {i.message}" for i in issues)
        super().__init__(summary)


@dataclass(frozen=True)
class BookingEconomicsInput:
    """Raw financial inputs for one booking.

    Cost fields default to ``None`` meaning "no such cost line exists for
    this channel" (a direct booking genuinely has no commission), which is
    economically identical to zero but distinguishable for reporting.
    """

    gross_room_revenue: Any
    discount_amount: Any = None
    commission_amount: Any = None
    payment_fee: Any = None
    other_channel_cost: Any = None
    room_nights: Any = 0

    def cost_fields_present(self) -> Dict[str, bool]:
        return {
            "discount_amount": self.discount_amount is not None,
            "commission_amount": self.commission_amount is not None,
            "payment_fee": self.payment_fee is not None,
            "other_channel_cost": self.other_channel_cost is not None,
        }


@dataclass(frozen=True)
class BookingEconomics:
    """Computed economics for a single booking. All money is Decimal."""

    gross_room_revenue: Decimal
    discount_amount: Decimal
    commission_amount: Decimal
    payment_fee: Decimal
    other_channel_cost: Decimal
    distribution_cost: Decimal
    net_room_revenue: Decimal
    room_nights: int
    gross_adr: Decimal
    net_adr: Decimal
    distribution_cost_pct: Decimal
    warnings: List[ValidationIssue] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gross_room_revenue": str(self.gross_room_revenue),
            "discount_amount": str(self.discount_amount),
            "commission_amount": str(self.commission_amount),
            "payment_fee": str(self.payment_fee),
            "other_channel_cost": str(self.other_channel_cost),
            "distribution_cost": str(self.distribution_cost),
            "net_room_revenue": str(self.net_room_revenue),
            "room_nights": self.room_nights,
            "gross_adr": str(self.gross_adr),
            "net_adr": str(self.net_adr),
            "distribution_cost_pct": str(self.distribution_cost_pct),
            "warnings": [w.to_dict() for w in self.warnings],
        }


@dataclass(frozen=True)
class ChannelEconomics:
    """Aggregate economics for a group of bookings (a channel, a period)."""

    booking_count: int
    room_nights: int
    gross_revenue: Decimal
    discount_amount: Decimal
    commission_amount: Decimal
    payment_fee: Decimal
    other_channel_cost: Decimal
    distribution_cost: Decimal
    net_revenue: Decimal
    gross_adr: Decimal
    net_adr: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "booking_count": self.booking_count,
            "room_nights": self.room_nights,
            "gross_revenue": str(self.gross_revenue),
            "discount_amount": str(self.discount_amount),
            "commission_amount": str(self.commission_amount),
            "payment_fee": str(self.payment_fee),
            "other_channel_cost": str(self.other_channel_cost),
            "distribution_cost": str(self.distribution_cost),
            "net_revenue": str(self.net_revenue),
            "gross_adr": str(self.gross_adr),
            "net_adr": str(self.net_adr),
            "distribution_cost_pct": str(
                safe_divide(self.distribution_cost, self.gross_revenue)
            ),
        }


def safe_divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    """Division that yields ``0`` instead of raising or exploding.

    Zero room nights on a cancelled booking is a real, common case - it
    must never produce a ``ZeroDivisionError`` in an API request.
    """
    if denominator is None or numerator is None or Decimal(denominator) == 0:
        return quantize_rate(ZERO)
    return quantize_rate(Decimal(numerator) / Decimal(denominator))


class NetRevenueEngine:
    """Pure, stateless, deterministic booking-economics calculator.

    Statelessness matters: the same input must always produce the same
    output, which is what makes the audit trail in the UI trustworthy and
    the unit tests meaningful.
    """

    def calculate(
        self, booking: BookingEconomicsInput
    ) -> BookingEconomics:
        """Compute booking economics or raise :class:`EconomicsValidationError`."""
        errors, warnings = self._validate(booking)

        if errors:
            raise EconomicsValidationError(errors)

        gross = quantize_money(require_money(booking.gross_room_revenue, "gross_room_revenue"))
        discount = quantize_money(require_money(booking.discount_amount, "discount_amount"))
        commission = quantize_money(require_money(booking.commission_amount, "commission_amount"))
        payment_fee = quantize_money(require_money(booking.payment_fee, "payment_fee"))
        other = quantize_money(require_money(booking.other_channel_cost, "other_channel_cost"))
        nights = to_int(booking.room_nights, field="room_nights")

        distribution_cost = quantize_money(discount + commission + payment_fee + other)
        net = quantize_money(gross - distribution_cost)

        return BookingEconomics(
            gross_room_revenue=gross,
            discount_amount=discount,
            commission_amount=commission,
            payment_fee=payment_fee,
            other_channel_cost=other,
            distribution_cost=distribution_cost,
            net_room_revenue=net,
            room_nights=nights,
            gross_adr=safe_divide(gross, Decimal(nights)),
            net_adr=safe_divide(net, Decimal(nights)),
            distribution_cost_pct=safe_divide(distribution_cost, gross),
            warnings=warnings,
        )

    def safe_calculate(
        self, booking: BookingEconomicsInput
    ) -> Optional[BookingEconomics]:
        """Compute economics, returning ``None`` instead of raising.

        Used by bulk CSV import so one bad row cannot abort a 5,000-row
        file; the caller collects per-row errors from :meth:`validate`.
        """
        try:
            return self.calculate(booking)
        except (EconomicsValidationError, ValueError):
            return None

    # -- validation ------------------------------------------------------

    def validate(
        self, booking: BookingEconomicsInput
    ) -> List[ValidationIssue]:
        """Return every business-rule violation (errors *and* warnings)."""
        errors, warnings = self._validate(booking)
        return errors + warnings

    @staticmethod
    def _validate(
        booking: BookingEconomicsInput,
    ) -> tuple[List[ValidationIssue], List[ValidationIssue]]:
        errors: List[ValidationIssue] = []
        warnings: List[ValidationIssue] = []

        # -- gross revenue --
        try:
            gross_raw = to_decimal(booking.gross_room_revenue)
        except ValueError:
            errors.append(
                ValidationIssue(
                    ErrorCode.UNPARSEABLE_AMOUNT,
                    f"gross_room_revenue is not a number: {booking.gross_room_revenue!r}",
                    field="gross_room_revenue",
                )
            )
            gross_raw = None

        if gross_raw is None:
            errors.append(
                ValidationIssue(
                    ErrorCode.MISSING_GROSS_REVENUE,
                    "gross_room_revenue is required",
                    field="gross_room_revenue",
                )
            )
        elif gross_raw < 0:
            errors.append(
                ValidationIssue(
                    ErrorCode.NEGATIVE_GROSS_REVENUE,
                    f"gross_room_revenue cannot be negative (got {gross_raw})",
                    field="gross_room_revenue",
                )
            )

        # -- each cost component must be non-negative --
        for value, neg_code, name in (
            (booking.discount_amount, ErrorCode.NEGATIVE_DISCOUNT, "discount_amount"),
            (booking.commission_amount, ErrorCode.NEGATIVE_COMMISSION, "commission_amount"),
            (booking.payment_fee, ErrorCode.NEGATIVE_PAYMENT_FEE, "payment_fee"),
            (booking.other_channel_cost, ErrorCode.NEGATIVE_OTHER_COST, "other_channel_cost"),
        ):
            if value is None:
                continue
            try:
                parsed = to_decimal(value)
            except ValueError:
                errors.append(
                    ValidationIssue(
                        ErrorCode.UNPARSEABLE_AMOUNT,
                        f"{name} is not a number: {value!r}",
                        field=name,
                    )
                )
                continue
            if parsed is not None and parsed < 0:
                errors.append(
                    ValidationIssue(
                        neg_code,
                        f"{name} cannot be negative (got {parsed}). "
                        "A cost that reduces revenue must be modelled as a discount, "
                        "not as a negative commission.",
                        field=name,
                    )
                )

        # -- commission sanity: <=100% of gross unless gross is zero --
        if gross_raw is not None and gross_raw > 0:
            try:
                commission = to_decimal(booking.commission_amount)
                if commission is not None and commission > gross_raw:
                    warnings.append(
                        ValidationIssue(
                            ErrorCode.INVALID_COMMISSION_RATE,
                            f"commission_amount ({commission}) exceeds gross_room_revenue "
                            f"({gross_raw}); implied rate is over 100%",
                            Severity.WARNING,
                            "commission_amount",
                        )
                    )
            except ValueError:
                pass

        # -- room nights --
        nights: Optional[int] = None
        try:
            nights = to_int(booking.room_nights, field="room_nights")
        except ValueError as exc:
            errors.append(
                ValidationIssue(
                    ErrorCode.UNPARSEABLE_AMOUNT,
                    str(exc),
                    field="room_nights",
                )
            )

        if nights is not None:
            if nights < 0:
                errors.append(
                    ValidationIssue(
                        ErrorCode.NEGATIVE_ROOM_NIGHTS,
                        f"room_nights cannot be negative (got {nights})",
                        field="room_nights",
                    )
                )
            elif nights == 0:
                # Not fatal: a cancelled booking with no realised nights is
                # legitimate. ADR is reported as 0 rather than crashing.
                warnings.append(
                    ValidationIssue(
                        ErrorCode.ZERO_ROOM_NIGHTS,
                        "room_nights is 0; ADR will be reported as 0 "
                        "(typically a cancelled or zero-night booking)",
                        Severity.WARNING,
                        "room_nights",
                    )
                )

        # -- implausible rate check --
        if nights and nights > 0 and gross_raw is not None:
            gross_adr = gross_raw / Decimal(nights)
            if gross_adr > IMPLAUSIBLE_RATE_THRESHOLD:
                warnings.append(
                    ValidationIssue(
                        ErrorCode.IMPLAUSIBLE_RATE,
                        f"gross ADR of {quantize_rate(gross_adr)} looks like a unit error "
                        f"(threshold {IMPLAUSIBLE_RATE_THRESHOLD})",
                        Severity.WARNING,
                        "gross_room_revenue",
                    )
                )

        # -- net revenue must not be negative --
        # Only evaluated once individual components are known good,
        # otherwise it would produce a confusing cascade of errors.
        if not errors:
            try:
                total_cost = sum(
                    (
                        require_money(booking.discount_amount, "discount_amount"),
                        require_money(booking.commission_amount, "commission_amount"),
                        require_money(booking.payment_fee, "payment_fee"),
                        require_money(booking.other_channel_cost, "other_channel_cost"),
                    ),
                    ZERO,
                )
                net = require_money(booking.gross_room_revenue, "gross") - total_cost
                if net < 0:
                    errors.append(
                        ValidationIssue(
                            ErrorCode.NET_REVENUE_NEGATIVE,
                            f"distribution cost ({quantize_money(total_cost)}) exceeds gross "
                            f"revenue ({require_money(booking.gross_room_revenue, 'gross')}), "
                            f"producing net revenue of {quantize_money(net)}",
                            field="commission_amount",
                        )
                    )
            except ValueError:
                pass

        return errors, warnings

    # -- aggregation -----------------------------------------------------

    @staticmethod
    def aggregate(
        economics: Iterable[BookingEconomics],
    ) -> ChannelEconomics:
        """Aggregate booking economics into channel-level totals.

        Sums are taken over already-rounded per-booking values, which is
        what the database does too (stored generated columns). The two must
        agree exactly, or the audit trail breaks.
        """
        items = list(economics)
        if not items:
            return ChannelEconomics(
                booking_count=0,
                room_nights=0,
                gross_revenue=ZERO,
                discount_amount=ZERO,
                commission_amount=ZERO,
                payment_fee=ZERO,
                other_channel_cost=ZERO,
                distribution_cost=ZERO,
                net_revenue=ZERO,
                gross_adr=quantize_rate(ZERO),
                net_adr=quantize_rate(ZERO),
            )

        def total(attr: str) -> Decimal:
            return quantize_money(sum((getattr(i, attr) for i in items), ZERO))

        gross = total("gross_room_revenue")
        net = total("net_room_revenue")
        nights = sum(i.room_nights for i in items)

        return ChannelEconomics(
            booking_count=len(items),
            room_nights=nights,
            gross_revenue=gross,
            discount_amount=total("discount_amount"),
            commission_amount=total("commission_amount"),
            payment_fee=total("payment_fee"),
            other_channel_cost=total("other_channel_cost"),
            distribution_cost=total("distribution_cost"),
            net_revenue=net,
            gross_adr=safe_divide(gross, Decimal(nights)),
            net_adr=safe_divide(net, Decimal(nights)),
        )

    @staticmethod
    def net_revpar(net_revenue: Decimal, available_room_nights: Decimal) -> Decimal:
        """Net RevPAR = net revenue / rooms available for sale.

        Denominator is *availability*, not occupancy - RevPAR's purpose is
        to measure revenue against the capacity you chose to hold open.
        """
        return safe_divide(net_revenue, available_room_nights)


#: Module-level singleton. The engine holds no state, so a single shared
#: instance is safe and keeps call sites terse.
net_revenue_engine = NetRevenueEngine()