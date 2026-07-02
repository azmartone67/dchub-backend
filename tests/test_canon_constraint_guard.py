"""r-fixpack (2026-07-02) — canon-constraint + poison-buffer canary.

Static source assertions guarding the 2026-07-02 fixpack (commit
2ac9b095) against reverts. The rogue Neon constraint
`mcp_connections_platform_unique` was created out-of-band TWICE, capping
the append-only mcp_connections event log at one row per platform: every
direct INSERT for an already-seen platform raised 23505, got buffered
into SQLite, and the 60s sync loop's break-on-error replayed the poison
row forever — spamming logs every 60s and head-of-line-blocking ALL
buffered MCP telemetry (6 rows landed in 14 days).

Guards (all static reads — no DB, no network, sub-second in CI; same
source-extraction style as the rest of the pre-merge gauntlet's
pure-function tests):

  1. ai_tracking.init_db() still contains the idempotent
     DROP CONSTRAINT / DROP INDEX for mcp_connections_platform_unique,
     so a restore/replica/out-of-band DDL can't resurrect the constraint
     past the next boot.
  2. ai_tracking._sync_buffer_to_neon() still dead-letters duplicate-key
     (pgcode 23505 / UniqueViolation) rows — marks them synced and
     CONTINUES instead of break, so one poison row can't block the queue.
  3. mcp_calls_deloop.PROBE_PLATFORMS still contains 'dchub-canon-probe'
     so the AI-Surface sentinel's canon probe traffic keeps classifying
     as self-traffic (it identified as bare 'canon' before 2026-07-02).

Crude by design: this is "did someone revert this" protection, not a
behavior test. If you intentionally refactor one of these anchors, move
the protection with it — do not just delete the assertion.

Run locally:
    python -m pytest tests/test_canon_constraint_guard.py -v
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AI_TRACKING = REPO_ROOT / "ai_tracking.py"
DELOOP = REPO_ROOT / "mcp_calls_deloop.py"

FIXPACK = "commit 2ac9b095 (2026-07-02 mcp_connections poison-loop fixpack)"


def _func_source(path: Path, name: str) -> str:
    """Extract the full source of a top-level (or nested) function by name."""
    src = path.read_text(encoding="utf-8", errors="ignore")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(src, node)
            if seg:
                return seg
    raise AssertionError(
        f"{path.name}: function {name}() not found — this guard anchors to "
        f"it ({FIXPACK}). If it was renamed, update this test to follow it."
    )


def test_init_db_drops_rogue_platform_unique_constraint():
    """init_db() must keep the idempotent DROP for the rogue constraint.

    The constraint was created out-of-band on live Neon TWICE (no repo
    code creates it). The each-boot DROP in init_db() is the only thing
    stopping a third resurrection (restore, replica promotion, manual
    DDL) from re-poisoning the telemetry pipeline.
    """
    body = _func_source(AI_TRACKING, "init_db")
    assert "DROP CONSTRAINT IF EXISTS mcp_connections_platform_unique" in body, (
        "ai_tracking.init_db() no longer drops the rogue UNIQUE(platform) "
        "constraint mcp_connections_platform_unique. This DROP is the "
        f"boot-time immune response added in {FIXPACK} — without it, an "
        "out-of-band recreation caps mcp_connections at one row per "
        "platform and re-poisons the buffer sync loop. Restore the "
        "ALTER TABLE ... DROP CONSTRAINT IF EXISTS line."
    )
    assert "DROP INDEX IF EXISTS mcp_connections_platform_unique" in body, (
        "ai_tracking.init_db() dropped the constraint but not its backing "
        f"index (see {FIXPACK}). Keep BOTH idempotent DROP statements — a "
        "leftover unique index enforces the same one-row-per-platform cap."
    )


def test_sync_buffer_dead_letters_duplicate_key_rows():
    """_sync_buffer_to_neon() must dead-letter 23505 rows, not break.

    The original bug: the sync loop's except handler did `break` on ANY
    error, so a permanently-failing duplicate-key row was replayed every
    60s forever, head-of-line-blocking all buffered MCP telemetry. The
    fix marks 23505 rows synced (dead-letter) and continues.
    """
    body = _func_source(AI_TRACKING, "_sync_buffer_to_neon")
    assert ("23505" in body) or ("UniqueViolation" in body), (
        "ai_tracking._sync_buffer_to_neon() no longer special-cases "
        "duplicate-key errors (pgcode 23505 / UniqueViolation). Without "
        "the dead-letter branch a single poison row is replayed every "
        f"60s forever and blocks the whole buffer (see {FIXPACK})."
    )
    assert "continue" in body, (
        "ai_tracking._sync_buffer_to_neon() has no `continue` in its row "
        "loop — the 23505 handler must mark the row synced and CONTINUE "
        "to the next row, never break-on-error (the break was the "
        f"head-of-line-blocking half of the bug; see {FIXPACK})."
    )


def test_probe_platforms_contains_canon_probe():
    """PROBE_PLATFORMS must classify the canon probe as self-traffic.

    ai_surface_canon.py identifies as 'dchub-canon-probe' since
    2026-07-02 (was bare 'canon'). If that name falls out of
    mcp_calls_deloop.PROBE_PLATFORMS, every canon sweep inflates
    real-agent metrics (reach, funnel, /ai headline) with self-traffic.
    """
    src = DELOOP.read_text(encoding="utf-8", errors="ignore")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "PROBE_PLATFORMS":
                    values = ast.literal_eval(node.value)
                    assert "dchub-canon-probe" in values, (
                        "mcp_calls_deloop.PROBE_PLATFORMS no longer contains "
                        "'dchub-canon-probe' — the AI-Surface sentinel's probe "
                        "traffic would be counted as a real external agent in "
                        f"every reach/funnel metric (see {FIXPACK})."
                    )
                    return
    raise AssertionError(
        "mcp_calls_deloop.py has no top-level PROBE_PLATFORMS assignment — "
        f"this guard anchors to it ({FIXPACK}). If the constant moved, "
        "update this test to follow it."
    )
