-- =====================================================================
-- D6 DISTRIBOX - PostgreSQL schema
-- Hotel Channel Intelligence & Distribution Cost Optimisation Platform
-- =====================================================================
-- Money is NUMERIC(14,2). Never float. Never application-only.
--
-- Financial columns (distribution_cost, net_room_revenue, gross_adr,
-- net_adr, net_revpar) are GENERATED ALWAYS AS ... STORED columns that
-- call IMMUTABLE functions. This means the database physically enforces
-- the net-revenue identity and cannot drift from the Python engine.
-- =====================================================================

BEGIN;

DROP TABLE IF EXISTS recommendations        CASCADE;
DROP TABLE IF EXISTS scenario_results       CASCADE;
DROP TABLE IF EXISTS scenarios              CASCADE;
DROP TABLE IF EXISTS bookings               CASCADE;
DROP TABLE IF EXISTS channel_costs          CASCADE;
DROP TABLE IF EXISTS rate_plans             CASCADE;
DROP TABLE IF EXISTS channels               CASCADE;
DROP TABLE IF EXISTS rooms                  CASCADE;
DROP TABLE IF EXISTS room_types             CASCADE;
DROP TABLE IF EXISTS guests                 CASCADE;
DROP TABLE IF EXISTS hotels                 CASCADE;

DROP FUNCTION IF EXISTS d6_distribution_cost(NUMERIC, NUMERIC, NUMERIC, NUMERIC) CASCADE;
DROP FUNCTION IF EXISTS d6_net_revenue(NUMERIC, NUMERIC, NUMERIC, NUMERIC, NUMERIC) CASCADE;
DROP FUNCTION IF EXISTS d6_safe_div(NUMERIC, NUMERIC) CASCADE;
DROP FUNCTION IF EXISTS d6_net_adr(NUMERIC, NUMERIC, NUMERIC, NUMERIC, NUMERIC, INTEGER) CASCADE;
DROP FUNCTION IF EXISTS d6_distribution_cost_pct(NUMERIC, NUMERIC, NUMERIC, NUMERIC, NUMERIC) CASCADE;

-- ---------------------------------------------------------------------
-- IMMUTABLE financial functions (single source of truth)
-- ---------------------------------------------------------------------

-- distribution_cost = discount + commission + payment_fee + other
CREATE FUNCTION d6_distribution_cost(
    p_discount NUMERIC,
    p_commission NUMERIC,
    p_payment_fee NUMERIC,
    p_other NUMERIC
) RETURNS NUMERIC
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT round(COALESCE(p_discount, 0)
               + COALESCE(p_commission, 0)
               + COALESCE(p_payment_fee, 0)
               + COALESCE(p_other, 0), 2);
$$;

-- net_room_revenue = gross - distribution_cost
CREATE FUNCTION d6_net_revenue(
    p_gross NUMERIC,
    p_discount NUMERIC,
    p_commission NUMERIC,
    p_payment_fee NUMERIC,
    p_other NUMERIC
) RETURNS NUMERIC
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT round(COALESCE(p_gross, 0)
               - d6_distribution_cost(p_discount, p_commission, p_payment_fee, p_other), 2);
$$;

-- safe division: returns 0 when denominator is 0 or NULL
CREATE FUNCTION d6_safe_div(p_numerator NUMERIC, p_denominator NUMERIC)
RETURNS NUMERIC
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN COALESCE(p_denominator, 0) = 0 THEN 0
        ELSE round(COALESCE(p_numerator, 0) / p_denominator, 4)
    END;
$$;

-- PostgreSQL forbids a generated column from referencing another generated
-- column, so derived-on-derived metrics get their own immutable functions
-- over the *raw* inputs. Same identity, one definition each.
CREATE FUNCTION d6_net_adr(
    p_gross NUMERIC,
    p_discount NUMERIC,
    p_commission NUMERIC,
    p_payment_fee NUMERIC,
    p_other NUMERIC,
    p_nights INTEGER
) RETURNS NUMERIC
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT d6_safe_div(
        d6_net_revenue(p_gross, p_discount, p_commission, p_payment_fee, p_other),
        p_nights
    );
$$;

CREATE FUNCTION d6_distribution_cost_pct(
    p_gross NUMERIC,
    p_discount NUMERIC,
    p_commission NUMERIC,
    p_payment_fee NUMERIC,
    p_other NUMERIC
) RETURNS NUMERIC
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT d6_safe_div(
        d6_distribution_cost(p_discount, p_commission, p_payment_fee, p_other),
        p_gross
    );
$$;

