#!/usr/bin/env python3
"""tests/test_asset_ingest_secret_guards_are_loud.py — an infrastructure ingest
lane must never turn a missing secret into a green no-op.

NO NETWORK, NO DB. Reads the workflow YAML as text.

WHAT WENT WRONG. Every asset-ingest workflow guarded its admin key like this:

    if [ -z "${DCHUB_ADMIN_KEY:-}" ]; then
      echo "::warning::DCHUB_ADMIN_KEY not set — skipping"; exit 0
    fi

A deleted or rotated secret therefore produced a GREEN run that ingested nothing.
gas-pipeline-ingest.yml fixed this on 2026-08-08 (audit SH52-120) and recorded
the reasoning — "a red run is cheap and honest; the ingest is idempotent so a
retry after a fix is safe" — but the fix was applied to one file. Its five
siblings kept the silent form for another three weeks.

★ THAT IS THE FAILURE MODE THIS REPO KEEPS REPEATING: a correct fix landed in
one place, and nothing made the siblings follow. This test is the thing that
makes them follow.

SCOPE, DELIBERATELY NARROW. The silent-skip idiom appears ~70 times across ~50
workflows — brain ticks, media publishing, SEO submits. Most of those have a
defensible reason to skip: they are optional enrichment, not the lane that keeps
a dataset alive. This test governs ONLY the workflows whose job is to write
infrastructure asset rows. The rest are a real backlog and are named in
_OUT_OF_SCOPE below so they are a number somebody can drive down rather than a
rumour — the same shape as tests/test_admin_gate_fail_closed.py's baseline.

Run standalone:   python3 tests/test_asset_ingest_secret_guards_are_loud.py
Run under pytest: pytest tests/test_asset_ingest_secret_guards_are_loud.py
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"

# The lanes whose job is to keep an infrastructure dataset alive. A silent skip
# here means a dataset quietly stops being refreshed, which is the defect.
ASSET_INGEST = (
    "gas-pipeline-ingest.yml",
    "transmission-ingest.yml",
    "power-plants-ingest.yml",
    "generator-inventory-ingest.yml",
    "planned-generators-ingest.yml",
    "fcc-fiber-refresh.yml",
)

# Frozen 2026-09-02. Everything else in .github/workflows that still turns a
# missing secret into a green no-op. NOT governed by this test — brain/media/SEO
# lanes can legitimately skip. Lower this number as they are fixed; the test
# fails on an INCREASE, which is what stops a new one landing.
_OUT_OF_SCOPE_SILENT_SKIPS = 69

_SILENT = re.compile(r"not set\s+—\s+skipping|secret not set\s+—\s+skipping", re.I)


def _text(name):
    p = WF / name
    assert p.exists(), f"{name} does not exist — update ASSET_INGEST"
    return p.read_text(encoding="utf-8")


@pytest.mark.parametrize("wf", ASSET_INGEST)
def test_asset_ingest_fails_loudly_on_a_missing_secret(wf):
    """The guard must exit non-zero and say so as an ::error::, not a ::warning::."""
    src = _text(wf)
    assert "exit 0" not in _guard_block(src), (
        f"{wf}: the secret guard still exits 0 — a rotated key would produce a "
        f"green run that ingested nothing")
    assert "::error::" in src, (
        f"{wf}: no ::error:: annotation; a missing secret must be a red run")
    assert "exit 1" in src, f"{wf}: the secret guard never fails the job"


def _guard_block(src):
    """The lines of every `if [ -z ... ]` secret check, and what they do."""
    out, depth = [], 0
    for line in src.splitlines():
        if re.search(r"if \[ -z \"\$\{[A-Z_]*(KEY|TOKEN|USERNAME)[A-Z_]*:?-?\}\"", line):
            depth = 6
        if depth:
            out.append(line)
            depth -= 1
    return "\n".join(out)


@pytest.mark.parametrize("wf", ASSET_INGEST)
def test_asset_ingest_has_no_silent_skip_wording(wf):
    """The `— skipping` phrasing is the tell; it always paired with exit 0."""
    assert not _SILENT.search(_text(wf)), (
        f"{wf}: still says 'not set — skipping'. If a skip is genuinely correct "
        f"here, say WHY in a comment and remove this file from ASSET_INGEST "
        f"deliberately, under review.")


def test_the_out_of_scope_backlog_does_not_grow():
    """A ratchet, not a rule. It fails on an INCREASE so a new silent skip cannot
    land unnoticed, and on a DECREASE so the number stays honest when someone
    fixes a batch."""
    silent = 0
    for p in sorted(WF.glob("*.yml")):
        if p.name in ASSET_INGEST:
            continue
        silent += len(_SILENT.findall(p.read_text(encoding="utf-8")))
    assert silent == _OUT_OF_SCOPE_SILENT_SKIPS, (
        f"out-of-scope silent skips moved {_OUT_OF_SCOPE_SILENT_SKIPS} -> {silent}. "
        f"If you fixed some, lower _OUT_OF_SCOPE_SILENT_SKIPS. If this went UP, a "
        f"new green-no-op guard just landed.")


def test_the_governed_list_is_not_quietly_emptied():
    """A parametrized test over an empty list passes vacuously and reports green
    forever — the exact shape this whole program exists to eliminate."""
    assert len(ASSET_INGEST) >= 6, "ASSET_INGEST shrank; a lane lost its guard fence"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
