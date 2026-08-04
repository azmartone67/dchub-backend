#!/usr/bin/env python3
"""Surface 1 — the AGENT SEAT.

This is the surface no other shell can see. Every master shell reads the
database; an MCP caller reads an *envelope*, and those two have drifted apart
repeatedly:

* shell #38 measured our #1 tool returning **0 data fields and 97.2% envelope**,
  with a quota that never decremented and consecutive calls contradicting each
  other — none of which is visible in any table.
* shell #49 nearly shipped a duplicate of an already-shipped return nudge because
  the probe was ANONYMOUS and searched ``content[].text`` while the offer lives in
  ``structuredContent.next_session``.

So every check here declares its seat and the exact field it read, and the
anon/paid split is load-bearing rather than decorative: for an ANON caller, few
data fields may be correct gating; for a PAYING caller it is a defect.
"""
from __future__ import annotations

import re

from . import config as C
from .finding import (CRITICAL, GAUGE, INFO, MAJOR, MINOR, PASS, RED,
                      SEAT_ANON, SEAT_PAID, Finding, blind, stable_key)
from .http import MCPSession, Unreachable, envelope_all

# Keys that sell, meter or narrate rather than answer the question. Everything
# else in structuredContent counts as DATA. Kept explicit so the ratio stays
# auditable — if the server adds a new envelope key, the ratio moves and the
# gauge shows it rather than silently reclassifying it as data.
ENVELOPE_KEYS = {
    "quota", "trial_taste", "inline_full", "taste_bounded", "for_your_human",
    "auto_trial_key", "retry_with_header", "persist_command", "first_call_nudge",
    "retry_instructions", "remaining_full_today", "unlocked_tools", "upgrade",
    "starter_pack", "next_session", "_return_loop", "come_back", "paid_only",
    "_error_mitigation", "tool", "_entity", "unlock", "cta", "pricing",
    "how_to_unlock", "sample", "preview",
    # ★ Added 2026-08-04 after the board printed
    #   "paid: 9 data fields — anon: 11 data fields"
    # and the paid-vs-anon check passed anyway. Four of the anonymous caller's
    # "data" fields were nothing of the kind:
    #   agent_payment      — a pay-rail offer
    #   trial_preview      — the trial upsell block
    #   preview_is_partial — a gating flag announcing what was withheld
    #   platform           — request metadata
    # and `success`/`resume` are a status flag and a return-loop block. Counting
    # them as data flattered the envelope ratio (reported 48% envelope when the
    # true figure is far higher) and inverted the paid-vs-anon comparison, which
    # is exactly the measurement the ratio exists to make honest.
    "agent_payment", "trial_preview", "preview_is_partial", "platform",
    "success", "resume", "digest_offer", "retention_tools", "learn",
}


def _is_envelope(key: str) -> bool:
    """Envelope = anything that sells, meters or annotates rather than answers.

    Two rules, both auditable. The explicit set above, plus the leading-underscore
    convention the server itself uses for meta: ``_gated``, ``_upgrade``,
    ``_entity``, ``_recent_facilities_total_in_pro``. That last shape is the
    clearest case — "how much more you would get in Pro" is a sales figure about
    the answer, not part of it, and counting it as data would flatter the ratio
    with the very fields the ratio exists to expose.
    """
    return key.startswith("_") or key in ENVELOPE_KEYS


def _data_keys(env: dict) -> list[str]:
    sc = env.get("structuredContent") or {}
    if not isinstance(sc, dict):
        return []
    return [k for k in sc if not _is_envelope(k)]


def _envelope_keys(env: dict) -> list[str]:
    sc = env.get("structuredContent") or {}
    if not isinstance(sc, dict):
        return []
    return [k for k in sc if _is_envelope(k)]


def _open(seat: str):
    key = C.SEATS.get(seat)
    return MCPSession(C.MCP_URL, api_key=key, timeout=C.MCP_TIMEOUT).open()


def probe(findings: list[Finding]) -> None:
    """Run the agent-seat probes, appending findings."""
    _probe_seat_anon(findings)
    if C.seat_available(SEAT_PAID):
        _probe_seat_paid(findings)
    else:
        findings.append(blind(
            key=stable_key("mcp", "paid", "seat-unavailable"),
            surface="mcp", seat=SEAT_PAID,
            title="Paid seat not exercised — no reviewer key in the environment",
            why="DCHUB_REVIEWER_KEY is unset, so no paying-caller check ran",
            basis="env DCHUB_REVIEWER_KEY"))


