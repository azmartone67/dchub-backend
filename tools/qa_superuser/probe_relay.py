#!/usr/bin/env python3
"""The agent→human relay, probed from both ends — the funnel's only bridge.

Two regressions this guards, both already suffered once:

1. **The link that measures humans stopped being handed to agents.** The
   funnel's human_acted stage reads opens of the relay artifacts, so if a
   gated envelope ships without ``for_your_human`` the stage is structurally
   zero and everyone downstream debates "appeal" while measuring nothing
   (the server.mjs:125 class — four consecutive live calls returned
   for_your_human=false in July; a probe here would have caught it same-day).

2. **The gateway's own machinery consuming the handoff.** ``_autoRedeemClaim``
   machine-redeemed ~96% of minted claims in 0.79s median — every high-intent
   moment became a free key for the agent instead of a decision for a human.
   Fixed 2026-08-16 (mcp-server #193, auto-redeem now opt-in via
   ``DCHUB_AUTO_REDEEM_ENABLE``, default OFF) — so the platform's own contract
   is ZERO machine redemptions, and the relay-watch endpoint publishes its own
   machine threshold (``threshold_seconds: 5``). No invented targets: both
   thresholds here are the platform's.

Seat notes. The presence check runs ANON — the seat real agents arrive from.
It deliberately calls FLAGSHIP_TOOL, whose (ip, tool, day) budget the earlier
mcp probes have usually spent by the time this runs (PROBES order: mcp first),
so the call lands on the GATED path where the relay link is contractually
present. If the budget happens to be intact the envelope is a full answer,
where absence is legitimate — that files as a GAUGE, never RED (the check
declines to convict on a condition it did not reach). The arbitrage check runs
from the ADMIN seat against the Railway ORIGIN (admin GETs through the edge
are cache-poisoned; the beat precedent applies).

Opening the relay URL stamps an open — that is safe by design: the funnel's
human_acted v3 is probe-excluded by User-Agent, and this probe rides the
harness UA precisely so its own verification traffic can never inflate the
metric it verifies (the /go/c QA rule).
"""
from __future__ import annotations

import re
import urllib.parse

from . import config as C
from .finding import (BLIND, CRITICAL, GAUGE, INFO, MAJOR, PASS, RED,
                      SEAT_ADMIN, SEAT_ANON, Finding, stable_key)
from .http import MCPSession, Unreachable, envelope_text, fetch, get_json

SURFACE = "mcp"

# structuredContent markers that identify a GATED / paywall-shaped envelope —
# the condition under which the gateway's buildHumanRelay contract applies.
# Kept explicit and ordered; the basis string names them so a future marker
# rename shows up as a basis mismatch, not a silent no-op.
_GATED_MARKERS = ("_gated", "preview_is_partial", "agent_payment", "upgrade")


def is_gated_shape(sc: dict) -> bool:
    """True when the envelope is the paywall/gated shape (any marker truthy
    for flags, or present at all for the offer/upgrade blocks)."""
    if not isinstance(sc, dict):
        return False
    for k in ("_gated", "preview_is_partial"):
        if sc.get(k):
            return True
    for k in ("agent_payment", "upgrade"):
        if k in sc:
            return True
    return False


def relay_presence_verdict(gated: bool, has_link: bool):
    """(verdict, severity). RED only on the reached condition: a gated
    envelope missing its human link. An ungated full answer proves nothing
    about the relay and gauges instead of convicting."""
    if not gated:
        return (GAUGE, INFO)
    return (PASS, MAJOR) if has_link else (RED, MAJOR)


def arbitrage_verdict(minted, machine):
    """(verdict, severity). The post-#193 contract is zero machine
    redemptions; a window with no mints judges nothing."""
    try:
        minted = int(minted or 0)
        machine = int(machine or 0)
    except Exception:
        return (GAUGE, INFO)
    if minted <= 0:
        return (GAUGE, INFO)
    return (RED, CRITICAL) if machine > 0 else (PASS, CRITICAL)


# ── the THIRD regression: the relayed link stops carrying an identity ───────
# Ship #2's brief asserted a "session lost" symptom on the unlock path and no
# one had produced the failing trace. Walked by hand 2026-09-09 there is none:
# /go/c/<token> 302s to Stripe with the session in client_reference_id. That
# answer cost an hour of manual probing and was worth having; leaving it as a
# one-off means the next person argues it from priors again.
#
# ★ IT CHECKS THE LINK IN THE TEXT, NOT THE ONE IN structuredContent.
# _check_presence above reads structuredContent.for_your_human.url — the
# /upgrade/h/ artifact. The block a client renders and a model relays is
# content[].text, and the link IN it is /go/c/<token>, a different endpoint
# writing a different table. Both are human handoffs and they had one probe
# between them.
_GO_C_RE = re.compile(r"https://[a-z0-9.\-]+/go/c/[A-Za-z0-9_\-]+\.[a-f0-9]+")
_STRIPE_HOSTS = ("buy.stripe.com", "checkout.stripe.com")
_REDIRECT_CODES = (301, 302, 303, 307, 308)


