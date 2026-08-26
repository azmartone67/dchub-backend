"""
growth_master_shell.py — the self-driving GROWTH orchestrator (2026-07-03).

One level above audience_master_shell / agent_usefulness_master_shell: those
own a single domain; this one owns the whole growth flywheel and, every tick,
scores FIVE levers, finds the weakest, and pulls exactly one bounded action.

The five levers (owner-greenlit 2026-07-03):
  1. DISCOVERY   — do the machine-readable surfaces advertise self-serve
                   onboarding (/onboard, /platforms/register), so an agent can
                   find its way in without a human?
  2. MEASUREMENT — is the honest funnel legible (active_platforms sane, cron
                   health truthful)? You can't grow what you can't see.
  3. AUTONOMY    — is the brain actually ACTING (self-director armed + ticking),
                   not just proposing into the void?
  4. MEDIA       — is the analyst voice publishing on cadence (recent posts +
                   citation velocity), not starved by novelty-suppression?
  5. DISTRIBUTION— are the broad-query GEO surfaces + MCP registries covered,
                   so new agents discover DC Hub at the moment of intent?

Design mirrors the house master-shell pattern exactly: admin-gated, killable
(GROWTH_MASTER_DISABLED=1), loopback self-calls, a snapshot table, tier1..4,
0-100 score. Every ACTION is bounded (one/tick), individually killable, and
fail-soft. Wired into the cron dispatcher so it self-drives.

Endpoints:
  POST /api/v1/admin/growth/master-tick   — measure → score → act → verify
  GET  /api/v1/admin/growth/state         — latest snapshot + trend
"""
import os
import json
import time
import hmac
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

growth_master_shell_bp = Blueprint("growth_master_shell", __name__)

# Self-calls go LOOPBACK on Railway (mirrors cron_heartbeat.BASE) — the shell
# runs INSIDE the backend, so hitting the public URL would round-trip out
# through Cloudflare and back (~2s/call × ~10 calls = a 17s tick that 503s past
# CF's ~15s proxy limit and pins a worker). Loopback keeps each call sub-100ms.
_BACKEND_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else os.environ.get("DCHUB_BACKEND_BASE", "https://dchub-backend-production.up.railway.app")
)
_GEO_BASE = "https://dchub.cloud"


