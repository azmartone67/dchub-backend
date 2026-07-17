"""
brain_smoke_regression.py — L22 recipe: smoke_regression (2026-07-17).

THE MISSING AUTONOMY RECIPE. On 2026-07-16/17, /api/v1/map + /api/v1/facility
500-stormed for ~5 hours: the post-deploy-smoke 'map' probe failed 8+
consecutive runs from 22:12Z, brain L14 filed slo_hard_burn (n5xx≈247), CF
surfaced "Backend unreachable" — DETECTION worked end to end, but no repair PR
was ever generated. The fix (2fbce20d, a one-subquery join correction) came
from a human session 5 hours later. The repair machinery (real-PR writers,
pre-merge gauntlet, automerge, canary) already existed and was armed; what was
missing was the TRIGGER + CONTEXT recipe for this incident class. This module
is that recipe.

PIPELINE (a persisted state machine, advanced one stage per tick — safe to
run every 30 min from the brain-autonomy cron):

  1. DETECT      — the post-deploy-smoke workflow has >= SMOKEREG_CONSEC_FAILS
                   consecutive completed failures (GH API), OR an open L14
                   slo_hard_burn finding names a smoke-covered route. Then the
                   failing probe is identified by LIVE-probing smoke_test.py's
                   SMOKE_CHECKS table (name + path + expected status), so the
                   trigger is grounded in what is failing RIGHT NOW, not a
                   stale log line. One active incident at a time.
  2. CONTEXT     — the failing probe (name/URL/status/body), the git compare
                   of EVERY deploy between the last-green run's sha and the
                   first-red run's sha (the guilty commit is in that window
                   by construction), current-content excerpts of the touched
                   files around the changed regions, recent error text for
                   the route, and a schema-drift signature scan (issue #1604
                   folded in: 'operator does not exist: text = integer' etc.
                   gets class-specific fix guidance).
  3. AUTHOR      — one LLM call (brain reasoning tier, L14-style model
                   fallback chain) returns a STRICT-JSON single-file
                   search→replace fix. Hard validation before anything is
                   written: file must be in the deploy window, not a
                   forbidden path, search_text exactly-once on LIVE main,
                   <= SMOKEREG_MAX_DIFF_LINES changed lines, no forbidden
                   diff tokens, AST-parse of the patched .py.
  4. PR          — a REAL fix PR (reuses brain_draft_pr_writer's REST flow:
                   branch off main -> commit -> draft PR). It changes a real
                   code path, so the brain-pr-substance-gate passes on
                   substance, and the pre-merge gauntlet (AST + delta
                   regression-lint + tests) runs like any PR. Branch prefix
                   brain/smokefix- (NOT brain/autofix-) so the mechanical
                   automerge lane never touches it — THIS lane owns merging.
  5. MERGE       — only when the gauntlet is GREEN (ci_status_for_sha,
                   fail-closed) AND the shared automerge flags are armed
                   (BRAIN_AUTOMERGE_ENABLED=1, BRAIN_AUTOMERGE_DRY_RUN!=1,
                   breaker not tripped, health green). Squash merge, same as
                   the mechanical lane.
  6. LAND-VERIFY — never mark done on merge alone (the never-landing pattern,
                   finding #100097). After SMOKEREG_LANDING_WAIT_S (default
                   300s — Railway's old replicas DRAIN for minutes after a
                   promote and still serve the pre-fix 500; a smoke run inside
                   the drain window is a FALSE RED, seen twice on 07-16),
                   re-dispatch the post-deploy-smoke workflow. Green run AND
                   the previously-failing probe live-green => resolved. Red
                   => ONE re-dispatch retry (drain guard), then the attempt
                   has landed red.
  7. ESCALATE    — if no safe fix can be produced, or the fix lands red, and
                   attempts >= SMOKEREG_MAX_ATTEMPTS (default 2, also the
                   daily PR cap): open ONE deduped high-priority issue and
                   STOP. No re-firing findings, no PR floods.

SAFETY:
  · Kill switch SMOKE_REGRESSION_DISABLE=1 — tick returns {disabled} first.
  · PR-opening requires DCHUB_L22_REAL_PR=1 (the same arm switch every other
    real-PR writer uses); without it the authored fix is recorded as a
    preview and nothing touches GitHub.
  · Merging additionally requires the BRAIN_AUTOMERGE_* flags (shared with
    routes/brain_automerge — one operator knob arms/disarms both lanes).
  · ?dry_run=1 forces a READ-ONLY preview (no DB/LLM/GitHub writes).
  · Forbidden paths reuse brain_mechanical_classifier._forbidden_path_hits
    (billing/auth/migrations/worker/honest-numbers are unreachable).
  · Attempt + daily caps; incident dedup by (probe, first_red_run).

Endpoints (blueprint built lazily; admin gate mirrors the classifier's):
  POST /api/v1/brain/smoke-regression/tick    — advance the state machine
  GET  /api/v1/brain/smoke-regression/status  — config + incidents

Heavy imports (flask, requests, psycopg2, sibling flask-importing modules)
are LAZY so this module imports cleanly in a no-flask test env where every
GitHub / DB / LLM primitive is monkeypatched. Tests must never import main.
"""
from __future__ import annotations

import ast
import json
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


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


SMOKE_WORKFLOW_FILE = os.environ.get("SMOKEREG_WORKFLOW_FILE",
                                     "post-deploy-smoke.yml")
RECIPE = "smoke_regression"
BRANCH_PREFIX = "brain/smokefix-"   # NOT brain/autofix- (mechanical lane)

# Terminal states never advance again.
TERMINAL_STATES = ("resolved", "escalated", "abandoned")


def _consec_fails_required() -> int:
    return max(1, _env_int("SMOKEREG_CONSEC_FAILS", 2))


def _max_attempts() -> int:
    """Fix-PR attempts per incident before escalate-and-STOP (default 2)."""
    return max(1, _env_int("SMOKEREG_MAX_ATTEMPTS", 2))


def _daily_cap() -> int:
    """Max fix PRs this recipe may open per UTC day (default 2)."""
    return max(0, _env_int("SMOKEREG_DAILY_CAP", 2))


def _max_diff_lines() -> int:
    return max(1, _env_int("SMOKEREG_MAX_DIFF_LINES", 12))


def _landing_wait_s() -> int:
    """Post-merge wait before the landing re-dispatch. Railway keeps the OLD
    replicas draining for several minutes after a promote; a smoke run inside
    that window sees the pre-fix 500 and is a FALSE red (twice on 07-16)."""
    return max(0, _env_int("SMOKEREG_LANDING_WAIT_S", 300))


def _landing_retries() -> int:
    return max(0, _env_int("SMOKEREG_LANDING_RETRIES", 1))


def _kill_switch_on() -> bool:
    return _truthy(os.environ.get("SMOKE_REGRESSION_DISABLE"))


def _real_pr_armed() -> bool:
    """Same arm switch as every other L22 real-PR writer."""
    return os.environ.get("DCHUB_L22_REAL_PR", "0") == "1"


def _probe_budget_s() -> float:
    """Wall-clock budget for the live-probe sweep inside one tick."""
    return max(5.0, float(_env_int("SMOKEREG_PROBE_BUDGET_S", 30)))


def _author_deadline_s() -> float:
    """Authoring (LLM + PR open) only starts if the tick has this much
    wall-clock left — keeps a tick inside the gunicorn 120s window."""
    return max(10.0, float(_env_int("SMOKEREG_AUTHOR_DEADLINE_S", 35)))


