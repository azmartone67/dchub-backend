"""
brain_autonomy_loop.py — THE AUTONOMY LOOP (2026-06-18).

Wires the brain's existing self-fix pieces into ONE scheduled orchestrator
that runs REACTIVELY (captured runtime/DB errors → tracked findings) and
PROACTIVELY (sweep the deployed Python tree for known bug-classes → store
mechanical-fix proposals), then classifies the open proposals and opens
DRAFT PRs (human still merges) via the existing Phase-2 writer.

★ BUILT DORMANT. A single master switch gates EVERY write:

    BRAIN_AUTONOMY_ENABLED   (default "0" = OFF)

  With it OFF, autonomy_tick() returns {disabled:true, preview:...} after a
  READ-ONLY dry summary and makes ZERO DB / GitHub writes. The off-gate is
  the FIRST check in autonomy_tick(), before any write path.

  This is SEPARATE from BRAIN_AUTOMERGE_ENABLED (Phase 3 auto-merge, also
  off by default). LAYERING:
    · AUTONOMY on  → brain auto-detects + auto-opens DRAFT PRs (human merges).
    · AUTOMERGE on → ALSO auto-merges those DRAFT PRs (run via the cron's
                     chained /automerge/run + /automerge/canary calls).
  Each flag is independent: AUTONOMY can be on with AUTOMERGE off (the
  intended first rollout — draft PRs, human merge).

REUSE (no re-derivation):
  · routes/brain_mechanical_classifier — classify_mechanical, _admin_ok,
    ALLOWLIST_CLASSES, FORBIDDEN_PATH_PATTERNS / _forbidden_path_hits,
    _fetch_open_proposals (open-proposal read).
  · routes/brain_draft_pr_writer — open_mechanical_draft_prs (rate-capped,
    token-gated, DRAFT-only writer).
  · routes/brain_runtime_errors — grouped_runtime_patterns() accessor over
    the in-memory WARNING+ capture.
  · routes/brain_findings_writer — upsert_brain_finding (canonical writer).
  · routes/brain_v2_layer5 — reconcile_proposals (already-fixed → resolved).

The PROACTIVE sweep derives each anti-pattern's buggy-form regex + the
deterministic transform from the SAME allowlist the classifier gates on, so
a proposal this loop creates is guaranteed to be classify_mechanical()-true
(or it is dropped before insert).

Endpoints (own blueprint, admin gate mirrors the classifier's _admin_ok):
  POST /api/v1/brain/autonomy/tick    — run one tick (honors the OFF gate)
  GET  /api/v1/brain/autonomy/status  — {autonomy_enabled, automerge_enabled,
                                         last_tick, counts}

Heavy imports (flask, psycopg2, the flask-importing sibling modules) are
LAZY so this module imports cleanly in a no-flask test env where the DB /
GitHub / writer primitives are all monkeypatched.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from routes._swallowed_writes import note_swallowed_write


# ── Tunables (env-driven; conservative defaults) ─────────────────────
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


AUTONOMY_MAX_NEW_PROPOSALS = _env_int("AUTONOMY_MAX_NEW_PROPOSALS", 3)
AUTONOMY_RUNTIME_MIN_COUNT = _env_int("AUTONOMY_RUNTIME_MIN_COUNT", 3)
# Cap how many files the proactive sweep walks per run so a tick stays cheap.
AUTONOMY_SCAN_MAX_FILES = _env_int("AUTONOMY_SCAN_MAX_FILES", 4000)

LOOP_NAME = "autonomy_proactive"
SCAN_MODEL = "autonomy-scan"
SCAN_CONFIDENCE = 0.92


# ── The master switch (the FIRST gate before any write) ──────────────
def autonomy_enabled() -> bool:
    """True only when BRAIN_AUTONOMY_ENABLED == '1'. Default OFF."""
    return os.environ.get("BRAIN_AUTONOMY_ENABLED", "0") == "1"


def automerge_enabled() -> bool:
    """Mirror of the Phase-3 flag (separate switch). Default OFF."""
    return os.environ.get("BRAIN_AUTOMERGE_ENABLED", "0") == "1"


# ── Repo root + file walk ────────────────────────────────────────────
def _repo_root() -> str:
    """The deployed tree root (e.g. /app on Railway) = the parent of routes/."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# This file's own repo-relative path — NEVER scan ourselves (the literal
# anti-pattern regexes below would self-match the documentation/seed lines).
_SELF_REL = os.path.join("routes", os.path.basename(__file__))

# Extra skip prefixes on top of FORBIDDEN_PATH_PATTERNS: tests/ (fixtures
# deliberately plant anti-patterns) + the classifier's own seed module (its
# _KNOWN_BUG_PATTERNS / ALLOWLIST_CLASSES literals are anti-patterns by design).
_SKIP_REL_PREFIXES = ("tests/", "test/")
_SKIP_REL_SUBSTR = (
    "brain_mechanical_classifier.py",   # holds the buggy-form seed regexes
    "brain_v2_layer5.py",               # holds _KNOWN_BUG_PATTERNS literals
    "brain_autonomy_loop.py",           # this file (belt-and-suspenders)
)


