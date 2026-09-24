"""AI citation signals — ingest + read (2026-09-24). See ai_citation_signals.py.

  POST /api/ai/citation-hit          edge beacon (dchub-frontend _worker.js,
                                     beaconCitationSignal). Fire-and-forget.
  GET  /api/v1/ai/citation-signals   public, aggregate-only read.

The ingest RE-CLASSIFIES from the raw UA / Referer / query it is sent. It never
accepts a class or source from the caller, so the worst a forged POST can do is
add one hit to a bucket the classifier itself would have chosen — the same
exposure /api/ai/track-request has had since it shipped. Stated in `method`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

import ai_citation_signals as acs

logger = logging.getLogger(__name__)

citation_signals_bp = Blueprint("citation_signals", __name__)

COLLECTOR_STARTED = "2026-09-24"
WINDOWS = (("today", 1), ("7d", 7), ("30d", 30))


def _execute(sql, params=None, fetchall=False):
    from ai_tracking import _execute as _ex
    return _ex(sql, params, fetchall=fetchall)


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@citation_signals_bp.route("/api/ai/citation-hit", methods=["POST"])
def citation_hit():
    data = request.get_json(silent=True) or {}
    path = str(data.get("path") or "")[:500]
    ua = str(data.get("user_agent") or "")[:500]
    referer = str(data.get("referer") or "")[:500]
    query = str(data.get("query") or "")[:1000]
    dest = data.get("fetch_dest")
    dest = str(dest)[:20] if dest is not None else None

    cls = acs.classify(ua, referer, query, dest)
    if cls["signal_class"] == acs.OTHER and cls["agent"] == "no_signal":
        return jsonify({"status": "skipped", "signal_class": cls["signal_class"],
                        "source": cls["source"], "agent": cls["agent"]})
    ok = acs.record(cls, path, lambda s, p=None: _execute(s, p))
    return jsonify({"status": "recorded" if ok else "not_recorded",
                    "signal_class": cls["signal_class"],
                    "source": cls["source"], "agent": cls["agent"]})


def _method_block() -> dict:
    return {
        "collector": ("dchub-frontend _worker.js beaconCitationSignal -> "
                      "POST /api/ai/citation-hit (ctx.waitUntil, never delays "
                      "the page). One collector, one table: " + acs.TABLE),
        "collector_started": COLLECTOR_STARTED,
        "coverage": ("Only paths the Pages worker is routed for "
                     "(_routes.json include) are seen. The homepage and other "
                     "static-only pages are served by Pages without the worker "
                     "and are NOT counted. Direct calls to the Railway origin "
                     "bypass the edge and are NOT counted."),
        "classes": {
            "user_fetch": "an assistant fetched the URL because a user asked "
                          "(ChatGPT-User, Claude-User, Perplexity-User, "
                          "Meta-ExternalFetcher, Google-GeminiNotebook).",
            "search_crawler": "automated crawl for an assistant's search index "
                              "(OAI-SearchBot, Claude-SearchBot, PerplexityBot, "
                              "Meta-WebIndexer). Vendor-documented as not "
                              "training and not user-triggered.",
            "training_crawler": "crawl for model training (GPTBot, ClaudeBot, "
                                "Meta-ExternalAgent, CCBot, Bytespider).",
            "assistant_referral": "a non-bot UA on a top-level navigation whose "
                                  "Referer host or exact utm_source is an "
                                  "assistant (chatgpt.com, claude.ai, "
                                  "perplexity.ai, gemini.google.com, "
                                  "copilot.microsoft.com, ...).",
            "other": "a candidate that is none of the above, e.g. a bot UA "
                     "carrying an assistant Referer (agent=bot_ua) or a "
                     "subresource load (agent=non_document).",
        },
        "units": ("hits (requests), not unique people. A reload of a landing "
                  "URL that still carries utm_source counts again."),
        "identity": ("UA tokens are claimed, not verified against vendor IP "
                     "ranges; a spoofed UA counts as that vendor."),
        "robots_only_tokens": ("Google-Extended and Applebot-Extended are "
                               "robots.txt control tokens with no HTTP UA; "
                               "they read 0 by construction."),
        "microsoft": ("Microsoft publishes no Copilot fetch UA; microsoft can "
                      "only appear as assistant_referral. Plain bing.com is "
                      "search and is not counted."),
        "windows": "whole UTC days ending today; today is partial.",
    }


@citation_signals_bp.route("/api/v1/ai/citation-signals", methods=["GET"])
def citation_signals():
    out = {
        "success": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "classes": list(acs.CLASSES),
        "sources": list(acs.SOURCES),
        "windows": {name: None for name, _ in WINDOWS},
        "daily": None,
        "top_landing_paths": {"assistant_referral": None, "user_fetch": None},
        "method": _method_block(),
        "degraded": None,
    }
    try:
        windows = {}
        for name, days in WINDOWS:
            windows[name] = acs.fold_window(
                _execute(acs.window_counts_sql(days), fetchall=True))
        out["windows"] = windows
        out["daily"] = [
            {"date": str(r["day"]), "signal_class": r["signal_class"],
             "source": r["source"], "hits": int(r["hits"] or 0)}
            for r in _execute(acs.daily_series_sql(30), fetchall=True)
        ]
        tops = {}
        for klass in acs.PATH_CLASSES:
            tops[klass] = {
                win: [{"path": r["landing_path"], "source": r["source"],
                       "hits": int(r["hits"] or 0)}
                      for r in _execute(acs.top_paths_sql(klass, days),
                                        fetchall=True)]
                for win, days in (("7d", 7), ("30d", 30))
            }
        out["top_landing_paths"] = tops
    except Exception as exc:
        # Unknown, not zero: every figure stays null with the reason named.
        if "does not exist" in str(exc):
            out["degraded"] = ("no rows yet — the collector table has not been "
                               "written. Counts are unknown, not zero.")
        else:
            out["degraded"] = (f"db read failed ({type(exc).__name__}) — "
                               "counts are unknown, not zero.")
        out["windows"] = {name: None for name, _ in WINDOWS}
        out["daily"] = None
        out["top_landing_paths"] = {"assistant_referral": None,
                                    "user_fetch": None}
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return _cors(resp)
