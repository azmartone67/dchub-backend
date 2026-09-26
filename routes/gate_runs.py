"""Gate liveness ledger — the dead-man board for GATES, not feeds.

`ingest_runs` + /api/v1/ops/deadman answer "did the loop RUN and insert sane
data". Nothing answers the same question about a GATE, and a gate fails in the
opposite direction from a feed:

    feed   healthy = rows > 0        broken = zero rows      -> VISIBLE
    gate   healthy = usually silent  broken = stays silent   -> IDENTICAL

A feed that stops working goes quiet, and quiet is detectable. A gate that stops
working goes quiet too — and quiet is exactly what a healthy gate looks like.
A green check and a check that never ran are the same colour. This makes them
different colours.

★★★ THE VERDICT SEMANTICS ARE INVERTED FROM ingest_runs. READ THIS BEFORE
"FIXING" _OK_VERDICT.
`verdict="fail"` means the gate REFUSED something — it did its job, the PR went
red, and the ledger is HEALTHY. That is not an alarm here. A gate is unhealthy
when it never ran, went stale, could not measure, passed vacuously, or cannot
prove it is still able to fail. Someone will eventually read `fail` in this
column and "fix" it into an alarm; it is not one, and the whole board inverts
if they do.

Registry granularity is the JOB, not the workflow: `pre-merge.yml` holds four
blocking gates and one (`smoke-probe`) that carries job-level
continue-on-error and cannot fail a PR at all. A workflow-level board would
average those together and report the honest ones as cover for the inert one.

★ SINGLE WRITER. The gate beats for itself, at the end of its own run, and
NOTHING may beat on its behalf. tools/deadman/watch.py learned this three times
(osm-crawl 08-08, news-ner/iso-lmp/eia 08-10, iso-queue 08-19): its conclusion
beat overwrote honest `error` and `no_new_data` verdicts with a bare `success`,
turning a fix for a false green back INTO a false green. Do not add a
convenience writer here. A gate that did not beat reads `never-run`, which is
the correct and SAFE direction — a dropped beat makes a gate look dead, never
healthy.

Public read: GET  /api/v1/ops/gates          (keyless, 60s cache — same as /ops/deadman)
Beat:        POST /api/v1/admin/gates/beat   (admin-gated, fail-soft)
"""
import os
import datetime
import logging

import psycopg2
from flask import Blueprint, jsonify, request

log = logging.getLogger("gate_runs")
gate_runs_bp = Blueprint("gate_runs", __name__)

# Phase 4 flips this to True and G5 (no must-fail control) becomes an alarm
# instead of an advisory. Left as an env so the flip is a config change with a
# revert, not a deploy — and so the board can be dry-run against it first.
G5_BLOCKS = os.environ.get("DCHUB_GATE_G5_BLOCKS") == "1"

# Verdicts that are NOT themselves an alarm. `fail` is in here deliberately —
# see the docstring. `no_scope` is the gate analogue of the feed ledger's
# `no_new_data`: an AFFIRMATIVE "the gate ran, and there was nothing in scope
# to check this time" (a delta lint on a PR that touched no Python). Without it
# every scope-limited gate climbs the vacuous counter while perfectly healthy —
# the exact false red that burned eia-pricing and osm-crawl for days.
_OK_VERDICT = {"pass", "fail", "no_scope"}
_VERDICTS = {"pass", "fail", "unmeasured", "no_scope"}
# Verdicts asserting "examining zero things is EXPECTED this run" — they RESET
# the vacuous counter instead of climbing it.
_NO_SCOPE = {"no_scope"}
_SELFTEST = {"pass", "fail", "absent"}

# Fallback cadence when neither the registry nor the beat carried one.
_DEFAULT_CADENCE_H = 72.0
# G6 needs a long window — on a clean main a healthy gate is legitimately
# silent for months, which is why G6 only ever advises.
_NEVER_FIRED_DAYS = 90

