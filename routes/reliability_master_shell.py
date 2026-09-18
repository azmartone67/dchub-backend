"""
reliability_master_shell.py — the self-driving RELIABILITY-RECOVERY orchestrator
(2026-07-04).

Sibling to growth_master_shell / audience_master_shell, but pointed inward: its
single job is to get the autonomous brain's verified fix-success rate over — and
HELD over — 50% so Phase-3 narrow auto-merge (the route_alias_404 recipe behind
its canary + auto-revert) can be armed safely. It owns the reliability flywheel
and, every tick, scores FIVE levers, finds the weakest, and pulls exactly one
bounded action.

The five levers (owner-greenlit 2026-07-04, from the reliability-recovery watch):
  1. ACCOUNTING   — is the published rate an HONEST read of executed-fix landing,
                    and how much of the sub-50% is just chronic-benched patterns'
                    FALSE rows that haven't aged out of the 30d window yet?
                    (rate_live vs rate_excl_quarantined + the A/B/C arm gate.)
  2. THRASH       — is the detect→act loop burning its budget re-firing the same
                    finding (competitor_announcement:dcd ×51/day, rate_limited)?
                    Thrash inflates HTTP failures and starves real fixes.
  3. DEAD_ROUTE   — are structurally-dead detectors (mcp_health_*_unreachable,
                    operator_profile_gap, page_content_drift) manufacturing
                    un-fixable failures the recipes can never land?
  4. VERIFIER     — do the patterns that ACT actually have an effect-verifier?
                    A missing verifier makes a genuine win score as no-effect.
  5. CALIBRATION  — is L16 prediction-vs-outcome calibration actually running
                    (ANTHROPIC_API_KEY set, predictions logged), so the brain
                    learns which fixes are low-yield instead of repeating them?

Design mirrors the house master-shell pattern exactly: admin-gated, killable
(RELIABILITY_MASTER_DISABLED=1), loopback self-calls, a snapshot table, tier1..4,
0-100 score. SHADOW BY DEFAULT — it measures + scores + persists every tick but
takes NO action unless RELIABILITY_MASTER_ARM=1 (mirrors the reliability-first,
then-arm posture). Every ACTION is bounded (one/tick), individually killable
(RELIABILITY_LEVER_<NAME>_OFF=1), and fail-soft.

The snapshot table also becomes the SERVER-SIDE home of the exact A/B/C arm gate
the brain-reliability-recovery-watch scheduled task checks — consecutive-hold is
read from our own prior snapshot, so /reliability/state is a single source of
truth for "is auto-merge arm-ready yet".

Endpoints:
  POST /api/v1/admin/reliability/master-tick   — measure → score → act → verify
  GET  /api/v1/admin/reliability/state         — latest snapshot + arm gate + trend
"""
import os
import json
import time
import hmac
import urllib.request
import urllib.error
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

reliability_master_shell_bp = Blueprint("reliability_master_shell", __name__)

# Self-calls go LOOPBACK on Railway (mirrors growth_master_shell) — the shell
# runs INSIDE the backend, so hitting the public URL would round-trip out through
# Cloudflare and back. Loopback keeps each call sub-100ms and the tick well under
# CF's ~15s proxy limit.
_BACKEND_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else os.environ.get("DCHUB_BACKEND_BASE", "https://dchub-backend-production.up.railway.app")
)

# The arm gate (must ALL hold — see brain-reliability-recovery-watch task).
_ARM_RATE_FLOOR = 0.50
_ARM_SAMPLE_FLOOR = 20
_ARM_CONSECUTIVE_DAYS = 2


