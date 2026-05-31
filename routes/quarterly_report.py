"""quarterly_report.py — DC Hub Quarterly "State of Power" report.

A real, recurring, self-contained, CITABLE data event: one permanent,
per-quarter URL an LLM can both QUERY and CITE. Lives under the
/state-of-power and /api/* prefixes, which the dchub-frontend
_routes.json already proxies to this Railway backend (the existing
/state-of-power page proves it) — so NO frontend change is needed.

  GET  /state-of-power/<quarter>                       — HTML report
       (<quarter> like 'q3-2026'; 'latest'/'current' → current quarter)
  GET  /state-of-power/quarterly                       — 302 → current quarter
  GET  /api/v1/reports/quarterly/<quarter>.json        — structured payload
  GET  /api/v1/reports/quarterly/<quarter>.csv         — CSV of DCPI scores
                                                          + this-quarter shifts

The LEAD story is computed from DATA, not an LLM (it must render even if
Claude/egress is down): the count of markets that flipped to AVOID this
quarter, the top new BUILD gravity centers, a grid-stress line, and the
M&A run-rate. Verdict shifts are read from `dcpi_daily_snapshots` — the
same daily-snapshot source the DCPI movers / agent-broadcast surfaces
use — comparing a snapshot near the quarter's start against the latest.

Robustness: EVERY DB read is wrapped in try/except with a fallback to the
canonical site constants, so the report NEVER 500s or shows blanks even
if a table/query is unavailable. The <quarter> param is validated
(q[1-4]-20\\d\\d or latest/current) → a malformed quarter 404s cleanly.

Same CC-BY-4.0 + Link header + CORS * pattern as state_of_power.py.

NOTE ON THIS MODULE: the prior `quarterly_report_bp` (Phase AAAAA,
2026-05-16) served /reports/quarterly + /api/v1/reports/quarterly. Those
two live routes are PRESERVED verbatim at the bottom of this file so the
existing surface + any inbound links don't regress; the blueprint is
already registered in main.py, so the new routes ship alongside them.
"""

from __future__ import annotations

import os
import re
import io
import csv
import json
import logging
import datetime
import html as _html

from flask import Blueprint, Response, jsonify, request, redirect

logger = logging.getLogger(__name__)
quarterly_report_bp = Blueprint("quarterly_report", __name__)

_CC_LINK_HEADER = '<https://creativecommons.org/licenses/by/4.0/>; rel="license"'
_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"

# ── Canonical site constants (fallbacks so a dead table never blanks the
# page). Sourced from main.py ~13405 (get_stats) + the homepage/pulse
# stats + the MCP catalog descriptions. These are deliberately the SAME
# numbers the homepage brags about. ───────────────────────────────────
_CANON = {
    "facilities":   21418,   # discovered_facilities raw count (get_stats)
    "markets":      232,     # DCPI market universe
    "isos":         51,      # ISOs / balancing authorities tracked
    "mna_usd":      324_000_000_000,   # $324B+ tracked M&A (deals.value_usd)
    "pipeline_gw":  369,     # under-construction GW (homepage stats)
    "substations":  126427,  # HIFLD substations
}

# Quarter regex: q1-2026 .. q4-2099, case-insensitive.
_QUARTER_RE = re.compile(r"^q([1-4])-(20\d\d)$", re.IGNORECASE)
_LATEST_ALIASES = {"latest", "current", "now", "this"}


# ─────────────────────────────────────────────────────────────────────
# DB helper — mirrors routes/dcpi.py._conn (DATABASE_URL, sslmode=require)
# ─────────────────────────────────────────────────────────────────────
def _conn():
    """Open a short-lived psycopg2 connection. NEVER used without a
    try/except at the call site — a single-replica backend can't afford a
    slow/blocked query on a public page (see dchub backend flapping note)."""
    import psycopg2
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        raise RuntimeError("DATABASE_URL not set")
    c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
    c.autocommit = True
    return c


# ─────────────────────────────────────────────────────────────────────
# Quarter math
# ─────────────────────────────────────────────────────────────────────
def _current_quarter_slug() -> str:
    """Resolve the current quarter from the Flask runtime clock."""
    today = datetime.datetime.utcnow().date()
    q = (today.month - 1) // 3 + 1
    return f"q{q}-{today.year}"


def _parse_quarter(raw: str):
    """Validate + normalize a <quarter> path param.

    Returns a dict {slug, q, year, label, start, end, is_current} on
    success, or None if the param is malformed (caller → 404). 'latest'/
    'current' resolve to the current quarter at request time.
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if s in _LATEST_ALIASES:
        s = _current_quarter_slug()
    m = _QUARTER_RE.match(s)
    if not m:
        return None
    q = int(m.group(1))
    year = int(m.group(2))
    start_month = (q - 1) * 3 + 1
    start = datetime.date(year, start_month, 1)
    # End = day before next quarter's start.
    if q == 4:
        end = datetime.date(year, 12, 31)
    else:
        end = datetime.date(year, start_month + 3, 1) - datetime.timedelta(days=1)
    cur = _current_quarter_slug()
    return {
        "slug":       f"q{q}-{year}",
        "q":          q,
        "year":       year,
        "label":      f"Q{q} {year}",
        "start":      start,
        "end":        end,
        "is_current": (f"q{q}-{year}" == cur),
        "is_future":  start > datetime.datetime.utcnow().date(),
    }


# ─────────────────────────────────────────────────────────────────────
# Data gather — every read guarded, every value has a canonical fallback
# ─────────────────────────────────────────────────────────────────────
def _headline_stats() -> dict:
    """Canonical headline numbers, pulled from the same tables get_stats
    uses. Each read is independently guarded → a single dead table only
    loses that one number (falls back to the canonical constant), never
    the whole block."""
    s = dict(_CANON)  # start from canonical; override with live values
    s["_origin"] = {}
    c = None
    try:
        c = _conn()
        cur = c.cursor()
        try:
            cur.execute("SET LOCAL statement_timeout = 6000")
        except Exception:
            pass

        # Facilities — discovered_facilities raw count (homepage headline).
        try:
            cur.execute("SELECT COUNT(*) FROM discovered_facilities")
            n = (cur.fetchone() or [0])[0]
            if n:
                s["facilities"] = int(n)
                s["_origin"]["facilities"] = "discovered_facilities"
        except Exception:
            pass

        # Substations.
        try:
            cur.execute("SELECT COUNT(*) FROM substations")
            n = (cur.fetchone() or [0])[0]
            if n:
                s["substations"] = int(n)
                s["_origin"]["substations"] = "substations"
        except Exception:
            pass

        # M&A total $ — deals.value_usd (same source as homepage deal_volume).
        try:
            cur.execute("SELECT COALESCE(SUM(value_usd),0) FROM deals WHERE value_usd > 0")
            v = (cur.fetchone() or [0])[0] or 0
            if v and v > 1e9:
                s["mna_usd"] = float(v)
                s["_origin"]["mna_usd"] = "deals.value_usd"
        except Exception:
            pass

        # Pipeline GW — under-construction MW from discovered_facilities.
        try:
            cur.execute("""
                SELECT COALESCE(SUM(power_mw),0) FROM discovered_facilities
                 WHERE LOWER(status) IN ('under construction','construction',
                       'planning','planned','announced','approved',
                       'under_construction','pre-construction','in development',
                       'proposed','permitted')
            """)
            mw = (cur.fetchone() or [0])[0] or 0
            if mw:
                s["pipeline_gw"] = round(mw / 1000.0, 1)
                s["_origin"]["pipeline_gw"] = "discovered_facilities.power_mw"
        except Exception:
            pass
    except Exception as e:
        logger.info(f"quarterly_report: headline stats fell back to canonical ({e})")
    finally:
        try:
            if c:
                c.close()
        except Exception:
            pass
    return s


def _mna_runrate(qinfo: dict) -> dict:
    """M&A deal count + $ volume within the quarter window, plus an
    annualized run-rate. Guarded → returns {} on any failure (the
    narrative + JSON tolerate an empty dict)."""
    out = {}
    c = None
    try:
        c = _conn()
        cur = c.cursor()
        try:
            cur.execute("SET LOCAL statement_timeout = 6000")
        except Exception:
            pass
        # deals.date is the canonical deal date (stored as text/date;
        # cast defensively). value_usd is the $ column get_stats uses.
        cur.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(value_usd),0)
              FROM deals
             WHERE value_usd > 0
               AND date::timestamp >= %s
               AND date::timestamp <  %s
            """,
            (qinfo["start"].isoformat(),
             (qinfo["end"] + datetime.timedelta(days=1)).isoformat()),
        )
        r = cur.fetchone() or (0, 0)
        count = int(r[0] or 0)
        total = float(r[1] or 0)
        out = {
            "deal_count":    count,
            "deal_value_usd": total,
            "deal_value_b":  round(total / 1e9, 1),
            # Quarter → annual run-rate (×4), a recognizable analyst frame.
            "annualized_b":  round(total / 1e9 * 4, 1),
        }
    except Exception as e:
        logger.info(f"quarterly_report: M&A run-rate unavailable ({e})")
        out = {}
    finally:
        try:
            if c:
                c.close()
        except Exception:
            pass
    return out


