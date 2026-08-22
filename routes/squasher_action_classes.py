"""routes/squasher_action_classes.py — ACTION CLASSES for the squasher inbox
(claim loop, step 2; 2026-08-22).

WHY
===
The squasher's awaiting_ops exit (2026-08-20) stopped the lane throwing its
analyses away, and replaced one dead end with a better-labelled one: 19 rows
sit in awaiting_ops, 12 of them ONE root problem (facility-dedup not applied)
fanned out per country, each naming the identical remedy —
    POST /api/v1/admin/facility-dedup/apply?country=XX&confirm=1
— and waiting for a human to paste it into curl. The detector re-raises the
finding every 6h, the investigator re-bills ~80s of model time to reach the
same conclusion, and nothing moves. resolve_post()'s docstring is right that a
model at ~0.35 confidence must not be handed the write path; it is also true
that a remedy which is (a) a fixed endpoint, (b) reversible, (c) verifiable by
a read the detector itself already makes, is not a judgement call any more once
a human has read it ONCE.

So: a finding's named endpoint is mapped to an ACTION CLASS. A class is
granted ONCE, by a human, for all rows that name it; the drain then executes
rows of a granted class, one per drain, and VERIFIES each run against the
class's own read endpoint before it may call the row resolved. The grant is
the human decision; the verifier is what keeps it honest.

THE CONTRACT (every clause is mutation-tested in tests/test_squasher_action_classes.py)
  * The class defines the endpoint. A queue row contributes exactly ONE
    whitelisted parameter (country, ^[A-Z]{2}$). The URL that runs is rebuilt
    from the registry below — never the model's text — so prose cannot smuggle
    a different path, a different verb or an extra argument into production.
  * GRANT TEST: a class may be granted only when it is reversible AND has a
    verifier_url AND has bound_params. The drain re-checks the same test at
    run time, so a grant edited straight into the table is still refused.
  * VERIFY OR FAIL: a run is ok only if the verifier's count DROPPED versus the
    pre-run read. An executed-but-unverified run is a FAILURE: runs_failed++,
    consecutive_failed++, the row stays awaiting_ops carrying the reason.
    Three consecutive failures trip the class breaker; only a human clears it.
  * BOUNDED: ACTION_CLASS_MAX_PER_DRAIN (default 1) and ACTION_CLASS_MAX_PER_DAY
    (default 6, the run ledger IS the budget), and the mutation never starts
    once the step has spent its wall budget — verification must be affordable.
  * DARK BY DEFAULT: ACTION_CLASSES_ENABLED must be exactly "1"; a missing or
    any other value is OFF. Per-class granted=false and the breaker are the
    other two kill switches. GET endpoints never act; only the drain acts.
  * Every execution writes a brain_action_class_runs row (append-only) with
    the pre/post counts, and registers a Step-1 claim (routes/claim_ledger,
    imported lazily — it may not be deployed yet) whose outcome the ledger's
    own verifier stamps. This module never stamps a claim outcome itself.

Surface (admin; under /api/v1/brain/ for the CF bypass rule):
  GET  /api/v1/brain/squasher/classes[?dry_run=1]   registry + switch + breaker
                                                    state; dry_run=1 reports
                                                    what a drain WOULD run
  POST /api/v1/brain/squasher/classify              backfill action_class on
                                                    open rows (idempotent)
  POST /api/v1/brain/squasher/grant                 {class, granted, by?,
                                                     clear_breaker?}
  POST /api/v1/brain/squasher/drain?dry_run=1       (squasher_queue) same plan
Kill: ACTION_CLASSES_ENABLED unset/≠1 (global) · granted=false (per class) ·
      breaker_tripped (per class, after 3 consecutive failures) ·
      SQUASHER_QUEUE_DISABLE=1 (the whole squasher; endpoints answer 404)

AGENTIC LOOP #65, PART B (2026-08-22) — candidates in DATA, a track record,
and a graduation PROPOSAL that never grants
  * CANDIDATES LIVE IN DATA. Every class below is seeded granted=FALSE with
    its candidate_reason and track_record_required; an operator grants it
    through POST /grant — never a prompt, never this module. Two classes wrap
    the autonomy shell's DATA actuators (routes/brain_autonomy_master_shell
    .ACTUATORS, IMPORTED — never restated): the actuator's trigger is the
    class's verifier (GET /verifier/<class>, the count as a top-level int),
    its fire is the action (POST /actuate/<class>?confirm=1, which honours the
    shell's own budget ledger brain_actuator_runs and stores the rollback on
    it), and POST /rollback-run applies that rollback. Without ?confirm=1,
    /actuate is its own dry run — exactly as facility-dedup's /apply is.
  * THE TRACK RECORD. A real drain PROBES each ungranted, untripped class (at
    most every 6h, 2 per drain): verifier read → the action endpoint WITHOUT
    its bound params (by registry contract, its dry run) → verifier read
    again. The probe is a dry_run=TRUE ledger row; it is CLEAN when the
    verifier was readable, the endpoint answered 2xx and the metric did not
    move. A dry run that moved the metric trips the breaker: an endpoint that
    mutates without its confirm is the one thing a probe must never find
    twice. Probes never count against the day budget (executed=FALSE).
  * GRADUATION PROPOSES, NEVER GRANTS. graduation_report() computes, per
    class: dry-run reads and clean dry runs (7d), runs ok/failed (7d), the
    breaker, and eligible_for_grant — the CODE rule: reversible AND verifier
    AND bound_params AND ≥N clean dry runs AND 0 consecutive failures AND not
    already granted AND breaker clear. With file=True it files ONE
    awaiting_decision inbox row per eligible class ("Grant action class X?"
    carrying the evidence and the one-click /grant payload); the open-row
    identity is the class (finding_key action-class-grant:<class>), so a
    re-run refreshes the row instead of duplicating it. Nothing here writes
    granted=TRUE except grant_post — the human's endpoint.
  Surface added (admin; same /api/v1/brain/ bypass; kill switch → 404):
    GET  /api/v1/brain/squasher/classes            now carries `graduation`
    POST /api/v1/brain/squasher/graduation         graduation_report(file=True)
    GET  /api/v1/brain/squasher/verifier/<class>   the actuator trigger, as an int
    POST /api/v1/brain/squasher/actuate/<class>    dry run; ?confirm=1 fires
    POST /api/v1/brain/squasher/rollback-run       {actuator_run_id}
  Plain functions for the #65 shell (JSON-safe dicts, never raise):
    graduation_report(file=False)  ·  routes.squasher_queue.queue_ages()
    routes.squasher_queue.resolve_class(cls, decision, note, by)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

squasher_action_classes_bp = Blueprint("squasher_action_classes", __name__)

# ── the registry ─────────────────────────────────────────────────────────
#
# One entry per class. `path` + `method` is what a finding must name to be
# classified; everything else is what the drain needs to run and verify it.
# Adding a class here is a code review; granting it is an operator decision
# made through POST /grant (the seed row is granted=false).
# The track record a candidate must show before graduation PROPOSES it:
# clean dry runs (probe rows, see probe_one) in the last 7 days, and the
# consecutive-failure ceiling. Per class in the registry and on the row
# (track_record_required JSONB); this is the default.
_TRACK_RECORD_DEFAULT = {"clean_dry_runs": 3, "max_consecutive_failed": 0}

# Every class's action endpoint MUST be its own dry run when called without
# its bound params — /apply without confirm=1 plans and returns would_mark,
# /actuate/<class> without confirm=1 reads the trigger and fires nothing.
# Probes rely on it (build_dry_run_url) and a probe that sees the metric
# move trips the class breaker.
ACTION_CLASSES = {
    "facility_dedup_apply": {
        "path": "/api/v1/admin/facility-dedup/apply",
        "match_paths": (),
        "method": "POST",
        # GET analyze is the detector's own read: `duplicate_rows` counts the
        # proposed duplicates among rows with duplicate_of_id IS NULL, so a
        # successful apply makes it DROP (facility_dedup._load filters them).
        "verifier_url": "/api/v1/admin/facility-dedup/analyze",
        "metric": "duplicate_rows",
        "bound_params": {"confirm": "1"},
        "row_param": "country",
        "row_param_re": r"^[A-Z]{2}$",
        "reversible": True,
        "undo": "POST /api/v1/admin/facility-dedup/undo?country=<XX>",
        "actuator": None,
        "candidate_reason": (
            "Per-country finding the radar re-files every 6h (12 inbox rows for "
            "7 countries on 2026-08-22): the apply is a visibility flag with a "
            "one-call undo, and the detector's own analyze read is the "
            "verifier. Granted 2026-08-22 after a human read it once."),
        "track_record_required": _TRACK_RECORD_DEFAULT,
        "notes": ("Marks facilities.duplicate_of_id (a VISIBILITY flag, never "
                  "a delete) for the clusters GET analyze proposes. Reversible "
                  "via /undo?country=XX. Verifier: analyze duplicate_rows must "
                  "drop."),
    },
    # ── #65 part B: the autonomy shell's DATA actuators, wrapped ──────────
    # Each has, on origin/main, a trigger (the defect count), a fire (the
    # bounded repair) and a rollback record; what it lacked was a per-actuator
    # HTTP surface the class machinery could verify against. The wrapper
    # endpoints below supply exactly that and nothing more: they import the
    # shell's ACTUATORS and budget, they do not restate them.
    "news_entity_reresolve": {
        "path": "/api/v1/brain/squasher/actuate/news_entity_reresolve",
        # What a finding may NAME (exists on origin/main: news_entity_extraction
        # .ner_reresolve). The registry still decides what RUNS — the wrapper,
        # which adds the budget ledger, the pre-image and the rollback that the
        # bare endpoint lacks.
        "match_paths": ("/api/v1/admin/news-ner/re-resolve",),
        "method": "POST",
        "verifier_url": "/api/v1/brain/squasher/verifier/news_entity_reresolve",
        "metric": "blindspot",
        "bound_params": {"confirm": "1"},
        "row_param": None,          # class-scoped: the class IS the work item
        "row_param_re": None,
        "reversible": True,
        "undo": ("POST /api/v1/brain/squasher/rollback-run {actuator_run_id} — "
                 "the pre-image (id, prior status) of every row the run flipped "
                 "is stored on brain_actuator_runs.rollback"),
        "actuator": "news_entity_reresolve",
        "candidate_reason": (
            "Autonomy-shell actuator (graph-spine lane 5a): flips "
            "news_discovered_entities.in_facilities FALSE→TRUE for entities that "
            "already prefix-match a facility, with the scan's own matcher. "
            "Trigger = the blind-spot count; a verified run makes it DROP. The "
            "wrapper captures the pre-image the bare endpoint does not."),
        "track_record_required": _TRACK_RECORD_DEFAULT,
        "notes": ("Wraps brain_autonomy_master_shell ACTUATORS['news_entity_"
                  "reresolve']: FALSE→TRUE only, derived from the facilities "
                  "tables. Budget: the shell's own 1/actuator/day, 3/day global "
                  "(brain_actuator_runs). Reversible via /rollback-run. "
                  "Verifier: blindspot must drop."),
    },
    "deals_exact_dupe_quarantine": {
        "path": "/api/v1/brain/squasher/actuate/deals_exact_dupe_quarantine",
        "match_paths": (),          # no endpoint on origin/main names this repair
        "method": "POST",
        "verifier_url": "/api/v1/brain/squasher/verifier/deals_exact_dupe_quarantine",
        "metric": "excess",
        "bound_params": {"confirm": "1"},
        "row_param": None,
        "row_param_re": None,
        "reversible": True,
        "undo": ("POST /api/v1/brain/squasher/rollback-run {actuator_run_id} — "
                 "id→prior data_flag, collected BEFORE the UPDATE by the fire "
                 "itself and stored on brain_actuator_runs.rollback (the same "
                 "shape repair_deals_exact_dupes.py --rollback reads)"),
        "actuator": "deals_exact_dupe_quarantine",
        "candidate_reason": (
            "Autonomy-shell actuator (graph-spine lane 3a): served deal rows "
            "byte-identical on all 19 business columns get data_flag="
            "'quarantine_duplicate' (keeper = min(id); NEVER a delete). Trigger "
            "= the excess count; a verified run makes it DROP; rollback is on "
            "the run row."),
        "track_record_required": _TRACK_RECORD_DEFAULT,
        "notes": ("Wraps brain_autonomy_master_shell ACTUATORS['deals_exact_"
                  "dupe_quarantine']: data_flag flip with a stored id→prior "
                  "rollback, capped 200 victims per fire. Budget: the shell's own "
                  "ledger. Reversible via /rollback-run. Verifier: excess must "
                  "drop."),
    },
}
# Classification: the path a finding names → the class. A class's own path
# and every match_paths alias map to it; the URL that RUNS is always rebuilt
# from the registry's `path` (build_action_url), never from the text.
_PATH_TO_CLASS = {}
for _name, _spec in ACTION_CLASSES.items():
    _PATH_TO_CLASS[_spec["path"]] = _name
    for _alias in (_spec.get("match_paths") or ()):
        _PATH_TO_CLASS[_alias] = _name
del _name, _spec

# Same shape as squasher_queue._ACTION_RE, with the query split out so the
# row parameter can be validated on its own. A path with no verb is prose.
_ACTION_RE = re.compile(
    r"\b(POST|PUT|PATCH|DELETE|GET)\s+(/api/[A-Za-z0-9/_.\-]*)"
    r"(\?[A-Za-z0-9=&_%.\-]*)?")

_BREAKER_THRESHOLD = 3       # consecutive verified failures that trip a class
_WALL_BUDGET_S = 20          # seconds the step may spend BEFORE it mutates —
                             # inside cron_heartbeat._hit's 30s read timeout,
                             # with headroom for the apply + the verify read
_RETRY_ROW_AFTER_HOURS = 24  # one attempt per queue row per day, whatever
                             # the outcome — the ledger enforces it


# ── switches and caps ────────────────────────────────────────────────────

def _disabled() -> bool:
    """The whole squasher's kill switch — this surface is part of it."""
    return os.environ.get("SQUASHER_QUEUE_DISABLE", "0") == "1"


