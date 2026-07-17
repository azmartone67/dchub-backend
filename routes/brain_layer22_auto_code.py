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
# Phase FF+7-meta (2026-05-19): default flipped to LIVE — user wants
# L22 actually opening issues now. Set AUTO_CODE_DRY_RUN=1 in Railway
# env to revert to dry-run-only mode if it gets too noisy.
_DRY_RUN = os.environ.get("AUTO_CODE_DRY_RUN", "0") == "1"
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


# r-l22harden (2026-07-07): noise guards. The L22 queue was 100% non-actionable
# (#1478 proposed a route that already exists; #1482 a page path served by CF;
# #1489 a garbled 'unscheduled cron endpoint' signal). These helpers stop each
# class BEFORE a draft is opened. All are best-effort + fail-OPEN (return the
# non-blocking value on any error) so they can never suppress a legitimate fix.

def _route_already_registered(pattern: str) -> bool:
    """True if the Flask url_map already has a rule matching `pattern`,
    comparing route SHAPE (converter-agnostic: <slug>, <path:slug>, <id> all
    collapse to one token). L21 normalizes real slugs to <slug>, so a 404 on
    an ALREADY-REGISTERED pattern is app-level 'resource not found' (bad slug),
    NOT a missing route — adding an alias does nothing (the #1478 false-draft:
    /api/v1/facilities/<slug> was already served). Fail-OPEN → False on any
    error (no app context / import fail) so it never blocks a real draft."""
    try:
        from flask import current_app
        def _norm(s: str) -> str:
            return re.sub(r"<[^>]+>", "<*>", s or "").rstrip("/")
        want = _norm(pattern)
        if not want or "<*>" not in want:
            # Only guard parameterized routes; a bare literal path is handled
            # by the missing_route recipe's own /api scoping.
            return False
        for rule in current_app.url_map.iter_rules():
            if _norm(str(rule)) == want:
                return True
        return False
    except Exception:
        return False


def _is_backend_owned_path(path: str) -> bool:
    """True only for paths a Flask route could legitimately own — i.e. /api/*.
    Page paths (/dcpi/*, /markets/*, /facility/*, /connect ...) are served by
    the SEO/CF surface, NOT main.py; proposing a Flask handler for them would
    SHADOW the real serving path (#1482 was /dcpi/<slug>). Fail-CLOSED for
    non-/api so missing_route only fires where a backend route makes sense."""
    p = (path or "").strip()
    return p.startswith("/api/")


def _is_valid_404_pattern(path: str) -> bool:
    """Reject un-interpolated template URLs a crawler leaked. L21 normalizes a
    real slug segment to a single <slug>/<id>/<path:...> token, so a LEGIT
    pattern has at most that. A path carrying a literal example-placeholder like
    <your-slug>, <market>, {id}, :id, %7B is a template leak, never a real
    route to add. Returns False to skip. Fail-OPEN → True on empty."""
    p = (path or "")
    if not p:
        return True
    # Strip the recognized single-segment converter tokens L21 emits, then any
    # residual brace/colon/percent placeholder means a template leak.
    stripped = re.sub(r"<(?:path:)?(?:slug|id|facility_id|market|state_slug)>",
                      "", p)
    if re.search(r"<[^>]*>", stripped):   # a NON-standard <...> token remains
        return False
    if re.search(r"[{}]|%7[BbDd]|/:[a-zA-Z]", p):  # {id}, :id, encoded braces
        return False
    return True


# Recipes that represent ONE standing concept (not per-target): the cron
# collision + schema drift findings get drafted by multiple code paths
# (inspector-brief, radar scan, L21) each with a DIFFERENT target_path key,
# so per-target dedup missed them and leaked duplicate issues every cycle
# (#1119/#1121/#1129 were all the same cron issue). These dedup by recipe
# CLASS instead.
_CLASS_SINGLETON_RECIPES = {
    "cron_if_mismatched", "cron_schedule_collision", "schema_drift_guard",
}


def _already_drafted_class(recipe: str) -> bool:
    """Cross-path dedup for singleton recipes — any open draft of this recipe
    class within the window blocks a new one, regardless of target_path."""
    if recipe not in _CLASS_SINGLETON_RECIPES:
        return False
    try:
        from main import get_db
        conn = get_db()
        if not conn:
            return False
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM brain_auto_code_actions "
                "WHERE recipe = %s AND drafted_at > NOW() - INTERVAL %s "
                "AND pr_url IS NOT NULL LIMIT 1",
                (recipe, f"{_DEDUP_WINDOW_DAYS} days"),
            )
            return bool(cur.fetchone())
        finally:
            try: conn.close()
            except Exception: pass
    except Exception:
        return False


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
    # r-l22harden: template-leak / already-served guards (see helpers above).
    if not _is_valid_404_pattern(pattern): return None
    if _route_already_registered(pattern):
        # The route EXISTS — this 404 is app-level 'resource not found' (bad
        # slug), not a missing route. Adding an alias is a no-op (#1478).
        return None

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


