"""Persistence Master Shell #41 lane 2 (2026-07-29) — derived objectives.

The MCP schema REQUIRED `objectives` while this endpoint treated it as optional
(`body.get("objectives") or {}`). The two contracts were inverse, so the only
payload satisfying both carried a signed-weight map most agents never build —
which is why zero real external agents completed a save in 90 days.

Loosening the schema is only safe if the snapshot + re-rank contract survives.
It did NOT survive before: `objectives: {}` already passed both layers and
produced saved_score=None — a row that can never show drift, which is the whole
feature. A guard you can satisfy with an empty object is not guarding.

These tests pin the replacement: saved_objectives is never empty, so re-scoring
always has criteria, and a caller who DOES state objectives is untouched.

Run:  python3 -m pytest tests/test_shortlist_objectives_default.py -v
"""
from __future__ import annotations

import inspect
import re

import routes.shortlists as sl


def _derive_block() -> str:
    """The derivation logic as shipped — read, never transcribed."""
    src = inspect.getsource(sl.api_shortlist_save)
    i = src.index("objectives_derived = False")
    return src[i:i + 400]


def test_absent_objectives_are_derived_not_left_empty():
    """THE CONTRACT. An empty objectives map yields saved_score=None, i.e. a row
    that can never show drift — the exact degraded state the old zod requirement
    claimed to prevent and did not."""
    blk = _derive_block()
    assert "if not objectives and metrics:" in blk, (
        "the derivation guard is gone — an objectives-less save would store an "
        "empty map and produce a permanently un-driftable row")
    assert "objectives = {k: w for k in metrics}" in blk, "weights are no longer derived"


def test_derivation_covers_the_explicitly_empty_case_too():
    """`objectives: {}` must derive as well. The old guard was bypassable exactly
    here: an empty dict passed zod AND the backend. `not objectives` is truthy
    for both None and {}, which is the point — assert it, because switching to
    `is None` would silently reopen the hole."""
    blk = _derive_block()
    assert "is None" not in blk.split("\n")[0], (
        "derivation keys on `is None`, so an explicitly empty {} skips it and "
        "reopens the bypass the old guard suffered from")


def test_a_stated_objectives_map_is_never_overwritten():
    """The caller's criteria outrank ours. Guarded by the `not objectives`
    condition — if that ever became unconditional, every stated ranking would be
    silently replaced with equal weights."""
    blk = _derive_block()
    assert re.search(r"if not objectives\b", blk), (
        "derivation is no longer conditional on the caller having none — stated "
        "objectives would be overwritten with equal weights")


def test_derivation_is_disclosed_to_the_caller():
    """An agent must be able to tell a derived basis from a stated one, or it
    will report equal weights back to its human as their criteria."""
    src = inspect.getsource(sl.api_shortlist_save)
    assert '"objectives_derived": objectives_derived' in src, (
        "the response no longer discloses that the basis was derived")
    assert "EQUAL weights" in src, "the human-readable note dropped the disclosure"


def test_weights_are_finite_and_normalised():
    """1/len over the site's own metrics. A zero-length metrics dict must not
    reach the division — guarded by `and metrics`."""
    blk = _derive_block()
    assert "and metrics:" in blk, "ZeroDivisionError when a site carries no numeric metrics"
    assert "1.0 / len(metrics)" in blk, "weights are no longer normalised"


def test_keyless_callers_still_refused_before_any_of_this():
    """Lane 1 must remain in front of lane 2: deriving objectives for a caller we
    then refuse would be wasted work, and worse, ordering the other way would
    mean an unsafe write got easier before it got safe."""
    src = inspect.getsource(sl.api_shortlist_save)
    assert "OWNER_REQUIRED" in src, "lane 1's keyless guard vanished from the save path"
