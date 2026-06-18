"""
brain_mechanical_classifier.py — PHASE 0 (2026-06-18). SHADOW-ONLY.

The brain stores code-fix proposals in `brain_proposed_code_fixes`
(routes/brain_v2_layer5.py) but NEVER applies them. This module is the
conservative classifier that will LATER (Phase 2) gate auto-merge —
but Phase 0 only CLASSIFIES + REPORTS, with zero risk:

  · NO autonomous merge, NO auto-apply, NO writes to prod code.
  · The GET surface is READ-ONLY (it SELECTs proposals + classifies).
  · The optional draft-PR POST is best-effort, opens DRAFT PRs only via
    the EXISTING L22 machinery, and never auto-merges. It is a no-op
    unless DCHUB_L22_REAL_PR is set AND a GH token is available.

The whole point of Phase 0 is to build a TRACK RECORD: surface which
proposals the classifier would have called "mechanical-safe" so a human
can eyeball the calls before any autonomy is granted in a later phase.

CONSERVATIVE BY DESIGN — when in doubt, human-only. A proposal is
`is_mechanical=True` ONLY when ALL of these hold:
  1. Single file + single contiguous hunk; replace differs from search;
     <= MECH_MAX_LINES (default 8) changed lines.
  2. search_text is non-trivial (>=10 chars) and — IF the target file is
     readable locally — occurs EXACTLY ONCE in the current file. (If the
     file is unreadable we do NOT block on this rule alone; we note it.)
  3. replace_text introduces NO new control-flow keyword, NO new import,
     and NO new function-call name vs search_text (token-diff check).
  4. file_path is NOT in FORBIDDEN_PATH_PATTERNS (migrations/auth/billing/
     security/PII/_worker/deploy-config/honest-numbers-fenced files).
  5. search_text matches EXACTLY ONE allowlisted transform class.
  6. confidence >= MECH_MIN_CONF (default 0.8).

Endpoints (own blueprint, admin gate mirrors brain_inspector._admin_ok):
  GET  /api/v1/brain/proposals/mechanical            — the shadow surface
  POST /api/v1/brain/proposals/mechanical/draft-prs  — gated, best-effort
"""
from __future__ import annotations

import os
import re
import json
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

try:
    from internal_auth import accepted_internal_keys
except Exception:  # pragma: no cover — keep import-safe in odd contexts
    def accepted_internal_keys():
        return set()


brain_mechanical_bp = Blueprint("brain_mechanical", __name__)


# ── Auth (mirrors routes/brain_inspector._admin_ok exactly) ──────────
_INTERNAL_KEYS = accepted_internal_keys()
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


# ── Tunables (env-driven; conservative defaults) ─────────────────────
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return default


MECH_MAX_LINES = _env_int("MECH_MAX_LINES", 8)
MECH_MIN_CONF = _env_float("MECH_MIN_CONF", 0.8)
MECH_MIN_SEARCH_CHARS = 10


# ── Forbidden paths ──────────────────────────────────────────────────
# Reuse L22's forbidden list (routes/brain_layer22_auto_code._FORBIDDEN_PATH_PATTERNS)
# + EXTEND it. L22's set: \.env /secrets/ \.github/ scripts/.*\.sh auth login
# password credential stripe.*webhook. We import it best-effort so the two
# stay in sync; if the import fails we fall back to an inline copy.
try:
    from routes.brain_layer22_auto_code import (
        _FORBIDDEN_PATH_PATTERNS as _L22_FORBIDDEN_PATHS,
    )
except Exception:  # pragma: no cover
    _L22_FORBIDDEN_PATHS = [
        r"\.env", r"/secrets/", r"\.github/", r"scripts/.*\.sh",
        r"auth", r"login", r"password", r"credential", r"stripe.*webhook",
    ]

