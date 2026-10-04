"""Tests for the deterministic recommendation engine.

The point of these tests is not that the engine produces *a* list - it is
that every claim in that list is arithmetic on supplied numbers, that
observation and projection are labelled apart, and that nothing is
fabricated when the data is thin.
"""

import unittest
from datetime import date
from decimal import Decimal

from app.analytics.channel_analytics import BookingFact, compute_channel_metrics
from app.services.recommendations import (
    CANNIBALIZATION,
    CHANNEL_MIX,
    COST_REDUCTION,
    DIRECT_CONVERSION,
    MODELLED_ESTIMATE,
    OBSERVED,
    SCENARIO_ASSUMPTION,
    RecommendationEngine,
)
from app.services.scenario_engine import ScenarioAssumptions

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


class TestNoHallucination(unittest.TestCase):
    """The most important tests in this file."""

    def test_every_evidence_string_contains_actual_figures(self):
        pf = compute_channel_metrics(
            [
                fact("OTA_ALPHA", "OTA Alpha", "OTA", False, "4000", "600", "612", "50"),
                fact("DIRECT", "Direct Website", "DIRECT", True, "4000", "0", "0", "0"),
            ],
            total_rooms=10,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        )
        recs = RecommendationEngine().generate(pf)
        self.assertTrue(recs)
        for r in recs:
            self.assertTrue(r.evidence.strip(), f"{r.title} has empty evidence")
            # Every recommendation must cite at least one rupee figure and
            # one percentage, so a reader can check it.
            self.assertIn("Rs", r.evidence, f"{r.title} cites no rupee amount")
            self.assertIn("%", r.evidence, f"{r.title} cites no percentage")

    def test_every_recommendation_declares_its_evidence_type(self):
        pf = compute_channel_metrics(
            [
                fact("OTA_ALPHA", "OTA Alpha", "OTA", False, "4000", "600", "612", "50"),
                fact("DIRECT", "Direct Website", "DIRECT", True, "4000", "0", "0", "0"),
            ],
            total_rooms=10,
        )
        valid = {OBSERVED, MODELLED_ESTIMATE, SCENARIO_ASSUMPTION}
        for r in RecommendationEngine().generate(pf):
            self.assertIn(r.evidence_type, valid)

    def test_empty_data_yields_no_recommendations(self):
        """No data means no claims. Silence is the honest output."""
        pf = compute_channel_metrics([])
        self.assertEqual(RecommendationEngine().generate(pf), [])

    def test_zero_cost_portfolio_yields_no_cost_recommendations(self):
        """Nothing to renegotiate when nothing is being charged."""
        pf = compute_channel_metrics(
            [fact("DIRECT", "Direct", "DIRECT", True, "1000", "0", "0", "0")]
        )
        recs = RecommendationEngine().generate(pf)
        self.assertEqual([r for r in recs if r.category == COST_REDUCTION], [])

    def test_priority_is_within_range_and_sorted(self):
        pf = compute_channel_metrics(
            [
                fact("OTA_ALPHA", "OTA Alpha", "OTA", False, "4000", "600", "612", "50"),
                fact("OTA_BETA", "OTA Beta", "OTA", False, "4000", "700", "680", "50"),
                fact("DIRECT", "Direct Website", "DIRECT", True, "4000", "0", "0", "0"),
            ],
            total_rooms=10,
        )
        recs = RecommendationEngine().generate(pf)
        priorities = [r.priority for r in recs]
        for p in priorities:
            self.assertGreaterEqual(p, 0)
            self.assertLessEqual(p, 100)
        self.assertEqual(priorities, sorted(priorities, reverse=True))

    def test_output_is_deterministic(self):
        pf = compute_channel_metrics(
            [
                fact("OTA_ALPHA", "OTA Alpha", "OTA", False, "4000", "600", "612", "50"),
                fact("DIRECT", "Direct Website", "DIRECT", True, "4000", "0", "0", "0"),
            ],
            total_rooms=10,
        )
        a = [r.to_dict() for r in RecommendationEngine().generate(pf)]
        b = [r.to_dict() for r in RecommendationEngine().generate(pf)]
        self.assertEqual(a, b)