# ── THE REGISTRY ──────────────────────────────────────────────────────────
# Source of truth for "which gates must keep running". A gate listed here but
# never beating reads `never-run` (G1) rather than being absent from the board,
# which is the entire point: a gate that was written, merged and never wired to
# a trigger is invisible without this list.
#
# `blocking` records whether the job can actually fail a PR. check-route-tables
# and pre-merge:smoke-probe carry job-level `continue-on-error: true`, so they
# cannot — they are real work reported as advisory, and a board that called
# them gates would be claiming protection that does not exist.
#
# cadence: 48h for jobs that also run on push to main (~30 commits/day, so 48h
# of silence is a real stall); 72h for pull_request-only jobs, generous enough
# that a quiet weekend is not an alarm.
GATE_REGISTRY = {
    # workflow:job                              repo              cad  blocking
    "api-response-contract:contract":          ("dchub-backend",  48,  True),
    "app-contract-gate:app-contract-gate":     ("dchub-backend",  48,  True),
    "pre-merge:syntax-check":                  ("dchub-backend",  48,  True),
    "pre-merge:regression-lint":               ("dchub-backend",  48,  True),
    "pre-merge:unit-tests":                    ("dchub-backend",  48,  True),
    "pre-merge:db-parity":                     ("dchub-backend",  48,  True),
    "pre-merge:smoke-probe":                   ("dchub-backend",  48,  False),
    "regression-lint:lint":                    ("dchub-backend",  48,  True),
    # ★ 2026-09-05 False → True. It carried job-level continue-on-error since
    # it shipped, so it could not fail a PR at all; the reason was that its
    # extractor reported 78% of routes uncovered on every run. The extractor is
    # fixed and the residue is baselined in scripts/route_table_baseline.json,
    # so the job now blocks on NEW mis-registration only.
    "check-route-tables:check":                ("dchub-backend",  48,  True),
    "brain-pr-substance-gate:substance-gate":  ("dchub-backend",  72,  True),
    "brain-pr-post-merge-guard:guard":         ("dchub-backend",  72,  True),
    "brain-spec-debt-tracker:file-spec-debt":  ("dchub-backend",  72,  True),
}


def _dsn():
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("NEON_DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or ""
    )


def _admin_ok():
    """Fails CLOSED — no admin key configured means 401, matching ingest_runs."""
    exp = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY") or ""
    got = (
        request.headers.get("X-Admin-Key")
        or request.headers.get("X-Internal-Key")
        or request.args.get("admin_key")
        or ""
    )
    return bool(exp) and got == exp


def _ensure(cur):
    """DDL via a RAW psycopg2 connection only — never db_utils.get_db().

    db_utils' PGCursorWrapper drops DDL silently when SKIP_DDL is set (default
    ON, absent from prod config), so a lazy CREATE TABLE through the pool is a
    no-op and the first INSERT dies inside somebody's except: pass. See
    scripts/check_ddl_through_pool.py.
    """
    cur.execute(
        """CREATE TABLE IF NOT EXISTS gate_runs (
            gate                TEXT PRIMARY KEY,
            repo                TEXT,
            blocking            BOOLEAN,
            last_run            TIMESTAMPTZ,
            last_verdict        TEXT,
            last_refusal        TIMESTAMPTZ,
            refusals_total      BIGINT DEFAULT 0,
            last_checked_n      BIGINT,
            consecutive_vacuous INT DEFAULT 0,
            selftest            TEXT DEFAULT 'absent',
            selftest_at         TIMESTAMPTZ,
            cadence_hours       NUMERIC,
            first_seen          TIMESTAMPTZ DEFAULT NOW(),
            note                TEXT,
            updated_at          TIMESTAMPTZ DEFAULT NOW()
        )"""
    )
    # Seed the registry. Runtime columns are never touched here — only the
    # static config the registry owns, so a registry edit propagates without
    # resetting a gate's history.
    for gate, (repo, cad, blocking) in GATE_REGISTRY.items():
        cur.execute(
            # 'absent' is BOUND, not spelled inline: regression_lint's
            # insert-no-on-conflict regex is `INSERT INTO (\w+)[^;"']*`, so a
            # quoted literal in VALUES ends the match before ON CONFLICT and the
            # statement reads as non-idempotent. Binding it is better SQL and
            # keeps the rule able to see the clause.
            """INSERT INTO gate_runs (gate, repo, blocking, cadence_hours, selftest)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (gate) DO UPDATE SET
                   repo          = EXCLUDED.repo,
                   blocking      = EXCLUDED.blocking,
                   cadence_hours = EXCLUDED.cadence_hours""",
            (gate, repo, blocking, cad, "absent"),
        )


