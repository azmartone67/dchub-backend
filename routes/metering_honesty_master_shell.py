"""Metering Honesty Master Shell (#54) — GET /admin/metering-honesty
tick: /api/v1/admin/metering-honesty/master-tick
kill: METERING_HONESTY_SHELL_DISABLE=1

Built 2026-08-08 to close, in one place, the whole class the outside-in QA
super-user kept re-discovering one tool at a time: A PUBLISHED METER THAT DOES
NOT MEASURE.

★ WHY A SHELL AND NOT ANOTHER PROBE. The qa-superuser board already reports
this — and has flapped on it six times in five days, red-green-red, because its
finding key is `mcp::anon::quota-meter` with NO TOOL IN IT while the tool it
samples ROTATES every 4h run block. Three genuinely different behaviours were
being averaged into one verdict. Measured live 2026-08-08T19:4x from an
anonymous seat:

  get_gas_intelligence   cap=2 remaining=2 ... 2 ... 2  over three calls with
                         DIFFERENT arguments, every response carrying
                         `omitted_no_fabrication` — the WITHDRAWN payload. The
                         gas full answer was withdrawn on 08-08; the meter is
                         still advertising a budget that tool cannot spend.
  get_market_intel       cap=2, remaining 1 -> 0 across two calls. Correct.
  get_iso_context        publishes no full_answers meter at all.

One tool over-promises, one behaves, one is silent. A single rotating check
cannot say that; four per-tool lanes can. Every check id in this shell carries
its tool name, so a finding can never again be true of "the meter" in general.

★★ THE DISCRIMINATOR THIS SHELL GOT WRONG FIRST, CAUGHT ON ITS OWN FIRST LIVE
RUN. The first draft abstained whenever the envelope looked "partial", using
`_upgrade` / `upgrade` / `inline_full` / `*_total_in_pro` as the markers. Those
are on almost every free response — including get_market_intel's, which was
DECREMENTING correctly at the time. So the draft absolved the one tool that
worked and, by the same rule, would have absolved gas too. Tier-slicing depth
(`*_total_in_pro`: "3 more providers in Pro") is a NORMAL paid answer and costs
a full answer. Only `omitted_no_fabrication` — the payload that no longer
exists at any tier — means nothing can ever be spent. Payload shape does not
decide whether a call was billable; only the WITHDRAWN marker does, and
everything else is judged by whether the meter moved.

★★★ THE INSTRUMENT SPENDS THE THING IT MEASURES. The anonymous cap is keyed on
(ip, tool, day). This shell probes anonymously from ONE Railway egress IP, so
each tick it runs burns that IP's free budget for the tools it sampled — and a
tool pinned at remaining=0 can no longer prove anything either way. An eager
board blinds itself by the second tick. Same class as #2439, where /heal/force
was an anonymous GET that spent the whole probe budget. Three consequences,
all deliberate:
  - `_SAMPLE_SIZE` tools per tick, ROTATED by day-of-year, never all of them;
  - remaining==0 is UNOBSERVED, never a failure — a meter already at zero
    cannot count down, so it proves nothing;
  - the tick is idempotent-by-cadence, not by call: run it hourly and you have
    a blind board, which is why nothing schedules it here.

★★★ THE INSTRUMENT MUST PROVE WHICH SEAT IT SAT IN. Our own egress can be
allowlisted, rate-limit-exempt, or Origin-injected by the edge (the worker
injects Origin on our own traffic), and a probe that quietly gets a better seat
than a real agent reports a paywall that does not exist for anyone else. So
every call records the tier the ENVELOPE itself claims, and any lane observed
at a tier other than `free` renders UNOBSERVED with that tier named — never a
pass, and never a red about the platform.

Four lanes, one per distinct failure the 08-08 evidence exposes:

  1. consistent   — a tool that publishes a meter on call 1 must still publish
                    it on call 2. (The "meter VANISHES from the envelope
                    between calls" defect, #2210.)
  2. spends       — a meter with room to move must go DOWN across two
                    consuming calls. Judged whenever the tool answered at all
                    and remaining > 0.
  3. unspendable  — a meter published with room to move on a tool whose payload
                    has been WITHDRAWN, so it can never move. This is the gas
                    case, and it is the one red the board keeps rediscovering.
                    Lane 2 defers to this one so a single defect is a single
                    red.
  4. coherent     — cap >= remaining >= 0, cap > 0 wherever a meter is
                    published, and remaining never INCREASES inside one tick.

HONESTY RULES (each one is a defect somebody already shipped):
- UNREADABLE IS NOT DEAD. A transport failure, a protocol error or an
  unparseable envelope renders pass=None with the reason. Never False.
- DEPTH-SLICING IS NOT WITHDRAWAL. `*_total_in_pro` means "more of this exists
  in Pro" and rides on answers that DO cost a full answer;
  `omitted_no_fabrication` means the payload is gone at every tier. Only the
  second can make a meter unspendable, and conflating them absolved the one
  tool that worked (see above).
- DIFFERENT ARGUMENTS ON EVERY CALL, so an unchanged value can never be
  explained away as a cached response.
- NOTHING IS WRITTEN. Read-only tools only (see _SAMPLE_POOL); no key, no
  header, no mutation. This shell cannot bill, unlock, or persist anything.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request

# Imported, never copied — the honesty semantics must not drift between boards.
from routes.brain_ascension_master_shell import (  # noqa: F401
    _admin_ok, _check, _lane_verdict, _safe_lane)

metering_honesty_master_shell_bp = Blueprint(
    "metering_honesty_master_shell", __name__)

# The public door, reached the way a real agent reaches it. NOT an internal
# hostname and NOT carrying X-Internal-Key: the anonymous seat is the subject.
_MCP_URL = (os.environ.get("DCHUB_MCP_URL")
            or "https://dchub.cloud/mcp").rstrip("/")

# How many tools one tick may sample. Every sampled tool costs that tool's
# anonymous day-budget for this IP, so this is a blindness budget, not a
# performance knob. Three is two calls each plus headroom under the cap.
_SAMPLE_SIZE = 3

# Calls per tool per tick. Two is the minimum that can observe a DELTA, and
# the maximum that leaves a 2-answer cap able to show one.
_CALLS_PER_TOOL = 2

# ★ Read-only, single-capability, cheap, and each takes an argument that can be
# VARIED between calls — required, or an unchanged meter is explainable as a
# cached response. Deliberately excludes execute_plan (expensive, multi-step),
# anything that writes (save_*, set_*, subscribe_*, claim_*, bind_*), and
# unlock_more_data (it exists to move money).
_SAMPLE_POOL: tuple[tuple[str, str, list[dict]], ...] = (
    ("get_gas_intelligence", "state", [{"state": "TX"}, {"state": "PA"}]),
    ("get_market_intel", "market", [{"market": "ashburn"}, {"market": "dallas"}]),
    ("get_iso_context", "iso", [{"iso": "ERCOT"}, {"iso": "PJM"}]),
    ("get_energy_prices", "state", [{"state": "OH"}, {"state": "GA"}]),
    ("get_market_dcpi_rank", "market", [{"market": "phoenix"},
                                        {"market": "atlanta"}]),
    ("get_fiber_intel", "location", [{"location": "Columbus, OH"},
                                     {"location": "Reno, NV"}]),
)

# The meter's own field names, in the two shapes the server has published.
_REMAINING_KEYS = ("full_answers_remaining_today", "remaining_full_today")
_CAP_KEYS = ("full_answers_cap_today", "full_answers_cap")

# ★★ WITHDRAWN MARKERS — the ONLY payload signal this shell judges on. Their
# meaning is "this content no longer exists at any tier", which is what makes a
# published budget unspendable. Measured live 2026-08-08: get_gas_intelligence
# carries both on every call while its meter sits at 2 of 2.
_WITHDRAWN_MARKERS = (
    "omitted_no_fabrication", "_omitted_no_fabrication_total_in_pro",
)

# ★ Depth-slicing, NOT withdrawal — "3 more providers in Pro". It rides on
# answers that DO consume a full answer (get_market_intel carried three of
# these while going 1 -> 0), so it is recorded as context and judges NOTHING.
# Treating it as partiality is the bug this shell shipped and caught on its
# own first live run.
_SLICE_SUFFIX = "_total_in_pro"

# Upsell furniture. On nearly every free response, billable or not. Never
# evidence of anything.
_CTA_KEYS = ("_upgrade", "upgrade", "inline_full", "trial_preview",
             "preview_is_partial")

# Envelope furniture — present regardless of whether an answer was served, so
# it can never count as evidence that one WAS.
_ENVELOPE_KEYS = frozenset({
    "quota", "citation", "license", "note", "ok", "query", "tool", "platform",
    "error", "detail", "error_version", "resume", "next_session",
    "starter_pack", "auto_trial_key", "first_call_nudge", "for_your_human",
    "persist_command", "retry_instructions", "retry_with_header",
    "unlocked_tools", "count", "corpus",
})

_HTTP_TIMEOUT = 45


def _disabled() -> bool:
    return os.environ.get("METERING_HONESTY_SHELL_DISABLE", "") == "1"


# ── the anonymous seat ────────────────────────────────────────────────

def _sse_json(text: str):
    """Parse one MCP response body, which may be JSON or an SSE frame.

    Multi-line `data:` payloads join on newline and lose exactly one leading
    space each, per the SSE grammar — joining on "" corrupts long envelopes,
    which is how the first draft of this parser died on a 44kB reply.
    """
    parts = []
    for line in text.split("\n"):
        if line.startswith("data:"):
            v = line[5:]
            parts.append(v[1:] if v.startswith(" ") else v)
    return json.loads("\n".join(parts) if parts else text)


class _AnonSeat:
    """One anonymous MCP session. Never carries a key or an internal header."""

    def __init__(self) -> None:
        # requests, not urllib: regression_lint blocks urllib.request.urlopen
        # repo-wide with no exemption path.
        import requests  # noqa: PLC0415

        self._requests = requests
        self.session = requests.Session()
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            # Self-identifying, so this traffic can be excluded from reach and
            # usage metrics BY USER-AGENT — the MCP server overwrites platform.
            "User-Agent": "dchub-metering-honesty-shell",
        }
        self.error: str | None = None
        self.server: str | None = None
        self._open()

    def _open(self) -> None:
        try:
            r = self.session.post(
                _MCP_URL, headers=self.headers, timeout=_HTTP_TIMEOUT,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-03-26",
                                 "capabilities": {},
                                 "clientInfo": {
                                     "name": "dchub-metering-honesty-shell",
                                     "version": "1"}}})
            sid = r.headers.get("mcp-session-id")
            if sid:
                self.headers["mcp-session-id"] = sid
            info = (_sse_json(r.text).get("result") or {}).get("serverInfo") or {}
            self.server = f"{info.get('name')} {info.get('version')}".strip()
            self.session.post(
                _MCP_URL, headers=self.headers, timeout=_HTTP_TIMEOUT,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {str(e)[:120]}"

    def call(self, tool: str, args: dict) -> dict:
        """One tools/call. Always returns an observation, never raises."""
        if self.error:
            return {"ok": False, "why": f"session never opened ({self.error})"}
        try:
            r = self.session.post(
                _MCP_URL, headers=self.headers, timeout=_HTTP_TIMEOUT,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": tool, "arguments": args}})
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "why": f"transport {type(e).__name__}: "
                                        f"{str(e)[:100]}"}
        if r.status_code >= 400:
            return {"ok": False, "why": f"HTTP {r.status_code}"}
        try:
            body = _sse_json(r.text)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "why": f"unparseable envelope "
                                        f"({type(e).__name__})"}
        if body.get("error"):
            return {"ok": False,
                    "why": f"protocol error {str(body['error'])[:100]}"}
        sc = (body.get("result") or {}).get("structuredContent")
        if not isinstance(sc, dict):
            return {"ok": False, "why": "no structuredContent in the result"}
        return _read_envelope(sc, args)


def _first(d: dict, keys) -> object:
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _read_envelope(sc: dict, args: dict) -> dict:
    """What one envelope says about the meter, the seat, and the answer."""
    quota = sc.get("quota") if isinstance(sc.get("quota"), dict) else {}
    remaining = _first(quota, _REMAINING_KEYS)
    if remaining is None:
        remaining = _first(sc, _REMAINING_KEYS)
    cap = _first(quota, _CAP_KEYS)

    keys = set(sc.keys())
    withdrawn = sorted(k for k in _WITHDRAWN_MARKERS if k in keys)
    sliced = sorted(k for k in keys
                    if k.endswith(_SLICE_SUFFIX) and k not in withdrawn)
    data_fields = sorted(k for k in keys
                         if not k.startswith("_")
                         and k not in _ENVELOPE_KEYS
                         and k not in _CTA_KEYS)

    # ★ Three states, and only the first two are decidable. WITHDRAWN wins over
    # ANSWERED even when data rides along, because the withheld part is the
    # part the budget was for.
    if sc.get("error"):
        answer = "ERROR"
    elif withdrawn:
        answer = "WITHDRAWN"
    elif data_fields:
        answer = "ANSWERED"
    else:
        answer = "EMPTY"

    return {
        "ok": True,
        "args": args,
        "answer": answer,
        "tier": (quota.get("tier") or sc.get("caller_tier")
                 or sc.get("tier") or None),
        "has_meter": remaining is not None,
        "remaining": remaining if isinstance(remaining, (int, float)) else None,
        "cap": cap if isinstance(cap, (int, float)) else None,
        "withdrawn": withdrawn,
        "sliced": sliced[:4],
        "data_fields": len(data_fields),
    }


# ── sampling ──────────────────────────────────────────────────────────

def _todays_sample(override: str | None = None) -> list[tuple]:
    """Which tools this tick may spend budget on.

    Rotated by day-of-year rather than randomly: the same day always samples
    the same tools, so a red is reproducible by a human on the same day, and
    consecutive days cover the pool instead of re-burning one tool. (Randomness
    would also make this untestable.)
    """
    if override:
        want = {t.strip() for t in override.split(",") if t.strip()}
        picked = [row for row in _SAMPLE_POOL if row[0] in want]
        if picked:
            return picked[:len(_SAMPLE_POOL)]
    doy = datetime.now(timezone.utc).timetuple().tm_yday
    start = (doy * _SAMPLE_SIZE) % len(_SAMPLE_POOL)
    return [_SAMPLE_POOL[(start + i) % len(_SAMPLE_POOL)]
            for i in range(min(_SAMPLE_SIZE, len(_SAMPLE_POOL)))]


def _observe(seat: _AnonSeat, sample: list[tuple]) -> dict:
    """Call every sampled tool _CALLS_PER_TOOL times with DIFFERENT arguments.

    One pass, shared by all four lanes: the lanes must judge the SAME calls, or
    they would each spend the budget again and the later ones would find a
    board they themselves blinded.
    """
    out: dict[str, list[dict]] = {}
    for tool, _param, arglist in sample:
        obs = []
        for i in range(_CALLS_PER_TOOL):
            obs.append(seat.call(tool, arglist[i % len(arglist)]))
            time.sleep(0.4)  # not a rate-limit dodge; just not a burst
        out[tool] = obs
    return out


def _seat_ok(obs: list[dict]) -> str | None:
    """None when the anon seat was genuinely observed, else why not."""
    good = [o for o in obs if o.get("ok")]
    if not good:
        why = next((o.get("why") for o in obs if o.get("why")), "no reply")
        return f"could not reach the tool ({why})"
    tiers = {o.get("tier") for o in good if o.get("tier")}
    off = sorted(t for t in tiers if t != "free")
    if off:
        return (f"the envelope claims tier {off} — this probe was not served "
                f"the anonymous seat, so nothing here describes a real agent")
    return None


# ── lanes ─────────────────────────────────────────────────────────────

def _lane_consistent(runs: dict) -> list[dict]:
    """1 · a meter published on call 1 is still published on call 2.

    The regression guard for #2210 ("Quota meter VANISHES from the envelope
    between calls"), closed 2026-08-04. A meter that disappears is worse than
    one that does not move: the agent cannot even tell it is being metered.
    """
    checks = []
    for tool, obs in runs.items():
        blind = _seat_ok(obs)
        if blind:
            checks.append(_check(f"consistent::{tool}",
                                 f"{tool} publishes its meter on every call",
                                 None, blind))
            continue
        seen = [o for o in obs if o.get("ok")]
        flags = [o["has_meter"] for o in seen]
        if not any(flags):
            checks.append(_check(
                f"consistent::{tool}",
                f"{tool} publishes its meter on every call", None,
                f"{tool} publishes no full-answers meter at all "
                f"({len(seen)} call(s)) — nothing to be consistent about. "
                f"Silence is a defensible contract; see the coherent lane"))
            continue
        ok = all(flags)
        checks.append(_check(
            f"consistent::{tool}",
            f"{tool} publishes its meter on every call", ok,
            (f"meter present on all {len(flags)} call(s)" if ok else
             f"meter present on call(s) "
             f"{[i + 1 for i, f in enumerate(flags) if f]} and ABSENT on "
             f"{[i + 1 for i, f in enumerate(flags) if not f]} — the caller "
             f"cannot tell whether it is still being metered"),
            critical=not ok))
    return checks


def _lane_spends(runs: dict) -> list[dict]:
    """2 · a meter with room to move goes DOWN across two consuming calls.

    ★ It does NOT try to decide whether the answer was "full enough" to be
    billable — that judgement is what the first draft got wrong, and it is the
    platform's to make, not the probe's. The published contract is per tool:
    "you have N left of this tool today". Two calls to that tool with different
    arguments either move it or they do not.

    Two abstentions, both principled: a meter already at zero cannot count down
    (it proves nothing either way), and a WITHDRAWN payload is lane 3's
    conviction, not a second red here for the same defect.
    """
    checks = []
    for tool, obs in runs.items():
        cid = f"spends::{tool}"
        name = f"{tool} charges the caller for a full answer"
        blind = _seat_ok(obs)
        if blind:
            checks.append(_check(cid, name, None, blind))
            continue
        seen = [o for o in obs if o.get("ok") and o["has_meter"]]
        if len(seen) < 2:
            checks.append(_check(cid, name, None,
                                 f"{tool} did not publish a meter on two "
                                 f"comparable calls — nothing to difference"))
            continue
        a, b = seen[0], seen[-1]
        if a["remaining"] is None or b["remaining"] is None:
            checks.append(_check(cid, name, None,
                                 "the meter was published without a numeric "
                                 "remaining value"))
            continue
        if a["remaining"] <= 0:
            checks.append(_check(cid, name, None,
                                 f"{tool} was already at remaining="
                                 f"{a['remaining']} for this IP — a meter at "
                                 f"zero cannot count down, so it proves "
                                 f"nothing either way"))
            continue
        if any(o["answer"] == "WITHDRAWN" for o in (a, b)):
            checks.append(_check(cid, name, None,
                                 f"{tool} returns a WITHDRAWN payload "
                                 f"{a['withdrawn'] or b['withdrawn']} — it "
                                 f"cannot spend what it no longer serves. "
                                 f"Convicted in the unspendable lane instead, "
                                 f"so one defect is one red"))
            continue
        if any(o["answer"] in ("ERROR", "EMPTY") for o in (a, b)):
            checks.append(_check(cid, name, None,
                                 f"{tool} did not answer on both calls "
                                 f"({a['answer']}, {b['answer']}) — an "
                                 f"unanswered call should not be billed, so "
                                 f"an unmoved meter proves nothing"))
            continue
        moved = b["remaining"] < a["remaining"]
        checks.append(_check(
            cid, name, moved,
            (f"{a['remaining']} -> {b['remaining']} across two answered calls "
             f"(args {a['args']} then {b['args']})" if moved else
             f"remaining stayed at {a['remaining']} across two ANSWERED calls "
             f"with DIFFERENT arguments ({a['args']} then {b['args']}) and "
             f"with room to move — a meter that does not measure teaches the "
             f"agent the wrong cost model"),
            critical=not moved))
    return checks


def _lane_unspendable(runs: dict) -> list[dict]:
    """3 · no tool advertises a budget it cannot spend.

    ★ THE GAS CASE, and the reason this shell exists. get_gas_intelligence
    publishes cap=2 / remaining=2 while every call returns the WITHDRAWN
    payload (`omitted_no_fabrication`). Nothing can ever consume it, so the
    meter is not so much broken as MEANINGLESS — and it is read by agents
    deciding whether to pay. Either stop publishing the meter on a tool whose
    answer was withdrawn, or publish cap=0.

    Deliberately narrower than "the meter did not move" (lane 2's job): this
    one names the CAUSE, and it is the difference between "fix the counter"
    and "stop selling something you withdrew".
    """
    checks = []
    for tool, obs in runs.items():
        cid = f"unspendable::{tool}"
        name = f"{tool} does not advertise budget it cannot spend"
        blind = _seat_ok(obs)
        if blind:
            checks.append(_check(cid, name, None, blind))
            continue
        seen = [o for o in obs if o.get("ok")]
        metered = [o for o in seen if o["has_meter"]
                   and isinstance(o["remaining"], (int, float))]
        if not metered:
            checks.append(_check(cid, name, None,
                                 f"{tool} publishes no numeric meter — it "
                                 f"cannot over-promise what it does not claim"))
            continue
        room = [o for o in metered if o["remaining"] > 0]
        if not room:
            checks.append(_check(cid, name, None,
                                 f"{tool} is at remaining=0 for this IP — a "
                                 f"spent meter cannot be shown to be "
                                 f"unspendable"))
            continue
        withdrawn = [o for o in seen if o["answer"] == "WITHDRAWN"]
        if not withdrawn:
            slices = sorted({s for o in seen for s in o["sliced"]})
            checks.append(_check(
                cid, name, True,
                f"{tool} answered while claiming remaining="
                f"{room[0]['remaining']} of cap={room[0]['cap']} — the "
                f"advertised budget buys something that still exists"
                + (f" (depth-sliced in Pro: {slices}, which is a normal paid "
                   f"answer, not a withdrawal)" if slices else "")))
            continue
        marks = sorted({m for o in withdrawn for m in o["withdrawn"]})
        checks.append(_check(
            cid, name, False,
            f"{tool} advertises cap={room[0]['cap']} remaining="
            f"{room[0]['remaining']} while {len(withdrawn)} of {len(seen)} "
            f"call(s) came back WITHDRAWN {marks} — the payload the budget is "
            f"for no longer exists at any tier, so the meter can never move. "
            f"Withdraw the meter or publish cap=0; an agent reads this budget "
            f"when it decides whether to pay",
            critical=True))
    return checks


def _lane_coherent(runs: dict) -> list[dict]:
    """4 · the numbers are internally possible.

    cap >= remaining >= 0, cap > 0 wherever a meter is published, and remaining
    never INCREASES inside one tick. Cheap, and it catches the class where the
    counter is read from a different key space than it is written to.
    """
    checks = []
    for tool, obs in runs.items():
        cid = f"coherent::{tool}"
        name = f"{tool} publishes internally consistent numbers"
        blind = _seat_ok(obs)
        if blind:
            checks.append(_check(cid, name, None, blind))
            continue
        metered = [o for o in obs if o.get("ok") and o["has_meter"]
                   and isinstance(o["remaining"], (int, float))]
        if not metered:
            checks.append(_check(cid, name, None,
                                 f"{tool} publishes no numeric meter"))
            continue
        bad = []
        for i, o in enumerate(metered, 1):
            if o["remaining"] < 0:
                bad.append(f"call {i}: remaining={o['remaining']} < 0")
            if isinstance(o["cap"], (int, float)):
                if o["cap"] <= 0:
                    bad.append(f"call {i}: cap={o['cap']} while a meter is "
                               f"published")
                elif o["remaining"] > o["cap"]:
                    bad.append(f"call {i}: remaining={o['remaining']} > "
                               f"cap={o['cap']}")
            else:
                bad.append(f"call {i}: remaining={o['remaining']} published "
                           f"with no cap to read it against")
        for i in range(1, len(metered)):
            if metered[i]["remaining"] > metered[i - 1]["remaining"]:
                bad.append(f"remaining ROSE {metered[i - 1]['remaining']} -> "
                           f"{metered[i]['remaining']} inside one tick")
        checks.append(_check(
            cid, name, not bad,
            "; ".join(bad) if bad else
            f"cap={metered[0]['cap']} remaining="
            f"{[o['remaining'] for o in metered]} — consistent",
            critical=bool(bad)))
    return checks


_LANES = (
    ("consistent", "a meter shown once is shown every time", _lane_consistent),
    ("spends", "a full answer costs what the meter says", _lane_spends),
    ("unspendable", "no budget advertised that cannot be spent",
     _lane_unspendable),
    ("coherent", "the numbers are internally possible", _lane_coherent),
)


def _population(sample: list[tuple], seat: _AnonSeat) -> dict:
    """Built from the sample actually executed, never hand-typed (#2253)."""
    return {
        "question": "not 'is the caller gated' but 'does the METER the caller "
                    "is shown actually measure anything'",
        "door": _MCP_URL,
        "seat": "anonymous — no API key, no X-Internal-Key, no admin header",
        "server": seat.server or "unidentified",
        "lanes": [lid for lid, _, _ in _LANES],
        "sampled_tools": [t for t, _, _ in sample],
        "pool": [t for t, _, _ in _SAMPLE_POOL],
        "rotation": f"{_SAMPLE_SIZE} tool(s) per tick, rotated by day-of-year "
                    f"— the cap is keyed on (ip, tool, day), so a tick spends "
                    f"the budget it measures and an eager board blinds itself",
        "calls_per_tool": _CALLS_PER_TOOL,
        "argument_rule": "every call inside a tool uses DIFFERENT arguments, "
                         "so an unchanged meter cannot be a cached response",
        "answer_rule": "ANSWERED = real data and no withdrawal marker. "
                       "WITHDRAWN = the payload is gone at every tier and the "
                       "budget can never be spent. Depth-slicing "
                       f"(*{_SLICE_SUFFIX}) is a normal paid answer and "
                       "judges nothing",
        "withdrawn_markers": list(_WITHDRAWN_MARKERS),
        "never_red": ["remaining==0 (a spent meter proves nothing)",
                      "a tier other than free (not the anonymous seat)",
                      "a transport or protocol failure (unreadable is not "
                      "dead)"],
        "writes": "none — read-only tools, no key, no mutation",
    }


def _tick(override: str | None = None) -> dict:
    sample = _todays_sample(override)
    seat = _AnonSeat()
    runs = _observe(seat, sample) if not seat.error else {
        t: [{"ok": False, "why": f"session never opened ({seat.error})"}]
        for t, _, _ in sample}
    lanes = []
    for lid, name, fn in _LANES:
        checks = _safe_lane(fn, runs)
        lanes.append({"id": lid, "name": name, "checks": checks,
                      "verdict": _lane_verdict(checks)})
    return {
        "shell": "metering-honesty",
        "note": ("A meter that does not measure is the defect, and it is "
                 "PER TOOL: gas advertises a budget nothing can spend, "
                 "market_intel drops 2->0 on the first answer, iso_context "
                 "publishes no meter at all. Every check id names its tool so "
                 "a finding can never again be true of 'the meter' in "
                 "general."),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "population": _population(sample, seat),
        "observations": runs,
        "lanes": lanes,
        "lanes_total": len(lanes),
        "lanes_pass": sum(1 for x in lanes if x["verdict"] == "PASS"),
        "summary": " ".join(f"{x['id']}={x['verdict']}" for x in lanes),
    }


@metering_honesty_master_shell_bp.route(
    "/api/v1/admin/metering-honesty/master-tick", methods=["GET"])
def metering_honesty_master_tick():
    if _disabled():
        return jsonify({"disabled": True}), 200
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_tick(request.args.get("tools")))


@metering_honesty_master_shell_bp.route(
    "/admin/metering-honesty", methods=["GET"])
def metering_honesty_board():
    if _disabled():
        return Response("shell disabled", mimetype="text/plain")
    if not _admin_ok():
        return Response("unauthorized", status=401, mimetype="text/plain")
    t = _tick(request.args.get("tools"))
    rows = []
    for lane in t["lanes"]:
        rows.append(f"\n{lane['verdict']:<5} {lane['id']} — {lane['name']}")
        for c in lane["checks"]:
            mark = {True: "OK ", False: "RED", None: " ? "}[c["pass"]]
            rows.append(f"   [{mark}] {c['name']}\n        {c['detail']}")
    head = (f"{t['summary']}\n{t['note']}\n"
            f"seat: {t['population']['seat']} @ {t['population']['door']} "
            f"({t['population']['server']})\n"
            f"sampled: {', '.join(t['population']['sampled_tools'])}")
    return Response(head + "\n" + "\n".join(rows), mimetype="text/plain")
