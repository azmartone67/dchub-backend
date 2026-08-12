"""Data Liveness Master Shell — GET /admin/data-liveness
tick: /api/v1/admin/data-liveness/master-tick
kill: DATA_LIVENESS_SHELL_DISABLE=1

Built 2026-08-07 as the answer to a question the ingestion-freshness board
(#2317) could not answer: not "was this layer WRITTEN" but "is it GROWING".

★ WHY A SECOND BOARD. Freshness and growth are different questions and
gas_pipelines proves it. It rewrote 30,000 of its 30,918 rows on 2026-08-03,
so the freshness board reads it GREEN — written 3.8 days ago, well inside
cadence. Its net row change over 54 days is ZERO. That is a treadmill: a
loader that burns compute, passes every recency check, and adds nothing. A
static inventory with a heartbeat is exactly the competitor posture this
platform exists not to be, and no board caught it, because recency cannot.

Four lanes, one per open question from the 2026-08-07 liveness audit:

  1. net_growth      — per-layer net change over 7d/30d, read from
                       infra_growth_snapshot (daily counts since 2026-06-14).
  2. treadmill       — layers that WRITE heavily and net ~zero.
  3. never_ran       — jobs the scheduler declares that have never executed.
  4. health_signal   — whether cron_last_run.last_status can be trusted at all.

★★★ THE TRAP THIS BOARD ALMOST SHIPPED: A REPOINT IS NOT GROWTH.
Measured live 2026-08-07, the 7d snapshot deltas read transmission_lines
+95,560 and power_plants_eia +1,034. Neither is growth. Both layers had been
counting ABANDONED TWINS (transmission_lines counted `infrastructure_layers
WHERE category='transmission'` = 0 rows; power_plants_eia counted a 13,446-row
table with no timestamp) and were repointed at their live tables on 2026-08-07
by #2320. The series therefore contains a STEP where the instrument changed,
not where the data grew. Publishing "+95,560 transmission lines this week"
would be the same class of lie as reading a truncate-and-reload as growth —
flattering, and false.

So every delta is classified BY THE SHAPE OF THE SERIES:
  window starts at 0, ends > 0       -> DISCONTINUITY (an empty twin was
                                        repointed; nothing is knowable)
  one day moves >= 5% of the TABLE   -> STEP (repoint or one-shot bulk load)
  otherwise                          -> SPREAD, and the net is real growth
DISCONTINUITY and STEP both make sustained growth UNMEASURABLE — reported as
UNKNOWN with the date named, NEVER as 0 and never as growth. A repoint and a
genuine bulk load are indistinguishable from the series alone, so the board
shows the shape and the date and lets a human settle it.

★ SHARE OF THE TABLE, NOT SHARE OF THE NET — the first draft used share-of-net,
which is 100% for ANY layer whose change all landed on one day, and so flagged
metro_fiber_routes (+18 rows on a 55,064-row table) as a discontinuity.
★ AND `sustained` MUST BE None, NOT net-minus-step. The first draft subtracted
the step and published the remainder as a measured 0; the treadmill lane then
convicted transmission_lines and power_plants — repointed the previous day, so
holding exactly one day of valid history — of adding nothing for 30 days.

HONESTY RULES (carried from #2317, each a defect shipped this week):
- UNREADABLE IS NOT DEAD. Any query that fails renders pass=None with the
  reason. Never False.
- AN UNKNOWN COUNT IS NEVER RENDERED 0.
- ABSENCE OF EVIDENCE IS NOT EVIDENCE OF ABSENCE. Lane 3 judges a job only if
  its handler is registered ON THE STAMPING BLUEPRINT (@jobs_bp.route). The URL
  path is NOT the test — that mistake shipped six false accusations on
  2026-08-07 (see _blueprint_job_routes). Everything else is listed OUT OF
  SCOPE rather than accused.
- THE PUBLISHED POPULATION IS BUILT FROM THE EXECUTED ONE (#2253).
"""
from __future__ import annotations

import ast
import os
import re as _re

from flask import Blueprint, Response, jsonify