def _verdict_shifts(qinfo: dict) -> dict:
    """THE LEAD STORY: DCPI verdict shifts within the quarter window.

    Reads dcpi_daily_snapshots (the daily per-market snapshot table the
    DCPI movers + agent-broadcast 'dcpi_verdict_shift' surfaces rely on)
    and compares the snapshot nearest the quarter START against the
    LATEST snapshot inside the window. A genuine shift = baseline verdict
    IS DISTINCT FROM the current verdict.

    Mirrors agent_broadcast._fetch_dcpi_verdict_shifts' two-pass shape:
      Pass 1 — real shifts from the snapshot table.
      Pass 2 — if the table has no usable history yet (it bootstraps on a
               daily cron), fall back to the CURRENT decisive verdicts
               (BUILD/AVOID) from market_power_scores so the report still
               leads with live DCPI signal instead of an empty section.

    Returns {to_avoid, to_build, to_caution, all_shifts, baseline_date,
    fallback}. Always returns a dict; on total failure → empty lists.
    """
    out = {
        "to_avoid": [], "to_build": [], "to_caution": [], "other": [],
        "all_shifts": [], "baseline_date": None, "fallback": False,
        "shift_count": 0,
    }
    c = None
    try:
        c = _conn()
        cur = c.cursor()
        try:
            cur.execute("SET LOCAL statement_timeout = 8000")
        except Exception:
            pass

        start_iso = qinfo["start"].isoformat()
        end_iso = qinfo["end"].isoformat()

        # Pass 1 — genuine shifts from the snapshot table. baseline = the
        # earliest snapshot ON/AFTER the quarter start (clamped so a
        # future/edge quarter just yields nothing); current = the latest
        # snapshot ON/BEFORE the quarter end (or today for the live qtr).
        try:
            cur.execute(
                """
                WITH baseline AS (
                    SELECT DISTINCT ON (market_slug)
                           market_slug, market_name, verdict AS prior_verdict,
                           snapshot_date
                      FROM dcpi_daily_snapshots
                     WHERE snapshot_date >= %s
                     ORDER BY market_slug, snapshot_date ASC
                ),
                current AS (
                    SELECT DISTINCT ON (market_slug)
                           market_slug, market_name, verdict AS now_verdict,
                           excess_power_score, constraint_score, snapshot_date
                      FROM dcpi_daily_snapshots
                     WHERE snapshot_date <= %s
                     ORDER BY market_slug, snapshot_date DESC
                )
                SELECT c.market_slug, c.market_name,
                       b.prior_verdict, c.now_verdict,
                       c.excess_power_score, c.constraint_score,
                       b.snapshot_date, c.snapshot_date
                  FROM current c
                  JOIN baseline b USING (market_slug)
                 WHERE b.prior_verdict IS DISTINCT FROM c.now_verdict
                   AND b.snapshot_date < c.snapshot_date
                 ORDER BY c.market_slug
                """,
                (start_iso, end_iso),
            )
            rows = cur.fetchall() or []
            baselines = set()
            for r in rows:
                slug, name, was, now_, ex, con, bts, cts = r
                baselines.add(bts)
                item = {
                    "market":      name or slug,
                    "slug":        slug,
                    "was":         was,
                    "now":         now_,
                    "excess":      _num(ex),
                    "constraint":  _num(con),
                    "page":        f"https://dchub.cloud/dcpi/{slug}",
                }
                out["all_shifts"].append(item)
                nv = (now_ or "").upper()
                if nv == "AVOID":
                    out["to_avoid"].append(item)
                elif nv == "BUILD":
                    out["to_build"].append(item)
                elif nv == "CAUTION":
                    out["to_caution"].append(item)
                else:
                    out["other"].append(item)
            if baselines:
                # Earliest baseline date used (informational).
                try:
                    out["baseline_date"] = min(
                        (b.isoformat() if hasattr(b, "isoformat") else str(b))
                        for b in baselines)
                except Exception:
                    pass
        except Exception as e:
            logger.info(f"quarterly_report: shift pass-1 failed ({e})")

        # Pass 2 — fallback to current decisive verdicts so the lead story
        # is never empty while DCPI data exists.
        if not out["all_shifts"]:
            try:
                cur.execute(
                    """
                    SELECT DISTINCT ON (market_slug)
                           market_slug, market_name, iso, verdict,
                           excess_power_score, constraint_score
                      FROM market_power_scores
                     WHERE COALESCE(published, true) = true
                       AND verdict IN ('BUILD','AVOID')
                     ORDER BY market_slug, computed_at DESC
                    """
                )
                rows = cur.fetchall() or []
                out["fallback"] = True
                for r in rows:
                    slug, name, iso, verdict, ex, con = r
                    item = {
                        "market":     name or slug,
                        "slug":       slug,
                        "iso":        iso,
                        "was":        None,
                        "now":        verdict,
                        "excess":     _num(ex),
                        "constraint": _num(con),
                        "page":       f"https://dchub.cloud/dcpi/{slug}",
                    }
                    if (verdict or "").upper() == "AVOID":
                        out["to_avoid"].append(item)
                    else:
                        out["to_build"].append(item)
                # Sort BUILD by excess desc, AVOID by constraint desc.
                out["to_build"].sort(key=lambda x: -(x.get("excess") or 0))
                out["to_avoid"].sort(key=lambda x: -(x.get("constraint") or 0))
                out["all_shifts"] = out["to_build"] + out["to_avoid"]
            except Exception as e:
                logger.info(f"quarterly_report: shift pass-2 failed ({e})")
    except Exception as e:
        logger.info(f"quarterly_report: verdict shifts unavailable ({e})")
    finally:
        try:
            if c:
                c.close()
        except Exception:
            pass

    out["shift_count"] = len(out["all_shifts"])
    return out


def _num(v):
    """Coerce a DB numeric to int when whole, float otherwise, else None."""
    if v is None:
        return None
    try:
        f = float(v)
        return int(f) if f == int(f) else round(f, 1)
    except (TypeError, ValueError):
        return None


def _energy_block() -> dict:
    """Reuse energy_report._gather_energy('quarterly') for the verdict
    distribution + top BUILD/AVOID leaderboards (5-min cached, CC-BY) —
    identical to how state_of_power.py sources its DCPI data. Guarded so
    a brain/egress hiccup degrades gracefully to {}."""
    try:
        from routes.energy_report import _gather_energy
        return _gather_energy("quarterly") or {}
    except Exception as e:
        logger.info(f"quarterly_report: _gather_energy failed ({e})")
        return {}


def _dcpi_scores() -> list:
    """All current DCPI market scores (for the CSV + the scores table),
    read straight from market_power_scores. Mirrors the SELECT in
    routes/dcpi.api_scores (DISTINCT ON latest per slug). Guarded → []
    on failure; the CSV/table simply renders empty rather than 500ing.

    NOTE: this is the UNMASKED score set served from the report's own
    permanent, CC-BY surface — the quarterly report's whole point is to be
    the citation/moat play, so the scored leaderboard is open here."""
    rows = []
    c = None
    try:
        c = _conn()
        cur = c.cursor()
        try:
            cur.execute("SET LOCAL statement_timeout = 8000")
        except Exception:
            pass
        cur.execute(
            """
            SELECT DISTINCT ON (market_slug)
                   market_slug, market_name, state, iso, verdict,
                   excess_power_score, constraint_score, time_to_power_months,
                   computed_at
              FROM market_power_scores
             WHERE COALESCE(published, true) = true
             ORDER BY market_slug, computed_at DESC
            """
        )
        for r in cur.fetchall() or []:
            slug, name, state, iso, verdict, ex, con, ttp, ts = r
            rows.append({
                "market":      name or slug,
                "slug":        slug,
                "state":       state,
                "iso":         iso,
                "verdict":     verdict,
                "excess":      _num(ex),
                "constraint":  _num(con),
                "ttp_months":  _num(ttp),
                "computed_at": (ts.isoformat() if hasattr(ts, "isoformat") else None),
            })
    except Exception as e:
        logger.info(f"quarterly_report: dcpi scores unavailable ({e})")
        rows = []
    finally:
        try:
            if c:
                c.close()
        except Exception:
            pass
    # Sort by verdict priority (BUILD, CAUTION, AVOID, other) then excess.
    _vrank = {"BUILD": 0, "CAUTION": 1, "AVOID": 2}
    rows.sort(key=lambda x: (_vrank.get((x.get("verdict") or "").upper(), 3),
                             -(x.get("excess") or 0)))
    return rows


