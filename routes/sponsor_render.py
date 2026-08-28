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
import os
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

# ── click-destination fallback ───────────────────────────────────────
# WHY THIS IS NOT JUST _cache. /click's whole job is to put the advertiser's
# prospect on the advertiser's site. It was doing that only while the WRITE
# pool was healthy: on a pool blip it returned 503 {"error":"no_db"} and the
# prospect landed on OUR error JSON wearing OUR domain — a lost click and a
# lost referral, on the one path an advertiser's own customers can see.
# Observed live once before this existed.
#
# _cache is keyed by SLOT and expires in 60s. That is correct for deciding what
# to render and useless for answering "where does sponsorship 12 point?" during
# an outage, which is exactly when the answer is needed. This map is keyed by
# ID and deliberately outlives the render TTL.
#
# ★ 15 minutes, not forever. Long enough to ride out a pool blip (seconds to
#   minutes); short enough that a cancelled sponsor stops receiving forwarded
#   clicks quickly even if invalidate() never runs.
_LINK_TTL_SECONDS = 900.0
_link_by_id: dict = {}     # sponsorship id -> (expires_at, link_url)
_link_lock = threading.Lock()

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
        row = {"id": int(r[0]), "sponsor_name": r[1],
               "hero_html": r[2], "link_url": r[3]}
        _remember_link(row["id"], row["link_url"])
        return row
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
    # A cancel must reach the click fallback too, or /click would keep
    # forwarding to a sponsor who has stopped paying for the traffic.
    with _link_lock:
        _link_by_id.clear()


def _remember_link(sid, link_url) -> None:
    """Note where a sponsorship points, for the /click fallback."""
    try:
        if not sid or not link_url:
            return
        with _link_lock:
            _link_by_id[int(sid)] = (time.time() + _LINK_TTL_SECONDS,
                                     str(link_url))
    except Exception:
        pass


def _read_link_by_id(sid):
    """The active row's link_url for `sid`, straight from the read replica."""
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
                "SELECT link_url FROM sponsorships "
                " WHERE id = %s AND status = 'active' LIMIT 1", (int(sid),),
            )
            r = cur.fetchone()
        return str(r[0]) if r and r[0] else None
    except Exception as e:
        logger.warning("[sponsor_render] link read failed for id=%s: %s", sid, e)
        return None
    finally:
        try: conn.close()
        except Exception: pass


def link_url_for_id(sid):
    """Best-known destination for `sid`, or None. FOR THE FALLBACK PATH ONLY.

    Cache first — it cannot fail, costs nothing, and is the reason the map
    exists. Then the READ replica, because the two pools are separate: the
    write pool being saturated is not evidence the read pool is
    (see reference: pool health is measured over two pools, not one).

    Deliberately does NOT stamp a click. The caller reaches this function only
    because the counting write could not happen, and an invoice that
    OVER-states clicks is the dangerous direction. Drop the count, not the
    customer.
    """
    try:
        sid = int(sid)
    except Exception:
        return None
    now = time.time()
    with _link_lock:
        hit = _link_by_id.get(sid)
        if hit and hit[0] > now:
            return hit[1]
        if hit:
            _link_by_id.pop(sid, None)
    link = _read_link_by_id(sid)
    if link:
        _remember_link(sid, link)
    return link


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


# ── the proven-demand gate (P1-3) ────────────────────────────────────
# WHY THIS EXISTS. /advertise sells Product 1 as: "Runs across the 7,292 pages
# with proven search demand — not the whole sitemap". The render call in
# facility_profile_page.py was UNCONDITIONAL, so the module would have drawn on
# every facility page that route serves — roughly 17k, not 7,292. The claim is
# already public, so this was not a missing feature but a false published
# claim, and an advertiser's own analyst can check it against the Search
# Console access the same page grants them.
#
# THE 7,292 IS NOT A CONSTANT, it is `SELECT count(*) FROM seo_proven_pages
# WHERE impressions >= 10` — measured 2026-08-28 as exactly 7,292 of 21,672
# rows, which is precisely the pair /advertise prints. Gating on the live table
# keeps the copy true as the number moves instead of pinning a stale integer.
#
# ★ ONLY facility_module is gated. seo_proven_pages is populated from GSC rows
#   matching /facilities/<slug> ONLY (see google_search_console._FACILITY_URL_RE),
#   so the 7,292 is a statement about facility pages. market_module runs on ~250
#   curated market pages, which is a different and already-narrow set; gating it
#   on a facility table would silently zero it.
_PROVEN_TTL_SECONDS = 3600.0   # the source table refreshes at most daily
_proven_cache: dict = {}       # {"at": float, "slugs": frozenset}
_proven_lock = threading.Lock()


def _proven_min_impressions() -> int:
    """The impression floor, read the same way every other consumer reads it.

    ★ MUST STAY IN LOCKSTEP with main.py's _SITEMAP_PROVEN_MIN_IMPRESSIONS and
      google_search_console.PROVEN_MIN_IMPRESSIONS_DEFAULT. A gate that admits a
      different set than the sitemap, while the rate card quotes one number for
      both, is the same class of drift this gate exists to close.
      tests/test_sponsorship_render_and_counters.py pins all three together.
    """
    try:
        v = int(str(os.environ.get("SITEMAP_PROVEN_MIN_IMPRESSIONS", "")).strip())
        return v if v > 0 else 10
    except Exception:
        return 10


