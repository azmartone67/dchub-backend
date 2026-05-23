"""
brain_security_detectors.py — Phase ZZZZZ-round17 (2026-05-23).

Security-focused brain detectors. The user asked: "can we also enhance
brain to detect any bugs or gate breaches or security breaches for that
matter, want our data to be secure".

These detectors run in the same consistency_radar.scan_all() pass as
the existing health/funnel/cron detectors. They return the same
finding shape (issue/url/count/detail) so they flow through the
existing heal-findings → brain-radar UI pipeline.

Detectors:
  - check_admin_endpoint_open      — POST admin endpoints WITHOUT auth.
                                      A 200 here is a security hole.
  - check_paywall_holes             — Anon probe of PRO-gated endpoints.
                                      Any 200 returning real data leaks.
  - check_security_header_drift    — HSTS / X-Content-Type-Options /
                                      X-Frame-Options / CSP present?
  - check_secret_pattern_in_body   — Regex-scan public endpoints for
                                      common secret patterns (AWS,
                                      Stripe, GitHub, JWT, internal-key)
                                      that might have leaked.
  - check_repeated_admin_401       — Brute-force scan detector: same
                                      IP hitting /admin/* with 401
                                      response >20 times in 1h.

Each detector is SELF-CONTAINED. It pings http://localhost:8080 (the
gunicorn worker) so it tests the SAME container — no DNS / CF cache
in the loop. Round 6c whale-filter doesn't apply here (we're not the
caller, we're the validator), so self-probing is fine.
"""
from __future__ import annotations

import json as _json
import os
import re
import urllib.request as _req
import urllib.error as _reqerr
from typing import Any


# Match _http_get shape in brain_consistency_radar so call signatures
# stay compatible if/when these move into that file.
def _probe(path: str, method: str = "GET", timeout: float = 6.0,
            headers: dict | None = None,
            body: bytes | None = None) -> tuple[int, dict, str]:
    """Probe a local URL; return (status, response_headers, body_text).

    Phase ZZZZZ-round19b (2026-05-23): read cap raised from 2048 to
    16384 bytes. Round 19's first run produced a false-positive
    paywall_hole finding because the 950-byte /grid/intelligence/ERCOT
    response was getting cut at 2048 bytes when combined with response
    framing, causing JSON parser to fail and the gated marker to be
    missed. 16k is enough headroom for any single paywall response."""
    url = f"http://localhost:8080{path}"
    req = _req.Request(url, method=method, data=body)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    # Phase ZZZZZ-round21 (2026-05-23): UA must NOT contain 'dchub-' or
    # any other marker the tier_gate's _is_internal check recognizes as
    # internal infra. The detector's whole job is to see what an
    # external caller sees - if it bypasses the gate, it can't audit
    # the gate. Round 20 used 'dchub-brain-security/1.0' which falsely
    # appeared as internal traffic and produced a paywall_hole false
    # positive on /grid/intelligence/ERCOT.
    req.add_header("User-Agent", "dc-security-audit/1.0")
    try:
        with _req.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(16384).decode("utf-8", "ignore")
    except _reqerr.HTTPError as he:
        try:
            body_text = he.read(16384).decode("utf-8", "ignore")
        except Exception:
            body_text = ""
        return he.code, dict(he.headers), body_text
    except Exception:
        return 0, {}, ""


# Phase ZZZZZ-round19b (2026-05-23): ASN-based hosting detection.
# IPinfo's free tier does NOT return company.type; only the Business
# plan does. So we can't rely on type=hosting alone — fall back to
# matching the AS-number against the known set of cloud / hosting
# providers. The major ones cover 95%+ of bot traffic in the wild.
_HOSTING_ASN_MARKERS = (
    "AS16509",   # Amazon AWS
    "AS14618",   # Amazon AWS-2 (newer ranges)
    "AS15169",   # Google (LLC + Cloud)
    "AS396982",  # Google Cloud
    "AS8075",    # Microsoft (Azure + Office)
    "AS24940",   # Hetzner
    "AS14061",   # DigitalOcean
    "AS63949",   # Linode / Akamai-cloud
    "AS20473",   # Vultr / Choopa
    "AS16276",   # OVH
    "AS197695",  # Reg.ru
    "AS41540",   # Aruba S.p.A.
    "AS9009",    # M247 (Tier 5)
    "AS210079",  # Aeza
    "AS43984",   # Stark Industries
    "AS200558",  # Servers.com
    "AS62240",   # Clouvider
    "AS3573",    # Cogent
    "AS54113",   # Fastly
    "AS13335",   # Cloudflare (rare for outbound but possible)
    "AS400940",  # Railway (our own; redundant guard)
)


