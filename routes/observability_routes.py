"""Phase 38 — minimal observability blueprint with conversion tracking."""
from flask import Blueprint, jsonify, request
import datetime

observability_bp = Blueprint('observability', __name__)


@observability_bp.route('/api/v1/observability/conversion/track', methods=['POST', 'GET'])  # phase39_canonical_track
@observability_bp.route('/api/v1/conversion/track', methods=['POST', 'GET'])  # legacy fallback (CF-blocked)
def phase38_track_click():
    """Phase 38 — record an attributed upgrade-URL click."""
    args = request.args if request.method == 'GET' else (request.get_json(silent=True) or request.form)
    tool = (args.get('tool') or 'unknown')[:64]
    calls = args.get('calls', '0')
    tier = (args.get('tier') or 'free')[:32]
    try: calls_int = int(calls)
    except (ValueError, TypeError): calls_int = 0

    out = {'success': True, 'tracked': True, 'tool': tool, 'calls': calls_int, 'tier': tier,
           'tracked_at': datetime.datetime.utcnow().isoformat() + 'Z'}
    try:
        from db_utils import try_get_db
        conn = try_get_db()
        if conn:
            cur = conn.cursor()
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS mcp_conversion_clicks (
                        id SERIAL PRIMARY KEY,
                        clicked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        tool_name TEXT,
                        prior_calls INTEGER,
                        tier_at_click TEXT,
                        user_agent TEXT,
                        referer TEXT
                    )
                """)
                conn.commit()
                cur.execute("""
                    INSERT INTO mcp_conversion_clicks
                        (tool_name, prior_calls, tier_at_click, user_agent, referer)
                    VALUES (%s, %s, %s, %s, %s)
                """, (tool, calls_int, tier,
                      (request.headers.get('User-Agent') or '')[:300],
                      (request.headers.get('Referer') or '')[:300]))
                conn.commit()
            except Exception as _e:
                try: conn.rollback()
                except Exception: pass
                out['_db_error'] = type(_e).__name__
            try: conn.close()
            except Exception: pass
    except Exception as _e:
        out['_error'] = type(_e).__name__
    return jsonify(out)


@observability_bp.route('/api/v1/observability/conversion/funnel', methods=['GET'])  # phase39_canonical_track
@observability_bp.route('/api/v1/conversion/funnel', methods=['GET'])  # legacy fallback (CF-blocked)
def phase38_funnel():
    """Quick funnel report — signals, clicks, conversions over last N days."""
    days = max(1, min(int(request.args.get('days', 30)), 90))
    out = {'success': True, 'days': days, 'data': {}}
    try:
        from db_utils import try_get_db
        conn = try_get_db()
        if conn:
            cur = conn.cursor()
            for label, sql in [
                ('signals', f"SELECT COUNT(*) FROM mcp_conversions WHERE created_at > NOW() - INTERVAL '{days} days'"),
                ('clicks', f"SELECT COUNT(*) FROM mcp_conversion_clicks WHERE clicked_at > NOW() - INTERVAL '{days} days'"),
                ('paid', f"SELECT COUNT(*) FROM mcp_conversions WHERE stage = 'paid' AND created_at > NOW() - INTERVAL '{days} days'"),
            ]:
                try:
                    cur.execute(sql)
                    out['data'][label] = int((cur.fetchone() or (0,))[0])
                except Exception as _e:
                    try: conn.rollback()
                    except Exception: pass
                    out['data'][label] = -1
            try: conn.close()
            except Exception: pass
    except Exception as _e:
        out['_error'] = type(_e).__name__
    return jsonify(out)