-- ---------------------------------------------------------------------
-- hotels
-- ---------------------------------------------------------------------
CREATE TABLE hotels (
    id                  BIGSERIAL PRIMARY KEY,
    code                TEXT        NOT NULL UNIQUE,
    name                TEXT        NOT NULL,
    city                TEXT,
    country             TEXT,
    timezone            TEXT        NOT NULL DEFAULT 'Asia/Kolkata',
    currency            TEXT        NOT NULL DEFAULT 'INR',
    total_rooms         INTEGER     NOT NULL,
    is_demo_data        BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT hotels_total_rooms_positive CHECK (total_rooms > 0)
);

-- ---------------------------------------------------------------------
-- channels  (configurable - never hard-coded in application logic)
-- ---------------------------------------------------------------------
CREATE TABLE channels (
    id                  BIGSERIAL PRIMARY KEY,
    hotel_id            BIGINT      NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    code                TEXT        NOT NULL,
    name                TEXT        NOT NULL,
    -- DIRECT, OTA, TRAVEL_AGENT, CORPORATE, WALK_IN
    channel_type        TEXT        NOT NULL,
    is_direct           BOOLEAN     NOT NULL DEFAULT FALSE,
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    sort_order          INTEGER     NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT channels_code_unique  UNIQUE (hotel_id, code),
    CONSTRAINT channels_type_valid   CHECK (channel_type IN
        ('DIRECT','OTA','TRAVEL_AGENT','CORPORATE','WALK_IN','GDS','WHOLESALE'))
);

CREATE INDEX idx_channels_hotel ON channels(hotel_id);

-- ---------------------------------------------------------------------
-- room_types / rooms
-- ---------------------------------------------------------------------
CREATE TABLE room_types (
    id                  BIGSERIAL PRIMARY KEY,
    hotel_id            BIGINT      NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    code                TEXT        NOT NULL,
    name                TEXT        NOT NULL,
    base_rate           NUMERIC(14,2) NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT room_types_code_unique UNIQUE (hotel_id, code),
    CONSTRAINT room_types_rate_positive CHECK (base_rate >= 0)
);

CREATE TABLE rooms (
    id                  BIGSERIAL PRIMARY KEY,
    hotel_id            BIGINT      NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    room_type_id        BIGINT      NOT NULL REFERENCES room_types(id) ON DELETE RESTRICT,
    room_number         TEXT        NOT NULL,
    floor               INTEGER,
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT rooms_number_unique UNIQUE (hotel_id, room_number)
);

CREATE INDEX idx_rooms_hotel ON rooms(hotel_id);
CREATE INDEX idx_rooms_type  ON rooms(room_type_id);

-- ---------------------------------------------------------------------
-- guests
-- ---------------------------------------------------------------------
CREATE TABLE guests (
    id                  BIGSERIAL PRIMARY KEY,
    hotel_id            BIGINT      NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    external_id         TEXT,
    first_name          TEXT,
    last_name           TEXT,
    country             TEXT,
    -- BOOKED / CANCELLED / CHECKED_IN / CHECKED_OUT / NO_SHOW
    guest_status        TEXT        NOT NULL DEFAULT 'BOOKED',
    first_booking_date  DATE,
    last_booking_date   DATE,
    total_stays         INTEGER     NOT NULL DEFAULT 0,
    total_nights        INTEGER     NOT NULL DEFAULT 0,
    total_gross_revenue NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_net_revenue   NUMERIC(14,2) NOT NULL DEFAULT 0,
    has_direct_booking  BOOLEAN     NOT NULL DEFAULT FALSE,
    is_repeat_guest     BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT guests_status_valid CHECK (guest_status IN
        ('BOOKED','CANCELLED','CHECKED_IN','CHECKED_OUT','NO_SHOW')),
    CONSTRAINT guests_stays_nonnegative CHECK (total_stays >= 0 AND total_nights >= 0),
    CONSTRAINT guests_external_unique UNIQUE (hotel_id, external_id)
);

CREATE INDEX idx_guests_hotel   ON guests(hotel_id);
CREATE INDEX idx_guests_repeat  ON guests(hotel_id, is_repeat_guest);
CREATE INDEX idx_guests_country ON guests(hotel_id, country);

