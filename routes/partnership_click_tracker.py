"""
partnership_click_tracker.py — track which /partners#anchor gets clicks.

Phase ZZZZZ-round47.17 (2026-05-25). We now post about /partners
across LinkedIn (daily quad + weekly partnership cycle) + press
releases + direct email. We need to know which anchor gets clicked
so the rotation can favor what's working.

Approach: a /go/partners/<slug> redirect endpoint that:
  1. Logs the click (timestamp, slug, IP-ASN, user-agent, referrer)
  2. 302s to https://dchub.cloud/partners#<anchor>

LinkedIn posts + emails CAN use the bare /partners URL for cleanliness;
for traffic-attribution purposes, the email module + the optional
LinkedIn variant point at /go/partners/<slug> instead.

Endpoints:
  GET  /go/partners/<slug>                           302 + log click
  GET  /api/v1/partnerships/clicks/stats             counts by slug + 7d/30d
  GET  /api/v1/partnerships/clicks/recent            last 50 click rows
"""
import os
import re
import datetime
from contextlib import contextmanager
from flask import Blueprint, request, redirect, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

from routes.linkedin_partnership_weekly import _TRACKS as _LINKEDIN_TRACKS
from routes._swallowed_writes import note_swallowed_write

partnership_click_bp = Blueprint("partnership_click", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


def _ensure_table():
    if not (_pg and _dsn()):
        return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS partnership_clicks (
                    id          SERIAL PRIMARY KEY,
                    track_slug  TEXT NOT NULL,
                    clicked_at  TIMESTAMPTZ DEFAULT NOW(),
                    ip          TEXT,
                    user_agent  TEXT,
                    referrer    TEXT,
                    source      TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_pc_slug ON partnership_clicks(track_slug, clicked_at DESC);
                CREATE INDEX IF NOT EXISTS ix_pc_ts ON partnership_clicks(clicked_at DESC);
            """)
    except Exception:
        pass


_ensure_table()


_VALID_SLUGS = {t["slug"] for t in _LINKEDIN_TRACKS}


def _log_click(slug, src=None):
    try:
        with _conn() as c, c.cursor() as cur:
            ip = request.headers.get("CF-Connecting-IP") or request.remote_addr or ""
            ua = (request.headers.get("User-Agent", "") or "")[:300]
            ref = (request.headers.get("Referer", "") or "")[:300]
            cur.execute("""
                INSERT INTO partnership_clicks
                  (track_slug, ip, user_agent, referrer, source)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """, (slug, ip[:80], ua, ref, src or ""))
    except Exception:
        note_swallowed_write("partnership_clicks", where="partnership_click_tracker._log_click")
        pass


@partnership_click_bp.route("/go/partners", methods=["GET"], strict_slashes=False)
@partnership_click_bp.route("/go/partners/", methods=["GET"], strict_slashes=False)
def go_root():
    _log_click("partners", request.args.get("src"))
    return redirect("https://dchub.cloud/partners", code=302)


@partnership_click_bp.route("/go/partners/<slug>", methods=["GET"])
def go_anchor(slug):
    s = (slug or "").lower().strip()
    if s not in _VALID_SLUGS:
        # Unknown slug → redirect to /partners root, still log so we can
        # see what slugs are being typed wrong
        _log_click(f"unknown:{s[:30]}", request.args.get("src"))
        return redirect("https://dchub.cloud/partners", code=302)

    _log_click(s, request.args.get("src"))
    # Find the actual anchor for this slug
    track = next((t for t in _LINKEDIN_TRACKS if t["slug"] == s), None)
    target = (track or {}).get("url", "https://dchub.cloud/partners")
    return redirect(target, code=302)


# ── Referral links (2026-08-07) ────────────────────────────────────────────
# /go/partners/<slug> above is about ANCHORS on our own /partners page — which
# marketing track got clicked. This is a different thing: a reseller sending us
# traffic they expect commission on. Separate namespace so the two don't get
# tangled, and so a marketing anchor can never mint a commissionable ref.
#
# The cookie is the whole point. Attribution has to survive the 302 and the
# browsing session between landing and checkout; without it the conversion
# lands as web__pricing__none and the partner's claim is unfalsifiable.

# One leading slash, and the second character must NOT be another slash:
# "//evil.example.com" is a protocol-relative URL. Prefixing our host keeps it
# on dchub.cloud today, but the shape is a known redirect-bypass and proxies
# normalize it inconsistently — cheaper to refuse it than to rely on the
# prefix. Backslashes are excluded from the class for the same reason.
_SAFE_DEST = re.compile(r"^/(?!/)[A-Za-z0-9/_.-]{0,120}$")


@partnership_click_bp.route("/r/<partner>", methods=["GET"], strict_slashes=False)
def referral_entry(partner):
    """Log a referral click, stamp the attribution cookie, forward on.

    Unknown partner → still logged (so a wrong slug in a partner's own
    materials is visible to us rather than silently dead), but NO cookie is
    set, so it cannot become a commissionable conversion.
    """
    from routes._attribution_ref import (
        PARTNER_COOKIE, normalize_partner, partner_cookie_max_age,
    )

    slug = normalize_partner(partner)
    _log_click(f"referral:{slug or ('unknown:' + str(partner)[:30])}",
               request.args.get("src"))

    # ?to= lets a partner deep-link, but only to our own paths — never an
    # open redirect off-site.
    dest = (request.args.get("to") or "/").strip()
    if not _SAFE_DEST.match(dest):
        dest = "/"
    resp = redirect(f"https://dchub.cloud{dest}", code=302)

    if slug:
        resp.set_cookie(
            PARTNER_COOKIE, slug,
            max_age=partner_cookie_max_age(),
            path="/",
            secure=True,
            httponly=True,     # read server-side at checkout; JS never needs it
            samesite="Lax",    # must survive a top-level GET from the partner's site
        )
    return resp


@partnership_click_bp.route("/api/v1/partnerships/clicks/stats", methods=["GET"])
def stats():
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503
    try:
        with _conn() as c, c.cursor() as cur:
            # 7d + 30d + all-time per slug
            cur.execute("""
                SELECT track_slug,
                       COUNT(*) FILTER (WHERE clicked_at > NOW() - INTERVAL '7 days')  AS d7,
                       COUNT(*) FILTER (WHERE clicked_at > NOW() - INTERVAL '30 days') AS d30,
                       COUNT(*) AS total,
                       MAX(clicked_at) AS last_click
                  FROM partnership_clicks
                 GROUP BY track_slug
                 ORDER BY d30 DESC, total DESC
            """)
            by_slug = [{
                "track": r[0],
                "clicks_7d": int(r[1] or 0),
                "clicks_30d": int(r[2] or 0),
                "clicks_total": int(r[3] or 0),
                "last_click": r[4].isoformat() if r[4] else None,
            } for r in cur.fetchall()]

            # By source (linkedin / email / direct)
            cur.execute("""
                SELECT COALESCE(NULLIF(source,''),'direct') AS s, COUNT(*)
                  FROM partnership_clicks
                 WHERE clicked_at > NOW() - INTERVAL '30 days'
                 GROUP BY 1 ORDER BY 2 DESC
            """)
            by_source = [{"source": r[0], "clicks_30d": int(r[1])} for r in cur.fetchall()]

            # Top referrers
            cur.execute("""
                SELECT COALESCE(NULLIF(referrer,''),'(direct)') AS r, COUNT(*)
                  FROM partnership_clicks
                 WHERE clicked_at > NOW() - INTERVAL '30 days'
                 GROUP BY r ORDER BY 2 DESC LIMIT 10
            """)
            top_refs = [{"referrer": r[0][:120], "clicks_30d": int(r[1])} for r in cur.fetchall()]

            cur.execute("SELECT COUNT(*) FROM partnership_clicks")
            total_all = cur.fetchone()[0]

        return jsonify({
            "by_track": by_slug,
            "by_source": by_source,
            "top_referrers": top_refs,
            "total_all_time": int(total_all or 0),
            "computed_at": datetime.datetime.utcnow().isoformat() + "Z",
        }), 200, {"Cache-Control": "public, max-age=300"}
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {str(e)[:160]}"}), 500


@partnership_click_bp.route("/api/v1/partnerships/clicks/recent", methods=["GET"])
def recent():
    if not (_pg and _dsn()):
        return jsonify({"recent": []}), 200
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT track_slug, clicked_at, ip, user_agent, referrer, source
                  FROM partnership_clicks
                 ORDER BY clicked_at DESC LIMIT 50
            """)
            recent = [{
                "track": r[0],
                "clicked_at": r[1].isoformat() if r[1] else None,
                "ip": r[2][:50] if r[2] else None,
                "user_agent": (r[3] or "")[:120],
                "referrer": (r[4] or "")[:120],
                "source": r[5] or "",
            } for r in cur.fetchall()]
        return jsonify({"recent": recent, "count": len(recent)}), 200
    except Exception as e:
        return jsonify({"error": str(e)[:140], "recent": []}), 200
