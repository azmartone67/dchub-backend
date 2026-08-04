#!/usr/bin/env python3
"""Surface 2 — the PUBLIC WEB, the map and the paywall, as a visitor sees them.

Three failure modes here are specific to this platform's topology and invisible
from inside the backend:

* **The edge can serve a 404 while the origin is healthy.** A slow endpoint makes
  the Cloudflare zone worker time out the primary and fail over to a STALE Render
  origin, so ``dchub.cloud`` returns 404 while the Railway origin returns 200 for
  the same request. Nothing that reads the database can see this — you have to
  ask both doors the same question.
* **A checkout link can be live, well-formed and wrong.** The checkout-integrity
  wave found a link that billed the wrong price, one that did not exist (a capital
  I for a lowercase l, shipped as three live 403 buttons), and one selling a plan
  that had nothing left to sell. The repo agreed with itself in every case.
* **robots.txt decides whether the AI-discovery strategy exists at all.** There
  are two copies and the frontend one is a decoy; only the backend-served file is
  authoritative.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from . import config as C
from .finding import (CRITICAL, GAUGE, INFO, MAJOR, MINOR, PASS, RED, SEAT_ANON,
                      SEAT_NONE, Finding, blind, stable_key)
from .http import Unreachable, fetch

# Crawlers whose access IS the GEO strategy. A blanket Disallow on any of these
# silently switches off AI discovery while every page still renders perfectly.
AI_CRAWLERS = ["GPTBot", "ClaudeBot", "Claude-Web", "PerplexityBot",
               "Google-Extended", "CCBot", "anthropic-ai", "Bingbot"]

# Paths asked at BOTH doors. Cheap, cacheable, and representative of the surfaces
# an agent or crawler actually lands on.
EDGE_ORIGIN_PATHS = ["/api/health", "/robots.txt", "/sitemap.xml",
                     "/api/v1/ops/deadman",
                     # Documented repeat offenders for the no-store-vs-HIT
                     # contradiction below: /api/v1/health was measured serving
                     # age=2296 (38 min stale) while the origin was already
                     # green, and origin-freshness — the endpoint whose entire
                     # job is "is the origin fresh?" — was itself served from
                     # cache for 15 minutes of polling.
                     "/api/v1/health", "/api/v1/ops/origin-freshness"]

# Directives that mean "do not keep a copy of this". If a response carries one
# of these and the edge still hands back a stored copy, the platform is
# contradicting itself — see _check_stale_edge_cache.
_NO_STORE_TOKENS = ("no-store", "no-cache", "private", "must-revalidate")


def _hdr(headers: dict, name: str) -> str:
    """Case-insensitive header read. CF sends lowercase, Flask sends title-case."""
    want = name.lower()
    for k, v in (headers or {}).items():
        if str(k).lower() == want:
            return str(v)
    return ""

_STRIPE_RE = re.compile(r"https://buy\.stripe\.com/[A-Za-z0-9_\-]+")


def probe(findings: list[Finding]) -> None:
    _probe_pages(findings)
    _probe_discovery(findings)
    _probe_edge_vs_origin(findings)
    _probe_checkout_links(findings)


def _probe_pages(findings: list[Finding]) -> None:
    """Every public page must render. A 404 here is a door nailed shut."""
    broken, ok = [], 0
    for path in C.PUBLIC_PAGES:
        url = f"{C.EDGE}{path}"
        try:
            status, _h, body = fetch(url, timeout=C.HTTP_TIMEOUT)
        except Unreachable as e:
            findings.append(blind(
                key=stable_key("web", "page", path), surface="web", seat=SEAT_ANON,
                title=f"Public page {path} unobserved", why=str(e), basis=f"GET {url}"))
            continue
        # A 200 that returns almost nothing is a broken page with a good status —
        # size is the cheapest guard against an empty shell rendering as success.
        if status >= 400 or len(body) < 500:
            broken.append(f"{path} -> HTTP {status}, {len(body)}b")
        else:
            ok += 1

    findings.append(Finding(
        key=stable_key("web", "public-pages"), surface="web", seat=SEAT_ANON,
        title=(f"All {ok} public pages render" if not broken else
               f"{len(broken)} public page(s) do not render"),
        verdict=PASS if not broken else RED,
        severity=INFO if not broken else CRITICAL, value=len(broken),
        evidence="; ".join(broken) if broken else
                 f"{ok} page(s) returned 2xx with real content: "
                 f"{', '.join(C.PUBLIC_PAGES)}",
        basis=f"anonymous GET of {len(C.PUBLIC_PAGES)} public paths at {C.EDGE}, "
              "asserting both status and a non-trivial body",
        red_when="a public page returns >=400, or returns 200 with under 500 "
                 "bytes (an empty shell rendering as success)"))


def _probe_discovery(findings: list[Finding]) -> None:
    """The machine-readable surfaces are how agents and AI crawlers find us."""
    missing, present = [], []
    robots_body = ""
    for path in C.DISCOVERY_PATHS:
        url = f"{C.EDGE}{path}"
        try:
            status, _h, body = fetch(url, timeout=C.HTTP_TIMEOUT)
        except Unreachable as e:
            findings.append(blind(
                key=stable_key("web", "discovery", path), surface="web",
                seat=SEAT_NONE, title=f"Discovery surface {path} unobserved",
                why=str(e), basis=f"GET {url}"))
            continue
        if status >= 400 or len(body) < 50:
            missing.append(f"{path} -> HTTP {status}, {len(body)}b")
        else:
            present.append(f"{path} ({len(body)}b)")
        if path == "/robots.txt" and status < 400:
            robots_body = body

    findings.append(Finding(
        key=stable_key("web", "discovery"), surface="web", seat=SEAT_NONE,
        title=(f"All {len(present)} machine-readable discovery surfaces present"
               if not missing else
               f"{len(missing)} discovery surface(s) missing"),
        verdict=PASS if not missing else RED,
        severity=INFO if not missing else MAJOR, value=len(missing),
        evidence="; ".join(missing) if missing else "; ".join(present),
        basis=f"anonymous GET of {C.DISCOVERY_PATHS} at {C.EDGE}",
        red_when="robots.txt, llms.txt, sitemap.xml or .well-known/mcp.json is "
                 "missing or trivially small — agents and AI crawlers discover us "
                 "through exactly these files"))

    if robots_body:
        _check_ai_crawler_access(robots_body, findings)


def _check_ai_crawler_access(robots: str, findings: list[Finding]) -> None:
    """Is the AI-discovery strategy switched on in the file that decides it?

    Reads the file the EDGE actually serves. There are two copies of robots.txt in
    this codebase and the frontend one is a decoy — asserting against the repo
    would prove nothing about what a crawler receives.
    """
    blocked = []
    # Split into per-user-agent blocks; only a Disallow of "/" is a full block.
    blocks = re.split(r"(?im)^user-agent:\s*", robots)
    for block in blocks[1:]:
        lines = block.splitlines()
        agent = lines[0].strip() if lines else ""
        body = "\n".join(lines[1:])
        # Stop at the next directive-free blank separation is unnecessary here:
        # re.split already bounded the block at the next User-agent line.
        if re.search(r"(?im)^disallow:\s*/\s*$", body):
            for bot in AI_CRAWLERS:
                if agent.lower() == bot.lower() or agent == "*":
                    blocked.append(f"{agent} (Disallow: /)")

    findings.append(Finding(
        key=stable_key("web", "ai-crawler-access"), surface="web", seat=SEAT_NONE,
        title=("AI crawlers are permitted by the served robots.txt"
               if not blocked else
               f"robots.txt BLOCKS AI crawlers: {', '.join(sorted(set(blocked)))}"),
        verdict=PASS if not blocked else RED,
        severity=INFO if not blocked else CRITICAL, value=len(set(blocked)),
        evidence=(f"no blanket Disallow for any of {AI_CRAWLERS}"
                  if not blocked else "; ".join(sorted(set(blocked)))),
        basis=f"GET {C.EDGE}/robots.txt — the file the EDGE serves, which is the "
              "backend copy; the frontend copy in the repo is a decoy and proves "
              "nothing about what a crawler receives",
        red_when="a named AI crawler, or *, carries a blanket 'Disallow: /' — the "
                 "entire AI-discovery strategy would be switched off while every "
                 "page still rendered perfectly"))


def _probe_edge_vs_origin(findings: list[Finding]) -> None:
    """Ask both doors the same question.

    A disagreement where the EDGE fails and the ORIGIN succeeds is the signature
    of failover to the stale secondary — the case where the site is broken for
    every real visitor while every internal check is green.
    """
    disagreements, checked = [], 0
    observed = []   # (path, edge_headers, edge_bytes, origin_bytes) for the cache check
    for path in EDGE_ORIGIN_PATHS:
        try:
            e_status, e_h, e_body = fetch(f"{C.EDGE}{path}", timeout=C.HTTP_TIMEOUT)
            o_status, _oh, o_body = fetch(f"{C.ORIGIN}{path}", timeout=C.HTTP_TIMEOUT)
        except Unreachable as ex:
            findings.append(blind(
                key=stable_key("web", "edge-origin", path), surface="web",
                seat=SEAT_NONE, title=f"Edge/origin comparison for {path} unobserved",
                why=str(ex), basis=f"GET {path} at both {C.EDGE} and {C.ORIGIN}"))
            continue
        checked += 1
        observed.append((path, e_h, len(e_body), len(o_body)))
        if e_status >= 400 and o_status < 400:
            disagreements.append(
                f"{path}: edge {e_status} vs origin {o_status} "
                f"(served-by={_hdr(e_h, 'x-dc-hub-served-by') or '-'}, "
                f"attempts={_hdr(e_h, 'x-dc-attempts') or '-'})")

    if not checked:
        return
    _check_stale_edge_cache(observed, findings)
    findings.append(Finding(
        key=stable_key("web", "edge-origin"), surface="web", seat=SEAT_NONE,
        title=("Edge and origin agree on every probed path" if not disagreements
               else f"EDGE serves errors the ORIGIN does not on "
                    f"{len(disagreements)} path(s)"),
        verdict=PASS if not disagreements else RED,
        severity=INFO if not disagreements else CRITICAL, value=len(disagreements),
        evidence="; ".join(disagreements) if disagreements else
                 f"{checked} path(s) returned matching-class statuses at both doors",
        basis=f"same paths fetched at {C.EDGE} and {C.ORIGIN} in the same run; "
              "response headers x-dc-hub-served-by / x-dc-attempts captured",
        red_when="the public edge returns >=400 for a path the Railway origin "
                 "serves fine — the signature of failover to the stale secondary, "
                 "which is invisible to every check that reads the database",
        remedy="Check x-dc-hub-served-by and x-dc-attempts on the failing path; a "
               "cached edge 404 needs an explicit purge, since this zone's cache "
               "key ignores query strings for /api/v1/*."))


def _check_stale_edge_cache(observed: list, findings: list[Finding]) -> None:
    """Is the edge storing copies of things the origin told it not to store?

    ★ WHY THIS EXISTS. The edge/origin check above compares STATUS CODES only,
    and that is not enough: on 2026-08-04 both doors returned 200 while the edge
    served a 13,886-byte page and the origin served 19,243 bytes of the same URL.
    A public page was 40 minutes stale and the harness would have called it PASS.

    ★ WHY IT ASSERTS ON HEADERS, NOT ON THE BODY. A body diff between two
    requests false-alarms on every timestamp, counter and nonce, and a threshold
    for "materially different" would be exactly the invented number rule 3
    forbids. But `Cache-Control: no-store` from the origin together with
    `cf-cache-status: HIT` and a non-zero `age` from the edge is a
    SELF-CONTRADICTION — one side says do not keep this, the other says here is
    the copy I kept and it is N seconds old. No threshold, no body, no judgement
    call, and it catches the whole class: /api/v1/health at age 2296, the reveal
    feed serving an empty body after the fix, rag/search returning quarantined
    ids for 32 minutes, and this board itself.

    The measured byte divergence is reported alongside as a GAUGE, because on a
    cacheable endpoint a difference is normal and only a human (or the reasoning
    layer) can say whether a given one matters.
    """
    contradictions, cached_paths, divergence = [], 0, []
    for path, e_h, e_len, o_len in observed:
        cache_status = _hdr(e_h, "cf-cache-status").strip().upper()
        age_raw = _hdr(e_h, "age").strip()
        try:
            age = int(age_raw) if age_raw else 0
        except ValueError:
            age = 0
        directives = _hdr(e_h, "cache-control").lower()
        cdn = _hdr(e_h, "cdn-cache-control").lower()
        says_no_store = any(t in directives or t in cdn for t in _NO_STORE_TOKENS)
        stored = cache_status in ("HIT", "EXPIRED", "REVALIDATED", "STALE")

        if stored:
            cached_paths += 1
            if e_len != o_len:
                divergence.append(f"{path}: edge {e_len}b vs origin {o_len}b "
                                  f"(cf-cache={cache_status}, age={age}s)")
        if stored and says_no_store and age > 0:
            contradictions.append(
                f"{path}: response says '{directives or cdn}' yet the edge "
                f"returned cf-cache-status={cache_status} with age={age}s "
                f"({age / 60.0:.0f} min old)"
                + (f"; body differs from origin by {abs(e_len - o_len)}b"
                   if e_len != o_len else "; body matches origin"))

    findings.append(Finding(
        key=stable_key("web", "edge-honours-no-store"), surface="web",
        seat=SEAT_NONE,
        title=("The edge honours no-store on every probed path"
               if not contradictions else
               f"EDGE is caching {len(contradictions)} path(s) that declare no-store"),
        verdict=PASS if not contradictions else RED,
        severity=INFO if not contradictions else MAJOR,
        value=len(contradictions),
        evidence="; ".join(contradictions) if contradictions else
                 f"{len(observed)} path(s) checked; {cached_paths} served from "
                 f"cache, none of them against a no-store directive",
        basis=f"GET each path at {C.EDGE}; compared the response's OWN "
              "Cache-Control / CDN-Cache-Control against cf-cache-status and age "
              "on the SAME response — a self-contradiction, so no threshold is "
              "invented and no body comparison is required",
        red_when="a response carrying no-store / no-cache / private comes back "
                 "from the edge as a stored copy with a non-zero age — the origin "
                 "says do not keep this and the edge is serving what it kept",
        remedy="Origin headers never beat a CF Cache Rule: rule 'Cache Public "
               "API' uses mode:override_origin and bulldozes no-store. The fix is "
               "a last-position bypass rule "
               "(set_cache_settings {cache:false}) for the path, not more headers "
               "and not a purge — a purge is re-cached on the next request."))

    if divergence:
        findings.append(Finding(
            key=stable_key("web", "edge-origin-divergence"), surface="web",
            seat=SEAT_NONE,
            title=f"{len(divergence)} cached path(s) differ in size from the origin",
            verdict=GAUGE, severity=INFO, value=len(divergence),
            evidence="; ".join(divergence[:6]),
            basis=f"byte length of the same path at {C.EDGE} and {C.ORIGIN} in the "
                  "same run, reported only where the edge served a stored copy",
            red_when="n/a — GAUGE by design. On a legitimately cacheable endpoint a "
                     "difference is expected: timestamps, counters and rotating "
                     "content all move between two requests. Calling that a defect "
                     "needs a 'materially different' threshold nobody has defined, "
                     "so the number is reported and the no-store check above is "
                     "what actually votes."))


def _probe_checkout_links(findings: list[Finding]) -> None:
    """Does every 'buy' button on the pricing page lead somewhere that sells?

    Deliberately limited to link LIVENESS. Whether a link charges the right price
    for the right plan can only be answered by asking Stripe what the link is,
    which needs a Stripe credential this runner does not hold — and guessing would
    be exactly the invented evidence this tool refuses to produce. A dead link,
    though, is decidable from the outside and has shipped to production before.
    """
    url = f"{C.EDGE}/pricing"
    try:
        _s, _h, body = fetch(url, timeout=C.HTTP_TIMEOUT)
    except Unreachable as e:
        findings.append(blind(
            key=stable_key("web", "checkout-links"), surface="web", seat=SEAT_ANON,
            title="Checkout links unobserved", why=str(e), basis=f"GET {url}"))
        return

    links = sorted(set(_STRIPE_RE.findall(body)))
    if not links:
        findings.append(Finding(
            key=stable_key("web", "checkout-links"), surface="web", seat=SEAT_ANON,
            title="No Stripe checkout links found on the pricing page",
            verdict=GAUGE, severity=INFO, value=0,
            evidence=f"{len(body)}b of HTML at {url} contained no "
                     "buy.stripe.com URL — checkout may be routed another way",
            basis=f"GET {url}, regex over the served HTML for buy.stripe.com URLs",
            red_when="n/a — gauge; checkout can legitimately be mounted via JS or "
                     "a server-side session rather than a static link"))
        return

    dead = []
    for link in links[:12]:   # bounded; the page is not expected to carry more
        try:
            status, _hh, _bb = fetch(link, timeout=C.HTTP_TIMEOUT, retries=1)
        except Unreachable as e:
            dead.append(f"{link} -> unreachable ({str(e)[:60]})")
            continue
        if status >= 400:
            dead.append(f"{link} -> HTTP {status}")

    findings.append(Finding(
        key=stable_key("web", "checkout-links"), surface="web", seat=SEAT_ANON,
        title=(f"All {len(links)} checkout links resolve" if not dead else
               f"{len(dead)} of {len(links)} checkout links are DEAD"),
        verdict=PASS if not dead else RED,
        severity=INFO if not dead else CRITICAL, value=len(dead),
        evidence="; ".join(dead) if dead else
                 f"{len(links)} distinct buy.stripe.com link(s), all 2xx/3xx",
        basis=f"GET {url}, then a live GET of each distinct buy.stripe.com URL "
              "found in the served HTML",
        red_when="a checkout link on the live pricing page returns >=400 — a "
                 "visitor who decided to pay us cannot",
        remedy="This class has shipped before as three live 403 buttons from a "
               "capital I in place of a lowercase l. Liveness only: pricing "
               "correctness needs a Stripe credential and must not be guessed."))
