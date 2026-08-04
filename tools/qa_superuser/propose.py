#!/usr/bin/env python3
"""Turn an investigated finding into a REVIEWABLE DIFF — never into a merge.

This is the last link of the chain the QA super-user was built for:

    probe (symptom, outside-in)
      -> investigate (root cause, adversarially refuted)
        -> propose (a diff a human reads and merges)

It exists because "I open the issues but they just sit there" is a real
complaint about a real gap, and because the honest fix for that gap is NOT an
auto-fixer. Every argument for keeping the merge button human is in this
codebase already; the strongest one arrived while this tool was being built.
The edge-caching finding's own remedy said TWO opposite fixes existed and that
picking wrong would re-create the Neon stampede — an auto-fixer reading
"a no-store path is being cached, add a bypass rule" would have picked wrong.
So: propose, validate hard, and hand a human a diff.

WHAT MAKES A PROPOSAL LEGITIMATE HERE

Four gates, each one earned:

1. **The investigation must have survived its own refutation.** The brain's
   investigator was measured refuting ~70% of its own drafts. Generating code
   from a recommendation its author already knocked down is precisely the
   "plausible-but-wrong fix shipped" failure this platform keeps re-learning.

2. **The investigation must be CURRENT** — bound to the evidence on screen. A
   fix derived from last week's observation is a fix to a different problem.

3. **The edit must be exact and unique.** `find` present once, verified against
   the real file before a PR slot is spent. Ambiguity is refused, not resolved.

4. **The blast radius must be small.** A proposal that deletes far more than it
   inserts is the signature of a stale-copy revert, not a targeted fix — that
   pattern has cost this repo real work before, so it is refused by size.

Nothing here writes to main, merges, or deploys.
"""
from __future__ import annotations

# Paths a proposal may never touch, whatever the analysis concluded.
#
# ★ .github/ is the load-bearing one. Everything else in this file is guarded by
#   "a human reads the diff" — and CI config is exactly the thing that decides
#   how much a human is shown before merging. A change that weakens the checks
#   is the one change that must never arrive through an automated proposer.
FORBIDDEN_PREFIXES = (".github/",)

# A single targeted fix that removes this many more lines than it adds is
# almost always a stale-copy revert rather than a fix. See the 0728 CI wave.
MAX_NET_LINES_DELETED = 12

FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "fixable": {
            "type": "boolean",
            "description": "False if this finding cannot be fixed by a single "
                           "exact string replacement in one file. Be honest — "
                           "false is a correct and common answer.",
        },
        "why_not": {
            "type": "string",
            "description": "When fixable is false, the reason in one sentence.",
        },
        "file": {"type": "string",
                 "description": "Repo-relative path, e.g. routes/foo.py"},
        "find": {"type": "string",
                 "description": "EXACT substring to replace, copied verbatim "
                                "from the file, unique within it. Include "
                                "enough surrounding context to be unique."},
        "replace": {"type": "string", "description": "Replacement text."},
        "rationale": {"type": "string",
                      "description": "Why this edit fixes the finding, 1-3 "
                                     "sentences."},
    },
    "required": ["fixable"],
}


def build_fix_prompt(finding: dict, investigation: dict) -> str:
    """The prompt that asks for a diff. Written to make refusal easy.

    ★ "false is a correct and common answer" is load-bearing, not politeness. A
    model asked to produce a patch will produce a patch; most QA findings on this
    platform are NOT single-string fixes (they are config, data, or a design
    choice between two valid remedies), so the cheap failure mode is a confident
    edit to the wrong line. The prompt is built to make "I can't" the easy path.
    """
    return (
        "A QA probe observed the following defect from OUTSIDE the system, from "
        "a real caller's seat. An investigation has already established the "
        "likely cause. Your ONLY job is to decide whether the fix is a single, "
        "exact, unique string replacement in ONE file — and if so, to write it.\n\n"
        f"## Finding\n"
        f"- key: {finding.get('key')}\n"
        f"- title: {finding.get('title')}\n"
        f"- surface: {finding.get('surface')} | seat: {finding.get('seat')}\n"
        f"- evidence: {(finding.get('evidence') or '')[:1200]}\n"
        f"- what would make it red: {finding.get('red_when')}\n\n"
        f"## Investigation\n"
        f"{(investigation.get('recommendation') or '(none)')[:3000]}\n\n"
        "## Rules\n"
        "- `find` must be copied VERBATIM from the file and must be UNIQUE in "
        "it. If you are not certain of the exact current text, set fixable=false.\n"
        "- One file, one replacement. No multi-file changes, no refactors.\n"
        "- Never edit anything under .github/.\n"
        "- If the remedy is a config change, a data backfill, an infrastructure "
        "change, or a choice between two valid approaches, set fixable=false.\n"
        "- If the investigation is uncertain about the cause, set fixable=false.\n"
        "- Deleting a large block is NOT a fix. Keep the edit surgical.\n"
    )


