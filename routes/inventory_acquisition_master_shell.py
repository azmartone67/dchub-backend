"""
routes/inventory_acquisition_master_shell.py — Inventory-Acquisition Shell (#40, 2026-07-28).

★ WHY THIS EXISTS — and the mistake that produced it.

The operator asked how to grow inventory ("more data centers, more transmission, more
gas, more transformers, more fiber… we were 21.7 sites"). The first read of
discovered_facilities.first_seen showed 16 source labels all last-adding on
2026-03-18 and I reported "sixteen feeds died on the same day". That was WRONG.

Querying COUNT(DISTINCT date_trunc('minute', first_seen)) per source returned 1 for
every one of them, with an identical timestamp of 2026-03-18 03:29:40 — a single
bulk BACKFILL that stamped first_seen on ~1,700 pre-existing rows. Those labels were
never recurring crawlers; they were one-off imports and manual seeds. `first_seen`
means "when the backfill ran", not "when we discovered it".

The truth is narrower and harder: the recurring acquisition surface is THREE
registered crawlers (crawler_scheduler.py) plus news extraction —

    ('peeringdb', run_peeringdb_discovery), ('openstreetmap', run_osm_discovery),
    ('datacentermap', run_datacentermap_discovery)

— and datacentermap's cron is COMMENTED OUT (DataCenterMap raised a Vercel bot wall)
while DCM_CRAWL_ENABLED=true remains set in Railway, so it reads as armed. There is
essentially nothing to "restart": the machinery was never built past two live
crawlers. This shell measures the acquisition surface honestly so that stays visible.

LANES
  1. ACQUISITION SURFACE — how many recurring crawlers actually ADD rows, on what
     recency. Counts distinct-minute spread so a backfill can never again be
     mistaken for a live feed.
  2. DISTINCT vs RECORDS — growth must be in DISTINCT facilities (canonical_slug),
     not rows. Re-ingesting PeeringDB adds ~5,900 rows and ZERO facilities; that is
     how 22,775 records became "21.7k sites". Guards the duplicate-inflation path.
  3. OPERATOR-AUTHORITATIVE VERIFICATION — the cheapest bulk coverage available.
     AWS 200/508 = 39%, Digital Realty 421/686 = 61%, Google 54%, NTT 56%. These
     operators publish complete property lists (and REIT 10-Ks carry full property
     schedules), so hundreds verify per document.
  4. HELD-BUT-UNUSED SOURCES — credentials we already pay for / hold with no
     ingestion behind them: FCC_BDC (fiber), EPA_AQS + air_permitting_data.py (a
     large DC must permit backup generators BY NAME AND ADDRESS — an underused
     discovery vector), the LBNL interconnection-queue workbook in-repo.
  5. LAYER COVERAGE + LANDMINES — infra layers are healthy (substations 126,838
     0d, transmission 94,626 1d, gas 30,918 1d, fiber 55,064 3d) so effort does NOT
     belong there. Two real gaps: NO transformers table exists at all, and
     `power_plants` holds 66 rows 120d stale while 182k plants are published from a
     different table — a landmine for anyone who queries the obvious name.

★ Pure-DB (read replica) + repo-file reads. NO self-requests through the public edge.
Fail-soft. Admin-gated. Snapshot to the PRIMARY. Kill: INVENTORY_SHELL_DISABLE=1

Endpoints:
  GET/POST /api/v1/admin/inventory/master-tick   JSON (5 lanes)
  GET      /admin/inventory                       HTML (60s refresh)
  GET      /api/v1/admin/inventory                CF zone-worker bypass alias
"""
from __future__ import annotations

import os
import json
import time
import logging
import threading
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, jsonify, request, Response

logger = logging.getLogger(__name__)

inventory_acquisition_master_shell_bp = Blueprint(
    "inventory_acquisition_master_shell", __name__)

_TICK_TTL = 45
_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()

# A source is a LIVE FEED only if it added rows recently AND its timestamps spread
# over more than one minute. One-minute spread = a bulk backfill wearing a feed's
# clothes (the 2026-03-18 03:29:40 lesson).
_FEED_FRESH_DAYS = 7
_MIN_SPREAD_MINUTES = 2