def _iter_py_files(root: str):
    """Yield (rel_path, abs_path) for every *.py under root, SKIPPING
    forbidden paths + tests/ + this file + the seed-pattern modules. The
    forbidden-path check reuses the classifier's _forbidden_path_hits so the
    sweep can NEVER walk a billing/auth/migration/worker/honest-numbers file."""
    from routes.brain_mechanical_classifier import _forbidden_path_hits

    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune obvious noise dirs in-place (faster + avoids .git/venv churn).
        dirnames[:] = [d for d in dirnames if d not in (
            ".git", "__pycache__", "node_modules", ".venv", "venv",
            "site-packages", ".mypy_cache", ".pytest_cache")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            abs_path = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            # Skip ourselves + the seed modules.
            if rel == _SELF_REL.replace(os.sep, "/"):
                continue
            if any(rel.startswith(p) for p in _SKIP_REL_PREFIXES):
                continue
            if any(s in rel for s in _SKIP_REL_SUBSTR):
                continue
            # Reuse the classifier's forbidden-path gate (migrations/auth/
            # billing/security/worker/honest-numbers/...). Non-empty = skip.
            if _forbidden_path_hits(rel):
                continue
            seen += 1
            if seen > AUTONOMY_SCAN_MAX_FILES:
                return
            yield rel, abs_path


# ── Anti-pattern sweep specs ─────────────────────────────────────────
# Each spec pairs a BUGGY-FORM line matcher with the class's deterministic
# transform. The `klass` MUST be an allowlist class name in the classifier so
# the resulting proposal classifies mechanical. `line_re` matches the buggy
# LINE in source; `transform(line)` returns the fixed line (search→replace is
# done on the single matched line, kept unique). A spec is dropped silently if
# its klass is not in ALLOWLIST_CLASSES (keeps the two in lock-step).
def _t_interval_literal(line: str) -> str:
    # NOW() - INTERVAL '%s days'  ->  NOW() - %s * INTERVAL '1 day'
    # Singularize the unit; multiply by the bound int.
    def repl(m):
        unit = m.group(1).lower().rstrip("s")  # days->day, hours->hour, ...
        return f"%s * INTERVAL '1 {unit}'"
    return re.sub(r"INTERVAL\s+'%s\s+(day|days|hour|hours|minute|minutes|"
                  r"month|months)'", repl, line, flags=re.IGNORECASE)


def _t_now_text_cast(line: str) -> str:
    # NOW()::TEXT  ->  NOW()   (strictly removes the cast token)
    return re.sub(r"NOW\(\)\s*::\s*TEXT", "NOW()", line, flags=re.IGNORECASE)


def _t_sqlite_datetime(line: str) -> str:
    # datetime('now', '-7 days')  ->  NOW() - INTERVAL '7 days'  (best-effort)
    # Handle the common single-modifier form; fall back to bare NOW() swap.
    m = re.search(r"datetime\(\s*['\"]now['\"]\s*,\s*['\"]([+-]?\d+)\s+"
                  r"(day|days|hour|hours|minute|minutes|month|months)['\"]\s*\)",
                  line, flags=re.IGNORECASE)
    if m:
        n = abs(int(m.group(1)))
        unit = m.group(2).lower()
        if not unit.endswith("s"):
            unit += "s"
        return line[:m.start()] + f"NOW() - INTERVAL '{n} {unit}'" + line[m.end():]
    # datetime('now')  ->  NOW()
    return re.sub(r"datetime\(\s*['\"]now['\"]\s*\)", "NOW()", line,
                  flags=re.IGNORECASE)


def _t_bool_is_active(line: str) -> str:
    # COALESCE(is_active, 1) = 1  ->  is_active IS TRUE
    return re.sub(r"COALESCE\(\s*([A-Za-z_][A-Za-z0-9_.]*\.)?is_active\s*,\s*1\s*\)"
                  r"\s*=\s*1",
                  lambda m: f"{m.group(1) or ''}is_active IS TRUE",
                  line, flags=re.IGNORECASE)


def _t_tz_naive_utcnow(line: str) -> str:
    # datetime.utcnow()  ->  datetime.now(timezone.utc)   (tz-aware)
    line = re.sub(r"datetime\.datetime\.utcnow\(\)",
                  "datetime.datetime.now(timezone.utc)", line)
    line = re.sub(r"datetime\.utcnow\(\)",
                  "datetime.now(timezone.utc)", line)
    return line


def _t_immutable_index(line: str) -> str:
    # DATE(<col>) inside a CREATE INDEX line -> (<col> AT TIME ZONE 'UTC')::date
    return re.sub(r"\bDATE\s*\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\)",
                  lambda m: f"({m.group(1)} AT TIME ZONE 'UTC')::date",
                  line, flags=re.IGNORECASE)


# `line_re` is the BUGGY-form line detector. `guard` (optional) is an extra
# predicate the WHOLE line must satisfy (e.g. immutable_index only fires on a
# CREATE INDEX line). `klass` ties back to the allowlist.
_SWEEP_SPECS = [
    {
        "klass": "interval_literal",
        "line_re": re.compile(r"INTERVAL\s+'%s\s+(?:day|days|hour|hours|"
                              r"minute|minutes|month|months)'", re.IGNORECASE),
        "guard": None,
        "transform": _t_interval_literal,
    },
    {
        "klass": "now_text_cast",
        "line_re": re.compile(r"NOW\(\)\s*::\s*TEXT", re.IGNORECASE),
        "guard": None,
        "transform": _t_now_text_cast,
    },
    {
        "klass": "sqlite_datetime_on_pg",
        "line_re": re.compile(r"datetime\(\s*['\"]now['\"]", re.IGNORECASE),
        "guard": None,
        "transform": _t_sqlite_datetime,
    },
    {
        "klass": "bool_is_active",
        "line_re": re.compile(r"COALESCE\([^)]*is_active\s*,\s*1\s*\)\s*=\s*1",
                              re.IGNORECASE),
        "guard": None,
        "transform": _t_bool_is_active,
    },
    # r-autonomy-safety (2026-06-18): tz_naive_utcnow is DELIBERATELY EXCLUDED
    # from the blind proactive sweep. Bare datetime.utcnow() is NOT a bug — it's
    # used ~600x in this codebase — and swapping it to datetime.now(timezone.utc)
    # changes naive→aware, which breaks callers that compare against naive
    # datetimes or naive DB columns. It's only a bug in the narrow "utcnow() minus
    # a tz-aware value" case, which a line regex can't tell apart. The class stays
    # in the classifier's allowlist for context-bearing proposals, but the blind
    # sweep must not propose 600 risky swaps. (Found via the dormant dry preview.)
    {
        "klass": "immutable_index",
        "line_re": re.compile(r"\bDATE\s*\(", re.IGNORECASE),
        # Only a CREATE INDEX line (the classifier's immutable_index search
        # requires CREATE INDEX ... DATE(); a bare DATE() in a SELECT is fine).
        "guard": re.compile(r"CREATE\s+INDEX", re.IGNORECASE),
        "transform": _t_immutable_index,
    },
]


def _allowed_classes() -> set:
    """The allowlist class names the classifier currently gates on. A sweep
    spec whose klass isn't here is dropped (keeps sweep ⊆ allowlist)."""
    try:
        from routes.brain_mechanical_classifier import ALLOWLIST_CLASSES
        return {c["klass"] for c in ALLOWLIST_CLASSES}
    except Exception:
        return set()


# r-autonomy-safety: these classes are only BUGS against Postgres. In a genuine
# SQLite file datetime('now',…)/is_active=1 are CORRECT, so the sweep must skip
# them when the file touches sqlite3 (the line alone can't tell which DB).
_SQLITE_AMBIGUOUS_CLASSES = {"sqlite_datetime_on_pg", "bool_is_active"}


def _candidate_fixes_in_text(rel_path: str, text: str) -> list:
    """Find every unique-in-file mechanical-fix candidate in one file's text.

    For each sweep spec, scan line-by-line; when the buggy form matches, build
    (search_text, replace_text) from the single matched LINE (trimmed) and the
    spec's deterministic transform. Keep ONLY if:
      · the transform actually changed the line (replace != search), and
      · search_text occurs EXACTLY ONCE in the whole file (uniqueness — the
        classifier needs it), and
      · search_text is non-trivial (>= 10 chars, matching MECH_MIN_SEARCH_CHARS).
    Returns [{file_path, search_text, replace_text, klass, rationale}]."""
    allowed = _allowed_classes()
    out = []
    seen_search = set()   # de-dup identical search lines within this file
    lines = text.split("\n")
    # SQLite-ambiguous classes are correct in a real SQLite file → skip them when
    # this file touches sqlite3, or we'd mis-"fix" working code into broken SQL.
    file_uses_sqlite = ("import sqlite3" in text) or ("sqlite3.connect" in text)
    for spec in _SWEEP_SPECS:
        if spec["klass"] not in allowed:
            continue
        if file_uses_sqlite and spec["klass"] in _SQLITE_AMBIGUOUS_CLASSES:
            continue
        guard = spec.get("guard")
        for raw in lines:
            line = raw.rstrip("\n")
            if not spec["line_re"].search(line):
                continue
            if guard is not None and not guard.search(line):
                continue
            search_text = line.strip()
            if len(search_text) < 10:
                continue
            if search_text in seen_search:
                continue
            try:
                replace_text = spec["transform"](search_text)
            except Exception:
                continue
            if not replace_text or replace_text == search_text:
                continue
            # Uniqueness: the trimmed search line must occur exactly once in
            # the file (the classifier + the writer both require this).
            if text.count(search_text) != 1:
                continue
            seen_search.add(search_text)
            out.append({
                "file_path": rel_path,
                "search_text": search_text,
                "replace_text": replace_text,
                "klass": spec["klass"],
                "rationale": f"proactive antipattern sweep: {spec['klass']}",
            })
    return out


# ── DB: existing-proposal dedup + insert ─────────────────────────────
def _existing_proposal_keys() -> set:
    """Return the set of (file_path, search_text) already in
    brain_proposed_code_fixes that we must NOT re-propose: any row that is
    already pr_opened / resolved / carries a pr_url, PLUS every open row (so a
    still-open proposal isn't duplicated). Best-effort; on any DB failure
    returns an EMPTY set AND a flag so the caller can fail CLOSED (skip inserts)
    rather than risk a duplicate flood. Tests monkeypatch this."""
    keys = set()
    try:
        import psycopg2
    except Exception:
        return keys
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return keys
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("ALTER TABLE brain_proposed_code_fixes "
                                "ADD COLUMN IF NOT EXISTS pr_url TEXT;")
                    conn.commit()
                except Exception:
                    conn.rollback()
            with conn.cursor() as cur:
                # Dedup against EVERYTHING non-rejected: an open row (don't
                # duplicate), and especially anything already pr_opened /
                # resolved / with a pr_url (don't re-open a fixed thing).
                cur.execute(
                    "SELECT file_path, search_text FROM brain_proposed_code_fixes "
                    "WHERE COALESCE(status,'proposed') <> 'rejected'")
                for fp, st in cur.fetchall():
                    keys.add((fp or "", st or ""))
        return keys
    except Exception:
        return keys


def _insert_proposal(cand: dict):
    """INSERT one swept candidate into brain_proposed_code_fixes mirroring
    brain_v2_layer5's insert (loop_name/file_path/search_text/replace_text/
    rationale/confidence/model/status). ON CONFLICT on the (loop_name,
    file_path, search_text) UNIQUE key DO NOTHING (idempotent). Returns the
    new row id, or None when the conflict short-circuited / on failure. Tests
    monkeypatch this."""
    try:
        import psycopg2
    except Exception:
        return None
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brain_proposed_code_fixes "
                "(loop_name, file_path, search_text, replace_text, rationale, "
                " confidence, model, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'proposed') "
                "ON CONFLICT (loop_name, file_path, search_text) DO NOTHING "
                "RETURNING id",
                (LOOP_NAME, cand["file_path"], cand["search_text"],
                 cand["replace_text"], cand["rationale"],
                 SCAN_CONFIDENCE, SCAN_MODEL),
            )
            row = cur.fetchone()
            conn.commit()
        return row[0] if row else None
    except Exception:
        note_swallowed_write("brain_proposed_code_fixes", where="brain_autonomy_loop._insert_proposal")
        return None


