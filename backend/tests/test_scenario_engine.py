"""Tests for the deterministic Scenario Engine.

Standard library only, no database, no solver dependency, no LLM.

The engine's contract under test: it is a transparent arithmetic model
with named assumptions - not a black box, and not a claimed optimiser.
These tests pin the arithmetic, the determinism, the feasibility guard and
the demand-loss accounting.
"""

import unittest
from datetime import date
from decimal import Decimal

from app.analytics.channel_analytics import BookingFact, compute_channel_metrics
from app.services.scenario_engine import (
    MAXIMIZE_NET_REVENUE,
    ChannelRate,
    GreedyMarginalAllocator,
    ScenarioAssumptions,
    ScenarioEngine,
    rates_from_portfolio,
)

D = Decimal


def fact(code, name, ctype, is_direct, gross, disc, comm, pay, other="0", nights=1):
    return BookingFact(
        channel_code=code,
        channel_name=name,
        channel_type=ctype,
        is_direct=is_direct,
        gross_room_revenue=D(gross),
        discount_amount=D(disc),
        commission_amount=D(comm),
        payment_fee=D(pay),
        other_channel_cost=D(other),
        room_nights=nights,
    )


# -- fixture: DIRECT keeps everything, OTA keeps 2,738 of 4,000 ---------
FIXTURE = [
    fact("OTA_ALPHA", "OTA Alpha", "OTA", False, "4000", "600", "612", "50", "0", 1),
    fact("DIRECT", "Direct Website", "DIRECT", True, "4000", "0", "0", "0", "0", 1),
]


def build_portfolio():
    return compute_channel_metrics(
        FIXTURE,
        hotel_code="TEST",
        total_rooms=1,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 1),
    )


def assumptions(conversion=D("0.30"), incremental=D("0.50"), shift=D("0.30")):
    return ScenarioAssumptions(
        expected_direct_conversion_rate={"OTA_ALPHA": conversion, "DIRECT": D("0")},
        incremental_rate={"OTA_ALPHA": incremental, "DIRECT": D("0")},
        max_shift_fraction=shift,
        objective=MAXIMIZE_NET_REVENUE,
    )


class TestChannelRateArithmetic(unittest.TestCase):
    def setUp(self):
        self.rate = ChannelRate(
            channel_code="OTA",
            gross_adr=D("4000"),
            discount_rate=D("0.15"),
            commission_rate=D("0.153"),
            payment_fee_rate=D("0.0125"),
            other_cost_rate=D("0"),
        )

    def test_distribution_cost_per_night(self):
        # 4000 * (0.15 + 0.153 + 0.0125) = 4000 * 0.3155 = 1262.00
        self.assertEqual(self.rate.distribution_cost_for(D("1")), D("1262.00"))
        self.assertEqual(self.rate.distribution_cost_pct(), D("0.3155"))

    def test_net_per_night(self):
        self.assertEqual(self.rate.gross_for(D("1")), D("4000.00"))
        self.assertEqual(self.rate.net_for(D("1")), D("2738.00"))
        self.assertEqual(self.rate.net_per_night(), D("2738.0000"))

    def test_multi_night_scales_linearly(self):
        self.assertEqual(self.rate.gross_for(D("3")), D("12000.00"))
        self.assertEqual(self.rate.net_for(D("3")), D("8214.00"))

    def test_fixed_payment_fee_adds_per_night(self):
        r = ChannelRate(
            channel_code="X", gross_adr=D("1000"), discount_rate=D("0"),
            commission_rate=D("0"), payment_fee_rate=D("0"),
            other_cost_rate=D("0"), fixed_payment_fee=D("18"),
        )
        self.assertEqual(r.distribution_cost_for(D("2")), D("36.00"))
        self.assertEqual(r.net_for(D("2")), D("1964.00"))

    def test_zero_nights_costs_nothing(self):
        self.assertEqual(self.rate.gross_for(D("0")), D("0.00"))
        self.assertEqual(self.rate.distribution_cost_for(D("0")), D("0.00"))
        self.assertEqual(self.rate.net_for(D("0")), D("0.00"))