class TestCostReductionRule(unittest.TestCase):
    def setUp(self):
        self.pf = compute_channel_metrics(
            [
                fact("OTA_ALPHA", "OTA Alpha", "OTA", False, "4000", "600", "612", "50"),
                fact("DIRECT", "Direct Website", "DIRECT", True, "4000", "0", "0", "0"),
            ],
            total_rooms=10,
        )
        self.recs = RecommendationEngine().generate(self.pf)

    def test_ota_is_flagged_as_cost_reduction(self):
        cost = [r for r in self.recs if r.category == COST_REDUCTION]
        self.assertEqual(len(cost), 1)
        self.assertEqual(cost[0].channel_code, "OTA_ALPHA")
        self.assertEqual(cost[0].evidence_type, OBSERVED)

    def test_share_gap_appears_in_the_title(self):
        cost = [r for r in self.recs if r.category == COST_REDUCTION][0]
        # OTA has 50% of bookings but 40.64% of net revenue
        self.assertIn("50.00%", cost.title)
        self.assertIn("40.64%", cost.title)

    def test_impact_is_a_rate_delta_not_a_forecast(self):
        cost = [r for r in self.recs if r.category == COST_REDUCTION][0]
        # 1pp of 4000 gross = Rs 40.00
        self.assertIn("40.00", cost.expected_impact)
        self.assertIn("holding volume constant", cost.expected_impact)

    def test_assumption_is_stated(self):
        cost = [r for r in self.recs if r.category == COST_REDUCTION][0]
        self.assertIn("negotiable", cost.assumption)


class TestCannibalizationRule(unittest.TestCase):
    def test_direct_adr_advantage_is_reported(self):
        pf = compute_channel_metrics(
            [
                fact("OTA_ALPHA", "OTA Alpha", "OTA", False, "4000", "600", "612", "50"),
                fact("DIRECT", "Direct Website", "DIRECT", True, "4000", "0", "0", "0"),
            ],
            total_rooms=10,
        )
        recs = RecommendationEngine().generate(pf)
        cann = [r for r in recs if r.category == CANNIBALIZATION]
        self.assertEqual(len(cann), 1)
        self.assertEqual(cann[0].evidence_type, OBSERVED)
        self.assertIn("4,000.00", cann[0].title)   # direct net ADR
        self.assertIn("1,262.00", cann[0].title)  # the gap
        self.assertIn("upper bound", cann[0].evidence)
        self.assertIn("NOT a forecast", cann[0].evidence)

    def test_no_rule_when_adr_advantage_is_negligible(self):
        """A Rs 100/night gap is noise, not a recommendation."""
        pf = compute_channel_metrics(
            [
                fact("OTA_ALPHA", "OTA Alpha", "OTA", False, "1000", "100", "100", "0"),
                fact("DIRECT", "Direct Website", "DIRECT", True, "1000", "0", "0", "0"),
            ],
            total_rooms=10,
        )
        recs = RecommendationEngine().generate(pf)
        self.assertEqual([r for r in recs if r.category == CANNIBALIZATION], [])

    def test_no_rule_without_a_direct_channel(self):
        pf = compute_channel_metrics(
            [fact("OTA_ALPHA", "OTA Alpha", "OTA", False, "4000", "600", "612", "50")]
        )
        recs = RecommendationEngine().generate(pf)
        self.assertEqual([r for r in recs if r.category == CANNIBALIZATION], [])