# Operators that publish authoritative complete property lists.
_AUTHORITATIVE_OPERATORS = ("Digital Realty", "Equinix", "Amazon Web Services",
                            "Microsoft", "Google", "NTT")
_VERIFY_PCT_FLOOR = 70

# Credentials held in Railway env with an ingestion path that may not exist/run.
_HELD_CREDENTIALS = (
    ("FCC_BDC_TOKEN", "fiber/broadband coverage (FCC Broadband Data Collection)"),
    ("EPA_AQS_API_KEY", "air permits — large DCs permit backup generators by "
                        "name+address (air_permitting_data.py is in-repo)"),
    ("GRIDSTATUS_API_KEY", "ISO/grid telemetry breadth"),
    ("ERCOT_API_KEY", "ERCOT large-load interconnection"),
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("INVENTORY_SHELL_DISABLE") or "").strip() == "1"


def _conn():
    try:
        import psycopg2 as _pg
        url = ((os.environ.get("NEON_REPLICA_URL") or "").strip()
               or (os.environ.get("DATABASE_URL") or "").strip()
               or (os.environ.get("NEON_DATABASE_URL") or "").strip())
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[inventory] db connect failed: %s", e)
        return None


def _rows(c, sql: str) -> list:
    if c is None:
        return []
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    except Exception as e:
        logger.debug("[inventory] rows failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return []


def _scalar(c, sql: str):
    r = _rows(c, sql)
    return (r[0][0] if r and r[0] else None)


def _check(cid: str, name: str, passed, detail: str, critical: bool = False) -> dict:
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:400], "critical": critical}


def _lane_verdict(checks: list):
    decided = [ch for ch in checks if ch["pass"] is not None]
    if not decided:
        return None
    return all(ch["pass"] for ch in decided)


def _read_repo(relpath: str):
    try:
        p = os.path.join(_REPO_ROOT, relpath)
        if os.path.exists(p):
            with open(p, "r", errors="replace") as f:
                return f.read()
    except Exception:
        pass
    return None


# ── lane 1 · acquisition surface ─────────────────────────────────────
def _lane_surface(c) -> list:
    out = []
    # ★distinct-minute spread is the backfill detector. A source whose rows all
    # share one minute is a bulk import, however recent it looks.
    rows = _rows(c, f"""
        SELECT source, COUNT(*),
               COUNT(DISTINCT date_trunc('minute', first_seen)),
               round(EXTRACT(EPOCH FROM (now()-MAX(first_seen)))/86400.0,0)
          FROM discovered_facilities
         GROUP BY 1 HAVING COUNT(*) > 20""")
    live = [(s, n, mins, age) for (s, n, mins, age) in rows
            if age is not None and float(age) <= _FEED_FRESH_DAYS
            and int(mins or 0) >= _MIN_SPREAD_MINUTES]
    backfill = [(s, n, mins, age) for (s, n, mins, age) in rows
                if int(mins or 0) < _MIN_SPREAD_MINUTES]
    out.append(_check(
        "live_feeds", "at least 4 genuinely LIVE facility feeds", len(live) >= 4,
        f"{len(live)} live feed(s) (fresh <={_FEED_FRESH_DAYS}d AND timestamps spread "
        f">1min): " + ", ".join(f"{s}({n})" for s, n, _, _ in live[:6])
        + f" · {len(backfill)} label(s) are bulk BACKFILLS wearing a feed's clothes, "
        f"not sources — that is the 2026-03-18 03:29:40 artifact.", critical=True))

    # The registered recurring crawlers, read from the scheduler itself.
    sched = _read_repo("crawler_scheduler.py") or ""
    registered = [n for n in ("peeringdb", "openstreetmap", "datacentermap")
                  if f"('{n}'," in sched]
    out.append(_check(
        "crawlers_registered", "recurring crawler registry >= 4 sources",
        len(registered) >= 4,
        f"crawler_scheduler.py registers only {len(registered)}: "
        f"{', '.join(registered)}. The acquisition surface is this list plus news "
        f"extraction — NOT the 20 source labels in the table.", critical=True))

    dcm = _read_repo(".github/workflows/datacentermap-crawl.yml") or ""
    dcm_scheduled = any(ln.strip().startswith("- cron:") for ln in dcm.splitlines())
    out.append(_check(
        "dcm_scheduled", "datacentermap crawl is actually scheduled", dcm_scheduled,
        "datacentermap-crawl.yml has its cron COMMENTED OUT (DataCenterMap raised a "
        "Vercel bot wall) while DCHUB env still sets DCM_CRAWL_ENABLED=true — it "
        "reads armed and is not. Either solve the wall or drop the env flag."
        if not dcm_scheduled else "cron present"))
    return out


