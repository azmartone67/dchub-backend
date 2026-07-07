"""Phase 5 — long-horizon statefulness: persistent, drift-aware site shortlists.

Turns the in-conversation scoring stack (get_refined_queue -> analyze_site ->
rank_sites) into an ACROSS-conversation capability. An agent saves winners into a
named shortlist along with the objectives + metrics + percentile score they were
ranked under; days later it re-scores the whole list against the CURRENT national
baseline and gets an explicit drift delta per site — answering Grok's "did this
site get worse, or did the comparison set change?".

Ownership is API-key-scoped (Grok's assumed default): a shortlist belongs to the
key that created it; anonymous callers share a 'public' bucket.
"""
import os
import json
import hashlib

from flask import Blueprint, jsonify, request

import psycopg2
import psycopg2.extras

shortlists_bp = Blueprint("shortlists", __name__)

_MAX_SITES = 200  # per shortlist (perf bound for re-ranking)


def _dsn():
    return os.environ.get("DATABASE_URL", "")


def _conn():
    return psycopg2.connect(_dsn())


def _owner():
    key = (request.headers.get("X-API-Key", "")
           or request.args.get("api_key", "")).strip()
    if not key:
        return "public"
    return "k_" + hashlib.sha256(key.encode()).hexdigest()[:24]


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
    saved_score = None
    try:
        from site_baseline import score_site
        saved_score, _ = score_site(metrics, objectives)
    except Exception:
        saved_score = None

    _ensure_table()
    owner = _owner()
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
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
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
        "note": ("Saved with its objectives + percentile score snapshot. Call "
                 "/api/v1/shortlist/get?name=" + name + "&refresh=true later to re-score "
                 "against the current baseline and see drift. Shortlist is scoped to your API key."),
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
    try:
        c = _conn(); cur = c.cursor()
        cur.execute("""INSERT INTO shortlist_alerts
            (owner, shortlist_name, percentile_below, delta_below, notify_webhook, notify_email)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
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
            lines = "\n".join(f"- {h['site_ref']}: score {h.get('current_score')} (delta {h.get('score_delta_since_saved')})" for h in hits)
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
            cur.execute("""SELECT site_ref, saved_metrics, saved_objectives, saved_score
                           FROM agent_shortlist_sites WHERE owner=%s AND shortlist_name=%s""",
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
                    hits.append({"site_ref": r["site_ref"], "current_score": cur_score,
                                 "saved_score": saved, "score_delta_since_saved": delta})
            if hits:
                _notify(al, hits)
                summary["alerts_fired"] += 1
                try:
                    c = _conn(); cc = c.cursor()
                    cc.execute("UPDATE shortlist_alerts SET last_fired_at=now(), last_result=%s WHERE id=%s",
                               (json.dumps(hits), al["id"]))
                    c.commit(); c.close()
                except Exception:
                    pass
        except Exception as e:
            if len(summary["errors"]) < 3:
                summary["errors"].append(str(e)[:80])
    return summary
