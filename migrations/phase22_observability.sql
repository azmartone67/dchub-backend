-- Phase 22: observability tables
CREATE TABLE IF NOT EXISTS observability_metrics (
    metric TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_obs_metrics_time
    ON observability_metrics(metric, recorded_at);

CREATE TABLE IF NOT EXISTS daily_anomalies (
    id SERIAL PRIMARY KEY,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    severity TEXT NOT NULL DEFAULT 'info',
    summary TEXT NOT NULL,
    details JSONB
);
CREATE INDEX IF NOT EXISTS idx_anomalies_time
    ON daily_anomalies(detected_at DESC);
