#!/usr/bin/env python3
"""The /agent/result payload for one run — the publish job's last word.

Usage: report.py result.json QUEUE_ID RUN_URL   (reads the job's env)
Every path settles the row; the only question is how. A patch the guard or
the re-run tests refused is reported needs_human WITH the reason and a
pointer to the artifact — a refused patch is evidence, not garbage.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


def decide(r: dict, env: dict, guard_reasons: list[str] | None = None) -> dict:
    o = r.get("outcome")
    run = env.get("RUN", "")
    qid = env.get("QID", "")
    base = {"summary": str(r.get("summary") or ""),
            "root_cause": str(r.get("root_cause") or ""),
            "evidence": r.get("evidence") if isinstance(r.get("evidence"), list) else [],
            "tests": r.get("tests") if isinstance(r.get("tests"), list) else []}
    review = f"Review the agent's patch (artifact agent-out, {run})"
    if env.get("AGENT_RESULT") not in ("success", None, ""):
        return {**base, "outcome": "failed",
                "summary": f"agent job ended {env.get('AGENT_RESULT')}"}
    if o == "fixed":
        if env.get("PR_URL"):
            return {**base, "outcome": "pr_opened", "pr_url": env["PR_URL"]}
        if env.get("GUARD_OK") != "1":
            why = "; ".join(guard_reasons or []) or "guard did not pass"
            return {**base, "outcome": "needs_human",
                    "human_action": f"{review} — the guard refused it: {why}"[:800]}
        if env.get("TESTS_OK") != "1":
            return {**base, "outcome": "needs_human",
                    "human_action": (f"{review} — the tests it listed did not "
                                     f"pass on a clean main with the patch "
                                     f"applied, or it listed none")[:800]}
        return {**base, "outcome": "failed",
                "summary": f"patch passed guard+tests but the PR step ended "
                           f"{env.get('PR_STEP') or 'unknown'}"}
    if o == "needs_human":
        if not str(r.get("human_action") or "").strip():
            return {**base, "outcome": "failed",
                    "summary": "agent said needs_human but named no action"}
        return {**base, "outcome": "needs_human",
                "human_action": str(r["human_action"])[:800]}
    if o == "not_reproducible":
        return {**base, "outcome": "not_reproducible"}
    return {**base, "outcome": "failed",
            "summary": base["summary"] or f"no usable result from row {qid}'s agent run"}


def guard_reasons_for(r: dict, env: dict, src_dir: str, guard_fn=None
                      ) -> list[str] | None:
    """Why the guard refused, read from the RIGHT repo's patch. None unless
    the agent said fixed and the guard did not pass."""
    if r.get("outcome") != "fixed" or env.get("GUARD_OK") == "1":
        return None
    target = env.get("TARGET") or "backend"
    if target == "both":
        return ["the patch touches both dchub-backend and dchub-mcp-server "
                "— one repo per fix"]
    if target == "none":
        return ["empty diff — the agent reported a fix but changed nothing"]
    name = "mcp.patch" if target == "mcp" else "agent.patch"
    return (guard_fn or _guard_reasons)(os.path.join(src_dir, name), target)


def _guard_reasons(patch: str, repo: str = "backend") -> list[str]:
    try:
        p = subprocess.run([sys.executable, "-I", os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "guard.py"), "--patch", patch,
            "--repo", repo], capture_output=True, text=True, timeout=60)
        return json.loads(p.stdout or "{}").get("reasons") or []
    except Exception:  # noqa: BLE001
        return []


if __name__ == "__main__":
    src, qid, run = sys.argv[1:4]
    try:
        r = json.load(open(src, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        r = {}
    env = dict(os.environ, QID=qid, RUN=run)
    reasons = guard_reasons_for(r, env, os.path.dirname(src))
    out = decide(r, env, reasons)
    out.update(queue_id=int(qid), run_url=run)
    print(json.dumps(out))