# ── GitHub client (reuses the draft writer's single token/config) ────
def _gh_cfg() -> dict:
    from routes.brain_draft_pr_writer import _gh_config
    return _gh_config()


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_get(path: str, params: dict | None = None, timeout: int = 20) -> dict:
    """GET api.github.com/repos/{upstream}{path}. Returns {ok, json} or
    {ok:False, error}. Never raises. Tests monkeypatch this."""
    cfg = _gh_cfg()
    token = cfg.get("token")
    if not token:
        return {"ok": False, "error": "no_token"}
    try:
        import requests
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": f"deps:{e}"}
    try:
        r = requests.get(
            f"https://api.github.com/repos/{cfg['upstream']}{path}",
            headers=_gh_headers(token), params=params or {}, timeout=timeout)
        if r.status_code != 200:
            return {"ok": False, "error": f"{r.status_code}:{r.text[:160]}"}
        return {"ok": True, "json": r.json()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}:{str(e)[:160]}"}


def _gh_post(path: str, body: dict, timeout: int = 20,
             ok_codes=(200, 201, 204)) -> dict:
    """POST helper mirroring _gh_get. Tests monkeypatch this."""
    cfg = _gh_cfg()
    token = cfg.get("token")
    if not token:
        return {"ok": False, "error": "no_token"}
    try:
        import requests
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": f"deps:{e}"}
    try:
        r = requests.post(
            f"https://api.github.com/repos/{cfg['upstream']}{path}",
            headers=_gh_headers(token), json=body, timeout=timeout)
        if r.status_code not in ok_codes:
            return {"ok": False, "error": f"{r.status_code}:{r.text[:200]}"}
        try:
            return {"ok": True, "json": (r.json() if r.text else {})}
        except Exception:
            return {"ok": True, "json": {}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}:{str(e)[:160]}"}


def _gh_patch(path: str, body: dict, timeout: int = 20) -> dict:
    cfg = _gh_cfg()
    token = cfg.get("token")
    if not token:
        return {"ok": False, "error": "no_token"}
    try:
        import requests
    except Exception as e:  # pragma: no cover
        return {"ok": False, "error": f"deps:{e}"}
    try:
        r = requests.patch(
            f"https://api.github.com/repos/{cfg['upstream']}{path}",
            headers=_gh_headers(token), json=body, timeout=timeout)
        if r.status_code not in (200, 201):
            return {"ok": False, "error": f"{r.status_code}:{r.text[:160]}"}
        return {"ok": True, "json": r.json()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}:{str(e)[:160]}"}


# ── (1) DETECT — workflow-run streak + live probe identification ─────
def recent_smoke_runs(limit: int = 30) -> list:
    """Newest-first COMPLETED runs of the post-deploy-smoke workflow:
    [{id, conclusion, created_at, head_sha, event}]. [] on any failure.
    Tests monkeypatch this (or feed detect_streak directly)."""
    res = _gh_get(f"/actions/workflows/{SMOKE_WORKFLOW_FILE}/runs",
                  params={"status": "completed", "per_page": limit})
    if not res.get("ok"):
        return []
    out = []
    for r in ((res.get("json") or {}).get("workflow_runs") or []):
        out.append({
            "id": r.get("id"),
            "conclusion": (r.get("conclusion") or "").lower(),
            "created_at": r.get("created_at"),
            "head_sha": r.get("head_sha"),
            "event": r.get("event"),
        })
    return out


def detect_streak(runs: list, min_consec: int | None = None) -> dict:
    """PURE. Given newest-first completed runs, return {triggered,
    consec_fails, first_red, newest_red, last_green}. first_red is the
    OLDEST run in the current leading failure streak (stable incident key);
    newest_red is the NEWEST (the compare window runs last_green..newest_red
    — the workflow is probe-AGNOSTIC, so on a multi-failure day the guilty
    commit for the probe being fixed can land after the streak began; only
    the last_green..newest_red superset is guaranteed to contain it). A
    cancelled/skipped run breaks nothing — only 'success' ends the streak;
    only 'failure' extends it."""
    need = _consec_fails_required() if min_consec is None else int(min_consec)
    streak: list = []
    last_green = None
    for r in runs:
        c = r.get("conclusion")
        if last_green is None:
            if c == "failure":
                streak.append(r)
                continue
            if c == "success":
                last_green = r
                continue
            continue  # cancelled/skipped/etc — ignore
        break
    first_red = streak[-1] if streak else None
    return {
        "triggered": len(streak) >= need and first_red is not None,
        "consec_fails": len(streak),
        "first_red": first_red,
        "newest_red": streak[0] if streak else None,
        "last_green": last_green,
    }


def _api_base() -> str:
    return os.environ.get("DCHUB_API_BASE",
                          "https://dchub-api-production.up.railway.app")


def _smoke_checks() -> list:
    """smoke_test.py's SMOKE_CHECKS table — (name, path, method, needs_auth,
    timeout, expected). Lazy import (smoke_test pulls psycopg2/internal_auth).
    Tests monkeypatch this."""
    try:
        from smoke_test import SMOKE_CHECKS
        return list(SMOKE_CHECKS)
    except Exception:
        return []


def _expected_ok(status: int, expected) -> bool:
    if isinstance(expected, (tuple, list, set)):
        return status in expected
    return status == expected


def _probe_once(path: str, timeout: float = 8.0) -> tuple[int, str]:
    """GET one probe URL. Returns (status, body_preview). 0 = transport error.
    Tests monkeypatch this."""
    try:
        import requests
    except Exception as e:  # pragma: no cover
        return 0, f"deps:{e}"
    url = _api_base().rstrip("/") + path
    try:
        r = requests.get(url, timeout=timeout, headers={
            "User-Agent": "DCHub-SmokeRegression/1.0"})
        return r.status_code, (r.text or "")[:500]
    except Exception as e:
        return 0, str(e)[:200]


def live_probe_failures(budget_s: float | None = None) -> list:
    """Probe every SMOKE_CHECKS row live and return the failures:
    [{name, path, status, body, expected}]. Short per-probe timeout + a
    wall-clock budget so a fully-down backend can't stall the tick; stops
    after the FIRST failure (we repair one probe at a time anyway)."""
    budget = _probe_budget_s() if budget_s is None else budget_s
    t0 = time.time()
    fails = []
    for row in _smoke_checks():
        try:
            name, path, method, _auth, _t, expected = row
        except Exception:
            continue
        if (method or "GET").upper() != "GET":
            continue
        if time.time() - t0 > budget:
            break
        status, body = _probe_once(path, timeout=6.0)
        if not _expected_ok(status, expected):
            fails.append({"name": name, "path": path, "status": status,
                          "body": body, "expected": expected})
            break  # one incident at a time — first failing probe wins
    return fails


def _hard_burn_probe_paths() -> list:
    """Open L14 slo_hard_burn findings whose pattern maps onto a smoke-covered
    path. Returns [{name, path, pattern}]. Best-effort; [] on any failure.
    Tests monkeypatch this."""
    patterns = []
    try:
        import psycopg2
    except Exception:
        return []
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return []
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT url FROM brain_findings "
                "WHERE issue = 'slo_hard_burn' "
                "  AND COALESCE(status,'open') = 'open' "
                "ORDER BY id DESC LIMIT 50")
            patterns = [(r[0] or "") for r in cur.fetchall()]
    except Exception:
        return []
    out = []
    for row in _smoke_checks():
        try:
            name, path = row[0], row[1]
        except Exception:
            continue
        base = path.split("?")[0]
        for pat in patterns:
            if base and base in (pat or ""):
                out.append({"name": name, "path": path, "pattern": pat})
                break
    return out


