"""Phase FF+25-followup-r5 (2026-05-20) — sponsorship queue.
==========================================================================

User's coworker pitched a paid "Pocket Listings of the Week" newsletter
+ banner ad model. Friends-and-family trial with Jarrett. They need
queryable inventory so Bert can pitch with a real rate card.

This module ships:
  POST /api/v1/sponsorships          (admin)  — queue a new sponsorship
  GET  /api/v1/sponsorships          (admin)  — list queued + past
  GET  /api/v1/sponsorships/active   (public) — currently-running slot(s)
  POST /api/v1/sponsorships/<id>/run (admin) — promote queue to active
  DELETE /api/v1/sponsorships/<id>   (admin) — cancel

A sponsorship row is { slot, sponsor_name, hero_html, link_url, week_of,
status }. Slots: 'digest_featured', 'digest_banner', 'site_banner'.
Status moves queued → active → archived.

The digest renderer reads /api/v1/sponsorships/active each tick. When a
slot has an active row, the digest template fills the placeholder card;
otherwise it renders empty (the same digest still goes out).

No payment processing here — invoicing happens out-of-band for the
friends-and-family launch. Stripe wiring is the next layer.
"""
import os
from internal_auth import accepted_internal_keys
import json
import logging
import datetime
from flask import Blueprint, jsonify, redirect, request
from routes._swallowed_writes import note_swallowed_write

logger = logging.getLogger(__name__)
sponsorships_bp = Blueprint("sponsorships", __name__)


# ── Auth ─────────────────────────────────────────────────────────────
_INTERNAL_KEYS = accepted_internal_keys()
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


# ── DB ───────────────────────────────────────────────────────────────
# Slots. digest_* and site_banner predate the 2026-08 rate card; the three
# added below are the ones the two sold products actually render into:
#   facility_module / market_module -> Product 1 (facility & market pages)
#   ai_source_block                 -> Product 2 (root domain + DCPI score API)
# routes/sponsor_render.py imports this set, so adding a slot here is the only
# thing needed to make it renderable.
_VALID_SLOTS = {
    "digest_featured", "digest_banner", "site_banner",
    "facility_module", "market_module", "ai_source_block",
}
_VALID_STATUS = {"queued", "active", "archived", "cancelled"}


def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _ensure_table():
    conn = _get_db()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sponsorships (
                    id              SERIAL PRIMARY KEY,
                    slot            TEXT NOT NULL,
                    sponsor_name    TEXT NOT NULL,
                    sponsor_email   TEXT,
                    hero_html       TEXT NOT NULL,
                    link_url        TEXT NOT NULL,
                    week_of         DATE,
                    price_cents     INTEGER,
                    status          TEXT NOT NULL DEFAULT 'queued',
                    impressions     INTEGER NOT NULL DEFAULT 0,
                    clicks          INTEGER NOT NULL DEFAULT 0,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    activated_at    TIMESTAMPTZ,
                    archived_at     TIMESTAMPTZ,
                    notes           TEXT
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sponsorships_status_slot "
                "ON sponsorships(status, slot)"
            )
            conn.commit()
        return True
    except Exception as e:
        logger.warning(f"[sponsorships] table create failed: {e}")
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        try: conn.close()
        except Exception: pass


