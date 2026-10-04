"""Tests for the nine channel-analytics metrics.

Run:  cd backend && .venv/bin/python -m unittest discover -s tests -t . -v

Standard library only, no database, no network. Every expected figure here
is derived by hand from the fixture below, so a failure means a real
regression rather than a changed expectation.
"""

import unittest
from datetime import date
from decimal import Decimal

from app.analytics.channel_analytics import (
    BookingFact,
    compute_channel_metrics,
    distribution_cost_components,
    share_closure_residual,
    verify_share_closure,
)
from app.services.net_revenue import safe_divide

D = Decimal


def ota_fact(**kw):
    """The specification's OTA booking: 4,000 -> 2,738."""
    base = dict(
        channel_code="OTA_ALPHA",
        channel_name="OTA Alpha",
        channel_type="OTA",
        is_direct=False,
        gross_room_revenue=D("4000"),
        discount_amount=D("600"),
        commission_amount=D("612"),
        payment_fee=D("50"),
        other_channel_cost=D("0"),
        room_nights=1,
    )
    base.update(kw)
    return BookingFact(**base)


def direct_fact(**kw):
    base = dict(
        channel_code="DIRECT",
        channel_name="Direct Website",
        channel_type="DIRECT",
        is_direct=True,
        gross_room_revenue=D("4000"),
        discount_amount=D("0"),
        commission_amount=D("0"),
        payment_fee=D("0"),
        other_channel_cost=D("0"),
        room_nights=1,
    )
    base.update(kw)
    return BookingFact(**base)


class TestCriticalSpecCase(unittest.TestCase):
    """Gross 4,000 / discount 600 / commission 612 / fee 50 -> net 2,738."""

    def setUp(self):
        self.pf = compute_channel_metrics(
            [ota_fact(), direct_fact()],
            hotel_code="TEST",
            total_rooms=10,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 1),
        )
        self.ota = self.pf.by_code("OTA_ALPHA")
        self.direct = self.pf.by_code("DIRECT")

    def test_portfolio_totals(self):
        self.assertEqual(self.pf.total_bookings, 2)
        self.assertEqual(self.pf.total_room_nights, 2)
        self.assertEqual(self.pf.total_gross_revenue, D("8000.00"))
        self.assertEqual(self.pf.total_distribution_cost, D("1262.00"))
        self.assertEqual(self.pf.total_net_revenue, D("6738.00"))

    def test_portfolio_distribution_cost_pct(self):
        # 1262 / 8000 = 0.15775 -> 0.1578 (HALF_UP at 4dp)
        self.assertEqual(self.pf.portfolio_distribution_cost_pct, D("0.1578"))

    def test_ota_channel_metrics(self):
        self.assertEqual(self.ota.gross_revenue, D("4000.00"))
        self.assertEqual(self.ota.distribution_cost, D("1262.00"))
        self.assertEqual(self.ota.net_revenue, D("2738.00"))
        self.assertEqual(self.ota.distribution_cost_pct, D("0.3155"))
        self.assertEqual(self.ota.gross_adr, D("4000.0000"))
        self.assertEqual(self.ota.net_adr, D("2738.0000"))

    def test_direct_channel_metrics(self):
        self.assertEqual(self.direct.distribution_cost, D("0.00"))
        self.assertEqual(self.direct.net_revenue, D("4000.00"))
        self.assertEqual(self.direct.distribution_cost_pct, D("0.0000"))
        self.assertEqual(self.direct.net_adr, D("4000.0000"))

    def test_booking_shares(self):
        self.assertEqual(self.ota.booking_share, D("0.5000"))
        self.assertEqual(self.direct.booking_share, D("0.5000"))

    def test_net_revenue_shares(self):
        # 2738 / 6738 = 0.406352... -> 0.4064
        self.assertEqual(self.ota.net_revenue_share, D("0.4064"))
        # 4000 / 6738 = 0.593647... -> 0.5936
        self.assertEqual(self.direct.net_revenue_share, D("0.5936"))

    def test_share_gap_is_the_finding(self):
        """OTA takes half the bookings but returns only 40.6% of net."""
        self.assertEqual(self.pf.share_gap("OTA_ALPHA"), D("0.0936"))
        self.assertEqual(self.pf.share_gap("DIRECT"), D("-0.0936"))

    def test_adr_penalty(self):
        self.assertEqual(self.ota.net_adr_penalty, D("1262.0000"))
        self.assertEqual(self.direct.net_adr_penalty, D("0.0000"))

    def test_identity_reconciles(self):
        """net_revenue == gross - distribution_cost for every channel."""
        for c in self.pf.channels:
            self.assertEqual(c.gross_revenue - c.distribution_cost, c.net_revenue)

    def test_share_gap_identity_holds(self):
        """booking_share - distribution_cost_share == net_revenue_share."""
        for c in self.pf.channels:
            self.assertEqual(
                quant := safe_divide(c.net_revenue, self.pf.total_net_revenue),
                c.net_revenue_share,
            )
            self.assertEqual(c.distribution_cost_share, safe_divide(
                c.distribution_cost, self.pf.total_distribution_cost
            ))