# Files the honest-numbers fence guards (tests/test_honest_numbers.py).
# These are accuracy-load-bearing — a "mechanical" edit here could re-inflate
# a number or relax a cache floor that test locks. NEVER auto-apply to them.
# (grepped from tests/test_honest_numbers.py: the explicit os.path.join(ROOT,...)
#  targets it asserts against.)
_HONEST_NUMBERS_FENCED = [
    r"canonical_stats\.py",
    r"routes/surface_brain\.py",
    r"routes/facility_profile_page\.py",
    r"tests/test_honest_numbers\.py",
]

# DC Hub-specific extensions on top of L22's list. Conservative: any path
# touching migrations, billing/payments, security/gating/paywall, the CF
# worker, PII, or deploy config is human-only.
FORBIDDEN_PATH_PATTERNS = _L22_FORBIDDEN_PATHS + [
    # migrations / schema management
    r"migrations?/", r"alembic", r"\bschema\b", r"ALTER\s+TABLE",
    # billing / payments / webhooks
    r"stripe", r"billing", r"payment", r"checkout", r"invoice",
    r"webhook", r"subscription", r"mrr", r"\bpricing\b",
    # security / gating / paywall / auth
    r"security", r"gating", r"paywall", r"trim_?for_?trial", r"\bgate\b",
    r"internal_auth", r"\bkeys?[._/]", r"api[_-]?key", r"token",
    r"\bsession\b", r"\bcookie\b",
    # PII / credentials
    r"\bpii\b", r"\bemail\b", r"credential", r"secret",
    # the CF worker(s)
    r"_worker", r"worker\.js",
    # deploy / infra config
    r"requirements", r"Dockerfile", r"railway", r"\bdeploy\b",
    r"Procfile", r"wrangler", r"\.toml$", r"\.cfg$", r"\.ini$",
] + _HONEST_NUMBERS_FENCED

_FORBIDDEN_PATH_RE = [re.compile(p, re.IGNORECASE) for p in FORBIDDEN_PATH_PATTERNS]


def _forbidden_path_hits(path: str) -> list[str]:
    """Return the list of forbidden patterns that match `path` (empty = clean)."""
    p = path or ""
    return [pat.pattern for pat in _FORBIDDEN_PATH_RE if pat.search(p)]