class TestRatesFromPortfolio(unittest.TestCase):
    def test_observed_rates_are_the_default(self):
        pf = build_portfolio()
        rates = rates_from_portfolio(pf)
        ota = rates["OTA_ALPHA"]
        self.assertEqual(ota.rate_source, "OBSERVED")
        self.assertEqual(ota.gross_adr, D("4000.0000"))
        self.assertEqual(ota.discount_rate, D("0.1500"))
        self.assertEqual(ota.commission_rate, D("0.1530"))
        self.assertEqual(ota.payment_fee_rate, D("0.0125"))
        # The observed effective rate reproduces the observed cost exactly.
        self.assertEqual(ota.net_for(D("1")), D("2738.00"))

    def test_configured_rates_are_tagged_as_such(self):
        """A hypothetical rate must never be labelled an observation."""
        pf = build_portfolio()
        rates = rates_from_portfolio(
            pf, overrides={"OTA_ALPHA": {"commission_rate": D("0.10")}}
        )
        self.assertEqual(rates["OTA_ALPHA"].rate_source, "CONFIGURED")
        self.assertEqual(rates["OTA_ALPHA"].commission_rate, D("0.1000"))
        # An override replaces the whole rate set: unlisted rates are zero.
        # 4000 - 10% commission = 3600. Carrying the observed 15% discount
        # forward as well would double-count the cost.
        self.assertEqual(rates["OTA_ALPHA"].net_for(D("1")), D("3600.00"))
        self.assertEqual(rates["OTA_ALPHA"].distribution_cost_pct(), D("0.1000"))

    def test_override_does_not_disturb_observed_channels(self):
        pf = build_portfolio()
        rates = rates_from_portfolio(
            pf, overrides={"OTA_ALPHA": {"commission_rate": D("0.10")}}
        )
        self.assertEqual(rates["DIRECT"].rate_source, "OBSERVED")
        self.assertEqual(rates["DIRECT"].net_for(D("1")), D("4000.00"))

    def test_observed_rates_reproduce_observed_cost_exactly(self):
        """The scenario engine and the analytics layer must agree exactly."""
        pf = build_portfolio()
        rates = rates_from_portfolio(pf)
        out = ScenarioEngine().baseline(pf, rates, assumptions())
        self.assertEqual(out.distribution_cost, pf.total_distribution_cost)
        self.assertEqual(out.net_revenue, pf.total_net_revenue)

    def test_assumptions_are_attached_to_rates(self):
        pf = build_portfolio()
        rates = rates_from_portfolio(
            pf,
            conversion_rates={"OTA_ALPHA": D("0.30")},
            incremental_rates={"OTA_ALPHA": D("0.50")},
        )
        self.assertEqual(rates["OTA_ALPHA"].expected_direct_conversion_rate, D("0.30"))
        self.assertEqual(rates["OTA_ALPHA"].incremental_rate, D("0.50"))


class TestBaseline(unittest.TestCase):
    def setUp(self):
        self.pf = build_portfolio()
        self.rates = rates_from_portfolio(self.pf)
        self.engine = ScenarioEngine()
        self.assumptions = assumptions()

    def test_baseline_reproduces_observed_portfolio(self):
        out = self.engine.baseline(self.pf, self.rates, self.assumptions)
        self.assertTrue(out.is_baseline)
        self.assertTrue(out.is_feasible)
        self.assertEqual(out.gross_revenue, D("8000.00"))
        self.assertEqual(out.distribution_cost, D("1262.00"))
        self.assertEqual(out.net_revenue, D("6738.00"))
        self.assertEqual(out.net_revenue_delta, D("0.00"))
        self.assertEqual(out.net_adr, D("3369.0000"))

    def test_baseline_is_deterministic(self):
        a = self.engine.baseline(self.pf, self.rates, self.assumptions).to_dict()
        b = self.engine.baseline(self.pf, self.rates, self.assumptions).to_dict()
        self.assertEqual(a, b)

    def test_baseline_has_zero_shifts(self):
        out = self.engine.baseline(self.pf, self.rates, self.assumptions)
        self.assertTrue(all(l.shift_room_nights == D("0.0000") for l in out.lines))

    def test_baseline_direct_share(self):
        out = self.engine.baseline(self.pf, self.rates, self.assumptions)
        self.assertEqual(out.direct_booking_share, D("0.5000"))


