"""
facilities_by_dims.py — Phase r51 (2026-05-25); reconciled 2026-07-31.

Now carries ONE endpoint:

  GET /api/v1/stats/canonical

The module's original /api/v1/facilities/by-market and /by-provider handlers
were removed in the 2026-07-31 ghost-route reconcile: main.py registers the
same rules directly at import time, and Werkzeug serves the FIRST-registered
match, so the blueprint twins (registered later, at wiring time) never served
a byte — live traffic always got main.py's filter-less versions. The twins
also aggregated the legacy `facilities` table; the canonical handlers in
main.py aggregate `discovered_facilities` under the #1539 fleet filter
(COALESCE(is_duplicate,0)=0) and honor ?market= / ?provider=.

Do NOT re-add those paths here — tests/test_facilities_by_dims_canonical.py
fences one-registration-per-path.
"""
from __future__ import annotations

import os
from flask import Blueprint, jsonify

try:
    import psycopg2
    import psycopg2.extras
except Exception:
    psycopg2 = None


facilities_by_dims_bp = Blueprint("facilities_by_dims", __name__)


def _conn():
    if not psycopg2:
        return None
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=5)
    except Exception:
        return None


# r51-C: canonical facility count — single source of truth so the
# homepage, /daily, Gemini, and AI agents all see the SAME number.
# Currently: site says 21,400+, /daily shows 12,877, Gemini sees
# 10,700. This divergence is the user-reported "what is the truth"
# question. This endpoint resolves it from the live count.
@facilities_by_dims_bp.route("/api/v1/stats/canonical", methods=["GET"])
def stats_canonical():
    """Single authoritative count of facilities + key totals."""
    c = _conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c, c.cursor() as cur:
            stats: dict = {}
            # ★2026-08-09 ROGUE COUNT — `total_facilities` was COUNT(*) FROM
            # facilities, the LEGACY publication table (live 20,132), served
            # from the very endpoint whose stated purpose is making surfaces
            # agree, sitting three keys away from facilities_distinct = 17,294.
            # /api/v1/stats (main.py) already publishes total_facilities as the
            # canonical distinct count, so this endpoint was the one disagreeing.
            # js/live-count.js reads `facilities_distinct || total_facilities`,
            # so the fallback path was painting the legacy row count on the site.
            # total_facilities is now an ALIAS of facilities_distinct (set below,
            # after the canonical query runs); the legacy figure stays reachable
            # under a name that says which table it counts.
            from util.facility_canon_count import LEGACY_SQL as _LEGACY_SQL
            cur.execute(_LEGACY_SQL)
            stats["legacy_facilities_table_rows"] = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM facilities WHERE country IS NOT NULL AND country != ''")
            stats["facilities_with_country"] = int(cur.fetchone()[0] or 0)
            # ★2026-07-27: added `country != ''` — without it the empty-string
            # bucket counts as a country and this returned 181 while
            # /api/v1/stats (which has the guard) returned 180.
            # ★2026-07-30 — WRONG-TABLE PAIRING (same class as the 07-30
            # wrong-table audit; same fix as /api/v1/stats total_countries in
            # main.py): this counted DISTINCT country on the LEGACY `facilities`
            # table (186) while the citeable facility figure below
            # (`facilities_distinct`) counts discovered_facilities (15,363+).
            # The legacy table mixes full names with ISO codes ("USA"+"US",
            # "Germany"+"DE" — 9 of its 186 distinct values are format
            # duplicates), so 186 over-stated; the fleet the facility figures
            # count spans 178 distinct codes. Countries must be counted on the
            # SAME table as the facility count they are paired with.
            cur.execute("SELECT COUNT(DISTINCT country) FROM discovered_facilities "
                        "WHERE country IS NOT NULL AND country <> ''")
            stats["countries_covered"] = int(cur.fetchone()[0] or 0)
            # ★2026-08-30: this counted `news` — a DEAD table — and published
            # the result as `news_articles` on the endpoint whose stated
            # purpose is "canonical truth ... use this". Its only writer is
            # news_aggregator.py, which no workflow or cron invokes; see
            # tests/test_news_freshness_watches_live_table.py (2026-08-13),
            # written after four monitors read it and reported a WORKING
            # pipeline as stale.
            #
            # So a citable surface served a FROZEN count (3,503) under the live
            # table's name, ~4x below reality. Every other serving path already
            # agrees on `announcements`:
            #     /api/health   news_count      COUNT(*) FROM announcements
            #     /api/news     the feed itself FROM announcements
            #     ai_discovery_routes           derives from /api/health
            #     /api/v1/ai-agents.json        COUNT(*) FROM announcements
            # This was the last surface reading the dead one, and being the
            # CITABLE one made it the most quoted.
            #
            # ★ This is what made the ai-agents manifest look wrong on
            # 2026-08-30. It was "fixed" by pointing that manifest at `news` to
            # match this endpoint — matching the stale authority instead of
            # correcting it — which shipped a 4x under-claim to agents before
            # the freshness guard caught it. Fixing the authority is the change
            # that ends the class; the surfaces were never the problem.
            #
            # ★ VISIBLE NUMBER CHANGE, stated rather than buried: news_articles
            # goes 3,503 -> ~15,200 on a published, citable endpoint. That is a
            # correction, not growth. Nothing "grew" — the endpoint stopped
            # quoting an abandoned table.
            try:
                cur.execute("SELECT COUNT(*) FROM announcements")
                stats["news_articles"] = int(cur.fetchone()[0] or 0)
            except Exception:
                pass
            # ★2026-07-27: `deals_tracked` was COUNT(*) FROM deals — the RAW row
            # pile (4,484). The AUTO id embeds the ingest date
            # (AUTO-<yyyymmdd>-<hash>) so a re-ingest of the same deal never
            # conflicts and accrues one row per DAY; one atNorth deal held 945
            # rows. Publishing rows as "deals tracked" over-stated reality ~3.2x
            # — on the very endpoint whose stated purpose is making surfaces
            # agree. Now serves the deduped count (canonical_stats does the
            # AUTO-by-content-hash / tuple dedup and drops data_flag quarantine
            # rows); the raw row count stays available as `deals_rows`.
            try:
                cur.execute("SELECT COUNT(*) FROM deals")
                stats["deals_rows"] = int(cur.fetchone()[0] or 0)
            except Exception:
                pass
            try:
                from canonical_stats import get_canonical_stats as _gcs_deals
                _dd = int((_gcs_deals() or {}).get("deals") or 0)
                if _dd > 0:
                    stats["deals_tracked"] = _dd
            except Exception:
                pass
            stats.setdefault("deals_tracked", stats.get("deals_rows", 0))
            # ★2026-07-29 published-number integrity — `dcpi_markets_scored` was
            # COUNT(*) of market_power_scores ROWS (live 317). The canonical market
            # count (canonical_stats.py:165-167) is COUNT(DISTINCT market_name)
            # WHERE COALESCE(published,true)=true AND market_slug NOT IN the three
            # aggregate regions — live 306, which is what /api/v1/stats publishes as
            # top-level `markets`. So THIS endpoint, whose stated purpose is making
            # surfaces agree, was itself a fourth disagreeing surface (306 / 311 /
            # 317 / 685). Worse, ai_surface_canon.py:76 lists "317 " in
            # stale_markers: the page scrubber was deleting the exact number this
            # API was serving. Rows stay available under a name that says what they
            # are; same raw-vs-deduped split already applied to deals above.
            # Fail-soft: if the canon is unavailable the key is published as null,
            # never as the row count and never as 0 — an unmeasured count must read
            # as unknown (routes/pillars_master_shell.py:221 renders None as "—").
            try:
                cur.execute("SELECT COUNT(*) FROM market_power_scores")
                stats["dcpi_market_score_rows"] = int(cur.fetchone()[0] or 0)
            except Exception:
                pass
            try:
                from canonical_stats import get_canonical_stats as _gcs_mkts
                _mm = int((_gcs_mkts() or {}).get("markets") or 0)
                if _mm > 0:
                    stats["dcpi_markets_scored"] = _mm
            except Exception:
                pass
            stats.setdefault("dcpi_markets_scored", None)
            # provenance-v1 (2026-07-11): the PUBLICLY CITEABLE verified count —
            # the canonical fleet filter (COALESCE(is_duplicate,0)=0 on
            # discovered_facilities; issue #1539). This is the number we grow in
            # public; facilities_tracked = the raw discovery pile it comes from.
            # Placed LAST in the tx block (a failed statement aborts the tx for
            # any query after it); falls back to cached canonical_stats.
            # ★★2026-07-27 data QA — `facilities_verified` is AMBIGUOUS and must
            # not be cited until the dedup repair lands. Its documented method
            # (COALESCE(is_duplicate,0)=0) returns 5,737, but the deployed
            # surface serves 13,395 (duplicate_of_id IS NULL) — repo and prod
            # disagree, so consumers cannot know which they got.
            #
            # ROOT CAUSE of the 5,737: the dedup pipeline flags rows
            # is_duplicate=1 WITHOUT electing a keeper. Grouping by
            # canonical_slug, 9,318 of 14,686 distinct facilities have NO row
            # with is_duplicate=0 — every member flagged, so the facility is
            # invisible to any is_duplicate-based count. The suppressed set
            # includes Meta Hyperion, Stargate Abilene, CoreWeave Project
            # Horizon and Microsoft Wisconsin (confidence 0.85-0.95).
            #
            # The three fields below are UNAMBIGUOUS by construction. New
            # consumers should read `facilities_distinct`; the two legacy fields
            # are kept only for back-compat.
            try:
                from util.facility_canon_count import CANON_SQL as _CANON_SQL
                cur.execute(_CANON_SQL)
                stats["facilities_distinct"] = int(cur.fetchone()[0] or 0)
                # total_facilities is an ALIAS of the citeable distinct count so
                # this endpoint cannot contradict itself (or /api/v1/stats).
                if stats["facilities_distinct"] > 0:
                    stats["total_facilities"] = stats["facilities_distinct"]
                cur.execute("SELECT COUNT(*) FROM discovered_facilities")
                stats["facilities_records"] = int(cur.fetchone()[0] or 0)
                cur.execute("SELECT COUNT(*) FROM discovered_facilities "
                            "WHERE COALESCE(is_duplicate,0)=0")
                stats["facilities_with_keeper"] = int(cur.fetchone()[0] or 0)
            except Exception:
                pass
            try:
                # canonical fleet = distinct sites after cross-source dedup
                # (duplicate_of_id IS NULL, r-facility-dedup 2026-07-20), which
                # supersedes the older is_duplicate flag.
                cur.execute("SELECT COUNT(*) FROM discovered_facilities "
                            "WHERE duplicate_of_id IS NULL")
                stats["facilities_verified"] = int(cur.fetchone()[0] or 0)
                cur.execute("SELECT COUNT(*) FROM discovered_facilities")
                stats["facilities_tracked"] = int(cur.fetchone()[0] or 0)
            except Exception:
                try:
                    from canonical_stats import get_canonical_stats
                    _cs = get_canonical_stats()
                    stats.setdefault("facilities_verified",
                                     _cs.get("facilities_verified"))
                    stats.setdefault("facilities_tracked", _cs.get("facilities"))
                except Exception:
                    pass
            # Present-but-null if the canonical query failed. NEVER fall back to
            # legacy_facilities_table_rows: an unmeasured count must read as
            # unknown, not as a plausible number from a different table.
            stats.setdefault("total_facilities", None)
            # ── per-layer asset counts (audit SH52-060) ──────────────────
            # Canonical owner for the physical-layer figures that the MCP
            # server description otherwise hardcodes as drifting literals
            # ("13k plants / 94k transmission / 55k fiber"). Live COUNT(*)
            # over the canonical table for each layer, so every surface can
            # cite one moving number instead of a stale constant.
            # power_plants (NOT power_plants_eia) is the canonical fleet
            # table — it carries provenance + last_updated and holds the same
            # count /api/v1/whats-new and /api/land-power publish (audit
            # SH52-052). Each probe is guarded by to_regclass + rollback, so a
            # table absent on this deploy yields NO key (never a false 0) and
            # a failure can never poison the facility counts already captured
            # above in `stats`.
            for _akey, _atbl in (
                ("power_plants",       "power_plants"),
                ("transmission_lines", "transmission_lines"),
                ("gas_pipelines",      "gas_pipelines"),
                ("fiber_routes",       "fiber_routes"),
                ("substations",        "substations"),
                ("subsea_cables",      "subsea_cables"),
            ):
                try:
                    cur.execute("SELECT to_regclass(%s)", (f"public.{_atbl}",))
                    if not (cur.fetchone() or [None])[0]:
                        continue
                    cur.execute(f"SELECT COUNT(*) FROM {_atbl}")
                    stats[_akey] = int((cur.fetchone() or [0])[0] or 0)
                except Exception:
                    try:
                        c.rollback()
                    except Exception:
                        pass
                    continue
        _canon_payload = {
            "ok":         True,
            "stats":      stats,
            "source":     "Neon — live COUNT() at request time",
            "purpose":    ("Canonical truth for facility/news/deals/DCPI "
                            "counts. Use this endpoint when site copy, "
                            "AI agents, and reports need to agree."),
        }
        # provenance-v1: standard envelope on the citeable stats surface itself.
        try:
            from routes.provenance import (attach_provenance,
                                           FACILITIES_FALLBACK_URL)
            attach_provenance(
                _canon_payload,
                source="DC Hub canonical stats (Neon live counts)",
                # ★2026-07-29: this string described the WRONG FIELD. It said
                # facilities_verified = COALESCE(is_duplicate,0)=0 — but that
                # query is `facilities_with_keeper` (15,433 live); the served
                # `facilities_verified` is duplicate_of_id IS NULL (13,623).
                # An agent following the documented method got a number ~1,800
                # off the one it was handed, from the very endpoint whose stated
                # purpose is making surfaces agree. All four fields are now
                # named for their own query, and the citeable one is called out.
                method=("live COUNT() over discovered_facilities at request "
                        "time. facilities_distinct = COUNT(DISTINCT "
                        "canonical_slug) WHERE canonical_slug IS NOT NULL — "
                        "distinct BUILDINGS, and the field to cite. "
                        "total_facilities is an ALIAS of facilities_distinct "
                        "(★2026-08-09: it was COUNT(*) over the LEGACY "
                        "`facilities` table — a different table, 20,132 rows, "
                        "undeduplicated — which contradicted this endpoint's "
                        "own citeable field; that figure is now published only "
                        "as legacy_facilities_table_rows). "
                        "facilities_records = facilities_tracked = COUNT(*) — "
                        "raw source records, ~1.5x the buildings because the "
                        "March 2026 backfill wrote several rows per site. "
                        "facilities_with_keeper = COUNT(*) WHERE "
                        "COALESCE(is_duplicate,0)=0. facilities_verified = "
                        "COUNT(*) WHERE duplicate_of_id IS NULL. Both of the "
                        "last two are DE-DUPLICATION states, not source "
                        "verifications — do not publish either as 'verified'."),
                # v1: facilities surface — counts cover the whole discovery
                # pile, so the conservative facilities tier is tracked; the
                # explicit fallback pins the facilities directory (no
                # cite_url_template on a stats surface).
                fallback_url=FACILITIES_FALLBACK_URL,
                default_v="tracked",
            )
        except Exception:
            pass
        resp = jsonify(_canon_payload)
        # Edge-cache 5 min — these counts change slowly
        resp.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=1800"
        return resp, 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:160]}), 200
