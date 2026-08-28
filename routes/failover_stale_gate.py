"""
routes/failover_stale_gate.py — fail-CLOSED gate for the FAILOVER origin.

★ 2026-07-24 incident (the reason this exists)
-----------------------------------------------------------------------
The operator opened /admin/flywheel and read "2/6 lanes green · 0 distinct
real agents/7d · 0 real calls/7d · mcp_tool_calls newest row 262.4h ago ·
last X post 11.0d ago" and reasonably concluded the whole flywheel had
regressed. Nothing had. That render was served by the RENDER failover
origin, whose database is frozen at 2026-07-13 — and it answered with
HTTP 200. Same requests, same minute, both origins:

    /api/v1/mcp/funnel     railway 78 agents / 3,907 calls → render 0 / 0
    /api/v1/media/feed-v3  railway 106 items               → render 61
    /api/v1/ops/deadman    railway ok=true                 → render ok=false

A backup that answers 200 with confident zeros is worse than one that is
simply down: every ops surface silently prints "total collapse", and the
operator spends the morning chasing a phantom regression. Staleness must
be LOUD.

What this does
-----------------------------------------------------------------------
On the failover origin ONLY, probe the canonical ingest heartbeat
(max(mcp_tool_calls.created_at), the same clock lane 3 of the flywheel
reads) and, when it is older than STALE_GATE_MAX_AGE_H (default 6h),
return 503 on the DATA-BEARING surfaces where staleness reads as a lie.

Why 503 is safe here — verified against dchub-frontend/_worker.js
proxyWithRetry (2026-07-24): the secondary is only consulted after the
primary already failed, and `if (!secResp || secResp.status >= 500)`
records a failure on the secondary and returns *the primary's* error.
So a 503 from here yields Railway's real error (an honest status code),
trips the render breaker for 30s (routing away from a mirror that cannot
answer truthfully), and CANNOT bounce back into a retry loop. Confirmed
before shipping — there is no path that re-enters this origin.

Scope is deliberately SURGICAL. Two different things get called "stale":
  · metrics/ops/admin — staleness is a LIE (0 agents, 0 calls). GATED.
  · facility/grid/market content — staleness is merely OLD, and old
    infrastructure data still beats an error page. NOT gated, so a real
    Railway outage still serves useful reads off the mirror.

★ SECOND GATE (2026-08-28) — CODE staleness, which the age probe cannot see.
The probe above measures DATA age. A mirror can hold perfectly fresh data
and month-old CODE, answer 200, and be wrong about what it is capable of
doing. That is not hypothetical:

  On 2026-08-28 the canonical GET /api/v1/sponsorships/active served the
  PRE-#3256 three-slot contract from render-failover — 200, valid JSON,
  fresh data, and missing the three slots the two sold advertising
  products render into. CF Rule #3 ("Cache Public API", mode
  override_origin) then cached it past its own no-store for ~2 minutes.
  Nothing alarmed: a 200 with the wrong body is quieter than the 404 this
  failover chain is known for, and the data-age gate never fired because
  the data was not stale.

So _CODE_DEPENDENT_PREFIXES gates on ROLE ALONE, with no age condition. A
path belongs there when its answer describes what this build can DO rather
than what the database holds — a mirror has nothing true to say about
that, at any data age. It is a much smaller set than _GATED_PREFIXES and
should stay that way; content paths still serve, per the rule above.

Safety properties
  · The primary can never gate itself: _is_failover() mirrors main.py:82
    and keys off RENDER / RENDER_SERVICE_ID / DCHUB_FAILOVER, none of
    which exist on Railway. Verify post-deploy via the freshness probe.
  · FAIL-OPEN on probe error. If the heartbeat query itself fails we do
    NOT gate — a probe hiccup must never take out a healthy mirror. A
    truly broken origin will 5xx on its own and the worker handles that.
  · Kill switch: STALE_GATE_DISABLE=1 (reverts to today's behavior).
  · 60s probe cache + a short-lived connection opened OUTSIDE the app
    pool, so the gate can never contribute to pool saturation.

Also exposes GET /api/v1/ops/origin-freshness — public, no secrets, just
{role, data_age_hours, stale}. This is the cross-origin probe that lets
the Integrity master shell (#25) see a stale mirror from the primary,
which is otherwise invisible: the mirror's staleness lives in a DIFFERENT
database, so no pure-DB query from Railway can ever detect it.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from flask import Blueprint, Response, g, jsonify, request

logger = logging.getLogger(__name__)

failover_stale_gate_bp = Blueprint("failover_stale_gate", __name__)

# ── origin role ───────────────────────────────────────────────────────
# Mirrors main.py:82 (and mcp_gateway.py:51, which mirrors it the same
# way). Computed locally so this module has no import-order coupling to
# main. RENDER is set automatically by Render; Railway sets none of these,
# which is the property that makes it impossible for the PRIMARY to gate
# itself.


def _is_failover() -> bool:
    return (
        os.environ.get("RENDER", "").lower() in ("true", "1", "yes")
        or bool(os.environ.get("RENDER_SERVICE_ID"))
        or os.environ.get("DCHUB_FAILOVER", "").lower() in ("true", "1", "yes")
    )


def _disabled() -> bool:
    return (os.environ.get("STALE_GATE_DISABLE") or "").strip() == "1"


def _max_age_seconds() -> float:
    try:
        return float(os.environ.get("STALE_GATE_MAX_AGE_H") or 6.0) * 3600.0
    except (TypeError, ValueError):
        return 6.0 * 3600.0


# ── gated surfaces ────────────────────────────────────────────────────
# Metrics / ops / admin: a stale answer here is indistinguishable from a
# real collapse, which is exactly the failure this module exists to stop.
_GATED_PREFIXES = (
    "/api/v1/admin/",
    "/admin/",
    "/api/v1/ops/",
    "/api/v1/analytics/",
    "/api/v1/mcp/funnel",
    "/api/v1/mcp/retention",
    "/api/v1/mcp/agent-leaderboard",
)

# Never gated: liveness (the host platform's own health checks must keep
# passing — a gated origin is degraded, not dead), and the freshness probe
# itself, which has to stay reachable precisely WHEN the origin is stale.
_EXEMPT_PREFIXES = (
    "/api/v1/ops/origin-freshness",
    "/health",
    "/healthz",
    "/api/v1/health",
    "/version",
    "/.well-known/",
)


def _path_is_gated(path: str) -> bool:
    p = (path or "").rstrip("/") or "/"
    for ex in _EXEMPT_PREFIXES:
        if p == ex.rstrip("/") or p.startswith(ex):
            return False
    for pref in _GATED_PREFIXES:
        if p == pref.rstrip("/") or p.startswith(pref):
            return True
    return False


# ── code-dependent surfaces (gated on ROLE, never on data age) ────────
# See the "SECOND GATE" section of the module docstring. These paths
# answer "what can this build do", not "what does the database hold", so
# a mirror running older code answers them wrongly with fresh data and a
# 200. Keep this set small: every addition costs availability during a
# real Railway outage.
_CODE_DEPENDENT_PREFIXES = (
    # The sponsorship surface reports which ad slots exist and renders the
    # sold blocks. A mirror without the current renderers reports a slot
    # set that no longer matches the products being sold. Proven case,
    # 2026-08-28.
    "/api/v1/sponsorships",
)


def _path_is_code_dependent(path: str) -> bool:
    p = (path or "").rstrip("/") or "/"
    for ex in _EXEMPT_PREFIXES:
        if p == ex.rstrip("/") or p.startswith(ex):
            return False
    for pref in _CODE_DEPENDENT_PREFIXES:
        if p == pref.rstrip("/") or p.startswith(pref):
            return True
    return False


# ── freshness probe (60s cache, off-pool connection) ──────────────────

_probe: dict = {"ts": 0.0, "age": None, "err": None}
_probe_lock = threading.Lock()
_PROBE_TTL = 60.0


def _conn():
    """Short-lived raw connection, deliberately OUTSIDE the app pool — the
    gate runs on every request and must never hold a shared pool slot.
    Mirrors flywheel_master_shell._conn."""
    try:
        import psycopg2 as _pg

        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=5)
        c.autocommit = True
        return c
    except Exception as e:
        logger.debug("[stale-gate] connect failed: %s", e)
        return None


def _probe_age_seconds(force: bool = False):
    """Age of the newest ingest row, or None if we could not determine it.
    None is NOT 0 — the caller must treat 'unknown' as 'do not gate'."""
    now = time.time()
    if not force:
        with _probe_lock:
            if _probe["ts"] and now - _probe["ts"] < _PROBE_TTL:
                return _probe["age"]
    age, err = None, None
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                # Literal SQL, no params tuple — psycopg2 would try to
                # %-substitute an empty tuple against any literal % here.
                cur.execute(
                    "SELECT EXTRACT(EPOCH FROM (now() - max(created_at))) FROM mcp_tool_calls"
                )
                row = cur.fetchone()
            age = float(row[0]) if row and row[0] is not None else None
        except Exception as e:
            err = str(e)[:200]
            logger.debug("[stale-gate] probe failed: %s", e)
        finally:
            try:
                c.close()
            except Exception:
                pass
    else:
        err = "no_db_connection"
    with _probe_lock:
        _probe["ts"] = now
        _probe["age"] = age
        _probe["err"] = err
    return age


def _commit() -> str | None:
    """The git SHA this container is actually running.

    ★ 2026-07-24: /api/v1/version reports a hand-maintained `build: 91`
    that is byte-identical on both origins, so it can never reveal code
    drift — which is why check_render_pipeline_blocked could not work even
    in principle. Both platforms inject the real SHA; expose it.
      Render  → RENDER_GIT_COMMIT (also SOURCE_VERSION)
      Railway → RAILWAY_GIT_COMMIT_SHA
    """
    for var in ("RENDER_GIT_COMMIT", "RAILWAY_GIT_COMMIT_SHA",
                "SOURCE_VERSION", "GIT_COMMIT"):
        v = (os.environ.get(var) or "").strip()
        if v:
            return v[:7]
    return None


def origin_state(force: bool = False) -> dict:
    """Public snapshot used by the freshness endpoint, the Integrity shell
    and the admin-HTML banner."""
    failover = _is_failover()
    age = _probe_age_seconds(force=force)
    max_age = _max_age_seconds()
    stale = bool(age is not None and age > max_age)
    return {
        "ok": True,
        "role": "failover" if failover else "primary",
        "is_failover": failover,
        "commit": _commit(),
        "data_age_hours": round(age / 3600.0, 2) if age is not None else None,
        "threshold_hours": round(max_age / 3600.0, 2),
        "stale": stale,
        "gate_active": bool(failover and stale and not _disabled()),
        "gate_disabled": _disabled(),
        "probe_error": _probe["err"],
        # Disclosed so a caller can see WHICH surfaces this origin refuses
        # on role alone, without having to probe each one and read a 503.
        "code_dependent_gated": bool(failover and not _disabled()),
        "code_dependent_prefixes": list(_CODE_DEPENDENT_PREFIXES),
    }


# ── the gate ──────────────────────────────────────────────────────────


def _before_request_gate():
    # Cheapest checks first — this runs on EVERY request.
    if _disabled() or not _is_failover():
        return None

    # ── gate 2: code-dependent paths. Role only, no probe, no age. ────
    # Deliberately BEFORE the age probe: this must hold even when the
    # mirror's data is perfectly fresh, which is the case that shipped
    # the wrong sponsorship contract to the edge on 2026-08-28.
    if _path_is_code_dependent(request.path):
        body = jsonify(
            ok=False,
            error="code_dependent_surface_on_failover_origin",
            detail=(
                "This is the FAILOVER origin. This surface reports what "
                "the running build can do, and a mirror may be behind on "
                "code regardless of how fresh its data is — so answering "
                "would risk publishing a contract that no longer matches "
                "the primary. Retry against the primary; if the primary "
                "is down, this surface is unavailable until it returns."
            ),
            role="failover",
            commit=_commit(),
            remedy="https://dchub.cloud/api/v1/ops/origin-freshness",
        )
        resp: Response = body
        resp.status_code = 503
        resp.headers["X-DC-Hub-Code-Dependent-Gate"] = "1"
        resp.headers["Retry-After"] = "60"
        resp.headers["Cache-Control"] = "no-store"
        g._dchub_stale_gated = True
        return resp

    if not _path_is_gated(request.path):
        return None
    age = _probe_age_seconds()
    if age is None:
        # FAIL-OPEN: unknown freshness must not take out the mirror.
        return None
    if age <= _max_age_seconds():
        return None

    hours = round(age / 3600.0, 1)
    body = jsonify(
        ok=False,
        error="stale_failover_origin",
        detail=(
            f"This is the FAILOVER origin and its data is {hours}h stale "
            f"(threshold {round(_max_age_seconds()/3600.0,1)}h). Refusing to "
            "answer with numbers that would read as a real collapse. Retry "
            "against the primary; if the primary is down, this surface is "
            "unavailable until it returns."
        ),
        data_age_hours=hours,
        role="failover",
        remedy="https://dchub.cloud/admin/integrity",
    )
    resp: Response = body
    resp.status_code = 503
    resp.headers["X-DC-Hub-Stale-Origin"] = str(hours)
    resp.headers["Retry-After"] = "60"
    resp.headers["Cache-Control"] = "no-store"
    g._dchub_stale_gated = True
    return resp


@failover_stale_gate_bp.route("/api/v1/ops/origin-freshness", methods=["GET"])
def origin_freshness():
    """Public. Exposes only a timestamp AGE and a role — no secrets, no row
    contents. Deliberately un-gated so it still answers while stale."""
    force = (request.args.get("fresh") or "") == "1"
    out = origin_state(force=force)
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def banner_html() -> str:
    """Origin-authoritative banner for admin HTML shells. The CF header
    x-dc-hub-served-by is invisible in a browser view-source workflow, so
    the origin states its own identity in the page body. Empty string on
    the healthy primary — no chrome when there is nothing to warn about."""
    try:
        st = origin_state()
    except Exception:
        return ""
    if not st["is_failover"] and not st["stale"]:
        return ""
    age = st["data_age_hours"]
    age_txt = f"{age}h" if age is not None else "unknown"
    if st["is_failover"] and st["stale"]:
        bg, msg = "#7f1d1d", (
            f"⚠ SERVED BY THE FAILOVER ORIGIN — data is {age_txt} stale. "
            "Numbers on this page are NOT current. Re-open against the primary."
        )
    elif st["is_failover"]:
        bg, msg = "#78350f", (
            f"SERVED BY THE FAILOVER ORIGIN (data age {age_txt}). "
            "The primary is the source of truth."
        )
    else:
        bg, msg = "#78350f", (
            f"PRIMARY ORIGIN, but the ingest heartbeat is {age_txt} old — "
            "freshness-dependent checks below may be understated."
        )
    return (
        f"<div style='background:{bg};color:#fee2e2;border-radius:10px;"
        f"padding:10px 14px;margin:12px 0;font-size:13px;font-weight:600'>{msg}</div>"
    )


def init_app(app) -> None:
    """Wire the gate + the freshness endpoint. Safe to call once at boot."""
    app.before_request(_before_request_gate)
    try:
        app.register_blueprint(failover_stale_gate_bp)
    except Exception as e:  # already registered on a re-init
        logger.debug("[stale-gate] blueprint register skipped: %s", e)
    logger.info(
        "[stale-gate] wired · role=%s · threshold=%sh · disabled=%s",
        "failover" if _is_failover() else "primary",
        round(_max_age_seconds() / 3600.0, 1),
        _disabled(),
    )
