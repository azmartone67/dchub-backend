#!/usr/bin/env python3
"""Surface 5 — the CONTRACTS a public page makes with whoever reads it.

Every other probe asks "did the wire carry something?". This one asks "is what
it carried TRUE against the platform's own source?" — which is a different
question, and the one that eleven defects walked through on 2026-08-05.

WHY THIS SURFACE EXISTS

That day a hand-run sweep found eleven live defects while the board reported
0 red across 26 checks. The board was not wrong; it was NARROW. Of the eleven,
three had a check built for them the same day and now close themselves. The
other eight had none, which meant something worse than "nobody was told": if any
of them regressed, no issue would open, and none could ever close.

Every check below is a defect that WAS live, written so it could not return
quietly:

  numbers        /pricing advertised "81 MCP tools" against a live 82 and
                 "15,000+ facilities" against a canon floor of 16,500+. The
                 homepage published TWO different facility floors at once.
  catalog        /operators served "0 tracked" under index,follow, with a meta
                 description reading "Live directory of 0 data center operators"
                 while /api/v1/stats reported 6,432 providers.
  brief          /api/v1/operators/equinix reported 543 facilities in the same
                 second /api/v1/operator-brief/equinix answered
                 "operator_not_found".
  freshness      /api/v1/data-freshness returned {"success": true, "sources": []}
                 on every call and had NEVER once returned a row.
  redirects      /press spent hours in an infinite 308 loop — a real browser got
                 ERR_TOO_MANY_REDIRECTS — while /press was not in PUBLIC_PAGES,
                 and the pages that WERE checked asserted only "2xx", which a
                 308 satisfies.
  admin          /admin answered HTTP 200 to Googlebot with three of four
                 indexation signals saying "index".
  soft-404       /operators/<slug>/brief returned 200 + "not yet in our tracked
                 set" for EVERY slug — an unbounded crawl sink of the exact shape
                 robots.txt already blocks for /sites/<slug>.

NO INVENTED THRESHOLDS. Canon floors come from /api/v1/canon/phrases, catalog
counts from the platform's own API, and 404-vs-200 and redirect-loop are
decidable facts. Where the platform declares nothing, this file reports a GAUGE.
"""
from __future__ import annotations

import re

import requests

from . import config as C
from .finding import (GAUGE, INFO, MAJOR, MINOR, PASS, RED, SEAT_ANON,
                      SEAT_CRAWLER, Finding, blind, stable_key)
from .http import QA_UA, Unreachable, body_text, fetch


def probe(findings: list[Finding]) -> None:
    canon = _canon(findings)
    _check_published_numbers(canon, findings)
    _check_catalog_not_zero(findings)
    _check_brief_resolves(findings)
    _check_freshness_returns_rows(findings)
    _check_redirects_terminate(findings)
    _check_admin_closed_to_crawlers(findings)


# ── the platform's own declared floors ──────────────────────────────────────
def _canon(findings: list[Finding]) -> dict:
    """Fetch /api/v1/canon/phrases — the source every published number owes."""
    url = f"{C.EDGE}{C.CANON_PHRASES_PATH}"
    try:
        status, _h, body = fetch(url, timeout=C.HTTP_TIMEOUT)
    except Unreachable as e:
        findings.append(blind(
            key=stable_key("contract", "canon"), surface="contract",
            seat=SEAT_ANON, title="Canon floors unobserved",
            why=str(e), basis=f"GET {url}"))
        return {}
    if status != 200:
        findings.append(blind(
            key=stable_key("contract", "canon"), surface="contract",
            seat=SEAT_ANON, title="Canon floors unobserved",
            why=f"HTTP {status}", basis=f"GET {url}"))
        return {}
    try:
        import json
        return json.loads(body) or {}
    except Exception as e:  # noqa: BLE001
        findings.append(blind(
            key=stable_key("contract", "canon"), surface="contract",
            seat=SEAT_ANON, title="Canon floors unobserved",
            why=f"unparseable: {type(e).__name__}", basis=f"GET {url}"))
        return {}