# ── Allowlisted transform classes ───────────────────────────────────
# Each class is a named, proven-safe mechanical fix. `search` is a regex
# tested against the proposal's search_text; `replace` (optional) is a
# regex tested against replace_text to further confirm the shape. A
# proposal must match EXACTLY ONE class to be mechanical.
#
# Seeds derived from this repo's real before/after (git log + the brain's
# own _KNOWN_BUG_PATTERNS in brain_v2_layer5.py):
#   interval_literal:    NOW() - INTERVAL '%s days'  ->  NOW() - %s * INTERVAL '1 day'
#   tz_naive_utcnow:     datetime.utcnow()           ->  add a tz-aware guard
#   bool_is_active:      COALESCE(is_active, 1) = 1  ->  is_active IS TRUE
#   sqlite_datetime_on_pg: datetime('now', ...)      ->  NOW() - INTERVAL ...
#   now_text_cast:       NOW()::TEXT                 ->  NOW()
#   immutable_index:     DATE(<col>) in CREATE INDEX ->  (<col> AT TIME ZONE 'UTC')::date
# `allowed_new_tokens` is the small, hand-audited set of identifier/call
# names a class is PERMITTED to introduce in replace_text. Several of these
# transforms ARE a deliberate SQL-function swap (datetime(...) -> NOW(),
# DATE(...) -> ...::date), so the generic "no new call name" rule-3 check would
# otherwise make those classes impossible to ever pass. By scoping the
# exception to the matched class's audited token set, rule 3 still blocks ANY
# token outside it (an interval_literal fix still can't sneak in `subprocess(`
# or an `if`), preserving the conservative guarantee.
ALLOWLIST_CLASSES = [
    {
        "klass": "interval_literal",
        # The %s-inside-the-quoted-literal bug. INTERVAL '%s days' can't bind
        # an int safely; the fix multiplies a single-unit interval. The fix is
        # pure SQL-literal restructuring — introduces NO new call name.
        "search": re.compile(r"INTERVAL\s+'%s\s+(day|days|hour|hours|"
                             r"minute|minutes|month|months)'", re.IGNORECASE),
        "replace": re.compile(r"%s\s*\*\s*INTERVAL\s+'1\s+(day|hour|minute|month)'",
                              re.IGNORECASE),
        "allowed_new_tokens": {"INTERVAL"},
    },
    {
        "klass": "tz_naive_utcnow",
        # tz-naive utcnow() — the fix adds a tzinfo guard / swaps to
        # now(timezone.utc). search_text must reference utcnow. The audited
        # swap may add .replace(tzinfo=...) or now(timezone.utc).
        "search": re.compile(r"datetime\.utcnow\(\)|datetime\.datetime\.utcnow\(\)"),
        "replace": None,
        "allowed_new_tokens": {"replace", "now", "timezone", "utc"},
    },
    {
        "klass": "bool_is_active",
        # COALESCE(is_active, 1) = 1  ->  is_active IS TRUE  (Postgres boolean).
        # Pure operator swap; introduces no new call.
        "search": re.compile(r"is_active\s*,\s*1\s*\)\s*=\s*1"),
        "replace": re.compile(r"is_active\s+IS\s+TRUE", re.IGNORECASE),
        "allowed_new_tokens": set(),
    },
    {
        "klass": "sqlite_datetime_on_pg",
        # datetime('now', '-7 days') etc. — SQLite-ism that runs on PG only
        # by accident; fix swaps to NOW() - INTERVAL. This DELIBERATELY swaps
        # the datetime(...) call for the NOW()/INTERVAL SQL functions.
        "search": re.compile(r"datetime\(\s*['\"]now['\"]"),
        "replace": re.compile(r"NOW\(\)\s*-\s*INTERVAL", re.IGNORECASE),
        "allowed_new_tokens": {"NOW", "INTERVAL"},
    },
    {
        "klass": "now_text_cast",
        # NOW()::TEXT  ->  NOW()  (the ::TEXT cast breaks TIMESTAMPTZ columns
        # / non-IMMUTABLE comparisons). search has the cast; replace drops it
        # — strictly REMOVES a token, never adds one.
        "search": re.compile(r"NOW\(\)\s*::\s*TEXT", re.IGNORECASE),
        "replace": None,
        "allowed_new_tokens": set(),
    },
    {
        "klass": "immutable_index",
        # DATE(<col>) inside a CREATE INDEX is non-IMMUTABLE on a TIMESTAMPTZ;
        # fix is (<col> AT TIME ZONE 'UTC')::date. The audited swap drops DATE(
        # and adds the AT TIME ZONE / ::date cast (no callable function name).
        "search": re.compile(r"CREATE\s+INDEX[\s\S]*DATE\s*\(", re.IGNORECASE),
        "replace": re.compile(r"AT\s+TIME\s+ZONE\s+'UTC'\s*\)\s*::\s*date",
                              re.IGNORECASE),
        "allowed_new_tokens": set(),
    },
]


def _matching_classes(search_text: str, replace_text: str) -> list[dict]:
    """Return the allowlist class DICTS whose `search` (and `replace`, if
    specified) regex match. Conservative: when a class declares a `replace`
    regex, BOTH must match for it to count. Returns the dicts (not just names)
    so the caller can read each matched class's `allowed_new_tokens`."""
    out = []
    s = search_text or ""
    r = replace_text or ""
    for c in ALLOWLIST_CLASSES:
        if not c["search"].search(s):
            continue
        if c["replace"] is not None and not c["replace"].search(r):
            continue
        out.append(c)
    return out


# ── Token-diff check (rule 3) ────────────────────────────────────────
_CONTROL_FLOW_KW = {
    "if", "for", "while", "try", "except", "finally", "def", "class",
    "return", "lambda", "with", "yield", "raise", "elif", "else", "async",
    "await", "assert", "global", "nonlocal",
}

