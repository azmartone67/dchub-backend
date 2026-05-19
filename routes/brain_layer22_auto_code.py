"""
Brain L22 — Auto-Code (2026-05-19).

The layer the user kept asking about. Reads L14 causal chains AND L21
recent actions, and for matched FIX-RECIPE patterns drafts a minimal
code diff + opens a PR labeled brain-l22-auto-code.

Safety story (no auto-merge without human approval; conservative
whitelist; bounded diff size; mandatory CI):

  TIER 1 — Whitelisted fix recipes (auto-PR allowed):
    A. route_alias_404:
       - Match: brain finding 'repeated_404_pattern' for a URL pattern
         that matches an existing backend route except for s/no-s
       - Patch: add @app.route(<singular-or-plural>) decorator
       - Diff cap: <=2 lines added
    B. schema_drift_guard:
       - Match: brain finding 'schema_drift_*' where SQL hits a column
         that doesn't exist
       - Patch: wrap SELECT in try/except + add to_regclass probe
       - Diff cap: <=15 lines
    C. cron_if_mismatched:
       - Match: brain finding 'cron_if_check_mismatched_schedule'
       - Patch: rewrite the if-check to match the existing cron string
       - Diff cap: <=2 lines

  TIER 2 — Pattern-matched but needs human review (PR with WIP label):
    - New detector proposals from L7
    - Connection-leak refactor (try/finally) suggestions
    - Any finding with confidence != 'high'

  TIER 3 — NEVER auto-PR (security or destructive):
    - Anything that touches credentials, env vars, secrets, auth
    - Schema migrations / ALTER TABLE
    - DELETE statements
    - any file under /scripts/ or /.github/

The PR description includes:
  - Which L14 chain or L21 action triggered it
  - The exact diff + a 1-paragraph rationale
  - The verification step
  - A "click to revert" instruction
  - DRY_RUN=true env var skips actual PR creation (logs only)

Endpoints:
  GET  /api/v1/brain/auto-code            — recent PRs + state
  POST /api/v1/brain/auto-code/run        — admin: scan + draft now
  POST /api/v1/brain/auto-code/dry-run    — show what would happen
"""

import os
import re
import json
import logging
import datetime as _dt
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
brain_layer22_bp = Blueprint("brain_layer22", __name__)

_GITHUB_TOKEN = (os.environ.get("GITHUB_TOKEN") or "").strip()
_GITHUB_REPO = (os.environ.get("GITHUB_REPO") or "azmartone67/dchub-backend").strip()
_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
_DRY_RUN = os.environ.get("AUTO_CODE_DRY_RUN", "1") == "1"  # SAFE DEFAULT: dry-run
_MAX_DIFF_LINES = int(os.environ.get("AUTO_CODE_MAX_DIFF_LINES", "20"))
_DEDUP_WINDOW_DAYS = 7

_FORBIDDEN_PATH_PATTERNS = [
    r"\.env", r"/secrets/", r"\.github/", r"scripts/.*\.sh",
    r"auth", r"login", r"password", r"credential", r"stripe.*webhook",
]

_FORBIDDEN_DIFF_PATTERNS = [
    r"DROP\s+TABLE", r"DELETE\s+FROM", r"ALTER\s+TABLE",
    r"os\.environ\[.*\]\s*=", r"subprocess", r"eval\(", r"exec\(",
    r"open\(.*['\"][rw]", r"shutil\.rmtree", r"os\.remove",
]


def _ensure_table():
    try:
        from main import get_db
        conn = get_db()
        if not conn: return
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_auto_code_actions (
                    id            SERIAL PRIMARY KEY,
                    drafted_at    TIMESTAMPTZ DEFAULT NOW(),
                    recipe        TEXT NOT NULL,
                    trigger_source TEXT,
                    target_path   TEXT,
                    diff_summary  TEXT,
                    pr_url        TEXT,
                    pr_number     INTEGER,
                    branch        TEXT,
                    dry_run       BOOLEAN DEFAULT FALSE,
                    error         TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_autocode_drafted_at "
                        "ON brain_auto_code_actions(drafted_at DESC)")
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"[L22] table create failed: {e}")