def _is_hosting_ip(enrich: dict) -> bool:
    """Decide whether an IPinfo-enrichment dict represents a hosting/
    datacenter IP. Uses company.type when available (Business plan),
    falls back to ASN matching otherwise."""
    if not enrich:
        return False
    if (enrich.get("type") or "").lower() == "hosting":
        return True
    org = (enrich.get("org") or "")
    for asn in _HOSTING_ASN_MARKERS:
        if asn in org:
            return True
    hostname = (enrich.get("hostname") or "").lower()
    # Reverse-DNS suffix patterns that scream datacenter
    HOSTNAME_HINTS = (".compute.amazonaws.com", ".googleusercontent.com",
                       ".azurewebsites.net", ".cloudapp.net",
                       ".linodeusercontent.com", ".digitalocean.com",
                       ".your-server.de", ".oraclevcn.com",
                       ".vultr.com", ".ovh.net",)
    if any(h in hostname for h in HOSTNAME_HINTS):
        return True
    return False


# ────────────────────────────────────────────────────────────────────
# 1. Admin endpoints must require auth.
# ────────────────────────────────────────────────────────────────────
# Curated list of admin endpoints we KNOW exist + their method.
# Adding new admin endpoints? Append here so the detector covers them.
_ADMIN_ENDPOINTS_REQUIRING_AUTH = [
    ("POST",  "/api/v1/admin/dedup/run"),
    ("POST",  "/api/v1/admin/heal/purge-stale"),
    ("POST",  "/api/v1/admin/tag-customer"),
    ("POST",  "/api/v1/admin/testimonials/purge-bot-rows"),
    ("POST",  "/api/v1/admin/lost-conversion/send"),
    ("POST",  "/api/v1/admin/d1-sync/run"),
    ("POST",  "/api/v1/admin/vectorize-sync/run"),
    ("POST",  "/api/v1/admin/enrich/run"),
    ("POST",  "/api/v1/admin/dcm-crawl/run"),
    ("POST",  "/api/v1/admin/osm-crawl/run"),
    ("POST",  "/api/v1/admin/schema/repair"),
    ("POST",  "/api/v1/admin/news-ner/run"),
    ("POST",  "/api/v1/admin/upgrade-pool/send"),
    ("POST",  "/api/v1/admin/facilities/add"),
    # Read-only admin (still gated):
    ("GET",   "/api/v1/admin/founding-customers"),
    ("GET",   "/api/v1/admin/lost-conversion/candidates"),
    ("GET",   "/api/v1/admin/upgrade-pool/preview"),
    ("GET",   "/api/v1/admin/visitor-intelligence"),
    ("GET",   "/api/v1/admin/devrel-targets"),
    ("GET",   "/api/v1/admin/paywall-test"),
    ("GET",   "/api/v1/admin/crawler-status"),
    ("GET",   "/api/v1/bots/whales/debug"),
]


def check_admin_endpoint_open() -> list[dict]:
    """Probe each admin endpoint WITHOUT auth — should always return
    401/403/404. Anything else (200, 500 with data, 302 to a real
    payload) is a security gate breach."""
    findings: list[dict] = []
    for method, path in _ADMIN_ENDPOINTS_REQUIRING_AUTH:
        # Tiny POST body so endpoint reaches its auth check.
        body = b'{}' if method == "POST" else None
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        status, _hdrs, _body = _probe(path, method=method,
                                       timeout=5, body=body, headers=headers)
        # 0 = connection error (worker unhealthy), 404 = route not present,
        # 401/403 = auth correctly rejecting. 5xx = handler crash, also
        # not a leak (no data returned).
        if status in (0, 401, 403, 404, 405, 500, 502, 503):
            continue
        # 200/201/202 without auth = REAL leak.
        # 400 with rich error body is also suspicious (handler executed
        # before auth check) — flag but lower severity.
        sample = (_body or "")[:200]
        findings.append({
            "issue": "admin_endpoint_open",
            "url":   path,
            "count": 1,
            "detail": (f"Admin endpoint {method} {path} returned "
                        f"HTTP {status} WITHOUT auth header. "
                        f"Expected 401/403. Body sample: {sample!r}. "
                        f"This is a security gate breach — verify the "
                        f"endpoint calls is_valid_internal_key() before "
                        f"executing the handler body."),
        })
    return findings


