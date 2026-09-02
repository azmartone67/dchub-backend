"""Self-growing DB index engine (2026-07-16).

The brain adds indexes as new hot paths appear, instead of waiting for a human to
notice a seq-scan starving the read pool — the exact failure L14 DETECTED but
nothing ACTED on: /api/v1/facilities/<slug> hard_burn (unindexed
LEFT(MD5(provider|name),8) fallback over `facilities`).

WHAT IT DOES (run_index_advisor):
  1. reads pg_stat_statements for the hottest + slowest SELECTs,
  2. EXPLAIN (FORMAT JSON) each (params → NULL so the normalized text plans),
  3. walks the plan for Seq Scan nodes on BIG tables that have a single-column
     equality/range Filter,
  4. AUTO-CREATES the covering single-column btree index (IF NOT EXISTS), capped
     per run, then ANALYZEs.

SAFETY (why auto-apply is OK for THIS class):
  * additive + behaviour-neutral: an extra index never changes query RESULTS,
  * IF NOT EXISTS → idempotent, EXPLAIN (not ANALYZE) never executes the query,
  * table + column are WHITELISTED against pg_catalog — never interpolate raw
    pg_stat_statements text (SQL-injection guard),
  * only the SAFE class auto-applies: ONE real column, btree. Anything else
    (multi-col, expression like LEFT(MD5(...)), partial) is PROPOSED as a
    brain_finding, never auto-applied,
  * per-run cap (SGI_MAX_APPLY) bounds blast radius,
  * kill switch: SELF_GROWING_INDEX_DISABLE=1.

DDL rule: a RAW autocommit psycopg2 conn — NEVER db_utils (SKIP_DDL='1' silently
no-ops CREATE INDEX via safe_db). Mirrors add_performance_indexes.py / canonical_stats._conn.
"""
import os
import re
import json
import logging

logger = logging.getLogger(__name__)

_MIN_MEAN_MS       = float(os.environ.get("SGI_MIN_MEAN_MS", "120"))    # slow only
_MIN_CALLS         = int(os.environ.get("SGI_MIN_CALLS", "15"))         # hot only
_MIN_TABLE_ROWS    = int(os.environ.get("SGI_MIN_TABLE_ROWS", "5000"))  # seq-scan hurts only on big tables
_MAX_APPLY_PER_RUN = int(os.environ.get("SGI_MAX_APPLY", "3"))          # blast-radius cap
_TOP_QUERIES       = int(os.environ.get("SGI_TOP_QUERIES", "50"))

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")   # safe unquoted SQL identifier
# column in a Seq-Scan Filter: "(state = 'x')", "((provider)::text = ...)", "(power_mw > 0)"
_FILTER_COL = re.compile(r"\(*\(?([a-z_][a-z0-9_]*)\)?(?:::[a-z0-9 ]+)?\s*(?:=|>=|<=|<>|>|<|~~|IS)\s")


def _conn():
    """Raw autocommit psycopg2 conn for DDL/EXPLAIN. NEVER db_utils."""
    try:
        import psycopg2
    except Exception:
        return None
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        return None
    try:
        c = psycopg2.connect(dsn, sslmode="require", connect_timeout=10)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("sgi: connect failed: %s", e)
        return None


