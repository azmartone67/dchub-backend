"""routes/brain_audit_intake.py — the 138 audit findings become brain work.

WHY (docs/BRAIN_SUPERUSER_TAGTEAM.md, remaining item 3)
=======================================================
Shell #52 machine-verifies the 2026-08-07 audit's 138-finding registry every
tick and renders a closure board — but the brain never SAW those findings.
They lived in the shell's own dashboard, so the audit's central lesson ("the
platform sees almost everything and acts on almost nothing") reproduced
itself one level up: a board that grades the backlog is not a loop that works
it.

This module is the intake. It takes shell #52's registry verdicts and feeds
the machine-verified-FAILING ones into `/api/v1/heal/findings`'s
`actionable_backend_issues`, which is the brain's Layer-5 worklist. From
there they flow through the SAME triage every other finding does — including
the finding-router (active / operator_config / mcp_server / terminal).

## Three rules that keep this from making things worse

1. **OPEN-RED only.** A registry row with no live checker is `OPEN` — honest
   ignorance, not evidence. Seeding those would hand the brain 138 items it
   cannot verify or close, crowd out the ~10/cycle model budget, and inflate
   the backlog the finding-router exists to deflate. Only rows a checker
   currently FAILS are real, reproducible work.
2. **Severity-capped.** `AUDIT_INTAKE_MAX` (default 8) rows per refresh,
   ordered critical→high→medium, so the audit stream can never starve the
   detectors already feeding the loop.
3. **Never on the hot path.** `/api/v1/heal/findings` is public, hot and
   single-replica; shell #52's tick makes live probes. So the tick runs on a
   REFRESH cadence (`AUDIT_INTAKE_TTL_S`, default 6h) behind a brain_state
   snapshot, and the heal path only ever reads that cached snapshot.

Kill switch: `AUDIT_INTAKE_DISABLE=1` → `audit_findings()` returns [].
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

brain_audit_intake_bp = Blueprint("brain_audit_intake", __name__)

_STATE_KEY = "audit_intake_snapshot"
_SEV_ORDER = {"C": 0, "H": 1, "M": 2, "L": 3}
_ISSUE_PREFIX = "audit_"


def _disabled() -> bool:
    return os.environ.get("AUDIT_INTAKE_DISABLE", "0") == "1"


def _max_rows() -> int:
    try:
        return max(0, int(os.environ.get("AUDIT_INTAKE_MAX", "8")))
    except Exception:
        return 8


def _ttl_s() -> int:
    try:
        return max(600, int(os.environ.get("AUDIT_INTAKE_TTL_S", "21600")))
    except Exception:
        return 21600


def _db_url() -> str | None:
    return (os.environ.get("NEON_DATABASE_URL")
            or os.environ.get("DATABASE_URL"))


def _state_get(key: str):
    url = _db_url()
    if not url:
        return None
    try:
        import psycopg2
        with psycopg2.connect(url, connect_timeout=5) as conn, \
                conn.cursor() as cur:
            cur.execute(
                "SELECT state_value FROM brain_state WHERE state_key=%s",
                (key,))
            row = cur.fetchone()
            val = row[0] if row else None
            if isinstance(val, str):
                val = json.loads(val or "null")
            return val
    except Exception:
        return None


def _state_set(key: str, value) -> bool:
    url = _db_url()
    if not url:
        return False
    try:
        import psycopg2
        with psycopg2.connect(url, connect_timeout=5) as conn, \
                conn.cursor() as cur:
            cur.execute(
                """INSERT INTO brain_state (state_key, state_value, updated_at)
                   VALUES (%s, %s::jsonb, NOW() ON CONFLICT DO NOTHING)
                   ON CONFLICT (state_key)
                   DO UPDATE SET state_value = EXCLUDED.state_value,
                                 updated_at = NOW()""",
                (key, json.dumps(value)))
            conn.commit()
        return True
    except Exception:
        return False


# ── pure selection (the part worth testing) ─────────────────────────────

def _cycle_no(now_s: float | None = None) -> int:
    """One tick per TTL window — the rotation clock."""
    return int((time.time() if now_s is None else now_s) // _ttl_s())


def select_seedable(rows: list, limit: int | None = None,
                    cycle: int | None = None) -> tuple[list, int]:
    """(rows to seed, how many OPEN-RED exist). Severity-ordered, ROTATED, capped.

    `rows` are shell #52 registry rows: {id, domain, sev, effort, status,
    title}. OPEN (no checker) and CLOSED/ACKED/'?' are all excluded — only a
    checker that is CURRENTLY FAILING is evidence of live, reproducible work.

    ★ ROTATION, added 2026-08-08 after the first live refresh measured 26
    OPEN-RED against a cap of 8. A fixed severity sort always returns the SAME
    top 8, so the other 18 would never once reach the worklist — the r78
    head-of-list starvation that flatlined Layer-5 proposals for twelve days,
    rebuilt one layer up. The window advances by `limit` each TTL cycle and
    wraps, so every OPEN-RED finding gets budget within ceil(n/limit) cycles
    while severity still decides the order within a window.
    """
    limit = _max_rows() if limit is None else limit
    red = [r for r in (rows or []) if (r or {}).get("status") == "OPEN-RED"]
    red.sort(key=lambda r: (_SEV_ORDER.get(r.get("sev"), 9),
                            str(r.get("id"))))
    total = len(red)
    if limit <= 0 or total <= limit:
        return red[:limit], total
    cyc = _cycle_no() if cycle is None else cycle
    start = (cyc * limit) % total
    window = (red + red)[start:start + limit]
    return window, total


def to_findings(rows: list) -> list:
    """Registry rows → the {url, issue, count, detail} shape the heal
    endpoint's actionable_backend_issues list uses.

    The `issue` label is prefixed `audit_` so no FIX_MAP key matches it —
    the master-heal string-replacer must never try to body-substitute an
    audit finding (same reasoning as the `asset_` prefix)."""
    out = []
    for r in rows or []:
        fid = str(r.get("id") or "").strip()
        if not fid:
            continue
        out.append({
            "url": "dchub://audit/%s" % fid,
            "issue": "%s%s %s" % (_ISSUE_PREFIX, r.get("sev") or "?",
                                  (r.get("title") or "")[:240]),
            "count": 1,
            "detail": ("Shell #52 audit-closure registry %s (domain=%s, "
                       "severity=%s, effort=%s) — its live checker is "
                       "FAILING. Evidence + closure board: "
                       "/admin/audit-closure"
                       % (fid, r.get("domain"), r.get("sev"),
                          r.get("effort"))),
        })
    return out


# ── snapshot refresh (runs shell #52's tick, off the hot path) ──────────

def refresh_snapshot(force: bool = False, tick_fn=None) -> dict:
    """Run shell #52's tick and persist the seedable slice. Fail-soft."""
    if _disabled():
        return {"ok": True, "skipped": "AUDIT_INTAKE_DISABLE=1"}
    prev = _state_get(_STATE_KEY) or {}
    age = time.time() - float(prev.get("ts") or 0)
    if not force and prev and age < _ttl_s():
        return {"ok": True, "skipped": "fresh", "age_s": int(age),
                "rows": len(prev.get("rows") or [])}
    try:
        if tick_fn is None:
            from routes.audit_closure_master_shell import _run_tick as tick_fn
        tick = tick_fn() or {}
        reg = tick.get("registry") or {}
        rows, open_red = select_seedable(reg.get("findings") or [])
        snap = {"ts": time.time(),
                "as_of": datetime.now(timezone.utc).isoformat(),
                "closure_pct": reg.get("closure_pct"),
                "total": reg.get("total"),
                "open_red_total": open_red,
                "cycle": _cycle_no(),
                "rows": rows}
        _state_set(_STATE_KEY, snap)
        # ★ NO SILENT CAPS: say what was left out. A bounded lane that reports
        # only what it took reads as full coverage to whoever finds it later.
        deferred = max(0, open_red - len(rows))
        return {"ok": True, "refreshed": True, "rows": len(rows),
                "open_red_total": open_red, "deferred_to_next_cycle": deferred,
                "closure_pct": reg.get("closure_pct")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:160]}


