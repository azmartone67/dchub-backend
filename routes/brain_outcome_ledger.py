"""routes/brain_outcome_ledger.py — the OUTCOME LEDGER (Phase 0, 2026-08-14).

#1488 actuation is closed; the brain acts every 30 minutes. What it lacks is
FEEDBACK: no signal whether the PRs it filed LIVED or DIED, and no ledger of
spec-debt transitions. This module records both — and nothing else.

ZERO NEW ACTUATION. This module reads GitHub, reads the spec-debt corpus, and
writes OBSERVATION rows. It cannot open, merge, close or modify a PR, an
issue, a doc, or any behavior flag. If a code path here ever acts, it is
Phase 1 and it was not approved.

WHAT GETS RECORDED (into brain_fix_outcomes — the EXISTING outcomes table;
no new parallel table, per the duplicate-truth rule):
  · proposal_kind='brain_pr_terminal', proposal_id=PR number (kind-scoped id
    space, same convention as the reconciler's kind='autopilot' rows):
      merged                        → resolved=TRUE   ("lived")
      closed unmerged               → resolved=FALSE  ("died")
      open with created_at older
      than ROT_DAYS (default 14)    → resolved=FALSE  ("rotted") — in a repo
        whose spec PRs merge in a median ~6 minutes, two silent weeks is a
        death. An in-flight PR younger than that records NOTHING.
  · proposal_kind='spec_debt', proposal_id=agenda/prop number from the doc
    filename (fallback: crc32 of the filename — kind-scoped):
      a doc whose checklist became fully checked → resolved=TRUE (obligation
      completed). Open debt is a LIVE condition, surfaced by
      /api/v1/brain/spec-debt — it is not an outcome and writes no row.

SCHEMA HONESTY (the live≠repo-DDL trap, introspected 2026-07-26 by the
strategic planner): the LIVE brain_fix_outcomes has the ORIGINAL columns
(proposal_id/proposal_kind/applied_at/checked_at/still_broken/evidence_*).
Three shipped readers (brain_work_selector's PRIMARY read, the issue
janitor's verify lookup, brain_memory's outcome correlator) query klass /
resolved / verified_at / file_path / reason — columns that DO NOT EXIST
live, so those reads have been failing silently to their fallbacks. The
recorder adds them (ALTER TABLE ADD COLUMN IF NOT EXISTS — additive,
nullable, instantaneous) and reports exactly which columns were present
before and after, so the claim is measured, not assumed.

LEGACY-METRIC ISOLATION: ledger rows write checked_at=NULL and
still_broken=NULL on purpose. Every legacy fix-success reader filters
`checked_at > NOW() - …` / `still_broken IS TRUE/FALSE`, so a merged spec
DOCUMENT can never inflate the brain's fix_success_rate. The work_selector's
primary read filters on verified_at/resolved/klass — exactly the columns
these rows do carry.

Surfaces:  GET  /api/v1/brain/outcome-ledger           (JSON; admin-gated)
           POST /api/v1/admin/brain/outcome-ledger/record
Recording is advisory-lock guarded (house snapshot precedent) and idempotent
per (proposal_id, proposal_kind) — stronger than per-day.
Kill:      OUTCOME_LEDGER_DISABLE=1
"""
from __future__ import annotations

import os
import zlib
from datetime import datetime, timezone

WINDOW_DAYS_DEFAULT = 14
ROT_DAYS_DEFAULT = 14
_LOCK_ID = 814202608  # stable advisory-lock id (mirrors pm_brief/dcpi gating)

_GITHUB_REPO_DEFAULT = "azmartone67/dchub-backend"

# Head-branch prefixes that mark a PR as brain-authored. Branch names are the
# one marker the brain writes mechanically (titles are free-form).
BRAIN_HEAD_PREFIXES = ("brain-spec/", "brain/autofix-", "brain/", "brain-v2/",
                      "brain-l5/", "brain-l6/")

LIVED, DIED, ROTTED, IN_FLIGHT = "lived", "died", "rotted", "in_flight"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def window_days() -> int:
    return max(1, _env_int("BRAIN_OUTCOME_LEDGER_WINDOW_DAYS", WINDOW_DAYS_DEFAULT))


def rot_days() -> int:
    return max(1, _env_int("BRAIN_OUTCOME_LEDGER_ROT_DAYS", ROT_DAYS_DEFAULT))


