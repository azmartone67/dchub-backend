#!/usr/bin/env python3
"""smoke_pro_seat_brief.py — the market brief, checked from a paying seat.

2026-09-11. Three defects reached customers past every existing smoke, because
no smoke looked from a paying seat:

  1. A Pro render of /markets/midland-tx/brief was served, out of the edge
     cache, to logged-out visitors: x-market-brief-tier PRO, no paywall cards,
     the Pro "Download PDF" button.
  2. The reverse: a Pro user was served the cached anonymous paywall.
  3. "Download PDF" failed for every Pro user. The route imported weasyprint,
     which cannot load on the Railway image: three clicks, three 5xx.

This probe arrives the way a customer does, through the public edge with the
DCHUB_API_KEY seat, and checks the page, the PDF, and that neither tier is ever
handed the other tier's copy. Every URL carries a run-unique ?_= token, so the
probe never reads, or plants, a copy under a URL the public requests.

A render is classified by what the page SHOWS (paywall cards and which PDF
button), not by the tier header: the header is what the origin claimed, the
body is what the customer got. Unrecognised markup is BLIND, never a verdict.

Verdicts: PASS, RED, BLIND. BLIND means the check could not observe (no key,
the key is not a PRO+ seat, the edge did not answer, the markup changed). It is
never counted as a pass: it prints ::warning:: and names what went unmeasured.

Exit codes: 0 no RED · 1 at least one RED · 2 the must-fail control did not
fire, so this instrument cannot be trusted and reports nothing.

Usage: DCHUB_API_KEY=... python3 scripts/smoke_pro_seat_brief.py [--base URL] [--slug dallas]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid

import requests

PASS, RED, BLIND = "PASS", "RED", "BLIND"

PAYWALL_CARD = "PRO unlocks all sections"
PRO_PDF_BUTTON = 'class="pdf-btn pro"'
UPGRADE_PDF_BUTTON = "Upgrade to download PDF"

USER_AGENT = "dchub-qa-pro-seat-smoke/1.0 (+https://github.com/azmartone67/dchub-backend)"
TIMEOUT_S = 30


def render_kind(body: str) -> str:
    """'paid' | 'anonymous' | 'unrecognised', read off what the page shows."""
    body = body or ""
    cards = body.count(PAYWALL_CARD)
    pro_button = PRO_PDF_BUTTON in body
    upgrade_button = UPGRADE_PDF_BUTTON in body
    if cards == 0 and pro_button and not upgrade_button:
        return "paid"
    if cards > 0 and upgrade_button and not pro_button:
        return "anonymous"
    return "unrecognised"


def verdict_seat(status: int, kind: str):
    """Keyed read of a URL nobody has requested: no cached copy can exist, so
    the body is the origin's view of the key."""
    if status != 200:
        return BLIND, f"keyed read of a fresh URL answered HTTP {status}"
    if kind == "paid":
        return PASS, "the DCHUB_API_KEY seat renders the paid brief"
    if kind == "anonymous":
        return BLIND, ("the DCHUB_API_KEY seat renders the anonymous brief on a fresh URL, "
                       "so it is not a PRO+ seat and nothing paid can be checked")
    return BLIND, "brief markup not recognised; the probe needs updating, this is not a verdict"


def verdict_anonymous_after_paid(status: int, kind: str):
    if status != 200:
        return BLIND, f"anonymous read answered HTTP {status}"
    if kind == "anonymous":
        return PASS, "an anonymous read of the URL a paid read had just rendered got the paywall"
    if kind == "paid":
        return RED, ("an anonymous request was served the PAID render of a URL a paid "
                     "request had just read: Pro content is leaking to logged-out visitors")
    return BLIND, "brief markup not recognised"


def verdict_paid_after_anonymous(status: int, kind: str):
    if status != 200:
        return BLIND, f"keyed read answered HTTP {status}"
    if kind == "paid":
        return PASS, "a paid read of a URL an anonymous read had just cached got the paid brief"
    if kind == "anonymous":
        return RED, ("a paid request was served the cached ANONYMOUS render: a Pro "
                     "customer sees 'Unlock with PRO'")
    return BLIND, "brief markup not recognised"


def verdict_pro_pdf(status: int, content_type: str, body: bytes):
    body = body or b""
    if (status == 200 and (content_type or "").lower().startswith("application/pdf")
            and body.startswith(b"%PDF-") and b"%%EOF" in body[-2048:]):
        return PASS, f"a complete PDF downloaded ({len(body)} bytes)"
    return RED, (f"Pro PDF download failed: HTTP {status}, content-type {content_type!r}, "
                 f"first bytes {body[:60]!r}")


def verdict_pdf_cache_control(cache_control: str):
    directives = {d.strip().split("=")[0] for d in (cache_control or "").lower().split(",") if d.strip()}
    if directives & {"public", "s-maxage"}:
        return RED, f"the Pro-only PDF is publicly cacheable: Cache-Control {cache_control!r}"
    if "no-store" not in directives:
        return RED, f"the Pro-only PDF does not forbid storage: Cache-Control {cache_control!r}"
    return PASS, f"Cache-Control {cache_control!r}"


def verdict_anonymous_pdf(status: int):
    if status == 402:
        return PASS, "anonymous PDF request is refused with 402"
    if status == 200:
        return RED, "an anonymous request downloaded the Pro-only PDF"
    return RED, f"anonymous PDF request answered HTTP {status}, expected the 402 gate"