# ── anonymous seat: how most agents actually arrive ─────────────────────────
def _probe_seat_anon(findings: list[Finding]) -> None:
    try:
        s = _open(SEAT_ANON)
    except Unreachable as e:
        findings.append(blind(
            key=stable_key("mcp", SEAT_ANON, "handshake"),
            surface="mcp", seat=SEAT_ANON,
            title="MCP handshake unreachable from the anonymous seat",
            why=str(e), basis=f"POST {C.MCP_URL} initialize"))
        return

    findings.append(Finding(
        key=stable_key("mcp", SEAT_ANON, "handshake"),
        surface="mcp", seat=SEAT_ANON,
        title="MCP handshake succeeds for an anonymous agent",
        verdict=PASS, severity=INFO,
        evidence=f"serverInfo={s.server_info.get('name')} "
                 f"v{s.server_info.get('version')}, session established",
        basis=f"POST {C.MCP_URL} initialize, streamable HTTP",
        red_when="initialize returns non-200 or no session id"))

    # -- tools/list vs the count the server itself advertises ---------------
    # Self-consistency between two things the platform publishes. No invented
    # target: the number comes from the server's own instructions string.
    try:
        tools = s.list_tools()
    except Unreachable as e:
        findings.append(blind(
            key=stable_key("mcp", SEAT_ANON, "tools-list"),
            surface="mcp", seat=SEAT_ANON, title="tools/list unreadable",
            why=str(e), basis="MCP tools/list"))
        tools = []

    if tools:
        listed = len(tools)
        advertised = _advertised_tool_count(s)
        if advertised is None:
            findings.append(Finding(
                key=stable_key("mcp", SEAT_ANON, "tool-count"),
                surface="mcp", seat=SEAT_ANON,
                title=f"tools/list exposes {listed} tools",
                verdict=GAUGE, severity=INFO, value=listed,
                evidence=f"tools/list={listed}; server instructions advertise no "
                         "parseable tool count to compare against",
                basis="MCP tools/list vs serverInfo.instructions",
                red_when="n/a — gauge; no self-published number to disagree with"))
        else:
            agree = listed == advertised
            findings.append(Finding(
                key=stable_key("mcp", SEAT_ANON, "tool-count"),
                surface="mcp", seat=SEAT_ANON,
                title=("Tool count agrees with the server's own claim"
                       if agree else
                       f"Tool count DISAGREES: serves {listed}, advertises {advertised}"),
                verdict=PASS if agree else RED,
                severity=INFO if agree else MAJOR, value=listed,
                evidence=f"tools/list={listed}; instructions string says {advertised}",
                basis="MCP tools/list vs the tool count in serverInfo.instructions",
                red_when="the number of tools served differs from the number the "
                         "server's own instructions advertise to every client",
                remedy="Reconcile server.mjs (source of truth) with the marketed "
                       "count; manifests are known to drift from it."))

    # -- what an anonymous caller actually RECEIVES -------------------------
    try:
        env = s.call(C.FLAGSHIP_TOOL, C.FLAGSHIP_ARGS)
    except Unreachable as e:
        findings.append(blind(
            key=stable_key("mcp", SEAT_ANON, "flagship"),
            surface="mcp", seat=SEAT_ANON,
            title=f"{C.FLAGSHIP_TOOL} unreachable anonymously",
            why=str(e), basis=f"MCP tools/call {C.FLAGSHIP_TOOL}"))
        return

    data = _data_keys(env)
    sell = _envelope_keys(env)
    total = len(data) + len(sell)
    ratio = (len(sell) / total * 100.0) if total else 0.0
    # GAUGE by design: for an ANONYMOUS caller a thin payload may be correct
    # gating, and there is no threshold the platform itself defines. The paid
    # seat below is where this becomes a pass/fail claim.
    findings.append(Finding(
        key=stable_key("mcp", SEAT_ANON, "envelope-ratio", C.FLAGSHIP_TOOL),
        surface="mcp", seat=SEAT_ANON,
        title=f"Anonymous {C.FLAGSHIP_TOOL}: {ratio:.0f}% of fields are envelope",
        verdict=GAUGE, severity=INFO, value=round(ratio, 1),
        evidence=f"{len(data)} data field(s) {sorted(data)[:6]} vs {len(sell)} "
                 f"envelope field(s) {sorted(sell)[:8]}; "
                 f"{len(envelope_all(env))} bytes on the wire",
        basis=f"anon MCP tools/call {C.FLAGSHIP_TOOL}, keys of structuredContent",
        red_when="n/a — gauge; a thin anonymous payload can be correct gating, "
                 "and no platform-defined threshold exists to fail against"))

    # -- does the quota meter actually move? --------------------------------
    _check_quota_moves(findings)


