"""
routes/answer_prober.py — the ANSWER prober (2026-09-05).
=========================================================

WHAT IS MISSING THAT THIS FILLS
-------------------------------
ai_surface_sentinel.py already fetches live bodies cache-bypassed and diffs
them against canon. It audits thirteen surfaces — llms.txt, AGENTS.md, the
.well-known manifests, openapi.json, robots.txt, /connect, /ai — and its test
is "does this body still contain a number we know is stale?"

Those are CLAIM surfaces: prose and manifests that DESCRIBE DC Hub.

Not one of the six surfaces that served wrong data on 2026-09-04 is in that
list, because none of them is prose. They are ANSWER surfaces — an agent asks
a question and gets data back:

    /dcpi/og.svg                      334 markets · 25 BUILD   (live: 327 · 24)
    /api/v1/agent/index               dcpi_scored_markets 334
    /api/v1/alive                     markets_scored 334
    /api/v1/reports/monthly.json      markets_scored 334
    /api/v1/open-data/*.csv           334 rows, 7 of them retired
    /api/v1/industry/pulse            80 markets / 14 BUILD / 63 AVOID
    /poe/query                        seven-week-old verdicts, two of them wrong

Every one was reachable for weeks. CI was green, the sentinel was green, and
the brain saw nothing — because "is our documentation numerically honest?" and
"is our answer correct?" are different questions and only the first was asked.

WHAT THIS ASKS
--------------
For each answer surface: fetch it from OUTSIDE, over its real transport, and
compare what it returned against what the canonical source returns RIGHT NOW.

Three rules, each of which is a bug this session actually shipped:

  1. EXPECTATIONS ARE DERIVED LIVE, NEVER PINNED. A probe that compares against
     a literal is the literal that rots — exactly the defect it exists to
     catch. Every expectation here is read from /api/v1/dcpi/scores at probe
     time. There is no number in this file.

  2. AN EXTRACTOR THAT FINDS NOTHING IS A FAILURE, NOT A PASS. A regex that
     stops matching because the page was rewritten must go RED, or the probe
     quietly becomes a no-op and reports success forever.

  3. PROBE FROM OUTSIDE, CACHE-BUSTED. Every one of these bugs would have
     passed an in-process assertion: the SQL the endpoint ran was wrong, so any
     check sharing that code path agreed with it. Only the served bytes tell
     the truth. This module therefore talks to the public origin over HTTP even
     though it runs inside that origin.

NOT A DUPLICATE of ai_surface_sentinel: different surfaces, different question,
different comparison. The sentinel scans a body for known-bad text; this one
asserts equality between two live reads. Neither subsumes the other.

  GET /api/v1/admin/answer-probe        → scorecard (read-only, admin-gated)
  GET /api/v1/admin/answer-probe?probe= → run one probe by key

Kill: ANSWER_PROBER_ENABLED=0.
"""
from __future__ import annotations

import csv
import hmac
import io
import json
import os
import random
import re

import requests
from flask import Blueprint, jsonify, request

answer_prober_bp = Blueprint("answer_prober", __name__)

#: Probed over the public hostname on purpose — through the edge, as an agent
#: sees it. See rule 3 in the module docstring.
_PUBLIC = "https://dchub.cloud"
_TIMEOUT = 25


def prober_enabled() -> bool:
    """True unless ANSWER_PROBER_ENABLED is explicitly set falsy.

    One reader for both gate sites (endpoint + scheduler), so the two cannot
    drift apart — the shape ai_surface_sentinel.sentinel_cron_enabled uses, and
    for the same reason.
    """
    raw = str(os.environ.get("ANSWER_PROBER_ENABLED", "")).strip().lower()
    return raw not in ("0", "false", "no", "off")


