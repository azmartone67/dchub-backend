"""routes/brain_qa_superuser_intake.py — the QA super-user board becomes brain work.

WHY
===
The QA super-user is the platform's only OUTSIDE-IN instrument. It probes
dchub.cloud the way a paying caller does, and its `critical` severity is
defined as "a paying caller is getting a wrong answer, or cannot pay"
(tools/qa_superuser/finding.py). Its verdicts have never reached the brain:
they land on a GitHub issue board and in `qa_superuser_runs`, and stop there.
So the sharpest evidence the platform produces about itself was invisible to
the loop whose whole job is working the backlog — the same shape as the audit
registry before `brain_audit_intake`, and the same lesson ("a board that
grades the backlog is not a loop that works it") one instrument over.

This is the intake, and it is deliberately a near-copy of
`routes/brain_audit_intake.py`: same bus (`/api/v1/heal/findings` →
`actionable_backend_issues`), same downstream triage (`brain_finding_router`
→ active / operator_config / mcp_server / terminal), same snapshot
discipline. A second board that grades itself is not what was missing; a
second FEEDER into the one loop is.

## Four rules that keep this from making things worse

0. ★★★ CANARY GATE — the rule with no equivalent in the audit intake, and the
   reason this module is not just a copy. Every QA run carries `canary_fired`:
   a must-fail control that proves the harness is capable of reporting a
   failure at all. When it is FALSE, the greens are unproven AND the reds are
   of unknown provenance — the run is an instrument reading, not evidence.
   We seed NOTHING from such a run. Seeding its reds anyway would be worse
   than seeding nothing: it launders an untrusted run into the brain's
   actionable backlog, where no consumer downstream carries the caveat. The
   board's own dashboard refuses to spend model budget on a run like this
   (`qa_superuser_dashboard`, the auto-investigate refusal); so does this.
   A STALE board is refused for the adjacent reason: it describes a platform
   that has since moved, so its reds may already be fixed or already worse.

1. RED-at-critical/major, or an INSTRUMENT FAULT. Nothing else.
   `BLIND` is honest ignorance — the probe could not look — and `GAUGE` makes
   no pass/fail claim by construction. Seeding either would ask the brain to
   explain a defect that has not been shown to exist, and would burn a
   ~10/cycle model budget on it. This is exactly the discrimination
   `auto_investigate_candidates` already makes before dispatching an
   investigation, so it is IMPORTED from there rather than re-stated here —
   two lanes reading one board must not drift on what counts as real work.
   If that import fails we seed nothing: not knowing the rule is a reason to
   stay quiet, not a reason to guess.

2. Capped and ROTATED. `QA_INTAKE_MAX` (default 4) rows per refresh, ordered
   customer-impact first. The cap exists because the brain's whole budget is
   ~10 findings/cycle and the audit intake may already claim up to 8 of it;
   an uncapped board would starve every other detector. The ROTATION exists
   because a fixed sort under a cap is head-of-list starvation — the r78
   class that flatlined Layer-5 proposals for twelve days, and which
   `brain_audit_intake` had to be retrofitted for after it shipped. Building
   it in from the start here.

3. Never on the hot path. `/api/v1/heal/findings` is public, hot and
   single-replica. The refresh runs on its own cadence (`QA_INTAKE_TTL_S`,
   default 1h — the board itself is rewritten every 4h) behind a `brain_state`
   snapshot, and the heal path only ever reads that cached snapshot.

Kill switch: `QA_INTAKE_DISABLE=1` → `qa_superuser_findings()` returns [].
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

brain_qa_superuser_intake_bp = Blueprint("brain_qa_superuser_intake", __name__)

_STATE_KEY = "qa_superuser_intake_snapshot"
_ISSUE_PREFIX = "qa_"

# Customer impact first. A confirmed wrong answer to a paying caller outranks
# our own probe being broken; the broken probe still outranks everything we
# are not seeding at all. `instrument_fault` is ranked BELOW major rather than
# by its own severity field because a fault's severity describes the check it
# belongs to, not the fault.
_RANK_CRITICAL = 0
_RANK_MAJOR = 1
_RANK_FAULT = 2

# Matches AUTO_INVESTIGATE_MAX_BOARD_AGE_H in qa_superuser_dashboard (imported
# when available; this is the fallback if that module cannot be loaded).
_DEFAULT_MAX_BOARD_AGE_H = 9.0


def _disabled() -> bool:
    return os.environ.get("QA_INTAKE_DISABLE", "0") == "1"


def _max_rows() -> int:
    try:
        return max(0, int(os.environ.get("QA_INTAKE_MAX", "4")))
    except Exception:
        return 4


def _ttl_s() -> int:
    try:
        return max(600, int(os.environ.get("QA_INTAKE_TTL_S", "3600")))
    except Exception:
        return 3600


def _max_board_age_h() -> float:
    env = os.environ.get("QA_INTAKE_MAX_BOARD_AGE_H")
    if env:
        try:
            return max(0.5, float(env))
        except Exception:
            pass
    try:
        from routes.qa_superuser_dashboard import (
            AUTO_INVESTIGATE_MAX_BOARD_AGE_H as _h)
        return float(_h)
    except Exception:
        return _DEFAULT_MAX_BOARD_AGE_H


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
                   VALUES (%s, %s::jsonb, NOW())
                   ON CONFLICT (state_key)
                   DO UPDATE SET state_value = EXCLUDED.state_value,
                                 updated_at = NOW()""",
                (key, json.dumps(value)))
            conn.commit()
        return True
    except Exception:
        return False


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    except Exception:
        return None


