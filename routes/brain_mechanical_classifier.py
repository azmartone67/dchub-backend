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
  POST /api/v1/brain/proposals/mechanical/draft-prs  — PHASE 2: opens DRAFT
       PRs via the generic search→replace writer in brain_draft_pr_writer.
       Default dry-run; ?apply=1 (+ DCHUB_L22_REAL_PR=1 + token) drafts.
       DRAFT only, base=main, head=branch, NEVER auto-merged.
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


# ── Auth (delegates to the shared internal_auth gate; see _admin_ok) ──
# This is the single admin gate for the whole brain autonomy surface:
# brain_autonomy_loop (/autonomy/tick), brain_automerge (/automerge/*),
# brain_smoke_regression (/smoke-regression/tick) and ~10 other modules all
# `from routes.brain_mechanical_classifier import _admin_ok`. Fix it here and
# every one of them is reconciled.
_INTERNAL_KEYS = accepted_internal_keys()
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    if not sent:
        return False
    # r-admin-gate-clean (2026-08-08): route through the shared internal_auth
    # gate FIRST. is_valid_internal_key() _clean_key()s BOTH the header AND the
    # env value (symmetric trailing-whitespace / appended-path stripping) and
    # re-reads env at REQUEST time. The 2026-08-08 secrets rotation re-pasted
    # DCHUB_ADMIN_KEY with trailing pollution; the raw import-time set below then
    # held the polluted value while `sent` was stripped, so `sent in
    # _INTERNAL_KEYS` missed and the ENTIRE brain autonomy/automerge/smoke
    # surface 403'd — even though the IDENTICAL key kept working for layer5,
    # which does a raw==raw compare (routes/brain_v2_layer5._admin_guard) and so
    # tolerates the pollution. Delegating here makes this gate accept exactly
    # what layer5 (and every other internal-keyed endpoint) accepts.
    # Pinned by tests/test_brain_admin_gate_parity.py.
    try:
        from internal_auth import require_internal_or_admin
        if require_internal_or_admin(request):
            return True
    except Exception:
        pass
    # Legacy fallback: the import-time captured set. Kept so no previously
    # accepted credential (e.g. a bare INTERNAL_KEY env var, which internal_auth
    # does not read) is silently dropped — acceptance here only ever widens.
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


# ── Per-class forbidden paths (sqlite-data guard) ────────────────────
# The `sqlite_datetime_on_pg` class is the one transform whose search text can
# appear as DATA (a SQLite→Postgres translation table) rather than executable
# SQL. db_utils.py holds exactly such a mapping (SQLITE_TO_PG_FUNC), where
# "datetime('now','-7 days')" is a DICT KEY — the source side of the swap. A
# "fix" there would Postgres-ify the key and DESTROY the translation. Belt: any
# path that IS db_utils, names a translation table, or carries "translat" in
# the path is human-only FOR THIS CLASS. We scope this to the class so the
# other transforms (interval_literal etc.) keep their broad reach.
_SQLITE_CLASS_FORBIDDEN_PATTERNS = [
    r"\bdb_utils\b",
    r"sqlite_?to_?pg",
    r"translat",  # *translate* / *translation* helpers carry mapping tables
]
_SQLITE_CLASS_FORBIDDEN_RE = [
    re.compile(p, re.IGNORECASE) for p in _SQLITE_CLASS_FORBIDDEN_PATTERNS
]


def _sqlite_class_forbidden_path_hits(path: str) -> list[str]:
    """Forbidden-path hits SPECIFIC to the sqlite_datetime_on_pg class."""
    p = path or ""
    return [pat.pattern for pat in _SQLITE_CLASS_FORBIDDEN_RE if pat.search(p)]


# ── SQLite-as-DATA context guard ─────────────────────────────────────
# Classes whose `search` can legitimately match inside a Python STRING LITERAL
# (i.e. as DATA, e.g. a key in a SQLite→PG translation table) rather than as
# executable SQL. For these, a "mechanical" rewrite of the data would silently
# corrupt a mapping. At minimum: sqlite_datetime_on_pg.
_STRING_LITERAL_RISK_CLASSES = {"sqlite_datetime_on_pg"}

