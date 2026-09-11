"""detector_scout.py — PHASE 0 of the brain detector-supply pipeline (2026-08-05).
============================================================================
Spec: docs/brain-detector-supply-pipeline.md (merged 2026-08-05, PR #2241).

★THE CONSTRAINT THIS EXISTS TO ATTACK. The white-glove BRAIN lane
(routes/white_glove_loop_master_shell.py:246) has been critical-red ON PURPOSE
since 2026-07-30, with a measured diagnosis:

    "The six mechanical transform classes are EXHAUSTED ... only EIGHT
     proposals are blocked SOLELY by the class gate, so widening the allowlist
     is NOT the lever. Autonomy is capped by DETECTOR SUPPLY."

Adding a detector is a human job that has happened six times, ever, and
`now_text_cast` alone produced 12 merged autofix PRs. This module is the first
and cheapest step toward mechanising the noticing: scout GitHub for repos whose
CONTENT is a corpus of known-shape code transforms (codemods, semgrep/ruff rule
sets, libcst transforms), apply a DETERMINISTIC filter, record what survives.

WHAT PHASE 0 DELIBERATELY DOES NOT DO
-------------------------------------
  · NO model call. Not one. There is no LLM in this file.
  · NO proposal, NO code generation, NO PR, NO merge, NO write outside
    `detector_scout_repos`.
  · NO change to ALLOWLIST_CLASSES, to the sweep specs, or to any gate. Nothing
    here can affect what the brain proposes or merges today.
It answers exactly ONE question: is there a funnel here at all?

★THE EXIT CRITERION CAN FAIL, AND THAT IS THE POINT. >=20 repos surviving the
filter over 2 weeks. If the funnel comes back empty the query set is wrong and
the expensive stages (Reader / Extractor / Score) are premature. `status()`
reports the count against the target so that answer is legible instead of
inferred — including when the answer is "no".

SHIPS DARK. DETECTOR_SCOUT_ENABLED defaults to "0": every endpoint returns
{skipped:"disabled"} and performs zero network + zero DB writes. Turning it on
is a Railway env change, not a deploy. Mirrors brain-innovation-digest.yml.

★DEVIATION FROM THE SPEC, STATED RATHER THAN HIDDEN. The doc's filter lists a
README-exists rule and a ">=3 files matching rule|transform|codemod|fixer|check"
rule. Both need a PER-REPO fetch, which turns one tick into an N+1 sweep of up
to 30 extra API calls for data the Reader (Phase 1) has to fetch anyway. So
Phase 0 filters ONLY on fields the search payload already carries, and uses a
`corpus_signal` keyword check over name/description/topics as the free proxy.
The two dropped rules move to the Reader. This is a narrowing of Phase 0, not a
silent one — `filter_repo()` never claims to have checked a README.

ENDPOINTS (admin-gated, mirrors routes/brain_inspector._admin_ok)
  POST /api/v1/admin/detector-scout/tick    run one query, upsert, report
  GET  /api/v1/admin/detector-scout/status  read-only funnel state + exit criterion
  GET  /api/v1/admin/detector-scout/preview dry-run: filter decisions, no writes
"""
from __future__ import annotations

import os
import logging
import datetime as _dt

logger = logging.getLogger("detector_scout")

# ── Config (every default is the SAFE value) ─────────────────────────
_TARGET_SURVIVORS = int(os.environ.get("DETECTOR_SCOUT_TARGET", "20"))
_WINDOW_DAYS = int(os.environ.get("DETECTOR_SCOUT_WINDOW_DAYS", "14"))
_MIN_STARS = int(os.environ.get("DETECTOR_SCOUT_MIN_STARS", "50"))
_MAX_AGE_DAYS = int(os.environ.get("DETECTOR_SCOUT_MAX_AGE_DAYS", "180"))
_PER_PAGE = min(int(os.environ.get("DETECTOR_SCOUT_PER_PAGE", "30")), 100)

# Permissive only. A detector is DERIVED from someone else's repository, so a
# copyleft or unlicensed source is refused at the door rather than at review
# time. We take the SHAPE of a transform, never the implementation — but the
# licence gate does not depend on that promise being kept.
PERMISSIVE_LICENCES = frozenset({
    "mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "unlicense",
    "0bsd", "mit-0",
})

# The search payload carries language for the repo as a whole. A rule corpus
# lives in Python or in declarative rule files.
ALLOWED_LANGUAGES = frozenset({"python", "yaml", "toml", "starlark"})