# ── POST /api/v1/sponsorships — queue ────────────────────────────────
@sponsorships_bp.route("/api/v1/sponsorships", methods=["POST"])
def queue_sponsorship():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    _ensure_table()
    p = request.get_json(silent=True) or {}
    slot = (p.get("slot") or "").strip()
    if slot not in _VALID_SLOTS:
        return jsonify(ok=False,
                       error=f"slot must be one of {sorted(_VALID_SLOTS)}"), 400
    sponsor_name = (p.get("sponsor_name") or "").strip()
    hero_html    = (p.get("hero_html") or "").strip()
    link_url     = (p.get("link_url") or "").strip()
    if not (sponsor_name and hero_html and link_url):
        return jsonify(ok=False,
                       error="sponsor_name, hero_html, link_url required"), 400

    conn = _get_db()
    if conn is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sponsorships
                  (slot, sponsor_name, sponsor_email, hero_html, link_url,
                   week_of, price_cents, status, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'queued',%s) ON CONFLICT DO NOTHING
                RETURNING id, created_at
            """, (slot, sponsor_name, p.get("sponsor_email"),
                  hero_html, link_url, p.get("week_of"),
                  p.get("price_cents"), p.get("notes")))
            r = cur.fetchone()
            conn.commit()
        return jsonify(ok=True, id=int(r[0]),
                       created_at=str(r[1]), status="queued",
                       slot=slot, sponsor_name=sponsor_name)
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: conn.close()
        except Exception: pass


# ── GET /api/v1/sponsorships — list (admin) ──────────────────────────
# AUTO-REPAIR: duplicate route '/api/v1/sponsorships' also in routes/sponsorships.py:117 — review and remove one
@sponsorships_bp.route("/api/v1/sponsorships", methods=["GET"])
def list_sponsorships():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    _ensure_table()
    status = (request.args.get("status") or "").strip().lower()
    slot   = (request.args.get("slot")   or "").strip().lower()
    where  = []
    args   = []
    if status in _VALID_STATUS:
        where.append("status = %s"); args.append(status)
    if slot in _VALID_SLOTS:
        where.append("slot = %s");   args.append(slot)
    sql = ("SELECT id, slot, sponsor_name, sponsor_email, link_url, "
           "       week_of, price_cents, status, impressions, clicks, "
           "       created_at, activated_at, archived_at, notes "
           "  FROM sponsorships")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT 100"

    conn = _get_db()
    if conn is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(args))
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "id": int(r[0]), "slot": r[1], "sponsor_name": r[2],
                    "sponsor_email": r[3], "link_url": r[4],
                    "week_of": str(r[5]) if r[5] else None,
                    "price_cents": r[6], "status": r[7],
                    "impressions": r[8] or 0, "clicks": r[9] or 0,
                    "created_at":   str(r[10]) if r[10] else None,
                    "activated_at": str(r[11]) if r[11] else None,
                    "archived_at":  str(r[12]) if r[12] else None,
                    "notes": r[13],
                })
        return jsonify(ok=True, count=len(rows), sponsorships=rows)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: conn.close()
        except Exception: pass


# ── GET /api/v1/sponsorships/active — public ─────────────────────────
@sponsorships_bp.route("/api/v1/sponsorships/active", methods=["GET"])
def active_sponsorships():
    """Public — what's currently rendering. Cached 60s edge."""
    _ensure_table()
    conn = _get_db()
    # S3b (2026-08-28): DERIVED, not a hardcoded literal. This was
    #   out = {"digest_featured": None, "digest_banner": None, "site_banner": None}
    # so a slot added to _VALID_SLOTS populated fine when a sponsor was active
    # but was simply ABSENT from the response when none was — and "no sponsor
    # running" is the case for the next several months. A consumer reading
    # resp["facility_module"] got a KeyError precisely in the normal case.
    out = {slot: None for slot in sorted(_VALID_SLOTS)}
    if conn is None:
        resp = jsonify(out)
        resp.headers["Cache-Control"] = "public, max-age=60"
        return resp
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT slot, sponsor_name, hero_html, link_url, id
                  FROM sponsorships
                 WHERE status = 'active'
                 ORDER BY activated_at DESC
            """)
            seen = set()
            for r in cur.fetchall():
                slot = r[0]
                if slot in seen: continue
                seen.add(slot)
                out[slot] = {
                    "id": int(r[4]),
                    "sponsor_name": r[1],
                    "hero_html": r[2],
                    "link_url":  r[3],
                }
                # S1 (2026-08-28): the impression UPDATE that used to live here
                # was DELETED, not moved to a background task. It counted API
                # READS rather than page views; this endpoint is public and
                # unauthenticated, so anyone could inflate an advertiser's
                # impression count — the number we invoice against — with a
                # curl loop; and it ran a synchronous per-row COMMIT on a
                # public hot path. Impressions are now stamped at RENDER, in
                # routes/sponsor_render.py, batched and off the request path.
                # Do not reintroduce a write in this GET.
    except Exception:
        note_swallowed_write("sponsorships", where="sponsorships.active_sponsorships")
        pass
    finally:
        try: conn.close()
        except Exception: pass

    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


# ── POST /api/v1/sponsorships/<id>/run — promote ─────────────────────
@sponsorships_bp.route("/api/v1/sponsorships/<int:sid>/run", methods=["POST"])
def run_sponsorship(sid: int):
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    _ensure_table()
    conn = _get_db()
    if conn is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with conn.cursor() as cur:
            # Archive any existing active in this slot first
            cur.execute("SELECT slot FROM sponsorships WHERE id = %s",
                        (sid,))
            r = cur.fetchone()
            if not r:
                return jsonify(ok=False, error="not_found"), 404
            slot = r[0]
            cur.execute("""
                UPDATE sponsorships SET status='archived',
                       archived_at = NOW()
                 WHERE slot = %s AND status = 'active'
            """, (slot,))
            cur.execute("""
                UPDATE sponsorships SET status='active',
                       activated_at = NOW(),
                       archived_at  = NULL
                 WHERE id = %s
             RETURNING id, slot, sponsor_name, status, activated_at
            """, (sid,))
            r2 = cur.fetchone()
            conn.commit()
        edge = _after_state_change(f"activate id={sid}")
        return jsonify(ok=True, id=int(r2[0]), slot=r2[1],
                       sponsor_name=r2[2], status=r2[3],
                       activated_at=str(r2[4]), edge=edge)
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: conn.close()
        except Exception: pass


# ── DELETE /api/v1/sponsorships/<id> — cancel ────────────────────────
@sponsorships_bp.route("/api/v1/sponsorships/<int:sid>", methods=["DELETE"])
def cancel_sponsorship(sid: int):
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    conn = _get_db()
    if conn is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sponsorships SET status='cancelled', "
                "       archived_at = NOW() "
                " WHERE id = %s RETURNING id", (sid,)
            )
            r = cur.fetchone()
            conn.commit()
        if not r:
            return jsonify(ok=False, error="not_found"), 404
        edge = _after_state_change(f"cancel id={sid}")
        return jsonify(ok=True, id=int(r[0]), status="cancelled", edge=edge)
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: conn.close()
        except Exception: pass


# ── P1-2: a state change must clear the edge ─────────────────────────
def _after_state_change(reason: str) -> dict:
    """Drop the in-process render cache AND the CF edge after activate /
    cancel / archive.

    THIS IS CONTRACTUAL, not housekeeping. Measured on 2026-08-28:
        /facilities/<slug>  s-maxage=300, stale-while-revalidate=3600
        /markets/<slug>     s-maxage=300, stale-while-revalidate=86400
    So without this, a CANCELLED sponsor keeps rendering for up to an hour on
    facility pages and a FULL DAY on market pages. If a competitor holds
    category exclusivity in that window, that is a breach of their clause.

    Fail-soft: a purge failure is logged and reported, never raised. The state
    change itself has already been committed and must not be rolled back
    because Cloudflare was unreachable.
    """
    out = {"cache_invalidated": False, "edge_purged": None}
    try:
        from routes.sponsor_render import invalidate
        invalidate()
        out["cache_invalidated"] = True
    except Exception as e:
        logger.warning("[sponsorships] render-cache invalidate failed: %s", e)
    try:
        from routes.cf_purge import _purge_everything
        res = _purge_everything()
        out["edge_purged"] = bool(res.get("ok"))
        if not res.get("ok"):
            # Say so loudly. A silent purge failure looks exactly like a
            # successful one from the outside, and the difference is whether a
            # cancelled sponsor is still on the page.
            logger.error("[sponsorships] EDGE PURGE FAILED after %s — a cancelled "
                         "sponsor may keep rendering for up to 24h on market "
                         "pages: %s", reason, res)
    except Exception as e:
        out["edge_purged"] = False
        logger.error("[sponsorships] EDGE PURGE ERRORED after %s: %s", reason, e)
    return out


# ── S4: GET /api/v1/sponsorships/<id>/click — count + forward ────────
@sponsorships_bp.route("/api/v1/sponsorships/<int:sid>/click", methods=["GET"])
def click_sponsorship(sid: int):
    """Stamp a click and forward to the sponsor's own link.

    `clicks` was created, SELECTed and reported to admins since 2026-05-20 and
    was NEVER WRITTEN by any code path, so click reporting did not exist — it
    was not broken, it was absent. This is that write.

    NO DESTINATION PARAMETER, deliberately. The target is read from the row's
    stored link_url. Taking a ?to= would make this an open redirect on a public
    unauthenticated GET, wearing our domain.
    """
    conn = _get_db()
    if conn is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sponsorships SET clicks = clicks + 1 "
                " WHERE id = %s AND status = 'active' "
                " RETURNING link_url", (sid,),
            )
            r = cur.fetchone()
            conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        logger.warning("[sponsorships] click stamp failed for id=%s: %s", sid, e)
        return jsonify(ok=False, error="click_failed"), 500
    finally:
        try: conn.close()
        except Exception: pass

    if not r or not r[0]:
        # Not active, or no link on the row. Do not invent a destination.
        return jsonify(ok=False, error="not_active"), 404
    dest = str(r[0]).strip()
    if not (dest.startswith("https://") or dest.startswith("http://")):
        logger.warning("[sponsorships] refusing non-http link_url on id=%s", sid)
        return jsonify(ok=False, error="bad_link"), 400
    resp = redirect(dest, code=302)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer-when-downgrade"
    return resp


# ── B1b: GET /api/v1/sponsorships/block — the fragment the root bake splices ──
@sponsorships_bp.route("/api/v1/sponsorships/block", methods=["GET"])
def sponsorship_block():
    """The labelled sponsor block for `slot`, as an embeddable HTML fragment.

    EXISTS FOR THE BUILD, NOT FOR A BROWSER. dchub.cloud/ is served by
    Cloudflare Pages from a separate repo whose sections are JS-INJECTED, and
    AI crawlers do not execute JS — so a client-side block on the most-cited
    URL we own would be invisible to exactly the engines Product 2 is sold
    against. deploy-pages.yml calls this at build time and splices the result
    into index.html as STATIC html.

    ★ Returns the fragment rather than letting the workflow assemble one, so
      the disclosure sentence has ONE author. Re-typing it into a YAML file is
      how the two copies drift and the published guarantee stops matching the
      served page.
    """
    slot = (request.args.get("slot") or "ai_source_block").strip()
    if slot not in _VALID_SLOTS:
        return jsonify(ok=False, error="unknown_slot",
                       valid=sorted(_VALID_SLOTS)), 400
    try:
        from routes.sponsor_render import sponsor_block_html, active_sponsor_id
        html = sponsor_block_html(slot)
        sid = active_sponsor_id(slot)
    except Exception as e:
        logger.warning("[sponsorships] block render failed for slot=%s: %s", slot, e)
        # Fail-soft: an empty fragment is the same shape as "no sponsor", and
        # the bake leaves the page unchanged rather than failing the deploy.
        return jsonify(ok=False, error="render_failed", slot=slot, html=""), 200
    return jsonify(ok=True, slot=slot, sponsor_id=sid, html=html)


def _smoke():
    logger.info("[sponsorships] ready · POST /api/v1/sponsorships "
                 "· GET /active (public)")

_smoke()