# ── Recipe: stale_media_image (2026-06-06) ──────────────────────────

def _try_recipe_stale_media_image(finding: dict) -> dict | None:
    """For an L23 'media_image_quality' weak finding (blank/stale post image),
    draft a fix: repoint the publisher off the frozen static landing-*.png onto
    the live /api/v1/og/dynamic.png card engine. DRAFT-ONLY (opens an issue with
    the proposed change) — media code paths are NOT in the real-PR whitelist, so
    a human reviews + merges. This is the exact class that shipped 4 blank
    LinkedIn posts on 2026-06-06 while the brain looped past it."""
    if finding.get("dim") != "media_image_quality":
        return None
    if _already_drafted("stale_media_image", "media-image"):
        return None
    diff_summary = (
        "Replace any remaining `/static/og/landing-*.png` reference in the "
        "publishers with a /api/v1/og/dynamic.png card URL built from the post's "
        "headline (pattern: linkedin_content_engine._card_url_for). Confirm the "
        "daily-digest Tier-0 dynamic card + the quad OG map both point at the "
        "dynamic engine, not a frozen static PNG.")
    return {
        "recipe": "stale_media_image",
        "target_path": "routes/linkedin_content_engine.py",
        "title": "[brain-l22] Repoint a blank/stale post image onto the live card engine",
        "body": _build_pr_body(
            recipe="stale_media_image",
            trigger=finding,
            target="routes/linkedin_content_engine.py + routes/linkedin_quad_daily.py + main.py(daily_cron)",
            diff_summary=diff_summary,
            rationale=(
                "L23 media_image_quality flagged a blank/thin (<18KB) or static "
                "landing-*.png image being posted. Rich cards are 30-55KB; the "
                "dynamic engine renders editorial / data_brutal / ai_hero from the "
                "post's own data. Same fix shipped manually on 2026-06-06."),
            verification=(
                "curl the publisher's og_image_url — expect a >=30KB PNG. Re-run "
                "the L23 audit; media_image_quality should return healthy."),
        ),
        "labels": ["brain-l22-auto-code", "recipe-stale-media-image",
                   "needs-human-merge"],
    }


# ── Recipe: cron_if_mismatched (r85h — WALK 2nd real-PR recipe) ──────

def _try_recipe_cron_mismatch(finding: dict) -> dict | None:
    """For a cron_schedule_collision finding, draft a PR that staggers the
    colliding cron. The actual file/minute is computed by the PR-writer
    (open_cron_stagger_pr) against the LIVE repo — it picks ONE safe file and
    NEVER touches evolve-cron.yml or any `if: github.event.schedule` guarded
    workflow; DR-critical jobs stay put. WALK phase: real fork PR, human-merge."""
    if finding.get("issue") != "cron_schedule_collision":
        return None
    expr = (finding.get("url") or "").strip() or "?"
    # r-l22harden: the signal must carry a REAL cron expression (5 fields of
    # [0-9*/,-]). #1489 fired on a placeholder ('unscheduled cron endpoint') and
    # drafted a 'stagger' fix for a collision it couldn't parse. Reject anything
    # that isn't a well-formed cron so garbled signals never become issues.
    if not re.fullmatch(r"[\d*/,\-]+(?:\s+[\d*/,\-]+){4}", expr):
        return None
    if _already_drafted("cron_if_mismatched", "workflows:" + expr):
        return None
    return {
        "recipe": "cron_if_mismatched",
        "target_path": ".github/workflows/",
        "title": f"[brain-l22] Stagger colliding cron `{expr}` in workflows",
        "body": _build_pr_body(
            recipe="cron_if_mismatched",
            trigger=finding,
            target=".github/workflows/*.yml",
            diff_summary=(
                f"Two+ workflows share cron `{expr}` — a thundering herd on the "
                "2-replica backend + Neon. Stagger the non-canonical workflow's "
                "minute to a free slot. The PR-writer picks ONE safe file and "
                "NEVER edits evolve-cron.yml or any workflow with an "
                "`if: github.event.schedule` job guard; DR-critical jobs stay "
                "put; the edited file is yaml-validated."),
            rationale=(finding.get("detail")
                       or "Colliding crons stampede the backend + Neon at the "
                          "same instant; staggering removes the herd."),
            verification=("Confirm no two workflows share the new minute "
                          "(`git grep cron: .github/workflows/`); the "
                          "cron_schedule_collision finding clears on next scan."),
        ),
        "labels": ["brain-l22-auto-code", "recipe-cron-stagger",
                   "needs-human-merge"],
    }