def _num(text: str):
    """'16,500+' -> 16500. None when there is no number in there."""
    m = re.search(r"[\d,]+", str(text or ""))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def verdict_for_published(published: int, canon: int) -> tuple[str, str, str]:
    """One published number vs the platform's own floor. PURE — so it is tested.

    ★ A floor ROUNDS DOWN. Publishing LESS than canon is conservative and
    honest; publishing MORE is an over-claim. The asymmetry is the platform's
    own stated doctrine ("floors round DOWN and can never exceed the live
    count"), not a threshold invented here.
    """
    if published > canon:
        return RED, MAJOR, "over-claims"
    if published < canon:
        return PASS, INFO, "rounds down"
    return PASS, INFO, "matches"


def _check_published_numbers(canon: dict, findings: list[Finding]) -> None:
    """Does every number on a public page agree with the source that owns it?"""
    key = stable_key("contract", "published-numbers")
    if not canon:
        return  # _canon already filed the blindness
    over, drift, checked = [], [], 0
    for path, canon_key, pattern in C.PUBLISHED_NUMBERS:
        floor = _num(canon.get(canon_key))
        if floor is None:
            continue
        url = f"{C.EDGE}{path}"
        try:
            status, _h, body = fetch(url, timeout=C.HTTP_TIMEOUT)
        except Unreachable:
            continue
        if status >= 400:
            continue
        found = {_num(m) for m in re.findall(pattern, body)} - {None}
        if not found:
            continue
        checked += 1
        # ★ TWO VALUES FOR ONE POPULATION is its own defect, independent of
        #   whether either is right — the homepage published 16,500+ and 16,100+
        #   simultaneously, and a reader cannot tell which to believe.
        if len(found) > 1:
            drift.append(f"{path} publishes {sorted(found)} for {canon_key!r}")
        for v in found:
            vd, _sev, _lab = verdict_for_published(v, floor)
            if vd == RED:
                over.append(f"{path}: {canon_key}={v:,} > canon floor {floor:,}")

    if checked == 0:
        findings.append(blind(
            key=key, surface="contract", seat=SEAT_ANON,
            title="Published numbers unobserved",
            why="no configured page/number pair could be read",
            basis=f"GET of {len(C.PUBLISHED_NUMBERS)} page(s)"))
        return

    problems = over + drift
    if problems:
        findings.append(Finding(
            key=key, surface="contract", seat=SEAT_ANON,
            title=f"{len(problems)} published number(s) disagree with canon",
            verdict=RED, severity=MAJOR, value=len(problems),
            evidence="; ".join(problems[:4])
                     + (f" (+{len(problems) - 4} more)" if len(problems) > 4 else "")
                     + f"; {checked} page/number pair(s) checked",
            basis=f"anonymous GET of each page, numbers extracted by regex and "
                  f"compared against {C.CANON_PHRASES_PATH} (the platform's own "
                  f"declared floors)",
            red_when="a public page publishes a number ABOVE the canon floor "
                     "(an over-claim), or publishes TWO different values for one "
                     "population",
            remedy="Bind the figure to /api/v1/canon/phrases the way the "
                   "homepage moat band already does (data-canon-floor). A number "
                   "corrected by hand drifts again the next time canon moves — "
                   "that is how the homepage came to publish two floors at once."))
        return

    findings.append(Finding(
        key=key, surface="contract", seat=SEAT_ANON,
        title=f"All {checked} published number(s) agree with canon",
        verdict=PASS, severity=INFO, value=checked,
        evidence=f"{checked} page/number pair(s); canon floors "
                 f"{ {k: canon.get(k) for k in ('tools', 'facilities')} }",
        basis=f"anonymous GET, compared against {C.CANON_PHRASES_PATH}",
        red_when="a page publishes a number above the canon floor, or two "
                 "different values for one population"))


