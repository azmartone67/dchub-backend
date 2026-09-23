"""
OSM Overpass loaders — the weekly OpenStreetMap energy lanes.

Driven by .github/workflows/dchub-osm-refresh.yml, which POSTs
/api/admin/load-osm-*-live (main.py, phase 13). Each POST starts ONE loader in a
background thread and returns the id of a durable row in `osm_load_runs`; the
workflow polls that row and goes red on a failed loader.

★ WHAT WAS BROKEN (measured 2026-09-22, read-only against production):
  • load_pipelines INSERTed into a table named `pipelines` that does not exist.
    Every row raised, the except swallowed it, and the run "finished".
  • load_transmission_lines INSERTed into infrastructure_layers columns
    (capacity_mw, lat, lng, state) that table does not have — newest row
    2026-03-13 — and nothing on the What's New board reads that table anyway.
  • load_power_plants wrote discovered_power_plants with discovered_at AND
    last_updated NULL on all 8,629 source='osm_overpass' rows, so the board's
    freshness read for that layer could not see a single OSM insert.
  • load_substations wrote source NULL (46,376 rows) — no lane could count it.
  • The outcome lived only in a process-local dict. The workflow's status read
    answered `{"loaders": {}}` from another replica and stayed green.

THE OSM LANES (owner decision 2026-09-22). OSM transmission lines and gas
pipelines go in their OWN tables — osm_transmission_lines / osm_gas_pipelines —
and are NEVER merged into transmission_lines / gas_pipelines: the federal
tables already hold the same physical lines, and a merge would count each twice.
A feature enters a lane only if it is NEW TO OSM SINCE THE FEDERAL SNAPSHOT:

  1. its way id is >= the first way id OSM allocated on/after the snapshot date
     (ids are allocated sequentially, so this is "created after the snapshot",
     exact, and filtered server-side by Overpass), AND
  2. at least NEW_NODE_SHARE of its nodes were also created after that date —
     a way SPLIT off an older line gets a new id but keeps the old nodes, and
     is not a new feature, AND
  3. (gas only) its centre is not within GAS_NEAR_FEDERAL_KM of a point we
     already hold in gas_pipelines. transmission_lines has no geometry at all,
     so no proximity test is possible there — the board says so.

"New to OSM" is not "newly built": a line OSM mapped late can be one EIA
already lists. The board states that too.

GROWTH IS COUNTED FROM first_seen_at, NEVER FROM THE BACKFILL. The first
successful sweep of each (lane, state) is its BASELINE: rows it inserts carry
in_baseline=TRUE and never appear as "+N new". Only rows first seen on a later
sweep of an already-baselined state count. A state whose sweep fails is not
baselined, so a partial first run cannot leak its backfill into next week's +N.
"""
import datetime
import json
import logging
import math
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

OVERPASS_URL = os.environ.get("OSM_OVERPASS_URL",
                              "https://overpass-api.de/api/interpreter")
# ★ The same knob routes/osm_crawler.py reads. overpass-api.de 406s any
# User-Agent containing "crawler" (bisected 2026-08-10, see that module); this
# default was measured accepted (HTTP 200) on 2026-09-22.
USER_AGENT = os.environ.get("OSM_USER_AGENT",
                            "DCHubBot/1.0 (+https://dchub.cloud/contact)")

US_STATES = [
    'AL','AK','AZ','AR','CA','CO','CT','DC','DE','FL',
    'GA','HI','ID','IL','IN','IA','KS','KY','LA','ME',
    'MD','MA','MI','MN','MS','MO','MT','NE','NV','NH',
    'NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI',
    'SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY',
]

# ── Federal snapshot dates and the OSM id floors that encode them ────────────
# EIA's services report dataLastEditDate 2025-08-26 (transmission, 94,619
# features = our 'eia-arcgis-runner' rows) and 2025-07-01 (natural-gas
# pipelines, our 'eia_geodot_lines' rows).
TRANSMISSION_CUTOFF = "2025-08-26"
GAS_CUTOFF = "2025-07-01"
# First OSM id whose version 1 was created on/after each date. Measured
# 2026-09-22 by binary search over api.openstreetmap.org/api/0.6/<type>/<id>/1:
#   way  1411044537  v1 2025-07-01T00:00:03Z  (id-1: 2025-06-30T23:59:46Z)
#   way  1425185987  first readable id at/after it: 2025-08-27T01:39:57Z
#   node 12971133082 first readable id at/after it: 2025-07-02T15:37:31Z
#   node 13097363986 first readable id at/after it: 2025-08-26T00:42:12Z
# Unreadable ids (never allocated / redacted) were treated as BEFORE the date,
# which can only push a floor later — i.e. exclude a day-old feature, never
# admit an older one.
ID_FLOORS = {
    TRANSMISSION_CUTOFF: {"way": 1425185987, "node": 13097363986},
    GAS_CUTOFF:          {"way": 1411044537, "node": 12971133082},
}
NEW_NODE_SHARE = 0.5
TRANSMISSION_MIN_KV = 69          # EIA's transmission layer starts at 69 kV
GAS_NEAR_FEDERAL_KM = 1.0
# ★ 2026-09-23: an OSM substation within this distance of ANY substation we
# already hold is the same substation, not a new one. Measured on the first
# run after #5306 (809 new source='osm' rows, read-only): 399 sat within 50 m
# of a held row (59 of them the same OSM element under a new name, e.g.
# "SRP Wilkins Substation" beside "Wilkins Substation"), 423 within 150 m,
# 441 within 300 m; 246 had nothing within ~1 km. The cliff is at 50 m, so
# 150 m catches the coordinate drift between HIFLD and OSM without swallowing
# a neighbouring station.
SUBSTATION_NEAR_HELD_M = 150.0

