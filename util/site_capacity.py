"""util/site_capacity.py — make `capacity_mw` load-bearing on /api/site-score.

THE DEFECT THIS CLOSES (measured live 2026-08-25, Dallas 32.7767,-96.7970):

    analyze_site{lat, lon}                -> composite_score 81.2
    analyze_site{lat, lon, capacity_mw:1} -> composite_score 81.2
    analyze_site{lat, lon, capacity_mw:5000} -> composite_score 81.2

Byte-identical after subtracting per-call noise. `capacity_mw` is DECLARED on
analyze_site and compare_sites, reaches the backend, is read into `capacity` —
and then used exactly once, echoed back as `capacity_requested_mw`. On the free
tier even that echo is stripped, so an agent can request 5,000 MW and get a
response carrying no trace of the constraint at all. Meanwhile the tool
description tells it to: "Set capacity_mw to your target load (e.g. 1000 for a
1 GW cluster)."

★ WHY THIS DOES NOT MOVE `overall_score`, DELIBERATELY.

The obvious "wiring" is to re-weight the composite by requested load. That would
be an UNDISCLOSED MEASUREMENT-DEFINITION CHANGE to a number that is published as
a citable free-tier headline and carries `methodology_version` —
routes/brain_consistency_radar has a `measurement_definition_changed` detector
for exactly this, and it would be right to fire. A cited index does not silently
acquire a new input.

So the requested load gets its OWN derived block. The parameter becomes
load-bearing (the response genuinely changes with it), the composite keeps its
published definition, and the block says plainly which is which and where the
load IS applied.

★ NAMEPLATE IS NOT HEADROOM. `generation_mw` here is installed nameplate within
80 km across all fuels and statuses. It is an ORDER-OF-MAGNITUDE frame for "is
this ask small or large for this area", never an interconnection answer. Saying
so is the point — see reference_dchub_grid_intelligence_fix (headroom fails
CLOSED) and the constraint_coverage contract the server instructions describe.
"""
from __future__ import annotations

import math

# Where the requested load IS applied, for the `instead` pointer. Kept as one
# string so the block can never name a tool that contradicts the tool list.
_INSTEAD = ("get_power_availability_timeline(state=…, mw=…) applies the requested load to a "
            "time-to-power read; get_hosting_capacity / get_retirement_headroom give draw-side "
            "headroom where a utility publishes it. This block is a scale frame, not either of those.")


def _fmt(n) -> str:
    """Thousands-separated, no trailing .0 on whole numbers."""
    f = float(n)
    return f"{int(round(f)):,}" if abs(f - round(f)) < 0.05 else f"{f:,.1f}"


def site_capacity_context(requested_mw, generation_mw, power_plants=None,
                          substations=None, radius_km: int = 80) -> dict | None:
    """Derived read of a REQUESTED load against nearby installed generation.

    Returns None when no positive load was requested — the block must be ABSENT
    rather than zero-filled, so its presence is itself the signal that the
    argument was received and used. A zero-filled block would be indistinguishable
    from the silent drop this exists to end.

    Never raises: a bad or missing generation figure yields an explicit
    unmeasured read, never a fabricated ratio.
    """
    try:
        req = float(requested_mw or 0)
    except (TypeError, ValueError):
        return None
    # ★ isfinite, not just > 0. nan and inf both survive float() and a bare
    # `> 0` test: inf overflows the formatter, and nan propagates through the
    # ratio into `NaN` — which json.dumps emits happily and no strict JSON
    # parser on the agent side will accept. Both were caught by the hostile-input
    # guard, not by review.
    if not math.isfinite(req) or req <= 0:
        return None

    try:
        gen = float(generation_mw)
        if not math.isfinite(gen):
            gen = None                 # nan/inf read as unmeasured, never as a ratio
        elif gen < 0:
            gen = 0.0
    except (TypeError, ValueError):
        gen = None

    out = {
        "requested_mw": round(req, 1),
        "affects_overall_score": False,
        "why_not": ("overall_score is a published location-suitability index with a fixed "
                    "methodology_version and a citable free-tier headline. Adding a new input to it "
                    "without a version bump would be an undisclosed definition change, so the "
                    "requested load is reported here instead of silently re-weighting that number."),
        "instead": _INSTEAD,
    }

    if gen is None:
        out["nearby_generation_mw"] = None
        out["basis"] = "unmeasured — nearby generation did not resolve for this location"
        out["note"] = (f"Requested load {_fmt(req)} MW recorded. Nearby installed generation could not be "
                       f"measured here, so no scale comparison is offered rather than a fabricated one.")
        return out

    out["nearby_generation_mw"] = round(gen, 1)
    out["basis"] = (f"installed nameplate generation within {radius_km} km, all fuels and statuses "
                    f"(EIA/HIFLD). NAMEPLATE IS NOT AVAILABLE HEADROOM — it is not what an "
                    f"interconnection study would grant, and none of it is reserved for this site.")
    if power_plants is not None:
        out["nearby_power_plants"] = power_plants
    if substations is not None:
        out["nearby_substations_50km"] = substations

    if gen <= 0:
        out["requested_pct_of_nearby_generation"] = None
        out["note"] = (f"Requested load {_fmt(req)} MW against NO installed generation mapped within "
                       f"{radius_km} km. Treat this as an unserved-area signal, not as a zero.")
        return out

    pct = (req / gen) * 100.0
    out["requested_pct_of_nearby_generation"] = round(pct, 2)
    # Adaptive precision: a 1 MW ask against 12 GW is 0.008%, and rendering that
    # as "0.0%" would erase the very signal the block exists to carry.
    _p = f"{pct:.1f}" if pct >= 1 else (f"{pct:.2f}" if pct >= 0.01 else f"{pct:.3g}")
    out["note"] = (f"Requested load {_fmt(req)} MW is {_p}% of ALL installed generation nameplate "
                   f"within {radius_km} km ({_fmt(gen)} MW). Nameplate is not available headroom; this "
                   f"is a scale frame only.")
    return out
