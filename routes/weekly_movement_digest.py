"""weekly_movement_digest.py — Phase r65 (2026-06-02).

One email per day to azmartone@gmail.com. Composes:
  - 7d delta on every tracked KPI (DCPI freshness %, conv %, etc.)
  - Brain outputs shipped 7d with verified_outcome split
  - Regressions detected + auto-reverts fired
  - Top 3 wins (moved_positive with biggest delta)
  - Top 3 losses (moved_negative or undetermined-overdue)

This is the OPERATOR's falsifiable scoreboard — if it's silent or full of
flat/negative outcomes, the brain isn't evolving regardless of volume.
"""
from __future__ import annotations
import os
import datetime
from flask import Blueprint, jsonify, request

weekly_movement_digest_bp = Blueprint('weekly_movement_digest', __name__)
_ADMIN_KEY = (os.environ.get('DCHUB_ADMIN_KEY')
              or os.environ.get('DCHUB_INTERNAL_KEY') or '').strip()
RESEND_KEY = os.environ.get('DCHUB_RESEND_API_KEY', '').strip()
FROM_EMAIL = os.environ.get('DCHUB_RESEND_FROM', 'DC Hub Brain <noreply@dchub.cloud>')
TO_EMAIL = os.environ.get('DCHUB_BRAIN_DIGEST_TO', 'azmartone@gmail.com')


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


def compose() -> dict:
    """Build the digest payload — also returned by the endpoint for diagnostics."""
    out = {'date': datetime.date.today().isoformat(),
           'metric_deltas': [], 'outputs_7d': {}, 'regressions_7d': [],
           'top_wins': [], 'top_losses': [], 'outcome_summary': {}}
    c = _conn()
    if not c:
        out['error'] = 'no_database'
        return out
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 7d delta per metric — latest vs ~7d ago
            cur.execute("""WITH latest AS (
                  SELECT DISTINCT ON (metric_key) metric_key, value, observed_at
                    FROM brain_metric_observations
                   ORDER BY metric_key, observed_at DESC
                ), prior AS (
                  SELECT DISTINCT ON (metric_key) metric_key, value, observed_at
                    FROM brain_metric_observations
                   WHERE observed_at <= NOW() - INTERVAL '7 days'
                   ORDER BY metric_key, observed_at DESC
                )
                SELECT l.metric_key, l.value AS now_v, p.value AS prev_v,
                       l.observed_at AS now_at, p.observed_at AS prev_at
                  FROM latest l LEFT JOIN prior p ON l.metric_key=p.metric_key""")
            for r in cur.fetchall():
                now_v = float(r['now_v']) if r['now_v'] is not None else None
                prev_v = float(r['prev_v']) if r['prev_v'] is not None else None
                delta = ((now_v - prev_v)
                         if (now_v is not None and prev_v is not None) else None)
                pct = (round(100.0 * delta / prev_v, 2)
                       if (delta is not None and prev_v) else None)
                out['metric_deltas'].append({
                    'metric_key': r['metric_key'], 'now': now_v, 'prev_7d': prev_v,
                    'delta': delta, 'pct_change': pct,
                    'now_at': r['now_at'].isoformat() if r['now_at'] else None})
            # outputs 7d with outcome split per kind
            cur.execute("""SELECT output_kind,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE verified_outcome='moved_positive') AS pos,
                COUNT(*) FILTER (WHERE verified_outcome='moved_negative') AS neg,
                COUNT(*) FILTER (WHERE verified_outcome='no_change') AS flat,
                COUNT(*) FILTER (WHERE verified_at IS NULL) AS pending
                FROM brain_metric_targets
                WHERE created_at >= NOW() - INTERVAL '7 days'
                GROUP BY output_kind ORDER BY total DESC""")
            for r in cur.fetchall():
                out['outputs_7d'][r['output_kind']] = {
                    'total': int(r['total']), 'pos': int(r['pos']),
                    'neg': int(r['neg']), 'flat': int(r['flat']),
                    'pending': int(r['pending'])}
            # regressions 7d
            cur.execute("""SELECT metric_key, baseline_value, regressed_value,
                pct_change, auto_reverted, escalation_summary, detected_at
                FROM brain_regression
                WHERE detected_at >= NOW() - INTERVAL '7 days'
                ORDER BY detected_at DESC LIMIT 25""")
            out['regressions_7d'] = [
                {k: (v.isoformat() if hasattr(v, 'isoformat') else v) for k, v in dict(r).items()}
                for r in cur.fetchall()
            ]
            # top wins / losses
            cur.execute("""SELECT metric_key, output_kind, output_ref, baseline_value,
                verified_value, evidence, verified_at FROM brain_metric_targets
                WHERE verified_outcome='moved_positive'
                  AND verified_at >= NOW() - INTERVAL '7 days'
                ORDER BY ABS(COALESCE(verified_value,0) - COALESCE(baseline_value,0)) DESC LIMIT 3""")
            out['top_wins'] = [
                {k: (v.isoformat() if hasattr(v, 'isoformat') else v) for k, v in dict(r).items()}
                for r in cur.fetchall()
            ]
            cur.execute("""SELECT metric_key, output_kind, output_ref, baseline_value,
                verified_value, evidence, verified_at FROM brain_metric_targets
                WHERE verified_outcome='moved_negative'
                  AND verified_at >= NOW() - INTERVAL '7 days'
                ORDER BY ABS(COALESCE(verified_value,0) - COALESCE(baseline_value,0)) DESC LIMIT 3""")
            out['top_losses'] = [
                {k: (v.isoformat() if hasattr(v, 'isoformat') else v) for k, v in dict(r).items()}
                for r in cur.fetchall()
            ]
    finally:
        try: c.close()
        except Exception: pass
    tot_pos = sum(v.get('pos', 0) for v in out['outputs_7d'].values())
    tot_neg = sum(v.get('neg', 0) for v in out['outputs_7d'].values())
    tot_flat = sum(v.get('flat', 0) for v in out['outputs_7d'].values())
    denom = tot_pos + tot_neg + tot_flat
    out['outcome_summary'] = {
        'outputs_verified': denom,
        'pos': tot_pos, 'neg': tot_neg, 'flat': tot_flat,
        'outcome_ratio': round(tot_pos / denom, 3) if denom else 0.0,
        'regressions': len(out['regressions_7d']),
    }
    return out


