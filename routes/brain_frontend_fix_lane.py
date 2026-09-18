"""brain_frontend_fix_lane — the squasher's dchub-frontend rows become PRs.

WHY THIS EXISTS
The bug squasher has been a healthy SENSOR and a dead END: 52 open findings,
re-detected nightly, **100% of them in dchub-frontend**, and the code-fix
proposer patches dchub-backend only. brain_bug_squash already publishes the
routing fact (`needs_repo`) and deliberately refuses to feed those rows to the
backend proposer, because doing so "manufactures PRs that cannot apply"
(#4231). That refusal was right. What was missing is the other half: a lane
that can actually open a PR against the OTHER repo.

★ WHY THIS DOES NOT JUST FIX ALL 52
Two of the three things standing between a finding and a patch are not
plumbing, and a lane that ignored them would ship bugs at machine speed.

1. **`hardcoded_hero_stat` (44 of 52) IS NOT AUTO-FIXABLE, by its own
   author's declaration.** Its pattern is `severity="polish"` with the comment
   "not blocking — may be intentional, just want visibility", and its
   suggested fix is a CHOICE between two options ("leave the literal as a
   placeholder '—'" OR "confirm the hydrator test"). Picking option (a)
   mechanically would replace live numbers with em-dashes across 23 files
   including index.html and landing/index.html — a visible marketing-surface
   regression, shipped by a bot, to fix a finding whose own severity says it
   may be intentional. These stay reported and addressable. They are not
   pending work; they are a visibility register.

2. ★★★ **The `js_field_fallback_missing` regex cannot tell a VALUE read from a
   PREDICATE read, and its suggested fix is only correct for the first.**
   Measured against live dchub-frontend main, all 8 open rows:

       :1149  const operatorInitials = (item.operator || 'UN')...    VALUE  ✓
       :1169  <span>${item.operator || 'Unknown'}</span>             VALUE  ✓
       :1353  item.operator || 'Unknown',                            VALUE  ✓
       js/dchub-infrastructure.js:521  OPERATOR: item.operator || item.name,  VALUE ✓
       :1003  if (item.operator && item.operator !== 'Unknown')      PREDICATE ✗
       :1069  if (operator && item.operator !== operator) return false  PREDICATE ✗
       :1332  if (operator && item.operator !== operator) return false  PREDICATE ✗
       js/activity-feed.js:223  if (item.operator || item.location) { PREDICATE ✗

   Applying `X.operator -> X.operator || X.company` to the four PREDICATE rows
   changes what a FILTER matches and what a GUARD admits — three of them are
   inside `if (...)` with a `!==` comparison that decides `return false`. That
   is a behaviour change dressed as a null-safety fix. So this lane applies the
   transform ONLY where the read is already a value-with-fallback, and reports
   the rest with the reason. Half of an "important"-severity queue being
   unsafe to auto-apply is the finding, not an inconvenience.

THE TRANSFORM, and why it is the safe one
Only `X.operator || <fallback>` -> `X.operator || X.company || <fallback>`.
It inserts a link in an EXISTING fallback chain: the final fallback still wins
when both are absent, so the expression's type, truthiness and end state are
unchanged. Every read it touches already declared "this may be missing".

SAFETY RAILS (each one is a refusal, never a guess)
  · admin-gated, and DRY RUN BY DEFAULT — `dry_run=0` is required to write.
  · preflight proves write access to the target repo before anything else, so
    a read-only token reports a REASON instead of a pile of 403s. (Every
    frontend checkout in .github/workflows uses DCHUB_FRONTEND_RO_TOKEN — a
    read-only credential — so this lane must never assume it can push.)
  · the recorded snippet must still match the line in main, or the row is
    `stale_finding` and is skipped. A finding is a claim about a file at a
    point in time.
  · the occurrence must appear EXACTLY ONCE on its line, or `ambiguous_line`.
  · a line already naming `.company` is `already_fixed` (idempotent re-runs).
  · branch names carry a content hash suffix: a deterministic name plus a
    janitor that closes PRs without deleting branches is a permanent 422
    "Reference already exists".
"""

import os
import re
import base64
import hashlib
import logging
from datetime import datetime, timezone

import psycopg2
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)
brain_frontend_fix_lane_bp = Blueprint("brain_frontend_fix_lane", __name__)

_TARGET_REPO = os.environ.get("FRONTEND_GITHUB_REPO",
                              "azmartone67/dchub-frontend").strip()
_REPO_LABEL = "dchub-frontend"
_AUTOFIXABLE_ISSUE = "bug_squash:js_field_fallback_missing"
_MAX_FILES_PER_PR = 10

