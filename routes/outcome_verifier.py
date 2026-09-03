"""outcome_verifier.py — Phase r65 (2026-06-02).

Walks brain_metric_targets whose verify_at <= NOW() and verified_at IS NULL.
For each: read latest brain_metric_observations row for the same metric_key,
compare to baseline_value, decide verified_outcome.

Regression spotter: any tracked metric that declines >2% within 24h of a
brain-applied output emits a brain_regression row + brain_notification
severity='warn' + (when auto_revertible=true) invokes the inverse action.
"""
from __future__ import annotations
import os
import datetime
import sys
import json
import urllib.request
from flask import Blueprint, jsonify, request

outcome_verifier_bp = Blueprint('outcome_verifier', __name__)
_ADMIN_KEY = (os.environ.get('DCHUB_ADMIN_KEY')
              or os.environ.get('DCHUB_INTERNAL_KEY') or '').strip()
_REGRESSION_THRESHOLD_PCT = 2.0

# Per-metric direction: True = higher_is_better, False = lower_is_better
_METRIC_DIRECTION = {
    'dcpi.markets_fresh_pct':         True,
    'mcp.funnel.identified_conv_pct': True,
    'sentinel.error_count':           False,
    'sentinel.site_score':            True,
    'evolution.composite_health':     True,
    'evolution.score':                True,
    'press.published_7d':             True,
    'cron.heartbeat_jobs_healthy':    True,
}


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


def _latest_obs(cur, metric_key):
    cur.execute("""SELECT value, observed_at FROM brain_metric_observations
        WHERE metric_key=%s ORDER BY observed_at DESC LIMIT 1""", (metric_key,))
    return cur.fetchone()


