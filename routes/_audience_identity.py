"""Whose address is this — shared by every contact export (2026-09-20).

ONE definition, imported by both `audience_export` and `warm_key_cohort`, not
copied into each. A copy is the failure mode: the last copy of a shared rule
survives on the busiest hop and the two lists drift apart silently.

★ WHY THIS EXISTS. `INTERNAL_MARKERS` (below) is a SUBSTRING match
over the address — `dchub.cloud`, `test@`, `+qa`, `noreply`. The operator's own
address is `azmartone@gmail.com`: a consumer mailbox, no dchub marker anywhere
in it. It carried no signal, so it entered the warm-key cohort as a PROSPECT
and would have entered any other contact export the same way. A marker list
cannot catch an address whose only distinguishing fact is *whose* it is — that
needs the address itself, named.

`normalize_email` exists because `azmartone+news@gmail.com` and
`azm.artone@gmail.com` are the same mailbox, and an exclusion that matches only
the literal string is one plus-tag away from being useless.

★ 2026-09-21 — THE MARKER HALF MOVED HERE TOO. It lived in warm_key_cohort, and
audience_export imported only `is_operator_email`, so the free-users export
applied the named rule and none of the markers. Measured live after #5002:
21 of its 149 rows were ours — role mailboxes on dchub.cloud, a QA probe,
test@ and example.com fixtures — while `x-operator-excluded` read 0, because
the operator's literal address is not a free user. `is_internal_email` is both
halves, and `warm_key_cohort._is_internal` IS that object, re-exported, so every
export that asks "is this ours?" gets one answer.
"""
from __future__ import annotations

import os

# Ours, by name. Not a marker, not a domain — the specific mailboxes that must
# never be counted as a lead in any export this repo produces.
# `azmartonetest1@gmail.com` is the operator's test account (confirmed by the
# operator 2026-09-21, after it turned up in the live free-users export). It is
# a DIFFERENT mailbox, not a plus-tag or dot variant, and no marker sees it:
# `test@` does not match `test1@`, and a bare `test` marker would swallow
# strangers. Naming it is the only rule that catches it and nothing else.
_DEFAULT_OPERATOR_EMAILS = frozenset({"azmartone@gmail.com",
                                      "azmartonetest1@gmail.com"})

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


def normalized_email_sql(expr: str) -> str:
    """`normalize_email`, as a Postgres expression over `expr`.

    Built from the SAME constants, so a domain added above reaches both. No
    literal `%` (callers %-format their SQL). Parity with the Python function
    is executed in tests/test_paid_attributed_relayed_checkout_sql.py.
    """
    e = r"lower(regexp_replace(coalesce(%s,''), '^\s+|\s+$', '', 'g'))" % expr
    local = "split_part(regexp_replace(%s, '@[^@]*$', ''), '+', 1)" % e
    dom = "regexp_replace(%s, '^.*@', '')" % e
    dots = ",".join("'%s'" % d for d in sorted(_DOT_INSENSITIVE_DOMAINS))
    alias = " ".join("when %s = '%s' then '%s'" % (dom, a, b)
                     for a, b in sorted(_DOMAIN_ALIASES.items()))
    return ("(case when position('@' in %s) = 0 then %s else"
            " (case when %s in (%s) then replace(%s, '.', '') else %s end)"
            " || '@' || (case %s else %s end) end)"
            % (e, e, dom, dots, local, local, alias, dom))


def operator_emails_sql_list() -> str:
    """operator_emails() as a SQL IN-list body. Alnum/@/./+/-/_ only, so an
    env-supplied address cannot close the quote."""
    import re as _re
    safe = sorted(e for e in operator_emails()
                  if _re.fullmatch(r"[a-z0-9@._+-]+", e))
    return ",".join("'%s'" % e for e in safe)


def is_operator_email(email) -> bool:
    """True when this address reaches an operator mailbox. Never a prospect."""
    return normalize_email(email) in operator_emails()


# Ours by SHAPE: our own domains, probe/QA plus-tags, hand-typed fixtures.
# `dchubmail.com` and the `+qa`/`+test` plus-tags were found in the live
# warm-key cohort on its first read — ours, and counted as leads until named.
# ★ `test@` is already a SUFFIX match on the local part: it matches `contest@`
# and `latest@` as well as `test@`. Never widen it to bare `test`, which would
# add `testing@`, `attestation@` and every other stranger containing the word.
# A mailbox that is ours but carries no marker belongs in the named set above.
INTERNAL_MARKERS = ("dchub.cloud", "dchub.io", "dchubmail.com", "@example.",
                    "example.com", "test@", "probe@", "+probe@", "+qa",
                    "+test", "+dev", "noreply", "no-reply")


def is_internal_email(email) -> bool:
    """True when this address is ours — a named operator mailbox, or one
    carrying an internal marker. Never a prospect.

    Moved verbatim from `warm_key_cohort._is_internal`, which is now this same
    object re-exported; audience_keys_export borrows it from there by identity.
    """
    e = (email or "").strip().lower()
    if is_operator_email(e):
        return True
    return any(m in e for m in INTERNAL_MARKERS)