# Imported, never copied — the honesty semantics must not drift between boards.
from routes.brain_ascension_master_shell import (  # noqa: F401
    _admin_ok, _check, _conn, _lane_verdict, _safe_lane)

data_liveness_master_shell_bp = Blueprint("data_liveness_master_shell", __name__)

# A single day moving at least this share of the WHOLE TABLE is a STEP — an
# instrument repoint or a one-shot bulk load, either way not accumulation.
#
# ★ Share of the TABLE, deliberately, not share of the window's net change.
# Share-of-net is 100% for ANY layer whose change all landed on one day, which
# flagged metro_fiber_routes (+18 rows on 55,064) as a discontinuity. Against
# the live 2026-08-07 spread this cut separates cleanly: the repointed layers
# moved 7.1% and 100% of their tables in a day, while the real movers moved
# 0.03% (fiber) and a few hundredths of a percent (substations) per day.
_STEP_TABLE_SHARE = 5.0

# A layer that rewrote at least this share of its rows in 30d...
_REWRITE_SHARE = 50.0
# ...while its sustained net change stayed under this share of the table, is a
# TREADMILL. Both halves are required: heavy writes alone are a healthy refresh,
# and zero growth alone is a legitimately static reference layer. Only the
# COMBINATION — full rewrite, no net change — is the pathology.
_TREADMILL_NET_SHARE = 0.1

# last_status is written by an after_request on EVERY response, while
# last_started_at is written only after auth succeeds. A completion stamped this
# long after the last successful start cannot belong to that run.
_SECOND_CALLER_MIN = 10.0

_SNAPSHOT_TABLE = "infra_growth_snapshot"


def _disabled() -> bool:
    return os.environ.get("DATA_LIVENESS_SHELL_DISABLE", "") == "1"


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _q(c, sql: str):
    """Fail-soft. (rows, None) or (None, reason). NEVER raises.

    LITERAL SQL only, no bound parameters, and therefore no literal % anywhere
    (the psycopg2 percent-substitution trap that has 500'd this codebase).
    """
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall(), None
    except Exception as e:  # noqa: BLE001
        try:
            c.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None, f"{type(e).__name__}: {str(e)[:110]}"


def _fmt(n) -> str:
    """An unknown count is NEVER rendered 0."""
    return "UNKNOWN" if n is None else f"{n:,}"


def _pct(a, b):
    return (100.0 * a / b) if b else None


# ── lane 1 · net growth ──────────────────────────────────────────────────────
def _series_sql(window_days: int) -> str:
    """Per-layer first/last count in the window, plus the largest single-day
    change and the date it happened. One statement, no params."""
    return f"""
WITH w AS (
  SELECT layer, snapshot_date, count,
         count - LAG(count) OVER (PARTITION BY layer ORDER BY snapshot_date)
           AS d
  FROM {_SNAPSHOT_TABLE}
  WHERE snapshot_date >= current_date - {window_days}),
f AS (SELECT DISTINCT ON (layer) layer, count c0 FROM w
      ORDER BY layer, snapshot_date),
l AS (SELECT DISTINCT ON (layer) layer, count c1 FROM w
      ORDER BY layer, snapshot_date DESC),
m AS (SELECT DISTINCT ON (layer) layer, d bigd, snapshot_date bigday FROM w
      WHERE d IS NOT NULL ORDER BY layer, abs(d) DESC),
n AS (SELECT layer, COUNT(DISTINCT snapshot_date) days FROM w GROUP BY layer)
SELECT f.layer, f.c0, l.c1, l.c1 - f.c0, m.bigd, m.bigday, n.days
FROM f JOIN l USING (layer) JOIN n USING (layer)
LEFT JOIN m USING (layer)
ORDER BY 4 DESC"""