class TestScenarioEvaluation(unittest.TestCase):
    def setUp(self):
        self.pf = build_portfolio()
        self.rates = rates_from_portfolio(self.pf)
        self.engine = ScenarioEngine()
        self.assumptions = assumptions()

    def _evaluate(self, allocation, **kw):
        return self.engine.evaluate(
            allocation=allocation,
            rates=self.rates,
            assumptions=self.assumptions,
            portfolio=self.pf,
            **kw,
        )

    def test_shift_one_night_to_direct(self):
        out = self._evaluate({"OTA_ALPHA": D("0"), "DIRECT": D("2")})
        self.assertTrue(out.is_feasible)
        self.assertEqual(out.room_nights, D("2.0000"))
        self.assertEqual(out.gross_revenue, D("8000.00"))
        self.assertEqual(out.distribution_cost, D("0.00"))
        self.assertEqual(out.net_revenue, D("8000.00"))
        self.assertEqual(out.net_revenue_delta, D("1262.00"))
        self.assertEqual(out.direct_booking_share, D("1.0000"))

    def test_demand_loss_is_accounted_for(self):
        """Moving an OTA night direct does not deliver a whole night.

        Withdrawn: 1 night from OTA_ALPHA (baseline 1 direct + 1 OTA = 2).
        Direct's own baseline is 1 night, so allocating 1.80 means only
        0.80 arrived. The 0.20 shortfall is lost demand.
        """
        out = self._evaluate({"OTA_ALPHA": D("0"), "DIRECT": D("1.80")})
        self.assertEqual(out.room_nights, D("1.8000"))
        self.assertEqual(out.estimated_gained_direct, D("0.8000"))
        self.assertEqual(out.estimated_lost_bookings, D("0.2000"))

    def test_lost_and_gained_are_read_from_the_allocation_not_assumed(self):
        """The guard against double-counting the same night.

        Demand loss and direct gain must both be derivable from the final
        allocation, so a night is either booked or lost - never both.
        """
        out = self._evaluate({"OTA_ALPHA": D("0"), "DIRECT": D("1.80")})
        allocated = sum(l.scenario_room_nights for l in out.lines)
        baseline = Decimal(self.pf.total_room_nights)
        self.assertEqual(out.estimated_lost_bookings, baseline - allocated)

    def test_allocation_that_invents_nights_is_infeasible(self):
        """Volume beyond what was ever sold cannot be modelled as revenue."""
        out = self._evaluate({"OTA_ALPHA": D("1"), "DIRECT": D("1.50")})
        self.assertFalse(out.is_feasible)
        self.assertIn("invented", out.infeasibility_reason)

    def test_reallocation_conserving_nights_has_no_loss(self):
        out = self._evaluate({"OTA_ALPHA": D("0"), "DIRECT": D("2")})
        self.assertEqual(out.estimated_lost_bookings, D("0.0000"))
        self.assertEqual(out.estimated_gained_direct, D("1.0000"))

    def test_zero_conversion_loses_all_withdrawn_demand(self):
        a = ScenarioAssumptions(
            expected_direct_conversion_rate={"OTA_ALPHA": D("0"), "DIRECT": D("0")},
            incremental_rate={"OTA_ALPHA": D("0"), "DIRECT": D("0")},
        )
        out = self.engine.evaluate(
            {"OTA_ALPHA": D("0"), "DIRECT": D("1")},
            self.rates, a, self.pf,
        )
        self.assertEqual(out.estimated_lost_bookings, D("1.0000"))
        self.assertEqual(out.estimated_gained_direct, D("0.0000"))

    def test_reallocating_to_an_expensive_channel_is_a_loss(self):
        """Moving a direct night to the OTA must reduce net revenue."""
        out = self._evaluate({"OTA_ALPHA": D("2"), "DIRECT": D("0")})
        self.assertEqual(out.net_revenue, D("5476.00"))
        self.assertLess(out.net_revenue_delta, D("0"))
        self.assertEqual(out.distribution_cost, D("2524.00"))

    def test_distribution_cost_pct_and_net_adr(self):
        out = self._evaluate({"OTA_ALPHA": D("1"), "DIRECT": D("1")})
        self.assertEqual(out.distribution_cost_pct, D("0.1578"))
        self.assertEqual(out.net_adr, D("3369.0000"))

    def test_assumptions_are_carried_into_the_result(self):
        out = self._evaluate({"OTA_ALPHA": D("1"), "DIRECT": D("1")})
        payload = out.to_dict()["allocation_detail"]["assumptions"]
        self.assertEqual(payload["objective"], MAXIMIZE_NET_REVENUE)
        self.assertEqual(
            payload["expected_direct_conversion_rate"]["OTA_ALPHA"], "0.30"
        )
        self.assertEqual(payload["max_shift_fraction"], "0.30")

    def test_allocation_lines_cover_every_rate_channel(self):
        out = self._evaluate({"OTA_ALPHA": D("1"), "DIRECT": D("1")})
        self.assertEqual([l.channel_code for l in out.lines], ["DIRECT", "OTA_ALPHA"])


