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

# More operator mailboxes, stored as SHA-256 of normalize_email(address) so this
# PUBLIC repo does not publish them. Two turned up in the live warm-key cohort
# as "prospects" on 2026-09-25. Check an address with
# `email_digest(addr) in _OPERATOR_EMAIL_DIGESTS`.
_OPERATOR_EMAIL_DIGESTS = frozenset({
    "0fee8b6f1fc37d825f61cb8f5e6cdd43b99b8aa63fc9d2a77bb06bdfa8354030",
    "8989522266a94e6bd50a17f7566e579914a14c1e8640f2c32f41706a27d342ef",
})

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
    if normalize_email(email) in operator_emails():
        return True
    return bool(email) and "@" in str(email) and \
        email_digest(email) in _OPERATOR_EMAIL_DIGESTS


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


# Invented addresses an agent test harness bound to keys, 2026-07-29..08-10.
# Measured 2026-09-25 (read-only query on mcp_dev_keys): every one came through
# claim_api under made-up client names ("Blue Bend Event Logistics",
# "opus-trajectory-agent", "trajectory-actor"...), one address reused across up
# to five of them. They are NOT ours and NOT prospects, and a plausible gmail
# address is most likely some real stranger's mailbox, so mailing it is spam.
# ★ Stored as SHA-256 of normalize_email(address): this repo is PUBLIC and the
#   addresses may belong to real people. Never paste them here in the clear.
# ★ The claim IP cannot identify them: MCP claims reach the backend through our
#   own Railway-hosted MCP server, so a real Claude.ai user's key carries a
#   Railway IP too.
# `DCHUB_HARNESS_PERSONA_EMAILS` (comma-separated, plain addresses) ADDS to the
# set, never replaces it. The warm-key cohort also flags the pattern
# (unverified, 2+ client names) so a new persona is caught without being named.
_HARNESS_PERSONA_DIGESTS = frozenset({
    "0c9264ddabd37f8b4fb13d0097fa3fe45b7fde2c18127b958652a13bf05130f6",
    "b3cd723841769449ae6decfc3540d442e52f4df9d67e94507e895f66a4b55415",
    "e8566a4c4ca01155d6883baf050b3dfb64655f7b980cac1de00ac2da76f1e4f6",
    "87421c87143e01b3554d70d7c0082c5097fc54fa3c7f45246c00ec3a95a5cbb3",
    "86280369278b17d1637329434c96bccd17fc350ac2f4fbfb4d92d55ec2371e16",
    "3e6545f45e4ff3dc9044b81095a191258f51f76add42e69b919d9030c54dc87a",
    "2f055d17061cc5648827c319ca57037c62612f0d2505876cac037178371ed6c8",
    "922762501ba38cbf31a220c876c9b093dc82aafebd613bfbb2f287f71c493218",
    "511cdeaf2198533ec8677c0d9538e998bc532d0251f7de2a0f6d88f72903dd4f",
    "5b2108699ec2926bad5274d308a2a4f5cc6187295d1384f066efe8bf822afc6d",
    "72ffbb56c20a72a67b1b4028153cf668a702e932adb275add7de864828f56fc2",
})


def email_digest(email) -> str:
    """SHA-256 hex of normalize_email(email) — how named lists are stored."""
    import hashlib
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


def is_harness_persona_email(email) -> bool:
    """True for an address a test harness invented. Never a prospect."""
    if not email or "@" not in str(email):
        return False
    extra = os.environ.get("DCHUB_HARNESS_PERSONA_EMAILS") or ""
    named = {normalize_email(e) for e in extra.split(",") if e.strip() and "@" in e}
    return (email_digest(email) in _HARNESS_PERSONA_DIGESTS
            or normalize_email(email) in named)


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