# ── rule 0: is this run trustworthy at all? ─────────────────────────────

def run_refusal(latest: dict | None, max_age_h: float | None = None
                ) -> str | None:
    """None if the run may be seeded from, else the REASON it may not be.

    Pure, so the gate is testable without a database. Order matters only for
    which reason gets reported; any one of them refuses the whole run.
    """
    if not latest:
        return "no QA super-user run has been recorded"
    if not latest.get("canary_fired"):
        # ★ The must-fail control did not fire: the harness was not shown
        #   capable of reporting a failure, so nothing it says is evidence.
        return ("must-fail control did not fire — every verdict on this run "
                "is unproven, so none of it may become brain work")
    age = _age_hours(latest.get("generated_at"))
    limit = _max_board_age_h() if max_age_h is None else max_age_h
    if age is None:
        # ★ CANNOT TELL is refused, not waved through. `generated_at` is
        #   NOT NULL in qa_superuser_runs, so an unreadable one means the row
        #   is not what we think it is — and without an age we cannot say the
        #   reds describe the CURRENT platform, which is the only reason to
        #   seed them. The auto-investigate lane treats an unreadable age as
        #   fresh; it is dispatching one reversible analysis, while this lane
        #   writes into a deduped backlog that persists, so it fails closed.
        return ("board age is unreadable (generated_at=%r) — cannot establish "
                "that its reds are current" % (latest.get("generated_at"),))
    if age > limit:
        return ("board is %.1fh old (limit %.1fh) — its reds describe a "
                "platform that has since moved" % (age, limit))
    return None


# ── rule 1 + 2: pure selection (the part worth testing) ─────────────────

def _eligible(f: dict) -> bool:
    """RED-at-critical/major or an instrument fault — imported, not restated.

    Fail-CLOSED: if the shared predicate cannot be imported we return False,
    so an import problem makes this lane quiet rather than making it guess.
    """
    try:
        from routes.qa_superuser_dashboard import is_actionable_finding
    except Exception:
        return False
    try:
        return bool(is_actionable_finding(f or {}))
    except Exception:
        return False


def _rank(f: dict) -> int:
    if f.get("verdict") == "RED":
        if f.get("severity") == "critical":
            return _RANK_CRITICAL
        if f.get("severity") == "major":
            return _RANK_MAJOR
    return _RANK_FAULT