STATE_SLEEP_S = float(os.environ.get("OSM_LOADER_STATE_SLEEP_S", "2"))
# Wall-clock budget per loader; states not reached are reported as failed.
LOADER_BUDGET_S = float(os.environ.get("OSM_LOADER_BUDGET_S", "2700"))
# A loader whose run row has not heartbeated for this long is dead (a deploy
# killed its thread): it is reported as stalled, never as still running.
#
# ★ 2026-09-23: was 900, with the row heartbeated once per finished STATE. The
# first live run showed why that cannot work here: bots merge to main every
# 10-15 min and every merge redeploys Railway, killing this thread; run 1 died
# at 20/51 states and its refire at 12/51. The row is now heartbeated before
# every Overpass attempt (the longest silent stretch is one attempt: the 200 s
# socket timeout plus a 40 s backoff), so a dead thread is visible in minutes.
STALL_AFTER_S = 420
# A stalled run younger than this is RESUMED by the next start of the same
# loader: the states it finished are carried over rather than swept again.
RESUME_WITHIN_H = 12
# Share of states that may fail (Overpass 429/504) before the run is an error.
MAX_FAILED_STATE_SHARE = 0.2
CADENCE_HOURS = 168


def _dsn():
    return (os.environ.get("DATABASE_URL")
            or os.environ.get("NEON_DATABASE_URL") or "").strip()


def _connect():
    # ★ A DIRECT connection, never the db_utils pool: DDL sent through a pooled
    # get_db() cursor is silently dropped (SKIP_DDL=1), and a pooled connection
    # held across slow Overpass fetches starves the site (osm_crawler, 06-18).
    import psycopg2
    return psycopg2.connect(_dsn(), sslmode="require", connect_timeout=10)