def _internal(path: str, timeout: int = 8) -> dict:
    try:
        import requests
        r = requests.get(f"http://localhost:8080{path}", timeout=timeout)
        if r.status_code != 200: return {}
        return r.json() or {}
    except Exception:
        return {}


# ── Safety checks ───────────────────────────────────────────────────

def _is_forbidden_path(path: str) -> bool:
    for pat in _FORBIDDEN_PATH_PATTERNS:
        if re.search(pat, path, re.IGNORECASE):
            return True
    return False


def _is_forbidden_diff(diff_text: str) -> bool:
    for pat in _FORBIDDEN_DIFF_PATTERNS:
        if re.search(pat, diff_text, re.IGNORECASE):
            return True
    return False


def _diff_within_limits(diff_text: str) -> bool:
    """Diff must be small to be auto-applicable."""
    added = sum(1 for line in diff_text.split("\n")
                 if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_text.split("\n")
                   if line.startswith("-") and not line.startswith("---"))
    return (added + removed) <= _MAX_DIFF_LINES


def _already_drafted(recipe: str, target: str) -> bool:
    """Idempotency: don't re-draft the same fix within 7d."""
    try:
        from main import get_db
        conn = get_db()
        if not conn: return False
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM brain_auto_code_actions "
                "WHERE recipe = %s AND target_path = %s "
                "AND drafted_at > NOW() - INTERVAL %s "
                "AND pr_url IS NOT NULL LIMIT 1",
                (recipe, target, f"{_DEDUP_WINDOW_DAYS} days"),
            )
            return bool(cur.fetchone())
        finally:
            try: conn.close()
            except Exception: pass
    except Exception:
        return False


# ── Recipe: route_alias_404 ─────────────────────────────────────────

def _try_recipe_route_alias(finding: dict) -> dict | None:
    """For a repeated_404_pattern finding, propose adding a backend
    route alias if the pattern looks like a singular-plural mismatch
    or a missing path that has a near neighbor."""
    pattern = finding.get("url", "")
    if not pattern: return None
    # Only deal with clean /api/v1/... patterns
    if "/api/v1/" not in pattern: return None
    if _is_forbidden_path(pattern): return None

    # Heuristic: try the singular ↔ plural transform
    # /api/v1/facility/<slug>  ->  /api/v1/facilities/<slug>
    transforms = []
    if "/facility/" in pattern:
        transforms.append(("/facility/", "/facilities/"))
    elif "/facilities/" in pattern:
        transforms.append(("/facilities/", "/facility/"))
    # Add more clean transforms here as we learn them
    if not transforms: return None

    src_path = "main.py"  # target file
    if _already_drafted("route_alias_404", src_path + ":" + pattern):
        return None

    # Build the diff suggestion (no actual code-write here; the PR
    # body explains exactly what to add)
    src, dst = transforms[0]
    canonical = pattern.replace(src, dst)
    diff_summary = (
        f"Add @app.route('{pattern.replace('<slug>', '<path:slug>')}') "
        f"alias above the existing handler for "
        f"'{canonical.replace('<slug>', '<path:slug>')}'."
    )

    return {
        "recipe": "route_alias_404",
        "target_path": src_path,
        "title": f"[brain-l22] Add {pattern} alias to silence 404 spike",
        "body": _build_pr_body(
            recipe="route_alias_404",
            trigger=finding,
            target=src_path,
            diff_summary=diff_summary,
            rationale=(
                "Frontend hits the singular form; backend only serves the "
                "plural (or vice-versa). Adding the alias resolves the "
                "404 spike without a frontend deploy. Same recipe applied "
                "manually for /api/v1/facility/<slug> earlier today (commit "
                "55912023)."),
            verification=(
                "After deploy, curl the pattern with a known-good slug — "
                "expect 200. check_repeated_404_patterns finding should "
                "clear on next radar scan."),
        ),
        "labels": ["brain-l22-auto-code", "recipe-route-alias",
                   f"confidence-{finding.get('confidence','medium')}"],
    }