def relayed_checkout_url(text: str):
    """The /go/c checkout link an agent would relay, from the envelope TEXT.

    None when the text carries none — which is a GAUGE, not a conviction: an
    ungated envelope has no reason to offer one.
    """
    m = _GO_C_RE.search(text or "")
    return m.group(0) if m else None


def checkout_binding_verdict(status: int, location: str):
    """(verdict, severity, reason) for one hop of the relayed checkout link.

    RED is reserved for the three ways a human is actually lost:
      · the link does not redirect at all
      · it redirects somewhere that is not a payment processor — the honest
        fallback checkout_click_tracker uses for a token it cannot verify, and
        from the human's side indistinguishable from being dumped on /pricing
      · it reaches the processor carrying no client_reference_id, which is the
        "session lost" symptom by its proper name: the sale can complete and
        nothing joins it back to the agent that asked for it
    """
    if status not in _REDIRECT_CODES:
        return (RED, MAJOR,
                "the relayed checkout link answered HTTP %s instead of "
                "redirecting to a checkout" % status)
    loc = location or ""
    host = urllib.parse.urlparse(loc).netloc.lower()
    if host not in _STRIPE_HOSTS:
        return (RED, MAJOR,
                "the relayed checkout link redirects to %r, not to a payment "
                "processor — a human following it lands somewhere they cannot "
                "buy the thing the message named" % (loc[:120] or "(no Location)"))
    crid = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get(
        "client_reference_id", [""])[0]
    if not crid.strip():
        return (RED, MAJOR,
                "the relayed checkout link reaches Stripe with NO "
                "client_reference_id — a completed sale cannot be joined back "
                "to the agent session that asked for it")
    return (PASS, MAJOR,
            "redirects to %s carrying client_reference_id" % host)


def _fyh(sc: dict):
    """The for_your_human object, from the ONE place the contract puts it:
    top-level structuredContent (2026-07-28 moved it OUT of upgrade.*)."""
    v = (sc or {}).get("for_your_human")
    return v if isinstance(v, dict) and v.get("url") else None


def _check_presence(findings: list) -> None:
    key = stable_key("relay", "anon", "for_your_human")
    basis = (f"anon MCP tools/call {C.FLAGSHIP_TOOL}; gated markers "
             f"{_GATED_MARKERS} in structuredContent; link read at "
             f"structuredContent.for_your_human.url (top-level contract); "
             f"link then fetched with the harness UA (probe-excluded funnel)")
    red_when = ("the envelope is the GATED shape and carries no "
                "for_your_human.url, or the URL answers >= 400 when opened "
                "immediately")
    try:
        s = MCPSession(C.MCP_URL, timeout=C.MCP_TIMEOUT).open()
        env = s.call(C.FLAGSHIP_TOOL, {"market": "Northern Virginia"})
    except Unreachable as e:
        findings.append(Finding(
            key=key, surface=SURFACE, seat=SEAT_ANON,
            title="relay presence unobserved — MCP unreachable",
            verdict=BLIND, severity=INFO,
            evidence=str(e)[:200], basis=basis, red_when=red_when))
        return
    except Exception as e:
        findings.append(Finding(
            key=key, surface=SURFACE, seat=SEAT_ANON,
            title="relay presence unobserved — call failed",
            verdict=BLIND, severity=INFO,
            evidence=f"{type(e).__name__}: {str(e)[:160]}",
            basis=basis, red_when=red_when, instrument_fault=True))
        return

    sc = (env or {}).get("structuredContent") or {}
    gated = is_gated_shape(sc)
    link = _fyh(sc)
    verdict, severity = relay_presence_verdict(gated, bool(link))

    if verdict == GAUGE:
        findings.append(Finding(
            key=key, surface=SURFACE, seat=SEAT_ANON,
            title="relay presence not judged — envelope was an ungated full answer",
            verdict=GAUGE, severity=INFO,
            evidence=(f"gated markers absent; sc keys: "
                      f"{sorted(sc.keys())[:12]}"),
            basis=basis,
            red_when=("n/a — a GAUGE. The gated condition was not reached this "
                      "run (budget intact); the contract applies only to the "
                      "gated shape")))
        return

    if verdict == RED:
        findings.append(Finding(
            key=key, surface=SURFACE, seat=SEAT_ANON,
            title="GATED envelope ships NO human relay link",
            verdict=RED, severity=MAJOR,
            evidence=(f"gated markers present "
                      f"({[k for k in _GATED_MARKERS if k in sc or sc.get(k)]}), "
                      f"for_your_human absent/linkless; sc keys "
                      f"{sorted(sc.keys())[:12]}"),
            basis=basis, red_when=red_when,
            remedy=("server.mjs relay emission — the :125 class: gated paths "
                    "returning before buildPaywallExtras attaches the link")))
        return

    # link present → open it like a human would, immediately.
    try:
        status, _hdrs, _body = fetch(link["url"], timeout=20)
    except Unreachable as e:
        findings.append(Finding(
            key=key, surface=SURFACE, seat=SEAT_ANON,
            title="relay link present but UNFETCHABLE",
            verdict=BLIND, severity=INFO,
            evidence=f"{link['url'][:80]} -> {str(e)[:120]}",
            basis=basis, red_when=red_when))
        return
    if status >= 400:
        findings.append(Finding(
            key=key, surface=SURFACE, seat=SEAT_ANON,
            title=f"relay link answers HTTP {status} at mint-time",
            verdict=RED, severity=MAJOR,
            evidence=f"GET {link['url'][:80]} -> {status} within seconds of mint",
            basis=basis, red_when=red_when,
            remedy="a link handed to a human must be live when handed over"))
    else:
        findings.append(Finding(
            key=key, surface=SURFACE, seat=SEAT_ANON,
            title="gated envelope carries a live human relay link",
            verdict=PASS, severity=MAJOR,
            evidence=f"for_your_human present; GET {link['url'][:80]} -> {status}",
            basis=basis, red_when=red_when))


