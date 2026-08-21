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
    # ★ Added 2026-08-21 after the board filed a CRITICAL "paying key receives
    # FEWER data fields than an anonymous caller (8 vs 9)" whose one missing
    # field was `machine_pay` — the autonomous pay offer (server.mjs
    # _wallMachinePay) that rides along with a GATED anonymous answer. A paying
    # key is correctly NOT offered a way to pay. Counting the offer as data
    # convicted the paywall for working, for 2 days, and filed spec-debt
    # inv-100258 on top.
    "machine_pay",
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
    # ★★ WHICH ANONYMOUS CALLER? A caller with trial budget and a caller past the
    # daily cap receive structurally different payloads, and this gauge has
    # published both under one title. On 2026-08-07 it read "100% of fields are
    # envelope" — a number that looks like a total gating failure and was in fact
    # the harness measuring its own spent budget; a control WITH budget got 7 data
    # fields and `_gated: false` in the same hour. A trend line that silently
    # switches which population it describes is worse than no trend line, so the
    # budget state is now part of the TITLE, which makes it part of the graph.
    _left, _caller = _budget_population(env)
    # GAUGE by design: for an ANONYMOUS caller a thin payload may be correct
    # gating, and there is no threshold the platform itself defines. The paid
    # seat below is where this becomes a pass/fail claim.
    findings.append(Finding(
        # ★ Key stays population-FREE on purpose. The population belongs in the
        # title (which the board renders), not in the identity: a key that
        # alternated with the budget state would read as one finding vanishing
        # and another appearing every run, and finding.py's whole stability rule
        # is that different keys must mean different FACTS, not different days.
        key=stable_key("mcp", SEAT_ANON, "envelope-ratio", C.FLAGSHIP_TOOL),
        surface="mcp", seat=SEAT_ANON,
        title=f"Anonymous {C.FLAGSHIP_TOOL} ({_caller}): "
              f"{ratio:.0f}% of fields are envelope",
        verdict=GAUGE, severity=INFO, value=round(ratio, 1),
        evidence=f"{len(data)} data field(s) {sorted(data)[:6]} vs {len(sell)} "
                 f"envelope field(s) {sorted(sell)[:8]}; "
                 f"{len(envelope_all(env))} bytes on the wire; "
                 f"quota.full_answers_remaining_today={_left!r}",
        basis=f"anon MCP tools/call {C.FLAGSHIP_TOOL}, keys of structuredContent, "
              f"from a caller whose remaining daily budget was {_left!r}. The cap "
              f"is keyed on (ip, tool, day), so this seat is post-cap for most "
              f"runs of the day — a fresh session does NOT reset it",
        red_when="n/a — gauge; a thin anonymous payload can be correct gating, "
                 "and no platform-defined threshold exists to fail against"))

    # -- does the quota meter actually move? --------------------------------
    _check_quota_moves(findings)

    # -- does the envelope's tier claim match the seat that called? ----------
    _check_tier_self_report(findings)

    # -- do the OTHER 76 advertised tools actually answer? -------------------
    # ★ Runs LAST and on its OWN session. It makes a dozen calls, which burns
    #   the anonymous daily allowance; running it earlier would change the
    #   envelope every check above observes. A probe must not consume the state
    #   it reports on.
    _check_tools_answer(tools, findings)


# Tools called per run. The server advertises 82; calling every zero-required
# tool each run is ~75 calls x 6 runs/day of self-traffic for a signal a sample
# already gives. The window ROTATES so coverage cycles, and the slice actually
# checked is printed in the evidence — a bounded check that does not say what it
# bounded reads as full coverage.
TOOLS_PER_RUN = 12

# JSON-RPC codes that mean the CALL itself failed, as opposed to the tool
# answering "no" in a usable way. -32602 is what an output-validation failure
# surfaces as: the tool ran and produced something its own schema rejected.
_PROTOCOL_FAILURE_CODES = {-32600, -32601, -32602, -32603}


def _zero_arg_tools(tools: list[dict]) -> list[str]:
    """Tools callable with `{}` — i.e. whose own schema declares no required
    properties. No arguments are invented; the server's schema decides."""
    out = []
    for t in tools or []:
        name = t.get("name")
        schema = t.get("inputSchema") or {}
        if name and not (schema.get("required") or []):
            out.append(name)
    return sorted(out)


