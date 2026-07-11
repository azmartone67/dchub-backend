"""Provenance envelope v1 (2026-07-11) — the citation-confidence moat.

WHY: AI agents citing DC Hub need to know HOW MUCH to trust each figure.
Nobody else in the vertical (LandGate/WoodMac, DC Byte, datacenterHawk,
Enverus) stamps per-record verification + collection-level provenance on
their data responses — this envelope is the asset that survives an
incumbent MCP launch.

DESIGN (payload-discipline locked — a past optimization cut list payloads
−47%; do not undo it):

  * ONE collection-level ``provenance`` block per response (never per
    record)::

        {
          "source":  "...where the data comes from...",
          "method":  "...how it was collected/derived...",
          "as_of":   "2026-07-11T...",              # only when meaningful
          "verification_counts": {"verified": N, "tracked": N},  # optional
          "cite_url_template": "https://dchub.cloud/facilities/{slug}",
          "license": "CC-BY-4.0",
          "cite_as": "DC Hub, dchub.cloud"
        }

  * Per-record: ONE compact field only — ``v``:
      - facilities:  "verified" | "tracked"   (canonical fleet filter
        COALESCE(is_duplicate,0)=0 on discovered_facilities — see
        canonical_stats.py, issue #1539)
      - queue/large-load: "published" | "inferred"  (published ISO figure
        vs name-match/derived inference — mirrors the depth shell's
        published_queue/inferred split)
  * Per-record ``as_of`` only where it genuinely differs per row (deals
    have dates, grid rows have mix_period — those already exist; never
    duplicate them).
  * cite_url_template lives at COLLECTION level — never per-row URLs.

FAIL-SOFT CONTRACT: nothing in this module may ever break a response.
Every public helper catches everything and degrades to a minimal-but-valid
value. Existing _source/_cite/citation fields stay untouched (additive
only, backward compatible).
"""
from __future__ import annotations

LICENSE = "CC-BY-4.0"
CITE_AS = "DC Hub, dchub.cloud"

# Canonical cite-URL templates (collection-level only — bytes discipline).
# These are literal TEMPLATES handed to agents (the {placeholder} is filled
# client-side per record), NOT hand-rolled URL emission — so they are built
# by concatenation to keep the url_registry chokepoint lint
# (tests/test_url_registry_chokepoint.py) authoritative for real emitters.
_BASE = "https://dchub.cloud"
FACILITY_CITE_TEMPLATE = _BASE + "/facilities/" + "{slug}"
MARKET_CITE_TEMPLATE = _BASE + "/markets/" + "{market_slug}"
DCPI_CITE_TEMPLATE = _BASE + "/dcpi/" + "{market_slug}"

_MINIMAL_BLOCK = {
    "source": "DC Hub (dchub.cloud)",
    "license": LICENSE,
    "cite_as": CITE_AS,
}


def _iso(dt):
    """Best-effort ISO-8601 string from datetime/date/str. None on failure."""
    if dt is None:
        return None
    try:
        if hasattr(dt, "isoformat"):
            return dt.isoformat()
        s = str(dt).strip()
        return s or None
    except Exception:
        return None


def provenance_block(source, method, as_of=None, counts=None,
                     cite_template=None):
    """Build the collection-level provenance block. NEVER raises.

    source        — where the data comes from (dataset/feed name).
    method        — how it was collected/derived, incl. what the per-record
                    ``v`` flag means on this response (when present).
    as_of         — datetime/date/ISO-string data vintage; omit for live
                    row-level-dated collections (deals, grid mix_period).
    counts        — optional {"verified": N, "tracked": N} (or
                    {"published": N, ...}) verification tally.
    cite_template — optional collection-level cite URL template.
    """
    try:
        block = {
            "source": str(source),
            "method": str(method),
            "license": LICENSE,
            "cite_as": CITE_AS,
        }
        a = _iso(as_of)
        if a:
            block["as_of"] = a
        if counts:
            try:
                vc = {str(k): int(v) for k, v in dict(counts).items()
                      if v is not None}
                if vc:
                    block["verification_counts"] = vc
            except Exception:
                pass
        if cite_template:
            block["cite_url_template"] = str(cite_template)
        return block
    except Exception:
        return dict(_MINIMAL_BLOCK)


def attach_provenance(payload, source, method, as_of=None, counts=None,
                      cite_template=None):
    """Stamp ``payload['provenance']`` in place (dict payloads only; never
    overwrites an existing block; never raises). Returns the payload."""
    try:
        if isinstance(payload, dict) and "provenance" not in payload:
            payload["provenance"] = provenance_block(
                source, method, as_of=as_of, counts=counts,
                cite_template=cite_template)
    except Exception:
        pass
    return payload


def verified_flag(row, default="tracked"):
    """Per-record facilities flag: 'verified' | 'tracked'.

    'verified' = the row passes the canonical fleet filter
    COALESCE(is_duplicate,0)=0 (discovered_facilities). Rows that do not
    carry an ``is_duplicate`` key (e.g. the legacy ``facilities`` table)
    fall back to the conservative default — floors never over-claim.
    NEVER raises."""
    try:
        if isinstance(row, dict) and "is_duplicate" in row:
            return "tracked" if (row.get("is_duplicate") or 0) else "verified"
    except Exception:
        pass
    return default


def queue_flag(published=True):
    """Per-record queue/large-load flag: 'published' | 'inferred'.

    'published' = the ISO's own published queue/disclosure figure;
    'inferred'  = DC Hub derivation (name-match, fuel_type=Load inference,
    modeled estimate). NEVER raises."""
    try:
        return "published" if published else "inferred"
    except Exception:
        return "inferred"


def facility_verification_counts():
    """{'verified': N, 'tracked': N} from the cached canonical stats
    (canonical_stats.get_canonical_stats — 10-min TTL, floor-safe).
    Returns None on any failure so callers can omit the field."""
    try:
        from canonical_stats import get_canonical_stats
        s = get_canonical_stats()
        v = s.get("facilities_verified")
        t = s.get("facilities")
        if v and t:
            return {"verified": int(v), "tracked": int(t)}
    except Exception:
        pass
    return None