# ════════════════════════════════════════════════════════════════════
#  (1) PROACTIVE — sweep the source tree for known anti-patterns
# ════════════════════════════════════════════════════════════════════
def scan_known_antipatterns(max_new: int | None = None) -> dict:
    """Walk the app's OWN Python source, find known buggy-form anti-patterns,
    build a correct mechanical (search→replace) proposal for each unique hit,
    DEDUP against existing proposals, and INSERT up to `max_new` NEW ones.

    Returns {created:[...], candidates, deduped, files_scanned, capped}.
    Never raises — a per-file read failure is skipped. WRITE-bearing: callers
    must only invoke this when autonomy_enabled() (autonomy_tick enforces that)."""
    cap = AUTONOMY_MAX_NEW_PROPOSALS if max_new is None else int(max_new)
    cap = max(0, cap)

    existing = _existing_proposal_keys()
    root = _repo_root()

    candidates = []
    files_scanned = 0
    for rel, abs_path in _iter_py_files(root):
        files_scanned += 1
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            continue
        # Cheap pre-filter: only run the (modest) per-line scan on files that
        # contain at least one of the buggy fragments.
        try:
            cands = _candidate_fixes_in_text(rel, text)
        except Exception:
            cands = []
        candidates.extend(cands)

    # Dedup against existing proposals (same file_path + search_text).
    deduped = 0
    fresh = []
    for c in candidates:
        if (c["file_path"], c["search_text"]) in existing:
            deduped += 1
            continue
        fresh.append(c)

    created = []
    capped = False
    for c in fresh:
        if len(created) >= cap:
            capped = True
            break
        pid = _insert_proposal(c)
        if pid is not None:
            created.append({
                "id": pid,
                "file_path": c["file_path"],
                "klass": c["klass"],
                "search_text": c["search_text"],
                "replace_text": c["replace_text"],
            })
        # pid None = ON CONFLICT short-circuit (already present) → silently skip.

    return {
        "created": created,
        "proposals_created": len(created),
        "candidates": len(candidates),
        "deduped": deduped,
        "files_scanned": files_scanned,
        "capped": capped,
        "rate_cap": cap,
    }