-- ---------------------------------------------------------------------
-- channel_costs  (per-channel economic configuration, effective-dated)
-- ---------------------------------------------------------------------
CREATE TABLE channel_costs (
    id                  BIGSERIAL PRIMARY KEY,
    hotel_id            BIGINT      NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    channel_id          BIGINT      NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    room_type_id        BIGINT      REFERENCES room_types(id) ON DELETE CASCADE,
    commission_rate     NUMERIC(6,4) NOT NULL DEFAULT 0,
    payment_fee_rate    NUMERIC(6,4) NOT NULL DEFAULT 0,
    discount_rate       NUMERIC(6,4) NOT NULL DEFAULT 0,
    other_cost_rate     NUMERIC(6,4) NOT NULL DEFAULT 0,
    -- planning assumption used by scenario / optimisation models
    expected_direct_conversion_rate NUMERIC(6,4),
    -- modelled probability an incremental booking would have gone elsewhere
    incremental_rate    NUMERIC(6,4),
    effective_from      DATE        NOT NULL DEFAULT CURRENT_DATE,
    effective_to        DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT channel_costs_unique UNIQUE (channel_id, room_type_id, effective_from),
    CONSTRAINT channel_costs_commission_range CHECK (commission_rate   >= 0 AND commission_rate   <= 1),
    CONSTRAINT channel_costs_payment_range    CHECK (payment_fee_rate  >= 0 AND payment_fee_rate  <= 1),
    CONSTRAINT channel_costs_discount_range   CHECK (discount_rate     >= 0 AND discount_rate     <= 1),
    CONSTRAINT channel_costs_other_range      CHECK (other_cost_rate   >= 0 AND other_cost_rate   <= 1),
    CONSTRAINT channel_costs_direct_range     CHECK (expected_direct_conversion_rate IS NULL
                                                 OR (expected_direct_conversion_rate >= 0
                                                 AND expected_direct_conversion_rate <= 1)),
    CONSTRAINT channel_costs_incremental_range CHECK (incremental_rate IS NULL
                                                 OR (incremental_rate >= 0 AND incremental_rate <= 1)),
    CONSTRAINT channel_costs_dates_valid CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE INDEX idx_channel_costs_lookup ON channel_costs(hotel_id, channel_id, effective_from);

-- ---------------------------------------------------------------------
-- rate_plans
-- ---------------------------------------------------------------------
CREATE TABLE rate_plans (
    id                  BIGSERIAL PRIMARY KEY,
    hotel_id            BIGINT      NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    channel_id          BIGINT      REFERENCES channels(id) ON DELETE CASCADE,
    room_type_id        BIGINT      NOT NULL REFERENCES room_types(id) ON DELETE CASCADE,
    code                TEXT        NOT NULL,
    name                TEXT        NOT NULL,
    base_rate           NUMERIC(14,2) NOT NULL,
    discount_rate       NUMERIC(6,4) NOT NULL DEFAULT 0,
    is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT rate_plans_code_unique UNIQUE (hotel_id, code),
    CONSTRAINT rate_plans_rate_positive CHECK (base_rate >= 0),
    CONSTRAINT rate_plans_discount_range CHECK (discount_rate >= 0 AND discount_rate <= 1)
);

CREATE INDEX idx_rate_plans_lookup ON rate_plans(hotel_id, channel_id, room_type_id);

-- ---------------------------------------------------------------------
-- bookings  (raw values preserved; financial outputs are generated)
-- ---------------------------------------------------------------------
CREATE TABLE bookings (
    id                      BIGSERIAL PRIMARY KEY,
    hotel_id                BIGINT      NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    booking_id              TEXT        NOT NULL,
    guest_id                BIGINT      NOT NULL REFERENCES guests(id) ON DELETE RESTRICT,
    channel_id              BIGINT      NOT NULL REFERENCES channels(id) ON DELETE RESTRICT,
    room_type_id            BIGINT      NOT NULL REFERENCES room_types(id) ON DELETE RESTRICT,

    booking_date            DATE        NOT NULL,
    check_in_date           DATE        NOT NULL,
    check_out_date          DATE        NOT NULL,

    room_nights             INTEGER     NOT NULL DEFAULT 0,

    -- ---- raw imported values (never overwritten) ----
    gross_room_revenue      NUMERIC(14,2) NOT NULL,
    discount_amount         NUMERIC(14,2) NOT NULL DEFAULT 0,
    commission_amount       NUMERIC(14,2) NOT NULL DEFAULT 0,
    payment_fee             NUMERIC(14,2) NOT NULL DEFAULT 0,
    other_channel_cost      NUMERIC(14,2) NOT NULL DEFAULT 0,

    -- ---- derived, enforced by the database ----
    distribution_cost       NUMERIC(14,2)
        GENERATED ALWAYS AS (d6_distribution_cost(
            discount_amount, commission_amount, payment_fee, other_channel_cost)) STORED,
    net_room_revenue        NUMERIC(14,2)
        GENERATED ALWAYS AS (d6_net_revenue(
            gross_room_revenue, discount_amount,
            commission_amount, payment_fee, other_channel_cost)) STORED,
    gross_adr               NUMERIC(14,4)
        GENERATED ALWAYS AS (d6_safe_div(gross_room_revenue, room_nights)) STORED,
    net_adr                 NUMERIC(14,4)
        GENERATED ALWAYS AS (d6_net_adr(
            gross_room_revenue, discount_amount,
            commission_amount, payment_fee, other_channel_cost, room_nights)) STORED,
    distribution_cost_pct   NUMERIC(9,6)
        GENERATED ALWAYS AS (d6_distribution_cost_pct(
            gross_room_revenue, discount_amount,
            commission_amount, payment_fee, other_channel_cost)) STORED,

    -- ---- reporting helpers ----
    -- Month-start derived by arithmetic rather than date_trunc, because
    -- date_trunc is only STABLE and generated columns demand IMMUTABLE.
    booking_month           DATE
        GENERATED ALWAYS AS (
            (booking_date - (EXTRACT(DAY FROM booking_date)::int - 1))::date) STORED,
    stay_month              DATE
        GENERATED ALWAYS AS (
            (check_in_date - (EXTRACT(DAY FROM check_in_date)::int - 1))::date) STORED,
    booking_lead_days       INTEGER,

    -- NOT_CANCELLED / CANCELLED
    cancellation_status     TEXT        NOT NULL DEFAULT 'NOT_CANCELLED',
    -- UPCOMING / IN_HOUSE / CHECKED_OUT / CANCELLED / NO_SHOW
    stay_status             TEXT        NOT NULL DEFAULT 'CHECKED_OUT',
    repeat_guest            BOOLEAN     NOT NULL DEFAULT FALSE,
    guest_country           TEXT,

    -- demo_synthetic / csv_import / api_import
    data_source             TEXT        NOT NULL DEFAULT 'api_import',
    is_demo_data            BOOLEAN     NOT NULL DEFAULT FALSE,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT bookings_source_unique UNIQUE (hotel_id, booking_id),
    CONSTRAINT bookings_dates_valid   CHECK (check_out_date > check_in_date),
    CONSTRAINT bookings_nights_valid  CHECK (room_nights >= 0),
    CONSTRAINT bookings_gross_valid   CHECK (gross_room_revenue >= 0),
    CONSTRAINT bookings_discount_valid CHECK (discount_amount >= 0),
    CONSTRAINT bookings_commission_valid CHECK (commission_amount >= 0),
    CONSTRAINT bookings_payment_valid CHECK (payment_fee >= 0),
    CONSTRAINT bookings_other_valid   CHECK (other_channel_cost >= 0),
    -- distribution cost can never exceed gross revenue: a booking cannot
    -- cost the hotel more than it brings in.
    CONSTRAINT bookings_net_not_negative
        CHECK (net_room_revenue >= 0),
    CONSTRAINT bookings_cancellation_valid
        CHECK (cancellation_status IN ('NOT_CANCELLED','CANCELLED')),
    CONSTRAINT bookings_stay_status_valid
        CHECK (stay_status IN ('UPCOMING','IN_HOUSE','CHECKED_OUT','CANCELLED','NO_SHOW'))
);

CREATE INDEX idx_bookings_hotel_stay   ON bookings(hotel_id, check_in_date);
CREATE INDEX idx_bookings_hotel_book   ON bookings(hotel_id, booking_date);
CREATE INDEX idx_bookings_channel      ON bookings(hotel_id, channel_id);
CREATE INDEX idx_bookings_guest        ON bookings(guest_id);
CREATE INDEX idx_bookings_room_type    ON bookings(room_type_id);
CREATE INDEX idx_bookings_stay_month   ON bookings(hotel_id, stay_month);
CREATE INDEX idx_bookings_book_month   ON bookings(hotel_id, booking_month);
CREATE INDEX idx_bookings_status       ON bookings(hotel_id, cancellation_status, stay_status);
CREATE INDEX idx_bookings_data_source  ON bookings(hotel_id, data_source);

-- ---------------------------------------------------------------------
-- scenarios
-- ---------------------------------------------------------------------
CREATE TABLE scenarios (
    id                      BIGSERIAL PRIMARY KEY,
    hotel_id                BIGINT      NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    name                    TEXT        NOT NULL,
    description             TEXT,
    -- MAXIMIZE_NET_REVENUE / MINIMIZE_DISTRIBUTION_COST / MAXIMIZE_DIRECT_SHARE
    objective               TEXT        NOT NULL DEFAULT 'MAXIMIZE_NET_REVENUE',
    period_start            DATE        NOT NULL,
    period_end              DATE        NOT NULL,
    expected_room_nights    NUMERIC(14,2) NOT NULL DEFAULT 0,
    average_room_rate       NUMERIC(14,2) NOT NULL DEFAULT 0,
    -- JSONB payload of target allocations and constraint set
    parameters              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    is_saved                BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT scenarios_objective_valid CHECK (objective IN
        ('MAXIMIZE_NET_REVENUE','MINIMIZE_DISTRIBUTION_COST','MAXIMIZE_DIRECT_SHARE')),
    CONSTRAINT scenarios_period_valid CHECK (period_end >= period_start),
    CONSTRAINT scenarios_params_object CHECK (jsonb_typeof(parameters) = 'object')
);

CREATE INDEX idx_scenarios_hotel ON scenarios(hotel_id, created_at DESC);

-- ---------------------------------------------------------------------
-- scenario_results
-- ---------------------------------------------------------------------
CREATE TABLE scenario_results (
    id                      BIGSERIAL PRIMARY KEY,
    scenario_id             BIGINT      NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    engine                  TEXT        NOT NULL DEFAULT 'DETERMINISTIC',
    is_baseline             BOOLEAN     NOT NULL DEFAULT FALSE,
    gross_revenue           NUMERIC(14,2) NOT NULL DEFAULT 0,
    distribution_cost       NUMERIC(14,2) NOT NULL DEFAULT 0,
    net_revenue             NUMERIC(14,2) NOT NULL DEFAULT 0,
    room_nights             NUMERIC(14,2) NOT NULL DEFAULT 0,
    net_adr                 NUMERIC(14,4) NOT NULL DEFAULT 0,
    net_revpar              NUMERIC(14,4) NOT NULL DEFAULT 0,
    direct_booking_share    NUMERIC(9,6)  NOT NULL DEFAULT 0,
    estimated_lost_bookings NUMERIC(14,2) NOT NULL DEFAULT 0,
    estimated_gained_direct NUMERIC(14,2) NOT NULL DEFAULT 0,
    is_feasible             BOOLEAN     NOT NULL DEFAULT TRUE,
    infeasibility_reason    TEXT,
    -- per-channel allocation + modelled metrics
    allocation_detail       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT scenario_results_allocation_object
        CHECK (jsonb_typeof(allocation_detail) = 'object')
);

CREATE INDEX idx_scenario_results_scenario ON scenario_results(scenario_id);

-- ---------------------------------------------------------------------
-- recommendations
-- ---------------------------------------------------------------------
CREATE TABLE recommendations (
    id                      BIGSERIAL PRIMARY KEY,
    hotel_id                BIGINT      NOT NULL REFERENCES hotels(id) ON DELETE CASCADE,
    scenario_id             BIGINT      REFERENCES scenarios(id) ON DELETE SET NULL,
    channel_id              BIGINT      REFERENCES channels(id) ON DELETE SET NULL,
    -- CHANNEL_MIX / DIRECT_CONVERSION / CANNIBALIZATION / COST_REDUCTION
    category                TEXT        NOT NULL,
    title                   TEXT        NOT NULL,
    evidence                TEXT        NOT NULL,
    assumption              TEXT,
    expected_impact         TEXT,
    -- LOW / MEDIUM / HIGH - how much confidence, never a probability of success
    confidence              TEXT        NOT NULL DEFAULT 'MEDIUM',
    -- OBSERVED / MODELLED_ESTIMATE / SCENARIO_ASSUMPTION
    evidence_type           TEXT        NOT NULL DEFAULT 'MODELLED_ESTIMATE',
    priority                INTEGER     NOT NULL DEFAULT 50,
    is_demo_data            BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT recommendations_category_valid CHECK (category IN
        ('CHANNEL_MIX','DIRECT_CONVERSION','CANNIBALIZATION','COST_REDUCTION')),
    CONSTRAINT recommendations_confidence_valid CHECK (confidence IN ('LOW','MEDIUM','HIGH')),
    CONSTRAINT recommendations_evidence_type_valid CHECK (evidence_type IN
        ('OBSERVED','MODELLED_ESTIMATE','SCENARIO_ASSUMPTION')),
    CONSTRAINT recommendations_priority_range CHECK (priority BETWEEN 0 AND 100)
);

CREATE INDEX idx_recommendations_hotel ON recommendations(hotel_id, priority DESC, created_at DESC);
CREATE INDEX idx_recommendations_channel ON recommendations(channel_id);

COMMIT;