# ── (2) CONTEXT PACK ─────────────────────────────────────────────────
# Schema-drift signatures (issue #1604 folded in). The 07-16 root cause was
# EXACTLY this class: a column ALTERed INTEGER→TEXT while a query still
# compared it to an integer → 'operator does not exist: text = integer'.
_SCHEMA_DRIFT_SIGNATURES = (
    r"operator does not exist",
    r"column .{0,80} does not exist",
    r"relation .{0,80} does not exist",
    r"invalid input syntax for",
    r"UndefinedColumn|UndefinedTable",
    r"cannot cast type",
)

_CLASS_GUIDANCE = {
    "schema_drift": (
        "This error signature is SCHEMA DRIFT: a query compares/joins a "
        "column whose live type or name no longer matches the SQL (e.g. a "
        "column ALTERed INTEGER->TEXT while the query still compares it to "
        "an integer, or a join keyed on the wrong id-space). The correct fix "
        "is to make the query match the LIVE schema: join/compare in the "
        "right id-space, add the explicit cast, or reference the renamed "
        "column. Do NOT wrap the query in try/except; fix the SQL itself."),
}


def classify_error_text(text: str) -> str | None:
    """'schema_drift' when the error text matches a known drift signature."""
    low = (text or "")
    for sig in _SCHEMA_DRIFT_SIGNATURES:
        if re.search(sig, low, re.IGNORECASE):
            return "schema_drift"
    return None


def compare_window(base_sha: str, head_sha: str) -> dict:
    """GH compare base...head → {ok, commits:[{sha,message}], files:
    [{filename, patch}]} capped for prompt budget. Tests monkeypatch this."""
    if not base_sha or not head_sha:
        return {"ok": False, "error": "missing_shas"}
    res = _gh_get(f"/compare/{base_sha}...{head_sha}", timeout=30)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error")}
    j = res.get("json") or {}
    commits = [{"sha": (c.get("sha") or "")[:10],
                "message": ((c.get("commit") or {}).get("message") or
                            "").split("\n")[0][:120]}
               for c in (j.get("commits") or [])][:60]
    files = []
    total = 0
    for f in (j.get("files") or []):
        fn = f.get("filename") or ""
        patch = f.get("patch") or ""
        if total + len(patch) > 24000:
            patch = patch[: max(0, 24000 - total)]
        total += len(patch)
        files.append({"filename": fn, "patch": patch})
        if len(files) >= 40:
            break
    return {"ok": True, "commits": commits, "files": files}


def _route_error_patterns(probe_path: str) -> str:
    """Best-effort recent 5xx pattern text for the probe's route from the
    brain's HTTP-error capture. '' on any failure."""
    base = probe_path.split("?")[0]
    try:
        import requests
        port = int(os.environ.get("PORT", 8080))
        r = requests.get(
            f"http://127.0.0.1:{port}/api/v1/brain/http-errors/patterns",
            params={"window": 21600}, timeout=6)
        if r.status_code != 200:
            return ""
        lines = []
        for p in ((r.json() or {}).get("patterns") or []):
            key = str(p.get("pattern") or "")
            if base in key:
                lines.append(f"{key} count={p.get('count')} "
                             f"sample={str(p.get('sample') or '')[:200]}")
        return "\n".join(lines[:5])
    except Exception:
        return ""


def _current_excerpts(window_files: list, probe_path: str,
                      max_files: int = 3, ctx_lines: int = 60) -> list:
    """For the most-relevant changed .py files, fetch CURRENT content off
    live main and excerpt +-ctx_lines around each region the window's patch
    touched. The LLM must copy search_text EXACTLY from current content, so
    it needs these excerpts (a 35k-line main.py can't ship whole)."""
    from routes.brain_draft_pr_writer import get_file_on_main

    seg = probe_path.split("?")[0].rstrip("/").split("/")[-1]
    ranked = []
    for f in window_files:
        fn = f.get("filename") or ""
        if not fn.endswith(".py"):
            continue
        patch = f.get("patch") or ""
        score = len(patch)
        if seg and seg in patch:
            score += 100000
        if seg and seg in fn:
            score += 50000
        ranked.append((score, fn, patch))
    ranked.sort(reverse=True)

    out = []
    for _score, fn, patch in ranked[:max_files]:
        fetched = get_file_on_main(fn)
        if not fetched.get("ok"):
            continue
        content = fetched.get("content") or ""
        lines = content.split("\n")
        # Locate the CURRENT positions of the window's added lines.
        anchors = set()
        for pl in patch.split("\n"):
            if pl.startswith("+") and not pl.startswith("+++"):
                needle = pl[1:].strip()
                if len(needle) < 10:
                    continue
                for i, ln in enumerate(lines):
                    if needle in ln:
                        anchors.add(i)
                        break
        if not anchors and seg:
            for i, ln in enumerate(lines):
                if seg in ln:
                    anchors.add(i)
                    break
        if not anchors:
            continue
        regions = []
        for a in sorted(anchors):
            lo, hi = max(0, a - ctx_lines), min(len(lines), a + ctx_lines)
            if regions and lo <= regions[-1][1]:
                regions[-1] = (regions[-1][0], hi)
            else:
                regions.append((lo, hi))
        text = ""
        for lo, hi in regions[:4]:
            text += f"\n# --- {fn} current content, lines {lo + 1}-{hi} ---\n"
            text += "\n".join(lines[lo:hi])
            if len(text) > 12000:
                text = text[:12000]
                break
        out.append({"filename": fn, "excerpt": text})
    return out


def build_context_pack(incident: dict) -> dict:
    """Assemble the fix-attempt context for one incident."""
    probe_path = incident.get("probe_path") or ""
    status, body = _probe_once(probe_path, timeout=8.0)
    window = compare_window(incident.get("last_green_sha") or "",
                            incident.get("newest_red_sha")
                            or incident.get("first_red_sha") or "")
    error_text = (body or "") + "\n" + _route_error_patterns(probe_path)
    suspected = classify_error_text(error_text)
    excerpts = []
    if window.get("ok"):
        try:
            excerpts = _current_excerpts(window.get("files") or [], probe_path)
        except Exception:
            excerpts = []
    return {
        "probe": {
            "name": incident.get("probe_name"),
            "path": probe_path,
            "url": _api_base().rstrip("/") + probe_path,
            "live_status": status,
            "body_preview": (body or "")[:400],
        },
        "window": window,
        "error_text": error_text[:2000],
        "suspected_class": suspected,
        "excerpts": excerpts,
        "prior_attempt": (incident.get("detail") or "")[:800]
        if (incident.get("attempts") or 0) > 0 else "",
    }


# ── (3) AUTHOR — one LLM call, strict JSON, hard validation ──────────
def _models_chain() -> list:
    """Env pin > brain reasoning tier > confirmed-valid fallback (L14's
    pattern — a mispinned/retired model can't zero the recipe out)."""
    chain = []
    pin = (os.environ.get("SMOKEREG_MODEL") or "").strip()
    if pin:
        chain.append(pin)
    try:
        from routes.brain_models import brain_model_for
        m = brain_model_for("reasoning")
        if m and m not in chain:
            chain.append(m)
    except Exception:
        pass
    if "claude-sonnet-4-5" not in chain:
        chain.append("claude-sonnet-4-5")
    return chain