def _advertised_tool_count(s: MCPSession) -> int | None:
    """Pull the tool count the server advertises in its own instructions.

    ``instructions`` sits beside serverInfo in the initialize result, not inside
    it — see MCPSession.open(). Reading the wrong place returns "" and quietly
    downgrades a real parity check into "nothing to compare against".
    """
    m = re.search(r"\b(\d{2,3})\s+tools\b", s.instructions or "")
    return int(m.group(1)) if m else None


def _remaining(env: dict):
    """Extract the published remaining-quota field, wherever it lives."""
    sc = env.get("structuredContent") or {}
    for f in ("remaining_full_today", "remaining", "remaining_today"):
        if isinstance(sc.get(f), (int, float)):
            return f, sc[f]
        q = sc.get("quota")
        if isinstance(q, dict) and isinstance(q.get(f), (int, float)):
            return f"quota.{f}", q[f]
    return None, None


def _check_quota_moves(findings: list[Finding]) -> None:
    """Does the meter an agent is shown actually measure anything?

    Shell #38 observed consecutive anonymous calls CONTRADICTING each other while
    ``remaining_full_today`` never decremented. Measured live on 2026-08-03, the
    real behaviour is a third case that is worse than either: on call 1 the
    anonymous caller is auto-trialled and shown ``remaining_full_today``; by call
    2 the field is **gone from the envelope entirely** rather than counting down.
    So the meter does not read 0 when the budget is spent — it disappears, and an
    agent has no way to learn it is out.

    Hence three distinct verdicts rather than a boolean. Two DIFFERENT arguments
    are used so an unchanged number cannot be explained away as a cached response.
    """
    key = stable_key("mcp", SEAT_ANON, "quota-meter")
    try:
        # ★ A FRESH session, not the caller's. The first-call envelope is
        # session-scoped: whichever call is #1 gets the auto-trial block that
        # carries the meter. Reusing the outer session made this probe compare
        # calls #2 and #3 — both already past the trial block — and it reported
        # "no meter exposed" when the real behaviour is "a meter that vanishes".
        # The probe must not consume the state it is trying to observe.
        s = MCPSession(C.MCP_URL, timeout=C.MCP_TIMEOUT).open()
        a = s.call(C.FLAGSHIP_TOOL, {"market": "ashburn"})
        b = s.call(C.FLAGSHIP_TOOL, {"market": "phoenix"})
    except Unreachable as e:
        findings.append(blind(key=key, surface="mcp", seat=SEAT_ANON,
                              title="Quota meter unobserved", why=str(e),
                              basis="two consecutive anon tools/call"))
        return

    fa, va = _remaining(a)
    fb, vb = _remaining(b)
    basis = ("anon MCP, two tools/call with DIFFERENT arguments (ashburn then "
             "phoenix) so an unchanged value cannot be a cached response; read "
             "from structuredContent.remaining_full_today / quota.*")
    red_when = ("the caller is shown a remaining-quota field that then holds the "
                "same value after a second consuming call (a meter that does not "
                "measure), or DISAPPEARS from the envelope instead of counting "
                "down (a meter that cannot say zero)")

    if va is None and vb is None:
        # No meter at all is a design choice, not a bug — do not invent a demand.
        findings.append(Finding(
            key=key, surface="mcp", seat=SEAT_ANON,
            title="No numeric quota meter exposed to an anonymous caller",
            verdict=GAUGE, severity=INFO,
            evidence=f"neither call exposed a numeric remaining field "
                     f"(envelope keys seen: {sorted(_envelope_keys(a))[:10]})",
            basis=basis,
            red_when="n/a — gauge; publishing no meter at all is a design choice"))
        _check_envelope_drift(a, b, findings)
        return

    if va is not None and vb is None:
        # ★ Derive the sentence from the observation. An earlier draft asserted
        # "the meter never reads 0" — and the very first live run measured
        # va == 0, so the hardcoded narrative was precise and FALSE about the
        # data printed beside it. State only what this run actually saw.
        reading = (f"had already reached 0 on call 1, then stopped being published"
                   if va == 0 else
                   f"was still at {va} on call 1 and then stopped being published "
                   f"without ever reaching 0")
        findings.append(Finding(
            key=key, surface="mcp", seat=SEAT_ANON,
            title="Quota meter VANISHES from the envelope between calls",
            verdict=RED, severity=MAJOR, value=va,
            evidence=f"call 1 published {fa}={va}; call 2 published no remaining "
                     f"field at all (envelope keys {len(_envelope_keys(a))} -> "
                     f"{len(_envelope_keys(b))}). The meter {reading} — so an "
                     f"agent cannot distinguish 'budget spent' from 'no budget "
                     f"applies here'.",
            basis=basis, red_when=red_when,
            remedy="Keep publishing the field once exhausted rather than dropping "
                   "it. An agent that cannot see its budget state cannot decide to "
                   "upgrade — it degrades silently and attributes the thinner "
                   "answer to us."))
        _check_envelope_drift(a, b, findings)
        return

    if va is None and vb is not None:
        findings.append(Finding(
            key=key, surface="mcp", seat=SEAT_ANON,
            title="Quota meter appears only after the first call",
            verdict=GAUGE, severity=INFO, value=vb,
            evidence=f"call 1 published no remaining field; call 2 published {fb}={vb}",
            basis=basis,
            red_when="n/a — gauge; late appearance is odd but makes no wrong claim"))
        _check_envelope_drift(a, b, findings)
        return

    moved = va != vb
    findings.append(Finding(
        key=key, surface="mcp", seat=SEAT_ANON,
        title=("Quota meter decrements across consecutive calls" if moved else
               "Quota meter does NOT move across two consuming calls"),
        verdict=PASS if moved else RED,
        severity=INFO if moved else MAJOR, value=vb,
        evidence=f"call 1 {fa}={va} (ashburn) then call 2 {fb}={vb} (phoenix) — "
                 f"{'changed' if moved else 'IDENTICAL'}",
        basis=basis, red_when=red_when,
        remedy="Either decrement it per consuming call or stop publishing it; a "
               "static meter teaches agents the wrong cost model."))

    _check_envelope_drift(a, b, findings)


