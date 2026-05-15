"""
util/provenance.py — Phase GG (2026-05-14): shared provenance helpers.

The "show your work" layer. Every aggregated/bundled endpoint that wraps
multiple data sources should attach a `sources: [...]` block so the calling
agent (or human) can see exactly which row in which table was the basis
for each claim.

Usage:
    from util.provenance import src, attach_sources

    sources = [
        src("DCPI verdict BUILD",      "market_power_scores",  computed_at),
        src("Operator pipeline 4.2 GW", "capacity_pipeline",   first_seen),
    ]
    return jsonify(attach_sources(payload, sources))

The shape returned per source:
    {claim, source, observed_at, url}

`url` is optional — a deep-link an agent can pass back to a human.
"""
from datetime import datetime, timezone


def _iso(v):
    """Coerce a datetime/string to ISO-8601 or None."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:
            return None
    return None


def src(claim, source, observed_at=None, url=None):
    """Build a single provenance record.

    Args:
        claim: human-readable summary of what this source supports.
        source: short name of the underlying table / API / file.
        observed_at: datetime or ISO string when the row was recorded.
        url: optional deep-link.
    """
    rec = {
        "claim": str(claim)[:240] if claim is not None else None,
        "source": str(source)[:80] if source is not None else None,
        "observed_at": _iso(observed_at),
    }
    if url:
        rec["url"] = str(url)[:300]
    return rec


def attach_sources(payload, sources, generated_at=None):
    """Wrap a payload dict with provenance metadata.

    Returns a NEW dict (caller's payload is not mutated).
    The two added keys:
      - sources:        list of provenance records (may be empty)
      - generated_at:   ISO timestamp the bundle was assembled
    """
    if not isinstance(payload, dict):
        payload = {"result": payload}
    out = dict(payload)
    out["sources"] = [s for s in (sources or []) if s]
    out["generated_at"] = generated_at or datetime.now(timezone.utc).isoformat()
    return out


def now_iso():
    return datetime.now(timezone.utc).isoformat()