# ── auth (mirrors audience_master_shell) ──────────────────────────────
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
    return str(os.environ.get("GROWTH_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


def _act_disabled() -> bool:
    """Shadow mode: measure + score but take NO action (still persists)."""
    return str(os.environ.get("GROWTH_MASTER_ACT_DISABLED", "")).lower() in ("1", "true", "yes")


def _lever_off(name: str) -> bool:
    """Per-lever kill switch, e.g. GROWTH_LEVER_AUTONOMY_OFF=1."""
    return str(os.environ.get(f"GROWTH_LEVER_{name.upper()}_OFF", "")).lower() in ("1", "true", "yes")


# ── self-call helpers ─────────────────────────────────────────────────
def _req(path: str, method: str = "GET", timeout: int = 10) -> dict:
    url = (path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path)
    started = time.time()
    try:
        req = urllib.request.Request(url, data=(b"" if method == "POST" else None), method=method)
        req.add_header("X-DC-Probe", "growth-tick")  # rate-limiter bypass
        req.add_header("User-Agent", "dchub-growth-orchestrator/1.0")
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
    """Trigger a downstream ACTION without blocking on its full completion — the
    endpoint runs to completion server-side regardless. A short read timeout is
    treated as 'dispatched' so the tick stays well under CF's ~15s proxy limit
    (some actions, e.g. a media-showcase LLM call, take longer than that)."""
    url = (path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path)
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        req.add_header("X-DC-Probe", "growth-tick")
        req.add_header("User-Agent", "dchub-growth-orchestrator/1.0")
        ak = _admin_key()
        if ak:
            req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": resp.status < 400, "http": resp.status, "dispatched": True}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "dispatched": True}
    except Exception:
        # timeout/other — the request WAS sent; the action runs server-side.
        return {"ok": True, "dispatched": True, "note": "not awaited"}


def _fetch_text(url: str, timeout: int = 12) -> tuple[int, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "dchub-growth-orchestrator/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


# ── DB ────────────────────────────────────────────────────────────────
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
                CREATE TABLE IF NOT EXISTS growth_snapshots (
                    id                SERIAL PRIMARY KEY,
                    computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    growth_score      NUMERIC(6,2),
                    real_agents_7d    INTEGER,
                    real_agents_wow   NUMERIC(7,3),
                    conversions_30d   INTEGER,
                    weakest_lever     TEXT,
                    action_taken      TEXT,
                    lever_scores      JSONB,
                    detail            JSONB
                )
            """)
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── TIER 1 — MEASURE (the honest funnel) ──────────────────────────────
def tier1_measure() -> dict:
    funnel = (_req("/api/v1/mcp/funnel").get("data") or {})
    reach = (_req("/api/v1/ai/reach").get("data") or {})
    trend = (_req("/api/v1/ai/reach/trend").get("data") or {})
    analytics = (_req("/api/ai-analytics").get("data") or {})
    cron = (_req("/api/v1/cron/last-fired").get("data") or {})
    pub = (_req("/api/v1/dchub-media/publisher-status").get("data") or {})
    north = (_req("/api/v1/media/north-star").get("data") or {})

    # ★★2026-08-26: was `unique_ips_7d_real OR distinct_agents_7d` — the SAME
    # pre-fix expression audience_master_shell.py:210 replaced on 2026-07-31,
    # left behind here. unique_ips_7d_real is COUNT(DISTINCT raw ip_address)
    # over mcp_tool_calls: XFF chains count once per form, and it applies the
    # live predicate only, without the identity view's internal-IP and
    # scripted-UA backstops. Its own basis string in the funnel response says
    # "never render it as 'agents'". Measured live 2026-08-26T21:28Z it
    # published 33 while the canonical identity count read 16 — 2.1x — so
    # growth_score and weakest_lever were both computed on an inflated number.
    # Order: canonical → ISO-week rollup → loose, each step labelled, so a cold
    # funnel still yields a number and the row records which basis produced it.
    real_agents_loose = _num(funnel.get("unique_ips_7d_real"))
    real_agents = (_num(funnel.get("real_external_agents_7d"))
                   or _num(reach.get("real_agents_7d"))
                   or _num(reach.get("distinct_agents_7d"))
                   or real_agents_loose)
    real_calls = _num(funnel.get("tool_calls_7d_real"))
    probe_calls = _num(funnel.get("tool_calls_7d_probes"))
    conversions_30d = _num(funnel.get("conversions_30d")) or _num(funnel.get("mcp_conversions_30d"))
    # WoW on real agents from the trend endpoint's current-vs-prior weeks.
    cur = trend.get("current") or {}
    prv = trend.get("previous") or {}
    a_now = _num(cur.get("distinct_agents")) or real_agents
    a_prev = _num(prv.get("distinct_agents"))
    wow = None
    if a_now is not None and a_prev:
        try: wow = round((a_now - a_prev) / a_prev, 3)
        except Exception: wow = None

    # media cadence signal
    loops = (pub.get("loops") or {})
    posts_24h = sum(_num(v.get("successes_24h")) or 0 for v in loops.values()) if isinstance(loops, dict) else 0
    attempts_24h = sum(_num(v.get("attempts_24h")) or 0 for v in loops.values()) if isinstance(loops, dict) else 0
    # qa-0704: the publisher-status loop counters are in-process and reset to
    # zero on every deploy (3 deploys on 07-04 → all loops 0/last=None while
    # linkedin_posts held 9 posts in 72h). All-zero loops → fall back to the
    # DB truth so the media lever doesn't false-alarm and burn the tick's one
    # action on a healthy lever.
    if not posts_24h and not attempts_24h:
        try:
            _mc = _conn()
            if _mc is not None:
                with _mc.cursor() as _cur:
                    _cur.execute("SELECT COUNT(*) FROM linkedin_posts "
                                 "WHERE created_at > NOW() - INTERVAL '24 hours'")
                    _db_posts = int((_cur.fetchone() or [0])[0] or 0)
                try: _mc.close()
                except Exception: pass
                if _db_posts:
                    posts_24h = _db_posts
                    attempts_24h = _db_posts
        except Exception:
            pass
    # qa-0704: the live north-star endpoint's actual key is citation_velocity_7d
    # — none of the three keys below exist in its response (same shape-mismatch
    # family as the autonomy probe). Kept for forward/backward compat.
    citation_velocity = (_num(north.get("citation_velocity_7d"))
                         or _num(north.get("distinct_agents_citing_7d"))
                         or _num(north.get("citation_velocity"))
                         or _num(north.get("citations_7d")))

    return {
        "real_agents_7d": real_agents,
        # The old loose counter, carried so the pre-2026-08-26 series stays
        # interpretable: every growth_snapshots row before that date holds a
        # LOOSE number ~2x the canonical one. Compare like with like.
        "real_agents_7d_loose": real_agents_loose,
        "real_agents_basis": (
            "canonical (funnel.real_external_agents_7d)"
            if _num(funnel.get("real_external_agents_7d"))
            else "canonical (reach.real_agents_7d)"
            if _num(reach.get("real_agents_7d"))
            else "reach rollup (ISO week — runs low mid-week)"
            if _num(reach.get("distinct_agents_7d"))
            else "LOOSE unique_ips_7d_real — canonical unavailable"
        ),
        "real_agents_wow": wow,
        "real_calls_7d": real_calls,
        "probe_calls_7d": probe_calls,
        "conversions_30d": conversions_30d,
        "active_platforms": _num(analytics.get("active_platforms")),
        "cron_healthy": bool(cron.get("healthy")),
        "cron_fires_today": _num(cron.get("fires_today")),
        "media_posts_24h": posts_24h,
        "media_attempts_24h": attempts_24h,
        "citation_velocity_7d": citation_velocity,
        "honest_read": (
            f"{real_agents} real agents/wk (WoW {('%+d%%' % int((wow or 0)*100)) if wow is not None else 'n/a'}); "
            f"{conversions_30d} conversions/30d; {posts_24h} posts/24h; "
            f"cron {'healthy' if cron.get('healthy') else 'STALE'}."
        ),
    }


# ── TIER 2 — SCORE THE FIVE LEVERS (0..1 each; weakest = next action) ──
def tier2_score_levers(m: dict) -> dict:
    # 1. DISCOVERY — do the surfaces advertise self-serve onboarding?
    code_llms, llms = _fetch_text(_GEO_BASE + "/llms.txt")
    hay = (llms or "").lower()
    disc_bits = [
        1.0 if code_llms == 200 else 0.0,
        1.0 if ("/onboard" in hay or "platforms/register" in hay) else 0.0,
        1.0 if "claim_free_key" in hay else 0.0,
    ]
    discovery = round(sum(disc_bits) / len(disc_bits), 3)

    # 2. MEASUREMENT — active_platforms sane (not 0-bug, not 171-junk) + cron truthful.
    ap = m.get("active_platforms")
    ap_sane = 1.0 if (ap is not None and 1 <= ap <= 30) else 0.0
    measurement = round(0.6 * ap_sane + 0.4 * (1.0 if m.get("cron_healthy") else 0.0), 3)

    # 3. AUTONOMY — is the brain self-director armed AND recently active?
    # qa-0704: /api/v1/brain/self-direct/status never existed (404) and the
    # /api/v1/brain/agenda fallback returns {"agenda": [...]} — neither carries
    # 'enabled'/'agenda_count', so this lever scored 0.0 forever while
    # brain_self_agenda held 27 rows in 7d and every tick burned its one
    # action re-nudging a healthy brain. Parse the REAL agenda shape, then
    # fall back to the env arm flag (this shell runs in the same process).
    sd = (_req("/api/v1/brain/self-direct/status").get("data") or {})
    enabled = bool(sd.get("enabled")) if (isinstance(sd, dict) and "enabled" in sd) else None
    recent = _num((sd or {}).get("agenda_count")) or _num((sd or {}).get("proposals_7d")) or 0
    if not recent:
        ag = (_req("/api/v1/brain/agenda").get("data") or {})
        items = ag.get("agenda") if isinstance(ag, dict) else None
        if isinstance(items, list) and items:
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            n7 = 0
            for it in items:
                try:
                    ts = str((it or {}).get("created_at") or "")
                    if ts and datetime.fromisoformat(ts.replace("Z", "+00:00")) >= cutoff:
                        n7 += 1
                except Exception:
                    pass
            recent = n7
            if enabled is None:
                enabled = True  # a populated self-chosen agenda == armed
    if enabled is None:
        enabled = str(os.environ.get("BRAIN_SELF_DIRECT_ENABLED", "")).strip().lower() in ("1", "true", "yes")
    autonomy = round((0.6 if enabled else 0.0) + (0.4 if recent else 0.0), 3)

    # 4. MEDIA — publishing on cadence? posts in 24h OR citation velocity > 0.
    posts = m.get("media_posts_24h") or 0
    cv = m.get("citation_velocity_7d") or 0
    media = round(min(1.0, 0.7 * min(posts, 2) / 2.0 + 0.3 * (1.0 if cv else 0.0)), 3)

    # 5. DISTRIBUTION — broad-query GEO coverage + registry presence.
    covered = 0
    broad = ["grid intelligence", "interconnection", "fiber", "m&a", "site selection",
             "energy prices", "water risk", "capacity data"]
    covered = sum(1 for kw in broad if kw in hay)
    geo_cov = covered / len(broad) if broad else 0.0
    reg = (_req("/api/v1/mcp/standing").get("data") or {})
    reg_platforms = _num((reg.get("platforms") or {}).get("active_count")) or 0
    distribution = round(0.6 * geo_cov + 0.4 * min(1.0, reg_platforms / 10.0), 3)

    scores = {
        "discovery": discovery,
        "measurement": measurement,
        "autonomy": autonomy,
        "media": media,
        "distribution": distribution,
    }
    # weakest lever that isn't individually killed
    ranked = sorted(scores.items(), key=lambda kv: kv[1])
    weakest = next((name for name, _ in ranked if not _lever_off(name)), ranked[0][0])
    return {"scores": scores, "weakest": weakest,
            "detail": {"llms_http": code_llms, "brain_enabled": enabled,
                       "reg_active_platforms": reg_platforms}}


# ── TIER 3 — ACT (one bounded action on the weakest lever) ────────────
def tier3_act(levers: dict) -> dict:
    if _act_disabled():
        return {"action": "none", "reason": "GROWTH_MASTER_ACT_DISABLED (shadow mode)"}
    lever = levers.get("weakest")
    if _lever_off(lever):
        return {"action": "none", "reason": f"lever '{lever}' killed"}

    if lever == "autonomy":
        # Nudge the brain to actually pick + act on work (propose-only unless armed).
        r = _fire("/api/v1/brain/self-direct/tick")
        return {"action": "brain_self_direct_tick", "lever": lever, "dispatched": r.get("dispatched")}

    if lever == "media":
        # Delegate to the Media master shell — it publishes a number-led evergreen if starved.
        r = _fire("/api/v1/admin/media/master-tick")
        return {"action": "media_master_tick", "lever": lever, "dispatched": r.get("dispatched")}

    if lever == "distribution":
        # Delegate to the Distribution master shell — GEO coverage + registry presence.
        r = _fire("/api/v1/admin/distribution/master-tick")
        return {"action": "distribution_master_tick", "lever": lever, "dispatched": r.get("dispatched")}

    if lever == "discovery":
        # Keep the machine-readable surfaces non-stale (llms.txt content adds are one-time).
        r = _fire("/api/v1/heartbeat/auto?batch=200")
        return {"action": "surface_refresh", "lever": lever, "dispatched": r.get("dispatched"),
                "flag": "Advertise /onboard + /platforms/register in llms.txt (one-time content)."}

    if lever == "measurement":
        return {"action": "flag", "lever": lever,
                "flag": "active_platforms or cron health off — see growth snapshot detail."}

    return {"action": "none", "reason": f"unknown lever {lever}"}


# ── SCORE, PERSIST, VERIFY ────────────────────────────────────────────
def growth_score(m: dict, levers: dict) -> float:
    s = levers.get("scores") or {}
    # lever health is 60% of the score; the acquisition KPI (real-agent WoW) is 40%.
    lever_avg = (sum(s.values()) / len(s)) if s else 0.0
    wow = m.get("real_agents_wow")
    # map WoW in [-0.5, +0.5] → [0,1]; None → neutral 0.5
    kpi = 0.5 if wow is None else max(0.0, min(1.0, 0.5 + wow))
    return round(100.0 * (0.6 * lever_avg + 0.4 * kpi), 2)


def _persist(m: dict, levers: dict, score: float, action: dict) -> bool:
    if not _ensure_tables():
        return False
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO growth_snapshots
                  (growth_score, real_agents_7d, real_agents_wow, conversions_30d,
                   weakest_lever, action_taken, lever_scores, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                score, m.get("real_agents_7d"), m.get("real_agents_wow"),
                m.get("conversions_30d"), levers.get("weakest"),
                (action or {}).get("action"),
                json.dumps(levers.get("scores") or {}),
                json.dumps({"measure": m, "levers": levers, "action": action}),
            ))
        return True
    except Exception:
        note_swallowed_write("growth_snapshots", where="growth_master_shell._persist")
        return False
    finally:
        try: c.close()
        except Exception: pass


def _prev_snapshot() -> dict | None:
    c = _conn()
    if c is None:
        return None
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM growth_snapshots ORDER BY id DESC LIMIT 1")
            return cur.fetchone()
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def tier4_verify(m: dict, score: float) -> dict:
    prev = _prev_snapshot()
    if not prev:
        return {"baseline": True, "note": "first growth snapshot — deltas start next tick."}
    def d(cur, prev_v):
        try:
            return None if cur is None or prev_v is None else round(float(cur) - float(prev_v), 3)
        except (TypeError, ValueError):
            return None
    return {
        "baseline": False,
        "since": str(prev.get("computed_at")),
        "delta_growth_score": d(score, prev.get("growth_score")),
        "delta_real_agents_7d": d(m.get("real_agents_7d"), prev.get("real_agents_7d")),
    }


# ── ORCHESTRATOR ──────────────────────────────────────────────────────
@growth_master_shell_bp.route("/api/v1/admin/growth/master-tick", methods=["POST", "GET"])
def master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="GROWTH_MASTER_DISABLED"), 200
    started = time.time()
    measure = tier1_measure()
    levers = tier2_score_levers(measure)
    action = tier3_act(levers)
    score = growth_score(measure, levers)
    verify = tier4_verify(measure, score)
    persisted = _persist(measure, levers, score, action)

    s = levers.get("scores") or {}
    headline = (
        f"growth {score}/100 · {measure.get('real_agents_7d')} agents/wk "
        f"(WoW {('%+d%%' % int((measure.get('real_agents_wow') or 0)*100)) if measure.get('real_agents_wow') is not None else 'n/a'}) · "
        f"weakest lever → {levers.get('weakest')} ({s.get(levers.get('weakest'))}) · "
        f"acted: {action.get('action')}"
    )
    return jsonify(
        ok=True,
        ms=int((time.time() - started) * 1000),
        growth_score=score,
        headline=headline,
        tier1_measure=measure,
        tier2_levers=levers,
        tier3_action=action,
        tier4_verify=verify,
        persisted=persisted,
        generated_at=datetime.now(timezone.utc).isoformat(),
    ), 200


@growth_master_shell_bp.route("/api/v1/admin/growth/state", methods=["GET"])
def state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(error="db_unavailable"), 503
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM growth_snapshots ORDER BY id DESC LIMIT 20")
            rows = cur.fetchall()
        latest = rows[0] if rows else None
        return jsonify(
            ok=True,
            latest=latest,
            trend=[{"at": str(r.get("computed_at")), "score": _num(r.get("growth_score")),
                    "agents": r.get("real_agents_7d"), "weakest": r.get("weakest_lever"),
                    "acted": r.get("action_taken")} for r in rows],
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}"), 500
    finally:
        try: c.close()
        except Exception: pass
