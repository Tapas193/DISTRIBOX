"""SQLAlchemy ORM models for D6 DISTRIBOX.

The ORM mirrors ``database/schema.sql``. Critically, every financial
derived field on :class:`Booking` is declared with
``Computed(...)`` rather than ``Column(...)``: the values are produced by
PostgreSQL's IMMUTABLE functions, so Python can never accidentally
"helpfully" recompute or overwrite them.

That is the whole point. The database is the financial authority.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Computed, Date, DateTime, ForeignKey,
    Index, Integer, Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Hotel(Base, TimestampMixin):
    __tablename__ = "hotels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    city: Mapped[Optional[str]] = mapped_column(String(100))
    country: Mapped[Optional[str]] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata", nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="INR", nullable=False)
    total_rooms: Mapped[int] = mapped_column(Integer, nullable=False)
    is_demo_data: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    channels: Mapped[list["Channel"]] = relationship(back_populates="hotel", cascade="all, delete-orphan")
    room_types: Mapped[list["RoomType"]] = relationship(back_populates="hotel", cascade="all, delete-orphan")

    __table_args__ = (CheckConstraint("total_rooms > 0", name="hotels_total_rooms_positive"),)


class Channel(Base, TimestampMixin):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(32), nullable=False)
    is_direct: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    hotel: Mapped[Hotel] = relationship(back_populates="channels")
    bookings: Mapped[list["Booking"]] = relationship(back_populates="channel")

    __table_args__ = (
        UniqueConstraint("hotel_id", "code", name="channels_code_unique"),
        CheckConstraint(
            "channel_type IN ('DIRECT','OTA','TRAVEL_AGENT','CORPORATE','WALK_IN','GDS','WHOLESALE')",
            name="channels_type_valid",
        ),
        Index("idx_channels_hotel", "hotel_id"),
    )


class RoomType(Base, TimestampMixin):
    __tablename__ = "room_types"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_rate: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    hotel: Mapped[Hotel] = relationship(back_populates="room_types")

    __table_args__ = (
        UniqueConstraint("hotel_id", "code", name="room_types_code_unique"),
        CheckConstraint("base_rate >= 0", name="room_types_rate_positive"),
    )


class Room(Base, TimestampMixin):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False)
    room_type_id: Mapped[int] = mapped_column(ForeignKey("room_types.id", ondelete="RESTRICT"), nullable=False)
    room_number: Mapped[str] = mapped_column(String(20), nullable=False)
    floor: Mapped[Optional[int]] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("hotel_id", "room_number", name="rooms_number_unique"),
        Index("idx_rooms_hotel", "hotel_id"),
        Index("idx_rooms_type", "room_type_id"),
    )


class Guest(Base, TimestampMixin):
    __tablename__ = "guests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(String(80))
    first_name: Mapped[Optional[str]] = mapped_column(String(120))
    last_name: Mapped[Optional[str]] = mapped_column(String(120))
    country: Mapped[Optional[str]] = mapped_column(String(80))
    guest_status: Mapped[str] = mapped_column(String(32), default="BOOKED", nullable=False)
    first_booking_date: Mapped[Optional[date]] = mapped_column(Date)
    last_booking_date: Mapped[Optional[date]] = mapped_column(Date)
    total_stays: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_nights: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_gross_revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    total_net_revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    has_direct_booking: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_repeat_guest: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    bookings: Mapped[list["Booking"]] = relationship(back_populates="guest")

    __table_args__ = (
        UniqueConstraint("hotel_id", "external_id", name="guests_external_unique"),
        CheckConstraint(
            "guest_status IN ('BOOKED','CANCELLED','CHECKED_IN','CHECKED_OUT','NO_SHOW')",
            name="guests_status_valid",
        ),
        CheckConstraint("total_stays >= 0 AND total_nights >= 0", name="guests_stays_nonnegative"),
        Index("idx_guests_hotel", "hotel_id"),
        Index("idx_guests_repeat", "hotel_id", "is_repeat_guest"),
        Index("idx_guests_country", "hotel_id", "country"),
    )


class ChannelCost(Base, TimestampMixin):
    __tablename__ = "channel_costs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    room_type_id: Mapped[Optional[int]] = mapped_column(ForeignKey("room_types.id", ondelete="CASCADE"))
    commission_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=0, nullable=False)
    payment_fee_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=0, nullable=False)
    discount_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=0, nullable=False)
    other_cost_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=0, nullable=False)
    expected_direct_conversion_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4))
    incremental_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4))
    effective_from: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    effective_to: Mapped[Optional[date]] = mapped_column(Date)

    __table_args__ = (
        UniqueConstraint("channel_id", "room_type_id", "effective_from", name="channel_costs_unique"),
        CheckConstraint("commission_rate >= 0 AND commission_rate <= 1", name="channel_costs_commission_range"),
        CheckConstraint("payment_fee_rate >= 0 AND payment_fee_rate <= 1", name="channel_costs_payment_range"),
        CheckConstraint("discount_rate >= 0 AND discount_rate <= 1", name="channel_costs_discount_range"),
        CheckConstraint("other_cost_rate >= 0 AND other_cost_rate <= 1", name="channel_costs_other_range"),
        Index("idx_channel_costs_lookup", "hotel_id", "channel_id", "effective_from"),
    )


class RatePlan(Base, TimestampMixin):
    __tablename__ = "rate_plans"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False)
    channel_id: Mapped[Optional[int]] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    room_type_id: Mapped[int] = mapped_column(ForeignKey("room_types.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_rate: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    discount_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("hotel_id", "code", name="rate_plans_code_unique"),
        CheckConstraint("base_rate >= 0", name="rate_plans_rate_positive"),
        Index("idx_rate_plans_lookup", "hotel_id", "channel_id", "room_type_id"),
    )


class Booking(Base):
    """A booking. Raw amounts are stored; economics are DB-computed.

    ``distribution_cost``, ``net_room_revenue``, ``gross_adr``, ``net_adr``
    and ``distribution_cost_pct`` are ``Computed`` columns. They are
    read-only in Python by construction - assigning to them is not
    possible, and the values in the table are produced by the same
    IMMUTABLE SQL functions the unit tests mirror.
    """

    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False)
    booking_id: Mapped[str] = mapped_column(String(80), nullable=False)
    guest_id: Mapped[int] = mapped_column(ForeignKey("guests.id", ondelete="RESTRICT"), nullable=False)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="RESTRICT"), nullable=False)
    room_type_id: Mapped[int] = mapped_column(ForeignKey("room_types.id", ondelete="RESTRICT"), nullable=False)

    booking_date: Mapped[date] = mapped_column(Date, nullable=False)
    check_in_date: Mapped[date] = mapped_column(Date, nullable=False)
    check_out_date: Mapped[date] = mapped_column(Date, nullable=False)
    room_nights: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    gross_room_revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    commission_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    payment_fee: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    other_channel_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    # --- database-computed economics: never assigned in Python ---
    distribution_cost: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        Computed(
            "d6_distribution_cost(discount_amount, commission_amount, "
            "payment_fee, other_channel_cost)",
            persisted=True,
        ),
    )
    net_room_revenue: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        Computed(
            "d6_net_revenue(gross_room_revenue, discount_amount, commission_amount, "
            "payment_fee, other_channel_cost)",
            persisted=True,
        ),
    )
    gross_adr: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), Computed("d6_safe_div(gross_room_revenue, room_nights)", persisted=True)
    )
    net_adr: Mapped[Decimal] = mapped_column(
        Numeric(14, 4),
        Computed(
            "d6_net_adr(gross_room_revenue, discount_amount, commission_amount, "
            "payment_fee, other_channel_cost, room_nights)",
            persisted=True,
        ),
    )
    distribution_cost_pct: Mapped[Decimal] = mapped_column(
        Numeric(9, 6),
        Computed(
            "d6_distribution_cost_pct(gross_room_revenue, discount_amount, commission_amount, "
            "payment_fee, other_channel_cost)",
            persisted=True,
        ),
    )

    booking_month: Mapped[date] = mapped_column(
        Date,
        Computed("(booking_date - (EXTRACT(DAY FROM booking_date)::int - 1))::date", persisted=True),
    )
    stay_month: Mapped[date] = mapped_column(
        Date,
        Computed("(check_in_date - (EXTRACT(DAY FROM check_in_date)::int - 1))::date", persisted=True),
    )
    booking_lead_days: Mapped[Optional[int]] = mapped_column(Integer)

    cancellation_status: Mapped[str] = mapped_column(String(32), default="NOT_CANCELLED", nullable=False)
    stay_status: Mapped[str] = mapped_column(String(32), default="CHECKED_OUT", nullable=False)
    repeat_guest: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    guest_country: Mapped[Optional[str]] = mapped_column(String(80))

    data_source: Mapped[str] = mapped_column(String(32), default="api_import", nullable=False)
    is_demo_data: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    guest: Mapped[Guest] = relationship(back_populates="bookings")
    channel: Mapped[Channel] = relationship(back_populates="bookings")

    __table_args__ = (
        UniqueConstraint("hotel_id", "booking_id", name="bookings_source_unique"),
        CheckConstraint("check_out_date > check_in_date", name="bookings_dates_valid"),
        CheckConstraint("room_nights >= 0", name="bookings_nights_valid"),
        CheckConstraint("gross_room_revenue >= 0", name="bookings_gross_valid"),
        CheckConstraint("discount_amount >= 0", name="bookings_discount_valid"),
        CheckConstraint("commission_amount >= 0", name="bookings_commission_valid"),
        CheckConstraint("payment_fee >= 0", name="bookings_payment_valid"),
        CheckConstraint("other_channel_cost >= 0", name="bookings_other_valid"),
        CheckConstraint("net_room_revenue >= 0", name="bookings_net_not_negative"),
        CheckConstraint(
            "cancellation_status IN ('NOT_CANCELLED','CANCELLED')", name="bookings_cancellation_valid"
        ),
        CheckConstraint(
            "stay_status IN ('UPCOMING','IN_HOUSE','CHECKED_OUT','CANCELLED','NO_SHOW')",
            name="bookings_stay_status_valid",
        ),
        Index("idx_bookings_hotel_stay", "hotel_id", "check_in_date"),
        Index("idx_bookings_hotel_book", "hotel_id", "booking_date"),
        Index("idx_bookings_channel", "hotel_id", "channel_id"),
        Index("idx_bookings_guest", "guest_id"),
        Index("idx_bookings_room_type", "room_type_id"),
        Index("idx_bookings_stay_month", "hotel_id", "stay_month"),
        Index("idx_bookings_book_month", "hotel_id", "booking_month"),
        Index("idx_bookings_status", "hotel_id", "cancellation_status", "stay_status"),
        Index("idx_bookings_data_source", "hotel_id", "data_source"),
    )


class Scenario(Base, TimestampMixin):
    __tablename__ = "scenarios"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    objective: Mapped[str] = mapped_column(String(64), default="MAXIMIZE_NET_REVENUE", nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    expected_room_nights: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    average_room_rate: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_saved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    results: Mapped[list["ScenarioResult"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "objective IN ('MAXIMIZE_NET_REVENUE','MINIMIZE_DISTRIBUTION_COST','MAXIMIZE_DIRECT_SHARE')",
            name="scenarios_objective_valid",
        ),
        CheckConstraint("period_end >= period_start", name="scenarios_period_valid"),
        Index("idx_scenarios_hotel", "hotel_id"),
    )


class ScenarioResult(Base):
    __tablename__ = "scenario_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id", ondelete="CASCADE"), nullable=False)
    engine: Mapped[str] = mapped_column(String(64), default="DETERMINISTIC", nullable=False)
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    gross_revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    distribution_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    net_revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    room_nights: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    net_adr: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=0, nullable=False)
    net_revpar: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=0, nullable=False)
    direct_booking_share: Mapped[Decimal] = mapped_column(Numeric(9, 6), default=0, nullable=False)
    estimated_lost_bookings: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    estimated_gained_direct: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    is_feasible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    infeasibility_reason: Mapped[Optional[str]] = mapped_column(Text)
    allocation_detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    scenario: Mapped[Scenario] = relationship(back_populates="results")


class Recommendation(Base, TimestampMixin):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hotel_id: Mapped[int] = mapped_column(ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False)
    scenario_id: Mapped[Optional[int]] = mapped_column(ForeignKey("scenarios.id", ondelete="SET NULL"))
    channel_id: Mapped[Optional[int]] = mapped_column(ForeignKey("channels.id", ondelete="SET NULL"))
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    assumption: Mapped[Optional[str]] = mapped_column(Text)
    expected_impact: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(16), default="MEDIUM", nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(32), default="MODELLED_ESTIMATE", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    is_demo_data: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "category IN ('CHANNEL_MIX','DIRECT_CONVERSION','CANNIBALIZATION','COST_REDUCTION')",
            name="recommendations_category_valid",
        ),
        CheckConstraint("confidence IN ('LOW','MEDIUM','HIGH')", name="recommendations_confidence_valid"),
        CheckConstraint(
            "evidence_type IN ('OBSERVED','MODELLED_ESTIMATE','SCENARIO_ASSUMPTION')",
            name="recommendations_evidence_type_valid",
        ),
        CheckConstraint("priority BETWEEN 0 AND 100", name="recommendations_priority_range"),
        Index("idx_recommendations_hotel", "hotel_id", "priority"),
        Index("idx_recommendations_channel", "channel_id"),
    )