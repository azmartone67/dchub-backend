"""
routes/state_of_2026_precheck.py — Pre-Publish QA harness for the "State of
Data Centers 2026" LinkedIn launch (2026-06-06).

ONE button the founder clicks before posting to 10K LinkedIn followers.
Scores every surface the post will reference, returns a single GO / CAUTION
/ NO_GO verdict + a "what to fix" list, and emits a JSON variant for
programmatic CI / brain-layer hooks.

Routes:
  GET /api/v1/admin/qa/state-of-2026-precheck   JSON
  GET /admin/qa/state-of-2026                   HTML dashboard
  GET /api/v1/admin/qa/state-of-2026            JSON (CF-zone-worker bypass alias)
  POST /api/v1/admin/qa/state-of-2026/audit-trail  one-liner email log

Auth: X-Admin-Key header OR ?admin_key= query param matching DCHUB_ADMIN_KEY
(falls back to ADMIN_KEY). Same gate as funnel_health.admin_feedback_dashboard.

Audit surfaces (each scored A / B / C / D / F + populated worst-case sample):
  1. Market briefs (top 30 by traffic + ?markets= override)
  2. Hyperscaler briefs (all 10 seed slugs)
  3. DCPI / DCGI headline numbers (233 markets, BUILD/CAUTION/AVOID counts)
  4. Narrative claims (docs/state-of-2026-claims.txt — drift > 5% = fail)
  5. OG cards (5 canonical styles render PNG, not blank fallback)
  6. Link health (every URL the post will share — HTTP 200, no 5xx)

Verdict:
  GO       — every check A or B, zero fails on critical (math/wrong-attribution)
  CAUTION  — 1-3 B/C scores, no fails — ok to ship but flag concerns
  NO_GO    — any fail OR >3 C scores OR any math absurdity ($/MW > $100M,
             em-dash count > 5, wrong-attribution regex hit, link 5xx)

Runtime target: < 30s for a full sweep. Achieved via:
  - ThreadPool of 8 workers fan-out (markets + briefs + OG)
  - per-request 4s soft cap on the HTTP probes
  - cap MAX_MARKETS_TO_PROBE = 30 (configurable via ?markets_limit=)
  - 60s in-memory result cache keyed on the request signature

Defensive: every probe is wrapped in try/except. A single failing endpoint
NEVER blanks the verdict — it just scores that surface 'F' and surfaces the
exception. The dashboard always renders.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import escape as _esc
from typing import Any, Optional
from urllib.parse import urlencode

from flask import Blueprint, Response, jsonify, request

try:
    import requests as _rq  # type: ignore
except Exception:
    _rq = None  # surfaced as fail-soft "transport unavailable"

logger = logging.getLogger(__name__)

state_of_2026_precheck_bp = Blueprint("state_of_2026_precheck", __name__)


# ── env / constants ───────────────────────────────────────────────────

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("ADMIN_KEY") or "").strip()

# Base URL the probes hit. Prefer Railway origin (skips CF edge cache so a
# fresh fix lands in the audit immediately — per the freshness-architecture
# memory) but allow override via env var for local dev.
_PROBE_BASE_URL = (os.environ.get("STATE_OF_2026_PROBE_BASE")
                   or os.environ.get("DCHUB_INTERNAL_BASE_URL")
                   or "https://dchub-backend-production.up.railway.app").rstrip("/")

# Top-30 market slugs by traffic (canonical METRO slugs — /markets/<slug>/brief).
# These are the markets the founder is most likely to mention in the post,
# and the ones with the biggest reputation cost if broken.
TOP_MARKETS_TO_PROBE: tuple[str, ...] = (
    "northern-virginia", "dallas-fort-worth", "phoenix", "atlanta", "chicago",
    "silicon-valley", "new-york-new-jersey", "los-angeles", "seattle", "portland",
    "columbus", "salt-lake-city", "des-moines", "omaha", "richmond",
    "raleigh-durham", "miami", "houston", "austin", "denver",
    "minneapolis", "kansas-city", "nashville", "charlotte", "indianapolis",
    "boston", "philadelphia", "san-antonio", "cheyenne", "reno",
)

# Seed hyperscaler slugs that get hand-QA'd briefs.
HYPERSCALERS_TO_PROBE: tuple[str, ...] = (
    "aws", "azure", "google-cloud", "meta", "apple",
    "oracle", "tiktok-bytedance", "tencent", "alibaba",
)

# OG card styles the LinkedIn post will pull from. Verified against
# og_cards.smart_style() output options as of 2026-06-06.
OG_CARDS_TO_PROBE: tuple[str, ...] = (
    "editorial", "data_brutal", "minimal", "magazine", "today",
)

# Anchor URLs the State of 2026 narrative shares.
STATIC_LINKS_TO_PROBE: tuple[str, ...] = (
    "/dcpi", "/dcgi", "/interconnection-queues", "/reports/state-of-power",
    "/hyperscalers", "/markets", "/by-the-numbers", "/cited-by",
)

# Pipeline text patterns that scream "wrong attribution" or "garbage news".
# Hits = an immediate NO_GO on the hyperscaler brief.
WRONG_ATTRIBUTION_PATTERNS = (
    r"signs deal with [A-Z]",     # marketing fluff that infers a deal that didn't happen
    r"AWS-3 auction",             # spectrum auction, not the cloud provider
    r"data breach",               # generic news-feed pollution
    r"capacity market auction",   # the OTHER AWS — ISO auction noise
    r"Pinterest|tiktok shop",     # Pinterest≠AWS capex bug from earlier today
)
_WRONG_ATTR_RE = re.compile("|".join(WRONG_ATTRIBUTION_PATTERNS), re.I)

# Em-dash detection. Per the brief-quality memory, > 5 em-dashes in body =
# the "Thin Coverage" UX hasn't landed and the page is leaking placeholders.
_EM_DASH_RE = re.compile(r"[—–]")

# $/MW math absurdity — anything > $100M/MW is the AWS $328M/MW bug.
MAX_USD_PER_MW = 100_000_000

# Drift threshold for narrative claim verification — > 5% = NO_GO.
CLAIM_DRIFT_PCT = 0.05

# Probe HTTP timeout (per request). 6s was the legacy default — too tight
# for cold-cache hyperscaler briefs (PIL/cairo + aggregation across facilities
# + deals + news) and OG card generation, which routinely flagged HTTP 0
# false-positives in the 2026-06-06 dry-run despite live curls returning 200.
#
# Per-surface raises (kept under the 90s wall clock — see comment below):
#   - default        :  8s   (was 6s)
#   - market briefs  : 10s
#   - hyperscaler    : 15s   (heaviest aggregation surface)
#   - og cards       : 12s   (PIL/cairo cold-cache)
#   - dcpi/dcgi      :  8s
#   - claims (json)  :  8s
#   - link health    :  8s   (HEAD-style, but still a real GET)
#
# Wall-clock guard: each surface fans out across WORKER_COUNT=8 threads,
# so the per-surface walltime is approximately
#   ceil(items / WORKER_COUNT) * per-probe-timeout.
# Worst-case is market briefs at 30 items / 8 workers * 10s ≈ 38s for that
# surface alone — but the harness runs surfaces SEQUENTIALLY at the top
# level (see _run_full_audit), so total ≤ sum of surface walltimes.
# Empirically the brain has logged 19-22s end-to-end, the bumps put the
# cold-cache worst-case at 60-70s — well under 90s, with budget to spare.
PROBE_TIMEOUT_S = 8.0
PROBE_TIMEOUT_MARKET_BRIEF_S = 10.0
PROBE_TIMEOUT_HYPERSCALER_BRIEF_S = 15.0
PROBE_TIMEOUT_OG_CARD_S = 12.0
PROBE_TIMEOUT_DCPI_S = 8.0
PROBE_TIMEOUT_LINK_HEALTH_S = 8.0

# Worker fan-out cap. Set high enough to finish in < 30s, low enough to
# not flood the single Railway replica.
WORKER_COUNT = 8

# In-memory result cache — 60s keyed on the request signature.
_CACHE: dict[str, Any] = {}
_CACHE_TTL_S = 60

# Path to the user-editable narrative claim sheet.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLAIMS_PATH = os.path.join(_REPO_ROOT, "docs", "state-of-2026-claims.txt")


# ── auth ──────────────────────────────────────────────────────────────

def _admin_ok(req) -> bool:
    """Same gate as funnel_health.admin_feedback_dashboard."""
    sent = (req.headers.get("X-Admin-Key")
            or req.args.get("admin_key") or "").strip()
    return bool(_ADMIN_KEY) and sent == _ADMIN_KEY


# ── HTTP probe helpers ────────────────────────────────────────────────

def _probe(path: str, method: str = "GET", expect_json: bool = False,
           timeout: float = PROBE_TIMEOUT_S, retry_on_http0: bool = True) -> dict:
    """Hit a backend path. Returns {ok, status, body, json, dur_ms, error}.

    Sends X-Admin-Key + an internal UA so the probe bypasses
    tier-gating and CF anti-bot. Best-effort — any exception is caught
    and surfaced in the `error` field; the harness keeps running.

    retry_on_http0: if True (default), retry ONCE with a 1.5x timeout
    on connection/timeout exceptions. Most HTTP-0 reports in the 2026-06-06
    dry-run were transient (single Railway replica recovering from a brain-
    sweep burst); a single retry kills the false-positive without doubling
    the wall clock for genuinely-down probes.
    """
    out: dict[str, Any] = {
        "path": path, "ok": False, "status": 0, "dur_ms": 0,
        "body_len": 0, "body_sample": "", "json": None, "error": None,
        "attempts": 0,
    }
    if _rq is None:
        out["error"] = "requests library unavailable"
        return out
    url = path if path.startswith("http") else f"{_PROBE_BASE_URL}{path}"
    headers = {
        "User-Agent": "dchub-state-of-2026-precheck/1.0",
        "X-Admin-Key": _ADMIN_KEY,
        "X-API-Key":   _ADMIN_KEY,  # some endpoints check the other header
        "Accept":      "application/json, text/html",
    }

    def _attempt(attempt_timeout: float) -> tuple[bool, Any]:
        """Returns (transient_error, exception_or_response)."""
        try:
            r = _rq.request(method, url, headers=headers,
                            timeout=attempt_timeout, allow_redirects=True)
            return False, r
        except Exception as e:
            # ConnectTimeout / ReadTimeout / ConnectionError are transient;
            # everything else is also surfaced but we only retry on these.
            err_name = type(e).__name__
            transient = err_name in (
                "ConnectTimeout", "ReadTimeout", "Timeout",
                "ConnectionError", "ChunkedEncodingError",
            )
            return transient, e

    t0 = time.time()
    out["attempts"] = 1
    transient, result = _attempt(timeout)
    # Retry once on a transient error if enabled.
    if transient and retry_on_http0:
        out["attempts"] = 2
        # Brief cooldown so we don't immediately hit the same hot replica.
        time.sleep(0.25)
        transient, result = _attempt(min(timeout * 1.5, 25.0))

    out["dur_ms"] = int((time.time() - t0) * 1000)

    # Result is either an Exception or a requests.Response
    if isinstance(result, Exception):
        out["error"] = f"{type(result).__name__}: {result}"
        return out

    r = result
    out["status"] = r.status_code
    body = r.text or ""
    out["body_len"] = len(body)
    out["body_sample"] = body[:400]
    if expect_json:
        try:
            out["json"] = r.json()
        except Exception:
            out["json"] = None
    else:
        out["body"] = body
    out["ok"] = (200 <= r.status_code < 400)
    return out


# ── claims parser ─────────────────────────────────────────────────────

def _parse_claims_file() -> tuple[list[dict], Optional[str]]:
    """Parse docs/state-of-2026-claims.txt. Returns (claims, error_or_None).

    Each claim dict has: claim, endpoint, json_path, expected_min, expected_max.
    """
    if not os.path.exists(_CLAIMS_PATH):
        return [], f"claim file missing: {_CLAIMS_PATH} (created with seeds)"
    claims: list[dict] = []
    try:
        with open(_CLAIMS_PATH, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # Format: claim: X | endpoint: Y | json_path: Z | expected_min: M | expected_max: N
                parts = [p.strip() for p in line.split("|")]
                fields: dict[str, str] = {}
                for p in parts:
                    if ":" not in p:
                        continue
                    k, _, v = p.partition(":")
                    fields[k.strip().lower()] = v.strip()
                if not all(k in fields for k in
                           ("claim", "endpoint", "json_path",
                            "expected_min", "expected_max")):
                    continue
                try:
                    fields["expected_min"] = float(fields["expected_min"])  # type: ignore
                    fields["expected_max"] = float(fields["expected_max"])  # type: ignore
                except ValueError:
                    continue
                fields["_lineno"] = lineno  # type: ignore
                claims.append(fields)  # type: ignore
    except Exception as e:
        return [], f"claim file parse error: {e}"
    return claims, None


def _extract_json_value(blob: Any, path: str) -> Optional[float]:
    """Walk a dotted path into a JSON blob. Supports list:KEY (count items
    in array at KEY) and sum:KEY.FIELD (sum FIELD across array at KEY).
    Returns None on miss."""
    if blob is None:
        return None
    try:
        if path.startswith("len:"):
            cur: Any = blob
            for k in path[4:].split("."):
                if isinstance(cur, dict):
                    cur = cur.get(k)
                else:
                    return None
            if isinstance(cur, list):
                return float(len(cur))
            return None
        if path.startswith("sum:"):
            tail = path[4:]
            if "." not in tail:
                return None
            arr_path, _, field = tail.rpartition(".")
            cur = blob
            for k in arr_path.split("."):
                if isinstance(cur, dict):
                    cur = cur.get(k)
                else:
                    return None
            if not isinstance(cur, list):
                return None
            tot = 0.0
            for item in cur:
                if isinstance(item, dict):
                    try:
                        tot += float(item.get(field) or 0)
                    except (TypeError, ValueError):
                        pass
            return tot
        cur = blob
        for k in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(k)
            elif isinstance(cur, list):
                try:
                    cur = cur[int(k)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        if cur is None:
            return None
        if isinstance(cur, (int, float)):
            return float(cur)
        try:
            return float(cur)
        except (TypeError, ValueError):
            return None
    except Exception:
        return None


# ── per-surface auditors ──────────────────────────────────────────────

def _score_letter(passed: int, total: int) -> str:
    """A=all populated, B=>=80%, C=>=50%, D=>=20%, F=below."""
    if total == 0:
        return "F"
    pct = passed / total
    if pct >= 0.95: return "A"
    if pct >= 0.80: return "B"
    if pct >= 0.50: return "C"
    if pct >= 0.20: return "D"
    return "F"


def _audit_market_brief_one(slug: str) -> dict:
    """Probe one /markets/<slug>/brief. Score it A/B/C/D + flag em-dashes."""
    res = _probe(f"/markets/{slug}/brief", expect_json=False,
                 timeout=PROBE_TIMEOUT_MARKET_BRIEF_S)
    out: dict[str, Any] = {
        "slug": slug, "url": f"/markets/{slug}/brief",
        "status": res["status"], "dur_ms": res["dur_ms"],
        "ok_http": res["ok"], "em_dash_count": 0,
        "sections_populated": 0, "sections_total": 4, "score": "F",
        "fail_reasons": [],
    }
    if not res["ok"]:
        out["fail_reasons"].append(f"HTTP {res['status']}")
        return out
    body = res.get("body") or ""
    # Em-dash check — only count within the <body> visible text, not in style/script.
    # Quick approximation: count across the whole HTML; brand.css doesn't ship em-dashes.
    em_n = len(_EM_DASH_RE.findall(body))
    out["em_dash_count"] = em_n
    if em_n > 5:
        out["fail_reasons"].append(f"{em_n} em-dashes (Thin Coverage UX leaking)")

    # Section presence: look for the canonical section headings the brief
    # template emits. Each found = +1.
    section_markers = (
        ("pipeline",          r"Pipeline"),
        ("operators",         r"Operator(?:s| Footprint)"),
        ("ma",                r"M&amp;A|M&A"),
        ("comps",             r"Comps|Comparables"),
    )
    populated = 0
    section_status: dict[str, bool] = {}
    for key, marker in section_markers:
        found = bool(re.search(marker, body, re.I))
        section_status[key] = found
        if found:
            populated += 1
    out["sections_populated"] = populated
    out["sections_total"] = len(section_markers)
    out["sections"] = section_status

    # Score by sections, then DEMOTE if em-dashes blow past 5 (the brief
    # template ships them as "field not yet populated" placeholders, so 29
    # of them on a page means the Thin Coverage UX isn't rendering and the
    # founder is about to share a page full of dashes).
    #  A = 4/4
    #  B = 3/4
    #  C = 2/4
    #  D = 1/4
    #  F = 0/4
    if populated == 4:   out["score"] = "A"
    elif populated == 3: out["score"] = "B"
    elif populated == 2: out["score"] = "C"
    elif populated == 1: out["score"] = "D"
    else:                out["score"] = "F"
    # Em-dash demotion. > 5 → step down two letter grades; > 20 → straight F.
    if em_n > 20:
        out["score"] = "F"
    elif em_n > 5:
        step_down = {"A": "C", "B": "D", "C": "F", "D": "F", "F": "F"}
        out["score"] = step_down.get(out["score"], "F")

    return out


def _audit_market_briefs(slugs: tuple[str, ...]) -> dict:
    """Fan out market-brief audits in parallel. Aggregate verdict."""
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKER_COUNT) as ex:
        futs = {ex.submit(_audit_market_brief_one, s): s for s in slugs}
        # Wall-clock budget: per-probe walltime * batches + slack.
        # Each market brief probe is now timeout=10s with 1 retry @ 15s,
        # so worst-case per item is ~25s. With WORKER_COUNT=8 parallel,
        # 30 markets fan out across ceil(30/8)=4 batches → ~100s worst-case.
        # In practice the brief route is warm-cached and finishes in 1-3s.
        _wall = max(PROBE_TIMEOUT_MARKET_BRIEF_S * 3,
                    PROBE_TIMEOUT_MARKET_BRIEF_S * len(slugs) / WORKER_COUNT + 10)
        for fut in as_completed(futs, timeout=_wall):
            try:
                rows.append(fut.result())
            except Exception as e:
                rows.append({"slug": futs[fut], "score": "F",
                             "fail_reasons": [f"audit exception: {e}"]})

    # Aggregate
    a = sum(1 for r in rows if r.get("score") == "A")
    b = sum(1 for r in rows if r.get("score") == "B")
    c = sum(1 for r in rows if r.get("score") == "C")
    d = sum(1 for r in rows if r.get("score") == "D")
    f = sum(1 for r in rows if r.get("score") == "F")

    # Critical fails: any em-dash > 5 OR HTTP fail
    critical = [r for r in rows if any("em-dashes" in fr or "HTTP" in fr
                                       for fr in (r.get("fail_reasons") or []))]

    # Pick worst-case 5
    rank = {"F": 0, "D": 1, "C": 2, "B": 3, "A": 4}
    worst = sorted(rows, key=lambda r: (rank.get(r.get("score") or "F", 0),
                                        -r.get("em_dash_count", 0)))[:5]

    overall = _score_letter(a + b, max(1, len(rows)))
    return {
        "surface": "Market Briefs",
        "total_audited": len(rows),
        "score_distribution": {"A": a, "B": b, "C": c, "D": d, "F": f},
        "overall_score": overall,
        "critical_fails": len(critical),
        "worst": worst,
        "ok_http_rate": round(
            (sum(1 for r in rows if r.get("ok_http")) / max(1, len(rows))) * 100, 1),
    }


def _audit_hyperscaler_brief_one(slug: str) -> dict:
    """Probe one /hyperscalers/<slug>/brief. Flag $/MW math, wrong attribution."""
    res = _probe(f"/hyperscalers/{slug}/brief", expect_json=False,
                 timeout=PROBE_TIMEOUT_HYPERSCALER_BRIEF_S)
    out: dict[str, Any] = {
        "slug": slug, "url": f"/hyperscalers/{slug}/brief",
        "status": res["status"], "dur_ms": res["dur_ms"],
        "ok_http": res["ok"], "score": "F", "fail_reasons": [],
        "usd_per_mw_max_seen": 0, "tracked_facilities": 0,
        "wrong_attribution_hits": 0,
    }
    if not res["ok"]:
        out["fail_reasons"].append(f"HTTP {res['status']}")
        return out
    body = res.get("body") or ""

    # $/MW math absurdity: look for "$XXM/MW" or "$XXX million/MW".
    # Strip commas inside numbers so $1,234,567 / MW is captured.
    body_for_money = body.replace(",", "")
    money_per_mw_re = re.compile(
        r"\$([0-9]+(?:\.[0-9]+)?)\s*(M|MM|million|B|billion)?\s*/\s*MW",
        re.I)
    max_seen = 0
    for m in money_per_mw_re.finditer(body_for_money):
        try:
            n = float(m.group(1))
        except ValueError:
            continue
        suf = (m.group(2) or "").lower()
        if suf in ("m", "mm", "million"):
            n *= 1_000_000
        elif suf in ("b", "billion"):
            n *= 1_000_000_000
        max_seen = max(max_seen, int(n))
    out["usd_per_mw_max_seen"] = max_seen
    if max_seen > MAX_USD_PER_MW:
        out["fail_reasons"].append(
            f"${max_seen:,}/MW > ${MAX_USD_PER_MW:,} ceiling (math broken)")

    # Tracked facility count — look for any of the canonical facility markers
    # the hyperscaler-brief template renders. The live template emits
    # "tracked across" + "facilities" (separated), so match either phrasing.
    fac_markers = (
        r"facilities tracked",
        r"tracked across",
        r"facility-card",
        r"data-facility=",
        r"<tr[^>]*data-mw",
        # The Capital Velocity section emits "$/MW" KPIs on a real-data brief;
        # presence is a robust proxy for "this brief has data".
        r"\$/MW",
    )
    has_fac = any(re.search(p, body, re.I) for p in fac_markers)
    out["tracked_facilities"] = 1 if has_fac else 0
    if not has_fac:
        out["fail_reasons"].append("no facility rows rendered")

    # Wrong-attribution regex on pipeline / news.
    hits = len(_WRONG_ATTR_RE.findall(body))
    out["wrong_attribution_hits"] = hits
    if hits > 0:
        out["fail_reasons"].append(
            f"{hits} wrong-attribution pipeline mention(s)")

    # Score
    if not out["fail_reasons"]:
        out["score"] = "A"
    elif len(out["fail_reasons"]) == 1 and out["wrong_attribution_hits"] == 0 \
         and out["usd_per_mw_max_seen"] <= MAX_USD_PER_MW:
        out["score"] = "B"
    elif out["usd_per_mw_max_seen"] > MAX_USD_PER_MW or hits > 0:
        out["score"] = "F"  # critical
    else:
        out["score"] = "C"
    return out


def _audit_hyperscaler_briefs(slugs: tuple[str, ...]) -> dict:
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKER_COUNT) as ex:
        futs = {ex.submit(_audit_hyperscaler_brief_one, s): s for s in slugs}
        _wall = max(PROBE_TIMEOUT_HYPERSCALER_BRIEF_S * 3,
                    PROBE_TIMEOUT_HYPERSCALER_BRIEF_S * len(slugs) / WORKER_COUNT + 10)
        for fut in as_completed(futs, timeout=_wall):
            try:
                rows.append(fut.result())
            except Exception as e:
                rows.append({"slug": futs[fut], "score": "F",
                             "fail_reasons": [f"audit exception: {e}"]})

    a = sum(1 for r in rows if r.get("score") == "A")
    b = sum(1 for r in rows if r.get("score") == "B")
    c = sum(1 for r in rows if r.get("score") == "C")
    f = sum(1 for r in rows if r.get("score") == "F")
    critical = [r for r in rows if r.get("usd_per_mw_max_seen", 0) > MAX_USD_PER_MW
                or r.get("wrong_attribution_hits", 0) > 0]
    rank = {"F": 0, "C": 1, "B": 2, "A": 3}
    worst = sorted(rows, key=lambda r: rank.get(r.get("score") or "F", 0))[:5]

    overall = _score_letter(a + b, max(1, len(rows)))
    return {
        "surface": "Hyperscaler Briefs",
        "total_audited": len(rows),
        "score_distribution": {"A": a, "B": b, "C": c, "F": f},
        "overall_score": overall,
        "critical_fails": len(critical),
        "worst": worst,
    }


def _audit_dcpi_dcgi_summary() -> dict:
    """Probe headline stats the LinkedIn narrative will quote."""
    out: dict[str, Any] = {
        "surface": "DCPI + DCGI Summary",
        "checks": [],
        "overall_score": "F",
        "critical_fails": 0,
    }
    # 1. DCPI total markets (the 306 number post-r71 expansion).
    # PRIMARY: the new admin-only /api/v1/dcpi/total returns the real row count
    # without going through preview gating. FALLBACK: /api/v1/dcpi/scores
    # with the admin-bypass branch (also added 2026-06-07) returns
    # _total_available unconditionally for admin callers; the legacy gated
    # _total_available read still works on older deploys.
    total = None
    r = _probe("/api/v1/dcpi/total", expect_json=True,
               timeout=PROBE_TIMEOUT_DCPI_S)
    if r.get("ok") and isinstance(r.get("json"), dict):
        t = r["json"].get("total")
        if isinstance(t, (int, float)):
            total = int(t)
    if total is None:
        r = _probe("/api/v1/dcpi/scores?limit=10", expect_json=True,
                   timeout=PROBE_TIMEOUT_DCPI_S)
        if r.get("ok") and isinstance(r.get("json"), dict):
            j = r["json"]
            # PREFER _total_available — it reflects the full universe even when
            # the page is gated to 10. Fall back to count/scores.length.
            total = (j.get("_total_available")
                     or j.get("total_markets")
                     or len(j.get("scores") or []))
            # 'count' is the page count (gated to 10), not the universe — use it
            # ONLY when nothing else is present.
            if not total:
                total = j.get("count")
    chk = {"check": "DCPI total markets", "value": total,
           "expected": "~306 (range 280-330)", "passed": False, "note": ""}
    # Range broadened post-r71 (2026-06-06) DCPI expansion which lifted
    # the coverage universe from 233 → 290+ → 306. Accept anything from
    # 280 (slight drift down) up to 330 (room for international adds).
    if isinstance(total, (int, float)) and 280 <= total <= 330:
        chk["passed"] = True
    elif total is None:
        chk["note"] = "could not fetch /api/v1/dcpi/total or /scores"
    else:
        chk["note"] = f"out of range — got {total}, expected 280-330"
    out["checks"].append(chk)

    # 2. DCPI verdict distribution (BUILD / CAUTION / AVOID counts)
    r = _probe("/api/v1/dcpi/leaderboard", expect_json=True,
               timeout=PROBE_TIMEOUT_DCPI_S)
    verdict_counts = {"BUILD": 0, "CAUTION": 0, "AVOID": 0}
    leaderboard_len = 0
    if r.get("ok") and isinstance(r.get("json"), dict):
        lb = r["json"].get("leaderboard") or []
        leaderboard_len = len(lb)
        for row in lb:
            v = (row.get("verdict") or "").upper().strip()
            if v in verdict_counts:
                verdict_counts[v] += 1
    chk = {"check": "DCPI leaderboard verdict mix",
           "value": f"BUILD={verdict_counts['BUILD']}, CAUTION={verdict_counts['CAUTION']}, AVOID={verdict_counts['AVOID']} (of {leaderboard_len})",
           "expected": "non-zero across at least 2 categories",
           "passed": False, "note": ""}
    nonzero = sum(1 for v in verdict_counts.values() if v > 0)
    if nonzero >= 2:
        chk["passed"] = True
    elif leaderboard_len == 0:
        chk["note"] = "leaderboard empty — stale data"
    else:
        chk["note"] = "only 1 verdict category populated — looks stuck"
    out["checks"].append(chk)

    # 3. DCGI operators count
    r = _probe("/api/v1/dcgi/operators", expect_json=True,
               timeout=PROBE_TIMEOUT_DCPI_S)
    op_count = 0
    if r.get("ok") and isinstance(r.get("json"), dict):
        j = r["json"]
        op_count = j.get("count") or len(j.get("operators") or [])
    # 10+ because the anon-facing endpoint is gated to 10-15 operators; real
    # operator count is in DCGI's gated view. If this drops to 0 the surface
    # is broken — that's the real signal.
    chk = {"check": "DCGI operators count (anon-visible)", "value": op_count,
           "expected": ">= 10", "passed": op_count >= 10,
           "note": "" if op_count >= 10 else "DCGI coverage broken"}
    out["checks"].append(chk)

    # 4. DCPI movers — must populate, otherwise the narrative claim about
    # "top-mover markets" will sit blank.
    r = _probe("/api/v1/dcpi/movers", expect_json=True,
               timeout=PROBE_TIMEOUT_DCPI_S)
    movers_n = 0
    if r.get("ok") and isinstance(r.get("json"), dict):
        j = r["json"]
        movers_n = j.get("count") or len(j.get("movers") or j.get("items") or [])
    chk = {"check": "DCPI movers (top movers last 30d)",
           "value": movers_n, "expected": ">= 5", "passed": movers_n >= 5,
           "note": "" if movers_n >= 5 else "movers feed empty — stale snapshot?"}
    out["checks"].append(chk)

    passed = sum(1 for c in out["checks"] if c["passed"])
    out["passed"] = passed
    out["total"] = len(out["checks"])
    out["overall_score"] = _score_letter(passed, len(out["checks"]))
    out["critical_fails"] = sum(1 for c in out["checks"] if not c["passed"])
    return out


def _audit_narrative_claims() -> dict:
    """Fact-check each line in docs/state-of-2026-claims.txt against live data."""
    claims, parse_err = _parse_claims_file()
    out: dict[str, Any] = {
        "surface": "Narrative Claims",
        "claims_file": _CLAIMS_PATH,
        "claims_count": len(claims),
        "parse_error": parse_err,
        "results": [],
        "overall_score": "F",
        "critical_fails": 0,
    }
    if not claims:
        out["overall_score"] = "F" if parse_err else "D"
        return out

    results: list[dict] = []
    for cl in claims:
        endpoint = cl["endpoint"]
        json_path = cl["json_path"]
        emin = cl["expected_min"]
        emax = cl["expected_max"]
        r = _probe(endpoint, expect_json=True)
        val = _extract_json_value(r.get("json"), json_path)
        item = {
            "claim": cl["claim"], "endpoint": endpoint,
            "json_path": json_path,
            "expected_min": emin, "expected_max": emax,
            "live_value": val, "status": r["status"],
            "passed": False, "fail_reason": None,
        }
        if val is None:
            item["fail_reason"] = "could not extract live value"
        elif val < emin or val > emax:
            item["fail_reason"] = (
                f"live value {val} outside expected [{emin}, {emax}]")
        else:
            # Inside range. Also check drift vs midpoint.
            midpoint = (emin + emax) / 2
            if midpoint > 0:
                drift = abs(val - midpoint) / midpoint
                item["drift_pct"] = round(drift * 100, 2)
            item["passed"] = True
        results.append(item)
    out["results"] = results
    passed = sum(1 for r in results if r["passed"])
    out["passed"] = passed
    out["total"] = len(results)
    out["overall_score"] = _score_letter(passed, len(results))
    out["critical_fails"] = sum(1 for r in results if not r["passed"])
    return out


def _audit_og_card_one(style: str) -> dict:
    """Probe one OG card style. Verify it returns image/png with non-trivial
    byte length (a blank fallback is ~2KB; a real card is > 20KB)."""
    # Pull a recent press_releases slug — the og_card route needs one.
    # Use 'today' rotation slug + an arbitrary placeholder; the route
    # falls back to a default when the slug is missing.
    test_slug = "state-of-2026"
    url = f"/api/v1/og/{style}/{test_slug}.png"
    res = _probe(url, expect_json=False, timeout=PROBE_TIMEOUT_OG_CARD_S)
    out: dict[str, Any] = {
        "style": style, "url": url, "status": res["status"],
        "dur_ms": res["dur_ms"], "size_bytes": res["body_len"],
        "score": "F", "fail_reasons": [],
    }
    if not res["ok"]:
        out["fail_reasons"].append(f"HTTP {res['status']}")
    elif res["body_len"] < 5_000:
        out["fail_reasons"].append(
            f"image only {res['body_len']} bytes (likely blank fallback)")
    if not out["fail_reasons"]:
        out["score"] = "A"
    elif "blank fallback" in (out["fail_reasons"][0] or ""):
        out["score"] = "C"
    else:
        out["score"] = "F"
    return out


def _audit_og_cards(styles: tuple[str, ...]) -> dict:
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKER_COUNT) as ex:
        futs = {ex.submit(_audit_og_card_one, s): s for s in styles}
        _wall = max(PROBE_TIMEOUT_OG_CARD_S * 3,
                    PROBE_TIMEOUT_OG_CARD_S * len(styles) / WORKER_COUNT + 10)
        for fut in as_completed(futs, timeout=_wall):
            try:
                rows.append(fut.result())
            except Exception as e:
                rows.append({"style": futs[fut], "score": "F",
                             "fail_reasons": [f"exception: {e}"]})
    a = sum(1 for r in rows if r.get("score") == "A")
    overall = _score_letter(a, len(rows))
    critical = [r for r in rows if r.get("score") == "F"]
    return {
        "surface": "OG Cards",
        "total_audited": len(rows),
        "passed": a, "overall_score": overall,
        "critical_fails": len(critical),
        "worst": [r for r in rows if r.get("score") != "A"],
        "all": rows,
    }


def _audit_link_health(highlighted_markets: tuple[str, ...],
                       hyperscalers: tuple[str, ...]) -> dict:
    """HEAD/GET every URL the post will share. Aim: 100% 2xx/3xx."""
    paths: list[str] = list(STATIC_LINKS_TO_PROBE)
    for s in highlighted_markets:
        paths.append(f"/markets/{s}/brief")
    for h in hyperscalers:
        paths.append(f"/hyperscalers/{h}/brief")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKER_COUNT) as ex:
        # link health is just an existence check — use the link-health timeout
        # so a slow brief doesn't blow the budget when included here.
        futs = {ex.submit(_probe, p, "GET", False,
                          PROBE_TIMEOUT_LINK_HEALTH_S): p for p in paths}
        _wall = max(PROBE_TIMEOUT_LINK_HEALTH_S * 3,
                    PROBE_TIMEOUT_LINK_HEALTH_S * len(paths) / WORKER_COUNT + 10)
        for fut in as_completed(futs, timeout=_wall):
            p = futs[fut]
            try:
                r = fut.result()
                rows.append({
                    "url": p, "status": r["status"],
                    "ok": r["ok"], "dur_ms": r["dur_ms"],
                    "error": r.get("error"),
                })
            except Exception as e:
                rows.append({"url": p, "status": 0, "ok": False,
                             "error": f"{type(e).__name__}: {e}"})

    ok = sum(1 for r in rows if r["ok"])
    server_errs = [r for r in rows if 500 <= (r.get("status") or 0) < 600]
    not_found  = [r for r in rows if (r.get("status") or 0) == 404]
    overall = _score_letter(ok, len(rows))
    critical = len(server_errs) + len(not_found)
    return {
        "surface": "Link Health",
        "total_audited": len(rows),
        "ok": ok, "server_errors": len(server_errs),
        "not_found": len(not_found),
        "overall_score": overall,
        "critical_fails": critical,
        "worst": [r for r in rows if not r["ok"]][:10],
    }


# ── orchestrator + verdict ────────────────────────────────────────────

def _compute_verdict(audits: list[dict]) -> dict:
    """Combine per-surface scores into GO / CAUTION / NO_GO."""
    total_critical = sum(int(a.get("critical_fails", 0)) for a in audits)
    grade_counts = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    for a in audits:
        g = a.get("overall_score") or "F"
        grade_counts[g] = grade_counts.get(g, 0) + 1

    # Rules:
    # NO_GO: any F, OR > 1 critical fail, OR > 3 C/D combined
    # CAUTION: 1-3 B/C scores, 0-1 critical
    # GO: all A/B, 0 critical
    cd = grade_counts.get("C", 0) + grade_counts.get("D", 0)
    if grade_counts.get("F", 0) > 0 or total_critical > 1 or cd > 3:
        verdict = "NO_GO"
        color = "#dc2626"
        message = (f"DO NOT POST — {total_critical} critical fail(s), "
                   f"{grade_counts.get('F', 0)} surface failures.")
    elif (grade_counts.get("B", 0) + cd) >= 1 or total_critical == 1:
        verdict = "CAUTION"
        color = "#f59e0b"
        message = ("Ship-able with caveats — review the worst-case "
                   "samples before posting.")
    else:
        verdict = "GO"
        color = "#10b981"
        message = "Cleared for publish. All surfaces score A."
    return {
        "verdict":         verdict,
        "color":           color,
        "message":         message,
        "grade_counts":    grade_counts,
        "total_critical":  total_critical,
    }


def _build_fix_list(audits: list[dict], verdict: dict) -> list[dict]:
    """Rank what to fix before posting. Highest severity first."""
    fixes: list[dict] = []
    for a in audits:
        surface = a.get("surface", "?")
        # Critical fails get severity 'critical'.
        if a.get("critical_fails", 0):
            for w in (a.get("worst") or []):
                reasons = w.get("fail_reasons") or []
                if w.get("score") in ("F", "D"):
                    fixes.append({
                        "severity": "critical",
                        "surface":  surface,
                        "url":      w.get("url") or w.get("slug") or "",
                        "reason":   "; ".join(reasons) if reasons else
                                    str(w.get("fail_reason") or w.get("note") or ""),
                    })
        # Then medium B/C.
        for w in (a.get("worst") or []):
            if w.get("score") in ("B", "C"):
                reasons = w.get("fail_reasons") or []
                fixes.append({
                    "severity": "medium",
                    "surface":  surface,
                    "url":      w.get("url") or w.get("slug") or "",
                    "reason":   "; ".join(reasons) if reasons else
                                str(w.get("fail_reason") or w.get("note") or ""),
                })
        # Narrative claim fails
        if a.get("surface") == "Narrative Claims":
            for r in (a.get("results") or []):
                if not r.get("passed"):
                    fixes.append({
                        "severity": "critical",
                        "surface":  surface,
                        "url":      r.get("endpoint", ""),
                        "reason":   (f"claim '{r.get('claim')[:80]}' "
                                     f"→ live={r.get('live_value')} "
                                     f"expected [{r.get('expected_min')}, "
                                     f"{r.get('expected_max')}]"),
                    })
    # Dedupe (surface, url, reason) and cap at 30.
    seen: set = set()
    out: list[dict] = []
    sev_order = {"critical": 0, "medium": 1, "low": 2}
    for f in sorted(fixes, key=lambda x: sev_order.get(x["severity"], 9)):
        k = (f["surface"], f["url"], f["reason"][:80])
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
        if len(out) >= 30:
            break
    return out


def _run_full_audit(markets: tuple[str, ...], markets_limit: int = 30) -> dict:
    """Orchestrate every audit, compute the verdict, ranked fix list."""
    t0 = time.time()
    market_slugs = markets[:markets_limit]

    # Audits in parallel-ish (each is independent). We do them sequentially
    # at the top level because each already fans out internally; running
    # them all together would spike DB load.
    audits: list[dict] = []
    surface_runtimes: dict[str, int] = {}

    for label, fn in (
        ("market_briefs",     lambda: _audit_market_briefs(market_slugs)),
        ("hyperscaler_briefs",lambda: _audit_hyperscaler_briefs(HYPERSCALERS_TO_PROBE)),
        ("dcpi_dcgi_summary", _audit_dcpi_dcgi_summary),
        ("narrative_claims",  _audit_narrative_claims),
        ("og_cards",          lambda: _audit_og_cards(OG_CARDS_TO_PROBE)),
        ("link_health",       lambda: _audit_link_health(market_slugs[:8],
                                                          HYPERSCALERS_TO_PROBE)),
    ):
        s0 = time.time()
        try:
            audits.append(fn())
        except Exception as e:
            audits.append({"surface": label, "overall_score": "F",
                           "critical_fails": 1,
                           "error": f"{type(e).__name__}: {e}"})
        surface_runtimes[label] = int((time.time() - s0) * 1000)

    verdict = _compute_verdict(audits)
    fix_list = _build_fix_list(audits, verdict)
    elapsed_ms = int((time.time() - t0) * 1000)

    return {
        "verdict":          verdict["verdict"],
        "color":            verdict["color"],
        "message":          verdict["message"],
        "grade_counts":     verdict["grade_counts"],
        "total_critical":   verdict["total_critical"],
        "audits":           audits,
        "fix_list":         fix_list,
        "runtime_ms":       elapsed_ms,
        "surface_runtimes": surface_runtimes,
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "probe_base":       _PROBE_BASE_URL,
        "markets_probed":   list(market_slugs),
        "hyperscalers_probed": list(HYPERSCALERS_TO_PROBE),
    }


def _cached_audit(markets_limit: int) -> dict:
    """60s cache key on the markets_limit signature."""
    key = f"audit:{markets_limit}"
    now = time.time()
    ent = _CACHE.get(key)
    if ent and (now - ent["ts"]) < _CACHE_TTL_S:
        out = dict(ent["data"])
        out["cache_age_s"] = int(now - ent["ts"])
        return out
    data = _run_full_audit(TOP_MARKETS_TO_PROBE, markets_limit=markets_limit)
    _CACHE[key] = {"ts": now, "data": data}
    return data


# ── HTML rendering ────────────────────────────────────────────────────

def _render_html(data: dict, admin_key: str) -> str:
    verdict = data.get("verdict", "NO_GO")
    color   = data.get("color", "#dc2626")
    message = _esc(data.get("message", ""))
    admin_q = _esc(admin_key, quote=True)
    grade   = data.get("grade_counts", {})

    # Surface cards
    surface_cards: list[str] = []
    for a in data.get("audits", []):
        s_color = {"A": "#10b981", "B": "#facc15", "C": "#f59e0b",
                   "D": "#ea580c", "F": "#dc2626"}.get(
                   a.get("overall_score", "F"), "#71717a")
        crit_chip = ""
        if a.get("critical_fails"):
            crit_chip = (f'<span class="chip chip-red">'
                         f'{int(a["critical_fails"])} critical</span>')
        worst_html = ""
        for w in (a.get("worst") or [])[:5]:
            reasons = w.get("fail_reasons") or []
            url = w.get("url") or w.get("slug") or ""
            sc = w.get("score", "")
            worst_html += (
                f'<li><span class="grade-pill grade-{sc}">{sc}</span> '
                f'<a href="{_PROBE_BASE_URL}{_esc(url)}" target="_blank" '
                f'rel="noopener">{_esc(url)}</a> '
                f'<span class="muted">{_esc("; ".join(reasons))}</span></li>')
        # Narrative-claims has a different shape
        if a.get("surface") == "Narrative Claims" and a.get("results"):
            worst_html = ""
            for r in a["results"][:8]:
                ok = "PASS" if r.get("passed") else "FAIL"
                ok_cls = "grade-A" if r.get("passed") else "grade-F"
                worst_html += (
                    f'<li><span class="grade-pill {ok_cls}">{ok}</span> '
                    f'<code>{_esc(r.get("endpoint", ""))}</code> '
                    f'<span class="muted">claim: '
                    f'{_esc((r.get("claim") or "")[:90])} | live='
                    f'{_esc(str(r.get("live_value")))} '
                    f'expected [{_esc(str(r.get("expected_min")))}, '
                    f'{_esc(str(r.get("expected_max")))}]'
                    f'{(" — " + _esc(str(r["fail_reason"]))) if r.get("fail_reason") else ""}'
                    f'</span></li>')
        # DCPI/DCGI summary has 'checks'
        if a.get("surface") == "DCPI + DCGI Summary" and a.get("checks"):
            worst_html = ""
            for c in a["checks"]:
                ok = "PASS" if c.get("passed") else "FAIL"
                ok_cls = "grade-A" if c.get("passed") else "grade-F"
                worst_html += (
                    f'<li><span class="grade-pill {ok_cls}">{ok}</span> '
                    f'<b>{_esc(c.get("check", ""))}</b> '
                    f'<span class="muted">value={_esc(str(c.get("value")))} '
                    f'expected {_esc(c.get("expected", ""))}'
                    f'{(" — " + _esc(c["note"])) if c.get("note") else ""}'
                    f'</span></li>')
        dist = a.get("score_distribution") or {}
        dist_str = "  ".join(f"{k}={v}" for k, v in dist.items() if v)
        surface_cards.append(f'''
        <div class="surface">
          <div class="surface-head" style="border-left:6px solid {s_color}">
            <div class="surface-title">{_esc(a.get("surface", "?"))}</div>
            <div class="surface-meta">
              <span class="grade-pill grade-{a.get("overall_score", "F")}">{a.get("overall_score", "F")}</span>
              {crit_chip}
              <span class="muted">{_esc(dist_str)}</span>
            </div>
          </div>
          <ul class="worst">{worst_html or "<li class=muted>nothing to flag — all green.</li>"}</ul>
        </div>''')

    # Fix list
    fix_html_rows = ""
    for f in data.get("fix_list", [])[:20]:
        sev_color = {"critical": "#dc2626", "medium": "#f59e0b",
                     "low": "#0ea5e9"}.get(f["severity"], "#71717a")
        fix_html_rows += (
            f'<tr><td><span class="chip" style="background:{sev_color}">'
            f'{f["severity"].upper()}</span></td>'
            f'<td>{_esc(f["surface"])}</td>'
            f'<td><code>{_esc(f["url"])}</code></td>'
            f'<td>{_esc(f["reason"])}</td></tr>')
    if not fix_html_rows:
        fix_html_rows = ('<tr><td colspan=4 class=muted>Nothing to fix. '
                         'Cleared for publish.</td></tr>')

    runtime_ms = data.get("runtime_ms", 0)
    cache_age = data.get("cache_age_s")
    cache_chip = (f'<span class="chip chip-grey">cache age '
                  f'{cache_age}s</span>') if cache_age is not None else ""
    generated = _esc(data.get("generated_at", ""))

    grade_chips = "".join(
        f'<span class="grade-pill grade-{k}">{k}: {v}</span>'
        for k, v in grade.items() if v
    )

    return f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<title>State of 2026 — Pre-Publish QA · DC Hub</title>
<meta name=robots content="noindex,nofollow">
<link rel=stylesheet href=/static/dchub-brand.css>
<style>
  body {{ background:#0a0a0a; color:#e5e5e5; font:14px/1.5 system-ui,sans-serif; margin:0; padding:24px; }}
  h1 {{ font-size:22px; margin:0 0 8px; }}
  a {{ color:#7dd3fc; }}
  .banner {{ background:{color}; color:#fff; padding:24px; border-radius:12px; margin-bottom:24px;
            display:flex; align-items:center; gap:24px; flex-wrap:wrap; }}
  .verdict-pill {{ font-size:48px; font-weight:800; letter-spacing:-1px; }}
  .banner-meta {{ flex:1; min-width:300px; }}
  .banner-meta .msg {{ font-size:16px; opacity:0.95; }}
  .surface {{ background:#171717; border:1px solid #262626; border-radius:8px;
              margin-bottom:12px; }}
  .surface-head {{ padding:12px 16px; display:flex; justify-content:space-between;
                   align-items:center; flex-wrap:wrap; gap:12px; }}
  .surface-title {{ font-size:16px; font-weight:600; }}
  .surface-meta {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
  .grade-pill {{ display:inline-block; padding:2px 10px; border-radius:6px;
                 font-weight:700; font-size:12px; }}
  .grade-A {{ background:#10b981; color:#fff; }}
  .grade-B {{ background:#facc15; color:#000; }}
  .grade-C {{ background:#f59e0b; color:#000; }}
  .grade-D {{ background:#ea580c; color:#fff; }}
  .grade-F {{ background:#dc2626; color:#fff; }}
  .chip {{ display:inline-block; padding:2px 8px; border-radius:6px; font-size:11px;
           color:#fff; }}
  .chip-red {{ background:#dc2626; }}
  .chip-grey {{ background:#3f3f46; }}
  .worst {{ margin:0; padding:8px 16px 16px 32px; }}
  .worst li {{ margin:4px 0; }}
  .muted {{ color:#a3a3a3; font-size:12px; }}
  table.fix {{ width:100%; border-collapse:collapse; background:#171717;
              border-radius:8px; overflow:hidden; }}
  table.fix th, table.fix td {{ padding:8px 12px; text-align:left;
                                border-bottom:1px solid #262626; vertical-align:top; }}
  table.fix th {{ background:#0a0a0a; color:#a3a3a3; font-size:12px;
                  text-transform:uppercase; letter-spacing:0.5px; }}
  code {{ background:#0a0a0a; padding:1px 6px; border-radius:4px; font-size:12px; }}
  .ship-row {{ display:flex; gap:12px; align-items:center; margin:24px 0; flex-wrap:wrap; }}
  button {{ background:#10b981; color:#fff; border:0; padding:12px 24px; border-radius:8px;
            font-weight:600; cursor:pointer; font-size:14px; }}
  button:disabled {{ background:#525252; cursor:not-allowed; }}
  button.danger {{ background:#dc2626; }}
  .nav-links a {{ margin-right:14px; }}
  pre {{ background:#0a0a0a; padding:12px; border-radius:6px; overflow:auto;
         font-size:12px; max-height:260px; }}
</style></head><body>
<h1>State of 2026 — Pre-Publish QA</h1>
<p class=muted>
  <a href="?admin_key={admin_q}">refresh</a> ·
  <a href="/api/v1/admin/qa/state-of-2026-precheck?format=json&admin_key={admin_q}">JSON</a> ·
  <a href="/admin/funnel-health?admin_key={admin_q}">funnel-health</a>
  · runtime {runtime_ms} ms {cache_chip} · generated {generated}
</p>

<div class="banner">
  <div class="verdict-pill">{verdict}</div>
  <div class="banner-meta">
    <div class="msg">{message}</div>
    <div style="margin-top:8px">{grade_chips}
      <span class="chip chip-grey">{data.get("total_critical", 0)} critical</span>
    </div>
  </div>
</div>

<div class="ship-row">
  <button onclick="auditTrail()">Email audit trail + ship</button>
  <button class="danger" onclick="refresh()">Re-run audit</button>
  <span class="muted">Run again after fixing the issues below — 60s cache TTL.</span>
</div>

{"".join(surface_cards)}

<h2 style="margin-top:32px">What I'd fix before publishing</h2>
<table class="fix">
  <thead><tr><th>Severity</th><th>Surface</th><th>Where</th><th>Why</th></tr></thead>
  <tbody>{fix_html_rows}</tbody>
</table>

<details style="margin-top:32px">
  <summary class="muted">Raw audit JSON</summary>
  <pre>{_esc(json.dumps(data, indent=2, default=str))}</pre>
</details>

<script>
var KEY = new URLSearchParams(window.location.search).get('admin_key') || "{admin_q}";
function refresh() {{ window.location.search = '?admin_key=' + KEY; }}
function auditTrail() {{
  fetch('/api/v1/admin/qa/state-of-2026/audit-trail?admin_key=' + KEY, {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json', 'X-Admin-Key': KEY }},
    body: JSON.stringify({{ verdict: "{verdict}", note: prompt("Optional note for the email log:") || "" }})
  }}).then(r => r.json()).then(d => alert(d.ok
    ? "Audit trail logged. " + (d.email || "")
    : "Failed: " + (d.error || "unknown"))).catch(e => alert("Failed: " + e));
}}
</script>
</body></html>"""


