"""Capability / data-milestone radar — the autonomous "what can we announce" input.

The announce machinery already exists (media_editorial.rank_data_events ranks
leads -> the LinkedIn analyst quad posts them 4x/day). This is the missing INPUT:
it turns "we shipped or grew a capability" into a ranked analyst-voice lead, so
new feeds/tools AND coverage/reach milestones get announced WITHOUT hand-feeding.

Registry-driven (owner choice). Two entry modes:
  • mode="launch"    a NEW data source/feed we just shipped. Emits capability_launch
                     the first time it's seen (no baseline), then data_milestone on
                     a jump_pct growth.
  • mode="milestone" an EXISTING metric (coverage/reach) worth announcing on
                     round-number crossings (round_step) or jump_pct growth. Needs a
                     SEEDED baseline so it never announces the current level as "new"
                     — seed once with seed_milestone_baselines().

Bookkeeping (took care to get right): capability_radar_leads() is READ-ONLY. The
baseline (data_milestone_snapshots) advances ONLY via mark_capability_announced(),
called from linkedin_quad_daily AFTER a successful LinkedIn post — so a lead stays
visible until actually posted, and previews never consume it.

Adding a future feed/metric = ONE registry row.
"""
import logging
import os

import psycopg2
from routes._swallowed_writes import note_swallowed_write

logger = logging.getLogger("brain_capability_radar")


def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


def _smithery_core_rank() -> dict | None:
    """LIVE external check: how many CORE Smithery search terms DC Hub ranks #1 for.
    A registry source can use `check` instead of `metric_sql` — this is the first.
    FAIL-SAFE: returns None if Smithery is unreachable (→ the radar skips this source,
    never a false announce). Per-term errors are non-fatal. Browser UA (Smithery 403s
    the default urllib UA). Used by the smithery_rank_1 launch source below."""
    import json as _json
    import urllib.parse
    import urllib.request
    terms = ["data center", "power grid", "fiber", "capacity",
             "grid interconnection", "interconnection"]
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    at1, ok_any = [], False
    for t in terms:
        try:
            url = "https://registry.smithery.ai/servers?" + urllib.parse.urlencode({"q": t, "pageSize": "5"})
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                d = _json.load(resp)
            ok_any = True
            servers = d.get("servers") or []
            if servers and "dchub" in (servers[0].get("qualifiedName") or "").lower():
                at1.append(t)
        except Exception:
            continue
    if not ok_any:
        return None
    return {"core_at_1": float(len(at1)), "terms": at1,
            "terms_str": ", ".join(f'"{t}"' for t in at1)}