def _check_arbitrage(findings: list) -> None:
    key = stable_key("relay", "admin", "machine_arbitrage")
    basis = (f"admin GET {C.ORIGIN}/api/v1/admin/relay-watch?days=1 (origin, "
             f"not edge — admin GETs are edge-cache-poisoned); fields "
             f"stages['2_minted'].claims and "
             f"stages['3_redeemed_by_MACHINE'].count; the endpoint's own "
             f"threshold_seconds defines 'machine'")
    red_when = ("any claim in the window is machine-redeemed while the "
                "post-#193 contract (auto-redeem opt-in, default OFF) promises "
                "zero")
    if not C.ADMIN_KEY:
        findings.append(Finding(
            key=key, surface=SURFACE, seat=SEAT_ADMIN,
            title="arbitrage unobserved — no admin credential in this seat",
            verdict=BLIND, severity=INFO,
            evidence="DCHUB_ADMIN_KEY absent", basis=basis, red_when=red_when))
        return
    try:
        code, doc = get_json(
            f"{C.ORIGIN}/api/v1/admin/relay-watch?days=1",
            headers={"X-Admin-Key": C.ADMIN_KEY}, timeout=25)
    except Unreachable as e:
        findings.append(Finding(
            key=key, surface=SURFACE, seat=SEAT_ADMIN,
            title="arbitrage unobserved — relay-watch unreachable",
            verdict=BLIND, severity=INFO,
            evidence=str(e)[:200], basis=basis, red_when=red_when))
        return
    if code != 200 or not isinstance(doc, dict):
        findings.append(Finding(
            key=key, surface=SURFACE, seat=SEAT_ADMIN,
            title=f"arbitrage unobserved — relay-watch HTTP {code}",
            verdict=BLIND, severity=INFO,
            evidence=f"HTTP {code}, body type {type(doc).__name__}",
            basis=basis, red_when=red_when))
        return

    stages = doc.get("stages") or {}
    minted = ((stages.get("2_minted") or {}).get("claims"))
    machine_stage = stages.get("3_redeemed_by_MACHINE") or {}
    machine = machine_stage.get("count")
    median = ((machine_stage.get("gap_histogram") or {}).get("median_seconds"))
    verdict, severity = arbitrage_verdict(minted, machine)

    if verdict == GAUGE:
        findings.append(Finding(
            key=key, surface=SURFACE, seat=SEAT_ADMIN,
            title="arbitrage not judged — zero mints in the last day",
            verdict=GAUGE, severity=INFO,
            evidence=f"minted={minted} machine={machine}",
            basis=basis,
            red_when=("n/a — a GAUGE. No claims minted in the window, so the "
                      "zero-machine contract had nothing to bind"),
            value=0))
        return
    findings.append(Finding(
        key=key, surface=SURFACE, seat=SEAT_ADMIN,
        title=("machine arbitrage REGRESSED — claims are being self-redeemed"
               if verdict == RED else
               "no machine redemptions — the #193 auto-redeem fix is holding"),
        verdict=verdict, severity=severity,
        evidence=(f"minted={minted}, machine-redeemed={machine}, "
                  f"median gap={median}s, machine threshold="
                  f"{machine_stage.get('threshold_seconds')}s (endpoint's own)"),
        basis=basis, red_when=red_when, value=machine,
        remedy=("check DCHUB_AUTO_REDEEM_ENABLE on the mcp-server service — "
                "the flag defaulting ON again is the #193 regression"
                if verdict == RED else "")))



