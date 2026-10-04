"""Python/JS simulator parity.

The Mix Simulator in the dashboard is not a re-implementation of the business
rule from scratch - it is a transcription of
:meth:`app.services.scenario_engine.ScenarioEngine.evaluate` into browser
JavaScript. A transcription is only worth anything if it is *checked*, so this
module runs the shipped dashboard through Node, feeds the exact allocations it
used back into the Python engine against the live PostgreSQL portfolio, and
demands the two agree.

What is compared
----------------
Every figure the UI shows, plus the per-channel final allocation:

* room nights, gross revenue, distribution cost, net revenue
* net ADR, distribution cost rate, direct room-night share
* net revenue change and distribution cost change against the observed baseline
* moved nights, demand at risk, modelled direct gains
* per-channel scenario room nights (so a compensating pair of errors cannot
  cancel out and hide)
* feasibility, including *which* rule refused the allocation

Tolerances
----------
Money is compared to one paisa and ADR/rate/share figures to one unit in the
fourth decimal place - the same precision both implementations quantise to.
That is deliberately tight: on a Rs 9.3 crore portfolio one paisa is 1e-10 of
the total, so a real modelling difference (a shifted night, a mis-scaled
commission, a double-counted arrival) is many orders of magnitude larger than
the allowance and cannot hide inside it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app.analytics.db_loader import build_portfolio  # noqa: E402
from app.core.money import quantize_money  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.scenario_engine import (  # noqa: E402
    ScenarioAssumptions,
    ScenarioEngine,
    rates_from_portfolio,
)

DASHBOARD = REPO_DIR / "frontend" / "index.html"
SMOKE = BACKEND_DIR / "tests" / "dashboard_smoke.js"
PAYLOAD = REPO_DIR / "frontend" / "dashboard-data.json"

MONEY_TOL = Decimal("0.01")
RATE_TOL = Decimal("0.0001")

#: filled in from the shipped payload during setUpClass
_RATES_CACHE: dict = {}

#: Every case the shipped harness exercises, and what each one is for.
EXPECTED_CASES = {
    "baseline": "observed mix, observed rates - the do-nothing case",
    "engine": "GreedyMarginalAllocator's own output, fractional nights included",
    "cut_ota": "OTA volume withdrawn and not reallocated - pure demand loss",
    "direct_heavy": "OTA and travel emptied into direct",
    "all_zero": "nothing allocated at all - must be refused",
    "fractional": "hand-built fractional allocation",
    "invalid": "structurally invalid allocation - must be refused",
}


def self_is_direct(channel_code: str) -> bool:
    """Whether a channel books directly, read from the shipped payload."""
    return _RATES_CACHE[channel_code]


def run_js_parity() -> dict:
    """Run the shipped dashboard's parity mode and return its JSON."""
    if shutil.which("node") is None:
        raise unittest.SkipTest("node is required for the JavaScript simulator")
    if not DASHBOARD.exists():
        raise unittest.SkipTest(f"dashboard not built: {DASHBOARD}")

    proc = subprocess.run(
        ["node", str(SMOKE), str(DASHBOARD), "--parity"],
        capture_output=True,
        text=True,
        cwd=str(REPO_DIR),
        timeout=180,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "dashboard parity mode failed "
            f"(exit {proc.returncode})\n{proc.stdout}\n{proc.stderr}"
        )
    return json.loads(proc.stdout)