# The free proxy for "this repo contains a corpus of code transforms", checked
# against name + description + topics (all present in the search payload).
CORPUS_KEYWORDS = (
    "codemod", "libcst", "semgrep", "ruff", "flake8", "pyupgrade", "autofix",
    "auto-fix", "lint rule", "lint-rule", "refactor", "transform", "fixer",
    "bugbear", "ast rewrite", "ast-rewrite", "modernize", "sqlfluff",
)

# One query per tick, rotated deterministically by day-of-year. Rotation beats
# firing all six per tick: it keeps a tick inside one search rate-limit bucket
# and spreads coverage without needing randomness (which would also make the
# tick non-reproducible).
SCOUT_QUERIES = (
    {"slug": "codemod", "q": "codemod language:Python"},
    {"slug": "libcst", "q": "libcst transformer language:Python"},
    {"slug": "semgrep-rules", "q": "semgrep rules"},
    {"slug": "ruff-flake8", "q": "flake8 plugin OR ruff rule language:Python"},
    {"slug": "autofix", "q": "autofix OR pyupgrade language:Python"},
    {"slug": "sql-antipattern", "q": "psycopg antipattern OR sql lint language:Python"},
)


def _enabled() -> bool:
    """Master switch. Defaults OFF — the module ships dark."""
    return (os.environ.get("DETECTOR_SCOUT_ENABLED") or "0").strip() == "1"


def _dsn() -> str:
    return (os.environ.get("DATABASE_URL")
            or os.environ.get("NEON_DATABASE_URL") or "")


def _token() -> str:
    return (os.environ.get("GITHUB_TOKEN") or "").strip()


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def pick_query(day_of_year: int) -> dict:
    """Deterministic rotation. Exposed so a test can assert the rotation covers
    every query across a year rather than trusting the modulo by eye."""
    return SCOUT_QUERIES[day_of_year % len(SCOUT_QUERIES)]


# ── Normalise ────────────────────────────────────────────────────────
def normalise_repo(item: dict) -> dict:
    """GitHub search item -> the flat shape the filter and the upsert use.

    Tolerant by design: the search payload is external data and a missing
    `license` block is normal (unlicensed repos), not an error.
    """
    lic = (item.get("license") or {})
    return {
        "full_name": (item.get("full_name") or "").strip(),
        "html_url": item.get("html_url") or "",
        "description": (item.get("description") or "")[:2000],
        "stars": int(item.get("stargazers_count") or 0),
        "language": (item.get("language") or "").strip(),
        "licence": ((lic.get("spdx_id") or "").strip().lower() or None),
        "pushed_at": item.get("pushed_at") or "",
        "archived": bool(item.get("archived")),
        "topics": [str(t).lower() for t in (item.get("topics") or [])],
    }


def _parse_ts(s: str):
    """Parse a GitHub ISO-8601 timestamp to an AWARE datetime, or None.

    ★Returns None rather than a naive datetime on anything unexpected. A naive
    value compared against an aware _utcnow() raises TypeError, and the caller
    treats None as "cannot establish age" -> reject as stale, which is the
    conservative direction.
    """
    if not s:
        return None
    try:
        txt = s.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(txt)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def _corpus_signal(repo: dict) -> bool:
    hay = " ".join([
        repo.get("full_name") or "",
        repo.get("description") or "",
        " ".join(repo.get("topics") or []),
    ]).lower()
    return any(k in hay for k in CORPUS_KEYWORDS)


# ── Filter (deterministic; NO model, NO network) ─────────────────────
def filter_repo(repo: dict, now=None) -> tuple:
    """Return (keep: bool, reason: str).

    Records the ONE rule that fired, in a fixed order, so the reject histogram
    in status() is a real distribution and not an artefact of evaluation order
    changing between releases. `reason` is 'keep' when nothing fired.

    ★Never inspects a README. The doc's README and file-corpus rules need a
    per-repo fetch and belong to the Reader — see the module docstring.
    """
    now = now or _utcnow()
    if not (repo.get("full_name") or "").strip():
        return False, "no_full_name"
    if repo.get("archived"):
        return False, "archived"
    lic = repo.get("licence")
    if not lic:
        return False, "licence:none"
    if lic not in PERMISSIVE_LICENCES:
        return False, f"licence:{lic}"
    pushed = _parse_ts(repo.get("pushed_at") or "")
    if pushed is None:
        return False, "stale:unknown_pushed_at"
    if (now - pushed).days > _MAX_AGE_DAYS:
        return False, "stale"
    lang = (repo.get("language") or "").lower()
    if lang not in ALLOWED_LANGUAGES:
        return False, f"language:{lang or 'none'}"
    if int(repo.get("stars") or 0) < _MIN_STARS:
        return False, "too_few_stars"
    if not _corpus_signal(repo):
        return False, "no_corpus_signal"
    return True, "keep"