def _build_fix_prompt(pack: dict) -> str:
    probe = pack.get("probe") or {}
    window = pack.get("window") or {}
    commits = "\n".join(f"  {c['sha']} {c['message']}"
                        for c in (window.get("commits") or [])[:40])
    diffs = ""
    for f in (window.get("files") or []):
        diffs += f"\n### {f['filename']}\n{f.get('patch') or '(no patch)'}\n"
        if len(diffs) > 24000:
            diffs = diffs[:24000] + "\n[diff truncated]"
            break
    excerpts = "\n".join(e["excerpt"] for e in (pack.get("excerpts") or []))
    guidance = _CLASS_GUIDANCE.get(pack.get("suspected_class") or "", "")
    prior = pack.get("prior_attempt") or ""
    return f"""You are DC Hub's smoke-regression autofix recipe. A production smoke probe
started failing right after a deploy. Author the MINIMAL root-cause fix.

## Failing probe
name: {probe.get('name')}
GET {probe.get('url')}
live status: {probe.get('live_status')}
response preview: {probe.get('body_preview')}

## Recent error text for this route
{pack.get('error_text') or '(none captured)'}
{('## Error-class guidance: ' + guidance) if guidance else ''}

## Deploys between last-green and first-red (the regression is IN this window)
Commits:
{commits or '  (compare unavailable)'}

File diffs (what changed in the window):
{diffs or '(compare unavailable)'}

## CURRENT content excerpts of the touched files (live main — copy search_text from HERE)
{excerpts or '(no excerpts available)'}
{('## Prior failed attempt (do something DIFFERENT)' + chr(10) + prior) if prior else ''}

## Reply
Reply with ONLY one JSON object, no markdown fence:
{{"file_path": "<repo-relative path>", "search_text": "<exact snippet copied from CURRENT content>", "replace_text": "<fixed snippet>", "rationale": "<1-3 sentences>", "confidence": <0.0-1.0>}}

HARD RULES:
- ONE file, ONE contiguous snippet swap. search_text must be copied EXACTLY
  (whitespace included) from the CURRENT content above and must be unique in the file.
- file_path MUST be one of the files changed in the deploy window above.
- At most {_max_diff_lines()} changed lines. No new imports. No try/except that
  hides the error — fix the root cause (usually the query/join itself).
- Never touch auth, billing, Stripe, migrations, or .github files.
- If you cannot produce a safe minimal fix, reply {{"no_fix": true, "reason": "..."}}."""


