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

from . import config as C
from .finding import (BLIND, CRITICAL, GAUGE, INFO, MAJOR, PASS, RED,
                      SEAT_ADMIN, SEAT_ANON, Finding, stable_key)
from .http import MCPSession, Unreachable, fetch, get_json

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



def probe(findings: list) -> None:
    """Runner convention: append to the given list, return None."""
    _check_presence(findings)
    _check_arbitrage(findings)
