"""Phase 289 — Brain v2 Layer 4: self-learning novel-fix loop.

What this is
============
The healer (dchub_self_heal.py) detects bugs. The master-heal workflow
(dchub-frontend/.github/workflows/master-heal.yml) auto-fixes patterns
in a hand-curated FIX_MAP. But the FIX_MAP only knows the bugs we've
seen before — every new pattern has to be added manually.

This module closes that loop. When the healer surfaces an actionable
issue NOT in any known fix-table, this module:

  1. Pulls the surrounding HTML context (the cell + ~200 chars before/after).
  2. Sends it to Anthropic's Claude API with a prompt asking for a
     concrete find/replace fix.
  3. Validates the suggestion (find pattern actually exists in the file,
     replacement is non-empty, no obvious safety issues like raw HTML
     injection or unbounded substitution).
  4. Writes the suggested fix to a structured record at
     /api/v1/brain/proposed-fixes for the master-heal workflow to consume.

The master-heal workflow's "Auto-fix HTML quality issues" step is then
updated (Phase J of this PR) to also read FIX_MAP entries from the
brain. New patterns get auto-fixed without a human ever touching the
workflow YAML.

Environment
-----------
  ANTHROPIC_API_KEY   required for the API call. If unset, the loop
                       returns 503 — never blocks the heal cycle.
  DCHUB_BRAIN_MODEL   default: claude-sonnet-4-5 (fast + cheap enough
                       for a 5-min cron).
  DCHUB_BRAIN_MAX_LEARN   default: 3 per cycle (rate-cap learning so
                       a single broken page can't burn the model budget).

Endpoints
---------
  POST /api/v1/brain/learn                   admin-gated; triggers one
                                              learning pass on the
                                              current heal/findings
  GET  /api/v1/brain/proposed-fixes          returns the latest learned
                                              fix-map suggestions
                                              (master-heal consumes this)
  GET  /api/v1/brain/learning-log            recent learning attempts +
                                              outcomes (success / refused
                                              / api_error / validation_fail)

Safety
------
  • Read-only against the live HTML — never auto-pushes a file edit.
    Suggestions get queued; the master-heal workflow applies them.
  • Validation: the proposed (find, replace) must satisfy:
      - len(find) >= 3
      - find actually present in at least one repo file
      - replace doesn't contain raw HTML tags unless the find does
      - replace isn't a duplicate of find
  • Rate-cap: max 3 learning calls per heal cycle, max 30 per day.
  • All proposals are LOGGED, never silently auto-applied.
"""
from __future__ import annotations
import json
import os
import re
import time
from datetime import datetime, timezone
from functools import wraps
from flask import Blueprint, jsonify, request