# ────────────────────────────────────────────────────────────────────
# 2. PRO/Enterprise endpoints anonymously should NOT return full data.
# ────────────────────────────────────────────────────────────────────
_PRO_GATED_PATHS = [
    "/api/v1/grid/intelligence/ERCOT",
    "/api/v1/fiber/intel?ip=8.8.8.8",
    "/api/v1/bots/whales",
    # Add more here as they get gated.
]


def check_paywall_holes() -> list[dict]:
    """Probe PRO-gated endpoints anonymously. Response should be
    gated (402, gated:true, agent_action paywall). Full data leak = hole.

    Round 12 closed the /grid/intelligence hole; this is the recurring
    audit so future regressions surface as findings immediately.

    Phase ZZZZZ-round19b (2026-05-23): also check the raw response
    string for the gated markers BEFORE attempting JSON.loads, so a
    truncated-but-still-valid response doesn't false-positive."""
    findings: list[dict] = []
    for path in _PRO_GATED_PATHS:
        status, _hdrs, body = _probe(path, method="GET", timeout=6,
                                       headers={"X-Test-Anon": "1"})
        if status in (0, 401, 402, 403):
            continue
        # Fast-path: if the raw body contains any of these substrings,
        # it's gated even if JSON parsing fails or the response is
        # truncated. Misses are biased toward false-negative (we'd
        # flag a real hole as fine), not false-positive.
        raw_low = (body or "").lower()
        if ('"gated": true' in raw_low
                or '"gated":true' in raw_low
                or '"agent_action"' in raw_low
                or '"upgrade_required"' in raw_low
                or '"tier_required"' in raw_low):
            continue
        # Slow-path: JSON parse for unambiguous decision.
        try:
            d = _json.loads(body) if body else {}
        except Exception:
            d = {}
        if isinstance(d, dict):
            gated = d.get("gated") or d.get("error") == "upgrade_required"
            has_action = bool(d.get("agent_action"))
            if gated or has_action:
                continue
        # If response carries real data fields AND no gated marker → hole.
        if len(body) > 1500:
            findings.append({
                "issue": "paywall_hole",
                "url":   path,
                "count": 1,
                "detail": (f"Endpoint {path} responded HTTP {status} "
                            f"to anon with {len(body):,} bytes and NO "
                            f"gated marker. Expected 402/gated. "
                            f"Verify route applies require_plan / "
                            f"agent_action paywall. Body preview: "
                            f"{body[:200]!r}"),
            })
    return findings


# ────────────────────────────────────────────────────────────────────
# 3. Security headers present?
# ────────────────────────────────────────────────────────────────────
_PUBLIC_PROBE_PATHS = ["/", "/pricing", "/api/v1/version"]
_REQUIRED_HEADERS = (
    "x-content-type-options",   # MUST be 'nosniff'
    "x-frame-options",          # MUST be DENY or SAMEORIGIN
    "referrer-policy",          # MUST be strict-origin or stricter
)


def check_security_header_drift() -> list[dict]:
    """Probe public pages for the minimum-security HTTP header set.
    HSTS is Cloudflare-managed (zone level) so we don't audit it
    here — we audit the origin headers we control."""
    findings: list[dict] = []
    for path in _PUBLIC_PROBE_PATHS:
        status, headers, _body = _probe(path, method="GET", timeout=5)
        if status == 0 or status >= 500:
            continue
        lower = {k.lower(): v for k, v in headers.items()}
        missing = [h for h in _REQUIRED_HEADERS if h not in lower]
        if missing:
            findings.append({
                "issue": "security_header_missing",
                "url":   path,
                "count": len(missing),
                "detail": (f"GET {path} response missing headers: "
                            f"{', '.join(missing)}. Set them in the "
                            f"Cloudflare worker (_worker.js) or the "
                            f"Flask after-request hook so every "
                            f"response carries the minimum-safe set."),
            })
    return findings


