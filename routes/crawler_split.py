"""Crawler-channel externality split — GET /api/v1/ai/crawler-split.

Public, machine-readable, no auth. See crawler_externality.py for why this
exists and what it does and does not claim. In short: the crawler & citation
channel had no externality filter of any kind, it was our best-looking number,
and the week it doubled was the week we ran a campaign instructing AI
assistants to fetch our surfaces. This endpoint splits that channel by PATH —
metadata (what an instructed fetch reads) against content (what a citation
reads) — and publishes the two separately, never summed.

★ EVERY BUCKET IS MEASURED OR IT SAYS IT IS NOT. A bucket with no rows renders
null with a reason, never 0: `0` reads as "we measured and found none", which is
a confident claim, and it is only defensible for a bucket a collector can see.

★ organic_content HAS BEEN INSTRUMENTED SINCE 2026-08-29 (dchub-frontend #1274,
beaconOrganicCrawl in the live _worker.js). Until 2026-09-05 this endpoint still
withheld it and published "no collector can record a content-page crawl" over a
real 28,706-row count. See _NOT_INSTRUMENTED below. `buckets_reconcile` in the
payload now makes that class of withholding self-announcing.

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
    aligned_rows_sql,
    coverage_sql,
    crawler_population,
    published_series_sql,
    table_span_sql,
)
from routes.brain_ascension_master_shell import _conn

crawler_split_bp = Blueprint("crawler_split", __name__)

# ── Withholding, derived from collector coverage ────────────────────────────
# A bucket renders null-with-a-reason when NO collector can observe it, so an
# unobservable bucket can never render a confident 0.
#
# ★★★ 2026-09-05 — THIS TUPLE HELD "organic_content" AND WAS NOT SHRUNK WHEN
# THE COLLECTOR SHIPPED, and the cost was not a stale comment. dchub-frontend
# #1274 (a55240151, 2026-08-29) added beaconOrganicCrawl() to the LIVE
# _worker.js: it POSTs AI-platform hits on the organic content prefixes to
# /api/ai/track-request, which does not apply is_ai_endpoint(). Rows have been
# landing in ai_requests ever since. But _bucket_result() returns on this tuple
# BEFORE it reads `counts`, so the endpoint computed a real number, threw it
# away, and published "no collector on this channel can record a content-page
# crawl" in its place. Measured live on days=7:
#
#     rows_classified   34,167
#     buckets summed     5,461   (2,372 api + 1,114 metadata + 1,975 unclass.)
#     DISCARDED         28,706   ← organic_content, counted then dropped
#
# Per platform the tell was stark: copilot rendered 142 of 142 and gemini 793
# of 807, but perplexity rendered 40 of 7,597 and chatgpt 443 of 18,791 — the
# two platforms whose traffic IS content crawling were the two the split hid,
# during the largest content crawl this site has ever received.
#
# The tuple is kept and EMPTY rather than deleted: it is the mechanism that
# stops an unobservable bucket rendering 0, and the next bucket added may well
# be unobservable at first. The old comment claimed it was "derived from the
# coverage declaration, not hand-listed" — it was hand-listed, in this repo,
# while the collector that invalidated it shipped in another. Neither repo's CI
# can read the other, so the durable guard is the reconciliation published
# below (buckets_reconcile), not a comment asking the next person to remember.
_NOT_INSTRUMENTED: tuple = ()

_NOT_INSTRUMENTED_REASON = (
    "not instrumented — no collector on this channel can record this bucket "
    "(see population.collector_coverage). This is NOT a measurement of zero; "
    "it is the absence of a measurement.")

# When a bucket's collector was born. A window reaching back before this date
# CANNOT have observed the bucket for its whole span, so a count over such a
# window is a floor, not a measurement of the window — and a ZERO over one says
# nothing at all. Published rather than silently ignored: /api/v1/ai/crawler-split
# accepts days up to 90 and the beacon is younger than that, so the honest
# reading of ?days=90 organic_content differs from ?days=7 by construction.
_COLLECTOR_STARTED = {"organic_content": "2026-08-29"}


def _bucket_result(counts: dict, bucket: str, rows_in_window, window_days=None,
                   today=None):
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

    since = _COLLECTOR_STARTED.get(bucket)
    n = counts.get(bucket)
    if not n:
        if not rows_in_window:
            return {"requests": None,
                    "reason": ("no classifiable rows in the window at all — "
                               "count unknown, not zero")}
        if since:
            # A zero here is ambiguous in a way the other buckets' zeros are
            # not: the collector may simply not have existed for this window.
            return {"requests": None,
                    "reason": (f"no rows in this bucket among {rows_in_window} "
                               f"classified in the window. This bucket's collector "
                               f"began on {since}; a window reaching back before "
                               f"that could not have observed it, so read this as "
                               f"unknown rather than none.")}
        return {"requests": None,
                "reason": (f"no rows in this bucket among {rows_in_window} "
                           "classified in the window")}

    out = {"requests": int(n), "reason": None}
    if since:
        out["collector_started"] = since
        if window_days is not None:
            import datetime as _dt
            began = _dt.date.fromisoformat(since)
            # `today` is injectable ONLY so the tests can pin it. A guard whose
            # verdict depends on the wall clock passes today and fails on a date
            # nobody chose — this repo has shipped that bomb before.
            now = today or _dt.date.today()
            window_start = now - _dt.timedelta(days=int(window_days))
            if window_start < began:
                out["partial_window"] = (
                    f"the requested window opens {window_start.isoformat()}, before "
                    f"this bucket's collector began on {since}. The count covers "
                    f"only the instrumented part of the window and is a FLOOR for "
                    f"the window as a whole — it is not comparable to the other "
                    f"buckets, which were instrumented throughout.")
    return out


def _split_payload(counts, rows_in_window, window_days=None, today=None) -> dict:
    """Assemble the bucket block. `counts` is None when the query failed."""
    return {b: _bucket_result(counts, b, rows_in_window, window_days, today)
            for b in BUCKETS}


def _buckets_reconcile(counts, rows_in_window) -> dict:
    """Publish bucket-sum vs rows_classified so a dropped bucket cannot hide.

    ★ This is the durable guard for the 2026-09-05 defect above. Every row that
    passes the window + roster + real-UA filters gets a bucket — path_bucket_case()
    ends in ELSE 'unclassified', so the four buckets partition the population and
    the sum MUST equal rows_classified. When _NOT_INSTRUMENTED silently withheld
    organic_content the two differed by 28,706 and nothing in the payload said so:
    a reader could only find it by summing the buckets by hand. Now the payload
    does the subtraction itself and names the delta.
    """
    if counts is None or rows_in_window is None:
        return {"buckets_sum": None, "rows_classified": rows_in_window,
                "delta": None,
                "note": "counts unavailable — reconciliation not computed"}
    withheld = sum(int(v or 0) for k, v in counts.items() if k in _NOT_INSTRUMENTED)
    total = sum(int(v or 0) for v in counts.values())
    return {
        "buckets_sum": total,
        "rows_classified": int(rows_in_window),
        "delta": total - int(rows_in_window),
        "withheld_by_policy": withheld,
        "note": ("path_bucket_case() ends in ELSE 'unclassified', so the buckets "
                 "PARTITION the classified population and buckets_sum must equal "
                 "rows_classified. A non-zero delta means a bucket is being "
                 "dropped or withheld before it reaches the payload — which is "
                 "exactly what happened on 2026-09-05 (delta 28,706, the whole "
                 "organic_content bucket). `withheld_by_policy` names the part of "
                 "any delta that is a deliberate _NOT_INSTRUMENTED withholding "
                 "rather than a leak."),
    }


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
        "buckets": _split_payload(None, None, days),
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
            out["buckets"] = _split_payload(counts, rows_in_window, days)
            out["buckets_reconcile"] = _buckets_reconcile(counts, rows_in_window)

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
            except Exception as exc:
                out["reclassifiable_history"] = {
                    "earliest_row": None, "latest_row": None,
                    "rows_total": None,
                    "note": (f"span query failed ({type(exc).__name__}) — the "
                             "reclassifiable history is UNKNOWN, not empty"),
                }

            # The path-LESS counter the public weekly reach is served from,
            # over the same platforms. Published so the gap is VISIBLE.
            #
            # ★ 2026-08-06: this compared the ROLLING days×24h split against a
            # calendar-date sum spanning days+1 DATES, and published the
            # difference as `delta`. Live that read -3,544 (-15%), which any
            # reader would take as the split losing traffic. Measured: the
            # extra calendar day alone is +4,099 of it, and once the windows
            # are aligned the counters agree to +555 (~2%). An uninterpretable
            # number on a disclosure surface is the same defect as a wrong one.
            # Both sides now cover the SAME calendar dates and the SAME
            # exclusions, so `delta` finally measures what a reader assumes it
            # does: divergence between two counters, not window skew.
            try:
                cur.execute(aligned_rows_sql(days))
                aligned = cur.fetchone()
                aligned_n = int(aligned[0] or 0) if aligned else None
                cur.execute(published_series_sql(days))
                pub = cur.fetchone()
                pub_n = int(pub[0] or 0) if pub else None
                out["cross_check"] = {
                    "compared_on": (f"the same {days} calendar dates "
                                    f"(D-{days - 1}..D), both sides"),
                    "ai_requests_rows_aligned": aligned_n,
                    "ai_daily_stats_sum": pub_n,
                    "delta": (None if None in (aligned_n, pub_n)
                              else aligned_n - pub_n),
                    "rows_classified_rolling": rows_in_window,
                    "note": ("two independent counters incremented in the "
                             "same call, now compared over identical calendar "
                             "dates with identical exclusions — so `delta` is "
                             "counter divergence, NOT window skew. Do not "
                             "compare `ai_daily_stats_sum` against "
                             "`rows_classified_rolling` above it: that is a "
                             "calendar sum against a rolling window and the "
                             "difference is dominated by the partial day "
                             "(measured 2026-08-06: -3,544 raw, of which "
                             "+4,099 was one extra calendar date). The split "
                             "is a parallel view and is NOT a correction of "
                             "the published reach series."),
                }
            except Exception as exc:
                out["cross_check"] = {
                    "rows_classified_rolling": rows_in_window,
                    "ai_requests_rows_aligned": None,
                    "ai_daily_stats_sum": None, "delta": None,
                    "note": (f"published-series comparison failed "
                             f"({type(exc).__name__}) — the gap between the "
                             "split and the reach series is UNKNOWN here, "
                             "which is NOT the same as agreeing"),
                }

            if by_platform:
                # ★ 2026-08-06, caught while verifying this endpoint in prod:
                # this block used to swallow its failure into a bare
                # `by_platform: null` with no reason, and at days=30 the
                # grouped scan does exceed the 8s timeout. A consumer read
                # that null as zeros and published "claude: 0 instructed, 0
                # data" for a window whose 7d subset held 6,779 — the exact
                # not-measured-rendered-as-measured defect this endpoint
                # exists to prevent, one level up. Every other null here says
                # why; this one now does too.
                out["by_platform_reason"] = None
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
                except Exception as exc:
                    out["by_platform"] = None
                    out["by_platform_reason"] = (
                        f"per-platform breakdown unavailable "
                        f"({type(exc).__name__}) — this is a MISSING "
                        "breakdown, not an empty one. Do not render its "
                        "absence as zeros; the totals in `buckets` above are "
                        "unaffected and still measured.")
    except Exception as exc:
        out["degraded"] = f"query failed: {type(exc).__name__}"
        out["buckets"] = _split_payload(None, None, days)
        out["rows_classified"] = None
        out["buckets_reconcile"] = _buckets_reconcile(None, None)
    finally:
        try:
            c.close()
        except Exception:
            pass

    return jsonify(out), 200