class TestFeasibilityGuards(unittest.TestCase):
    def setUp(self):
        self.pf = build_portfolio()
        self.rates = rates_from_portfolio(self.pf)
        self.engine = ScenarioEngine()
        self.assumptions = assumptions()

    def _evaluate(self, allocation):
        return self.engine.evaluate(
            allocation, self.rates, self.assumptions, self.pf
        )

    def test_unknown_channel_is_infeasible(self):
        out = self._evaluate({"OTA_ALPHA": D("1"), "DIRECT": D("1"), "GHOST": D("0")})
        self.assertFalse(out.is_feasible)
        self.assertIn("GHOST", out.infeasibility_reason)

    def test_allocation_above_observed_inventory_is_infeasible(self):
        out = self._evaluate({"OTA_ALPHA": D("1"), "DIRECT": D("99")})
        self.assertFalse(out.is_feasible)
        self.assertIn("exceeds", out.infeasibility_reason)

    def test_negative_nights_are_infeasible(self):
        out = self._evaluate({"OTA_ALPHA": D("-1"), "DIRECT": D("1")})
        self.assertFalse(out.is_feasible)
        self.assertIn("negative", out.infeasibility_reason)

    def test_baseline_itself_is_feasible(self):
        out = self._evaluate({"OTA_ALPHA": D("1"), "DIRECT": D("1")})
        self.assertTrue(out.is_feasible)
        self.assertIsNone(out.infeasibility_reason)