def _parse_iso(v):
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ── classification (pure; the mutation-tested core) ──────────────────

def is_brain_pr(pr: dict) -> bool:
    branch = (((pr or {}).get("head") or {}).get("ref") or "")
    return any(branch.startswith(p) for p in BRAIN_HEAD_PREFIXES)


def klass_for(pr: dict) -> str:
    """Kind-scoped lane class for the work_selector's per-klass read.
    Deliberately coarse (lane granularity, not fix-class granularity) and
    deliberately DISJOINT from the mechanical classifier's class names, so
    these rows can never distort an existing class's learned weight."""
    branch = (((pr or {}).get("head") or {}).get("ref") or "")
    if branch.startswith("brain-spec/"):
        return "brain_spec_pr"
    if branch.startswith("brain/autofix-"):
        return "brain_autofix_pr"
    return "brain_code_pr"


def classify_pr(pr: dict, now: datetime | None = None,
                rot_after_days: int | None = None) -> dict:
    """Terminal-state classification for one PR. Pure.

      merged                          → lived,  resolved=True,  terminal
      closed and NOT merged           → died,   resolved=False, terminal
      open, created >= rot_days ago   → rotted, resolved=False, terminal
      open, younger                   → in_flight, resolved=None, NOT terminal

    resolved is the tri-state the work_selector reads: True = what the brain
    filed lived, False = it died/rotted, None = no verdict yet. Recording a
    died PR as lived would poison the only feedback signal this phase adds —
    tests/test_outcome_ledger.py mutation-guards exactly that."""
    now = now or datetime.now(timezone.utc)
    rot = rot_after_days if rot_after_days is not None else rot_days()
    p = pr or {}
    merged_at = p.get("merged_at")
    state = (p.get("state") or "").lower()
    created = _parse_iso(p.get("created_at"))
    age_days = ((now - created).total_seconds() / 86400.0) if created else None

    if merged_at:
        return {"state": LIVED, "terminal": True, "resolved": True,
                "at": merged_at, "age_days": age_days,
                "reason": "merged — the PR lived"}
    if state == "closed":
        return {"state": DIED, "terminal": True, "resolved": False,
                "at": p.get("closed_at"), "age_days": age_days,
                "reason": "closed without merge — the PR died"}
    if age_days is not None and age_days >= rot:
        return {"state": ROTTED, "terminal": True, "resolved": False,
                "at": p.get("created_at"), "age_days": age_days,
                "reason": f"open {age_days:.0f}d with no merge (rot rule: "
                          f">={rot}d in a repo that merges brain PRs in "
                          f"minutes) — the PR rotted"}
    return {"state": IN_FLIGHT, "terminal": False, "resolved": None,
            "at": p.get("created_at"), "age_days": age_days,
            "reason": "open and younger than the rot window — no verdict yet"}


def spec_debt_doc_id(filename: str) -> int:
    """Stable kind-scoped id for a spec-debt doc: the agenda/prop number from
    the filename, else crc32 of the name. Only ever used under
    proposal_kind='spec_debt' — never joins any other id space."""
    try:
        from routes.brain_spec_debt import agenda_id_for
        n = agenda_id_for(filename)
        if n:
            return int(n)
    except Exception:
        pass
    return int(zlib.crc32((filename or "").encode("utf-8")))


# ── GitHub read (observation only) ───────────────────────────────────

def _gh_repo() -> str:
    return (os.environ.get("GITHUB_REPO") or _GITHUB_REPO_DEFAULT).strip()


def _gh_api(path: str):
    try:
        import requests
    except Exception:
        return None
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "dchub-brain-outcome-ledger/1.0"}
    tok = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        r = requests.get(f"https://api.github.com{path}", headers=headers,
                         timeout=15)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        return None


