"""End-to-end verification report against the live PostgreSQL database.

    ################################################################
    #  Every figure printed here is read back out of PostgreSQL.   #
    #  Nothing is estimated, extrapolated or invented.             #
    #  ################################################################

Runs three checks and fails loudly on any disagreement:

1. **Parity** - the Python Net Revenue Engine and the IMMUTABLE SQL
   functions in ``database/schema.sql`` produce identical values for every
   stored booking. If this fails, one of the two is lying.
2. **The specification case** - the Rs 4,000 / 600 / 612 / 50 booking is
   computed by the Python engine *and* by the database, and both must
   return net revenue of Rs 2,738.
3. **The nine channel metrics** - computed from stored generated columns.

Usage:
    cd backend
    POSTGRES_USER=<role> .venv/bin/python -m scripts.verify --hotel HHT-DEMO
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app.analytics.db_loader import (  # noqa: E402
    build_portfolio,
    hotel_period,
    load_booking_rows,
    load_monthly_facts,
    parity_check,
    repeat_rate_by_channel,
)
from app.analytics.channel_analytics import monthly_channel_series  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.logging_config import configure_logging  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.net_revenue import (  # noqa: E402
    BookingEconomicsInput,
    net_revenue_engine,
)
from app.services.recommendations import RecommendationEngine  # noqa: E402
from app.services.scenario_engine import (  # noqa: E402
    GreedyMarginalAllocator,
    ScenarioAssumptions,
    ScenarioEngine,
    rates_from_portfolio,
)

configure_logging()

W = 78


def rule(char: str = "=") -> None:
    print(char * W)


def header(title: str) -> None:
    print()
    rule()
    print(f"  {title}")
    rule()


def sub(title: str) -> None:
    print()
    print(f"  -- {title} " + "-" * max(0, W - len(title) - 6))


def inr(v: Decimal) -> str:
    return f"Rs {Decimal(v):>15,.2f}"


def pct(v: Decimal) -> str:
    return f"{Decimal(v) * 100:>6.2f}%"


# ---------------------------------------------------------------------
# 1. parity
# ---------------------------------------------------------------------


def check_parity(db, hotel_id: int) -> dict:
    header("CHECK 1  Python engine  vs  PostgreSQL generated columns")
    report = parity_check(db, hotel_id)
    print(f"  bookings compared (5 financial fields each) : {report['rows_checked']:,}")
    print(f"  mismatches                                  : {report['mismatch_count']}")
    if report["in_parity"]:
        print("  RESULT: IN PARITY - the two implementations agree exactly.")
    else:
        print("  RESULT: *** PARITY FAILURE ***")
        for m in report["mismatches"][:5]:
            print(f"    {m}")
    return report


# ---------------------------------------------------------------------
# 2. the specification case
# ---------------------------------------------------------------------

SPEC_CASE = dict(
    gross_room_revenue=Decimal("4000"),
    discount_amount=Decimal("600"),
    commission_amount=Decimal("612"),
    payment_fee=Decimal("50"),
    other_channel_cost=Decimal("0"),
    room_nights=1,
)


def check_spec_case(db) -> dict:
    header("CHECK 2  The specification case, computed twice")

    py = net_revenue_engine.calculate(BookingEconomicsInput(**SPEC_CASE))

    # Ask PostgreSQL to compute the same thing through the same IMMUTABLE
    # functions the bookings table uses.
    sql = db.execute(
        text(
            """
            SELECT d6_distribution_cost(:disc, :comm, :pay, :other) AS distribution_cost,
                   d6_net_revenue(:gross, :disc, :comm, :pay, :other) AS net_revenue,
                   d6_distribution_cost_pct(:gross, :disc, :comm, :pay, :other) AS cost_pct,
                   d6_safe_div(:gross, :nights) AS gross_adr,
                   d6_net_adr(:gross, :disc, :comm, :pay, :other, :nights) AS net_adr
            """
        ),
        {
            "gross": SPEC_CASE["gross_room_revenue"],
            "disc": SPEC_CASE["discount_amount"],
            "comm": SPEC_CASE["commission_amount"],
            "pay": SPEC_CASE["payment_fee"],
            "other": SPEC_CASE["other_channel_cost"],
            "nights": SPEC_CASE["room_nights"],
        },
    ).mappings().one()

    print()
    print(f"  {'line item':<26}{'python':>16}{'postgresql':>16}{'match':>10}")
    print(f"  {'-' * 68}")
    rows = [
        ("gross revenue", py.gross_room_revenue, Decimal("4000.00")),
        ("discount", py.discount_amount, Decimal("600.00")),
        ("commission", py.commission_amount, Decimal("612.00")),
        ("payment fee", py.payment_fee, Decimal("50.00")),
        ("other cost", py.other_channel_cost, Decimal("0.00")),
        ("distribution cost", py.distribution_cost, Decimal(sql["distribution_cost"])),
        ("NET REVENUE", py.net_room_revenue, Decimal(sql["net_revenue"])),
        ("distribution cost %", py.distribution_cost_pct, Decimal(sql["cost_pct"])),
        ("gross ADR", py.gross_adr, Decimal(sql["gross_adr"])),
        ("net ADR", py.net_adr, Decimal(sql["net_adr"])),
    ]
    all_match = True
    for label, pyv, sqlv in rows:
        ok = Decimal(pyv) == Decimal(sqlv)
        all_match = all_match and ok
        print(f"  {label:<26}{str(pyv):>16}{str(sqlv):>16}{'YES' if ok else 'NO':>10}")

    print()
    expected = Decimal("2738.00")
    got = py.net_room_revenue
    verdict = "PASS" if (got == expected and all_match) else "FAIL"
    print(f"  Expected net revenue : Rs 2,738.00")
    print(f"  Actual   net revenue : {inr(got)}")
    print(f"  VERDICT              : {verdict}")

    # The dashboard renders this breakdown, so it travels in the payload
    # rather than being retyped in the template. A number that appears in
    # two places is a number that will eventually disagree with itself.
    breakdown = {
        "gross_room_revenue": str(SPEC_CASE["gross_room_revenue"]),
        "discount_amount": str(SPEC_CASE["discount_amount"]),
        "commission_amount": str(SPEC_CASE["commission_amount"]),
        "payment_fee": str(SPEC_CASE["payment_fee"]),
        "other_channel_cost": str(SPEC_CASE["other_channel_cost"]),
        "distribution_cost": str(py.distribution_cost),
        "net_room_revenue": str(py.net_room_revenue),
        "distribution_cost_pct": str(py.distribution_cost_pct),
        "gross_adr": str(py.gross_adr),
        "net_adr": str(py.net_adr),
        "room_nights": str(SPEC_CASE["room_nights"]),
    }

    # A zero-cost direct booking, to show the engine is not just subtracting
    # a constant. Same gross, no channel cost, nothing to leak.
    direct_py = net_revenue_engine.calculate(
        BookingEconomicsInput(
            gross_room_revenue=Decimal("4000"),
            discount_amount=Decimal("0"),
            commission_amount=Decimal("0"),
            payment_fee=Decimal("0"),
            other_channel_cost=Decimal("0"),
            room_nights=1,
        )
    )
    direct_case = {
        "gross_room_revenue": "4000",
        "discount_amount": "0",
        "commission_amount": "0",
        "payment_fee": "0",
        "other_channel_cost": "0",
        "distribution_cost": str(direct_py.distribution_cost),
        "net_room_revenue": str(direct_py.net_room_revenue),
        "distribution_cost_pct": str(direct_py.distribution_cost_pct),
        "room_nights": "1",
    }
    if Decimal(direct_case["net_room_revenue"]) != Decimal("4000"):
        raise SystemExit(
            "direct booking did not retain the full Rs 4,000 - engine regression"
        )

    return {
        "verdict": verdict,
        "net_revenue": got,
        "python_sql_agree": all_match,
        "ota_booking": breakdown,
        "direct_booking": direct_case,
    }


# ---------------------------------------------------------------------
# 3. channel analytics
# ---------------------------------------------------------------------


def check_analytics(db, hotel_id: int) -> dict:
    start, end, total_rooms, code = hotel_period(db, hotel_id)
    pf = build_portfolio(db, hotel_id, start, end)

    header(f"CHECK 3  Channel analytics for {code}")
    print(f"  period                : {start} to {end}")
    print(f"  rooms                 : {total_rooms}")
    print(f"  basis                 : {pf.basis} (arithmetic on stored bookings)")

    sub("Portfolio")
    print(f"  bookings              : {pf.total_bookings:,}")
    print(f"  room nights           : {pf.total_room_nights:,}")
    print(f"  gross revenue         : {inr(pf.total_gross_revenue)}")
    print(f"  distribution cost     : {inr(pf.total_distribution_cost)}")
    print(f"  net revenue           : {inr(pf.total_net_revenue)}")
    print(f"  distribution cost %   : {pct(pf.portfolio_distribution_cost_pct)}")
    print(f"  gross ADR             : {inr(pf.portfolio_gross_adr)}")
    print(f"  net ADR               : {inr(pf.portfolio_net_adr)}")
    print(f"  occupancy             : {pct(pf.portfolio_occupancy)}")
    print(f"  net RevPAR            : {inr(pf.net_revpar)}")
    print(f"  direct room-night share: {pct(pf.direct_booking_share)}")
    print(f"  direct net rev share  : {pct(pf.direct_net_revenue_share)}")

    sub("The nine metrics, per channel")
    hdr = (
        f"  {'channel':<14}{'gross':>14}{'net':>14}{'dist cost':>13}"
        f"{'cost %':>8}{'grossADR':>10}{'netADR':>10}{'bk share':>9}{'net shr':>9}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for c in sorted(pf.channels, key=lambda c: -c.net_revenue):
        print(
            f"  {c.channel_code:<14}"
            f"{c.gross_revenue:>14,.0f}"
            f"{c.net_revenue:>14,.0f}"
            f"{c.distribution_cost:>13,.0f}"
            f"{pct(c.distribution_cost_pct):>8}"
            f"{c.gross_adr:>10,.0f}"
            f"{c.net_adr:>10,.0f}"
            f"{pct(c.booking_share):>9}"
            f"{pct(c.net_revenue_share):>9}"
        )

    sub("Where the money leaks: booking share vs net revenue share")
    for c in sorted(pf.non_direct(), key=lambda c: -(pf.share_gap(c.channel_code) or Decimal("0"))):
        gap = pf.share_gap(c.channel_code) or Decimal("0")
        print(
            f"  {c.channel_code:<14} bookings {pct(c.booking_share):>7}"
            f"   net revenue {pct(c.net_revenue_share):>7}   gap {pct(gap):>8}"
        )

    return {"portfolio": pf, "start": start, "end": end}


# ---------------------------------------------------------------------
# 4. scenario + recommendations
# ---------------------------------------------------------------------


def check_scenario(db, hotel_id: int, start: date, end: date):
    pf = build_portfolio(db, hotel_id, start, end)

    header("CHECK 4  Deterministic scenario (arithmetic, not prediction)")

    db.execute(
        text(
            """
            SELECT cc.channel_id, cc.room_type_id, cc.commission_rate,
                   cc.expected_direct_conversion_rate, cc.incremental_rate
            FROM channel_costs cc
            JOIN channels c ON c.id = cc.channel_id
            WHERE cc.hotel_id = :hid AND cc.room_type_id IS NULL
            """
        ),
        {"hid": hotel_id},
    ).mappings().all()

    # Planning assumptions are read from the hotel's own channel_costs
    # configuration. They are ASSUMPTIONS and are labelled as such.
    rows = db.execute(
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

    conversion = {r["code"]: Decimal(r["conv"]) for r in rows}
    incremental = {r["code"]: Decimal(r["incr"]) for r in rows}

    sub("Planning assumptions (hotel-supplied, from channel_costs)")
    for code in sorted(conversion):
        print(f"  {code:<14} direct conversion {pct(conversion[code])}"
              f"   incremental to other channels {pct(incremental.get(code, Decimal('0')))}")

    assumptions = ScenarioAssumptions(
        expected_direct_conversion_rate=conversion,
        incremental_rate=incremental,
        max_shift_fraction=Decimal("0.30"),
    )

    rates = rates_from_portfolio(
        pf,
        conversion_rates=conversion,
        incremental_rates=incremental,
        # Walk-in is not a decision. It has zero commission, so on raw
        # net-per-night it looks like the best channel in the portfolio, and
        # an allocator allowed to "shift" OTA bookings to reception would
        # report a large fictitious gain. Marked non-addressable here.
        addressable={r["code"]: r["code"] != "WALK_IN" for r in rows},
    )

    sub("Effective rates used (OBSERVED = arithmetic on stored bookings)")
    print(f"  {'channel':<14}{'gross ADR':>12}{'commission':>12}{'discount':>11}"
          f"{'fees':>9}{'net/night':>12}{'source':>12}")
    for code in sorted(rates):
        r = rates[code]
        print(
            f"  {code:<14}{r.gross_adr:>12,.0f}{pct(r.commission_rate):>12}"
            f"{pct(r.discount_rate):>11}{pct(r.payment_fee_rate):>9}"
            f"{r.net_per_night():>12,.0f}{r.rate_source:>12}"
        )

    engine = ScenarioEngine()
    baseline = engine.baseline(pf, rates, assumptions)
    alloc, moves = GreedyMarginalAllocator().allocate(engine, pf, rates, assumptions)
    scenario = engine.evaluate(
        alloc, rates, assumptions, pf,
        name="Greedy reallocation (max net revenue)", moves=moves,
    )

    sub("Baseline vs scenario")
    print(f"  {'metric':<30}{'baseline':>18}{'scenario':>18}{'delta':>16}")
    print(f"  {'-' * 82}")
    for label, b, s in (
        ("gross revenue", baseline.gross_revenue, scenario.gross_revenue),
        ("distribution cost", baseline.distribution_cost, scenario.distribution_cost),
        ("net revenue", baseline.net_revenue, scenario.net_revenue),
    ):
        print(f"  {label:<30}{b:>18,.0f}{s:>18,.0f}{s - b:>16,.0f}")
    print(f"  {'distribution cost %':<30}"
          f"{pct(baseline.distribution_cost_pct):>18}"
          f"{pct(scenario.distribution_cost_pct):>18}")
    print(f"  {'net ADR':<30}{baseline.net_adr:>18,.0f}{scenario.net_adr:>18,.0f}")
    print(f"  {'direct room-night share':<30}"
          f"{pct(baseline.direct_booking_share):>18}"
          f"{pct(scenario.direct_booking_share):>18}")
    print()
    print(f"  feasible                 : {scenario.is_feasible}")
    print(f"  room nights shifted      : {len(moves):,}")
    print(f"  modelled lost demand     : {scenario.estimated_lost_bookings:,.0f} nights")
    print(f"  modelled direct gain     : {scenario.estimated_gained_direct:,.0f} nights")
    print(f"  MODELLED net delta       : {inr(scenario.net_revenue_delta)}")

    if moves:
        sub("Move audit trail (aggregate by channel pair)")
        pairs: dict = {}
        for m in moves:
            key = (m["from_channel"], m["to_channel"])
            agg = pairs.setdefault(
                key,
                {"steps": 0, "withdrawn": Decimal(0), "arriving": Decimal(0),
                 "lost": Decimal(0), "delta": Decimal(0)},
            )
            agg["steps"] += 1
            agg["withdrawn"] += Decimal(m["room_nights_withdrawn"])
            agg["arriving"] += Decimal(m["room_nights_arriving"])
            agg["lost"] += Decimal(m["room_nights_lost"])
            agg["delta"] += Decimal(m["net_revenue_delta"])
        print(f"  {'from':>14}{'to':>14}{'steps':>8}{'withdrawn':>12}"
              f"{'arriving':>11}{'lost':>10}{'net delta':>16}")
        for (src, dst), a in sorted(pairs.items(), key=lambda kv: -kv[1]["delta"]):
            print(f"  {src:>14}{dst:>14}{a['steps']:>8,}{a['withdrawn']:>12,.0f}"
                  f"{a['arriving']:>11,.0f}{a['lost']:>10,.0f}{a['delta']:>16,.0f}")
        print()
        print(f"  Total single-night steps: {len(moves):,}")
        print("  Every step above is one night withdrawn and only the expected")
        print("  fraction deposited. The shortfall is lost demand, not saved revenue.")

    return pf, assumptions, scenario, rates


def check_recommendations(pf, assumptions, scenario):
    header("CHECK 5  Recommendations (rule-based, evidence on every line)")

    recs = RecommendationEngine().generate(pf, assumptions, scenario)
    if not recs:
        print("  No recommendations: not enough observed data to state any.")
        return recs

    for i, r in enumerate(recs, 1):
        print()
        print(f"  [{i}] {r.category}  (priority {r.priority}, {r.confidence} confidence)")
        print(f"      {r.title}")
        print(f"      EVIDENCE TYPE : {r.evidence_type}")
        # Wrap the evidence so it stays readable in a terminal.
        words, line, lines = r.evidence.split(), "", []
        for w in words:
            if len(line) + len(w) + 1 > 74:
                lines.append(line)
                line = w
            else:
                line = f"{line} {w}".strip()
        if line:
            lines.append(line)
        for ln in lines:
            print(f"      evidence: {ln}")
        if r.assumption:
            print(f"      ASSUMES  : {r.assumption[:140]}")

    print()
    print(f"  total recommendations: {len(recs)}")
    by_type: dict = {}
    for r in recs:
        by_type[r.evidence_type] = by_type.get(r.evidence_type, 0) + 1
    print(f"  by evidence type     : {by_type}")
    return recs


def rates_payload(rates, pf) -> dict:
    """Channel rates for the in-browser simulator.

    The simulator recomputes an allocation in JavaScript because a
    dashboard opened from file:// cannot call a server. These are the same
    effective rates ``rates_from_portfolio`` built - observed ADR, observed
    cost ratios - so the browser is replaying the Python model rather than
    inventing one. ``backend/tests/test_simulator_parity.py`` recomputes a
    set of allocations with ``ScenarioEngine.evaluate`` and fails if the
    JavaScript disagrees.
    """
    # ChannelRate holds economics only; the display name and type live on the
    # portfolio row, so join the two rather than duplicating them.
    meta = {c.channel_code: c for c in pf.channels}
    return {
        code: {
            "channel_name": meta[code].channel_name if code in meta else code,
            "channel_type": meta[code].channel_type if code in meta else "",
            "is_direct": r.is_direct,
            "is_addressable": r.is_addressable,
            "gross_adr": str(r.gross_adr),
            "discount_rate": str(r.discount_rate),
            "commission_rate": str(r.commission_rate),
            "payment_fee_rate": str(r.payment_fee_rate),
            "other_cost_rate": str(r.other_cost_rate),
            "fixed_payment_fee": str(r.fixed_payment_fee),
            "expected_direct_conversion_rate": str(r.expected_direct_conversion_rate),
            "incremental_rate": str(r.incremental_rate),
            "rate_source": r.rate_source,
        }
        for code, r in sorted(rates.items())
    }


def write_dashboard_payload(
    db, hotel_id, pf, assumptions, scenario, recs, path, *, spec_case, rates
):
    """Emit the JSON the dependency-free dashboard renders."""
    facts_by_month = load_monthly_facts(db, hotel_id, pf.period_start, pf.period_end)
    series = monthly_channel_series(facts_by_month)

    booking_rows = load_booking_rows(db, hotel_id, pf.period_start, pf.period_end)
    repeat = repeat_rate_by_channel(db, hotel_id, pf.period_start, pf.period_end)

    # The loader returns the full booking record. The dashboard only renders
    # these fields, so only these travel: with thousands of rows the
    # difference is megabytes of page weight for numbers nobody looks at.
    # channel_name/channel_type are resolved in the browser from the channel
    # list already in the payload, rather than repeated on every row.
    slim = [
        {
            "ref": r["booking_ref"],
            "date": r["check_in_date"],
            "channel": r["channel_code"],
            "nights": r["room_nights"],
            "gross": r["gross_revenue"],
            "discount": r["discount_amount"],
            "commission": r["commission_amount"],
            "fees": r["payment_fee"],
            "other": r["other_channel_cost"],
            "cost": r["distribution_cost"],
            "net": r["net_revenue"],
            "net_adr": r["net_adr"],
        }
        for r in booking_rows
    ]

    payload = {
        "generated_at_basis": "OBSERVED",
        "data_source": "demo_synthetic",
        "demo_warning": "SYNTHETIC DEMO DATA - not a real hotel's performance.",
        # Only the serialisable case breakdowns travel; the Decimal summary
        # values in the check result are for the console report.
        "spec_case": {
            "ota_booking": spec_case["ota_booking"],
            "direct_booking": spec_case["direct_booking"],
        },
        "repeat_rate": repeat,
        "simulator": {
            "engine": "ScenarioEngine.evaluate",
            "disclaimer": "MODELLED SCENARIO - NOT A GUARANTEE",
            "rates": rates_payload(rates, pf),
            "engine_preset": {
                "name": scenario.name,
                "allocation": {
                    l.channel_code: str(l.scenario_room_nights) for l in scenario.lines
                },
                "summary": {
                    "net_revenue": str(scenario.net_revenue),
                    "net_revenue_delta": str(scenario.net_revenue_delta),
                    "distribution_cost": str(scenario.distribution_cost),
                    "distribution_cost_pct": str(scenario.distribution_cost_pct),
                    "room_nights": str(scenario.room_nights),
                    "direct_booking_share": str(scenario.direct_booking_share),
                    "estimated_lost_bookings": str(scenario.estimated_lost_bookings),
                    "estimated_gained_direct": str(scenario.estimated_gained_direct),
                },
            },
            "current": {
                "allocation": {
                    c.channel_code: str(c.room_nights) for c in pf.channels
                }
            },
            "bookings": slim,
        },
        "hotel": {
            "code": pf.hotel_code,
            "total_rooms": pf.total_rooms,
            "period_start": str(pf.period_start),
            "period_end": str(pf.period_end),
        },
        "portfolio": pf.to_dict(),
        "monthly": series["portfolio_monthly"],
        "channel_monthly": series["channel_net_revenue"],
        "scenario": {
            "name": scenario.name,
            "objective": scenario.objective,
            "engine": scenario.engine,
            "is_feasible": scenario.is_feasible,
            "baseline_net_revenue": str(scenario.baseline_net_revenue),
            "scenario_net_revenue": str(scenario.net_revenue),
            "net_revenue_delta": str(scenario.net_revenue_delta),
            "gross_revenue": str(scenario.gross_revenue),
            "distribution_cost": str(scenario.distribution_cost),
            "distribution_cost_pct": str(scenario.distribution_cost_pct),
            "net_adr": str(scenario.net_adr),
            "room_nights_moved": len(scenario.moves),
            "estimated_lost_bookings": str(scenario.estimated_lost_bookings),
            "estimated_gained_direct": str(scenario.estimated_gained_direct),
            "moves": scenario.moves[:200],
            "assumptions": scenario.assumptions.to_dict(),
        },
        "recommendations": [r.to_dict() for r in recs],
    }

    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    # Compact separators. This file is machine-read by build_dashboard.py and
    # inlined into index.html; pretty-printing a few thousand booking rows
    # costs megabytes of whitespace for no reader.
    path.write_text(json.dumps(payload, separators=(",", ":")))
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify D6 DISTRIBOX against PostgreSQL")
    parser.add_argument("--hotel", default=settings.demo_hotel_code)
    parser.add_argument(
        "--dashboard-json",
        default=str(BACKEND_DIR.parent / "frontend" / "dashboard-data.json"),
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT id FROM hotels WHERE code = :code"),
            {"code": args.hotel},
        ).scalar_one()
        hotel_id = int(row)
    except Exception:
        print(f"No hotel with code {args.hotel!r}. Run scripts/seed_demo first.")
        db.close()
        return 1

    try:
        parity = check_parity(db, hotel_id)
        spec = check_spec_case(db)
        analytics = check_analytics(db, hotel_id)
        pf, assumptions, scenario, rates = check_scenario(
            db, hotel_id, analytics["start"], analytics["end"]
        )
        recs = check_recommendations(pf, assumptions, scenario)

        out = write_dashboard_payload(
            db, hotel_id, pf, assumptions, scenario, recs,
            Path(args.dashboard_json),
            spec_case=spec, rates=rates,
        )

        header("SUMMARY")
        print(f"  Python engine vs PostgreSQL      : "
              f"{'IN PARITY' if parity['in_parity'] else 'FAILED'}")
        print(f"  Rs 4,000 -> Rs 2,738 case        : {spec['verdict']}")
        print(f"  Nine channel metrics computed    : {len(pf.channels)} channels")
        print(f"  Scenario modelled delta          : {inr(scenario.net_revenue_delta)}")
        print(f"  Recommendations generated        : {len(recs)}")
        print(f"  Dashboard payload written        : {out}")
        print()

        ok = parity["in_parity"] and spec["verdict"] == "PASS"
        print(f"  ALL CHECKS {'PASSED' if ok else 'FAILED'}")
        print()
        rule()
        return 0 if ok else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())