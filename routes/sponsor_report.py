"""The monthly advertiser report (B5, 2026-08-28).

WHAT AN ADVERTISER IS ACTUALLY BUYING, and therefore what leads this report:
the per-engine CRAWL TABLE. A sponsor on DC Hub is not buying banner
impressions; they are buying presence on the pages AI engines read when they
answer infrastructure questions. Only the owner of the edge those pages are
served from can prove an engine fetched them. That table is the artifact no
competitor can produce about our surfaces — it goes first, not in an appendix.

EVERY NUMBER HERE UNDER-STATES, AND EACH ONE SAYS SO. That is deliberate: on an
invoice, under-counting is the safe direction and over-counting is a refund
conversation. The four known undercounts, all measured rather than assumed:

  1. ROOT-DOMAIN IMPRESSIONS DO NOT EXIST AT ALL. dchub.cloud/ is a static
     Cloudflare Pages asset with the block baked in at build time, so a reader
     never touches our origin and there is nothing to stamp. Root-domain reach
     is the crawl table, full stop.
  2. /api/v1/dcpi/scores IS CACHED (max-age=120, plus CF Rule #3), so only
     origin reads stamp an impression. Edge-served reads are invisible.
  3. CLICKS ARE DROPPED, NOT QUEUED, when the counting write fails — /click
     forwards the visitor and gives up the count, because replaying a write
     that may have already committed would bill for clicks that did not happen.
  4. THE CRAWL TABLE CANNOT COVER 30 DAYS YET. Cloudflare retains 8 days of
     request-level analytics for this zone (measured: 30d and 14d are REFUSED,
     8d is the ceiling), so a monthly table has to be accumulated from daily
     snapshots and its coverage grows one day at a time.

A report that prints numbers without these lines is not shorter, it is wrong.
"""
import logging

logger = logging.getLogger(__name__)

# The surfaces a Product 2 block sits on. Measured 2026-08-28 over the
# available 8-day window: / took 427 of 435 AI crawls (98%), /llms.txt 8, and
# /api/v1/dcpi/scores 0. The root domain is not one surface among three — it
# is essentially all of the reach.
SPONSOR_SURFACES = ["/", "/llms.txt", "/api/v1/dcpi/scores"]


def _row(sid, conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, slot, sponsor_name, link_url, status, impressions, "
            "       clicks, activated_at, week_of "
            "  FROM sponsorships WHERE id = %s", (int(sid),))
        r = cur.fetchone()
    if not r:
        return None
    return {"id": int(r[0]), "slot": r[1], "sponsor_name": r[2],
            "link_url": r[3], "status": r[4], "impressions": int(r[5] or 0),
            "clicks": int(r[6] or 0), "activated_at": str(r[7]) if r[7] else None,
            "week_of": str(r[8]) if r[8] else None}


