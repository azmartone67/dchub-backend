"""The browser-attestation cookie's NAME must not collide with a Cloudflare
cache-rule credential family.

THE DEFECT THIS PINS
────────────────────
`routes/session_cookie.py` mints an anti-scrape attestation — "this caller is
a real browser, not a curl loop" — and hands one to EVERY visitor, including
anonymous ones. That is deliberate and documented: the value is
`<issued_ts>|<ip_prefix>|<hmac_sig>`, it carries no user id and no plan, and
map_tier_gating.py's "★ WHY THIS IS SAFE" note states outright that it may
never stand in for paid status.

It was named `dchub_session`.

Cloudflare cache rule 24 (ruleset fecada93, rule id b3ce82fb) bypasses the
edge cache for /api/* whenever the raw Cookie header contains any of six
credential SUBSTRINGS — session, token, key, admin, sid, refresh. So a name
chosen to describe a non-credential matched a rule written to catch
credentials, and every browser on the site stopped being cacheable. Measured
on /api/v1/stats, 2026-09-07, before the rename:

    anonymous, cookieless        → cf-cache-status: HIT      (age 1705)
    same URL + dchub_session     → cf-cache-status: DYNAMIC

Rule 24 is not the bug and must not be weakened — a cookie that says
"session" genuinely should not share a URL-only cache key with anonymous
callers. The bug was shipping a credential-shaped NAME on a non-credential.

WHY THIS READS THE CANON INSTEAD OF HARDCODING THE SIX
──────────────────────────────────────────────────────
A guard that hardcodes the families it checks against goes stale the moment
someone adds a seventh in the Cloudflare dashboard, and then reports green
about a rule that no longer exists in that shape. The families are therefore
EXTRACTED from scripts/cf_cache_ruleset_canon.json — the same pinned canon
check_cf_cache_ruleset.py diffs against live — so adding a family upstream
either fails this test or fails that one, never neither.

★ CANNOT-CHECK IS NOT PASS. A missing canon, a missing rule, or an expression
we cannot parse fails. A zero-length family list fails on the floor below;
a scan that can find nothing must never be able to report green.

★ NON-VACUOUS. test_guard_can_actually_fail re-runs the identical assertion
against the pre-rename name and REQUIRES it to fail, so the matcher is proven
able to fire rather than assumed to be.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CANON = REPO / "scripts" / "cf_cache_ruleset_canon.json"
SOURCE = REPO / "routes" / "session_cookie.py"

# The credentialed-bypass rule. Pinned by id, not by index or description:
# rule ORDER is last-match-wins and has been reordered before, and the
# description is prose that gets rewritten on every re-measure.
RULE_ID_PREFIX = "b3ce82fb"

# Floor. Rule 24 named six cookie families when this guard was written; it may
# grow, but a canon that yields FEWER than this has lost coverage and the
# guard must not quietly pass on the remainder.
MIN_COOKIE_FAMILIES = 6

# The alphabet the cookie VALUE is drawn from: "<ts>|<ip_prefix>|<hmac16>".
# ts is decimal; ip_prefix is IPv4 digits+dots or the first four IPv6 hextets
# (hex + colons); the signature is lowercase hex. Nothing else can appear.
VALUE_ALPHABET = set("0123456789abcdef.:|")


def _canon_rules() -> list[dict]:
    if not CANON.exists():
        pytest.fail(f"canon missing at {CANON} — cannot check is not pass")
    doc = json.loads(CANON.read_text())
    rules = doc.get("rules") or []
    if not rules:
        pytest.fail("canon carries zero rules — floor violated")
    return rules


def _cookie_families() -> list[str]:
    """Every literal X in `http.cookie contains "X"` within rule b3ce82fb."""
    rules = _canon_rules()
    match = [r for r in rules if str(r.get("id", "")).startswith(RULE_ID_PREFIX)]
    if len(match) != 1:
        pytest.fail(
            f"expected exactly 1 rule with id {RULE_ID_PREFIX}*, found "
            f"{len(match)} — the credentialed-bypass rule moved or was removed"
        )
    expr = match[0].get("expression") or ""
    if not expr:
        pytest.fail(f"rule {RULE_ID_PREFIX} has an empty expression")
    fams = re.findall(r'http\.cookie\s+contains\s+"([^"]+)"', expr)
    if len(fams) < MIN_COOKIE_FAMILIES:
        pytest.fail(
            f"extracted only {len(fams)} cookie families {fams} from rule "
            f"{RULE_ID_PREFIX}; floor is {MIN_COOKIE_FAMILIES}. Either the "
            f"rule changed shape or the parser stopped matching — both mean "
            f"this guard is no longer checking what it claims to."
        )
    return [f.lower() for f in fams]


def _module_constant(name: str) -> str:
    """Read a module-level string constant by AST.

    Deliberately NOT a substring grep and NOT an import: a grep is satisfied
    by the name appearing in a comment (this file's own docstring names the
    old cookie many times), and importing routes.session_cookie drags in the
    routes package. The AST binds to the assigned VALUE.
    """
    if not SOURCE.exists():
        pytest.fail(f"{SOURCE} missing — cannot check is not pass")
    tree = ast.parse(SOURCE.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    if not isinstance(node.value, ast.Constant) or not isinstance(
                        node.value.value, str
                    ):
                        pytest.fail(f"{name} is not a string literal")
                    return node.value.value
    pytest.fail(f"{name} not found as a module-level assignment in {SOURCE}")


def _collisions(cookie_name: str, families: list[str]) -> list[str]:
    low = cookie_name.lower()
    return [f for f in families if f in low]


# ── the guard ────────────────────────────────────────────────────────────

def test_issued_cookie_name_matches_no_credential_family():
    families = _cookie_families()
    name = _module_constant("COOKIE_NAME")
    hits = _collisions(name, families)
    assert not hits, (
        f"COOKIE_NAME={name!r} contains cache-rule credential "
        f"{'family' if len(hits) == 1 else 'families'} {hits}. Cloudflare rule "
        f"{RULE_ID_PREFIX} bypasses the edge cache for /api/* on any Cookie "
        f"header containing these, and this cookie is issued to every "
        f"anonymous visitor — so this name un-caches all browser traffic. "
        f"Rename the cookie; do NOT weaken the rule."
    )


def test_cookie_value_alphabet_cannot_contain_a_family():
    """The NAME is only half the header. Rule 24 matches the raw Cookie
    string, so a value containing "key" would bypass just as hard."""
    families = _cookie_families()
    unprovable = [f for f in families if not (set(f) - VALUE_ALPHABET)]
    assert not unprovable, (
        f"families {unprovable} are spellable entirely from the cookie-value "
        f"alphabet {sorted(VALUE_ALPHABET)}, so a value could collide and this "
        f"guard can no longer prove it cannot. Constrain the value encoding."
    )


def test_set_cookie_is_bound_to_the_constant():
    """set_cookie_on_response must pass COOKIE_NAME, not a re-typed literal —
    otherwise the constant can be renamed while the wire name stays put."""
    tree = ast.parse(SOURCE.read_text())
    fn = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == "set_cookie_on_response"),
        None,
    )
    if fn is None:
        pytest.fail("set_cookie_on_response not found")
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "set_cookie"
    ]
    if not calls:
        pytest.fail("no .set_cookie(...) call inside set_cookie_on_response")
    for call in calls:
        assert call.args, "set_cookie called with no positional cookie name"
        first = call.args[0]
        assert isinstance(first, ast.Name) and first.id == "COOKIE_NAME", (
            "set_cookie's first argument must be the COOKIE_NAME constant, "
            f"got {ast.dump(first)[:80]}"
        )


def test_legacy_name_is_accepted_but_never_issued():
    """The pre-rename cookie must still VALIDATE (a browser holding one when
    this deploys keeps its attestation) and must never be ISSUED again (or it
    would keep tripping rule 24 forever)."""
    legacy = _module_constant("LEGACY_COOKIE_NAME")
    tree = ast.parse(SOURCE.read_text())

    validate = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == "validate_cookie"), None)
    if validate is None:
        pytest.fail("validate_cookie not found")
    reads = {
        n.args[0].id
        for n in ast.walk(validate)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get"
        and n.args
        and isinstance(n.args[0], ast.Name)
    }
    assert {"COOKIE_NAME", "LEGACY_COOKIE_NAME"} <= reads, (
        "validate_cookie must read BOTH COOKIE_NAME and LEGACY_COOKIE_NAME "
        f"during the transition window; it reads {sorted(reads)}"
    )

    issuer = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == "set_cookie_on_response"), None)
    issued = [
        n.args[0].id
        for n in ast.walk(issuer)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "set_cookie"
        and n.args
        and isinstance(n.args[0], ast.Name)
    ]
    assert "LEGACY_COOKIE_NAME" not in issued, (
        f"{legacy!r} is being issued again — it contains a rule-24 family and "
        f"would re-break the edge cache for every browser."
    )


# ── proof the guard can fail ─────────────────────────────────────────────

def test_guard_can_actually_fail():
    """Mutation check, permanent rather than a one-time manual step.

    Runs the exact assertion from test_issued_cookie_name_matches_no_credential_family
    against the PRE-RENAME name and requires a collision. If this ever passes,
    the family extraction has silently stopped returning anything meaningful
    and the real test above is vacuous.
    """
    families = _cookie_families()
    legacy = _module_constant("LEGACY_COOKIE_NAME")
    hits = _collisions(legacy, families)
    assert hits, (
        f"the pre-rename name {legacy!r} no longer collides with any of "
        f"{families} — the matcher cannot fire, so the guard above proves "
        f"nothing. Fix the extraction before trusting a green run."
    )
    assert "session" in hits, (
        f"expected {legacy!r} to collide on the 'session' family specifically; "
        f"got {hits}"
    )