def _classify(c0, c1, net, bigd):
    """(shape, sustained). `sustained` is None whenever the series spans a
    discontinuity — UNMEASURABLE, never 0.

    ★ THE FIRST DRAFT GOT THIS WRONG TWICE AND BOTH WERE CAUGHT ON LIVE DATA.
    (a) It removed the step's magnitude and published the remainder as a
        measured `sustained` of 0. But a series containing an instrument
        repoint has no comparable before-and-after: 0 there is UNKNOWN, and
        rendering unknown as zero is the rule this codebase keeps relearning.
        Downstream, the treadmill lane then CONVICTED transmission_lines and
        power_plants — repointed 2026-08-07, so possessing exactly one day of
        valid history — of adding nothing over 30 days.
    (b) It labelled ANY one-day change a STEP, because share-of-NET is 100%
        whenever all the change lands on one day. metro_fiber_routes moved
        55,046 -> 55,064: eighteen rows, entirely real, flagged as a
        discontinuity. The magnitude that matters is share of the TABLE, not
        share of the net.

    So:
      c0 == 0 while c1 > 0        -> DISCONTINUITY. The layer was counting an
                                     empty twin (or is cold-starting). Nothing
                                     about growth is knowable from this window.
      one day >= _STEP_TABLE_SHARE of the table
                                  -> STEP. A repoint and a genuine bulk load
                                     are indistinguishable from the series
                                     alone, so this board refuses to guess and
                                     names the date instead.
      otherwise                   -> SPREAD, and the net is real growth.
    """
    if net is None:
        return "UNKNOWN", None
    if c0 == 0 and (c1 or 0) > 0:
        return "DISCONTINUITY", None
    if not net:
        return "FLAT", 0
    if bigd is not None and c1:
        if (_pct(abs(bigd), c1) or 0.0) >= _STEP_TABLE_SHARE:
            return "STEP", None
    return "SPREAD", net


