"""Financial correctness tests for the D6 Net Revenue Engine.

Run:  cd backend && .venv/bin/python -m unittest discover -s tests -v

These are ``unittest`` tests on purpose: they depend on nothing outside
the standard library, so the hotel's financial truth can be verified on
any machine, in CI, or in a debugging session with no environment set up.
"""

import unittest
from decimal import Decimal

from app.services.net_revenue import (
    BookingEconomicsInput,
    EconomicsValidationError,
    ErrorCode,
    NetRevenueEngine,
    net_revenue_engine,
)


class TestCriticalFinancialCases(unittest.TestCase):
    """The cases named explicitly in the D6 specification."""

    def test_ota_booking_yields_2738_net(self):
        """Spec case: Rs 4,000 gross through an OTA retains Rs 2,738.

        Gross      4,000
        Discount     600
        Commission   612
        Payment fee   50
        Other          0
        ---------------------------
        Net        2,738
        """
        result = net_revenue_engine.calculate(
            BookingEconomicsInput(
                gross_room_revenue=Decimal("4000"),
                discount_amount=Decimal("600"),
                commission_amount=Decimal("612"),
                payment_fee=Decimal("50"),
                other_channel_cost=Decimal("0"),
                room_nights=1,
            )
        )

        self.assertEqual(result.gross_room_revenue, Decimal("4000.00"))
        self.assertEqual(result.distribution_cost, Decimal("1262.00"))
        self.assertEqual(result.net_room_revenue, Decimal("2738.00"))
        self.assertEqual(result.gross_adr, Decimal("4000.0000"))
        self.assertEqual(result.net_adr, Decimal("2738.0000"))

    def test_direct_booking_retains_full_gross(self):
        """Spec case: a direct booking keeps the entire Rs 4,000."""
        result = net_revenue_engine.calculate(
            BookingEconomicsInput(
                gross_room_revenue=Decimal("4000"),
                discount_amount=Decimal("0"),
                commission_amount=Decimal("0"),
                payment_fee=Decimal("0"),
                other_channel_cost=Decimal("0"),
                room_nights=1,
            )
        )

        self.assertEqual(result.net_room_revenue, Decimal("4000.00"))
        self.assertEqual(result.distribution_cost, Decimal("0.00"))

    def test_gross_versus_net_gap_is_explicit(self):
        """The product's core claim, asserted numerically.

        The same Rs 4,000 stay is worth Rs 4,000 direct but Rs 2,738 via
        an OTA. A 31.55% distribution cost is the entire reason this
        platform exists.
        """
        direct = net_revenue_engine.calculate(
            BookingEconomicsInput(gross_room_revenue=Decimal("4000"), room_nights=1)
        )
        ota = net_revenue_engine.calculate(
            BookingEconomicsInput(
                gross_room_revenue=Decimal("4000"),
                discount_amount=Decimal("600"),
                commission_amount=Decimal("612"),
                payment_fee=Decimal("50"),
                room_nights=1,
            )
        )

        gap = direct.net_room_revenue - ota.net_room_revenue
        self.assertEqual(gap, Decimal("1262.00"))
        self.assertEqual(ota.distribution_cost_pct, Decimal("0.3155"))


class TestDistributionCostIdentity(unittest.TestCase):
    def test_all_four_components_are_summed(self):
        result = net_revenue_engine.calculate(
            BookingEconomicsInput(
                gross_room_revenue=Decimal("10000"),
                discount_amount=Decimal("500"),
                commission_amount=Decimal("1200"),
                payment_fee=Decimal("80"),
                other_channel_cost=Decimal("20"),
                room_nights=2,
            )
        )
        self.assertEqual(result.distribution_cost, Decimal("1800.00"))
        self.assertEqual(result.net_room_revenue, Decimal("8200.00"))

    def test_distribution_cost_pct(self):
        result = net_revenue_engine.calculate(
            BookingEconomicsInput(
                gross_room_revenue=Decimal("10000"),
                discount_amount=Decimal("500"),
                commission_amount=Decimal("1200"),
                payment_fee=Decimal("80"),
                other_channel_cost=Decimal("20"),
                room_nights=2,
            )
        )
        self.assertEqual(result.distribution_cost_pct, Decimal("0.1800"))

    def test_100_percent_commission_leaves_zero_net(self):
        """A 100% commission channel is worth exactly zero to the hotel."""
        result = net_revenue_engine.calculate(
            BookingEconomicsInput(
                gross_room_revenue=Decimal("5000"),
                commission_amount=Decimal("5000"),
                room_nights=1,
            )
        )
        self.assertEqual(result.net_room_revenue, Decimal("0.00"))
        self.assertEqual(result.distribution_cost_pct, Decimal("1.0000"))

    def test_missing_cost_fields_default_to_zero(self):
        """A direct booking with no cost lines at all is valid."""
        result = net_revenue_engine.calculate(
            BookingEconomicsInput(
                gross_room_revenue=Decimal("4000"),
                room_nights=2,
            )
        )
        self.assertEqual(result.distribution_cost, Decimal("0.00"))
        self.assertEqual(result.net_room_revenue, Decimal("4000.00"))
        self.assertEqual(result.net_adr, Decimal("2000.0000"))

    def test_missing_room_nights_defaults_to_zero_and_warns(self):
        result = net_revenue_engine.calculate(
            BookingEconomicsInput(gross_room_revenue=Decimal("4000"))
        )
        self.assertEqual(result.room_nights, 0)
        self.assertEqual(result.gross_adr, Decimal("0.0000"))
        self.assertEqual(result.net_room_revenue, Decimal("4000.00"))
        codes = [w.code for w in result.warnings]
        self.assertIn(ErrorCode.ZERO_ROOM_NIGHTS, codes)