def preview_scan(max_new: int | None = None) -> dict:
    """READ-ONLY dry summary of what scan_known_antipatterns WOULD do — no DB
    writes (no insert; dedup read is best-effort + read-only). Used by the
    disabled-gate preview so the OFF path proves it makes zero writes."""
    cap = AUTONOMY_MAX_NEW_PROPOSALS if max_new is None else int(max_new)
    existing = _existing_proposal_keys()
    root = _repo_root()
    candidates = []
    files_scanned = 0
    for rel, abs_path in _iter_py_files(root):
        files_scanned += 1
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            continue
        try:
            candidates.extend(_candidate_fixes_in_text(rel, text))
        except Exception:
            pass
    would_create = [c for c in candidates
                    if (c["file_path"], c["search_text"]) not in existing][:cap]
    return {
        "files_scanned": files_scanned,
        "candidates": len(candidates),
        "would_create": len(would_create),
        "rate_cap": cap,
        "sample": [{"file_path": c["file_path"], "klass": c["klass"]}
                   for c in would_create],
    }


# ════════════════════════════════════════════════════════════════════
#  (2) REACTIVE — captured runtime/DB errors → tracked findings
# ════════════════════════════════════════════════════════════════════
def _grouped_runtime_errors() -> list:
    """Read the brain's in-memory WARNING+ runtime-error capture (grouped by
    normalized pattern). Returns a list of dicts with at least
    {pattern, count, level, logger, sample}. Best-effort; [] on any failure.
    Tests monkeypatch this."""
    try:
        from routes.brain_runtime_errors import grouped_runtime_patterns
    except Exception:
        return []
    try:
        return grouped_runtime_patterns() or []
    except Exception:
        return []


