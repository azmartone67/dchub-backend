#!/usr/bin/env python3
"""Fail when a workflow step pushes straight to a protected `main`.

★ THE TRAP. `main` in this repo requires six status checks, and dchub-mcp-server's
is moving the same way. A `git push` to such a branch is rejected with GH006
("N of N required status checks are expected"), and a `[skip ci]` commit reports
none of them, so the push can NEVER succeed — it fails identically every run,
forever. The job around it usually still exits 0, so the failure is invisible.

★ THIS IS NOT HYPOTHETICAL, AND IT HAS COST REAL TIME. mcp-facts-export.yml
carries a comment recording SEVENTEEN DAYS OF DAILY RED from exactly this,
ignored the whole time because the remediation text shipped alongside it told
the reader the failure was cosmetic ("not served by any route anyway"). A wrong
remediation is worse than none: it converts a real alarm into a known-ignorable
one. Two more workflows carry their own postmortems of the same push —
brain-pr-post-merge-guard.yml ("`git push origin main` here was rejected by
branch protection (GH006...)") and weekly-shadow-audit.yml ("this used to
`git push origin main || true`").

★ WHY A GUARD AND NOT A COMMENT. Three workflows already carry a hand-written
warning about this, which is the problem: the knowledge lives in prose, is
rediscovered by whoever gets bitten next, and does nothing for the workflow
written tomorrow. The rule is "PR, never push" — so it should be a check.

WHAT COUNTS AS A VIOLATION, per `run:` block:
  * `git push` with NO refspec, in a block that never created a branch. The
    checkout is on main, so a bare push targets main.
  * any push naming `main` as the destination — `origin main`, `HEAD:main`,
    `:main`.

WHAT IS FINE:
  * pushing a branch the block just created (`git switch -c` / `git checkout -b`),
    including via `HEAD` — refresh-meta-replays.yml and
    refresh-architecture-map.yml both do this correctly and must stay green.
  * pushing an explicit branch variable (`git push origin "$BRANCH"`).

Usage:
    check_no_push_to_protected_main.py [workflow_dir]
    check_no_push_to_protected_main.py --self-test
"""
import re
import sys
from pathlib import Path

PUSH_RE = re.compile(r'\bgit\s+push\b([^\n|;&]*)')
BRANCH_CREATED_RE = re.compile(r'\bgit\s+(?:switch\s+-c|checkout\s+-b)\b')
# Flags that take no value; anything else starting with '-' is dropped too.
FLAG_RE = re.compile(r'^-')


def _push_targets(argstr):
    """Positional args of a `git push`, flags removed."""
    return [a for a in argstr.split() if not FLAG_RE.match(a)]


def violations_in_block(block, where):
    """Return a list of human-readable violations for one `run:` script."""
    out = []
    made_branch = bool(BRANCH_CREATED_RE.search(block))
    for m in PUSH_RE.finditer(block):
        args = _push_targets(m.group(1))
        line = block[: m.start()].count('\n') + 1
        # `git push` / `git push origin` with no refspec.
        if len(args) < 2:
            if not made_branch:
                out.append(
                    f'{where} (~line {line}): bare `git push` in a block that never '
                    f'created a branch — the checkout is on main, so this targets a '
                    f'protected main and can only ever fail with GH006. Open a PR instead.'
                )
            continue
        dest = args[1].strip('"\'')
        if dest == 'main' or dest.endswith(':main'):
            out.append(
                f'{where} (~line {line}): pushes to `main` ({dest}) — main requires '
                f'status checks, so this is rejected with GH006 every run. Open a PR instead.'
            )
    return out


def _run_blocks(path):
    """Yield (label, script) for each `run:` block. Text-scanned on purpose:
    the YAML here embeds ${{ }} templating that a strict loader rejects, and a
    guard that cannot read the file it guards is worse than no guard."""
    text = path.read_text(encoding='utf-8')
    blocks, cur, indent = [], [], None
    for raw in text.split('\n'):
        if indent is not None:
            stripped = raw.strip()
            if raw.strip() == '' or (len(raw) - len(raw.lstrip())) > indent:
                cur.append(raw)
                continue
            blocks.append('\n'.join(cur))
            cur, indent = [], None
        m = re.match(r'^(\s*)-?\s*run:\s*[|>]', raw)
        if m:
            indent = len(m.group(1))
            cur = []
    if cur:
        blocks.append('\n'.join(cur))
    return blocks


def scan(workflow_dir):
    found = []
    for p in sorted(Path(workflow_dir).glob('*.yml')) + sorted(Path(workflow_dir).glob('*.yaml')):
        for block in _run_blocks(p):
            found.extend(violations_in_block(block, p.name))
    return found


def self_test():
    """Must-fail controls. A guard nobody has seen fail is not a guard."""
    cases = [
        # (block, expect_violation, label)
        ('          git commit -m x\n          git push\n', True, 'bare push, no branch'),
        ('          git push origin main\n', True, 'explicit origin main'),
        ('          git push -u origin HEAD:main\n', True, 'HEAD:main refspec'),
        ('          git switch -c "chore/x"\n          git push -u origin HEAD\n',
         False, 'HEAD after switch -c is fine'),
        ('          git checkout -b "$BRANCH"\n          git push -q -u origin "$BRANCH"\n',
         False, 'explicit branch var is fine'),
        ('          git push --force-with-lease -u origin "$BR"\n', False, 'flags are not refspecs'),
    ]
    bad = 0
    for block, expect, label in cases:
        got = bool(violations_in_block(block, 'selftest.yml'))
        if got != expect:
            print(f'SELF-TEST FAIL: {label} — expected violation={expect}, got {got}')
            bad += 1
    if bad:
        print(f'{bad} self-test case(s) failed — the guard does not detect what it claims.')
        return 1
    print(f'self-test OK — {len(cases)} cases, both directions')
    return 0


def main(argv):
    if '--self-test' in argv:
        return self_test()
    root = argv[1] if len(argv) > 1 else str(Path(__file__).resolve().parent.parent / '.github/workflows')
    hits = scan(root)
    if hits:
        print('Workflow steps push directly to a protected main:\n')
        for h in hits:
            print('  ' + h)
        print('\nThe rule in this repo is PR, never push. See mcp-facts-export.yml.')
        return 1
    print('OK — no workflow pushes directly to a protected main')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv))
    except Exception as e:  # noqa: BLE001
        print(f'check_no_push_to_protected_main: {e}')
        sys.exit(2)