def _narrative(qinfo: dict, shifts: dict, energy: dict, mna: dict, stats: dict) -> dict:
    """Compute the story FROM THE NUMBERS — fully templated, no LLM
    dependency. Returns {headline, paragraphs[], bullets[]}.

    The lead is the count of markets that flipped to AVOID this quarter
    (the scarcity story); then the new BUILD gravity centers; then a
    grid-stress line from the verdict distribution; then the M&A run-rate.
    """
    label = qinfo["label"]
    n_avoid = len(shifts.get("to_avoid") or [])
    n_build = len(shifts.get("to_build") or [])
    fallback = shifts.get("fallback")

    vd = energy.get("verdict_distribution") or {}
    n_build_total = vd.get("BUILD", 0)
    n_caution_total = vd.get("CAUTION", 0)
    n_avoid_total = vd.get("AVOID", 0)
    scored = energy.get("markets_scored_total") or stats.get("markets")

    paras = []
    bullets = []

    # ── Lead: scarcity / flips to AVOID ──
    if fallback:
        # No usable quarter-over-quarter history yet — lead with the live
        # decisive split instead (honest about what the data supports).
        lead = (
            f"As of {label}, DC Hub's Data Center Power Index rates "
            f"{n_avoid_total} markets AVOID and {n_build_total} BUILD across "
            f"{scored} scored markets. Quarter-over-quarter verdict-shift "
            f"tracking begins once a full quarter of daily snapshots has "
            f"accumulated; this edition leads with the current decisive "
            f"split."
        )
    elif n_avoid > 0:
        top_avoid = ", ".join(m["market"] for m in (shifts["to_avoid"] or [])[:3])
        lead = (
            f"{n_avoid} market{'s' if n_avoid != 1 else ''} flipped to "
            f"AVOID in {label} — power scarcity is spreading"
            + (f", led by {top_avoid}" if top_avoid else "") + ". "
            f"An AVOID verdict means the grid can no longer absorb new "
            f"large-load data centers without multi-year interconnection "
            f"waits or transmission upgrades."
        )
    else:
        lead = (
            f"No market deteriorated to an AVOID verdict in {label} — a "
            f"rare quarter of grid stability. DC Hub's index still rates "
            f"{n_avoid_total} markets AVOID overall across {scored} scored "
            f"markets."
        )
    paras.append(lead)

    # ── New BUILD gravity centers ──
    if not fallback and n_build > 0:
        names = ", ".join(m["market"] for m in (shifts["to_build"] or [])[:4])
        paras.append(
            f"On the upside, {n_build} market{'s' if n_build != 1 else ''} "
            f"newly cleared to BUILD this quarter"
            + (f" — {names}" if names else "") + ". These are the emerging "
            f"gravity centers where excess generation, queue velocity, and "
            f"transmission headroom now favor new construction."
        )
    elif energy.get("top_build_markets"):
        names = ", ".join(
            (m.get("market") or "?") for m in energy["top_build_markets"][:4])
        paras.append(
            f"The strongest BUILD markets by DCPI composite this quarter: "
            f"{names}. These lead on buildable headroom with the lowest "
            f"grid constraint."
        )

    # ── Grid-stress line from the verdict distribution ──
    total_decisive = (n_build_total or 0) + (n_caution_total or 0) + (n_avoid_total or 0)
    if total_decisive:
        avoid_pct = round(100 * (n_avoid_total or 0) / total_decisive)
        build_pct = round(100 * (n_build_total or 0) / total_decisive)
        paras.append(
            f"Grid stress, {label}: of scored markets, {build_pct}% rate "
            f"BUILD and {avoid_pct}% rate AVOID, with {n_caution_total} on "
            f"CAUTION. The interconnection queue remains the binding "
            f"constraint on AI data-center siting."
        )

    # ── M&A run-rate ──
    if mna and mna.get("deal_count"):
        paras.append(
            f"Capital kept moving: {mna['deal_count']} tracked data-center "
            f"M&A deals closed in {label} worth ${mna['deal_value_b']}B — an "
            f"annualized run-rate of roughly ${mna['annualized_b']}B against "
            f"DC Hub's ${round(stats['mna_usd']/1e9)}B+ historical deal "
            f"database."
        )
    else:
        paras.append(
            f"DC Hub tracks ${round(stats['mna_usd']/1e9)}B+ in data-center "
            f"M&A across the full deal history; per-quarter deal flow is "
            f"detailed in the transactions database."
        )

    # ── Quick bullets ──
    if not fallback:
        bullets.append(f"{n_avoid} flipped to AVOID")
        bullets.append(f"{n_build} cleared to BUILD")
    bullets.append(f"{n_build_total} BUILD · {n_caution_total} CAUTION · "
                   f"{n_avoid_total} AVOID (live)")
    if mna and mna.get("deal_count"):
        bullets.append(f"{mna['deal_count']} M&A deals · ${mna['deal_value_b']}B")
    bullets.append(f"{stats['facilities']:,} facilities tracked")

    headline = (
        f"{n_avoid} market{'s' if n_avoid != 1 else ''} flipped to AVOID"
        if (not fallback and n_avoid > 0)
        else f"{n_build_total} BUILD · {n_avoid_total} AVOID"
    )

    return {"headline": headline, "paragraphs": paras, "bullets": bullets}


def _citation(qinfo: dict, accessed: str) -> dict:
    url = f"https://dchub.cloud/state-of-power/{qinfo['slug']}"
    apa = (f"DC Hub. ({qinfo['year']}). The State of Data Center Power — "
           f"{qinfo['label']}. {url} (accessed {accessed}). Licensed CC-BY-4.0.")
    bibtex = (
        "@misc{dchub_state_of_power_" + qinfo["slug"].replace("-", "_") + ",\n"
        "  author       = {{DC Hub}},\n"
        f"  title        = {{The State of Data Center Power --- {qinfo['label']}}},\n"
        f"  year         = {{{qinfo['year']}}},\n"
        f"  howpublished = {{\\url{{{url}}}}},\n"
        f"  note         = {{Accessed {accessed}. Licensed CC-BY-4.0.}}\n"
        "}"
    )
    return {"url": url, "apa": apa, "bibtex": bibtex, "license": "CC-BY-4.0"}


def _gather(qinfo: dict) -> dict:
    """Assemble the full quarterly report payload (the data both the HTML
    and the JSON render). Each sub-gather is independently guarded."""
    accessed = datetime.date.today().isoformat()
    stats = _headline_stats()
    energy = _energy_block()
    shifts = _verdict_shifts(qinfo)
    mna = _mna_runrate(qinfo)
    scores = _dcpi_scores()
    narrative = _narrative(qinfo, shifts, energy, mna, stats)

    slug = f"state-of-power-{qinfo['slug']}"
    out = {
        "report":          "The State of Data Center Power — Quarterly",
        "quarter":         qinfo["slug"],
        "quarter_label":   qinfo["label"],
        "temporal_coverage": f"{qinfo['start'].isoformat()}/{qinfo['end'].isoformat()}",
        "is_current":      qinfo["is_current"],
        "generated_at":    datetime.datetime.utcnow().isoformat() + "Z",
        "as_of_date":      accessed,
        "url":             f"https://dchub.cloud/state-of-power/{qinfo['slug']}",
        "og_slug":         slug,
        "og_image":        f"https://dchub.cloud/api/v1/og/today/{slug}.png",
        "license": {
            "id": "CC-BY-4.0",
            "name": "Creative Commons Attribution 4.0 International",
            "url": _LICENSE_URL,
            "attribution_required": True,
            "commercial_use_allowed": True,
        },
        "headline_stats": {
            "facilities":   stats["facilities"],
            "markets":      energy.get("markets_scored_total") or stats["markets"],
            "isos":         stats["isos"],
            "substations":  stats["substations"],
            "mna_usd":      stats["mna_usd"],
            "mna_b":        round(stats["mna_usd"] / 1e9),
            "pipeline_gw":  stats["pipeline_gw"],
        },
        "narrative":            narrative,
        "verdict_shifts":       shifts,
        "verdict_distribution": energy.get("verdict_distribution") or {},
        "top_build_markets":    energy.get("top_build_markets") or [],
        "top_avoid_markets":    energy.get("top_avoid_markets") or [],
        "iso_rollup":           energy.get("iso_rollup") or [],
        "ma_runrate":           mna,
        "dcpi_scores":          scores,
        "downloads": {
            "json": f"https://dchub.cloud/api/v1/reports/quarterly/{qinfo['slug']}.json",
            "csv":  f"https://dchub.cloud/api/v1/reports/quarterly/{qinfo['slug']}.csv",
            "html": f"https://dchub.cloud/state-of-power/{qinfo['slug']}",
        },
    }
    out["citation"] = _citation(qinfo, accessed)
    return out


