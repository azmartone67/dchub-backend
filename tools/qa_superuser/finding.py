#!/usr/bin/env python3
"""The verdict model for the QA super-user — and the rules that keep it honest.

Every other shell on this platform reads the database. This one sits in the
CALLER'S SEAT and asserts on what a real agent or human actually receives. That
difference is the whole point, and it only holds if the verdict model refuses
the four ways this platform has historically lied to itself:

1. **BLIND IS NOT RED.** `/api/land-power/status` returned a hardcoded
   ``"healthy"`` for four months; `get_backup_status` reported 9/9 feeds healthy
   while news content was dated two months in the FUTURE. The inverse error is
   just as expensive: a probe that cannot reach a surface must report BLIND, and
   BLIND must never be rendered, counted or escalated as a failure. A watcher
   problem masquerading as a dead loop burned six hours of false RED on the
   dead-man board on 2026-07-30.

2. **EVERY CHECK MUST BE ABLE TO SAY RED.** Shell #47 read PASS because the
   branch that would have failed was skipped (`is False` where it needed
   `is not True`) and it appended its "all consistent" summary anyway — a green
   line on top of "we could not tell". So `red_when` is a REQUIRED field: state
   the condition that produces RED, in words, at construction time. A check whose
   author cannot write that sentence does not belong in the critical set.

3. **NO INVENTED TARGETS.** Wrong evidence is worse than none (#39: a fixed
   evidence bundle made the model say something precise and FALSE). A dimension
   worth watching but lacking a threshold the system itself defines is a GAUGE:
   it reports a number, it never votes on the run's outcome.

4. **EVIDENCE IS AN OBSERVATION, NOT A RESTATEMENT.** ``evidence`` records what
   came back on the wire. Never assert a string's presence in source when you can
   exercise the behaviour — comments satisfy grep (#37).

``basis`` exists for a fifth reason, learned the expensive way in shell #49: an
absence proven with the wrong auth or by reading the wrong field is not an
absence. It nearly shipped a redundant fix for a return-nudge that already
existed, because the probe was anonymous and grepped ``content[].text`` while the
offer lives in ``structuredContent.next_session``. So every finding must state
the seat it was measured from and where in the envelope it looked.
"""
from __future__ import annotations

import dataclasses
import hashlib
from typing import Any


# ── verdicts ────────────────────────────────────────────────────────────────
PASS = "PASS"      # observed, and the behaviour is correct
RED = "RED"        # observed, and the behaviour is wrong
BLIND = "BLIND"    # NOT observed — probe could not reach/parse. Never a failure.
GAUGE = "GAUGE"    # observed and reported as a number; no pass/fail claim

# ── severities (only CRITICAL and MAJOR vote on the run outcome) ────────────
CRITICAL = "critical"   # a paying caller is getting a wrong answer, or cannot pay
MAJOR = "major"         # a real defect with a workaround, or an honesty gap
MINOR = "minor"         # cosmetic / low blast radius
INFO = "info"           # a measurement worth trending, no claim

_SEVERITY_RANK = {CRITICAL: 0, MAJOR: 1, MINOR: 2, INFO: 3}

# Seats. The seat is part of a finding's identity: "gated for anon" and "gated
# for a paying key" are DIFFERENT facts, and conflating them manufactures the
# label-vs-measure contradiction this whole tool exists to police.
SEAT_ANON = "anon"        # no credential at all — how most agents actually arrive
SEAT_TRIAL = "trial"      # auto-minted trial key
SEAT_PAID = "paid"        # a real paying key (reviewer key in CI)
SEAT_ADMIN = "admin"      # owner/admin credential
SEAT_CRAWLER = "crawler"  # a search/AI crawler — a REAL seat, not a flavour of
                          # anon. It is served different headers, obeys robots
                          # directives no human reads, and indexes what it is
                          # shown. /admin answered 200 to Googlebot with three of
                          # four indexation signals saying "index", and
                          # /operators published "0 tracked" under index,follow —
                          # neither is visible from the anon seat.
SEAT_NONE = "none"        # not seat-dependent (e.g. a public page's HTTP status)