def _cycle_no(now_s: float | None = None) -> int:
    """One tick per TTL window — the rotation clock."""
    return int((time.time() if now_s is None else now_s) // _ttl_s())


def select_seedable(findings: list, limit: int | None = None,
                    cycle: int | None = None) -> tuple[list, int]:
    """(rows to seed, how many eligible exist). Impact-ordered, ROTATED, capped.

    `findings` are board findings: {key, verdict, severity, surface, title,
    failing_since, instrument_fault, ...}. PASS / GAUGE / BLIND-without-fault
    are all excluded by `_eligible` — only an observed failure to a paying
    caller, or our own probe being broken, is real work.
    """
    limit = _max_rows() if limit is None else limit
    real = [f for f in (findings or []) if _eligible(f or {})]
    real.sort(key=lambda f: (_rank(f), str(f.get("key") or "")))
    total = len(real)
    if limit <= 0 or total <= limit:
        return real[:limit], total
    cyc = _cycle_no() if cycle is None else cycle
    start = (cyc * limit) % total
    window = (real + real)[start:start + limit]
    return window, total


def to_findings(rows: list, board_as_of: str | None = None) -> list:
    """Board findings → the {url, issue, count, detail} shape the heal
    endpoint's actionable_backend_issues list uses.

    The `issue` label is prefixed `qa_` so no FIX_MAP key matches it — the
    master-heal string-replacer must never try to body-substitute a QA finding
    (same reasoning as the `audit_`, `asset_` and `contract_` prefixes). The
    (issue, url) pair is STABLE across runs because it is built from the
    board's own `key`, which is what lets the Layer-5 learn loop dedupe a
    finding it has already triaged instead of re-proposing it every cycle.
    """
    out = []
    for f in rows or []:
        key = str((f or {}).get("key") or "").strip()
        if not key:
            continue
        fault = bool(f.get("instrument_fault"))
        kind = "fault" if fault else str(f.get("severity") or "?")
        since = f.get("failing_since")
        age = _age_hours(since)
        # failing_since is the real severity signal: three weeks red is a
        # different problem from red overnight, and the brain cannot see the
        # board's history, so it has to travel in the detail string.
        age_txt = (" failing for %.1fh" % age) if age is not None else ""
        out.append({
            "url": "dchub://qa-superuser/%s" % key,
            "issue": "%s%s %s" % (_ISSUE_PREFIX, kind,
                                  (f.get("title") or key)[:240]),
            "count": 1,
            # ★2026-09-02: the board key travels with the finding, and the
            # claim that will judge any "fix" is named here — the RED itself is
            # the instrument (claim_ledger `qa:` scheme). See mint_fix_claims.
            "finding_key": key,
            "fix_claim_metric": fix_claim_metric(key),
            "detail": (
                "QA super-user board finding %s (surface=%s, verdict=%s, "
                "severity=%s%s%s). This is an OUTSIDE-IN probe of "
                "dchub.cloud, so it reflects what a real caller sees. "
                "Board (canary-verified, generated %s): "
                "/admin/qa-superuser"
                % (key, f.get("surface"), f.get("verdict"),
                   f.get("severity"),
                   ", INSTRUMENT FAULT — our probe is broken, not the "
                   "platform" if fault else "",
                   age_txt, board_as_of or "unknown")),
        })
    return out


# ── fix claims: the RED is the instrument ───────────────────────────────
#
# ★ 2026-09-02. `media::item-links` was RED for 142h while two merged
#   [brain-spec] PRs (#3444 on 08-31, #3494 on 09-01) each counted as its fix:
#   the loop graded documents, and nothing ever re-read the key that was red.
#   Every RED the intake seeds now mints ONE open `fix` claim whose expected
#   metric is the board key itself (`qa:<key> verdict == PASS`, 168h horizon).
#   L16 judges it against the next canary-verified board: still RED at the
#   horizon -> `refuted`, which is evidence nobody has to argue with; PASS ->
#   `confirmed`. register_claim dedupes on the open (subject, statement,
#   metric), so re-seeding the same RED every hour cannot flood the ledger.

_FIX_HORIZON_H = 168


def fix_claim_metric(key: str) -> str:
    return "qa:%s verdict" % key


def fix_claim_for(f: dict, board_as_of: str | None = None) -> dict | None:
    """The claim one seeded board finding implies, or None when it implies
    none. Pure. Only an observed RED is a fix to claim — an instrument fault is
    OUR probe being broken, and "PASS" would just mean we fixed the probe."""
    f = f or {}
    key = str(f.get("key") or "").strip()
    if not key or f.get("instrument_fault") or f.get("verdict") != "RED":
        return None
    return {
        "kind": "fix",
        "subject": "qa:%s" % key,
        "statement": ("QA super-user RED %s (%s) reads PASS on a canary-verified "
                      "board within %dh of being seeded to the brain"
                      % (key, (f.get("title") or key)[:120], _FIX_HORIZON_H)),
        "expected_metric": fix_claim_metric(key),
        "expected_value": "== PASS",
        "horizon_hours": _FIX_HORIZON_H,
        "regime": {"severity": f.get("severity"),
                   "surface": f.get("surface"),
                   "failing_since": f.get("failing_since"),
                   "board_as_of": board_as_of,
                   "producer": "brain_qa_superuser_intake"},
        "surfaces": ["/admin/qa-superuser"],
        # The finding is on the bus the moment it is seeded — the clock starts
        # now; there is no separate artefact to wait for.
        "shipped": True,
    }


def mint_fix_claims(rows: list, board_as_of: str | None = None,
                    register_fn=None) -> dict:
    """Register the fix claim for every seeded RED. Fail-soft per row; a
    ledger error never blocks the seed. Returns counts, never raises."""
    out = {"minted": 0, "already": 0, "refused": 0, "errors": 0, "ids": []}
    try:
        if register_fn is None:
            from routes.claim_ledger import register_claim as register_fn
    except Exception:  # noqa: BLE001
        out["errors"] = len(rows or [])
        return out
    for f in rows or []:
        spec = fix_claim_for(f, board_as_of)
        if not spec:
            continue
        try:
            r = register_fn(**spec) or {}
        except Exception:  # noqa: BLE001
            out["errors"] += 1
            continue
        if r.get("refused"):
            out["refused"] += 1
        elif not r.get("ok"):
            out["errors"] += 1
        elif r.get("already"):
            out["already"] += 1
        else:
            out["minted"] += 1
            if r.get("id") is not None:
                out["ids"].append(r["id"])
    return out


# ── snapshot refresh (reads the board row, off the hot path) ────────────

def _load_latest() -> dict:
    """Latest recorded QA run. Fail-soft; never raises."""
    try:
        from routes.qa_superuser_dashboard import _load
        return (_load(limit=1) or {}).get("latest") or {}
    except Exception:
        return {}


def refresh_snapshot(force: bool = False, load_fn=None) -> dict:
    """Read the latest board run and persist the seedable slice. Fail-soft."""
    if _disabled():
        return {"ok": True, "skipped": "QA_INTAKE_DISABLE=1"}
    prev = _state_get(_STATE_KEY) or {}
    age = time.time() - float(prev.get("ts") or 0)
    if not force and prev and age < _ttl_s():
        return {"ok": True, "skipped": "fresh", "age_s": int(age),
                "rows": len(prev.get("rows") or [])}
    try:
        latest = (load_fn or _load_latest)() or {}
        refusal = run_refusal(latest)
        if refusal:
            # ★ Persist the REFUSAL, don't just return it. Without this the
            #   previous (trusted) snapshot would keep serving findings from a
            #   run we have since decided not to trust — the stalest possible
            #   failure, and invisible.
            snap = {"ts": time.time(),
                    "as_of": datetime.now(timezone.utc).isoformat(),
                    "board_as_of": latest.get("generated_at"),
                    "canary_fired": bool(latest.get("canary_fired")),
                    "refused": refusal, "eligible_total": 0,
                    "cycle": _cycle_no(), "rows": []}
            _state_set(_STATE_KEY, snap)
            return {"ok": True, "refreshed": True, "refused": refusal,
                    "rows": 0}
        rows, eligible = select_seedable(latest.get("findings") or [])
        claims = mint_fix_claims(rows, latest.get("generated_at"))
        snap = {"ts": time.time(),
                "as_of": datetime.now(timezone.utc).isoformat(),
                "board_as_of": latest.get("generated_at"),
                "canary_fired": True,
                "refused": None,
                "eligible_total": eligible,
                "cycle": _cycle_no(),
                "fix_claims": claims,
                "rows": rows}
        _state_set(_STATE_KEY, snap)
        # ★ NO SILENT CAPS: say what was left out. A bounded lane that reports
        # only what it took reads as full coverage to whoever finds it later.
        deferred = max(0, eligible - len(rows))
        return {"ok": True, "refreshed": True, "rows": len(rows),
                "eligible_total": eligible,
                "deferred_to_next_cycle": deferred,
                "fix_claims": claims,
                "board_as_of": latest.get("generated_at")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:160]}