# A python identifier optionally followed by `(` (a call) — group 1 is the
# name, group 2 is "(" when it's a call site.
_TOKEN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b(\s*\()?")
_IMPORT_RE = re.compile(r"\bimport\b")
_WORD_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")


def _call_names(text: str) -> set[str]:
    """Identifiers that appear as a call (`name(`) in `text`."""
    names = set()
    for m in _TOKEN_RE.finditer(text or ""):
        if m.group(2):  # followed by '('
            names.add(m.group(1))
    return names


def _control_flow_kw(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text or "") if w in _CONTROL_FLOW_KW}


def _token_diff_blocks(search_text: str, replace_text: str,
                       allowed_new_tokens: set[str] | None = None) -> list[str]:
    """Return reasons the replace introduces risky NEW tokens vs search.
    Empty list = clean (no new control-flow kw, no new import, no new call).

    `allowed_new_tokens` is the matched allowlist class's hand-audited set of
    token names it is permitted to introduce (e.g. an SQL-function swap like
    datetime(...) -> NOW()). New CONTROL-FLOW keywords and new IMPORTS are
    NEVER exemptable — only call-name additions can be whitelisted, and only to
    the exact audited set for that class. With NO class matched the set is
    empty, so the check is at its strictest."""
    blocks = []
    s, r = search_text or "", replace_text or ""
    # Case-insensitive so SQL function names (NOW / now / INTERVAL / interval)
    # compare consistently with the class's audited token set.
    allowed = {t.lower() for t in (allowed_new_tokens or set())}

    new_kw = _control_flow_kw(r) - _control_flow_kw(s)
    if new_kw:
        blocks.append("adds control-flow keyword(s): " + ",".join(sorted(new_kw)))

    if _IMPORT_RE.search(r) and not _IMPORT_RE.search(s):
        blocks.append("adds an import")

    new_calls = _call_names(r) - _call_names(s)
    # control-flow keywords can read as calls (e.g. `print(`-style) but the
    # kw set already excludes them; subtract them defensively anyway.
    new_calls -= _CONTROL_FLOW_KW
    # Exempt ONLY the matched class's audited token set (SQL-function swaps).
    new_calls = {c for c in new_calls if c.lower() not in allowed}
    if new_calls:
        blocks.append("adds call name(s) not in search: " + ",".join(sorted(new_calls)))

    return blocks


# ── Local file read (for the exactly-once check, rule 2) ─────────────
def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_file(path: str):
    """Read a repo-relative file. Returns (text, ok). ok=False on any
    failure — the caller must NOT block on rule 2 alone when ok is False."""
    try:
        full = os.path.join(_repo_root(), path)
        if not os.path.exists(full):
            return "", False
        with open(full, encoding="utf-8", errors="replace") as f:
            return f.read(), True
    except Exception:
        return "", False