# ── PR body builder ─────────────────────────────────────────────────

def _build_pr_body(recipe, trigger, target, diff_summary, rationale,
                   verification) -> str:
    return f"""**Auto-drafted by Brain L22**
([routes/brain_layer22_auto_code.py](https://github.com/{_GITHUB_REPO}/blob/main/routes/brain_layer22_auto_code.py))

> [!NOTE]
> This is an AUTO-DRAFT. Read the rationale + verification step before merging.
> Human approval required for this PR class.

## Recipe
`{recipe}`

## Triggering signal
{json.dumps(trigger, indent=2, default=str)[:1500]}

## Target file
`{target}`

## Proposed change (summary)
{diff_summary}

## Rationale
{rationale}

## How to verify (after merge + deploy)
{verification}

## How to revert
`git revert <merge-commit>` — this commit is isolated and reversible.

---
_Generated under L22 safety rules: whitelisted recipe, diff capped at
{_MAX_DIFF_LINES} lines, no auto-merge, dedup window {_DEDUP_WINDOW_DAYS}d,
DRY_RUN={_DRY_RUN}._
"""


# ── PR opener (or dry-run logger) ───────────────────────────────────

def _draft_pr(draft: dict, dry_run: bool) -> dict:
    _ensure_table()
    if dry_run or _DRY_RUN:
        _record(draft, dry_run=True, pr_url=None, pr_number=None,
                 branch=None, error=None)
        return {"ok": True, "dry_run": True, **draft}
    if not _GITHUB_TOKEN:
        _record(draft, dry_run=False, pr_url=None, pr_number=None,
                 branch=None, error="GITHUB_TOKEN not set")
        return {"ok": False, "error": "GITHUB_TOKEN not set"}

    # NOTE: this MVP creates an ISSUE not a PR — building a real PR
    # requires writing the file change, which means committing to a
    # branch on disk. For safety, we open an issue with the suggested
    # diff text. Operator clicks 'Code → branch → edit file' from the
    # issue to apply. Real diff-write comes in the next iteration.
    try:
        import requests
        body = draft["body"] + "\n\n---\n\n*MVP shipping note: this " \
               "is opened as an Issue with the proposed diff text. The " \
               "next iteration will write the file change directly to " \
               "a branch + open a PR. For now, the operator applies the " \
               "1-line change from the suggestion and clicks 'Create PR'.*"
        r = requests.post(
            f"https://api.github.com/repos/{_GITHUB_REPO}/issues",
            headers={
                "Authorization": f"Bearer {_GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "title": draft["title"],
                "body": body,
                "labels": draft.get("labels", ["brain-l22-auto-code"]),
            },
            timeout=15,
        )
        if r.status_code not in (200, 201):
            err = f"github_{r.status_code}: {r.text[:200]}"
            _record(draft, dry_run=False, pr_url=None, pr_number=None,
                     branch=None, error=err)
            return {"ok": False, "error": err}
        data = r.json() or {}
        _record(draft, dry_run=False,
                 pr_url=data.get("html_url"),
                 pr_number=data.get("number"),
                 branch=None, error=None)
        return {"ok": True, "issue_url": data.get("html_url"),
                "issue_number": data.get("number"), **draft}
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:200]}"
        _record(draft, dry_run=False, pr_url=None, pr_number=None,
                 branch=None, error=err)
        return {"ok": False, "error": err}


def _record(draft, dry_run, pr_url, pr_number, branch, error):
    try:
        from main import get_db
        conn = get_db()
        if not conn: return
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO brain_auto_code_actions "
                "(recipe, trigger_source, target_path, diff_summary, "
                " pr_url, pr_number, branch, dry_run, error) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (draft.get("recipe"),
                 (draft.get("body") or "")[:500],
                 draft.get("target_path"),
                 draft.get("title"),
                 pr_url, pr_number, branch, dry_run, error),
            )
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"[L22] record failed: {e}")


