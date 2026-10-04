"""D6 DISTRIBOX - Deterministic Scenario Engine.

Answers: *"if I shift room nights away from this OTA, what happens to net
revenue?"* - with arithmetic, not opinion.

What this is
------------
A transparent, fully reproducible cost/mix model. Given an allocation of
room nights across channels it computes gross revenue, distribution cost
and net revenue from the configured or observed channel cost rates, and it
accounts for the two effects that naive models ignore:

* **Demand loss.** Volume removed from a channel does not all arrive
  elsewhere. Only ``incremental_rate`` of it lands on another channel; the
  remainder is genuinely lost revenue.
* **Direct conversion.** A portion of removed volume comes back through
  the hotel's own direct channel, at a much lower cost rate.

What this is NOT
----------------
* **Not an optimiser.** :class:`GreedyMarginalAllocator` is a heuristic
  that repeatedly moves one night at a time from the worst marginal net
  value to the best. It is *not* guaranteed to find the global optimum and
  does not claim to be. Its advantage is that every single move is
  recorded and can be shown to the user.
* **Not a prediction.** ``expected_direct_conversion_rate`` and
  ``incremental_rate`` are *assumptions supplied by the hotel*. They are
  named that way everywhere they appear, and every result carries the
  assumption set it was computed under.
* **Not LLM-generated.** This module imports nothing from ``app.ai``.

The economic model, in full
---------------------------
For channel *c* with *n* room nights at observed ADR ``a`` and rates
``d`` (discount), ``m`` (commission), ``f`` (payment fee), ``o`` (other)::

    gross_n  = a * n
    discount = gross_n * d
    commission = gross_n * m          # charged on PRE-discount gross,
                                      # which is how OTA contracts work
    payment_fee = gross_n * f + F     # F = fixed gateway fee per booking
    other      = gross_n * o
    distribution_cost = discount + commission + payment_fee + other
    net_n      = gross_n - distribution_cost

Shift of ``k`` nights from channel *x* to channel *y``::

    net_delta = k * (net_per_night[y] - net_per_night[x])
                * (expected_direct_conversion_rate[y] + incremental_rate[x])

Both effects are multiplicative on the *arriving* rate; the destination
channel's economics determine whether the shifted night is worth having.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from app.analytics.channel_analytics import PortfolioAnalytics
from app.core.money import ZERO, quantize_money, quantize_rate
from app.services.net_revenue import safe_divide

__all__ = [
    "ChannelRate",
    "ScenarioAssumptions",
    "AllocationLine",
    "ScenarioOutcome",
    "ScenarioEngine",
    "GreedyMarginalAllocator",
    "MAXIMIZE_NET_REVENUE",
    "MINIMIZE_DISTRIBUTION_COST",
    "MAXIMIZE_DIRECT_SHARE",
]

MAXIMIZE_NET_REVENUE = "MAXIMIZE_NET_REVENUE"
MINIMIZE_DISTRIBUTION_COST = "MINIMIZE_DISTRIBUTION_COST"
MAXIMIZE_DIRECT_SHARE = "MAXIMIZE_DIRECT_SHARE"


# ---------------------------------------------------------------------
# Cost configuration
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelRate:
    """Effective economics for one channel over the scenario period."""

    channel_code: str
    gross_adr: Decimal            # observed gross ADR per room night
    discount_rate: Decimal
    commission_rate: Decimal
    payment_fee_rate: Decimal
    other_cost_rate: Decimal
    fixed_payment_fee: Decimal = Decimal("0")
    # -- planning assumptions (hotel-supplied, never inferred) --
    expected_direct_conversion_rate: Decimal = Decimal("0")
    incremental_rate: Decimal = Decimal("0")
    is_direct: bool = False
    rate_source: str = "CONFIGURED"
    #: Whether this channel can *receive* demand reallocated by the
    #: allocator. WALK_IN is the canonical ``False``: nobody decides to send
    #: a guest to the front desk, walk-ins are whatever walks in. Treating
    #: it as a target would let the model "improve" net revenue by moving
    #: bookings to a channel that was never a choice.
    is_addressable: bool = True

    def gross_for(self, nights: Decimal) -> Decimal:
        return quantize_money(self.gross_adr * nights)

    def distribution_cost_for(self, nights: Decimal) -> Decimal:
        gross = self.gross_adr * nights
        return quantize_money(
            gross * self.discount_rate
            + gross * self.commission_rate
            + gross * self.payment_fee_rate
            + self.fixed_payment_fee * nights
            + gross * self.other_cost_rate
        )

    def net_for(self, nights: Decimal) -> Decimal:
        return quantize_money(self.gross_for(nights) - self.distribution_cost_for(nights))

    def net_per_night(self) -> Decimal:
        return quantize_rate(self.net_for(Decimal(1)))

    def gross_per_night(self) -> Decimal:
        return quantize_rate(self.gross_for(Decimal(1)))

    def distribution_cost_pct(self) -> Decimal:
        return safe_divide(self.distribution_cost_for(Decimal(1)), self.gross_for(Decimal(1)))


def rates_from_portfolio(
    portfolio: PortfolioAnalytics,
    *,
    overrides: Optional[Dict[str, Dict[str, Decimal]]] = None,
    conversion_rates: Optional[Dict[str, Decimal]] = None,
    incremental_rates: Optional[Dict[str, Decimal]] = None,
    fixed_payment_fees: Optional[Dict[str, Decimal]] = None,
    addressable: Optional[Dict[str, bool]] = None,
    ) -> Dict[str, ChannelRate]:
    """Build effective channel rates for the scenario model.

    Cost rates default to **OBSERVED**: the effective rates implied by the
    money actually on the bookings, which is the only figure that is known
    rather than assumed. ADR is always the observed gross ADR.

    Pass ``overrides`` to model a *hypothetical* cost structure - typically
    read from the ``channel_costs`` table to ask "what if OTA Beta's
    commission were 12% instead of 17%?". An override **replaces the entire
    rate set** for that channel (unlisted rates become zero) because
    carrying observed rates forward alongside a hypothetical commission
    would double-count the cost. Override results are tagged
    ``rate_source='CONFIGURED'`` and can never be mistaken for an
    observation.

    ``conversion_rates`` and ``incremental_rate`` are *planning
    assumptions*, keyed by the channel being withdrawn from: "if this
    channel's volume were reduced, this fraction would arrive direct /
    on another channel". They default to zero, which is the conservative
    choice: with no stated assumption, no volume is assumed to come back.
    """
    overrides = overrides or {}
    conversion_rates = conversion_rates or {}
    incremental_rates = incremental_rates or {}
    fixed_payment_fees = fixed_payment_fees or {}
    addressable = addressable or {}

    rates: Dict[str, ChannelRate] = {}
    for c in portfolio.channels:
        conversion = conversion_rates.get(c.channel_code, Decimal("0"))
        incremental = incremental_rates.get(c.channel_code, Decimal("0"))
        fixed = fixed_payment_fees.get(c.channel_code, Decimal("0"))
        reachable = addressable.get(c.channel_code, True)

        if c.channel_code in overrides:
            spec = overrides[c.channel_code]
            rates[c.channel_code] = ChannelRate(
                channel_code=c.channel_code,
                gross_adr=c.gross_adr,
                discount_rate=spec.get("discount_rate", Decimal("0")),
                commission_rate=spec.get("commission_rate", Decimal("0")),
                payment_fee_rate=spec.get("payment_fee_rate", Decimal("0")),
                other_cost_rate=spec.get("other_cost_rate", Decimal("0")),
                fixed_payment_fee=spec.get("fixed_payment_fee", fixed),
                expected_direct_conversion_rate=conversion,
                incremental_rate=incremental,
                is_direct=c.is_direct,
                rate_source="CONFIGURED",
                is_addressable=reachable,
            )
            continue

        gross = c.gross_revenue or Decimal("1")
        rates[c.channel_code] = ChannelRate(
            channel_code=c.channel_code,
            gross_adr=c.gross_adr,
            discount_rate=safe_divide(c.discount_amount, gross),
            commission_rate=safe_divide(c.commission_amount, gross),
            payment_fee_rate=safe_divide(c.payment_fee, gross),
            other_cost_rate=safe_divide(c.other_channel_cost, gross),
            fixed_payment_fee=fixed,
            expected_direct_conversion_rate=conversion,
            incremental_rate=incremental,
            is_direct=c.is_direct,
            rate_source="OBSERVED",
            is_addressable=reachable,
        )
    return rates



# ---------------------------------------------------------------------
# Assumptions
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioAssumptions:
    """Every non-observed input, in one place, so it can be displayed.

    A scenario without visible assumptions is a scenario nobody can argue
    with or trust. The UI renders this object verbatim next to the result.
    """

    expected_direct_conversion_rate: Dict[str, Decimal] = field(default_factory=dict)
    incremental_rate: Dict[str, Decimal] = field(default_factory=dict)
    max_shift_fraction: Decimal = Decimal("0.30")
    objective: str = MAXIMIZE_NET_REVENUE
    source: str = "HOTEL_SUPPLIED"

    def conversion_for(self, code: str) -> Decimal:
        return self.expected_direct_conversion_rate.get(code, Decimal("0"))

    def incremental_for(self, code: str) -> Decimal:
        return self.incremental_rate.get(code, Decimal("0"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "objective": self.objective,
            "source": self.source,
            "max_shift_fraction": str(self.max_shift_fraction),
            "expected_direct_conversion_rate": {
                k: str(v) for k, v in sorted(self.expected_direct_conversion_rate.items())
            },
            "incremental_rate": {
                k: str(v) for k, v in sorted(self.incremental_rate.items())
            },
        }


# ---------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class AllocationLine:
    channel_code: str
    baseline_room_nights: Decimal
    scenario_room_nights: Decimal
    shift_room_nights: Decimal
    gross_revenue: Decimal
    distribution_cost: Decimal
    net_revenue: Decimal
    net_per_night: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel_code": self.channel_code,
            "baseline_room_nights": str(self.baseline_room_nights),
            "scenario_room_nights": str(self.scenario_room_nights),
            "shift_room_nights": str(self.shift_room_nights),
            "gross_revenue": str(self.gross_revenue),
            "distribution_cost": str(self.distribution_cost),
            "net_revenue": str(self.net_revenue),
            "net_per_night": str(self.net_per_night),
        }


@dataclass(frozen=True)
class ScenarioOutcome:
    """A modelled outcome. Never presented as observed fact."""

    name: str
    objective: str
    is_baseline: bool
    engine: str
    room_nights: Decimal
    gross_revenue: Decimal
    distribution_cost: Decimal
    net_revenue: Decimal
    net_adr: Decimal
    net_revpar: Decimal
    distribution_cost_pct: Decimal
    direct_booking_share: Decimal
    estimated_lost_bookings: Decimal
    estimated_gained_direct: Decimal
    is_feasible: bool
    infeasibility_reason: Optional[str]
    lines: List[AllocationLine]
    moves: List[Dict[str, Any]]
    assumptions: ScenarioAssumptions
    total_rooms: int
    period_start: Optional[date]
    period_end: Optional[date]
    baseline_net_revenue: Decimal
    net_revenue_delta: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "objective": self.objective,
            "is_baseline": self.is_baseline,
            "engine": self.engine,
            "total_rooms": self.total_rooms,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "room_nights": str(self.room_nights),
            "gross_revenue": str(self.gross_revenue),
            "distribution_cost": str(self.distribution_cost),
            "net_revenue": str(self.net_revenue),
            "net_adr": str(self.net_adr),
            "net_revpar": str(self.net_revpar),
            "distribution_cost_pct": str(self.distribution_cost_pct),
            "direct_booking_share": str(self.direct_booking_share),
            "estimated_lost_bookings": str(self.estimated_lost_bookings),
            "estimated_gained_direct": str(self.estimated_gained_direct),
            "is_feasible": self.is_feasible,
            "infeasibility_reason": self.infeasibility_reason,
            "baseline_net_revenue": str(self.baseline_net_revenue),
            "net_revenue_delta": str(self.net_revenue_delta),
            "allocation_detail": {
                "lines": [l.to_dict() for l in self.lines],
                "moves": self.moves,
                "assumptions": self.assumptions.to_dict(),
            },
        }


# ---------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------


class ScenarioEngine:
    """Deterministic net-revenue model for a hypothetical channel mix.

    Stateless. The same rates + allocation + assumptions always produce the
    same outcome, which is what makes a before/after comparison in the UI
    defensible.
    """

    engine_name = "DETERMINISTIC"

    def baseline(
        self,
        portfolio: PortfolioAnalytics,
        rates: Dict[str, ChannelRate],
        assumptions: ScenarioAssumptions,
    ) -> ScenarioOutcome:
        """The 'do nothing' case: observed mix, observed rates.

        Every scenario delta is measured against this. A scenario without
        a baseline is just a number.
        """
        allocation = {
            c.channel_code: Decimal(c.room_nights) for c in portfolio.channels
        }
        return self.evaluate(
            allocation=allocation,
            rates=rates,
            assumptions=assumptions,
            portfolio=portfolio,
            name="Baseline (observed mix)",
            is_baseline=True,
            moves=[],
        )

    def evaluate(
        self,
        allocation: Dict[str, Decimal],
        rates: Dict[str, ChannelRate],
        assumptions: ScenarioAssumptions,
        portfolio: PortfolioAnalytics,
        *,
        name: str = "Scenario",
        is_baseline: bool = False,
        moves: Optional[List[Dict[str, Any]]] = None,
    ) -> ScenarioOutcome:
        """Compute the outcome of an explicit allocation of room nights."""
        baseline_alloc = {
            c.channel_code: Decimal(c.room_nights) for c in portfolio.channels
        }

        # -- feasibility --
        unknown = [c for c in allocation if c not in rates]
        infeasible_reason: Optional[str] = None
        if unknown:
            infeasible_reason = f"unknown channel(s) in allocation: {sorted(unknown)}"
        elif portfolio.total_room_nights and sum(
            allocation.values(), ZERO
        ) > Decimal(portfolio.total_room_nights):
            infeasible_reason = (
                f"allocation of {sum(allocation.values(), ZERO)} room nights exceeds "
                f"the {portfolio.total_room_nights} observed - impossible"
            )
        else:
            for code, nights in allocation.items():
                if nights < 0:
                    infeasible_reason = f"{code}: room nights cannot be negative ({nights})"
                    break
                rate = rates.get(code)
                if (
                    rate is not None
                    and not rate.is_addressable
                    and quantize_rate(nights) != baseline_alloc.get(code, ZERO)
                ):
                    infeasible_reason = (
                        f"{code} is non-addressable and cannot be reallocated: "
                        f"{baseline_alloc.get(code, ZERO)} observed room nights "
                        f"cannot be modelled as {quantize_rate(nights)}. Nobody "
                        f"chooses to send a guest there, so any movement is an "
                        f"assumption the booking history cannot support."
                    )
                    break

        lines: List[AllocationLine] = []
        baseline_direct_nights = quantize_rate(
            sum(
                (
                    baseline_alloc.get(code, Decimal(0))
                    for code, r in rates.items()
                    if r.is_direct
                ),
                ZERO,
            )
        )

        for code in sorted(rates):
            rate = rates[code]
            base_nights = baseline_alloc.get(code, Decimal(0))
            nights = quantize_rate(allocation.get(code, Decimal(0)))
            shift = quantize_rate(nights - base_nights)

            gross = rate.gross_for(nights) if nights >= 0 else ZERO
            dist = rate.distribution_cost_for(nights) if nights >= 0 else ZERO

            lines.append(
                AllocationLine(
                    channel_code=code,
                    baseline_room_nights=base_nights,
                    scenario_room_nights=nights,
                    shift_room_nights=shift,
                    gross_revenue=gross,
                    distribution_cost=dist,
                    net_revenue=quantize_money(gross - dist),
                    net_per_night=rate.net_per_night(),
                )
            )

        total_nights = quantize_rate(sum((l.scenario_room_nights for l in lines), ZERO))
        total_gross = quantize_money(sum((l.gross_revenue for l in lines), ZERO))
        total_dist = quantize_money(sum((l.distribution_cost for l in lines), ZERO))
        total_net = quantize_money(total_gross - total_dist)

        direct_nights = quantize_rate(
            sum((l.scenario_room_nights for l in lines if rates[l.channel_code].is_direct), ZERO)
        )

        # Demand loss and direct gain are read back out of the ALLOCATION,
        # not multiplied out from the assumption rates.
        #
        # This matters. If the allocator credits a shifted night as fully
        # arriving while the loss accounting separately discounts it, the
        # same volume is counted twice and the modelled gain is inflated.
        # Deriving both from the allocation makes them non-additive with the
        # revenue figures by construction: the nights are either in the
        # allocation or they are lost, never both.
        lost_bookings = quantize_rate(
            Decimal(portfolio.total_room_nights) - total_nights
        )
        if lost_bookings < 0:
            # The allocation created nights that were never observed. That
            # is not demand loss; it is an unsupportable assumption, so it
            # is reported as infeasible rather than quietly netted off.
            lost_bookings = ZERO
            infeasible_reason = (
                f"allocation of {total_nights} room nights exceeds the "
                f"{portfolio.total_room_nights} observed - volume was invented, "
                f"which no booking history can support"
            )
        gained_direct = quantize_rate(direct_nights - baseline_direct_nights)

        days = 1
        if portfolio.period_start and portfolio.period_end:
            days = (portfolio.period_end - portfolio.period_start).days + 1
        availability = Decimal(portfolio.total_rooms * days)

        baseline_net = quantize_money(portfolio.total_net_revenue)

        return ScenarioOutcome(
            name=name,
            objective=assumptions.objective,
            is_baseline=is_baseline,
            engine=self.engine_name,
            room_nights=total_nights,
            gross_revenue=total_gross,
            distribution_cost=total_dist,
            net_revenue=total_net,
            net_adr=safe_divide(total_net, total_nights),
            net_revpar=safe_divide(total_net, availability),
            distribution_cost_pct=safe_divide(total_dist, total_gross),
            direct_booking_share=safe_divide(direct_nights, total_nights),
            estimated_lost_bookings=lost_bookings,
            estimated_gained_direct=gained_direct,
            is_feasible=infeasible_reason is None,
            infeasibility_reason=infeasible_reason,
            lines=lines,
            moves=moves or [],
            assumptions=assumptions,
            total_rooms=portfolio.total_rooms,
            period_start=portfolio.period_start,
            period_end=portfolio.period_end,
            baseline_net_revenue=baseline_net,
            net_revenue_delta=quantize_money(total_net - baseline_net),
        )


def _best_net_per_night(
    rates: Dict[str, ChannelRate],
    *,
    is_direct: Optional[bool] = None,
    exclude: Optional[List[str]] = None,
) -> Optional[Decimal]:
    """Highest net revenue per night among the channels matching the filter.

    ``exclude`` removes the source and destination of a shift so that
    "where else could this night go" is answered by a genuine third party.
    """
    skip = set(exclude or [])
    candidates = [
        r.net_per_night()
        for code, r in rates.items()
        if code not in skip and (is_direct is None or r.is_direct == is_direct)
    ]
    return max(candidates) if candidates else None


# ---------------------------------------------------------------------
# Allocator
# ---------------------------------------------------------------------


class GreedyMarginalAllocator:
    """Deterministic single-night-at-a-time reallocator.

    Algorithm, stated plainly so it can be audited:

    1. Compute marginal net revenue per night for every *addressable*
       channel (WALK_IN and anything else flagged ``is_addressable=False``
       is excluded - nobody can decide to send a guest there).
    2. Find the addressable channel with the highest marginal net value
       (*best*) and the one with the lowest (*worst*).
    3. Work out how much of a night withdrawn from *worst* actually
       arrives anywhere at all::

           arrival = min(1, conversion[worst] + incremental[worst])

    4. If that arrival is net-accretive, withdraw 1 night from *worst* and
       deposit ``arrival`` nights into *best*. Record the move.
    5. Repeat until no single-night move is an improvement, or the
       ``max_shift_fraction`` cap on any one channel is reached.

    Step 3 is the one that keeps the model honest. A shifted night does not
    arrive whole: if only 84% of withdrawn OTA volume is expected to come
    back, depositing 1.0 nights would book revenue that does not exist. The
    shortfall is lost demand and shows up as a smaller allocation, which is
    exactly where :meth:`ScenarioEngine.evaluate` reads it from.

    This is a greedy local search. It finds a **better** mix than the
    baseline whenever the arithmetic allows, but it is **not** guaranteed
    to be globally optimal - no claim to the contrary is made anywhere in
    this codebase.
    """

    def __init__(self, max_iterations: int = 100000):
        self.max_iterations = max_iterations

    def allocate(
        self,
        engine: ScenarioEngine,
        portfolio: PortfolioAnalytics,
        rates: Dict[str, ChannelRate],
        assumptions: ScenarioAssumptions,
        *,
        locked_channels: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Decimal], List[Dict[str, Any]]]:
        """Return an improved allocation plus the audit trail of moves."""
        locked = set(locked_channels or [])
        allocation = {
            c.channel_code: Decimal(c.room_nights) for c in portfolio.channels
        }
        # Cap how far any one channel may move from its observed volume.
        cap = quantize_rate(
            Decimal(portfolio.total_room_nights) * assumptions.max_shift_fraction
        )
        moved: Dict[str, Decimal] = {k: ZERO for k in allocation}

        moves: List[Dict[str, Any]] = []

        for _ in range(self.max_iterations):
            best = _best_channel(allocation, rates, locked, moved, cap, maximise=True)
            worst = _best_channel(allocation, rates, locked, moved, cap, maximise=False)
            if best is None or worst is None or best == worst:
                break

            gain = _marginal_gain(best, worst, rates, assumptions)
            if gain <= 0:
                break

            # Only the fraction expected to arrive is deposited. The rest
            # is demand the hotel never sees.
            arrival = _arrival_fraction(worst, assumptions)
            if arrival <= 0:
                break

            allocation[worst] = quantize_rate(allocation[worst] - 1)
            allocation[best] = quantize_rate(allocation[best] + arrival)
            moved[worst] = quantize_rate(moved[worst] - 1)
            moved[best] = quantize_rate(moved[best] + arrival)

            moves.append(
                {
                    "step": len(moves) + 1,
                    "from_channel": worst,
                    "to_channel": best,
                    "room_nights_withdrawn": 1,
                    "room_nights_arriving": str(arrival),
                    "room_nights_lost": str(quantize_rate(1 - arrival)),
                    "net_revenue_delta_per_night": str(gain),
                    "net_revenue_delta": str(quantize_money(arrival * gain)),
                    "cumulative_room_nights_withdrawn": str(len(moves)),
                }
            )

        return allocation, moves


def _best_channel(
    allocation: Dict[str, Decimal],
    rates: Dict[str, ChannelRate],
    locked: set,
    moved: Dict[str, Decimal],
    cap: Decimal,
    *,
    maximise: bool,
) -> Optional[str]:
    """Channel with the best (or worst) marginal net value, respecting caps."""
    candidates: List[Tuple[Decimal, str]] = []
    for code, rate in rates.items():
        if code in locked or not rate.is_addressable:
            continue
        if allocation.get(code, ZERO) <= 0 and not maximise:
            continue  # nothing left to withdraw
        if abs(moved.get(code, ZERO)) >= cap:
            continue  # this channel has moved as far as the cap allows
        candidates.append((rate.net_per_night(), code))

    if not candidates:
        return None
    if maximise:
        return max(candidates, key=lambda t: (t[0], t[1]))[1]
    return min(candidates, key=lambda t: (t[0], t[1]))[1]


def _arrival_fraction(
    from_code: str, assumptions: ScenarioAssumptions
) -> Decimal:
    """Share of a night withdrawn from ``from_code`` that arrives at all.

    Capped at 1: no channel can return more demand than was taken from it,
    and an assumption set that claims otherwise is misconfigured, not
    profitable.
    """
    total = (
        assumptions.conversion_for(from_code)
        + assumptions.incremental_for(from_code)
    )
    return quantize_rate(min(max(total, Decimal("0")), Decimal("1")))


def _marginal_gain(
    to_code: str,
    from_code: str,
    rates: Dict[str, ChannelRate],
    assumptions: ScenarioAssumptions,
) -> Decimal:
    """Net revenue change from moving one night ``from_code -> to_code``.

    The withdrawn night does not reliably become a night on ``to_code``,
    so the move is valued as the hotel's stated assumptions describe it:

    * the ``conversion`` fraction arrives **direct**, valued at the best
      direct channel's net per night;
    * the ``incremental`` fraction lands on some *other* non-direct
      channel, valued at the best such channel's net per night;
    * the remainder is lost demand and contributes nothing.

    Each portion is valued at its own destination's economics, which is
    what stops the allocator from over-crediting a shift. Only the
    *source* channel is excluded from consideration - the destination is a
    legitimate landing place, and excluding it would make the last channel
    in the portfolio look like it has nowhere to send its volume.
    """
    from_rate = rates[from_code]
    from_net = from_rate.net_per_night()

    conversion = assumptions.conversion_for(from_code)
    incremental = assumptions.incremental_for(from_code)

    # A conversion fraction above 1 would be nonsense; clamp rather than
    # silently produce a negative shift value.
    conversion = min(conversion, Decimal("1"))
    incremental = max(Decimal("0"), min(incremental, Decimal("1") - conversion))

    gain = Decimal("0")

    if conversion > 0:
        direct_net = _best_net_per_night(rates, is_direct=True, exclude=[from_code])
        if direct_net is not None:
            gain += conversion * (direct_net - from_net)

    if incremental > 0:
        other_net = _best_net_per_night(rates, is_direct=False, exclude=[from_code])
        if other_net is not None:
            gain += incremental * (other_net - from_net)

    return quantize_rate(gain)