# ── lane 2 · distinct vs records ─────────────────────────────────────
def _lane_distinct(c) -> list:
    out = []
    recs = int(_scalar(c, "SELECT COUNT(*) FROM discovered_facilities") or 0)
    dist = int(_scalar(c, "SELECT COUNT(DISTINCT canonical_slug) FROM "
                          "discovered_facilities WHERE canonical_slug IS NOT NULL") or 0)
    ratio = (recs / dist) if dist else 0
    out.append(_check(
        "record_inflation", "records-per-distinct-facility <= 1.6", ratio <= 1.6,
        f"{recs:,} records represent {dist:,} distinct facilities ({ratio:.2f}x). "
        f"Growing the RECORD count is trivial and meaningless — a second PeeringDB "
        f"ingest adds ~5,900 rows and ZERO facilities. Publish distinct.",
        critical=True))
    grew = int(_scalar(c, "SELECT COUNT(DISTINCT canonical_slug) FROM "
                          "discovered_facilities WHERE canonical_slug IS NOT NULL "
                          "AND first_seen > now()-interval '30 days'") or 0)
    out.append(_check(
        "distinct_growth_30d", "distinct facilities added in 30d > 0", grew > 0,
        f"{grew:,} canonical_slug values first seen in the last 30d — the only "
        f"honest growth number."))
    return out


# ── lane 3 · operator-authoritative verification ─────────────────────
def _lane_operators(c) -> list:
    out = []
    names = ",".join("'" + o.replace("'", "''") + "'"
                     for o in _AUTHORITATIVE_OPERATORS)
    rows = _rows(c, f"""
        SELECT provider, COUNT(*),
               COUNT(*) FILTER (WHERE COALESCE(is_duplicate,0)=0)
          FROM discovered_facilities WHERE provider IN ({names})
         GROUP BY 1 ORDER BY 2 DESC""")
    weak = []
    for prov, tracked, keep in rows:
        pct = (100.0 * int(keep or 0) / int(tracked or 1))
        if pct < _VERIFY_PCT_FLOOR:
            weak.append(f"{prov} {int(keep or 0)}/{int(tracked or 0)}={pct:.0f}%")
    out.append(_check(
        "operator_verify", f"all authoritative operators >= {_VERIFY_PCT_FLOOR}% verified",
        len(weak) == 0,
        ("below floor: " + " · ".join(weak) if weak else "all clear")
        + ". These operators publish COMPLETE property lists, and the REITs file full "
          "property schedules in their 10-Ks — hundreds verify per document. This is "
          "the cheapest coverage available and it is unspent.", critical=True))
    return out


# ── lane 4 · held-but-unused sources ─────────────────────────────────
def _lane_unused(c) -> list:
    out = []
    unused = []
    for env_name, what in _HELD_CREDENTIALS:
        held = bool((os.environ.get(env_name) or "").strip())
        if held:
            unused.append(f"{env_name} ({what})")
    out.append(_check(
        "held_credentials", "held credentials all have a live ingestion path",
        False if unused else True,
        f"{len(unused)} credential(s) held in env whose ingestion is unverified: "
        + " · ".join(unused[:4]) +
        ". Each is a source we already pay for. Air permits are the standout: a "
        "large DC must permit backup generators BY NAME AND ADDRESS."))
    lbnl = os.path.exists(os.path.join(
        _REPO_ROOT, "LBNL_Ix_Queue_Data_File_thru2024_v2.xlsx"))
    out.append(_check(
        "lbnl_queue_ingested", "LBNL interconnection queue workbook is ingested",
        None if not lbnl else False,
        "LBNL_Ix_Queue_Data_File_thru2024_v2.xlsx sits in the repo root; utility "
        "large-load queues NAME the data centre and are public. This is also the "
        "honest home for the AEP-63GW / Dominion-48GW figures that were "
        "contaminating capacity_pipeline." if lbnl else "workbook not present"))
    return out


