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
ACTION_CLASSES = {
    "facility_dedup_apply": {
        "path": "/api/v1/admin/facility-dedup/apply",
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
        "notes": ("Marks facilities.duplicate_of_id (a VISIBILITY flag, never "
                  "a delete) for the clusters GET analyze proposes. Reversible "
                  "via /undo?country=XX. Verifier: analyze duplicate_rows must "
                  "drop."),
    },
}
_PATH_TO_CLASS = {spec["path"]: name for name, spec in ACTION_CLASSES.items()}

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
    return spec["verifier_url"] + "?" + "&".join(
        f"{k}={v}" for k, v in row_params.items())


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
               "consecutive_failed", "last_run_at", "breaker_tripped", "notes")


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
    for name, spec in ACTION_CLASSES.items():
        cur.execute(
            "INSERT INTO brain_action_classes (class, granted, reversible, verifier_url, bound_params, notes) VALUES (%s, FALSE, %s, %s, %s::jsonb, %s) ON CONFLICT (class) DO NOTHING",  # noqa: E501
            (name, bool(spec["reversible"]), spec["verifier_url"],
             json.dumps(spec["bound_params"]), spec["notes"]))


def _row_dict(cols, r) -> dict:
    d = dict(zip(cols, r))
    for k in ("granted_at", "last_run_at", "finished_at", "requested_at"):
        if d.get(k) is not None and hasattr(d[k], "isoformat"):
            d[k] = d[k].isoformat()
    if "bound_params" in d:
        d["bound_params"] = _params_dict(d["bound_params"]) or {}
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
    subject = f"{cls}:" + "/".join(str(v) for v in params.values())
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
                elapsed_ms, error=None):
    cur.execute(
        """UPDATE brain_action_class_runs
              SET post_count = %s, verified = %s, outcome = %s,
                  http_status = %s, marked = %s, error = %s,
                  finished_at = NOW(), elapsed_ms = %s
            WHERE id = %s""",
        (post, bool(verified), outcome, http_status, marked, (error or None),
         int(elapsed_ms), run_id))


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
    if not params:
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
    marked = body.get("marked_duplicates") if isinstance(body, dict) else None
    marked = marked if isinstance(marked, int) and not isinstance(marked, bool) else None
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
                elapsed_ms, error=err)
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