# ── GitHub search ────────────────────────────────────────────────────
def github_search(query: str, per_page: int = None) -> tuple:
    """Return (items, error). NEVER raises — a scout that crashes the tick is
    worse than a scout that reports it found nothing this round.

    Unauthenticated search is 10 req/min vs 30 authenticated; one query per
    tick keeps us inside either.

    ★`requests`, not urllib.request.urlopen — scripts/regression_lint.py blocks
    the latter (`urllib-request-on-railway`). It also gets us an explicit
    status_code, so a 403 rate-limit is REPORTED as a rate-limit rather than
    surfacing as a generic HTTPError string.
    """
    import requests

    per_page = per_page or _PER_PAGE
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "dchub-detector-scout/0.1",
    }
    tok = _token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        r = requests.get(
            "https://api.github.com/search/repositories",
            params={"q": query, "per_page": str(per_page),
                    "sort": "updated", "order": "desc"},
            headers=headers, timeout=20,
        )
    except Exception as e:  # DNS, connect timeout, TLS
        return [], f"{type(e).__name__}:{str(e)[:160]}"
    if r.status_code != 200:
        # 403 with a zero remaining-quota header is the rate limit, which is a
        # DIFFERENT condition from a bad query and must not read as one.
        remaining = r.headers.get("X-RateLimit-Remaining")
        if r.status_code == 403 and remaining == "0":
            return [], "rate_limited:x-ratelimit-remaining=0"
        return [], f"http_{r.status_code}:{r.text[:160]}"
    try:
        payload = r.json()
    except Exception as e:
        return [], f"bad_json:{type(e).__name__}:{str(e)[:120]}"
    return (payload.get("items") or []), ""


# ── Persistence ──────────────────────────────────────────────────────
def _connect():
    """psycopg2 connection with autocommit ON.

    ★NOT `with psycopg2.connect(...)`. psycopg2's connection context manager is
    a TRANSACTION manager, so one failing statement aborts the transaction and
    every later statement dies with InFailedSqlTransaction — the bug fixed in
    brain_capability_radar on 2026-08-01 (#2071). Autocommit keeps each upsert
    independent, which is what "one bad row must not lose the batch" requires.
    """
    import psycopg2
    dsn = _dsn()
    if not dsn:
        return None
    c = psycopg2.connect(dsn, sslmode="require", connect_timeout=8)
    c.autocommit = True
    return c


_UPSERT_SQL = """
INSERT INTO detector_scout_repos
    (full_name, html_url, description, stars, language, licence,
     pushed_at, status, reject_reason, query_slug, first_seen_at, last_seen_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING, NOW())
ON CONFLICT (full_name) DO UPDATE SET
    html_url      = EXCLUDED.html_url,
    description   = EXCLUDED.description,
    stars         = EXCLUDED.stars,
    language      = EXCLUDED.language,
    licence       = EXCLUDED.licence,
    pushed_at     = EXCLUDED.pushed_at,
    status        = EXCLUDED.status,
    reject_reason = EXCLUDED.reject_reason,
    query_slug    = EXCLUDED.query_slug,
    last_seen_at  = NOW()
"""


def persist(rows: list, query_slug: str) -> dict:
    """Upsert the filtered rows. Returns counts; never raises.

    first_seen_at is preserved by the DO UPDATE (it is not in the SET list), so
    the 14-day exit-criterion window measures genuine FIRST discovery and is not
    reset every time a repo is re-surfaced by a later tick.
    """
    from routes._swallowed_writes import note_swallowed_write

    out = {"written": 0, "failed": 0, "db": True}
    conn = None
    try:
        conn = _connect()
    except Exception:
        note_swallowed_write("detector_scout_repos", "detector_scout.persist")
        conn = None
    if conn is None:
        out["db"] = False
        return out
    try:
        with conn.cursor() as cur:
            for r in rows:
                try:
                    cur.execute(_UPSERT_SQL, (
                        r["full_name"], r["html_url"], r["description"],
                        r["stars"], r["language"], r["licence"],
                        (r["pushed_at"] or None), r["status"],
                        r["reject_reason"], query_slug,
                    ))
                    out["written"] += 1
                except Exception:
                    out["failed"] += 1
                    note_swallowed_write("detector_scout_repos",
                                         "detector_scout.persist")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


