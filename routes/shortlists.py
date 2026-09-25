"""Phase 5 — long-horizon statefulness: persistent, drift-aware site shortlists.

Turns the in-conversation scoring stack (get_refined_queue -> analyze_site ->
rank_sites) into an ACROSS-conversation capability. An agent saves winners into a
named shortlist along with the objectives + metrics + percentile score they were
ranked under; days later it re-scores the whole list against the CURRENT national
baseline and gets an explicit drift delta per site — answering Grok's "did this
site get worse, or did the comparison set change?".

Ownership is API-key-scoped (Grok's assumed default): a shortlist belongs to the
key that created it.

★2026-07-29 (Persistence Master Shell #41, lane 1): anonymous callers used to
share a 'public' bucket — one global namespace addressed by name, so any keyless
agent could read or overwrite any other's shortlist. Keyless callers are now
REFUSED with a claim_free_key instruction instead. Persistence across
conversations requires durable identity, and a shared bucket provided neither
privacy nor durability. See _owner().
"""
import os
import json
import math
import hashlib

from flask import Blueprint, jsonify, request

import psycopg2
import psycopg2.extras
from routes._swallowed_writes import note_swallowed_write

shortlists_bp = Blueprint("shortlists", __name__)

_MAX_SITES = 200  # per shortlist (perf bound for re-ranking)
_SAME_REGION_KM = 300.0  # proximity proxy for "same ISO/market" — per-site ISO isn't stored


def _haversine_km(a_lat, a_lng, b_lat, b_lng):
    try:
        r = 6371.0
        p1, p2 = math.radians(a_lat), math.radians(b_lat)
        dp = math.radians(b_lat - a_lat)
        dl = math.radians(b_lng - a_lng)
        h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return round(2 * r * math.asin(min(1.0, math.sqrt(h))), 1)
    except Exception:
        return None


