"""Reproducibility and economic-sanity tests for the synthetic demo data.

    ############################################################
    #  THE DATA UNDER TEST IS SYNTHETIC. These tests prove that   #
    #  it is deterministic and internally consistent - not that #
    #  it describes any real hotel.                             #
    #  ############################################################

If the generator's figures drift, every recorded demo figure, screenshot
and quote in the documentation becomes false. That is why reproducibility
is asserted here rather than assumed.
"""

import json
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_demo_data import RNG_SEED, generate  # noqa: E402


class TestDeterministicGeneration(unittest.TestCase):
    """Small enough to run in milliseconds; the properties are identical."""

    @classmethod
    def setUpClass(cls):
        cls.a = generate(n_bookings=300, n_guests=60, n_rooms=100)
        cls.b = generate(n_bookings=300, n_guests=60, n_rooms=100)

    def _fingerprint(self, data):
        return hash(json.dumps([str(x) for x in data.bookings], sort_keys=True))

    def test_same_seed_produces_identical_bookings(self):
        self.assertEqual(self._fingerprint(self.a), self._fingerprint(self.b))

    def test_same_seed_produces_identical_guests(self):
        self.assertEqual(
            [(g.external_id, g.first_name, g.last_name, g.country) for g in self.a.guests],
            [(g.external_id, g.first_name, g.last_name, g.country) for g in self.b.guests],
        )

    def test_same_seed_produces_identical_channel_costs(self):
        self.assertEqual(self.a.channel_costs, self.b.channel_costs)

    def test_different_seed_produces_different_data(self):
        other = generate(n_bookings=300, n_guests=60, n_rooms=100, seed=RNG_SEED + 1)
        self.assertNotEqual(self._fingerprint(self.a), self._fingerprint(other))

    def test_booking_ids_are_unique_and_contiguous(self):
        ids = sorted(b.booking_id for b in self.a.bookings)
        self.assertEqual(len(ids), len(set(ids)))
        # IDs are assigned in creation order, then rows are sorted by date,
        # so the *set* is contiguous even though the first row is not BK000001.
        self.assertEqual(ids[0], "BK000001")
        self.assertEqual(ids[-1], f"BK{len(ids):06d}")

    def test_bookings_are_sorted_by_date(self):
        dates = [b.booking_date for b in self.a.bookings]
        self.assertEqual(dates, sorted(dates))


