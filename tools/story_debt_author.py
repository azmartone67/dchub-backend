#!/usr/bin/env python3
"""tools/story_debt_author.py — the writer the story pipeline never had.

Runs on a daily GH cron (.github/workflows/story-debt-author.yml). Computes
ship-to-story debt with the SAME detector the master shell reads
(utils/story_debt.py), and when debt exists it stages one skeleton card per
uncovered product on branch ``story-debt/drafts`` and opens a GitHub DRAFT
pull request. A human finishes the copy, flips status to "published", marks
the PR ready, and the merge is the approval — exactly the store's contract.

FAIL-CLOSED TWICE, because this repo auto-merges PRs on their first green
moment:
  1. the PR is a GitHub DRAFT — auto-merge cannot take drafts;
  2. every staged entry is status="draft", which the store's _is_published
     gate withholds even if the file lands on main somehow.
So the worst this script can do is stage prose nobody sees.

Deterministic by construction: the branch is recreated from origin/main every
run and skeletons are regenerated from current debt, so the diff always shows
exactly today's uncovered products — no drift, no stacking.

Exit codes: 0 = ran (debt or not), 1 = could not measure or could not stage.
The workflow's beat step reports this run to the deadman board as feed
"story-debt-author" (rows_inserted = skeletons staged this run).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from utils.story_debt import compute_debt, parse_nav_new_items, skeleton_entry  # noqa: E402

NAV_URL = os.environ.get("STORY_DEBT_NAV_URL", "https://dchub.cloud/js/dchub-nav.js")
STORE = os.path.join(ROOT, "data", "platform_updates.json")
BRANCH = "story-debt/drafts"
UA = "dchub-story-debt-author/1.0"


def _run(args, check=True, **kw):
    print("+ " + " ".join(args), flush=True)
    return subprocess.run(args, check=check, text=True, capture_output=True, **kw)


def _gh_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s=%s\n" % (name, value))


def _fetch_nav():
    import requests
    # cache-bust: CF Pages caches this asset; a stale nav here would stage
    # skeletons for badges that no longer exist (same lesson as the shell).
    r = requests.get(NAV_URL + ("&" if "?" in NAV_URL else "?") + "_=" + str(int(time.time())),
                     headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    # utf-8 by hand: requests latin-1s charsetless text/* (the SSE lesson).
    return r.content.decode("utf-8", "replace")


def _open_pr_number():
    """The open PR for our head branch, or None. `gh pr list` (not `view`):
    view resolves closed/merged PRs too, and a merged PR must read as absent."""
    out = _run(["gh", "pr", "list", "--head", BRANCH, "--state", "open",
                "--json", "number"], check=False)
    try:
        rows = json.loads(out.stdout or "[]")
        return rows[0]["number"] if rows else None
    except Exception:
        return None


def main() -> int:
    try:
        nav_js = _fetch_nav()
    except Exception as e:
        print("::error::nav fetch failed: %s" % e)
        return 1
    items = parse_nav_new_items(nav_js)
    if not items:
        # Empty parse = instrument failure. Staging "no debt" off it would be
        # the empty-parse=PASS trap; refuse to conclude anything.
        print("::error::nav parse produced 0 entries — refusing to measure")
        return 1

    with open(STORE, "r", encoding="utf-8") as fh:
        store = json.load(fh)
    debt = compute_debt(items, store.get("updates") or [])
    print("nav NEW items: %d · uncovered: %d" % (len(items), len(debt)), flush=True)

    if not debt:
        _gh_output("staged", "0")
        # ★ EARNED idle. We fetched the nav, parsed %d NEW entries (a zero
        # parse returned 1 above, so this is evidence and not silence), and
        # every one of them is already covered by a published card. That is
        # the one shape the dead-man board's `no_new_data` is FOR: ran to
        # completion, genuinely nothing to write. Reported as `success` with
        # rows=0 it instead bumps consecutive_zero and, at 3, convicts a
        # producer that is working exactly as designed — which is precisely
        # what happened, red daily from 2026-08-18.
        _gh_output("beat_status", "no_new_data")
        pr = _open_pr_number()
        if pr:
            _run(["gh", "pr", "close", str(pr), "--comment",
                  "Debt cleared — every NEW-badged nav item now has a published "
                  "card; closing the draft-staging PR."], check=False)
            print("closed stale draft PR #%s" % pr, flush=True)
        print("no story debt — nothing to stage", flush=True)
        return 0

    today = date.today().isoformat()
    skeletons = [skeleton_entry(d, today) for d in debt]

    _run(["git", "config", "user.name", "story-debt-author"])
    _run(["git", "config", "user.email", "story-debt-author@users.noreply.github.com"])
    _run(["git", "fetch", "origin", "main", "-q"])
    _run(["git", "checkout", "-B", BRANCH, "origin/main"])

    # Re-read the store from the fresh branch tip, then prepend skeletons.
    with open(STORE, "r", encoding="utf-8") as fh:
        store = json.load(fh)
    updates = store.get("updates") or []
    have = {str(e.get("id")) for e in updates if isinstance(e, dict)}
    fresh = [s for s in skeletons if s["id"] not in have]
    store["updates"] = fresh + updates
    with open(STORE, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(store, indent=2, ensure_ascii=False) + "\n")

    if not fresh:
        # Same debt already staged on a previous run and main hasn't moved —
        # keep the PR as-is, report zero NEW skeletons this run.
        _gh_output("staged", "0")
        # Also earned: debt EXISTS but this run had nothing new to add, so the
        # author idled correctly. The feed measures what the AUTHOR wrote, not
        # whether debt is outstanding — the shell's ship_vs_story lane owns
        # that question and is unaffected by this beat.
        _gh_output("beat_status", "no_new_data")
        print("debt unchanged — skeletons already staged", flush=True)
        return 0

    _run(["git", "add", os.path.relpath(STORE, ROOT)])
    labels = ", ".join(re.sub(r"^\[DRAFT\] ", "", s["title"]) for s in fresh)
    _run(["git", "commit", "-q", "-m",
          "story-debt: stage %d draft card(s) — %s\n\n"
          "Auto-staged by tools/story_debt_author.py. Every entry is "
          "status=draft (invisible to the loader); flip to published and "
          "polish the copy to approve." % (len(fresh), labels)])
    _run(["git", "push", "-q", "-f", "-u", "origin", BRANCH])

    if _open_pr_number() is None:
        body = (
            "Ship-to-story debt detected by the daily author "
            "(`tools/story_debt_author.py`): the nav badges these as NEW but "
            "no published card covers them.\n\n" +
            "\n".join("- `%s` — %s" % (d["path"], " / ".join(d["labels"])) for d in debt) +
            "\n\n**To approve:** replace each TODO body with real copy (no "
            "figures in prose — bind numbers via metric tokens), flip "
            "`status` to `published`, mark ready for review, merge. Drafts "
            "are invisible to the loader even if this merges as-is.\n\n"
            "🤖 Staged by the story-debt author lane")
        _run(["gh", "pr", "create", "--draft", "--head", BRANCH,
              "--title", "story-debt: draft cards awaiting copy + approval",
              "--body", body])
        print("opened draft PR", flush=True)
    else:
        print("draft PR already open — branch updated in place", flush=True)

    _gh_output("staged", str(len(fresh)))
    _gh_output("beat_status", "success")
    return 0


if __name__ == "__main__":
    sys.exit(main())
