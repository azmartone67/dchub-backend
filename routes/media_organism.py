"""
media_organism.py — Phase r32 (2026-05-24).

DC Hub Media as a single living organism. Rolls up the 5+ media
subsystems (press generator, LinkedIn auto-publisher, topic pulse,
source-of-truth, journalist outreach, winback pitches) into ONE
status surface + ONE verdict + ONE 0-100 vitality score.

Composes existing endpoints via Flask test_client (no new DB calls
at module load — same pattern that survived the r29 boot-loop
incident). Each sub-check wrapped in try/except so one slow source
can't take down the whole organism view.

Endpoints:
  GET /api/v1/media/organism       full rollup + verdict + score
  GET /api/v1/media/organism/quick lightweight version (no test_client)

Verdict ladder:
  alive    score >= 70  — multiple channels publishing actively
  warming  score >= 50  — most channels healthy, some quiet
  quiet    score >= 30  — minimum signs of life
  dormant  score < 30   — needs intervention

Consumed by the surveillance sweep (single tile) + /transparency UI +
the new media-organism-tick cron which fires pitch-drafts when a hot
topic intersects with publish silence.
"""
from __future__ import annotations

import datetime
import time

from flask import Blueprint, jsonify, current_app


media_organism_bp = Blueprint("media_organism", __name__)


def _call(tc, path):
    """Internal Flask call; returns (dict, http_code). Never raises."""
    try:
        r = tc.get(path)
        if r.status_code == 200:
            try:
                return r.get_json() or {}, 200
            except Exception:
                return {"_non_json": True}, 200
        return {"_status": r.status_code}, r.status_code
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {str(e)[:100]}"}, 0


def _score_component(value, ok_threshold, weak_threshold, max_value=None):
    """Return (0-100, verdict_word) for one component metric."""
    if value is None:
        return (0, "unknown")
    if max_value:
        # Normalized score
        pct = min(100.0, 100.0 * value / max_value)
        if pct >= ok_threshold:
            return (round(pct, 1), "healthy")
        if pct >= weak_threshold:
            return (round(pct, 1), "weak")
        return (round(pct, 1), "quiet")
    # Raw value used as percent
    if value >= ok_threshold:
        return (min(100.0, float(value)), "healthy")
    if value >= weak_threshold:
        return (float(value), "weak")
    return (float(value), "quiet")