# ── The classifier ──────────────────────────────────────────────────
def _extract_single_change(proposal: dict):
    """Phase 0 only auto-applies SINGLE-FILE, SINGLE-HUNK changes. Pull the
    (file, search, replace) out of either proposal shape. Returns
    (file, search, replace, multi_file_block). multi_file_block is a reason
    string when the proposal spans >1 change (=> human-only), else ''."""
    raw = proposal.get("changes_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    if isinstance(raw, list) and raw:
        if len(raw) > 1:
            return None, None, None, f"multi-file change ({len(raw)} hunks)"
        ch = raw[0]
        if isinstance(ch, dict):
            return (ch.get("file") or proposal.get("file_path") or "",
                    ch.get("search") or proposal.get("search_text") or "",
                    ch.get("replace") if ch.get("replace") is not None
                    else proposal.get("replace_text") or "",
                    "")
    # Legacy single-file columns
    return (proposal.get("file_path") or "",
            proposal.get("search_text") or "",
            proposal.get("replace_text") if proposal.get("replace_text") is not None
            else "",
            "")


def classify_mechanical(proposal: dict) -> dict:
    """Classify ONE proposal. Returns
       {is_mechanical: bool, klass: str|None, reasons: [...], blocked_by: [...]}.

    Conservative — ALL gates must pass for is_mechanical=True. `reasons`
    records why each passing gate held; `blocked_by` records every failing
    gate (we evaluate ALL gates so the shadow surface shows the full picture,
    not just the first failure)."""
    reasons: list[str] = []
    blocked_by: list[str] = []

    if not isinstance(proposal, dict):
        return {"is_mechanical": False, "klass": None, "reasons": [],
                "blocked_by": ["proposal is not a dict"]}

    file_path, search_text, replace_text, multi_block = _extract_single_change(proposal)
    search_text = search_text or ""
    replace_text = replace_text or ""

    # Rule 1a: single file + single contiguous hunk.
    if multi_block:
        blocked_by.append(multi_block)
    else:
        reasons.append("single file, single hunk")

    # Rule 1b: replace differs from search.
    if search_text and replace_text and search_text != replace_text:
        reasons.append("replace differs from search")
    elif search_text == replace_text:
        blocked_by.append("replace_text equals search_text (no-op)")
    # (an empty replace IS allowed — e.g. removing a shadowed route — but an
    #  empty search is meaningless; rule 2 catches the short/empty search.)

    # Rule 1c: <= MECH_MAX_LINES changed lines. Count the larger of the two
    # sides' newline-delimited lines as the hunk size.
    s_lines = search_text.count("\n") + 1 if search_text else 0
    r_lines = replace_text.count("\n") + 1 if replace_text else 0
    changed_lines = max(s_lines, r_lines)
    if changed_lines <= MECH_MAX_LINES:
        reasons.append(f"<= {MECH_MAX_LINES} changed lines ({changed_lines})")
    else:
        blocked_by.append(f"{changed_lines} changed lines > MECH_MAX_LINES={MECH_MAX_LINES}")

    # Rule 2: non-trivial search; exactly-once in the live file (if readable).
    if len(search_text) < MECH_MIN_SEARCH_CHARS:
        blocked_by.append(f"search_text < {MECH_MIN_SEARCH_CHARS} chars "
                          f"({len(search_text)}) — too ambiguous")
    else:
        reasons.append(f"search_text non-trivial ({len(search_text)} chars)")
        text, ok = _read_file(file_path)
        if not ok:
            # Do NOT block on this rule alone — note it.
            reasons.append("note: target file unreadable locally — "
                           "exactly-once not re-verified")
        else:
            occ = text.count(search_text)
            if occ == 1:
                reasons.append("search_text occurs exactly once in live file")
            elif occ == 0:
                blocked_by.append("search_text not present in live file (stale/drifted)")
            else:
                blocked_by.append(f"search_text occurs {occ}x in live file (ambiguous)")

    # Rule 5 (computed BEFORE rule 3 so its audited token set can scope the
    # token-diff): matches EXACTLY ONE allowlist class. _matching_classes
    # returns the class dicts; we read the single match's allowed_new_tokens.
    matched = _matching_classes(search_text, replace_text)
    klass = None
    allowed_new_tokens: set[str] = set()
    if len(matched) == 1:
        klass = matched[0]["klass"]
        allowed_new_tokens = set(matched[0].get("allowed_new_tokens") or set())
        reasons.append(f"matches allowlist class: {klass}")
    elif not matched:
        blocked_by.append("no allowlist transform class matched")
    else:
        blocked_by.append("ambiguous: matched multiple allowlist classes "
                          + ",".join(c["klass"] for c in matched))

    # Rule 3: token-diff — no new control-flow kw / import / call name. The
    # matched class's audited token set scopes the call-name exemption (an
    # SQL-function swap like datetime(...)->NOW() is allowed for that class
    # ONLY). New control-flow keywords and new imports are never exemptable.
    tok_blocks = _token_diff_blocks(search_text, replace_text, allowed_new_tokens)
    if tok_blocks:
        blocked_by.extend(tok_blocks)
    else:
        reasons.append("no new control-flow / import / call introduced")

    # Rule 4: file_path not forbidden.
    fp_hits = _forbidden_path_hits(file_path)
    if fp_hits:
        blocked_by.append("forbidden path pattern(s): " + ",".join(fp_hits[:5]))
    else:
        reasons.append("path not in forbidden set")

    # Rule 6: confidence >= MECH_MIN_CONF.
    try:
        conf = float(proposal.get("confidence") or 0.0)
    except Exception:
        conf = 0.0
    if conf >= MECH_MIN_CONF:
        reasons.append(f"confidence {conf:.2f} >= {MECH_MIN_CONF}")
    else:
        blocked_by.append(f"confidence {conf:.2f} < MECH_MIN_CONF={MECH_MIN_CONF}")

    is_mechanical = (not blocked_by) and (klass is not None)
    return {
        "is_mechanical": is_mechanical,
        "klass": klass,
        "reasons": reasons,
        "blocked_by": blocked_by,
    }


def classify_open_proposals(rows) -> dict:
    """Map classify_mechanical over rows. Returns
       {total, mechanical: [...], human_only: [...]} where each entry carries
       id / file / class / reasons / blocked_by + the raw confidence."""
    mechanical, human_only = [], []
    rows = rows or []
    for row in rows:
        try:
            verdict = classify_mechanical(row)
        except Exception as e:  # never let one bad row break the surface
            verdict = {"is_mechanical": False, "klass": None, "reasons": [],
                       "blocked_by": [f"classify_error: {str(e)[:120]}"]}
        entry = {
            "id": row.get("id"),
            "file": (row.get("file_path") or ""),
            "class": verdict["klass"],
            "confidence": row.get("confidence"),
            "status": row.get("status") or "proposed",
            "reasons": verdict["reasons"],
            "blocked_by": verdict["blocked_by"],
        }
        (mechanical if verdict["is_mechanical"] else human_only).append(entry)
    return {
        "total": len(rows),
        "mechanical": mechanical,
        "human_only": human_only,
    }


# ── DB read of open proposals (read-only) ────────────────────────────
def _fetch_open_proposals(include_resolved: bool, limit: int) -> tuple[list, str]:
    """SELECT open proposals from brain_proposed_code_fixes. Returns
    (rows, error). Open set mirrors brain_v2_layer5.reconcile_proposals:
    COALESCE(status,'proposed') IN ('proposed','unfixable_by_sonnet')
    AND pr_url IS NULL. include_resolved widens to all non-rejected."""
    try:
        import psycopg2
        import psycopg2.extras
    except Exception as e:
        return [], f"psycopg2 unavailable: {e}"

    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return [], "no_db_url"

    if include_resolved:
        where = "COALESCE(status, 'proposed') <> 'rejected'"
    else:
        where = ("COALESCE(status, 'proposed') IN ('proposed', 'unfixable_by_sonnet') "
                 "AND pr_url IS NULL")
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn:
            # pr_url may be absent on very old tables — add defensively so the
            # WHERE never aborts the txn (mirrors layer5.reconcile_proposals).
            with conn.cursor() as cur:
                try:
                    cur.execute("ALTER TABLE brain_proposed_code_fixes "
                                "ADD COLUMN IF NOT EXISTS pr_url TEXT;")
                    conn.commit()
                except Exception:
                    conn.rollback()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"""
                    SELECT id, loop_name, file_path, search_text, replace_text,
                           rationale, confidence, status, changes_json
                      FROM brain_proposed_code_fixes
                     WHERE {where}
                     ORDER BY proposed_at DESC
                     LIMIT %s
                """, (limit,))
                rows = [dict(r) for r in cur.fetchall()]
        return rows, ""
    except Exception as e:
        return [], f"query_failed: {str(e)[:200]}"


