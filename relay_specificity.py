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

# ★ THE WRITER MUST KNOW EVERY LABEL THE GATE EMITS, or it drops them.
#
# This nearly failed silently and completely. dchub-mcp-server#318 renamed the
# arms to treatment/control/ineligible when assignment became randomized, and
# for a moment only the READER was taught the new vocabulary. This function is
# the WRITER: an unrecognised label is discarded and the row is written as a
# bare `trial_preview`, so the arm would never have been recorded at all. The
# reader would have been perfectly correct with nothing to read — the whole
# experiment logging zero, with no error anywhere.
#
# The rule that follows: a label vocabulary spans a WRITER and a READER in
# different repos, and both halves ship together or neither does.
#
# The pre-randomization labels stay accepted. They cost nothing, they keep the
# rollout window from dropping rows while the gate redeploys, and the reader
# buckets them separately as `legacy_shape_assigned` precisely because they were
# assigned by payload shape rather than at random.
_RELAY_SPECIFICITY_VALUES = (
    "treatment", "control", "ineligible",     # randomized arms (r-arms)
    "quantified", "generic",                  # pre-randomization, shape-assigned
)


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