# Marker that a file defines a SQLite→PG translation mapping (search occurrences
# inside such a file are almost certainly DATA, not SQL).
_TRANSLATION_MAP_MARKER_RE = re.compile(r"SQLITE_?TO_?PG", re.IGNORECASE)

# Marker that the file itself RUNS SQLite (imports sqlite3). In such a file
# datetime('now', ...) is CORRECT SQLite syntax — rewriting it to NOW()/INTERVAL
# (Postgres-only) BREAKS the real query. e.g. api_server.py uses a sqlite3 cursor.
_SQLITE3_IMPORT_RE = re.compile(r"(?m)^\s*(?:import\s+sqlite3|from\s+sqlite3\b)")


def _sqlite_data_context_block(file_path: str, search_text: str) -> str | None:
    """For the sqlite-class search, decide whether the matched SQLite syntax is
    DATA (a dict key / translation-table entry) rather than executable SQL.

    Returns a human-readable block REASON (str) when the match should be
    REJECTED, or None when the context looks like real SQL (safe to proceed).

    CONSERVATIVE: false-positives here are harmful (the brain corrupted a live
    translation table), so when the file is unreadable we BLOCK rather than
    wave the proposal through — the caller must NOT let an unverifiable
    sqlite-class match pass.
    """
    text, ok = _read_file(file_path)
    if not ok:
        return ("sqlite syntax may be DATA — target file unreadable locally, "
                "cannot verify it is executable SQL (conservative block)")

    # The file RUNS SQLite (imports sqlite3) → datetime('now', ...) is CORRECT
    # here; swapping to NOW()/INTERVAL is Postgres-only and would BREAK the real
    # SQLite query. (e.g. api_server.py — the brain proposed exactly this.)
    if _SQLITE3_IMPORT_RE.search(text):
        return ("sqlite syntax is CORRECT here: file imports sqlite3 (a real "
                "SQLite DB) — NOW()/INTERVAL is Postgres-only and would break it")

    # The file defines a SQLite→PG translation mapping anywhere → its
    # occurrences of SQLite syntax are the SOURCE side of the swap (DATA).
    if _TRANSLATION_MAP_MARKER_RE.search(text):
        return ("sqlite syntax is DATA: file defines a SQLITE_TO_PG translation "
                "mapping (rewriting it would destroy the SQLite->PG swap)")

    # The search occurs as a DICT KEY: the quoted search text immediately
    # followed by a colon, e.g.  "datetime('now','-7 days')": "(NOW() - ...)".
    # Build a tolerant regex: an opening quote, the (whitespace-flexible)
    # escaped search, a closing quote, optional ws, then a colon.
    needle = (search_text or "").strip()
    if needle:
        # Allow flexible inner whitespace (the table key may differ from the
        # proposal's search only by spaces, e.g. "'now', '-7 days'" vs
        # "'now','-7 days'"). Escape, then relax runs of whitespace.
        esc = re.escape(needle)
        esc = re.sub(r"\\?\s+", r"\\s*", esc)
        dict_key_re = re.compile(r"""["']\s*""" + esc + r"""\s*["']\s*:""")
        if dict_key_re.search(text):
            return ("sqlite syntax is DATA: search_text appears as a DICT KEY "
                    "(quoted-then-colon), not executable SQL")

    return None


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

    # Rule 4b (sqlite-data guard): the sqlite_datetime_on_pg transform is the one
    # class whose `search` can match SQLite syntax that is DATA — a key in a
    # SQLite->PG translation table (e.g. db_utils.SQLITE_TO_PG_FUNC) — rather than
    # executable SQL. Rewriting that DATA to Postgres DESTROYS the mapping. Two
    # belt-and-suspenders defenses, scoped to this class so other transforms keep
    # their reach:
    #   (i)  per-class forbidden paths (db_utils / *translat* / sqlite_to_pg);
    #   (ii) a content context-check that BLOCKS when the search occurs as a dict
    #        key or the file defines a SQLITE_TO_PG mapping — and, conservatively,
    #        BLOCKS when the file is unreadable (an unverifiable match is unsafe).
    if klass in _STRING_LITERAL_RISK_CLASSES:
        sq_path_hits = _sqlite_class_forbidden_path_hits(file_path)
        if sq_path_hits:
            blocked_by.append(
                "sqlite-data guard: forbidden translation-table path "
                + ",".join(sq_path_hits[:5]))
        try:
            ctx_block = _sqlite_data_context_block(file_path, search_text)
        except Exception as e:  # best-effort; never crash a tick — fail closed
            ctx_block = f"sqlite-data guard error (fail-closed): {str(e)[:80]}"
        if ctx_block:
            blocked_by.append("sqlite-data guard: " + ctx_block)
        if not sq_path_hits and not ctx_block:
            reasons.append("sqlite-data guard: search is executable SQL "
                           "(not a translation-table key)")

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
# ── blocker attribution ──────────────────────────────────────────────
# WHY THIS EXISTS. On 2026-08-31 the automerge board had shown `idle` for 666
# consecutive runs, and the brain's own status text explained it as proposals
# being "rejected `not_mechanical` against a 6-class SQL/datetime allowlist".
# That reads as one thing: the allowlist is too narrow, widen it.
#
# It is the wrong read, and the numbers say so plainly. Classifying all 85 open
# proposals:
#
#     82  cite "no allowlist transform class matched"
#      0  would become mechanical if that gate were removed
#
# Every one of the 82 carries at least one OTHER blocker — 47 have two, 24 have
# three, 3 have six. The most common pair is low_confidence + no_class (34),
# where the model scored its own fix at 0.55 against a 0.8 bar. Widening the
# allowlist releases nothing; it just moves the refusal one gate along.
#
# The defect was never the gate, it was the REPORTING: "no allowlist transform
# class matched" is the most-cited blocker and never the deciding one, so the
# board pointed at the one lever that changes nothing. This attributes each
# refusal to the gate that is actually holding it, and — the number that
# matters for a decision — says how many proposals each gate would release ON
# ITS OWN.
#
# Read `sole_blocker` before widening anything. A gate with sole_blocker=0 is
# not what is stopping you.
_BLOCKER_LABELS = (
    ("no_class", "allowlist transform class"),
    ("low_confidence", "MECH_MIN_CONF"),
    ("too_many_lines", "MECH_MAX_LINES"),
    ("adds_import", "adds an import"),
    ("adds_control_flow", "control-flow"),
    ("adds_new_call", "adds call name"),
)