def _filed_finding_keys() -> set:
    """The set of issue strings already filed as runtime_error findings, so we
    don't re-file an already-tracked pattern. Best-effort; {} on failure.
    Tests monkeypatch this."""
    keys = set()
    try:
        import psycopg2
    except Exception:
        return keys
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return keys
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT issue FROM brain_findings "
                "WHERE issue LIKE 'runtime_error:%%' "
                "  AND COALESCE(status,'open') NOT IN ('resolved','wont_fix','dismissed')")
            for (issue,) in cur.fetchall():
                keys.add(issue or "")
        return keys
    except Exception:
        return keys


def runtime_errors_to_findings(min_count: int | None = None) -> dict:
    """Turn captured runtime/DB errors (which the brain's HTTP/heal detectors
    are blind to) into tracked brain_findings.

    For each grouped pattern seen >= min_count that is NOT already filed,
    upsert_brain_finding(issue=f"runtime_error:{normalized}", url="dchub://runtime",
    count=n, detail=sample+level+logger, detector="autonomy_runtime").

    Returns {filed:[...], findings_filed, examined, skipped_below_min,
    skipped_already_filed}. WRITE-bearing — only call when autonomy_enabled()."""
    threshold = (AUTONOMY_RUNTIME_MIN_COUNT if min_count is None
                 else int(min_count))
    groups = _grouped_runtime_errors()
    already = _filed_finding_keys()

    examined = len(groups)
    below = 0
    pre_filed = 0
    to_file = []
    for g in groups:
        try:
            n = int(g.get("count") or 0)
        except Exception:
            n = 0
        if n < threshold:
            below += 1
            continue
        normalized = (g.get("pattern") or "").strip()
        if not normalized:
            below += 1
            continue
        issue = f"runtime_error:{normalized}"[:200]
        if issue in already:
            pre_filed += 1
            continue
        detail = (f"sample={str(g.get('sample') or '')[:300]} | "
                  f"level={g.get('level')} | logger={g.get('logger')}")
        to_file.append({"issue": issue, "count": n, "detail": detail})

    filed = []
    if to_file:
        filed = _write_findings(to_file)

    return {
        "filed": filed,
        "findings_filed": len(filed),
        "examined": examined,
        "skipped_below_min": below,
        "skipped_already_filed": pre_filed,
        "min_count": threshold,
    }


def _write_findings(items: list) -> list:
    """Open ONE DB transaction and upsert every finding via the canonical
    writer (upsert_brain_finding). Returns the list of {issue, outcome} that
    were inserted/updated. Best-effort; [] on DB failure. Tests monkeypatch
    this (so the reactive path needs no real DB)."""
    try:
        import psycopg2
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception:
        return []
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return []
    filed = []
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            for it in items:
                try:
                    outcome = upsert_brain_finding(
                        cur, issue=it["issue"], url="dchub://runtime",
                        count=it["count"], detail=it["detail"],
                        detector="autonomy_runtime")
                    if outcome in ("inserted", "updated"):
                        filed.append({"issue": it["issue"], "outcome": outcome})
                except Exception:
                    pass
            conn.commit()
        return filed
    except Exception:
        return []