def _lane_net_growth() -> list[dict]:
    """Is the data actually accumulating?

    FAILS only when NOTHING is growing — i.e. no layer shows sustained
    (non-step) growth in 30d. That is the organism-is-dead condition and it is
    the only one worth waking someone for. Per-layer numbers are gauges: a
    quarterly reference layer sitting flat is normal and convicting on it is
    how a guard earns deletion.
    """
    checks: list[dict] = []
    c = _conn()
    if c is None:
        return [_check("growth_db", "growth measurable", None,
                       "db unavailable — growth UNKNOWN for every layer "
                       "(not zero, not flat)", critical=True)]
    try:
        # instrument trustworthiness first: a sparse series makes every delta
        # below less certain, and the reader must be told before the numbers.
        rows, err = _q(c, f"SELECT COUNT(DISTINCT snapshot_date), "
                          f"COUNT(DISTINCT layer), MAX(snapshot_date), "
                          f"current_date - MAX(snapshot_date) "
                          f"FROM {_SNAPSHOT_TABLE} "
                          f"WHERE snapshot_date >= current_date - 30")
        if not rows:
            checks.append(_check(
                "growth_instrument", "growth instrument is readable", None,
                f"UNMEASURABLE: {_SNAPSHOT_TABLE} "
                + ("unreadable — " + str(err) if rows is None
                   else "returned no rows in the last 30d")
                + f". Net "
                f"growth cannot be computed; this is a read failure, not a "
                f"finding of zero growth.", critical=True))
            return checks
        days, layers, newest, age = rows[0]
        fresh = age is not None and age <= 2
        checks.append(_check(
            "growth_instrument", "growth instrument is readable", fresh,
            (f"{_SNAPSHOT_TABLE}: {days}/31 days present across {layers} "
             f"layers, newest {newest} ({age}d old). Every delta below is only "
             f"as good as this series — a missing day understates a window. "
             + ("Series is current." if fresh else
                "SERIES IS STALE: deltas below may be truncated.")),
            critical=False))

        by_layer: dict = {}
        for win in (7, 30):
            rows, err = _q(c, _series_sql(win))
            if rows is None:
                checks.append(_check(
                    f"growth_{win}d", f"{win}d growth readable", None,
                    f"UNMEASURABLE: {win}d series query failed — {err}",
                    critical=True))
                continue
            for lay, c0, c1, net, bigd, bigday, ndays in rows:
                shape, sust = _classify(c0, c1, net, bigd)
                by_layer.setdefault(lay, {})[win] = {
                    "c0": c0, "c1": c1, "net": net, "shape": shape,
                    "sustained": sust, "bigd": bigd, "bigday": bigday,
                    "days": ndays}

        if not by_layer:
            return checks

        grew = sorted([l for l, d in by_layer.items()
                       if (d.get(30, {}).get("sustained") or 0) > 0])
        stepped = sorted([l for l, d in by_layer.items()
                          if d.get(30, {}).get("shape") == "STEP"])
        flat = sorted([l for l in by_layer if l not in grew and l not in stepped])

        checks.append(_check(
            "something_is_growing", "at least one layer is accumulating",
            bool(grew),
            (f"{len(grew)} of {len(by_layer)} layers show SUSTAINED growth in "
             f"30d: {grew or 'NONE'}. STEP (single-day change — repoint or "
             f"bulk load, excluded from sustained): {stepped or 'none'}. "
             f"Flat: {len(flat)} — {flat}. "
             + ("A flat quarterly reference layer is normal; this check fails "
                "only if NOTHING is accumulating anywhere."
                if grew else
                "NOTHING IS ACCUMULATING. Every layer is flat or stepped — "
                "the inventory is static.")),
            critical=True))

        for lay in sorted(by_layer):
            d7 = by_layer[lay].get(7, {})
            d30 = by_layer[lay].get(30, {})
            step_note = ""
            if d30.get("shape") == "DISCONTINUITY":
                step_note = (
                    f" ★DISCONTINUITY: the window STARTS AT ZERO and ends at "
                    f"{_fmt(d30.get('c1'))}. A layer does not populate itself "
                    f"from empty — it was counting an abandoned twin and was "
                    f"repointed (see #2320). Before and after are not "
                    f"comparable, so growth here is UNMEASURABLE, NOT "
                    f"{_fmt(d30.get('net'))}. It becomes measurable once 30d "
                    f"of post-repoint history exists.")
            elif d30.get("shape") == "STEP":
                step_note = (
                    f" ★STEP: {_fmt(d30.get('bigd'))} moved on a single day "
                    f"({d30.get('bigday')}) — that is "
                    f"{(_pct(abs(d30.get('bigd') or 0), d30.get('c1')) or 0):.1f}% "
                    f"of the whole table in one day. An instrument repoint and "
                    f"a genuine bulk load look identical from the series, so "
                    f"this board does NOT guess: sustained growth is "
                    f"UNMEASURABLE for this window. Check what ran on that "
                    f"date.")
            checks.append(_check(
                f"growth_{lay}", f"{lay} — net change", True,
                (f"7d {_fmt(d7.get('net'))} ({d7.get('shape','?')}), "
                 f"30d {_fmt(d30.get('net'))} ({d30.get('shape','?')}); "
                 f"now {_fmt(d30.get('c1'))}, 30d ago {_fmt(d30.get('c0'))}, "
                 f"{d30.get('days','?')} snapshots.{step_note} GAUGE ONLY."),
                critical=False))
        return checks
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


# ── lane 2 · treadmill ───────────────────────────────────────────────────────
# (snapshot layer -> physical table, freshness column). Mirrors routes/
# infra_growth.py _LAYERS for the layers whose physical table carries an
# ingestion timestamp; a layer with no such column cannot be judged and says so.
_TREADMILL_LAYERS = (
    ("gas_pipelines", "gas_pipelines", "created_at"),
    ("transmission_lines", "transmission_lines", "created_at"),
    ("power_plants_eia", "power_plants", "created_at"),
    ("metro_fiber_routes", "fiber_routes", "created_at"),
    ("substations", "substations", "created_at"),
    ("data_centers", "discovered_facilities", "first_seen"),
)