def _render_html(d: dict) -> str:
    rows = []
    rows.append(f'<h2>DC Hub Brain — What Moved (week of {d["date"]})</h2>')
    s = d.get('outcome_summary') or {}
    rows.append(f'<p><b>Outcome ratio:</b> {s.get("outcome_ratio")} '
                f'({s.get("pos")} moved positive / {s.get("neg")} negative / '
                f'{s.get("flat")} flat from {s.get("outputs_verified")} verified outputs)</p>')
    rows.append(f'<p><b>Regressions caught:</b> {s.get("regressions")}</p>')
    rows.append('<h3>KPI deltas (7d)</h3><table border=1 cellpadding=6>'
                '<tr><th>Metric</th><th>Now</th><th>7d ago</th><th>Δ</th><th>%</th></tr>')
    for m in d.get('metric_deltas') or []:
        rows.append(f'<tr><td>{m["metric_key"]}</td><td>{m["now"]}</td>'
                    f'<td>{m["prev_7d"]}</td><td>{m["delta"]}</td>'
                    f'<td>{m["pct_change"]}</td></tr>')
    rows.append('</table>')
    rows.append('<h3>Outputs by kind (7d)</h3><table border=1 cellpadding=6>'
                '<tr><th>Kind</th><th>Total</th><th>+</th><th>-</th><th>flat</th><th>pending</th></tr>')
    for k, v in (d.get('outputs_7d') or {}).items():
        rows.append(f'<tr><td>{k}</td><td>{v["total"]}</td><td>{v["pos"]}</td>'
                    f'<td>{v["neg"]}</td><td>{v["flat"]}</td><td>{v["pending"]}</td></tr>')
    rows.append('</table>')
    if d.get('regressions_7d'):
        rows.append('<h3>Regressions</h3><ul>')
        for r in d['regressions_7d']:
            rev = ' (auto-reverted)' if r.get('auto_reverted') else ''
            rows.append(f'<li>{r["metric_key"]}: {r.get("pct_change")}% '
                        f'after {r.get("escalation_summary")}{rev}</li>')
        rows.append('</ul>')
    return '\n'.join(rows)


def _send(html: str, subject: str) -> bool:
    if not RESEND_KEY or not TO_EMAIL:
        return False
    try:
        import requests
        r = requests.post(
            'https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {RESEND_KEY}',
                     'Content-Type': 'application/json'},
            json={'from': FROM_EMAIL, 'to': [TO_EMAIL],
                  'subject': subject, 'html': html}, timeout=15)
        return r.status_code in (200, 201)
    except Exception:
        return False


@weekly_movement_digest_bp.post('/api/v1/brain/weekly-movement-digest/run')
def run_digest():
    sent = (request.headers.get('X-Admin-Key')
            or request.headers.get('X-Internal-Key') or '').strip()
    if _ADMIN_KEY and sent != _ADMIN_KEY and (request.headers.get('X-DC-Internal-Cron') or '') != '1':
        return jsonify(error='unauthorized'), 401
    do_send = (request.args.get('send') or '').lower() in ('1', 'true', 'yes')
    payload = compose()
    html = _render_html(payload)
    subject = f'[DC Hub Brain] What moved — {payload["date"]}'
    sent_ok = _send(html, subject) if do_send else False
    return jsonify(ok=True, sent=sent_ok, to=TO_EMAIL if do_send else None,
                   payload=payload, html_chars=len(html)), 200
