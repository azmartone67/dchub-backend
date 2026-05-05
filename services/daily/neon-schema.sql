-- DC Hub Daily — Neon Postgres schema
-- Everything lives in the `daily` schema so it can't collide with your
-- existing tables in the main DB.
-- $ psql "$DATABASE_URL" -f neon-schema.sql

CREATE SCHEMA IF NOT EXISTS daily;

CREATE TABLE IF NOT EXISTS daily.snapshots (
    date          DATE PRIMARY KEY,
    payload       JSONB NOT NULL,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS snapshots_generated_idx
  ON daily.snapshots (generated_at DESC);

CREATE TABLE IF NOT EXISTS daily.renders (
    date          DATE NOT NULL,
    theme         TEXT NOT NULL CHECK (theme IN ('a', 'b', 'c')),
    size          TEXT NOT NULL CHECK (size  IN ('portrait', 'square', 'story')),
    r2_key        TEXT NOT NULL,
    bytes         INTEGER NOT NULL,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (date, theme, size)
);

CREATE INDEX IF NOT EXISTS renders_date_idx ON daily.renders(date DESC);

CREATE TABLE IF NOT EXISTS daily.posts (
    date          DATE NOT NULL,
    platform      TEXT NOT NULL CHECK (platform IN ('x', 'linkedin', 'webhook')),
    external_id   TEXT,
    success       BOOLEAN NOT NULL,
    payload       JSONB,
    posted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (date, platform)
);
