"""
media_growth_master_shell.py — the self-driving MEDIA GROWTH MANAGER (2026-07-15).

Peer to media_master_shell (which is a cadence dead-man switch) but a level up: it
does not just keep the feed alive, it MANAGES growth toward a GOAL. The existing
media stack has a real MEASURE→LEARN half (per-post engagement sync, the r86d
engagement-by-kind bandit, the topic tuner, the style A/B) but it is a set of
RELATIVE optimizers — it reshuffles fixed choices to maximize engagement-per-post
and is BLIND to its own audience (no follower tracking at all, X entirely
fire-and-forget). This shell adds the missing SEE → GOAL → MANAGE loop.

Five stages (built incrementally):
  0 SEE      — audience telemetry the brain lacks today: LinkedIn + X follower
               snapshots, X engagement, and a social→site referral rollup.
  1 GOAL     — a target object (followers / citation velocity / reach) + the
               gap-to-target and trend, so growth is pursued, not just observed.
  2 MANAGE   — score the weakest growth lever and take ONE bounded strategy
               action (cadence / channel-mix / topic-strategy), weighting
               ABSOLUTE reach + follower growth, not just engagement-rate.
  3 SMARTER  — analyst-voice expansion + long-form thought leadership + a
               generative pre-publish critic (added in a later pass).
  4 AUTONOMY — this shell IS the autonomy: a cron-ticked master shell with the
               house guardrails (admin-gated, killable, one bounded action/tick).

DARK BY DEFAULT. The tick always MEASURES + snapshots + persists (benign
telemetry writes). It only EXECUTES a strategy action when MEDIA_GROWTH_ACT_ENABLED
is truthy; otherwise it runs in SHADOW mode — it records what it WOULD do to
media_growth_actions without touching the live feed. Kill switch:
MEDIA_GROWTH_DISABLED=1.

Endpoints:
  POST /api/v1/admin/media-growth/master-tick   — measure → score → act → persist
  GET  /api/v1/admin/media-growth/master-state   — latest snapshot + trend
  POST /api/v1/admin/media-growth/goal           — set/replace a growth target
"""
import os
import json
import hmac
import urllib.request
import urllib.error
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

media_growth_master_shell_bp = Blueprint("media_growth_master_shell", __name__)

_BACKEND_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else os.environ.get("DCHUB_BACKEND_BASE", "https://dchub-backend-production.up.railway.app")
)

# Growth targets the shell pursues when no explicit goal row is set. Deliberately
# modest + honest (audience is ~4 followers today); the operator raises these via
# the /goal endpoint. horizon_days is the window the target is judged over.
DEFAULT_GOALS = {
    "li_followers":      {"target": 250, "horizon_days": 90, "label": "LinkedIn followers"},
    "x_followers":       {"target": 250, "horizon_days": 90, "label": "X followers"},
    "citation_velocity": {"target": 25,  "horizon_days": 30, "label": "AI citations / wk"},
}


def _admin_key() -> str | None:
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key") or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _disabled() -> bool:
    return str(os.environ.get("MEDIA_GROWTH_DISABLED", "")).lower() in ("1", "true", "yes")


def _act_enabled() -> bool:
    # OFF by default → shadow mode (record proposed actions, don't execute).
    return str(os.environ.get("MEDIA_GROWTH_ACT_ENABLED", "")).lower() in ("1", "true", "yes")


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


def _pct(now, then):
    """Percent change now vs then; None if not computable."""
    try:
        now = float(now); then = float(then)
        if then == 0:
            return None
        return round((now - then) / abs(then) * 100.0, 1)
    except (TypeError, ValueError):
        return None


