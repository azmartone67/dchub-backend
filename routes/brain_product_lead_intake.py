"""routes/brain_product_lead_intake.py — the product lead's lane into the brain.

WHY THIS ONE IS DIFFERENT
=========================
`brain_qa_superuser_intake` and `brain_site_qa_intake` connect INSTRUMENTS: a
probe ran, something was measured, the intake decides whether to believe the
measurement. The product lead has no instrument. It emits JUDGEMENT — "the nav
hides search", "agents cannot see this page" — and judgement has no must-fail
control, no consecutive-failure count, and no canary. Seeding it directly would
put unfalsifiable opinion into the brain's actionable backlog, which is the
exact defect class the 2026-08-29 sweep spent six PRs removing:

    A claim was published, and nothing could falsify it.
    The highest-yield question is not "is this number right" but
    "what would turn this red, and does it exist?"

So this lane does not carry opinions. It carries CLAIMS THAT SURVIVED A
VERIFIER, and it reuses `routes/claim_ledger.py` — which already enforces the
only contract that makes judgement admissible:

  * `register_claim` is REFUSED when it carries no expectation. `expected_metric`
    must name a real instrument and `expected_value` must be a comparator.
    An opinion with no possible red state cannot be filed at all.
  * The OUTCOME WRITER IS NEVER THE AUTHOR. Producers may only register and
    mark shipped; `stamp_outcome` runs from the L16 verifier cron.
  * An instrument that has not measured is `unobserved`, NEVER refuted.

That gives this lane a STRONGER rule 0 than either sibling, and it is the
codebase's own answer rather than a new invention.

## What actually becomes brain work: REFUTATIONS

A `confirmed` claim is good news, and — in the words of the QA super-user's own
board code — *good news is the one output nobody investigates*. It is not work.
A `refuted` product claim is: we asserted something about the product, named the
instrument that could falsify it, and the instrument did. That is evidence with
a reproduction built in, which is exactly what the brain's worklist wants.

`unobserved` (the instrument never measured) and `retracted` (the owner withdrew
it) are both excluded — the first is honest ignorance, the second is not a
verdict. A claim with `superseded_by` set describes a regime that no longer
exists and is excluded too.

## Rule 0's board-level half

Per-row provenance is not enough: a VERIFIER that has broken could stamp
everything. Two gates, both weaker than a canary and named as such:

  * VERIFIER LIVENESS — if nothing has been stamped recently the standing
    verdicts cannot be said to describe now.
  * BLAST RADIUS — if an implausible share of recently-judged product claims
    came back refuted, a broken verifier or instrument is likelier than the
    product being wrong about everything at once.
    ★ This gate is SAMPLE-GATED: below `PLEAD_INTAKE_MIN_SAMPLE` judged claims
    there is no ratio worth computing, so it is NOT APPLIED and the status
    endpoint says so rather than implying a check that did not run.

## The write side lives here too

Nothing registers product claims today, so a read-only intake would ship empty
forever — the "registered ≠ delivered" failure this codebase keeps re-learning.
`POST /api/v1/brain/product-lead/claim` is the producer path. It is a thin
wrapper over `register_claim` — it does NOT re-implement the contract, so a
claim with no expectation is refused by the same validator every other producer
signs. It adds exactly two guardrails:

  * `subject` is forced into the `product:` namespace, matching the ledger's
    existing `canon:` / `finding:` / `linkedin:` convention. That is also how
    this intake finds its own rows; there is no producer column.
  * `kind` is restricted to the OBSERVATIONAL kinds. `canon` and `fix` belong
    to other producers, and a product-lead session must not be able to file a
    claim that impersonates them.

It ships at registration (`shipped=True`) because a product observation has no
separate artefact to wait for — and `verify_due_claims` only ever considers
rows with `shipped_at IS NOT NULL`, so an unshipped claim would never be judged.

Kill switch: `PLEAD_INTAKE_DISABLE=1` → `product_lead_findings()` returns [].
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from routes.brain_intake_common import (age_hours, cycle_no, rotate_window,
                                        state_get, state_set)

brain_product_lead_intake_bp = Blueprint("brain_product_lead_intake", __name__)

_STATE_KEY = "product_lead_intake_snapshot"
_ISSUE_PREFIX = "plead_"
_SUBJECT_NS = "product:"

# The ledger's own source_layer for claim rows.
_SOURCE_LAYER = "CLAIM"

# Kinds a product-lead session may file. Deliberately excludes `canon` and
# `fix`, which are owned by claim_ledger's canon producer and the squasher.
PRODUCT_KINDS = ("fact", "score", "tool_answer")

# Only a refutation is work. See the module docstring.
_SEEDABLE_OUTCOME = "refuted"
# Judged = the verifier reached a verdict of any kind (the blast-radius
# denominator). `unobserved` is NOT judged — it is the absence of a verdict.
_JUDGED_OUTCOMES = ("confirmed", "refuted", "retracted")


def _disabled() -> bool:
    return os.environ.get("PLEAD_INTAKE_DISABLE", "0") == "1"


def _int_env(name: str, default: int, lo: int = 0) -> int:
    try:
        return max(lo, int(os.environ.get(name, str(default))))
    except Exception:
        return default


def _max_rows() -> int:
    return _int_env("PLEAD_INTAKE_MAX", 2)


def _ttl_s() -> int:
    return _int_env("PLEAD_INTAKE_TTL_S", 3600, lo=600)


def _min_sample() -> int:
    return _int_env("PLEAD_INTAKE_MIN_SAMPLE", 5, lo=1)


def _max_verdict_age_h() -> float:
    try:
        return max(1.0, float(os.environ.get("PLEAD_INTAKE_MAX_AGE_H", "168")))
    except Exception:
        return 168.0


def _max_refuted_ratio() -> float:
    try:
        v = float(os.environ.get("PLEAD_INTAKE_MAX_REFUTED_RATIO", "0.75"))
        return min(1.0, max(0.1, v))
    except Exception:
        return 0.75


def _db_url() -> str | None:
    return (os.environ.get("NEON_DATABASE_URL")
            or os.environ.get("DATABASE_URL"))


# ── rule 0: may we trust this verifier's output at all? ─────────────────

def run_refusal(board: dict | None, max_age_h: float | None = None,
                max_ratio: float | None = None,
                min_sample: int | None = None) -> str | None:
    """None if these verdicts may be seeded from, else the REASON they may not.

    Pure, so both gates are testable without a database.
    """
    if not board:
        return "no product-claim board could be read"
    judged = board.get("claims") or []
    if not judged:
        # Not a refusal — an empty ledger is a correct, quiet state. The
        # caller seeds nothing either way; saying "refused" here would report
        # a problem where there is none.
        return None

    newest = board.get("newest_outcome_at")
    age = age_hours(newest)
    limit = _max_verdict_age_h() if max_age_h is None else max_age_h
    if age is None:
        return ("newest product-claim verdict has an unreadable timestamp "
                "(%r) — cannot establish that the verifier is still running"
                % (newest,))
    if age > limit:
        return ("the newest product-claim verdict is %.1fh old (limit %.1fh) "
                "— the verifier has not judged anything recently, so these "
                "standing verdicts cannot be said to describe now"
                % (age, limit))

    sample = len(judged)
    floor = _min_sample() if min_sample is None else min_sample
    if sample >= floor:
        refuted = sum(1 for c in judged
                      if (c or {}).get("outcome") == _SEEDABLE_OUTCOME)
        ratio = refuted / float(sample)
        cap = _max_refuted_ratio() if max_ratio is None else max_ratio
        if ratio > cap:
            return ("%d of %d judged product claims came back refuted "
                    "(%.0f%% > %.0f%% limit) — a broken verifier or instrument "
                    "is likelier than every product assertion being wrong at "
                    "once. There is no must-fail control on this lane, so this "
                    "cannot be told apart from a genuinely bad week; it is "
                    "refused rather than guessed"
                    % (refuted, sample, ratio * 100, cap * 100))
    return None


def blast_radius_applied(board: dict | None,
                         min_sample: int | None = None) -> bool:
    """Did the ratio gate actually run? Surfaced on the status endpoint so a
    small corpus cannot read as a check that passed."""
    floor = _min_sample() if min_sample is None else min_sample
    return len((board or {}).get("claims") or []) >= floor


# ── rule 1 + 2: pure selection ──────────────────────────────────────────

def _eligible(c: dict) -> bool:
    c = c or {}
    if c.get("outcome") != _SEEDABLE_OUTCOME:
        return False                      # confirmed/unobserved/retracted
    if c.get("superseded_by") is not None:
        return False                      # describes a regime that is gone
    return str(c.get("subject") or "").startswith(_SUBJECT_NS)


def _order_key(c: dict):
    """Freshest refutation first. There is no severity on a claim, and the
    staleness gate already bounds the window, so recency is the priority that
    remains — a refutation from an hour ago is more actionable than one from
    three weeks ago."""
    age = age_hours((c or {}).get("outcome_at"))
    return (age if age is not None else float("inf"),
            -int((c or {}).get("id") or 0))


def select_seedable(claims: list, limit: int | None = None,
                    cycle: int | None = None) -> tuple[list, int]:
    """(rows to seed, how many eligible exist). Recency-ordered, rotated, capped."""
    limit = _max_rows() if limit is None else limit
    real = [c for c in (claims or []) if _eligible(c or {})]
    real.sort(key=_order_key)
    total = len(real)
    cyc = cycle_no(_ttl_s()) if cycle is None else cycle
    return rotate_window(real, limit, cyc), total


def to_findings(rows: list, board_as_of=None) -> list:
    """Refuted product claims → the {url, issue, count, detail} shape the heal
    endpoint's actionable_backend_issues list uses.

    Prefixed `plead_` so no FIX_MAP key matches it (same reasoning as `audit_`,
    `qa_`, `siteqa_`, `asset_`, `contract_`). Identity is the claim id, which is
    immutable, so the Layer-5 learn loop's (issue, url) dedupe sees one stable
    item per refutation.
    """
    out = []
    for c in rows or []:
        cid = (c or {}).get("id")
        if cid is None:
            continue
        subject = str(c.get("subject") or "")
        regime = c.get("regime")
        if not isinstance(regime, dict):
            regime = {}
        ev = str(c.get("outcome_evidence") or "")
        out.append({
            "url": "dchub://product-lead/claim/%s" % cid,
            "issue": "%s%s" % (_ISSUE_PREFIX,
                               subject[len(_SUBJECT_NS):][:240] or str(cid)),
            "count": 1,
            "detail": (
                "REFUTED product claim #%s. The product lead asserted: %s "
                "Instrument: `%s` expected `%s`. The verifier measured "
                "otherwise and stamped `refuted` at %s. %s"
                "This is evidence, not an opinion: the claim named the check "
                "that could falsify it before it was judged, and the outcome "
                "was written by the verifier, never by the author. "
                "Regime: %s. Ledger: /api/v1/ops/claims (board as of %s)."
                % (cid, str(c.get("statement") or "")[:400],
                   c.get("expected_metric"), c.get("expected_value"),
                   c.get("outcome_at"),
                   ("Evidence: %s. " % ev[:400]) if ev else "",
                   regime.get("basis") or regime.get("as_of") or "unstated",
                   board_as_of or "unknown")),
        })
    return out


# ── the board read (off the hot path) ───────────────────────────────────

_BOARD_SQL = (
    "SELECT id, kind, subject, statement, regime, expected_metric, "
    "       expected_value, outcome, outcome_evidence, outcome_at, "
    "       superseded_by "
    "  FROM brain_predictions_log "
    " WHERE source_layer = %s AND subject LIKE %s "
    "   AND outcome = ANY(%s) "
    " ORDER BY outcome_at DESC NULLS LAST, id DESC LIMIT %s")


# ── the second source: PRODUCT GAPS the machines already measured ───────
#
# ★ 2026-09-02. The refuted-claims board above has had ONE row in its life
#   (#100959, a throwaway probe, retracted): nothing registers fact/score/
#   tool_answer claims, so this lane read an empty feed forever
#   (board_as_of=null, judged_total=0). Two instruments already measure what
#   the product lacks and nobody consumes them:
#     · agentic_query_misses — the question an agent asked that no tool
#       answered (served on /api/v1/admin/agentic/unmet-demand, GROUP BY norm)
#     · mcp_upgrade_signals  — the tool an agent asked for and hit the paywall
#   Neither is an opinion; both are counts of a caller's own request. They
#   become `product_gap:<intent|tool>` findings, through the same discipline
#   as the claims: TRUST GATE (the source read succeeded and is fresh, and a
#   gap has been asked for at least PLEAD_GAP_MIN_COUNT times — one miss is
#   noise), ELIGIBILITY, CAP + ROTATE, and a PERSISTED refusal.

_GAP_ISSUE_PREFIX = _ISSUE_PREFIX + "product_gap:"


def _gap_max_rows() -> int:
    return _int_env("PLEAD_GAP_MAX", 2)


def _gap_min_count() -> int:
    return _int_env("PLEAD_GAP_MIN_COUNT", 3, lo=1)


def _gap_max_age_h() -> float:
    try:
        return max(1.0, float(os.environ.get("PLEAD_GAP_MAX_AGE_H", "720")))
    except Exception:
        return 720.0


def gap_rows(unmet: list | None, pressure: list | None) -> list:
    """Normalise the two sources into one row shape. Pure.
    unmet:    [{norm, count, last, surfaces, samples}]  (30d, by intent)
    pressure: [{tool, count, distinct, last}]           (7d, by tool)
    -> [{key, kind, count, distinct, last, samples}]"""
    out = []
    for r in unmet or []:
        norm = str((r or {}).get("norm") or "").strip()
        if not norm:
            continue
        out.append({"key": norm[:120], "kind": "intent",
                    "count": int(r.get("count") or 0),
                    "distinct": None, "last": r.get("last"),
                    "samples": list(r.get("samples") or [])[:3]})
    for r in pressure or []:
        tool = str((r or {}).get("tool") or "").strip()
        if not tool:
            continue
        out.append({"key": tool[:120], "kind": "tool",
                    "count": int(r.get("count") or 0),
                    "distinct": r.get("distinct"), "last": r.get("last"),
                    "samples": []})
    return out


def gap_refusal(source: dict | None, max_age_h: float | None = None) -> str | None:
    """Trust gate for the gap source. None = may seed; else the reason."""
    if not source:
        return "no product-gap source could be read"
    if source.get("error"):
        return "product-gap source read failed: %s" % str(source["error"])[:120]
    newest = source.get("newest_at")
    rows = source.get("rows") or []
    if not rows:
        return None                      # an empty source is a quiet state
    age = age_hours(newest)
    limit = _gap_max_age_h() if max_age_h is None else max_age_h
    if age is None:
        return ("newest product-gap observation has an unreadable timestamp "
                "(%r)" % (newest,))
    if age > limit:
        return ("the newest product-gap observation is %.1fh old (limit %.1fh) "
                "— the instruments have gone quiet, these gaps may not describe "
                "now" % (age, limit))
    return None


def _gap_eligible(r: dict, min_count: int) -> bool:
    return bool((r or {}).get("key")) and int((r or {}).get("count") or 0) >= min_count


def select_seedable_gaps(rows: list, limit: int | None = None,
                         cycle: int | None = None,
                         min_count: int | None = None) -> tuple[list, int]:
    """(rows to seed, how many eligible). Most-asked first, rotated, capped."""
    limit = _gap_max_rows() if limit is None else limit
    floor = _gap_min_count() if min_count is None else min_count
    real = [r for r in (rows or []) if _gap_eligible(r or {}, floor)]
    real.sort(key=lambda r: (-int(r.get("count") or 0), str(r.get("key"))))
    total = len(real)
    cyc = cycle_no(_ttl_s()) if cycle is None else cycle
    return rotate_window(real, limit, cyc), total


def to_gap_findings(rows: list, source_as_of=None) -> list:
    """Gap rows -> the heal shape. `count` is the number of times callers asked
    for it (a magnitude, not a recurrence tally — count_kind says so)."""
    out = []
    for r in rows or []:
        key = str((r or {}).get("key") or "")
        if not key:
            continue
        kind = r.get("kind") or "?"
        out.append({
            "url": "dchub://product-lead/gap/%s/%s" % (kind, key),
            "issue": "%s%s" % (_GAP_ISSUE_PREFIX, key[:200]),
            "count": int(r.get("count") or 0),
            "count_kind": "item_count",
            "detail": (
                "PRODUCT GAP (%s): callers asked for `%s` %d time(s)%s and the "
                "product had no answer%s. Source: %s — a count of the callers' "
                "own requests, not an opinion. %sInstrument as of %s. "
                "Board: /api/v1/admin/agentic/unmet-demand"
                % (kind, key, int(r.get("count") or 0),
                   (" (%s distinct callers)" % r.get("distinct"))
                   if r.get("distinct") is not None else "",
                   " (paywall hit — the tool exists but is gated)"
                   if kind == "tool" else "",
                   "mcp_upgrade_signals 7d" if kind == "tool"
                   else "agentic_query_misses 30d",
                   ("Samples: %s. " % "; ".join(
                       str(x)[:80] for x in (r.get("samples") or [])[:3]))
                   if r.get("samples") else "",
                   source_as_of or "unknown")),
        })
    return out


def _load_gap_source() -> dict:
    """Both instruments, one dict: {rows, newest_at, error}. Fail-soft."""
    src = {"rows": [], "newest_at": None, "error": None}
    url = _db_url()
    if not url:
        src["error"] = "no database"
        return src
    try:
        import psycopg2
        with psycopg2.connect(url, connect_timeout=5) as conn, \
                conn.cursor() as cur:
            cur.execute("""
                SELECT norm, count(*), max(created_at)::text,
                       (array_agg(query ORDER BY created_at DESC))[1:3]
                  FROM agentic_query_misses
                 WHERE created_at > NOW() - INTERVAL '30 days'
                 GROUP BY norm ORDER BY count(*) DESC LIMIT 40""")
            unmet = [{"norm": r[0], "count": int(r[1]), "last": r[2],
                      "samples": list(r[3] or [])} for r in cur.fetchall() or []]
            pressure = []
            try:
                cur.execute("""
                    SELECT tool_requested, count(*),
                           count(DISTINCT session_id), max(created_at)::text
                      FROM mcp_upgrade_signals
                     WHERE created_at > NOW() - INTERVAL '7 days'
                       AND tool_requested IS NOT NULL
                     GROUP BY tool_requested ORDER BY count(*) DESC LIMIT 40""")
                pressure = [{"tool": r[0], "count": int(r[1]),
                             "distinct": int(r[2] or 0), "last": r[3]}
                            for r in cur.fetchall() or []]
            except Exception:  # noqa: BLE001
                conn.rollback()  # the table may not exist on this DB
            rows = gap_rows(unmet, pressure)
            src["rows"] = rows
            lasts = [r["last"] for r in rows if r.get("last")]
            src["newest_at"] = max(lasts) if lasts else None
    except Exception as e:  # noqa: BLE001
        src["error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    return src


def _load_board() -> dict:
    """Judged product claims + the newest verdict time. Fail-soft; never raises.

    Reads only rows the verifier has STAMPED — an open claim is an unverified
    opinion and has no business in this query at all.
    """
    board = {"claims": [], "newest_outcome_at": None}
    url = _db_url()
    if not url:
        return board
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        with psycopg2.connect(url, connect_timeout=5) as conn, \
                conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(_BOARD_SQL, (_SOURCE_LAYER, _SUBJECT_NS + "%",
                                     list(_JUDGED_OUTCOMES), 200))
            rows = []
            for r in (cur.fetchall() or []):
                d = dict(r)
                oa = d.get("outcome_at")
                d["outcome_at"] = oa.isoformat() if oa else None
                rows.append(d)
            board["claims"] = rows
            board["newest_outcome_at"] = next(
                (r["outcome_at"] for r in rows if r.get("outcome_at")), None)
    except Exception:
        return board
    return board


def _gap_snapshot(gap_fn=None) -> dict:
    """The gap half of one refresh: read, trust-gate, select. Pure given
    gap_fn; never raises."""
    try:
        source = (gap_fn or _load_gap_source)() or {}
    except Exception as e:  # noqa: BLE001
        source = {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}
    refusal = gap_refusal(source)
    if refusal:
        return {"gap_rows": [], "gap_eligible_total": 0,
                "gap_refused": refusal, "gap_as_of": source.get("newest_at")}
    rows, eligible = select_seedable_gaps(source.get("rows") or [])
    return {"gap_rows": rows, "gap_eligible_total": eligible,
            "gap_refused": None, "gap_as_of": source.get("newest_at")}


def refresh_snapshot(force: bool = False, load_fn=None, gap_fn=None) -> dict:
    """Read the judged product claims and persist the seedable slice."""
    if _disabled():
        return {"ok": True, "skipped": "PLEAD_INTAKE_DISABLE=1"}
    prev = state_get(_STATE_KEY) or {}
    age = time.time() - float(prev.get("ts") or 0)
    if not force and prev and age < _ttl_s():
        return {"ok": True, "skipped": "fresh", "age_s": int(age),
                "rows": len(prev.get("rows") or [])}
    try:
        board = (load_fn or _load_board)() or {}
        judged = board.get("claims") or []
        # The gap source is independent of the claims board: a refused board
        # must not silence the machines, and a broken gap read must not
        # silence the verifier. Each carries its own persisted refusal.
        gaps = _gap_snapshot(gap_fn)
        base = {"ts": time.time(),
                "as_of": datetime.now(timezone.utc).isoformat(),
                "board_as_of": board.get("newest_outcome_at"),
                "judged_total": len(judged),
                "blast_radius_applied": blast_radius_applied(board),
                "min_sample": _min_sample(),
                "cycle": cycle_no(_ttl_s()),
                **gaps}
        refusal = run_refusal(board)
        if refusal:
            # ★ Persist the refusal, or the last trusted snapshot keeps
            #   serving findings from verdicts we have since distrusted.
            snap = dict(base, refused=refusal, eligible_total=0, rows=[])
            state_set(_STATE_KEY, snap)
            return {"ok": True, "refreshed": True, "refused": refusal,
                    "rows": 0, "gap_rows": len(gaps["gap_rows"]),
                    "gap_refused": gaps["gap_refused"]}
        rows, eligible = select_seedable(judged)
        snap = dict(base, refused=None, eligible_total=eligible, rows=rows)
        state_set(_STATE_KEY, snap)
        return {"ok": True, "refreshed": True, "rows": len(rows),
                "eligible_total": eligible,
                "deferred_to_next_cycle": max(0, eligible - len(rows)),
                "judged_total": len(judged),
                "blast_radius_applied": base["blast_radius_applied"],
                "gap_rows": len(gaps["gap_rows"]),
                "gap_eligible_total": gaps["gap_eligible_total"],
                "gap_deferred_to_next_cycle": max(
                    0, (gaps["gap_eligible_total"] or 0) - len(gaps["gap_rows"])),
                "gap_refused": gaps["gap_refused"]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:160]}


def product_lead_findings() -> list:
    """The heal endpoint's read: cached snapshot only, never a live board read."""
    if _disabled():
        return []
    try:
        snap = state_get(_STATE_KEY) or {}
        return (to_findings(snap.get("rows") or [], snap.get("board_as_of"))
                + to_gap_findings(snap.get("gap_rows") or [],
                                  snap.get("gap_as_of")))
    except Exception:
        return []