# ── a catalog page that publishes a zero ────────────────────────────────────
def _check_catalog_not_zero(findings: list[Finding]) -> None:
    """An indexable directory that says it tracks nothing.

    ★ RED, not a gauge: the page itself is the claim. /operators shipped
    `<title>Data Center Operators · 0 tracked</title>` with index,follow and a
    meta description reading "Live directory of 0 data center operators", while
    the platform's own API held thousands. A crawler that lands there learns
    something false about the business.
    """
    key = stable_key("contract", "catalog-not-zero")
    zeros, checked = [], 0
    for path, api in C.CATALOG_PAGES:
        try:
            _s, _h, body = fetch(f"{C.EDGE}{path}", timeout=C.HTTP_TIMEOUT)
            a_status, _ah, a_body = fetch(f"{C.EDGE}{api}", timeout=C.HTTP_TIMEOUT)
        except Unreachable:
            continue
        checked += 1
        # The page's own headline count, taken from its <title>.
        m = re.search(r"<title>[^<]*?([\d,]+)\s+tracked", body, re.I)
        page_n = _num(m.group(1)) if m else None
        api_n = None
        try:
            import json
            api_n = (json.loads(a_body) or {}).get("count") if a_status == 200 else None
        except Exception:  # noqa: BLE001
            api_n = None
        indexable = bool(re.search(r'<meta[^>]+name=["\']robots["\'][^>]*content='
                                   r'["\'][^"\']*index', body, re.I))
        if page_n == 0:
            zeros.append(f"{path} publishes '0 tracked'"
                         + (f" while {api} reports {api_n}" if api_n else "")
                         + (" — and is marked index,follow" if indexable else ""))

    if checked == 0:
        findings.append(blind(
            key=key, surface="contract", seat=SEAT_CRAWLER,
            title="Catalog pages unobserved", why="none could be read",
            basis="anonymous GET of each catalog page + its API"))
        return
    if zeros:
        findings.append(Finding(
            key=key, surface="contract", seat=SEAT_CRAWLER,
            title=f"{len(zeros)} catalog page(s) publish a ZERO count",
            verdict=RED, severity=MAJOR, value=len(zeros),
            evidence="; ".join(zeros[:3]),
            basis="anonymous GET; headline count read from <title>, compared "
                  "against the page's own backing API; robots meta read from "
                  "the served HTML",
            red_when="an indexable catalog page states it tracks 0 of something "
                     "the platform's own API can count",
            remedy="Either wire the page to the data, or serve it noindex until "
                   "it is wired. An indexable zero-state teaches crawlers and "
                   "AI agents a false fact about the business."))
        return
    findings.append(Finding(
        key=key, surface="contract", seat=SEAT_CRAWLER,
        title=f"All {checked} catalog page(s) publish a non-zero count",
        verdict=PASS, severity=INFO, value=checked,
        evidence=f"{checked} page(s) checked against their backing API",
        basis="anonymous GET; <title> count vs the page's own API",
        red_when="an indexable catalog page states it tracks 0 of something"))


