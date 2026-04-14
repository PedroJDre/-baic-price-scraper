-- ============================================================
-- Run this once in Supabase SQL Editor
-- ============================================================

-- Table: one row per scraping run per brand
CREATE TABLE IF NOT EXISTS runs (
    id          BIGSERIAL PRIMARY KEY,
    run_date    DATE        NOT NULL,
    brand       TEXT        NOT NULL,
    total       INTEGER     NOT NULL DEFAULT 0,
    avg_price   BIGINT,
    min_price   BIGINT,
    max_price   BIGINT,
    n_up        INTEGER     NOT NULL DEFAULT 0,
    n_down      INTEGER     NOT NULL DEFAULT 0,
    n_new       INTEGER     NOT NULL DEFAULT 0,
    n_same      INTEGER     NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_date, brand)
);

-- Table: one row per listing per run
CREATE TABLE IF NOT EXISTS listings (
    id           BIGSERIAL PRIMARY KEY,
    run_date     DATE        NOT NULL,
    brand        TEXT        NOT NULL,
    model        TEXT        NOT NULL,
    variant      TEXT,
    subcategory  TEXT,
    seller       TEXT,
    price        BIGINT      NOT NULL DEFAULT 0,
    currency     TEXT        NOT NULL DEFAULT '$',
    price_change TEXT,          -- 'up' | 'down' | 'same' | 'new'
    price_diff   BIGINT      NOT NULL DEFAULT 0,
    anticipo     BIGINT      NOT NULL DEFAULT 0,
    location     TEXT,
    url          TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_date, url)
);

-- Table: avg price per model per run (for trend charts)
CREATE TABLE IF NOT EXISTS model_stats (
    id          BIGSERIAL PRIMARY KEY,
    run_date    DATE        NOT NULL,
    brand       TEXT        NOT NULL,
    model       TEXT        NOT NULL,
    avg_price   BIGINT,
    min_price   BIGINT,
    max_price   BIGINT,
    count       INTEGER     NOT NULL DEFAULT 0,
    n_up        INTEGER     NOT NULL DEFAULT 0,
    n_down      INTEGER     NOT NULL DEFAULT 0,
    n_new       INTEGER     NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_date, brand, model)
);

-- Useful indexes
CREATE INDEX IF NOT EXISTS idx_listings_run_date  ON listings (run_date);
CREATE INDEX IF NOT EXISTS idx_listings_brand     ON listings (brand);
CREATE INDEX IF NOT EXISTS idx_listings_model     ON listings (model);
CREATE INDEX IF NOT EXISTS idx_listings_url       ON listings (url);
CREATE INDEX IF NOT EXISTS idx_model_stats_brand  ON model_stats (brand, model, run_date);