def _check_checkout_binding(findings: list) -> None:
    key = stable_key("relay", "anon", "checkout_binding")
    basis = (f"anon MCP tools/call {C.FLAGSHIP_TOOL}; the /go/c link is read "
             f"from content[].text — the block a client renders and a model "
             f"relays — NOT from structuredContent, which carries the other "
             f"artifact; the link is then fetched ONCE with the harness UA and "
             f"allow_redirects=False, and the verdict reads the Location "
             f"header only. Stripe is never called.")
    red_when = ("the relayed checkout link does not redirect, redirects "
                "somewhere that is not a payment processor, or reaches one "
                "with no client_reference_id")
    try:
        s = MCPSession(C.MCP_URL, timeout=C.MCP_TIMEOUT).open()
        env = s.call(C.FLAGSHIP_TOOL, {"market": "Northern Virginia"})
    except Unreachable as e:
        findings.append(Finding(
            key=key, surface=SURFACE, seat=SEAT_ANON,
            title="checkout binding unobserved — MCP unreachable",
            verdict=BLIND, severity=INFO,
            evidence=str(e)[:200], basis=basis, red_when=red_when))
        return
    except Exception as e:
        findings.append(Finding(
            key=key, surface=SURFACE, seat=SEAT_ANON,
            title="checkout binding unobserved — call failed",
            verdict=BLIND, severity=INFO,
            evidence=f"{type(e).__name__}: {str(e)[:160]}",
            basis=basis, red_when=red_when, instrument_fault=True))
        return

    url = relayed_checkout_url(envelope_text(env))
    if not url:
        # Same rule as _check_presence: an envelope that never offered the
        # link did not reach the condition, and a probe that convicts on a
        # condition it did not reach flaps with the runner IP's daily budget.
        findings.append(Finding(
            key=key, surface=SURFACE, seat=SEAT_ANON,
            title="checkout binding not judged — no /go/c link in the text",
            verdict=GAUGE, severity=INFO,
            evidence=("envelope text carries no /go/c URL; "
                      f"sc keys {sorted((env.get('structuredContent') or {}).keys())[:12]}"),
            basis=basis,
            red_when=("n/a — a GAUGE. The gated text did not offer a relayed "
                      "checkout link this run, so the binding contract had "
                      "nothing to bind")))
        return

    try:
        status, hdrs, _body = fetch(url, timeout=20, allow_redirects=False)
    except Unreachable as e:
        findings.append(Finding(
            key=key, surface=SURFACE, seat=SEAT_ANON,
            title="relayed checkout link UNFETCHABLE",
            verdict=BLIND, severity=INFO,
            evidence=f"{url[:80]} -> {str(e)[:120]}",
            basis=basis, red_when=red_when))
        return

    location = hdrs.get("Location") or hdrs.get("location") or ""
    verdict, severity, reason = checkout_binding_verdict(status, location)
    findings.append(Finding(
        key=key, surface=SURFACE, seat=SEAT_ANON,
        title=("the relayed checkout link loses the human"
               if verdict == RED else
               "relayed checkout link reaches Stripe with the session bound"),
        verdict=verdict, severity=severity,
        evidence=f"GET {url[:70]} -> {status} Location={location[:130]}",
        basis=basis, red_when=red_when,
        remedy=("routes/checkout_click_tracker._verify (a token it cannot "
                "verify falls back to /pricing) or server.mjs _goUrl, which "
                "emits the DIRECT Stripe link when DCHUB_INTERNAL_KEY is unset"
                if verdict == RED else "")))


def probe(findings: list) -> None:
    """Runner convention: append to the given list, return None."""
    _check_presence(findings)
    _check_checkout_binding(findings)
    _check_arbitrage(findings)