# ── The registry: one row per announceable capability/metric ────────────────
REGISTRY = [
    # ---- LAUNCH: new data feeds (announce once on ship) --------------------
    {
        "key": "planned_generation",
        "mode": "launch",
        "metric_sql": ("SELECT COUNT(*) AS n, COALESCE(SUM(capacity_mw),0) AS mw, "
                       "COUNT(DISTINCT state) AS states FROM planned_generators "
                       "WHERE source='eia860m_planned'"),
        "value_key": "mw", "jump_pct": 0.20, "score": 82,
        "source_url": "https://dchub.cloud/land-power",
        "headline": lambda r: (f"DC Hub now maps the full US generation build pipeline: "
                               f"{r['mw'] / 1000:.0f} GW of planned capacity across "
                               f"{int(r['n']):,} generators in {int(r['states'])} states"),
        "trend": ("planned, permitting and under-construction generators nationwide, "
                  "including the non-ISO regions (TVA, Southern, Arizona) the per-ISO "
                  "interconnection queues miss"),
        "so_what": ("the forward power-supply curve for every siting decision, on the "
                    "map and via the get_power_pipeline MCP tool."),
    },
    {
        "key": "operable_generation",
        "mode": "launch",
        "metric_sql": ("SELECT COUNT(*) AS n, COALESCE(SUM(capacity_mw),0) AS mw, "
                       "COUNT(DISTINCT ba_code) AS bas FROM generator_inventory "
                       "WHERE source='eia860m'"),
        "value_key": "mw", "jump_pct": 0.20, "score": 78,
        "source_url": "https://dchub.cloud/dcpi",
        "headline": lambda r: (f"DC Hub now tracks the full operable US generator fleet: "
                               f"{r['mw'] / 1000:.0f} GW across {int(r['n']):,} generators, "
                               f"keyed to {int(r['bas'])} balancing authorities"),
        "trend": ("operating, standby and returning-to-service capacity by ISO and fuel, "
                  "with the standby reserve that signals grid headroom"),
        "so_what": "the installed-supply side of the power picture, by ISO, behind the DCPI scores.",
    },
    # ---- MILESTONE: coverage (round-number crossings) ----------------------
    {
        "key": "facility_coverage",
        "mode": "milestone", "round_step": 1000, "value_key": "n", "score": 74,
        "metric_sql": "SELECT COUNT(*) AS n, COUNT(DISTINCT country) AS countries FROM discovered_facilities",
        "source_url": "https://dchub.cloud/map",
        "headline": lambda r: (f"DC Hub's live index just crossed {int(r['_milestone']):,} "
                               f"data-center facilities, across {int(r['countries'])} countries"),
        "trend": "the most complete machine-readable map of the physical AI buildout, refreshed daily",
        "so_what": "every facility queryable by AI agents and on the map, with power, fiber and tenant context.",
    },
    {
        "key": "country_coverage",
        "mode": "milestone", "round_step": 10, "value_key": "n", "score": 70,
        "metric_sql": "SELECT COUNT(DISTINCT country) AS n FROM discovered_facilities",
        "source_url": "https://dchub.cloud/map",
        "headline": lambda r: (f"DC Hub now tracks data-center infrastructure in "
                               f"{int(r['_milestone'])}+ countries"),
        "trend": "global coverage of the physical layer behind AI compute, one live dataset",
        "so_what": "cross-border site comparisons agents and developers can run in one query.",
    },
    {
        "key": "dcpi_markets",
        "mode": "milestone", "round_step": 25, "value_key": "n", "score": 70,
        "metric_sql": "SELECT COUNT(*) AS n FROM market_power_scores",
        "source_url": "https://dchub.cloud/dcpi",
        "headline": lambda r: (f"The DC Hub Power Index now scores {int(r['_milestone'])}+ "
                               f"markets on buildable headroom"),
        "trend": "a single comparable power-availability score per market, updated continuously",
        "so_what": "rank-and-shortlist any market on time-to-power without a consultant.",
    },
    # ---- MILESTONE: reach -------------------------------------------------
    {
        "key": "ai_citations",
        "mode": "milestone", "round_step": 10, "value_key": "n", "score": 80,
        "metric_sql": "SELECT COUNT(*) AS n, COUNT(DISTINCT engine) AS engines FROM ai_citations WHERE dchub_cited=TRUE",
        "source_url": "https://dchub.cloud",
        "headline": lambda r: (f"DC Hub has now been cited {int(r['_milestone'])}+ times in AI "
                               f"answers across {int(r['engines'])} engines"),
        "trend": "AI assistants are sourcing live data-center infrastructure facts from DC Hub",
        "so_what": "the MCP-native data layer is becoming the default ground truth agents cite.",
    },
    # ---- ACHIEVEMENT: competitive standing (live-verified, announce once) ---
    {
        "key": "smithery_rank_1",
        "mode": "launch",            # announce once when the achievement is first verified
        "check": _smithery_core_rank,  # live external check instead of metric_sql
        "value_key": "core_at_1",
        "min_value": 5,              # only fire once we lead a robust majority of the core cluster
        "score": 88,
        "source_url": "https://smithery.ai/servers/azmartone67/dchub",
        "headline": lambda r: (f"DC Hub is now the #1 data-center MCP server on Smithery — "
                               f"ranked #1 for {r['terms_str']}"),
        "trend": ("agents browsing the Smithery MCP marketplace now find DC Hub first for "
                  "data-center, power-grid and interconnection queries — ahead of every other "
                  "server in the data-center category"),
        "so_what": ("the live, MCP-native data layer agents reach for on data-center "
                    "infrastructure — query it and cite it at dchub.cloud/mcp."),
    },
]