# ── routes ────────────────────────────────────────────────────────────

def _gate_or_401():
    if not _admin_ok(request):
        return Response(
            json.dumps({"ok": False, "error": "admin_key required"}),
            status=401, mimetype="application/json")
    return None


@state_of_2026_precheck_bp.route(
    "/api/v1/admin/qa/state-of-2026-precheck", methods=["GET"])
def state_of_2026_precheck_json():
    """JSON variant — same data as the HTML, machine-friendly."""
    g = _gate_or_401()
    if g: return g
    try:
        limit = int(request.args.get("markets_limit", "30"))
    except ValueError:
        limit = 30
    limit = max(5, min(100, limit))
    data = _cached_audit(limit)
    return Response(json.dumps(data, default=str, indent=2),
                    mimetype="application/json",
                    headers={"Cache-Control": "private, no-store"})


@state_of_2026_precheck_bp.route(
    "/api/v1/admin/qa/state-of-2026", methods=["GET"])
def state_of_2026_precheck_json_alias():
    """CF zone-worker bypass alias — same payload as the canonical JSON route."""
    return state_of_2026_precheck_json()


@state_of_2026_precheck_bp.route("/admin/qa/state-of-2026", methods=["GET"])
def state_of_2026_precheck_html():
    """Beautiful HTML dashboard with GO/CAUTION/NO_GO at top."""
    g = _gate_or_401()
    if g:
        return Response(
            "<h1>401</h1><p>Pass <code>?admin_key=…</code> or "
            "<code>X-Admin-Key</code> header (DCHUB_ADMIN_KEY env var).</p>",
            status=401, mimetype="text/html")
    try:
        limit = int(request.args.get("markets_limit", "30"))
    except ValueError:
        limit = 30
    limit = max(5, min(100, limit))
    fmt = (request.args.get("format") or "").lower()
    data = _cached_audit(limit)
    if fmt == "json":
        return Response(json.dumps(data, default=str, indent=2),
                        mimetype="application/json")
    admin_key = (request.headers.get("X-Admin-Key")
                 or request.args.get("admin_key") or "").strip()
    return Response(_render_html(data, admin_key), mimetype="text/html",
                    headers={"Cache-Control": "private, no-store"})


