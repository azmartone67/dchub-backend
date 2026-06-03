"""brain_metric_targets.py — Phase r65 (2026-06-02): outcome-bound brain.

Every brain output (code fix, press release, autopilot action, linkedin
post, outreach pitch, lifecycle proposal) registers a metric_target row
here at emit time. A separate verifier (outcome_verifier_bp) re-reads the
target metric N days later and writes verified_outcome. brain_value_shipped
then scores by verified_outcome, not by emit volume.

Tables:
  brain_metric_targets        — what we promised when output shipped
  brain_metric_observations   — hourly KPI snapshots (source of truth)
  brain_regression            — KPI regressions auto-detected post-ship

Endpoints:
  POST /api/v1/brain/metric-target  (admin)  register a target
  GET  /api/v1/brain/metric-targets          list recent targets + verdicts
  GET  /api/v1/brain/metric-observation/<k>  current value of a KPI
"""
from __future__ import annotations
import os
import datetime
import json
import sys
from flask import Blueprint, jsonify, request

brain_metric_targets_bp = Blueprint('brain_metric_targets', __name__)
_ADMIN_KEY = (os.environ.get('DCHUB_ADMIN_KEY')
              or os.environ.get('DCHUB_INTERNAL_KEY') or '').strip()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_metric_targets (
    id BIGSERIAL PRIMARY KEY,
    output_kind TEXT NOT NULL,
    output_ref  TEXT NOT NULL,
    metric_key  TEXT NOT NULL,
    baseline_value NUMERIC,
    target_delta   NUMERIC,
    verify_at      TIMESTAMPTZ NOT NULL,
    verified_at    TIMESTAMPTZ,
    verified_value NUMERIC,
    verified_outcome TEXT,
    evidence       TEXT,
    auto_revertible BOOLEAN NOT NULL DEFAULT FALSE,
    revert_payload  JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_bmt_pending ON brain_metric_targets(verify_at) WHERE verified_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_bmt_recent  ON brain_metric_targets(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_bmt_metric  ON brain_metric_targets(metric_key, created_at DESC);

CREATE TABLE IF NOT EXISTS brain_metric_observations (
    id BIGSERIAL PRIMARY KEY,
    metric_key TEXT NOT NULL,
    value NUMERIC NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source TEXT
);
CREATE INDEX IF NOT EXISTS ix_bmo_metric_time ON brain_metric_observations(metric_key, observed_at DESC);

CREATE TABLE IF NOT EXISTS brain_regression (
    id BIGSERIAL PRIMARY KEY,
    metric_key TEXT NOT NULL,
    baseline_value NUMERIC,
    regressed_value NUMERIC,
    pct_change NUMERIC,
    suspected_target_id BIGINT REFERENCES brain_metric_targets(id),
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    auto_reverted BOOLEAN NOT NULL DEFAULT FALSE,
    escalation_summary TEXT
);
CREATE INDEX IF NOT EXISTS ix_brg_recent ON brain_regression(detected_at DESC);
"""

_SCHEMA_OK = {'init': False}


def _conn():
    import psycopg2
    db = os.environ.get('DATABASE_URL') or os.environ.get('NEON_DATABASE_URL')
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode='require', connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _ensure_schema():
    if _SCHEMA_OK['init']:
        return True
    c = _conn()
    if not c:
        return False
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
        _SCHEMA_OK['init'] = True
        return True
    except Exception as e:
        print(f'[brain_metric_targets] schema init failed: {e}', file=sys.stderr)
        return False
    finally:
        try: c.close()
        except Exception: pass


try:
    _ensure_schema()
except Exception:
    pass


def register_target(output_kind: str, output_ref: str, metric_key: str,
                    baseline_value=None, target_delta=None,
                    verify_after_days: int = 7,
                    auto_revertible: bool = False,
                    revert_payload: dict | None = None):
    """Fire-and-forget. Called by every emit path. Returns target id on success."""
    if not _ensure_schema():
        return None
    c = _conn()
    if not c:
        return None
    try:
        verify_at = datetime.datetime.utcnow() + datetime.timedelta(days=verify_after_days)
        with c.cursor() as cur:
            cur.execute("""INSERT INTO brain_metric_targets
                (output_kind, output_ref, metric_key, baseline_value,
                 target_delta, verify_at, auto_revertible, revert_payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                RETURNING id""",
                (output_kind, output_ref, metric_key, baseline_value,
                 target_delta, verify_at, auto_revertible,
                 json.dumps(revert_payload or {}, default=str)))
            row = cur.fetchone()
            return int(row[0]) if row else None
    except Exception as e:
        print(f'[brain_metric_targets] register failed: {e}', file=sys.stderr)
        return None
    finally:
        try: c.close()
        except Exception: pass


@brain_metric_targets_bp.post('/api/v1/brain/metric-target')
def metric_target_register():
    sent = (request.headers.get('X-Admin-Key')
            or request.headers.get('X-Internal-Key') or '').strip()
    if _ADMIN_KEY and sent != _ADMIN_KEY:
        return jsonify(error='unauthorized'), 401
    body = request.get_json(silent=True) or {}
    tid = register_target(
        output_kind=body.get('output_kind') or 'unknown',
        output_ref=body.get('output_ref') or '',
        metric_key=body.get('metric_key') or '',
        baseline_value=body.get('baseline_value'),
        target_delta=body.get('target_delta'),
        verify_after_days=int(body.get('verify_after_days') or 7),
        auto_revertible=bool(body.get('auto_revertible') or False),
        revert_payload=body.get('revert_payload') or None,
    )
    return jsonify(ok=tid is not None, target_id=tid), 200


@brain_metric_targets_bp.get('/api/v1/brain/metric-targets')
def metric_targets_list():
    c = _conn()
    if not c:
        return jsonify(ok=False, error='db_unavailable'), 200
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT * FROM brain_metric_targets
                ORDER BY created_at DESC LIMIT 200""")
            rows = cur.fetchall() or []
            cur.execute("""SELECT
                COUNT(*) FILTER (WHERE verified_outcome='moved_positive') AS pos,
                COUNT(*) FILTER (WHERE verified_outcome='moved_negative') AS neg,
                COUNT(*) FILTER (WHERE verified_outcome='no_change') AS flat,
                COUNT(*) FILTER (WHERE verified_at IS NULL AND verify_at <= NOW()) AS overdue,
                COUNT(*) FILTER (WHERE verified_at IS NULL AND verify_at > NOW()) AS pending
                FROM brain_metric_targets
                WHERE created_at >= NOW() - INTERVAL '30 days'""")
            summary = cur.fetchone() or {}
    finally:
        try: c.close()
        except Exception: pass
    # JSONB date serialize
    def _ser(row):
        out = {}
        for k, v in row.items():
            if hasattr(v, 'isoformat'):
                out[k] = v.isoformat()
            else:
                out[k] = v
        return out
    return jsonify(ok=True, items=[_ser(r) for r in rows],
                   summary=_ser(summary) if summary else {}), 200


@brain_metric_targets_bp.get('/api/v1/brain/metric-observation/<metric_key>')
def metric_observation_latest(metric_key):
    c = _conn()
    if not c:
        return jsonify(ok=False, error='db_unavailable'), 200
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT value, observed_at, source FROM brain_metric_observations
                WHERE metric_key=%s ORDER BY observed_at DESC LIMIT 1""", (metric_key,))
            row = cur.fetchone()
            if not row:
                return jsonify(ok=True, metric_key=metric_key, value=None), 200
            return jsonify(ok=True, metric_key=metric_key,
                value=float(row[0]) if row[0] is not None else None,
                observed_at=row[1].isoformat() if row[1] else None,
                source=row[2]), 200
    finally:
        try: c.close()
        except Exception: pass