def monthly_report(sponsorship_id, days=30, conn=None, brand_aliases=()) -> dict:
    """Everything an advertiser is owed for the period, with its limits.

    `ok` is False if the sponsorship row cannot be read. Individual sections
    degrade independently — a Cloudflare outage must not delete the impression
    figures — and each carries its own `limits`.
    """
    out = {"ok": False, "sponsorship": None, "window_days": int(days),
           "crawl": None, "delivery": None, "mentions": None, "limits": []}
    owned = conn is None
    if owned:
        try:
            from main import get_read_db
            conn = get_read_db()
        except Exception as e:
            out["limits"].append(f"database unavailable: {e}")
            return out
    if conn is None:
        out["limits"].append("database unavailable")
        return out
    try:
        row = _row(sponsorship_id, conn)
    except Exception as e:
        logger.warning("[sponsor_report] row read failed for %s: %s", sponsorship_id, e)
        out["limits"].append(f"sponsorship read failed: {e}")
        return out
    finally:
        if owned:
            try: conn.close()
            except Exception: pass
    if not row:
        out["limits"].append(f"no sponsorship with id {sponsorship_id}")
        return out
    out["sponsorship"] = row

    # ── 1. the crawl table, first ────────────────────────────────────
    try:
        from routes.sponsor_crawl import crawls_from_snapshots, engine_crawls
        crawl = crawls_from_snapshots(SPONSOR_SURFACES, days=days)
        # Snapshots are the only route to a full month, but on a fresh install
        # there are none. Fall back to the live 8-day window rather than
        # printing an empty table, and label which one produced the numbers.
        if not crawl.get("ok") or crawl.get("days_covered", 0) == 0:
            live = engine_crawls(SPONSOR_SURFACES, days=days)
            live["source"] = "cloudflare_live"
            live.setdefault("limits", []).append(
                "No daily snapshots yet, so this is the live Cloudflare window "
                "rather than the accumulated month.")
            crawl = live
        else:
            crawl["source"] = "daily_snapshots"
        out["crawl"] = crawl
    except Exception as e:
        logger.warning("[sponsor_report] crawl section failed: %s", e)
        out["crawl"] = {"ok": False, "limits": [f"crawl table unavailable: {e}"]}

    # ── 2. delivery: what we stamped, and what we could not ──────────
    out["delivery"] = {
        "impressions_stamped": row["impressions"],
        "clicks_counted": row["clicks"],
        "limits": [
            "Impressions are stamped at RENDER, on origin reads only. "
            "/api/v1/dcpi/scores is served with max-age=120 and sits behind a "
            "Cloudflare cache rule, so edge-served reads are not counted. This "
            "figure under-states.",
            "The root domain contributes ZERO impressions by construction: the "
            "block is baked into a static Cloudflare Pages asset at build time, "
            "so a reader never reaches our origin. Root-domain reach is the "
            "crawl table above, not this number.",
            "Clicks that could not be written are FORWARDED and dropped, never "
            "replayed — a write that may already have committed must not be "
            "billed twice. Clicks under-state during any database incident.",
        ],
    }

    # ── 3. brand mentions in observed AI answers ─────────────────────
    try:
        from routes.sponsor_mentions import brand_mentions
        out["mentions"] = brand_mentions(row["sponsor_name"],
                                         aliases=brand_aliases, days=days)
    except Exception as e:
        logger.warning("[sponsor_report] mention section failed: %s", e)
        out["mentions"] = {"ok": False, "limits": [f"mention scan unavailable: {e}"]}

    out["limits"].append(
        "Every figure in this report under-states rather than over-states. The "
        "reasons are listed per section. We would rather invoice you for less "
        "than we delivered than for more.")
    out["ok"] = True
    return out


def render_text(rep) -> str:
    """The report as plain text. Sections that failed say so; they never
    silently render as zero."""
    if not rep.get("ok"):
        return "Report unavailable:\n" + "\n".join(
            f"  - {l}" for l in rep.get("limits") or ["unknown error"])
    s = rep["sponsorship"]
    L = [f"DC Hub sponsorship report — {s['sponsor_name']}",
         f"Slot: {s['slot']}   Status: {s['status']}   "
         f"Activated: {s['activated_at']}",
         f"Window: trailing {rep['window_days']} days", ""]

    c = rep.get("crawl") or {}
    L.append("AI ENGINE CRAWLS OF YOUR SPONSORED SURFACES")
    L.append("-" * 52)
    if c.get("ok"):
        cov = c.get("days_covered")
        L.append(f"  source: {c.get('source')}"
                 + (f"   days covered: {cov}" if cov is not None else "")
                 + f"   window: {c.get('window_days')}d")
        for eng, n in (c.get("by_engine") or {}).items():
            L.append(f"  {eng:<34} {n:>7}")
        L.append(f"  {'TOTAL':<34} {c.get('total_ai_crawls', 0):>7}")
    else:
        L.append("  UNAVAILABLE — not zero. See limits below.")
    for l in c.get("limits") or []:
        L.append(f"  * {l}")
    L.append("")

    d = rep.get("delivery") or {}
    L.append("DELIVERY")
    L.append("-" * 52)
    L.append(f"  impressions stamped {d.get('impressions_stamped', 0):>7}")
    L.append(f"  clicks counted      {d.get('clicks_counted', 0):>7}")
    for l in d.get("limits") or []:
        L.append(f"  * {l}")
    L.append("")

    m = rep.get("mentions") or {}
    L.append("YOUR BRAND IN AI ANSWERS WE OBSERVED")
    L.append("-" * 52)
    if m.get("ok"):
        L.append(f"  {m.get('mentions', 0)} mentions across "
                 f"{m.get('sampled_answers', 0)} answers we elicited")
        pw = m.get("prior_window") or {}
        L.append(f"  prior window: {pw.get('mentions')} of "
                 f"{pw.get('sampled_answers')}")
        L.append(f"  of these, answers that also cited DC Hub: "
                 f"{m.get('alongside_dchub', 0)}")
        for e, n in (m.get("by_engine") or {}).items():
            L.append(f"    {e:<20} {n:>5}")
    else:
        L.append("  UNAVAILABLE — not zero. See limits below.")
    for l in (m.get("ambiguous") or []) + (m.get("limits") or []):
        L.append(f"  * {l}")
    L.append("")
    for l in rep.get("limits") or []:
        L.append(f"* {l}")
    return "\n".join(L)