def _reallocation_for(rows, drifted, score_fn):
    """Re-allocation primitive: when a saved site drifts, return the tiered set of
    replacement candidates drawn from the REST of the shortlist, re-scored against the
    DRIFTED slot's objectives, split same-region vs cross-region, plus a systemic-drift
    flag. Data-agnostic — uses only stored metrics/objectives/coords, no queue rescan.

    The systemic flag is the honest part: if the rest of the shortlist ALSO slipped
    (median peer delta clearly negative), the drift is region/baseline-wide and a
    same-region swap likely inherits it; if peers held, the drop is idiosyncratic.
    DC Hub returns the reduced, annotated set — the final weighting is the agent's call.
    """
    d_obj = drifted.get("saved_objectives") if isinstance(drifted.get("saved_objectives"), dict) else {}
    d_lat = float(drifted["lat"]) if drifted.get("lat") is not None else None
    d_lng = float(drifted["lng"]) if drifted.get("lng") is not None else None

    peer_deltas = []
    cands = []
    for r in rows:
        if r.get("id") == drifted.get("id"):
            continue
        m = r["saved_metrics"] if isinstance(r["saved_metrics"], dict) else {}
        own_obj = r["saved_objectives"] if isinstance(r["saved_objectives"], dict) else {}
        # this peer's OWN drift (for the systemic-vs-idiosyncratic signal)
        try:
            cs_own = score_fn(m, own_obj)[0] if own_obj else None
        except Exception:
            cs_own = None
        saved_own = float(r["saved_score"]) if r.get("saved_score") is not None else None
        if cs_own is not None and saved_own is not None:
            peer_deltas.append(round(cs_own - saved_own, 1))
        # this peer's FIT for the drifted slot (scored against the drifted objectives)
        try:
            fit = score_fn(m, d_obj)[0] if d_obj else None
        except Exception:
            fit = None
        if fit is None:
            continue
        dist = (_haversine_km(d_lat, d_lng, float(r["lat"]), float(r["lng"]))
                if (d_lat is not None and r.get("lat") is not None) else None)
        tier = "unknown_region" if dist is None else (
            "same_region" if dist <= _SAME_REGION_KM else "cross_region")
        cands.append({
            "site_ref": r.get("site_ref"), "fit_score": fit, "distance_km": dist,
            "tier": tier,
            "capacity_mw": float(r["capacity_mw"]) if r.get("capacity_mw") is not None else None,
        })

    systemic, med = None, None
    if peer_deltas:
        s = sorted(peer_deltas)
        n = len(s)
        med = s[n // 2] if n % 2 else round((s[n // 2 - 1] + s[n // 2]) / 2, 1)
        systemic = med <= -3.0

    def _top(tier):
        return sorted([c for c in cands if c["tier"] == tier],
                      key=lambda x: x["fit_score"], reverse=True)[:3]

    if systemic:
        rec = (f"prefer_cross_region — the rest of your shortlist also slipped (median peer "
               f"delta {med}); this drift looks systemic to the region/baseline, so a same-region "
               f"swap will likely inherit it.")
    elif systemic is False:
        rec = (f"tactical_ok — the rest of your shortlist held (median peer delta {med}); this drop "
               f"looks idiosyncratic to this site, so a same-region swap is reasonable.")
    else:
        rec = "insufficient_signal — too few re-scorable peers to separate systemic from idiosyncratic drift."

    return {
        "drifted_site": drifted.get("site_ref"),
        "drift_is_systemic": systemic,
        "peer_median_delta": med,
        "tier_1_same_region": _top("same_region"),
        "tier_2_cross_region": _top("cross_region"),
        "unknown_region": _top("unknown_region"),
        "recommendation": rec,
        "candidate_universe": "your_saved_shortlist",
        "caveats": [
            "Candidates come only from THIS shortlist's other saved sites, not the full viable "
            "queue (that's v2). If none fit, save more or re-run get_refined_queue.",
            f"Region tiers are a proximity proxy (<= {int(_SAME_REGION_KM)}km = same_region); per-site "
            "ISO isn't stored, so a cross-ISO boundary inside the band won't be caught — tiers are directional.",
            "fit_score re-scores each candidate against the DRIFTED slot's objectives (who best fills "
            "THIS slot), not each candidate's own saved score.",
        ],
    }


def _dsn():
    return os.environ.get("DATABASE_URL", "")


def _conn():
    return psycopg2.connect(_dsn())


# Persistence Master Shell #41 lane 1 (2026-07-29).
#
# _owner() used to return the literal "public" for every keyless caller, so ALL
# anonymous shortlists shared ONE namespace addressed by name — any anonymous
# agent could read, overwrite or collide with any other's list by guessing
# "my-sites". The tool's own response text tells the caller "Shortlist is scoped
# to your API key", which was false for exactly the callers who needed to know
# otherwise. It never caused an incident only because real anonymous saves
# number one in 90 days, and that one was a probe from building the shell.
#
# The rejected fix was scoping keyless callers to X-MCP-Session. The gateway does
# forward that header, so it would have compiled and an isolation test would have
# passed — but session ids rotate per connection (of 7,933 sessions in 30d exactly
# one recurred), so a session-scoped shortlist cannot survive to the next
# conversation. That trades a cross-tenant bug for a silently useless feature:
# saves appear to work, and the list is simply gone next time. Harder to notice,
# no better for the user.
#
# Persistence requires durable identity. There is no third option, so a keyless
# save must now REFUSE and convert — claim_free_key is one call with no email and
# already exists for this. OWNER_REQUIRED is the sentinel; callers turn it into a
# 401 carrying that instruction rather than writing to a shared bucket.
OWNER_REQUIRED = None


def _owner():
    """Durable owner for the caller, or OWNER_REQUIRED (None) when keyless.

    Returns None rather than raising so each call site keeps its own response
    shape; every site MUST check for None before writing or reading.
    """
    key = (request.headers.get("X-API-Key", "")
           or request.args.get("api_key", "")).strip()
    if not key:
        return OWNER_REQUIRED
    return "k_" + hashlib.sha256(key.encode()).hexdigest()[:24]


def _owner_required_response():
    """The honest 402-shaped refusal: what failed, and the one call that fixes it."""
    return jsonify({
        "ok": False,
        "error": "identity_required",
        "message": (
            "Shortlists persist across conversations, which needs a durable key — "
            "anonymous callers have none. Call the `claim_free_key` tool (no email, "
            "one call), set the returned key as your X-API-Key header, and retry. "
            "Your shortlist is then private to that key."
        ),
        "next_tool": "claim_free_key",
    }), 401


def _ensure_table():
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_shortlist_sites (
                id bigserial PRIMARY KEY,
                owner text NOT NULL,
                shortlist_name text NOT NULL,
                site_ref text,
                lat numeric, lng numeric, capacity_mw numeric,
                saved_metrics jsonb, saved_objectives jsonb,
                saved_score numeric, notes text,
                saved_at timestamptz DEFAULT now()
            )
        """)
        cur.execute("""CREATE INDEX IF NOT EXISTS idx_shortlist_owner_name
                       ON agent_shortlist_sites (owner, shortlist_name)""")
        c.commit()
    finally:
        c.close()


@shortlists_bp.route("/api/v1/shortlist/save", methods=["POST"])
def api_shortlist_save():
    """Save one site into a named shortlist, snapshotting its current percentile
    objective_score. Body: {shortlist_name, site:{site_ref?, lat, lng, capacity_mw,
    <metric fields>}, objectives:{field:weight}, notes?}."""
    body = request.get_json(silent=True) or {}
    name = (body.get("shortlist_name") or "").strip()
    site = body.get("site") or {}
    objectives = body.get("objectives") or {}
    notes = (body.get("notes") or "")[:500]
    if not name or not isinstance(site, dict) or not site:
        return jsonify(ok=False, _entity="error", error="shortlist_name and site are required"), 400

    metrics = {k: v for k, v in site.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)
               and k not in ("lat", "lng", "lon", "capacity_mw")}

    # Persistence Master Shell #41 lane 2 (2026-07-29) — derive objectives when
    # the caller has none.
    #
    # The MCP schema used to REQUIRE objectives while this endpoint treated them
    # as optional (`or {}`), so the two contracts were inverse and the only
    # payload satisfying both carried a signed-weight map most agents never
    # build. That is why zero real external agents completed a save in 90 days.
    #
    # Making it optional is only safe if the snapshot + re-rank contract survives,
    # and it did NOT survive before: `objectives: {}` already passed both layers
    # and produced saved_score=None — a row that can never show drift, which is
    # the entire feature. The old zod requirement was satisfiable with an empty
    # object, so it never protected what it claimed to.
    #
    # Equal weights over the site's OWN numeric metrics close it at the layer
    # that can enforce it: saved_objectives is never empty, so re-scoring always
    # has criteria, and a caller who DOES pass objectives is untouched.
    # Direction is unknowable without the caller's intent, so weights are
    # positive (treat each metric as better-when-higher) and `objectives_derived`
    # marks the row so a consumer can tell a derived basis from a stated one and
    # never present it as the user's stated criteria.
    objectives_derived = False
    if not objectives and metrics:
        w = round(1.0 / len(metrics), 6)
        objectives = {k: w for k in metrics}
        objectives_derived = True
    saved_score = None
    try:
        from site_baseline import score_site
        saved_score, _ = score_site(metrics, objectives)
    except Exception:
        saved_score = None

    _ensure_table()
    owner = _owner()
    if owner is OWNER_REQUIRED:            # keyless -> convert, never share a bucket
        return _owner_required_response()
    try:
        c = _conn()
        cur = c.cursor()
        cur.execute("SELECT count(*) FROM agent_shortlist_sites WHERE owner=%s AND shortlist_name=%s",
                    (owner, name))
        if (cur.fetchone()[0] or 0) >= _MAX_SITES:
            c.close()
            return jsonify(ok=False, _entity="error",
                           error=f"shortlist '{name}' is full ({_MAX_SITES} sites max)"), 400
        cur.execute("""
            INSERT INTO agent_shortlist_sites
                (owner, shortlist_name, site_ref, lat, lng, capacity_mw,
                 saved_metrics, saved_objectives, saved_score, notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id
        """, (owner, name, site.get("site_ref") or site.get("queue_id") or site.get("id"),
              site.get("lat"), site.get("lng") or site.get("lon"), site.get("capacity_mw"),
              json.dumps(metrics), json.dumps(objectives), saved_score, notes))
        sid = cur.fetchone()[0]
        c.commit()
        c.close()
    except Exception as e:
        return jsonify(ok=False, _entity="error", error=str(e)[:200]), 200

    return jsonify({
        "_entity": "shortlist_saved", "ok": True,
        "shortlist_name": name, "id": sid, "saved_score": saved_score,
        # Tell the caller when the basis was DERIVED rather than stated, so an
        # agent never reports equal weights back to its human as their criteria.
        "objectives_derived": objectives_derived,
        "note": ("Saved with its objectives + percentile score snapshot. Call "
                 "/api/v1/shortlist/get?name=" + name + "&refresh=true later to re-score "
                 "against the current baseline and see drift. Shortlist is scoped to your API key."
                 + (" You passed no objectives, so this site was scored with EQUAL weights "
                    "across its own metric fields — pass an objectives map on the next save "
                    "if you ranked it under specific criteria."
                    if objectives_derived else "")),
        "_source": "DC Hub — dchub.cloud",
    })


@shortlists_bp.route("/api/v1/shortlist/get", methods=["GET"])
def api_shortlist_get():
    """Return a saved shortlist. With refresh=true (default), each site is re-scored
    against the CURRENT national baseline and returns saved_score, current_score, and
    score_delta_since_saved — so an agent sees exactly what drifted."""
    name = (request.args.get("name") or "").strip()
    refresh = str(request.args.get("refresh", "true")).lower() not in ("0", "false", "no")
    if not name:
        return jsonify(ok=False, _entity="error", error="name is required"), 400
    _ensure_table()
    owner = _owner()
    if owner is OWNER_REQUIRED:            # keyless -> convert, never share a bucket
        return _owner_required_response()
    try:
        c = _conn()
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT * FROM agent_shortlist_sites
                       WHERE owner=%s AND shortlist_name=%s ORDER BY saved_score DESC NULLS LAST""",
                    (owner, name))
        rows = cur.fetchall()
        c.close()
    except Exception as e:
        return jsonify(ok=False, _entity="error", error=str(e)[:200]), 200

    baseline_meta = {}
    _score_fn = None
    if refresh:
        try:
            from site_baseline import score_site, load_baseline, baseline_meta as _bm
            load_baseline(force=True)
            baseline_meta = _bm()
            _score_fn = score_site
        except Exception:
            _score_fn = None

    results = []
    for r in rows:
        metrics = r["saved_metrics"] if isinstance(r["saved_metrics"], dict) else {}
        objectives = r["saved_objectives"] if isinstance(r["saved_objectives"], dict) else {}
        saved = float(r["saved_score"]) if r["saved_score"] is not None else None
        entry = {
            "id": r["id"], "site_ref": r["site_ref"],
            "lat": float(r["lat"]) if r["lat"] is not None else None,
            "lng": float(r["lng"]) if r["lng"] is not None else None,
            "capacity_mw": float(r["capacity_mw"]) if r["capacity_mw"] is not None else None,
            "notes": r["notes"], "saved_at": r["saved_at"].isoformat() if r["saved_at"] else None,
            "saved_score": saved,
        }
        if _score_fn and objectives:
            cur_score, _ = _score_fn(metrics, objectives)
            entry["current_score"] = cur_score
            if saved is not None and cur_score is not None:
                entry["score_delta_since_saved"] = round(cur_score - saved, 1)
        results.append(entry)

    if refresh:
        results.sort(key=lambda e: (e.get("current_score") if e.get("current_score") is not None else -1), reverse=True)

    return jsonify({
        "_entity": "shortlist", "ok": True,
        "shortlist_name": name, "count": len(results),
        "refreshed_against_current_baseline": bool(refresh and _score_fn),
        "baseline": baseline_meta,
        "results": results,
        "note": ("score_delta_since_saved = current percentile objective_score minus the snapshot "
                 "at save time; a negative delta with an unchanged site means the national population "
                 "improved around it (it slipped in rank), not that the site got worse. Scored with "
                 "the objectives saved per-site. Shortlist scoped to your API key."),
        "_source": "DC Hub — dchub.cloud",
    })


@shortlists_bp.route("/api/v1/shortlist/reallocate", methods=["POST"])
def api_shortlist_reallocate():
    """When a saved site drifts, suggest replacements from the rest of the shortlist,
    tiered same-region vs cross-region, with a systemic-drift flag. Body:
    {shortlist_name, drifted_site_ref?}. If drifted_site_ref is omitted, the current
    lowest-scoring site is treated as the drifted slot. Returns the reduced, annotated
    candidate set — the final weighted pick is the agent's call."""
    body = request.get_json(silent=True) or {}
    name = (body.get("shortlist_name") or "").strip()
    drifted_ref = (body.get("drifted_site_ref") or "").strip()
    if not name:
        return jsonify(ok=False, _entity="error", error="shortlist_name is required"), 400
    _ensure_table()
    owner = _owner()
    if owner is OWNER_REQUIRED:            # keyless -> convert, never share a bucket
        return _owner_required_response()
    try:
        from site_baseline import score_site, load_baseline
        load_baseline(force=True)
    except Exception as e:
        return jsonify(ok=False, _entity="error", error=f"baseline unavailable: {str(e)[:120]}"), 200
    try:
        c = _conn()
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM agent_shortlist_sites WHERE owner=%s AND shortlist_name=%s",
                    (owner, name))
        rows = cur.fetchall()
        c.close()
    except Exception as e:
        return jsonify(ok=False, _entity="error", error=str(e)[:200]), 200
    if not rows:
        return jsonify(ok=False, _entity="error", error=f"shortlist '{name}' is empty or not yours"), 200
    if len(rows) < 2:
        return jsonify({
            "_entity": "reallocation_suggestion", "ok": True, "shortlist_name": name,
            "drifted_site": rows[0].get("site_ref"),
            "tier_1_same_region": [], "tier_2_cross_region": [], "drift_is_systemic": None,
            "recommendation": "no_alternatives — only one site is saved; add more candidates to enable re-allocation.",
            "_source": "DC Hub — dchub.cloud",
        })

    drifted = None
    if drifted_ref:
        drifted = next((r for r in rows if (r.get("site_ref") or "") == drifted_ref), None)
        if drifted is None:
            return jsonify(ok=False, _entity="error",
                           error=f"site_ref '{drifted_ref}' not found in shortlist '{name}'"), 200
    if drifted is None:
        def _cur_score(r):
            m = r["saved_metrics"] if isinstance(r["saved_metrics"], dict) else {}
            o = r["saved_objectives"] if isinstance(r["saved_objectives"], dict) else {}
            try:
                s = score_site(m, o)[0]
                return s if s is not None else 999
            except Exception:
                return 999
        drifted = min(rows, key=_cur_score)

    sug = _reallocation_for(rows, drifted, score_site)
    sug.update({
        "_entity": "reallocation_suggestion", "ok": True, "shortlist_name": name,
        "note": ("Server-side reduction only: DC Hub returns the tiered, constraint-annotated candidate "
                 "set + a systemic-drift signal; the final weighted pick is the agent's call."),
        "_source": "DC Hub — dchub.cloud",
    })
    return jsonify(sug)


