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

★ 2026-09-13 — THE THIRD TOKEN FIELD. The MCP server appends the caller's
session id beside a durable-key ref (`plan|ref|sid`), so a keyed click carries
a session the operator self-traffic exclusion can test. Two-field links are
already in the wild, so they are pinned below to verify exactly as they did;
the session must never reach Stripe; and the writer must store it.
tests/test_human_acted_relayed_checkout_sql.py runs the same path against a
real Postgres.

These run against the real module functions pulled out of the source with
ast + exec against stubs — tests never import main.py (it opens DB pools and
registers ~200 blueprints), per the repo convention.
"""

import ast
import base64
import hashlib
import hmac
import os
import re
import types
from contextlib import contextmanager

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "routes", "checkout_click_tracker.py")

SECRET = "test-internal-key-not-a-real-secret"
PACK_LINK = "https://buy.stripe.com/9B69AU08y2FfbSR55UaZi0i"
SESSION = "1aa6536d-b1d4-24b4-74a8-e89ba266e781"
KEY_REF = "k-" + "b" * 64

_FUNCTIONS = ("_ref_kind", "_verify", "mint_checkout_token",
              "_session_id_column", "_log_click")
_CONSTANTS = ("_REF_OK", "_SESSION_ID_COLUMN_SQL")


def _load(schema_ready=True):
    """Exec the tracker's functions against stubs, with no Flask/DB import.

    Pulling the functions out by NAME (rather than exec'ing the module) keeps
    this honest: if _verify is renamed or deleted the test errors loudly
    instead of silently passing over an empty namespace — the vacuous-parse
    trap that has bitten this repo before.

    ★ The module constants those functions read are pulled out of the source
    the same way (2026-09-13). This used to retype _REF_OK, and a retyped
    charset keeps passing after the real one is loosened.
    """
    with open(SRC) as f:
        tree = ast.parse(f.read())
    found = {n.name: n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name in _FUNCTIONS}
    missing = set(_FUNCTIONS) - set(found)
    assert not missing, f"checkout_click_tracker lost {missing} — test is stale"
    consts = [n for n in tree.body if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id in _CONSTANTS
                      for t in n.targets)]
    assert len(consts) == len(_CONSTANTS), (
        "checkout_click_tracker lost a constant its functions read — test is stale")

    from routes._stripe_links import STRIPE_LINKS
    ns = {"os": os, "hmac": hmac, "base64": base64, "hashlib": hashlib,
          "re": re, "STRIPE_LINKS": STRIPE_LINKS,
          "_SCHEMA_READY": [schema_ready]}
    for node in consts + [found[name] for name in _FUNCTIONS]:
        exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, "exec"), ns)
    return ns


def _sign(raw, secret=SECRET):
    """Sign an arbitrary payload under the /go/c/ contract."""
    payload = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def _mint(plan, ref, secret=SECRET, sid=None):
    """Build a token exactly the way server.mjs _goUrl does: `plan|ref`, and
    `plan|ref|sid` when the caller has a session beside its key (2026-09-13)."""
    return _sign(f"{plan}|{ref}" if sid is None else f"{plan}|{ref}|{sid}", secret)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)


def test_verifies_a_token_minted_the_mcp_way():
    """The cross-repo contract: server.mjs mints, this verifies."""
    ns = _load()
    ref = "pk-" + "a" * 64
    plan, got_ref, sid, ok = ns["_verify"](_mint("metered", ref))
    assert ok is True
    assert plan == "metered"
    # If this ever drifts, checkouts still complete but stop attaching to the
    # key that earned them.
    assert got_ref == ref
    assert sid == ""


def test_rejects_tampered_and_foreign_signatures():
    ns = _load()
    good = _mint("metered", "pk-abc")
    payload, _, sig = good.rpartition(".")

    # Flipped signature byte.
    bad_sig = payload + "." + ("0" if sig[0] != "0" else "1") + sig[1:]
    assert ns["_verify"](bad_sig)[3] is False

    # Payload swapped to a pricier plan, original signature kept.
    swapped = base64.urlsafe_b64encode(b"pro|pk-abc").decode().rstrip("=") + "." + sig
    assert ns["_verify"](swapped)[3] is False

    # Correctly-formed token signed with somebody else's secret.
    assert ns["_verify"](_mint("metered", "pk-abc", "other-secret"))[3] is False

    # A session appended to a token whose signature covered no session.
    appended = base64.urlsafe_b64encode(
        ("metered|pk-abc|" + SESSION).encode()).decode().rstrip("=") + "." + sig
    assert ns["_verify"](appended)[3] is False


def test_unsigned_or_malformed_tokens_never_verify():
    ns = _load()
    for t in ["", "no-dot", ".", "x.y", "a.b.c", "!!!.###"]:
        assert ns["_verify"](t) == ("", "", "", False), f"{t!r} must not verify"


def test_no_secret_configured_means_nothing_verifies(monkeypatch):
    """Fail CLOSED on the destination: an unverifiable token must not pick a plan.

    The redirect still fails OPEN (the route sends the human to /pricing) —
    but the plan must never be trusted, or the allowlist means nothing.
    """
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    ns = _load()
    assert ns["_verify"](_mint("metered", "pk-abc"))[3] is False
    assert ns["_verify"](_mint("metered", "pk-abc", sid=SESSION))[3] is False


def test_ref_charset_is_enforced():
    """A ref is concatenated into the Location header — junk must be dropped."""
    ns = _load()
    for bad in ["a&b=c", "a b", "x\nLocation: evil", "?x", "a/b", "a#f"]:
        _, ref, _, ok = ns["_verify"](_mint("metered", bad))
        assert ok is True, "signature is valid; only the ref is rejected"
        assert ref == "", f"{bad!r} should have been dropped, got {ref!r}"
    # ...while every shape we actually mint survives.
    for good in ["pk-" + "a" * 64, "k-" + "b" * 64,
                 "1aa6536d-b1d4-24b4-74a8-e89ba266e781"]:
        assert ns["_verify"](_mint("metered", good))[1] == good


# The two-field links already relayed and sitting in agent transcripts. Each
# `want` is the tuple the two-field parser returned for that payload, with
# sid "" appended: those links must keep verifying to exactly the plan and ref
# they were minted with, or a click stops attaching to the key that earned it.
_IN_THE_WILD = [
    (("metered", "pk-" + "a" * 64), ("metered", "pk-" + "a" * 64, "", True)),
    (("starter", "k-" + "b" * 64), ("starter", "k-" + "b" * 64, "", True)),
    (("metered", SESSION), ("metered", SESSION, "", True)),
    (("metered", "a-0123abcd"), ("metered", "a-0123abcd", "", True)),
    (("metered", ""), ("metered", "", "", True)),
    (("metered", "a b"), ("metered", "", "", True)),
    ((" pro ", " pk-abc "), ("pro", "pk-abc", "", True)),
]


@pytest.mark.parametrize("minted,want", _IN_THE_WILD,
                         ids=["pack_key", "sub_key", "session", "anon",
                              "empty_ref", "junk_ref", "padded"])
def test_two_field_tokens_already_in_the_wild_verify_exactly_as_before(minted, want):
    ns = _load()
    assert ns["_verify"](_mint(*minted)) == want


def test_a_three_field_token_carries_the_session_beside_the_ref():
    ns = _load()
    assert ns["_verify"](_mint("metered", KEY_REF, sid=SESSION)) == \
        ("metered", KEY_REF, SESSION, True)
    assert ns["_verify"](_mint("metered", "", sid=SESSION)) == \
        ("metered", "", SESSION, True)


def test_the_session_field_is_held_to_the_ref_charset():
    """It is written to the database: junk is dropped, never stored — and the
    ref and destination beside it are untouched."""
    ns = _load()
    for bad in ["a&b=c", "a b", "x\nLocation: evil", "?x", "a/b", "a#f", "s" * 201]:
        plan, ref, sid, ok = ns["_verify"](_mint("metered", KEY_REF, sid=bad))
        assert (plan, ref, ok) == ("metered", KEY_REF, True), bad
        assert sid == "", f"{bad!r} should have been dropped, got {sid!r}"


@pytest.mark.parametrize("raw", ["metered",
                                 "metered|" + KEY_REF + "|" + SESSION + "|x",
                                 "a|b|c|d|e"],
                         ids=["one_field", "four_fields", "five_fields"])
def test_any_other_field_count_does_not_verify(raw):
    """Nothing we mint has that shape, so no reading of it is trusted — even
    under a good signature. The route still lands the human on /pricing."""
    ns = _load()
    assert ns["_verify"](_sign(raw)) == ("", "", "", False)


@pytest.mark.parametrize("plan,ref,sid,want", [
    ("metered", "pk-" + "c" * 64, SESSION, ("metered", "pk-" + "c" * 64, SESSION, True)),
    ("metered", KEY_REF, "", ("metered", KEY_REF, "", True)),
    ("metered", SESSION, SESSION, ("metered", SESSION, "", True)),
    ("pro", "a-00ff", "", ("pro", "a-00ff", "", True)),
    ("metered", KEY_REF, "not a session", ("metered", KEY_REF, "", True)),
], ids=["key_with_session", "key_without_session", "session_is_the_ref",
        "anon", "junk_session_dropped"])
def test_a_minted_token_round_trips(plan, ref, sid, want):
    ns = _load()
    token = ns["mint_checkout_token"](plan, ref, sid)
    assert token and ns["_verify"](token) == want


def test_the_minter_builds_the_mcp_servers_token_byte_for_byte():
    ns = _load()
    mint = ns["mint_checkout_token"]
    assert mint("metered", KEY_REF, SESSION) == _mint("metered", KEY_REF, sid=SESSION)
    assert mint("metered", KEY_REF) == _mint("metered", KEY_REF)
    # a session that IS the ref is not appended: the payload stays two-field
    assert mint("metered", SESSION, SESSION) == _mint("metered", SESSION)


def test_the_minter_refuses_a_link_that_would_not_verify_as_asked(monkeypatch):
    ns = _load()
    mint = ns["mint_checkout_token"]
    assert mint("not-a-plan", KEY_REF) is None
    assert mint("metered", "a&b=c") is None
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    assert mint("metered", KEY_REF, SESSION) is None


def test_stripe_receives_the_ref_and_never_the_session(monkeypatch):
    """The redirect is what it was for a two-field token: client_reference_id
    and nothing else. Driven through the real route."""
    flask = pytest.importorskip("flask")
    from routes import checkout_click_tracker as tracker
    from routes._stripe_links import STRIPE_LINKS
    stamped = []
    monkeypatch.setattr(tracker, "_log_click", lambda *a: stamped.append(a))
    app = flask.Flask("go-c-test")
    app.register_blueprint(tracker.checkout_click_bp)
    client = app.test_client()
    link = STRIPE_LINKS["metered"]
    want = link + ("&" if "?" in link else "?") + "client_reference_id=" + KEY_REF
    for token in (_mint("metered", KEY_REF, sid=SESSION), _mint("metered", KEY_REF)):
        r = client.get("/go/c/" + token)
        assert (r.status_code, r.headers["Location"]) == (302, want)
    assert stamped == [("metered", KEY_REF, SESSION, True),
                       ("metered", KEY_REF, "", True)]
    # A field count we never mint still lands the human somewhere payable.
    r = client.get("/go/c/" + _sign("metered|a|b|c"))
    assert (r.status_code, r.headers["Location"]) == (302, "https://dchub.cloud/pricing")
    assert stamped[-1] == ("unknown", "", "", False)


class _Cursor:
    """Records what _log_click sends; answers the catalog read with `present`."""

    def __init__(self, present):
        self.present = present
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return (self.present,)


def _stamp(ns, sid, present=True):
    """Run the REAL _log_click against a recording connection.

    → ({column: bound value} for the one INSERT it sent, the cursor).
    """
    cur = _Cursor(present)

    @contextmanager
    def _conn():
        yield types.SimpleNamespace(cursor=lambda: cur)

    swallowed = []
    ns["_conn"] = _conn
    ns["note_swallowed_write"] = lambda *a, **k: swallowed.append(a)
    ns["request"] = types.SimpleNamespace(
        headers={"User-Agent": "Mozilla/5.0"}, remote_addr="203.0.113.9")
    ns["_log_click"]("metered", KEY_REF, sid, True)
    assert not swallowed, "the write was swallowed: %r" % (cur.executed,)
    inserts = [(s, p) for s, p in cur.executed if "INSERT INTO" in s]
    assert len(inserts) == 1, cur.executed
    sql, params = inserts[0]
    cols = [c.strip() for c in
            re.search(r"\(([^)]*)\)\s*VALUES", sql).group(1).split(",")]
    assert len(cols) == len(params) == sql.count("%s"), (cols, params)
    return dict(zip(cols, params)), cur


def test_the_click_stores_the_session_its_token_carried():
    row, _ = _stamp(_load(), SESSION)
    assert row["session_id"] == SESSION
    assert (row["ref"], row["ref_kind"], row["sig_ok"]) == (KEY_REF, "sub_key", True)


def test_a_click_without_a_session_stores_null_not_an_empty_string():
    row, _ = _stamp(_load(), "")
    assert "session_id" in row and row["session_id"] is None


def test_the_insert_is_chosen_from_the_catalog_never_guessed():
    """★ An INSERT naming a column the database lacks stamps nothing. So an
    unconfirmed column is asked for (a read) before the INSERT is chosen, and a
    click on a database the ALTER has not reached is still stamped."""
    ns = _load(schema_ready=False)
    row, cur = _stamp(ns, SESSION, present=True)
    assert cur.executed[0][0] == ns["_SESSION_ID_COLUMN_SQL"]
    assert row["session_id"] == SESSION and ns["_SCHEMA_READY"] == [True]

    ns = _load(schema_ready=False)
    row, _ = _stamp(ns, SESSION, present=False)
    assert "session_id" not in row
    assert (row["ref"], row["sig_ok"]) == (KEY_REF, True)
    assert ns["_SCHEMA_READY"] == [False]


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
    plan, _, _, ok = ns["_verify"](_mint("https://evil.example/pay", "pk-a"))
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