class TestDirectConversionRule(unittest.TestCase):
    def setUp(self):
        self.pf = compute_channel_metrics(
            [
                fact("OTA_ALPHA", "OTA Alpha", "OTA", False, "4000", "600", "612", "50"),
                fact("DIRECT", "Direct Website", "DIRECT", True, "4000", "0", "0", "0"),
            ],
            total_rooms=10,
        )

    def test_no_conversion_rule_without_assumptions(self):
        """Never assume a conversion rate the hotel has not stated."""
        recs = RecommendationEngine().generate(self.pf)
        self.assertEqual([r for r in recs if r.category == DIRECT_CONVERSION], [])

    def test_conversion_is_labelled_modelled_not_observed(self):
        a = ScenarioAssumptions(
            expected_direct_conversion_rate={"OTA_ALPHA": D("0.30")}
        )
        recs = RecommendationEngine().generate(self.pf, a)
        conv = [r for r in recs if r.category == DIRECT_CONVERSION]
        self.assertEqual(len(conv), 1)
        self.assertEqual(conv[0].evidence_type, MODELLED_ESTIMATE)
        self.assertEqual(conv[0].confidence, "MEDIUM")

    def test_gain_arithmetic_is_stated_and_correct(self):
        """1 night x 30% x (4000 - 2738) = 378.60."""
        a = ScenarioAssumptions(
            expected_direct_conversion_rate={"OTA_ALPHA": D("0.30")}
        )
        conv = [
            r for r in RecommendationEngine().generate(self.pf, a)
            if r.category == DIRECT_CONVERSION
        ][0]
        self.assertIn("378.60", conv.expected_impact)
        self.assertIn("30.00%", conv.evidence)
        self.assertIn("1,262.00", conv.evidence)

    def test_zero_conversion_produces_no_recommendation(self):
        a = ScenarioAssumptions(
            expected_direct_conversion_rate={"OTA_ALPHA": D("0")}
        )
        recs = RecommendationEngine().generate(self.pf, a)
        self.assertEqual([r for r in recs if r.category == DIRECT_CONVERSION], [])

    def test_no_gain_when_direct_is_not_better(self):
        """A conversion that loses money must not be recommended."""
        pf = compute_channel_metrics(
            [
                # OTA is *cheaper* than direct here (bulk corporate net).
                fact("OTA_ALPHA", "OTA Alpha", "OTA", False, "4000", "0", "100", "0"),
                fact("DIRECT", "Direct Website", "DIRECT", True, "1000", "0", "0", "0"),
            ],
            total_rooms=10,
        )
        a = ScenarioAssumptions(
            expected_direct_conversion_rate={"OTA_ALPHA": D("0.30")}
        )
        recs = RecommendationEngine().generate(pf, a)
        self.assertEqual([r for r in recs if r.category == DIRECT_CONVERSION], [])


class TestChannelMixRule(unittest.TestCase):
    def test_low_direct_share_is_observed(self):
        """9 OTA nights to 1 direct night = 10% direct, under the 20% floor."""
        facts = [
            fact("OTA_ALPHA", "OTA Alpha", "OTA", False, "4000", "600", "612", "50")
            for _ in range(9)
        ] + [fact("DIRECT", "Direct Website", "DIRECT", True, "4000", "0", "0", "0")]
        pf = compute_channel_metrics(
            facts,
            total_rooms=10,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        )
        recs = RecommendationEngine().generate(pf)
        mix = [r for r in recs if r.category == CHANNEL_MIX]
        self.assertTrue(mix)
        self.assertEqual(mix[0].evidence_type, OBSERVED)
        self.assertIn("10.00%", mix[0].evidence)   # direct room-night share
        # 4,000 direct net of 28,642 portfolio net = 13.97%
        self.assertIn("13.97%", mix[0].evidence)

    def test_strong_direct_share_suppresses_the_mix_rule(self):
        pf = compute_channel_metrics(
            [
                fact("OTA_ALPHA", "OTA Alpha", "OTA", False, "4000", "600", "612", "50"),
                fact("DIRECT", "Direct Website", "DIRECT", True, "40000", "0", "0", "0"),
            ],
            total_rooms=1,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 1),
        )
        recs = RecommendationEngine().generate(pf)
        mix = [r for r in recs if r.category == CHANNEL_MIX]
        # 1 of 2 nights direct over 1 room x 1 day -> 50% > 20% floor
        self.assertEqual(mix, [])

    def test_scenario_recommendation_is_flagged_as_assumption(self):
        from app.services.scenario_engine import ScenarioEngine, rates_from_portfolio

        pf = compute_channel_metrics(
            [
                fact("OTA_ALPHA", "OTA Alpha", "OTA", False, "4000", "600", "612", "50"),
                fact("DIRECT", "Direct Website", "DIRECT", True, "4000", "0", "0", "0"),
            ],
            total_rooms=10,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        )
        a = ScenarioAssumptions(
            expected_direct_conversion_rate={"OTA_ALPHA": D("0.30")}
        )
        rates = rates_from_portfolio(pf)
        scenario = ScenarioEngine().evaluate(
            {"OTA_ALPHA": D("0"), "DIRECT": D("1")}, rates, a, pf, name="All direct"
        )
        recs = RecommendationEngine().generate(pf, a, scenario)
        scen = [r for r in recs if r.evidence_type == SCENARIO_ASSUMPTION]
        self.assertTrue(scen)
        self.assertIn("greedy heuristic, not a proven optimum", scen[0].assumption)


if __name__ == "__main__":
    unittest.main(verbosity=2)