def enabled() -> bool:
    """Global switch. Exactly "1" is on; missing, "0", "true", "yes" are OFF.
    Shipped dark: the orchestrator flips it after verifying the grant row, the
    classify backfill and a dry drain in production."""
    return os.environ.get("ACTION_CLASSES_ENABLED") == "1"


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def max_per_drain() -> int:
    return _int_env("ACTION_CLASS_MAX_PER_DRAIN", 1, 0, 3)


def max_per_day() -> int:
    return _int_env("ACTION_CLASS_MAX_PER_DAY", 6, 0, 24)


# ── the class-extraction rule ────────────────────────────────────────────

def _query_params(query: str) -> dict:
    out = {}
    for part in (query or "").lstrip("?").split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v.rstrip(".,;:)")
    return out


def build_action_url(cls: str, row_params: dict) -> str:
    """The URL that RUNS: registry path + the row's parameter + the class's
    bound params, in that order. Values were validated by classify_text."""
    spec = ACTION_CLASSES[cls]
    items = list(row_params.items()) + list(spec["bound_params"].items())
    return spec["path"] + "?" + "&".join(f"{k}={v}" for k, v in items)


def build_verifier_url(cls: str, row_params: dict) -> str:
    spec = ACTION_CLASSES[cls]
    q = "&".join(f"{k}={v}" for k, v in row_params.items())
    return spec["verifier_url"] + ("?" + q if q else "")


def build_dry_run_url(cls: str, row_params: dict) -> str:
    """The action endpoint WITHOUT its bound params — by registry contract
    that is its own dry run (/apply without confirm=1 plans and returns
    would_mark; /actuate/<class> without confirm=1 reads the trigger and
    fires nothing). Probes call this and only this; never build_action_url."""
    spec = ACTION_CLASSES[cls]
    q = "&".join(f"{k}={v}" for k, v in row_params.items())
    return spec["path"] + ("?" + q if q else "")


def classify_text(*texts) -> dict | None:
    """Derive (action_class, action_url, action_method, params) from the
    endpoint a finding's analysis names.

    Scans every `VERB /api/...` mention in each text, in the order given
    (callers pass reason, decision, analysis, title, finding_key), and returns
    the FIRST that (1) is a registry path, (2) carries the class's verb, and
    (3) carries a valid row parameter. Two live rows (SG, AU on 2026-08-22)
    lead with `GET .../analyze` and only name the apply later — so the rule
    must read past the first match rather than stop at it. Anything else is
    None: unknown endpoints are not invented into classes.
    """
    for text in texts:
        if not text:
            continue
        for m in _ACTION_RE.finditer(str(text)):
            verb, path, query = m.group(1).upper(), m.group(2), m.group(3) or ""
            cls = _PATH_TO_CLASS.get(path.rstrip("/"))
            if not cls:
                continue
            spec = ACTION_CLASSES[cls]
            if verb != spec["method"]:
                continue
            if spec["row_param"] is None:
                # Class-scoped (#65 B): the class is the work item; the text
                # contributes nothing but the path and the verb.
                params = {}
            else:
                val = _query_params(query).get(spec["row_param"])
                if not val or not re.match(spec["row_param_re"], val):
                    continue
                params = {spec["row_param"]: val}
            return {"action_class": cls,
                    "action_method": spec["method"],
                    "action_url": build_action_url(cls, params),
                    "params": params}
    return None


def row_params_of(row: dict) -> dict | None:
    """Re-derive the row parameter from the STORED action_url, validated
    again — a row edited by hand gets the same scrutiny as one classified
    here."""
    cls = row.get("action_class")
    spec = ACTION_CLASSES.get(cls or "")
    if not spec:
        return None
    if spec["row_param"] is None:
        return {}          # class-scoped: valid, and nothing to validate
    q = (row.get("action_url") or "").partition("?")[2]
    val = _query_params(q).get(spec["row_param"])
    if not val or not re.match(spec["row_param_re"], val):
        return None
    return {spec["row_param"]: val}


# ── the grant test ───────────────────────────────────────────────────────

def _params_dict(v) -> dict | None:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            d = json.loads(v)
            return d if isinstance(d, dict) else None
        except Exception:  # noqa: BLE001
            return None
    return None


def grant_allowed(row: dict | None) -> tuple[bool, str]:
    """A grant is refused unless reversible AND verifier_url AND bound_params
    are ALL present. Pure; used by the endpoint and re-run by the drain."""
    if not row:
        return False, "refused: no registry row for that class"
    if row.get("class") not in ACTION_CLASSES:
        return False, "refused: class is not in the code registry"
    if not row.get("reversible"):
        return False, "refused: class is not marked reversible"
    if not str(row.get("verifier_url") or "").strip():
        return False, "refused: class has no verifier_url"
    bp = _params_dict(row.get("bound_params"))
    if not bp:
        return False, "refused: class has no bound_params"
    return True, "ok"


def eligible(cls_row: dict | None) -> tuple[bool, str]:
    """May the DRAIN run this class right now? The grant test again (a row
    edited straight into the table gets no free pass), then the two per-class
    kill switches. The global switch is checked by the caller."""
    ok, why = grant_allowed(cls_row)
    if not ok:
        return False, why
    if not cls_row.get("granted"):
        return False, "not granted"
    if cls_row.get("breaker_tripped"):
        return False, "breaker tripped"
    return True, "ok"


# ── db ───────────────────────────────────────────────────────────────────

def _db_url():
    return (os.environ.get("NEON_DATABASE_URL")
            or os.environ.get("DATABASE_URL"))


def _conn():
    url = _db_url()
    if not url:
        raise RuntimeError("no database url")
    import psycopg2
    return psycopg2.connect(url, connect_timeout=5)