def _check_envelope_drift(a: dict, b: dict, findings: list[Finding]) -> None:
    """What does an agent LOSE between its first and second call?

    Measured live: the first anonymous call carries ``for_your_human`` — the
    signed, *measured* human-relay link that exists precisely so a handoff can be
    counted — plus ``auto_trial_key``, ``upgrade`` and ``starter_pack``. The
    second call carries none of them.

    Reported as a GAUGE, not RED, and deliberately so: offering the upgrade path
    once per session may well be intended. But it is the single most
    consequential thing that silently changes between two identical calls, and
    ``relay_opens`` sitting at approximately zero is a live open question — so the
    board should carry the number rather than an accusation.
    """
    ka, kb = set((a.get("structuredContent") or {})), set((b.get("structuredContent") or {}))
    lost = sorted(ka - kb)
    conversion_keys = {"for_your_human", "upgrade", "starter_pack",
                       "auto_trial_key", "first_call_nudge"}
    lost_conv = sorted(conversion_keys & set(lost))
    findings.append(Finding(
        key=stable_key("mcp", SEAT_ANON, "envelope-drift"),
        surface="mcp", seat=SEAT_ANON,
        title=(f"Call 2 loses {len(lost_conv)} conversion field(s) present on call 1"
               if lost_conv else "Envelope shape is stable across consecutive calls"),
        verdict=GAUGE, severity=INFO, value=len(lost_conv),
        evidence=(f"present on call 1, absent on call 2: {lost_conv or 'none'}"
                  f" (all dropped keys: {lost[:12]})"),
        basis="anon MCP, structuredContent key sets of two consecutive "
              f"{C.FLAGSHIP_TOOL} calls in ONE session",
        red_when="n/a — gauge BY DESIGN: showing the upgrade path once per session "
                 "may be intended. Tracked because it is the most consequential "
                 "silent difference between two identical calls, and it decides "
                 "whether a handoff can be measured at all."))


