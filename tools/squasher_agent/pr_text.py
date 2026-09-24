#!/usr/bin/env python3
"""Commit message, PR title and PR body for an agent fix.

Usage: pr_text.py result.json QUEUE_ID RUN_URL OUT_DIR
Writes OUT_DIR/{title.txt,msg.txt,body.md}. The agent's prose is rendered as
quoted text inside a PR body — never executed, never interpolated into shell.
"""
from __future__ import annotations

import json
import os
import sys


def _one_line(v, n: int) -> str:
    return " ".join(str(v or "").split())[:n]


def texts(r: dict, qid: str, run: str) -> tuple[str, str, str]:
    subject = _one_line(r.get("summary"), 70) or f"queue row {qid}"
    title = f"squasher-agent: {subject}"
    ev = "\n".join(f"- {_one_line(e, 400)}" for e in (r.get("evidence") or [])[:12])
    ts = "\n".join(f"- `{_one_line(t, 200)}`" for t in (r.get("tests") or [])[:12])
    msg = (f"{title}\n\n{_one_line(r.get('summary'), 600)}\n\n"
           f"Root cause: {_one_line(r.get('root_cause'), 600)}\n\n"
           f"squasher queue row {qid} · {run}\n\n"
           "Co-Authored-By: Claude <noreply@anthropic.com>\n")
    body = (
        f"Opened by the squasher agent lane (`routes/squasher_agent_lane.py`) "
        f"for queue row **{qid}**.\n\n"
        f"**Summary.** {_one_line(r.get('summary'), 1200)}\n\n"
        f"**Root cause.** {_one_line(r.get('root_cause'), 1500)}\n\n"
        f"**Evidence the agent gathered**\n{ev or '- (none listed)'}\n\n"
        f"**Tests.** Re-run by the `verify` job on a clean checkout of main "
        f"with this patch applied, and passed:\n{ts}\n\n"
        f"`tools/squasher_agent/guard.py` passed on a clean checkout: no denied "
        f"paths, within size, compiles, nothing shaped like a credential.\n\n"
        f"This row counts as **fixed** only if the detector stops reporting the "
        f"finding after this merges (reconciled on the next agent run).\n\n"
        f"**Not right? Close this PR without merging.** That is recorded as a "
        f"human rejection in `brain_review_decisions` — the one signal the "
        f"brain's self-assessment reads to learn it was wrong — and the agent "
        f"will not retry this queue row.\n\n"
        f"Run: {run}\n\n"
        f"🤖 Generated with [Claude Code](https://claude.com/claude-code)\n")
    return title, msg, body


if __name__ == "__main__":
    src, qid, run, out = sys.argv[1:5]
    try:
        r = json.load(open(src, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        r = {}
    t, m, b = texts(r, qid, run)
    for name, v in (("title.txt", t), ("msg.txt", m), ("body.md", b)):
        with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
            fh.write(v)