# ── the write side: the producer path ───────────────────────────────────

def normalize_subject(raw: str) -> str:
    """Force the `product:` namespace. Idempotent, so a caller that already
    namespaced its subject is not double-prefixed."""
    s = (raw or "").strip()
    if s.startswith(_SUBJECT_NS):
        return s
    return _SUBJECT_NS + s.lstrip(":")


def file_claim(payload: dict) -> dict:
    """Register one product-lead claim. Thin wrapper over register_claim — the
    contract itself is NOT re-implemented here, so a claim with no expectation
    is refused by the same validator every other producer signs."""
    p = payload if isinstance(payload, dict) else {}
    kind = str(p.get("kind") or "fact").strip()
    if kind not in PRODUCT_KINDS:
        return {"ok": False, "refused": True,
                "error": ("refused: kind must be one of %s for a product-lead "
                          "claim (canon and fix are owned by other producers)"
                          % (PRODUCT_KINDS,))}
    subject = normalize_subject(str(p.get("subject") or ""))
    if subject == _SUBJECT_NS:
        return {"ok": False, "refused": True,
                "error": "refused: subject required"}
    regime = p.get("regime")
    if regime is not None and not isinstance(regime, dict):
        return {"ok": False, "refused": True,
                "error": "refused: regime must be an object"}
    try:
        from routes.claim_ledger import register_claim
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "claim ledger unavailable: %s"
                % str(e)[:120]}
    return register_claim(
        kind=kind,
        subject=subject,
        statement=str(p.get("statement") or ""),
        expected_metric=str(p.get("expected_metric") or ""),
        expected_value=str(p.get("expected_value") or ""),
        horizon_hours=p.get("horizon_hours") or 24,
        regime=regime,
        surfaces=p.get("surfaces"),
        # A product observation has no separate artefact to wait for, and
        # verify_due_claims only considers rows with shipped_at set — an
        # unshipped claim would never be judged at all.
        shipped=True,
    )


