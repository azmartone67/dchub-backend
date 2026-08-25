"""One name, four shapes — say which one you sent.

★★★ WHY (2026-08-25). `constraint_coverage` is the field DC Hub points at when
it claims it publishes its own limits. Measured live on that date, it ships in
FOUR mutually incompatible shapes under that one name:

    get_power_availability_timeline  list[str]
        ["generation ≠ deliverable load: …", …]

    rank_sites                       dict[str, str]
        {"power_score": "unavailable"}

    site_selection_canvas            dict[str, {applied, reason, instead}]
        {"capacity_mw": {"applied": false, "reason": "…", "instead": "…"}}

    cross_layer_sites                dict[str, {status, reason}]
        {"headroom_mw": {"status": "unavailable", "reason": "…"}}

An agent that writes `for c in constraint_coverage` gets prose strings from the
first and dict KEYS from the other three — a silent wrong-type read, no error
raised. That is worse than a missing block, because it looks like it worked.

★ This module does NOT unify the shapes. Unifying them is a breaking change for
every consumer that already parses one of them, and the shapes carry genuinely
different information — an argument disposition is not a field status. What it
does is make the shape MACHINE-READABLE, so a consumer can branch instead of
sniffing types.

★★★ DERIVED, NEVER DECLARED. `shape_of()` reads the value that is actually
being sent. A hardcoded per-emitter label is a second thing to keep in sync, and
this codebase's whole 08-25 sweep was about labels that drifted from what the
response really contained (`_facility_count_notes.primary`, the MCP
`instructions` sentence). A label you compute cannot drift from the payload.
"""
from __future__ import annotations

# The vocabulary. Published alongside the value so a consumer can switch on it.
CAVEAT_LIST = "caveat_list"                  # list[str] — prose limitations
FIELD_STATUS_MAP = "field_status_map"        # {field: "status"}
FIELD_STATUS_DETAIL = "field_status_detail"  # {field: {status, reason, …}}
ARGUMENT_DISPOSITION = "argument_disposition"  # {arg: {applied, reason, instead}}
EMPTY = "empty"                              # nothing withheld — NOT "unknown"
UNKNOWN = "unknown"                          # a shape this vocabulary cannot name

SHAPES = (CAVEAT_LIST, FIELD_STATUS_MAP, FIELD_STATUS_DETAIL,
          ARGUMENT_DISPOSITION, EMPTY, UNKNOWN)

# What each value means, shipped with the response so the vocabulary is not
# something a consumer has to find in a changelog.
SHAPE_LEGEND = {
    CAVEAT_LIST: "list of prose strings, each a limitation of this answer",
    FIELD_STATUS_MAP: "object keyed by field name, value is a status string",
    FIELD_STATUS_DETAIL: "object keyed by field name, value is an object carrying at least `status` and `reason`",
    ARGUMENT_DISPOSITION: "object keyed by ARGUMENT you sent, value carries `applied` — false means the argument was accepted by the schema and then NOT used, with `reason` and `instead`",
    EMPTY: "nothing was withheld on this call — an empty coverage block, not an unknown one",
    UNKNOWN: "a shape this vocabulary does not name; read it defensively",
}


def shape_of(coverage) -> str:
    """Name the shape of a constraint_coverage value by INSPECTING it.

    Order matters: argument_disposition is checked before field_status_detail
    because an `applied` key is the strictly more specific signal, and it is the
    one an agent most needs to notice — it means an argument it sent was
    silently dropped.
    """
    if coverage is None:
        return EMPTY
    if isinstance(coverage, (list, tuple)):
        return CAVEAT_LIST if coverage else EMPTY
    if isinstance(coverage, dict):
        if not coverage:
            return EMPTY
        values = list(coverage.values())
        if all(isinstance(v, str) for v in values):
            return FIELD_STATUS_MAP
        if all(isinstance(v, dict) for v in values):
            if any("applied" in v for v in values):
                return ARGUMENT_DISPOSITION
            if all("status" in v for v in values):
                return FIELD_STATUS_DETAIL
        return UNKNOWN
    return UNKNOWN


def annotate(payload: dict, key: str = "constraint_coverage") -> dict:
    """Stamp `<key>_shape` next to `<key>`, in place. Returns the payload.

    No-op when the key is absent — an emitter that does not publish coverage
    must not grow an orphan shape field claiming it does.
    """
    if not isinstance(payload, dict) or key not in payload:
        return payload
    payload[key + "_shape"] = shape_of(payload[key])
    return payload