_CLASS_COLS = ("class", "granted", "granted_by", "granted_at", "bound_params",
               "verifier_url", "reversible", "runs_ok", "runs_failed",
               "consecutive_failed", "last_run_at", "breaker_tripped", "notes",
               "candidate_reason", "track_record_required")


def ensure_tables(cur) -> None:
    """Idempotent. Seeds one registry row per class with granted=FALSE —
    ON CONFLICT DO NOTHING, so an operator's grant is never overwritten by
    a redeploy."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS brain_action_classes (
            class              TEXT PRIMARY KEY,
            granted            BOOLEAN NOT NULL DEFAULT FALSE,
            granted_by         TEXT,
            granted_at         TIMESTAMPTZ,
            bound_params       JSONB,
            verifier_url       TEXT,
            reversible         BOOLEAN NOT NULL DEFAULT FALSE,
            runs_ok            INTEGER NOT NULL DEFAULT 0,
            runs_failed        INTEGER NOT NULL DEFAULT 0,
            consecutive_failed INTEGER NOT NULL DEFAULT 0,
            last_run_at        TIMESTAMPTZ,
            breaker_tripped    BOOLEAN NOT NULL DEFAULT FALSE,
            notes              TEXT,
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
    # Append-only run ledger: one row per execution, and the budget counter.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS brain_action_class_runs (
            id            BIGSERIAL PRIMARY KEY,
            class         TEXT NOT NULL,
            queue_id      BIGINT,
            params        JSONB,
            action_url    TEXT,
            verifier_url  TEXT,
            pre_count     INTEGER,
            post_count    INTEGER,
            executed      BOOLEAN NOT NULL DEFAULT FALSE,
            verified      BOOLEAN NOT NULL DEFAULT FALSE,
            outcome       TEXT,
            http_status   INTEGER,
            marked        INTEGER,
            claim_id      BIGINT,
            error         TEXT,
            dry_run       BOOLEAN NOT NULL DEFAULT FALSE,
            started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at   TIMESTAMPTZ,
            elapsed_ms    INTEGER
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS brain_action_class_runs_started_idx "
                "ON brain_action_class_runs (started_at DESC)")
    # The queue's own ensure path adds these too; IF EXISTS keeps this safe
    # on a database where the queue table has not been created yet.
    for col in ("action_class", "action_url", "action_method"):
        cur.execute(f"ALTER TABLE IF EXISTS squasher_work_queue "
                    f"ADD COLUMN IF NOT EXISTS {col} TEXT")
    # #65 part B: the candidate registry lives in DATA — why a class is a
    # candidate and what record it must show; and the run ledger carries the
    # actuator run it fired (where the rollback lives) for the wrapped classes.
    for col, typ in (("candidate_reason", "TEXT"),
                     ("track_record_required", "JSONB")):
        cur.execute(f"ALTER TABLE brain_action_classes "
                    f"ADD COLUMN IF NOT EXISTS {col} {typ}")
    cur.execute("ALTER TABLE brain_action_class_runs "
                "ADD COLUMN IF NOT EXISTS actuator_run_id BIGINT")
    for name, spec in ACTION_CLASSES.items():
        req = json.dumps(track_record_required_of(None, spec), sort_keys=True)
        cur.execute(
            "INSERT INTO brain_action_classes (class, granted, reversible, verifier_url, bound_params, notes, candidate_reason, track_record_required) VALUES (%s, FALSE, %s, %s, %s::jsonb, %s, %s, %s::jsonb) ON CONFLICT (class) DO NOTHING",  # noqa: E501
            (name, bool(spec["reversible"]), spec["verifier_url"],
             json.dumps(spec["bound_params"]), spec["notes"],
             spec.get("candidate_reason"), req))


def backfill_candidate_columns(cur) -> int:
    """A row seeded BEFORE #65 (facility_dedup_apply, 2026-08-22 05:25Z)
    carries NULL candidate_reason / track_record_required. Fill them from
    the registry, once. A REAL drain's job — never ensure_tables, which runs
    on every GET and ?dry_run=1 report, and those must stay read-only beyond
    DDL and DO-NOTHING seeds. Touches nothing else: never granted, never the
    operator's notes, never a row that already has a reason. -> rows filled."""
    n = 0
    for name, spec in ACTION_CLASSES.items():
        cur.execute(
            "UPDATE brain_action_classes SET candidate_reason = %s, track_record_required = %s::jsonb, updated_at = NOW() WHERE class = %s AND candidate_reason IS NULL",  # noqa: E501
            (spec.get("candidate_reason"),
             json.dumps(track_record_required_of(None, spec), sort_keys=True), name))
        n += max(0, cur.rowcount or 0)
    return n


def _row_dict(cols, r) -> dict:
    d = dict(zip(cols, r))
    for k in ("granted_at", "last_run_at", "finished_at", "requested_at"):
        if d.get(k) is not None and hasattr(d[k], "isoformat"):
            d[k] = d[k].isoformat()
    if "bound_params" in d:
        d["bound_params"] = _params_dict(d["bound_params"]) or {}
    if "track_record_required" in d:
        d["track_record_required"] = _params_dict(d["track_record_required"]) or {}
    return d


def class_rows(cur) -> list[dict]:
    cur.execute("SELECT " + ", ".join(_CLASS_COLS)
                + " FROM brain_action_classes ORDER BY class")
    return [_row_dict(_CLASS_COLS, r) for r in (cur.fetchall() or [])]


def class_row(cur, cls: str) -> dict | None:
    cur.execute("SELECT " + ", ".join(_CLASS_COLS)
                + " FROM brain_action_classes WHERE class = %s", (cls,))
    r = cur.fetchone()
    return _row_dict(_CLASS_COLS, r) if r else None


def day_used(cur) -> int:
    """Executions in the last 24h, all classes — the ledger is the budget."""
    cur.execute("SELECT COUNT(*) FROM brain_action_class_runs "
                " WHERE executed AND NOT dry_run "
                "   AND started_at > NOW() - INTERVAL '24 hours'")
    r = cur.fetchone()
    return int((r or (0,))[0] or 0)


def verified_runs_7d(cur) -> int:
    """Runs that VERIFIED (not merely executed) in the actuation window —
    what the portal adds to fixes LANDED."""
    cur.execute("SELECT COUNT(*) FROM brain_action_class_runs "
                " WHERE verified AND NOT dry_run "
                "   AND finished_at > NOW() - INTERVAL '7 days'")
    r = cur.fetchone()
    return int((r or (0,))[0] or 0)


_OPEN_STATUSES = ("queued", "running", "awaiting_ops", "awaiting_decision")


def classify_open_rows(cur) -> dict:
    """Backfill action_class on OPEN rows that have none. Idempotent: a row
    already classified is never touched, a row that classifies to nothing is
    re-read on the next pass (cheap, bounded) in case the registry grew."""
    cur.execute(
        """SELECT id, finding_key, title, reason, decision, analysis
             FROM squasher_work_queue
            WHERE status IN %s AND action_class IS NULL
            ORDER BY id DESC LIMIT 500""", (_OPEN_STATUSES,))
    rows = cur.fetchall() or []
    out = {"scanned": len(rows), "classified": 0, "by_class": {}}
    for rid, fk, title, reason, decision, analysis in rows:
        c = classify_text(reason, decision, analysis, title, fk)
        if not c:
            continue
        cur.execute(
            """UPDATE squasher_work_queue
                  SET action_class = %s, action_url = %s, action_method = %s
                WHERE id = %s AND action_class IS NULL""",
            (c["action_class"], c["action_url"], c["action_method"], rid))
        n = max(0, cur.rowcount or 0)
        out["classified"] += n
        if n:
            out["by_class"][c["action_class"]] = (
                out["by_class"].get(c["action_class"], 0) + n)
    return out


def classify_in_tx(cur, item_id: int, *texts) -> bool:
    """Classify ONE row inside the caller's transaction, under a SAVEPOINT so
    a failure here can never poison the caller's commit (the reclaim_misfiled
    lesson: a swallowed error inside a transaction is a silent rollback)."""
    c = classify_text(*texts)
    if not c:
        return False
    try:
        cur.execute("SAVEPOINT action_class_tag")
        cur.execute(
            """UPDATE squasher_work_queue
                  SET action_class = %s, action_url = %s, action_method = %s
                WHERE id = %s""",
            (c["action_class"], c["action_url"], c["action_method"], item_id))
        cur.execute("RELEASE SAVEPOINT action_class_tag")
        return True
    except Exception:  # noqa: BLE001
        try:
            cur.execute("ROLLBACK TO SAVEPOINT action_class_tag")
        except Exception:  # noqa: BLE001
            pass
        return False


def classify_queue_row(item_id: int) -> bool:
    """Analysis-time classification: called by the drain when a row settles
    to a waiting state. Own connection; fail-soft."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT reason, decision, analysis, title, finding_key
                     FROM squasher_work_queue WHERE id = %s""", (item_id,))
            r = cur.fetchone()
            if not r:
                return False
            ok = classify_in_tx(cur, item_id, *r)
            conn.commit()
            return ok
    except Exception as e:  # noqa: BLE001
        logger.warning("[action_classes] classify row %s failed: %s", item_id, e)
        return False


def inbox_by_class(cur) -> dict:
    cur.execute(
        """SELECT id, finding_key, title, status, action_class, action_url,
                  action_method, finished_at
             FROM squasher_work_queue
            WHERE status IN ('awaiting_ops', 'awaiting_decision')
            ORDER BY finished_at DESC NULLS LAST, id DESC LIMIT 200""")
    cols = ("id", "finding_key", "title", "status", "action_class",
            "action_url", "action_method", "finished_at")
    groups: dict = {}
    for r in cur.fetchall() or []:
        d = _row_dict(cols, r)
        groups.setdefault(d.get("action_class") or "unclassified", []).append(d)
    return groups


# ── the drain step ───────────────────────────────────────────────────────