def control_fires() -> bool:
    """Must-fail control: every verdict above goes RED (or, for the seat, never
    PASS) on inputs that are known to be wrong. If one does not, the instrument
    cannot see the defect it is named for."""
    paid = f'<a {PRO_PDF_BUTTON} download>Download PDF</a>'
    anon = f'<div>{PAYWALL_CARD}</div><a class="pdf-btn upgrade">{UPGRADE_PDF_BUTTON}</a>'
    checks = [
        render_kind(paid) == "paid",
        render_kind(anon) == "anonymous",
        render_kind(paid + anon) == "unrecognised",
        verdict_seat(200, "anonymous")[0] == BLIND,
        verdict_anonymous_after_paid(200, "paid")[0] == RED,
        verdict_paid_after_anonymous(200, "anonymous")[0] == RED,
        verdict_pro_pdf(200, "application/json", b'{"error":"pdf_engine_unavailable"}')[0] == RED,
        verdict_pro_pdf(503, "application/json", b"")[0] == RED,
        verdict_pro_pdf(200, "application/pdf", b"%PDF-1.7 truncated")[0] == RED,
        verdict_pdf_cache_control("public, max-age=3600, s-maxage=3600")[0] == RED,
        verdict_anonymous_pdf(200)[0] == RED,
    ]
    return all(checks)


def run(base: str, slug: str, key: str, session=None, pause_s: float = 1.2):
    """Returns [(check, verdict, detail)]. Never raises for transport errors."""
    results = []
    if not key:
        return [("paid-seat", BLIND, "DCHUB_API_KEY is not set; the paid seat went unmeasured")]
    s = session or requests.Session()
    token = uuid.uuid4().hex[:12]
    page = f"{base.rstrip('/')}/markets/{slug}/brief"

    def get(url, keyed):
        headers = {"User-Agent": USER_AGENT}
        if keyed:
            headers["X-API-Key"] = key
        return s.get(url, headers=headers, timeout=TIMEOUT_S, allow_redirects=False)

    def attempt(name, fn):
        try:
            results.append((name, *fn()))
            return results[-1][1]
        except requests.RequestException as e:
            results.append((name, BLIND, f"the edge did not answer: {type(e).__name__}"))
            return BLIND

    seat_url = f"{page}?_=seat{token}"
    seat = attempt("pro-brief-renders-paid",
                   lambda: (lambda r: verdict_seat(r.status_code, render_kind(r.text)))(get(seat_url, True)))
    if seat != PASS:
        for name in ("anonymous-never-gets-the-paid-copy", "paid-never-gets-the-anonymous-copy",
                     "pro-pdf-downloads"):
            results.append((name, BLIND, "not checked: no working PRO+ seat"))
    else:
        time.sleep(pause_s)
        attempt("anonymous-never-gets-the-paid-copy",
                lambda: (lambda r: verdict_anonymous_after_paid(r.status_code, render_kind(r.text)))(get(seat_url, False)))

        def paid_after_anonymous():
            url = f"{page}?_=anonfirst{token}"
            get(url, False)
            time.sleep(pause_s)
            r = get(url, True)
            return verdict_paid_after_anonymous(r.status_code, render_kind(r.text))
        attempt("paid-never-gets-the-anonymous-copy", paid_after_anonymous)

        def pro_pdf():
            r = get(f"{page}.pdf?_=pdf{token}", True)
            v = verdict_pro_pdf(r.status_code, r.headers.get("Content-Type", ""), r.content)
            if r.status_code == 200:
                results.append(("pro-pdf-never-publicly-cacheable",
                                *verdict_pdf_cache_control(r.headers.get("Cache-Control", ""))))
            return v
        attempt("pro-pdf-downloads", pro_pdf)

    attempt("anonymous-pdf-is-402",
            lambda: verdict_anonymous_pdf(get(f"{page}.pdf?_=pdfanon{token}", False).status_code))
    return results


def report(results) -> int:
    reds = [r for r in results if r[1] == RED]
    blinds = [r for r in results if r[1] == BLIND]
    lines = ["| check | verdict | detail |", "|---|---|---|"]
    for name, verdict, detail in results:
        print(f"{verdict:5s}  {name}: {detail}")
        lines.append(f"| {name} | {verdict} | {detail} |")
        if verdict == RED:
            print(f"::error title=Pro-seat brief smoke: {name}::{detail}")
        elif verdict == BLIND:
            print(f"::warning title=Pro-seat brief smoke unmeasured: {name}::{detail}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("### Pro-seat market brief smoke\n\n" + "\n".join(lines) + "\n\n")
            if blinds and not reds:
                fh.write(f"**{len(blinds)} check(s) went unmeasured — this is not a pass.**\n")
    return 1 if reds else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="https://dchub.cloud")
    ap.add_argument("--slug", default="dallas")
    args = ap.parse_args(argv)
    if not control_fires():
        print("::error title=Pro-seat brief smoke::the must-fail control did not fire; "
              "this instrument cannot see the defects it checks for")
        return 2
    return report(run(args.base, args.slug, os.environ.get("DCHUB_API_KEY", "").strip()))


if __name__ == "__main__":
    sys.exit(main())