def _lane_treadmill() -> list[dict]:
    """A loader that rewrites everything and adds nothing.

    Convicts ONLY on the combination: >=50% of the table rewritten in 30d AND
    sustained net change under 0.1% of the table. Heavy writes alone are a
    healthy refresh; zero growth alone is a legitimately static reference
    layer. gas_pipelines is the live case — 30,000 of 30,918 rows rewritten
    2026-08-03, net zero over 54 days.
    """
    checks: list[dict] = []
    c = _conn()
    if c is None:
        return [_check("treadmill_db", "treadmill measurable", None,
                       "db unavailable — UNKNOWN, not clean", critical=True)]
    try:
        rows, err = _q(c, _series_sql(30))
        if rows is None:
            # Query FAILED. Distinct from a query that succeeded and found
            # nothing — reporting "unreadable — None" for an empty result
            # sends the reader after a database problem that does not exist.
            return [_check("treadmill_series", "treadmill measurable", None,
                           f"UNMEASURABLE: snapshot series query failed — "
                           f"{err}", critical=True)]
        series = {r[0]: r for r in rows}
        for lay, tbl, col in _TREADMILL_LAYERS:
            got, err = _q(c, f"SELECT COUNT(*), COUNT(*) FILTER (WHERE {col} "
                             f">= now() - interval '30 days') FROM {tbl}")
            if not got:
                checks.append(_check(
                    f"treadmill_{lay}", f"{lay} — writes produce growth", None,
                    f"UNMEASURABLE: cannot read {tbl}.{col} — {err}. Read "
                    f"failure, not a treadmill finding.", critical=False))
                continue
            total, written = got[0]
            s = series.get(lay)
            if not s or not total:
                checks.append(_check(
                    f"treadmill_{lay}", f"{lay} — writes produce growth", None,
                    f"UNMEASURABLE: no 30d snapshot series for '{lay}' "
                    f"(table {tbl} holds {_fmt(total)} rows). Net change "
                    f"unknown — NOT zero.", critical=False))
                continue
            _, sc0, sc1, net, bigd, bigday, _ = s
            shape, sust = _classify(sc0, sc1, net, bigd)
            wshare = _pct(written, total) or 0.0
            # ★ sustained is None when the series spans a repoint or a step.
            # A treadmill verdict needs a MEASURED net change; convicting on an
            # unknown-rendered-as-zero is how transmission_lines and
            # power_plants — repointed one day earlier — were first accused of
            # adding nothing for 30 days.
            if sust is None:
                checks.append(_check(
                    f"treadmill_{lay}", f"{lay} — writes produce growth", None,
                    (f"{_fmt(written)} of {_fmt(total)} rows ({wshare:.0f}%) "
                     f"rewritten in 30d via {tbl}.{col}, but net change is "
                     f"UNMEASURABLE (shape={shape}"
                     + (f", step on {bigday}" if bigday else "")
                     + f"): the 30d series spans an instrument change, so "
                       f"there is no comparable before-and-after. A heavy "
                       f"rewrite with unknown net growth is NOT a proven "
                       f"treadmill — recheck once 30d of clean history "
                       f"exists."),
                    critical=False))
                continue
            nshare = abs(_pct(sust, total) or 0.0)
            treadmill = (wshare >= _REWRITE_SHARE
                         and nshare <= _TREADMILL_NET_SHARE)
            checks.append(_check(
                f"treadmill_{lay}", f"{lay} — writes produce growth",
                not treadmill,
                (f"{_fmt(written)} of {_fmt(total)} rows ({wshare:.0f}%) "
                 f"rewritten in 30d via {tbl}.{col}; sustained net change "
                 f"{_fmt(sust)} ({nshare:.2f}% of table, shape={shape}). "
                 + ("TREADMILL: the loader rewrites the table and adds "
                    "nothing. It passes every freshness check while the "
                    "inventory stands still — burn with no metabolism. Either "
                    "the upstream is genuinely unchanged (then stop paying to "
                    "rewrite it) or the loader is pinned to a cached copy "
                    "(then it is broken and looks healthy)."
                    if treadmill else
                    "Writes and growth are consistent." if wshare >= _REWRITE_SHARE
                    else "Not a full-rewrite loader; treadmill test N/A.")),
                critical=False))
        return checks
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


