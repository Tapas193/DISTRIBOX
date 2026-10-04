"""Seed D6 DISTRIBOX with the synthetic demo hotel.

    ####################################################################
    #  SYNTHETIC DEMO DATA. Not real hotel data. Every row is flagged  #
    #  is_demo_data = TRUE and data_source = 'demo_synthetic'.         #
    ####################################################################

Usage:
    cd backend
    .venv/bin/python -m scripts.seed_demo --reset

Loads: 1 hotel, 100 rooms, 4 room types, 6 channels, channel cost curves,
1,000 guests, 5,000 bookings across 12 months. Guest lifetime rollups are
recomputed in SQL so the aggregates are derived from the bookings rather
than invented by the generator.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR.parent / "scripts"))

from sqlalchemy import text  # noqa: E402

from app.core.logging_config import configure_logging, get_logger  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402

configure_logging()
logger = get_logger("seed")

from generate_demo_data import generate  # noqa: E402


def reset_database() -> None:
    logger.info("resetting demo tables")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE recommendations, scenario_results, scenarios, bookings,
                         channel_costs, rate_plans, guests, rooms, room_types,
                         channels, hotels
                RESTART IDENTITY CASCADE
                """
            )
        )


def seed(reset: bool = False, bookings: int = 5000, guests: int = 1000) -> dict:
    if reset:
        reset_database()

    logger.info("generating synthetic dataset", extra={"bookings": bookings, "guests": guests})
    data = generate(n_bookings=bookings, n_guests=guests, n_rooms=data_hotel_rooms())

    db = SessionLocal()
    try:
        # ---------------- hotel ----------------
        hotel_id = _insert_returning_id(
            db,
            """
            INSERT INTO hotels (code, name, city, country, timezone, currency,
                                total_rooms, is_demo_data)
            VALUES (:code, :name, :city, :country, :tz, :cur, :rooms, TRUE)
            RETURNING id
            """,
            code=data.hotel["code"], name=data.hotel["name"], city=data.hotel["city"],
            country=data.hotel["country"], tz=data.hotel["timezone"],
            cur=data.hotel["currency"], rooms=data.hotel["total_rooms"],
        )
        logger.info("hotel created", extra={"hotel_id": hotel_id})

        # ---------------- room types ----------------
        room_type_ids: dict[str, int] = {}
        for rt in data.room_types:
            room_type_ids[rt["code"]] = _insert_returning_id(
                db,
                """
                INSERT INTO room_types (hotel_id, code, name, base_rate)
                VALUES (:hid, :code, :name, :rate) RETURNING id
                """,
                hid=hotel_id, code=rt["code"], name=rt["name"], rate=rt["base_rate"],
            )

        # ---------------- rooms ----------------
        # Distribute 100 physical rooms across the 4 types.
        total_rooms = data.hotel["total_rooms"]
        codes = [rt["code"] for rt in data.room_types]
        weights = [rt["occupancy_weight"] for rt in data.room_types]
        assign = _allocate_rooms(total_rooms, weights)
        room_rows = []
        floor = 1
        counter = 1
        for code, count in zip(codes, assign):
            for _ in range(count):
                room_rows.append((hotel_id, room_type_ids[code], f"{counter:03d}", floor))
                counter += 1
                if counter % 20 == 1:
                    floor += 1
        db.execute(
            text(
                "INSERT INTO rooms (hotel_id, room_type_id, room_number, floor) "
                "VALUES (:hid, :rtid, :num, :floor)"
            ),
            [{"hid": h, "rtid": r, "num": n, "floor": f} for h, r, n, f in room_rows],
        )
        logger.info("rooms created", extra={"count": len(room_rows)})

        # ---------------- channels ----------------
        channel_ids: dict[str, int] = {}
        for ch in data.channels:
            channel_ids[ch["code"]] = _insert_returning_id(
                db,
                """
                INSERT INTO channels (hotel_id, code, name, channel_type,
                                      is_direct, is_active, sort_order)
                VALUES (:hid, :code, :name, :ctype, :direct, TRUE, :sort)
                RETURNING id
                """,
                hid=hotel_id, code=ch["code"], name=ch["name"], ctype=ch["channel_type"],
                direct=ch["is_direct"], sort=ch["sort_order"],
            )
        logger.info("channels created", extra={"count": len(channel_ids)})

        # ---------------- channel costs ----------------
        cost_rows = [
            {
                "hid": hotel_id,
                "cid": channel_ids[c["channel_code"]],
                "rtid": room_type_ids[c["room_type_code"]],
                "comm": c["commission_rate"],
                "pay": c["payment_fee_rate"],
                "disc": c["discount_rate"],
                "other": c["other_cost_rate"],
                "conv": c["expected_direct_conversion_rate"],
                "incr": c["incremental_rate"],
            }
            for c in data.channel_costs
        ]
        db.execute(
            text(
                """
                INSERT INTO channel_costs (hotel_id, channel_id, room_type_id,
                    commission_rate, payment_fee_rate, discount_rate, other_cost_rate,
                    expected_direct_conversion_rate, incremental_rate, effective_from)
                VALUES (:hid, :cid, :rtid, :comm, :pay, :disc, :other, :conv, :incr, :eff)
                """
            ),
            [{**r, "eff": data.period_start} for r in cost_rows],
        )
        logger.info("channel costs created", extra={"count": len(cost_rows)})

        # ---------------- guests ----------------
        guest_ids: dict[str, int] = {}
        for g in data.guests:
            gid = _insert_returning_id(
                db,
                """
                INSERT INTO guests (hotel_id, external_id, first_name, last_name, country)
                VALUES (:hid, :eid, :fn, :ln, :country) RETURNING id
                """,
                hid=hotel_id, eid=g.external_id, fn=g.first_name, ln=g.last_name, country=g.country,
            )
            guest_ids[g.external_id] = gid
        logger.info("guests created", extra={"count": len(guest_ids)})

        # ---------------- bookings ----------------
        # Only raw amounts are inserted. distribution_cost, net_room_revenue,
        # gross_adr, net_adr and distribution_cost_pct are GENERATED columns
        # and are computed by PostgreSQL, not by this script.
        booking_rows = [
            {
                "hid": hotel_id,
                "bid": b.booking_id,
                "gid": guest_ids[b.guest_external_id],
                "cid": channel_ids[b.channel_code],
                "rtid": room_type_ids[b.room_type_code],
                "bdate": b.booking_date,
                "cin": b.check_in_date,
                "cout": b.check_out_date,
                "nights": b.room_nights,
                "gross": b.gross_room_revenue,
                "disc": b.discount_amount,
                "comm": b.commission_amount,
                "pay": b.payment_fee,
                "other": b.other_channel_cost,
                "lead": b.booking_lead_days,
                "cxl": b.cancellation_status,
                "stay": b.stay_status,
                "rep": b.repeat_guest,
                "ctry": b.guest_country,
            }
            for b in data.bookings
        ]
        db.execute(
            text(
                """
                INSERT INTO bookings (hotel_id, booking_id, guest_id, channel_id, room_type_id,
                    booking_date, check_in_date, check_out_date, room_nights,
                    gross_room_revenue, discount_amount, commission_amount, payment_fee,
                    other_channel_cost, booking_lead_days, cancellation_status, stay_status,
                    repeat_guest, guest_country, data_source, is_demo_data)
                VALUES (:hid, :bid, :gid, :cid, :rtid, :bdate, :cin, :cout, :nights,
                    :gross, :disc, :comm, :pay, :other, :lead, :cxl, :stay,
                    :rep, :ctry, 'demo_synthetic', TRUE)
                """
            ),
            booking_rows,
        )
        logger.info("bookings created", extra={"count": len(booking_rows)})

        # ---------------- guest rollups (derived, in SQL) ----------------
        # total_stays, total_net_revenue etc. must be *derived from the
        # bookings*, never invented by the generator, or the guest analytics
        # would be measuring fiction.
        db.execute(
            text(
                """
                UPDATE guests g SET
                    total_stays = agg.stays,
                    total_nights = agg.nights,
                    total_gross_revenue = agg.gross,
                    total_net_revenue = agg.net,
                    has_direct_booking = agg.has_direct,
                    is_repeat_guest = agg.stays > 1,
                    first_booking_date = agg.first_booking,
                    last_booking_date = agg.last_booking
                FROM (
                    SELECT b.guest_id,
                           COUNT(*)::int                                   AS stays,
                           COALESCE(SUM(b.room_nights), 0)::int             AS nights,
                           COALESCE(SUM(b.gross_room_revenue), 0)           AS gross,
                           COALESCE(SUM(b.net_room_revenue), 0)            AS net,
                           BOOL_OR(EXISTS (
                               SELECT 1 FROM channels c
                               WHERE c.id = b.channel_id AND c.is_direct
                           ))                                              AS has_direct,
                           MIN(b.booking_date)                              AS first_booking,
                           MAX(b.booking_date)                              AS last_booking
                    FROM bookings b
                    WHERE b.cancellation_status = 'NOT_CANCELLED'
                    GROUP BY b.guest_id
                ) agg
                WHERE g.id = agg.guest_id
                """
            )
        )
        db.commit()

        stats = _verify(db, hotel_id, data.period_start, data.period_end)
        logger.info("seed complete", extra=stats)
        return stats

    except Exception:
        db.rollback()
        logger.exception("seed failed")
        raise
    finally:
        db.close()


