"""relay_specificity.py — which human line a gated MCP response carried.

The gated response's one human line used to be fixed prose ("see what your
agent found and how to unlock it") and is now quantified wherever the gate
measured something ("3 of 47 `sites` rows — the other 44 ..."). That change
rests on a claim that can be wrong: that the line failed for want of CONTENT,
not placement. Placement was already measured in both positions —
5,704 paywall signals -> 1 real handoff open -> 0 converted (r-data-first,
2026-08-26) — so content is the remaining variable. Without recording which
variant actually went out, the claim can never be settled and simply becomes
another thing everyone believes.

It rides message_shown rather than a new column because that column means,
exactly, which message was shown — and the message is what varies. No DDL on a
fire-and-forget telemetry path (see _AB_DDL_LOCK in paywall_hint_middleware.py
for what that costs), and the `trial_preview` PREFIX is preserved so every
existing rollup and LIKE keeps matching. Nothing in this repo filters
message_shown on equality — checked before choosing this.

Its own module ON PURPOSE: tests/ deliberately never imports Flask or
the DB, so logic left inside the route handler would be untested by default —
the failure mode this repo has written down more than once.
"""

_RELAY_SPECIFICITY_VALUES = ("quantified", "generic")


def tag_relay_specificity(message_shown, relay_specificity, _max=2000):
    """Append the human-line variant to message_shown, or return it unchanged.

    Unchanged is the right answer for every uncertain case: an unknown label, a
    missing one, or no message at all. A telemetry field that guesses is worse
    than one that is absent, because the guess is what gets counted.
    """
    if not message_shown:
        return message_shown
    spec = (relay_specificity or "")
    if not isinstance(spec, str):
        return message_shown
    spec = spec.strip().lower()
    if spec not in _RELAY_SPECIFICITY_VALUES:
        return message_shown
    return (message_shown + ":" + spec)[:_max]