# ── lane 5 · layer coverage + landmines ──────────────────────────────
def _lane_layers(c) -> list:
    out = []
    layers = (("substations", "created_at"), ("transmission_lines", "created_at"),
              ("gas_pipelines", "created_at"), ("fiber_routes", "created_at"))
    stale = []
    for tbl, col in layers:
        age = _scalar(c, f"SELECT round(EXTRACT(EPOCH FROM (now()-MAX({col})))"
                         f"/86400.0,0) FROM {tbl}")
        if age is None or float(age) > _FEED_FRESH_DAYS:
            stale.append(f"{tbl}={age}d")
    out.append(_check(
        "infra_fresh", "grid/gas/fiber layers all fresh", len(stale) == 0,
        ("stale: " + ", ".join(stale)) if stale else
        "substations / transmission_lines / gas_pipelines / fiber_routes all fresh — "
        "these pipelines WORK, so growth effort does not belong here."))

    has_tx = _scalar(c, "SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name ~ 'transformer'")
    out.append(_check(
        "transformers_layer", "a transformers layer exists", int(has_tx or 0) > 0,
        "NO transformers table exists at all — genuine greenfield. OSM "
        "power=transformer plus HIFLD substation attributes are the free sources."))

    pp = int(_scalar(c, "SELECT COUNT(*) FROM power_plants") or 0)
    out.append(_check(
        "power_plants_landmine", "power_plants table is not a misleading stub",
        pp > 1000,
        f"power_plants holds {pp} rows. The US plant fleet it is supposed to "
        f"hold lives in power_plants_eia (13,446 EIA plant records) — same "
        f"population, loaded to {pp / 134.46:.1f}%. Cause: the eia-860-plants "
        f"crawler keys its dedup on rec['plantid'], a key the EIA v2 "
        f"facility-fuel response does not carry, and silently skips every "
        f"record without it (55,000 fetched, {pp} upserted, errors=0). "
        f"Separately, ~182k rows are published from gem_power — a DIFFERENT "
        f"POPULATION (global, generating UNITS not plants, all statuses "
        f"including cancelled/retired), not merely a different table. Anyone "
        f"querying the obvious name gets a near-empty stub."))
    return out


_LANES = [
    ("surface",   "1 · Acquisition surface (what actually crawls)", _lane_surface,
     "solve or retire the DataCenterMap bot wall; add crawlers — the registry has "
     "3 sources, not 20"),
    ("distinct",  "2 · Distinct facilities vs records",             _lane_distinct,
     "publish distinct canonical_slug, never the raw record count"),
    ("operators", "3 · Operator-authoritative verification",        _lane_operators,
     "batch-verify from published property lists + REIT 10-K property schedules "
     "(AWS/Digital Realty first) — cheapest coverage available"),
    ("unused",    "4 · Held-but-unused sources",                    _lane_unused,
     "wire FCC_BDC + EPA air-permits + the LBNL queue workbook; each is already "
     "paid for"),
    ("layers",    "5 · Layer coverage + landmines",                 _lane_layers,
     "build the transformers layer; fix the power_plants stub; leave the healthy "
     "grid/gas/fiber pipelines alone"),
]


def _ensure_snapshots(pc) -> None:
    try:
        with pc.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS inventory_shell_snapshots ("
                " id BIGSERIAL PRIMARY KEY,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " lanes_pass INT, lanes_total INT, payload JSONB)")
    except Exception as e:
        logger.debug("[inventory] snapshot ddl skipped: %s", e)