def record_gate_beat(gate, verdict="pass", refusals=None, checked=None,
                     selftest=None, cad=None, repo=None, lr=None, note=None):
    """THE upsert. One row per gate; the vacuous counter is authoritative HERE.

    Takes ALREADY-COERCED values — HTTP concerns stay in the handler, which is
    what keeps the wire contract identical between the route and any in-process
    caller. Raises on failure; callers decide whether to fail soft.
    """
    dsn = _dsn()
    if not dsn:
        raise RuntimeError("no DATABASE_URL")
    # Sentinel so ON CONFLICT can tell "checked 0" (climb) from "unknown" (leave).
    checked_sig = checked if checked is not None else -1
    # no_scope asserts zero-checked is EXPECTED -> feed the counter a positive
    # sentinel so it RESETS rather than climbing toward the alarm.
    if str(verdict).lower() in _NO_SCOPE and checked_sig == 0:
        checked_sig = 1
    refused = 1 if str(verdict).lower() == "fail" else 0
    n_ref = int(refusals) if refusals is not None else refused
    with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c, c.cursor() as cur:
        _ensure(cur)
        cur.execute(
            """INSERT INTO gate_runs
                   (gate, repo, last_run, last_verdict, last_refusal, refusals_total,
                    last_checked_n, consecutive_vacuous, selftest, selftest_at,
                    cadence_hours, note, updated_at)
               VALUES (%s, %s, COALESCE(%s::timestamptz, NOW() ON CONFLICT DO NOTHING), %s,
                       CASE WHEN %s > 0 THEN NOW() END, %s, %s,
                       CASE WHEN %s = 0 THEN 1 ELSE 0 END, %s,
                       CASE WHEN %s IS NOT NULL THEN NOW() END, %s, %s, NOW())
               ON CONFLICT (gate) DO UPDATE SET
                   repo           = COALESCE(gate_runs.repo, EXCLUDED.repo),
                   last_run       = COALESCE(EXCLUDED.last_run, gate_runs.last_run),
                   last_verdict   = EXCLUDED.last_verdict,
                   last_refusal   = COALESCE(EXCLUDED.last_refusal, gate_runs.last_refusal),
                   refusals_total = gate_runs.refusals_total + EXCLUDED.refusals_total,
                   last_checked_n = COALESCE(EXCLUDED.last_checked_n, gate_runs.last_checked_n),
                   consecutive_vacuous = CASE WHEN %s = 0
                                              THEN gate_runs.consecutive_vacuous + 1
                                              WHEN %s < 0 THEN gate_runs.consecutive_vacuous
                                              ELSE 0 END,
                   selftest       = COALESCE(EXCLUDED.selftest, gate_runs.selftest),
                   selftest_at    = COALESCE(EXCLUDED.selftest_at, gate_runs.selftest_at),
                   cadence_hours  = COALESCE(gate_runs.cadence_hours, EXCLUDED.cadence_hours),
                   note           = COALESCE(EXCLUDED.note, gate_runs.note),
                   updated_at     = NOW()""",
            (gate, repo, lr, verdict, n_ref, n_ref, checked,
             checked_sig, selftest, selftest, cad, note,
             checked_sig, checked_sig),
        )
        c.commit()


