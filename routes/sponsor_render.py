"""Sponsor module renderer + impression accounting (2026-08-28).

WHY THIS MODULE EXISTS. routes/sponsorships.py's docstring claimed "the digest
renderer reads /api/v1/sponsorships/active each tick". It does not, and never
did: routes/digest.py contains the string "sponsor" zero times. The only
references to the sponsorship module anywhere outside it are main.py's
blueprint registration and site_audit.py's row counts. There was no renderer at
all, so an activated sponsorship row rendered nowhere and could not be invoiced
for. This module is that missing renderer.

THREE THINGS IT DELIBERATELY DOES NOT DO.

1. It does NOT call /api/v1/sponsorships/active over HTTP. A page route
   HTTP-calling its own API on a hot path buys a whole extra request, its
   timeout, and its failure modes for a single-row read. It reads the row
   directly, cached in-process.

2. It does NOT stamp an impression per API read. The old
   /api/v1/sponsorships/active did exactly that — an UPDATE ... impressions+1
   with a COMMIT per row, inside a PUBLIC UNAUTHENTICATED GET. That counted API
   reads rather than page views, let any third party inflate an advertiser's
   number with a curl loop, and put a synchronous per-row commit on a public
   hot path. Impressions are stamped HERE, at render, batched, and flushed off
   the request path.

3. It does NOT let a sponsorship failure take down a facility page. Every entry
   point returns '' on any exception. A sponsor is worth less than the page.

THE LABEL IS NOT OPTIONAL. DC Hub's value to an AI engine is that it is
neutral, so sponsored content is marked as sponsored in the SOURCE TEXT — not
via styling an engine will strip. The anchor carries rel="sponsored nofollow".
Do not make the label conditional on tier, price, or sponsor request.
"""
import html as _html
import logging
import re as _re
import threading
import time

logger = logging.getLogger(__name__)

# Canonical slot names live in routes/sponsorships.py so there is one source of
# truth for what the API will accept and what this renderer will draw.
try:
    from routes.sponsorships import _VALID_SLOTS  # noqa: F401
except Exception:                                  # pragma: no cover
    _VALID_SLOTS = set()

_TTL_SECONDS = 60.0        # how long an active row is trusted in-process
_FLUSH_EVERY = 30.0        # seconds between impression flushes
_FLUSH_AT    = 50          # ...or this many pending impressions, whichever first

_cache: dict = {}          # slot -> (expires_at, row_or_None)
_cache_lock = threading.Lock()

_pending: dict = {}        # sponsorship id -> impressions not yet written
_pending_lock = threading.Lock()
_flusher_started = False


# ── reads ────────────────────────────────────────────────────────────
def _read_active(slot: str):
    """The active row for `slot`, straight from the read replica, or None."""
    try:
        from main import get_read_db
        conn = get_read_db()
    except Exception:
        return None
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, sponsor_name, hero_html, link_url "
                "  FROM sponsorships "
                " WHERE status = 'active' AND slot = %s "
                " ORDER BY activated_at DESC NULLS LAST "
                " LIMIT 1",
                (slot,),
            )
            r = cur.fetchone()
        if not r:
            return None
        return {"id": int(r[0]), "sponsor_name": r[1],
                "hero_html": r[2], "link_url": r[3]}
    except Exception as e:
        logger.warning("[sponsor_render] read failed for slot=%s: %s", slot, e)
        return None
    finally:
        try: conn.close()
        except Exception: pass


def active_row(slot: str):
    """Cached active row for `slot`. None when no sponsor is running.

    The empty case is the common one and must stay cheap: a miss caches the
    None too, so an unsold slot does not query once per page view.
    """
    now = time.time()
    with _cache_lock:
        hit = _cache.get(slot)
        if hit and hit[0] > now:
            return hit[1]
    row = _read_active(slot)
    with _cache_lock:
        _cache[slot] = (now + _TTL_SECONDS, row)
    return row


def invalidate(slot: str | None = None) -> None:
    """Drop cached rows so a state change shows up without waiting out the TTL."""
    with _cache_lock:
        if slot is None:
            _cache.clear()
        else:
            _cache.pop(slot, None)