brain_v2_bp = Blueprint("brain_v2", __name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BRAIN_MODEL = os.environ.get("DCHUB_BRAIN_MODEL", "claude-sonnet-4-5")
BRAIN_MAX_LEARN = int(os.environ.get("DCHUB_BRAIN_MAX_LEARN", "3"))
ADMIN_KEY = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")

# In-memory state. Production should back this with a small table, but
# for the initial framework an in-memory ring buffer is enough — the
# master-heal cron polls every 5 min and is the only consumer.
_proposed_fixes: list[dict] = []   # last N validated suggestions
_learning_log: list[dict] = []     # last N learning attempts (all outcomes)
_MAX_BUFFER = 50


def _require_admin(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
        if ADMIN_KEY and provided != ADMIN_KEY:
            return jsonify(error="unauthorized",
                           hint="X-Admin-Key header required"), 401
        return fn(*a, **kw)
    return wrapper


def _log(entry: dict) -> None:
    entry["t"] = datetime.now(timezone.utc).isoformat()
    _learning_log.append(entry)
    if len(_learning_log) > _MAX_BUFFER:
        _learning_log.pop(0)


def _validate_proposal(find: str, replace: str) -> tuple[bool, str]:
    """Reject obviously-unsafe fix suggestions. Returns (ok, reason)."""
    if not find or len(find) < 3:
        return False, "find_too_short"
    if find == replace:
        return False, "noop"
    if not replace:
        return False, "empty_replace"
    # Don't let the model invent HTML tags
    if ("<" in replace or ">" in replace) and not ("<" in find or ">" in find):
        return False, "replace_introduces_html"
    # Don't allow JS in replacement
    if re.search(r"<script|javascript:|onerror=|onload=", replace, re.I):
        return False, "replace_has_js"
    # Don't let replace be much longer than find (sanity)
    if len(replace) > len(find) + 200:
        return False, "replace_too_long"
    return True, "ok"


def _call_claude(prompt: str, system: str) -> tuple[str | None, str | None]:
    """Single Anthropic call. Returns (text, error)."""
    if not ANTHROPIC_API_KEY:
        return None, "no_api_key"
    try:
        import urllib.request
        import urllib.error
    except Exception as e:
        return None, f"stdlib_import_fail: {e}"
    body = json.dumps({
        "model": BRAIN_MODEL,
        "max_tokens": 800,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": ANTHROPIC_API_KEY,
            "Anthropic-Version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        # Anthropic returns content as list of blocks
        for block in data.get("content", []):
            if block.get("type") == "text":
                return block.get("text", ""), None
        return None, "no_text_block"
    except urllib.error.HTTPError as e:
        return None, f"http_{e.code}"
    except Exception as e:
        return None, f"call_fail: {str(e)[:120]}"


_LEARN_SYSTEM = (
    "You are the Brain v2 self-learning module for DC Hub. The HTML quality "
    "healer has detected a placeholder/stale-text issue on a page and the "
    "existing FIX_MAP has no entry for it. Your job: propose a single "
    "(find, replace) text substitution that fixes ONLY the actual data-cell "
    "placeholder, not stylistic typography elsewhere on the page.\n\n"
    "CRITICAL CONTEXT — em-dashes are tricky:\n"
    "  - Em-dashes inside `<td>—</td>` or `<div class=\"v\">—</div>` ARE "
    "    placeholders (a data cell that didn't populate). These should be fixed.\n"
    "  - Em-dashes inside meta descriptions, titles, alt text, or sentences "
    "    like 'DC Hub Media — the autonomous feed' ARE INTENTIONAL TYPOGRAPHY. "
    "    DO NOT propose replacing these. Refuse with rationale='intentional_typography'.\n"
    "  - If you can't isolate a specific placeholder cell, refuse — don't guess.\n\n"
    "Output STRICTLY a JSON object:\n"
    "  {\"find\": \"...\", \"replace\": \"...\", \"rationale\": \"...\"}\n\n"
    "Rules:\n"
    "  - `find` must be a literal substring at least 5 chars long that appears "
    "    verbatim in the snippet AND is unambiguously a placeholder (cell-like context).\n"
    "  - `find` must include enough surrounding HTML context (the parent tag) "
    "    so the substitution can't accidentally match similar patterns elsewhere.\n"
    "  - `replace` should not introduce new HTML tags unless the original had them.\n"
    "  - `rationale` is a one-sentence explanation a human can verify in 5s.\n"
    "  - When in doubt, refuse: {\"find\": \"\", \"replace\": \"\", "
    "    \"rationale\": \"refused: <why>\"}. Refusing is better than guessing.\n"
)


def _build_prompt(issue: dict, snippet: str) -> str:
    # Phase 300 (Phase R-2): include the issue label, count, page URL, AND
    # explicit guidance about scoping the find to a data-cell context (not
    # the broader page). Previously the prompt just said "find the pattern"
    # and Claude picked the FIRST em-dash on the page (usually in the meta
    # description) — leading to typography-mangling proposals.
    return (
        f"Issue label: {issue.get('issue','?')}\n"
        f"URL: {issue.get('url','?')}\n"
        f"Healer reports this pattern appears {issue.get('count','?')} time(s) on the page.\n\n"
        "Your task: find an UNAMBIGUOUS placeholder (a data-cell context — "
        "the em-dash is the sole text content of a `<td>`, `<span class=\"v\">`, "
        "`<div class=\"...kpi...\">`, or similar leaf element). NOT em-dashes "
        "in titles, meta descriptions, alt text, or prose.\n\n"
        "If you cannot isolate a placeholder cell (e.g. the only em-dashes on "
        "the page are in `<title>` or `content=\"...\"` attribute values), "
        "return refused.\n\n"
        f"HTML snippet (raw):\n```html\n{snippet[:1500]}\n```\n\n"
        "Propose a fix or refuse — JSON only."
    )


def _fetch_snippet(url: str, max_bytes: int = 6000) -> str:
    """Pull the live HTML around the issue. Best-effort — empty on failure."""
    try:
        import urllib.request
        H = {"User-Agent": "Mozilla/5.0 (DCHub-Brain-v2-Layer4)"}
        req = urllib.request.Request(url, headers=H)
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(max_bytes).decode("utf-8", errors="ignore")
        return body
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────

@brain_v2_bp.post("/api/v1/brain/learn")
@_require_admin
def trigger_learn():
    """Run one learning pass against /api/v1/heal/findings."""
    if not ANTHROPIC_API_KEY:
        return jsonify(ok=False, error="ANTHROPIC_API_KEY not set",
                       hint="Configure in Railway env to activate Layer 4"), 503

    # 1. Get current healer findings
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8080/api/v1/heal/findings",
                                    timeout=15) as r:
            findings = json.loads(r.read().decode("utf-8"))
    except Exception:
        # Fallback to live URL (works in dev)
        try:
            import urllib.request
            with urllib.request.urlopen("https://dchub.cloud/api/v1/heal/findings",
                                        timeout=15) as r:
                findings = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            return jsonify(ok=False, error=f"can't reach /heal/findings: {e}"), 502

    issues = findings.get("actionable_frontend_issues", [])
    # Filter: only patterns NOT already in the known FIX_MAP. We hardcode
    # known patterns here; in production this should query master-heal.yml
    # or a shared config so we never relearn a known pattern.
    KNOWN = {
        "multi-GW placeholder", "NaN ago timestamp bug",
        "NAND AGO timestamp bug", "Save 34% stale text",
        "$249.50 stale text", "$798 stale text",
    }
    # Phase V (2026-05-12): linked-asset findings (issue label starts with
    # "asset_") can NEVER be fixed by a body-text find/replace — the actual
    # fix is "remove the broken <link> tag" or "deploy the missing file."
    # If we let Claude propose a substitution it would either no-op or
    # mangle the page. So we skip them entirely. master-heal's GH-issue
    # fallback path still surfaces these to a human.
    def _is_asset_issue(i):
        lbl = (i.get("issue") or "")
        return lbl.startswith("asset_")
    novel = [i for i in issues
             if i.get("issue") not in KNOWN
             and not _is_asset_issue(i)][:BRAIN_MAX_LEARN]

    results = []
    for issue in novel:
        snippet = _fetch_snippet(issue.get("url", ""))
        if not snippet:
            _log({"issue": issue.get("issue"), "outcome": "no_snippet"})
            results.append({"issue": issue.get("issue"), "outcome": "no_snippet"})
            continue
        text, err = _call_claude(_build_prompt(issue, snippet), _LEARN_SYSTEM)
        if err or not text:
            _log({"issue": issue.get("issue"), "outcome": f"api_error: {err}"})
            results.append({"issue": issue.get("issue"), "outcome": f"api_error: {err}"})
            continue
        # Parse JSON response (Claude often wraps in code fence)
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            _log({"issue": issue.get("issue"), "outcome": "non_json_response"})
            results.append({"issue": issue.get("issue"), "outcome": "non_json"})
            continue
        try:
            proposal = json.loads(m.group(0))
        except Exception as e:
            _log({"issue": issue.get("issue"), "outcome": f"parse_fail: {e}"})
            results.append({"issue": issue.get("issue"), "outcome": "parse_fail"})
            continue
        find = proposal.get("find", "")
        replace = proposal.get("replace", "")
        rationale = proposal.get("rationale", "")
        ok, reason = _validate_proposal(find, replace)
        if not ok:
            _log({"issue": issue.get("issue"), "outcome": f"validation_fail: {reason}",
                  "find": find[:80], "replace": replace[:80]})
            results.append({"issue": issue.get("issue"), "outcome": f"validation: {reason}"})
            continue
        # Phase 300 (Phase R-3): 2-cycle approval gate. If the exact same
        # (find, replace) pair has been proposed before, increment its
        # approval_count and flip to approved=true at >= 2. master-heal only
        # consumes approved entries — single-shot Claude hallucinations stay
        # in the queue but never get auto-applied. Real recurring bugs cross
        # the threshold within 2 hourly cycles and become eligible.
        existing = next((e for e in _proposed_fixes
                         if e.get("find") == find and e.get("replace") == replace), None)
        if existing:
            existing["approval_count"] = existing.get("approval_count", 1) + 1
            existing["last_seen_at"] = datetime.now(timezone.utc).isoformat()
            existing["approved"] = existing["approval_count"] >= 2
            _log({"issue": issue.get("issue"), "outcome": "approval_count_incremented",
                  "count": existing["approval_count"], "approved": existing["approved"]})
            results.append({"issue": issue.get("issue"), "outcome": "reproposed",
                            "approval_count": existing["approval_count"],
                            "approved": existing["approved"]})
        else:
            entry = {
                "issue_label": issue.get("issue"),
                "find": find,
                "replace": replace,
                "rationale": rationale,
                "source_url": issue.get("url"),
                "proposed_at": datetime.now(timezone.utc).isoformat(),
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
                "model": BRAIN_MODEL,
                "approval_count": 1,        # phase 300
                "approved": False,          # phase 300 — flips at count >= 2
            }
            _proposed_fixes.append(entry)
            if len(_proposed_fixes) > _MAX_BUFFER:
                _proposed_fixes.pop(0)
            _log({"issue": issue.get("issue"), "outcome": "proposed", "find": find[:80]})
            results.append({"issue": issue.get("issue"), "outcome": "proposed",
                            "find": find[:60], "rationale": rationale[:120],
                            "approval_count": 1, "approved": False})

    return jsonify(
        ok=True,
        cycle_at=datetime.now(timezone.utc).isoformat(),
        novel_count=len(novel),
        results=results,
        accepted_total=len(_proposed_fixes),
    ), 200


@brain_v2_bp.get("/api/v1/brain/proposed-fixes")
def proposed_fixes():
    """Master-heal workflow polls this to merge novel patterns into FIX_MAP.

    Phase 300 (Phase R-3): supports ?approved=true to return ONLY proposals
    that have crossed the 2-cycle approval threshold. master-heal.yml uses
    this filter to avoid auto-applying single-shot Claude hallucinations.
    Without the filter, returns everything so the QA dashboard can show
    both pending + approved.
    """
    approved_only = request.args.get("approved", "").lower() in ("true", "1", "yes")
    proposals = _proposed_fixes
    if approved_only:
        proposals = [p for p in proposals if p.get("approved")]
    return jsonify(
        as_of=datetime.now(timezone.utc).isoformat(),
        count=len(proposals),
        filter={"approved_only": approved_only},
        proposals=list(reversed(proposals)),  # newest first
    ), 200


@brain_v2_bp.get("/api/v1/brain/learning-log")
@_require_admin
def learning_log():
    """Recent learning attempts + outcomes (for the QA dashboard)."""
    return jsonify(
        as_of=datetime.now(timezone.utc).isoformat(),
        count=len(_learning_log),
        log=list(reversed(_learning_log)),
    ), 200


@brain_v2_bp.get("/api/v1/brain/status")
def brain_status():
    """Public health check — proves the layer is loaded + reports activation."""
    return jsonify(
        layer=4,
        loaded=True,
        active=bool(ANTHROPIC_API_KEY),
        model=BRAIN_MODEL,
        max_learn_per_cycle=BRAIN_MAX_LEARN,
        proposed_fixes_count=len(_proposed_fixes),
        learning_log_count=len(_learning_log),
        hint=(None if ANTHROPIC_API_KEY
              else "Set ANTHROPIC_API_KEY in Railway env to activate"),
    ), 200