def evaluate_gate(rec, now):
    """PURE. Given one ledger row, return (alarms, advisories) as reason strings.

    Pure and importable on purpose: scripts/gate_runs_selftest.py plants a
    defect in `rec` and asserts THIS function goes red, so the must-fail
    control exercises the real predicate rather than a paraphrase of it.
    """
    alarms, advisories = [], []
    cad_h = float(rec["cadence_hours"]) if rec.get("cadence_hours") is not None else _DEFAULT_CADENCE_H
    lr, verdict = rec.get("last_run"), (rec.get("last_verdict") or "").lower()

    # G1 — never ran. In the registry, never beat. Not protecting anything.
    if lr is None:
        alarms.append("never ran")
    else:
        # G2 — stale. Same 2x-cadence dead-man rule the feed board uses.
        age_h = (now - lr).total_seconds() / 3600.0
        if age_h > 2 * cad_h:
            alarms.append("last run %.0fh ago (>2x cadence %.0fh)" % (age_h, cad_h))

    # G3 — unmeasured. "Could not measure" is never rounded up to "fine";
    # api-response-contract.yml already runs on this rule (exit 2 = failure).
    if verdict == "unmeasured":
        alarms.append("verdict=unmeasured (could not compute — NOT a pass)")
    elif verdict and verdict not in _VERDICTS:
        alarms.append("verdict=%s (unknown)" % verdict[:40])

    # G4 — vacuous pass. Ran, examined nothing, reported green. This is the
    # 2,285-test collection abort and every self-skipping lint step.
    cv = rec.get("consecutive_vacuous") or 0
    checked = rec.get("last_checked_n")
    if verdict not in _NO_SCOPE:
        if checked == 0:
            alarms.append("examined 0 items — vacuous pass")
        elif cv >= 3:
            alarms.append("%d consecutive runs examined nothing" % cv)

    # G5 — unproven. No must-fail control means the gate cannot show it is
    # still able to fail. Advisory until DCHUB_GATE_G5_BLOCKS=1 (phase 4).
    st = (rec.get("selftest") or "absent").lower()
    if st != "pass":
        msg = ("no must-fail control — UNPROVEN, not passing" if st == "absent"
               else "must-fail control is %s" % st)
        (alarms if G5_BLOCKS else advisories).append(msg)

    # G6 — never fired. ADVISORY ONLY, permanently. This cannot distinguish a
    # dead gate from a clean main, which is precisely why G5 is the
    # load-bearing rule. Its job is ranking the G5 backfill: a gate that has
    # never refused anything AND has no control is the highest-risk row here.
    if not rec.get("refusals_total") and rec.get("last_refusal") is None:
        since = rec.get("first_seen") or lr
        if since is not None and (now - since).days >= _NEVER_FIRED_DAYS:
            advisories.append("never refused anything in %d+ days (cannot distinguish "
                              "dead gate from clean main — see must-fail control)"
                              % _NEVER_FIRED_DAYS)
    return alarms, advisories