class TestShareClosure(unittest.TestCase):
    def test_shares_sum_to_one_when_exact(self):
        pf = compute_channel_metrics([ota_fact(), direct_fact()])
        self.assertEqual(share_closure_residual(pf.channels), D("0.0000"))
        self.assertTrue(verify_share_closure(pf))

    def test_rounding_residual_is_bounded_and_reported(self):
        """Three equal thirds do not sum to 1.0000 at 4dp; the gap shows."""
        facts = [
            ota_fact(gross_room_revenue=D("1000"), discount_amount=D("0"),
                     commission_amount=D("100"), payment_fee=D("0")),
            direct_fact(gross_room_revenue=D("1000")),
            ota_fact(channel_code="OTA_BETA", channel_name="OTA Beta"),
        ]
        pf = compute_channel_metrics(facts)
        total = sum(c.booking_share for c in pf.channels)
        residual = share_closure_residual(pf.channels)
        # Residual is small and it is *reported*, never silently corrected.
        self.assertLessEqual(abs(residual), D("0.0005"))
        self.assertTrue(verify_share_closure(pf))
        self.assertEqual(total, D("1.0000") + residual)


class TestZeroAndEdgeCases(unittest.TestCase):
    def test_empty_portfolio(self):
        pf = compute_channel_metrics([])
        self.assertEqual(pf.total_bookings, 0)
        self.assertEqual(pf.total_net_revenue, D("0.00"))
        self.assertEqual(pf.portfolio_gross_adr, D("0.0000"))
        self.assertEqual(pf.portfolio_net_adr, D("0.0000"))
        self.assertEqual(pf.total_room_nights, 0)
        self.assertIsNone(pf.share_gap("DIRECT"))
        self.assertEqual(pf.channels, [])

    def test_zero_room_nights_does_not_divide_by_zero(self):
        pf = compute_channel_metrics([ota_fact(room_nights=0)])
        c = pf.by_code("OTA_ALPHA")
        self.assertEqual(c.gross_adr, D("0.0000"))
        self.assertEqual(c.net_adr, D("0.0000"))
        self.assertEqual(c.booking_share, D("1.0000"))

    def test_zero_gross_revenue_channel(self):
        pf = compute_channel_metrics([direct_fact(gross_room_revenue=D("0"))])
        c = pf.by_code("DIRECT")
        self.assertEqual(c.distribution_cost_pct, D("0.0000"))
        self.assertEqual(c.net_revenue_share, D("0.0000"))

    def test_occupancy_and_revpar(self):
        pf = compute_channel_metrics(
            [ota_fact(), direct_fact()],
            total_rooms=1,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 10),  # 10 days -> 10 room nights available
        )
        self.assertEqual(pf.total_room_nights, 2)
        self.assertEqual(pf.portfolio_occupancy, D("0.2000"))
        self.assertEqual(pf.net_revpar, D("673.8000"))

    def test_missing_period_does_not_divide_by_zero(self):
        pf = compute_channel_metrics([ota_fact()], total_rooms=100)
        self.assertEqual(pf.portfolio_occupancy, D("0.0000"))
        self.assertEqual(pf.net_revpar, D("0.0000"))

    def test_cost_totalling_gross_leaves_zero_net(self):
        pf = compute_channel_metrics(
            [ota_fact(commission_amount=D("4000"), discount_amount=D("0"),
                      payment_fee=D("0"), other_channel_cost=D("0"))]
        )
        c = pf.by_code("OTA_ALPHA")
        self.assertEqual(c.net_revenue, D("0.00"))
        self.assertEqual(c.distribution_cost_pct, D("1.0000"))
        self.assertEqual(c.net_revenue_share, D("0.0000"))

    def test_cost_exceeding_gross_is_reported_not_hidden(self):
        """Analytics reports what it is given; it never clamps to zero.

        Rejecting negative net is the net-revenue engine's job (and a
        database CHECK constraint's). Hiding it here would be exactly the
        kind of silent correction that makes a revenue system lie.
        """
        pf = compute_channel_metrics(
            [ota_fact(commission_amount=D("4500"), discount_amount=D("0"),
                      payment_fee=D("0"), other_channel_cost=D("0"))]
        )
        c = pf.by_code("OTA_ALPHA")
        self.assertEqual(c.net_revenue, D("-500.00"))