# ── auth (mirrors growth_master_shell) ────────────────────────────────
def _admin_key() -> str | None:
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _disabled() -> bool:
    return str(os.environ.get("RELIABILITY_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


def _act_enabled() -> bool:
    """SHADOW by default: measure + score + persist but take NO action unless the
    operator explicitly arms it. This is the 'reliability-first, then arm' posture
    — the shell has to prove the rate recovers in shadow before it acts."""
    return str(os.environ.get("RELIABILITY_MASTER_ARM", "")).lower() in ("1", "true", "yes")


def _lever_off(name: str) -> bool:
    """Per-lever kill switch, e.g. RELIABILITY_LEVER_DEAD_ROUTE_OFF=1."""
    return str(os.environ.get(f"RELIABILITY_LEVER_{name.upper()}_OFF", "")).lower() in ("1", "true", "yes")


# ── self-call + probe helpers (mirror growth_master_shell) ────────────
def _req(path: str, method: str = "GET", timeout: int = 10) -> dict:
    url = (path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path)
    started = time.time()
    try:
        req = urllib.request.Request(url, data=(b"" if method == "POST" else None), method=method)
        req.add_header("X-DC-Probe", "reliability-tick")  # rate-limiter bypass
        req.add_header("User-Agent", "dchub-reliability-orchestrator/1.0")
        ak = _admin_key()
        if ak:
            req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {"_raw": body[:300]}
            return {"ok": True, "http": resp.status,
                    "ms": int((time.time() - started) * 1000), "data": parsed}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "http": None, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def _fire(path: str, timeout: int = 4) -> dict:
    """Trigger a downstream ACTION without blocking on full completion — a short
    read timeout is treated as 'dispatched' so the tick stays under CF's limit."""
    url = (path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path)
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        req.add_header("X-DC-Probe", "reliability-tick")
        req.add_header("User-Agent", "dchub-reliability-orchestrator/1.0")
        ak = _admin_key()
        if ak:
            req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(64)
            return {"dispatched": True, "http": resp.status}
    except (urllib.error.URLError, TimeoutError) as e:
        # A read timeout means the server accepted it and is running it — treat as dispatched.
        if isinstance(e, urllib.error.HTTPError):
            return {"dispatched": False, "http": e.code, "error": f"HTTP {e.code}"}
        return {"dispatched": True, "note": f"async ({type(e).__name__})"}
    except Exception as e:
        return {"dispatched": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def _probe_http(url: str, timeout: int = 6) -> int | None:
    """Return the HTTP status for a bare GET (dead-route classification)."""
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "dchub-reliability-orchestrator/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def _num(v):
    try:
        if v is None:
            return None
        return float(v) if isinstance(v, str) and "." in v else int(v)
    except (TypeError, ValueError):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


# ── db (mirror growth_master_shell) ───────────────────────────────────
def _conn():
    try:
        from routes.ai_reach import _conn as _raw
        return _raw()
    except Exception:
        return None


def _ensure_tables() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reliability_snapshots (
                    id                 SERIAL PRIMARY KEY,
                    computed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    reliability_score  NUMERIC(6,2),
                    rate_live          NUMERIC(6,3),
                    rate_clean         NUMERIC(6,3),
                    verified_sample    INTEGER,
                    sample_clean       INTEGER,
                    arm_ready          BOOLEAN,
                    consecutive_ge50   INTEGER,
                    weakest_lever      TEXT,
                    action_taken       TEXT,
                    lever_scores       JSONB,
                    detail             JSONB
                )
            """)
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


def _fetchone(cur, sql, default=None):
    try:
        cur.execute(sql)
        row = cur.fetchone()
        return row if row is not None else default
    except Exception:
        return default


# ── TIER 1 — MEASURE (the honest reliability read, straight from SQL) ──
def tier1_measure() -> dict:
    """Read the ground truth directly rather than trusting endpoint shapes —
    growth_master_shell got repeatedly burned by drifting response schemas, so
    the reliability shell computes its own numbers from the same tables the
    self-model reads (autopilot_outcomes / brain_autopilot_actions / quarantine).
    """
    m = {
        "rate_live": None, "rate_clean": None,
        "verified_sample": None, "sample_clean": None,
        "verified_ok": None, "verified_fail": None,
        "actions_24h": None, "rate_limited_24h": None,
        "executed_ok_30d": None, "verifier_rows_30d": None,
        "quarantined_active": None,
        "predictions_30d": None, "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                # 1a. LIVE rate — exactly what the self-model publishes.
                row = _fetchone(cur, """
                    SELECT
                      COUNT(*) FILTER (WHERE succeeded IS TRUE
                                        AND verified_at >= NOW() - INTERVAL '30 days'),
                      COUNT(*) FILTER (WHERE succeeded IS FALSE
                                        AND verified_at >= NOW() - INTERVAL '30 days')
                      FROM autopilot_outcomes
                """, default=(0, 0))
                v_ok, v_fail = int(row[0] or 0), int(row[1] or 0)
                sample = v_ok + v_fail
                m["verified_ok"], m["verified_fail"] = v_ok, v_fail
                m["verified_sample"] = sample
                m["rate_live"] = round(v_ok / sample, 3) if sample >= 5 else None

                # 1b. CLEAN rate — the leading indicator: exclude outcomes whose
                # pattern is currently effect-benched. Their FALSE rows are the
                # chronic failures that only age out with time; excluding them
                # shows where the rate is HEADED once the 30d window clears. This
                # is a MEASUREMENT, not a change to the published number.
                row = _fetchone(cur, """
                    SELECT
                      COUNT(*) FILTER (WHERE o.succeeded IS TRUE
                                        AND o.verified_at >= NOW() - INTERVAL '30 days'),
                      COUNT(*) FILTER (WHERE o.succeeded IS FALSE
                                        AND o.verified_at >= NOW() - INTERVAL '30 days')
                      FROM autopilot_outcomes o
                     WHERE o.pattern_name NOT IN (
                            SELECT pattern_name FROM brain_pattern_quarantine
                             WHERE released_at IS NULL)
                """, default=(v_ok, v_fail))
                cok, cfail = int(row[0] or 0), int(row[1] or 0)
                csample = cok + cfail
                m["sample_clean"] = csample
                m["rate_clean"] = round(cok / csample, 3) if csample >= 5 else m["rate_live"]

                # 2. THRASH — 24h action mix from brain_autopilot_actions.
                row = _fetchone(cur, """
                    SELECT
                      COUNT(*),
                      COUNT(*) FILTER (WHERE outcome = 'rate_limited')
                      FROM brain_autopilot_actions
                     WHERE started_at >= NOW() - INTERVAL '24 hours'
                """, default=(0, 0))
                m["actions_24h"], m["rate_limited_24h"] = int(row[0] or 0), int(row[1] or 0)
                # top storming finding (most rate_limited rows in 24h)
                try:
                    cur.execute("""
                        SELECT pattern_name, COUNT(*) AS n
                          FROM brain_autopilot_actions
                         WHERE started_at >= NOW() - INTERVAL '24 hours'
                           AND outcome = 'rate_limited'
                         GROUP BY pattern_name ORDER BY n DESC LIMIT 3
                    """)
                    m["top_thrash"] = [{"pattern": r[0], "n": int(r[1] or 0)} for r in cur.fetchall()]
                except Exception:
                    m["top_thrash"] = []

                # 4. VERIFIER coverage — executed_ok actions vs verifier rows (30d).
                row = _fetchone(cur, """
                    SELECT COUNT(*) FROM brain_autopilot_actions
                     WHERE outcome = 'executed_ok'
                       AND started_at >= NOW() - INTERVAL '30 days'
                """, default=(0,))
                m["executed_ok_30d"] = int(row[0] or 0)
                row = _fetchone(cur, """
                    SELECT COUNT(*) FROM autopilot_outcomes
                     WHERE verified_at >= NOW() - INTERVAL '30 days'
                """, default=(0,))
                m["verifier_rows_30d"] = int(row[0] or 0)

                # quarantine depth
                row = _fetchone(cur, """
                    SELECT COUNT(*) FROM brain_pattern_quarantine WHERE released_at IS NULL
                """, default=(0,))
                m["quarantined_active"] = int(row[0] or 0)

                # 5. CALIBRATION — predictions logged in the window.
                row = _fetchone(cur, """
                    SELECT COUNT(*) FROM brain_predictions_log
                     WHERE predicted_at >= NOW() - INTERVAL '30 days'
                """, default=(None,))
                m["predictions_30d"] = None if row[0] is None else int(row[0])
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass

    # 3. DEAD-ROUTE — pull the unreachable-style open findings and probe a bounded
    # sample. Uses the public self-model / autopilot status for the finding list.
    dead = _measure_dead_routes()
    m.update(dead)

    m["honest_read"] = (
        f"live rate {m['rate_live']} (sample {m['verified_sample']}) · "
        f"clean rate {m['rate_clean']} (sample {m['sample_clean']}) · "
        f"thrash {m['rate_limited_24h']}/{m['actions_24h']} rate-limited/24h · "
        f"{m['quarantined_active']} benched · "
        f"{m['dead_confirmed']}/{m['dead_probed']} probed routes dead · "
        f"L16 {'live' if m['anthropic_key_set'] and (m['predictions_30d'] or 0) else 'dark'}"
    )
    return m


def _measure_dead_routes() -> dict:
    """Probe a bounded sample of the unreachable-style runaway findings."""
    out = {"dead_probed": 0, "dead_confirmed": 0, "dead_samples": []}
    status = (_req("/api/v1/brain/autopilot/status").get("data") or {})
    benched = status.get("quarantined_now") or []
    # extract any http(s) URLs embedded in the unreachable-style patterns
    urls = []
    for p in benched:
        low = str(p).lower()
        if ("unreachable" in low or "page_content_drift" in low or "operator_profile_gap" in low):
            frag = str(p).split("|", 1)[-1].strip()
            if frag.startswith("http"):
                urls.append(frag)
            elif frag.startswith("/"):
                urls.append("https://dchub-backend-production.up.railway.app" + frag)
    seen = []
    for u in urls[:6]:  # bounded: never more than 6 probes/tick
        code = _probe_http(u)
        dead = code in (404, 410, None) or (code is not None and code >= 500)
        out["dead_probed"] += 1
        if dead:
            out["dead_confirmed"] += 1
        seen.append({"url": u[:120], "http": code, "dead": dead})
    out["dead_samples"] = seen
    return out


# ── TIER 2 — SCORE THE FIVE LEVERS (0..1 each; higher = healthier) ────
def tier2_score_levers(m: dict) -> dict:
    # 1. ACCOUNTING — how close is the LIVE rate to the arm floor, and does the
    # clean rate say recovery is coming? Health rewards being at/over 0.50.
    rl = m.get("rate_live")
    rc = m.get("rate_clean")
    live_health = 0.0 if rl is None else max(0.0, min(1.0, rl / _ARM_RATE_FLOOR))
    clean_health = 0.0 if rc is None else max(0.0, min(1.0, rc / _ARM_RATE_FLOOR))
    accounting = round(0.7 * live_health + 0.3 * clean_health, 3)

    # 2. THRASH — fraction of the 24h action budget NOT wasted on rate_limited.
    a = m.get("actions_24h") or 0
    rlm = m.get("rate_limited_24h") or 0
    thrash = round(1.0 - (rlm / a), 3) if a else 1.0

    # 3. DEAD_ROUTE — fraction of probed routes that are alive.
    dp = m.get("dead_probed") or 0
    dc = m.get("dead_confirmed") or 0
    dead_route = round(1.0 - (dc / dp), 3) if dp else 1.0

    # 4. VERIFIER — do acting patterns have an effect-verifier? coverage ratio.
    ex = m.get("executed_ok_30d") or 0
    vr = m.get("verifier_rows_30d") or 0
    verifier = round(min(1.0, vr / ex), 3) if ex else 1.0

    # 5. CALIBRATION — is L16 actually running (key set AND predictions logged)?
    key = bool(m.get("anthropic_key_set"))
    preds = m.get("predictions_30d") or 0
    calibration = round((0.5 if key else 0.0) + (0.5 if preds else 0.0), 3)

    scores = {
        "accounting": accounting,
        "thrash": thrash,
        "dead_route": dead_route,
        "verifier": verifier,
        "calibration": calibration,
    }
    ranked = sorted(scores.items(), key=lambda kv: kv[1])
    weakest = next((name for name, _ in ranked if not _lever_off(name)), ranked[0][0])
    return {"scores": scores, "weakest": weakest}


# ── ARM GATE (the exact A/B/C the reliability-recovery-watch task checks) ──
def _arm_gate(m: dict, prev: dict | None) -> dict:
    rl = m.get("rate_live")
    sample = m.get("verified_sample") or 0
    a_rate = (rl is not None) and (rl >= _ARM_RATE_FLOOR)
    b_sample = sample >= _ARM_SAMPLE_FLOOR
    # C — consecutive-hold measured in CALENDAR DAYS, not ticks: the orchestrator
    # drives this shell every ~2h, so a raw per-snapshot counter would hit "2
    # consecutive" in 4 hours and false-green the gate. Count distinct days with
    # rate>=0.50, keyed off OUR prior snapshot (server-side state).
    prev_consec = int((prev or {}).get("consecutive_ge50") or 0)
    prev_ge50 = bool(prev and prev.get("rate_live") is not None
                     and float(prev["rate_live"]) >= _ARM_RATE_FLOOR)
    # day gap between prev snapshot and now (0 = same day, 1 = yesterday, >1 = gap)
    day_gap = None
    pv_at = (prev or {}).get("computed_at")
    if pv_at is not None:
        try:
            if isinstance(pv_at, str):
                pv_at = datetime.fromisoformat(pv_at.replace("Z", "+00:00"))
            day_gap = (datetime.now(timezone.utc).date() - pv_at.date()).days
        except Exception:
            day_gap = None
    if not a_rate:
        consecutive = 0
    elif prev is None or day_gap is None:
        consecutive = 1                       # first snapshot with a rate
    elif day_gap == 0:
        consecutive = max(prev_consec, 1)     # same day → already counted, hold
    elif day_gap == 1 and prev_ge50:
        consecutive = prev_consec + 1         # a fresh consecutive day held >=50%
    else:
        consecutive = 1                       # streak broken (gap, or prev day <50%)
    c_hold = a_rate and consecutive >= _ARM_CONSECUTIVE_DAYS
    arm_ready = bool(a_rate and b_sample and c_hold)
    if arm_ready:
        reason = "arm-ready"
    elif not a_rate:
        reason = f"rate {rl} < {_ARM_RATE_FLOOR}"
    elif not b_sample:
        reason = f"sample {sample} < {_ARM_SAMPLE_FLOOR}"
    else:
        reason = "need a 2nd consecutive day >=50%"
    return {"arm_ready": arm_ready, "A_rate": a_rate, "B_sample": b_sample,
            "C_consecutive_hold": c_hold, "consecutive_ge50": consecutive,
            "reason": reason}


# ── TIER 3 — ACT (one bounded action on the weakest lever) ────────────
def tier3_act(m: dict, levers: dict) -> dict:
    if not _act_enabled():
        return {"action": "none", "reason": "SHADOW (set RELIABILITY_MASTER_ARM=1 to act)"}
    lever = levers.get("weakest")
    if _lever_off(lever):
        return {"action": "none", "reason": f"lever '{lever}' killed"}

    if lever == "calibration":
        # Wake L14/L16 so predictions get logged + calibrated (needs ANTHROPIC key).
        r = _fire("/api/v1/brain/self-critique/calibration", timeout=6)
        return {"action": "calibration_tick", "lever": lever, "dispatched": r.get("dispatched"),
                "flag": None if m.get("anthropic_key_set")
                        else "ANTHROPIC_API_KEY unset — L16 cannot calibrate until it's set."}

    if lever == "dead_route":
        # Bounded: flag the confirmed-dead routes for detector retirement. The
        # runaway auto-quarantine already benches them; retiring the DETECTOR is
        # a code change, so we surface it rather than mutate autonomously.
        dead = [s["url"] for s in (m.get("dead_samples") or []) if s.get("dead")]
        return {"action": "flag", "lever": lever,
                "flag": f"{len(dead)} confirmed-dead detector route(s) manufacturing failures — retire the detector: {dead[:3]}"}

    if lever == "thrash":
        top = m.get("top_thrash") or []
        return {"action": "flag", "lever": lever,
                "flag": f"finding-level cooldown needed — top storm: {top[:2]} (rate_limited re-fires waste the action budget)."}

    if lever == "verifier":
        return {"action": "flag", "lever": lever,
                "flag": (f"verifier coverage {m.get('verifier_rows_30d')}/{m.get('executed_ok_30d')} executed — "
                         "add per-recipe effect probes so genuine wins aren't scored as no-effect.")}

    if lever == "accounting":
        return {"action": "flag", "lever": lever,
                "flag": (f"live {m.get('rate_live')} vs clean {m.get('rate_clean')} — "
                         "gap is chronic-benched FALSE rows aging out; hold course, don't bench to 50%.")}

    return {"action": "none", "reason": f"unknown lever {lever}"}


# ── SCORE, PERSIST, VERIFY ────────────────────────────────────────────
def reliability_score(levers: dict, gate: dict) -> float:
    s = levers.get("scores") or {}
    lever_avg = (sum(s.values()) / len(s)) if s else 0.0
    # arm-readiness half: rate progress, sample floor, consecutive-hold.
    parts = [
        1.0 if gate.get("A_rate") else 0.0,
        1.0 if gate.get("B_sample") else 0.0,
        min(1.0, (gate.get("consecutive_ge50") or 0) / _ARM_CONSECUTIVE_DAYS),
    ]
    arm_readiness = sum(parts) / len(parts)
    return round(100.0 * (0.5 * lever_avg + 0.5 * arm_readiness), 2)


def _prev_snapshot() -> dict | None:
    c = _conn()
    if c is None:
        return None
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM reliability_snapshots ORDER BY id DESC LIMIT 1")
            return cur.fetchone()
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def _persist(m: dict, levers: dict, gate: dict, score: float, action: dict) -> bool:
    if not _ensure_tables():
        return False
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO reliability_snapshots
                  (reliability_score, rate_live, rate_clean, verified_sample, sample_clean,
                   arm_ready, consecutive_ge50, weakest_lever, action_taken, lever_scores, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (
                score, m.get("rate_live"), m.get("rate_clean"),
                m.get("verified_sample"), m.get("sample_clean"),
                gate.get("arm_ready"), gate.get("consecutive_ge50"),
                levers.get("weakest"), (action or {}).get("action"),
                json.dumps(levers.get("scores") or {}),
                json.dumps({"measure": m, "levers": levers, "gate": gate, "action": action}),
            ))
        return True
    except Exception:
        note_swallowed_write("reliability_snapshots", where="reliability_master_shell._persist")
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── ORCHESTRATOR ──────────────────────────────────────────────────────
@reliability_master_shell_bp.route("/api/v1/admin/reliability/master-tick", methods=["POST", "GET"])
def master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="RELIABILITY_MASTER_DISABLED"), 200
    started = time.time()
    prev = _prev_snapshot()
    measure = tier1_measure()
    levers = tier2_score_levers(measure)
    gate = _arm_gate(measure, prev)
    action = tier3_act(measure, levers)
    score = reliability_score(levers, gate)
    persisted = _persist(measure, levers, gate, score, action)

    s = levers.get("scores") or {}
    headline = (
        f"reliability {score}/100 · live rate {measure.get('rate_live')} "
        f"(sample {measure.get('verified_sample')}) → clean {measure.get('rate_clean')} · "
        f"arm {'READY ✅' if gate.get('arm_ready') else 'not ready (' + gate.get('reason', '') + ')'} · "
        f"weakest → {levers.get('weakest')} ({s.get(levers.get('weakest'))}) · acted: {action.get('action')}"
    )
    return jsonify(
        ok=True,
        ms=int((time.time() - started) * 1000),
        reliability_score=score,
        headline=headline,
        arm_gate=gate,
        tier1_measure=measure,
        tier2_levers=levers,
        tier3_action=action,
        persisted=persisted,
        mode=("armed" if _act_enabled() else "shadow"),
        generated_at=datetime.now(timezone.utc).isoformat(),
    ), 200


@reliability_master_shell_bp.route("/api/v1/admin/reliability/state", methods=["GET"])
def state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(error="db_unavailable"), 503
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM reliability_snapshots ORDER BY id DESC LIMIT 20")
            rows = cur.fetchall()
        latest = rows[0] if rows else None
        return jsonify(
            ok=True,
            latest=latest,
            arm_ready=(latest or {}).get("arm_ready") if latest else None,
            history=rows,
            mode=("armed" if _act_enabled() else "shadow"),
        ), 200
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {str(e)[:160]}"), 500
    finally:
        try: c.close()
        except Exception: pass