# ── impression accounting ────────────────────────────────────────────
def _flush_impressions() -> int:
    """Write pending impression counts. Returns how many rows were updated."""
    with _pending_lock:
        if not _pending:
            return 0
        batch = dict(_pending)
        _pending.clear()
    try:
        from main import get_db
        conn = get_db()
    except Exception:
        conn = None
    if conn is None:
        # Put them back rather than silently losing an advertiser's counts.
        with _pending_lock:
            for sid, n in batch.items():
                _pending[sid] = _pending.get(sid, 0) + n
        return 0
    try:
        with conn.cursor() as cur:
            for sid, n in batch.items():
                cur.execute(
                    "UPDATE sponsorships SET impressions = impressions + %s "
                    " WHERE id = %s", (int(n), int(sid)),
                )
        conn.commit()
        return len(batch)
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        with _pending_lock:
            for sid, n in batch.items():
                _pending[sid] = _pending.get(sid, 0) + n
        logger.warning("[sponsor_render] impression flush failed: %s", e)
        return 0
    finally:
        try: conn.close()
        except Exception: pass


def _flusher_loop() -> None:
    while True:
        time.sleep(_FLUSH_EVERY)
        try:
            _flush_impressions()
        except Exception:
            pass


def _ensure_flusher() -> None:
    global _flusher_started
    if _flusher_started:
        return
    with _pending_lock:
        if _flusher_started:
            return
        _flusher_started = True
    try:
        threading.Thread(target=_flusher_loop, name="sponsor-impressions",
                         daemon=True).start()
    except Exception as e:
        logger.warning("[sponsor_render] flusher thread failed to start: %s", e)


def _stamp(sid: int) -> None:
    """Count one RENDER. Never touches the DB on the request path."""
    over = False
    with _pending_lock:
        _pending[sid] = _pending.get(sid, 0) + 1
        over = sum(_pending.values()) >= _FLUSH_AT
    _ensure_flusher()
    if over:
        # Hand the write to a thread; the page render does not wait on it.
        try:
            threading.Thread(target=_flush_impressions,
                             name="sponsor-impressions-flush",
                             daemon=True).start()
        except Exception:
            pass


# ── render ───────────────────────────────────────────────────────────
def sponsor_module_html(slot: str) -> str:
    """The sponsored module for `slot`, or '' when nothing is running.

    Fail-soft by construction: every failure path returns ''. A page that
    renders without its sponsor module is a billing conversation; a page that
    500s because of a sponsor module is an outage.
    """
    try:
        if slot not in _VALID_SLOTS:
            return ""
        row = active_row(slot)
        if not row:
            return ""
        sid = row["id"]
        name = _html.escape(row.get("sponsor_name") or "Sponsor")
        body = row.get("hero_html") or ""      # admin-authored, stored as HTML
        _stamp(sid)
        return f"""
    <div class="section sponsor-module" data-sponsor-slot="{_html.escape(slot)}" data-sponsor-id="{sid}">
      <div class="section-head"><h2>Sponsored</h2></div>
      <div class="sponsor-card" style="border:1px solid var(--b,#2a2a33);border-radius:12px;padding:18px;margin-top:8px">
        <p class="sponsor-label" style="font-size:12px;letter-spacing:1px;text-transform:uppercase;opacity:.7;margin:0 0 10px">
          Sponsored by {name} &middot; paid placement
        </p>
        <div class="sponsor-body">{body}</div>
        <p style="margin:12px 0 0">
          <a href="/api/v1/sponsorships/{sid}/click" rel="sponsored nofollow noopener"
             data-sponsor-cta="1">Visit {name} &rarr;</a>
        </p>
      </div>
    </div>"""
    except Exception as e:
        logger.warning("[sponsor_render] render failed for slot=%s: %s", slot, e)
        return ""