# ── the brief surface, and indexable soft-404s ──────────────────────────────
def _check_brief_resolves(findings: list[Finding]) -> None:
    """A tracked operator must resolve on the brief surface.

    ★ TWO defects in one check, because they had one cause. The API said 543
    facilities while the brief said "operator_not_found" — a self-contradiction
    the platform published to itself. And the brief's not-found page answered
    HTTP 200 for EVERY slug, which is an unbounded crawl sink of exactly the
    shape robots.txt already blocks for /sites/<slug>.
    """
    key = stable_key("contract", "brief-resolves")
    op = C.BRIEF_PROBE_OPERATOR
    try:
        o_s, _h, o_body = fetch(f"{C.EDGE}/api/v1/operators/{op}",
                                timeout=C.HTTP_TIMEOUT)
        b_s, _bh, b_body = fetch(f"{C.EDGE}/api/v1/operator-brief/{op}",
                                 timeout=C.HTTP_TIMEOUT)
        f_s, _fh, _fb = fetch(f"{C.EDGE}/operators/qa-superuser-nonexistent-{op}/brief",
                              timeout=C.HTTP_TIMEOUT)
    except Unreachable as e:
        findings.append(blind(
            key=key, surface="contract", seat=SEAT_ANON,
            title="Operator brief unobserved", why=str(e),
            basis=f"GET /api/v1/operators/{op} and /api/v1/operator-brief/{op}"))
        return

    import json
    def _j(t):
        try:
            return json.loads(t) or {}
        except Exception:  # noqa: BLE001
            return {}

    api_n = _j(o_body).get("facility_count")
    brief = _j(b_body)
    problems = []
    if o_s == 200 and api_n and not brief.get("ok"):
        problems.append(
            f"/api/v1/operators/{op} reports {api_n} facilities but "
            f"/api/v1/operator-brief/{op} returns {brief.get('error')!r}")
    # A "we do not have that" page answering 200 is an indexable soft-404.
    if f_s == 200:
        problems.append(
            f"/operators/<invented-slug>/brief returns HTTP 200 — an unbounded "
            f"soft-404 crawl sink")

    if problems:
        findings.append(Finding(
            key=key, surface="contract", seat=SEAT_ANON,
            title=f"{len(problems)} contradiction(s) on the operator brief surface",
            verdict=RED, severity=MAJOR, value=len(problems),
            evidence="; ".join(problems),
            basis=f"anonymous GET of /api/v1/operators/{op}, "
                  f"/api/v1/operator-brief/{op}, and an INVENTED slug to test "
                  f"the not-found path",
            red_when="the brief cannot resolve an operator the operators API "
                     "counts, or the not-found page answers 200 for a slug that "
                     "does not exist",
            remedy="Both symptoms had one cause: every query in the brief module "
                   "read `merged_at IS NULL AND is_duplicate = 0`, the drained "
                   "pending-review queue. The fleet filter is "
                   "COALESCE(is_duplicate, 0) = 0 alone."))
        return

    findings.append(Finding(
        key=key, surface="contract", seat=SEAT_ANON,
        title=f"Operator brief resolves {op} and 404s an invented slug",
        verdict=PASS, severity=INFO,
        evidence=f"/api/v1/operators/{op} facility_count={api_n}; brief ok="
                 f"{brief.get('ok')}; invented slug -> HTTP {f_s}",
        basis=f"anonymous GET of both APIs plus an invented slug",
        red_when="the brief cannot resolve an operator the API counts, or the "
                 "not-found page answers 200"))


# ── an endpoint whose whole job is freshness ────────────────────────────────
def _check_freshness_returns_rows(findings: list[Finding]) -> None:
    """success:true with nothing measured is not success.

    ★ /api/v1/data-freshness answered `{"success": true, "sources": []}` on
    every call and had NEVER returned a row — a RealDictCursor read by integer
    index raised KeyError on all 10 candidate tables before a single MAX() ran,
    and a bare except swallowed it. Two CI workflows grep this response as proof
    the loaders landed data, so that lane had always verified nothing.
    """
    key = stable_key("contract", "freshness-rows")
    url = f"{C.EDGE}/api/v1/data-freshness"
    try:
        status, _h, body = fetch(url, timeout=C.HTTP_TIMEOUT)
    except Unreachable as e:
        findings.append(blind(key=key, surface="contract", seat=SEAT_ANON,
                              title="Freshness endpoint unobserved",
                              why=str(e), basis=f"GET {url}"))
        return
    import json
    try:
        d = json.loads(body) or {}
    except Exception as e:  # noqa: BLE001
        findings.append(blind(key=key, surface="contract", seat=SEAT_ANON,
                              title="Freshness endpoint unobserved",
                              why=f"unparseable: {type(e).__name__}",
                              basis=f"GET {url}"))
        return

    sources = d.get("sources") or []
    skipped = d.get("skipped") or []
    if d.get("success") and not sources:
        findings.append(Finding(
            key=key, surface="contract", seat=SEAT_ANON,
            title="Freshness endpoint reports success while measuring nothing",
            verdict=RED, severity=MAJOR, value=0,
            evidence=f"HTTP {status} {{'success': true, 'sources': []}} at {url}"
                     + (f"; {len(skipped)} skipped" if skipped else
                        "; no `skipped` key either, so it does not even say what "
                        "it could not read"),
            basis=f"anonymous GET {url}, `success` and `sources` fields",
            red_when="the endpoint whose entire job is reporting freshness "
                     "returns zero sources and calls that success",
            remedy="Report per-source failures and let `success` reflect whether "
                   "anything was actually measured. Two CI workflows grep this "
                   "response as proof the loaders ran."))
        return

    findings.append(Finding(
        key=key, surface="contract", seat=SEAT_ANON,
        title=f"Freshness endpoint reports {len(sources)} measured source(s)",
        verdict=PASS if sources else GAUGE, severity=INFO, value=len(sources),
        evidence=f"{len(sources)} source(s)"
                 + (f", {len(skipped)} skipped ({skipped[0].get('reason','')[:60]}…)"
                    if skipped else "")
                 + f"; success={d.get('success')}",
        basis=f"anonymous GET {url}",
        red_when="success is true while sources is empty" if sources else
                 "n/a — GAUGE: the endpoint reported no sources AND did not claim "
                 "success, which is an honest degraded answer",
        # ★lane 6: this check reduces to ONE reading of ONE field, so it can be
        # re-judged by the claim ledger on a clock instead of waiting for the
        # next manual harness run. `>= len(sources)` asserts the floor observed
        # now: sources appearing later is fine, sources DISAPPEARING is the
        # regression this endpoint exists to make visible — and it is exactly
        # what went unnoticed when it answered {"success": true, "sources": []}.
        claim_metric=("get:/api/v1/data-freshness sources#len" if sources else ""),
        claim_expect=(f">= {len(sources)}" if sources else "")))


