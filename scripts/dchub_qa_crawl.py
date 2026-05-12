#!/usr/bin/env python3
"""dchub_qa_crawl.py — comprehensive QA crawler for dchub.cloud.

Reads sitemap.xml, fetches every URL with browser-class headers, runs a
battery of signal checks against each response, and reports findings
grouped by severity. Designed to be re-run by the healer (cron) so
regressions are caught continuously, not just once.

Usage:
  DCHUB_API_KEY=... python3 scripts/dchub_qa_crawl.py
  python3 scripts/dchub_qa_crawl.py --json > qa_findings.json
  python3 scripts/dchub_qa_crawl.py --max-urls 20    # quick sample
  python3 scripts/dchub_qa_crawl.py --concurrency 8

Read-only. No writes. No destructive endpoints called.
"""
from __future__ import annotations
import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

BASE = os.environ.get("DCHUB_BASE", "https://dchub.cloud")
API_KEY = os.environ.get("DCHUB_API_KEY", "")
SITEMAP = f"{BASE}/sitemap.xml"

# Browser-class headers so Cloudflare doesn't return 403 to a Python UA
H_BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 "
                   "Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}

# Severity thresholds
SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")

# ─────────────────────────────────────────────────────────────────────────────
# Fetch + parse helpers
# ─────────────────────────────────────────────────────────────────────────────

def fetch(url: str, timeout: int = 15) -> dict:
    """Return {status, headers, body, ms, error?} for a single GET."""
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers=H_BROWSER)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return {
                "url": url,
                "status": r.status,
                "headers": {k.lower(): v for k, v in r.headers.items()},
                "body": body.decode("utf-8", errors="ignore"),
                "ms": int((time.monotonic() - t0) * 1000),
                "size": len(body),
            }
    except urllib.error.HTTPError as e:
        body = b""
        try: body = e.read()
        except Exception: pass
        return {
            "url": url, "status": e.code,
            "headers": dict(getattr(e, "headers", {}) or {}),
            "body": body.decode("utf-8", errors="ignore"),
            "ms": int((time.monotonic() - t0) * 1000),
            "size": len(body),
            "error": f"HTTP {e.code}",
        }
    except Exception as e:
        return {
            "url": url, "status": 0,
            "headers": {}, "body": "",
            "ms": int((time.monotonic() - t0) * 1000),
            "size": 0,
            "error": str(e)[:200],
        }


def fetch_sitemap_urls(limit: int | None = None) -> list[str]:
    r = fetch(SITEMAP)
    if r["status"] != 200:
        print(f"WARN: sitemap fetch failed ({r['status']}); falling back to known URLs",
              file=sys.stderr)
        return [f"{BASE}/", f"{BASE}/pricing", f"{BASE}/dcpi", f"{BASE}/markets"]
    locs = re.findall(r"<loc>([^<]+)</loc>", r["body"])
    locs = [u.strip() for u in locs if u.strip().startswith(BASE)]
    if limit:
        locs = locs[:limit]
    return locs

# ─────────────────────────────────────────────────────────────────────────────
# Signal checks. Each returns list[(severity, code, msg)].
# ─────────────────────────────────────────────────────────────────────────────

def check_status(r: dict) -> list[tuple]:
    f = []
    s = r["status"]
    if s == 0:
        f.append(("critical", "fetch_failed", r.get("error", "unknown")))
    elif s >= 500:
        f.append(("critical", f"http_{s}", f"server error"))
    elif s >= 400 and s != 404:  # 404 reported separately below
        f.append(("high", f"http_{s}", f"client error"))
    elif s == 404:
        f.append(("high", "http_404", "page not found"))
    elif 300 <= s < 400:
        f.append(("info", f"http_{s}", "redirect"))
    return f


def check_size_and_perf(r: dict) -> list[tuple]:
    f = []
    if r["size"] > 500_000:
        f.append(("medium", "page_oversized", f"{r['size']//1024} KB > 500 KB"))
    if r["ms"] > 3000:
        f.append(("medium", "slow_page", f"{r['ms']} ms"))
    return f