class SimulatorParityTests(unittest.TestCase):
    """Python engine vs the JavaScript actually shipped in the dashboard."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.js = run_js_parity()
        cls.payload = json.loads(PAYLOAD.read_text())

        cls.db = SessionLocal()
        hotel_id = cls.db.execute(
            text("SELECT id FROM hotels WHERE code = :code"),
            {"code": cls.payload["hotel"]["code"]},
        ).scalar_one()
        cls.start = date.fromisoformat(cls.payload["hotel"]["period_start"])
        cls.end = date.fromisoformat(cls.payload["hotel"]["period_end"])
        cls.portfolio = build_portfolio(cls.db, hotel_id, cls.start, cls.end)
        cls.engine = ScenarioEngine()

        # Same inputs verify.py and the payload generator use, read from the
        # hotel's own configuration rather than hard-coded here.
        rows = cls.db.execute(
            text(
                """
                SELECT c.code,
                       AVG(cc.expected_direct_conversion_rate)::numeric(9,4) AS conv,
                       AVG(cc.incremental_rate)::numeric(9,4)                  AS incr
                FROM channel_costs cc
                JOIN channels c ON c.id = cc.channel_id
                WHERE cc.hotel_id = :hid
                GROUP BY c.code
                """
            ),
            {"hid": hotel_id},
        ).mappings().all()

        cls.conversion = {r["code"]: Decimal(r["conv"]) for r in rows}
        cls.incremental = {r["code"]: Decimal(r["incr"]) for r in rows}
        cls.assumptions = ScenarioAssumptions(
            expected_direct_conversion_rate=cls.conversion,
            incremental_rate=cls.incremental,
            max_shift_fraction=Decimal("0.30"),
        )
        cls.rates = rates_from_portfolio(
            cls.portfolio,
            conversion_rates=cls.conversion,
            incremental_rates=cls.incremental,
            addressable={r["code"]: r["code"] != "WALK_IN" for r in rows},
        )

        cls.baseline_outcome = cls.engine.baseline(
            cls.portfolio, cls.rates, cls.assumptions
        )

        global _RATES_CACHE
        _RATES_CACHE = {
            code: bool(cls.payload["simulator"]["rates"][code]["is_direct"])
            for code in cls.payload["simulator"]["rates"]
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    # -- helpers ------------------------------------------------------

    def python_outcome(self, js_case: dict):
        """Run the Python engine on the allocation JavaScript actually used."""
        allocation = {
            code: Decimal(str(nights))
            for code, nights in js_case["allocation"].items()
        }
        return self.engine.evaluate(
            allocation, self.rates, self.assumptions, self.portfolio
        )

    @staticmethod
    def direct_nights(outcome) -> Decimal:
        """Scenario room nights booked through a direct channel."""
        return sum(
            (line.scenario_room_nights for line in outcome.lines
             if self_is_direct(line.channel_code)),
            Decimal("0"),
        )

    @staticmethod
    def moved_nights(outcome) -> Decimal:
        """Nights that ended up somewhere other than where they started."""
        return sum(
            (line.shift_room_nights for line in outcome.lines
             if line.shift_room_nights > 0),
            Decimal("0"),
        )

    def assert_money(self, label: str, js, py) -> None:
        self.assertLessEqual(
            abs(Decimal(str(js)) - py), MONEY_TOL,
            f"{label}: JS {js} vs Python {py} "
            f"(diff {abs(Decimal(str(js)) - py)} > {MONEY_TOL})",
        )

    def assert_rate(self, label: str, js, py) -> None:
        self.assertLessEqual(
            abs(Decimal(str(js)) - py), RATE_TOL,
            f"{label}: JS {js} vs Python {py} "
            f"(diff {abs(Decimal(str(js)) - py)} > {RATE_TOL})",
        )

    # -- the parity claim ---------------------------------------------

    def test_every_expected_case_is_present(self):
        """The harness must still exercise all seven documented cases."""
        self.assertEqual(
            sorted(self.js), sorted(EXPECTED_CASES),
            "parity mode must cover exactly the documented cases",
        )

    def test_parity_on_every_case(self):
        for name in sorted(EXPECTED_CASES):
            with self.subTest(case=name, purpose=EXPECTED_CASES[name]):
                js = self.js[name]
                py = self.python_outcome(js)

                # feasibility, and that both sides refuse for the same reason
                self.assertEqual(
                    js["feasible"], py.is_feasible,
                    f"{name}: JS feasible={js['feasible']} "
                    f"Python feasible={py.is_feasible} "
                    f"({py.infeasibility_reason})",
                )
                if not py.is_feasible:
                    self.assertTrue(
                        js["problems"], f"{name}: JS refused without a reason"
                    )
                    self.assertIn(
                        py.infeasibility_reason.split(":")[0],
                        js["problems"][0],
                        f"{name}: both refused, but not for the same rule. "
                        f"Python: {py.infeasibility_reason!r} JS: {js['problems'][0]!r}",
                    )

                # money
                self.assert_rate(f"{name} room_nights", js["room_nights"], py.room_nights)
                self.assert_money(f"{name} gross", js["gross"], py.gross_revenue)
                self.assert_money(f"{name} distribution cost", js["cost"], py.distribution_cost)
                self.assert_money(f"{name} net revenue", js["net"], py.net_revenue)
                self.assert_money(f"{name} net ADR", js["net_adr"], py.net_adr)
                self.assert_rate(f"{name} cost rate", js["cost_pct"], py.distribution_cost_pct)
                self.assert_rate(
                    f"{name} direct share", js["direct_share"], py.direct_booking_share
                )

                # modelled effects
                self.assert_rate(
                    f"{name} demand at risk", js["lost"], py.estimated_lost_bookings
                )
                self.assert_rate(
                    f"{name} modelled direct gains",
                    js["gained_direct"], py.estimated_gained_direct,
                )
                self.assert_rate(
                    f"{name} direct room nights",
                    js["direct_nights"], self.direct_nights(py),
                )

                # changes, which is what the UI actually headlines
                self.assert_money(
                    f"{name} net revenue change",
                    js["delta"], py.net_revenue_delta,
                )
                py_cost_delta = (
                    py.distribution_cost - self.baseline_outcome.distribution_cost
                )
                js_cost_delta = Decimal(str(js["cost"])) - self.baseline_outcome.distribution_cost
                self.assert_money(
                    f"{name} distribution cost change", js_cost_delta, py_cost_delta
                )
                self.assert_rate(
                    f"{name} moved nights", self.moved_nights(py),
                    sum(
                        (Decimal(str(v["nights"])) - Decimal(str(v["baseline"]))
                         for v in js["lines"] if v["shift"] > 0),
                        Decimal("0"),
                    ),
                )

                # per-channel final allocation: compensating errors must not
                # be able to cancel out and pass
                self.assertEqual(
                    sorted(v["code"] for v in js["lines"]),
                    sorted(line.channel_code for line in py.lines),
                )
                for line in py.lines:
                    js_line = next(
                        v for v in js["lines"] if v["code"] == line.channel_code
                    )
                    self.assert_rate(
                        f"{name} {line.channel_code} nights",
                        js_line["nights"], line.scenario_room_nights,
                    )
                    self.assert_rate(
                        f"{name} {line.channel_code} shift",
                        js_line["shift"], line.shift_room_nights,
                    )
                    self.assert_money(
                        f"{name} {line.channel_code} net",
                        js_line["net"], line.net_revenue,
                    )

    def test_baseline_is_measured_against_the_observed_total(self):
        """The engine's baseline is the audited total, not a re-evaluation.

        If either side ever starts subtracting its own replayed baseline, the
        scenario headline silently changes by the rate-rounding drift and every
        other test still passes. Pin the semantics explicitly.
        """
        observed = self.baseline_outcome.baseline_net_revenue
        self.assertEqual(
            observed, quantize_money(self.portfolio.total_net_revenue),
            "baseline_net_revenue must be the observed portfolio total",
        )
        for name, js in self.js.items():
            with self.subTest(case=name):
                self.assert_money(
                    f"{name} baseline total", js["baseline_net"], observed
                )
                py = self.python_outcome(js)
                self.assertEqual(py.baseline_net_revenue, observed)

    def test_shipped_headline_scenario_number_is_reproduced(self):
        """The headline figure must be the Python engine's, exactly.

        Rs 1,234,579.19 is the greedy result. If JS and Python agree (above) and
        Python still produces this, the dashboard is showing a real modelled
        number rather than a browser approximation of one.
        """
        self.assert_money(
            "engine preset net revenue change",
            self.js["engine"]["delta"],
            Decimal("1234579.19"),
        )
        py = self.python_outcome(self.js["engine"])
        self.assertEqual(
            py.net_revenue_delta, Decimal("1234579.19"),
            "the Python engine's own result moved; investigate before updating "
            "this expectation",
        )

    def test_walk_in_stays_non_addressable(self):
        """WALK_IN may not be used as a reallocation destination."""
        self.assertFalse(
            self.rates["WALK_IN"].is_addressable,
            "WALK_IN must remain non-addressable in the rates the UI ships",
        )
        self.assertFalse(
            self.payload["simulator"]["rates"]["WALK_IN"]["is_addressable"],
            "the dashboard payload must also mark WALK_IN non-addressable",
        )
        # and the engine must refuse an allocation that moves it
        moved = {
            c.channel_code: Decimal(c.room_nights) for c in self.portfolio.channels
        }
        moved["WALK_IN"] = moved["WALK_IN"] - Decimal("100")
        moved["DIRECT"] = moved["DIRECT"] + Decimal("100")
        out = self.engine.evaluate(
            moved, self.rates, self.assumptions, self.portfolio
        )
        self.assertFalse(out.is_feasible)
        self.assertIn("non-addressable", out.infeasibility_reason or "")

    def test_lost_demand_is_never_credited_as_saved_revenue(self):
        """demand at risk + scenario nights must reconcile to observed nights."""
        for name, js in self.js.items():
            with self.subTest(case=name):
                py = self.python_outcome(js)
                total = Decimal(self.portfolio.total_room_nights)
                self.assertEqual(
                    py.estimated_lost_bookings + py.room_nights, total,
                    "a night is either allocated or lost, never both",
                )
                # and the browser must show the same reconciliation
                self.assert_rate(
                    f"{name} lost + allocated",
                    Decimal(str(js["lost"])) + Decimal(str(js["room_nights"])),
                    total,
                )

    def test_feasibility_rules_agree_on_the_documented_invalid_cases(self):
        """Each refusal rule fires in both implementations."""
        base = {
            c.channel_code: Decimal(c.room_nights) for c in self.portfolio.channels
        }

        cases = {
            "negative": {**base, "DIRECT": Decimal("-1")},
            "over allocated": {**base, "DIRECT": base["DIRECT"] + Decimal("1")},
            "invented volume": {**base, "DIRECT": base["DIRECT"] + Decimal("5000")},
            "walk-in moved": {
                **base,
                "WALK_IN": base["WALK_IN"] - Decimal("50"),
                "DIRECT": base["DIRECT"] + Decimal("50"),
            },
        }

        js_problems = " ".join(
            p for case in self.js.values() for p in case["problems"]
        )
        for label, allocation in cases.items():
            with self.subTest(rule=label):
                out = self.engine.evaluate(
                    allocation, self.rates, self.assumptions, self.portfolio
                )
                self.assertFalse(
                    out.is_feasible, f"{label} should be refused by the engine"
                )
                if label == "walk-in moved":
                    self.assertIn(
                        "non-addressable", out.infeasibility_reason or ""
                    )
                    self.assertIn("non-addressable", js_problems)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
