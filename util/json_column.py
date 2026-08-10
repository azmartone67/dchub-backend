"""Serialise a payload to JSON that is ALWAYS valid, however large it gets.

★ r-json-truncation (2026-08-10) — THE FIVE-DAY PRESS OUTAGE, AND ITS CLASS.

`json.dumps(payload)[:8000]` slices the SERIALISED STRING, not the data. Every
json/jsonb column parses what it is handed, so a truncated blob fails the whole
statement:

    psycopg2.errors.InvalidTextRepresentation: invalid input syntax for type json
    DETAIL: Token ""Midland\\u2...        <- the cut fell inside a \\uXXXX escape

★ The mid-escape cut in that report is INCIDENTAL, not the rare trigger.
Truncating a serialised object leaves its brackets unbalanced, so essentially
EVERY cut is invalid — verified against a live jsonb column, where a plain cut
at an ordinary comma failed just as hard, reporting only an invalid bare quote
token. What made the outage intermittent was solely whether the payload
exceeded the cap that day; once it reliably did, the loss was total.

Why total: in marketing_engine._write_release that INSERT was the LAST statement
of a single transaction and the `except` around it calls rollback(), so its
failure discarded the press_releases row and the press_integrity review too. The
release simply never existed and nothing a dashboard reads showed a trace.

    ★ CRITICAL DISTINCTION, for anyone auditing this class:
        json.dumps(xs[:8])      slices the DATA    — always SAFE
        json.dumps(xs)[:8000]   slices the TEXT    — the bug
    A grep cannot tell them apart. tests/test_json_column_binding.py walks the
    AST for a Subscript(Slice) over a Call to json.dumps and fails the build if
    one ever binds to a database parameter again.

Truncating DATA is fine; truncating JSON TEXT is never fine. When a payload is
too large this stores a VALID object that says so, keeping whatever is still
worth having: the caller's chosen scalar keys, the original size, and a preview.
"""

import json

__all__ = ["json_for_column"]

# Room the bookkeeping keys need before a preview is worth attempting.
_STUB_HEADROOM = 200


def json_for_column(payload, max_chars: int = 8000, keep_keys=()) -> str:
    """Return JSON text for a json/jsonb column that is valid at any size.

    Small payloads pass through WHOLE — that non-vacuity matters, because a
    helper that always returned the stub would satisfy "the output parses"
    while destroying every audit row it touched.

    `keep_keys` names scalar keys worth preserving from a dict payload when the
    full thing does not fit, so the surviving row still identifies itself.

    Never raises: an unserialisable payload becomes a marker object. These calls
    sit inside live transactions, and raising here would recreate the outage
    through a different door.
    """
    try:
        blob = json.dumps(payload, default=str)
    except Exception:
        return json.dumps({"_unserialisable": True})

    if len(blob) <= max_chars:
        return blob

    keep = {}
    try:
        for k in keep_keys or ():
            v = (payload or {}).get(k)
            if isinstance(v, (str, int, float, bool)):
                keep[k] = str(v)[:300]
    except Exception:
        pass                      # payload is not a mapping — nothing to keep
    keep["_truncated"] = True
    keep["_original_chars"] = len(blob)

    # A preview keeps the row debuggable. It is a JSON *string value*, so
    # json.dumps escapes it and the result stays valid no matter where the
    # source blob was cut.
    room = max_chars - len(json.dumps(keep)) - _STUB_HEADROOM
    if room > 0:
        preview = dict(keep)
        preview["_preview"] = blob[:room]
        out = json.dumps(preview)
        if len(out) <= max_chars:
            return out

    out = json.dumps(keep)
    if len(out) <= max_chars:
        return out
    # Caller passed a cap smaller than the bookkeeping itself. Emit the
    # irreducible valid object rather than slicing our way back into the bug.
    return json.dumps({"_truncated": True, "_original_chars": len(blob)})
