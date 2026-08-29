#!/usr/bin/env python3
"""Lane 6 (qa-as-claims) — a green that later stops being true becomes a lesson.

Every PASS on this board is true at the moment the harness ran, and then it
STANDS UNCHALLENGED until a human runs the harness again. That is the silent
green this platform keeps rediscovering: /api/land-power/status returned a
hardcoded "healthy" for four months, and get_backup_status reported 9/9 feeds
healthy while news content was dated two months in the FUTURE. Nothing was
lying at the moment of the check; the check simply stopped being re-asked.

routes/claim_ledger already solves exactly this for other producers: register
an assertion with a HORIZON, and a verifier that is not the author judges it
when the horizon arrives. A refutation lands in the negative-lesson corpora and
comes back as recall on the next decision. So a QA check that can name a
re-measurable instrument registers itself as a claim, and the ledger re-asks
the question on a clock instead of on someone's memory.

★ COVERAGE IS REPORTED, NEVER IMPLIED. Most checks here are multi-request
behavioural assertions from a specific seat ("all 12 public pages render",
"the edge honours no-store on every probed path"). The ledger's instruments
are single readings — get: / finding: / canon: / linkedin:. Those checks
genuinely CANNOT be expressed as a claim, and finding.py's rule 3 is explicit
that a dimension without a threshold the system itself defines must not invent
one. So they are counted as UNBACKED and named in the result, and the caller
gets `backed`, `unbacked` and the reasons — not a number that reads like full
coverage.

That distinction is the deliverable as much as the registration is. A harness
that reported "all checks are claim-backed" while most of them were not would
be the same defect the board exists to catch, wearing the board's own uniform.
"""
from __future__ import annotations

from .finding import BLIND, GAUGE, PASS, RED, Finding

# A day. Long enough that a transient blip is not a refutation, short enough
# that a green cannot stand for a season unchallenged.
DEFAULT_HORIZON_HOURS = 24

# Why a finding could not be registered. Reported per key, so the gap is
# legible instead of being a bare count.
_NOT_PASS = "not a PASS — nothing to refute"
_NO_INSTRUMENT = ("no re-measurable instrument: this check is a multi-request "
                  "behavioural assertion, not a single reading")


def _reason(f: Finding) -> str | None:
    """None when the finding IS registrable."""
    if f.verdict != PASS:
        return "%s (%s)" % (_NOT_PASS, f.verdict)
    if not (f.claim_metric.strip() and f.claim_expect.strip()):
        return _NO_INSTRUMENT
    return None


def plan(findings) -> dict:
    """What WOULD be registered, without touching the ledger. Pure."""
    backed, unbacked = [], {}
    for f in findings or []:
        why = _reason(f)
        if why is None:
            backed.append(f)
        else:
            unbacked[f.key] = why
    total = len(findings or [])
    passes = len([f for f in (findings or []) if f.verdict == PASS])
    return {
        "total": total,
        "passes": passes,
        "backed": [f.key for f in backed],
        "unbacked": unbacked,
        # Of the PASSES — the only findings that COULD be claim-backed. Using
        # `total` here would flatter the number by counting REDs and BLINDs as
        # legitimately-unbacked, which they are, but that is not the gap the
        # reader is asking about.
        "coverage_of_passes": (round(len(backed) / passes, 3) if passes else None),
        "_backed_findings": backed,
    }


def register_run_claims(findings, register=None,
                        horizon_hours: int = DEFAULT_HORIZON_HOURS) -> dict:
    """Pre-register every claim-backed PASS. Returns the plan plus outcomes.

    `register` is injectable so this is testable without a database; it
    defaults to routes.claim_ledger.register_claim. A ledger failure is
    fail-soft and REPORTED — the QA run must not die because the ledger is
    down, and it must not silently report success either.
    """
    out = plan(findings)
    backed = out.pop("_backed_findings")
    out.update({"registered": 0, "already": 0, "refused": [], "errors": []})
    if not backed:
        return out
    if register is None:
        try:
            from routes.claim_ledger import register_claim as register
        except Exception as e:  # noqa: BLE001
            out["errors"].append("ledger unavailable: %s" % str(e)[:140])
            return out
    for f in backed:
        try:
            res = register(
                kind="qa",
                subject="qa:%s" % f.key,
                statement=f.title,
                expected_metric=f.claim_metric.strip(),
                expected_value=f.claim_expect.strip(),
                horizon_hours=horizon_hours,
                regime={"seat": f.seat, "surface": f.surface,
                        "basis": f.basis, "red_when": f.red_when,
                        "observed_value": f.value,
                        "evidence_at_registration": f.evidence},
                surfaces=[f.surface],
                shipped=True)
        except Exception as e:  # noqa: BLE001
            out["errors"].append("%s: %s" % (f.key, str(e)[:140]))
            continue
        if not isinstance(res, dict):
            out["errors"].append("%s: ledger returned %r" % (f.key, res))
        elif res.get("refused"):
            out["refused"].append({"key": f.key, "error": res.get("error")})
        elif res.get("ok") and res.get("already"):
            out["already"] += 1
        elif res.get("ok"):
            out["registered"] += 1
        else:
            out["errors"].append("%s: %s" % (f.key, res.get("error")))
    return out


def http_register(origin: str = "", admin_key: str = ""):
    """A `register` for register_run_claims that speaks HTTP, not SQL.

    ★ THE HARNESS HAS NO DATABASE. It runs from a GitHub Action whose only
    dependency is `requests`; routes.claim_ledger imports psycopg2 and the app
    config, so register_run_claims' in-process default degrades to a reported
    "ledger unavailable" on EVERY run from this seat. Registering over the admin
    API is the only path that actually reaches the ledger from here — the same
    reason board.py posts its beat instead of writing the table.

    ★ TARGETS THE RAILWAY ORIGIN, NEVER THE CF EDGE, for the reason board.py
    already documents: the zone's 15s route timeout kills admin POSTs through
    dchub.cloud.

    Returns None when no admin key is in the environment, so the caller can
    report a MISSING CREDENTIAL instead of registering nothing and printing a
    zero that reads like "there was nothing to register".
    """
    from . import config as C
    from .http import QA_UA_TOKEN

    base = (origin or C.ORIGIN).rstrip("/")
    admin = (admin_key or C.ADMIN_KEY or "").strip()
    if not admin:
        return None
    url = "%s/api/v1/brain/claims" % base

    def _register(**kw) -> dict:
        import requests
        try:
            r = requests.post(url, json=kw, timeout=30, headers={
                "Content-Type": "application/json",
                "X-Admin-Key": admin,
                "User-Agent": QA_UA_TOKEN + "/1.0",
            })
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            return {"ok": False,
                    "error": "HTTP %s %s" % (r.status_code, r.text[:120])}
        if not isinstance(body, dict):
            return {"ok": False, "error": "ledger returned %r" % (body,)}
        return body

    return _register