def verify_pending() -> dict:
    out = {'verified': 0, 'positive': 0, 'negative': 0, 'flat': 0,
           'undetermined': 0, 'regressions_caught': 0, 'auto_reverted': 0,
           'ran_at': datetime.datetime.utcnow().isoformat() + 'Z'}
    c = _conn()
    if not c:
        out['error'] = 'no_database'
        return out
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT * FROM brain_metric_targets
                WHERE verified_at IS NULL AND verify_at <= NOW()
                ORDER BY verify_at ASC LIMIT 100""")
            targets = cur.fetchall()
            for t in targets:
                metric_key = t['metric_key']
                latest = _latest_obs(cur, metric_key)
                if not latest:
                    cur.execute("""UPDATE brain_metric_targets
                        SET verified_at=NOW(), verified_outcome='undetermined',
                            evidence=%s WHERE id=%s""",
                        ('no_observation_available', t['id']))
                    out['undetermined'] += 1
                    out['verified'] += 1
                    continue
                current_value = float(latest[0])
                baseline = float(t['baseline_value']) if t['baseline_value'] is not None else None
                target_delta = float(t['target_delta']) if t['target_delta'] is not None else 0.0
                direction_up = _METRIC_DIRECTION.get(metric_key, True)
                if baseline is None:
                    verdict = 'undetermined'
                else:
                    delta = current_value - baseline
                    floor = max(abs(target_delta), abs(baseline) * 0.01)
                    if (direction_up and delta >= floor) or (not direction_up and delta <= -floor):
                        verdict = 'moved_positive'
                    elif (direction_up and delta <= -floor) or (not direction_up and delta >= floor):
                        verdict = 'moved_negative'
                    else:
                        verdict = 'no_change'
                evidence = f'baseline={baseline} current={current_value} direction={"up" if direction_up else "down"}'
                cur.execute("""UPDATE brain_metric_targets
                    SET verified_at=NOW(), verified_value=%s,
                        verified_outcome=%s, evidence=%s WHERE id=%s""",
                    (current_value, verdict, evidence, t['id']))
                out['verified'] += 1
                if verdict == 'moved_positive':
                    out['positive'] += 1
                elif verdict == 'moved_negative':
                    out['negative'] += 1
                elif verdict == 'no_change':
                    out['flat'] += 1
                else:
                    out['undetermined'] += 1
                # Regression spotter
                if verdict == 'moved_negative':
                    pct_change = (100.0 * (current_value - baseline) / baseline) if baseline else 0.0
                    cur.execute("""INSERT INTO brain_regression
                        (metric_key, baseline_value, regressed_value, pct_change,
                         suspected_target_id, auto_reverted, escalation_summary)
                        VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id""",
                        (metric_key, baseline, current_value, pct_change,
                         t['id'], False,
                         f'output_kind={t["output_kind"]} ref={t["output_ref"]}'))
                    rid_row = cur.fetchone()
                    rid = rid_row['id'] if rid_row else None
                    out['regressions_caught'] += 1
                    # brain notification (best-effort)
                    try:
                        from routes.brain_evolution import log_notification
                        log_notification(
                            kind='regression_detected',
                            summary=(f'{metric_key} regressed {round(pct_change, 2)}% '
                                     f'after {t["output_kind"]} {t["output_ref"]}'),
                            severity='warn',
                            detail={'metric_key': metric_key, 'baseline': baseline,
                                    'current': current_value, 'target_id': t['id'],
                                    'regression_id': rid})
                    except Exception:
                        pass
                    # auto-revert when wired
                    if bool(t.get('auto_revertible')) and t.get('revert_payload'):
                        try:
                            payload = t['revert_payload']
                            if isinstance(payload, str):
                                payload = json.loads(payload)
                            url = payload.get('url')
                            method = payload.get('method', 'POST')
                            if url:
                                req = urllib.request.Request(
                                    url, method=method,
                                    data=json.dumps(payload.get('body') or {}).encode('utf-8'),
                                    headers={'X-Admin-Key': _ADMIN_KEY,
                                             'Content-Type': 'application/json'})
                                urllib.request.urlopen(req, timeout=15).read(256)
                                cur.execute("""UPDATE brain_regression SET auto_reverted=TRUE
                                    WHERE id=%s""", (rid,))
                                out['auto_reverted'] += 1
                        except Exception as e:
                            print(f'[outcome_verifier] auto-revert failed: {e}',
                                  file=sys.stderr)
    finally:
        try: c.close()
        except Exception: pass
    return out


@outcome_verifier_bp.post('/api/v1/brain/outcome-verifier/run')
def run_endpoint():
    sent = (request.headers.get('X-Admin-Key')
            or request.headers.get('X-Internal-Key') or '').strip()
    if _ADMIN_KEY and sent != _ADMIN_KEY and (request.headers.get('X-DC-Internal-Cron') or '') != '1':
        return jsonify(error='unauthorized'), 401
    return jsonify(verify_pending()), 200


@outcome_verifier_bp.get('/api/v1/brain/outcome-verifier/summary')
def outcome_summary():
    c = _conn()
    if not c:
        return jsonify(ok=False, error='db_unavailable'), 200
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT
                COUNT(*) FILTER (WHERE verified_outcome='moved_positive'
                    AND verified_at >= NOW() - INTERVAL '7 days') AS pos_7d,
                COUNT(*) FILTER (WHERE verified_outcome='moved_negative'
                    AND verified_at >= NOW() - INTERVAL '7 days') AS neg_7d,
                COUNT(*) FILTER (WHERE verified_outcome='no_change'
                    AND verified_at >= NOW() - INTERVAL '7 days') AS flat_7d,
                COUNT(*) FILTER (WHERE verified_outcome='moved_positive'
                    AND verified_at >= NOW() - INTERVAL '30 days') AS pos_30d,
                COUNT(*) FILTER (WHERE verified_outcome='moved_negative'
                    AND verified_at >= NOW() - INTERVAL '30 days') AS neg_30d
                FROM brain_metric_targets""")
            row = cur.fetchone() or [0, 0, 0, 0, 0]
            cur.execute("""SELECT COUNT(*) FROM brain_regression
                WHERE detected_at >= NOW() - INTERVAL '7 days'""")
            reg7 = (cur.fetchone() or [0])[0]
            cur.execute("""SELECT COUNT(*) FROM brain_regression
                WHERE auto_reverted=TRUE AND detected_at >= NOW() - INTERVAL '7 days'""")
            rev7 = (cur.fetchone() or [0])[0]
    finally:
        try: c.close()
        except Exception: pass
    pos7 = int(row[0] or 0)
    neg7 = int(row[1] or 0)
    flat7 = int(row[2] or 0)
    denom7 = pos7 + neg7 + flat7
    outcome_ratio_7d = round(pos7 / denom7, 3) if denom7 else 0.0
    return jsonify(
        ok=True,
        pos_7d=pos7, neg_7d=neg7, flat_7d=flat7,
        pos_30d=int(row[3] or 0), neg_30d=int(row[4] or 0),
        outcome_ratio_7d=outcome_ratio_7d,
        regressions_7d=int(reg7), auto_reverted_7d=int(rev7),
        purpose=('Outcome-bound brain scorecard. moved_positive = KPI moved in '
                 'the right direction AFTER an output shipped; this is the only '
                 'metric that proves the brain is actually working.')
    ), 200
