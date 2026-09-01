"""tests/test_revoke_tool_has_one_implementation.py — the revoke fix landed in one copy of two (2026-08-31).

Companion to test_revoke_actually_revokes.py, which pins what `revoke` must DO.
This file pins WHERE that behaviour has to exist: every path the repo can invoke
as `gen_dev_key.py` must be the same implementation.

★★★ THE DEFECT. `dchub-mcp-v2.1/gen_dev_key.py` was a vendored copy of the root
`gen_dev_key.py`, and it never received the 2026-08-16 revoke fix (#2766). Its
cmd_revoke was still:

    UPDATE mcp_dev_keys SET status='revoked' WHERE api_key=%s AND status='active'
    print(json.dumps({"revoked": bool(n), ...}))

It never touched `api_keys`, so revoking a dashboard/partner/paid key through the
bundle copy printed `"revoked": true` while the credential stayed FULLY LIVE —
util/tier_gate.resolve_tier step 1b grants access from
`api_keys WHERE key_hash IN (sha256(key), rawkey) AND (is_active IS NULL OR
is_active = 1)`. That copy was reachable: run_v21.sh:128 runs
`python3 "${BUNDLE}/gen_dev_key.py"`.

★ WHY IT WENT UNNOTICED FOR SIX WEEKS. test_revoke_actually_revokes.py pins
`_SRC = _ROOT / "gen_dev_key.py"` — the root file only. A second implementation
was invisible to it by construction, and run_v21.sh is not referenced from
.github/workflows/, so nothing else looked either. A guard that covers one of
two copies of a credential tool reports on the copy that was already correct.

★ THE FIX IS STRUCTURAL, NOT A RE-SYNC. The bundle path is now a symlink to the
root file, so there is one implementation and drift is not expressible. These
tests exist because a symlink can be silently materialised back into a regular
file — a zip round-trip, `rsync` without -l, `cp -L`, a checkout without symlink
support — and that is exactly the drift returning.

Composition note: correctness of the ONE implementation is asserted by
test_revoke_actually_revokes.py. This file asserts there is only one. Together
they cover every reachable path, and they stay true when the root file's revoke
semantics change again (they are being changed on fix/revoke-origin-aware) —
nothing here restates what revoke should do.

Ways it can regress, each asserted below:
  (1) The symlink is replaced by a real file that then drifts — the original bug.
  (2) The symlink is committed as a regular file (checkout without symlink
      support, or a tool that dereferenced it), so the drift is only a matter of
      time even while the bytes still match today.
  (3) A NEW third copy is vendored somewhere else in the repo.
  (4) run_v21.sh is repointed at a path that is not the root implementation.

Run:  python3 -m pytest tests/test_revoke_tool_has_one_implementation.py -v
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CANON = _ROOT / "gen_dev_key.py"
_BUNDLE_COPY = _ROOT / "dchub-mcp-v2.1" / "gen_dev_key.py"

# Directories that legitimately contain unrelated checkouts / vendored trees.
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".worktrees"}


def _discovered_copies() -> list[pathlib.Path]:
    """Every file named gen_dev_key.py in the repo, excluding the canonical one."""
    out = []
    for p in _ROOT.rglob("gen_dev_key.py"):
        if _SKIP_DIRS & set(p.relative_to(_ROOT).parts):
            continue
        if p == _CANON:
            continue
        out.append(p)
    return out


def test_canonical_tool_exists():
    """★ A silently-empty discovery would pass every assertion below."""
    assert _CANON.is_file(), f"canonical revoke tool missing: {_CANON}"
    assert "cmd_revoke" in _CANON.read_text(), f"{_CANON} is not the revoke tool"


# ── (1) + (3) no second implementation, here or anywhere ───────────────────

def test_no_second_revoke_implementation():
    """THE PIN: a second copy of a credential-revocation tool IS the failure mode.

    A copy is tolerated only if it cannot drift — i.e. it resolves to the
    canonical file. Byte-identity is deliberately NOT accepted: identical-today
    is how the bundle copy looked on 2026-07-30, six weeks before anyone noticed
    it had stopped matching.
    """
    canon = _CANON.resolve()
    drifted = [p for p in _discovered_copies() if p.resolve() != canon]
    assert not drifted, (
        "independent copies of the revoke tool found — these can drift out of "
        "sync with the fix in gen_dev_key.py and silently report a successful "
        "revoke while the credential stays live:\n  "
        + "\n  ".join(str(p.relative_to(_ROOT)) for p in drifted)
        + "\nMake each one a symlink to the root gen_dev_key.py, or delete it."
    )


def test_bundle_path_still_resolves_to_the_canonical_tool():
    """run_v21.sh:128 invokes this exact path; DEPLOY.md documents it."""
    assert _BUNDLE_COPY.exists(), (
        f"{_BUNDLE_COPY.relative_to(_ROOT)} is gone — run_v21.sh:43 preflights it "
        "and run_v21.sh:128 invokes it. Removing it needs both call sites updated."
    )
    assert _BUNDLE_COPY.resolve() == _CANON.resolve(), (
        f"{_BUNDLE_COPY.relative_to(_ROOT)} no longer resolves to the root "
        f"gen_dev_key.py (got {_BUNDLE_COPY.resolve()})"
    )


# ── (2) committed as a link, not merely identical in the working tree ──────

def test_bundle_path_is_committed_as_a_symlink():
    """Mode 120000 in the index, not 100644.

    The working-tree check above passes for a dereferenced copy that happens to
    match today. This is the one that catches the copy BEFORE it drifts.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-s", "dchub-mcp-v2.1/gen_dev_key.py"],
            cwd=_ROOT, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        pytest.skip(f"git unavailable: {exc}")
    if out.returncode != 0 or not out.stdout.strip():  # pragma: no cover
        pytest.skip("path not tracked by git here")
    mode = out.stdout.split()[0]
    assert mode == "120000", (
        f"dchub-mcp-v2.1/gen_dev_key.py is committed with mode {mode}, not 120000 "
        "(symlink). A regular file here is a second implementation of the revoke "
        "tool and will drift — that is exactly what happened between 2026-07-30 "
        "and 2026-08-31."
    )


# ── (4) what run_v21.sh actually invokes ───────────────────────────────────

@pytest.mark.parametrize("script", ["run_v21.sh", "dchub-mcp-v2.1/run_v21.sh"])
def test_run_v21_invokes_the_canonical_tool(script):
    """Resolve the gen_dev_key.py paths run_v21.sh runs, and check where they land.

    Both copies of run_v21.sh set BUNDLE="${HERE}/dchub-mcp-v2.1" with
    HERE="$(pwd)", so BUNDLE is always <repo-root>/dchub-mcp-v2.1.
    """
    path = _ROOT / script
    if not path.exists():  # pragma: no cover
        pytest.skip(f"{script} not present")
    text = path.read_text()

    invoked = re.findall(r'"?\$\{(BUNDLE|HERE)\}/gen_dev_key\.py"?', text)
    assert invoked, (
        f"{script} no longer invokes gen_dev_key.py via ${{BUNDLE}}/${{HERE}} — "
        "this guard can no longer see what it runs; update the test with it."
    )

    base = {"BUNDLE": _ROOT / "dchub-mcp-v2.1", "HERE": _ROOT}
    for var in set(invoked):
        target = base[var] / "gen_dev_key.py"
        assert target.exists(), f"{script} invokes a missing {target}"
        assert target.resolve() == _CANON.resolve(), (
            f"{script} invokes {target.relative_to(_ROOT)}, which is NOT the root "
            "gen_dev_key.py — it is a separate implementation that can drift."
        )
