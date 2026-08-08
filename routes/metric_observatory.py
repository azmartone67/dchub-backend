"""metric_observatory.py — Phase r65 (2026-06-02).

One hourly tick. Each tracked KPI computed from existing in-process
endpoints (test_client GETs, no remote fetches, no DB schema work).
Writes one brain_metric_observations row per metric. This is what the
outcome_verifier compares against. Brain self-scoring HAS to be grounded
in a metric the OPERATOR also looks at — so every value here is
readable on a dashboard and gettable via curl.

Tracked metrics (extend by editing _METRICS):
  dcpi.markets_fresh_pct        — % of DCPI markets with fresh source
  mcp.funnel.identified_conv_pct — paying / identified, /api/v1/mcp/funnel
  sentinel.error_count           — non-200 entries in latest scan
  sentinel.site_score            — /api/v1/sentinel/page-integrity site_score
  evolution.composite_health     — /api/v1/brain/lifecycle/findings.composite_health
  evolution.score                — /api/v1/brain/evolution.evolution_score
  press.published_7d             — /api/v1/brain/value-shipped.shipped_7d.press_releases
  cron.heartbeat_jobs_healthy    — last heartbeat ran_healthy / ran_total
"""
from __future__ import annotations
import os
import datetime
from flask import Blueprint, jsonify, current_app, request

metric_observatory_bp = Blueprint('metric_observatory', __name__)
_ADMIN_KEY = (os.environ.get('DCHUB_ADMIN_KEY')
              or os.environ.get('DCHUB_INTERNAL_KEY') or '').strip()


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


def _probe(path: str, timeout: int = 6) -> dict:
    try:
        with current_app.test_client() as tc:
            r = tc.get(path)
            if r.status_code != 200:
                return {}
            return r.get_json(silent=True) or {}
    except Exception:
        return {}


def _dcpi_fresh_pct(d):
    s = d.get('stats') or {}
    tot = int(s.get('total') or 0) or 1
    fresh = int(s.get('fresh') or 0)
    return round(100.0 * fresh / tot, 2)


def _mcp_conv_pct(d):
    funnel = (d.get('funnel') or {})
    ident = int(funnel.get('identified_count')
                or funnel.get('keyed_active_30d') or 0) or 1
    paid = int(funnel.get('paid_count')
               or funnel.get('paying_30d') or 0)
    return round(100.0 * paid / ident, 3)


def _sentinel_errors(d):
    findings = d.get('findings') or d.get('unhealthy') or []
    return len(findings) if isinstance(findings, list) else int(findings or 0)


def _sentinel_site_score(d):
    return float(d.get('site_score') or 0.0)


def _composite_health(d):
    return float(d.get('composite_health') or 0.0)


def _evolution_score(d):
    return float(d.get('evolution_score') or 0.0)


def _press_7d(d):
    return int(((d.get('shipped_7d') or {}).get('press_releases')) or 0)


def _heartbeat_healthy(d):
    tot = int(d.get('jobs_ran') or 0) or 1
    ok = int(d.get('jobs_healthy') or 0)
    return round(100.0 * ok / tot, 2)


_METRICS = [
    ('dcpi.markets_fresh_pct',         '/api/v1/dcpi/freshness',                _dcpi_fresh_pct),
    ('mcp.funnel.identified_conv_pct', '/api/v1/mcp/funnel',                    _mcp_conv_pct),
    ('sentinel.error_count',           '/api/v1/sentinel/scan',                 _sentinel_errors),
    ('sentinel.site_score',            '/api/v1/sentinel/page-integrity',       _sentinel_site_score),
    ('evolution.composite_health',     '/api/v1/brain/lifecycle/findings',      _composite_health),
    ('evolution.score',                '/api/v1/brain/evolution',               _evolution_score),
    ('press.published_7d',             '/api/v1/brain/value-shipped',           _press_7d),
    ('cron.heartbeat_jobs_healthy',    '/api/v1/cron/health',                   _heartbeat_healthy),
]


def snapshot_all() -> dict:
    """Probe every metric, persist to DB. Fail-soft per metric."""
    out = {'snapshotted': [], 'errors': [],
           'ran_at': datetime.datetime.utcnow().isoformat() + 'Z'}
    c = _conn()
    if not c:
        out['error'] = 'no_database'
        return out
    try:
        for key, path, extractor in _METRICS:
            try:
                data = _probe(path)
                val = extractor(data)
                if val is None:
                    out['errors'].append({'metric': key, 'reason': 'extract_none'})
                    continue
                with c.cursor() as cur:
                    cur.execute("""INSERT INTO brain_metric_observations
                        (metric_key, value, source) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                        (key, float(val), path))
                out['snapshotted'].append({'metric': key, 'value': float(val)})
            except Exception as e:
                out['errors'].append(
                    {'metric': key, 'reason': f'{type(e).__name__}: {str(e)[:80]}'})
    finally:
        try: c.close()
        except Exception: pass
    return out


@metric_observatory_bp.post('/api/v1/brain/metric-observatory/snapshot')
def snapshot_endpoint():
    sent = (request.headers.get('X-Admin-Key')
            or request.headers.get('X-Internal-Key') or '').strip()
    if _ADMIN_KEY and sent != _ADMIN_KEY and (request.headers.get('X-DC-Internal-Cron') or '') != '1':
        return jsonify(error='unauthorized'), 401
    return jsonify(snapshot_all()), 200


@metric_observatory_bp.get('/api/v1/brain/metric-observatory/current')
def current_metrics():
    c = _conn()
    if not c:
        return jsonify(ok=False, error='db_unavailable'), 200
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT DISTINCT ON (metric_key)
                metric_key, value, observed_at, source
                FROM brain_metric_observations
                ORDER BY metric_key, observed_at DESC""")
            rows = cur.fetchall() or []
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, items=[
        {'metric_key': r[0], 'value': float(r[1]),
         'observed_at': r[2].isoformat() if r[2] else None,
         'source': r[3]} for r in rows]), 200
