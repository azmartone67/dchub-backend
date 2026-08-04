"""ddl_audit.py — which of the frozen table-creates actually MATTER (2026-08-04).

WHY THIS EXISTS
===============
`scripts/check_ddl_through_pool.py` froze 59 functions / 214 DDL statements
that have never executed: they run `CREATE TABLE` through the db_utils wrapper,
which drops DDL under SKIP_DDL (default on, absent from prod config). That
answers "did this CREATE run?" — no — but not the question that decides what to
do about it:

  ★ DOES THE TABLE EXIST ANYWAY?

Three outcomes, three different fixes, and they are not distinguishable from
source:

  · EXISTS   — a migration or a deploy predating SKIP_DDL created it. The lazy
               CREATE is dead weight. Converting it to a direct connection
               changes nothing and adds risk; DELETE it or leave it dormant.
  · MISSING  — the module is writing to a table that is not there, and every
               one of those writes has been failing inside someone's
               `except: pass`. THIS is the live bug, and the only case worth
               converting.
  · UNKNOWN  — we could not ask. Reported as UNKNOWN, never as either of the
               above.

★ WHY THIS IS NOT JUST "FIX ALL 59". Converting every frozen CREATE to a direct
connection would fire ~214 dormant DDL statements at production at once —
creating ~140 tables that do not exist today, some superseded years ago, plus
ALTER TABLE statements against live schemas and CREATE INDEX (not CONCURRENTLY)
which takes a write lock on whatever it touches. A blanket fix is a schema
change disguised as a lint cleanup. This endpoint turns the question into a
measurement first.

Endpoints:
  GET /api/v1/admin/ddl-audit            every frozen entry + table existence
  GET /api/v1/admin/ddl-audit?only=missing   just the live bugs
  GET /api/v1/admin/ddl-audit?refresh=1  re-run the source scan (~15s)

Auth: X-Admin-Key / ?admin_key=. Read-only — this module creates nothing, which
would be a poor joke otherwise.
Kill: DDL_AUDIT_DISABLE=1
"""
from __future__ import annotations