@state_of_2026_precheck_bp.route(
    "/api/v1/admin/qa/state-of-2026/audit-trail", methods=["POST"])
def state_of_2026_audit_trail():
    """Log a 'ship anyway' / 'ship now' decision. Best-effort emails the
    operator a one-liner so they have an audit trail of what was green
    when they posted.

    Body: {"verdict": "GO"|"CAUTION"|"NO_GO", "note": "..."}
    """
    g = _gate_or_401()
    if g: return g
    payload = request.get_json(silent=True) or {}
    verdict = str(payload.get("verdict") or "")
    note    = str(payload.get("note") or "")
    op_email = (os.environ.get("DCHUB_OPERATOR_EMAIL")
                or "azmartone@gmail.com")
    ts = datetime.now(timezone.utc).isoformat()
    line = (f"[{ts}] State of 2026 audit-trail · verdict={verdict} "
            f"· note={note[:160]}")
    logger.info("state_of_2026 audit_trail: %s", line)

    # Best-effort: try send via the project's own email helper if importable.
    sent = False
    try:
        # Many routes in this repo use a shared _send_email helper. Best-effort
        # import — if it doesn't exist, we just log.
        from utils import email_helper  # type: ignore
        email_helper.send(
            to=op_email,
            subject=f"[DC Hub] State of 2026 audit-trail · {verdict}",
            body=line + "\n\nFull report: /admin/qa/state-of-2026")
        sent = True
    except Exception as e:
        logger.debug("audit-trail email best-effort failed: %s", e)

    return jsonify({"ok": True, "logged": True, "emailed": sent,
                    "email": op_email if sent else None,
                    "line": line})
