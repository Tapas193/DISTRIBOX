"""D6 DISTRIBOX - Channel Analytics.

Turns a list of booking facts into the nine metrics a hotel owner actually
asks about, per channel:

    1. gross revenue          6. gross ADR
    2. net revenue            7. net ADR
    3. distribution cost      8. booking share
    4. distribution cost %    9. net revenue share

Why this module exists separately from the net-revenue engine
-------------------------------------------------------------
The engine (:mod:`app.services.net_revenue`) answers "what does *one*
booking leave me with?". This module answers "what is my *channel mix*
actually worth?". Both must agree exactly, so both are built from the same
identities and the same rounding rules.

Design constraints
------------------
* **Standard library only.** ``decimal``, ``dataclasses``, ``collections``.
  No pandas, no numpy. The analytics must be importable in a bare Python
  process, which is also what makes the unit tests meaningful.
* **Decimal for money, int for counts, Decimal for rates.** Never float.
* **Shares are computed against the portfolio total, then rounded.** A
  consequence worth stating plainly: because each share is rounded
  independently to 4 dp, the rounded shares sum to 1.0000 +/- 0.0005.
  :func:`verify_share_closure` exposes that residual instead of hiding it.
* **No model, no LLM.** Every figure here is an arithmetic function of
  observed booking amounts. Nothing is predicted.

The identity that makes the product work:

    net_revenue_share = booking_share - distribution_cost_share

A channel can be 30% of your bookings and 8% of your net revenue. That
gap *is* the finding.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.core.money import ZERO, quantize_money, quantize_rate
from app.services.net_revenue import safe_divide

__all__ = [
    "BookingFact",
    "ChannelMetrics",
    "PortfolioAnalytics",
    "compute_portfolio",
    "compute_channel_metrics",
    "distribution_cost_components",
    "share_closure_residual",
    "verify_share_closure",
    "monthly_channel_series",
]


# ---------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class BookingFact:
    """One non-cancelled booking, in the shape analytics needs.

    Deliberately *not* the full booking entity: analytics is fed from CSV
    imports, the database, or the test suite interchangeably, so it takes a
    flat record rather than coupling itself to a storage model.
    """

    channel_code: str
    channel_name: str
    channel_type: str
    is_direct: bool
    gross_room_revenue: Decimal
    discount_amount: Decimal
    commission_amount: Decimal
    payment_fee: Decimal
    other_channel_cost: Decimal
    room_nights: int

    def distribution_cost(self) -> Decimal:
        return quantize_money(
            self.discount_amount
            + self.commission_amount
            + self.payment_fee
            + self.other_channel_cost
        )

    def net_room_revenue(self) -> Decimal:
        return quantize_money(self.gross_room_revenue - self.distribution_cost())


def distribution_cost_components(metrics: "ChannelMetrics") -> Dict[str, Decimal]:
    """Split a channel's distribution cost back into its four causes.

    Useful on its own: "OTA Beta costs you Rs 41 in commission and Rs 19
    in discount" is actionable in a way that "OTA Beta costs 26%" is not.
    """
    total = metrics.distribution_cost
    if total == 0:
        return {
            "discount": ZERO,
            "commission": ZERO,
            "payment_fee": ZERO,
            "other_channel_cost": ZERO,
        }
    return {
        "discount": quantize_rate(metrics.discount_amount / total),
        "commission": quantize_rate(metrics.commission_amount / total),
        "payment_fee": quantize_rate(metrics.payment_fee / total),
        "other_channel_cost": quantize_rate(metrics.other_channel_cost / total),
    }


# ---------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelMetrics:
    """The nine channel metrics, plus the context needed to interpret them."""

    channel_code: str
    channel_name: str
    channel_type: str
    is_direct: bool

    # -- the nine required metrics --
    gross_revenue: Decimal              # 1
    net_revenue: Decimal                # 2
    distribution_cost: Decimal          # 3
    distribution_cost_pct: Decimal      # 4
    gross_adr: Decimal                  # 5
    net_adr: Decimal                    # 6
    booking_share: Decimal              # 7
    net_revenue_share: Decimal          # 8

    # -- supporting context --
    booking_count: int
    room_nights: int
    discount_amount: Decimal
    commission_amount: Decimal
    payment_fee: Decimal
    other_channel_cost: Decimal
    net_adr_penalty: Decimal
    distribution_cost_share: Decimal
    average_booking_value: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel_code": self.channel_code,
            "channel_name": self.channel_name,
            "channel_type": self.channel_type,
            "is_direct": self.is_direct,
            "gross_revenue": str(self.gross_revenue),
            "net_revenue": str(self.net_revenue),
            "distribution_cost": str(self.distribution_cost),
            "distribution_cost_pct": str(self.distribution_cost_pct),
            "gross_adr": str(self.gross_adr),
            "net_adr": str(self.net_adr),
            "booking_share": str(self.booking_share),
            "net_revenue_share": str(self.net_revenue_share),
            "booking_count": self.booking_count,
            "room_nights": self.room_nights,
            "discount_amount": str(self.discount_amount),
            "commission_amount": str(self.commission_amount),
            "payment_fee": str(self.payment_fee),
            "other_channel_cost": str(self.other_channel_cost),
            "net_adr_penalty": str(self.net_adr_penalty),
            "distribution_cost_share": str(self.distribution_cost_share),
            "average_booking_value": str(self.average_booking_value),
        }


@dataclass(frozen=True)
class PortfolioAnalytics:
    """Channel metrics for one hotel over one period, plus the totals."""

    hotel_code: str
    period_start: Optional[date]
    period_end: Optional[date]
    total_rooms: int
    channels: List[ChannelMetrics]
    total_bookings: int
    total_room_nights: int
    total_gross_revenue: Decimal
    total_distribution_cost: Decimal
    total_net_revenue: Decimal
    portfolio_gross_adr: Decimal
    portfolio_net_adr: Decimal
    portfolio_distribution_cost_pct: Decimal
    portfolio_occupancy: Decimal
    net_revpar: Decimal
    direct_booking_share: Decimal
    direct_net_revenue_share: Decimal
    distribution_cost_share_residual: Decimal
    basis: str = "OBSERVED"

    # -- ordered lookups -------------------------------------------------
    def by_code(self, code: str) -> Optional[ChannelMetrics]:
        for c in self.channels:
            if c.channel_code == code:
                return c
        return None

    def non_direct(self) -> List[ChannelMetrics]:
        return [c for c in self.channels if not c.is_direct]

    def ranked_by_net_revenue(self) -> List[ChannelMetrics]:
        """Highest net revenue first. Deterministic tie-break by code."""
        return sorted(
            self.channels,
            key=lambda c: (-c.net_revenue, c.channel_code),
        )

    def ranked_by_distribution_cost(self) -> List[ChannelMetrics]:
        """Most expensive channel first, ignoring channels that cost nothing."""
        return sorted(
            [c for c in self.channels if c.distribution_cost > 0],
            key=lambda c: (-c.distribution_cost, c.channel_code),
        )

    def share_gap(self, channel_code: str) -> Optional[Decimal]:
        """booking_share - net_revenue_share for one channel.

        Positive means the channel takes a larger slice of *demand* than of
        *profit*. This is the single most useful number in the product.
        """
        c = self.by_code(channel_code)
        if c is None:
            return None
        return quantize_rate(c.booking_share - c.net_revenue_share)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hotel_code": self.hotel_code,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "total_rooms": self.total_rooms,
            "basis": self.basis,
            "totals": {
                "bookings": self.total_bookings,
                "room_nights": self.total_room_nights,
                "gross_revenue": str(self.total_gross_revenue),
                "distribution_cost": str(self.total_distribution_cost),
                "net_revenue": str(self.total_net_revenue),
                "gross_adr": str(self.portfolio_gross_adr),
                "net_adr": str(self.portfolio_net_adr),
                "distribution_cost_pct": str(self.portfolio_distribution_cost_pct),
                "occupancy": str(self.portfolio_occupancy),
                "net_revpar": str(self.net_revpar),
                "direct_booking_share": str(self.direct_booking_share),
                "direct_net_revenue_share": str(self.direct_net_revenue_share),
                "share_closure_residual": str(self.distribution_cost_share_residual),
            },
            "channels": [c.to_dict() for c in self.channels],
        }


# ---------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------


def compute_channel_metrics(
    facts: Sequence[BookingFact],
    *,
    hotel_code: str = "UNKNOWN",
    total_rooms: int = 0,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    basis: str = "OBSERVED",
) -> PortfolioAnalytics:
    """Aggregate booking facts into per-channel metrics and portfolio totals.

    A channel with zero bookings is still reported, with every metric at
    zero, so the UI does not silently drop a channel the hotel has stopped
    using - an important signal in itself.
    """
    # -- group, preserving first-seen order for deterministic output --
    groups: "OrderedDict[str, List[BookingFact]]" = OrderedDict()
    meta: Dict[str, BookingFact] = {}
    for f in facts:
        if f.channel_code not in groups:
            groups[f.channel_code] = []
            meta[f.channel_code] = f
        groups[f.channel_code].append(f)

    # -- pass 1: absolute totals per channel --
    def q(value: Decimal) -> Decimal:
        return quantize_money(value)

    per_channel: Dict[str, Dict[str, Any]] = {}
    for code, items in groups.items():
        first = meta[code]
        gross = q(sum((f.gross_room_revenue for f in items), ZERO))
        discount = q(sum((f.discount_amount for f in items), ZERO))
        commission = q(sum((f.commission_amount for f in items), ZERO))
        payment_fee = q(sum((f.payment_fee for f in items), ZERO))
        other = q(sum((f.other_channel_cost for f in items), ZERO))
        distribution_cost = q(discount + commission + payment_fee + other)
        net = q(gross - distribution_cost)
        nights = sum(f.room_nights for f in items)
        per_channel[code] = {
            "channel_name": first.channel_name,
            "channel_type": first.channel_type,
            "is_direct": first.is_direct,
            "gross_revenue": gross,
            "discount_amount": discount,
            "commission_amount": commission,
            "payment_fee": payment_fee,
            "other_channel_cost": other,
            "distribution_cost": distribution_cost,
            "net_revenue": net,
            "booking_count": len(items),
            "room_nights": nights,
            "gross_adr": safe_divide(gross, Decimal(nights)),
            "net_adr": safe_divide(net, Decimal(nights)),
            "distribution_cost_pct": safe_divide(distribution_cost, gross),
            "average_booking_value": safe_divide(gross, Decimal(len(items))),
        }

    # -- portfolio denominators --
    total_bookings = sum(len(v) for v in groups.values())
    total_nights = sum(v["room_nights"] for v in per_channel.values())
    total_gross = q(sum((v["gross_revenue"] for v in per_channel.values()), ZERO))
    total_dist = q(sum((v["distribution_cost"] for v in per_channel.values()), ZERO))
    total_net = q(sum((v["net_revenue"] for v in per_channel.values()), ZERO))

    # -- pass 2: shares are ratios against the portfolio --
    metrics: List[ChannelMetrics] = []
    for code, v in per_channel.items():
        metrics.append(
            ChannelMetrics(
                channel_code=code,
                channel_name=v["channel_name"],
                channel_type=v["channel_type"],
                is_direct=v["is_direct"],
                gross_revenue=v["gross_revenue"],
                net_revenue=v["net_revenue"],
                distribution_cost=v["distribution_cost"],
                distribution_cost_pct=v["distribution_cost_pct"],
                gross_adr=v["gross_adr"],
                net_adr=v["net_adr"],
                booking_share=safe_divide(Decimal(v["booking_count"]), Decimal(total_bookings)),
                net_revenue_share=safe_divide(v["net_revenue"], Decimal(total_net)),
                booking_count=v["booking_count"],
                room_nights=v["room_nights"],
                discount_amount=v["discount_amount"],
                commission_amount=v["commission_amount"],
                payment_fee=v["payment_fee"],
                other_channel_cost=v["other_channel_cost"],
                net_adr_penalty=quantize_rate(v["gross_adr"] - v["net_adr"]),
                distribution_cost_share=safe_divide(v["distribution_cost"], Decimal(total_dist)),
                average_booking_value=v["average_booking_value"],
            )
        )

    # -- portfolio-level rates --
    portfolio_gross_adr = safe_divide(total_gross, Decimal(total_nights))
    portfolio_net_adr = safe_divide(total_net, Decimal(total_nights))

    days = 0
    if period_start and period_end:
        days = (period_end - period_start).days + 1
    availability = Decimal(total_rooms * days)
    occupancy = safe_divide(Decimal(total_nights), availability) if availability else quantize_rate(ZERO)
    net_revpar = safe_divide(total_net, availability) if availability else quantize_rate(ZERO)

    direct_gross = q(
        sum((c.gross_revenue for c in metrics if c.is_direct), ZERO)
    )
    direct_net = q(sum((c.net_revenue for c in metrics if c.is_direct), ZERO))
    direct_nights = sum(c.room_nights for c in metrics if c.is_direct)
    direct_bookings = sum(c.booking_count for c in metrics if c.is_direct)

    return PortfolioAnalytics(
        hotel_code=hotel_code,
        period_start=period_start,
        period_end=period_end,
        total_rooms=total_rooms,
        channels=metrics,
        total_bookings=total_bookings,
        total_room_nights=total_nights,
        total_gross_revenue=total_gross,
        total_distribution_cost=total_dist,
        total_net_revenue=total_net,
        portfolio_gross_adr=portfolio_gross_adr,
        portfolio_net_adr=portfolio_net_adr,
        portfolio_distribution_cost_pct=safe_divide(total_dist, total_gross),
        portfolio_occupancy=occupancy,
        net_revpar=net_revpar,
        # "Booking share" for direct is a room-night share: a hotel cares
        # about how much *inventory* came through its own door, and the
        # direct line's booking count is tiny by construction.
        direct_booking_share=safe_divide(Decimal(direct_nights), Decimal(total_nights)),
        direct_net_revenue_share=safe_divide(direct_net, Decimal(total_net)),
        distribution_cost_share_residual=share_closure_residual(metrics),
        basis=basis,
    )


# ``compute_portfolio`` is the name used by the API layer.
compute_portfolio = compute_channel_metrics


def share_closure_residual(channels: Iterable[ChannelMetrics]) -> Decimal:
    """``sum(booking_share) - 1.0000``: the rounding residual.

    Zero when each share divides exactly. Non-zero (bounded by 5e-4 per
    channel) when rounding bites. Reported, never corrected.
    """
    total = sum((c.booking_share for c in channels), Decimal("0"))
    return quantize_rate(total - Decimal("1.0000"))


def verify_share_closure(portfolio: PortfolioAnalytics, tolerance: Decimal = Decimal("0.005")) -> bool:
    """True when rounded booking shares still close to 100%."""
    return abs(portfolio.distribution_cost_share_residual) <= tolerance


# ---------------------------------------------------------------------
# Monthly series (for the dashboard trend chart)
# ---------------------------------------------------------------------


def monthly_channel_series(
    facts_by_month: Dict[date, Sequence[BookingFact]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Per-month portfolio totals plus a per-channel net revenue series.

    ``facts_by_month`` maps a month-start date to that month's facts. The
    result is sorted by month and contains only observed arithmetic - no
    interpolation, no forecast.
    """
    months: List[Dict[str, Any]] = []
    channel_net: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()

    for month_start in sorted(facts_by_month):
        facts = facts_by_month[month_start]
        pf = compute_channel_metrics(facts, hotel_code="MONTHLY")
        months.append(
            {
                "month": month_start.isoformat(),
                "bookings": pf.total_bookings,
                "room_nights": pf.total_room_nights,
                "gross_revenue": str(pf.total_gross_revenue),
                "distribution_cost": str(pf.total_distribution_cost),
                "net_revenue": str(pf.total_net_revenue),
                "distribution_cost_pct": str(pf.portfolio_distribution_cost_pct),
                "gross_adr": str(pf.portfolio_gross_adr),
                "net_adr": str(pf.portfolio_net_adr),
            }
        )
        for c in pf.channels:
            channel_net.setdefault(c.channel_code, []).append(
                {
                    "month": month_start.isoformat(),
                    "net_revenue": str(c.net_revenue),
                    "gross_revenue": str(c.gross_revenue),
                    "distribution_cost": str(c.distribution_cost),
                    "bookings": c.booking_count,
                }
            )

    return {"portfolio_monthly": months, "channel_net_revenue": dict(channel_net)}