def _ensure_runs_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS index_advisor_runs (
            id          BIGSERIAL PRIMARY KEY,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            reason      TEXT,
            seen        INT DEFAULT 0,
            applied     JSONB DEFAULT '[]'::jsonb,
            proposed    JSONB DEFAULT '[]'::jsonb
        )
    """)


def _real_columns(cur, table):
    """Whitelist: real columns of a real public BASE table (relkind='r')."""
    cur.execute("""
        SELECT a.attname
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = %s AND c.relkind = 'r' AND n.nspname = 'public'
          AND a.attnum > 0 AND NOT a.attisdropped
    """, (table,))
    return {r[0] for r in cur.fetchall()}


def _table_rows(cur, table):
    try:
        cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname=%s AND relkind='r'", (table,))
        r = cur.fetchone()
        return int(r[0]) if r and r[0] is not None else 0
    except Exception:
        return 0


def _column_already_indexed(cur, table, col):
    """True if an existing index LEADS with this column (so a new one is redundant)."""
    try:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE tablename=%s AND schemaname='public'", (table,))
        for (idxdef,) in cur.fetchall():
            # match "... ON table (col" or "... ON table (col," as the FIRST indexed col
            m = re.search(r"\(\s*([a-z_][a-z0-9_]*)", idxdef.lower())
            if m and m.group(1) == col:
                return True
    except Exception:
        return True   # unsure → treat as covered (never over-create)
    return False


def _seq_scan_targets(plan_json):
    """Walk EXPLAIN(FORMAT JSON) tree; return [(relation, filter_text), ...] for
    every Seq Scan node that carries a Filter."""
    out = []

    def walk(node):
        if not isinstance(node, dict):
            return
        if node.get("Node Type") == "Seq Scan" and node.get("Relation Name") and node.get("Filter"):
            out.append((node["Relation Name"], node.get("Filter") or ""))
        ch = node.get("Plans")
        if isinstance(ch, list):
            for c in ch:
                walk(c)

    try:
        root = plan_json[0] if isinstance(plan_json, list) else plan_json
        walk(root.get("Plan", root) if isinstance(root, dict) else {})
    except Exception:
        pass
    return out


def _candidate_columns(filter_text, real_cols):
    cols = []
    for m in _FILTER_COL.finditer(filter_text or ""):
        c = m.group(1)
        if c in real_cols and c not in cols:
            cols.append(c)
    return cols


def _explainable(query):
    """Normalized pg_stat_statements text has params ($1..) — substitute NULL so
    EXPLAIN can plan it. Skip DDL/CTE-write/truncated text."""
    q = query.strip().rstrip(";")
    if not q.upper().startswith("SELECT"):
        return None
    if len(q) > 8000 or q.endswith("..."):   # truncated by track_activity_query_size
        return None
    # $N -> NULL (LIMIT NULL / col = NULL both plan fine; type mismatches just skip on EXPLAIN error)
    return re.sub(r"\$\d+", "NULL", q)


def _write_finding(cur, issue: str, url: str, count: int, detail: str,
                   detector: str) -> str:
    """★2026-09-02 (D14): route through the canonical brain_findings_writer.
    This module's conn is AUTOCOMMIT (DDL rule, see _conn), and the writer is
    savepoint-wrapped: `SAVEPOINT` outside a transaction block fails, and the
    writer then reports "skipped" — the reason the two INSERTs below were
    hand-rolled, and the reason every hand-rolled writer re-opens the 477k-
    duplicates class (no episode ledger, no runaway quarantine). Open a real
    transaction around the write instead. Returns the writer's verdict."""
    from routes.brain_findings_writer import upsert_brain_finding
    conn = cur.connection
    was_autocommit = bool(getattr(conn, "autocommit", False))
    if was_autocommit:
        conn.autocommit = False
    try:
        verdict = upsert_brain_finding(cur, issue=issue, url=url, count=count,
                                       detail=detail, detector=detector,
                                       status="open")
        conn.commit()
        return verdict
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if was_autocommit:
            conn.autocommit = True


def _file_finding(cur, applied, proposed):
    """File the run's finding through the canonical writer (D14)."""
    try:
        n_app, n_prop = len(applied), len(proposed)
        idx_list = ", ".join(a["index"] for a in applied) or "—"
        detail = (f"self-growing index engine: auto-created {n_app} index(es) [{idx_list}] "
                  f"for hot seq-scans; proposed {n_prop} more (cap/complex). "
                  f"Pillar: performance_self_heal.")
        _write_finding(cur, "self_growing_index", "", n_app, detail[:800],
                       "self_growing_index")
    except Exception as e:
        logger.warning("sgi: finding write failed: %s", e)


def run_index_advisor(reason="weekly", max_apply=None):
    """Analyze pg_stat_statements and auto-create safe single-column indexes for
    hot seq-scans. Returns a summary dict. Fail-soft; never raises."""
    if (os.environ.get("SELF_GROWING_INDEX_DISABLE") or "").strip() == "1":
        return {"skipped": "disabled"}
    cap = _MAX_APPLY_PER_RUN if max_apply is None else int(max_apply)
    conn = _conn()
    if conn is None:
        return {"error": "no db"}
    applied, proposed, seen = [], [], 0
    try:
        cur = conn.cursor()
        _ensure_runs_table(cur)
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        except Exception as e:
            logger.warning("sgi: pg_stat_statements unavailable: %s", str(e)[:120])
            return {"error": "no_pg_stat_statements", "detail": str(e)[:160]}
        try:
            cur.execute(
                "SELECT query, calls, mean_exec_time FROM pg_stat_statements "
                "WHERE query ILIKE 'SELECT%%' AND calls >= %s AND mean_exec_time >= %s "
                "ORDER BY total_exec_time DESC LIMIT %s",
                (_MIN_CALLS, _MIN_MEAN_MS, _TOP_QUERIES),
            )
            rows = cur.fetchall()
        except Exception as e:
            return {"error": "pgss_query_failed", "detail": str(e)[:160]}

        done_cols = set()   # (table, col) handled this run
        for query, calls, mean_ms in rows:
            seen += 1
            exq = _explainable(query)
            if not exq:
                continue
            try:
                cur.execute("EXPLAIN (FORMAT JSON) " + exq)
                plan = cur.fetchone()[0]
                if isinstance(plan, str):
                    plan = json.loads(plan)
            except Exception:
                continue  # EXPLAIN failed (type mismatch, truncation) → skip
            for table, filt in _seq_scan_targets(plan):
                if not _IDENT.match(table or ""):
                    continue
                if _table_rows(cur, table) < _MIN_TABLE_ROWS:
                    continue
                real = _real_columns(cur, table)
                cols = _candidate_columns(filt, real)
                if not cols:
                    continue
                col = cols[0]                      # SAFE class: single real column
                key = (table, col)
                if key in done_cols:
                    continue
                done_cols.add(key)
                if _column_already_indexed(cur, table, col):
                    continue
                idx = f"idx_{table}_{col}_sgi"[:63]
                rec = {"table": table, "column": col, "index": idx,
                       "mean_ms": round(float(mean_ms), 1), "calls": int(calls)}
                if len(applied) >= cap:
                    proposed.append(rec)           # over cap → propose only
                    continue
                try:
                    cur.execute('CREATE INDEX IF NOT EXISTS "%s" ON "%s" ("%s")' % (idx, table, col))
                    cur.execute('ANALYZE "%s"' % table)
                    applied.append(rec)
                    logger.info("🌱 sgi: created %s on %s(%s) — hot seq-scan %.0fms×%s [%s]",
                                idx, table, col, mean_ms, calls, reason)
                except Exception as e:
                    logger.warning("sgi: create %s failed: %s", idx, str(e)[:120])

        try:
            cur.execute(
                "INSERT INTO index_advisor_runs (reason, seen, applied, proposed) "
                "VALUES (%s, %s, %s, %s)",
                (reason[:200], seen, json.dumps(applied), json.dumps(proposed)),
            )
        except Exception as e:
            logger.warning("sgi: run-record failed: %s", str(e)[:120])
        if applied or proposed:
            _file_finding(cur, applied, proposed)
    except Exception as e:
        logger.error("sgi: run failed: %s", str(e)[:200])
        return {"error": str(e)[:200]}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"reason": reason, "seen": seen,
            "applied": applied, "proposed": proposed}