def _req(path: str, timeout: int = 10) -> dict:
    url = (path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path)
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("X-DC-Probe", "media-growth-tick")
        req.add_header("User-Agent", "dchub-media-growth/1.0")
        ak = _admin_key()
        if ak:
            req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return {"ok": True, "http": resp.status, "data": json.loads(body)}
            except Exception:
                return {"ok": True, "http": resp.status, "data": {"_raw": body[:200]}}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


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
        with c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS social_audience (
                    id              BIGSERIAL PRIMARY KEY,
                    snap_date       DATE NOT NULL,
                    platform        TEXT NOT NULL,
                    followers       BIGINT,
                    followers_delta INTEGER,
                    eng_rate        DOUBLE PRECISION,
                    source_ok       BOOLEAN,
                    reason          TEXT,
                    captured_at     TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (snap_date, platform)
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_growth_goals (
                    metric        TEXT PRIMARY KEY,
                    target        DOUBLE PRECISION NOT NULL,
                    horizon_days  INTEGER NOT NULL DEFAULT 90,
                    label         TEXT,
                    set_at        TIMESTAMPTZ DEFAULT NOW()
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_growth_snapshots (
                    id                BIGSERIAL PRIMARY KEY,
                    li_followers      BIGINT,
                    li_followers_wow  DOUBLE PRECISION,
                    x_followers       BIGINT,
                    x_followers_wow   DOUBLE PRECISION,
                    citation_velocity DOUBLE PRECISION,
                    citation_trend    DOUBLE PRECISION,
                    li_eng_rate       DOUBLE PRECISION,
                    posts_7d          INTEGER,
                    growth_score      DOUBLE PRECISION,
                    weakest_lever     TEXT,
                    action_taken      TEXT,
                    act_mode          TEXT,
                    detail            JSONB,
                    created_at        TIMESTAMPTZ DEFAULT NOW()
                )""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_growth_actions (
                    id           BIGSERIAL PRIMARY KEY,
                    lever        TEXT,
                    action       TEXT,
                    rationale    TEXT,
                    payload      JSONB,
                    mode         TEXT,          -- 'shadow' | 'executed'
                    created_at   TIMESTAMPTZ DEFAULT NOW()
                )""")
        return True
    except Exception as e:
        note_swallowed_write("social_audience", where=f"media_growth_master_shell._ensure_tables:{type(e).__name__}")
        return False


# ── STAGE 0 — SEE ────────────────────────────────────────────────────────────

def _fetch_x_audience() -> dict:
    """OUR X account's follower count + recent-tweet engagement rate, via the
    OAuth1 quad (same creds twitter_diagnostic pings /2/users/me with). FAIL-SOFT:
    missing creds / requests_oauthlib absent / non-200 → {ok:False, reason}."""
    out = {"ok": False, "followers": None, "eng_rate": None, "reason": None}
    ck = os.environ.get("TWITTER_API_KEY", "").strip()
    cs = os.environ.get("TWITTER_API_SECRET", "").strip()
    at = os.environ.get("TWITTER_ACCESS_TOKEN", "").strip()
    ats = os.environ.get("TWITTER_ACCESS_SECRET", "").strip()
    if not (ck and cs and at and ats):
        out["reason"] = "no_creds"
        return out
    try:
        from requests_oauthlib import OAuth1
        import requests as req
        auth = OAuth1(ck, cs, at, ats)
        r = req.get("https://api.twitter.com/2/users/me?user.fields=public_metrics",
                    auth=auth, timeout=10)
        if r.status_code != 200:
            out["reason"] = f"http_{r.status_code}"
            return out
        d = (r.json() or {}).get("data") or {}
        pm = d.get("public_metrics") or {}
        out["followers"] = pm.get("followers_count")
        out["ok"] = out["followers"] is not None
        uid = d.get("id")
        if uid:
            try:
                tr = req.get(f"https://api.twitter.com/2/users/{uid}/tweets"
                             "?max_results=20&tweet.fields=public_metrics", auth=auth, timeout=10)
                if tr.status_code == 200:
                    tws = (tr.json() or {}).get("data") or []
                    imp = eng = 0
                    for tw in tws:
                        m = tw.get("public_metrics") or {}
                        imp += m.get("impression_count") or 0
                        eng += ((m.get("like_count") or 0) + (m.get("reply_count") or 0)
                                + (m.get("retweet_count") or 0) + (m.get("quote_count") or 0))
                    out["eng_rate"] = round(eng / imp, 4) if imp else None
            except Exception:
                pass
        return out
    except Exception as e:
        out["reason"] = type(e).__name__
        return out


def _snapshot_audience() -> dict:
    """Capture today's LinkedIn + X follower counts into social_audience (idempotent
    per (snap_date, platform)), computing the delta vs the most recent PRIOR snapshot.
    Returns the captured rows. Benign telemetry write — safe to run every tick."""
    today = datetime.now(timezone.utc).date().isoformat()
    results = {}
    # LinkedIn
    try:
        from linkedin_poster import fetch_linkedin_followers
        li = fetch_linkedin_followers()
    except Exception as e:
        li = {"ok": False, "followers": None, "reason": type(e).__name__}
    results["linkedin"] = li
    # X
    x = _fetch_x_audience()
    results["x"] = x

    c = _conn()
    if c is None:
        return {"stored": False, "results": results}
    try:
        with c, c.cursor() as cur:
            for platform, r in (("linkedin", li), ("x", x)):
                foll = r.get("followers")
                eng = r.get("eng_rate")
                # prior follower count (most recent snapshot before today) → delta
                delta = None
                if foll is not None:
                    cur.execute("""SELECT followers FROM social_audience
                                    WHERE platform=%s AND snap_date < %s AND followers IS NOT NULL
                                    ORDER BY snap_date DESC LIMIT 1""", (platform, today))
                    prev = cur.fetchone()
                    if prev and prev[0] is not None:
                        delta = int(foll) - int(prev[0])
                cur.execute("""
                    INSERT INTO social_audience
                        (snap_date, platform, followers, followers_delta, eng_rate, source_ok, reason)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (snap_date, platform) DO UPDATE SET
                        followers=EXCLUDED.followers, followers_delta=EXCLUDED.followers_delta,
                        eng_rate=EXCLUDED.eng_rate, source_ok=EXCLUDED.source_ok,
                        reason=EXCLUDED.reason, captured_at=NOW()
                """, (today, platform, foll, delta, eng, bool(r.get("ok")), r.get("reason")))
        return {"stored": True, "results": results}
    except Exception as e:
        note_swallowed_write("social_audience", where=f"_snapshot_audience:{type(e).__name__}")
        return {"stored": False, "results": results, "error": type(e).__name__}


def _followers_wow(platform: str):
    """(latest_followers, wow_pct) from social_audience for a platform."""
    c = _conn()
    if c is None:
        return None, None
    try:
        with c, c.cursor() as cur:
            cur.execute("""SELECT snap_date, followers FROM social_audience
                            WHERE platform=%s AND followers IS NOT NULL
                            ORDER BY snap_date DESC LIMIT 30""", (platform,))
            rows = cur.fetchall() or []
        if not rows:
            return None, None
        latest = rows[0][1]
        # nearest snapshot >= 7 days older than latest
        wk = None
        for _d, f in rows[1:]:
            wk = f  # fallback to oldest within 30
            if (rows[0][0] - _d).days >= 7:
                wk = f
                break
        return int(latest), _pct(latest, wk) if wk is not None else None
    except Exception:
        return None, None


def _social_referrals(days: int = 7) -> dict:
    """press_engagement.referrer grouped into social channels (the missing
    social→site attribution). Real-time; the only owned-surface traffic signal."""
    c = _conn()
    if c is None:
        return {}
    out = {}
    try:
        with c, c.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(LOWER(referrer),'') AS ref,
                       COUNT(*) FILTER (WHERE event_type='view') AS views,
                       COUNT(*) FILTER (WHERE event_type IN ('click_out','stripe_click')) AS clicks
                  FROM press_engagement
                 WHERE t > NOW() - make_interval(days => %s)
                 GROUP BY 1
            """, (int(days),))
            for ref, views, clicks in cur.fetchall() or []:
                if "linkedin" in ref or "lnkd.in" in ref:
                    ch = "linkedin"
                elif "twitter.com" in ref or "t.co" in ref or "x.com" in ref:
                    ch = "x"
                elif not ref:
                    ch = "direct"
                else:
                    ch = "other"
                o = out.setdefault(ch, {"views": 0, "clicks": 0})
                o["views"] += int(views or 0)
                o["clicks"] += int(clicks or 0)
    except Exception:
        pass
    return out