# ── Overpass client ─────────────────────────────────────────────────────────
def _overpass_fetch(query, timeout=200, retries=3, backoff=20, on_attempt=None):
    """(data, status). status: ok | throttle | timeout | rejected | error.

    ★ Overpass reports a server-side timeout or memory abort as HTTP 200 with a
    `remark` and a TRUNCATED element list. Accepting that as a sweep would
    baseline a state on partial data, so it is a timeout here, not ok.
    """
    body = urllib.parse.urlencode({'data': query}).encode('utf-8')
    status = "error"
    for attempt in range(retries):
        if on_attempt:
            try:
                on_attempt()
            except Exception:  # noqa: BLE001 — a heartbeat must not kill the fetch
                pass
        try:
            req = urllib.request.Request(
                OVERPASS_URL, data=body, method="POST",
                headers={'User-Agent': USER_AGENT, 'Accept': 'application/json',
                         'Content-Type': 'application/x-www-form-urlencoded'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            remark = str(data.get("remark") or "").lower()
            if "error" in remark or "timed out" in remark or "out of memory" in remark:
                status = "timeout"
            else:
                return data, "ok"
        except urllib.error.HTTPError as e:
            if e.code in (403, 406):
                # The service refuses this CLIENT; every state gets the same
                # answer, so neither a retry nor the next state helps.
                logger.error("Overpass REJECTED the request (HTTP %d) — check "
                             "OSM_USER_AGENT (%r)", e.code, USER_AGENT)
                return None, "rejected"
            status = ("throttle" if e.code == 429 else
                      "timeout" if e.code in (502, 503, 504) else "error")
        except (socket.timeout, TimeoutError):
            status = "timeout"
        except Exception as e:  # noqa: BLE001 — classified, never swallowed
            logger.warning("Overpass attempt %d failed: %s", attempt + 1, e)
            status = "error"
        if attempt < retries - 1:
            time.sleep(backoff * (attempt + 1))
    return None, status


def _overpass(query, timeout=90, retries=3, backoff=8):
    """Legacy shape (data or None) for load_communications_towers."""
    data, _status = _overpass_fetch(query, timeout=timeout, retries=retries,
                                    backoff=backoff)
    return data


def _parse_voltage_kv(raw):
    """OSM voltage is in volts as a string, possibly with semicolons."""
    if not raw: return 0
    try:
        first = str(raw).split(';')[0].strip().split()[0]
        return int(first) // 1000 if first.isdigit() else 0
    except Exception:
        return 0


def _max_voltage_kv(raw):
    """Highest circuit on a multi-circuit tag ("345000;138000" -> 345)."""
    best = 0
    for part in str(raw or "").split(';'):
        tok = part.strip().split(' ')[0] if part.strip() else ''
        if tok.isdigit():
            best = max(best, int(tok) // 1000)
    return best


def _get_db():
    from db_utils import try_get_db
    return try_get_db()


def _center(el):
    lat = el.get('lat') or (el.get('center') or {}).get('lat')
    lng = el.get('lon') or (el.get('center') or {}).get('lon')
    return lat, lng


def _now_text():
    # discovered_power_plants stores its timestamps as TEXT in this shape
    # ('2026-02-26T00:45:38.847648', UTC, no zone) — match it exactly.
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()


# ── The "new since the federal snapshot" rule ───────────────────────────────
def new_node_share(el, cutoff):
    nodes = el.get('nodes') or []
    if not nodes:
        return 0.0
    floor = ID_FLOORS[cutoff]["node"]
    return sum(1 for n in nodes if int(n) >= floor) / len(nodes)


def is_new_since(el, cutoff):
    """Rules 1 and 2 of the module docstring. Pure, so the rule is testable."""
    if el.get('type') != 'way':
        return False
    if int(el.get('id') or 0) < ID_FLOORS[cutoff]["way"]:
        return False
    return new_node_share(el, cutoff) >= NEW_NODE_SHARE


def _haversine_km(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def _near_federal_gas(cur, lat, lng, km=GAS_NEAR_FEDERAL_KM):
    """True when a gas_pipelines point we already hold sits within `km`."""
    dlat = km / 111.0
    dlng = km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    cur.execute(
        """SELECT lat, COALESCE(lng, lon) FROM gas_pipelines
            WHERE lat BETWEEN %s AND %s
              AND COALESCE(lng, lon) BETWEEN %s AND %s
            LIMIT 50""",
        (lat - dlat, lat + dlat, lng - dlng, lng + dlng))
    return any(r[0] is not None and r[1] is not None
               and _haversine_km(lat, lng, float(r[0]), float(r[1])) <= km
               for r in cur.fetchall())


# ── Tables (created here with a direct connection — see _connect) ───────────
_LANE_DDL = """
CREATE TABLE IF NOT EXISTS {t} (
    osm_type        TEXT NOT NULL,
    osm_id          BIGINT NOT NULL,
    name            TEXT,
    operator        TEXT,
    {extra}
    state           TEXT,
    lat             DOUBLE PRECISION,
    lng             DOUBLE PRECISION,
    osm_version     INTEGER,
    osm_timestamp   TIMESTAMPTZ,
    new_node_share  REAL,
    tags            JSONB,
    source          TEXT NOT NULL DEFAULT 'osm_overpass',
    in_baseline     BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (osm_type, osm_id)
)"""

LANES = {
    "osm_transmission_lines": {
        "cutoff": TRANSMISSION_CUTOFF,
        "extra": "voltage_kv REAL,",
    },
    "osm_gas_pipelines": {
        "cutoff": GAS_CUTOFF,
        "extra": "substance TEXT, diameter_mm REAL,",
    },
}


def ensure_tables(conn):
    with conn.cursor() as cur:
        for t, spec in LANES.items():
            cur.execute(_LANE_DDL.format(t=t, extra=spec["extra"]))
            cur.execute(f"CREATE INDEX IF NOT EXISTS {t}_first_seen_idx "
                        f"ON {t} (first_seen_at) WHERE NOT in_baseline")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS osm_lane_baseline (
                lane          TEXT NOT NULL,
                state         TEXT NOT NULL,
                baselined_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (lane, state))""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS osm_load_runs (
                id             BIGSERIAL PRIMARY KEY,
                loader         TEXT NOT NULL,
                status         TEXT NOT NULL,
                started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                heartbeat_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at    TIMESTAMPTZ,
                states_done    INTEGER NOT NULL DEFAULT 0,
                states_total   INTEGER NOT NULL DEFAULT 0,
                fetched        INTEGER NOT NULL DEFAULT 0,
                inserted       INTEGER NOT NULL DEFAULT 0,
                failed_states  JSONB,
                detail         JSONB,
                note           TEXT)""")
        cur.execute("CREATE INDEX IF NOT EXISTS osm_load_runs_loader_idx "
                    "ON osm_load_runs (loader, started_at DESC)")
    conn.commit()


# ── The state sweep every loader shares ─────────────────────────────────────
def _sweep(loader, states, query_for, write_state, progress=None, carried=None):
    """Fetch each state, write it in ONE transaction, and account for all of it.

    write_state(conn, state, elements) -> (inserted, extra_counts). It raises on
    a DB failure — the state is then reported failed, never silently short.

    `carried` = states a dead run of this loader already finished (see
    start_run): they are not swept again and count as done, so a sweep that
    is killed by a deploy every few minutes still completes across refires.
    """
    carried = sorted(set(carried or ()))
    all_states = list(states or US_STATES)
    states = [s for s in all_states if s not in set(carried)]
    res = {"loader": loader, "states_total": len(all_states),
           "states_done": len([s for s in carried if s in set(all_states)]),
           "fetched": 0, "inserted": 0, "failed_states": {}, "rejected": False,
           "per_state": {}, "carried_states": carried,
           "done_states": [s for s in carried if s in set(all_states)]}

    def _beat():
        if progress:
            try:
                progress(res)
            except Exception:  # noqa: BLE001 — a heartbeat must not kill the sweep
                pass
    if not _dsn():
        res["error"] = "no DATABASE_URL"
        return res
    t0 = time.monotonic()
    for i, state in enumerate(states):
        if time.monotonic() - t0 > LOADER_BUDGET_S:
            for s in states[i:]:
                res["failed_states"][s] = "budget"
            break
        data, status = _overpass_fetch(query_for(state), on_attempt=_beat)
        if status != "ok":
            res["failed_states"][state] = status
            if status == "rejected":
                res["rejected"] = True
                for s in states[i + 1:]:
                    res["failed_states"][s] = "not_attempted"
                break
            continue
        els = data.get('elements') or []
        res["fetched"] += len(els)
        try:
            conn = _connect()
            try:
                ins, extra = write_state(conn, state, els)
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001 — reported per state, not swallowed
            logger.warning("%s write %s: %s", loader, state, e)
            res["failed_states"][state] = "db: " + type(e).__name__ + ": " + str(e)[:120]
            continue
        res["inserted"] += ins
        res["states_done"] += 1
        res["done_states"].append(state)
        res["per_state"][state] = {"count": len(els), "inserted": ins}
        for k, v in (extra or {}).items():
            res[k] = res.get(k, 0) + v
        _beat()
        time.sleep(STATE_SLEEP_S)
    return res


def _insert_returning(cur, sql, rows, template=None):
    from psycopg2.extras import execute_values
    if not rows:
        return 0
    out = execute_values(cur, sql, rows, template=template, page_size=500, fetch=True)
    return sum(1 for r in out if r and r[0])


# ── Loaders ─────────────────────────────────────────────────────────────────
# Each loader = an Overpass query per state + a module-level write_* function
# (module-level so tests/test_osm_lane_baseline_pg.py can run it against a
# real Postgres).

class _PointGrid:
    """Held points bucketed by ~1 km cells, for a cheap "anything within R?"."""
    CELL = 0.01

    def __init__(self):
        self.cells = {}

    def _key(self, lat, lng):
        return (int(math.floor(lat / self.CELL)), int(math.floor(lng / self.CELL)))

    def add(self, lat, lng):
        self.cells.setdefault(self._key(lat, lng), []).append((lat, lng))

    def near(self, lat, lng, metres):
        ki, kj = self._key(lat, lng)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for (a, b) in self.cells.get((ki + di, kj + dj), ()):
                    if _haversine_km(lat, lng, a, b) * 1000.0 <= metres:
                        return True
        return False


def _held_substations(cur, pts, margin=0.02):
    """Grid of every substation we hold inside the bbox of `pts` (any source)."""
    grid = _PointGrid()
    if not pts:
        return grid
    lats = [p[0] for p in pts]
    lngs = [p[1] for p in pts]
    cur.execute("""SELECT lat, lng FROM substations
                    WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s""",
                (min(lats) - margin, max(lats) + margin,
                 min(lngs) - margin, max(lngs) + margin))
    for a, b in cur.fetchall():
        if a is not None and b is not None:
            grid.add(float(a), float(b))
    return grid


def write_substations(conn, state, els):
    """One state's substations in one statement; returns (inserted, counts).

    Not a duplicate of what we hold: an element within SUBSTATION_NEAR_HELD_M
    of any substation already in the table (HIFLD, an earlier OSM row under
    another name, discovery) is skipped and counted as near_held_skipped, and
    so is a second element of the same sweep within that distance (a node and
    a way drawn for one station).

    source / source_id use the tag the existing OSM discovery lane already
    writes (infrastructure_discovery.py: source='osm', source_id='osm_sub_<id>'),
    so both lanes share one identity under substations_source_id_key. Rows
    already held are left exactly as they are (ON CONFLICT DO NOTHING) — the
    46,376 NULL-source rows are neither restamped nor relabelled.
    """
    cands = []
    for el in els:
        lat, lng = _center(el)
        if lat and lng:
            cands.append((float(lat), float(lng), el))
    rows, skipped = [], 0
    with conn.cursor() as cur:
        grid = _held_substations(cur, cands)
        for lat, lng, el in cands:
            if grid.near(lat, lng, SUBSTATION_NEAR_HELD_M):
                skipped += 1
                continue
            grid.add(lat, lng)
            tags = el.get('tags') or {}
            rows.append(((tags.get('name') or f"OSM-{el.get('id')}")[:200],
                         (tags.get('operator') or 'Unknown')[:200],
                         _parse_voltage_kv(tags.get('voltage')), lat, lng,
                         (tags.get('addr:city') or '')[:100], state, 'US',
                         'osm', f"osm_sub_{el.get('id')}"))
        ins = _insert_returning(cur, """
            INSERT INTO substations
              (name, operator, voltage_kv, lat, lng, city, state, country,
               source, source_id)
            VALUES %s ON CONFLICT DO NOTHING RETURNING 1""", rows)
    return ins, {"near_held_skipped": skipped}


def load_substations(states=None, progress=None, carried=None):
    """OSM `power=substation` nodes/ways -> substations (source='osm')."""
    def q(state):
        return f'''[out:json][timeout:120];
        area["ISO3166-2"="US-{state}"]->.s;
        (node["power"="substation"](area.s); way["power"="substation"](area.s););
        out center;'''
    return _sweep("osm_substations", states, q, write_substations, progress, carried)


def write_power_plants(conn, state, els):
    """One state's plants -> discovered_power_plants; returns (inserted, {}).

    ★ New rows carry discovered_at / last_updated (TEXT, the shape the other
    lanes use), so the board's freshness read for this layer can see an OSM
    insert. Existing rows are untouched: ON CONFLICT DO NOTHING on the
    deterministic id, so the 8,629 held rows keep their NULL stamps and are
    never presented as new.
    """
    with conn.cursor() as cur:
        # r70g: the repo CREATE TABLE is stale (no `country`; `id` is TEXT NOT
        # NULL with no default) — write only columns the LIVE table has.
        cur.execute("""SELECT column_name FROM information_schema.columns
                        WHERE table_name='discovered_power_plants'""")
        cols = {r[0] for r in cur.fetchall()}
        stamp = _now_text()
        recs = []
        for el in els:
            tags = el.get('tags') or {}
            lat, lng = _center(el)
            if not lat or not lng:
                continue
            output = tags.get('plant:output:electricity', '') or ''
            try:
                mw = float(output.replace('MW', '').strip()) if 'MW' in output else 0
            except Exception:
                mw = 0
            recs.append({"id": f"osm-{el.get('type', 'x')}-{el.get('id')}",
                         "name": (tags.get('name') or f"OSM-{el.get('id')}")[:200],
                         "operator": (tags.get('operator') or '')[:200],
                         "fuel_type": (tags.get('plant:source') or '')[:80],
                         "capacity_mw": mw, "lat": lat, "lng": lng,
                         "state": state, "source": "osm_overpass",
                         "discovered_at": stamp, "last_updated": stamp})
        keys = [k for k in ("id", "name", "operator", "fuel_type", "capacity_mw",
                            "lat", "lng", "state", "source",
                            "discovered_at", "last_updated") if k in cols]
        if "id" not in keys:
            raise RuntimeError("discovered_power_plants has no id column")
        col_sql = ", ".join(keys)
        ins = _insert_returning(
            cur,
            f"INSERT INTO discovered_power_plants ({col_sql}) VALUES %s ON CONFLICT DO NOTHING RETURNING 1",
            [tuple(r[k] for k in keys) for r in recs])
    return ins, {}


def load_power_plants(states=None, progress=None, carried=None):
    """OSM `power=plant` -> discovered_power_plants (source='osm_overpass')."""
    def q(state):
        return f'''[out:json][timeout:120];
        area["ISO3166-2"="US-{state}"]->.s;
        (node["power"="plant"](area.s); way["power"="plant"](area.s);
         relation["power"="plant"](area.s););
        out center;'''
    return _sweep("osm_power_plants", states, q, write_power_plants, progress, carried)


def _lane_writer(lane, row_for, extra_cols, reject=None):
    """write_state for an OSM lane: rule-filter, baseline-aware upsert."""
    cutoff = LANES[lane]["cutoff"]

    def write(conn, state, els):
        ensure_tables(conn)
        counts = {"rule_rejected": 0, "near_federal_skipped": 0}
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM osm_lane_baseline WHERE lane=%s AND state=%s",
                        (lane, state))
            baseline_sweep = cur.fetchone() is None
            rows, seen = [], set()
            for el in els:
                key = (el.get('type'), el.get('id'))
                if key in seen:
                    continue
                seen.add(key)
                lat, lng = _center(el)
                if not lat or not lng or not is_new_since(el, cutoff):
                    counts["rule_rejected"] += 1
                    continue
                extra = row_for(el)
                if extra is None:
                    counts["rule_rejected"] += 1
                    continue
                if reject and reject(cur, lat, lng):
                    counts["near_federal_skipped"] += 1
                    continue
                tags = el.get('tags') or {}
                rows.append((el['type'], int(el['id']),
                             (tags.get('name') or '')[:200] or None,
                             (tags.get('operator') or '')[:200] or None,
                             *extra, state, lat, lng,
                             el.get('version'), el.get('timestamp'),
                             round(new_node_share(el, cutoff), 3),
                             json.dumps(tags), baseline_sweep))
            names = [c.strip() for c in extra_cols.split(",")]
            cols = ", ".join(["osm_type", "osm_id", "name", "operator", *names,
                              "state", "lat", "lng", "osm_version", "osm_timestamp",
                              "new_node_share", "tags", "in_baseline"])
            upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in
                            ("name", "operator", *names, "osm_version",
                             "osm_timestamp", "new_node_share", "tags"))
            # (xmax = 0) is true only for a row this statement INSERTED — an
            # existing row refreshed by DO UPDATE is never counted as new, and
            # first_seen_at / in_baseline are never in the SET list.
            ins = _insert_returning(cur, f"""
                INSERT INTO {lane} ({cols}) VALUES %s
                ON CONFLICT (osm_type, osm_id) DO UPDATE
                   SET {upd}, last_seen_at = NOW()
                RETURNING (xmax = 0)""", rows)
            if baseline_sweep:
                # Same transaction as the rows: a sweep that fails to commit
                # leaves the state un-baselined, so its rows are baseline next time.
                cur.execute("""INSERT INTO osm_lane_baseline (lane, state)
                               VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                            (lane, state))
        counts["baseline_rows" if baseline_sweep else "new_rows"] = ins
        return ins, counts

    return write


def _transmission_row(el, min_kv=TRANSMISSION_MIN_KV):
    kv = _max_voltage_kv((el.get('tags') or {}).get('voltage'))
    return (kv,) if kv >= min_kv else None


def _gas_row(el):
    tags = el.get('tags') or {}
    try:
        raw = str(tags.get('diameter') or '').replace('mm', '').strip()
        dia = float(raw) if raw else None
    except ValueError:
        dia = None
    return ((tags.get('substance') or tags.get('type') or 'gas')[:60], dia)


def transmission_writer(min_kv=TRANSMISSION_MIN_KV):
    return _lane_writer("osm_transmission_lines",
                        lambda el: _transmission_row(el, min_kv), "voltage_kv")


def gas_writer():
    return _lane_writer("osm_gas_pipelines", _gas_row, "substance, diameter_mm",
                        reject=_near_federal_gas)


def load_transmission_lines(states=None, progress=None, min_kv=TRANSMISSION_MIN_KV,
                            carried=None):
    """OSM `power=line` ways NEW SINCE the EIA snapshot -> osm_transmission_lines.

    Never touches transmission_lines (see the module docstring).
    """
    floor = ID_FLOORS[TRANSMISSION_CUTOFF]["way"]

    def q(state):
        return f'''[out:json][timeout:180];
        area["ISO3166-2"="US-{state}"]->.s;
        way["power"="line"]["voltage"](area.s)(if: id() >= {floor});
        out center meta;'''

    return _sweep("osm_transmission_lines", states, q, transmission_writer(min_kv),
                  progress, carried)


def load_pipelines(states=None, progress=None, carried=None):
    """OSM natural-gas pipelines NEW SINCE the EIA snapshot -> osm_gas_pipelines.

    Gas only (substance=gas|natural_gas|cng|lng, or legacy type=gas): water,
    sewage and oil pipelines were never what this lane is for. Never touches
    gas_pipelines; a candidate within GAS_NEAR_FEDERAL_KM of a gas_pipelines
    point is skipped as a probable duplicate.
    """
    floor = ID_FLOORS[GAS_CUTOFF]["way"]

    def q(state):
        return f'''[out:json][timeout:180];
        area["ISO3166-2"="US-{state}"]->.s;
        (way["man_made"="pipeline"]["substance"~"^(gas|natural_gas|cng|lng)$"](area.s)(if: id() >= {floor});
         way["man_made"="pipeline"]["type"="gas"](area.s)(if: id() >= {floor}););
        out center meta;'''

    return _sweep("osm_pipelines", states, q, gas_writer(), progress, carried)


def load_communications_towers(states=None):
    """OSM `man_made=communications_tower` nodes (proxy for comm/fiber infra).

    Not part of the weekly refresh and not tracked in osm_load_runs.
    """
    results = {}
    db = _get_db()
    if not db: return {'error': 'no DB'}
    states = states or US_STATES
    for state in states:
        q = f'''
        [out:json][timeout:60];
        area["ISO3166-2"="US-{state}"]->.s;
        node["man_made"="communications_tower"](area.s);
        out;
        '''
        data = _overpass(q)
        if not data:
            results[state] = {'error': 'overpass timeout'}; continue
        els = data.get('elements', [])
        if not els:
            results[state] = {'count': 0}; continue
        cur = db.cursor()
        ins = 0
        for el in els:
            tags = el.get('tags', {})
            name = (tags.get('name') or f"OSM-tower-{el.get('id')}")[:200]
            lat = el.get('lat')
            lng = el.get('lon')
            if not lat or not lng: continue
            try:
                cur.execute(
                    """INSERT INTO infrastructure_layers
                       (id, category, name, lat, lng, state)
                       VALUES (%s, 'comm_tower', %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (f"osm-tower-{el.get('id')}", name, lat, lng, state))
                ins += cur.rowcount
            except Exception as e:
                db.rollback()
                logger.warning(f"comm_tower insert {state}: {e}")
        try: db.commit()
        except Exception: pass
        results[state] = {'count': len(els), 'inserted': ins}
        time.sleep(2)
    try: db.close()
    except Exception: pass
    return results


# ── Durable run record + dead-man beat ──────────────────────────────────────
# loader key (main.py fn_map) -> (ingest_runs feed, loader function)
TRACKED = {
    "osm_substations":        ("osm-substations", load_substations),
    "osm_power_plants":       ("osm-power-plants", load_power_plants),
    "osm_transmission_lines": ("osm-transmission", load_transmission_lines),
    "osm_pipelines":          ("osm-gas-pipelines", load_pipelines),
}


def classify(res):
    """(status, note) for ONE loader result. Statuses are the dead-man
    ledger's: success | no_new_data | error. Only a completed sweep may say
    no_new_data — that status resets the zero-row alarm."""
    if not isinstance(res, dict):
        return "error", "loader returned no result"
    if res.get("error"):
        return "error", str(res["error"])[:200]
    failed = res.get("failed_states") or {}
    total = int(res.get("states_total") or 0)
    if res.get("rejected"):
        return "error", ("Overpass REJECTED the client (403/406) — not an "
                         "outage; check OSM_USER_AGENT")
    db_failed = sorted(s for s, why in failed.items() if str(why).startswith("db:"))
    if db_failed:
        return "error", f"DB write failed for {len(db_failed)} state(s): {', '.join(db_failed[:8])}"
    if total == 0 or len(failed) > MAX_FAILED_STATE_SHARE * total:
        return "error", (f"{len(failed)}/{total} states failed "
                         f"({', '.join(sorted(failed)[:8])})")
    ins = int(res.get("inserted") or 0)
    note = (f"swept {res.get('states_done', 0)}/{total} states, "
            f"fetched {res.get('fetched', 0)}, inserted {ins}")
    if res.get("carried_states"):
        note += (f" ({len(res['carried_states'])} state(s) carried from an "
                 f"interrupted run)")
    if failed:
        note += f"; failed: {', '.join(sorted(failed)[:8])}"
    return ("success" if ins > 0 else "no_new_data"), note


def active_run(loader):
    """The live run row for `loader`, or None. A row whose heartbeat is older
    than STALL_AFTER_S is dead, not active."""
    try:
        conn = _connect()
    except Exception:
        return None
    try:
        ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT id FROM osm_load_runs
                            WHERE loader=%s AND status='running'
                              AND heartbeat_at > NOW() - make_interval(secs => %s)
                            ORDER BY id DESC LIMIT 1""", (loader, STALL_AFTER_S))
            r = cur.fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def start_run(loader):
    """Insert the durable 'running' row; returns its id (raises on DB failure).

    If the newest run of this loader is a STALLED one (status running, no
    heartbeat for STALL_AFTER_S, started within RESUME_WITHIN_H), it is closed
    as 'abandoned' and the states it had finished are carried into the new
    row's detail.carried_states, which run_tracked hands to the sweep. The
    abandoned row never beats the dead-man ledger; only a finished run does.
    """
    conn = _connect()
    try:
        ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, COALESCE(detail->'done_states', '[]'::jsonb)
                     FROM osm_load_runs
                    WHERE loader = %s AND status = 'running'
                      AND heartbeat_at < NOW() - make_interval(secs => %s)
                      AND started_at > NOW() - make_interval(hours => %s)
                    ORDER BY id DESC LIMIT 1 FOR UPDATE""",
                (loader, STALL_AFTER_S, RESUME_WITHIN_H))
            dead = cur.fetchone()
            carried = sorted(set(dead[1] or [])) if dead else []
            # Append-only log on a serial id: the ON CONFLICT can never fire.
            cur.execute("""INSERT INTO osm_load_runs (loader, status, states_total, detail)
                           VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id""",
                        (loader, "running", len(US_STATES),
                         json.dumps({"carried_states": carried,
                                     "resumed_from": dead[0] if dead else None})))
            rid = cur.fetchone()[0]
            if dead:
                cur.execute("""UPDATE osm_load_runs
                                  SET status = 'abandoned', finished_at = NOW(),
                                      note = %s
                                WHERE id = %s""",
                            (f"thread died mid-sweep (no heartbeat); resumed by run {rid} "
                             f"with {len(carried)} state(s) carried", dead[0]))
        conn.commit()
        return rid
    finally:
        conn.close()


def _carried_for(run_id):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT COALESCE(detail->'carried_states', '[]'::jsonb)
                             FROM osm_load_runs WHERE id = %s""", (run_id,))
            r = cur.fetchone()
        return list(r[0] or []) if r else []
    finally:
        conn.close()


def _update_run(run_id, res, status=None, note=None):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE osm_load_runs
                      SET heartbeat_at = NOW(),
                          states_done = %s, states_total = %s,
                          fetched = %s, inserted = %s, failed_states = %s,
                          detail = %s,
                          status = COALESCE(%s, status),
                          note = COALESCE(%s, note),
                          finished_at = CASE WHEN %s IS NULL THEN finished_at ELSE NOW() END
                    WHERE id = %s""",
                (int(res.get("states_done") or 0), int(res.get("states_total") or 0),
                 int(res.get("fetched") or 0), int(res.get("inserted") or 0),
                 json.dumps(res.get("failed_states") or {}),
                 json.dumps({k: v for k, v in res.items()
                             if k not in ("per_state", "failed_states")}, default=str),
                 status, note, status, run_id))
        conn.commit()
    finally:
        conn.close()


def run_tracked(loader, run_id=None):
    """Run one TRACKED loader: durable row + heartbeat + dead-man beat.

    Never raises. The row always ends in success / no_new_data / error unless
    the process itself dies — which the reader reports as stalled.
    """
    feed, fn = TRACKED[loader]
    res, status, note = {}, "error", None
    try:
        if run_id is None:
            run_id = start_run(loader)
        res = fn(progress=lambda r: _update_run(run_id, r),
                 carried=_carried_for(run_id))
        status, note = classify(res)
    except Exception as e:  # noqa: BLE001 — recorded below, never swallowed
        status, note = "error", f"{type(e).__name__}: {str(e)[:200]}"
    if run_id is not None:
        try:
            _update_run(run_id, res if isinstance(res, dict) else {}, status, note)
        except Exception as e:  # noqa: BLE001
            logger.error("osm_load_runs finish write failed for %s: %s", loader, e)
    try:
        from routes.ingest_runs import record_beat
        record_beat(feed, status=status,
                    rows=int((res or {}).get("inserted") or 0) if isinstance(res, dict) else 0,
                    cad=float(CADENCE_HOURS), note=(note or "")[:280] or None)
    except Exception as e:  # noqa: BLE001 — a dropped beat is logged, loudly
        logger.error("deadman beat DROPPED feed=%s status=%s err=%s", feed, status, e)
    return {"run_id": run_id, "status": status, "note": note,
            "inserted": (res or {}).get("inserted") if isinstance(res, dict) else None}


def recent_runs(loader=None, run_id=None, limit=20):
    """Rows from osm_load_runs, newest first, each with a derived `state`:
    the stored status, or 'stalled' for a running row that stopped beating."""
    conn = _connect()
    try:
        ensure_tables(conn)
        where, args = [], []
        if run_id is not None:
            where.append("id = %s"); args.append(int(run_id))
        if loader:
            where.append("loader = %s"); args.append(loader)
        sql = ("SELECT id, loader, status, started_at, heartbeat_at, finished_at, "
               "states_done, states_total, fetched, inserted, failed_states, note, "
               "(status = 'running' AND heartbeat_at < NOW() - make_interval(secs => %s)) "
               "FROM osm_load_runs")
        args.insert(0, STALL_AFTER_S)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT %s"
        args.append(max(1, min(int(limit), 200)))
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        out.append({"id": r[0], "loader": r[1], "status": r[2],
                    "state": "stalled" if r[12] else r[2],
                    "started_at": r[3].isoformat() if r[3] else None,
                    "heartbeat_at": r[4].isoformat() if r[4] else None,
                    "finished_at": r[5].isoformat() if r[5] else None,
                    "states_done": r[6], "states_total": r[7],
                    "fetched": r[8], "inserted": r[9],
                    "failed_states": r[10], "note": r[11]})
    return out


def run_all_osm(priority_states=None):
    """One-shot orchestrator: every tracked loader, each with its own run row."""
    return {k: run_tracked(k) for k in TRACKED}