class TestEconomicSanity(unittest.TestCase):
    """Money in the demo set must obey the same rules as real imports."""

    @classmethod
    def setUpClass(cls):
        cls.data = generate(n_bookings=500, n_guests=100, n_rooms=100)

    def test_no_negative_amounts(self):
        for b in self.data.bookings:
            for field in (
                "gross_room_revenue", "discount_amount", "commission_amount",
                "payment_fee", "other_channel_cost",
            ):
                self.assertGreaterEqual(
                    getattr(b, field), Decimal("0"), f"{b.booking_id}.{field}"
                )

    def test_distribution_cost_never_exceeds_gross(self):
        """A demo booking can never cost the hotel more than it earns."""
        for b in self.data.bookings:
            cost = (
                b.discount_amount + b.commission_amount
                + b.payment_fee + b.other_channel_cost
            )
            self.assertLessEqual(
                cost, b.gross_room_revenue,
                f"{b.booking_id} costs {cost} against gross {b.gross_room_revenue}",
            )

    def test_gross_equals_nightly_rate_times_nights(self):
        """Gross is internally consistent with the room type and season."""
        for b in self.data.bookings:
            if b.cancellation_status == "CANCELLED":
                continue
            base = next(
                r["base_rate"] for r in self.data.room_types
                if r["code"] == b.room_type_code
            )
            nightly = (b.gross_room_revenue / Decimal(b.room_nights)).quantize(Decimal("1"))
            # Within +/-25% of base rate: seasonality and weekend premiums.
            self.assertGreater(nightly, base * Decimal("0.75"))
            self.assertLess(nightly, base * Decimal("1.75"))

    def test_cancelled_bookings_have_no_nights_or_cost(self):
        for b in self.data.bookings:
            if b.cancellation_status != "CANCELLED":
                continue
            self.assertEqual(b.room_nights, 0)
            self.assertEqual(b.commission_amount, Decimal("0.00"))
            self.assertEqual(b.payment_fee, Decimal("0.00"))

    def test_direct_channel_bookings_carry_no_commission(self):
        for b in self.data.bookings:
            if b.channel_code == "DIRECT":
                self.assertEqual(
                    b.commission_amount, Decimal("0.00"),
                    f"{b.booking_id} charged commission on a direct booking",
                )

    def test_checkout_is_after_checkin(self):
        for b in self.data.bookings:
            self.assertGreater(b.check_out_date, b.check_in_date)

    def test_booking_date_is_not_after_checkin(self):
        for b in self.data.bookings:
            self.assertLessEqual(b.booking_date, b.check_in_date)

    def test_lead_days_matches_dates(self):
        for b in self.data.bookings:
            self.assertEqual(b.booking_lead_days, (b.check_in_date - b.booking_date).days)

    def test_walk_in_bookings_have_zero_lead_time(self):
        for b in self.data.bookings:
            if b.channel_code == "WALK_IN":
                self.assertEqual(b.booking_lead_days, 0)

    def test_every_channel_and_room_type_is_exercised(self):
        seen_channels = {b.channel_code for b in self.data.bookings}
        seen_types = {b.room_type_code for b in self.data.bookings}
        self.assertEqual(seen_channels, {c["code"] for c in self.data.channels})
        self.assertEqual(seen_types, {r["code"] for r in self.data.room_types})

    def test_room_types_total_the_hotel_inventory(self):
        self.assertEqual(sum(r["occupancy_weight"] for r in self.data.room_types), 1.0)

    def test_channel_mix_weights_are_a_distribution(self):
        total = sum(c["mix_weight"] for c in self.data.channels)
        self.assertAlmostEqual(total, 1.0, places=9)

    def test_channel_costs_cover_every_channel_and_room_type_pair(self):
        pairs = {
            (c["channel_code"], c["room_type_code"]) for c in self.data.channel_costs
        }
        expected = {
            (ch["code"], rt["code"])
            for ch in self.data.channels
            for rt in self.data.room_types
        }
        self.assertEqual(pairs, expected)

    def test_all_cost_rates_are_valid_probabilities(self):
        for c in self.data.channel_costs:
            for field in (
                "commission_rate", "payment_fee_rate", "discount_rate",
                "other_cost_rate", "expected_direct_conversion_rate",
                "incremental_rate",
            ):
                self.assertGreaterEqual(c[field], Decimal("0"), f"{field} < 0")
                self.assertLessEqual(c[field], Decimal("1"), f"{field} > 1")

    def test_ota_channels_carry_real_commission(self):
        for c in self.data.channel_costs:
            if c["channel_code"].startswith("OTA"):
                self.assertGreater(c["commission_rate"], Decimal("0.10"))

    def test_zero_commission_channels_have_zero_configured_commission(self):
        """The luxury-room premium must not create commission from nothing.

        Guards the config layer: a 0% base rate must stay 0% for every room
        type, or direct bookings on suites would be charged commission.
        """
        for c in self.data.channel_costs:
            if c["channel_code"] in {"DIRECT", "WALK_IN"}:
                self.assertEqual(
                    c["commission_rate"], Decimal("0.00"),
                    f"{c['channel_code']}/{c['room_type_code']} has commission "
                    f"{c['commission_rate']}",
                )

    def test_luxury_rooms_do_cost_more_on_ota(self):
        """The premium the previous test protects must still exist."""
        std = next(
            c for c in self.data.channel_costs
            if c["channel_code"] == "OTA_ALPHA" and c["room_type_code"] == "STD"
        )
        suite = next(
            c for c in self.data.channel_costs
            if c["channel_code"] == "OTA_ALPHA" and c["room_type_code"] == "SUI"
        )
        self.assertGreater(suite["commission_rate"], std["commission_rate"])

    def test_walk_in_bookings_are_entirely_free(self):
        """Walk-in is the purest baseline: no commission, no discount."""
        for b in self.data.bookings:
            if b.channel_code != "WALK_IN" or b.cancellation_status == "CANCELLED":
                continue
            self.assertEqual(b.commission_amount, Decimal("0.00"))
            self.assertEqual(b.discount_amount, Decimal("0.00"))
            self.assertEqual(b.other_channel_cost, Decimal("0.00"))

    def test_summary_reports_the_data_source(self):
        summary = self.data.summary()
        self.assertEqual(summary["data_source"], "demo_synthetic")
        self.assertEqual(summary["bookings"], 500)


class TestGeneratorIsStandardLibraryOnly(unittest.TestCase):
    def test_generator_does_not_import_numpy_or_pandas(self):
        """The generator must run in a bare CI container."""
        source = (REPO_ROOT / "scripts" / "generate_demo_data.py").read_text()
        for banned in ("import numpy", "import pandas", "from numpy", "from pandas",
                       "import sklearn", "from sklearn"):
            self.assertNotIn(banned, source, f"generator must not use {banned}")

    def test_generator_does_not_import_anything_ai(self):
        """No LLM may touch the data that the product reasons over."""
        source = (REPO_ROOT / "scripts" / "generate_demo_data.py").read_text()
        for banned in ("openai", "anthropic", "app.ai", "langchain"):
            self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)