def _run_tick() -> dict:
    c = _conn()
    lanes = []
    for key, label, fn, actuator in _LANES:
        t0 = time.time()
        try:
            checks = fn(c)
        except Exception as e:
            checks = [_check(f"{key}_error", "lane crashed", None, str(e)[:200])]
        ms = int((time.time() - t0) * 1000)
        decided = [ch for ch in checks if ch["pass"] is not None]
        lanes.append({"lane": key, "label": label, "pass": _lane_verdict(checks),
                      "actuator": actuator, "checks": checks, "ms": ms,
                      "progress": f"{sum(1 for ch in decided if ch['pass'])}/{len(checks)}"})
    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": ("read-only DIAGNOSTIC. Built after 'sixteen feeds died on 2026-03-18' "
                 "turned out to be ONE backfill at 03:29:40 — lane 1 now spreads-tests "
                 "every feed so a bulk import can never pose as a source again."),
        "lanes_pass": sum(1 for l in lanes if l["pass"] is True),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "note": "Inventory-Acquisition shell #40 — routes/inventory_acquisition_master_shell.py",
    }
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    pc = None
    try:
        import psycopg2 as _pg
        purl = ((os.environ.get("DATABASE_URL") or "").strip()
                or (os.environ.get("NEON_DATABASE_URL") or "").strip())
        if purl:
            pc = _pg.connect(purl, connect_timeout=8)
            pc.autocommit = True
            _ensure_snapshots(pc)
            with pc.cursor() as cur:
                cur.execute("INSERT INTO inventory_shell_snapshots "
                            "(lanes_pass, lanes_total, payload) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            (payload["lanes_pass"], payload["lanes_total"],
                             json.dumps(payload)))
    except Exception as e:
        logger.debug("[inventory] snapshot insert skipped: %s", e)
    finally:
        if pc is not None:
            try:
                pc.close()
            except Exception:
                pass
    return payload


def _tick_cached() -> dict:
    with _cache_lock:
        if _cache["payload"] is not None and time.time() - _cache["ts"] < _TICK_TTL:
            return _cache["payload"]
    payload = _run_tick()
    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["payload"] = payload
    return payload


@inventory_acquisition_master_shell_bp.route(
    "/api/v1/admin/inventory/master-tick", methods=["GET", "POST"])
def inventory_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@inventory_acquisition_master_shell_bp.route("/admin/inventory", methods=["GET"])
@inventory_acquisition_master_shell_bp.route("/api/v1/admin/inventory", methods=["GET"])
def inventory_dashboard():
    if _disabled():
        return Response("inventory shell disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _tick_cached()

    def _chip(v):
        if v is True:
            return '<span style="color:#22c55e">✓</span>'
        if v is False:
            return '<span style="color:#ef4444">✗</span>'
        return '<span style="color:#eab308">?</span>'

    cards = []
    for lane in p["lanes"]:
        rows = "".join(
            f"<tr><td style='padding:4px 8px;vertical-align:top'>{_chip(ch['pass'])}</td>"
            f"<td style='padding:4px 8px;vertical-align:top;white-space:nowrap'>{_esc(ch['name'])}</td>"
            f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}</td></tr>"
            for ch in lane["checks"])
        border = ("#22c55e" if lane["pass"] is True
                  else ("#eab308" if lane["pass"] is None else "#ef4444"))
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_chip(lane['pass'])} "
            f"{_esc(lane['label'])} <span style='color:#64748b;font-weight:400'>"
            f"({lane['progress']} · {lane.get('ms',0)}ms)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>{rows}</table>"
            f"<div style='margin-top:8px;font-size:12px;color:#64748b'>⚡ next: "
            f"{_esc(lane.get('actuator',''))}</div></div>")

    green = p["lanes_pass"] == p["lanes_total"]
    html = (
        "<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='60'>"
        "<title>Inventory-Acquisition Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:980px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Inventory-Acquisition Shell "
        f"<span style='color:{'#22c55e' if green else '#ef4444'}'>"
        f"{p['lanes_pass']}/{p['lanes_total']} lanes green</span></h2>"
        f"<div style='color:#64748b;font-size:12px;line-height:1.5'>#40 · 07-28 · "
        f"how inventory actually grows. Built after <b>“16 feeds died on 2026-03-18”</b> "
        f"turned out to be <b>ONE backfill at 03:29:40</b> — lane 1 spread-tests every "
        f"feed so a bulk import can never pose as a source again. 45s cache · read "
        f"replica · generated {_esc(p['generated_at'])} · JSON "
        f"/api/v1/admin/inventory/master-tick</div>"
        + "".join(cards) +
        "<div style='color:#475569;font-size:11px;margin-top:16px'>Growth means DISTINCT "
        "canonical_slug, never records: 22,775 records are ~15,100 facilities, and a "
        "second PeeringDB ingest would add ~5,900 rows and zero coverage.</div></body>")
    return Response(html, mimetype="text/html")