def _check_tools_answer(tools: list[dict], findings: list[Finding]) -> None:
    """Do the advertised tools FUNCTION, or are they merely REGISTERED?

    ★ THE GAP THIS CLOSES. The board's tool-count check proves 82 tools are
    registered and says nothing about whether any of them work. On 2026-08-05
    three did not: deal_autopsy, get_interconnection_queue and
    site_selection_canvas each returned `-32602 Output validation error` — a Zod
    schema dump — for every argument set tried, anonymously. The board was green,
    because the six tools it exercises were not among them.

    This is the platform's own recorded lesson (REGISTRATION != FUNCTION, shell
    #49) applied to the tool surface: a registry entry is a promise, and only a
    call collects on it.

    ★ A protocol-level failure is the RED. A tool-level `isError` carrying a
    usable hint is the system working — the agent is told why and what to do.
    The distinction is the finding.
    """
    names = _zero_arg_tools(tools)
    if not names:
        findings.append(blind(
            key=stable_key("mcp", SEAT_ANON, "tools-answer"),
            surface="mcp", seat=SEAT_ANON,
            title="Tool functionality unobserved",
            why="no advertised tool declares an empty `required` list, so none "
                "can be called without inventing arguments",
            basis="tools/list inputSchema.required"))
        return

    # Deterministic rotating window — no randomness, so a run is reproducible,
    # and full coverage cycles every ceil(len/TOOLS_PER_RUN) runs.
    import datetime as _dt
    day = _dt.datetime.now(_dt.timezone.utc).timetuple().tm_yday
    start = (day * TOOLS_PER_RUN) % len(names)
    window = [names[(start + i) % len(names)] for i in range(min(TOOLS_PER_RUN, len(names)))]

    try:
        s = MCPSession(C.MCP_URL, timeout=C.MCP_TIMEOUT).open()
    except Unreachable as e:
        findings.append(blind(
            key=stable_key("mcp", SEAT_ANON, "tools-answer"),
            surface="mcp", seat=SEAT_ANON,
            title="Tool functionality unobserved",
            why=str(e), basis="fresh anonymous MCP session"))
        return

    broken, checked = [], 0
    for name in window:
        try:
            env = s.call(name, {})
        except Unreachable:
            continue  # transport — BLIND for this tool, never a verdict
        checked += 1
        err = env.get("_jsonrpc_error")
        if isinstance(err, dict) and err.get("code") in _PROTOCOL_FAILURE_CODES:
            msg = str(err.get("message") or "")[:120]
            broken.append(f"{name}: {err.get('code')} {msg}")

    if checked == 0:
        findings.append(blind(
            key=stable_key("mcp", SEAT_ANON, "tools-answer"),
            surface="mcp", seat=SEAT_ANON,
            title="Tool functionality unobserved",
            why=f"all {len(window)} sampled tools were unreachable at the "
                "transport layer",
            basis=f"anon tools/call with {{}} on {len(window)} tool(s)"))
        return

    scope = (f"{checked} of {len(names)} zero-argument tool(s) this run "
             f"[{window[0]}..{window[-1]}]; the window rotates daily")
    if broken:
        findings.append(Finding(
            key=stable_key("mcp", SEAT_ANON, "tools-answer"),
            surface="mcp", seat=SEAT_ANON,
            title=f"{len(broken)} advertised tool(s) fail at the protocol level",
            verdict=RED, severity=MAJOR, value=len(broken),
            evidence="; ".join(broken[:4])
                     + (f" (+{len(broken) - 4} more)" if len(broken) > 4 else "")
                     + f" — {scope}",
            basis=f"anon MCP tools/call with {{}} (arguments taken from each "
                  f"tool's own inputSchema.required being empty — none invented); "
                  f"JSON-RPC error code",
            red_when="a tool present in tools/list returns a JSON-RPC protocol "
                     "error (-32600/-32601/-32602/-32603) — registration is not "
                     "function, and an agent gets a schema dump instead of data",
            remedy="An output-validation error (-32602) means the tool ran and "
                   "produced a shape its OWN declared schema rejects. Fix the "
                   "handler or the schema; do not silence the validator."))
        return

    findings.append(Finding(
        key=stable_key("mcp", SEAT_ANON, "tools-answer"),
        surface="mcp", seat=SEAT_ANON,
        title=f"All {checked} sampled tool(s) answer without a protocol error",
        verdict=PASS, severity=INFO, value=checked,
        evidence=scope,
        basis="anon MCP tools/call with {} on a rotating window of the tools "
              "whose own schema declares no required arguments",
        red_when="a tool present in tools/list returns a JSON-RPC protocol error"))