def _linkedin_reach(days: int = 21) -> dict:
    """Aggregate LinkedIn reach from linkedin_posts: avg eng_rate + posts + total
    impressions over the window (the absolute-reach signal the eng-rate bandit ignores)."""
    c = _conn()
    if c is None:
        return {}
    try:
        with c, c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(impressions),0) AS impr,
                       COALESCE(SUM(COALESCE(clicks,0)+COALESCE(likes,0)
                                +COALESCE(comments,0)+COALESCE(shares,0)),0) AS eng,
                       COUNT(*) FILTER (WHERE posted_at > NOW() - INTERVAL '7 days') AS posts_7d
                  FROM linkedin_posts
                 WHERE posted_at > NOW() - make_interval(days => %s)
                   AND COALESCE(status,'') IN ('success','published')
            """, (int(days),))
            row = cur.fetchone()
        if not row:
            return {}
        n, impr, eng, posts_7d = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0), int(row[3] or 0)
        return {"posts": n, "impressions": impr, "engagements": eng,
                "eng_rate": round(eng / impr, 4) if impr else None, "posts_7d": posts_7d}
    except Exception:
        return {}


def tier1_measure() -> dict:
    """SEE. Snapshot the audience, then assemble every growth signal the manager
    needs: LinkedIn/X followers + WoW, citation velocity + trend, LinkedIn reach +
    engagement, social→site referrals, cadence."""
    snap = _snapshot_audience()
    li_foll, li_wow = _followers_wow("linkedin")
    x_foll, x_wow = _followers_wow("x")

    ns = _req("/api/v1/media/north-star", timeout=8)
    cv = cv_trend = None
    if ns.get("ok"):
        d = ns.get("data") or {}
        cv = _num(d.get("citation_velocity_7d") or d.get("velocity_7d")
                  or (d.get("citation_velocity") or {}).get("per_week"))
        cv_trend = _num(d.get("citation_trend_pct") or d.get("trend_pct"))

    reach = _req("/api/v1/ai/reach/trend", timeout=8)
    agent_reach = None
    if reach.get("ok"):
        rd = reach.get("data") or {}
        agent_reach = _num(rd.get("new_external_ips_7d") or rd.get("latest")
                           or (rd.get("current") or {}).get("new_external_ips"))

    li_reach = _linkedin_reach()
    refs = _social_referrals(7)

    return {
        "li_followers": li_foll, "li_followers_wow": li_wow,
        "x_followers": x_foll, "x_followers_wow": x_wow,
        "citation_velocity": cv, "citation_trend": cv_trend,
        "agent_reach_new_ips_7d": agent_reach,
        "li_eng_rate": li_reach.get("eng_rate"), "li_impressions_21d": li_reach.get("impressions"),
        "posts_7d": li_reach.get("posts_7d"),
        "referrals_7d": refs,
        "snapshot": snap.get("results"),
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }


# ── STAGE 1 — GOAL ───────────────────────────────────────────────────────────

def _goals() -> dict:
    """Effective goals: DB rows override DEFAULT_GOALS."""
    goals = {k: dict(v) for k, v in DEFAULT_GOALS.items()}
    c = _conn()
    if c is None:
        return goals
    try:
        with c, c.cursor() as cur:
            cur.execute("SELECT metric, target, horizon_days, label FROM media_growth_goals")
            for metric, target, horizon, label in cur.fetchall() or []:
                goals[metric] = {"target": _num(target), "horizon_days": int(horizon or 90),
                                 "label": label or metric}
    except Exception:
        pass
    return goals


def _gaps(m: dict) -> dict:
    """Per-metric gap-to-target: current, target, pct_to_target, on_track heuristic."""
    goals = _goals()
    cur_vals = {
        "li_followers": m.get("li_followers"),
        "x_followers": m.get("x_followers"),
        "citation_velocity": m.get("citation_velocity"),
    }
    out = {}
    for metric, g in goals.items():
        cur = cur_vals.get(metric)
        tgt = g.get("target")
        pct = None
        if cur is not None and tgt:
            pct = round(min(100.0, float(cur) / float(tgt) * 100.0), 1)
        out[metric] = {"current": cur, "target": tgt, "horizon_days": g.get("horizon_days"),
                       "label": g.get("label"), "pct_to_target": pct,
                       "on_track": (pct is not None and pct >= 100.0)}
    return out


# ── STAGE 2 — MANAGE ─────────────────────────────────────────────────────────

def tier2_score(m: dict) -> dict:
    """Score growth health and find the WEAKEST lever. Health blends: cadence
    (are we shipping), reach trend (LinkedIn engagement present), citation trend,
    and follower momentum. Weakest lever = the one dragging the score down most."""
    gaps = _gaps(m)
    posts_7d = _num(m.get("posts_7d")) or 0
    # component sub-scores in [0,1]
    cadence = min(1.0, posts_7d / 14.0)                       # target ~2/day
    reach_ok = 1.0 if (m.get("li_eng_rate") or 0) > 0 else 0.0
    cv_trend = m.get("citation_trend")
    cv_mom = 1.0 if (cv_trend is not None and cv_trend >= 0) else (0.4 if cv_trend is not None else 0.5)
    li_mom = m.get("li_followers_wow")
    foll_mom = 1.0 if (li_mom is not None and li_mom > 0) else (0.3 if li_mom is not None else 0.5)
    blind = (m.get("li_followers") is None and m.get("x_followers") is None)

    comps = {"cadence": cadence, "reach": reach_ok, "citation_momentum": cv_mom,
             "follower_momentum": foll_mom}
    health = round(0.30 * cadence + 0.25 * reach_ok + 0.25 * cv_mom + 0.20 * foll_mom, 3)
    weakest = min(comps, key=comps.get)
    return {"growth_score": round(100.0 * health, 1), "health": health,
            "components": comps, "weakest_lever": weakest, "audience_blind": blind,
            "gaps": gaps}


def tier3_act(m: dict, sc: dict) -> dict:
    """MANAGE. Recommend ONE bounded action for the weakest lever. Shadow by
    default: records to media_growth_actions and returns the recommendation
    WITHOUT executing. Only executes low-risk levers when MEDIA_GROWTH_ACT_ENABLED=1.
    (Stage 3 will add analyst-expansion + thought-leadership action types.)"""
    lever = sc.get("weakest_lever")
    blind = sc.get("audience_blind")
    executed = _act_enabled()

    # If we cannot even SEE the audience, the only sane action is to fix telemetry
    # — never chase a follower goal blind. This is a report, not a live change.
    if blind:
        lever, action, rationale = "telemetry", "surface_audience_blindspot", (
            "No follower data captured yet (LinkedIn networkSizes / X /2/users/me "
            "returned no count) — follower goals are unpursuable until the read works. "
            "Check LinkedIn r_organization_admin scope + X OAuth1 creds.")
    elif lever == "cadence":
        action, rationale = "raise_cadence", (
            f"Only {m.get('posts_7d') or 0} posts in 7d — below the 2/day floor. "
            "Recommend the desk fill more slots (loosen suppression / commission an evergreen).")
    elif lever == "reach":
        action, rationale = "diagnose_reach", (
            "LinkedIn engagement reads zero — the daily engagement sync may be failing "
            "(token scope) so both learners are starved. Verify linkedin_engagement_sync.")
    elif lever == "citation_momentum":
        action, rationale = "boost_citation_surfaces", (
            "AI-citation velocity is flat/falling — lean the feed toward the cite-magnet "
            "angles (provenance, methodology, dataset drops) that earn agent citations.")
    else:  # follower_momentum
        action, rationale = "shift_toward_follow_worthy", (
            "Followers flat — reweight toward series/thread formats and thought-leadership "
            "that earn FOLLOWS, not just per-post clicks (Stage 3 expands this).")

    rec = {"lever": lever, "action": action, "rationale": rationale,
           "mode": "executed" if executed else "shadow"}

    # Persist the recommendation (always — this is the audit trail of the manager's thinking)
    c = _conn()
    if c is not None:
        try:
            with c, c.cursor() as cur:
                cur.execute("""INSERT INTO media_growth_actions (lever, action, rationale, payload, mode)
                               VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                            (lever, action, rationale[:500],
                             json.dumps({"score": sc.get("growth_score"),
                                         "components": sc.get("components")}),
                             rec["mode"]))
        except Exception as e:
            note_swallowed_write("media_growth_actions", where=f"tier3_act:{type(e).__name__}")

    # Execution of live strategy changes is deferred to Stage 2's enablement +
    # Stage 3's generators; for now even "executed" mode only records + reports.
    rec["executed_live"] = False
    return rec