# ── Drift-triggered shortlist alerts (Grok's #1 fast-follow) ────────────────
def _ensure_alert_table():
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shortlist_alerts (
                id bigserial PRIMARY KEY,
                owner text NOT NULL,
                shortlist_name text NOT NULL,
                percentile_below numeric,
                delta_below numeric,
                notify_webhook text,
                notify_email text,
                active boolean DEFAULT true,
                created_at timestamptz DEFAULT now(),
                last_fired_at timestamptz,
                last_result jsonb
            )
        """)
        c.commit()
    finally:
        c.close()


@shortlists_bp.route("/api/v1/shortlist/alert", methods=["POST"])
def api_shortlist_alert():
    """Set a drift alert on a shortlist. Fires when any saved site's current
    percentile score drops below percentile_below OR its score_delta_since_saved
    drops below delta_below. Body: {shortlist_name, percentile_below?, delta_below?,
    notify:{webhook?, email?}}. Evaluated after each daily baseline refresh."""
    body = request.get_json(silent=True) or {}
    name = (body.get("shortlist_name") or "").strip()
    notify = body.get("notify") or {}
    pct_below = body.get("percentile_below")
    delta_below = body.get("delta_below")
    if not name or (pct_below is None and delta_below is None):
        return jsonify(ok=False, _entity="error",
                       error="shortlist_name and at least one of percentile_below / delta_below are required"), 400
    if not (notify.get("webhook") or notify.get("email")):
        return jsonify(ok=False, _entity="error", error="notify.webhook or notify.email is required"), 400
    _ensure_alert_table()
    owner = _owner()
    if owner is OWNER_REQUIRED:            # keyless -> convert, never share a bucket
        return _owner_required_response()
    try:
        c = _conn(); cur = c.cursor()
        cur.execute("""INSERT INTO shortlist_alerts
            (owner, shortlist_name, percentile_below, delta_below, notify_webhook, notify_email)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id""",
            (owner, name, pct_below, delta_below, notify.get("webhook"), notify.get("email")))
        aid = cur.fetchone()[0]; c.commit(); c.close()
    except Exception as e:
        return jsonify(ok=False, _entity="error", error=str(e)[:200]), 200
    return jsonify({
        "_entity": "shortlist_alert_set", "ok": True, "id": aid, "shortlist_name": name,
        "condition": {"percentile_below": pct_below, "delta_below": delta_below},
        "note": ("Evaluated after each daily baseline refresh; fires the webhook/email when any site "
                 "in the shortlist crosses the threshold. Scoped to your API key."),
        "_source": "DC Hub — dchub.cloud",
    })


def _notify(alert, hits):
    """Best-effort delivery: webhook POST + email (Resend if configured)."""
    import json as _json
    import urllib.request as _u
    payload = {"event": "shortlist_drift_alert", "shortlist_name": alert["shortlist_name"],
               "condition": {"percentile_below": alert.get("percentile_below"),
                             "delta_below": alert.get("delta_below")},
               "sites": hits, "source": "DC Hub (dchub.cloud)"}
    if alert.get("notify_webhook"):
        try:
            req = _u.Request(alert["notify_webhook"], data=_json.dumps(payload).encode(),
                             method="POST", headers={"Content-Type": "application/json",
                                                     "User-Agent": "dchub-shortlist-alert/1.0"})
            _u.urlopen(req, timeout=15)
        except Exception:
            pass
    email = alert.get("notify_email")
    rk = os.environ.get("DCHUB_RESEND_API_KEY") or os.environ.get("RESEND_API_KEY")
    if email and rk:
        try:
            def _alt_line(h):
                base = f"- {h['site_ref']}: score {h.get('current_score')} (delta {h.get('score_delta_since_saved')})"
                ra = h.get("reallocation") or {}
                pool = (ra.get("tier_1_same_region") or []) + (ra.get("tier_2_cross_region") or [])
                if pool:
                    top = pool[0]
                    base += f"\n    ↳ next-best: {top.get('site_ref')} (fit {top.get('fit_score')}, {top.get('tier')})"
                return base
            lines = "\n".join(_alt_line(h) for h in hits)
            eb = {"from": "DC Hub <" + (os.environ.get("DCHUB_FROM_EMAIL") or "jonathan@dchub.cloud") + ">",
                  "to": [email], "subject": f"DC Hub drift alert — {alert['shortlist_name']}",
                  "text": f"{len(hits)} site(s) in '{alert['shortlist_name']}' crossed your threshold:\n{lines}\n\n— DC Hub (dchub.cloud)"}
            req = _u.Request("https://api.resend.com/emails", data=_json.dumps(eb).encode(),
                             method="POST", headers={"Authorization": f"Bearer {rk}", "Content-Type": "application/json"})
            _u.urlopen(req, timeout=15)
        except Exception:
            pass


def evaluate_shortlist_alerts():
    """Called after the baseline tick recomputes the reference distribution: check
    every active alert's shortlist against the fresh baseline and notify on breach."""
    summary = {"alerts_checked": 0, "alerts_fired": 0, "errors": []}
    try:
        from site_baseline import score_site, load_baseline
        load_baseline(force=True)
    except Exception as e:
        summary["errors"].append(f"baseline:{str(e)[:60]}")
        return summary
    _ensure_alert_table()
    try:
        c = _conn()
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM shortlist_alerts WHERE active = true")
        alerts = cur.fetchall()
        c.close()
    except Exception as e:
        summary["errors"].append(f"load:{str(e)[:60]}")
        return summary
    for al in alerts:
        summary["alerts_checked"] += 1
        try:
            c = _conn()
            cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""SELECT * FROM agent_shortlist_sites
                           WHERE owner=%s AND shortlist_name=%s""",
                        (al["owner"], al["shortlist_name"]))
            rows = cur.fetchall(); c.close()
            hits = []
            for r in rows:
                m = r["saved_metrics"] if isinstance(r["saved_metrics"], dict) else {}
                o = r["saved_objectives"] if isinstance(r["saved_objectives"], dict) else {}
                cur_score, _ = score_site(m, o)
                if cur_score is None:
                    continue
                saved = float(r["saved_score"]) if r["saved_score"] is not None else None
                delta = round(cur_score - saved, 1) if saved is not None else None
                breach = False
                if al.get("percentile_below") is not None and cur_score < float(al["percentile_below"]):
                    breach = True
                if al.get("delta_below") is not None and delta is not None and delta < float(al["delta_below"]):
                    breach = True
                if breach:
                    hit = {"site_ref": r["site_ref"], "current_score": cur_score,
                           "saved_score": saved, "score_delta_since_saved": delta}
                    # operationalize the drift: attach a re-allocation suggestion
                    if len(rows) >= 2:
                        try:
                            hit["reallocation"] = _reallocation_for(rows, r, score_site)
                        except Exception:
                            pass
                    hits.append(hit)
            if hits:
                _notify(al, hits)
                summary["alerts_fired"] += 1
                try:
                    c = _conn(); cc = c.cursor()
                    cc.execute("UPDATE shortlist_alerts SET last_fired_at=now(), last_result=%s WHERE id=%s",
                               (json.dumps(hits), al["id"]))
                    c.commit(); c.close()
                except Exception:
                    note_swallowed_write("shortlist_alerts", where="shortlists.evaluate_shortlist_alerts")
                    pass
        except Exception as e:
            if len(summary["errors"]) < 3:
                summary["errors"].append(str(e)[:80])
    return summary