class TestZeroRoomNights(unittest.TestCase):
    def test_zero_nights_does_not_raise(self):
        result = net_revenue_engine.calculate(
            BookingEconomicsInput(gross_room_revenue=Decimal("4000"), room_nights=0)
        )
        self.assertEqual(result.gross_adr, Decimal("0.0000"))
        self.assertEqual(result.net_adr, Decimal("0.0000"))

    def test_zero_gross_revenue_does_not_raise(self):
        result = net_revenue_engine.calculate(
            BookingEconomicsInput(gross_room_revenue=Decimal("0"), room_nights=3)
        )
        self.assertEqual(result.gross_adr, Decimal("0.0000"))
        self.assertEqual(result.distribution_cost_pct, Decimal("0.0000"))

    def test_net_revpar_handles_zero_availability(self):
        self.assertEqual(
            NetRevenueEngine.net_revpar(Decimal("100000"), Decimal("0")),
            Decimal("0.0000"),
        )

    def test_net_revpar_normal_case(self):
        self.assertEqual(
            NetRevenueEngine.net_revpar(Decimal("3000000"), Decimal("36500")),
            Decimal("82.1918"),
        )


class TestInvalidInputHandling(unittest.TestCase):
    def _codes(self, **kwargs) -> list:
        issues = net_revenue_engine.validate(BookingEconomicsInput(**kwargs))
        return [i.code for i in issues]

    def test_missing_gross_revenue_is_an_error(self):
        self.assertIn(ErrorCode.MISSING_GROSS_REVENUE, self._codes(gross_room_revenue=None))

    def test_negative_gross_revenue_is_an_error(self):
        self.assertIn(
            ErrorCode.NEGATIVE_GROSS_REVENUE,
            self._codes(gross_room_revenue=Decimal("-100")),
        )

    def test_negative_commission_is_an_error(self):
        self.assertIn(
            ErrorCode.NEGATIVE_COMMISSION,
            self._codes(
                gross_room_revenue=Decimal("4000"),
                commission_amount=Decimal("-100"),
                room_nights=1,
            ),
        )

    def test_negative_discount_is_an_error(self):
        self.assertIn(
            ErrorCode.NEGATIVE_DISCOUNT,
            self._codes(
                gross_room_revenue=Decimal("4000"),
                discount_amount=Decimal("-50"),
                room_nights=1,
            ),
        )

    def test_negative_payment_fee_is_an_error(self):
        self.assertIn(
            ErrorCode.NEGATIVE_PAYMENT_FEE,
            self._codes(
                gross_room_revenue=Decimal("4000"),
                payment_fee=Decimal("-10"),
                room_nights=1,
            ),
        )

    def test_negative_room_nights_is_an_error(self):
        self.assertIn(
            ErrorCode.NEGATIVE_ROOM_NIGHTS,
            self._codes(gross_room_revenue=Decimal("4000"), room_nights=-2),
        )

    def test_commission_exceeding_gross_is_a_warning_not_an_error(self):
        codes = self._codes(
            gross_room_revenue=Decimal("1000"),
            commission_amount=Decimal("1500"),
            room_nights=1,
        )
        self.assertIn(ErrorCode.INVALID_COMMISSION_RATE, codes)
        # Not fatal: still computable, but the UI must show the warning.
        result = net_revenue_engine.safe_calculate(
            BookingEconomicsInput(
                gross_room_revenue=Decimal("1000"),
                commission_amount=Decimal("900"),
                room_nights=1,
            )
        )
        self.assertIsNotNone(result)

    def test_cost_exceeding_gross_is_an_error(self):
        codes = self._codes(
            gross_room_revenue=Decimal("1000"),
            commission_amount=Decimal("1200"),
            room_nights=1,
        )
        self.assertIn(ErrorCode.NET_REVENUE_NEGATIVE, codes)

    def test_calculate_raises_on_invalid_input(self):
        with self.assertRaises(EconomicsValidationError):
            net_revenue_engine.calculate(
                BookingEconomicsInput(gross_room_revenue=Decimal("-5"))
            )

    def test_safe_calculate_returns_none_on_invalid_input(self):
        self.assertIsNone(
            net_revenue_engine.safe_calculate(
                BookingEconomicsInput(gross_room_revenue=None)
            )
        )

    def test_unparseable_amount_is_an_error(self):
        codes = self._codes(gross_room_revenue="not-a-number")
        self.assertIn(ErrorCode.UNPARSEABLE_AMOUNT, codes)