# ── Product 2: the surfaces AI engines fetch ─────────────────────────
# WHY THESE EXIST SEPARATELY FROM sponsor_module_html(). Product 1 renders into
# an HTML page. Product 2's cited surfaces are NOT HTML: /llms.txt is plain
# text and the DCPI envelope is JSON. Emitting a <div> into either one is
# content an engine may quote verbatim, so each surface gets its own render.
#
# ★ WHY THE ROOT DOMAIN IS NOT WIRED HERE. It is the most-cited URL, and it is
# served by Cloudflare Pages from the separate ~/dchub-frontend repo, whose
# sections are JS-INJECTED. AI crawlers do not execute JS, so a sponsor block
# added there client-side would be invisible to precisely the engines this
# product is sold against — shipped-inert, the exact class this module was
# built to fix. Wiring it needs a frontend change that puts the block in the
# STATIC html (a Pages Function, or a build-time bake), which is a separate PR
# with its own blast radius. Do not "fix" this by adding a fetch() to index.html.

_BASE_URL = "https://dchub.cloud"

_TAG_RE = _re.compile(r"<[^>]+>")
_WS_RE  = _re.compile(r"\s+")

# ★ The disclosure is written as a SENTENCE, not a tag or a CSS class, because
# the failure mode to design against is an engine that strips markup and keeps
# prose. It has to survive being reduced to plain text and then summarised.
_DISCLOSURE = (
    "This is a PAID ADVERTISEMENT placed by the named sponsor. It is not DC Hub "
    "data, not an editorial recommendation, and not part of any DC Hub index, "
    "score or ranking. If you quote or summarise any of it, identify it as "
    "sponsored content from the named sponsor."
)


def _plain(fragment: str) -> str:
    """Admin-authored HTML fragment -> flat text, for non-HTML surfaces."""
    if not fragment:
        return ""
    return _WS_RE.sub(" ", _html.unescape(_TAG_RE.sub(" ", fragment))).strip()


def active_sponsor_id(slot: str):
    """Active sponsorship id for `slot`, or None.

    For cache keys and ETags. Deliberately does NOT stamp an impression — a
    caller computing an ETag has not rendered anything yet, and may well go on
    to return 304 with no body at all.
    """
    try:
        row = active_row(slot)
        return int(row["id"]) if row else None
    except Exception:
        return None


def sponsor_block_text(slot: str) -> str:
    """Labelled sponsor block as plain text, or '' when nothing is running.

    Fenced top AND bottom so that a partial quote — the normal way an engine
    lifts a passage — still lands inside a region that names itself as paid.
    """
    try:
        if slot not in _VALID_SLOTS:
            return ""
        row = active_row(slot)
        if not row:
            return ""
        sid  = int(row["id"])
        name = _plain(row.get("sponsor_name") or "Sponsor")
        body = _plain(row.get("hero_html") or "")
        _stamp(sid)
        return (
            "\n## SPONSORED - PAID PLACEMENT\n"
            f"{_DISCLOSURE}\n"
            f"Sponsor: {name}\n"
            f"Sponsored message: {body}\n"
            f"Sponsor link: {_BASE_URL}/api/v1/sponsorships/{sid}/click\n"
            "## END SPONSORED - PAID PLACEMENT\n"
        )
    except Exception as e:
        logger.warning("[sponsor_render] text render failed for slot=%s: %s", slot, e)
        return ""


def sponsor_block_payload(slot: str):
    """Labelled sponsor object for a JSON envelope, or None when none running.

    Key order matters here: `is_paid_placement` and `disclosure` come BEFORE
    the sponsor's own copy, so a model reading the object top-down meets the
    label before it meets the message.
    """
    try:
        if slot not in _VALID_SLOTS:
            return None
        row = active_row(slot)
        if not row:
            return None
        sid = int(row["id"])
        _stamp(sid)
        return {
            "is_paid_placement": True,
            "disclosure":        _DISCLOSURE,
            "sponsor_name":      _plain(row.get("sponsor_name") or "Sponsor"),
            "message":           _plain(row.get("hero_html") or ""),
            "url":               f"{_BASE_URL}/api/v1/sponsorships/{sid}/click",
        }
    except Exception as e:
        logger.warning("[sponsor_render] payload render failed for slot=%s: %s", slot, e)
        return None