def _blocker_key(reason: str) -> str:
    """Collapse a raw blocked_by string to a stable gate name."""
    for key, needle in _BLOCKER_LABELS:
        if needle in (reason or ""):
            return key
    return "other"


def attribute_blockers(rows) -> dict:
    """Why the open proposals are refused, and what each gate is worth.

    Returns {total, mechanical, cited, sole_blocker, blocker_counts,
    would_release_if_removed} where:

      cited                   how often each gate appears at all — the number
                              the old reporting showed, and the misleading one
      sole_blocker            proposals that gate is ALONE in refusing
      would_release_if_removed  == sole_blocker, named for the decision it
                              informs: remove this gate, release this many

    Never raises: a classifier error counts as one 'classifier_error' blocker
    rather than dropping the row, because a proposal that cannot be classified
    is still a proposal that is not moving."""
    cited, sole = {}, {}
    total = mechanical = 0
    for row in (rows or []):
        total += 1
        try:
            verdict = classify_mechanical(dict(row))
        except Exception:
            keys = {"classifier_error"}
            verdict = None
        else:
            if verdict.get("is_mechanical"):
                mechanical += 1
                continue
            keys = {_blocker_key(b) for b in (verdict.get("blocked_by") or [])}
            if not keys:
                keys = {"unexplained"}
        for k in keys:
            cited[k] = cited.get(k, 0) + 1
        if len(keys) == 1:
            k = next(iter(keys))
            sole[k] = sole.get(k, 0) + 1
    return {
        "total": total,
        "mechanical": mechanical,
        "blocked": total - mechanical,
        "cited": dict(sorted(cited.items(), key=lambda kv: -kv[1])),
        "sole_blocker": dict(sorted(sole.items(), key=lambda kv: -kv[1])),
        "would_release_if_removed": dict(sorted(sole.items(), key=lambda kv: -kv[1])),
        "note": ("`cited` counts every mention; `sole_blocker` counts refusals "
                 "that gate owns alone. Widening a gate whose sole_blocker is 0 "
                 "releases nothing."),
    }


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