def read_ledger(days: int | None = None) -> dict:
    """The trailing-window ledger of brain PR terminal states, read live from
    GitHub. Three-valued: an unreadable GitHub is UNMEASURED, never zeros."""
    days = days or window_days()
    now = datetime.now(timezone.utc)
    basis = {
        "repo": _gh_repo(),
        "window_days": days,
        "query": ("GitHub REST /pulls?state=all&sort=updated&direction=desc "
                  "(2 pages x 100), filtered to head branches "
                  + ", ".join(BRAIN_HEAD_PREFIXES)),
        "states": {"lived": "merged", "died": "closed unmerged",
                   "rotted": f"open with created_at >= {rot_days()}d ago",
                   "in_flight": "open, younger than the rot window"},
        "queried_at": now.isoformat(),
    }
    prs: list[dict] = []
    got_any_page = False
    for page in (1, 2):
        data = _gh_api(f"/repos/{_gh_repo()}/pulls?state=all&sort=updated"
                       f"&direction=desc&per_page=100&page={page}")
        if not isinstance(data, list):
            break
        got_any_page = True
        prs.extend(data)
        if len(data) < 100:
            break
    if not got_any_page:
        return {"state": "UNMEASURED",
                "reason": "GitHub unreadable (no token / network / rate "
                          "limit) — refusing to report zero outcomes",
                "counts": None, "prs": None, "basis": basis}

    counts = {LIVED: 0, DIED: 0, ROTTED: 0, IN_FLIGHT: 0}
    rows = []
    for pr in prs:
        if not is_brain_pr(pr):
            continue
        updated = _parse_iso(pr.get("updated_at"))
        if updated and (now - updated).total_seconds() > days * 86400:
            continue
        verdict = classify_pr(pr, now=now)
        counts[verdict["state"]] += 1
        rows.append({
            "pr_number": pr.get("number"),
            "title": (pr.get("title") or "")[:160],
            "branch": ((pr.get("head") or {}).get("ref") or "")[:120],
            "klass": klass_for(pr),
            "state": verdict["state"],
            "resolved": verdict["resolved"],
            "terminal": verdict["terminal"],
            "at": verdict["at"],
            "reason": verdict["reason"],
            "url": pr.get("html_url"),
        })
    basis["prs_scanned"] = len(prs)
    basis["brain_prs_in_window"] = len(rows)
    return {"state": "MEASURED", "counts": counts, "prs": rows, "basis": basis}


# ── DB: additive columns + recorder ──────────────────────────────────

_OPTIONAL_COLUMNS = (
    ("klass", "TEXT"),
    ("resolved", "BOOLEAN"),
    ("verified_at", "TIMESTAMPTZ"),
    ("file_path", "TEXT"),
    ("reason", "TEXT"),
)


def _conn():
    try:
        import psycopg2
    except Exception:
        return None
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        c = psycopg2.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception:
        return None


def _columns_present(cur) -> set:
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'brain_fix_outcomes'")
    return {r[0] for r in cur.fetchall()}


def _ensure_selector_columns(cur) -> dict:
    """ADD COLUMN IF NOT EXISTS for the columns three shipped readers already
    query (work_selector primary read, janitor verify lookup, brain_memory
    correlator). Additive + nullable — instantaneous on Postgres, no rewrite.
    Reports before/after so the schema claim is measured, not assumed."""
    before = _columns_present(cur)
    added = []
    for name, typ in _OPTIONAL_COLUMNS:
        if name in before:
            continue
        cur.execute(f"ALTER TABLE brain_fix_outcomes "
                    f"ADD COLUMN IF NOT EXISTS {name} {typ}")
        added.append(name)
    return {"before": sorted(before), "added": added}


