"""Phase 268 — public /freshness surface.

Why this exists
---------------
The internal /heartbeat ops dashboard exists, but no *public* page proves
that dchub.cloud refreshes itself faster than DC Hawk / DC Byte /
datacenters.com (which the disruption audit confirmed don't even have
freshness signals — DCH has no AI surface, DCB's "MCP" is a WordPress 404,
datacenters.com rate-limits LLM crawlers entirely).

This module ships:

  • GET /freshness            — public HTML pitch page with live freshness
                                stats, intended for journalists / LLMs /
                                competitive deck citations.
  • GET /api/v1/freshness     — JSON companion. CORS '*' so anyone can poll.

Both pull from the same data the internal heartbeat already maintains:
the `freshness_checks` rows + DCPI quality summary.

Read-only. No writes. No auth. Heavy CDN caching is intentional (60s).

Phase GG (2026-05-14): fixed a silent bug — this module queried a table
named `heartbeat_surfaces` that NO code in the repo ever created or
wrote to (the real table heartbeat.py maintains is `freshness_checks`).
So `_surfaces_snapshot()` always returned [] + an error and the public
freshness page — the "proof we don't go stale" pitch page — was itself
empty. Repointed to the real table.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone, timedelta
from html import escape as _h
from flask import Blueprint, jsonify, Response

# phase 270 hardening: only these status values map to a CSS class; anything
# else gets rendered as "unknown". Defense-in-depth even though the DB writers
# today only produce these three.
_STATUS_WHITELIST = {"fresh", "stale", "unknown"}

freshness_public_bp = Blueprint("freshness_public", __name__)


def _conn():
    import psycopg2
    return psycopg2.connect(os.environ.get("DATABASE_URL"))


def _surfaces_snapshot():
    """Return list of surfaces with status (fresh/stale/unknown) + age."""
    rows = []
    try:
        import psycopg2.extras
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT surface,
                       last_updated,
                       stale_after_hours,
                       last_refresh_info
                FROM freshness_checks
                ORDER BY last_updated DESC NULLS LAST
            """)
            rows = cur.fetchall()
    except Exception as e:
        return [], str(e)
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        lu = r.get("last_updated")
        if lu and getattr(lu, "tzinfo", None) is None:
            lu = lu.replace(tzinfo=timezone.utc)
        age_h = ((now - lu).total_seconds() / 3600.0) if lu else None
        stale_after = r.get("stale_after_hours") or 24
        status = "unknown" if age_h is None else ("fresh" if age_h <= stale_after else "stale")
        out.append({
            "surface": r["surface"],
            "last_updated": lu.isoformat() if lu else None,
            "age_hours": round(age_h, 2) if age_h is not None else None,
            "stale_after_hours": stale_after,
            "status": status,
            "info": r.get("last_refresh_info"),
        })
    return out, None