# ── endpoints (admin) ───────────────────────────────────────────────────

def _admin_ok_local() -> bool:
    try:
        from routes.brain_mechanical_classifier import _admin_ok
        return bool(_admin_ok())
    except Exception:
        key = os.environ.get("DCHUB_ADMIN_KEY", "")
        sent = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
        return bool(key) and sent == key


@brain_product_lead_intake_bp.post("/api/v1/brain/product-lead/claim")
def product_lead_file_claim():
    if not _admin_ok_local():
        return jsonify(ok=False, error="admin key required"), 401
    result = file_claim(request.get_json(silent=True) or {})
    resp = jsonify(result)
    resp.headers["Cache-Control"] = "no-store"
    return resp, (400 if result.get("refused") else
                  (200 if result.get("ok") else 500))


@brain_product_lead_intake_bp.get("/api/v1/brain/product-lead-intake")
def product_lead_intake_status():
    if not _admin_ok_local():
        return jsonify(ok=False, error="admin key required"), 401
    snap = state_get(_STATE_KEY) or {}
    seeded = to_findings(snap.get("rows") or [], snap.get("board_as_of"))
    eligible = snap.get("eligible_total")
    out = {"ok": True, "enabled": not _disabled(),
           "max_rows": _max_rows(), "ttl_s": _ttl_s(),
           "max_verdict_age_h": _max_verdict_age_h(),
           "max_refuted_ratio": _max_refuted_ratio(),
           "min_sample": snap.get("min_sample"),
           # ★ A sample-gated check that did not run must not read as a check
           #   that passed.
           "blast_radius_applied": snap.get("blast_radius_applied"),
           "has_must_fail_control": False,
           "outcome_writer_is_not_the_author": True,
           "seedable_outcome": _SEEDABLE_OUTCOME,
           "producer_kinds": list(PRODUCT_KINDS),
           "snapshot_as_of": snap.get("as_of"),
           "board_as_of": snap.get("board_as_of"),
           "judged_total": snap.get("judged_total"),
           "refused": snap.get("refused"),
           "eligible_total": eligible,
           "cycle": snap.get("cycle"),
           "deferred_to_next_cycle": (max(0, eligible - len(seeded))
                                      if isinstance(eligible, int) else None),
           "seeded": seeded,
           # ★2026-09-02 the machine source (finding 8): counts of callers'
           #   own requests, gated/capped/rotated like the claims lane.
           "gaps": {"sources": ["agentic_query_misses 30d (intent)",
                                "mcp_upgrade_signals 7d (tool)"],
                    "max_rows": _gap_max_rows(),
                    "min_count": _gap_min_count(),
                    "max_age_h": _gap_max_age_h(),
                    "as_of": snap.get("gap_as_of"),
                    "refused": snap.get("gap_refused"),
                    "eligible_total": snap.get("gap_eligible_total"),
                    "seeded": to_gap_findings(snap.get("gap_rows") or [],
                                              snap.get("gap_as_of"))}}
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@brain_product_lead_intake_bp.post("/api/v1/brain/product-lead-intake/refresh")
def product_lead_intake_refresh():
    if not _admin_ok_local():
        return jsonify(ok=False, error="admin key required"), 401
    result = refresh_snapshot(force=request.args.get("force") == "1")
    resp = jsonify(result)
    resp.headers["Cache-Control"] = "no-store"
    return resp