# ── paying seat: the claims that are unambiguous once money is involved ─────
def _probe_seat_paid(findings: list[Finding]) -> None:
    try:
        s = _open(SEAT_PAID)
    except Unreachable as e:
        findings.append(blind(
            key=stable_key("mcp", SEAT_PAID, "handshake"),
            surface="mcp", seat=SEAT_PAID,
            title="MCP handshake unreachable from the paying seat",
            why=str(e), basis=f"POST {C.MCP_URL} initialize with X-API-Key"))
        return

    # -- a paying caller must receive DATA ----------------------------------
    try:
        env = s.call(C.FLAGSHIP_TOOL, C.FLAGSHIP_ARGS)
    except Unreachable as e:
        findings.append(blind(
            key=stable_key("mcp", SEAT_PAID, "flagship"),
            surface="mcp", seat=SEAT_PAID,
            title=f"{C.FLAGSHIP_TOOL} unreachable for a paying caller",
            why=str(e), basis=f"MCP tools/call {C.FLAGSHIP_TOOL} with X-API-Key"))
        return

    data = _data_keys(env)
    sell = _envelope_keys(env)
    gated = "paid_only" in (env.get("structuredContent") or {})
    ok = bool(data) and not gated
    findings.append(Finding(
        key=stable_key("mcp", SEAT_PAID, "receives-data", C.FLAGSHIP_TOOL),
        surface="mcp", seat=SEAT_PAID,
        title=(f"Paying caller receives {len(data)} data field(s) from "
               f"{C.FLAGSHIP_TOOL}" if ok else
               f"PAYING CALLER RECEIVES NO DATA from {C.FLAGSHIP_TOOL}"),
        verdict=PASS if ok else RED,
        severity=INFO if ok else CRITICAL, value=len(data),
        evidence=f"data fields {sorted(data)[:8]}; envelope fields {len(sell)}; "
                 f"paid_only={gated}; {len(envelope_all(env))} bytes",
        basis=f"paid seat (X-API-Key) MCP tools/call {C.FLAGSHIP_TOOL}, "
              "keys of structuredContent minus the known envelope set",
        red_when="a caller presenting a paying key gets zero data fields, or is "
                 "told paid_only — they are paying and being refused",
        remedy="Check the live gate in mcp-server/server.mjs (NOT main.py — the "
               "map_tier_gating.py path is dead) and the key's tier resolution."))

    # -- paid must actually beat anon ---------------------------------------
    _check_paid_beats_anon(env, findings)

    # -- the return mechanism, from the seat that can see it ----------------
    _check_return_nudge(env, findings)

    # -- the documented front door ------------------------------------------
    _check_front_door(s, findings)

    # -- error paths --------------------------------------------------------
    _check_error_mitigation(s, findings)


def seat_comparison_verdict(paid_n: int, anon_n: int) -> tuple[str, str, str]:
    """Decide paid-vs-anon on DATA FIELDS alone. Pure, so it is testable.

    ★ The first version passed on ``paid_n > anon_n OR paid_b > anon_b``, so a
    bigger payload alone satisfied it — and payload size is inflated by exactly
    the envelope this tool exists to discount. The live board printed
    "paid: 9 data fields — anon: 11 data fields" and still reported PASS: the
    check could not detect a paying caller receiving less, which is the one
    thing it is named for. Bytes are now supporting evidence only.

    It was extracted into a pure function because the inline version had no test
    — reverting the OR passed the whole suite.
    """
    if paid_n > anon_n:
        return PASS, INFO, "A paying key buys more data than anonymous access"
    if paid_n < anon_n:
        return (RED, CRITICAL,
                f"A paying key receives FEWER data fields than an anonymous "
                f"caller ({paid_n} vs {anon_n})")
    # Same shape. Values may still be deeper, and this probe does not compare
    # value depth — so it reports the number and makes no claim.
    return (GAUGE, INFO,
            f"Paid and anonymous callers receive the same {paid_n} data "
            f"field(s) from {C.FLAGSHIP_TOOL}")