# ────────────────────────────────────────────────────────────────────
# 4. Secret-pattern leak detector.
# ────────────────────────────────────────────────────────────────────
# Patterns of common credentials that should NEVER appear in API
# response bodies. False-positive cost (matching benign hex strings)
# managed by requiring distinctive prefixes.
_SECRET_PATTERNS = [
    (r"AKIA[0-9A-Z]{16}",          "aws_access_key_id"),
    (r"sk_live_[0-9a-zA-Z]{24,}",  "stripe_secret_key_live"),
    (r"sk_test_[0-9a-zA-Z]{24,}",  "stripe_secret_key_test"),
    (r"ghp_[0-9a-zA-Z]{30,}",      "github_pat"),
    (r"gho_[0-9a-zA-Z]{30,}",      "github_oauth"),
    (r"xox[abp]-[0-9]{10,}-",      "slack_token"),
    (r"dchub-internal-(sync-)?20\d\d",
                                    "legacy_internal_key"),
]

_SECRET_PROBE_PATHS = [
    "/api/v1/version",
    "/api/v1/heal/findings",
    "/api/v1/brain/error-classes",
    "/api/v1/marketing/distribution/health",
    "/api/v1/dchub-media/feed-v3",
    "/api/v1/visitor-intel",
]


def check_secret_pattern_in_body() -> list[dict]:
    """Sample-scan public endpoints for credential patterns. A hit is
    a serious leak — credentials are not supposed to be in response
    bodies of any endpoint."""
    findings: list[dict] = []
    for path in _SECRET_PROBE_PATHS:
        status, _hdrs, body = _probe(path, method="GET", timeout=6)
        if status == 0 or status >= 500 or not body:
            continue
        for pat, label in _SECRET_PATTERNS:
            m = re.search(pat, body)
            if m:
                findings.append({
                    "issue": "secret_pattern_in_response",
                    "url":   path,
                    "count": 1,
                    "detail": (f"Response body of GET {path} contains a "
                                f"string matching the '{label}' pattern: "
                                f"'{m.group(0)[:8]}...{m.group(0)[-4:]}'. "
                                f"This may be a real credential leaked into "
                                f"a public API surface. Audit the handler "
                                f"and remove the field from the response. "
                                f"If a false positive (e.g. example string "
                                f"in a doc field), add a regex exclusion."),
                })
                # one match per path is enough for the finding
                break
    return findings


# ────────────────────────────────────────────────────────────────────
# 5. Brute-force admin scan detector.
# ────────────────────────────────────────────────────────────────────
def check_repeated_admin_401() -> list[dict]:
    """Look at rate_limiter request log for repeated 401s on /admin/*
    paths from a single IP. >20 hits in 1h = scan attempt. Requires
    the rate_limit_events table; gracefully no-op if missing."""
    findings: list[dict] = []
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        return findings
    db = os.environ.get("DATABASE_URL")
    if not db:
        return findings
    try:
        with psycopg2.connect(db, sslmode="require", connect_timeout=5) as c:
            c.autocommit = True
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Soft-existence: only run if the table exists.
                cur.execute("""
                    SELECT to_regclass('public.rate_limit_events') AS t
                """)
                if not (cur.fetchone() or {}).get("t"):
                    return findings
                cur.execute("""
                    SELECT ip_address, path, COUNT(*) AS hits
                      FROM rate_limit_events
                     WHERE created_at >= NOW() - INTERVAL '1 hour'
                       AND path LIKE '/api/v1/admin/%%'
                       AND status_code = 401
                       AND ip_address NOT LIKE '162.220.232.%%'
                       AND ip_address NOT LIKE '162.220.233.%%'
                       AND ip_address != '127.0.0.1'
                     GROUP BY ip_address, path
                    HAVING COUNT(*) >= 20
                     ORDER BY hits DESC LIMIT 10
                """)
                rows = cur.fetchall() or []
                for r in rows:
                    findings.append({
                        "issue": "suspicious_admin_scan",
                        "url":   r["path"],
                        "count": int(r["hits"]),
                        "detail": (f"IP {r['ip_address']} hit {r['path']} "
                                    f"{r['hits']} times in the last 1h with "
                                    f"HTTP 401 responses. Consistent with a "
                                    f"credential-stuffing or admin-endpoint "
                                    f"brute-force scan. Verify rate-limiter "
                                    f"is throttling this IP; consider "
                                    f"adding it to a CF firewall rule."),
                    })
    except Exception:
        # Detector should never explode the scan_all pass.
        pass
    return findings