# ── Recipe: schema_drift_guard (r86 — Inspector-brief-only, DRAFT issue) ──

# Honest/already-resolved drift the Inspector still names in stale briefs — we
# NEVER draft a "fix" for reality (mirrors the citation skip in
# brain_inspector._parse_code_fix_candidates). citation_score=0 is an honest
# measurement; the mcp_funnel api_key→ip_address drift was fixed in r85f.
_SCHEMA_DRIFT_SUPPRESS = (
    "citation",
    "api_key",          # mcp_funnel signal: resolved r85f (uses ip_address)
)


def _try_recipe_schema_drift_guard(finding: dict) -> dict | None:
    """r86: DRAFT-only GitHub Issue for a schema/route-drift code defect the
    Inspector surfaced (a query referencing a column/table that doesn't exist,
    or an autopilot action targeting a dead route). This recipe NEVER edits DB
    code or opens a real PR — _draft_pr's default path opens an Issue with the
    precise target + a suggested guard, so a human applies the change. This is
    the Inspector→L22 handoff for the schema_drift_guard recipe class, which
    previously had no handler at all (so every such candidate was silently
    dropped — the '0 L22 proposals' autonomy gap)."""
    target = (finding.get("target") or "").strip()[:160]
    rationale = (finding.get("rationale") or "").strip()[:300]
    if not target:
        return None
    low = (target + " " + rationale).lower()
    if any(s in low for s in _SCHEMA_DRIFT_SUPPRESS):
        return None
    if _already_drafted("schema_drift_guard", target):
        return None
    return {
        "recipe": "schema_drift_guard",
        "target_path": target,
        "title": f"[brain-l22] Schema-drift guard: {target}",
        "body": _build_pr_body(
            recipe="schema_drift_guard",
            trigger=finding,
            target=target,
            diff_summary=(
                f"The Inspector flagged a schema/route drift in `{target}`:\n\n"
                f"> {rationale or 'a query or action references a column/table/route that does not exist'}\n\n"
                "Suggested guard: wrap the failing query/call in an existence "
                "check — `information_schema.columns` for a column, a registered-"
                "route check for an endpoint — so the signal degrades to an "
                "honest 'not measured' instead of erroring (or repointing a dead "
                "endpoint to a live one)."),
            rationale=(
                "Schema/route drift surfaces as a hard error in a brain signal "
                "or as execution_failed in an autopilot action. A guard turns it "
                "into honest telemetry instead of a crash, and stops the brain "
                "re-flagging it every pass."),
            verification=(
                "After the guard ships, the originating finding stops erroring "
                "on the next Inspector brief."),
        ),
        "labels": ["brain-l22-auto-code", "recipe-schema-drift-guard",
                   "confidence-investigation-only"],
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

    # Cross-path recipe-class dedup (singleton recipes only) — stops the
    # duplicate cron/schema-drift issues that different code paths leaked.
    if _already_drafted_class(draft.get("recipe", "")):
        return {"ok": False, "deduped_recipe_class": True,
                "recipe": draft.get("recipe")}

    # r58 (2026-05-25): autonomy promotion for route_alias_404.
    # When the recipe is route_alias_404 AND DCHUB_L22_REAL_PR=1 in
    # env, defer to the PR-writer in brain_layer22_pr_writer.py
    # which actually clones, patches, commits, pushes, and opens a
    # PR. If that returns ok=True we record the PR url and stop.
    # If it fails for ANY reason (env missing, fork unavailable,
    # canonical route not found via regex), we fall back to opening
    # an Issue — the operator still gets a paper trail.
    #
    # This is the missing wiring from r57: PR-writer existed but
    # was only callable via the manual admin endpoint. Now the L22
    # scan loop autoroutes to it for the whitelisted recipe.
    if (draft.get("recipe") == "route_alias_404"
            and os.environ.get("DCHUB_L22_REAL_PR", "0") == "1"):
        try:
            from routes.brain_layer22_pr_writer import open_route_alias_pr
            trigger = draft.get("trigger") or {}
            # derive src/dst from the diff_summary text we already
            # built — it has the pattern in @app.route('...')
            import re as _re
            ds = draft.get("body") or ""
            m_src = _re.search(r"@app\.route\('([^']+)'\)\s+alias", ds)
            m_dst = _re.search(r"existing handler for\s*\n?'([^']+)'", ds)
            if m_src and m_dst:
                pr_res = open_route_alias_pr(
                    src_path=m_src.group(1),
                    dst_path=m_dst.group(1),
                    trigger_count=int(trigger.get("count") or 0),
                    rationale=(draft.get("title") or "")[:200],
                )
                if pr_res.get("ok"):
                    _record(draft, dry_run=False,
                             pr_url=pr_res.get("pr_url"),
                             pr_number=None,
                             branch=pr_res.get("branch"),
                             error=None)
                    return {"ok": True,
                            "pr_url": pr_res.get("pr_url"),
                            "branch": pr_res.get("branch"),
                            "via": "brain_layer22_pr_writer",
                            **draft}
                # Fall through to Issue on PR failure (operator
                # still gets a record + can apply manually)
                print(f"[L22] PR-writer failed for {draft.get('title')}: "
                      f"{pr_res.get('error') or pr_res.get('stage')} "
                      f"— falling back to Issue", flush=True)
        except Exception as _prw_err:
            print(f"[L22] PR-writer import/call exception: {_prw_err} "
                  f"— falling back to Issue", flush=True)

    # r85h: autonomy promotion for cron_if_mismatched (WALK 2nd recipe).
    # Routes to open_cron_stagger_pr (fenced: never evolve-cron / schedule-
    # guarded; DR jobs stay put; yaml-validated; DRAFT PR; human-merge). If the
    # live repo has no SAFE collision it returns no_action — we record + stop
    # (never open an empty Issue). Any other failure falls through to the Issue.
    if (draft.get("recipe") == "cron_if_mismatched"
            and os.environ.get("DCHUB_L22_REAL_PR", "0") == "1"):
        try:
            from routes.brain_layer22_pr_writer import open_cron_stagger_pr
            pr_res = open_cron_stagger_pr(rationale=(draft.get("title") or "")[:200])
            if pr_res.get("ok"):
                _record(draft, dry_run=False, pr_url=pr_res.get("pr_url"),
                         pr_number=None, branch=pr_res.get("branch"), error=None)
                return {"ok": True, "pr_url": pr_res.get("pr_url"),
                        "branch": pr_res.get("branch"),
                        "via": "brain_layer22_pr_writer:cron", **draft}
            if pr_res.get("no_action"):
                _record(draft, dry_run=False, pr_url=None, pr_number=None,
                         branch=None, error="no_action:" + str(pr_res.get("reason")))
                return {"ok": True, "no_action": True,
                        "reason": pr_res.get("reason"), **draft}
            print(f"[L22] cron PR-writer failed: "
                  f"{pr_res.get('error') or pr_res.get('stage')} "
                  f"— falling back to Issue", flush=True)
        except Exception as _cron_err:
            print(f"[L22] cron PR-writer exception: {_cron_err} "
                  f"— falling back to Issue", flush=True)

    # MVP fallback: open an Issue (always works given GITHUB_TOKEN).
    # The Issue carries the suggested diff text + verification steps;
    # operator clicks 'Code → branch → edit file' to apply.
    try:
        import requests
        body = draft["body"] + "\n\n---\n\n*MVP shipping note: this " \
               "is opened as an Issue with the proposed diff text. The " \
               "next iteration will write the file change directly to " \
               "a branch + open a PR. For now, the operator applies the " \
               "1-line change from the suggestion and clicks 'Create PR'.*"
        _gh_headers = {
            "Authorization": f"Bearer {_GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # Dedup guard (2026-06-21): GitHub-side search-before-create, fail-CLOSED.
        # The local DB dedup (_already_drafted) fails OPEN, so a wiped table could
        # re-flood. Skip if an OPEN issue with this exact title already exists; and
        # if the search itself errors, do NOT post (fail closed — never risk a flood).
        try:
            import urllib.parse as _ulp
            _q = _ulp.quote(f'repo:{_GITHUB_REPO} is:issue is:open in:title "{draft["title"]}"')
            _sr = requests.get(f"https://api.github.com/search/issues?q={_q}&per_page=20",
                               headers=_gh_headers, timeout=15)
            if _sr.status_code == 200:
                _match = next((i for i in (_sr.json() or {}).get("items", [])
                               if i.get("title") == draft["title"]), None)
                if _match:
                    _record(draft, dry_run=False, pr_url=_match.get("html_url"),
                             pr_number=_match.get("number"), branch=None,
                             error="deduped_existing_open_issue")
                    return {"ok": True, "deduped": True,
                            "issue_url": _match.get("html_url"),
                            "issue_number": _match.get("number"), **draft}
            else:
                _e = f"dedup_search_{_sr.status_code}"
                _record(draft, dry_run=False, pr_url=None, pr_number=None, branch=None, error=_e)
                return {"ok": False, "error": _e}
        except Exception as _de:
            _e = f"dedup_search_exc: {str(_de)[:120]}"
            _record(draft, dry_run=False, pr_url=None, pr_number=None, branch=None, error=_e)
            return {"ok": False, "error": _e}
        r = requests.post(
            f"https://api.github.com/repos/{_GITHUB_REPO}/issues",
            headers=_gh_headers,
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

def _try_recipe_missing_route(pattern_key: str, count: int) -> dict | None:
    """For any 404 pattern with no route alias match, open an issue
    suggesting the operator add the route OR fix the frontend caller.
    Lower confidence than route_alias but still actionable."""
    # pattern_key looks like "GET /api/ai-analytics [404]"
    import re
    m = re.match(r"(\w+)\s+(/\S+)\s+\[(\d+)\]", pattern_key)
    if not m: return None
    method, path, status = m.group(1), m.group(2), m.group(3)
    if status != "404": return None
    if _is_forbidden_path(path): return None
    # r-l22harden: only propose a Flask handler where one could legitimately
    # live (/api/*). Page paths (/dcpi/*, /markets/* ...) are SEO/CF-served, so
    # a backend route would shadow them (#1482 was /dcpi/<slug>). Also skip
    # template-leak paths and any route that already resolves.
    if not _is_backend_owned_path(path): return None
    if not _is_valid_404_pattern(path): return None
    if _route_already_registered(path): return None
    if _already_drafted("missing_api_route", path):
        return None

    title = f"[brain-l22] 404 spike on {method} {path} ({count}x in 1h)"
    return {
        "recipe": "missing_api_route",
        "target_path": path,
        "title": title,
        "body": _build_pr_body(
            recipe="missing_api_route",
            trigger={"method": method, "path": path, "count": count, "status": 404},
            target=path,
            diff_summary=(
                f"Add a backend handler for `{method} {path}` OR fix the "
                f"frontend caller that's hitting this path. Search the "
                f"frontend for the literal string `{path}` to find the "
                f"broken link."),
            rationale=(
                f"The L21 HTTP capture ring buffer recorded {count}x 404s "
                f"on this path in the last hour. Either the backend is "
                f"missing this route entirely (add it) or the frontend is "
                f"hitting a typo (fix the href). Today's pattern: many "
                f"singular/plural mismatches like /facility/<slug> vs "
                f"/facilities/<slug>."),
            verification=(
                f"After fix, curl {path} — should be 200 (or whatever "
                f"intended status). The L21 detector should stop firing "
                f"on this pattern within an hour."),
        ),
        "labels": ["brain-l22-auto-code", "recipe-missing-route",
                   "confidence-medium"],
    }


def _try_recipe_high_5xx(pattern_key: str, count: int) -> dict | None:
    """r56 (2026-05-25): persistent 5xx pattern → draft a 'why is this
    failing' investigation issue.

    Different from 404 recipes (those propose fixes); 5xx means the
    backend is crashing. Drafts an investigation request with the
    pattern + recent count, asking operator to inspect logs.
    """
    import re
    m = re.match(r"(\w+)\s+(/\S+)\s+\[(\d+)\]", pattern_key)
    if not m: return None
    method, path, status = m.group(1), m.group(2), m.group(3)
    if not status.startswith("5"): return None
    if _is_forbidden_path(path): return None
    if _already_drafted("high_5xx_pattern", path):
        return None

    title = f"[brain-l22] {status} spike on {method} {path} ({count}x in 1h)"
    return {
        "recipe": "high_5xx_pattern",
        "target_path": path,
        "title": title,
        "body": _build_pr_body(
            recipe="high_5xx_pattern",
            trigger={"method": method, "path": path,
                      "count": count, "status": int(status)},
            target=path,
            diff_summary=(
                f"Backend handler for `{method} {path}` is raising "
                f"{status} consistently. {count}x in last hour.\n\n"
                f"Likely causes:\n"
                f"- Unhandled exception in the view function\n"
                f"- Database connection pool exhaustion\n"
                f"- Downstream API timeout\n"
                f"- Memory pressure / OOM\n\n"
                f"Action: tail Railway logs filtered to `{path}`, find "
                f"the stack trace, wrap the failing block in try/except "
                f"with degraded-200 response or fix the root cause."),
            rationale=(
                f"5xx pattern doesn't auto-fix — root cause varies. "
                f"L22 flags it so the operator investigates before "
                f"users notice. This recipe drafts ISSUES, never PRs."),
            verification=(
                f"After fix, monitor /api/v1/brain/http-errors/patterns "
                f"— the {status}-on-{path} count should drop to 0 "
                f"within an hour."),
        ),
        "labels": ["brain-l22-auto-code", "recipe-high-5xx",
                   "confidence-investigation-only",
                   f"status-{status}"],
    }


def _try_recipe_gone_410_alias(pattern_key: str, count: int) -> dict | None:
    """r56 (2026-05-25): for repeated 404s on KNOWN-OBSOLETE paths
    (e.g. /old-blog/*, /v0/api/*), draft a 410 Gone response handler
    instead of a 404 — tells crawlers to stop checking.

    Heuristic for 'obsolete': URL contains 'old' segment, or matches
    a /v0/ / /beta/ / /legacy/ pattern.
    """
    import re
    m = re.match(r"(\w+)\s+(/\S+)\s+\[(\d+)\]", pattern_key)
    if not m: return None
    method, path, status = m.group(1), m.group(2), m.group(3)
    if status != "404": return None
    if _is_forbidden_path(path): return None
    # Heuristic match
    obsolete_markers = ["/old", "/legacy/", "/v0/", "/beta/", "/deprecated/",
                         "/archive/", "/old-"]
    if not any(marker in path.lower() for marker in obsolete_markers):
        return None
    if _already_drafted("gone_410_alias", path):
        return None

    title = f"[brain-l22] Convert {path} 404 → 410 Gone ({count}x)"
    return {
        "recipe": "gone_410_alias",
        "target_path": path,
        "title": title,
        "body": _build_pr_body(
            recipe="gone_410_alias",
            trigger={"method": method, "path": path,
                      "count": count, "status": 404},
            target=path,
            diff_summary=(
                f"`{path}` is an obsolete URL pattern (matches obsolete "
                f"heuristic). Return HTTP 410 Gone instead of 404 so "
                f"crawlers (Google, AI agents) permanently de-index it.\n\n"
                f"Add to main.py:\n"
                f"```python\n"
                f"@app.route('{path}')\n"
                f"def _gone_{path.replace('/', '_').replace('-', '_')}():\n"
                f"    return jsonify(gone=True, hint='Use the v1 path'), 410\n"
                f"```"),
            rationale=(
                f"The {count}x 404s in 1h indicate crawlers still try "
                f"this URL. 410 stops the retry — saves origin capacity "
                f"+ improves SEO signal."),
            verification=f"After deploy, curl {path} — expect 410.",
        ),
        "labels": ["brain-l22-auto-code", "recipe-gone-410",
                   "confidence-medium"],
    }


def _scan_and_draft(dry_run: bool, brief_id: int | None = None) -> dict:
    """Pull recent findings + L21 ring buffer; match against recipes; draft.

    Phase FF+7-meta (2026-05-19): wired DIRECTLY to L21's
    /api/v1/brain/http-errors/patterns endpoint. The radar's
    check_repeated_404_patterns detector requires 10 hits/hour which
    is too high for fresh-deploy traffic. The ring buffer threshold
    here is 4 hits/hour (r-l22harden: was 2 — too noisy; 2-hit crawler
    pairs on leaked template URLs were the whole L22 noise queue).

    r86: when called with a brief_id (the Inspector→L22 auto-fire passes one),
    FIRST consume that brief's parsed Code-fix RECIPE candidates and draft them.
    Before r86 this function ignored brief_id entirely and only ran its own
    independent L21/radar scan, so the Inspector's RECIPE candidates were
    parsed, logged, and silently dropped — the literal '0 L22 PR proposals /
    the handoff pipe needs one more step' autonomy gap on /brain/innovation.
    """
    drafted = []
    skipped = []

    # 0. r86: Inspector-brief candidates (the handoff). Re-parse the brief by
    # id, map each RECIPE candidate to its handler, and draft. Honest/already-
    # resolved candidates are suppressed inside each recipe (e.g. citation,
    # mcp_funnel api_key). _already_drafted dedups within 7d so a recurring
    # candidate doesn't spam. This is the step that finally crosses the
    # Inspector→L22 handoff frontier.
    if brief_id:
        try:
            from routes.brain_inspector import _parse_code_fix_candidates, _get_db
            md = ""
            _c = _get_db()
            if _c is not None:
                try:
                    with _c.cursor() as _cur:
                        _cur.execute(
                            "SELECT brief_md FROM brain_briefs WHERE id = %s",
                            (brief_id,))
                        _r = _cur.fetchone()
                        md = (_r[0] if _r else "") or ""
                finally:
                    try: _c.close()
                    except Exception: pass
            candidates = _parse_code_fix_candidates(md)
            for cand in candidates:
                recipe = (cand.get("recipe") or "").strip()
                # r86b: dedup per RECIPE CLASS, not per target. The Inspector LLM
                # rephrases the target each brief ("schema.org coverage detector"
                # vs "schema_org_coverage_low handler"), so per-target dedup would
                # let near-duplicate issues through on every 6h brief while a
                # finding lingers in the rolling 24h window. One open draft per
                # recipe class per dedup window is enough. We record with this
                # stable key (set on the draft below) so future cycles dedup.
                dedup_key = f"inspector_brief:{recipe}"
                if recipe and _already_drafted(recipe, dedup_key):
                    skipped.append({
                        "recipe": recipe, "source": "inspector_brief",
                        "brief_id": brief_id,
                        "reason": "already_drafted_recipe_class",
                    })
                    continue
                draft = None
                if recipe == "schema_drift_guard":
                    draft = _try_recipe_schema_drift_guard(cand)
                elif recipe == "cron_if_mismatched":
                    draft = _try_recipe_cron_mismatch({
                        "issue":  "cron_schedule_collision",
                        "url":    cand.get("target") or "",
                        "detail": cand.get("rationale") or "",
                        "count":  1,
                    })
                elif recipe == "route_alias_404":
                    draft = _try_recipe_route_alias({
                        "url":        cand.get("target") or "",
                        "count":      1,
                        "confidence": "medium",
                    })
                if draft:
                    # Stable per-recipe-class dedup key so the NEXT brief cycle's
                    # _already_drafted(recipe, dedup_key) check above matches.
                    draft["target_path"] = dedup_key
                    res = _draft_pr(draft, dry_run=dry_run)
                    (drafted if res.get("ok") else skipped).append({
                        "recipe": draft["recipe"], "title": draft.get("title"),
                        "source": "inspector_brief", "brief_id": brief_id,
                        "result": res,
                    })
                else:
                    skipped.append({
                        "recipe": recipe, "target": cand.get("target"),
                        "source": "inspector_brief", "brief_id": brief_id,
                        "reason": "no_handler_or_suppressed_or_already_drafted",
                    })
        except Exception as e:
            skipped.append({"source": "inspector_brief",
                            "reason": f"brief_candidate_consume_failed: {str(e)[:160]}"})

    # 1. From L21 ring buffer (real-time, fresh)
    try:
        patterns = _internal("/api/v1/brain/http-errors/patterns?window=3600")
        for p in (patterns.get("patterns") or []):
            key = p.get("pattern", "")
            n = int(p.get("count", 0))
            # r-l22harden: 2 hits/hour was noise — #1478/#1482 were 2-hit
            # template-leak crawls. Require >=4/hour so a genuine spike drafts
            # but a stray crawler pair does not.
            if n < 4: continue
            if "[404]" not in key: continue
            # Try route-alias first (singular/plural)
            synthetic = {
                "url": (key.split(" ", 1)[1].rsplit(" ", 1)[0]
                        if " " in key else key),
                "count": n,
                "confidence": "medium",
            }
            # r56 (2026-05-25): recipe ladder — try in order from
            # safest (route alias = 1-line code change) to most
            # diagnostic (high_5xx = investigation only).
            draft = _try_recipe_route_alias(synthetic)
            if not draft:
                draft = _try_recipe_gone_410_alias(key, n)
            if not draft:
                draft = _try_recipe_high_5xx(key, n)
            if not draft:
                draft = _try_recipe_missing_route(key, n)
            if not draft:
                skipped.append({"pattern": key, "count": n,
                                "reason": "no matching recipe / forbidden / dedup"})
                continue
            res = _draft_pr(draft, dry_run=dry_run)
            (drafted if res.get("ok") else skipped).append({
                "recipe": draft["recipe"], "title": draft["title"],
                "pattern": key, "count": n,
                "result": res,
            })
    except Exception as e:
        skipped.append({"reason": f"L21 read failed: {e}"})

    # 2. Also scan consistency-radar (slow, but covers older patterns)
    try:
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
    except Exception: pass

    # 3. r-media (2026-06-06): scan L23 lifecycle findings for the media-image
    # defect class (the one that needed a human this week). Draft-only — opens an
    # issue with the proposed repoint; a human merges (media paths aren't in the
    # real-PR whitelist).
    try:
        audit = _internal("/api/v1/brain/lifecycle/audit")
        for f in (audit.get("findings") or []):
            if f.get("dim") == "media_image_quality" and f.get("status") == "weak":
                draft = _try_recipe_stale_media_image(f)
                if draft:
                    res = _draft_pr(draft, dry_run=dry_run)
                    (drafted if res.get("ok") else skipped).append({
                        "recipe": draft["recipe"], "title": draft["title"], "result": res})
    except Exception: pass

    # 4. r85h (WALK 2nd recipe): scan consistency-radar for cron_schedule_collision
    # → draft a FENCED cron-stagger PR. open_cron_stagger_pr no-ops safely when the
    # live workflows have no SAFE collision (e.g. already staggered), so this never
    # opens an empty PR; evolve-cron.yml + schedule-guarded workflows are never
    # touched (the writer fences them). One stagger PR per scan.
    try:
        radar2 = _internal("/api/v1/brain/consistency-radar")
        for f in (radar2.get("findings") or []):
            if f.get("issue") != "cron_schedule_collision":
                continue
            draft = _try_recipe_cron_mismatch(f)
            if draft:
                res = _draft_pr(draft, dry_run=dry_run)
                (drafted if res.get("ok") else skipped).append({
                    "recipe": draft["recipe"], "title": draft["title"], "result": res})
                break
    except Exception: pass

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
        recipes=["route_alias_404", "cron_if_mismatched", "missing_route", "high_5xx",
                 "gone_410_alias", "stale_media_image", "smoke_regression"],
        real_pr_whitelist=["route_alias_404", "cron_if_mismatched",
                           "smoke_regression"],  # these write a branch+PR
        draft_only=["missing_route", "high_5xx", "gone_410_alias", "stale_media_image"],
        recent_actions=actions,
        note=("L22 recipe library (r85h 2026-06-13): route_alias_404 + cron_if_mismatched "
              "are whitelisted for REAL fork PRs (branch + file change, human-merge). "
              "smoke_regression (2026-07-17) lives in routes/brain_smoke_regression.py — "
              "its own trigger (post-deploy-smoke streak / L14 hard_burn), real fix PR, "
              "gauntlet-gated merge + post-merge LANDING verification. "
              "cron_if_mismatched staggers a colliding GitHub Actions cron, FENCED: never "
              "edits evolve-cron.yml or any `if: github.event.schedule` guarded workflow, "
              "DR jobs stay put, plain-int-minute crons only, yaml-validated, DRAFT PR. The "
              "rest (incl. stale_media_image) are DRAFT-ONLY (issue for a human). "
              "DCHUB_L22_REAL_PR=1 enables real PRs; AUTO_CODE_DRY_RUN=0 enables issues. "
              "RUN/auto-merge stays OFF."),
    )


def _brief_id_from_request():
    """r86: the Inspector→L22 auto-fire POSTs {trigger, brief_id}. Read it so
    _scan_and_draft can consume that brief's RECIPE candidates."""
    try:
        body = request.get_json(silent=True) or {}
        bid = body.get("brief_id")
        return int(bid) if bid is not None else None
    except Exception:
        return None


@brain_layer22_bp.route("/api/v1/brain/auto-code/run", methods=["POST"])
def auto_code_run():
    if _ADMIN_KEY:
        provided = (request.headers.get("X-Admin-Key") or "").strip()
        if provided != _ADMIN_KEY:
            return jsonify(error="unauthorized"), 401
    return jsonify(_scan_and_draft(dry_run=False, brief_id=_brief_id_from_request()))


@brain_layer22_bp.route("/api/v1/brain/auto-code/dry-run",
                        methods=["POST", "GET"])
def auto_code_dry_run():
    return jsonify(_scan_and_draft(dry_run=True, brief_id=_brief_id_from_request()))