def _self_headers() -> dict:
    """The keys the drain already uses for its loopbacks (squasher_queue)."""
    h = {}
    a = os.environ.get("DCHUB_ADMIN_KEY")
    i = os.environ.get("DCHUB_INTERNAL_KEY") or os.environ.get("INTERNAL_KEY")
    if a:
        h["X-Admin-Key"] = a
    if i:
        h["X-Internal-Key"] = i
    return h


def _loopback(method: str, path: str):
    """Internal call through the app — the same in-process hop _investigate
    uses. -> (status, json dict). Never raises."""
    try:
        from flask import current_app
        with current_app.test_client() as c:
            r = c.open(path, method=method, headers=_self_headers())
            body = {}
            try:
                body = r.get_json(silent=True) or {}
            except Exception:  # noqa: BLE001
                body = {}
            return int(r.status_code), (body if isinstance(body, dict) else {})
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {str(e)[:120]}"}


def _read_metric(fetch, url: str, metric: str):
    """-> (int | None, evidence). None means the instrument did not measure —
    UNOBSERVED, never zero."""
    status, body = fetch("GET", url)
    val = body.get(metric) if isinstance(body, dict) else None
    if status == 200 and isinstance(val, int) and not isinstance(val, bool):
        return val, {"status": status, metric: val}
    return None, {"status": status, "error": (body or {}).get("error")}


def _register_claim(cls: str, params: dict, verifier_url: str, metric: str,
                    pre: int, queue_id: int):
    """Step-1 claim, registered BEFORE the mutation. Lazy import: the ledger
    may not exist on this deploy. -> (claim_id | None, error | None).
    Never stamps an outcome — the ledger's verifier does that."""
    try:
        from routes.claim_ledger import register_claim
    except Exception:  # noqa: BLE001
        return None, "claim_ledger unavailable"
    subject = cls + (":" + "/".join(str(v) for v in params.values())
                     if params else "")
    statement = (f"{cls} on {json.dumps(params, sort_keys=True)} drops "
                 f"{metric} at {verifier_url} below {pre}")
    kw = dict(expected_metric=f"get:{verifier_url} {metric}",
              expected_value=str(pre), horizon_hours=1, expected_op="lt",
              regime={"as_of": datetime.now(timezone.utc).isoformat(),
                      "pre_count": pre, "queue_id": queue_id, "metric": metric},
              surfaces=[verifier_url], shipped=True)
    try:
        try:
            r = register_claim("fix", subject, statement, **kw)
        except TypeError:
            # An older ledger shape keeps the comparator in expected_value.
            kw.pop("expected_op", None)
            kw["expected_value"] = f"< {pre}"
            r = register_claim("fix", subject, statement, **kw)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:120]}"
    if isinstance(r, dict):
        return r.get("id"), (r.get("error") or None)
    try:
        return (int(r) if r is not None else None), None
    except (TypeError, ValueError):
        return None, "unexpected return"


