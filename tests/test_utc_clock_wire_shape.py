"""utc_clock is the one UTC clock and the one wire shape — batch 2 fence.

★ THE TRAP. `datetime.utcnow()` is naive and deprecated, so the reflex fix is
`datetime.now(timezone.utc)`. For a SERIALIZED timestamp that is wrong, and
wrong in a way that ships:

    naive  utcnow().isoformat() + "Z"   -> 2026-09-05T04:24:58.917155Z
    aware  now(utc).isoformat() + "Z"   -> ...917282+00:00Z      <- malformed

Measured by AST on 2026-09-05: 347 sites carried that exact `+ "Z"` shape and
368 more emit a bare `.isoformat()` that would silently GAIN an offset. A
find-and-replace corrupts those into API responses and DB rows — far larger
than the ~90 naive/aware COMPARISON sites the deprecation notice points at.

★ `Z` is canonical BY DECISION: it is the majority shape. ops_activation.py and
ops_claims.py serve `+00:00` from their own aware helpers (measured live at
/api/v1/ops/install-stats) — drift for a later batch, not the standard.

★ utc_now() is deliberately AWARE and utc_iso_z() deliberately emits Z. Those
are different jobs: one is for arithmetic and comparison, the other is for the
wire. Mixing them up is how the remaining ~90 comparison sites will bite.
"""
import re
from datetime import datetime, timezone

from utc_clock import utc_iso_z, utc_now

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def test_utc_iso_z_emits_the_canonical_shape():
    got = utc_iso_z()
    assert ISO_Z.match(got), f"not the canonical ...Z wire shape: {got!r}"


def test_utc_iso_z_never_emits_an_offset():
    """The exact defect a naive swap to now(timezone.utc) introduces."""
    got = utc_iso_z()
    assert "+00:00" not in got, f"offset leaked into the wire shape: {got!r}"
    assert not got.endswith("+00:00Z"), f"malformed double suffix: {got!r}"


def test_shape_matches_what_the_call_sites_used_to_emit():
    """Digits differ (time passes); the SHAPE must not. This is the whole claim
    of the batch — the clock became aware, the serialized bytes did not move."""
    legacy = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
    shape = lambda s: re.sub(r"\d", "N", s)
    assert shape(utc_iso_z()) == shape(legacy)


def test_utc_now_is_aware_and_utc():
    """The comparison-safe half. A naive return here would silently reintroduce
    `can't compare offset-naive and offset-aware datetimes` at the call sites
    that do arithmetic."""
    n = utc_now()
    assert n.tzinfo is not None, "utc_now() must be tz-aware"
    assert n.utcoffset().total_seconds() == 0, "utc_now() must be UTC"


# ── must-fail controls ──
def test_the_shape_regex_rejects_every_defect_shape():
    assert not ISO_Z.match("2026-09-05T04:24:58.917282+00:00")   # aware, bare
    assert not ISO_Z.match("2026-09-05T04:24:58.917282+00:00Z")  # the malformed swap
    assert not ISO_Z.match("2026-09-05T04:24:58.917282")         # naive, no suffix
    assert ISO_Z.match("2026-09-05T04:24:58.917282Z")            # and accepts the real one


def test_the_awareness_assertion_can_fail():
    naive = datetime.now(timezone.utc).replace(tzinfo=None)
    assert naive.tzinfo is None, "control: a naive datetime must be detectable as naive"