import importlib.util
import logging
import os
import time

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
ddl_audit_bp = Blueprint("ddl_audit", __name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCANNER = os.path.join(_ROOT, "scripts", "check_ddl_through_pool.py")

# The source scan walks ~1,270 files (~15s). Cached per process; ?refresh=1
# re-runs it. A stale scan is fine — the source only changes on deploy, and the
# cache is stamped so the reader can see how old it is.
_CACHE: dict = {}


def _disabled() -> bool:
    return (os.environ.get("DDL_AUDIT_DISABLE") or "").strip() == "1"


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    exp = ((os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == exp


def _scanner():
    """Load scripts/check_ddl_through_pool.py by path.

    Deliberately the SAME module CI runs. A second implementation here would
    drift from the guard and start disagreeing with it about what is frozen —
    which is how you end up auditing a list nobody enforces.
    """
    spec = importlib.util.spec_from_file_location("_ddl_guard", _SCANNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scan(refresh: bool = False) -> dict:
    """{ok, frozen:[{path,function,table,line,sql}], scanned_at} or {ok:False}."""
    if not refresh and _CACHE.get("scan"):
        return _CACHE["scan"]
    out = {"ok": False, "frozen": []}
    try:
        g = _scanner()
        files, offences = g.scan_tree(_ROOT)
        allowed = g.load_allowlist(_ROOT)
    except Exception as e:  # noqa: BLE001
        out["error"] = (f"source scan failed: {str(e)[:160]} — the audit is "
                        f"UNMEASURED, not clean")
        return out
    if files < g.MIN_FILES:
        out["error"] = (f"only {files} .py files scanned (expected "
                        f">= {g.MIN_FILES}) — refusing to report a vacuous "
                        f"clean audit")
        return out
    frozen = [o for o in offences
              if f"{o['path']}::{o['function']}" in allowed]
    out.update(ok=True, files=files, frozen=frozen,
               frozen_functions=len({f"{o['path']}::{o['function']}"
                                     for o in frozen}),
               scanned_at=int(time.time()))
    _CACHE["scan"] = out
    return out


def _conn():
    """★ Direct psycopg2, and not only for the obvious reason. This module has
    to be readable by whoever it is telling to stop using the pooled path."""
    try:
        import psycopg2
        url = ((os.environ.get("NEON_REPLICA_URL") or "").strip()
               or (os.environ.get("DATABASE_URL") or "").strip()
               or (os.environ.get("NEON_DATABASE_URL") or "").strip())
        if not url:
            return None
        c = psycopg2.connect(url, connect_timeout=10)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning("[ddl-audit] connect failed: %s", str(e)[:120])
        return None


def table_existence(cur, tables) -> dict:
    """{table: True|False} for every name, in ONE query.

    ★ A name absent from the result is NOT False — it means the query did not
    cover it, and the caller must report UNKNOWN. Treating "not returned" as
    "missing" would invent live bugs out of a query bug.
    """
    names = sorted({t for t in tables if t})
    if not names:
        return {}
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = ANY(%s)", (names,))
    present = {r[0].lower() for r in (cur.fetchall() or [])}
    return {n: (n in present) for n in names}


def verdicts(frozen, exists: dict) -> list:
    """One row per frozen function: does its table exist?"""
    by_fn = {}
    for o in frozen:
        key = f"{o['path']}::{o['function']}"
        row = by_fn.setdefault(key, {
            "path": o["path"], "function": o["function"],
            "line": o["line"], "tables": [], "statements": 0,
        })
        row["statements"] += 1
        if o["table"] and o["table"] not in [t["table"] for t in row["tables"]]:
            row["tables"].append({"table": o["table"],
                                  "exists": exists.get(o["table"])})
    out = []
    for row in by_fn.values():
        states = [t["exists"] for t in row["tables"]]
        if not states or any(s is None for s in states):
            row["verdict"] = "UNKNOWN"
        elif all(states):
            row["verdict"] = "EXISTS"
        elif not any(states):
            row["verdict"] = "MISSING"
        else:
            row["verdict"] = "PARTIAL"
        out.append(row)
    order = {"MISSING": 0, "PARTIAL": 1, "UNKNOWN": 2, "EXISTS": 3}
    return sorted(out, key=lambda r: (order[r["verdict"]], r["path"]))


_HOW_TO_READ = (
    "MISSING = the module writes to a table that is not there; every one of "
    "those writes has been failing inside an except:pass. Fix by moving the "
    "DDL to a direct psycopg2 connection (see routes/email_suppression."
    "_ensure_table) and removing the line from the allowlist. "
    "EXISTS = a migration or a pre-SKIP_DDL deploy created it; the lazy CREATE "
    "is dead weight — deleting it is the honest change, converting it is not. "
    "PARTIAL = one function, some tables present and some not. "
    "UNKNOWN = we could not ask. It is not EXISTS."
)


def audit_report(refresh: bool = False) -> dict:
    """The whole audit, independent of HTTP. Used by the route AND by the
    boot logger — one code path, so the log and the endpoint can never
    disagree about what is MISSING."""
    s = scan(refresh=refresh)
    if not s.get("ok"):
        return {"ok": False, "error": s.get("error", "scan_failed")}

    c = _conn()
    exists, db_err = {}, None
    if c is None:
        db_err = "no database — every verdict below is UNKNOWN, not EXISTS"
    else:
        try:
            with c.cursor() as cur:
                exists = table_existence(cur, [o["table"] for o in s["frozen"]])
        except Exception as e:  # noqa: BLE001
            db_err = f"existence query failed: {str(e)[:140]} — UNKNOWN, not EXISTS"
        finally:
            try:
                c.close()
            except Exception:
                pass

    rows = verdicts(s["frozen"], exists)
    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    out = {
        "ok": True,
        "frozen_functions": s["frozen_functions"],
        "frozen_statements": len(s["frozen"]),
        "distinct_tables": len({o["table"] for o in s["frozen"] if o["table"]}),
        "counts": counts,
        "entries": rows,
        "scanned_at": s.get("scanned_at"),
        "how_to_read": _HOW_TO_READ,
    }
    if db_err:
        out["db_error"] = db_err
    return out


# ── the audit publishes ITSELF ────────────────────────────────────────
# ★ WHY A LOG LINE AND NOT JUST THE ENDPOINT. The endpoint needs an admin key.
# Getting one into a terminal took five round-trips and ended with the live key
# pasted into a chat transcript, which then had to be rotated. A finding that
# only exists behind a credential is a finding nobody reads — the same failure
# as a fix that is never wired. This runs once per boot in a daemon thread and
# prints the MISSING list to stdout, so `railway logs | grep ddl-audit` answers
# it with no key, no curl, and nothing to paste.
_BOOT_DELAY_S = 90          # let the app finish booting and serve traffic first
_boot_started = False


def _boot_lines(rep: dict):
    """The log lines for one report. Pure, so a test can read them."""
    tag = "[ddl-audit]"
    if not rep.get("ok"):
        return [f"{tag} UNMEASURED: {rep.get('error', 'unknown')}"]
    c = rep["counts"]
    head = (f"{tag} {rep['frozen_functions']} frozen functions / "
            f"{rep['frozen_statements']} DDL statements · "
            + " ".join(f"{k}={v}" for k, v in sorted(c.items())))
    if rep.get("db_error"):
        head += f" · {rep['db_error']}"
    lines = [head]
    missing = [r for r in rep["entries"] if r["verdict"] == "MISSING"]
    if not missing:
        lines.append(f"{tag} no MISSING tables — nothing on the frozen list is "
                     f"currently costing us a write")
    for r in missing:
        tbls = ",".join(t["table"] for t in r["tables"])
        lines.append(f"{tag} MISSING {r['path']}:{r['line']} {r['function']} "
                     f"-> {tbls}")
    return lines


def _boot_audit():
    try:
        import time as _t
        _t.sleep(_BOOT_DELAY_S)
        for line in _boot_lines(audit_report()):
            print(line, flush=True)
    except Exception as e:  # noqa: BLE001
        # ★ Even the failure is logged. A silent boot audit is indistinguishable
        # from a clean one, which is the exact bug this module was built around.
        print(f"[ddl-audit] boot audit FAILED: {e!r}", flush=True)


def start_boot_audit():
    """Kick the one-shot boot audit. Idempotent; never raises."""
    global _boot_started
    if _boot_started or _disabled():
        return False
    if (os.environ.get("DDL_AUDIT_NO_BOOT_LOG") or "").strip() == "1":
        return False
    _boot_started = True
    try:
        import threading
        threading.Thread(target=_boot_audit, name="ddl-audit-boot",
                         daemon=True).start()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[ddl-audit] boot thread failed: %s", str(e)[:120])
        return False


@ddl_audit_bp.route("/api/v1/admin/ddl-audit", methods=["GET"])
def ddl_audit():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    refresh = (request.args.get("refresh") or "").strip() == "1"
    out = audit_report(refresh=refresh)
    if not out.get("ok"):
        return jsonify(out), 500
    only = (request.args.get("only") or "").strip().upper()
    if only:
        out["entries"] = [r for r in out["entries"] if r["verdict"] == only]
    return jsonify(out)