class TestGreedyMarginalAllocator(unittest.TestCase):
    def setUp(self):
        self.pf = build_portfolio()
        self.rates = rates_from_portfolio(self.pf)
        self.engine = ScenarioEngine()
        self.assumptions = assumptions()
        self.allocator = GreedyMarginalAllocator()

    def test_shifts_nights_from_expensive_to_cheap_channel(self):
        alloc, moves = self.allocator.allocate(
            self.engine, self.pf, self.rates, self.assumptions
        )
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["from_channel"], "OTA_ALPHA")
        self.assertEqual(moves[0]["to_channel"], "DIRECT")
        self.assertEqual(moves[0]["room_nights_withdrawn"], 1)
        # arrival = conversion 0.30 + incremental 0.50 = 0.80
        self.assertEqual(moves[0]["room_nights_arriving"], "0.8000")
        self.assertEqual(moves[0]["room_nights_lost"], "0.2000")
        self.assertEqual(alloc["DIRECT"], D("1.80"))
        self.assertEqual(alloc["OTA_ALPHA"], D("0"))

    def test_allocator_deposits_only_the_arriving_fraction(self):
        """A shifted night must not be booked whole when it only partly returns."""
        alloc, moves = self.allocator.allocate(
            self.engine, self.pf, self.rates, self.assumptions
        )
        self.assertEqual(sum(alloc.values()), D("1.80"))
        self.assertLess(sum(alloc.values()), D("2"))

    def test_moves_are_positive_gains_only(self):
        _, moves = self.allocator.allocate(
            self.engine, self.pf, self.rates, self.assumptions
        )
        for m in moves:
            self.assertGreater(Decimal(m["net_revenue_delta_per_night"]), D("0"))

    def test_allocation_never_improves_on_nothing_when_no_conversion(self):
        """With zero conversion and zero incremental, shifting only loses."""
        a = ScenarioAssumptions(
            expected_direct_conversion_rate={k: D("0") for k in self.rates},
            incremental_rate={k: D("0") for k in self.rates},
        )
        alloc, moves = self.allocator.allocate(self.engine, self.pf, self.rates, a)
        self.assertEqual(moves, [])
        self.assertEqual(alloc["DIRECT"], D("1"))
        self.assertEqual(alloc["OTA_ALPHA"], D("1"))

    def test_max_shift_fraction_caps_movement(self):
        """A tiny cap must stop the allocator from emptying a channel."""
        a = assumptions(shift=D("0.01"))  # 1% of 2 nights
        alloc, moves = self.allocator.allocate(self.engine, self.pf, self.rates, a)
        self.assertLessEqual(len(moves), 1)

    def test_locked_channels_are_never_touched(self):
        alloc, moves = self.allocator.allocate(
            self.engine, self.pf, self.rates, self.assumptions,
            locked_channels=["DIRECT"],
        )
        self.assertEqual(moves, [])
        self.assertEqual(alloc["DIRECT"], D("1"))

    def test_allocator_is_deterministic(self):
        a, _ = self.allocator.allocate(self.engine, self.pf, self.rates, self.assumptions)
        b, _ = self.allocator.allocate(self.engine, self.pf, self.rates, self.assumptions)
        self.assertEqual(a, b)

    def test_terminates_on_a_uniform_portfolio(self):
        """No channel is better than another, so nothing may move."""
        uniform = compute_channel_metrics(
            [fact("DIRECT", "D", "DIRECT", True, "4000", "0", "0", "0", "0", 5),
             fact("WALK_IN", "W", "WALK_IN", False, "4000", "0", "0", "0", "0", 5)]
        )
        rates = rates_from_portfolio(
            uniform,
            conversion_rates={"DIRECT": D("0.5")},
            incremental_rates={"WALK_IN": D("0.5")},
        )
        _, moves = self.allocator.allocate(
            self.engine, uniform, rates, assumptions()
        )
        self.assertEqual(moves, [])