def _verify(db, hotel_id: int, start: date, end: date) -> dict:
    """Read the numbers back out of the database.

    These are database-computed values, not Python re-derivations - which
    is precisely what makes them trustworthy.
    """
    row = db.execute(
        text(
            """
            SELECT COUNT(*)::int                              AS bookings,
                   COUNT(DISTINCT guest_id)::int              AS guests,
                   COALESCE(SUM(room_nights), 0)::bigint     AS room_nights,
                   ROUND(COALESCE(SUM(gross_room_revenue), 0), 2)  AS gross,
                   ROUND(COALESCE(SUM(distribution_cost), 0), 2)   AS distribution_cost,
                   ROUND(COALESCE(SUM(net_room_revenue), 0), 2)    AS net
            FROM bookings
            WHERE hotel_id = :hid
              AND cancellation_status = 'NOT_CANCELLED'
              AND check_in_date BETWEEN :start AND :end
            """
        ),
        {"hid": hotel_id, "start": start, "end": end},
    ).mappings().one()

    gross = row["gross"]
    dist = row["distribution_cost"]
    return {
        "bookings": row["bookings"],
        "guests": row["guests"],
        "room_nights": row["room_nights"],
        "gross_revenue": float(gross),
        "distribution_cost": float(dist),
        "net_revenue": float(row["net"]),
        "distribution_cost_pct": round(float(dist / gross * 100), 2) if gross else 0.0,
    }