# ── lane 3 · declared but never ran ──────────────────────────────────────────
def _declared_jobs() -> tuple[dict, str | None]:
    """{job_key: endpoint} from dchub-scheduler.py's JOBS, via ast.

    Parsed, never imported: the scheduler module runs work at import time.
    """
    path = os.path.join(_repo_root(), "dchub-scheduler.py")
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except Exception as e:  # noqa: BLE001
        return {}, f"{type(e).__name__}: {str(e)[:90]}"
    for node in tree.body:
        if (isinstance(node, ast.Assign) and node.targets
                and getattr(node.targets[0], "id", "") == "JOBS"
                and isinstance(node.value, ast.Dict)):
            out = {}
            for k, v in zip(node.value.keys, node.value.values):
                ep = None
                if isinstance(v, ast.Dict):
                    for kk, vv in zip(v.keys, v.values):
                        if (getattr(kk, "value", "") == "endpoint"
                                and isinstance(vv, ast.Constant)):
                            ep = vv.value
                out[getattr(k, "value", "?")] = ep
            return out, None
    return {}, "JOBS assignment not found in dchub-scheduler.py"


def _blueprint_job_routes() -> tuple[set, str | None]:
    """The /api/jobs/<name> routes registered ON THE STAMPING BLUEPRINT.

    ★★★ 2026-08-07 — THE URL PATH IS NOT THE TEST, AND USING IT SHIPPED SIX
    FALSE ACCUSATIONS. cron_last_run is written by `@jobs_bp.after_request` in
    routes/jobs_routes.py. That hook fires for routes registered on THAT
    BLUEPRINT — not for every route whose path happens to start with
    /api/jobs/. Six of this shell's original "never executed" findings —
    subsea_sync, fiber_sync, permit_scraper, sec_parser, smoke_test,
    daily_image_render — are registered with @app.route (or another
    blueprint's) in fiber_integration.py, main.py, wire_permits.py,
    smoke_test.py and daily_render_fanout.py. Not one of them CAN be stamped,
    so their absence from cron_last_run was never evidence of anything.

    Proven the hard way: /api/jobs/subsea-sync was triggered manually on
    2026-08-07 and worked perfectly — 691 -> 699 cables, 1,908 -> 1,927 landing
    points, first write since 2026-03-27 — while still having no cron_last_run
    row, because @app.route bypasses the hook.

    So stampability is read from the DECORATOR, by parsing jobs_routes.py for
    `@jobs_bp.route("/api/jobs/<name>")`. Parsed rather than imported: the
    module opens DB connections at import.
    """
    path = os.path.join(_repo_root(), "routes", "jobs_routes.py")
    try:
        src = open(path, encoding="utf-8").read()
    except Exception as e:  # noqa: BLE001
        return set(), f"cannot read routes/jobs_routes.py ({type(e).__name__})"
    names = set(_re.findall(
        r"@jobs_bp\.route\(\s*['\"]/api/jobs/([a-z0-9\-]+)['\"]", src))
    if not names:
        return set(), ("no @jobs_bp.route('/api/jobs/...') declarations found "
                       "— the stamping blueprint may have been renamed")
    return names, None