def validate_fix(fix: dict, file_content: str | None) -> tuple[bool, str]:
    """Gate a proposed edit against the REAL file. Returns (ok, reason).

    Runs before a PR slot is spent, so a bad proposal costs a 422 rather than a
    unit of the daily change budget and a PR someone has to close.
    """
    if not isinstance(fix, dict):
        return False, "proposal was not an object"
    if not fix.get("fixable"):
        return False, (fix.get("why_not")
                       or "the model judged this not fixable by a single edit")

    # ★ Check the RAW path, THEN normalise. Stripping the leading slash first
    #   makes the absolute-path guard dead code: "/etc/passwd" becomes
    #   "etc/passwd" and sails through `startswith("/")`. Worse than the dead
    #   check is what it implies — an absolute path means the model was confused
    #   about the repo layout, and quietly reinterpreting it as repo-relative
    #   hides that. Refuse it instead of coercing it.
    raw = (fix.get("file") or "").strip()
    find = fix.get("find")
    replace = fix.get("replace", "")

    if not raw:
        return False, "no file named"
    if ".." in raw or raw.startswith("/"):
        return False, f"unsafe path: {raw}"
    path = raw
    if any(path.startswith(p) for p in FORBIDDEN_PREFIXES):
        return False, (f"{path} is off-limits to automated proposals — CI "
                       "config decides what a reviewer is shown")
    if not find:
        return False, "'find' was empty"
    if find == replace:
        return False, "no-op edit (find == replace)"

    if file_content is None:
        return False, f"could not read {path} from the repo"
    n = file_content.count(find)
    if n == 0:
        return False, (f"'find' does not appear in {path} — the proposal was "
                       "written against text that is not in the file")
    if n > 1:
        return False, f"'find' appears {n}x in {path} — ambiguous, refused"

    net_deleted = find.count("\n") - str(replace).count("\n")
    if net_deleted > MAX_NET_LINES_DELETED:
        return False, (f"removes {net_deleted} more lines than it adds — that "
                       "is the shape of a stale-copy revert, not a fix")

    return True, (f"single exact replacement in {path} "
                  f"({len(find)}->{len(str(replace))} chars)")


def gate_investigation(inv: dict | None) -> tuple[bool, str]:
    """Decide whether an investigation is fit to generate code from."""
    if not inv:
        return False, ("no investigation yet — run 'Ask the brain' first, so a "
                       "proposal is grounded in a cause rather than a symptom")
    if inv.get("state") == "stale":
        return False, ("the investigation explains OLDER evidence than the "
                       "finding now shows — re-investigate before proposing")
    if inv.get("survived") is False:
        return False, ("the brain's own refutation pass knocked this "
                       "recommendation down — it is a lead, not a basis for a "
                       "code change")
    if not (inv.get("recommendation") or "").strip():
        return False, "the investigation produced no recommendation"
    return True, "investigation is current and survived refutation"


def pr_title_for(finding: dict) -> str:
    """One title per finding key, so PRs dedupe against each other.

    ★ The shared lane titles every PR `[brain auto-fix] generic_find_replace`.
    Sent through unchanged, every QA proposal would be indistinguishable in the
    PR list and any dedup check keyed on title would block the second distinct
    fix as a duplicate of the first.
    """
    key = (finding.get("key") or "unknown").strip()
    title = (finding.get("title") or "").strip()
    return f"[qa-superuser] {key} — {title}"[:120]