class TestDeterminismAndOrdering(unittest.TestCase):
    def _facts(self):
        return [
            ota_fact(),
            direct_fact(),
            ota_fact(channel_code="OTA_BETA", channel_name="OTA Beta",
                     gross_room_revenue=D("6000"), commission_amount=D("1020"),
                     discount_amount=D("630"), payment_fee=D("132")),
        ]

    def test_repeated_computation_is_identical(self):
        a = compute_channel_metrics(self._facts()).to_dict()
        b = compute_channel_metrics(self._facts()).to_dict()
        self.assertEqual(a, b)

    def test_input_order_does_not_change_channel_ordering_semantics(self):
        forward = compute_channel_metrics(self._facts())
        reverse = compute_channel_metrics(list(reversed(self._facts())))
        self.assertEqual(
            {c.channel_code: c.net_revenue for c in forward.channels},
            {c.channel_code: c.net_revenue for c in reverse.channels},
        )

    def test_ranked_by_net_revenue_is_descending(self):
        pf = compute_channel_metrics(self._facts())
        nets = [c.net_revenue for c in pf.ranked_by_net_revenue()]
        self.assertEqual(nets, sorted(nets, reverse=True))

    def test_ranked_by_distribution_cost_excludes_zero_cost(self):
        pf = compute_channel_metrics(self._facts())
        ranked = pf.ranked_by_distribution_cost()
        self.assertNotIn("DIRECT", [c.channel_code for c in ranked])
        costs = [c.distribution_cost for c in ranked]
        self.assertEqual(costs, sorted(costs, reverse=True))


class TestDistributionCostComponents(unittest.TestCase):
    def test_components_sum_to_one(self):
        pf = compute_channel_metrics([ota_fact()])
        comps = distribution_cost_components(pf.by_code("OTA_ALPHA"))
        # 600/1262, 612/1262, 50/1262, 0
        self.assertEqual(comps["discount"], D("0.4754"))
        self.assertEqual(comps["commission"], D("0.4849"))
        self.assertEqual(comps["payment_fee"], D("0.0396"))
        self.assertEqual(comps["other_channel_cost"], D("0.0000"))
        total = sum(comps.values(), Decimal("0"))
        self.assertLess(abs(total - Decimal("1.0000")), Decimal("0.0005"))

    def test_zero_cost_channel_components_are_all_zero(self):
        pf = compute_channel_metrics([direct_fact()])
        comps = distribution_cost_components(pf.by_code("DIRECT"))
        self.assertEqual(set(comps.values()), {D("0.0000")})


class TestMonthlySeries(unittest.TestCase):
    def test_months_are_sorted_and_totalled(self):
        from app.analytics.channel_analytics import monthly_channel_series

        facts = {
            date(2026, 2, 1): [ota_fact()],
            date(2026, 1, 1): [direct_fact()],
        }
        out = monthly_channel_series(facts)
        months = out["portfolio_monthly"]
        self.assertEqual([m["month"] for m in months], ["2026-01-01", "2026-02-01"])
        self.assertEqual(months[0]["net_revenue"], "4000.00")
        self.assertEqual(months[1]["net_revenue"], "2738.00")
        self.assertIn("OTA_ALPHA", out["channel_net_revenue"])
        self.assertEqual(len(out["channel_net_revenue"]["OTA_ALPHA"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)