# The platform's OWN pricing ladder, published at /.well-known/mcp.json under
# `pricing`. Nothing invented — these are the names it sells.
_PAID_TIER_NAMES = {"starter", "developer", "pro", "enterprise", "founding",
                    "team", "internal", "admin"}
_TIER_FIELDS = ("caller_tier", "tier", "plan")


def _declared_tier(env: dict) -> tuple[str, str] | None:
    """Return (field, value) of the first tier-ish field in structuredContent."""
    sc = env.get("structuredContent") or {}
    if not isinstance(sc, dict):
        return None
    for f in _TIER_FIELDS:
        v = sc.get(f)
        if isinstance(v, str) and v.strip():
            return f, v.strip().lower()
    return None


def _check_tier_self_report(findings: list[Finding]) -> None:
    """Does the envelope's claim about WHO IS CALLING match the seat that called?

    ★ THE GAP THIS CLOSES. On 2026-08-05 a fully anonymous session — no key —
    was told ``caller_tier: 'pro'`` by get_energy_prices, in the same envelope
    that gated it to a 1-result preview. The gating was correct; the LABEL was
    not, because the field describes the backend's caller (the MCP server, using
    its own credentials) rather than the agent.

    Nothing caught it because the paid-vs-anon check compares the SET of
    data-field NAMES, and `caller_tier` is one field name present in both seats.
    A check that counts names cannot see a name whose VALUE is a lie.

    Why it costs money rather than data: an agent that reads caller_tier to
    decide whether to surface an upgrade prompt concludes its human already pays
    — and never asks. Conversion dies silently on that tool.

    ★ The vocabulary is the platform's own published pricing ladder, so no
    threshold is invented, and self-contradiction is the assertion: you cannot
    be 'pro' AND be handed a preview.
    """
    key = stable_key("mcp", SEAT_ANON, "tier-self-report")
    try:
        s = MCPSession(C.MCP_URL, timeout=C.MCP_TIMEOUT).open()
    except Unreachable as e:
        findings.append(blind(key=key, surface="mcp", seat=SEAT_ANON,
                              title="Tier self-report unobserved", why=str(e),
                              basis="fresh anonymous MCP session"))
        return

    lying, seen, checked = [], [], 0
    for tool, args in C.TIER_PROBE_CALLS:
        try:
            env = s.call(tool, args)
        except Unreachable:
            continue
        checked += 1
        got = _declared_tier(env)
        if not got:
            continue
        field, value = got
        seen.append(f"{tool}:{field}={value}")
        if value in _PAID_TIER_NAMES:
            lying.append(f"{tool} -> {field}={value!r}")

    if checked == 0:
        findings.append(blind(key=key, surface="mcp", seat=SEAT_ANON,
                              title="Tier self-report unobserved",
                              why="no probe call completed",
                              basis="anon MCP tools/call"))
        return

    if lying:
        findings.append(Finding(
            key=key, surface="mcp", seat=SEAT_ANON,
            title=f"{len(lying)} tool(s) tell an ANONYMOUS caller it is on a paid tier",
            verdict=RED, severity=MAJOR, value=len(lying),
            evidence="; ".join(lying[:4])
                     + f" — no API key was sent; {checked} tool(s) called; "
                     + (f"all tier fields seen: {seen[:6]}" if seen else ""),
            basis="MCP tools/call from a session opened with NO X-API-Key; "
                  "structuredContent tier/caller_tier/plan compared against the "
                  "pricing ladder the platform publishes in /.well-known/mcp.json",
            red_when="a call made with no key returns a tier field naming a PAID "
                     "plan (starter/developer/pro/enterprise/founding/team) — the "
                     "envelope is describing someone other than the caller",
            remedy="The field is almost certainly the BACKEND's view of its "
                   "caller (the MCP server's own credentials) forwarded to the "
                   "agent unchanged. Report the AGENT's tier or omit the field; "
                   "an agent that reads 'pro' never shows the upgrade prompt."))
        return

    findings.append(Finding(
        key=key, surface="mcp", seat=SEAT_ANON,
        title="No tool claims a paid tier for an anonymous caller",
        verdict=PASS if seen else GAUGE,
        severity=INFO, value=len(seen),
        evidence=(f"{checked} tool(s) called with no key; tier fields seen: "
                  f"{seen[:6]}") if seen
                 else f"{checked} tool(s) called with no key; none declared a "
                      "tier field at all — nothing to contradict",
        basis="anon MCP tools/call, structuredContent tier/caller_tier/plan",
        red_when="a keyless call returns a tier field naming a paid plan"
                 if seen else
                 "n/a — GAUGE: no tool declared a tier field, so there is no "
                 "claim to check"))