def preview_reactive(min_count: int | None = None) -> dict:
    """READ-ONLY dry summary of runtime_errors_to_findings — no DB writes."""
    threshold = (AUTONOMY_RUNTIME_MIN_COUNT if min_count is None
                 else int(min_count))
    groups = _grouped_runtime_errors()
    already = _filed_finding_keys()
    would = []
    for g in groups:
        try:
            n = int(g.get("count") or 0)
        except Exception:
            n = 0
        if n < threshold:
            continue
        issue = f"runtime_error:{(g.get('pattern') or '').strip()}"[:200]
        if issue == "runtime_error:" or issue in already:
            continue
        would.append({"issue": issue, "count": n})
    return {"examined": len(groups), "would_file": len(would),
            "min_count": threshold, "sample": would[:5]}


# ════════════════════════════════════════════════════════════════════
#  (c) DRAFT PRs for classified-mechanical open proposals
# ════════════════════════════════════════════════════════════════════
def _open_draft_prs_for_open_proposals() -> dict:
    """Classify the OPEN proposals + open DRAFT PRs for the mechanical ones via
    the existing rate-capped + token-gated writer (open_mechanical_draft_prs,
    apply=True). The writer itself is the safety boundary: DRAFT-only, re-
    classifies + re-verifies against live main, and no-ops without
    DCHUB_L22_REAL_PR + a token. Returns the writer's result dict."""
    from routes.brain_draft_pr_writer import open_mechanical_draft_prs

    rows, err = _open_proposal_rows()
    if err:
        return {"error": err, "opened": [], "previewed": [], "skipped": []}
    return open_mechanical_draft_prs(rows, apply=True)


def _open_proposal_rows():
    """Fetch the OPEN proposals, leverage-ranked. Returns (rows, err).

    Factored out of _open_draft_prs_for_open_proposals so the mechanical lane
    and the review lane see the SAME rows in the SAME order — two divergent
    fetches would let a proposal be classified differently by each lane."""
    from routes.brain_mechanical_classifier import _fetch_open_proposals

    rows, err = _fetch_open_proposals(include_resolved=False, limit=200)
    if err:
        return [], err

    # ── SELF-AWARE work selection (BRAIN_WORK_SELECTOR, default ON) ───────
    # Re-ORDER the open proposals by evidence-weighted leverage BEFORE the
    # writer's per-run rate cap slices them, so the small budget is spent on
    # the highest-leverage, most-likely-to-LAND fixes (down-weighting fix-
    # classes with a poor historical resolve-rate, with a soft-greedy floor so
    # an unproven class is still explored). RANK-ONLY: rank_work returns a
    # permutation — it NEVER drops a candidate; lower-ranked work is deferred to
    # a future run, not discarded. On ANY error we keep the EXISTING scan order
    # (rank_work itself is fail-safe; this try/except is belt-and-suspenders so
    # a selector hiccup can never crash a tick).
    if os.environ.get("BRAIN_WORK_SELECTOR", "1") not in (
            "0", "false", "False", "off"):
        try:
            from routes.brain_work_selector import rank_work
            ranked = rank_work(rows)
            if isinstance(ranked, list) and len(ranked) == len(rows):
                rows = ranked
        except Exception:
            pass  # fall back to the existing scan order

    return rows, None


# ════════════════════════════════════════════════════════════════════
#  (d) Reconcile already-fixed proposals
# ════════════════════════════════════════════════════════════════════
def _reconcile_fixed_proposals() -> dict:
    """Reuse brain_v2_layer5's reconcile logic to mark open proposals
    'resolved' when the bug they diagnosed is already gone from source. The
    layer5 endpoint is a Flask view; we drive its underlying per-row check
    (_proposal_bug_gone) directly over the open set so this works inside the
    orchestrator (no request context). Returns {checked, resolved}."""
    try:
        import psycopg2
        import psycopg2.extras
        from routes.brain_v2_layer5 import _proposal_bug_gone
    except Exception as e:
        return {"error": f"import_failed: {str(e)[:120]}",
                "checked": 0, "resolved": 0}
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return {"error": "no_db_url", "checked": 0, "resolved": 0}
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("ALTER TABLE brain_proposed_code_fixes "
                                "ADD COLUMN IF NOT EXISTS pr_url TEXT;")
                    conn.commit()
                except Exception:
                    conn.rollback()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, loop_name, file_path, search_text,
                           rationale, status, changes_json
                      FROM brain_proposed_code_fixes
                     WHERE COALESCE(status, 'proposed')
                           IN ('proposed', 'unfixable_by_sonnet')
                       AND pr_url IS NULL
                     ORDER BY proposed_at DESC
                     LIMIT 200
                """)
                rows = [dict(r) for r in cur.fetchall()]
            resolved_ids = []
            for row in rows:
                try:
                    gone, _reason = _proposal_bug_gone(row)
                except Exception:
                    gone = False
                if gone:
                    resolved_ids.append(row.get("id"))
            applied = 0
            if resolved_ids:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE brain_proposed_code_fixes
                           SET status = 'resolved', reviewed_at = NOW(),
                               reviewer_note = COALESCE(reviewer_note, '')
                                   || ' [autonomy-reconcile: bug gone in source]'
                         WHERE id = ANY(%s)
                           AND COALESCE(status, 'proposed')
                               IN ('proposed', 'unfixable_by_sonnet')
                         RETURNING id
                    """, (resolved_ids,))
                    applied = len(cur.fetchall())
                conn.commit()
        return {"checked": len(rows), "resolved": applied}
    except Exception as e:
        return {"error": str(e)[:160], "checked": 0, "resolved": 0}