def _insert_run(cur, cls, row, params, action_url, verifier_url, pre,
                executed, outcome, claim_id=None, error=None, dry_run=False):
    cur.execute(
        """INSERT INTO brain_action_class_runs
               (class, queue_id, params, action_url, verifier_url, pre_count,
                executed, outcome, claim_id, error, dry_run)
           VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (cls, row.get("id"), json.dumps(params), action_url, verifier_url,
         pre, bool(executed), outcome, claim_id, (error or None), bool(dry_run)))
    r = cur.fetchone()
    return r[0] if r else None


def _finish_run(cur, run_id, post, verified, outcome, http_status, marked,
                elapsed_ms, error=None, executed=None, actuator_run_id=None):
    """executed=False demotes a ledger row the endpoint REFUSED (budget, kill
    switch) so it stops counting as a budget unit; actuator_run_id links a
    wrapped class's run to the autonomy ledger row that holds its rollback."""
    cur.execute(
        """UPDATE brain_action_class_runs
              SET post_count = %s, verified = %s, outcome = %s,
                  http_status = %s, marked = %s, error = %s,
                  finished_at = NOW(), elapsed_ms = %s,
                  executed = COALESCE(%s, executed),
                  actuator_run_id = COALESCE(%s, actuator_run_id)
            WHERE id = %s""",
        (post, bool(verified), outcome, http_status, marked, (error or None),
         int(elapsed_ms), executed, actuator_run_id, run_id))


def _marked_of(body) -> int | None:
    """Rows the action says it changed: facility-dedup reports
    marked_duplicates, the actuate wrapper rows_affected. None = unstated."""
    if not isinstance(body, dict):
        return None
    for k in ("marked_duplicates", "rows_affected"):
        v = body.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    return None


def _actuator_run_id_of(body) -> int | None:
    v = body.get("actuator_run_id") if isinstance(body, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _update_class(cur, cls: str, verified: bool, consecutive_before: int):
    """The counters and THE BREAKER. Decided here, in Python, so the branch is
    testable against a cursor stub; the SQL only records the decision."""
    new_consecutive = 0 if verified else int(consecutive_before or 0) + 1
    trip = (not verified) and new_consecutive >= _BREAKER_THRESHOLD
    cur.execute(
        """UPDATE brain_action_classes
              SET runs_ok = runs_ok + %s, runs_failed = runs_failed + %s,
                  consecutive_failed = %s,
                  breaker_tripped = (breaker_tripped OR %s),
                  last_run_at = NOW(), updated_at = NOW()
            WHERE class = %s""",
        (1 if verified else 0, 0 if verified else 1, new_consecutive,
         bool(trip), cls))
    return new_consecutive, trip


def _note_row(cur, queue_id: int, note: str, resolve: bool):
    """Evidence goes FIRST: reasons are already at the 600-char cap, so an
    appended note would be the part that gets cut off."""
    if resolve:
        cur.execute(
            """UPDATE squasher_work_queue
                  SET status = 'resolved',
                      reason = LEFT(%s || ' | ' || COALESCE(reason, ''), 600),
                      finished_at = NOW()
                WHERE id = %s AND status = 'awaiting_ops'""",
            (note[:400], queue_id))
    else:
        cur.execute(
            """UPDATE squasher_work_queue
                  SET reason = LEFT(%s || ' | ' || COALESCE(reason, ''), 600)
                WHERE id = %s AND status = 'awaiting_ops'""",
            (note[:400], queue_id))


def execute_one(conn, cur, row: dict, cls_row: dict, *, dry_run: bool = False,
                fetch=None, clock=None) -> dict:
    """Run ONE queue row of a granted class and verify it.

    Order is the contract: pre-read → (budget check) → claim → ledger row →
    mutation → post-read → verdict. The mutation is the only step that cannot
    be undone by this function, so everything that must be true for it to be
    judged afterwards is established before it runs.
    """
    fetch = fetch or _loopback
    clock = clock or time.monotonic
    cls = cls_row.get("class")
    spec = ACTION_CLASSES.get(cls or "")
    res = {"queue_id": row.get("id"), "class": cls, "executed": False,
           "verified": False, "dry_run": bool(dry_run), "outcome": None}
    if not spec:
        res["outcome"] = "skipped_unknown_class"
        return res
    if row.get("status") != "awaiting_ops":
        res["outcome"] = "skipped_not_awaiting_ops"
        return res
    params = row_params_of(row)
    if params is None:           # {} is a valid (class-scoped) answer
        res["outcome"] = "skipped_bad_row_param"
        return res
    action_url = build_action_url(cls, params)
    verifier_url = build_verifier_url(cls, params)
    metric = spec["metric"]
    res.update({"params": params, "action_url": action_url,
                "verifier_url": verifier_url, "metric": metric})

    t0 = clock()
    pre, pre_ev = _read_metric(fetch, verifier_url, metric)
    res["pre_count"] = pre

    if dry_run:
        res["outcome"] = "dry_run"
        res["would_run"] = bool(enabled() and pre is not None and pre > 0)
        res["note"] = ("would run" if res["would_run"] else
                       ("verifier unreadable" if pre is None else
                        "nothing to do" if pre == 0 else
                        "ACTION_CLASSES_ENABLED is not 1"))
        return res

    if not enabled():
        res["outcome"] = "skipped_disabled"
        return res

    if pre is None:
        # UNOBSERVED is not a failure of the class — and not a licence to act.
        _insert_run(cur, cls, row, params, action_url, verifier_url, None,
                    False, "skipped_verifier_unreadable",
                    error=json.dumps(pre_ev)[:300])
        _note_row(cur, row["id"],
                  f"action_class {cls}: verifier unreadable before the run "
                  f"({pre_ev.get('status')}) — not executed", resolve=False)
        res["outcome"] = "skipped_verifier_unreadable"
        return res

    if pre <= 0:
        _insert_run(cur, cls, row, params, action_url, verifier_url, pre,
                    False, "noop_clean")
        _note_row(cur, row["id"],
                  f"action_class {cls}: verifier reads {metric}=0 before any "
                  f"action — nothing to apply, finding no longer holds",
                  resolve=True)
        res["outcome"] = "noop_clean"
        return res

    spent = clock() - t0
    if spent > _WALL_BUDGET_S:
        # Cannot afford the mutation AND its verification inside the
        # heartbeat's window — so the mutation does not start.
        _insert_run(cur, cls, row, params, action_url, verifier_url, pre,
                    False, "skipped_budget",
                    error=f"pre-read took {spent:.1f}s > {_WALL_BUDGET_S}s")
        res["outcome"] = "skipped_budget"
        return res

    claim_id, claim_err = _register_claim(cls, params, verifier_url, metric,
                                          pre, row["id"])
    res["claim_id"], res["claim_error"] = claim_id, claim_err
    run_id = _insert_run(cur, cls, row, params, action_url, verifier_url, pre,
                         True, "started", claim_id=claim_id, error=claim_err)
    conn.commit()          # durable intent BEFORE the mutation
    res["run_id"] = run_id
    res["executed"] = True

    status, body = fetch(spec["method"], action_url)
    if status == 409 and isinstance(body, dict) and body.get("refused"):
        # The endpoint DECLINED to act (its own budget or kill switch — the
        # actuate wrapper answers 409 + refused). Not a class failure and not
        # a budget unit: the ledger row is demoted to executed=FALSE, the
        # counters are untouched, the row waits. A refusal that counted as
        # a failure would trip the breaker on a spent budget (#2505's lesson).
        why = str(body.get("refused"))[:200]
        _finish_run(cur, run_id, None, False, "refused_by_endpoint", status,
                    None, int((clock() - t0) * 1000), error=why,
                    executed=False)
        _note_row(cur, row["id"],
                  f"action_class {cls}: endpoint refused to act ({why}) — "
                  f"not a class failure; row stays awaiting_ops", resolve=False)
        conn.commit()
        res.update(outcome="refused_by_endpoint", executed=False, error=why,
                   http_status=status)
        return res
    marked = _marked_of(body)
    res["http_status"], res["marked"] = status, marked

    post, post_ev = _read_metric(fetch, verifier_url, metric)
    res["post_count"] = post
    verified = (200 <= int(status or 0) < 300
                and post is not None and post < pre)
    res["verified"] = verified
    if verified:
        outcome = "verified"
        err = None
        note = (f"action_class run #{run_id}: VERIFIED {cls} "
                f"{json.dumps(params, sort_keys=True)} {metric} {pre}->{post}"
                f" (marked {marked if marked is not None else '?'}; "
                f"claim #{claim_id if claim_id else 'none'})")
    else:
        outcome = "failed_no_drop" if 200 <= int(status or 0) < 300 else "failed_http"
        err = (f"HTTP {status}" if outcome == "failed_http" else
               f"{metric} did not drop: {pre}->{post}")
        note = (f"action_class run #{run_id} FAILED: {cls} "
                f"{json.dumps(params, sort_keys=True)} — {err}; "
                f"row stays awaiting_ops")
    res["outcome"], res["error"] = outcome, err
    new_consec, trip = _update_class(cur, cls, verified,
                                     cls_row.get("consecutive_failed") or 0)
    res["consecutive_failed"], res["breaker_tripped"] = new_consec, trip
    elapsed_ms = int((clock() - t0) * 1000)
    _finish_run(cur, run_id, post, verified, outcome, status, marked,
                elapsed_ms, error=err, actuator_run_id=_actuator_run_id_of(body))
    _note_row(cur, row["id"], note, resolve=verified)
    conn.commit()
    return res


def candidates(cur, limit: int) -> list[dict]:
    """Oldest awaiting_ops rows of a granted, un-tripped class that have not
    been attempted in the last 24h. The SQL keeps the scan bounded; every
    condition is re-checked in Python (eligible / execute_one) because a
    cursor stub serves whatever it is handed."""
    cur.execute(
        """SELECT q.id, q.finding_key, q.title, q.status, q.action_class,
                  q.action_url, q.action_method, q.finished_at
             FROM squasher_work_queue q
             JOIN brain_action_classes c ON c.class = q.action_class
            WHERE q.status = 'awaiting_ops'
              AND c.granted AND NOT c.breaker_tripped
              AND NOT EXISTS (
                    SELECT 1 FROM brain_action_class_runs r
                     WHERE r.queue_id = q.id AND NOT r.dry_run
                       AND r.started_at > NOW() - (%s * INTERVAL '1 hour'))
            ORDER BY q.finished_at ASC NULLS LAST, q.id ASC
            LIMIT %s""", (_RETRY_ROW_AFTER_HOURS, int(limit)))
    cols = ("id", "finding_key", "title", "status", "action_class",
            "action_url", "action_method", "finished_at")
    return [_row_dict(cols, r) for r in (cur.fetchall() or [])]


def run_granted_actions(dry_run: bool = False, fetch=None, clock=None) -> dict:
    """The drain step. Inert unless ACTION_CLASSES_ENABLED=1 (dry_run may
    look; it never acts). Bounded by max_per_drain / max_per_day."""
    on = enabled()
    out = {"ok": True, "enabled": on, "dry_run": bool(dry_run), "ran": 0,
           "candidates": [], "results": []}
    if not on and not dry_run:
        out["note"] = ("ACTION_CLASSES_ENABLED is not '1' — the step is dark; "
                       "nothing was read or run")
        return out
    try:
        with _conn() as conn, conn.cursor() as cur:
            ensure_tables(cur)
            if dry_run:
                # A dry run is strictly READ-ONLY against the queue: the
                # backfill UPDATE belongs to POST /classify and to a real
                # drain, never to a GET or a ?dry_run=1 report.
                out["classified"] = None
            else:
                out["classified"] = classify_open_rows(cur)
                out["candidate_columns_backfilled"] = backfill_candidate_columns(cur)
            conn.commit()
            cap_day, cap_drain = max_per_day(), max_per_drain()
            used = day_used(cur)
            out.update({"day_used": used, "day_cap": cap_day,
                        "drain_cap": cap_drain})
            classes = {r["class"]: r for r in class_rows(cur)}
            for row in candidates(cur, cap_drain)[:cap_drain]:
                cls_row = classes.get(row.get("action_class") or "")
                entry = {"queue_id": row.get("id"),
                         "class": row.get("action_class"),
                         "action_url": row.get("action_url")}
                ok, why = eligible(cls_row)
                if not ok:
                    entry["skip"] = why
                    out["candidates"].append(entry)
                    continue
                if used >= cap_day:
                    entry["skip"] = f"day cap {used}/{cap_day}"
                    out["candidates"].append(entry)
                    continue
                out["candidates"].append(entry)
                res = execute_one(conn, cur, row, cls_row, dry_run=dry_run,
                                  fetch=fetch, clock=clock)
                out["results"].append(res)
                if res.get("executed"):
                    out["ran"] += 1
                    used += 1
            if not dry_run:
                # ★ #65 part B: the track record for UNGRANTED classes. Real
                #   drains only — a ?dry_run=1 report stays strictly read-only.
                #   Entered only when a candidate exists: the probe step reads
                #   the clock, and a drain with nothing to probe must cost
                #   nothing (the executor's clock budget is per row).
                if any(not c.get("granted") for c in classes.values()):
                    out["probes"] = probe_candidates(conn, cur, classes,
                                                     fetch=fetch, clock=clock)
                else:
                    out["probes"] = {"probed": 0, "results": [], "skipped": []}
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("[action_classes] step failed: %s", e)
        out["ok"] = False
        out["error"] = str(e)[:200]
    return out


# ── portal read ──────────────────────────────────────────────────────────

def summary() -> dict:
    """Everything the portal shows. known=False when unreadable — a blind
    stage renders as a dash, never as 'no classes, nothing granted'."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            ensure_tables(cur)
            conn.commit()
            classes = class_rows(cur)
            for c in classes:
                c["grant_ok"], c["grant_reason"] = grant_allowed(c)
            out = {"known": True, "enabled": enabled(),
                   "caps": {"per_drain": max_per_drain(),
                            "per_day": max_per_day(),
                            "breaker_after": _BREAKER_THRESHOLD},
                   "day_used": day_used(cur),
                   "verified_7d": verified_runs_7d(cur),
                   "classes": classes,
                   "inbox_by_class": inbox_by_class(cur)}
            # #65 B: the graduation READ (file=False — a GET never files).
            # Last, and savepoint-guarded: a failure here must neither poison
            # the reads above nor blank the registry.
            out["graduation"] = _graduation_guarded(cur)
            return out
    except Exception as e:  # noqa: BLE001
        return {"known": False, "error": str(e)[:200]}


# ── endpoints ────────────────────────────────────────────────────────────

def _admin_ok() -> bool:
    keys = set()
    for n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
        v = os.environ.get(n)
        if v:
            keys.add(v)
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key")
            or request.cookies.get("dchub_innov_key") or "").strip()
    return bool(sent) and sent in keys


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["CDN-Cache-Control"] = "no-store"
    return resp


def _gate():
    """-> a response to return early, or None. A kill switch answers 404 —
    NEVER 5xx: the CF worker reads any 5xx from Railway as a dead origin and
    fails the site over to the stale Render mirror."""
    if _disabled():
        return _no_store(jsonify(ok=False, error="not found")), 404
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    return None


@squasher_action_classes_bp.get("/api/v1/brain/squasher/classes")
def classes_get():
    """Registry + state. Read-only; ?dry_run=1 adds what a drain WOULD run."""
    early = _gate()
    if early:
        return early
    out = {"ok": True, **summary()}
    if request.args.get("dry_run") == "1":
        out["plan"] = run_granted_actions(dry_run=True)
    return _no_store(jsonify(out))


@squasher_action_classes_bp.post("/api/v1/brain/squasher/classify")
def classify_post():
    """Backfill action_class on open rows. Idempotent; returns counts."""
    early = _gate()
    if early:
        return early
    try:
        with _conn() as conn, conn.cursor() as cur:
            ensure_tables(cur)
            counts = classify_open_rows(cur)
            conn.commit()
    except Exception as e:  # noqa: BLE001
        return _no_store(jsonify(ok=False, error=str(e)[:200])), 200
    return _no_store(jsonify(ok=True, **counts))


@squasher_action_classes_bp.post("/api/v1/brain/squasher/grant")
def grant_post():
    """{class, granted: bool, by?, clear_breaker?} — the human decision.
    The grant test is enforced here; revoking is always allowed."""
    early = _gate()
    if early:
        return early
    b = request.get_json(silent=True) or {}
    cls = str(b.get("class") or "").strip()
    granted = b.get("granted")
    clear = bool(b.get("clear_breaker"))
    by = (str(b.get("by") or "") or request.headers.get("User-Agent") or
          "operator")[:120]
    if cls not in ACTION_CLASSES:
        return _no_store(jsonify(ok=False, error="unknown class")), 404
    if not isinstance(granted, bool):
        return _no_store(jsonify(ok=False,
                                 error="granted must be true or false")), 400
    try:
        with _conn() as conn, conn.cursor() as cur:
            ensure_tables(cur)
            row = class_row(cur, cls)
            if granted:
                ok, why = grant_allowed(row)
                if not ok:
                    return _no_store(jsonify(ok=False, refused=True,
                                             error=why, **{"class": cls})), 400
            cur.execute(
                """UPDATE brain_action_classes
                      SET granted = %s,
                          granted_by = CASE WHEN %s THEN %s ELSE granted_by END,
                          granted_at = CASE WHEN %s THEN NOW() ELSE granted_at END,
                          breaker_tripped = CASE WHEN %s THEN FALSE
                                                 ELSE breaker_tripped END,
                          consecutive_failed = CASE WHEN %s THEN 0
                                                    ELSE consecutive_failed END,
                          updated_at = NOW()
                    WHERE class = %s""",
                (granted, granted, by, granted, clear, clear, cls))
            conn.commit()
            row = class_row(cur, cls)
    except Exception as e:  # noqa: BLE001
        return _no_store(jsonify(ok=False, error=str(e)[:200])), 200
    return _no_store(jsonify(ok=True, granted=granted, clear_breaker=clear,
                             row=row, **{"class": cls}))


# ══════════════════════════════════════════════════════════════════════════
#  #65 part B (2026-08-22) — the track record, the graduation proposal, and
#  the wrappers that give the autonomy shell's actuators a per-class surface.
# ══════════════════════════════════════════════════════════════════════════

_PROBE_EVERY_HOURS = 6       # one probe per ungranted class per 6h (≤4/day)
_PROBES_PER_DRAIN = 2        # two loopback triplets — well inside the wall budget
_PROBE_COLS = ("id", "finding_key", "title", "status", "action_class",
               "action_url", "action_method", "finished_at")


def track_record_required_of(cls_row: dict | None, spec: dict | None) -> dict:
    """The record a class must show: the default, overridden by the registry
    spec, overridden by non-negative ints on the DATA row (an operator may
    raise N in the table; a typo cannot lower it below 0 or to nonsense)."""
    req = dict(_TRACK_RECORD_DEFAULT)
    for k, v in ((spec or {}).get("track_record_required") or {}).items():
        if k in req and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            req[k] = v
    for k, v in (_params_dict((cls_row or {}).get("track_record_required")) or {}).items():
        if k in req and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            req[k] = v
    return req


def last_dry_run_age_hours(cur, cls: str) -> float | None:
    cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - MAX(started_at))) / 3600.0"
                "  FROM brain_action_class_runs WHERE class = %s AND dry_run",
                (cls,))
    r = cur.fetchone()
    if not r or r[0] is None:
        return None
    try:
        return float(r[0])
    except (TypeError, ValueError):
        return None


def oldest_open_row_of_class(cur, cls: str) -> dict | None:
    """The row a per-row class is probed WITH (its parameter comes from a
    row). Oldest first — the same fairness the drain uses."""
    cur.execute(
        """SELECT id, finding_key, title, status, action_class, action_url,
                  action_method, finished_at
             FROM squasher_work_queue
            WHERE action_class = %s AND status = 'awaiting_ops'
            ORDER BY requested_at ASC, id ASC LIMIT 1""", (cls,))
    r = cur.fetchone()
    return _row_dict(_PROBE_COLS, r) if r else None


def probe_one(conn, cur, cls_row: dict, row: dict | None, *, fetch=None,
              clock=None) -> dict:
    """ONE dry-run probe of an UNGRANTED class: verifier read → the action
    endpoint WITHOUT its bound params (its dry run) → verifier read again.

    Clean = verifier readable AND endpoint 2xx AND the metric did not move.
    A DROP between the two reads means the 'dry' call mutated — the breaker
    trips and a human must look before this class can be probed again.
    Writes one dry_run=TRUE ledger row (executed=FALSE: never a budget unit).
    """
    fetch = fetch or _loopback
    clock = clock or time.monotonic
    cls = cls_row.get("class")
    spec = ACTION_CLASSES.get(cls or "")
    res = {"class": cls, "queue_id": (row or {}).get("id"), "dry_run": True,
           "executed": False, "clean": False, "outcome": None}
    if not spec:
        res["outcome"] = "probe_skipped_unknown_class"
        return res
    params = {} if spec["row_param"] is None else (row_params_of(row) if row else None)
    if params is None:
        res["outcome"] = "probe_skipped_no_row"
        return res
    verifier_url = build_verifier_url(cls, params)
    dry_url = build_dry_run_url(cls, params)
    metric = spec["metric"]
    res.update(params=params, verifier_url=verifier_url, dry_run_url=dry_url,
               metric=metric)
    t0 = clock()
    pre, pre_ev = _read_metric(fetch, verifier_url, metric)
    status, body = fetch(spec["method"], dry_url)
    post, post_ev = _read_metric(fetch, verifier_url, metric)
    http_ok = 200 <= int(status or 0) < 300
    moved = pre is not None and post is not None and post < pre
    clean = pre is not None and http_ok and post == pre
    if moved:
        outcome = "probe_MUTATED"
        err = (f"{metric} moved {pre}->{post} across the DRY call {dry_url}: "
               f"the endpoint is not a dry run without its bound params — "
               f"breaker tripped, a human must inspect")
    elif pre is None:
        outcome, err = "probe_verifier_unreadable", json.dumps(pre_ev)[:300]
    elif not http_ok:
        outcome = f"probe_http_{status}"
        err = json.dumps(body if isinstance(body, dict) else {})[:300]
    elif post is None:
        outcome, err = "probe_post_read_unreadable", json.dumps(post_ev)[:300]
    else:
        outcome, err = "probe_clean", None
    run_id = _insert_run(cur, cls, row or {}, params, dry_url, verifier_url,
                         pre, False, outcome, error=err, dry_run=True)
    _finish_run(cur, run_id, post, clean, outcome, status, None,
                int((clock() - t0) * 1000), error=err)
    if moved:
        cur.execute("UPDATE brain_action_classes SET breaker_tripped = TRUE, "
                    "updated_at = NOW() WHERE class = %s", (cls,))
    conn.commit()
    res.update(run_id=run_id, outcome=outcome, pre_count=pre, post_count=post,
               http_status=status, clean=clean, breaker_tripped=bool(moved),
               error=err)
    return res


def probe_candidates(conn, cur, classes: dict, *, fetch=None, clock=None,
                     budget_s: float = _WALL_BUDGET_S) -> dict:
    """The track record for UNGRANTED classes, earned by a real drain.
    Granted classes earn theirs by running; a tripped class is skipped until
    a human clears it; each class is probed at most every _PROBE_EVERY_HOURS
    (ledger-enforced) and at most _PROBES_PER_DRAIN per drain."""
    clock = clock or time.monotonic
    t0 = clock()
    out = {"probed": 0, "results": [], "skipped": []}
    for cls in sorted(classes):
        cls_row = classes[cls]
        if cls not in ACTION_CLASSES or cls_row.get("granted"):
            continue
        if cls_row.get("breaker_tripped"):
            out["skipped"].append({"class": cls, "why": "breaker tripped"})
            continue
        if out["probed"] >= _PROBES_PER_DRAIN:
            out["skipped"].append({"class": cls, "why": "probes-per-drain cap"})
            continue
        if clock() - t0 > budget_s:
            out["skipped"].append({"class": cls, "why": "wall budget spent"})
            continue
        age = last_dry_run_age_hours(cur, cls)
        if age is not None and age < _PROBE_EVERY_HOURS:
            out["skipped"].append({"class": cls, "why": f"probed {age:.1f}h ago "
                                   f"(every {_PROBE_EVERY_HOURS}h)"})
            continue
        row = None
        if ACTION_CLASSES[cls]["row_param"] is not None:
            row = oldest_open_row_of_class(cur, cls)
            if row is None:
                out["skipped"].append({"class": cls,
                                       "why": "no open awaiting_ops row to probe with"})
                continue
        res = probe_one(conn, cur, cls_row, row, fetch=fetch, clock=clock)
        out["results"].append(res)
        if res.get("run_id") is not None:
            out["probed"] += 1
    return out


# ── graduation: track record → PROPOSAL ──────────────────────────────────

def class_track_record(cur) -> dict:
    """Per class, from the run ledger, last 7 days: dry-run reads, clean dry
    runs, verified runs, failed runs, last dry run."""
    cur.execute(
        """SELECT class,
                  COUNT(*) FILTER (WHERE dry_run),
                  COUNT(*) FILTER (WHERE dry_run AND verified),
                  COUNT(*) FILTER (WHERE NOT dry_run AND outcome = 'verified'),
                  COUNT(*) FILTER (WHERE NOT dry_run
                                     AND outcome IN ('failed_no_drop', 'failed_http')),
                  MAX(started_at) FILTER (WHERE dry_run)
             FROM brain_action_class_runs
            WHERE started_at > NOW() - INTERVAL '7 days'
            GROUP BY class""")
    out = {}
    for r in cur.fetchall() or []:
        try:
            cls, reads, clean, ok, failed, last = r[:6]
        except (TypeError, ValueError):
            continue
        out[cls] = {"dry_run_reads_7d": int(reads or 0),
                    "clean_dry_runs_7d": int(clean or 0),
                    "runs_ok_7d": int(ok or 0), "runs_failed_7d": int(failed or 0),
                    "last_dry_run_at": (last.isoformat()
                                        if hasattr(last, "isoformat") else last)}
    return out


def eligible_for_grant(cls_row: dict | None, record: dict | None) -> tuple[bool, list]:
    """THE CODE RULE — pure, so a cursor stub cannot vouch for it:
    reversible AND verifier_url AND bound_params (grant_allowed) AND ≥N clean
    dry runs in 7d AND consecutive_failed ≤ max AND not already granted AND
    breaker clear. Returns (eligible, the reasons it is not)."""
    why = []
    ok, reason = grant_allowed(cls_row)
    if not ok:
        why.append(reason)
    row = cls_row or {}
    if row.get("granted"):
        why.append("already granted")
    if row.get("breaker_tripped"):
        why.append("breaker tripped — a human clears it")
    req = track_record_required_of(row, ACTION_CLASSES.get(row.get("class") or ""))
    clean = int((record or {}).get("clean_dry_runs_7d") or 0)
    if clean < req["clean_dry_runs"]:
        why.append(f"{clean}/{req['clean_dry_runs']} clean dry runs in 7d")
    cf = int(row.get("consecutive_failed") or 0)
    if cf > req["max_consecutive_failed"]:
        why.append(f"{cf} consecutive failed run(s), max {req['max_consecutive_failed']}")
    return (not why), why


def proposal_key(cls: str) -> str:
    """The open-row identity of a class's graduation proposal."""
    return f"action-class-grant:{cls}"


def open_proposals(cur, classes: list) -> dict:
    """{class: {id, status}} for proposal rows that are still OPEN."""
    keys = [proposal_key(c) for c in classes]
    if not keys:
        return {}
    cur.execute(
        """SELECT finding_key, id, status FROM squasher_work_queue
            WHERE finding_key = ANY(%s)
              AND status IN ('queued', 'running', 'awaiting_ops', 'awaiting_decision')
            ORDER BY id ASC""", (keys,))
    out = {}
    for fk, rid, status in cur.fetchall() or []:
        cls = str(fk).partition(":")[2]
        out.setdefault(cls, {"id": rid, "status": status})
    return out


def _file_proposal(cur, entry: dict, by: str) -> dict | None:
    """Lazy: the queue module owns the table. Never raises."""
    try:
        from routes.squasher_queue import file_decision_row
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"squasher_queue unavailable: {type(e).__name__}"}
    cls = entry["class"]
    req = entry["track_record_required"]
    evidence = (
        f"{entry['clean_dry_runs_7d']} clean dry run(s) of {req['clean_dry_runs']} "
        f"required in 7d ({entry['dry_run_reads_7d']} dry-run read(s), last "
        f"{entry['last_dry_run_at'] or 'never'}); runs 7d ok {entry['runs_ok_7d']} / "
        f"failed {entry['runs_failed_7d']}; consecutive failed "
        f"{entry['consecutive_failed']} (max {req['max_consecutive_failed']}); breaker "
        f"{'TRIPPED' if entry['breaker_tripped'] else 'clear'}; grant test "
        f"{entry['grant_test']}; reversible {entry['reversible']}; verifier "
        f"{entry['verifier_url']}; bound params "
        f"{json.dumps(entry['bound_params'], sort_keys=True)}")
    decision = (
        f"Grant action class {cls}? Evidence: {evidence}. One click: POST "
        f"{entry['grant_url']} {json.dumps(entry['grant_payload'], sort_keys=True)}. "
        f"Decline: POST /api/v1/brain/squasher/resolve-class "
        f"{json.dumps({'class': cls, 'decision': 'not yet', 'outcome': 'rejected', 'note': '<why>'}, sort_keys=True)}. "
        f"This proposal never grants — granting is a human decision.")
    try:
        return file_decision_row(
            cur, finding_key=proposal_key(cls),
            title=f"Grant action class {cls}?",
            reason=f"human decision required: {decision[:400]}",
            decision=decision, analysis=json.dumps(entry, default=str)[:4000],
            source="graduation", action_class=cls,
            action_url=f"/api/v1/brain/squasher/grant?class={cls}",
            action_method="POST", by=by)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def _graduation(cur, *, file: bool = False, max_file: int = 3,
                by: str = "graduation") -> dict:
    """The report, on a caller's cursor. file=True also files the proposals
    (bounded to max_file NEW rows per call; an existing open row is
    refreshed, never duplicated). Grants nothing, ever."""
    classes = class_rows(cur)
    record = class_track_record(cur)
    props = open_proposals(cur, [c["class"] for c in classes])
    out = {"known": True, "as_of": datetime.now(timezone.utc).isoformat(),
           "file": bool(file), "required_default": dict(_TRACK_RECORD_DEFAULT),
           "classes": [], "eligible": [], "filed": [], "refreshed": [],
           "note": ("proposes only — eligible classes get ONE awaiting_decision "
                    "inbox row (identity = class); granting is POST /grant by "
                    "a human")}
    filed = 0
    for c in classes:
        cls = c["class"]
        spec = ACTION_CLASSES.get(cls) or {}
        rec = record.get(cls) or {}
        ok, why = eligible_for_grant(c, rec)
        entry = {
            "class": cls, "granted": bool(c.get("granted")),
            "granted_by": c.get("granted_by"), "granted_at": c.get("granted_at"),
            "candidate": not bool(c.get("granted")),
            "in_code_registry": cls in ACTION_CLASSES,
            "breaker_tripped": bool(c.get("breaker_tripped")),
            "consecutive_failed": int(c.get("consecutive_failed") or 0),
            "reversible": bool(c.get("reversible")),
            "verifier_url": c.get("verifier_url"),
            "bound_params": c.get("bound_params") or {},
            "undo": spec.get("undo"),
            "candidate_reason": c.get("candidate_reason") or spec.get("candidate_reason"),
            "track_record_required": track_record_required_of(c, spec),
            "dry_run_reads_7d": int(rec.get("dry_run_reads_7d") or 0),
            "clean_dry_runs_7d": int(rec.get("clean_dry_runs_7d") or 0),
            "last_dry_run_at": rec.get("last_dry_run_at"),
            "runs_ok_7d": int(rec.get("runs_ok_7d") or 0),
            "runs_failed_7d": int(rec.get("runs_failed_7d") or 0),
            "runs_ok_total": int(c.get("runs_ok") or 0),
            "runs_failed_total": int(c.get("runs_failed") or 0),
            "grant_test": grant_allowed(c)[1],
            "eligible_for_grant": ok, "not_eligible_because": why,
            "grant_url": "/api/v1/brain/squasher/grant",
            "grant_payload": {"class": cls, "granted": True, "by": "<operator>"},
            "proposal": props.get(cls),
        }
        if ok:
            out["eligible"].append(cls)
            if file and filed < max_file:
                r = _file_proposal(cur, entry, by)
                if r and r.get("ok"):
                    entry["proposal"] = {"id": r.get("id"), "status": r.get("status"),
                                         "created": bool(r.get("created"))}
                    if r.get("created"):
                        filed += 1
                        out["filed"].append(r.get("id"))
                    else:
                        out["refreshed"].append(r.get("id"))
                elif r:
                    entry["proposal_error"] = r.get("error")
        out["classes"].append(entry)
    return out


def _graduation_guarded(cur) -> dict:
    """The read-only report under a SAVEPOINT: a failure can neither poison
    the caller's transaction nor blank the registry it rides on."""
    try:
        cur.execute("SAVEPOINT graduation_read")
        out = _graduation(cur, file=False)
        cur.execute("RELEASE SAVEPOINT graduation_read")
        return out
    except Exception as e:  # noqa: BLE001
        try:
            cur.execute("ROLLBACK TO SAVEPOINT graduation_read")
        except Exception:  # noqa: BLE001
            pass
        return {"known": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def graduation_report(file: bool = False, max_file: int = 3,
                      by: str = "graduation") -> dict:
    """Plain function for the #65 shell. file=False (default) is READ-ONLY —
    safe from a GET or a lane check. file=True files ≤max_file new proposal
    rows (one per eligible class, identity = class, re-runs refresh). It
    NEVER grants: the only writer of granted=TRUE is grant_post. JSON-safe;
    never raises — unreadable is {"known": False}."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            ensure_tables(cur)
            conn.commit()
            out = _graduation(cur, file=file, max_file=max_file, by=by)
            conn.commit()
            return out
    except Exception as e:  # noqa: BLE001
        return {"known": False, "error": str(e)[:200], "file": bool(file)}


def file_graduation_proposals(by: str = "graduation", max_file: int = 3) -> dict:
    """The filing entry point, by name — graduation_report(file=True)."""
    return graduation_report(file=True, max_file=max_file, by=by)


# ── the actuator wrappers: verifier, actuate, rollback ───────────────────
#
# The autonomy shell's ACTUATORS are imported at call time and used as-is:
# trigger(cur) is the verifier's number, fire(conn, cur, n) is the action,
# _budget_ok reads the shell's own ledger. A class here cannot drift from
# the shell because it restates none of it.

_NEWS_PRE_IMAGE_CAP = 300    # == the cap _fire_entity_reresolve hands
                             #    _reresolve_unmatched (pinned by a test)
_ROLLBACK_SQL = {
    "deals_exact_dupe_quarantine": (
        "UPDATE deals AS d SET data_flag = v.prior"
        "  FROM unnest(%s::text[], %s::text[]) AS v(id, prior)"
        " WHERE d.id = v.id AND d.data_flag = 'quarantine_duplicate'"),
    "news_entity_reresolve": (
        "UPDATE news_discovered_entities AS e"
        "   SET in_facilities = FALSE, status = v.prior"
        "  FROM unnest(%s::int[], %s::text[]) AS v(id, prior)"
        " WHERE e.id = v.id AND e.in_facilities = TRUE"),
}


def _actuator_for(cls: str):
    """-> (actuator dict, the autonomy module). Lazy import: the shell may be
    absent or broken on a deploy; the wrappers then refuse, never 5xx."""
    spec = ACTION_CLASSES.get(cls or "") or {}
    aid = spec.get("actuator")
    if not aid:
        return None, None
    from routes import brain_autonomy_master_shell as bam
    for a in bam.ACTUATORS:
        if a.get("id") == aid:
            return a, bam
    return None, bam


def is_actuator_class(cls: str) -> bool:
    return bool((ACTION_CLASSES.get(cls or "") or {}).get("actuator"))


def read_trigger(cls: str) -> dict:
    """GET /verifier/<class>: the actuator's trigger as a top-level int under
    the class's metric name. Unreadable → ok=False and the metric None —
    UNOBSERVED, never zero (and never 5xx)."""
    spec = ACTION_CLASSES.get(cls or "")
    if not spec or not spec.get("actuator"):
        return {"ok": False, "error": "not an actuator class", "class": cls}
    metric = spec["metric"]
    try:
        a, _bam = _actuator_for(cls)
        if a is None:
            return {"ok": False, "class": cls, metric: None,
                    "error": "actuator not registered in the autonomy shell"}
        with _conn() as conn, conn.cursor() as cur:
            n = a["trigger"](cur)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "class": cls, metric: None,
                "error": f"{type(e).__name__}: {str(e)[:160]}"}
    if not isinstance(n, int) or isinstance(n, bool):
        return {"ok": False, "class": cls, metric: None,
                "error": "trigger unreadable — UNMEASURED, not zero"}
    return {"ok": True, "class": cls, "actuator": spec["actuator"], metric: int(n),
            "as_of": datetime.now(timezone.utc).isoformat()}


def _pre_image(cur, cls: str):
    """What the news re-resolve is about to scan — the same rows, in the same
    order, under the same cap as _reresolve_unmatched — so the rows it flips
    can be told apart afterwards. Other actuators return their own rollback."""
    if (ACTION_CLASSES.get(cls) or {}).get("actuator") != "news_entity_reresolve":
        return None
    cur.execute("SELECT id, status FROM news_discovered_entities"
                " WHERE in_facilities = FALSE"
                " ORDER BY last_seen_at DESC NULLS LAST LIMIT %s",
                (_NEWS_PRE_IMAGE_CAP,))
    return [(r[0], r[1]) for r in (cur.fetchall() or [])]


def _rollback_from_pre_image(cur, pre_image) -> list:
    if not pre_image:
        return []
    ids = [int(i) for i, _ in pre_image]
    cur.execute("SELECT id FROM news_discovered_entities"
                " WHERE id = ANY(%s) AND in_facilities = TRUE", (ids,))
    flipped = {r[0] for r in (cur.fetchall() or [])}
    return [{"id": i, "prior": s} for i, s in pre_image if i in flipped]


def _refused(out: dict, why: str) -> tuple[dict, int]:
    return {**out, "ok": False, "refused": why, "executed": False}, 409


def actuate(cls: str, *, confirm: bool, by: str = "") -> tuple[dict, int]:
    """POST /actuate/<class>. Without confirm it is a DRY RUN: reads the
    trigger, fires nothing, 200. With confirm=1 it fires the autonomy shell's
    actuator under every one of the shell's own gates — trigger readable and
    > 0, ACTION_CLASSES_ENABLED=1, BRAIN_ACTUATORS_DISABLE≠1, the 1/actuator/
    day + 3/day budget read from brain_actuator_runs — and writes the shell's
    ledger row with the rollback BEFORE it returns. A refusal is 409 +
    {refused}: the drain records it as not-a-failure. -> (body, http)."""
    spec = ACTION_CLASSES.get(cls or "")
    if not spec or not spec.get("actuator"):
        return {"ok": False, "error": "not an actuator class", "class": cls}, 404
    metric = spec["metric"]
    out = {"ok": True, "class": cls, "actuator": spec["actuator"],
           "dry_run": not confirm, "executed": False, "rows_affected": 0,
           metric: None}
    try:
        a, bam = _actuator_for(cls)
    except Exception as e:  # noqa: BLE001
        return _refused(out, f"autonomy shell unavailable: {type(e).__name__}")
    if a is None:
        return _refused(out, "actuator not registered in the autonomy shell")
    try:
        with _conn() as conn, conn.cursor() as cur:
            bam._ensure_tables(conn)         # brain_actuator_runs — the ledger IS the budget
            n = a["trigger"](cur)
            out[metric] = n if isinstance(n, int) and not isinstance(n, bool) else None
            if not confirm:
                out["note"] = "dry run — add ?confirm=1 to fire"
                return out, 200
            if out[metric] is None:
                return _refused(out, "trigger unreadable — will not fire blind")
            if not enabled():
                return _refused(out, "ACTION_CLASSES_ENABLED is not 1")
            if (os.environ.get("BRAIN_ACTUATORS_DISABLE") or "").strip() == "1":
                return _refused(out, "BRAIN_ACTUATORS_DISABLE=1")
            if out[metric] == 0:
                out["note"] = "defect absent — nothing to fire"
                return out, 200
            each_ok, global_ok = bam._budget_ok(cur, a["id"])
            if not (each_ok and global_ok):
                return _refused(out, f"actuator budget spent "
                                     f"({bam.ACTUATOR_DAILY_CAP_EACH}/actuator/day, "
                                     f"{bam.ACTUATOR_DAILY_CAP_GLOBAL}/day global; "
                                     f"brain_actuator_runs is the ledger)")
            pre_image = _pre_image(cur, cls)
            res = a["fire"](conn, cur, out[metric]) or {}
            rollback = res.get("rollback")
            if rollback is None and pre_image is not None:
                rollback = _rollback_from_pre_image(cur, pre_image)
            result = dict(res.get("result") or {})
            result.update({"via": "squasher_action_classes.actuate", "by": by[:120]})
            cur.execute(
                "INSERT INTO brain_actuator_runs"
                " (actuator, live, trigger_n, rows_affected, ok, result, rollback)"
                " VALUES (%s, TRUE, %s, %s, %s, %s::jsonb, %s::jsonb) RETURNING id",
                (a["id"], out[metric], res.get("rows_affected"), bool(res.get("ok")),
                 json.dumps(result, default=str),
                 json.dumps(rollback, default=str) if rollback is not None else None))
            r = cur.fetchone()
            conn.commit()
            out.update(executed=True, ok=bool(res.get("ok")),
                       rows_affected=int(res.get("rows_affected") or 0),
                       actuator_run_id=(r[0] if r else None),
                       rollback_rows=(len(rollback) if isinstance(rollback, list) else 0),
                       result=res.get("result"))
            return out, 200
    except Exception as e:  # noqa: BLE001
        logger.warning("[action_classes] actuate %s failed: %s", cls, e)
        return {**out, "ok": False,
                "error": f"{type(e).__name__}: {str(e)[:160]}"}, 200


def _rollback_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            d = json.loads(v)
            return d if isinstance(d, list) else []
        except Exception:  # noqa: BLE001
            return []
    return []


def rollback_run(actuator_run_id, by: str = "") -> tuple[dict, int]:
    """POST /rollback-run {actuator_run_id}: apply the rollback stored on a
    brain_actuator_runs row — ONE statement per actuator (_ROLLBACK_SQL),
    guarded on the state the run left (a row already restored is skipped),
    and itself ledgered as '<actuator>:rollback'. -> (body, http)."""
    try:
        rid = int(actuator_run_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "actuator_run_id required"}, 400
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT actuator, rollback FROM brain_actuator_runs"
                        " WHERE id = %s", (rid,))
            r = cur.fetchone()
            if not r:
                return {"ok": False, "error": "no such actuator run"}, 404
            actuator, rollback = r[0], _rollback_list(r[1])
            sql = _ROLLBACK_SQL.get(actuator)
            if not sql:
                return {"ok": False, "actuator": actuator,
                        "error": "no rollback rule for that actuator"}, 400
            if not rollback:
                return {"ok": False, "actuator": actuator, "of_run": rid,
                        "error": "that run stored no rollback payload — nothing to reverse"}, 400
            if actuator == "news_entity_reresolve":
                ids = [int(x.get("id")) for x in rollback]
            else:
                ids = [str(x.get("id")) for x in rollback]
            priors = [x.get("prior") for x in rollback]
            cur.execute(sql, (ids, priors))
            n = int(cur.rowcount or 0)
            cur.execute(
                "INSERT INTO brain_actuator_runs"
                " (actuator, live, trigger_n, rows_affected, ok, result, rollback)"
                " VALUES (%s, TRUE, NULL, %s, TRUE, %s::jsonb, NULL)",
                (f"{actuator}:rollback", n,
                 json.dumps({"rolled_back_run": rid, "by": by[:120],
                             "payload_rows": len(rollback)})))
            conn.commit()
        return {"ok": True, "actuator": actuator, "of_run": rid,
                "rolled_back": n, "payload_rows": len(rollback)}, 200
    except Exception as e:  # noqa: BLE001
        logger.warning("[action_classes] rollback-run %s failed: %s", actuator_run_id, e)
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}, 200


# ── endpoints (#65 B) ────────────────────────────────────────────────────

def _by() -> str:
    b = request.get_json(silent=True) or {}
    return (str(b.get("by") or "") or request.headers.get("User-Agent")
            or "operator")[:120]


@squasher_action_classes_bp.post("/api/v1/brain/squasher/graduation")
def graduation_post():
    """graduation_report(file=True): file ≤3 proposal rows. Never grants."""
    early = _gate()
    if early:
        return early
    out = graduation_report(file=True, by=_by())
    return _no_store(jsonify(ok=bool(out.get("known")), **out))


@squasher_action_classes_bp.get("/api/v1/brain/squasher/verifier/<cls>")
def verifier_get(cls):
    early = _gate()
    if early:
        return early
    if not is_actuator_class(cls):
        return _no_store(jsonify(ok=False, error="unknown actuator class")), 404
    return _no_store(jsonify(read_trigger(cls)))


@squasher_action_classes_bp.post("/api/v1/brain/squasher/actuate/<cls>")
def actuate_post(cls):
    early = _gate()
    if early:
        return early
    if not is_actuator_class(cls):
        return _no_store(jsonify(ok=False, error="unknown actuator class")), 404
    out, code = actuate(cls, confirm=(request.args.get("confirm") == "1"), by=_by())
    return _no_store(jsonify(out)), code


@squasher_action_classes_bp.post("/api/v1/brain/squasher/rollback-run")
def rollback_post():
    early = _gate()
    if early:
        return early
    b = request.get_json(silent=True) or {}
    rid = b.get("actuator_run_id") or request.args.get("actuator_run_id")
    out, code = rollback_run(rid, by=_by())
    return _no_store(jsonify(out)), code
