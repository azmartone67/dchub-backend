#!/usr/bin/env python3
"""The deterministic gate between the squasher agent's edits and a PR.

The agent is told these limits in its prompt; this is what ENFORCES them,
because a prompt is a request and this is a check. It reads the working
tree's diff against HEAD (the agent never commits) and refuses the patch when
it touches a path an automated writer must not own, is too big to review,
changes only tests, carries something shaped like a credential, or does not
compile. Exit 0 = ok, 3 = refused, 2 = could not evaluate. The JSON verdict
goes to stdout either way; the workflow reports refusals back to the queue
row as needs_human WITH the reasons, and uploads the patch as an artifact —
a refused patch is evidence for a human, not something to throw away.

Pure core: evaluate(). Everything else is I/O.
"""
from __future__ import annotations

import json
import py_compile
import re
import subprocess
import sys

MAX_FILES = 8
MAX_LINES = 300

# Paths an automated writer never owns. Each has a reason a reviewer can
# read in the refusal.
DENY = (
    (r"(^|/)\.git(/|$)|(^|/)\.gitmodules$", "git internals"),
    (r"^\.github/", "CI/workflow changes are human-authored"),
    (r"^\.claude/", "agent configuration is human-authored"),
    (r"(^|/)worker\.js$", "the zone worker fronts the whole API and deploys "
                          "by paste — not an agent's edit"),
    (r"(^|/)requirements[^/]*\.txt$|(^|/)(Dockerfile|Procfile)$|"
     r"(^|/)(railway|nixpacks)\.(json|toml)$", "dependencies/build config"),
    (r"(^|/)migrations?/", "schema migrations"),
    (r"^contracts/", "generated contract surfaces — regenerate, don't hand-edit"),
    (r"^tools/squasher_agent/", "the agent may not edit its own guard"),
    (r"stripe|billing|checkout|payment|pricing|entitle|subscription|"
     r"license|refund|invoice", "money/entitlement paths"),
    (r"(^|/|_)auth|oauth|jwt|api_?key|secret|credential|password|token",
     "auth/credential paths"),
)
_DENY_RE = [(re.compile(p, re.I), why) for p, why in DENY]

SECRET_SHAPES = re.compile(
    r"sk-ant-[A-Za-z0-9_\-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[baprs]-[A-Za-z0-9\-]{10,}")


def is_test_path(p: str) -> bool:
    return p.startswith("tests/") or "/tests/" in p or \
        re.search(r"(^|/)test_[^/]*\.py$", p) is not None


def evaluate(numstat: list[tuple[int, int, str]], added_text: str,
             compile_errors: list[str] | None = None) -> dict:
    """numstat rows are (added, removed, path) — binary files count as 0/0
    and are refused separately. Returns {ok, reasons, files, lines}."""
    reasons = []
    files = [p for _, _, p in numstat]
    lines = sum(a + d for a, d, _ in numstat)
    if not files:
        reasons.append("empty diff — the agent reported a fix but changed nothing")
    for p in files:
        if p.startswith(".squasher/"):
            continue
        for rx, why in _DENY_RE:
            if rx.search(p):
                reasons.append(f"{p}: {why}")
                break
    real = [p for p in files if not p.startswith(".squasher/")]
    if len(real) > MAX_FILES:
        reasons.append(f"{len(real)} files changed (max {MAX_FILES})")
    if lines > MAX_LINES:
        reasons.append(f"{lines} lines changed (max {MAX_LINES})")
    if real and all(is_test_path(p) for p in real):
        reasons.append("only tests changed — a test alone does not fix a finding")
    if SECRET_SHAPES.search(added_text or ""):
        reasons.append("an added line is shaped like a credential")
    for e in compile_errors or []:
        reasons.append(e if e.startswith("patch carries") else f"does not compile: {e}")
    return {"ok": not reasons, "reasons": reasons, "files": real, "lines": lines}


_SYMLINK_OR_BINARY = re.compile(
    r"^(new file mode|new mode|old mode|deleted file mode) 120000$|^GIT binary patch$",
    re.M)


def evaluate_patch(patch_text: str, numstat_text: str) -> dict:
    """Static verdict on a patch FILE, before it is applied anywhere. This is
    what the publish job trusts: it runs from a pristine checkout of main, so
    nothing the agent wrote has executed on that runner."""
    rows = []
    for line in numstat_text.splitlines():
        a, d, p = line.split("\t", 2)
        rows.append((0 if a == "-" else int(a), 0 if d == "-" else int(d), p))
    added = "\n".join(l[1:] for l in patch_text.splitlines()
                      if l.startswith("+") and not l.startswith("+++"))
    extra = []
    if _SYMLINK_OR_BINARY.search(patch_text):
        extra.append("patch carries a symlink or a binary blob")
    return evaluate(rows, added, extra)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True).stdout


def _collect() -> tuple[list[tuple[int, int, str]], str, list[str]]:
    # Untracked files count: `git add -N` makes them visible to diff without
    # staging content, and the workflow stages everything it commits anyway.
    _git("add", "-N", "--", ".", ":(exclude).squasher")
    rows = []
    for line in _git("diff", "--numstat", "HEAD").splitlines():
        a, d, p = line.split("\t", 2)
        if a == "-" or d == "-":
            rows.append((0, 0, p))
            continue
        rows.append((int(a), int(d), p))
    added = "\n".join(l[1:] for l in _git("diff", "-U0", "HEAD").splitlines()
                      if l.startswith("+") and not l.startswith("+++"))
    errs = []
    for _, _, p in rows:
        if p.endswith(".py"):
            try:
                py_compile.compile(p, doraise=True)
            except (py_compile.PyCompileError, FileNotFoundError) as e:
                if isinstance(e, FileNotFoundError):
                    continue  # a deleted file
                errs.append(f"{p}: {str(e).splitlines()[-1][:200]}")
    binary = [p for a, d, p in rows if (a, d) == (0, 0) and p.endswith(
        (".png", ".jpg", ".gif", ".pdf", ".zip", ".bin", ".so"))]
    errs += [f"{p}: binary file" for p in binary]
    return rows, added, errs


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--patch":
        try:
            text = open(sys.argv[2], encoding="utf-8", errors="replace").read()
            num = _git("apply", "--numstat", sys.argv[2])
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "reasons": [
                f"patch unreadable or does not apply: {type(e).__name__}"]}))
            return 2
        v = evaluate_patch(text, num)
        print(json.dumps(v))
        return 0 if v["ok"] else 3
    try:
        rows, added, errs = _collect()
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "reasons": [f"guard could not read "
                          f"the diff: {type(e).__name__}: {e}"]}))
        return 2
    v = evaluate(rows, added, errs)
    print(json.dumps(v))
    return 0 if v["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