# ── Endpoints ────────────────────────────────────────────────────────
@brain_mechanical_bp.get("/api/v1/brain/proposals/mechanical")
def proposals_mechanical():
    """SHADOW SURFACE (read-only, admin-gated): SELECT open proposals, run the
    Phase-0 classifier, and return the mechanical / human-only split. This is
    the Phase-0 must-have — it builds the track record with zero risk. No
    writes, no PRs, no merges. ?include_resolved=0 (default) limits to the
    open set; =1 widens to all non-rejected rows."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only",
                       hint="X-Admin-Key / X-Internal-Key header required"), 403

    include_resolved = (request.args.get("include_resolved") or "0").lower() in (
        "1", "true", "yes")
    try:
        limit = int(request.args.get("limit", "200"))
    except Exception:
        limit = 200
    limit = max(1, min(limit, 1000))

    rows, err = _fetch_open_proposals(include_resolved, limit)
    if err:
        return jsonify(ok=False, error=err,
                       as_of=datetime.now(timezone.utc).isoformat()), 200

    split = classify_open_proposals(rows)
    return jsonify(
        ok=True,
        as_of=datetime.now(timezone.utc).isoformat(),
        phase="0-shadow-only",
        include_resolved=include_resolved,
        config={
            "MECH_MAX_LINES": MECH_MAX_LINES,
            "MECH_MIN_CONF": MECH_MIN_CONF,
            "MECH_MIN_SEARCH_CHARS": MECH_MIN_SEARCH_CHARS,
            "allowlist_classes": [c["klass"] for c in ALLOWLIST_CLASSES],
        },
        total=split["total"],
        mechanical_count=len(split["mechanical"]),
        human_only_count=len(split["human_only"]),
        mechanical=split["mechanical"],
        human_only=split["human_only"],
        note=("Phase 0 = classify + surface ONLY. No auto-apply, no merge, no "
              "writes to prod code. The 2-cycle approval gate is enforced "
              "elsewhere; confidence is recorded here, not acted on."),
    ), 200


@brain_mechanical_bp.post("/api/v1/brain/proposals/mechanical/draft-prs")
def proposals_mechanical_draft_prs():
    """(Optional, gated, best-effort) Open DRAFT PRs for the mechanical
    proposals via the EXISTING L22 machinery — ONLY IF DCHUB_L22_REAL_PR is
    set AND a GH token is available. Never auto-merges. Phase 0 stubs the
    actual PR write: the L22 real-PR path is recipe-specific (route_alias_404
    / cron_if_mismatched) and does NOT yet have a generic "apply this
    search/replace" writer, so wiring it cleanly is a Phase-2 task. Here we
    return {would_open: [...]} so the track record shows what WOULD be drafted,
    and {skipped} when the flags/token are absent."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403

    real_pr = os.environ.get("DCHUB_L22_REAL_PR", "0") == "1"
    gh_token = bool((os.environ.get("GITHUB_TOKEN") or "").strip())

    try:
        limit = int(request.args.get("limit", "200"))
    except Exception:
        limit = 200
    limit = max(1, min(limit, 1000))

    rows, err = _fetch_open_proposals(include_resolved=False, limit=limit)
    if err:
        return jsonify(ok=False, error=err), 200

    split = classify_open_proposals(rows)
    would_open = [
        {"id": m["id"], "file": m["file"], "class": m["class"],
         "confidence": m["confidence"]}
        for m in split["mechanical"]
    ]

    if not real_pr or not gh_token:
        return jsonify(
            ok=True,
            skipped=("L22 real-PR disabled / no token"
                     if not (real_pr and gh_token) else None),
            real_pr_enabled=real_pr,
            gh_token_present=gh_token,
            would_open=would_open,
            note=("DCHUB_L22_REAL_PR must be 1 AND GITHUB_TOKEN set to draft. "
                  "Even then, Phase 0 stubs the write — L22's real-PR path is "
                  "recipe-specific; a generic search/replace draft-PR writer is "
                  "Phase 2."),
        ), 200

    # Flags present, but Phase 0 still STUBS the actual write (no generic
    # search/replace draft-PR writer exists in L22 yet). Surface the intent.
    return jsonify(
        ok=True,
        stubbed=True,
        real_pr_enabled=True,
        gh_token_present=True,
        would_open=would_open,
        note=("Phase 0 stub: L22 has no generic 'apply search/replace as a "
              "DRAFT PR' writer (its real-PR path is recipe-specific: "
              "route_alias_404 / cron_if_mismatched). Wiring a generic draft-PR "
              "writer is Phase 2. No PR opened, no merge."),
    ), 200