def record_outcomes() -> dict:
    """Record terminal PR states + spec-debt closures into brain_fix_outcomes.

    Advisory-lock guarded (only one recorder at a time); idempotent per
    (proposal_id, proposal_kind) via NOT EXISTS — a re-run inserts nothing
    new. OBSERVATION ONLY: rows in, nothing out."""
    ledger = read_ledger()
    out = {"ok": True, "ledger_state": ledger.get("state"),
           "inserted": {"brain_pr_terminal": 0, "spec_debt": 0},
           "skipped_existing": 0, "skipped_nonterminal": 0,
           "recorded_at": datetime.now(timezone.utc).isoformat()}
    c = _conn()
    if c is None:
        return {"ok": False, "error": "no database connection", **out}
    try:
        with c.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_ID,))
            got = bool(cur.fetchone()[0])
            if not got:
                return {"ok": False,
                        "error": "another recorder holds the advisory lock",
                        **out}
            try:
                out["schema"] = _ensure_selector_columns(cur)

                # 1 · PR terminal states
                for row in (ledger.get("prs") or []):
                    if not row.get("terminal"):
                        out["skipped_nonterminal"] += 1
                        continue
                    pr_num = row.get("pr_number")
                    if not pr_num:
                        continue
                    cur.execute(
                        """INSERT INTO brain_fix_outcomes
                               (proposal_id, proposal_kind, applied_at,
                                checked_at, still_broken, evidence_url,
                                evidence_note, check_count,
                                klass, resolved, verified_at, reason)
                        SELECT %s, 'brain_pr_terminal', COALESCE(%s::timestamptz, NOW()),
                               NULL, NULL, %s, %s, 1, %s, %s, NOW(), %s
                        WHERE NOT EXISTS (
                            SELECT 1 FROM brain_fix_outcomes
                             WHERE proposal_id = %s
                               AND proposal_kind = 'brain_pr_terminal')
                        ON CONFLICT DO NOTHING""",
                        (pr_num, row.get("at"), (row.get("url") or "")[:300],
                         (f"outcome-ledger: PR #{pr_num} {row['state']} — "
                          f"{row['reason']}")[:500],
                         row.get("klass"), row.get("resolved"),
                         (row.get("reason") or "")[:300], pr_num))
                    out["inserted"]["brain_pr_terminal"] += cur.rowcount
                    if cur.rowcount == 0:
                        out["skipped_existing"] += 1

                # 2 · spec-debt CLOSED transitions (a fully-checked doc)
                try:
                    from routes.brain_spec_debt import scan_corpus
                    scan = scan_corpus()
                except Exception:
                    scan = {"state": "UNMEASURED"}
                out["spec_debt_state"] = scan.get("state")
                if scan.get("state") == "MEASURED":
                    counts = scan.get("counts") or {}
                    closed_docs = counts.get("closed", 0)
                    # scan_corpus lists open + unknown docs; closed docs are
                    # the remainder — re-derive them for the row write.
                    listed = {r["doc"] for r in (scan.get("open_obligations") or [])}
                    listed |= {r["doc"] for r in (scan.get("unknown_docs") or [])}
                    import os as _os
                    from routes.brain_spec_debt import (classify_doc_text,
                                                        corpus_dir)
                    d = corpus_dir()
                    for name in sorted(_os.listdir(d)):
                        if not name.endswith(".md") or name in listed:
                            continue
                        try:
                            with open(_os.path.join(d, name),
                                      encoding="utf-8",
                                      errors="replace") as fh:
                                if classify_doc_text(fh.read()) != "closed":
                                    continue
                        except Exception:
                            continue
                        did = spec_debt_doc_id(name)
                        cur.execute(
                            """INSERT INTO brain_fix_outcomes
                                   (proposal_id, proposal_kind, applied_at,
                                    checked_at, still_broken, evidence_url,
                                    evidence_note, check_count,
                                    klass, resolved, verified_at,
                                    file_path, reason)
                            SELECT %s, 'spec_debt', NOW(), NULL, NULL, %s,
                                   %s, 1, 'spec_debt', TRUE, NOW(), %s,
                                   'spec obligation completed: checklist fully checked'
                            WHERE NOT EXISTS (
                                SELECT 1 FROM brain_fix_outcomes
                                 WHERE proposal_id = %s
                                   AND proposal_kind = 'spec_debt')
                            ON CONFLICT DO NOTHING""",
                            (did, f"docs/brain-proposals/{name}"[:300],
                             (f"outcome-ledger: spec-debt CLOSED — doc {name} "
                              f"checklist fully checked")[:500],
                             f"docs/brain-proposals/{name}"[:300], did))
                        out["inserted"]["spec_debt"] += cur.rowcount
                    out["spec_debt_closed_docs"] = closed_docs
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
        out["basis"] = ledger.get("basis")
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}",
                **out}
    finally:
        try:
            c.close()
        except Exception:
            pass


def _read_recorded(cur, limit: int = 20) -> dict:
    cur.execute("""SELECT proposal_kind, COUNT(*),
                          COUNT(*) FILTER (WHERE resolved IS TRUE),
                          COUNT(*) FILTER (WHERE resolved IS FALSE)
                     FROM brain_fix_outcomes
                    WHERE proposal_kind IN ('brain_pr_terminal', 'spec_debt')
                    GROUP BY 1""")
    by_kind = {r[0]: {"rows": int(r[1]), "lived": int(r[2]),
                      "died_or_rotted": int(r[3])} for r in cur.fetchall()}
    cur.execute("""SELECT proposal_id, proposal_kind, klass, resolved,
                          verified_at, evidence_note
                     FROM brain_fix_outcomes
                    WHERE proposal_kind IN ('brain_pr_terminal', 'spec_debt')
                    ORDER BY verified_at DESC NULLS LAST LIMIT %s""", (limit,))
    recent = [{"proposal_id": r[0], "kind": r[1], "klass": r[2],
               "resolved": r[3],
               "verified_at": r[4].isoformat() if r[4] else None,
               "note": r[5]} for r in cur.fetchall()]
    return {"by_kind": by_kind, "recent": recent}