def _load_proven_slugs():
    """Every facility slug with proven search demand, or None if unknown."""
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
                "SELECT slug FROM seo_proven_pages WHERE impressions >= %s",
                (_proven_min_impressions(),),
            )
            rows = cur.fetchall() or []
        slugs = frozenset(r[0] for r in rows if r and r[0])
        # An EMPTY result is not an answer. The table is populated (21,672 rows
        # on 2026-08-28) and refreshes additively — it never legitimately
        # empties — so empty means the read went wrong, and caching it would
        # silently switch the product off for a full TTL.
        return slugs or None
    except Exception as e:
        logger.warning("[sponsor_render] proven-page set unavailable: %s", e)
        return None
    finally:
        try: conn.close()
        except Exception: pass


def proven_slugs():
    """Cached proven-slug set, or None when it has never loaded.

    STALE-WHILE-REVALIDATE, deliberately. On a refresh failure the LAST GOOD
    set keeps serving: a stale membership list is still a correct policy, while
    dropping to None would pull the module off all 7,292 pages over a blip.
    Only a process that has never once loaded the set answers None.
    """
    now = time.time()
    with _proven_lock:
        hit = _proven_cache.get("slugs")
        if hit is not None and (now - _proven_cache.get("at", 0)) < _PROVEN_TTL_SECONDS:
            return hit
    fresh = _load_proven_slugs()
    with _proven_lock:
        if fresh is not None:
            _proven_cache["slugs"], _proven_cache["at"] = fresh, now
            return fresh
        stale = _proven_cache.get("slugs")
    if stale is not None:
        logger.warning("[sponsor_render] proven-page refresh failed — serving "
                       "the last good set of %d slugs", len(stale))
        return stale
    return None


def page_is_eligible(slot: str, page_slugs=None) -> bool:
    """May `slot` draw on the page identified by `page_slugs`?

    ★ FAILS CLOSED, LOUDLY. If the proven set has never loaded we do not know
      whether this page is one of the 7,292, and rendering anyway would put the
      module on pages we told advertisers it would not touch. Not rendering
      costs impressions, which under-counts an invoice — the safe direction,
      and the same one the render-stamp undercount already takes. Silence here
      would be the expensive failure, so it logs at ERROR.
    """
    if slot != "facility_module":
        return True
    if not page_slugs:
        return False
    allowed = proven_slugs()
    if allowed is None:
        logger.error("[sponsor_render] PROVEN-PAGE SET UNAVAILABLE — "
                     "facility_module withheld from every page rather than "
                     "render outside the 7,292 pages /advertise sells")
        return False
    return any(s in allowed for s in page_slugs if s)


# ── render ───────────────────────────────────────────────────────────
def sponsor_module_html(slot: str, page_slugs=None) -> str:
    """The sponsored module for `slot`, or '' when nothing is running.

    `page_slugs` identifies the page being rendered — for facility pages, the
    request slug AND the frozen canonical slug, because Search Console reports
    whichever one it indexed. Gated slots render '' when the page is not in the
    set /advertise sells; see page_is_eligible.

    Fail-soft by construction: every failure path returns ''. A page that
    renders without its sponsor module is a billing conversation; a page that
    500s because of a sponsor module is an outage.
    """
    try:
        if slot not in _VALID_SLOTS:
            return ""
        if not page_is_eligible(slot, page_slugs):
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


def sponsor_block_html(slot: str) -> str:
    """Labelled sponsor block as an HTML fragment, or '' when none running.

    WHY A THIRD RENDERER. sponsor_module_html() draws Product 1's card for the
    facility/market templates and carries only the short "paid placement" line.
    This is for the ROOT DOMAIN, which is a surface AI engines fetch and cite,
    so it carries the full _DISCLOSURE SENTENCE the same way the text and JSON
    renderers do — prose that survives an engine stripping the markup, rather
    than a class name that does not.

    ★ IT DOES NOT STAMP AN IMPRESSION, and cannot.
      The root domain is a static Cloudflare Pages asset. This fragment is
      baked into index.html at BUILD time, so a visitor reading it never
      touches the origin and there is no request for us to count. Stamping
      here would count BUILDS, which is the same defect the old
      /api/v1/sponsorships/active had when it counted API reads as page views.
      Root-domain reach is therefore reported from the Cloudflare per-engine
      crawl table, not from the impressions column. Say so on the invoice.
    """
    try:
        if slot not in _VALID_SLOTS:
            return ""
        row = active_row(slot)
        if not row:
            return ""
        sid = int(row["id"])
        name = _html.escape(_plain(row.get("sponsor_name") or "Sponsor"))
        body = _html.escape(_plain(row.get("hero_html") or ""))
        return (
            f'\n<section class="sponsor-source-block" data-sponsor-slot="{_html.escape(slot)}"'
            f' data-sponsor-id="{sid}" aria-label="Sponsored — paid placement"'
            f' style="max-width:1180px;margin:0 auto 40px;padding:0 24px">\n'
            f'  <div style="border:1px solid var(--border,#2a2a33);border-radius:12px;padding:20px">\n'
            f'    <p style="font-size:12px;letter-spacing:1px;text-transform:uppercase;'
            f'opacity:.7;margin:0 0 10px">Sponsored &middot; paid placement</p>\n'
            f'    <p style="margin:0 0 10px;font-size:13px;opacity:.85">{_html.escape(_DISCLOSURE)}</p>\n'
            f'    <p style="margin:0 0 6px"><strong>Sponsor:</strong> {name}</p>\n'
            f'    <p style="margin:0 0 12px">{body}</p>\n'
            f'    <p style="margin:0"><a href="{_BASE_URL}/api/v1/sponsorships/{sid}/click"'
            f' rel="sponsored nofollow noopener">Visit {name} &rarr;</a></p>\n'
            f'  </div>\n</section>\n'
        )
    except Exception as e:
        logger.warning("[sponsor_render] html block render failed for slot=%s: %s", slot, e)
        return ""