def _dcpi_summary():
    """Last DCPI computed_at + total published markets."""
    try:
        import psycopg2.extras
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT COUNT(*) AS published, MAX(computed_at) AS last_computed
                FROM (
                  SELECT DISTINCT ON (market_slug) market_slug, computed_at
                  FROM market_power_scores WHERE published = true
                  ORDER BY market_slug, computed_at DESC
                ) t
            """)
            r = cur.fetchone() or {}
        last = r.get("last_computed")
        if last and getattr(last, "tzinfo", None) is None:
            last = last.replace(tzinfo=timezone.utc)
        age_min = ((datetime.now(timezone.utc) - last).total_seconds() / 60.0) if last else None
        return {
            "published_markets": int(r.get("published") or 0),
            "last_computed_at": last.isoformat() if last else None,
            "age_minutes": round(age_min, 1) if age_min is not None else None,
        }
    except Exception as e:
        return {"error": str(e)[:160]}


def _aggregate(surfaces):
    fresh = sum(1 for s in surfaces if s["status"] == "fresh")
    stale = sum(1 for s in surfaces if s["status"] == "stale")
    unknown = sum(1 for s in surfaces if s["status"] == "unknown")
    last_24h = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    for s in surfaces:
        if s["last_updated"]:
            try:
                if datetime.fromisoformat(s["last_updated"].replace("Z", "+00:00")) >= cutoff:
                    last_24h += 1
            except Exception:
                pass
    return {"fresh": fresh, "stale": stale, "unknown": unknown,
            "total_surfaces": len(surfaces), "refreshed_last_24h": last_24h}


# Phase 296 (Phase O): per-domain SLA targets. Each data domain has a
# documented refresh target. Surfaces are grouped by domain; SLA-compliance
# is computed against the worst (oldest) surface in that domain.
#
# Hour values are tuned to actual upstream availability:
#   - ISO grid: 1h (real-time LMP feeds)
#   - Power retail rates: 168h (EIA monthly, ~lags 30-60d but pulled weekly)
#   - DCPI: 24h (daily 06:00 UTC recompute, plus emergency triggers)
#   - News: 1h (60+ source RSS poll)
#   - M&A: 24h (manual + scraped daily)
#   - Pipeline: 24h
#   - Renewables: 168h (NREL slow data)
#   - Gas: 24h
DOMAIN_SLA_HOURS = {
    # Phase YY (2026-05-17): right-size aspirational SLAs to match the
    # actual upstream cadence. iso=1h was unrealistic — the data-pulse
    # cron runs every 15 min but several specific surfaces (supported-
    # isos, summary, fuel-mix-live) update on EIA/gridstatus's hourly
    # cadence with their own lag. Anything stricter than upstream's
    # publish window guarantees a permanent breach finding that's just
    # noise. news=1h was the same trap — RSS feeds update hourly at
    # best, often every 2-4h. Use realistic operational targets:
    "iso":       4,      # /api/v1/grid/<iso>  (was 1h, raised to upstream LMP cadence)
    "power":     168,    # /api/v1/energy/electricity-rates
    "renewable": 168,    # /api/renewable/*
    "dcpi":      30,     # /api/v1/dcpi/live-count — r36: 24→30. The dcpi_scores
                         # recompute runs on a DAILY (~24h) cadence, so a 24h SLA
                         # is mathematically guaranteed to flicker into breach in
                         # the window before each daily run (observed 25.4h). Per
                         # this file's own rule ("anything stricter than upstream's
                         # publish window is just noise"), give it cadence+jitter
                         # headroom. 30h still catches a genuine multi-day stall.
    "news":      6,      # /api/news/live  (was 1h, raised to RSS aggregation cadence)
    "press":     168,    # r36: press releases are EVENT-driven (>=15pt DCPI shifts);
                         # split out of the 6h news domain so a quiet press week
                         # isn't a breach (matches the press_releases table SLA in
                         # brain_consistency_radar). See also _DOMAIN_SOURCE below.
    # r-sweep-green (2026-07-18): 24h → 168h. M&A deal flow is ORGANIC — the
    # extractor only writes when the news cycle produces a deal, and the live
    # ai_deals cadence shows 1-2 deals with gaps up to 5 days (7/09→7/14
    # observed). A 24h SLA false-breached every quiet stretch (sweep RED on
    # real_age=56h with a healthy extractor). 168h tolerates a quiet week;
    # a genuinely dead extractor still breaches within the window the radar's
    # hyperscaler_deals-empty drift check would also catch it.
    "mna":       168,    # /api/v1/deals — event-driven organic deal flow
    # r71-stabilize (2026-06-04): the 3 SLOW/STATIC infra domains below were on
    # a 24h SLA, but their real upstream cadence is weeks/months (gas+pipeline =
    # HIFLD static imports; facilities = bursty discovery). With no real-age
    # source mapped (see _DOMAIN_SOURCE's r36 note explaining WHY mapping them to
    # a 24h SLA would false-breach), they judged on surface-stamp age — which
    # drifts to 100h+ and tripped a permanent FALSE "data_freshness" RED on the
    # surveillance sweep. The DATA is not stale (facilities just grew to 21k via
    # OSM); the SLA was simply wrong for the cadence. Right-sized to match the
    # documented brain_consistency_radar.SLAS (gas_pipelines 720h, facilities
    # 336h) so freshness_public and the brain radar agree.
    "pipeline":  720,    # /api/v1/pipeline — HIFLD static pipeline infra
    "fiber":     168,    # /api/v1/connectivity/*
    "gas":       720,    # /api/v1/energy/gas-* — gas_pipelines HIFLD static-import
    "facilities": 336,   # /api/v1/facilities — bursty discovery (brain rates 336h)
}


def _domain_of(surface_name: str) -> str:
    """Map a surface name to one of the SLA domains.

    Phase YY-2 (2026-05-17): EXCLUDE agent-discovery surfaces
    (`/ai/learn/*`, `/api/agent/*`, `/ai/schema/*`, `/api/_diagnose/*`)
    from data-freshness SLAs. These track 'last-pinged-by-an-agent'
    timestamps, not underlying data age. Misclassifying them was the
    root cause of Phase TT's persistent ISO/news 'breach' findings
    (worst was always 29-35h because those endpoints get hit by AI
    crawlers on their own cadence — has nothing to do with our cron).
    Real data-source surfaces (e.g. /api/v1/grid/<iso>) are still
    monitored at their proper SLA.
    """
    s = (surface_name or "").lower()
    # ── Agent-discovery / learning surfaces — measured as 'other'
    # (no SLA reported) so they don't pollute data-source breach signal.
    if (s.startswith("/ai/learn") or s.startswith("/ai/schema")
            or s.startswith("/api/agent") or s.startswith("/api/_diagnose")):
        return "other"
    # Phase r34 (2026-05-31): EXCLUDE operational/internal surfaces for the
    # same reason. admin dashboards, ingest writers, CSV exports and draft
    # generators are hit on their own (or no) cadence — their "age" tracks
    # the last admin/ingest action, NOT user-facing data freshness. They were
    # dragging iso/news/dcpi/gas/facilities/mna/pipeline into perpetual breach
    # (all ~155-168h = "last touched a week ago", while the real public feeds
    # — dcpi 42min, fiber/power/renewable current — are fine). Demote to 'other'.
    _OPS_MARKERS = ("/admin/", "/ingest", "/export", "/draft-", "/draft/",
                    "ner/status", "/recompute", "/backfill", "/dedup",
                    "/probe/", "/import", "/upload", "/sync",
                    # r34b: more ops verbs that were left dragging domains into
                    # breach (iso/aeso/extract, press/scan, press/queue) — these
                    # are pipeline/worker actions, not user-facing data feeds.
                    "/extract", "/scan", "/queue",
                    # r36 (2026-05-31): the last 2 breaching domains (iso, dcpi)
                    # were dragged by non-data surfaces the markers above just
                    # missed. "-export" catches /api/v1/reveal-grid-export and
                    # its /status/<job_id> (the "/export" marker only matched a
                    # leading-slash form, not the hyphenated one). "/search/"
                    # catches /api/v1/search/grids — a query endpoint, not a
                    # cadenced feed (its freshness IS the underlying data's,
                    # tracked elsewhere). Both were classed "iso" via "grid" in
                    # the path and sat at ~160h (noop_default, never re-stamped).
                    "-export", "/search/")
    if any(m in s for m in _OPS_MARKERS):
        return "other"
    # r36 (2026-05-31): static snapshot exports under /data/ (e.g.
    # /data/dcpi-current.json, /data/dcpi-history.csv) have NO re-stamp hook —
    # their freshness row tracks REGISTRATION time (~160h), not the file's real
    # content age (the files carry no date field), so they sat perpetually
    # breaching dcpi's SLA. They're static exports, not live feeds — demote to
    # 'other' like the ops surfaces. The live endpoints that generate this data
    # stay tracked under their own domain.
    if s.startswith("/data/") and (s.endswith(".csv") or s.endswith(".json")):
        return "other"
    if "grid" in s or "iso" in s: return "iso"
    if "renewable" in s or "solar" in s or "wind" in s: return "renewable"
    if "rate" in s or "energy" in s and "gas" not in s: return "power"
    if "dcpi" in s: return "dcpi"
    if "press" in s: return "press"   # r36: event-driven; before news so press
                                       # surfaces aren't held to the 6h news SLA
    if "news" in s: return "news"
    if "deal" in s or "transaction" in s or "m&a" in s: return "mna"
    if "pipeline" in s and "gas" not in s: return "pipeline"
    if "fiber" in s or "ix" in s or "connectivity" in s: return "fiber"
    if "gas" in s: return "gas"
    if "facility" in s or "facilities" in s: return "facilities"
    return "other"


# ── r36 (2026-05-31): real-data-age override ────────────────────────────────
# Root cause of the recurring iso/news freshness whack-a-mole: the per-surface
# `freshness_checks.last_updated` DRIFTS from real data age. The heartbeat
# re-stamp loop only re-stamps a surface once it's stale per its OWN window
# (e.g. iso=12h), but the domain SLA is tighter (iso=4h) — so a surface showed
# 9h "stale" while grid_data was actually <1.5h fresh (the ISO cron is healthy).
# We were chasing individual surfaces forever.
#
# Durable fix: for every domain backed by a real source table, judge the breach
# on the TABLE's actual freshness (NOW - MAX(ts)) queried at check-time, not on
# the drifting surface tracking. Cached _DOMAIN_AGE_TTL seconds so /api/v1/
# freshness stays cheap on the single Railway replica. This mirrors the proven
# table-based approach in brain_consistency_radar.SLAS.
_DOMAIN_SOURCE: dict = {
    # domain -> (table, timestamp_column, stream_column | None).
    # MAX(col)::timestamptz is cast on the MAX result (index-friendly), tolerant
    # of TEXT ISO-8601 columns.
    #
    # ── r-worst-is-worst (2026-08-08) ────────────────────────────────────────
    # The third element is the STREAM column, and it exists because this map
    # previously published a lie. A single MAX(ts) over a whole table is the
    # FRESHEST row in it, and _sla_breakdown published that number as
    # `worst_age_hours`. For grid_data — one row per (iso, timestamp, metric) —
    # that meant one live ISO made the whole `iso` domain read green no matter
    # how many other ISO feeds had stopped. Measured live 2026-08-08T03:15Z:
    #
    #   "iso": {"worst_age_hours": 0.11,            <- the FRESHEST row
    #           "surface_worst_age_hours": 1653.93,
    #           "worst_surface": "/sitemap-grids.xml",   <- a different object
    #           "status": "within_sla"}
    #
    # Where a stream column is set, the age is now the OLDEST of the per-stream
    # latest timestamps — the actual worst. Where it is None the table is
    # append-only (news, press, deals) and has no per-stream notion of worst:
    # "when did anything last arrive" IS the right measure there, and the
    # response says so in `real_data_age_basis` rather than calling it "worst".
    #
    # SCOPED to the FAST, genuinely-refreshing, drift-prone domains — the ones
    # that actually whack-a-mole'd (iso/news). r36 note: do NOT map slow/static
    # INFRASTRUCTURE domains here (gas/pipeline → gas_pipelines.updated_at is a
    # HIFLD static-import date ~62d old; facilities → first_seen is slow
    # discovery the brain rates at 336h). Mapping those to a tight 24h domain
    # SLA turns a real-age read into a FALSE breach. They keep surface tracking
    # (within_sla today) and their real cadence is already watched by
    # brain_consistency_radar.SLAS (gas_pipelines 720h, facilities 336h).
    # grid_data is 7+ INDEPENDENT ISO feeds in one table. Each must be fresh on
    # its own, so the domain is judged on the worst of them.
    "iso":        ("grid_data",      "timestamp", "iso"),   # ISO telemetry, ~1.5h cron
    "dcpi":       ("market_power_scores", "computed_at", None),  # r71-stabilize: was
                  # "dcpi_scores" — NO SUCH TABLE, so the real-age query silently
                  # failed → fell back to drifting surface age → permanent false
                  # breach. The real DCPI scores live in market_power_scores
                  # (see _dcpi_summary); MAX(computed_at) ~= the daily recompute (~1.7h).
    "news":       ("news_articles",  "published_at", None),   # live RSS table served by /api/news/live (news_items is a phantom/variant — caused a permanent false SLA breach)
    "press":      ("press_releases", "published_at", None),   # event-driven
    # ★ 2026-08-10: was "ai_deals" — a REAL table, but the wrong one, and the
    # third instance of the phantom/variant class already fixed for dcpi and
    # news above. The live scraper writes `deals`; `ai_deals` was last written
    # 2026-07-26 and the product does not read it. Measured the same day:
    #     deals     4,893 rows · newest 2026-08-10 19:40 · +98 in 7d
    #     ai_deals    862 rows · newest 2026-07-26        ·  +0 in 7d
    # and POST /api/v1/transactions/ingest reports
    # entrypoint=deal_scraper.run_scrape with db_stats.total_deals=4893, i.e.
    # the working pipeline counts `deals`. Every public M&A surface reads
    # `deals` too (routes/transactions_browser.py, routes/hyperscaler_brief.py).
    # So the domain sat in permanent breach describing a table nothing feeds and
    # nothing serves, holding the surveillance sweep RED every 15 minutes while
    # M&A data was arriving normally.
    "mna":        ("deals",          "created_at", None),     # deal extractor
    # r-sweep-green (2026-07-18): power + facilities joined the real-age map.
    # The r36 objection above ("do NOT map slow domains") dated from when their
    # domain SLAs were a tight 24h; the SLAs were later right-sized (power 168h,
    # facilities 336h), but the mapping never followed — so both domains kept
    # judging on drifting surface stamps and the surveillance sweep sat
    # permanently RED on junk: facilities "breached" on a 48-day-old
    # /sitemap-facilities.xml STAMP while facilities.last_updated was 159h
    # (within 336h); power "breached" on a dormant powered-shell surface while
    # the EIA feed was 17.5h fresh (within 168h). Verified live 2026-07-18.
    "power":      ("eia_electricity_rates", "retrieved_at", None),  # EIA ingest, ~daily-weekly
    "facilities": ("facilities",     "last_updated", None),   # bursty discovery, 336h SLA
}

# What each domain's real-age number actually measures, published verbatim so a
# reader never has to infer it from a field name.
_STREAM_BASIS = ("worst of {n} independent {stream} streams in {table} — the "
                 "OLDEST per-stream latest {col}, so one live stream cannot "
                 "mask a dead one")
_SINGLE_BASIS = ("most recent {col} in {table}. This table is append-only and "
                 "has no independent per-stream feeds, so 'when did anything "
                 "last arrive' IS the freshness measure — it is NOT a worst-case "
                 "across sub-feeds")
_DOMAIN_AGE_CACHE: dict = {"data": {}, "t": 0.0}
_DOMAIN_AGE_TTL = 300  # 5 min


# ★ 2026-08-10: streams whose UPSTREAM publishes irregularly by nature. Not an
# excuse list — each entry must be annotated as such at its source. EU_BG is
# tagged `INTERMITTENT` in routes/iso_eu_entsoe.py's own zone registry
# ("10YCA-BULGARIA-R", r-eu-expand 2026-06-25).
#
# Why this exists: the iso domain is judged on the WORST of 109 streams, so one
# irregular publisher pins the whole domain — and with it the surveillance
# sweep — permanently red. Measured 2026-08-10: EU_BG 8h37m stale against a 4h
# target while the other 108 streams, including every sibling ENTSO-E zone, had
# updated 9 minutes earlier. Nothing was broken; Bulgaria just had not published.
#
# A LONGER LEASH, NOT IMMUNITY. An intermittent stream still breaches once it
# passes _INTERMITTENT_MAX_H — otherwise this list would be a place to hide a
# genuinely dead feed, which is the failure mode the worst-stream rule exists to
# prevent. Their ages are always reported, never dropped.
_INTERMITTENT_STREAMS: dict = {
    # ★ 2026-09-03 — FOUR MORE, EACH MEASURED, NOT ASSUMED. The squasher board
    #   carried one `iso_metric_count_zero_24h` row per EU zone for weeks. When
    #   the ENTSO-E outage cleared, 27 of the 31 zones resumed and their rows
    #   became ghosts — but EU_DK_1, EU_DK_2, EU_GR and EU_IE_SEM did not, and
    #   they are not broken either. Probed live via /api/v1/iso/eu/debug:
    #
    #     zone       http  document                       parsed
    #     EU_DK_1    200   Acknowledgement_MarketDocument None
    #     EU_DK_2    200   Acknowledgement_MarketDocument None
    #     EU_GR      200   Acknowledgement_MarketDocument None
    #     EU_IE_SEM  200   Acknowledgement_MarketDocument None
    #     EU_BE      200   GL_MarketDocument              {fuels: {...}}   <- control
    #
    #   An Acknowledgement is ENTSO-E saying "nothing published for this query",
    #   which is exactly the EU_BG shape this list was created for: the EIC codes
    #   are correct and the fetch works: these four zones do not publish A75
    #   Actual-Generation-per-Production-Type. Nothing here is broken, so nothing
    #   here is fixable by us — but at 24h they regenerated a finding every day
    #   forever, which is a detector that outlives its cause.
    #
    #   Still a leash and not immunity: past _INTERMITTENT_MAX_H they are judged
    #   like any other stream, so a zone that genuinely dies is still caught.
    "grid_data": frozenset({"EU_BG", "EU_DK_1", "EU_DK_2", "EU_GR", "EU_IE_SEM"}),
}
_INTERMITTENT_MAX_H = 168.0   # 7 days — past this, even "irregular" means dead


# ── feed FAMILIES (D1, 2026-09-02) ───────────────────────────────────
# The iso domain names every stale STREAM — 34 EU_* rows during the 2026-09-01
# ENTSO-E outage — but nothing rolled them up to the FAMILY a reader acts on
# ("is the ENTSO-E feed alive?"), and /api/v1/iso/eu/health answered HTTP 200
# with live_feed_ok:false. One roll-up per producer, keyed to its health route
# and to the deadman feed data-pulse.yml beats, so a dead family is one line.
#
# live_feed_ok is judged on the family's ANCHOR stream — the aggregate row the
# producer writes on every successful fan-out — not on "no member is stale":
# an upstream-intermittent zone (EU_BG) would otherwise pin the family dead
# forever. Members past target are still counted and named.
_FEED_FAMILIES = (
    {"family": "entsoe", "anchor": "ENTSOE",
     "member": lambda s: s == "ENTSOE" or s.startswith("EU_"),
     "health": "/api/v1/iso/eu/health", "deadman_feed": "iso-eu-entsoe",
     "producer": "routes/iso_eu_entsoe.py via .github/workflows/data-pulse.yml"},
)


def summarize_feed_families(per_stream, target_hours, families=_FEED_FAMILIES):
    """PURE. Roll per-stream ages up to producer families. {} when no family
    has a stream in `per_stream` (a missing family is absent, never "ok")."""
    ages = {}
    for s in per_stream or []:
        if s.get("age_hours") is None:
            continue
        ages[str(s.get("stream"))] = float(s["age_hours"])
    out = {}
    for fam in families:
        members = {n: a for n, a in ages.items() if fam["member"](n)}
        if not members:
            continue
        stale = sorted(((n, a) for n, a in members.items() if a > target_hours),
                       key=lambda t: -t[1])
        anchor_age = members.get(fam["anchor"])
        if anchor_age is not None:
            live = anchor_age <= target_hours
            basis = ("anchor stream %s is %.2fh old against a %sh target"
                     % (fam["anchor"], anchor_age, target_hours))
        else:
            live = not stale
            basis = ("no anchor stream %s in grid_data — judged on members: %d of "
                     "%d past target" % (fam["anchor"], len(stale), len(members)))
        out[fam["family"]] = {
            "live_feed_ok": live,
            "live_feed_ok_basis": basis,
            "streams_total": len(members),
            "streams_stale": len(stale),
            "worst_age_hours": round(max(members.values()), 2),
            "stale_streams": [n for n, _ in stale],
            "health": fam["health"],
            "deadman_feed": fam["deadman_feed"],
            "producer": fam["producer"],
        }
    return out


def summarize_stream_ages(rows, *, table, col, stream, intermittent=None):
    """PURE. Collapse per-stream ages into the domain reading.

    `rows` are (stream_name, age_hours) as returned by the grouped query. The
    domain's age is the WORST — the largest age, i.e. the stream that went
    quiet longest ago — never the smallest. Rows with a null age are ignored
    (they cannot be judged), and their count is reported so they are not
    silently dropped.

    `intermittent` names streams whose upstream publishes irregularly. They are
    excluded from the WORST until they pass _INTERMITTENT_MAX_H, after which
    they are judged like any other. They are always listed in `per_stream` and
    summarised in `intermittent_streams`, so an excluded stream is visible.
    """
    rated = [(str(name), float(age)) for name, age in (rows or [])
             if age is not None]
    unrated = len(rows or []) - len(rated)
    if not rated:
        return None
    skip = set(intermittent or ())
    deferred = [(n, a) for n, a in rated
                if n in skip and a < _INTERMITTENT_MAX_H]
    judged = [(n, a) for n, a in rated if (n, a) not in deferred]
    # Never let the leash empty the judgement set — if every stream is
    # deferred there is nothing left to be right about, so judge them all.
    if not judged:
        judged, deferred = rated, []
    judged.sort(key=lambda t: -t[1])         # worst (oldest) first
    rated.sort(key=lambda t: -t[1])
    worst_name, worst_age = judged[0]
    out = {
        "age_hours": worst_age,             # THE WORST. Not the freshest.
        "freshest_age_hours": rated[-1][1],
        "worst_object": "%s:%s" % (table, worst_name),
        "streams_total": len(judged),
        "streams_unrated": unrated,
        "per_stream": [{"stream": n, "age_hours": round(a, 2)} for n, a in rated],
        "basis": _STREAM_BASIS.format(n=len(judged), stream=stream,
                                      table=table, col=col),
    }
    if deferred:
        # Reported, never hidden — and the basis says the number excluded them.
        out["intermittent_streams"] = [
            {"stream": n, "age_hours": round(a, 2)} for n, a in
            sorted(deferred, key=lambda t: -t[1])]
        out["basis"] += (
            " · excludes %d upstream-intermittent stream(s) (%s) until %.0fh, "
            "after which they are judged normally"
            % (len(deferred), ", ".join(n for n, _ in deferred),
               _INTERMITTENT_MAX_H))
    return out


def _refresh_domain_ages() -> dict:
    """One connection, one cheap query per data-backed domain. Returns
    {domain: {age_hours, basis, …}}. Per-table try/except+rollback so one
    bad/missing table can't break the rest.

    r-worst-is-worst (2026-08-08): a domain with a STREAM column is grouped by
    it and reduced with summarize_stream_ages, so the published age is the
    oldest stream rather than the freshest row in the table.
    """
    ages: dict = {}
    c = None
    try:
        # NB: psycopg2's `with conn` manages the TRANSACTION, not closing — so
        # we hold the handle explicitly and close it in finally (avoids the
        # connection leak A1 just fixed elsewhere). Read-only, so no commit.
        c = _conn()
        for dom, spec in _DOMAIN_SOURCE.items():
            table, col, stream = (tuple(spec) + (None,))[:3]
            try:
                with c.cursor() as cur:
                    if stream:
                        cur.execute(
                            f"SELECT {stream}, EXTRACT(EPOCH FROM "
                            f"(NOW() - MAX({col})::timestamptz))/3600.0 "
                            f"FROM {table} GROUP BY {stream}"
                        )
                        entry = summarize_stream_ages(
                            cur.fetchall(), table=table, col=col, stream=stream,
                            intermittent=_INTERMITTENT_STREAMS.get(table))
                        if entry:
                            ages[dom] = entry
                    else:
                        # ★ 2026-08-10: the age is taken from the newest row
                        # that has ACTUALLY HAPPENED. A future-dated row used to
                        # be MAX() outright, which broke the measure in both
                        # directions: first it SATISFIED the SLA with a record
                        # that does not exist yet (the -1145h news reading), and
                        # after the negative-age branch was added to catch that,
                        # it BREACHED instead — equally wrong.
                        #
                        # Measured live: news_articles held exactly ONE future
                        # row, "Data Center World POWER" dated 2026-09-21, a
                        # legitimate conference listing. It made the domain read
                        # -997.99h and held the surveillance sweep red, while
                        # 163 real articles had landed in the previous 24h and
                        # the newest was 20 minutes old.
                        #
                        # Excluding future rows makes both failure modes
                        # impossible; they are still counted and reported below,
                        # so a table filling up with future dates stays visible.
                        # Several of these columns are TEXT (news_articles.
                        # published_at, deals.created_at), so a blanket
                        # per-row ::timestamptz throws on any malformed value.
                        # Resolve the type once and guard the cast for text.
                        cur.execute(
                            "SELECT data_type FROM information_schema.columns "
                            "WHERE table_name=%s AND column_name=%s", (table, col))
                        _dt = (cur.fetchone() or [""])[0]
                        expr = (
                            f"CASE WHEN {col} ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' "
                            f"THEN {col}::timestamptz END"
                            if _dt == "text" else f"{col}::timestamptz")
                        cur.execute(
                            f"SELECT EXTRACT(EPOCH FROM (NOW() - "
                            f"  MAX({expr}) FILTER (WHERE {expr} <= NOW())"
                            f"))/3600.0, "
                            f"COUNT(*) FILTER (WHERE {expr} > NOW()) "
                            f"FROM {table}"
                        )
                        row = cur.fetchone() or (None, 0)
                        v = row[0]
                        if v is not None:
                            ages[dom] = {
                                "age_hours": float(v),
                                "worst_object": "%s (whole table)" % table,
                                "basis": _SINGLE_BASIS.format(col=col, table=table)
                                         + " — future-dated rows are excluded "
                                           "from the age and counted separately",
                            }
                            if row[1]:
                                ages[dom]["future_dated_rows"] = int(row[1])
            except Exception:
                try: c.rollback()
                except Exception: pass
    except Exception:
        pass
    finally:
        if c is not None:
            try: c.close()
            except Exception: pass
    return ages


def _real_domain_age(domain: str):
    """Cached real-data-age ENTRY for a domain (dict), or None if it has no
    source table / the query failed (falls back to surface tracking)."""
    if domain not in _DOMAIN_SOURCE:
        return None
    import time as _t
    now = _t.time()
    if (now - _DOMAIN_AGE_CACHE["t"]) >= _DOMAIN_AGE_TTL or not _DOMAIN_AGE_CACHE["data"]:
        _DOMAIN_AGE_CACHE["data"] = _refresh_domain_ages()
        _DOMAIN_AGE_CACHE["t"] = now
    return _DOMAIN_AGE_CACHE["data"].get(domain)


def _real_domain_age_hours(domain: str):
    """Cached real data age (hours) for a domain, or None. Kept as the scalar
    accessor; it now returns the WORST stream's age, not the freshest row's."""
    entry = _real_domain_age(domain)
    return entry.get("age_hours") if entry else None


def _sla_breakdown(surfaces):
    """Compute per-domain SLA compliance. Returns
    {domain: {target_h, worst_age_h, status, surfaces_n, worst_object}}.

    r-worst-is-worst (2026-08-08): `worst_age_hours` is now the worst — the
    oldest of a domain's independent streams — and `worst_object` names the
    thing it measured. Previously the number came from MAX(ts) over a whole
    table (the FRESHEST row) while the object beside it was the worst tracked
    surface, so the public verifier reported a green age against a red object.

    Phase TT (2026-05-17): also expose `worst_surface` so ops can see
    WHICH specific surface is dragging the domain into breach. Without
    this, the freshness endpoint reported 'iso: breach worst=26h' with
    no way to tell whether one dead ISO surface or all 55 are stale.
    """
    by_domain = {}
    for s in surfaces:
        d = _domain_of(s.get("surface", ""))
        by_domain.setdefault(d, []).append(s)
    out = {}
    for domain, ss in by_domain.items():
        target = DOMAIN_SLA_HOURS.get(domain)
        if target is None:
            continue  # 'other' bucket — don't report SLA
        # Sort by age desc so the head is the worst offender
        rated = [(s.get("age_hours"), s.get("surface", "?")) for s in ss
                 if s.get("age_hours") is not None]
        rated.sort(reverse=True)
        worst_age = rated[0][0] if rated else None
        worst_surface = rated[0][1] if rated else None
        # Phase TT (2026-05-17): show the top-3 stale surfaces in each
        # breaching domain so ops know which ones to investigate. List
        # is omitted when status is within_sla (no signal needed).
        stale_list = [{"surface": surf, "age_hours": round(age, 2)}
                       for age, surf in rated[:3] if age > target]
        # r36 (2026-05-31): judge the breach on REAL data age when the domain has
        # a source table — the per-surface tracking drifts (see _DOMAIN_SOURCE).
        # If grid_data is <1.5h fresh, iso is within_sla even though some iso
        # surface's last_updated row says 9h. Falls back to surface age when the
        # domain has no source / the query failed.
        real = _real_domain_age(domain)
        real_age = real.get("age_hours") if real else None
        effective_age = real_age if real_age is not None else worst_age
        # r-worst-is-worst (2026-08-08): name the object `worst_age_hours`
        # actually came from. It used to be paired with `worst_surface`, which
        # is the worst TRACKED SURFACE — a different object entirely, and
        # measured on a different clock. Live before this change, the iso row
        # read worst_age_hours=0.11 beside worst_surface=/sitemap-grids.xml
        # (whose own age was 1653.93h).
        worst_object = (real.get("worst_object") if real_age is not None and real
                        else worst_surface)
        if effective_age is None:
            status = "unknown"
        elif effective_age < 0:
            # ★ A NEGATIVE AGE IS NOT FRESHNESS — it is future-dated source data,
            # and `<= target` swallowed it as healthy. Measured live: the news
            # domain reported real_data_age_hours = -1145.71 with
            # status "within_sla", i.e. its SLA was being satisfied by a record
            # that does not exist yet (one RSS row carrying an EVENT date, 48
            # days ahead, which is also MAX(published_at)).
            #
            # Classified as a breach because the signal is not healthy and the
            # vocabulary already has a consumer (sla_breaches / sla_overall);
            # `future_dated_hours` says WHY, so nobody reads it as "stale".
            status = "breach"
        elif effective_age <= target:
            status = "within_sla"
        elif effective_age <= target * 2:
            status = "warning"  # 1-2x the SLA target
        else:
            status = "breach"   # >2x the SLA target
        entry = {
            "target_hours":         target,
            "worst_age_hours":      round(effective_age, 2) if effective_age is not None else None,
            "real_data_age_hours":  round(real_age, 2) if real_age is not None else None,
            # Present ONLY when the source is future-dated, so a breach caused by
            # bad data cannot be mistaken for a breach caused by staleness.
            **({"future_dated_hours": round(abs(effective_age), 2)}
               if effective_age is not None and effective_age < 0 else {}),
            # The object `worst_age_hours` describes — a "<table>:<stream>" when
            # the real-data read wins, else the tracked surface. `worst_surface`
            # is kept as its long-standing name but now carries the SAME object,
            # so the two can never again describe different things.
            "worst_object":         worst_object,
            "worst_surface":        worst_object,
            "real_data_age_basis":  (real or {}).get("basis"),
            # The surface-tracking view, explicitly labelled as such. These two
            # always pair with each other, never with worst_age_hours.
            "surface_worst_age_hours": round(worst_age, 2) if worst_age is not None else None,
            "worst_tracked_surface":   worst_surface,
            "status":               status,
            "surfaces":             len(ss),
        }
        # Per-stream detail for a multi-stream domain: how many feeds there are,
        # and every one past target. A dead feed is now named, not averaged away.
        if real and real.get("streams_total"):
            entry["streams_total"] = real["streams_total"]
            if real.get("streams_unrated"):
                entry["streams_unrated"] = real["streams_unrated"]
            # ★ 2026-08-10 follow-up: `intermittent_streams` was computed by
            # summarize_stream_ages but never copied here, so the field existed
            # in the summariser and vanished from /api/v1/freshness. Caught by
            # reading the live payload after deploy: the basis string said
            # "excludes 1 upstream-intermittent stream" while the structured
            # field the exclusion is supposed to be auditable through was
            # absent. An exclusion you cannot enumerate is a silent one — the
            # precise failure this whole exclusion mechanism was built to avoid.
            if real.get("intermittent_streams"):
                entry["intermittent_streams"] = real["intermittent_streams"]
            _breaching = [s for s in real.get("per_stream", [])
                          if s["age_hours"] > target]
            entry["streams_within_sla"] = real["streams_total"] - len(_breaching)
            if _breaching:
                entry["stale_streams"] = _breaching
            # D1 (2026-09-02): family roll-up — "is ENTSO-E alive?" as one
            # line beside the 34 zone streams it explains.
            _fams = summarize_feed_families(real.get("per_stream", []), target)
            if _fams:
                entry["feed_families"] = _fams
        # Only surface the stale-surface list when the REAL data is actually
        # behind (status warning/breach) — otherwise it's just tracking drift.
        if status in ("warning", "breach") and stale_list:
            entry["stale_surfaces"] = stale_list
        out[domain] = entry
    return out


@freshness_public_bp.route("/api/v1/freshness", methods=["GET"])
def api_freshness():
    """JSON freshness snapshot. CORS '*'. Cache 60s."""
    surfaces, err = _surfaces_snapshot()
    # Phase 296 (Phase O): per-domain SLA breakdown — turns the raw surface
    # list into "is each data domain meeting its refresh target?" — same
    # signal a status-page would expose. Used by /freshness HTML and by AI
    # agents to decide whether to trust the data.
    sla = _sla_breakdown(surfaces)
    breaches = [d for d, info in sla.items() if info.get("status") == "breach"]
    body = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "summary": _aggregate(surfaces),
        "dcpi": _dcpi_summary(),
        "sla_by_domain": sla,                     # phase 296
        "sla_breaches": breaches,                 # phase 296
        "sla_overall": "all_within_sla" if not breaches
                       else f"{len(breaches)}_domains_breached",
        "surfaces": surfaces,
        "citation": "DC Hub freshness signal — public proof-of-self-heal. https://dchub.cloud/freshness",
    }
    if err:
        body["surfaces_error"] = err
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=60, must-revalidate"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


_FRESHNESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DC Hub · Freshness — live proof of self-healing data</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Live freshness signal for DC Hub. {{fresh}} of {{total}} data surfaces refreshed in the last 24 hours. DCPI recomputed {{dcpi_age}} ago.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://dchub.cloud/freshness">
<meta property="og:title" content="DC Hub · Live Data Freshness">
<meta property="og:description" content="{{fresh}} of {{total}} data surfaces refreshed in last 24h. DCPI: {{dcpi_age}} ago.">
<meta property="og:url" content="https://dchub.cloud/freshness">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "DataFeed",
  "name": "DC Hub freshness feed",
  "description": "Live freshness signal across all DC Hub data surfaces. {{fresh}} of {{total}} surfaces refreshed in the last 24h.",
  "url": "https://dchub.cloud/freshness",
  "isAccessibleForFree": true,
  "publisher": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
  "distribution": {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": "https://dchub.cloud/api/v1/freshness"}
}
</script>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dchub-brand.css">
<script src="/js/dchub-nav.js" defer></script>
<style>
:root{--bg:#0a0a0f;--bg2:#0f1119;--card:#131319;--bd:rgba(255,255,255,.08);--tx:#fafafa;--tx2:#a1a1aa;--tx3:#71717a;--green:#10b981;--red:#ef4444;--orange:#f59e0b;--acc:#6366f1;--gradient:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);}
*{box-sizing:border-box}
body{font-family:'Instrument Sans',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--tx);margin:0;line-height:1.55;-webkit-font-smoothing:antialiased;}
.wrap{max-width:1100px;margin:0 auto;padding:3rem 1.5rem;}
.eyebrow{font-family:'JetBrains Mono',monospace;font-size:0.75rem;color:var(--acc);text-transform:uppercase;letter-spacing:0.12em;margin-bottom:0.6rem;}
h1{font-size:2.6rem;margin:0 0 0.6rem;font-weight:800;letter-spacing:-0.025em;line-height:1.1;}
h1 .live{display:inline-block;width:14px;height:14px;background:var(--green);border-radius:50%;margin-right:0.6rem;animation:pulse 1.4s ease-in-out infinite;vertical-align:middle;}
@keyframes pulse{50%{opacity:0.3;transform:scale(0.85);}}
.lede{color:var(--tx2);font-size:1.1rem;max-width:760px;margin:0 0 2.4rem;}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem;margin:2rem 0 2.6rem;}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1.3rem 1.4rem;}
.kpi .v{font-family:'JetBrains Mono',monospace;font-size:2.1rem;font-weight:800;line-height:1;}
.kpi .v.green{color:var(--green);}.kpi .v.red{color:var(--red);}.kpi .v.orange{color:var(--orange);}
.kpi .l{color:var(--tx2);font-size:0.78rem;margin-top:0.55rem;text-transform:uppercase;letter-spacing:0.08em;}
.kpi .sub{color:var(--tx3);font-size:0.78rem;margin-top:0.35rem;}
.section-title{font-size:1.15rem;font-weight:700;margin:2.4rem 0 1rem;letter-spacing:-0.01em;}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden;font-size:0.9rem;}
th,td{text-align:left;padding:0.7rem 1rem;border-bottom:1px solid var(--bd);}
th{background:var(--bg2);color:var(--tx2);font-weight:600;font-size:0.74rem;text-transform:uppercase;letter-spacing:0.08em;}
tr:last-child td{border-bottom:none;}
td.mono{font-family:'JetBrains Mono',monospace;font-size:0.86rem;}
.status{display:inline-block;padding:2px 8px;border-radius:99px;font-size:0.7rem;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;}
.status.fresh{background:rgba(16,185,129,0.15);color:var(--green);}
.status.stale{background:rgba(239,68,68,0.15);color:var(--red);}
.status.unknown{background:rgba(156,163,175,0.15);color:var(--tx2);}
.cite{margin-top:3rem;padding:1.2rem 1.4rem;background:var(--bg2);border:1px solid var(--bd);border-radius:10px;color:var(--tx2);font-size:0.88rem;}
.cite code{background:#11121a;padding:2px 6px;border-radius:4px;color:var(--tx);font-family:'JetBrains Mono',monospace;font-size:0.84rem;}
a{color:var(--acc);text-decoration:none;border-bottom:1px dotted rgba(99,102,241,0.5);}
a:hover{color:#fff;border-bottom-color:#fff;}
.foot{margin-top:3rem;color:var(--tx3);font-size:0.8rem;}
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow">Live · proof of self-heal</div>
  <h1><span class="live"></span>Data freshness, in public</h1>
  <p class="lede">
    DC Hub's data is recomputed and self-healed continuously. Every public surface
    on this site reports its own freshness here, in real time. Compare against any
    other data-center intelligence source — most don't publish this at all.
  </p>

  <div class="kpis">
    <div class="kpi"><div class="v green">{{refreshed_24h}}</div><div class="l">Refreshed in last 24h</div><div class="sub">across {{total}} surfaces</div></div>
    <div class="kpi"><div class="v {{dcpi_class}}">{{dcpi_age}}</div><div class="l">DCPI last computed</div><div class="sub">{{dcpi_published}} markets published</div></div>
    <div class="kpi"><div class="v {{fresh_class}}">{{fresh}}/{{total}}</div><div class="l">Currently fresh</div><div class="sub">{{stale}} stale · {{unknown}} unknown</div></div>
    <div class="kpi"><div class="v">JSON</div><div class="l">Machine-readable</div><div class="sub"><a href="/api/v1/freshness">/api/v1/freshness</a></div></div>
  </div>

  <div class="section-title">Per-surface freshness</div>
  <table>
    <thead><tr><th>Surface</th><th>Status</th><th>Age</th><th>Stale after</th><th>Last note</th></tr></thead>
    <tbody>
{{rows_html}}
    </tbody>
  </table>

  <div class="cite">
    <strong>Cite this signal:</strong>
    <code>DC Hub freshness — https://dchub.cloud/freshness</code><br>
    Machine surface: <code>GET https://dchub.cloud/api/v1/freshness</code> (CORS open, 60s cache).<br>
    Methodology: <a href="/dcpi#methodology">/dcpi#methodology</a> · <a href="/audit/">site audit</a>
  </div>

  <p class="foot">As of {{as_of}}. This page is rendered fresh on every load.
  Healer detection findings: <a href="/api/v1/heal/findings">/api/v1/heal/findings</a>.</p>
</div>
</body>
</html>"""