def _insert_returning_id(db, sql: str, **params) -> int:
    return db.execute(text(sql), params).scalar_one()


def _allocate_rooms(total: int, weights: list[float]) -> list[int]:
    """Split physical rooms across room types, largest-remainder method."""
    raw = [total * w for w in weights]
    base = [int(r) for r in raw]
    remainder = total - sum(base)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - base[i], reverse=True)
    for i in range(remainder):
        base[order[i % len(order)]] += 1
    return base


def data_hotel_rooms() -> int:
    from app.core.config import settings

    return 100


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed D6 DISTRIBOX demo data")
    parser.add_argument("--reset", action="store_true", help="truncate existing data first")
    parser.add_argument("--bookings", type=int, default=5000)
    parser.add_argument("--guests", type=int, default=1000)
    args = parser.parse_args()

    stats = seed(reset=args.reset, bookings=args.bookings, guests=args.guests)

    print("\n" + "=" * 66)
    print("  D6 DISTRIBOX - SYNTHETIC DEMO DATA SEEDED")
    print("  (demo / synthetic data - not real hotel performance)")
    print("=" * 66)
    print(f"  bookings (non-cancelled in window) : {stats['bookings']:,}")
    print(f"  guests                             : {stats['guests']:,}")
    print(f"  room nights                        : {stats['room_nights']:,}")
    print(f"  gross room revenue                 : Rs {stats['gross_revenue']:>16,.2f}")
    print(f"  distribution cost                  : Rs {stats['distribution_cost']:>16,.2f}")
    print(f"  net room revenue                   : Rs {stats['net_revenue']:>16,.2f}")
    print(f"  distribution cost %                : {stats['distribution_cost_pct']:>16.2f}%")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())