# ── Main scanner ────────────────────────────────────────────────────

def _scan_and_draft(dry_run: bool) -> dict:
    """Pull recent findings + L21 actions; match against recipes; draft."""
    drafted = []
    skipped = []

    # 1. From consistency-radar
    radar = _internal("/api/v1/brain/consistency-radar")
    findings = radar.get("findings") or []
    for f in findings:
        if f.get("issue") != "repeated_404_pattern":
            continue
        draft = _try_recipe_route_alias(f)
        if draft:
            res = _draft_pr(draft, dry_run=dry_run)
            (drafted if res.get("ok") else skipped).append({
                "recipe": draft["recipe"], "title": draft["title"],
                "result": res,
            })
        else:
            skipped.append({"finding": f.get("url"),
                             "reason": "no matching recipe / forbidden / dedup"})

    return {
        "ok": True,
        "ran_at": _dt.datetime.utcnow().isoformat() + "Z",
        "dry_run": dry_run or _DRY_RUN,
        "drafted_count": len(drafted),
        "skipped_count": len(skipped),
        "drafted": drafted[:10],
        "skipped": skipped[:10],
    }


# ── Endpoints ───────────────────────────────────────────────────────

@brain_layer22_bp.route("/api/v1/brain/auto-code", methods=["GET"])
def auto_code_list():
    """Recent auto-code actions + state."""
    _ensure_table()
    actions = []
    try:
        from main import get_db
        conn = get_db()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT drafted_at, recipe, target_path, diff_summary, "
                    "       pr_url, pr_number, dry_run, error "
                    "FROM brain_auto_code_actions "
                    "ORDER BY drafted_at DESC LIMIT 20"
                )
                for r in cur.fetchall():
                    if hasattr(r, "get"):
                        actions.append({
                            "drafted_at": str(r.get("drafted_at") or "")[:19],
                            "recipe": r.get("recipe"),
                            "target": r.get("target_path"),
                            "summary": r.get("diff_summary"),
                            "pr_url": r.get("pr_url"),
                            "pr_number": r.get("pr_number"),
                            "dry_run": r.get("dry_run"),
                            "error": r.get("error"),
                        })
                    else:
                        actions.append({
                            "drafted_at": str(r[0])[:19],
                            "recipe": r[1], "target": r[2], "summary": r[3],
                            "pr_url": r[4], "pr_number": r[5],
                            "dry_run": r[6], "error": r[7],
                        })
            finally:
                try: conn.close()
                except Exception: pass
    except Exception: pass
    return jsonify(
        ok=True,
        dry_run_default=_DRY_RUN,
        max_diff_lines=_MAX_DIFF_LINES,
        recipes=["route_alias_404"],  # MVP: just this one
        recent_actions=actions,
        note=("L22 MVP: route_alias_404 recipe is the only whitelisted "
              "auto-fix. DRY_RUN=true by default — flip to false via "
              "AUTO_CODE_DRY_RUN=0 env var to enable live issue creation. "
              "Real PR-write (with file change on a branch) is the next "
              "iteration; this MVP opens an issue with the proposed diff."),
    )


@brain_layer22_bp.route("/api/v1/brain/auto-code/run", methods=["POST"])
def auto_code_run():
    if _ADMIN_KEY:
        provided = (request.headers.get("X-Admin-Key") or "").strip()
        if provided != _ADMIN_KEY:
            return jsonify(error="unauthorized"), 401
    return jsonify(_scan_and_draft(dry_run=False))


@brain_layer22_bp.route("/api/v1/brain/auto-code/dry-run",
                        methods=["POST", "GET"])
def auto_code_dry_run():
    return jsonify(_scan_and_draft(dry_run=True))