class TestMessyInputCoercion(unittest.TestCase):
    """Real CSV exports are never clean. The engine must cope."""

    def test_currency_symbols_and_commas_are_stripped(self):
        result = net_revenue_engine.calculate(
            BookingEconomicsInput(
                gross_room_revenue="Rs. 4,000",
                discount_amount="600",
                commission_amount="612",
                payment_fee="50",
                room_nights="1",
            )
        )
        self.assertEqual(result.net_room_revenue, Decimal("2738.00"))

    def test_rupee_symbol_prefix(self):
        result = net_revenue_engine.calculate(
            BookingEconomicsInput(gross_room_revenue="\u20b94,000", room_nights=1)
        )
        self.assertEqual(result.gross_room_revenue, Decimal("4000.00"))

    def test_accounting_negative_notation_is_rejected_as_negative_cost(self):
        """(600) parses as -600, which must be rejected as a negative discount."""
        codes = [
            i.code
            for i in net_revenue_engine.validate(
                BookingEconomicsInput(
                    gross_room_revenue=Decimal("4000"),
                    discount_amount="(600)",
                    room_nights=1,
                )
            )
        ]
        self.assertIn(ErrorCode.NEGATIVE_DISCOUNT, codes)

    def test_blank_cells_treated_as_zero_cost(self):
        result = net_revenue_engine.calculate(
            BookingEconomicsInput(
                gross_room_revenue=Decimal("4000"),
                discount_amount="",
                commission_amount="",
                payment_fee="",
                other_channel_cost="",
                room_nights=1,
            )
        )
        self.assertEqual(result.net_room_revenue, Decimal("4000.00"))

    def test_float_input_is_handled_without_binary_float_error(self):
        """0.1 + 0.2 must not become 0.30000000000000004 anywhere in the stack."""
        result = net_revenue_engine.calculate(
            BookingEconomicsInput(
                gross_room_revenue=1000.1,
                discount_amount=0.1,
                commission_amount=0.2,
                room_nights=1,
            )
        )
        self.assertEqual(result.net_room_revenue, Decimal("999.80"))


class TestMultiNightStays(unittest.TestCase):
    def test_two_night_adr_divides_by_nights(self):
        result = net_revenue_engine.calculate(
            BookingEconomicsInput(
                gross_room_revenue=Decimal("8000"),
                commission_amount=Decimal("1200"),
                payment_fee=Decimal("100"),
                room_nights=2,
            )
        )
        self.assertEqual(result.gross_adr, Decimal("4000.0000"))
        self.assertEqual(result.net_room_revenue, Decimal("6700.00"))
        self.assertEqual(result.net_adr, Decimal("3350.0000"))


class TestAggregation(unittest.TestCase):
    def test_aggregate_channel_totals(self):
        bookings = [
            BookingEconomicsInput(
                gross_room_revenue=Decimal("4000"),
                discount_amount=Decimal("600"),
                commission_amount=Decimal("612"),
                payment_fee=Decimal("50"),
                room_nights=1,
            ),
            BookingEconomicsInput(
                gross_room_revenue=Decimal("6000"),
                commission_amount=Decimal("900"),
                payment_fee=Decimal("60"),
                room_nights=2,
            ),
        ]
        computed = [net_revenue_engine.calculate(b) for b in bookings]
        agg = NetRevenueEngine.aggregate(computed)

        self.assertEqual(agg.booking_count, 2)
        self.assertEqual(agg.room_nights, 3)
        self.assertEqual(agg.gross_revenue, Decimal("10000.00"))
        self.assertEqual(agg.distribution_cost, Decimal("2222.00"))
        self.assertEqual(agg.net_revenue, Decimal("7778.00"))
        self.assertEqual(agg.gross_adr, Decimal("3333.3333"))
        self.assertEqual(agg.net_adr, Decimal("2592.6667"))

    def test_aggregate_empty_is_zero_not_error(self):
        agg = NetRevenueEngine.aggregate([])
        self.assertEqual(agg.booking_count, 0)
        self.assertEqual(agg.net_revenue, Decimal("0.00"))
        self.assertEqual(agg.net_adr, Decimal("0.0000"))


class TestDeterminism(unittest.TestCase):
    def test_repeated_calculation_is_identical(self):
        payload = BookingEconomicsInput(
            gross_room_revenue=Decimal("1234.56"),
            discount_amount=Decimal("78.90"),
            commission_amount=Decimal("150.25"),
            payment_fee=Decimal("12.34"),
            other_channel_cost=Decimal("5.67"),
            room_nights=3,
        )
        first = net_revenue_engine.calculate(payload)
        second = net_revenue_engine.calculate(payload)
        self.assertEqual(first.to_dict(), second.to_dict())


if __name__ == "__main__":
    unittest.main(verbosity=2)