# The pair the detector actually ships (scripts/bug_squash.py::_PAIRS).
_PRIMARY, _FALLBACK = "operator", "company"

# A read we may rewrite: `<var>.operator` immediately followed by `||`.
_VALUE_READ_RE = re.compile(
    rf"\b(item|row|r|d|x)\.{_PRIMARY}\b(?=\s*\|\|)")

# Anything that makes the read part of a decision rather than a value.
# ★ The comparison alternative is SPACED (`\s[<>]=?\s`) on purpose. A bare
# `[<>]=?` matched the `<` in `</span>` and refused
# `<span class="operator-name">${item.operator || 'Unknown'}</span>` — a plain
# value read — as a predicate. Half these findings live in .html, so an
# unanchored angle bracket classifies markup as arithmetic. Real JS relational
# comparisons are spaced; HTML tags are not.
_PREDICATE_RE = re.compile(
    r"(^|[^\w])(if|while|return)\s*\(|[!=]==|!=|\s[<>]=?\s|\?\s|&&"
    r"|return\s+false")

_SNIPPET_RE = re.compile(r"^Snippet:\s*(.+)$", re.M)


def _dsn():
    return os.environ.get("DATABASE_URL")


def _admin_ok() -> bool:
    import hmac
    want = (os.environ.get("BRAIN_ADMIN_KEY")
            or os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    got = (request.headers.get("X-Admin-Key") or "").strip()
    return bool(want) and hmac.compare_digest(want, got)


def _gh(method, path, body=None):
    """Reuses the PR opener's client. It takes a FULL path, so the repo is a
    caller-side f-string, not baked into the client — which is why this lane
    can sit beside the backend opener instead of refactoring it."""
    from routes.brain_pr_opener import _gh as gh
    return gh(method, path, body)


# ── preflight ────────────────────────────────────────────────────────
def _write_capability() -> dict:
    """Can the live token PUSH to the target repo? Asked ONCE, up front.

    A lane that discovers this at commit time reports a pile of 403s and looks
    like a code bug. The repo's own workflows authenticate to dchub-frontend
    with DCHUB_FRONTEND_RO_TOKEN, so a read-only answer here is the EXPECTED
    answer, not an error — and the remediation is a credential, not a patch."""
    r = _gh("GET", f"/repos/{_TARGET_REPO}")
    if r.status_code == 404:
        return {"can_write": False, "reason": "repo_not_visible_to_token",
                "detail": f"GET /repos/{_TARGET_REPO} -> 404. A fine-grained "
                          "token scoped to dchub-backend cannot see it.",
                "status": 404}
    if r.status_code != 200:
        return {"can_write": False, "reason": f"github_{r.status_code}",
                "detail": (r.text or "")[:200], "status": r.status_code}
    perms = ((r.json() or {}).get("permissions") or {})
    can = bool(perms.get("push"))
    return {
        "can_write": can,
        "reason": None if can else "token_is_read_only",
        "detail": None if can else (
            "The token can READ dchub-frontend but not push. Every frontend "
            "checkout in .github/workflows uses DCHUB_FRONTEND_RO_TOKEN. "
            "Provision a token with contents:write on dchub-frontend and set "
            "it as FRONTEND_PR_TOKEN, or open these as PRs by hand."),
        "permissions": perms,
        "status": 200,
    }


def _open_findings():
    """Open squasher rows routed to the frontend, with their detail."""
    dsn = _dsn()
    if not dsn:
        return None, "DATABASE_URL not set"
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT issue, url, detail, seen_count
                      FROM brain_findings
                     WHERE detector = 'brain_bug_squash'
                       AND status = 'open'
                     ORDER BY first_seen ASC
                """)
                rows = cur.fetchall()
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:200]}"
    out = []
    for issue, url, detail, seen in rows:
        u = url or ""
        if not (u.startswith(_REPO_LABEL + "/") or ("/" + _REPO_LABEL + "/") in u):
            continue
        path, _, line = u.partition(_REPO_LABEL + "/")[2].rpartition(":")
        if not path or not line.isdigit():
            continue
        out.append({"issue": issue, "path": path, "line": int(line),
                    "detail": detail or "", "seen_count": seen, "url": u})
    return out, None


def _classify(f, file_lines):
    """One finding -> (new_line | None, disposition, reason).

    Every branch that does not patch says WHY, in the row. A skipped row with
    no reason is indistinguishable from a row nobody looked at."""
    if f["issue"] != _AUTOFIXABLE_ISSUE:
        return None, "not_auto_fixable", (
            "hardcoded_hero_stat is severity 'polish' and its own pattern says "
            "it may be intentional; its suggested fix is a choice between two "
            "options, not a patch. Reported, never auto-applied.")

    n = f["line"]
    if not (0 < n <= len(file_lines)):
        return None, "stale_finding", (
            f"line {n} is past EOF ({len(file_lines)} lines) — the file moved "
            "under the finding")
    line = file_lines[n - 1]

    want = _SNIPPET_RE.search(f["detail"] or "")
    if want and want.group(1).strip()[:160] != line.strip()[:160]:
        return None, "stale_finding", (
            "the recorded snippet no longer matches this line; re-scan before "
            "patching")

    if f".{_FALLBACK}" in line:
        return None, "already_fixed", f"line already names .{_FALLBACK}"

    if _PREDICATE_RE.search(line):
        return None, "predicate_read", (
            "the read is part of a condition/comparison, so adding "
            f"`|| .{_FALLBACK}` would change what this filter matches or what "
            "this guard admits — a behaviour change, not a null-safety fix. "
            "The detector's regex cannot tell a value read from a predicate "
            "read; this is a detector-precision gap, not a plumbing gap.")

    hits = _VALUE_READ_RE.findall(line)
    if len(hits) != 1:
        return None, ("no_value_read" if not hits else "ambiguous_line"), (
            f"expected exactly one `<var>.{_PRIMARY} ||` on the line, found "
            f"{len(hits)}")

    var = hits[0]
    old = f"{var}.{_PRIMARY}"
    new = f"{var}.{_PRIMARY} || {var}.{_FALLBACK}"
    if line.count(old) != 1:
        return None, "ambiguous_line", (
            f"`{old}` appears {line.count(old)}x on the line")
    return line.replace(old, new, 1), "apply", None


def _plan():
    findings, err = _open_findings()
    if err:
        return None, err
    by_path = {}
    for f in findings:
        by_path.setdefault(f["path"], []).append(f)

    plan, skipped = [], []
    for path, group in sorted(by_path.items()):
        # Only fetch a file that has at least one candidate row.
        if not any(g["issue"] == _AUTOFIXABLE_ISSUE for g in group):
            for g in group:
                _, disp, why = _classify(g, [])
                skipped.append({**{k: g[k] for k in ("issue", "url")},
                                "disposition": disp, "reason": why})
            continue
        r = _gh("GET", f"/repos/{_TARGET_REPO}/contents/{path}?ref=main")
        if r.status_code != 200:
            for g in group:
                skipped.append({"issue": g["issue"], "url": g["url"],
                                "disposition": "unreadable",
                                "reason": f"GET contents -> {r.status_code}"})
            continue
        j = r.json() or {}
        try:
            text = base64.b64decode((j.get("content") or "").replace("\n", "")
                                    ).decode("utf-8")
        except Exception:
            for g in group:
                skipped.append({"issue": g["issue"], "url": g["url"],
                                "disposition": "unreadable",
                                "reason": "content not utf-8 decodable"})
            continue
        lines = text.split("\n")
        edits = []
        for g in sorted(group, key=lambda x: x["line"]):
            new_line, disp, why = _classify(g, lines)
            if disp == "apply":
                edits.append({"line": g["line"], "before": lines[g["line"] - 1],
                              "after": new_line, "url": g["url"]})
            else:
                skipped.append({"issue": g["issue"], "url": g["url"],
                                "disposition": disp, "reason": why})
        if edits:
            for e in edits:
                lines[e["line"] - 1] = e["after"]
            plan.append({"path": path, "sha": j.get("sha"),
                         "content": "\n".join(lines), "edits": edits})
    return {"plan": plan, "skipped": skipped}, None


# ── routes ───────────────────────────────────────────────────────────
@brain_frontend_fix_lane_bp.route(
    "/api/v1/admin/brain/frontend-fix-lane/preflight", methods=["GET"])
def preflight():
    """What this lane CAN do right now, before it is asked to do it."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    cap = _write_capability()
    built, err = _plan()
    if err:
        return jsonify(ok=False, error=err, write_capability=cap), 200
    counts = {}
    for s in built["skipped"]:
        counts[s["disposition"]] = counts.get(s["disposition"], 0) + 1
    return jsonify(
        ok=True,
        target_repo=_TARGET_REPO,
        write_capability=cap,
        would_apply=sum(len(p["edits"]) for p in built["plan"]),
        would_touch_files=[p["path"] for p in built["plan"]],
        skipped_by_disposition=counts,
        skipped=built["skipped"][:60],
        edits=[e for p in built["plan"] for e in p["edits"]][:60],
        note=("`predicate_read` rows are NOT plumbing failures — the detector's "
              "suggested fix is invalid there. Fix the detector, do not widen "
              "this lane."),
    ), 200


@brain_frontend_fix_lane_bp.route(
    "/api/v1/admin/brain/frontend-fix-lane/run", methods=["POST"])
def run():
    """Open ONE PR against the frontend repo. DRY RUN unless dry_run=0."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dry = request.args.get("dry_run", "1") not in ("0", "false", "no")

    cap = _write_capability()
    built, err = _plan()
    if err:
        return jsonify(ok=False, error=err, write_capability=cap), 200
    plan = built["plan"]
    if not plan:
        return jsonify(ok=True, acted=False, reason="nothing_to_apply",
                       write_capability=cap, skipped=built["skipped"][:60]), 200
    if len(plan) > _MAX_FILES_PER_PR:
        return jsonify(ok=False, error="blast_radius",
                       reason=f"{len(plan)} files > cap {_MAX_FILES_PER_PR}"), 200
    if dry:
        return jsonify(ok=True, acted=False, dry_run=True,
                       write_capability=cap,
                       would_open_pr_for=[{"path": p["path"],
                                           "edits": p["edits"]} for p in plan],
                       skipped=built["skipped"][:60]), 200
    if not cap["can_write"]:
        return jsonify(ok=False, acted=False, error="no_write_access",
                       write_capability=cap), 200

    fp = hashlib.sha1(
        "|".join(sorted(e["url"] for p in plan for e in p["edits"])
                 ).encode()).hexdigest()[:8]
    branch = f"brain/squash-fe-fallback-{fp}"
    r = _gh("GET", f"/repos/{_TARGET_REPO}/git/refs/heads/main")
    if r.status_code != 200:
        return jsonify(ok=False, error=f"base_sha_{r.status_code}"), 200
    base_sha = ((r.json() or {}).get("object") or {}).get("sha")

    cr = _gh("POST", f"/repos/{_TARGET_REPO}/git/refs",
             {"ref": f"refs/heads/{branch}", "sha": base_sha})
    if cr.status_code not in (200, 201):
        msg = ((cr.json() or {}).get("message") or "")[:120]
        if not (cr.status_code == 422 and "already exists" in msg.lower()):
            return jsonify(ok=False, error="create_branch",
                           status=cr.status_code, message=msg), 200

    committed = []
    for p in plan:
        body = {"message": f"squash: fall back to .{_FALLBACK} in {p['path']}",
                "content": base64.b64encode(
                    p["content"].encode("utf-8")).decode("ascii"),
                "branch": branch, "sha": p["sha"]}
        cm = _gh("PUT", f"/repos/{_TARGET_REPO}/contents/{p['path']}", body)
        if cm.status_code not in (200, 201):
            return jsonify(ok=False, error="commit_failed", path=p["path"],
                           status=cm.status_code,
                           committed_so_far=committed), 200
        committed.append(p["path"])

    n = sum(len(p["edits"]) for p in plan)
    lines = "\n".join(
        f"- `{e['url']}`\n  - `{e['before'].strip()[:110]}`\n"
        f"  - `{e['after'].strip()[:110]}`"
        for p in plan for e in p["edits"])
    skipped_md = "\n".join(
        f"- `{s['url']}` — **{s['disposition']}** — {s['reason']}"
        for s in built["skipped"][:40])
    pr = _gh("POST", f"/repos/{_TARGET_REPO}/pulls", {
        "title": f"[brain-squash] fall back to .{_FALLBACK} in {n} value read(s)",
        "head": branch, "base": "main",
        "body": (
            f"Opened by the brain's frontend fix lane at "
            f"{datetime.now(timezone.utc).isoformat()}.\n\n"
            f"Each edit inserts one link into an EXISTING fallback chain:\n"
            f"`X.{_PRIMARY} || <fallback>` -> "
            f"`X.{_PRIMARY} || X.{_FALLBACK} || <fallback>`. The final fallback "
            f"still wins when both are absent, so type, truthiness and end "
            f"state are unchanged.\n\n## Applied ({n})\n{lines}\n\n"
            f"## Not applied\n{skipped_md}\n\n"
            f"`predicate_read` rows are deliberately excluded: there the read "
            f"decides a filter or guard, so the detector's suggested fix would "
            f"change behaviour. That is a detector-precision gap.\n"),
    })
    if pr.status_code not in (200, 201):
        return jsonify(ok=False, error="open_pr", status=pr.status_code,
                       message=(pr.text or "")[:200], branch=branch), 200
    j = pr.json() or {}
    return jsonify(ok=True, acted=True, pr_url=j.get("html_url"),
                   pr_number=j.get("number"), branch=branch,
                   applied=n, files=committed,
                   skipped=built["skipped"][:60]), 200