def check_security_headers(r: dict) -> list[tuple]:
    f = []
    h = r["headers"]
    if r["status"] != 200:
        return f
    if not h.get("strict-transport-security"):
        f.append(("medium", "missing_hsts", "no Strict-Transport-Security"))
    if not h.get("x-content-type-options"):
        f.append(("low", "missing_x_content_type", "no X-Content-Type-Options: nosniff"))
    if not h.get("referrer-policy"):
        f.append(("low", "missing_referrer_policy", "no Referrer-Policy header"))
    # CSP is heavy; if present, just note it; if missing, warn
    if not h.get("content-security-policy"):
        f.append(("low", "missing_csp", "no Content-Security-Policy"))
    return f


def is_html(r: dict) -> bool:
    ct = (r["headers"].get("content-type", "") or "").lower()
    return "text/html" in ct or (r["body"][:200].lstrip().lower().startswith("<!doctype html"))


_TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>", re.I)
_TAG_RE = re.compile(r"<(h[1-6])\b[^>]*>(.*?)</\1>", re.I | re.DOTALL)
_IMG_RE = re.compile(r"<img\b([^>]*)>", re.I)
_LINK_RE = re.compile(r'<a\b[^>]*href="([^"]+)"', re.I)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[\s\S]*?</\1>", re.I)
_INLINE_ON_RE = re.compile(r"\son[a-z]+=", re.I)
_PHASE_MARKER_RE = re.compile(r"phase[\s-]?\d+", re.I)
_PLACEHOLDER_RE = re.compile(r">[\s ]*—[\s ]*<")
_CANONICAL_RE = re.compile(r'<link\b[^>]*rel="canonical"[^>]*href="([^"]+)"', re.I)
_OG_RE = re.compile(r'<meta\b[^>]*property="og:[^"]+"', re.I)
_JSONLD_RE = re.compile(r'<script\b[^>]*type="application/ld\+json"[^>]*>([\s\S]*?)</script>', re.I)
_LANG_RE = re.compile(r"<html\b[^>]*\blang=", re.I)
_TYPO_CONSOLE_RE = re.compile(r"\b(consoel|consol|conosle)\.(log|error|warn)\b", re.I)


def check_html_quality(r: dict) -> list[tuple]:
    f = []
    if not is_html(r) or r["status"] != 200:
        return f
    body = r["body"]
    # Strip script/style for text-quality checks
    text_body = _SCRIPT_STYLE_RE.sub("", body)

    # <title>
    title_m = _TITLE_RE.search(body)
    if not title_m or not title_m.group(1).strip():
        f.append(("high", "missing_title", "no <title>"))
    else:
        if len(title_m.group(1)) > 80:
            f.append(("low", "long_title", f"{len(title_m.group(1))} chars"))

    # <html lang>
    if not _LANG_RE.search(body):
        f.append(("medium", "missing_html_lang", "no lang on <html>"))

    # Headings
    headings = [(t.lower(), re.sub(r"<[^>]+>", "", txt).strip())
                for t, txt in _TAG_RE.findall(body)]
    h1s = [h for h in headings if h[0] == "h1"]
    if not h1s:
        f.append(("high", "no_h1", "page has no <h1>"))
    elif len(h1s) > 1:
        f.append(("medium", "multiple_h1", f"{len(h1s)} <h1> tags"))

    # Heading-level skips (e.g. h2 → h4)
    levels = [int(h[0][1]) for h in headings]
    skips = []
    for i in range(1, len(levels)):
        if levels[i] - levels[i-1] > 1:
            skips.append((levels[i-1], levels[i]))
    if skips:
        f.append(("low", "heading_skip", f"{len(skips)} level skips"))

    # Images without alt
    no_alt = 0
    decor_alt = 0
    for img_attrs in _IMG_RE.findall(body):
        if 'alt=' not in img_attrs.lower():
            no_alt += 1
        elif re.search(r'alt=""', img_attrs):
            decor_alt += 1
    if no_alt:
        f.append(("medium", "img_no_alt", f"{no_alt} <img> without alt attr"))

    # Real placeholder em-dashes (using phase 273 detector)
    n_placeholders = len(_PLACEHOLDER_RE.findall(text_body))
    if n_placeholders:
        f.append(("high", "data_placeholder", f"{n_placeholders} empty data cells (>—<)"))

    # Phase-marker comments (technical debt smell)
    n_phase = len(_PHASE_MARKER_RE.findall(body))
    if n_phase >= 4:
        f.append(("low", "phase_markers", f"{n_phase} phase-N hacks embedded inline"))

    # Inline event handlers (CSP-incompatible, harder to audit)
    n_inline_on = len(_INLINE_ON_RE.findall(text_body))
    if n_inline_on >= 3:
        f.append(("low", "inline_handlers", f"{n_inline_on} on*= attrs"))

    # Canonical
    if not _CANONICAL_RE.search(body):
        f.append(("medium", "missing_canonical", "no <link rel=canonical>"))

    # OG tags
    if len(_OG_RE.findall(body)) < 3:
        f.append(("low", "thin_opengraph", "fewer than 3 og:* meta tags"))

    # JSON-LD presence + validity
    jsonld_blocks = _JSONLD_RE.findall(body)
    if not jsonld_blocks:
        f.append(("medium", "no_jsonld", "no schema.org JSON-LD"))
    else:
        for i, blk in enumerate(jsonld_blocks):
            try:
                json.loads(blk.strip())
            except Exception as e:
                f.append(("high", "broken_jsonld",
                          f"JSON-LD block #{i+1} doesn't parse: {str(e)[:80]}"))

    # Typo console refs (consoel.log etc.)
    if _TYPO_CONSOLE_RE.search(body):
        f.append(("medium", "console_typo", "console.* typo in inline JS"))

    return f