def _advertised_tool_count(s: MCPSession) -> int | None:
    """Pull the tool count the server advertises in its own instructions.

    ``instructions`` sits beside serverInfo in the initialize result, not inside
    it — see MCPSession.open(). Reading the wrong place returns "" and quietly
    downgrades a real parity check into "nothing to compare against".
    """
    m = re.search(r"\b(\d{2,3})\s+tools\b", s.instructions or "")
    return int(m.group(1)) if m else None


# ★★ FIRST-TOUCH MINT MARKERS — they identify the RESPONSE TYPE, not the depth.
# The platform spends an agent's first response introducing itself: it mints a
# trial key alongside whatever it serves. Observed live 2026-08-07 on a fresh
# CI IP (0 data fields) AND on a spent local IP (6 data fields), so the block
# says "this was a first contact", never "the answer was withheld".
_MINT_MARKERS = ("auto_trial_key", "first_call_nudge")


def _budget_population(env: dict) -> tuple:
    """Which anonymous caller does this response describe?

    ``(remaining, label)`` over FOUR populations, because the anonymous seat is
    four callers and collapsing them is how this board keeps publishing numbers
    that describe nobody:

    ``first-touch``     carried the trial-mint block — an agent's FIRST response
    ``with-budget``     meter has room left
    ``post-cap``        daily allowance spent
    ``budget-unstated`` the envelope published no meter

    ★★ BUDGET AND DEPTH ARE NEARLY ORTHOGONAL HERE, which is why three states
    were not enough. Measured live on 2026-08-07:

        fresh CI runner IP, budget remaining  ->  0 data fields
        IP with its cap fully spent           ->  6 data fields

    The three-state version shipped in #2343 inferred "has budget" => "was served
    data" and labelled the mint response ``with-budget``, putting "(with-budget):
    100% of fields are envelope — 0 data field(s)" on the board: a
    self-contradicting line whose label was the wrong half.

    ★★★ AND ``first-touch`` DELIBERATELY DOES NOT CLAIM "ANSWER WITHHELD". The
    first draft of this function called it ``mint-preview`` and treated the mint
    markers as proof of a withheld answer. Measured live minutes later, on a
    spent IP:

        call 1  auto_trial_key=True  trial_preview=True  preview_is_partial=True
                -> 6 data fields
        call 2  none of those        -> 6 data fields

    So the mint block co-occurs with a served answer, and ``preview_is_partial``
    is a DEPTH tease — the platform nulls values and truncates arrays while the
    KEYS survive (the same trap that makes this whole file compare field names
    with a caveat). These markers identify the RESPONSE TYPE an agent got on
    first contact. They prove nothing about depth, so this label does not say
    they do.
    """
    sc = env.get("structuredContent") or {}
    q = sc.get("quota") if isinstance(sc.get("quota"), dict) else {}
    left = q.get("full_answers_remaining_today")
    if any(m in sc for m in _MINT_MARKERS):
        return left, "first-touch"
    if left == 0:
        return left, "post-cap"
    if isinstance(left, (int, float)):
        return left, "with-budget"
    return left, "budget-unstated"