@freshness_public_bp.route("/freshness", methods=["GET"])
def freshness_page():
    surfaces, _err = _surfaces_snapshot()
    summary = _aggregate(surfaces)
    dcpi = _dcpi_summary()

    dcpi_age_min = dcpi.get("age_minutes")
    if dcpi_age_min is None:
        dcpi_age = "—"
        dcpi_class = "orange"
    elif dcpi_age_min < 60:
        dcpi_age = f"{int(dcpi_age_min)}m"
        dcpi_class = "green"
    elif dcpi_age_min < 1440:
        dcpi_age = f"{int(dcpi_age_min/60)}h"
        dcpi_class = "green" if dcpi_age_min < 360 else "orange"
    else:
        dcpi_age = f"{int(dcpi_age_min/1440)}d"
        dcpi_class = "red"

    fresh_class = "green" if summary["stale"] == 0 else ("orange" if summary["fresh"] > summary["stale"] else "red")

    # phase 270 hardening: HTML-escape every field that comes from the DB.
    # `status` is whitelisted to known values so it can't break out of the
    # CSS class attribute; everything else uses html.escape().
    rows = []
    for s in surfaces[:80]:
        age = "—" if s["age_hours"] is None else (f"{int(s['age_hours']*60)}m" if s["age_hours"] < 1 else f"{int(s['age_hours'])}h")
        info = (s["info"] or "")[:90]
        status = s["status"] if s["status"] in _STATUS_WHITELIST else "unknown"
        rows.append(
            f'<tr><td class="mono">{_h(str(s["surface"]))}</td>'
            f'<td><span class="status {status}">{status}</span></td>'
            f'<td class="mono">{_h(age)}</td>'
            f'<td class="mono">{int(s["stale_after_hours"] or 24)}h</td>'
            f'<td>{_h(info)}</td></tr>'
        )
    html = (_FRESHNESS_HTML
            .replace("{{refreshed_24h}}", str(summary["refreshed_last_24h"]))
            .replace("{{fresh}}", str(summary["fresh"]))
            .replace("{{stale}}", str(summary["stale"]))
            .replace("{{unknown}}", str(summary["unknown"]))
            .replace("{{total}}", str(summary["total_surfaces"]))
            .replace("{{dcpi_age}}", dcpi_age)
            .replace("{{dcpi_class}}", dcpi_class)
            .replace("{{dcpi_published}}", str(dcpi.get("published_markets", 0)))
            .replace("{{fresh_class}}", fresh_class)
            .replace("{{rows_html}}", "\n".join(rows))
            .replace("{{as_of}}", datetime.now(timezone.utc).isoformat()))
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=60, must-revalidate"
    return resp
