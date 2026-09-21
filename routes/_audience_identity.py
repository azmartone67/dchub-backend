"""Whose address is this — shared by every contact export (2026-09-20).

ONE definition, imported by both `audience_export` and `warm_key_cohort`, not
copied into each. A copy is the failure mode: the last copy of a shared rule
survives on the busiest hop and the two lists drift apart silently.

★ WHY THIS EXISTS. `warm_key_cohort._INTERNAL_MARKERS` is a SUBSTRING match
over the address — `dchub.cloud`, `test@`, `+qa`, `noreply`. The operator's own
address is `azmartone@gmail.com`: a consumer mailbox, no dchub marker anywhere
in it. It carried no signal, so it entered the warm-key cohort as a PROSPECT
and would have entered any other contact export the same way. A marker list
cannot catch an address whose only distinguishing fact is *whose* it is — that
needs the address itself, named.

`normalize_email` exists because `azmartone+news@gmail.com` and
`azm.artone@gmail.com` are the same mailbox, and an exclusion that matches only
the literal string is one plus-tag away from being useless.
"""
from __future__ import annotations

import os

# Ours, by name. Not a marker, not a domain — the specific mailboxes that must
# never be counted as a lead in any export this repo produces.
_DEFAULT_OPERATOR_EMAILS = frozenset({"azmartone@gmail.com"})

# Providers that ignore dots in the local part. Plus-tags are stripped for
# every provider (near-universal); dots only for these, where it is the
# documented behaviour rather than a guess.
_DOT_INSENSITIVE_DOMAINS = frozenset({"gmail.com", "googlemail.com"})

# Domain aliases that deliver to the same mailbox. `googlemail.com` is not a
# separate provider, and leaving it unmapped is how `azm.artone@googlemail.com`
# reads as a stranger while `azmartone@gmail.com` is excluded.
_DOMAIN_ALIASES = {"googlemail.com": "gmail.com"}


def normalize_email(email) -> str:
    """Address reduced to the mailbox it actually reaches.

    Lowercased, plus-tag dropped, dots removed in the local part for the
    providers that ignore them, and alias domains mapped to the mailbox they
    deliver to. Anything without an `@` comes back trimmed and lowercased so a
    caller never has to special-case it.
    """
    e = str(email or "").strip().lower()
    if "@" not in e:
        return e
    local, _, domain = e.rpartition("@")
    local = local.split("+", 1)[0]
    if domain in _DOT_INSENSITIVE_DOMAINS:
        local = local.replace(".", "")
    return f"{local}@{_DOMAIN_ALIASES.get(domain, domain)}"


def operator_emails() -> frozenset:
    """The operator mailboxes, normalized.

    `DCHUB_OPERATOR_EMAILS` (comma-separated) ADDS to the built-in set — it
    never replaces it. Additive on purpose: an env var that is empty, typoed or
    simply unset in one service must not be able to REMOVE the operator from
    the exclusion. The only direction this var can move the list is wider.
    """
    extra = os.environ.get("DCHUB_OPERATOR_EMAILS") or ""
    return frozenset(
        {normalize_email(e) for e in _DEFAULT_OPERATOR_EMAILS}
        | {normalize_email(e) for e in extra.split(",")
           if e.strip() and "@" in e}
    )


def is_operator_email(email) -> bool:
    """True when this address reaches an operator mailbox. Never a prospect."""
    return normalize_email(email) in operator_emails()