def _check_paid_beats_anon(paid_env: dict, findings: list[Finding]) -> None:
    """The same question, asked from both seats, must not get the same answer.

    This is the 'paid caps unenforced' class seen from the outside: if a paying
    key buys nothing a stranger cannot get, either gating is inverted or the tier
    never resolved.
    """
    key = stable_key("mcp", SEAT_PAID, "beats-anon", C.FLAGSHIP_TOOL)
    try:
        anon = MCPSession(C.MCP_URL, timeout=C.MCP_TIMEOUT).open()
        anon_env = anon.call(C.FLAGSHIP_TOOL, C.FLAGSHIP_ARGS)
    except Unreachable as e:
        findings.append(blind(key=key, surface="mcp", seat=SEAT_PAID,
                              title="Paid-vs-anon comparison unobserved",
                              why=str(e), basis="anon control call"))
        return

    paid_keys, anon_keys = set(_data_keys(paid_env)), set(_data_keys(anon_env))
    paid_n, anon_n = len(paid_keys), len(anon_keys)
    paid_b, anon_b = len(envelope_all(paid_env)), len(envelope_all(anon_env))
    missing = sorted(anon_keys - paid_keys)
    extra = sorted(paid_keys - anon_keys)

    verdict, severity, title = seat_comparison_verdict(paid_n, anon_n)

    findings.append(Finding(
        key=key, surface="mcp", seat=SEAT_PAID,
        title=title, verdict=verdict, severity=severity,
        value=paid_n - anon_n,
        evidence=f"paid: {paid_n} data fields / {paid_b} bytes — "
                 f"anon: {anon_n} data fields / {anon_b} bytes"
                 + (f"; present for anon but NOT for paid: {missing}" if missing else "")
                 + (f"; paid-only: {extra}" if extra else ""),
        basis=f"same tool ({C.FLAGSHIP_TOOL}) and identical arguments from both "
              "seats in the same run, compared on the SET of data-field names "
              "(bytes reported as supporting evidence only — payload size is "
              "inflated by the envelope this tool discounts)",
        red_when="the paying seat receives strictly fewer data fields than the "
                 "anonymous seat — whatever the tier is doing, it is not buying "
                 "more of the answer"
                 if verdict != GAUGE else
                 "n/a — GAUGE: identical field sets may still differ in depth, "
                 "and this probe does not compare value depth",
        remedy="Verify tier resolution for the key and that the gate reads the "
               "resolved tier, not a cached anon bucket."))


def _check_return_nudge(env: dict, findings: list[Finding]) -> None:
    """Is the retention mechanism actually offered to a keyed caller?

    ★ Deliberately seated and field-precise. The 2026-08-02 version of this exact
    question was answered WRONG twice over: probed anonymously (the nudge is
    keyed-only by design) and grepped ``content[].text`` (it ships in
    ``structuredContent.next_session``). An absence proven with the wrong auth or
    the wrong field is not an absence.
    """
    sc = env.get("structuredContent") or {}
    present = any(k in sc for k in ("next_session", "_return_loop", "come_back"))
    findings.append(Finding(
        key=stable_key("mcp", SEAT_PAID, "return-nudge"),
        surface="mcp", seat=SEAT_PAID,
        title=("Return mechanism is offered to keyed callers" if present else
               "Return mechanism NOT offered to a keyed caller"),
        verdict=PASS if present else RED,
        severity=INFO if present else MAJOR,
        evidence=f"structuredContent keys present: "
                 f"{[k for k in ('next_session', '_return_loop', 'come_back') if k in sc]}"
                 or "none of next_session/_return_loop/come_back",
        basis="paid seat (keyed — the nudge is keyed-only BY DESIGN), read from "
              "structuredContent.next_session / _return_loop / come_back, NOT "
              "content[].text",
        red_when="a keyed caller's flagship result carries no next_session, "
                 "_return_loop or come_back block — the agent is never told how "
                 "to come back",
        remedy="withNextSession/personalizeNextSession in mcp-server "
               "lib/result-shaping.mjs; withReturnNudge in server.mjs."))