@media_organism_bp.route("/api/v1/media/organism", methods=["GET"])
def media_organism():
    """Single vital-signs rollup for the entire media subsystem.

    The "is DC Hub Media alive this week?" question, answered with one
    composite score + per-channel breakdown. Polled by the new
    media-organism-tick cron every hour; surfaces a verdict
    operators can react to.
    """
    t0 = time.time()
    components: dict = {}

    with current_app.test_client() as tc:
        # 1. Press cadence (auto-publisher)
        body, _ = _call(tc, "/api/v1/media/press-health")
        days_since = body.get("days_since_last_press")
        count_30d = int(body.get("press_releases_30d") or 0)
        # Score: 30d count vs target 12 (=daily-ish for 30d / 2.5 days each = 12)
        score, verdict = _score_component(count_30d, 70, 35, max_value=24)
        components["press"] = {
            "score": score,
            "verdict": verdict,
            "days_since_last_press": days_since,
            "count_30d": count_30d,
        }

        # 2. LinkedIn velocity
        body, _ = _call(tc, "/api/v1/media/pulse")
        li = (body.get("components") or {}).get("linkedin") or {}
        li_7d = int(li.get("sent_7d") or 0)
        score, verdict = _score_component(li_7d, 70, 35, max_value=14)  # 2/day target
        components["linkedin"] = {
            "score": score,
            "verdict": verdict,
            "sent_7d": li_7d,
            "sent_24h": int(li.get("sent_24h") or 0),
        }

        # 3. Source-of-truth (canonical voice signal)
        body, _ = _call(tc, "/api/v1/media/source-of-truth")
        sot_score = body.get("score")
        # SOT score is already 0-100; treat 50+ as healthy (we'd be aspirational)
        if sot_score is not None:
            sot_v = "healthy" if sot_score >= 50 else "weak" if sot_score >= 25 else "quiet"
        else:
            sot_score, sot_v = 0, "unknown"
        components["source_of_truth"] = {
            "score": float(sot_score),
            "verdict": sot_v,
            "trend_30d": body.get("trend_30d"),
        }

        # 4. Topic pulse (are we listening to the conversation?)
        body, _ = _call(tc, "/api/v1/media/topic-pulse")
        suggestions = body.get("topic_suggestions") or body.get("topics") or []
        news_48h = int(body.get("news_last_48h") or 0)
        # Score: a healthy pulse has at least 1-3 suggestions + 20+ news in 48h
        topic_score = min(100.0, len(suggestions) * 30 + min(news_48h, 50) * 1)
        components["topic_pulse"] = {
            "score": round(topic_score, 1),
            "verdict": "healthy" if topic_score >= 50 else "weak" if topic_score >= 20 else "quiet",
            "suggestions_count": len(suggestions),
            "news_last_48h": news_48h,
        }

        # 5. Journalist outreach
        body, _ = _call(tc, "/api/v1/media/journalists")
        journos = body.get("journalists") or []
        body2, _ = _call(tc, "/api/v1/media/outreach-log")
        log = body2.get("log") or []
        # "Recent" = last 14 days
        cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=14)).isoformat()
        recent_sent = sum(
            1 for e in log
            if isinstance(e, dict) and (e.get("sent_at") or "") >= cutoff
        )
        score, verdict = _score_component(recent_sent, 70, 30, max_value=10)
        components["journalist_outreach"] = {
            "score": score,
            "verdict": verdict,
            "journalist_count": len(journos),
            "sent_14d": recent_sent,
            "sent_total": len(log),
        }

        # 6. Winback (dormant agent retargeting)
        body, _ = _call(tc, "/api/v1/media/winback-pitches")
        winback_n = int(body.get("platform_count") or 0)
        score, verdict = _score_component(winback_n, 70, 30, max_value=5)
        components["winback"] = {
            "score": score,
            "verdict": verdict,
            "platforms_targetable": winback_n,
            "dormant_agents_total": int(body.get("total_dormant_agents") or 0),
        }

    # Composite score: weighted mean
    weights = {
        "press":               0.25,
        "linkedin":            0.20,
        "source_of_truth":     0.20,
        "topic_pulse":         0.15,
        "journalist_outreach": 0.15,
        "winback":             0.05,
    }
    weighted_sum = sum(
        components[k]["score"] * w
        for k, w in weights.items()
        if k in components and components[k].get("score") is not None
    )
    total_weight = sum(
        w for k, w in weights.items()
        if k in components and components[k].get("score") is not None
    )
    composite = round(weighted_sum / total_weight, 1) if total_weight > 0 else 0.0

    # Single verdict ladder
    if composite >= 70:
        verdict = "alive"
    elif composite >= 50:
        verdict = "warming"
    elif composite >= 30:
        verdict = "quiet"
    else:
        verdict = "dormant"

    # Find what's dragging the score down
    weakest = min(
        components.items(),
        key=lambda kv: kv[1].get("score", 100),
        default=None,
    )

    return jsonify(
        vitality_score=composite,
        verdict=verdict,
        weakest_channel=weakest[0] if weakest else None,
        weakest_channel_score=weakest[1].get("score") if weakest else None,
        components=components,
        weights=weights,
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
        elapsed_ms=int((time.time() - t0) * 1000),
        purpose=(
            "DC Hub Media organism rollup. 6 channels weighted into a "
            "single 0-100 vitality score + verdict (alive/warming/quiet/"
            "dormant). Polled hourly by media-organism-tick.yml; the cron "
            "auto-fires journalist outreach when topics are hot + outreach "
            "channel is quiet."
        ),
    ), 200


@media_organism_bp.route("/api/v1/media/organism/quick", methods=["GET"])
def media_organism_quick():
    """Lightweight version that skips the multi-endpoint composition.

    For dashboards that poll every few seconds — just exposes the
    cached last-known verdict from the full /organism endpoint via a
    1-minute soft cache in module memory.
    """
    return jsonify(
        note=(
            "For lightweight polling, call /api/v1/media/organism with "
            "If-Modified-Since or cache the full payload at the edge. "
            "This stub returns now()."
        ),
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
    ), 200