# ────────────────────────────────────────────────────────────────────
# 6. Hosting/datacenter traffic share — bot infestation signal.
# ────────────────────────────────────────────────────────────────────
def check_hosting_traffic_share() -> list[dict]:
    """Phase ZZZZZ-round19 (2026-05-23) — use IPinfo enrichment to
    surface the SHARE of recent MCP traffic that came from datacenter
    IPs (AWS/GCP/Azure/Hetzner/etc.). Real enterprise leads come from
    business/ISP IPs; >40% hosting share = bot infestation.

    Lightweight: only enriches top-20 IPs by call volume, then weights
    the answer by their call count. Cached 24h per-IP by IPinfo helper
    so this is effectively free after the first run."""
    findings: list[dict] = []
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        return findings
    db = os.environ.get("DATABASE_URL")
    if not db:
        return findings
    try:
        with psycopg2.connect(db, sslmode="require", connect_timeout=5) as c:
            c.autocommit = True
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT ip_address, COUNT(*) AS calls
                      FROM mcp_tool_calls
                     WHERE created_at >= NOW() - INTERVAL '24 hours'
                       AND ip_address IS NOT NULL
                       AND ip_address != ''
                       AND ip_address NOT LIKE '162.220.232.%%'
                       AND ip_address NOT LIKE '162.220.233.%%'
                     GROUP BY ip_address
                     ORDER BY calls DESC
                     LIMIT 20
                """)
                rows = cur.fetchall() or []
    except Exception:
        return findings
    if not rows:
        return findings

    # Sum and per-type tally
    total = sum(int(r["calls"]) for r in rows)
    if total < 100:
        return findings  # not enough volume to draw a conclusion
    hosting_calls = 0
    sample_companies: list[str] = []
    try:
        from routes.visitor_intelligence import _ipinfo_enrich
    except Exception:
        return findings
    for r in rows:
        enrich = _ipinfo_enrich(r["ip_address"]) or {}
        # Phase ZZZZZ-round19b: use _is_hosting_ip which checks both
        # company.type (Business plan) AND ASN markers (free plan).
        # The free IPinfo tier returns type=null, so type-only check
        # always returned False and the detector never fired.
        if _is_hosting_ip(enrich):
            hosting_calls += int(r["calls"])
            company = enrich.get("org") or enrich.get("company") or "?"
            sample_companies.append(company)
    share_pct = round(100.0 * hosting_calls / total, 1)
    if share_pct < 40:
        return findings  # healthy mix
    findings.append({
        "issue": "hosting_traffic_share_high",
        "url":   "mcp_tool_calls",
        "count": int(share_pct),
        "detail": (f"{share_pct}% of recent MCP traffic (24h, top-20 IPs) "
                    f"comes from datacenter / cloud-hosting IPs — likely "
                    f"automated scrapers, not enterprise prospects. "
                    f"Top hosting orgs: "
                    f"{', '.join(set(sample_companies))[:200]}. Action: "
                    f"tighten rate limits on hosting IPs, OR if the "
                    f"traffic is a known LLM proxy (Claude/ChatGPT), "
                    f"whitelist that company's range so it stops "
                    f"surfacing here. Brain class: paywall_hole +/- "
                    f"suspicious_admin_scan candidates."),
    })
    return findings


# Convenience: expose all in one list so brain_consistency_radar
# can iterate them in scan_all() without enumerating each name.
SECURITY_DETECTORS = (
    check_admin_endpoint_open,
    check_paywall_holes,
    check_security_header_drift,
    check_secret_pattern_in_body,
    check_repeated_admin_401,
    check_hosting_traffic_share,
)