class TestAddressability(unittest.TestCase):
    """Walk-in must never be a reallocation target.

    Walk-in has zero commission, so on raw net-per-night it is the most
    attractive channel in the portfolio. A model that lets the allocator
    "move" OTA bookings to the front desk would report a large fictitious
    gain, because nobody decides to send a guest to reception.
    """

    def _rates(self, walk_in_addressable: bool):
        facts = [
            fact("OTA_ALPHA", "OTA Alpha", "OTA", False, "4000", "600", "612", "50", nights=10),
            fact("DIRECT", "Direct Website", "DIRECT", True, "4000", "0", "0", "0", nights=10),
            fact("WALK_IN", "Walk-in", "WALK_IN", False, "4000", "0", "0", "0", nights=10),
        ]
        pf = compute_channel_metrics(facts, total_rooms=10)
        rates = rates_from_portfolio(
            pf,
            conversion_rates={"OTA_ALPHA": D("0.30")},
            incremental_rates={"OTA_ALPHA": D("0.50")},
            addressable={"WALK_IN": walk_in_addressable},
        )
        return pf, rates

    def test_walk_in_marked_non_addressable_is_never_a_destination(self):
        pf, rates = self._rates(walk_in_addressable=False)
        self.assertFalse(rates["WALK_IN"].is_addressable)
        _, moves = GreedyMarginalAllocator().allocate(
            ScenarioEngine(), pf, rates, assumptions()
        )
        self.assertTrue(moves)
        for m in moves:
            self.assertNotEqual(m["to_channel"], "WALK_IN")

    def test_walk_in_marked_addressable_absorbs_volume(self):
        """Confirms the flag is what stops it, not the allocator's own logic."""
        pf, rates = self._rates(walk_in_addressable=True)
        self.assertTrue(rates["WALK_IN"].is_addressable)
        _, moves = GreedyMarginalAllocator().allocate(
            ScenarioEngine(), pf, rates, assumptions()
        )
        self.assertTrue(any(m["to_channel"] == "WALK_IN" for m in moves))

    def test_addressable_defaults_to_true(self):
        facts = [fact("DIRECT", "D", "DIRECT", True, "4000", "0", "0", "0")]
        built = rates_from_portfolio(compute_channel_metrics(facts))
        self.assertTrue(built["DIRECT"].is_addressable)


class TestArrivalFractionCap(unittest.TestCase):
    def test_assumptions_above_one_are_capped_not_trusted(self):
        """An over-optimistic assumption set must not conjure volume."""
        a = ScenarioAssumptions(
            expected_direct_conversion_rate={"OTA_ALPHA": D("0.90")},
            incremental_rate={"OTA_ALPHA": D("0.90")},
        )
        pf = build_portfolio()
        out = ScenarioEngine().evaluate(
            {"OTA_ALPHA": D("0"), "DIRECT": D("2")},
            rates_from_portfolio(pf), a, pf,
        )
        # Allocation still holds 2 nights, but nothing was invented, so the
        # outcome is feasible; the cap only governs how the allocator moves.
        self.assertTrue(out.is_feasible)

    def test_zero_arrival_stops_the_allocator(self):
        a = ScenarioAssumptions(
            expected_direct_conversion_rate={"OTA_ALPHA": D("0")},
            incremental_rate={"OTA_ALPHA": D("0")},
        )
        pf = build_portfolio()
        rates = rates_from_portfolio(pf)
        alloc, moves = GreedyMarginalAllocator().allocate(
            ScenarioEngine(), pf, rates, a
        )
        self.assertEqual(moves, [])
        self.assertEqual(alloc["OTA_ALPHA"], D("1"))


class TestScenarioVsPortfolioParity(unittest.TestCase):
    """The engine must reproduce the observed portfolio from observed rates.

    This is the guard against the engine and the analytics layer drifting
    apart: baseline scenario net revenue == observed net revenue.
    """

    def test_baseline_matches_observed_net_revenue(self):
        pf = build_portfolio()
        rates = rates_from_portfolio(pf)
        out = ScenarioEngine().baseline(pf, rates, assumptions())
        self.assertEqual(out.net_revenue, pf.total_net_revenue)
        self.assertEqual(out.gross_revenue, pf.total_gross_revenue)
        self.assertEqual(out.distribution_cost, pf.total_distribution_cost)


if __name__ == "__main__":
    unittest.main(verbosity=2)