"""Provenance envelope v1 (2026-07-11, LOCKED 2026-07-11) — the
citation-confidence moat.

WHY: AI agents citing DC Hub need to know HOW MUCH to trust each figure.
Nobody else in the vertical (LandGate/WoodMac, DC Byte, datacenterHawk,
Enverus) stamps per-record verification + collection-level provenance on
their data responses — this envelope is the asset that survives an
incumbent MCP launch.

V1 CONTRACT LOCK (Gemini partnership contract-hardening, 2026-07-11):
``provenance_version``, ``fallback_url`` and ``default_v`` are now part of
the frozen v1 schema. Changing the MEANING of any existing key requires a
version bump; adding keys stays allowed (additive-only).

DESIGN (payload-discipline locked — a past optimization cut list payloads
−47%; do not undo it):

  * ONE collection-level ``provenance`` block per response (never per
    record)::

        {
          "provenance_version": 1,                  # v1 lock — always present
          "source":  "...where the data comes from...",
          "method":  "...how it was collected/derived...",
          "as_of":   "2026-07-11T...",              # only when meaningful
          "verification_counts": {"verified": N, "tracked": N},  # optional
          "cite_url_template": "https://dchub.cloud/facilities/{slug}",
          "fallback_url": "https://dchub.cloud/facilities/directory",
                          # always present — the deterministic cite URL a
                          # model uses when a record lacks the template's
                          # substitution variable
          "default_v": "tracked",                   # always present — the
                          # confidence tier a record WITHOUT a per-record
                          # ``v`` field inherits (the LOWEST tier that can
                          # legitimately appear in the collection)
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

PROVENANCE_VERSION = 1
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

# v1 fallback URLs — the deterministic page a model cites when a record
# lacks the cite_url_template's substitution variable. Most specific stable
# page per surface; same concatenation style as the templates above (keeps
# the url_registry chokepoint lint authoritative for real emitters).
FACILITIES_FALLBACK_URL = _BASE + "/facilities/" + "directory"
MARKETS_FALLBACK_URL = _BASE + "/markets/" + "directory"
DEFAULT_FALLBACK_URL = _BASE

_MINIMAL_BLOCK = {
    "provenance_version": PROVENANCE_VERSION,
    "source": "DC Hub (dchub.cloud)",
    "fallback_url": DEFAULT_FALLBACK_URL,
    "license": LICENSE,
    "cite_as": CITE_AS,
}


def _fallback_for(cite_template):
    """Map a collection cite template to its surface's fallback URL.
    facilities → the facilities directory; markets/DCPI → the markets
    directory; anything else (or no template) → the site root. NEVER
    raises."""
    try:
        t = str(cite_template or "")
        if "/facilities/" in t:
            return FACILITIES_FALLBACK_URL
        if "/markets/" in t or "/dcpi/" in t:
            return MARKETS_FALLBACK_URL
    except Exception:
        pass
    return DEFAULT_FALLBACK_URL


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
                     cite_template=None, fallback_url=None, *, default_v):
    """Build the collection-level provenance block. NEVER raises once
    called (``default_v`` is a REQUIRED keyword — forgetting it at a wiring
    site fails loudly at review/test time, by design).

    source        — where the data comes from (dataset/feed name).
    method        — how it was collected/derived, incl. what the per-record
                    ``v`` flag means on this response (when present).
    as_of         — datetime/date/ISO-string data vintage; omit for live
                    row-level-dated collections (deals, grid mix_period).
    counts        — optional {"verified": N, "tracked": N} (or
                    {"published": N, ...}) verification tally.
    cite_template — optional collection-level cite URL template.
    fallback_url  — optional explicit fallback cite URL; when omitted it is
                    derived from cite_template (facilities → facilities
                    directory, markets/dcpi → markets directory, else the
                    site root). Always emitted (v1 lock).
    default_v     — REQUIRED (v1 lock): the confidence tier a record
                    WITHOUT a per-record ``v`` field inherits. Pass the
                    LOWEST (most conservative) tier that can legitimately
                    appear in this collection — facilities: "tracked";
                    queue/grid: "published" or "inferred"; deals: "tracked".
    """
    try:
        block = {
            "provenance_version": PROVENANCE_VERSION,
            "source": str(source),
            "method": str(method),
            "default_v": str(default_v),
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
        # v1 lock: fallback_url is ALWAYS present (explicit > derived).
        try:
            block["fallback_url"] = (str(fallback_url) if fallback_url
                                     else _fallback_for(cite_template))
        except Exception:
            block["fallback_url"] = DEFAULT_FALLBACK_URL
        return block
    except Exception:
        blk = dict(_MINIMAL_BLOCK)
        try:
            blk["default_v"] = str(default_v)
        except Exception:
            pass
        return blk


def attach_provenance(payload, source, method, as_of=None, counts=None,
                      cite_template=None, fallback_url=None, *, default_v):
    """Stamp ``payload['provenance']`` in place (dict payloads only; never
    overwrites an existing block; never raises once called — ``default_v``
    is a required keyword, mirroring provenance_block). Returns the
    payload."""
    try:
        if isinstance(payload, dict) and "provenance" not in payload:
            payload["provenance"] = provenance_block(
                source, method, as_of=as_of, counts=counts,
                cite_template=cite_template, fallback_url=fallback_url,
                default_v=default_v)
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
