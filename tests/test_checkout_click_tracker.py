"""
Guards for /go/c/<token> — the relayed-checkout click proxy.

WHY THIS ENDPOINT EXISTS
────────────────────────
The admin waterfall reads "26 agents saw an offer → 0 paid", and the hop
between those two numbers was measured NOWHERE: unlock_more_data handed the
human a direct buy.stripe.com URL, so the click could not be observed. (The
existing funnel.stripe_clicked_30d looks like it covers this and does not —
it reads mcp_pair_codes, the /connect flow, which minted 1 code in 30d.)

So the endpoint's whole value is that it sits between a human and a payment.
That makes its failure modes asymmetric, and the tests are weighted to match:

  * A missed stamp costs one row of telemetry.
  * A broken redirect costs a SALE.
  * A dropped client_reference_id costs the ATTRIBUTION on a sale that still
    completes — the silent one, invisible for weeks.

Hence: every fail-open path is asserted to still produce a payable URL, and
the ref is asserted to survive intact.

These run against the real module functions pulled out of the source with
ast + exec against stubs — tests never import main.py (it opens DB pools and
registers ~200 blueprints), per the repo convention.
"""

import ast
import base64
import hashlib
import hmac
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "routes", "checkout_click_tracker.py")

SECRET = "test-internal-key-not-a-real-secret"
PACK_LINK = "https://buy.stripe.com/9B69AU08y2FfbSR55UaZi0i"


def _load():
    """Exec _verify + _ref_kind against stubs, with no Flask/DB import.

    Pulling the functions out by NAME (rather than exec'ing the module) keeps
    this honest: if _verify is renamed or deleted the test errors loudly
    instead of silently passing over an empty namespace — the vacuous-parse
    trap that has bitten this repo before.
    """
    with open(SRC) as f:
        tree = ast.parse(f.read())
    wanted = {"_verify", "_ref_kind"}
    found = {n.name: n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in wanted}
    missing = wanted - set(found)
    assert not missing, f"checkout_click_tracker lost {missing} — test is stale"

    ns = {"os": os, "hmac": hmac, "base64": base64, "hashlib": hashlib}
    # _REF_OK is a module-level compiled regex the extracted function closes over.
    import re as _re
    ns["_REF_OK"] = _re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
    for name in ("_ref_kind", "_verify"):
        mod = ast.Module(body=[found[name]], type_ignores=[])
        exec(compile(mod, SRC, "exec"), ns)
    return ns


def _mint(plan, ref, secret=SECRET):
    """Build a token exactly the way server.mjs _goUrl does."""
    payload = base64.urlsafe_b64encode(f"{plan}|{ref}".encode()).decode().rstrip("=")
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)


def test_verifies_a_token_minted_the_mcp_way():
    """The cross-repo contract: server.mjs mints, this verifies."""
    ns = _load()
    ref = "pk-" + "a" * 64
    plan, got_ref, ok = ns["_verify"](_mint("metered", ref))
    assert ok is True
    assert plan == "metered"
    # If this ever drifts, checkouts still complete but stop attaching to the
    # key that earned them.
    assert got_ref == ref


def test_rejects_tampered_and_foreign_signatures():
    ns = _load()
    good = _mint("metered", "pk-abc")
    payload, _, sig = good.rpartition(".")

    # Flipped signature byte.
    bad_sig = payload + "." + ("0" if sig[0] != "0" else "1") + sig[1:]
    assert ns["_verify"](bad_sig)[2] is False

    # Payload swapped to a pricier plan, original signature kept.
    swapped = base64.urlsafe_b64encode(b"pro|pk-abc").decode().rstrip("=") + "." + sig
    assert ns["_verify"](swapped)[2] is False

    # Correctly-formed token signed with somebody else's secret.
    assert ns["_verify"](_mint("metered", "pk-abc", "other-secret"))[2] is False


def test_unsigned_or_malformed_tokens_never_verify():
    ns = _load()
    for t in ["", "no-dot", ".", "x.y", "a.b.c", "!!!.###"]:
        assert ns["_verify"](t)[2] is False, f"{t!r} must not verify"


def test_no_secret_configured_means_nothing_verifies(monkeypatch):
    """Fail CLOSED on the destination: an unverifiable token must not pick a plan.

    The redirect still fails OPEN (the route sends the human to /pricing) —
    but the plan must never be trusted, or the allowlist means nothing.
    """
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    ns = _load()
    assert ns["_verify"](_mint("metered", "pk-abc"))[2] is False


def test_ref_charset_is_enforced():
    """A ref is concatenated into the Location header — junk must be dropped."""
    ns = _load()
    for bad in ["a&b=c", "a b", "x\nLocation: evil", "?x", "a/b", "a#f"]:
        _, ref, ok = ns["_verify"](_mint("metered", bad))
        assert ok is True, "signature is valid; only the ref is rejected"
        assert ref == "", f"{bad!r} should have been dropped, got {ref!r}"
    # ...while every shape we actually mint survives.
    for good in ["pk-" + "a" * 64, "k-" + "b" * 64,
                 "1aa6536d-b1d4-24b4-74a8-e89ba266e781"]:
        assert ns["_verify"](_mint("metered", good))[1] == good


def test_ref_kind_labels_each_identity_space():
    ns = _load()
    assert ns["_ref_kind"]("pk-" + "a" * 64) == "pack_key"
    assert ns["_ref_kind"]("k-" + "a" * 64) == "sub_key"
    assert ns["_ref_kind"]("some-session-uuid") == "session"
    assert ns["_ref_kind"]("") == "none"


def test_destination_comes_from_the_allowlist_not_the_token():
    """The property that makes an open redirect impossible.

    The token carries a plan NAME; the URL is looked up in _stripe_links. If a
    future edit ever puts a URL in the payload, this fails.
    """
    from routes._stripe_links import STRIPE_LINKS
    ns = _load()
    plan, _, ok = ns["_verify"](_mint("https://evil.example/pay", "pk-a"))
    assert ok is True                       # signature can be valid...
    assert plan not in STRIPE_LINKS         # ...and still resolve to nothing
    # And the plans we DO mint must all be resolvable, or the link 302s to
    # /pricing instead of checkout — a measurement change costing a sale.
    for p in ("metered", "starter", "developer", "pro"):
        assert STRIPE_LINKS.get(p, "").startswith("https://buy.stripe.com/")


def test_route_is_registered_in_main():
    """A blueprint that main.py never registers is unreachable code.

    Cheap to assert, and this repo has shipped that exact shape before
    (/claim/, /r/, /go/, /relay/ all reached production unreachable).
    """
    with open(os.path.join(REPO_ROOT, "main.py")) as f:
        main_src = f.read()
    assert "checkout_click_bp" in main_src
    assert "from routes.checkout_click_tracker import checkout_click_bp" in main_src


def test_pack_link_matches_the_mcp_side_plan_map():
    """Cross-repo drift guard: server.mjs maps this link id → 'metered'.

    If _stripe_links moves 'metered' to a new Stripe link and the MCP map is
    not updated, _goUrl stops recognising the URL and silently emits the DIRECT
    link — back to unmeasured, with nothing red.
    """
    from routes._stripe_links import STRIPE_LINKS
    assert STRIPE_LINKS["metered"] == PACK_LINK, (
        "metered link changed — update _GO_PLAN_BY_LINK in dchub-mcp-server "
        "server.mjs in the SAME wave or click tracking goes dark"
    )