# ── redirects that never land ───────────────────────────────────────────────
def _check_redirects_terminate(findings: list[Finding]) -> None:
    """Follow every public page. A loop is a RED, not blindness.

    ★ THE DISTINCTION THIS ENCODES. `fetch()` follows redirects, so a loop
    raises TooManyRedirects — a transport exception — and the harness would file
    it as BLIND ("could not observe"). That is wrong: a redirect loop is a
    DEFINITE observation that the page is broken. A real browser showed
    ERR_TOO_MANY_REDIRECTS on /press for hours while every instrument stayed
    green, partly because /press was not on the list and partly because the
    pages that WERE checked asserted only "2xx" — which a 308 satisfies.
    """
    key = stable_key("contract", "redirects-terminate")
    looping, unreachable, ok = [], 0, 0
    for path in C.PUBLIC_PAGES:
        url = f"{C.EDGE}{path}"
        try:
            r = requests.get(url, headers={"User-Agent": QA_UA},
                             timeout=C.HTTP_TIMEOUT, allow_redirects=True)
            ok += 1
            if len(r.history) >= 5:
                looping.append(f"{path} took {len(r.history)} redirect(s)")
        except requests.TooManyRedirects:
            looping.append(f"{path} never lands — redirect loop")
        except requests.RequestException:
            unreachable += 1

    if ok == 0 and not looping:
        findings.append(blind(
            key=key, surface="contract", seat=SEAT_ANON,
            title="Redirect termination unobserved",
            why=f"all {unreachable} page(s) failed at the transport layer",
            basis=f"anonymous GET of {len(C.PUBLIC_PAGES)} page(s), following "
                  f"redirects"))
        return
    if looping:
        findings.append(Finding(
            key=key, surface="contract", seat=SEAT_ANON,
            title=f"{len(looping)} public page(s) never finish redirecting",
            verdict=RED, severity=MAJOR, value=len(looping),
            evidence="; ".join(looping[:4])
                     + f"; {ok} page(s) landed, {unreachable} unreachable",
            basis=f"anonymous GET of {len(C.PUBLIC_PAGES)} public page(s) "
                  f"FOLLOWING redirects — a status-code-only check passes a 308",
            red_when="a public page redirects without ever landing, or takes "
                     ">=5 hops — a browser shows ERR_TOO_MANY_REDIRECTS",
            remedy="Look inside the WORKER HANDLER, not at the routing files. "
                   "Root cause on /press (frontend#1128, after four wrong "
                   "fixes): the handler asked `env.ASSETS` for '/press.html', "
                   "and CF Pages canonicalises every *.html asset to its pretty "
                   "URL with a 308 — so ASSETS answered '308 -> /press', "
                   "`pageResp.ok` was false, and the handler's fallback returned "
                   "that redirect to the caller. The page redirected to itself "
                   "forever. Fix: ask ASSETS for the PRETTY url ('/press'), and "
                   "never hand a 3xx from ASSETS back to the client. "
                   "Do NOT chase this in `_redirects` or `_routes.json`: a "
                   "worker-routed path ignores _redirects, and a '/x/*' include "
                   "ALSO matches bare '/x' (live: /docs, /operators, /relay, "
                   "/redeem are listed only as '/x/*' and all answer "
                   "worker-side), so 'the route is missing' is almost always a "
                   "misreading. Confirm which component emits the redirect "
                   "first: `curl -sSI` and check for x-dc-worker-version."))
        return
    findings.append(Finding(
        key=key, surface="contract", seat=SEAT_ANON,
        title=f"All {ok} public page(s) finish redirecting",
        verdict=PASS, severity=INFO, value=ok,
        evidence=f"{ok} page(s) landed following redirects"
                 + (f"; {unreachable} unreachable" if unreachable else ""),
        basis=f"anonymous GET of {len(C.PUBLIC_PAGES)} public page(s), "
              f"following redirects to a final response",
        red_when="a public page never lands, or takes >=5 hops"))