def _check_front_door(s: MCPSession, findings: list[Finding]) -> None:
    """execute_plan is the documented front door — a broken one is critical.

    The server's own instructions tell every client to call this FIRST for any
    multi-capability question, so if it errors, the platform's advertised entry
    point is the thing that fails.
    """
    key = stable_key("mcp", SEAT_PAID, "front-door")
    try:
        env = s.call(C.FRONT_DOOR_TOOL, C.FRONT_DOOR_ARGS)
    except Unreachable as e:
        findings.append(blind(key=key, surface="mcp", seat=SEAT_PAID,
                              title="execute_plan front door unobserved",
                              why=str(e),
                              basis=f"MCP tools/call {C.FRONT_DOOR_TOOL}"))
        return

    err = env.get("_jsonrpc_error")
    sc = env.get("structuredContent") or {}
    # ★ The steps live in `executed`, with a count in `totals.steps_run`.
    # A first version read `steps`/`plan`, found neither, and published a
    # CRITICAL "front door failed to produce a plan" against a tool that had
    # just returned 44KB and run three steps correctly. That is the shell-#49
    # error exactly — an absence proven by reading the wrong field — committed
    # by the harness written to prevent it. Verified against the live envelope:
    # top-level keys are executed / totals / replay / ok, never steps or plan.
    # `steps`/`plan` are kept only as forward-compatible fallbacks.
    steps = sc.get("executed")
    if not isinstance(steps, list):
        steps = sc.get("steps") or sc.get("plan") or []
    n_steps = len(steps) if isinstance(steps, list) else 0
    totals = sc.get("totals") if isinstance(sc.get("totals"), dict) else {}
    steps_run = totals.get("steps_run")
    if not n_steps and isinstance(steps_run, int):
        n_steps = steps_run
    has_replay = bool(sc.get("replay"))
    declared_ok = sc.get("ok")
    ok = (not err) and n_steps > 0 and declared_ok is not False
    findings.append(Finding(
        key=key, surface="mcp", seat=SEAT_PAID,
        title=(f"Front door (execute_plan) ran {n_steps} step(s) for a "
               "multi-capability intent" if ok else
               "FRONT DOOR execute_plan failed to produce a plan"),
        verdict=PASS if ok else RED,
        severity=INFO if ok else CRITICAL,
        value=n_steps,
        evidence=(f"jsonrpc_error={str(err)[:160]}" if err else
                  f"executed={n_steps} step(s), totals.steps_run={steps_run}, "
                  f"ok={declared_ok}, replay block "
                  f"{'present' if has_replay else 'ABSENT'}, "
                  f"{len(envelope_all(env))} bytes"),
        basis=f"paid seat MCP tools/call {C.FRONT_DOOR_TOOL} with the intent "
              f"{C.FRONT_DOOR_ARGS['intent']!r}, read from "
              "structuredContent.executed (count cross-checked against "
              "totals.steps_run) and structuredContent.ok",
        red_when="the tool the server's own instructions name as the FRONT DOOR "
                 "returns a JSON-RPC error, reports ok=false, or executes zero "
                 "steps",
        remedy="execute_plan is the advertised entry point for every "
               "multi-capability question — a failure here is the first thing an "
               "evaluating agent sees."))


def _check_error_mitigation(s: MCPSession, findings: list[Finding]) -> None:
    """Do error paths hand the agent something actionable?

    Reported as a GAUGE on purpose. The measured state (2026-08-02) was 3 of 5
    error paths carrying ``_error_mitigation``, and the fix needs an owner call on
    which generic codes are honest — INVENTING a hint is worse than shipping
    none. So this trends the ratio and refuses to fail against a number nobody
    has agreed to.
    """
    probes = [
        ("get_facility", {"slug": "definitely-not-a-real-facility-qa-probe"}),
        (C.SECOND_TOOL, {"limit": -1}),
    ]
    seen, mitigated, detail = 0, 0, []
    for name, args in probes:
        try:
            env = s.call(name, args)
        except Unreachable:
            continue
        blob = envelope_all(env)
        looks_error = ("_jsonrpc_error" in env or "error" in blob[:400].lower()
                       or (env.get("structuredContent") or {}).get("error"))
        if not looks_error:
            continue
        seen += 1
        has = "_error_mitigation" in blob or "deterministic_hint" in blob
        mitigated += 1 if has else 0
        detail.append(f"{name}:{'hint' if has else 'NO-HINT'}")

    if not seen:
        findings.append(blind(
            key=stable_key("mcp", SEAT_PAID, "error-mitigation"),
            surface="mcp", seat=SEAT_PAID,
            title="Error-path guidance unobserved",
            why="neither deliberately-invalid call produced a recognisable error",
            basis="MCP tools/call with invalid arguments"))
        return

    findings.append(Finding(
        key=stable_key("mcp", SEAT_PAID, "error-mitigation"),
        surface="mcp", seat=SEAT_PAID,
        title=f"Error paths carrying agent guidance: {mitigated}/{seen}",
        verdict=GAUGE, severity=INFO, value=mitigated,
        evidence="; ".join(detail),
        basis="paid seat, deliberately-invalid arguments; searched the WHOLE "
              "envelope for _error_mitigation / deterministic_hint",
        red_when="n/a — gauge BY DESIGN: which generic codes deserve a hint is an "
                 "open owner decision, and inventing a hint is worse than none"))