def _lane_never_ran() -> list[dict]:
    """Registration is not function.

    ★ SCOPE IS THE WHOLE POINT. cron_last_run is populated by an after_request
    on the /api/jobs/ blueprint, so ONLY jobs whose endpoint starts with
    /api/jobs/ can ever appear there. 11 of the scheduler's 34 jobs point at
    /api/v1/admin/*, /api/kmz-discovery/* etc; their absence from the table is
    EXPECTED and proves nothing. Accusing them would be inventing a finding out
    of an instrument's blind spot.
    """
    checks: list[dict] = []
    declared, err = _declared_jobs()
    if err:
        return [_check("never_ran_source", "scheduler job list readable", None,
                       f"UNMEASURABLE: {err}. Cannot tell declared jobs from "
                       f"executed ones.", critical=True)]
    bp_routes, bp_err = _blueprint_job_routes()
    if bp_err:
        return [_check("never_ran_scope", "stampable set is knowable", None,
                       f"UNMEASURABLE: {bp_err}. Without the blueprint's route "
                       f"list there is no way to tell a job that never ran "
                       f"from one that simply cannot be stamped.",
                       critical=True)]
    stampable = {j: e for j, e in declared.items()
                 if isinstance(e, str) and e.startswith("/api/jobs/")
                 and e[len("/api/jobs/"):].split("/")[0].split("?")[0]
                 in bp_routes}
    out_of_scope = sorted(j for j in declared if j not in stampable)

    c = _conn()
    if c is None:
        return [_check("never_ran_db", "execution history readable", None,
                       "db unavailable — cannot tell declared from executed",
                       critical=True)]
    try:
        rows, err2 = _q(c, "SELECT job_name FROM cron_last_run")
        if rows is None:
            return [_check("never_ran_db", "execution history readable", None,
                           f"UNMEASURABLE: cron_last_run unreadable — {err2}",
                           critical=True)]
        seen = {r[0] for r in rows}
        never = []
        for j, ep in sorted(stampable.items()):
            name = ep[len("/api/jobs/"):].split("/")[0].split("?")[0]
            if name not in seen:
                never.append(f"{j} ({name})")
        checks.append(_check(
            "declared_jobs_have_run",
            "every stampable scheduled job has executed", not never,
            (f"{len(never)} of {len(stampable)} stampable jobs have NO row in "
             f"cron_last_run: {never or 'none'}. "
             + ("A job can sit in JOBS with a full schedule and fire nothing: "
                "no start command in this repo launches dchub-scheduler.py "
                "(Procfile and railway.json both run start_web.sh), so the "
                "JOBS dict is a declaration, not proof of execution. "
                if never else "")
             + f"OUT OF SCOPE ({len(out_of_scope)} jobs NOT registered with "
               f"@jobs_bp.route and therefore structurally un-stampable — "
               f"their absence proves nothing; subsea-sync is the worked "
               f"example, registered via @app.route, and it ran clean on "
               f"2026-08-07 with no row): {out_of_scope}."),
            critical=True))
        return checks
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


# ── lane 4 · is the health signal itself trustworthy? ────────────────────────
def _lane_health_signal() -> list[dict]:
    """cron_last_run.last_status is written by an after_request on EVERY
    response; last_started_at only after auth SUCCEEDS. So an unauthenticated
    caller hitting /api/jobs/<name> overwrites last_status without touching
    last_started_at — and the job reads as failing when it ran fine.

    Live 2026-08-07: 11 jobs carry last_status=http_401 stamped between 14
    minutes and 6.3 DAYS after their last successful start. No request lasts
    six days. Anything reading last_status as job health — including the cron
    dead-man — is being lied to in both directions: healthy jobs look broken,
    and a genuinely broken job could be masked by a passing prober.
    """
    c = _conn()
    if c is None:
        return [_check("health_signal_db", "health signal readable", None,
                       "db unavailable — UNKNOWN, not clean", critical=True)]
    try:
        rows, err = _q(c, """SELECT job_name, last_status,
  EXTRACT(EPOCH FROM (last_completed_at - last_started_at))/60
  FROM cron_last_run
  WHERE last_started_at IS NOT NULL AND last_completed_at IS NOT NULL""")
        if rows is None:
            return [_check("health_signal_db", "health signal readable", None,
                           f"UNMEASURABLE: cron_last_run unreadable — {err}",
                           critical=True)]
        bad = sorted([(j, s, g) for j, s, g in rows
                      if (g or 0) > _SECOND_CALLER_MIN],
                     key=lambda x: -(x[2] or 0))
        worst = f"{bad[0][0]} ({bad[0][2]:.0f} min)" if bad else "none"
        return [_check(
            "status_belongs_to_the_run",
            "last_status was stamped by the run it describes", not bad,
            (f"{len(bad)} of {len(rows)} jobs carry a last_status stamped more "
             f"than {_SECOND_CALLER_MIN:.0f} min after their last successful "
             f"start — worst: {worst}. "
             + (f"These statuses come from a DIFFERENT, unauthenticated caller, "
                f"not from the job: {[j for j, _, _ in bad]}. last_status is "
                f"therefore NOT a health signal — it reports whoever called "
                f"last. Healthy jobs read as failing, and a real failure can be "
                f"overwritten by a prober. Fix the reader or stamp completion "
                f"only for the request that was authenticated."
                if bad else
                "Every completion belongs to the run that started it.")),
            critical=True)]
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