# ── admin surfaces open to crawlers ─────────────────────────────────────────
def _check_admin_closed_to_crawlers(findings: list[Finding]) -> None:
    """Ask as Googlebot. /admin answered 200 with 3 of 4 signals saying index."""
    key = stable_key("contract", "admin-noindex")
    open_to_bots, checked = [], 0
    for path in C.ADMIN_SURFACES:
        url = f"{C.EDGE}{path}"
        try:
            status, headers, body = fetch(
                url, headers={"User-Agent": C.GOOGLEBOT_UA},
                timeout=C.HTTP_TIMEOUT)
        except Unreachable:
            continue
        checked += 1
        if status >= 400 or status in (301, 302, 307, 308):
            continue  # not served to bots at all — fine
        hdr = ""
        for k, v in (headers or {}).items():
            if k.lower() == "x-robots-tag":
                hdr = str(v).lower()
        metas = re.findall(r'<meta[^>]+name=["\']robots["\'][^>]+content='
                           r'["\']([^"\']+)', body, re.I)
        says_index = [m for m in metas if "noindex" not in m.lower()]
        if "noindex" not in hdr or says_index:
            bits = []
            if "noindex" not in hdr:
                bits.append(f"X-Robots-Tag={hdr or '(absent)'!r}")
            if says_index:
                bits.append(f"{len(says_index)} meta robots say index: {says_index[:2]}")
            open_to_bots.append(f"{path} (HTTP {status}) — " + "; ".join(bits))

    if checked == 0:
        findings.append(blind(
            key=key, surface="contract", seat=SEAT_CRAWLER,
            title="Admin indexation unobserved", why="no admin surface read",
            basis=f"GET as {C.GOOGLEBOT_UA[:40]}…"))
        return
    if open_to_bots:
        findings.append(Finding(
            key=key, surface="contract", seat=SEAT_CRAWLER,
            title=f"{len(open_to_bots)} admin surface(s) invite indexing",
            verdict=RED, severity=MINOR, value=len(open_to_bots),
            evidence="; ".join(open_to_bots[:3]),
            basis="GET with a Googlebot User-Agent; X-Robots-Tag response "
                  "header and every <meta name=robots> in the served HTML",
            red_when="an admin surface serves 2xx to Googlebot without a "
                     "noindex header, or carries any meta robots that says index",
            remedy="A meta tag cannot override an HTTP header a crawler reads "
                   "first — the header and the page must agree. /admin once "
                   "carried BOTH an index,follow and a noindex,nofollow meta."))
        return
    findings.append(Finding(
        key=key, surface="contract", seat=SEAT_CRAWLER,
        title=f"All {checked} admin surface(s) are closed to crawlers",
        verdict=PASS, severity=INFO, value=checked,
        evidence=f"{checked} surface(s) served noindex to a Googlebot UA",
        basis="GET as Googlebot; X-Robots-Tag + every meta robots",
        red_when="an admin surface serves 2xx to Googlebot without noindex"))