@gate_runs_bp.route("/api/v1/admin/gates/beat", methods=["POST"])
def beat():
    """A gate records its OWN run. Body:
    {gate, verdict?, refusals?, checked?, selftest?, cadence_hours?, repo?, last_run?, note?}
    """
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    # ★ Argument validation runs BEFORE the DSN check, deliberately. With the
    # order reversed a bad `verdict` comes back 503 "no DATABASE_URL" — an
    # infrastructure error standing in for a caller error, so a gate beating
    # garbage would read as an outage and the sender would retry it forever.
    # Same family as the MCP surface returning 200 on a bad argument: what the
    # caller got wrong must be what the caller is told.
    j = request.get_json(silent=True) or {}
    gate = str(j.get("gate") or "").strip()[:120]
    if not gate:
        return jsonify(ok=False, error="gate required"), 400
    verdict = str(j.get("verdict") or "pass").strip().lower()[:40]
    if verdict not in _VERDICTS:
        return jsonify(ok=False, error="verdict must be one of %s" % sorted(_VERDICTS)), 400
    selftest = j.get("selftest")
    if selftest is not None:
        selftest = str(selftest).strip().lower()[:20]
        if selftest not in _SELFTEST:
            return jsonify(ok=False, error="selftest must be one of %s" % sorted(_SELFTEST)), 400

    def _int(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _float(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    if not _dsn():
        return jsonify(ok=False, error="no DATABASE_URL"), 503

    try:
        record_gate_beat(
            gate,
            verdict=verdict,
            refusals=_int(j.get("refusals")),
            checked=_int(j.get("checked")),
            selftest=selftest,
            cad=_float(j.get("cadence_hours")),
            repo=(str(j.get("repo")).strip()[:60] or None) if j.get("repo") else None,
            lr=(str(j.get("last_run")).strip() or None) if j.get("last_run") else None,
            note=(str(j.get("note")).strip()[:280] or None) if j.get("note") else None,
        )
    except Exception as e:  # noqa: BLE001 — fail soft; the ledger must never 500 a gate into a retry storm
        log.warning("gate_runs beat failed for %s: %s", gate, e)
        return jsonify(ok=False, error=str(e)[:200]), 500
    return jsonify(ok=True, gate=gate, verdict=verdict)


@gate_runs_bp.route("/api/v1/ops/gates", methods=["GET"])
def gates():
    """PUBLIC read — the gate liveness board. `any_overdue=true` means a gate
    stopped being able to protect anything.

    Envelope matches /api/v1/ops/deadman so anything that reads one reads the
    other, with one addition: `advisories` is separate from `overdue`, because
    G5/G6 rank work rather than raise alarms.
    """
    if not _dsn():
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        with psycopg2.connect(_dsn(), sslmode="require", connect_timeout=8) as c, c.cursor() as cur:
            _ensure(cur)
            cur.execute(
                """SELECT gate, repo, blocking, last_run, last_verdict, last_refusal,
                          refusals_total, last_checked_n, consecutive_vacuous,
                          selftest, selftest_at, cadence_hours, first_seen, note
                     FROM gate_runs"""
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("gate_runs read failed: %s", e)
        return jsonify(ok=False, error=str(e)[:200]), 500

    out, overdue, advised = [], [], []
    for (gate, repo, blocking, lr, verdict, lref, ref_n, checked,
         cv, selftest, st_at, cad, first_seen, note) in rows:
        rec = {
            "gate": gate, "repo": repo, "blocking": blocking,
            "last_run": lr, "last_verdict": verdict, "last_refusal": lref,
            "refusals_total": int(ref_n or 0), "last_checked_n": checked,
            "consecutive_vacuous": cv, "selftest": selftest,
            "cadence_hours": cad, "first_seen": first_seen,
        }
        alarms, advisories = evaluate_gate(rec, now)
        pub = {
            "gate": gate,
            "repo": repo,
            "blocking": bool(blocking) if blocking is not None else None,
            "last_run": lr.isoformat() if lr else None,
            "verdict": verdict,
            "last_refusal": lref.isoformat() if lref else None,
            "refusals_total": int(ref_n or 0),
            "last_checked_n": checked,
            "selftest": selftest or "absent",
            "selftest_at": st_at.isoformat() if st_at else None,
            "cadence_hours": float(cad) if cad is not None else _DEFAULT_CADENCE_H,
            "age_hours": round((now - lr).total_seconds() / 3600.0, 1) if lr else None,
            "overdue": bool(alarms),
            "reasons": alarms,
            "advisories": advisories,
        }
        if note:
            pub["note"] = note
        out.append(pub)
        if alarms:
            overdue.append(pub)
        if advisories:
            advised.append(pub)

    out.sort(key=lambda r: (not r["overdue"], not r["advisories"], r["gate"]))
    proven = sum(1 for r in out if r["selftest"] == "pass")
    resp = jsonify(
        ok=True,
        generated_at=now.isoformat(),
        tracked=len(out),
        any_overdue=bool(overdue),
        overdue_count=len(overdue),
        advisory_count=len(advised),
        proven_count=proven,
        proven_ratio=round(proven / len(out), 3) if out else None,
        g5_blocks=G5_BLOCKS,
        basis=("A gate is overdue when it never ran, went stale (>2x cadence), reported "
               "unmeasured, or examined nothing. verdict='fail' means the gate REFUSED "
               "something and is HEALTHY — it is not an alarm. selftest='absent' means the "
               "gate has no must-fail control and is UNPROVEN, reported as an advisory "
               "until DCHUB_GATE_G5_BLOCKS=1."),
        overdue=overdue,
        advisories=advised,
        gates=out,
    )
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


def register_gate_runs(app):
    try:
        app.register_blueprint(gate_runs_bp)
        log.info("gate_runs (gate liveness ledger) registered")
    except Exception as e:  # noqa: BLE001
        log.warning("gate_runs register failed: %s", e)