def _admin_ok() -> bool:
    ak = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    ik = (os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    ga = (request.headers.get("X-Admin-Key") or request.args.get("admin_key") or "").strip()
    gi = (request.headers.get("X-Internal-Key") or "").strip()
    if ak and ga and hmac.compare_digest(ga, ak):
        return True
    if ik and gi and hmac.compare_digest(gi, ik):
        return True
    return False


def _fetch(path, method="GET", payload=None, headers=None, timeout=_TIMEOUT):
    """Fetch a public path cache-busted. Returns (status, body_text).

    The cache-buster is not decoration: a probe that reads an edge-cached body
    is measuring what Cloudflare remembered, not what the origin computed, and
    would have reported the pre-fix numbers as correct for the whole TTL.

    `requests`, not urllib — required by regression_lint, and independently
    right here. Cloudflare 1010s the bare urllib user-agent prefix before the
    worker ever runs, and this module's whole job is fetching through that
    edge. A probe that 403s on its own infrastructure would report every
    surface `unreachable` and teach the operator to ignore it.
    """
    sep = "&" if "?" in path else "?"
    url = f"{_PUBLIC}{path}{sep}_ap={str(random.random())[2:9]}"
    req_headers = {"Cache-Control": "no-cache",
                   "User-Agent": "dchub-answer-prober/1"}
    for k, v in (headers or {}).items():
        req_headers[k] = v
    try:
        r = requests.request(method, url, json=payload, headers=req_headers,
                             timeout=timeout)
        return r.status_code, r.text
    except requests.RequestException as e:
        # Surfaced as `unreachable` by the callers, never as agreement.
        return 0, f"{type(e).__name__}: {str(e)[:120]}"


# ── the canonical answer, read live ──────────────────────────────────────────

class _CanonUnavailable(RuntimeError):
    """Raised when the source of truth itself could not be read.

    Reported as `inconclusive`, never as `clean`. A prober that cannot reach
    its own baseline knows nothing, and saying "no drift" there is the same
    class of lie it exists to catch.
    """


def _canon_counts() -> dict:
    """markets / build / avoid, from the scores API — the same universe /dcpi
    serves. Read at probe time; nothing here is remembered between runs."""
    out = {}
    for key, qs in (("markets", ""),
                    ("build", "&verdict=BUILD"),
                    ("avoid", "&verdict=AVOID")):
        code, body = _fetch(f"/api/v1/dcpi/scores?limit=1000{qs}")
        if code != 200:
            raise _CanonUnavailable(f"scores?{qs or 'all'} -> HTTP {code}")
        try:
            n = json.loads(body).get("_total_available")
        except Exception as e:
            raise _CanonUnavailable(f"scores?{qs or 'all'} unparseable: {e}")
        if not isinstance(n, int):
            raise _CanonUnavailable(f"_total_available missing for {key}")
        out[key] = n
    return out


def _canon_market(slug: str) -> dict:
    """The canonical row for one market slug."""
    code, body = _fetch(f"/api/v1/dcpi/scores/{slug}")
    if code != 200:
        raise _CanonUnavailable(f"scores/{slug} -> HTTP {code}")
    d = json.loads(body)
    row = d if "verdict" in d else (d.get("score") or d)
    if not row.get("verdict"):
        raise _CanonUnavailable(f"scores/{slug} carries no verdict")
    return row


# ── probe results ────────────────────────────────────────────────────────────

def _result(surface, field, observed, expected, note=None):
    """One comparison. `observed is None` means the extractor found nothing,
    which is a FAILURE — see rule 2 in the module docstring."""
    if observed is None:
        verdict = "extractor-blind"
    elif observed == expected:
        verdict = "agrees"
    else:
        verdict = "disagrees"
    r = {"surface": surface, "field": field, "observed": observed,
         "expected": expected, "verdict": verdict}
    if note:
        r["note"] = note
    return r


def _unreachable(surface, field, detail):
    return {"surface": surface, "field": field, "observed": None,
            "expected": None, "verdict": "unreachable", "note": detail}


# ── the probes ───────────────────────────────────────────────────────────────
#
# Each returns a list of comparisons. Keyed by the surface that was WRONG on
# 2026-09-04 — a proven-reachable failure mode is worth more than a
# hypothetical one.

def _probe_dcpi_page(canon):
    code, body = _fetch("/dcpi")
    if code != 200:
        return [_unreachable("dcpi_page", "markets", f"HTTP {code}")]
    m = re.search(r"across\s+([\d,]+)\s+data center markets", body)
    return [_result("dcpi_page", "markets",
                    int(m.group(1).replace(",", "")) if m else None,
                    canon["markets"])]


def _probe_og_card(canon):
    code, body = _fetch("/dcpi/og.svg")
    if code != 200:
        return [_unreachable("og_card", "markets", f"HTTP {code}")]
    m = re.search(r"(\d[\d,]*)\s+markets scored daily\s*·\s*(\d+)\s+rated BUILD", body)
    return [
        _result("og_card", "markets",
                int(m.group(1).replace(",", "")) if m else None, canon["markets"]),
        _result("og_card", "build",
                int(m.group(2)) if m else None, canon["build"]),
    ]


def _probe_agent_index(canon):
    code, body = _fetch("/api/v1/agent/index?domain=dcpi")
    if code != 200:
        return [_unreachable("agent_index", "markets", f"HTTP {code}")]
    try:
        v = json.loads(body).get("coverage", {}).get("dcpi_scored_markets")
    except Exception:
        v = None
    return [_result("agent_index", "markets", v, canon["markets"])]


def _probe_alive(canon):
    """★ A surface that declares itself NOT READY is not a surface that is wrong.

    /alive fills its DB-backed blocks inside try/except, so while a replica is
    still warming it returns `"warming": true` with `"dcpi": {}` — 200, well
    formed, empty. Scored naively that reads as extractor-blind, and the whole
    scorecard goes red for a few minutes after every deploy and every time a
    replica cycles.

    Measured on this prober's FIRST live run, which is how this was found:
    eight reads over seven minutes, `warming` true on two of them and the dcpi
    block empty on exactly those two. A probe that cries wolf after every
    deploy is one the operator learns to ignore, which is the same failure as
    having no probe — so `warming` is its own verdict, and it does not make the
    run red.

    The distinction that matters: EMPTY WHILE WARMING is honest; empty while
    claiming to be ready is not, and still reports extractor-blind. A warming
    flag excuses ABSENCE, never a wrong number.
    """
    code, body = _fetch("/api/v1/alive")
    if code != 200:
        return [_unreachable("alive", "markets", f"HTTP {code}")]
    try:
        d = json.loads(body)
    except Exception:
        return [_result("alive", "markets", None, canon["markets"])]
    v = (d.get("dcpi") or {}).get("markets_scored")
    if v is None and d.get("warming"):
        return [{"surface": "alive", "field": "markets", "observed": None,
                 "expected": canon["markets"], "verdict": "warming",
                 "note": "replica reports warming=true and has not filled its "
                         "DB-backed blocks yet — not a drift signal"}]
    return [_result("alive", "markets", v, canon["markets"])]


def _probe_monthly_report(canon):
    code, body = _fetch("/api/v1/reports/monthly.json")
    if code != 200:
        return [_unreachable("monthly_report", "markets", f"HTTP {code}")]
    try:
        v = json.loads(body).get("markets_scored")
    except Exception:
        v = None
    return [_result("monthly_report", "markets", v, canon["markets"])]


def _probe_industry_pulse(canon):
    """★ Null here is CORRECT, not drift.

    This endpoint serves a per-process cache. On a replica that has not warmed
    yet it now reports nulls, withholds its citation block, and starts a
    recompute — which is the honest answer to "how many did you measure?" and
    is exactly what #3865 built. Scoring that as drift would train the operator
    to ignore this probe, and the failure it must never miss is the OLD
    behaviour: confident numbers that disagree with canon.
    """
    code, body = _fetch("/api/v1/industry/pulse")
    if code != 200:
        return [_unreachable("industry_pulse", "markets", f"HTTP {code}")]
    try:
        d = json.loads(body)
        v = (d.get("metrics", {}).get("dcpi_verdicts") or {}).get("markets_scored")
        citable = "citation" in d
    except Exception:
        return [_result("industry_pulse", "markets", None, canon["markets"])]
    if v is None:
        return [{"surface": "industry_pulse", "field": "markets",
                 "observed": None, "expected": canon["markets"],
                 "verdict": "agrees" if not citable else "disagrees",
                 "note": ("cold replica reporting nulls and withholding citation "
                          "— honest" if not citable else
                          "NULL metrics served WITH a citation block")}]
    return [_result("industry_pulse", "markets", v, canon["markets"])]


def _probe_poe_answer(canon):
    """★ The one that is an ANSWER, not a count.

    Asks in the words a person uses, about a market whose slug was RETIRED, and
    checks the reply against the canonical row it should resolve to. Before
    #3841 this returned a row frozen seven weeks earlier, with a verdict that
    disagreed with the live market, under the words "daily-recomputed".
    """
    question = "should i build in northern virginia"
    code, body = _fetch("/poe/query", method="POST", payload={"query": question})
    if code != 200:
        return [_unreachable("poe_answer", "verdict", f"HTTP {code}")]
    m = re.search(r'"text":\s*"(.*?)"\}', body, re.S)
    text = json.loads('"' + m.group(1) + '"') if m else ""
    slug_m = re.search(r"dchub\.cloud/dcpi/([a-z0-9-]+)", text)
    if not slug_m:
        return [_result("poe_answer", "market_slug", None, "a canonical slug")]
    slug = slug_m.group(1)
    try:
        row = _canon_market(slug)
    except _CanonUnavailable as e:
        return [_unreachable("poe_answer", "verdict", str(e))]
    vm = re.search(r"Verdict:\s*\*\*(\w+)", text)
    em = re.search(r"Excess-Power score:\s*\*\*([\d.]+)", text)
    # ★ The cited slug is checked SEPARATELY, and this leg is the load-bearing
    # one. The two comparisons below ask "does the reply match the row for the
    # slug it cited?" — which a stale reply can satisfy by citing the stale
    # slug and quoting it faithfully. Today the per-slug API resolves aliases,
    # so that self-consistency would still be caught; but then this probe would
    # be relying on a behaviour of a DIFFERENT endpoint to see its own defect,
    # and would go quiet the day that behaviour changed.
    #
    # A retired twin is never a legitimate answer, whatever numbers accompany
    # it. Read from the alias table rather than listed here, so retiring
    # another market extends this check for free.
    try:
        from util.market_aliases import REDUNDANT_TWIN_SLUGS
        cites_retired = slug in REDUNDANT_TWIN_SLUGS
    except Exception:
        cites_retired = None
    return [
        _result("poe_answer", "cites_published_market",
                (not cites_retired) if cites_retired is not None else None,
                True,
                note=f"answer cited /dcpi/{slug}"),
        _result("poe_answer", "verdict",
                vm.group(1) if vm else None, str(row.get("verdict")),
                note=f"asked {question!r}, answered as {slug}"),
        _result("poe_answer", "excess_power_score",
                float(em.group(1)) if em else None,
                round(float(row["excess_power_score"]), 1)
                if row.get("excess_power_score") is not None else None),
    ]


def _probe_open_data_markets(canon):
    """Key-gated, so it is skipped rather than failed when no key is present —
    an unrunnable probe must not read as a clean one."""
    key = (os.environ.get("DCHUB_API_KEY") or "").strip()
    if not key:
        return [{"surface": "open_data_markets", "field": "rows",
                 "observed": None, "expected": canon["markets"],
                 "verdict": "skipped", "note": "no DCHUB_API_KEY in env"}]
    code, body = _fetch("/api/v1/open-data/dcpi-markets.csv",
                        headers={"X-API-Key": key})
    if code != 200:
        return [_unreachable("open_data_markets", "rows", f"HTTP {code}")]
    rows = list(csv.DictReader(
        io.StringIO("".join(l for l in body.splitlines(keepends=True)
                            if not l.startswith("#") and l.strip()))))
    return [_result("open_data_markets", "rows", len(rows), canon["markets"])]


#: Public path per surface. Used ONLY to give a finding a stable `url`, which
#: is half of brain_findings' (issue, url) dedup key — so a surface that stays
#: wrong updates one row instead of minting a new one every run.
_SURFACE_URLS = {
    "dcpi_page": "https://dchub.cloud/dcpi",
    "og_card": "https://dchub.cloud/dcpi/og.svg",
    "agent_index": "https://dchub.cloud/api/v1/agent/index?domain=dcpi",
    "alive": "https://dchub.cloud/api/v1/alive",
    "monthly_report": "https://dchub.cloud/api/v1/reports/monthly.json",
    "industry_pulse": "https://dchub.cloud/api/v1/industry/pulse",
    "poe_answer": "https://dchub.cloud/poe/query",
    "open_data_markets": "https://dchub.cloud/api/v1/open-data/dcpi-markets.csv",
}

#: Verdicts that become a brain finding.
#:
#: `disagrees` — the surface answered, and the answer is wrong.
#: `extractor-blind` — the probe could not find the value at all, which is
#:   either a rewritten surface or a retired probe. Both need a human.
#:
#: Deliberately EXCLUDED:
#:   `warming`  a replica that says it is not ready yet is not wrong;
#:   `skipped`  an unrunnable probe is not a failing one;
#:   `unreachable` a surface being DOWN is a liveness signal other monitors
#:              already own. Writing findings for it would make this detector
#:              fire on every deploy blip and dilute the ones that mean
#:              "we are serving a wrong answer" — the only thing it exists to
#:              say.
_FINDING_VERDICTS = ("disagrees", "extractor-blind")


def write_findings(out: dict) -> dict:
    """Upsert each wrong answer into brain_findings, deduped on (issue, url).

    Mirrors ai_surface_sentinel._write_findings — same canonical writer, which
    handles the UNIQUE(issue, url) schema trap that makes a hand-rolled INSERT
    fail silently.

    ★ An INCONCLUSIVE run writes NOTHING. If canon could not be read, every
    comparison is absent rather than false, and minting findings from that
    would manufacture alarms out of an outage in the baseline — the same class
    of lie this module exists to catch.

    Informational only: no surface is written, nothing is auto-fixed. Never
    raises.
    """
    if out.get("verdict") == "inconclusive":
        return {"written": 0, "skipped": "inconclusive run — canon unreadable"}
    try:
        from main import get_pg_connection, return_pg_connection
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception as e:
        return {"written": 0, "error": f"import: {str(e)[:80]}"}
    conn = None
    written = 0
    try:
        conn = get_pg_connection()
        with conn.cursor() as cur:
            for c in out.get("comparisons", []):
                if c.get("verdict") not in _FINDING_VERDICTS:
                    continue
                surface = c.get("surface")
                issue = f"answer_drift:{surface}:{c.get('field')}"
                detail = (f"{surface} {c.get('field')}: observed="
                          f"{c.get('observed')!r} expected={c.get('expected')!r} "
                          f"({c.get('verdict')})")
                if c.get("note"):
                    detail += f" — {c['note']}"
                try:
                    upsert_brain_finding(
                        cur, issue=issue,
                        url=_SURFACE_URLS.get(surface, "https://dchub.cloud/"),
                        detail=detail, detector="answer_prober")
                    written += 1
                except Exception:
                    pass
        conn.commit()
    except Exception as e:
        return {"written": written, "error": str(e)[:120]}
    finally:
        if conn is not None:
            try:
                return_pg_connection(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
    return {"written": written}


_PROBES = {
    "dcpi_page": _probe_dcpi_page,
    "og_card": _probe_og_card,
    "agent_index": _probe_agent_index,
    "alive": _probe_alive,
    "monthly_report": _probe_monthly_report,
    "industry_pulse": _probe_industry_pulse,
    "poe_answer": _probe_poe_answer,
    "open_data_markets": _probe_open_data_markets,
}


def run_answer_probe(only: str | None = None) -> dict:
    """Every probe, or one by key. Read-only; writes nothing anywhere."""
    try:
        canon = _canon_counts()
    except _CanonUnavailable as e:
        return {"ok": False, "verdict": "inconclusive",
                "reason": f"canonical source unreadable: {e}",
                "note": ("No comparison was made. This is NOT a clean run — a "
                         "prober that cannot read its own baseline knows "
                         "nothing."),
                "comparisons": []}
    names = [only] if only else list(_PROBES)
    unknown = [n for n in names if n not in _PROBES]
    if unknown:
        return {"ok": False, "verdict": "inconclusive",
                "reason": f"unknown probe(s): {unknown}",
                "known": sorted(_PROBES), "comparisons": []}
    comparisons = []
    for name in names:
        try:
            comparisons += _PROBES[name](canon)
        except Exception as e:
            comparisons.append(_unreachable(name, "*", f"{type(e).__name__}: {str(e)[:90]}"))
    counts: dict = {}
    for c in comparisons:
        counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
    bad = counts.get("disagrees", 0) + counts.get("extractor-blind", 0)
    return {
        "ok": bad == 0,
        "verdict": ("drift" if bad
                    else "degraded" if counts.get("unreachable")
                    # A run held back only by a warming replica is not clean and
                    # not drift: it is a run that did not fully happen yet.
                    else "warming" if counts.get("warming")
                    else "clean"),
        "canon": canon,
        "summary": counts,
        # worst first, so the scorecard opens on what is wrong
        "comparisons": sorted(
            comparisons,
            key=lambda c: {"disagrees": 0, "extractor-blind": 1, "unreachable": 2,
                           "warming": 3, "skipped": 4, "agrees": 5}.get(c["verdict"], 6)),
    }


@answer_prober_bp.route("/api/v1/admin/answer-probe", methods=["GET"])
def answer_probe():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 403
    if not prober_enabled():
        return jsonify(ok=False, verdict="disabled",
                       reason="ANSWER_PROBER_ENABLED is off"), 200
    return jsonify(run_answer_probe(request.args.get("probe") or None)), 200