def _remaining(env: dict):
    """Extract the published remaining-quota field, wherever it lives.

    ★★ THE CONTRACT IS `quota.full_answers_remaining_today`, and reading the
    wrong name here manufactured a RED against a working product.

    The first version searched the `quota` object for `remaining_full_today` —
    a name that exists only at TOP level, and only on the first call of a
    session, because it is emitted by the auto-trial mint block. The real
    quota field is `full_answers_remaining_today`, so the lookup never matched
    it, fell through to the top-level auto-trial key, and reported "the meter
    VANISHES between calls".

    Measured live from a fresh anonymous session:
        call 1  quota.full_answers_remaining_today = 1   (top-level = 1)
        call 2  quota.full_answers_remaining_today = 0   (top-level absent)
        call 3  quota.full_answers_remaining_today = 0   (top-level absent)

    The meter is present on every call and counts down correctly. The product
    was right; the probe was reading a field that legitimately appears once.
    Same class as the execute_plan false CRITICAL — an absence "proven" by
    reading the wrong field.

    So `quota.*` is checked FIRST and is the thing the verdict rests on; the
    top-level key is a bonus whose absence means nothing on its own.
    """
    sc = env.get("structuredContent") or {}
    q = sc.get("quota") if isinstance(sc.get("quota"), dict) else {}
    # The uniform contract, present on every response for cap-governed tools.
    for f in ("full_answers_remaining_today", "remaining_full_today",
              "remaining_today", "remaining"):
        if isinstance(q.get(f), (int, float)):
            return f"quota.{f}", q[f]
    # Top-level fallbacks: real, but first-call-only on the anon auto-trial path.
    for f in ("remaining_full_today", "remaining_today", "remaining"):
        if isinstance(sc.get(f), (int, float)):
            return f, sc[f]
    return None, None