# Self-alarm markers for the actuator-heartbeat audit read (report-only):
# a quarantined pattern matching one of these is the platform benching its
# own alarm as a runaway — the cadence_stall precedent.
SELF_ALARM_MARKERS = ("cadence_stall", "cron_silently_dead", "deadman",
                      "brain_pattern_acting", "pattern_unfixable")


def _read_quarantined_self_alarms(cur) -> dict:
    cur.execute("""SELECT pattern_name, fail_count, quarantined_at,
                          released_at, last_reason
                     FROM brain_pattern_quarantine
                    WHERE pattern_name LIKE 'runaway_finding:%'
                    ORDER BY quarantined_at DESC LIMIT 100""")
    rows = [{"pattern": r[0], "fail_count": r[1],
             "quarantined_at": r[2].isoformat() if r[2] else None,
             "released_at": r[3].isoformat() if r[3] else None,
             "reason": (r[4] or "")[:200]} for r in cur.fetchall()]
    self_alarms = [r for r in rows
                   if any(m in (r["pattern"] or "") for m in SELF_ALARM_MARKERS)]
    return {"runaway_quarantines_total": len(rows),
            "self_alarms_quarantined": self_alarms,
            "note": ("REPORT-ONLY: a self-alarm benched as a runaway means "
                     "the platform quarantined its own dead-man signal. "
                     "Un-benching is an OWNER decision, not an action this "
                     "surface takes.")}


# ── blueprint ────────────────────────────────────────────────────────
try:
    from flask import Blueprint, jsonify, request

    brain_outcome_ledger_bp = Blueprint("brain_outcome_ledger", __name__)

    def _admin_ok() -> bool:
        keys = set()
        for n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
            v = os.environ.get(n)
            if v:
                keys.add(v)
        sent = (request.headers.get("X-Internal-Key")
                or request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
        return bool(sent) and sent in keys

    def _no_store(resp):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["CDN-Cache-Control"] = "no-store"
        return resp

    def _disabled() -> bool:
        return os.environ.get("OUTCOME_LEDGER_DISABLE", "0") == "1"

    @brain_outcome_ledger_bp.get("/api/v1/brain/outcome-ledger")
    def outcome_ledger_view():
        if _disabled():
            return _no_store(jsonify(ok=False, error="disabled")), 503
        if not _admin_ok():
            return _no_store(jsonify(ok=False, error="admin key required")), 401
        try:
            days = max(1, min(int(request.args.get("days",
                                                   str(window_days()))), 90))
        except Exception:
            days = window_days()
        body = {"ok": True, "live": read_ledger(days)}
        c = _conn()
        if c is not None:
            try:
                with c.cursor() as cur:
                    body["recorded"] = _read_recorded(cur)
            except Exception as e:  # noqa: BLE001
                body["recorded"] = {"state": "UNMEASURED",
                                    "reason": str(e)[:160]}
            try:
                with c.cursor() as cur:
                    body["heartbeat_audit"] = _read_quarantined_self_alarms(cur)
            except Exception as e:  # noqa: BLE001
                body["heartbeat_audit"] = {"state": "UNMEASURED",
                                           "reason": str(e)[:160]}
            try:
                c.close()
            except Exception:
                pass
        else:
            body["recorded"] = {"state": "UNMEASURED", "reason": "no db"}
            body["heartbeat_audit"] = {"state": "UNMEASURED", "reason": "no db"}
        return _no_store(jsonify(**body))

    @brain_outcome_ledger_bp.post("/api/v1/admin/brain/outcome-ledger/record")
    def outcome_ledger_record():
        if _disabled():
            return _no_store(jsonify(ok=False, error="disabled")), 503
        if not _admin_ok():
            return _no_store(jsonify(ok=False, error="admin key required")), 401
        return _no_store(jsonify(record_outcomes()))
except Exception:  # pragma: no cover — no-flask test env
    brain_outcome_ledger_bp = None