@brain_mechanical_bp.post("/api/v1/brain/proposals/mechanical/test-open")
def proposals_mechanical_test_open():
    """Diagnostic (admin): drive the Phase-2 writer with an INLINE proposal (no DB
    row needed) to prove the full classify → re-verify-against-live-main → DRAFT-PR
    path. Body: {file_path, search_text, replace_text, confidence?, rationale?}.
    Default DRY-RUN; ?apply=1 opens a real DRAFT PR (still draft, never merged, and
    still subject to the writer's own classify + DCHUB_L22_REAL_PR + token gates).
    id defaults to a non-matching sentinel so the status UPDATE can't touch a real
    proposal row."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only"), 403
    body = request.get_json(silent=True) or {}
    proposal = {
        "id": body.get("id", 999999999),
        "file_path": body.get("file_path"),
        "search_text": body.get("search_text"),
        "replace_text": body.get("replace_text"),
        "confidence": float(body.get("confidence", 0.9)),
        "rationale": body.get("rationale", "inline test-open (diagnostic)"),
        "status": "proposed",
    }
    if not (proposal["file_path"] and proposal["search_text"] and proposal["replace_text"]):
        return jsonify(ok=False, error="file_path, search_text, replace_text required"), 400
    apply = (request.args.get("apply") or "0").lower() in ("1", "true", "yes")
    try:
        from routes.brain_draft_pr_writer import open_mechanical_draft_pr
    except Exception as e:  # pragma: no cover
        return jsonify(ok=False, error=f"writer_import_failed: {str(e)[:160]}"), 200
    res = open_mechanical_draft_pr(proposal, dry_run=(not apply))
    return jsonify(ok=True, apply=apply, result=res)


@brain_mechanical_bp.post("/api/v1/brain/proposals/mechanical/draft-prs")
def proposals_mechanical_draft_prs():
    """PHASE 2 (2026-06-18): open DRAFT PRs for the mechanical proposals via the
    GENERIC search→replace writer in routes/brain_draft_pr_writer
    (open_mechanical_draft_pr). Replaces the Phase-0 stub.

    Admin-gated. Default is DRY-RUN (?apply absent / != 1) — zero GitHub side
    effects, returns the preview. Real opening requires `?apply=1` AND (in the
    writer) DCHUB_L22_REAL_PR==1 + a token; PRs are DRAFT, base=main, head=a new
    branch, NEVER auto-merged. The per-run rate cap is MAX_DRAFT_PRS_PER_RUN.

    Returns {dry_run, opened:[{id,pr_url}], previewed:[{id,branch,file,
    diff_preview}], skipped:[{id,reason}]}."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only",
                       hint="X-Admin-Key / X-Internal-Key header required"), 403

    apply = (request.args.get("apply") or "0").lower() in ("1", "true", "yes")

    try:
        limit = int(request.args.get("limit", "200"))
    except Exception:
        limit = 200
    limit = max(1, min(limit, 1000))

    rows, err = _fetch_open_proposals(include_resolved=False, limit=limit)
    if err:
        return jsonify(ok=False, error=err), 200

    try:
        from routes.brain_draft_pr_writer import (
            open_mechanical_draft_prs, MAX_DRAFT_PRS_PER_RUN,
        )
    except Exception as e:  # pragma: no cover — keep the surface alive
        return jsonify(ok=False, error=f"writer_import_failed: {str(e)[:160]}"), 200

    real_pr = os.environ.get("DCHUB_L22_REAL_PR", "0") == "1"
    gh_token = bool((os.environ.get("PR_SUBMIT_TOKEN")
                     or os.environ.get("GITHUB_TOKEN") or "").strip())

    result = open_mechanical_draft_prs(rows, apply=apply)
    return jsonify(
        ok=True,
        phase="2-generic-draft-pr-writer",
        as_of=datetime.now(timezone.utc).isoformat(),
        apply=apply,
        dry_run=result["dry_run"],
        real_pr_enabled=real_pr,
        gh_token_present=gh_token,
        rate_cap=MAX_DRAFT_PRS_PER_RUN,
        opened=result["opened"],
        previewed=result["previewed"],
        skipped=result["skipped"],
        note=("DRAFT PRs only (base=main, head=branch); never auto-merged. "
              "?apply=1 attempts real opening, which ALSO requires "
              "DCHUB_L22_REAL_PR=1 + a token in the env. Default is dry-run."),
    ), 200