def check_edge_origin_divergence(probes=None):
    """#3 — detect the exact condition of the 2026-07-16 facility incident: the
    edge (CF worker) returns 5xx for a pattern while the Railway ORIGIN is healthy
    (200). Today NOTHING compares edge-vs-origin per pattern, so a stuck edge is
    invisible until a human probes. Files an 'edge_origin_divergence' finding and
    makes a BEST-EFFORT edge cache purge. HONEST LIMIT: there is no backend->worker
    breaker-reset channel — if the worker itself is stuck/bricked (not just cached),
    the real fix is a dchubapiproxy redeploy/rollback (manual dashboard or the
    selfheal worker), which this flags for a human. Fail-soft; never raises."""
    import urllib.request as _u
    origin = (os.environ.get("RAILWAY_ORIGIN")
              or "https://dchub-backend-production.up.railway.app")
    edge = "https://dchub.cloud"
    # representative dynamic paths; the facility-by-slug pattern is the incident class
    probes = probes or [
        "/api/v1/facilities/lumen-technologies-level-3-oakland-0ede8888",
        "/api/v1/stats",
    ]

    def _status(base, path):
        try:
            req = _u.Request(base + path, headers={"User-Agent": "dchub-sgi-divergence/1.0"})
            return _u.urlopen(req, timeout=12).getcode()
        except Exception as e:
            code = getattr(e, "code", None)
            return int(code) if code else 0

    diverged = []
    for p in probes:
        o, e = _status(origin, p), _status(edge, p)
        if o and o < 400 and e >= 500:
            diverged.append({"path": p, "origin": o, "edge": e})

    if not diverged:
        return {"diverged": []}

    # best-effort edge purge (won't clear a non-cached computed 503, but is the only
    # backend-side lever; a stuck worker still needs a redeploy — flagged below)
    purged = False
    try:
        from routes.cf_purge import _purge_urls  # existing helper
        _purge_urls([edge + d["path"] for d in diverged])
        purged = True
    except Exception:
        purged = False

    conn = _conn()
    if conn is not None:
        try:
            cur = conn.cursor()
            detail = ("edge (CF worker) returns 5xx while origin is healthy for: "
                      + "; ".join(f"{d['path']} (origin {d['origin']} / edge {d['edge']})"
                                  for d in diverged)
                      + f". Best-effort edge purge attempted={purged}. If it persists the "
                        "dchubapiproxy worker is stuck (no backend breaker-reset channel) "
                        "-> needs a worker redeploy/rollback. Pillar: edge_origin_integrity.")
            _write_finding(cur, "edge_origin_divergence", diverged[0]["path"],
                           len(diverged), detail[:900], "edge_origin_divergence")
        except Exception as e:
            logger.warning("sgi: divergence finding write failed: %s", str(e)[:120])
        finally:
            try: conn.close()
            except Exception: pass
    logger.warning("🚨 sgi: edge/origin divergence on %d path(s); purge=%s", len(diverged), purged)
    return {"diverged": diverged, "purged": purged}


def last_run_age_hours():
    """Hours since the last advisor run (for the scheduler's DB self-gate).
    None if never run / table absent → caller should run."""
    conn = _conn()
    if conn is None:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(created_at) FROM index_advisor_runs")
        last = (cur.fetchone() or [None])[0]
        if last is None:
            return None
        from datetime import datetime as _dt, timezone as _tz
        return (_dt.now(_tz.utc) - last).total_seconds() / 3600.0
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
