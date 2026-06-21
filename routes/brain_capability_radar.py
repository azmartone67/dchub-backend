"""Capability / data-milestone radar — the autonomous "what can we announce" input.

The announce machinery already exists (media_editorial.rank_data_events ranks
leads → the LinkedIn analyst quad posts them 4×/day). What was missing was an
INPUT that turns "we shipped / grew a data capability" into a ranked lead — so
new feeds, tools, and map layers get announced WITHOUT anyone hand-feeding the
media machine.

This is the registry-driven version (owner choice, 2026-06-21): a curated list
of announceable data sources, each with a headline-metric query + an analyst-
voice template. The radar runs each, diffs against a stored baseline, and emits
a lead when a source is NEW (first time we've seen it) or has JUMPED past a
threshold. Adding a future feed = ONE registry row — no edits to the desk.

  • NEW source  → "DC Hub now maps/tracks X — <headline number>"
  • jump        → "X crossed <N> — <so-what>"

The editorial desk's existing newsworthiness gate + 4-day dedup prevent spam;
the baseline advances only when a lead is emitted, so slow growth accumulates
toward the next milestone instead of re-firing daily.
"""
import logging
import os

import psycopg2

logger = logging.getLogger("brain_capability_radar")


def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


# ── The registry: one row per announceable data source ──────────────────────
# metric_sql must return a single row; `value_key` picks the number we diff for
# milestones. `headline`/`trend`/`so_what` are analyst-voice (number-first, no
# marketing words, no em-dashes — same spec the desk enforces).
REGISTRY = [
    {
        "key": "planned_generation",
        "table": "planned_generators",
        "metric_sql": (
            "SELECT COUNT(*) AS n, COALESCE(SUM(capacity_mw),0) AS mw, "
            "COUNT(DISTINCT state) AS states "
            "FROM planned_generators WHERE source='eia860m_planned'"
        ),
        "value_key": "mw",
        "jump_pct": 0.15,
        "score": 82,
        "source_url": "https://dchub.cloud/land-power",
        "headline": lambda r: (
            f"DC Hub now maps the full US generation build pipeline: "
            f"{r['mw'] / 1000:.0f} GW of planned capacity across "
            f"{int(r['n']):,} generators in {int(r['states'])} states"
        ),
        "trend": lambda r: (
            "planned, permitting and under-construction generators nationwide, "
            "including the non-ISO regions (TVA, Southern, Arizona, PacifiCorp) "
            "the per-ISO interconnection queues miss"
        ),
        "so_what": (
            "the forward power-supply curve for every siting decision: where new "
            "MW land, what fuel, and when they come online, on the map and via the "
            "get_power_pipeline MCP tool."
        ),
    },
    {
        "key": "operable_generation",
        "table": "generator_inventory",
        "metric_sql": (
            "SELECT COUNT(*) AS n, COALESCE(SUM(capacity_mw),0) AS mw, "
            "COUNT(DISTINCT ba_code) AS bas "
            "FROM generator_inventory WHERE source='eia860m'"
        ),
        "value_key": "mw",
        "jump_pct": 0.15,
        "score": 78,
        "source_url": "https://dchub.cloud/dcpi",
        "headline": lambda r: (
            f"DC Hub now tracks the full operable US generator fleet: "
            f"{r['mw'] / 1000:.0f} GW across {int(r['n']):,} generators, "
            f"keyed to {int(r['bas'])} balancing authorities"
        ),
        "trend": lambda r: (
            "operating, standby and returning-to-service capacity by ISO and fuel, "
            "with the standby reserve that signals grid headroom"
        ),
        "so_what": (
            "the installed-supply side of the power picture, by ISO, behind the "
            "DCPI excess-power scores."
        ),
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


def capability_radar_leads() -> list[dict]:
    """Return analyst-voice leads for NEW or milestone-jumped data sources.

    Shape matches media_editorial.rank_data_events() leads:
      {kind, headline_number, trend, so_what, source_url, dedup_key, score}.
    Fully defensive — any source/DB error is skipped (never breaks the desk)."""
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
                        cur.execute(src["metric_sql"])
                        cols = [d[0] for d in cur.description]
                        row = cur.fetchone()
                        if not row:
                            continue
                        r = {k: (float(v) if isinstance(v, (int, float)) else v)
                             for k, v in zip(cols, row)}
                        cur_val = float(r.get(src["value_key"]) or 0)
                        if cur_val <= 0:
                            continue

                        cur.execute(
                            "SELECT last_value FROM data_milestone_snapshots WHERE source_key=%s",
                            (src["key"],))
                        prev = cur.fetchone()

                        is_new = prev is None
                        jumped = (prev is not None and prev[0]
                                  and cur_val >= prev[0] * (1 + src.get("jump_pct", 0.15)))
                        if not (is_new or jumped):
                            continue

                        headline = src["headline"](r) if callable(src["headline"]) else src["headline"]
                        trend = src["trend"](r) if callable(src.get("trend")) else src.get("trend", "")
                        if jumped and not is_new:
                            trend = f"+{(cur_val / prev[0] - 1) * 100:.0f}% since last reported. " + trend
                        leads.append({
                            "kind": "capability_launch" if is_new else "data_milestone",
                            "headline_number": headline,
                            "trend": trend,
                            "so_what": src.get("so_what", ""),
                            "source_url": src.get("source_url", "https://dchub.cloud"),
                            "dedup_key": f"capability:{src['key']}" if is_new else f"milestone:{src['key']}",
                            "score": float(src.get("score", 70)),
                        })
                        # Advance the baseline ONLY on emit, so it doesn't re-fire
                        # daily; the desk's 4-day dedup covers same-week reposts.
                        cur.execute("""
                            INSERT INTO data_milestone_snapshots (source_key, last_value, announced_at)
                            VALUES (%s, %s, NOW())
                            ON CONFLICT (source_key)
                            DO UPDATE SET last_value=EXCLUDED.last_value, announced_at=NOW()
                        """, (src["key"], cur_val))
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
