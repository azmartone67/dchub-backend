"""Crawler-channel externality split — GET /api/v1/ai/crawler-split.

Public, machine-readable, no auth. See crawler_externality.py for why this
exists and what it does and does not claim. In short: the crawler & citation
channel had no externality filter of any kind, it was our best-looking number,
and the week it doubled was the week we ran a campaign instructing AI
assistants to fetch our surfaces. This endpoint splits that channel by PATH —
metadata (what an instructed fetch reads) against content (what a citation
reads) — and publishes the two separately, never summed.

★ EVERY BUCKET IS MEASURED OR IT SAYS IT IS NOT. A bucket with no rows renders
null with a reason, never 0: `0` reads as "we measured and found none", which
is a confident claim, and for the organic bucket it would be a false one — that
bucket is not instrumented at all.

  GET /api/v1/ai/crawler-split?days=7
  GET /api/v1/ai/crawler-split?days=7&by_platform=1
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from crawler_externality import (
    BUCKETS,
    CRAWLER_TABLE,
    HEADLINE_BUCKETS,
    bucket_counts_sql,
    collector_coverage,
    coverage_sql,
    crawler_population,
    published_series_sql,
    table_span_sql,
)
from routes.brain_ascension_master_shell import _conn

crawler_split_bp = Blueprint("crawler_split", __name__)

# Buckets that CANNOT be observed by either collector. Derived from the
# coverage declaration, not hand-listed: organic content paths match neither
# the Flask allowlist nor the worker's prefix filter, and content pages never
# reach the Flask app. If instrumentation is ever added, this set shrinks by
# the same edit that adds it.
_NOT_INSTRUMENTED = ("organic_content",)

_NOT_INSTRUMENTED_REASON = (
    "not instrumented — no collector on this channel can record a content-page "
    "crawl (see population.collector_coverage). This is NOT a measurement of "
    "zero organic citation traffic; it is the absence of a measurement.")


def _bucket_result(counts: dict, bucket: str, rows_in_window):
    """Pure verdict for one bucket: a count, or null and why.

    Split out from the query so the withholding rule is testable without a
    database — a gate only ever exercised in production is a gate nobody has
    checked.
    """
    if bucket in _NOT_INSTRUMENTED:
        return {"requests": None, "reason": _NOT_INSTRUMENTED_REASON}
    if counts is None:
        return {"requests": None,
                "reason": "source unavailable — count unknown, not zero"}
    n = counts.get(bucket)
    if not n:
        if not rows_in_window:
            return {"requests": None,
                    "reason": ("no classifiable rows in the window at all — "
                               "count unknown, not zero")}
        return {"requests": None,
                "reason": (f"no rows in this bucket among {rows_in_window} "
                           "classified in the window")}
    return {"requests": int(n), "reason": None}


def _split_payload(counts, rows_in_window) -> dict:
    """Assemble the bucket block. `counts` is None when the query failed."""
    return {b: _bucket_result(counts, b, rows_in_window) for b in BUCKETS}


@crawler_split_bp.route("/api/v1/ai/crawler-split", methods=["GET"])
def crawler_split():
    try:
        days = int(request.args.get("days", 7))
    except Exception:
        days = 7
    days = max(1, min(days, 90))
    by_platform = request.args.get("by_platform") in ("1", "true", "yes")

    out = {
        "channel": "crawler_and_citation",
        "window_days": days,
        "source_table": CRAWLER_TABLE,
        "headline_buckets": list(HEADLINE_BUCKETS),
        "never_sum": (
            "organic_content and instructed_metadata are different questions. "
            "Summing them reproduces the undefendable 'reach' figure this "
            "endpoint exists to take apart."),
        "buckets": _split_payload(None, None),
        "rows_classified": None,
        "population": crawler_population(days),
        "degraded": None,
    }

    c = _conn()
    if c is None:
        out["degraded"] = "db unavailable — every bucket is unknown, not zero"
        return jsonify(out), 200

    try:
        with c.cursor() as cur:
            # Inlined predicates, NO bound params: real_ua_predicate is a
            # regex form and carries no literal %, but the roster IN-list and
            # the LIKE patterns in path_bucket_case do — adding a parameter to
            # any execute() below would hand psycopg2 a % to eat.
            cur.execute("SET statement_timeout = '8000'")

            cur.execute(coverage_sql(days))
            row = cur.fetchone() or (None, None, None)
            rows_in_window = int(row[0] or 0)
            out["rows_classified"] = rows_in_window
            out["window_earliest_row"] = str(row[1]) if row[1] else None
            out["window_latest_row"] = str(row[2]) if row[2] else None

            cur.execute(bucket_counts_sql(days))
            counts = {r[0]: int(r[1] or 0) for r in cur.fetchall()}
            out["buckets"] = _split_payload(counts, rows_in_window)

            # How far back a re-split could ever go. Published as an observed
            # span rather than a claim about retention, because no purge job
            # for this table exists in the repo and "append-only" would be an
            # assumption.
            try:
                cur.execute(table_span_sql())
                span = cur.fetchone() or (None, None, None)
                out["reclassifiable_history"] = {
                    "earliest_row": str(span[0]) if span[0] else None,
                    "latest_row": str(span[1]) if span[1] else None,
                    "rows_total": int(span[2] or 0) if span[2] is not None
                    else None,
                    "note": ("observed span of the path-retaining table, "
                             "unfiltered — the bound on how much history the "
                             "split could cover"),
                }
            except Exception:
                out["reclassifiable_history"] = None

            # The path-LESS counter the public weekly reach is served from,
            # over the same platforms. Published so the gap is VISIBLE. The
            # windows differ by construction (ai_daily_stats buckets by
            # calendar DATE, ai_requests by rolling timestamp), so a nonzero
            # delta is expected and is not evidence of loss on its own.
            try:
                cur.execute(published_series_sql(days))
                pub = cur.fetchone()
                pub_n = int(pub[0] or 0) if pub else None
                out["cross_check"] = {
                    "ai_requests_rows": rows_in_window,
                    "ai_daily_stats_sum": pub_n,
                    "delta": (None if pub_n is None
                              else rows_in_window - pub_n),
                    "note": ("two independent counters incremented in the "
                             "same call. ai_daily_stats buckets by calendar "
                             "DATE and ai_requests by rolling timestamp, so "
                             "they are not expected to match exactly. This is "
                             "published to show the split does NOT sum to the "
                             "reach series and is not a correction of it."),
                }
            except Exception:
                out["cross_check"] = None

            if by_platform:
                try:
                    cur.execute(bucket_counts_sql(days, by_platform=True))
                    per = {}
                    for plat, bucket, n in cur.fetchall():
                        per.setdefault(plat, {})[bucket] = int(n or 0)
                    out["by_platform"] = {
                        p: {b: _bucket_result(v, b, sum(v.values()))
                            for b in BUCKETS}
                        for p, v in per.items()
                    }
                except Exception:
                    out["by_platform"] = None
    except Exception as exc:
        out["degraded"] = f"query failed: {type(exc).__name__}"
        out["buckets"] = _split_payload(None, None)
        out["rows_classified"] = None
    finally:
        try:
            c.close()
        except Exception:
            pass

    return jsonify(out), 200