_LANES = (
    ("net_growth", "is the data accumulating", _lane_net_growth),
    ("treadmill", "loaders that write but add nothing", _lane_treadmill),
    ("never_ran", "declared jobs that have never executed", _lane_never_ran),
    ("health_signal", "can cron health be trusted", _lane_health_signal),
)


def _population() -> dict:
    """Built from the executed lane/layer lists, never hand-typed (#2253)."""
    return {
        "question": "not 'was it written' (see /admin/ingestion-freshness) "
                    "but 'is it GROWING'",
        "lanes": [lid for lid, _, _ in _LANES],
        "growth_source": _SNAPSHOT_TABLE,
        "growth_basis": "last snapshot minus first snapshot inside the window",
        "treadmill_layers": [l[0] for l in _TREADMILL_LAYERS],
        "treadmill_rule": (
            f"rewrote >= {_REWRITE_SHARE:.0f}% of rows in 30d AND sustained "
            f"net change <= {_TREADMILL_NET_SHARE}% of the table"),
        "step_rule": (
            f"a window starting at 0 is a DISCONTINUITY (instrument repoint); "
            f"a single day moving >= {_STEP_TABLE_SHARE}% of the TABLE is a "
            f"STEP. Both make sustained growth UNMEASURABLE for that window — "
            f"never 0 — because a repoint and a bulk load are "
            f"indistinguishable from the series alone"),
        "never_ran_scope": (
            "only jobs whose handler is registered with @jobs_bp.route — the "
            "after_request that writes cron_last_run is bound to that "
            "BLUEPRINT, not to the /api/jobs/ path. Routes registered via "
            "@app.route under the same path prefix can never be stamped"),
        "sql": {"series_30d": _series_sql(30)},
    }


def _tick() -> dict:
    lanes = []
    for lid, name, fn in _LANES:
        checks = _safe_lane(fn)
        lanes.append({"id": lid, "name": name, "checks": checks,
                      "verdict": _lane_verdict(checks)})
    return {
        "shell": "data-liveness",
        "note": ("Freshness answers 'was it written'; this answers 'did it "
                 "GROW'. gas_pipelines is green on the first and a treadmill "
                 "on the second. A REPOINT IS NOT GROWTH: a single-day step is "
                 "excluded from sustained accumulation."),
        "population": _population(),
        "lanes": lanes,
        "lanes_total": len(lanes),
        "lanes_pass": sum(1 for x in lanes if x["verdict"] == "PASS"),
        "summary": " ".join(f"{x['id']}={x['verdict']}" for x in lanes),
    }


@data_liveness_master_shell_bp.route(
    "/api/v1/admin/data-liveness/master-tick", methods=["GET"])
def data_liveness_master_tick():
    if _disabled():
        return jsonify({"disabled": True}), 200
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_tick())


@data_liveness_master_shell_bp.route("/admin/data-liveness", methods=["GET"])
def data_liveness_board():
    if _disabled():
        return Response("shell disabled", status=404,
                        mimetype="text/plain")
    if not _admin_ok():
        return Response("unauthorized", status=401, mimetype="text/plain")
    t = _tick()
    rows = []
    for lane in t["lanes"]:
        rows.append(f"\n{lane['verdict']:<5} {lane['id']} — {lane['name']}")
        for c in lane["checks"]:
            mark = {True: "OK ", False: "RED", None: " ? "}[c["pass"]]
            rows.append(f"   [{mark}] {c['name']}\n        {c['detail']}")
    return Response(t["summary"] + "\n" + t["note"] + "\n" + "\n".join(rows),
                    mimetype="text/plain")