def _metered_tool_for_run() -> tuple[str, dict, dict]:
    """Pick the capped tool this run will spend its anonymous budget on.

    Deterministic, not random, so a run is reproducible from its timestamp. The
    cap is per (ip, TOOL, day) and the harness runs every 4h, so keying the
    choice on the 4-hour block gives each run of the day a tool whose 2-call
    budget is still intact. The day is mixed in so a given tool does not always
    land in the same slot — otherwise one tool would absorb every 04:00 run
    forever and its behaviour at other hours would go unwatched.
    """
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    slot = now.timetuple().tm_yday * 6 + (now.hour // 4)
    return C.METERED_TOOLS[slot % len(C.METERED_TOOLS)]


def _check_quota_moves(findings: list[Finding]) -> None:
    """Does the meter an agent is shown actually measure anything?

    Shell #38 observed consecutive anonymous calls CONTRADICTING each other while
    ``remaining_full_today`` never decremented. Measured live on 2026-08-03, the
    real behaviour is a third case that is worse than either: on call 1 the
    anonymous caller is auto-trialled and shown ``remaining_full_today``; by call
    2 the field is **gone from the envelope entirely** rather than counting down.
    So the meter does not read 0 when the budget is spent — it disappears, and an
    agent has no way to learn it is out.

    Hence distinct verdicts rather than a boolean. Two DIFFERENT arguments are
    used so an unchanged number cannot be explained away as a cached response.

    ★★ THE TOOL ROTATES, THE SESSION DOES NOT MATTER. See config.METERED_TOOLS:
    the budget is keyed on (ip, tool, day), so opening a fresh session — which
    this function used to do, believing it bought a fresh allowance — buys
    nothing at all. Spending a DIFFERENT capped tool each run is what actually
    lands on a caller with room to move.
    """
    key = stable_key("mcp", SEAT_ANON, "quota-meter")
    tool, args_a, args_b = _metered_tool_for_run()
    try:
        s = MCPSession(C.MCP_URL, timeout=C.MCP_TIMEOUT).open()
        a = s.call(tool, args_a)
        b = s.call(tool, args_b)
    except Unreachable as e:
        findings.append(blind(key=key, surface="mcp", seat=SEAT_ANON,
                              title="Quota meter unobserved", why=str(e),
                              basis=f"two consecutive anon tools/call to {tool}"))
        return

    fa, va = _remaining(a)
    fb, vb = _remaining(b)
    basis = (f"anon MCP, two tools/call to {tool} with DIFFERENT arguments "
             f"({args_a} then {args_b}) so an unchanged value cannot be a cached "
             f"response; read from structuredContent.remaining_full_today / "
             f"quota.*. The tool ROTATES per 4h run block because the cap is "
             f"keyed on (ip, tool, day) — a fresh session shares the spent "
             f"budget, a different tool does not")
    red_when = ("the caller is shown a remaining-quota field that STILL HAS ROOM "
                "TO MOVE and does not decrease after a second consuming call — a "
                "meter that does not measure. A meter already at zero is excluded: "
                "it cannot count down, so it proves nothing either way")

    if va is None and vb is None:
        # No meter at all is a design choice, not a bug — do not invent a demand.
        # ★ Name the TOOL. Since the pool rotates, "an anonymous caller" alone
        # would read as a platform-wide absence when it is one tool's behaviour —
        # and a tool dropping out of server.mjs's ALWAYS_PARTIAL_PREVIEW set lands
        # here legitimately, because an unmetered tool has no meter to publish.
        findings.append(Finding(
            key=key, surface="mcp", seat=SEAT_ANON,
            title=f"No numeric quota meter exposed on {tool} anonymously",
            verdict=GAUGE, severity=INFO,
            evidence=f"neither call to {tool} exposed a numeric remaining field "
                     f"(envelope keys seen: {sorted(_envelope_keys(a))[:10]}); "
                     f"expected only if {tool} has left ALWAYS_PARTIAL_PREVIEW",
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

    # ★ A METER AT ZERO CANNOT DECREASE. That is arithmetic, not a defect, and
    # treating it as one produced a RED whenever the probe's own IP had already
    # spent its free cap — which is exactly what repeated probing guarantees.
    # The defect is a meter that stays put while it still HAS room to move.
    #
    # ★★ AND IT IS NOT A GAUGE EITHER. Filing it as one is how this check went
    # dark for four days: a GAUGE is "observed, reported as a number, no pass/fail
    # claim", but nothing about meter MOVEMENT was observed here — the probe could
    # not look, which is the definition of BLIND (rule 1). The distinction is not
    # pedantry: BLIND is counted and rendered as `unobserved`, so a run that
    # cannot see the meter now says so on the board instead of parking a
    # reassuring number where a measurement should be.
    if va <= 0:
        findings.append(blind(
            key=key, surface="mcp", seat=SEAT_ANON,
            title="Quota meter movement unobserved — budget already spent",
            why=(f"call 1 {fa}={va}, call 2 {fb}={vb} on {tool}: this IP's daily "
                 f"allowance for this tool was exhausted before the run, so there "
                 f"was no decrement left to observe. The cap is keyed on "
                 f"(ip, tool, day) — the next 4h block rotates to a tool with "
                 f"budget and the movement check runs for real there"),
            basis=basis))
        _check_envelope_drift(a, b, findings)
        return

    moved = vb < va
    findings.append(Finding(
        key=key, surface="mcp", seat=SEAT_ANON,
        title=("Quota meter decrements across consecutive calls" if moved else
               "Quota meter does NOT move while it still has room to"),
        verdict=PASS if moved else RED,
        severity=INFO if moved else MAJOR, value=vb,
        evidence=f"{tool}: call 1 {fa}={va} ({args_a}) then call 2 {fb}={vb} "
                 f"({args_b}) — "
                 f"{'counted down' if moved else 'IDENTICAL despite budget remaining'}",
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

    # ★ SAY WHICH ANONYMOUS CALLER THIS WAS. "The anonymous seat" is not one
    #   seat: a fresh session's first call gets a trial taste, its second gets
    #   the full answer, and once the daily cap is spent every call gets a
    #   preview. Both numbers are true about different callers, and the board
    #   published them side by side as facts about "the anonymous caller", which
    #   describes nobody. State the budget the control had.
    _remaining, _population = _budget_population(anon_env)
    _budget = (f"anon control had {_remaining} full answer(s) of budget left "
               f"and was a {_population} caller"
               if _remaining is not None else
               f"anon control's remaining budget was not stated in the envelope "
               f"({_population})")

    # ★★ A SPENT CONTROL CANNOT SUPPORT THIS CLAIM — SO DO NOT MAKE IT.
    #
    # The old code opened a fresh session and asserted in its own `basis` that
    # this "lands on a caller WITH trial budget". It does not: the cap is keyed
    # on (ip, tool, day), so from the day's second run onward this control is a
    # POST-CAP preview caller. Comparing a paying key against an exhausted one
    # and calling the result "a paying key buys more data" is the green-that-
    # cannot-fail this harness exists to catch — verified 2026-08-07, when the
    # only paid-only field was `citation`, which a control WITH budget also
    # receives. The comparison is only meaningful against a caller who could
    # have been served the full answer and was not.
    # ★★ TWO WAYS THIS CONTROL IS UNFIT, AND ONLY ONE WAS BLOCKED IN #2343.
    #
    # 1. SPENT — the control is past its daily cap, so the comparison measures
    #    the cap and not the paywall.
    # 2. EMPTY — the control received ZERO data fields. Then "paid has more" is
    #    arithmetic on nothing: any non-empty paid response beats it, and the
    #    check would read PASS with the two tiers identical. This is the case the
    #    board actually hit — a fresh CI runner IP whose first response carried
    #    16 envelope fields and no data at all.
    #
    # ★ EMPTINESS is the test, NOT the presence of the mint block. The first
    #   draft of this guard rejected any first-touch response, on the theory that
    #   a minting caller is handed a key instead of an answer. Measured live the
    #   same hour, that theory is false: on a spent IP the first-touch response
    #   carried auto_trial_key AND 6 data fields. Rejecting every mint would
    #   throw away valid controls and quietly shrink coverage — the failure mode
    #   one row down from the one being fixed.
    #
    # ★★★ GUARD ON THE VALUE, NOT THE LABEL. Testing `_population == "post-cap"`
    #   here was wrong and a live run caught it: the mint markers outrank the
    #   meter in the LABEL, so a caller that was both spent AND minting reads
    #   `first-touch`, sailed past a post-cap test and became an invalid control
    #   again. A label is a description for humans; a guard must read the fact.
    if _remaining == 0 or anon_n == 0:
        _why = ("had 0 full answer(s) of budget left for "
                f"{C.FLAGSHIP_TOOL}, so it was served a post-cap preview"
                if _remaining == 0 else
                f"received ZERO data fields from {C.FLAGSHIP_TOOL} "
                f"({len(_envelope_keys(anon_env))} envelope field(s) and nothing "
                f"else), so there is no anonymous answer to compare against")
        findings.append(blind(
            key=key, surface="mcp", seat=SEAT_PAID,
            title=f"Paid-vs-anon comparison unobserved — anon control was "
                  f"{'spent' if _remaining == 0 else 'empty'}",
            why=(f"the anonymous control {_why}. Paid ({paid_n} data fields) vs "
                 f"this control ({anon_n}) measures the trial mechanics, not the "
                 f"paywall, and would read as a PASS even if the two tiers were "
                 f"identical for a caller who was served an answer"),
            basis=f"anon control call to {C.FLAGSHIP_TOOL}; population "
                  f"{_population!r} from quota.full_answers_remaining_today="
                  f"{_remaining!r}; anon data-field count {anon_n}"))
        return

    verdict, severity, title = seat_comparison_verdict(paid_n, anon_n)

    findings.append(Finding(
        key=key, surface="mcp", seat=SEAT_PAID,
        title=title, verdict=verdict, severity=severity,
        value=paid_n - anon_n,
        evidence=f"paid: {paid_n} data fields / {paid_b} bytes — "
                 f"anon: {anon_n} data fields / {anon_b} bytes"
                 + (f"; present for anon but NOT for paid: {missing}" if missing else "")
                 + (f"; paid-only: {extra}" if extra else "")
                 + f"; {_budget}",
        basis=f"same tool ({C.FLAGSHIP_TOOL}) and identical arguments from both "
              "seats in the same run, compared on the SET of data-field names. "
              "The anon control is a caller who was actually SERVED an answer — "
              "verified from the control's own response (budget remaining AND a "
              "non-empty data-field set), not assumed from session freshness "
              "(the cap is keyed on ip+tool+day, so a new session inherits a "
              "spent budget; and a caller WITH budget can still be handed an "
              "empty first response). That is the least flattering "
              "comparison for detecting a gating bug and the most flattering "
              "for describing the paywall. Bytes are supporting "
              "evidence only; "
              "payload size is inflated by the envelope this tool discounts. "
              "★ Field NAMES survive gating untouched — the server keeps keys "
              "and empties values — so this cannot see depth gating.",
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