def _persist(m: dict, sc: dict, act: dict) -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c, c.cursor() as cur:
            # ★fix-closure #33 (2026-07-26): one snapshot row per day. The
            # heartbeat fires this tick up to ~11× inside its cron hour and
            # every firing appended a full row (8 identical rows on 07-25).
            # Telemetry itself stays fresh (social_audience upserts on every
            # tick); only the duplicate snapshot append is skipped.
            cur.execute("SELECT 1 FROM media_growth_snapshots "
                        "WHERE created_at::date = CURRENT_DATE LIMIT 1")
            if cur.fetchone():
                return True
            cur.execute("""
                INSERT INTO media_growth_snapshots
                    (li_followers, li_followers_wow, x_followers, x_followers_wow,
                     citation_velocity, citation_trend, li_eng_rate, posts_7d,
                     growth_score, weakest_lever, action_taken, act_mode, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (m.get("li_followers"), m.get("li_followers_wow"),
                  m.get("x_followers"), m.get("x_followers_wow"),
                  m.get("citation_velocity"), m.get("citation_trend"),
                  m.get("li_eng_rate"), m.get("posts_7d"),
                  sc.get("growth_score"), sc.get("weakest_lever"),
                  act.get("action"), act.get("mode"),
                  json.dumps({"components": sc.get("components"), "gaps": sc.get("gaps"),
                              "referrals_7d": m.get("referrals_7d"),
                              "rationale": act.get("rationale")})))
        return True
    except Exception as e:
        note_swallowed_write("media_growth_snapshots", where=f"_persist:{type(e).__name__}")
        return False


# ── ROUTES ───────────────────────────────────────────────────────────────────

@media_growth_master_shell_bp.route("/api/v1/admin/media-growth/master-tick", methods=["POST", "GET"])
def master_tick():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if _disabled():
        return jsonify({"ok": True, "skipped": "MEDIA_GROWTH_DISABLED"}), 200
    _ensure_tables()
    m = tier1_measure()
    sc = tier2_score(m)
    act = tier3_act(m, sc)
    persisted = _persist(m, sc, act)
    return jsonify({
        "ok": True, "act_mode": act.get("mode"),
        "growth_score": sc.get("growth_score"), "weakest_lever": sc.get("weakest_lever"),
        "audience_blind": sc.get("audience_blind"),
        "action": {"lever": act.get("lever"), "action": act.get("action"),
                   "rationale": act.get("rationale")},
        "measure": {"li_followers": m.get("li_followers"), "x_followers": m.get("x_followers"),
                    "citation_velocity": m.get("citation_velocity"), "posts_7d": m.get("posts_7d"),
                    "li_eng_rate": m.get("li_eng_rate"), "referrals_7d": m.get("referrals_7d")},
        "gaps": sc.get("gaps"), "persisted": persisted,
    }), 200


@media_growth_master_shell_bp.route("/api/v1/admin/media-growth/master-state", methods=["GET"])
def master_state():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    _ensure_tables()
    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "no_db"}), 200
    try:
        with c, c.cursor() as cur:
            cur.execute("""SELECT created_at, li_followers, x_followers, citation_velocity,
                                  growth_score, weakest_lever, action_taken, act_mode
                             FROM media_growth_snapshots ORDER BY id DESC LIMIT 20""")
            rows = [{"at": str(r[0]), "li_followers": r[1], "x_followers": r[2],
                     "citation_velocity": r[3], "growth_score": r[4], "weakest_lever": r[5],
                     "action": r[6], "act_mode": r[7]} for r in (cur.fetchall() or [])]
            cur.execute("""SELECT platform, snap_date, followers, followers_delta, source_ok, reason
                             FROM social_audience ORDER BY snap_date DESC, platform LIMIT 10""")
            aud = [{"platform": r[0], "date": str(r[1]), "followers": r[2],
                    "delta": r[3], "ok": r[4], "reason": r[5]} for r in (cur.fetchall() or [])]
        return jsonify({"ok": True, "act_enabled": _act_enabled(), "disabled": _disabled(),
                        "goals": _goals(), "recent_snapshots": rows, "audience": aud}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}), 200


@media_growth_master_shell_bp.route("/api/v1/admin/media-growth/goal", methods=["POST"])
def set_goal():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    _ensure_tables()
    body = request.get_json(silent=True) or {}
    metric = str(body.get("metric") or "").strip()
    target = _num(body.get("target"))
    if not metric or target is None:
        return jsonify({"ok": False, "error": "metric + numeric target required"}), 400
    horizon = int(_num(body.get("horizon_days")) or 90)
    label = str(body.get("label") or metric)[:80]
    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "no_db"}), 200
    try:
        with c, c.cursor() as cur:
            cur.execute("""INSERT INTO media_growth_goals (metric, target, horizon_days, label)
                           VALUES (%s,%s,%s,%s)
                           ON CONFLICT (metric) DO UPDATE SET
                             target=EXCLUDED.target, horizon_days=EXCLUDED.horizon_days,
                             label=EXCLUDED.label, set_at=NOW()""",
                        (metric, target, horizon, label))
        return jsonify({"ok": True, "goals": _goals()}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}), 200


def register_media_growth_master_shell(app):
    try:
        if "media_growth_master_shell" not in app.blueprints:
            app.register_blueprint(media_growth_master_shell_bp)
    except Exception as e:
        print(f"[media_growth_master_shell] register failed: {e}", flush=True)