# ── The tick ─────────────────────────────────────────────────────────
def scout_tick(dry_run: bool = True, search=None, now=None) -> dict:
    """Run ONE scout query, filter, and (unless dry) upsert.

    `search` is injectable so tests exercise the real filter + accounting with
    zero network. Default is the live github_search.
    """
    now = now or _utcnow()
    if not _enabled():
        return {"skipped": "disabled", "enabled": False}

    search = search or github_search
    q = pick_query(now.timetuple().tm_yday)
    items, err = search(q["q"])
    if err:
        return {"skipped": "search_failed", "enabled": True,
                "query": q["slug"], "error": err, "seen": 0}

    kept, rejects, rows = [], {}, []
    for it in items:
        repo = normalise_repo(it)
        keep, reason = filter_repo(repo, now=now)
        if keep:
            kept.append(repo["full_name"])
        else:
            rejects[reason] = rejects.get(reason, 0) + 1
        rows.append({
            **repo,
            "status": "queued" if keep else "rejected",
            "reject_reason": None if keep else reason,
        })

    result = {
        "enabled": True,
        "query": q["slug"],
        "seen": len(items),
        "kept": len(kept),
        "kept_names": kept[:20],
        "rejects": dict(sorted(rejects.items(), key=lambda kv: -kv[1])),
        "dry_run": bool(dry_run),
    }
    if dry_run:
        result["written"] = 0
        return result
    result.update(persist(rows, q["slug"]))
    return result


# ── Read-only status ─────────────────────────────────────────────────
def status_snapshot() -> dict:
    """Funnel state + the exit criterion, read-only.

    ★Reports the exit criterion against a TARGET so "the funnel is empty" is a
    visible answer rather than an absence. A Phase-0 that cannot report its own
    failure is the `weekly-shadow-audit` pattern (two weeks of green while the
    push was rejected and swallowed by `|| true`).
    """
    snap = {
        "enabled": _enabled(),
        "has_token": bool(_token()),
        "target_survivors": _TARGET_SURVIVORS,
        "window_days": _WINDOW_DAYS,
        "queries": [q["slug"] for q in SCOUT_QUERIES],
    }
    conn = None
    try:
        conn = _connect()
    except Exception:
        conn = None
    if conn is None:
        snap["db"] = False
        snap["note"] = "no DATABASE_URL — counts unavailable (not zero)"
        return snap
    snap["db"] = True
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM detector_scout_repos
                 WHERE status = 'queued'
                   AND first_seen_at >= NOW() - %s * INTERVAL '1 day'
            """, (_WINDOW_DAYS,))
            in_window = int((cur.fetchone() or [0])[0] or 0)
            cur.execute("SELECT COUNT(*) FROM detector_scout_repos")
            total = int((cur.fetchone() or [0])[0] or 0)
            cur.execute("""
                SELECT reject_reason, COUNT(*) FROM detector_scout_repos
                 WHERE status = 'rejected' AND reject_reason IS NOT NULL
                 GROUP BY reject_reason ORDER BY COUNT(*) DESC LIMIT 15
            """)
            rejects = {r[0]: int(r[1]) for r in (cur.fetchall() or [])}
        snap["survivors_in_window"] = in_window
        snap["total_seen"] = total
        snap["reject_histogram"] = rejects
        snap["exit_criterion_met"] = in_window >= _TARGET_SURVIVORS
    except Exception as e:
        snap["db"] = False
        snap["error"] = f"{type(e).__name__}:{str(e)[:160]}"
        snap["note"] = "counts unavailable (not zero)"
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return snap


# ── Blueprint (built lazily so this module imports without flask) ────
def _make_blueprint():
    from flask import Blueprint, jsonify, request
    from routes.brain_inspector import _admin_ok

    bp = Blueprint("detector_scout", __name__)

    @bp.post("/api/v1/admin/detector-scout/tick")
    def _tick():
        if not _admin_ok():
            return jsonify(ok=False, error="admin only"), 403
        # Acting requires BOTH ?act=1 AND the env flag; scout_tick re-checks
        # the flag itself, so a caller cannot reach a write by URL alone.
        body = request.get_json(silent=True) or {}
        want_act = bool(body.get("act") or request.args.get("act") == "1")
        return jsonify(ok=True, result=scout_tick(dry_run=not want_act)), 200

    @bp.get("/api/v1/admin/detector-scout/status")
    def _status():
        if not _admin_ok():
            return jsonify(ok=False, error="admin only"), 403
        return jsonify(ok=True, status=status_snapshot()), 200

    @bp.get("/api/v1/admin/detector-scout/preview")
    def _preview():
        if not _admin_ok():
            return jsonify(ok=False, error="admin only"), 403
        return jsonify(ok=True, result=scout_tick(dry_run=True)), 200

    return bp


def register_detector_scout(app) -> None:
    """Called from main.py inside its guarded try/except, in the SAFE ZONE."""
    app.register_blueprint(_make_blueprint())