# ════════════════════════════════════════════════════════════════════
#  THE ORCHESTRATOR
# ════════════════════════════════════════════════════════════════════
# Last-tick state (in-memory; surfaced by /status). Reset per process.
_LAST_TICK = {"ts": None, "summary": None, "disabled": None}


def _record_tick(summary: dict, disabled: bool):
    try:
        _LAST_TICK["ts"] = datetime.now(timezone.utc).isoformat()
        _LAST_TICK["summary"] = summary
        _LAST_TICK["disabled"] = disabled
    except Exception:
        pass


def autonomy_tick(dry_run: bool = False) -> dict:
    """One autonomy tick. Idempotent + safe to run every 30 min.

    GATE (FIRST, before ANY write): if BRAIN_AUTONOMY_ENABLED != '1' →
    return {disabled:true, preview:...} after a READ-ONLY dry summary. ZERO
    DB / GitHub writes on this path.

    When enabled, run in order, each fault-isolated:
      (a) runtime_errors_to_findings   — reactive
      (b) scan_known_antipatterns      — proactive
      (c) open DRAFT PRs for the mechanical open proposals (apply=True)
      (d) reconcile already-fixed proposals
    Returns a summary {enabled, findings_filed, proposals_created,
    draft_prs_opened, reconciled, ...}."""
    # ── OFF GATE — the first check, before any write. ?dry_run=1 forces this
    #    read-only preview path even while armed (safe preview, zero writes). ──
    if dry_run or not autonomy_enabled():
        # READ-ONLY preview only — never writes.
        preview = {"reactive": None, "proactive": None}
        try:
            preview["reactive"] = preview_reactive()
        except Exception as e:
            preview["reactive"] = {"error": str(e)[:120]}
        try:
            preview["proactive"] = preview_scan()
        except Exception as e:
            preview["proactive"] = {"error": str(e)[:120]}
        out = {
            "ok": True,
            "disabled": not autonomy_enabled(),
            "dry_run": dry_run,
            "autonomy_enabled": autonomy_enabled(),
            "automerge_enabled": automerge_enabled(),
            "as_of": datetime.now(timezone.utc).isoformat(),
            "preview": preview,
            "note": ("BRAIN_AUTONOMY_ENABLED != 1 — dormant. No proposals "
                     "created, no PRs opened, no findings written. The above "
                     "is a read-only dry preview of what a tick WOULD do."),
        }
        _record_tick(out, disabled=True)
        return out

    # ── ENABLED: run the four stages, each fault-isolated. ───────────
    summary = {
        "ok": True,
        "disabled": False,
        "autonomy_enabled": True,
        "automerge_enabled": automerge_enabled(),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }

    # (a) REACTIVE — runtime errors → findings.
    try:
        reactive = runtime_errors_to_findings()
    except Exception as e:
        reactive = {"error": str(e)[:160], "findings_filed": 0}
    summary["reactive"] = reactive
    summary["findings_filed"] = reactive.get("findings_filed", 0)

    # (b) PROACTIVE — antipattern sweep → proposals.
    try:
        proactive = scan_known_antipatterns()
    except Exception as e:
        proactive = {"error": str(e)[:160], "proposals_created": 0}
    summary["proactive"] = proactive
    summary["proposals_created"] = proactive.get("proposals_created", 0)

    # (c) DRAFT PRs for the mechanical open proposals.
    try:
        prs = _open_draft_prs_for_open_proposals()
    except Exception as e:
        prs = {"error": str(e)[:160], "opened": []}
    summary["draft_prs"] = prs
    summary["draft_prs_opened"] = len(prs.get("opened", []) or [])

    # (c2) REVIEW LANE — the proposals whose ONLY blocker is the absence of a
    # named transform class. Without this the tick opens nothing at all: on
    # 2026-08-19 every one of ~26 open proposals was skipped not_mechanical,
    # dominated by "no allowlist transform class matched", so draft_prs.opened
    # was [] every tick and the automerge pass downstream had nothing to
    # evaluate. These open as DRAFT `brain/review-*` PRs a human merges — the
    # automerge pass is hard-scoped to `brain/autofix-*` and can never take
    # them. Kill switch BRAIN_REVIEW_LANE_ENABLED=0; backpressure is on the
    # count of OPEN review PRs, not on time.
    try:
        from routes.brain_review_lane import open_review_draft_prs
        _rev_rows, _rev_err = _open_proposal_rows()
        rev = ({"error": _rev_err, "opened": []} if _rev_err
               else open_review_draft_prs(_rev_rows, apply=True))
    except Exception as e:
        rev = {"error": str(e)[:160], "opened": []}
    summary["review_prs"] = rev
    summary["review_prs_opened"] = len(rev.get("opened", []) or [])

    # (d) Reconcile already-fixed proposals.
    try:
        rec = _reconcile_fixed_proposals()
    except Exception as e:
        rec = {"error": str(e)[:160], "resolved": 0}
    summary["reconcile"] = rec
    summary["reconciled"] = rec.get("resolved", 0)

    _record_tick(summary, disabled=False)
    return summary