def qa_superuser_findings() -> list:
    """The heal endpoint's read: cached snapshot only, never a live board read."""
    if _disabled():
        return []
    try:
        snap = _state_get(_STATE_KEY) or {}
        return to_findings(snap.get("rows") or [], snap.get("board_as_of"))
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


@brain_qa_superuser_intake_bp.get("/api/v1/brain/qa-superuser-intake")
def qa_intake_status():
    if not _admin_ok_local():
        return jsonify(ok=False, error="admin key required"), 401
    snap = _state_get(_STATE_KEY) or {}
    seeded = to_findings(snap.get("rows") or [], snap.get("board_as_of"))
    eligible = snap.get("eligible_total")
    out = {"ok": True, "enabled": not _disabled(),
           "max_rows": _max_rows(), "ttl_s": _ttl_s(),
           "max_board_age_h": _max_board_age_h(),
           "snapshot_as_of": snap.get("as_of"),
           "board_as_of": snap.get("board_as_of"),
           "canary_fired": snap.get("canary_fired"),
           # When this is set the lane is deliberately quiet, and saying so is
           # the whole point — a silent zero looks identical to a clean board.
           "refused": snap.get("refused"),
           "fix_claims": snap.get("fix_claims"),
           "eligible_total": eligible,
           "cycle": snap.get("cycle"),
           "deferred_to_next_cycle": (max(0, eligible - len(seeded))
                                      if isinstance(eligible, int) else None),
           "seeded": seeded}
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@brain_qa_superuser_intake_bp.post("/api/v1/brain/qa-superuser-intake/refresh")
def qa_intake_refresh():
    if not _admin_ok_local():
        return jsonify(ok=False, error="admin key required"), 401
    result = refresh_snapshot(force=request.args.get("force") == "1")
    resp = jsonify(result)
    resp.headers["Cache-Control"] = "no-store"
    return resp
