"""Phase 109E — DCPI weekly digest. Triggered by GHA cron Mondays 14:00 UTC.

POST /api/v1/dcpi/digest/send
"""
import os, json
from flask import Blueprint, request, jsonify
import psycopg2, psycopg2.extras

dcpi_digest_bp = Blueprint("dcpi_digest", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


def _build_digest_html():
    """Build the HTML body listing top movers + biggest excess opportunities."""
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT DISTINCT ON (market_slug) * FROM market_power_scores
                       ORDER BY market_slug, computed_at DESC""")
        rows = cur.fetchall()
    rows.sort(key=lambda r: -(r.get("excess_power_score") or 0))
    top = rows[:5]
    bottom_constraint = sorted(rows, key=lambda r: -(r.get("constraint_score") or 0))[:5]

    html = "<h2>DC Hub Power Index — Weekly Digest</h2>"
    html += "<h3>Top 5 markets by Excess Power (the buy signals)</h3><ul>"
    for r in top:
        html += f"<li><strong>{r['market_name']}</strong> · Excess {r['excess_power_score']} · {r['verdict']}</li>"
    html += "</ul><h3>Top 5 markets by Constraint (the avoid list)</h3><ul>"
    for r in bottom_constraint:
        html += f"<li><strong>{r['market_name']}</strong> · Constraint {r['constraint_score']}</li>"
    html += "</ul><p><a href='https://dchub.cloud/dcpi'>See the full index →</a></p>"
    return html


@dcpi_digest_bp.route("/api/v1/dcpi/digest/send", methods=["POST"])
def send_digest():
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if expected and provided != expected:
        return jsonify(error="unauthorized"), 401
    dry = request.args.get("dry", "1") == "1"

    # Pull all dev-key holders
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT DISTINCT email FROM mcp_dev_keys
                       WHERE email IS NOT NULL AND email != ''
                       AND tier IN ('free','paid','enterprise')""")
        emails = [r[0] for r in cur.fetchall()]

    if dry:
        return jsonify(dry_run=True, recipient_count=len(emails),
                       sample=emails[:5], note="pass &dry=0 to send"), 200

    html = _build_digest_html()
    sent = 0; failed = 0
    try:
        from routes.redeem_routes import _p99_send_email
    except Exception as e:
        return jsonify(error=f"import: {e}"), 500

    for em in emails:
        # Use _p99_send_email but inject the digest html — simpler to just
        # send a plain digest via Resend directly here
        try:
            import requests as _rq
            r = _rq.post(
                "https://api.resend.com/emails",
                json={
                    "from": os.environ.get("DCHUB_FROM_EMAIL", "DC Hub <jonathan@dchub.cloud>"),
                    "to": [em],
                    "subject": "DCPI Weekly · the markets that moved",
                    "html": html,
                },
                headers={
                    "Authorization": f"Bearer {os.environ.get('RESEND_API_KEY','').strip()}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (compatible; DCHub/1.0; +https://dchub.cloud)",
                },
                timeout=15,
            )
            if 200 <= r.status_code < 300: sent += 1
            else: failed += 1
        except Exception:
            failed += 1
    return jsonify(sent=sent, failed=failed, recipient_count=len(emails)), 200