def last_tick() -> dict:
    """The last tick's recorded summary (in-memory). {} if none yet."""
    return dict(_LAST_TICK)


# ════════════════════════════════════════════════════════════════════
#  BLUEPRINT (lazy — built only when main.py registers it under flask)
# ════════════════════════════════════════════════════════════════════
def make_blueprint():
    """Build the Flask blueprint LAZILY so this module imports without flask in
    the test env. main.py calls this inside a guarded try/except."""
    from flask import Blueprint, jsonify, request
    from routes.brain_mechanical_classifier import _admin_ok

    bp = Blueprint("brain_autonomy", __name__)

    @bp.after_request
    def _no_store(resp):
        # These admin status/JSON endpoints carry an admin key in the URL and
        # report live autonomy/automerge state — never let CF edge-cache a 200
        # and serve stale state to operators. (CF caches `private, max-age` on
        # /api/v1/* anyway; only `no-store` is honored. Mirrors the other admin
        # brain dashboards' _no_store after_request.)
        resp.headers["Cache-Control"] = "no-store, private"
        return resp

    @bp.post("/api/v1/brain/autonomy/tick")
    def _tick():
        if not _admin_ok():
            return jsonify(ok=False, error="admin only",
                           hint="X-Admin-Key / X-Internal-Key header required"), 403
        # autonomy_tick() itself enforces the OFF gate FIRST (returns
        # {disabled:true, preview} with zero writes when the flag is off).
        # ?dry_run=1 forces the read-only preview path even while armed.
        dry = (request.args.get("dry_run") or "").lower() in ("1", "true", "yes")
        result = autonomy_tick(dry_run=dry)
        return jsonify(ok=True, result=result), 200

    @bp.get("/api/v1/brain/autonomy/status")
    def _status():
        if not _admin_ok():
            return jsonify(ok=False, error="admin only"), 403
        lt = last_tick()
        counts = {}
        s = lt.get("summary") or {}
        if isinstance(s, dict):
            counts = {
                "findings_filed": s.get("findings_filed"),
                "proposals_created": s.get("proposals_created"),
                "draft_prs_opened": s.get("draft_prs_opened"),
                "reconciled": s.get("reconciled"),
                # The review lane was invisible here. It is the escape valve for
                # every proposal the mechanical lane refuses, so a status page
                # that omits it cannot show whether refused work goes anywhere.
                "review_prs_opened": s.get("review_prs_opened"),
            }
        # ★ Why the queue is refused, attributed to the gate that OWNS each
        # refusal. The board previously showed only that 82 proposals cited
        # "no allowlist transform class matched", which reads as "widen the
        # allowlist" — and widening it would have released exactly 0, because
        # every one of those 82 also trips another gate. Read
        # `would_release_if_removed` before changing any threshold.
        blockers = None
        try:
            from routes.brain_mechanical_classifier import (
                attribute_blockers, _fetch_open_proposals)
            _rows, _err = _fetch_open_proposals(include_resolved=False, limit=200)
            blockers = ({"error": _err} if _err else attribute_blockers(_rows))
        except Exception as _b_err:  # noqa: BLE001 — a diagnostic must not 500
            blockers = {"error": f"{type(_b_err).__name__}: {str(_b_err)[:120]}"}

        return jsonify(
            ok=True,
            autonomy_enabled=autonomy_enabled(),
            automerge_enabled=automerge_enabled(),
            blockers=blockers,
            last_tick=lt.get("ts"),
            last_tick_disabled=lt.get("disabled"),
            counts=counts,
            config={
                "AUTONOMY_MAX_NEW_PROPOSALS": AUTONOMY_MAX_NEW_PROPOSALS,
                "AUTONOMY_RUNTIME_MIN_COUNT": AUTONOMY_RUNTIME_MIN_COUNT,
                "allowlist_classes": sorted(_allowed_classes()),
            },
            note=("Dormant unless BRAIN_AUTONOMY_ENABLED=1. AUTONOMY on = "
                  "auto-detect + auto-open DRAFT PRs (human merges). "
                  "BRAIN_AUTOMERGE_ENABLED is a SEPARATE switch that also "
                  "auto-merges. Tick is idempotent + safe every 30 min."),
        ), 200

    return bp


def register(app) -> bool:
    """Register the autonomy blueprint on `app`. Returns True on success.
    Mirrors brain_automerge.register's guarded pattern."""
    try:
        app.register_blueprint(make_blueprint())
        return True
    except Exception:
        return False