@dataclasses.dataclass
class Finding:
    """One observation from one seat about one surface.

    ``key`` is the durable identity used to diff runs. It must be stable across
    runs for the SAME underlying fact and different for different facts —
    otherwise "regressed" and "new" are indistinguishable and the board becomes
    noise. It deliberately includes the seat.
    """

    key: str
    surface: str          # mcp | web | data | media | harness
    seat: str
    title: str
    verdict: str
    severity: str
    evidence: str         # what was OBSERVED on the wire, with numbers
    basis: str            # seat + endpoint + WHERE in the payload we looked
    red_when: str         # the condition that makes this RED, stated in words
    value: Any = None     # optional numeric for trending
    remedy: str = ""      # what to do about it — empty is honest when unknown

    # ★ BLIND has two causes and they need OPPOSITE responses. A platform
    #   surface being unreachable is a request to look again, and routing it
    #   anywhere would manufacture a defect (rule 1). THIS harness's own code
    #   crashing is a defect — ours — and the only people who can fix it are the
    #   ones reading this board. Conflating them is why `registries` crashed on
    #   every run for two days with nothing escalating: it was excluded from the
    #   red count (correct) AND from every actuation path (wrong).
    #
    #   This never makes a finding count as a failure — see counts_as_failure.
    #   It only says WHO the finding is addressed to.
    instrument_fault: bool = False

    # ★2026-08-29 lane 6 (qa-as-claims). A PASS here is true at the moment the
    # harness ran and then stands unchallenged until someone runs it again by
    # hand. These two fields let a check pre-register itself with the claim
    # ledger so the SAME assertion is re-judged at a horizon, and a green that
    # stops being true becomes a refutation — which the negative-lesson corpora
    # already feed back into the loop.
    #
    # OPTIONAL ON PURPOSE, and rule 3 is why. Most checks here are multi-request
    # behavioural assertions from a specific seat; the ledger's instruments are
    # single readings (get:/finding:/canon:/linkedin:). A check that cannot
    # state a re-measurable instrument must NOT invent one — it is counted as
    # unbacked and reported, so the coverage gap is visible instead of implied.
    claim_metric: str = ""   # e.g. "get:/api/v1/ops/freshness sources_measured"
    claim_expect: str = ""   # e.g. ">= 7"  — the ledger's comparator vocabulary

    def __post_init__(self) -> None:
        if self.verdict not in (PASS, RED, BLIND, GAUGE):
            raise ValueError(f"{self.key}: bad verdict {self.verdict!r}")
        if self.severity not in _SEVERITY_RANK:
            raise ValueError(f"{self.key}: bad severity {self.severity!r}")
        # Rule 2, enforced rather than documented. A check that cannot state its
        # own failure condition cannot be trusted to detect it.
        if not self.red_when.strip():
            raise ValueError(f"{self.key}: red_when is required — state the "
                             "condition that produces RED, or make it a GAUGE")
        if not self.basis.strip():
            raise ValueError(f"{self.key}: basis is required — name the seat and "
                             "the exact field/endpoint observed (shell #49 trap)")
        # Rule 3: a GAUGE reports, it never votes.
        if self.verdict == GAUGE and self.severity in (CRITICAL, MAJOR):
            raise ValueError(f"{self.key}: a GAUGE may not carry severity "
                             f"{self.severity} — gauges do not vote on the run")
        # Lane 6: half a claim is not a claim. A metric with no expectation
        # would register as "measured nothing in particular" and confirm
        # itself, which is the shape the canon fix removed from the ledger.
        if bool(self.claim_metric.strip()) != bool(self.claim_expect.strip()):
            raise ValueError(
                f"{self.key}: claim_metric and claim_expect must be set "
                "together — an instrument with no expectation cannot be "
                "refuted, and an expectation with no instrument cannot be "
                "measured")

    @property
    def claim_backed(self) -> bool:
        """Can this check be re-judged without re-running the harness?

        Only a PASS is registered: a RED is already known-bad, a BLIND was not
        observed, and a GAUGE makes no claim to refute.
        """
        return bool(self.verdict == PASS and self.claim_metric.strip()
                    and self.claim_expect.strip())

    @property
    def counts_as_failure(self) -> bool:
        """Only an OBSERVED wrong behaviour at real severity is a failure.

        BLIND is excluded by construction — that is rule 1, in code, at the one
        place every consumer funnels through.
        """
        return self.verdict == RED and self.severity in (CRITICAL, MAJOR)

    @property
    def rank(self) -> tuple:
        order = {RED: 0, BLIND: 1, GAUGE: 2, PASS: 3}
        return (order[self.verdict], _SEVERITY_RANK[self.severity], self.key)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


def blind(key: str, surface: str, seat: str, title: str, why: str,
          basis: str, instrument_fault: bool = False) -> Finding:
    """Build a BLIND finding — the probe could not observe, so it claims nothing.

    Use this for every transport failure, timeout, parse failure and auth
    rejection that prevents observation. Never turn one into a RED "because it
    was probably down": the deadman watcher's own rule is that a GitHub outage
    must not be reported as a fleet of dead loops.

    Pass ``instrument_fault=True`` when the thing that failed is OURS — a probe
    raising, a probe yielding nothing. Still BLIND, still never a failure; but
    addressed to this repo rather than filed as "look again later".
    """
    return Finding(
        key=key, surface=surface, seat=seat, title=title,
        verdict=BLIND, severity=INFO,
        evidence=f"could not observe: {why}",
        basis=basis,
        red_when="n/a — BLIND means unobserved; this finding makes no claim",
        instrument_fault=instrument_fault,
    )


def stable_key(*parts: str) -> str:
    """A short, stable identity for a finding.

    Hash-suffixed so a long descriptive part cannot collide after truncation,
    but readable at a glance on the board.
    """
    joined = "::".join(str(p) for p in parts)
    h = hashlib.sha1(joined.encode()).hexdigest()[:6]
    slug = "::".join(str(p) for p in parts)[:72]
    return f"{slug}#{h}"


def summarize(findings: list[Finding]) -> dict:
    """Roll a run up into counts. BLIND is reported separately from RED, always."""
    out = {"total": len(findings), "red": 0, "blind": 0, "pass": 0, "gauge": 0,
           "failures": 0, "critical": 0}
    for f in findings:
        if f.verdict == RED:
            out["red"] += 1
            if f.severity == CRITICAL:
                out["critical"] += 1
        elif f.verdict == BLIND:
            out["blind"] += 1
        elif f.verdict == PASS:
            out["pass"] += 1
        else:
            out["gauge"] += 1
        if f.counts_as_failure:
            out["failures"] += 1
    return out