def audit_findings() -> list:
    """The heal endpoint's read: cached snapshot only, never a live tick."""
    if _disabled():
        return []
    try:
        snap = _state_get(_STATE_KEY) or {}
        return to_findings(snap.get("rows") or [])
    except Exception:
        return []


# ── endpoints (admin) ───────────────────────────────────────────────────

def _admin_ok_local() -> bool:
    try:
        from routes.brain_mechanical_classifier import _admin_ok
        return bool(_admin_ok())
    except Exception:
        key = os.environ.get("DCHUB_ADMIN_KEY", "")
        sent = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
        return bool(key) and sent == key


@brain_audit_intake_bp.get("/api/v1/brain/audit-intake")
def audit_intake_status():
    if not _admin_ok_local():
        return jsonify(ok=False, error="admin key required"), 401
    snap = _state_get(_STATE_KEY) or {}
    seeded = to_findings(snap.get("rows") or [])
    open_red = snap.get("open_red_total")
    out = {"ok": True, "enabled": not _disabled(),
           "max_rows": _max_rows(), "ttl_s": _ttl_s(),
           "snapshot_as_of": snap.get("as_of"),
           "closure_pct": snap.get("closure_pct"),
           "registry_total": snap.get("total"),
           "open_red_total": open_red,
           "cycle": snap.get("cycle"),
           # The board must never imply the cap is the whole set.
           "deferred_to_next_cycle": (max(0, open_red - len(seeded))
                                      if isinstance(open_red, int) else None),
           "seeded": seeded}
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@brain_audit_intake_bp.post("/api/v1/brain/audit-intake/refresh")
def audit_intake_refresh():
    if not _admin_ok_local():
        return jsonify(ok=False, error="admin key required"), 401
    result = refresh_snapshot(force=request.args.get("force") == "1")
    resp = jsonify(result)
    resp.headers["Cache-Control"] = "no-store"
    return resp