def _ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS data_milestone_snapshots (
            source_key   TEXT PRIMARY KEY,
            last_value   DOUBLE PRECISION,
            announced_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


def _metric(cur, src):
    """Resolve a registry source's value: a live `check` callable (external, no DB)
    or metric_sql against the DB -> (row dict, diff value)."""
    if src.get("check"):
        r = src["check"]()
        if not r:
            return None, None
        return r, float(r.get(src["value_key"]) or 0)
    cur.execute(src["metric_sql"])
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    if not row:
        return None, None
    r = {k: (float(v) if isinstance(v, (int, float)) else v) for k, v in zip(cols, row)}
    return r, float(r.get(src["value_key"]) or 0)


def capability_radar_leads() -> list[dict]:
    """READ-ONLY. Return analyst-voice leads for new sources + milestone crossings.

    Shape matches media_editorial.rank_data_events() leads. Never writes (the
    baseline advances only on an actual post, via mark_capability_announced)."""
    dsn = _dsn()
    if not dsn:
        return []
    leads: list[dict] = []
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            c.autocommit = True
            with c.cursor() as cur:
                _ensure_table(cur)
                for src in REGISTRY:
                    try:
                        r, cur_val = _metric(cur, src)
                        if r is None or cur_val <= 0:
                            continue
                        if cur_val < float(src.get("min_value", 0)):
                            continue  # achievement threshold not met yet (e.g. not #1 enough)
                        cur.execute("SELECT last_value FROM data_milestone_snapshots WHERE source_key=%s",
                                    (src["key"],))
                        prev_row = cur.fetchone()
                        prev = prev_row[0] if prev_row else None
                        mode = src.get("mode", "launch")
                        jp = src.get("jump_pct")
                        rs = src.get("round_step")
                        is_new = prev is None
                        kind = None

                        if mode == "launch":
                            if is_new:
                                kind = "capability_launch"
                            elif jp and prev and cur_val >= prev * (1 + jp):
                                kind = "data_milestone"
                        else:  # milestone — needs a seeded baseline; never announces "new"
                            if is_new:
                                continue
                            crossed = rs and int(cur_val // rs) > int(prev // rs)
                            jumped = jp and cur_val >= prev * (1 + jp)
                            if crossed or jumped:
                                kind = "data_milestone"
                                r["_milestone"] = (int(cur_val // rs) * rs) if rs else cur_val
                        if not kind:
                            continue

                        headline = src["headline"](r) if callable(src["headline"]) else src["headline"]
                        trend = src.get("trend", "")
                        if kind == "data_milestone" and prev and mode == "launch":
                            trend = f"+{(cur_val / prev - 1) * 100:.0f}% since last reported. " + trend
                        leads.append({
                            "kind": kind,
                            "headline_number": headline,
                            "trend": trend,
                            "so_what": src.get("so_what", ""),
                            "source_url": src.get("source_url", "https://dchub.cloud"),
                            "dedup_key": (f"capability:{src['key']}" if kind == "capability_launch"
                                          else f"milestone:{src['key']}"),
                            "score": float(src.get("score", 70)),
                        })
                    except Exception as e:
                        logger.warning("[capability-radar] source %s skipped: %s",
                                       src.get("key"), str(e)[:140])
                        try:
                            c.rollback()
                        except Exception:
                            pass
    except Exception as e:
        logger.warning("[capability-radar] failed: %s", str(e)[:160])
    return leads


def mark_capability_announced(dedup_key: str) -> bool:
    """Advance a source's baseline because its lead was just POSTED (success).

    Called from linkedin_quad_daily after a successful publish. The ONLY writer of
    the baseline. dedup_key is 'capability:<key>' or 'milestone:<key>'."""
    if not dedup_key or ":" not in dedup_key:
        return False
    key = dedup_key.split(":", 1)[1]
    src = next((s for s in REGISTRY if s["key"] == key), None)
    if not src or not _dsn():
        return False
    try:
        with psycopg2.connect(_dsn(), sslmode="require", connect_timeout=8) as c:
            c.autocommit = True
            with c.cursor() as cur:
                _ensure_table(cur)
                _, cur_val = _metric(cur, src)
                if cur_val is None:
                    return False
                cur.execute("""
                    INSERT INTO data_milestone_snapshots (source_key, last_value, announced_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (source_key)
                    DO UPDATE SET last_value=EXCLUDED.last_value, announced_at=NOW()
                """, (key, cur_val))
                return True
    except Exception as e:
        logger.warning("[capability-radar] mark %s failed: %s", key, str(e)[:120])
        return False


def seed_milestone_baselines() -> dict:
    """One-time: record the CURRENT value of every mode='milestone' source so it
    only fires on the NEXT crossing (never announces the existing level as new).
    Launch sources are intentionally left unseeded so they announce on ship.
    Idempotent: only seeds milestone sources that have no baseline yet."""
    dsn = _dsn()
    if not dsn:
        return {"ok": False, "error": "no dsn"}
    seeded = []
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            c.autocommit = True
            with c.cursor() as cur:
                _ensure_table(cur)
                for src in REGISTRY:
                    if src.get("mode") != "milestone":
                        continue
                    cur.execute("SELECT 1 FROM data_milestone_snapshots WHERE source_key=%s", (src["key"],))
                    if cur.fetchone():
                        continue
                    try:
                        _, cur_val = _metric(cur, src)
                        cur.execute("""INSERT INTO data_milestone_snapshots (source_key, last_value, announced_at)
                                       VALUES (%s, %s, NOW()) ON CONFLICT (source_key) DO NOTHING""",
                                    (src["key"], cur_val))
                        seeded.append({src["key"]: cur_val})
                    except Exception:
                        note_swallowed_write("data_milestone_snapshots", where="brain_capability_radar.seed_milestone_baselines")
                        c.rollback()
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}
    return {"ok": True, "seeded": seeded}