def collect_internal_links(r: dict) -> set[str]:
    if not is_html(r) or r["status"] != 200:
        return set()
    body = r["body"]
    found = set()
    for href in _LINK_RE.findall(body):
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
            continue
        # resolve relative
        absu = urllib.parse.urljoin(r["url"], href)
        if absu.startswith(BASE):
            # drop fragment
            absu = absu.split("#")[0]
            found.add(absu)
    return found

# ─────────────────────────────────────────────────────────────────────────────
# API probe checks (sampled set)
# ─────────────────────────────────────────────────────────────────────────────

API_SAMPLES = [
    "/health",
    "/api/v1/health",
    "/health/deep",
    "/api/v1/dcpi/live-count",
    "/api/v1/dcpi/quality",
    "/api/v1/dcpi/leaderboard",        # phase 267
    "/api/v1/freshness",               # phase 268
    "/api/v1/mcp/funnel",
    "/api/v1/heal/findings",
    "/ai.txt",
    "/llms.txt",
    "/robots.txt",
    "/sitemap.xml",
]


def probe_api(path: str) -> dict:
    url = BASE + path
    H = dict(H_BROWSER)
    if API_KEY:
        H["X-API-Key"] = API_KEY
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers=H)
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", errors="ignore")
            return {
                "path": path, "url": url, "status": r.status,
                "ms": int((time.monotonic() - t0) * 1000),
                "ct": r.headers.get("content-type", ""),
                "size": len(body),
                "ok": True, "body_sample": body[:200],
            }
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8", errors="ignore")
        except Exception: pass
        return {"path": path, "url": url, "status": e.code,
                "ms": int((time.monotonic() - t0) * 1000),
                "ok": False, "error": f"HTTP {e.code}", "body_sample": body[:200]}
    except Exception as e:
        return {"path": path, "url": url, "status": 0,
                "ms": int((time.monotonic() - t0) * 1000),
                "ok": False, "error": str(e)[:200], "body_sample": ""}


def check_api(p: dict) -> list[tuple]:
    f = []
    if p["status"] == 0:
        f.append(("critical", f"api_{p['path']}_unreachable", p.get("error", "")))
    elif p["status"] >= 500:
        f.append(("critical", f"api_{p['path']}_5xx", f"{p['status']}"))
    elif p["status"] in (401, 403):
        f.append(("info", f"api_{p['path']}_auth", "needs internal/api key"))
    elif p["status"] >= 400:
        f.append(("high", f"api_{p['path']}_4xx", f"{p['status']}"))
    if p["ms"] > 3000:
        f.append(("medium", f"api_{p['path']}_slow", f"{p['ms']} ms"))
    # JSON content should parse
    ct = (p.get("ct") or "").lower()
    if "application/json" in ct and p.get("ok"):
        body = p.get("body_sample", "")
        if body and not (body.strip().startswith("{") or body.strip().startswith("[")):
            f.append(("medium", f"api_{p['path']}_bad_ct",
                      "Content-Type says JSON but body isn't"))
    return f

# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def crawl(urls: list[str], concurrency: int = 8) -> tuple[list[dict], dict]:
    """Fetch all URLs in parallel and run checks. Returns (per_url, summary)."""
    results = []
    # Phase 1: fetch all
    with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
        responses = list(ex.map(fetch, urls))
    # Phase 2: per-URL checks
    all_links = set()
    for r in responses:
        checks = []
        checks += check_status(r)
        checks += check_size_and_perf(r)
        checks += check_security_headers(r)
        checks += check_html_quality(r)
        results.append({
            "url": r["url"],
            "status": r["status"],
            "ms": r["ms"],
            "size": r["size"],
            "findings": checks,
        })
        all_links |= collect_internal_links(r)
    # Phase 3: broken internal links — for any link not already probed, check
    not_probed = [u for u in all_links if u not in urls]
    if not_probed:
        broken_check_limit = min(len(not_probed), 60)
        with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
            link_responses = list(ex.map(fetch, not_probed[:broken_check_limit]))
        for lr in link_responses:
            if lr["status"] in (0, 404) or lr["status"] >= 500:
                results.append({
                    "url": lr["url"],
                    "status": lr["status"],
                    "ms": lr["ms"],
                    "size": lr["size"],
                    "findings": [("high", "broken_internal_link",
                                  f"linked from another page, returns {lr['status']}")],
                })
    return results


def crawl_apis() -> list[dict]:
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        probes = list(ex.map(probe_api, API_SAMPLES))
    out = []
    for p in probes:
        out.append({"url": p["url"], "status": p["status"], "ms": p["ms"],
                    "size": p.get("size", 0),
                    "findings": check_api(p)})
    return out


def summarize(per_url: list[dict]) -> dict:
    by_sev = defaultdict(int)
    by_code = Counter()
    pages_with_issues = 0
    for row in per_url:
        if row["findings"]:
            pages_with_issues += 1
        for sev, code, _ in row["findings"]:
            by_sev[sev] += 1
            by_code[code] += 1
    return {
        "pages_scanned": len(per_url),
        "pages_with_issues": pages_with_issues,
        "by_severity": dict(by_sev),
        "top_codes": by_code.most_common(15),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-urls", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    print(f"[qa] sitemap: {SITEMAP}", file=sys.stderr)
    urls = fetch_sitemap_urls(limit=args.max_urls or None)
    print(f"[qa] crawling {len(urls)} URLs at concurrency={args.concurrency}",
          file=sys.stderr)

    t0 = time.monotonic()
    per_url = crawl(urls, concurrency=args.concurrency)
    per_api = crawl_apis()
    elapsed = time.monotonic() - t0
    print(f"[qa] done in {elapsed:.1f}s", file=sys.stderr)

    out = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "base": BASE,
        "elapsed_s": round(elapsed, 1),
        "summary": summarize(per_url + per_api),
        "summary_pages_only": summarize(per_url),
        "summary_apis_only": summarize(per_api),
        "pages": per_url,
        "apis": per_api,
    }
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(out, fh, indent=2)
        print(f"[qa] wrote {args.out}", file=sys.stderr)

    if args.json:
        json.dump(out, sys.stdout, indent=2)
        return

    # Human-readable report
    s = out["summary"]
    print(f"\n=== QA CRAWL REPORT — {out['ran_at']} ===")
    print(f"Base: {BASE}")
    print(f"Pages scanned: {s['pages_scanned']}, with issues: {s['pages_with_issues']}")
    print(f"By severity: {s['by_severity']}")
    print(f"\nTop issue codes:")
    for code, n in s["top_codes"]:
        print(f"  {n:>4}  {code}")

    # Critical + high findings detailed
    for sev in ("critical", "high"):
        rows = [(row["url"], code, msg)
                for row in per_url + per_api
                for s_, code, msg in row["findings"] if s_ == sev]
        if rows:
            print(f"\n--- {sev.upper()} ({len(rows)}) ---")
            for url, code, msg in rows[:50]:
                path = url.replace(BASE, "") or "/"
                print(f"  [{code}] {path}: {msg}")


if __name__ == "__main__":
    main()