def _call_llm(prompt: str) -> str | None:
    """Raw-HTTP Anthropic call with the model fallback chain. Returns the
    text body or None. Tests monkeypatch this."""
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        return None
    try:
        import requests
        from utils.anthropic_helper import anthropic_messages_url
    except Exception:
        return None
    for model in _models_chain():
        try:
            r = requests.post(
                anthropic_messages_url(),
                headers={"x-api-key": key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json",
                         "User-Agent": "dchub-brain-smokereg/1.0"},
                json={"model": model, "max_tokens": 4000,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=75)
            if r.status_code != 200:
                continue
            blocks = (r.json() or {}).get("content") or []
            text = "".join(b.get("text", "") for b in blocks
                           if b.get("type") == "text")
            if text.strip():
                return text
        except Exception:
            continue
    return None


def author_fix(pack: dict) -> dict:
    """LLM → parsed fix dict, or {no_fix, reason}."""
    raw = _call_llm(_build_fix_prompt(pack))
    if not raw:
        return {"no_fix": True, "reason": "llm_unavailable_or_empty"}
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {"no_fix": True, "reason": "unparseable_llm_reply"}
    try:
        fix = json.loads(raw[start:end + 1])
    except Exception:
        return {"no_fix": True, "reason": "json_parse_failed"}
    if fix.get("no_fix"):
        return {"no_fix": True,
                "reason": str(fix.get("reason") or "model_declined")[:300]}
    return fix


# Forbidden diff tokens (mirrors L22's list — this is a FIX lane, never a
# migration/exec lane).
_FORBIDDEN_DIFF_PATTERNS = (
    r"DROP\s+TABLE", r"DELETE\s+FROM", r"ALTER\s+TABLE",
    r"os\.environ\[.*\]\s*=", r"subprocess", r"\beval\(", r"\bexec\(",
    r"shutil\.rmtree", r"os\.remove",
)


def validate_fix(fix: dict, pack: dict) -> dict:
    """Hard gates before anything is written. Returns
    {ok, reasons, new_content, file_path, search_text, replace_text}."""
    reasons = []
    file_path = str(fix.get("file_path") or "").strip().lstrip("/")
    search_text = fix.get("search_text") or ""
    replace_text = fix.get("replace_text") or ""

    window_files = {f.get("filename") for f
                    in ((pack.get("window") or {}).get("files") or [])}
    if not file_path:
        reasons.append("no_file_path")
    elif window_files and file_path not in window_files:
        reasons.append("file_not_in_deploy_window")

    try:
        from routes.brain_mechanical_classifier import _forbidden_path_hits
        hits = _forbidden_path_hits(file_path)
        if hits:
            reasons.append("forbidden_path:" + ",".join(hits[:3]))
    except Exception:
        reasons.append("forbidden_path_check_unavailable")
    if file_path.startswith(".github/") or file_path.endswith((".yml", ".yaml")):
        reasons.append("workflow_files_off_limits")

    if len(search_text.strip()) < 10:
        reasons.append("search_text_too_short")
    if not replace_text or replace_text == search_text:
        reasons.append("replace_equals_search_or_empty")

    changed = max(len(search_text.split("\n")), len(replace_text.split("\n")))
    if changed > _max_diff_lines():
        reasons.append(f"diff_too_large:{changed}>{_max_diff_lines()}")

    for pat in _FORBIDDEN_DIFF_PATTERNS:
        if re.search(pat, replace_text, re.IGNORECASE) and \
                not re.search(pat, search_text, re.IGNORECASE):
            reasons.append(f"forbidden_diff_token:{pat}")
    if re.search(r"^\s*(import|from)\s+\w", replace_text, re.MULTILINE) and \
            not re.search(r"^\s*(import|from)\s+\w", search_text, re.MULTILINE):
        reasons.append("adds_an_import")

    new_content = None
    if not reasons:
        from routes.brain_draft_pr_writer import get_file_on_main
        fetched = get_file_on_main(file_path)
        if not fetched.get("ok"):
            reasons.append("fetch_main_failed:" + str(fetched.get("error"))[:80])
        else:
            content = fetched.get("content") or ""
            occ = content.count(search_text)
            if occ != 1:
                reasons.append(f"search_text_not_exactly_once:{occ}")
            else:
                new_content = content.replace(search_text, replace_text, 1)
                if file_path.endswith(".py"):
                    try:
                        ast.parse(new_content)
                    except Exception as e:
                        reasons.append(f"ast_parse_failed:{str(e)[:80]}")
                        new_content = None
    return {"ok": not reasons, "reasons": reasons, "new_content": new_content,
            "file_path": file_path, "search_text": search_text,
            "replace_text": replace_text,
            "rationale": str(fix.get("rationale") or "")[:500],
            "confidence": fix.get("confidence")}


# ── (4) PR ───────────────────────────────────────────────────────────
def _build_pr_body(incident: dict, pack: dict, valid: dict) -> str:
    probe = pack.get("probe") or {}
    window = pack.get("window") or {}
    commits = "\n".join(f"- `{c['sha']}` {c['message']}"
                        for c in (window.get("commits") or [])[:20])
    return f"""**Auto-authored by the brain's `smoke_regression` recipe**
([routes/brain_smoke_regression.py](https://github.com/{_gh_cfg().get('upstream', 'azmartone67/dchub-backend')}/blob/main/routes/brain_smoke_regression.py))

## Triggering incident
- Probe **{probe.get('name')}** (`GET {probe.get('path')}`) failing: live status {probe.get('live_status')}, {incident.get('consec_fails')} consecutive red smoke runs.
- Suspected class: `{pack.get('suspected_class') or 'unknown'}`
- Error preview: `{(probe.get('body_preview') or '')[:200]}`

## Deploy window (last-green `{(incident.get('last_green_sha') or '')[:10]}` → first-red `{(incident.get('first_red_sha') or '')[:10]}`)
{commits or '(compare unavailable)'}

## Fix (attempt {incident.get('attempts', 0) + 1}/{_max_attempts()})
`{valid.get('file_path')}` — {valid.get('rationale')}

## Merge + landing plan
1. pre-merge gauntlet must be GREEN (fail-closed).
2. This lane squash-merges only while BRAIN_AUTOMERGE_ENABLED=1 (+ dry-run off, breaker clear, health green).
3. **Landing verification**: {_landing_wait_s()}s after merge (Railway replica drain window), the post-deploy-smoke workflow is re-dispatched. Only a green run + a live-green `{probe.get('path')}` marks this incident resolved. Red landing retries once, then escalates and STOPS.

## How to revert
`git revert <merge-commit>` — single-file, isolated.

---
_Recipe safety: kill switch SMOKE_REGRESSION_DISABLE=1 · attempts cap {_max_attempts()} · daily PR cap {_daily_cap()} · diff cap {_max_diff_lines()} lines · forbidden paths enforced._
"""


def open_fix_pr(incident: dict, pack: dict, valid: dict) -> dict:
    """Open the REAL fix PR via the draft writer's REST flow. Tests
    monkeypatch open_draft_pr_with_content."""
    from routes.brain_draft_pr_writer import open_draft_pr_with_content
    probe = (incident.get("probe_name") or "probe").replace("_", "-")[:24]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    attempt = (incident.get("attempts") or 0) + 1
    branch = f"{BRANCH_PREFIX}{probe}-a{attempt}-{stamp}"
    title = (f"[brain-autofix:{RECIPE}] {valid['file_path']} "
             f"({incident.get('probe_name')} probe)")
    commit_msg = (
        f"[brain-autofix:{RECIPE}] fix {incident.get('probe_name')} probe "
        f"regression in {valid['file_path']}\n\n"
        f"{valid.get('rationale') or ''}\n\n"
        f"Incident: {incident.get('incident_key')}\n"
        f"Window: {(incident.get('last_green_sha') or '')[:10]}.."
        f"{(incident.get('first_red_sha') or '')[:10]}\n"
        f"Co-Authored-By: Brain smoke_regression recipe "
        f"<l22-bot@dchub.cloud>")
    return open_draft_pr_with_content(
        file_path=valid["file_path"], new_content=valid["new_content"],
        branch=branch, title=title,
        body=_build_pr_body(incident, pack, valid), commit_msg=commit_msg)


# ── (5) MERGE gates (shared arm switches with brain_automerge) ───────
def _automerge_armed() -> dict:
    """All shared merge preconditions. Returns {armed, reason}."""
    try:
        from routes.brain_automerge import (
            _enabled, _dry_run, breaker_tripped, health_db_green)
    except Exception as e:
        return {"armed": False, "reason": f"automerge_import_failed:{e}"}
    if not _enabled():
        return {"armed": False, "reason": "BRAIN_AUTOMERGE_ENABLED!=1"}
    if _dry_run():
        return {"armed": False, "reason": "BRAIN_AUTOMERGE_DRY_RUN=1"}
    try:
        if breaker_tripped():
            return {"armed": False, "reason": "breaker_tripped"}
    except Exception:
        return {"armed": False, "reason": "breaker_check_failed"}
    try:
        if not health_db_green():
            return {"armed": False, "reason": "health_degraded"}
    except Exception:
        return {"armed": False, "reason": "health_check_failed"}
    return {"armed": True, "reason": "armed"}


def _pr_state(pr_number: int) -> dict:
    """{ok, head_sha, draft, state, merged, node_id}. Tests monkeypatch."""
    res = _gh_get(f"/pulls/{pr_number}")
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error")}
    j = res.get("json") or {}
    return {"ok": True,
            "head_sha": ((j.get("head") or {}).get("sha")),
            "draft": bool(j.get("draft")),
            "state": j.get("state"),
            "merged": bool(j.get("merged")),
            "node_id": j.get("node_id")}


def _close_pr(pr_number: int, comment: str = "") -> dict:
    if comment:
        _gh_post(f"/issues/{pr_number}/comments", {"body": comment[:1000]})
    return _gh_patch(f"/pulls/{pr_number}", {"state": "closed"})


# ── (6) LANDING VERIFICATION ─────────────────────────────────────────
def dispatch_smoke_workflow() -> dict:
    """workflow_dispatch the smoke workflow on main. Tests monkeypatch."""
    return _gh_post(
        f"/actions/workflows/{SMOKE_WORKFLOW_FILE}/dispatches",
        {"ref": _gh_cfg().get("base", "main")}, ok_codes=(204,))


def _dispatch_run_since(since_iso: str) -> dict:
    """Newest workflow_dispatch run created at/after since_iso.
    {found, completed, conclusion, id}. Tests monkeypatch."""
    res = _gh_get(f"/actions/workflows/{SMOKE_WORKFLOW_FILE}/runs",
                  params={"event": "workflow_dispatch", "per_page": 5})
    if not res.get("ok"):
        return {"found": False, "error": res.get("error")}
    for r in ((res.get("json") or {}).get("workflow_runs") or []):
        created = r.get("created_at") or ""
        if created >= since_iso:
            return {"found": True,
                    "completed": (r.get("status") == "completed"),
                    "conclusion": (r.get("conclusion") or "").lower(),
                    "id": r.get("id")}
    return {"found": False}


def _probe_now_green(incident: dict) -> bool:
    """Live-probe the incident's previously-failing path right now."""
    expected = 200
    for row in _smoke_checks():
        try:
            if row[0] == incident.get("probe_name"):
                expected = row[5]
                break
        except Exception:
            continue
    status, _body = _probe_once(incident.get("probe_path") or "", timeout=8.0)
    return _expected_ok(status, expected)


# ── (7) ESCALATION ───────────────────────────────────────────────────
def escalate(incident: dict, reason: str) -> dict:
    """ONE deduped high-priority issue, then STOP. Tests monkeypatch the
    GH primitives."""
    title = (f"[smoke-regression] ESCALATION: {incident.get('probe_name')} "
             f"probe regression — autofix stopped ({reason})"[:200])
    # Search-before-create (fail-CLOSED like L22's issue dedup).
    q_res = _gh_get("/issues", params={"state": "open",
                                       "labels": "brain-smoke-regression",
                                       "per_page": 50})
    if q_res.get("ok"):
        for it in (q_res.get("json") or []):
            t = it.get("title") or ""
            if incident.get("probe_name") and \
                    f"ESCALATION: {incident.get('probe_name')} probe" in t:
                return {"ok": True, "deduped": True,
                        "issue_url": it.get("html_url")}
    body = (
        f"The `smoke_regression` autofix recipe is STOPPING on this incident "
        f"after {incident.get('attempts')} attempt(s): **{reason}**.\n\n"
        f"- Probe: `{incident.get('probe_name')}` → "
        f"`GET {incident.get('probe_path')}` (status {incident.get('probe_status')})\n"
        f"- Incident: `{incident.get('incident_key')}`\n"
        f"- Deploy window: `{(incident.get('last_green_sha') or '')[:10]}` → "
        f"`{(incident.get('first_red_sha') or '')[:10]}`\n"
        f"- Last fix PR: {incident.get('pr_url') or '(none produced)'}\n"
        f"- Detail: {(incident.get('detail') or '')[:600]}\n\n"
        f"A human needs to repair this route. The recipe will not re-fire "
        f"for this probe for 24h.\n\n"
        f"_Filed by routes/brain_smoke_regression.py (never re-fires; "
        f"one issue per incident)._")
    res = _gh_post("/issues", {
        "title": title, "body": body,
        "labels": ["brain-smoke-regression", "high-priority"]})
    if not res.get("ok"):
        res = _gh_post("/issues", {"title": title, "body": body})
    return {"ok": res.get("ok"),
            "issue_url": ((res.get("json") or {}).get("html_url")),
            "error": res.get("error")}


# ── DB: incident store ───────────────────────────────────────────────
def _db_url() -> str:
    return (os.environ.get("NEON_DATABASE_URL")
            or os.environ.get("DATABASE_URL") or "")


def _ensure_table() -> bool:
    try:
        import psycopg2
    except Exception:
        return False
    url = _db_url()
    if not url:
        return False
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_smoke_regression_incidents (
                    id BIGSERIAL PRIMARY KEY,
                    incident_key TEXT UNIQUE NOT NULL,
                    probe_name TEXT NOT NULL,
                    probe_path TEXT NOT NULL,
                    probe_status INTEGER,
                    trigger_source TEXT NOT NULL DEFAULT 'smoke_workflow',
                    first_red_run_id BIGINT,
                    first_red_sha TEXT,
                    newest_red_sha TEXT,
                    last_green_run_id BIGINT,
                    last_green_sha TEXT,
                    consec_fails INTEGER,
                    state TEXT NOT NULL DEFAULT 'detected',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    pr_number INTEGER,
                    pr_url TEXT,
                    branch TEXT,
                    file_path TEXT,
                    search_text TEXT,
                    replace_text TEXT,
                    rationale TEXT,
                    merge_sha TEXT,
                    merged_at TIMESTAMPTZ,
                    landing_dispatched_at TIMESTAMPTZ,
                    landing_retries INTEGER NOT NULL DEFAULT 0,
                    escalation_issue_url TEXT,
                    detail TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS brain_smokereg_state_idx
                  ON brain_smoke_regression_incidents(state, updated_at DESC);
            """)
            conn.commit()
        return True
    except Exception:
        return False


def _active_incidents() -> list:
    """Non-terminal incidents, oldest first. Tests monkeypatch."""
    try:
        import psycopg2
        import psycopg2.extras
    except Exception:
        return []
    url = _db_url()
    if not url:
        return []
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT *, EXTRACT(EPOCH FROM merged_at) AS merged_epoch, "
                    "       EXTRACT(EPOCH FROM landing_dispatched_at) AS dispatched_epoch "
                    "FROM brain_smoke_regression_incidents "
                    "WHERE state NOT IN %s ORDER BY id ASC LIMIT 20",
                    (TERMINAL_STATES,))
                return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def _recent_incident_for_probe(probe_name: str, hours: int = 24) -> dict | None:
    """Newest incident for this probe updated within `hours` (any state).
    Used for the escalation STOP + dedup. Tests monkeypatch."""
    try:
        import psycopg2
        import psycopg2.extras
    except Exception:
        return None
    url = _db_url()
    if not url:
        return None
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM brain_smoke_regression_incidents "
                    "WHERE probe_name = %s "
                    "  AND updated_at > NOW() - %s * INTERVAL '1 hour' "
                    "ORDER BY id DESC LIMIT 1",
                    (probe_name, hours))
                r = cur.fetchone()
                return dict(r) if r else None
    except Exception:
        return None


def _insert_incident(inc: dict) -> int | None:
    """INSERT ... ON CONFLICT (incident_key) DO NOTHING. Tests monkeypatch."""
    try:
        import psycopg2
    except Exception:
        return None
    url = _db_url()
    if not url:
        return None
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brain_smoke_regression_incidents "
                "(incident_key, probe_name, probe_path, probe_status, "
                " trigger_source, first_red_run_id, first_red_sha, "
                " newest_red_sha, last_green_run_id, last_green_sha, "
                " consec_fails, state) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'detected') "
                "ON CONFLICT (incident_key) DO NOTHING RETURNING id",
                (inc["incident_key"], inc["probe_name"], inc["probe_path"],
                 inc.get("probe_status"), inc.get("trigger_source",
                                                  "smoke_workflow"),
                 inc.get("first_red_run_id"), inc.get("first_red_sha"),
                 inc.get("newest_red_sha"),
                 inc.get("last_green_run_id"), inc.get("last_green_sha"),
                 inc.get("consec_fails")))
            row = cur.fetchone()
            conn.commit()
        return row[0] if row else None
    except Exception:
        note_swallowed_write("brain_smoke_regression_incidents",
                             where="brain_smoke_regression._insert_incident")
        return None


_ALLOWED_UPDATE_COLS = {
    "state", "attempts", "pr_number", "pr_url", "branch", "file_path",
    "search_text", "replace_text", "rationale", "merge_sha", "merged_at",
    "landing_dispatched_at", "landing_retries", "escalation_issue_url",
    "detail", "probe_status",
}


def _update_incident(incident_id, **fields) -> bool:
    """UPDATE whitelisted columns + updated_at. Tests monkeypatch."""
    cols = {k: v for k, v in fields.items() if k in _ALLOWED_UPDATE_COLS}
    if not cols:
        return False
    try:
        import psycopg2
    except Exception:
        return False
    url = _db_url()
    if not url:
        return False
    try:
        sets = ", ".join(f"{k} = %s" for k in cols)
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE brain_smoke_regression_incidents "
                f"SET {sets}, updated_at = NOW() WHERE id = %s",
                (*cols.values(), incident_id))
            conn.commit()
        return True
    except Exception:
        note_swallowed_write("brain_smoke_regression_incidents",
                             where="brain_smoke_regression._update_incident")
        return False


def _prs_opened_today() -> int:
    """Attempts started today (UTC) across all incidents — the daily cap
    counter. Fail-CLOSED: on DB error return the cap so no PR opens blind.
    Tests monkeypatch."""
    try:
        import psycopg2
    except Exception:
        return _daily_cap()
    url = _db_url()
    if not url:
        return _daily_cap()
    try:
        with psycopg2.connect(url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(attempts), 0) "
                "FROM brain_smoke_regression_incidents "
                "WHERE updated_at >= date_trunc('day', NOW())")
            return int(cur.fetchone()[0] or 0)
    except Exception:
        return _daily_cap()


# ── STATE-MACHINE ADVANCES (each fault-isolated in the tick) ─────────
def _fail_attempt(inc: dict, why: str) -> dict:
    """A fix attempt failed (gauntlet red / landing red / unfixable). Retry
    with a fresh attempt if budget remains, else escalate + STOP."""
    attempts = int(inc.get("attempts") or 0)
    detail = (f"attempt {attempts} failed: {why} | "
              + (inc.get("detail") or ""))[:1500]
    if attempts >= _max_attempts():
        esc = escalate({**inc, "detail": detail}, why)
        _update_incident(inc["id"], state="escalated", detail=detail,
                         escalation_issue_url=esc.get("issue_url"))
        return {"incident": inc["incident_key"], "action": "escalated",
                "why": why, "issue_url": esc.get("issue_url")}
    _update_incident(inc["id"], state="detected", detail=detail,
                     landing_retries=0)
    return {"incident": inc["incident_key"], "action": "retry_queued",
            "why": why, "attempts": attempts}


def _advance_detected(inc: dict, deadline_t0: float) -> dict:
    """detected → author + validate + open PR (or no_fix → fail_attempt)."""
    if time.time() - deadline_t0 > _author_deadline_s():
        return {"incident": inc["incident_key"], "action": "deferred",
                "why": "tick_budget"}
    if _prs_opened_today() >= _daily_cap():
        return {"incident": inc["incident_key"], "action": "held",
                "why": f"daily_cap_{_daily_cap()}"}
    # If the probe healed on its own (rollback / another fix), close honestly.
    if _probe_now_green(inc):
        _update_incident(inc["id"], state="resolved",
                         detail="probe green before any fix PR "
                                "(healed externally)")
        return {"incident": inc["incident_key"], "action": "resolved",
                "why": "healed_externally"}
    pack = build_context_pack(inc)
    fix = author_fix(pack)
    if fix.get("no_fix"):
        _update_incident(inc["id"], attempts=int(inc.get("attempts") or 0) + 1)
        return _fail_attempt({**inc, "attempts": int(inc.get("attempts") or 0) + 1},
                             "no_fix:" + str(fix.get("reason"))[:120])
    valid = validate_fix(fix, pack)
    if not valid["ok"]:
        _update_incident(inc["id"], attempts=int(inc.get("attempts") or 0) + 1)
        return _fail_attempt({**inc, "attempts": int(inc.get("attempts") or 0) + 1},
                             "validation:" + ",".join(valid["reasons"])[:200])
    if not _real_pr_armed():
        _update_incident(
            inc["id"],
            detail=(f"PREVIEW (DCHUB_L22_REAL_PR!=1): would fix "
                    f"{valid['file_path']} — {valid['rationale']}")[:1500])
        return {"incident": inc["incident_key"], "action": "preview_only",
                "file_path": valid["file_path"]}
    res = open_fix_pr(inc, pack, valid)
    if not res.get("ok"):
        _update_incident(inc["id"], attempts=int(inc.get("attempts") or 0) + 1)
        return _fail_attempt({**inc, "attempts": int(inc.get("attempts") or 0) + 1},
                             "pr_open_failed:" + str(res.get("error"))[:160])
    pr_url = res.get("pr_url") or ""
    m = re.search(r"/pull/(\d+)", pr_url)
    _update_incident(
        inc["id"], state="pr_opened",
        attempts=int(inc.get("attempts") or 0) + 1,
        pr_number=int(m.group(1)) if m else None, pr_url=pr_url,
        branch=res.get("branch"), file_path=valid["file_path"],
        search_text=valid["search_text"], replace_text=valid["replace_text"],
        rationale=valid["rationale"])
    return {"incident": inc["incident_key"], "action": "pr_opened",
            "pr_url": pr_url, "file_path": valid["file_path"]}


def _advance_pr_opened(inc: dict) -> dict:
    """pr_opened → merge when the gauntlet is green AND the lane is armed."""
    pr_number = inc.get("pr_number")
    if not pr_number:
        return _fail_attempt(inc, "pr_number_missing")
    st = _pr_state(int(pr_number))
    if not st.get("ok"):
        return {"incident": inc["incident_key"], "action": "waiting",
                "why": "pr_fetch_failed:" + str(st.get("error"))[:80]}
    if st.get("merged"):
        _update_incident(inc["id"], state="merged",
                         merged_at=datetime.now(timezone.utc))
        return {"incident": inc["incident_key"], "action": "merged",
                "why": "merged_externally"}
    if st.get("state") == "closed":
        return _fail_attempt(inc, "pr_closed_unmerged")

    from routes.brain_automerge import (
        ci_status_for_sha, mark_ready_for_review, squash_merge_pr)
    ci = ci_status_for_sha(st.get("head_sha"))
    if not ci.get("green"):
        reason = str(ci.get("reason") or "")
        # A terminal red check = the gauntlet FAILED this fix.
        if reason.startswith("check_run_") and "pending" not in reason:
            _close_pr(int(pr_number),
                      "smoke_regression recipe: pre-merge gauntlet red "
                      f"({reason}) — closing this attempt.")
            return _fail_attempt(inc, "gauntlet_red:" + reason)
        return {"incident": inc["incident_key"], "action": "waiting",
                "why": "ci:" + reason}
    armed = _automerge_armed()
    if not armed["armed"]:
        return {"incident": inc["incident_key"], "action": "held",
                "why": armed["reason"]}
    if st.get("draft"):
        mr = mark_ready_for_review(st.get("node_id"))
        if not mr.get("ok"):
            return {"incident": inc["incident_key"], "action": "waiting",
                    "why": "mark_ready_failed"}
    merge = squash_merge_pr(
        int(pr_number), sha=st.get("head_sha"),
        commit_title=(f"[brain-automerge:{RECIPE}] {inc.get('file_path')} "
                      f"(PR #{pr_number})"))
    if not merge.get("ok"):
        return {"incident": inc["incident_key"], "action": "waiting",
                "why": "merge_failed:" + str(merge.get("error"))[:120]}
    _update_incident(inc["id"], state="merged",
                     merge_sha=merge.get("merge_sha"),
                     merged_at=datetime.now(timezone.utc))
    return {"incident": inc["incident_key"], "action": "merged",
            "merge_sha": merge.get("merge_sha")}


def _advance_merged(inc: dict) -> dict:
    """merged → dispatch the landing smoke run AFTER the drain wait."""
    merged_epoch = float(inc.get("merged_epoch") or 0)
    waited = time.time() - merged_epoch if merged_epoch else None
    if waited is not None and waited < _landing_wait_s():
        return {"incident": inc["incident_key"], "action": "waiting",
                "why": f"drain_window {int(waited)}s/{_landing_wait_s()}s"}
    d = dispatch_smoke_workflow()
    if not d.get("ok"):
        # Fall back to the direct probe as landing evidence.
        if _probe_now_green(inc):
            _update_incident(inc["id"], state="resolved",
                             detail="landing: dispatch failed; live probe "
                                    "green (probe-only verification)")
            return {"incident": inc["incident_key"], "action": "resolved",
                    "why": "probe_only_green"}
        return _fail_attempt(inc, "landing_dispatch_failed_and_probe_red")
    _update_incident(inc["id"], state="landing_dispatched",
                     landing_dispatched_at=datetime.now(timezone.utc))
    return {"incident": inc["incident_key"], "action": "landing_dispatched"}


def _advance_landing(inc: dict) -> dict:
    """landing_dispatched → resolved / retry / landed-red."""
    dispatched_epoch = float(inc.get("dispatched_epoch") or 0)
    since_iso = datetime.fromtimestamp(
        max(0.0, dispatched_epoch - 120), tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    run = _dispatch_run_since(since_iso)
    if not run.get("found") or not run.get("completed"):
        return {"incident": inc["incident_key"], "action": "waiting",
                "why": "landing_run_pending"}
    if run.get("conclusion") == "success":
        if _probe_now_green(inc):
            _update_incident(
                inc["id"], state="resolved",
                detail=(f"LANDED: smoke run {run.get('id')} green + probe "
                        f"live-green after merge "
                        f"{(inc.get('merge_sha') or '')[:10]}"))
            return {"incident": inc["incident_key"], "action": "resolved",
                    "landing_run": run.get("id")}
        return _fail_attempt(inc, "smoke_green_but_probe_red")
    # Red landing — retry once (drain-window false-red guard).
    retries = int(inc.get("landing_retries") or 0)
    if retries < _landing_retries():
        d = dispatch_smoke_workflow()
        if d.get("ok"):
            _update_incident(inc["id"],
                             landing_retries=retries + 1,
                             landing_dispatched_at=datetime.now(timezone.utc))
            return {"incident": inc["incident_key"],
                    "action": "landing_retry", "retry": retries + 1}
    return _fail_attempt(inc, f"landed_red_after_{retries + 1}_runs")


# ── DETECTION → new incident ─────────────────────────────────────────
def detect_new_incident(dry_run: bool = False) -> dict:
    """Run the trigger. Creates at most ONE new incident. Returns a report
    (in dry_run, reports what WOULD be created with zero writes)."""
    runs = recent_smoke_runs()
    streak = detect_streak(runs)
    trigger_source = "smoke_workflow"
    if not streak["triggered"]:
        # Secondary trigger: L14 hard_burn naming a smoke-covered route.
        burns = _hard_burn_probe_paths()
        if not burns:
            return {"detected": False, "consec_fails": streak["consec_fails"]}
        trigger_source = "l14_hard_burn"
    fails = live_probe_failures()
    if not fails:
        return {"detected": False, "consec_fails": streak["consec_fails"],
                "note": "workflow red but all probes live-green "
                        "(transient or non-probe failure)"}
    probe = fails[0]
    recent = _recent_incident_for_probe(probe["name"], hours=24)
    if recent and recent.get("state") == "escalated":
        return {"detected": False,
                "note": f"escalated<24h for probe {probe['name']} — STOP"}
    if recent and recent.get("state") not in TERMINAL_STATES:
        return {"detected": False,
                "note": f"active incident exists for probe {probe['name']}"}
    first_red = streak.get("first_red") or {}
    newest_red = streak.get("newest_red") or {}
    last_green = streak.get("last_green") or {}
    key = (f"{probe['name']}:{first_red.get('id')}"
           if first_red.get("id")
           else f"{probe['name']}:hardburn:"
                f"{datetime.now(timezone.utc).strftime('%Y%m%d')}")
    inc = {
        "incident_key": key,
        "probe_name": probe["name"],
        "probe_path": probe["path"],
        "probe_status": probe["status"],
        "trigger_source": trigger_source,
        "first_red_run_id": first_red.get("id"),
        "first_red_sha": first_red.get("head_sha"),
        "newest_red_sha": newest_red.get("head_sha"),
        "last_green_run_id": last_green.get("id"),
        "last_green_sha": last_green.get("head_sha"),
        "consec_fails": streak["consec_fails"],
    }
    if dry_run:
        return {"detected": True, "would_create": inc, "dry_run": True}
    new_id = _insert_incident(inc)
    return {"detected": True, "incident_key": key,
            "created": new_id is not None, "id": new_id, **{
                k: inc[k] for k in ("probe_name", "consec_fails",
                                    "trigger_source")}}


# ── THE TICK ─────────────────────────────────────────────────────────
_LAST_TICK = {"ts": None, "summary": None}


def smoke_regression_tick(dry_run: bool = False) -> dict:
    """Advance the recipe one step. Idempotent; safe every 30 min.
    Order: kill switch → detect (cheap) → advance each active incident
    (each stage fault-isolated; at most one authoring per tick)."""
    t0 = time.time()
    if _kill_switch_on():
        out = {"ok": True, "disabled": True,
               "note": "SMOKE_REGRESSION_DISABLE=1"}
        _LAST_TICK.update(ts=datetime.now(timezone.utc).isoformat(),
                          summary=out)
        return out

    out = {"ok": True, "disabled": False, "dry_run": dry_run,
           "as_of": datetime.now(timezone.utc).isoformat()}

    if not dry_run:
        _ensure_table()

    try:
        out["detection"] = detect_new_incident(dry_run=dry_run)
    except Exception as e:
        out["detection"] = {"error": f"{type(e).__name__}:{str(e)[:160]}"}

    advanced = []
    if dry_run:
        out["active_incidents"] = [
            {"incident_key": i.get("incident_key"), "state": i.get("state"),
             "attempts": i.get("attempts"), "pr_url": i.get("pr_url")}
            for i in _active_incidents()]
        out["note"] = "dry_run: read-only preview, zero writes"
        _LAST_TICK.update(ts=out["as_of"], summary=out)
        return out

    authored = False
    for inc in _active_incidents():
        state = inc.get("state")
        try:
            if state == "detected":
                if authored:
                    advanced.append({"incident": inc.get("incident_key"),
                                     "action": "deferred",
                                     "why": "one_authoring_per_tick"})
                    continue
                step = _advance_detected(inc, t0)
                if step.get("action") in ("pr_opened", "preview_only"):
                    authored = True
            elif state == "pr_opened":
                step = _advance_pr_opened(inc)
            elif state == "merged":
                step = _advance_merged(inc)
            elif state == "landing_dispatched":
                step = _advance_landing(inc)
            else:
                step = {"incident": inc.get("incident_key"),
                        "action": "skipped", "why": f"state:{state}"}
        except Exception as e:
            step = {"incident": inc.get("incident_key"), "action": "error",
                    "why": f"{type(e).__name__}:{str(e)[:160]}"}
        advanced.append(step)

    out["advanced"] = advanced
    out["elapsed_s"] = round(time.time() - t0, 1)
    _LAST_TICK.update(ts=out["as_of"], summary=out)
    return out


# ── BLUEPRINT (lazy — main.py registers via register(app)) ──────────
def make_blueprint():
    from flask import Blueprint, jsonify, request
    from routes.brain_mechanical_classifier import _admin_ok

    bp = Blueprint("brain_smoke_regression", __name__)

    @bp.after_request
    def _no_store(resp):
        resp.headers["Cache-Control"] = "no-store, private"
        return resp

    @bp.post("/api/v1/brain/smoke-regression/tick")
    def _tick():
        if not _admin_ok():
            return jsonify(ok=False, error="admin only"), 403
        dry = (request.args.get("dry_run") or "").lower() in ("1", "true", "yes")
        return jsonify(ok=True, result=smoke_regression_tick(dry_run=dry)), 200

    @bp.get("/api/v1/brain/smoke-regression/status")
    def _status():
        if not _admin_ok():
            return jsonify(ok=False, error="admin only"), 403
        return jsonify(
            ok=True,
            recipe=RECIPE,
            kill_switch_on=_kill_switch_on(),
            real_pr_armed=_real_pr_armed(),
            automerge=_automerge_armed(),
            last_tick=_LAST_TICK.get("ts"),
            last_summary=_LAST_TICK.get("summary"),
            config={
                "SMOKEREG_CONSEC_FAILS": _consec_fails_required(),
                "SMOKEREG_MAX_ATTEMPTS": _max_attempts(),
                "SMOKEREG_DAILY_CAP": _daily_cap(),
                "SMOKEREG_MAX_DIFF_LINES": _max_diff_lines(),
                "SMOKEREG_LANDING_WAIT_S": _landing_wait_s(),
                "SMOKEREG_LANDING_RETRIES": _landing_retries(),
                "workflow": SMOKE_WORKFLOW_FILE,
                "branch_prefix": BRANCH_PREFIX,
            },
            active_incidents=[
                {"incident_key": i.get("incident_key"),
                 "state": i.get("state"), "attempts": i.get("attempts"),
                 "probe": i.get("probe_name"), "pr_url": i.get("pr_url")}
                for i in _active_incidents()],
            note=("smoke_regression recipe: detect (smoke streak / L14 "
                  "hard_burn) → context (deploy window + error class) → "
                  "LLM fix → real PR → gauntlet-gated merge → LANDING "
                  "verification (post-merge smoke re-dispatch after the "
                  "Railway drain window) → escalate + STOP after "
                  f"{_max_attempts()} attempts."),
        ), 200

    return bp


def register(app) -> bool:
    """Register the blueprint. Mirrors brain_autonomy_loop.register."""
    try:
        app.register_blueprint(make_blueprint())
        return True
    except Exception:
        return False