# ─────────────────────────────────────────────────────────────────────
# JSON-LD (Dataset + Report/Article) — mirrors state_of_power.py
# ─────────────────────────────────────────────────────────────────────
def _jsonld(d: dict) -> str:
    q = d["quarter_label"]
    csv_url = d["downloads"]["csv"]
    json_url = d["downloads"]["json"]
    return json.dumps({
        "@context": "https://schema.org",
        "@type": ["Dataset", "Report"],
        "name": f"The State of Data Center Power — {q}",
        "alternateName": f"DC Hub Quarterly State of Power · {q}",
        "headline": d["narrative"]["headline"],
        "description": (
            f"DC Hub's quarterly State of Data Center Power for {q}: the "
            f"markets that flipped to BUILD or AVOID this quarter, the new "
            f"BUILD gravity centers, grid-stress distribution across scored "
            f"markets via the Data Center Power Index (DCPI), and data-center "
            f"M&A run-rate. Machine-readable and CC-BY-4.0."),
        "url": d["url"],
        "image": d["og_image"],
        "datePublished": d["generated_at"],
        "dateModified": d["generated_at"],
        "temporalCoverage": d["temporal_coverage"],
        "license": _LICENSE_URL,
        "isAccessibleForFree": True,
        "creator": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "publisher": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
        "keywords": ["data center", "power availability", "DCPI", "BUILD verdict",
                     "AVOID verdict", "interconnection queue", "ISO grid",
                     "AI data center", "site selection", "data center M&A",
                     q.lower()],
        "spatialCoverage": "United States + international DCPI markets",
        "measurementTechnique": ("DCPI: Excess Power score + Constraint score → "
                                 "BUILD/CAUTION/AVOID verdict, recomputed daily; "
                                 "quarterly verdict shifts derived from the daily "
                                 "snapshot history."),
        "variableMeasured": [
            {"@type": "PropertyValue", "name": "DCPI Verdict", "description": "BUILD | CAUTION | AVOID"},
            {"@type": "PropertyValue", "name": "Excess Power Score", "minValue": 0, "maxValue": 100},
            {"@type": "PropertyValue", "name": "Constraint Score", "minValue": 0, "maxValue": 100},
            {"@type": "PropertyValue", "name": "Time to Power (months)"},
            {"@type": "PropertyValue", "name": "Quarter verdict shift", "description": "prior → current verdict"},
        ],
        "distribution": [
            {"@type": "DataDownload", "encodingFormat": "text/csv", "contentUrl": csv_url},
            {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": json_url},
            {"@type": "DataDownload", "encodingFormat": "text/html", "contentUrl": d["url"]},
        ],
        "citation": d["citation"]["apa"],
        "creditText": d["citation"]["apa"],
        "isPartOf": {"@type": "Dataset", "name": "The State of Data Center Power",
                     "url": "https://dchub.cloud/state-of-power"},
    }, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────
# HTML render
# ─────────────────────────────────────────────────────────────────────
def _esc(v) -> str:
    return _html.escape("" if v is None else str(v))


def _render_html(d: dict) -> str:
    s = d["headline_stats"]
    shifts = d["verdict_shifts"]
    vd = d["verdict_distribution"] or {}
    narr = d["narrative"]
    cite = d["citation"]
    q = d["quarter_label"]

    narr_html = "\n".join(f"<p>{_esc(p)}</p>" for p in narr["paragraphs"])
    bullets_html = "".join(f"<span class='chip'>{_esc(b)}</span>" for b in narr["bullets"])

    def _shift_rows(items, show_iso=False):
        out = []
        for m in items[:25]:
            arrow = (f"{_esc(m.get('was'))} &rarr; {_esc(m.get('now'))}"
                     if m.get("was") else _esc(m.get("now")))
            extra = (f"<td>{_esc(m.get('iso','—'))}</td>" if show_iso else "")
            out.append(
                f"<tr><td><a href='{_esc(m.get('page',''))}'><strong>"
                f"{_esc(m.get('market','?'))}</strong></a></td>{extra}"
                f"<td>{arrow}</td>"
                f"<td style='text-align:right'>{m.get('excess') if m.get('excess') is not None else '—'}</td>"
                f"<td style='text-align:right'>{m.get('constraint') if m.get('constraint') is not None else '—'}</td></tr>")
        return "\n".join(out)

    avoid_rows = _shift_rows(shifts.get("to_avoid") or [], show_iso=shifts.get("fallback"))
    build_rows = _shift_rows(shifts.get("to_build") or [], show_iso=shifts.get("fallback"))
    avoid_iso_th = "<th>ISO</th>" if shifts.get("fallback") else ""
    build_iso_th = "<th>ISO</th>" if shifts.get("fallback") else ""

    # DCPI scores table (top 30 for the page; full set in the CSV/JSON).
    scores = d["dcpi_scores"] or []
    score_rows = "\n".join(
        f"<tr><td><a href='https://dchub.cloud/dcpi/{_esc(r.get('slug'))}'>"
        f"{_esc(r.get('market'))}</a></td>"
        f"<td>{_esc(r.get('iso','—'))}</td>"
        f"<td class='v v-{(r.get('verdict') or '').lower()}'>{_esc(r.get('verdict','—'))}</td>"
        f"<td style='text-align:right'>{r.get('excess') if r.get('excess') is not None else '—'}</td>"
        f"<td style='text-align:right'>{r.get('constraint') if r.get('constraint') is not None else '—'}</td>"
        f"<td style='text-align:right'>{r.get('ttp_months') if r.get('ttp_months') is not None else '—'}</td></tr>"
        for r in scores[:30]
    ) or "<tr><td colspan='6' style='text-align:center;color:#71717a'><em>DCPI scores loading.</em></td></tr>"

    shift_lead = ("Current decisive verdicts (quarter-over-quarter shift "
                  "tracking begins after a full quarter of daily snapshots)."
                  if shifts.get("fallback")
                  else f"Markets whose DCPI verdict changed during {q}.")

    csv_url = d["downloads"]["csv"]
    json_url = d["downloads"]["json"]

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>The State of Data Center Power — {_esc(q)} — DC Hub</title>
<meta name="description" content="{_esc(q)}: {_esc(narr['headline'])}. DC Hub's quarterly State of Data Center Power — DCPI verdict shifts, new BUILD gravity centers, grid stress, and M&amp;A run-rate. CC-BY-4.0.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="{_esc(d['url'])}">
<meta property="og:type" content="article">
<meta property="og:title" content="The State of Data Center Power — {_esc(q)}">
<meta property="og:description" content="{_esc(narr['headline'])} · {vd.get('BUILD',0)} BUILD · {vd.get('AVOID',0)} AVOID · DCPI quarterly · CC-BY-4.0">
<meta property="og:url" content="{_esc(d['url'])}">
<meta property="og:image" content="{_esc(d['og_image'])}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="The State of Data Center Power — {_esc(q)}">
<meta name="twitter:image" content="{_esc(d['og_image'])}">
<script type="application/ld+json">{_jsonld(d)}</script>
<style>
  :root {{ --bg:#070b16; --card:rgba(255,255,255,.04); --ink:#e5e7eb; --mut:#94a3b8; --acc:#6366f1; --grn:#10b981; --amb:#f59e0b; --red:#ef4444; --pur:#a855f7 }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,Inter,sans-serif; line-height:1.55 }}
  .wrap {{ max-width:1040px; margin:0 auto; padding:64px 28px 110px }}
  .eyebrow {{ font-family:ui-monospace,Menlo,monospace; font-size:11px; text-transform:uppercase; letter-spacing:.14em; color:var(--acc); margin-bottom:14px }}
  h1 {{ font-size:46px; line-height:1.06; margin:0 0 14px; letter-spacing:-.025em }}
  h2 {{ font-size:23px; margin:54px 0 12px; letter-spacing:-.01em }}
  .lede {{ font-size:19px; color:#cbd5e1; margin:0 0 28px; max-width:72ch }}
  .stats {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin:28px 0 8px }}
  .stat {{ background:var(--card); padding:18px 16px; border-radius:10px; border-left:3px solid var(--acc) }}
  .stat-num {{ font-size:28px; font-weight:800; display:block; letter-spacing:-.02em }}
  .stat-lbl {{ font-family:ui-monospace,Menlo,monospace; font-size:10.5px; text-transform:uppercase; color:var(--mut); margin-top:6px; letter-spacing:.06em }}
  .verdicts {{ display:flex; gap:10px; flex-wrap:wrap; margin:8px 0 0 }}
  .vpill {{ padding:8px 16px; border-radius:999px; font-weight:700; font-size:14px; background:var(--card) }}
  .vbuild {{ color:var(--grn); border:1px solid rgba(16,185,129,.4) }}
  .vcaution {{ color:var(--amb); border:1px solid rgba(245,158,11,.4) }}
  .vavoid {{ color:var(--red); border:1px solid rgba(239,68,68,.4) }}
  .narr {{ background:rgba(99,102,241,.06); border-left:3px solid var(--acc); padding:22px 26px; border-radius:8px; margin:24px 0 18px }}
  .narr p {{ margin:0 0 12px; font-size:16px; line-height:1.65 }} .narr p:last-child {{ margin:0 }}
  .chips {{ display:flex; gap:8px; flex-wrap:wrap; margin:0 0 8px }}
  .chip {{ background:var(--card); border:1px solid rgba(99,102,241,.3); border-radius:999px; padding:6px 13px; font-size:12.5px; font-weight:600; color:#c7d2fe }}
  table {{ width:100%; border-collapse:collapse; margin:14px 0 28px }}
  th, td {{ padding:11px 13px; text-align:left; font-size:14px; border-bottom:1px solid rgba(255,255,255,.06); vertical-align:top }}
  th {{ background:rgba(255,255,255,.03); font-family:ui-monospace,Menlo,monospace; font-size:10.5px; text-transform:uppercase; letter-spacing:.08em; color:var(--mut) }}
  a {{ color:#93c5fd }}
  .v {{ font-weight:700 }} .v-build {{ color:var(--grn) }} .v-caution {{ color:var(--amb) }} .v-avoid {{ color:var(--red) }}
  .cta {{ background:rgba(16,185,129,.07); border:1px solid rgba(16,185,129,.3); border-radius:12px; padding:26px 28px; margin:40px 0 8px }}
  .cta h3 {{ margin:0 0 8px; font-size:20px }}
  .cta p {{ margin:0 0 16px; color:#cbd5e1; font-size:15px }}
  .cta form {{ display:flex; gap:10px; flex-wrap:wrap }}
  .cta input[type=email] {{ flex:1; min-width:220px; padding:12px 14px; border-radius:8px; border:1px solid rgba(255,255,255,.15); background:rgba(0,0,0,.25); color:var(--ink); font-size:15px }}
  .cta button {{ padding:12px 22px; border:0; border-radius:8px; background:var(--grn); color:#062012; font-weight:700; font-size:15px; cursor:pointer }}
  .cta small {{ display:block; margin-top:10px; color:#64748b; font-size:12px }}
  .dl {{ display:flex; gap:10px; flex-wrap:wrap; margin:4px 0 0 }}
  .dl a {{ background:var(--card); border:1px solid rgba(255,255,255,.12); border-radius:8px; padding:9px 15px; text-decoration:none; color:#c7d2fe; font-size:13px; font-weight:600 }}
  .cite {{ background:rgba(99,102,241,.08); border-left:3px solid var(--acc); padding:20px 24px; border-radius:8px; margin:24px 0 }}
  .cite code {{ display:block; background:rgba(255,255,255,.05); padding:12px 14px; border-radius:6px; font-size:13px; color:#c7d2fe; font-family:ui-monospace,Menlo,monospace; white-space:pre-wrap; word-break:break-word; margin-top:10px }}
  .license-foot {{ background:rgba(16,185,129,.06); border-left:3px solid var(--grn); padding:20px 24px; border-radius:8px; margin-top:48px; font-size:14px; color:#cbd5e1 }}
  @media (max-width:720px) {{ h1 {{ font-size:32px }} .stats {{ grid-template-columns:1fr 1fr }} .wrap {{ padding:40px 20px 80px }} }}
</style></head><body>
<div class="wrap">
  <div class="eyebrow">DC Hub · The State of Data Center Power · {_esc(q)} · CC-BY-4.0</div>
  <h1>The State of Data Center Power<br><span style="color:var(--acc)">{_esc(q)}</span></h1>
  <p class="lede">The quarter's answer to the only question that matters for an AI build: <strong>where did power get scarcer, and where did it open up?</strong> Computed from DC Hub's Data Center Power Index — refreshed daily, citable, CC-BY-4.0.</p>

  <div class="stats">
    <div class="stat"><span class="stat-num">{s['facilities']:,}</span><span class="stat-lbl">Facilities tracked</span></div>
    <div class="stat"><span class="stat-num">{_esc(s['markets'])}</span><span class="stat-lbl">DCPI markets</span></div>
    <div class="stat"><span class="stat-num">{_esc(s['isos'])}</span><span class="stat-lbl">ISOs / BAs</span></div>
    <div class="stat"><span class="stat-num">${_esc(s['mna_b'])}B+</span><span class="stat-lbl">M&amp;A tracked</span></div>
  </div>
  <div class="verdicts">
    <span class="vpill vbuild">{vd.get('BUILD',0)} BUILD</span>
    <span class="vpill vcaution">{vd.get('CAUTION',0)} CAUTION</span>
    <span class="vpill vavoid">{vd.get('AVOID',0)} AVOID</span>
    <span class="vpill" style="color:var(--mut)">{_esc(s['pipeline_gw'])} GW pipeline</span>
  </div>

  <h2>The quarter in brief</h2>
  <div class="chips">{bullets_html}</div>
  <div class="narr">{narr_html}</div>

  <h2>Markets that flipped to AVOID</h2>
  <p style="color:var(--mut);margin:0 0 6px">{_esc(shift_lead)} An AVOID verdict signals the grid can no longer absorb new large load without multi-year waits.</p>
  <table>
    <thead><tr><th>Market</th>{avoid_iso_th}<th>Verdict</th><th style="text-align:right">Excess Power</th><th style="text-align:right">Constraint</th></tr></thead>
    <tbody>{avoid_rows or "<tr><td colspan='5' style='text-align:center;color:#71717a'><em>No markets flipped to AVOID this quarter.</em></td></tr>"}</tbody>
  </table>

  <h2>New BUILD gravity centers</h2>
  <p style="color:var(--mut);margin:0 0 6px">Markets that cleared to a BUILD verdict — excess generation, queue velocity, and transmission headroom now favor construction.</p>
  <table>
    <thead><tr><th>Market</th>{build_iso_th}<th>Verdict</th><th style="text-align:right">Excess Power</th><th style="text-align:right">Constraint</th></tr></thead>
    <tbody>{build_rows or "<tr><td colspan='5' style='text-align:center;color:#71717a'><em>No new BUILD markets this quarter.</em></td></tr>"}</tbody>
  </table>

  <h2>DCPI market scores</h2>
  <p style="color:var(--mut);margin:0 0 6px">Top markets by verdict + Excess Power. <a href="{_esc(csv_url)}">Full CSV ({len(scores)} markets) &rarr;</a> · <a href="https://dchub.cloud/state-of-power/methodology">How this is scored &rarr;</a></p>
  <table>
    <thead><tr><th>Market</th><th>ISO</th><th>Verdict</th><th style="text-align:right">Excess</th><th style="text-align:right">Constraint</th><th style="text-align:right">TTP (mo)</th></tr></thead>
    <tbody>{score_rows}</tbody>
  </table>

  <div class="cta">
    <h3>Get the full dataset + quarterly briefing</h3>
    <p>The complete {_esc(q)} DCPI scores, verdict-shift history, and the next quarterly briefing — delivered to your inbox. Free.</p>
    <form action="https://api.dchub.cloud/pricing/checkout/submit" method="POST">
      <input type="hidden" name="tool" value="{_esc(d['og_slug'])}">
      <input type="hidden" name="tier" value="developer">
      <input type="email" name="email" placeholder="you@company.com" required aria-label="Email address">
      <button type="submit">Send me the dataset</button>
    </form>
    <small>Or grab it now:
      <span class="dl" style="display:inline-flex">
        <a href="{_esc(csv_url)}">CSV</a>
        <a href="{_esc(json_url)}">JSON</a>
      </span> — both CC-BY-4.0, no signup required.
    </small>
  </div>

  <h2>Cite this</h2>
  <div class="cite">
    Permanent URL: <a href="{_esc(d['url'])}">{_esc(d['url'])}</a>
    <code>{_esc(cite['apa'])}</code>
  </div>

  <div class="license-foot">
    <span style="display:inline-block;padding:3px 9px;background:var(--grn);color:#062012;font-weight:700;border-radius:4px;font-size:11px;letter-spacing:.5px;margin-right:10px">CC-BY-4.0</span>
    Licensed under <a rel="license" href="{_LICENSE_URL}">Creative Commons Attribution 4.0 International</a>.
    Quote, chart, and republish freely with attribution to DC Hub.
  </div>

  <p style="text-align:center;color:#64748b;font-size:13px;margin-top:48px">
    DC Hub · <a href="/">dchub.cloud</a> ·
    <a href="/state-of-power">State of Power (live)</a> ·
    <a href="{_esc(json_url)}">JSON</a> ·
    <a href="{_esc(csv_url)}">CSV</a> ·
    <a href="/state-of-power/methodology">Methodology</a>
  </p>
</div>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────
# CSV render — DCPI market scores + this-quarter verdict shifts
# ─────────────────────────────────────────────────────────────────────
def _render_csv(d: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    q = d["quarter"]
    # Provenance header rows (commented with #) — license is explicit.
    w.writerow([f"# DC Hub — The State of Data Center Power — {d['quarter_label']}"])
    w.writerow([f"# Source: {d['url']}"])
    w.writerow([f"# License: CC-BY-4.0 ({_LICENSE_URL})"])
    w.writerow([f"# Citation: {d['citation']['apa']}"])
    w.writerow([f"# Generated: {d['generated_at']}"])
    w.writerow([])

    # Section 1 — DCPI market scores.
    w.writerow(["section", "market", "slug", "state", "iso", "verdict",
                "excess_power_score", "constraint_score", "time_to_power_months",
                "computed_at"])
    for r in (d["dcpi_scores"] or []):
        w.writerow(["dcpi_score", r.get("market"), r.get("slug"), r.get("state"),
                    r.get("iso"), r.get("verdict"),
                    r.get("excess"), r.get("constraint"), r.get("ttp_months"),
                    r.get("computed_at")])

    # Section 2 — this-quarter verdict shifts (the lead story, as data).
    w.writerow([])
    w.writerow(["section", "market", "slug", "prior_verdict", "current_verdict",
                "excess_power_score", "constraint_score", "quarter"])
    for r in (d["verdict_shifts"].get("all_shifts") or []):
        w.writerow(["verdict_shift", r.get("market"), r.get("slug"),
                    r.get("was"), r.get("now"),
                    r.get("excess"), r.get("constraint"), q])
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────
# Headers
# ─────────────────────────────────────────────────────────────────────
def _html_headers():
    return {"Cache-Control": "public, max-age=900, s-maxage=3600",
            "Link": _CC_LINK_HEADER, "X-License": "CC-BY-4.0"}


def _json_headers():
    return {"Cache-Control": "public, max-age=900, s-maxage=3600",
            "Link": _CC_LINK_HEADER, "X-License": "CC-BY-4.0",
            "Access-Control-Allow-Origin": "*"}


def _404(quarter_raw: str):
    """Clean 404 for a malformed quarter param."""
    body = jsonify({
        "error": "invalid_quarter",
        "detail": (f"'{quarter_raw}' is not a valid quarter. Use the form "
                   f"q<1-4>-<year> (e.g. q3-2026), or 'latest'/'current'."),
        "current_quarter": _current_quarter_slug(),
        "example": "/state-of-power/q3-2026",
    })
    return body, 404, {"Access-Control-Allow-Origin": "*"}


# ─────────────────────────────────────────────────────────────────────
# Routes — the NEW quarterly State-of-Power surface
# ─────────────────────────────────────────────────────────────────────
@quarterly_report_bp.route("/state-of-power/quarterly",
                           methods=["GET"], strict_slashes=False)
def state_of_power_quarterly_redirect():
    """302 → the current quarter's permanent URL."""
    return redirect(f"/state-of-power/{_current_quarter_slug()}", code=302)


@quarterly_report_bp.route("/state-of-power/<quarter>",
                           methods=["GET"], strict_slashes=False)
def state_of_power_quarter_html(quarter):
    """Server-rendered HTML quarterly report for <quarter>."""
    qinfo = _parse_quarter(quarter)
    if not qinfo:
        return _404(quarter)
    try:
        d = _gather(qinfo)
        return Response(_render_html(d), mimetype="text/html",
                        headers=_html_headers())
    except Exception as e:
        # Last-resort guard: never 500 a public report. Render a minimal
        # but valid page from canonical constants.
        logger.warning(f"quarterly_report HTML render failed for {quarter}: {e}")
        return Response(_render_minimal(qinfo), mimetype="text/html",
                        headers=_html_headers())


@quarterly_report_bp.route("/api/v1/reports/quarterly/<quarter>.json",
                           methods=["GET"])
def quarterly_json(quarter):
    """Structured report payload (same data the HTML renders)."""
    qinfo = _parse_quarter(quarter)
    if not qinfo:
        return _404(quarter)
    try:
        d = _gather(qinfo)
    except Exception as e:
        logger.warning(f"quarterly_report JSON gather failed for {quarter}: {e}")
        d = {"report": "The State of Data Center Power — Quarterly",
             "quarter": qinfo["slug"], "quarter_label": qinfo["label"],
             "error": "partial_data", "headline_stats": dict(_CANON),
             "license": {"id": "CC-BY-4.0", "url": _LICENSE_URL}}
    return jsonify(d), 200, _json_headers()


@quarterly_report_bp.route("/api/v1/reports/quarterly/<quarter>.csv",
                           methods=["GET"])
def quarterly_csv(quarter):
    """CSV of the DCPI market scores + this-quarter verdict shifts."""
    qinfo = _parse_quarter(quarter)
    if not qinfo:
        return _404(quarter)
    try:
        d = _gather(qinfo)
        body = _render_csv(d)
    except Exception as e:
        logger.warning(f"quarterly_report CSV render failed for {quarter}: {e}")
        body = (f"# DC Hub — The State of Data Center Power — {qinfo['label']}\n"
                f"# License: CC-BY-4.0 ({_LICENSE_URL})\n"
                f"# error: partial_data\n")
    headers = {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f'attachment; filename="dchub-state-of-power-{qinfo["slug"]}.csv"',
        "Cache-Control": "public, max-age=900, s-maxage=3600",
        "Link": _CC_LINK_HEADER,
        "X-License": "CC-BY-4.0",
        "Access-Control-Allow-Origin": "*",
    }
    return Response(body, headers=headers)


def _render_minimal(qinfo: dict) -> str:
    """Absolute fallback HTML — valid, on-brand, never blank."""
    q = qinfo["label"]
    url = f"https://dchub.cloud/state-of-power/{qinfo['slug']}"
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>The State of Data Center Power — {_esc(q)} — DC Hub</title>
<link rel="canonical" href="{_esc(url)}">
<meta name="description" content="DC Hub's quarterly State of Data Center Power for {_esc(q)} — DCPI verdicts across {_CANON['markets']} markets. CC-BY-4.0.">
<style>body{{background:#070b16;color:#e5e7eb;font-family:-apple-system,sans-serif;max-width:760px;margin:0 auto;padding:64px 24px;line-height:1.6}}a{{color:#93c5fd}}</style>
</head><body>
<h1>The State of Data Center Power — {_esc(q)}</h1>
<p>DC Hub tracks {_CANON['facilities']:,}+ data-center facilities, {_CANON['markets']} DCPI markets, and ${round(_CANON['mna_usd']/1e9)}B+ in M&amp;A. Live verdicts: <a href="/state-of-power">/state-of-power</a>.</p>
<p>Machine-readable: <a href="/api/v1/reports/quarterly/{_esc(qinfo['slug'])}.json">JSON</a> · <a href="/api/v1/reports/quarterly/{_esc(qinfo['slug'])}.csv">CSV</a>. Licensed CC-BY-4.0.</p>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────
# PRESERVED — the original /reports/quarterly surface (Phase AAAAA,
# 2026-05-16). Kept verbatim so the existing live routes + any inbound
# links don't regress when this module is replaced. Same blueprint.
# ─────────────────────────────────────────────────────────────────────
def _legacy_conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _legacy_current_quarter():
    today = datetime.date.today()
    return today.year, (today.month - 1) // 3 + 1


def _legacy_compute_report_data() -> dict:
    out: dict = {
        "generated_at":   datetime.datetime.utcnow().isoformat() + "Z",
        "quarter_label":  f"Q{_legacy_current_quarter()[1]} {_legacy_current_quarter()[0]}",
        "year":           _legacy_current_quarter()[0],
        "quarter":        _legacy_current_quarter()[1],
    }
    c = _legacy_conn()
    if c is None:
        out["error"] = "no_database"
        return out
    try:
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT COUNT(*), COALESCE(SUM(power_mw),0)
                      FROM discovered_facilities
                     WHERE merged_at IS NULL AND is_duplicate = 0
                """)
                r = cur.fetchone() or (0, 0)
                out["headline"] = {"facilities": int(r[0] or 0), "total_mw": float(r[1] or 0)}
            except Exception:
                pass
            try:
                cur.execute("""
                    SELECT market_name, score, weekly_delta
                      FROM market_power_scores
                     WHERE published = true AND weekly_delta IS NOT NULL
                     ORDER BY ABS(weekly_delta) DESC LIMIT 10
                """)
                out["dcpi_movers"] = [
                    {"market": r[0], "score": int(r[1] or 0), "delta": int(r[2] or 0)}
                    for r in cur.fetchall()]
            except Exception:
                out["dcpi_movers"] = []
            try:
                cur.execute("""
                    WITH latest AS (
                      SELECT DISTINCT ON (market_slug) verdict
                        FROM market_power_scores WHERE published = true
                       ORDER BY market_slug, computed_at DESC)
                    SELECT verdict, COUNT(*) FROM latest GROUP BY verdict
                """)
                vd = {(r[0] or '').upper(): int(r[1] or 0) for r in cur.fetchall()}
                out["dcpi_verdicts"] = {
                    "build": vd.get("BUILD", 0), "caution": vd.get("CAUTION", 0),
                    "avoid": vd.get("AVOID", 0), "total": sum(vd.values())}
                cur.execute("""
                    SELECT DISTINCT ON (market_slug) market_name, iso,
                           excess_power_score, constraint_score
                      FROM market_power_scores
                     WHERE published = true AND verdict = 'BUILD'
                     ORDER BY market_slug, computed_at DESC
                """)
                rows = sorted(cur.fetchall(), key=lambda r: -(r[2] or 0))[:8]
                out["top_build"] = [
                    {"market": r[0], "iso": r[1], "excess": int(r[2] or 0),
                     "constraint": int(r[3] or 0)} for r in rows]
            except Exception:
                out["dcpi_verdicts"] = {}
                out["top_build"] = []
            try:
                cur.execute("""
                    SELECT COALESCE(market, city, '') AS m, COUNT(*) AS n,
                           COALESCE(SUM(power_mw), 0) AS mw
                      FROM discovered_facilities
                     WHERE merged_at IS NULL AND is_duplicate = 0
                       AND COALESCE(market, city) IS NOT NULL
                     GROUP BY COALESCE(market, city)
                     ORDER BY mw DESC LIMIT 10
                """)
                out["top_markets"] = [
                    {"market": r[0], "facilities": int(r[1]), "total_mw": float(r[2] or 0)}
                    for r in cur.fetchall() if r[0]]
            except Exception:
                out["top_markets"] = []
            try:
                cur.execute("""
                    SELECT COUNT(*), COALESCE(SUM(value),0), COALESCE(SUM(mw),0)
                      FROM deals
                     WHERE date >= (CURRENT_DATE - INTERVAL '90 days')
                """)
                r = cur.fetchone() or (0, 0, 0)
                out["ma_summary"] = {"deal_count": int(r[0] or 0),
                                     "total_value": float(r[1] or 0),
                                     "total_mw": float(r[2] or 0)}
                cur.execute("""
                    SELECT id, date, buyer, seller, value, mw
                      FROM deals
                     WHERE value IS NOT NULL
                       AND date >= (CURRENT_DATE - INTERVAL '90 days')
                     ORDER BY value DESC LIMIT 5
                """)
                out["ma_summary"]["top_deals"] = [{
                    "id": int(r[0]) if r[0] else None,
                    "date": r[1].isoformat() if hasattr(r[1], "isoformat") else (str(r[1]) if r[1] else None),
                    "buyer": r[2], "seller": r[3],
                    "value": float(r[4]) if r[4] is not None else None,
                    "mw": float(r[5]) if r[5] is not None else None,
                } for r in cur.fetchall()]
            except Exception:
                out["ma_summary"] = {"deal_count": 0, "total_value": 0, "top_deals": []}
            try:
                cur.execute("""
                    SELECT COALESCE(market, city, '') AS m, COUNT(*) AS n,
                           COALESCE(SUM(power_mw), 0) AS mw
                      FROM discovered_facilities
                     WHERE merged_at IS NULL AND is_duplicate = 0
                       AND LOWER(COALESCE(status,'')) IN
                          ('construction','planned','permitting',
                           'under construction','proposed','development')
                       AND COALESCE(market, city) IS NOT NULL
                     GROUP BY COALESCE(market, city)
                     ORDER BY mw DESC LIMIT 10
                """)
                out["pipeline_by_market"] = [
                    {"market": r[0], "projects": int(r[1]), "mw": float(r[2] or 0)}
                    for r in cur.fetchall() if r[0]]
            except Exception:
                out["pipeline_by_market"] = []
            try:
                cur.execute("SELECT score_pct FROM citation_scores ORDER BY score_date DESC LIMIT 1")
                r = cur.fetchone()
                out["brand_pulse"] = {"citation_score": float(r[0] or 0) if r else None,
                                      "source_of_truth": None}
            except Exception:
                out["brand_pulse"] = {"citation_score": None}
            try:
                cur.execute("SELECT source_of_truth_score FROM media_pulse_snapshots ORDER BY snapshot_date DESC LIMIT 1")
                r = cur.fetchone()
                if r and "brand_pulse" in out:
                    out["brand_pulse"]["source_of_truth"] = int(r[0] or 0)
            except Exception:
                pass
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


def _legacy_render_html(d: dict) -> str:
    h = d.get("headline") or {}
    ma = d.get("ma_summary") or {}
    bp = d.get("brand_pulse") or {}

    movers_rows = "".join(
        f'<tr><td>{m["market"]}</td><td>{m["score"]}/100</td>'
        f'<td style="color:{"#16a34a" if m["delta"]>0 else "#dc2626"}">{"+" if m["delta"]>0 else ""}{m["delta"]}</td></tr>'
        for m in (d.get("dcpi_movers") or [])
    ) or '<tr><td colspan=3 style="color:#9ca3af">No DCPI movers tracked.</td></tr>'
    markets_rows = "".join(
        f'<tr><td>{m["market"]}</td><td>{m["facilities"]:,}</td><td>{m["total_mw"]:,.0f}</td></tr>'
        for m in (d.get("top_markets") or [])
    ) or '<tr><td colspan=3 style="color:#9ca3af">No market data.</td></tr>'
    pipeline_rows = "".join(
        f'<tr><td>{m["market"]}</td><td>{m["projects"]:,}</td><td>{m["mw"]:,.0f}</td></tr>'
        for m in (d.get("pipeline_by_market") or [])
    ) or '<tr><td colspan=3 style="color:#9ca3af">No pipeline tracked.</td></tr>'
    deals_rows = "".join(
        f'<tr><td>{x["date"] or "—"}</td><td>{x["buyer"] or "?"}</td><td>{x["seller"] or "?"}</td>'
        f'<td>${(x["value"] or 0):,.0f}</td><td>{(x["mw"] or 0):,.0f}</td></tr>'
        for x in (ma.get("top_deals") or [])
    ) or '<tr><td colspan=5 style="color:#9ca3af">No deals in quarter.</td></tr>'
    vd = d.get("dcpi_verdicts") or {}
    build_rows = "".join(
        f'<tr><td>{m["market"]}</td><td>{m["iso"] or "—"}</td><td>{m["excess"]}</td><td>{m["constraint"]}</td></tr>'
        for m in (d.get("top_build") or [])
    ) or '<tr><td colspan=4 style="color:#9ca3af">No BUILD markets this quarter.</td></tr>'

    return f"""<!doctype html><html lang=en>
<head><meta charset=utf-8>
<title>DC Hub Quarterly Report · {d.get('quarter_label','')}</title>
<meta name="description" content="DC Hub data-center market intelligence quarterly report. {h.get('facilities',0):,} facilities, {h.get('total_mw',0):,.0f} MW, {ma.get('deal_count',0)} deals tracked. Auto-generated from live data.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://dchub.cloud/reports/quarterly">
<meta property="og:type" content="article">
<meta property="og:title" content="DC Hub Quarterly — Data Center Market Intelligence · {d.get('quarter_label','')}">
<meta property="og:description" content="{vd.get('build',0)} BUILD markets, {h.get('facilities',0):,} facilities, {h.get('total_mw',0):,.0f} MW, ${(ma.get('total_value') or 0)/1e3:.1f}B M&amp;A — from DC Hub's live Data Center Power Index.">
<meta property="og:image" content="https://dchub.cloud/dcpi/og.svg">
<meta property="og:url" content="https://dchub.cloud/reports/quarterly">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="DC Hub Quarterly · {d.get('quarter_label','')}">
<script type="application/ld+json">{{
 "@context":"https://schema.org","@type":"Report",
 "name":"DC Hub Quarterly Report — {d.get('quarter_label','')}",
 "datePublished":"{d.get('generated_at','')}",
 "publisher":{{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"}},
 "about":[{{"@type":"Thing","name":"Data Center Market Intelligence"}}],
 "url":"https://dchub.cloud/reports/quarterly"
}}</script>
<style>
@page {{ size: letter; margin: 1in; }}
body{{font-family:Georgia,serif;max-width:780px;margin:0 auto;padding:2rem 1rem;color:#1f2937;line-height:1.6}}
h1{{font-family:-apple-system,sans-serif;font-size:2.2rem;margin:0 0 .25rem;border-bottom:3px solid #6366f1;padding-bottom:.5rem}}
h2{{font-family:-apple-system,sans-serif;font-size:1.25rem;margin:2rem 0 .5rem;color:#6366f1}}
.cover{{margin-bottom:2.5rem}}
.cover .quarter{{color:#6b7280;font-family:-apple-system,sans-serif;font-size:1rem;margin:.25rem 0}}
.headline{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.6rem;margin:1.5rem 0;padding:1rem 1.25rem;background:#f9fafb;border-radius:8px;font-family:-apple-system,sans-serif}}
.headline .stat{{font-size:.78rem;color:#6b7280;text-transform:uppercase}}
.headline .stat b{{display:block;font-size:1.5rem;color:#1f2937;font-family:Georgia,serif}}
table{{width:100%;border-collapse:collapse;margin:.5rem 0 1.5rem;font-family:-apple-system,sans-serif;font-size:.9rem}}
th{{text-align:left;padding:.4rem .6rem;background:#f3f4f6;font-size:.7rem;text-transform:uppercase;color:#6b7280;border-bottom:1px solid #e5e7eb}}
td{{padding:.35rem .6rem;border-bottom:1px solid #f3f4f6}}
.print-note{{background:#eef2ff;border:1px solid #c7d2fe;color:#3730a3;padding:.6rem 1rem;border-radius:6px;font-family:-apple-system,sans-serif;font-size:.85rem;margin:1rem 0}}
@media print {{ .print-note, .nav, .foot {{ display: none !important; }} }}
.foot{{color:#9ca3af;font-size:.85rem;margin-top:3rem;font-family:-apple-system,sans-serif;text-align:center}}
.foot a{{color:#6366f1;text-decoration:none}}
</style>
</head><body>
<div class="cover">
 <p class="quarter">DC Hub Industry Report</p>
 <h1>Data Center Market Intelligence</h1>
 <p class="quarter">{d.get('quarter_label','')} · Auto-generated from live data · {d.get('generated_at','')[:10]}</p>
</div>
<p class="print-note">📄 Use your browser's <strong>Print → Save as PDF</strong> for the PDF artifact your investors expect. Every number on this page comes from a live DC Hub API.</p>
<div class="headline">
 <div class="stat">Facilities tracked<b>{h.get('facilities',0):,}</b></div>
 <div class="stat">Total MW<b>{h.get('total_mw',0):,.0f}</b></div>
 <div class="stat">Deals (quarter)<b>{ma.get('deal_count',0):,}</b></div>
 <div class="stat">Deal $ volume<b>${(ma.get('total_value') or 0)/1e3:.1f}B</b></div>
 <div class="stat">Citation score<b>{(bp.get('citation_score') or 0):.0f}%</b></div>
 <div class="stat">SOT score<b>{bp.get('source_of_truth') or '—'}/100</b></div>
</div>
<h2>The Data Center Power Index — {d.get('quarter_label','')}</h2>
<p>DC Hub's <a href="https://dchub.cloud/dcpi">Data Center Power Index</a> scores every tracked U.S. market on excess power vs. grid constraint, then issues a <strong>BUILD / CAUTION / AVOID</strong> verdict — refreshed <em>daily</em>, not quarterly. This period's distribution across {vd.get('total',0)} scored markets:</p>
<div class="headline">
 <div class="stat">BUILD<b style="color:#16a34a">{vd.get('build',0)}</b></div>
 <div class="stat">CAUTION<b style="color:#d97706">{vd.get('caution',0)}</b></div>
 <div class="stat">AVOID<b style="color:#dc2626">{vd.get('avoid',0)}</b></div>
 <div class="stat">Markets scored<b>{vd.get('total',0)}</b></div>
</div>
<table><thead><tr><th>Top BUILD markets</th><th>ISO</th><th>Excess</th><th>Constraint</th></tr></thead>
<tbody>{build_rows}</tbody></table>
<h2>1. DCPI Top Movers (week-over-week)</h2>
<table><thead><tr><th>Market</th><th>Score</th><th>Δ</th></tr></thead>
<tbody>{movers_rows}</tbody></table>
<h2>2. Top Markets by Operating MW</h2>
<table><thead><tr><th>Market</th><th>Facilities</th><th>Operating MW</th></tr></thead>
<tbody>{markets_rows}</tbody></table>
<h2>3. M&amp;A Summary · last 90 days</h2>
<p>{ma.get('deal_count',0)} tracked deals · ${(ma.get('total_value') or 0)/1e3:.1f}B aggregate value · {(ma.get('total_mw') or 0):,.0f} MW changed hands.</p>
<table><thead><tr><th>Date</th><th>Buyer</th><th>Seller</th><th>Value</th><th>MW</th></tr></thead>
<tbody>{deals_rows}</tbody></table>
<h2>4. Construction Pipeline by Market</h2>
<table><thead><tr><th>Market</th><th>Projects</th><th>Pipeline MW</th></tr></thead>
<tbody>{pipeline_rows}</tbody></table>
<h2>5. About This Report</h2>
<p>This report is auto-generated quarterly from DC Hub's live data pipeline. Unlike static-research alternatives (DCHawk, dcByte) that ship printed PDFs every 90 days, this report is regenerated <em>nightly</em> and the underlying numbers update <em>continuously</em>. Every section links back to a live API endpoint at <code>dchub.cloud/api/v1/*</code>.</p>
<p>For real-time access, the same data is available via:</p>
<ul>
 <li>REST API — <code>/api/v1/dcpi/scores</code>, <code>/api/v1/transactions</code>, <code>/api/v1/facilities/delta</code></li>
 <li>MCP server — <code>https://dchub.cloud/mcp</code> with 28 tools for AI agents</li>
 <li>Live ops dashboard — <a href="/transparency">/transparency</a></li>
</ul>
<h2>6. Cite &amp; Contact</h2>
<p><strong>Cite this report:</strong> DC Hub, <em>Data Center Market Intelligence — {d.get('quarter_label','')}</em>, dchub.cloud/reports/quarterly. Underlying index: DC Hub Data Center Power Index (DCPI), dchub.cloud/dcpi. Methodology &amp; BibTeX: <a href="https://dchub.cloud/dcpi/methodology">dchub.cloud/dcpi/methodology</a>.</p>
<p><strong>Press &amp; data requests:</strong> press@dchub.cloud — DCPI verdict packages, biggest-mover alerts, and quarterly data available to editorial on request.</p>
<p style="color:#6b7280;font-size:.85rem;font-style:italic">Referenced by leading AI assistants: Gemini calls DC Hub "the definitive platform" for data-center capacity intelligence; ChatGPT, Claude, Perplexity, and Grok cite it as a primary source.</p>
<p class="foot">DC Hub · live source of truth · <a href="/dcpi">Data Center Power Index</a> · <a href="/vs">vs static competitors</a> · <a href="/transparency">ops console</a></p>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""


@quarterly_report_bp.route("/reports/quarterly", methods=["GET"],
                           strict_slashes=False)
def report_html():
    try:
        from routes.surface_brain import auto_log
        auto_log("quarterly_report", "view", target="/reports/quarterly")
    except Exception:
        pass
    d = _legacy_compute_report_data()
    html = _legacy_render_html(d)
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=3600"})


@quarterly_report_bp.route("/api/v1/reports/quarterly", methods=["GET"])
def report_json():
    d = _legacy_compute_report_data()
    resp = jsonify(d)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
