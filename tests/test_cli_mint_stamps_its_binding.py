"""A hand-minted key asserts its own email binding (2026-09-12).

#4428 made paid MCP tier follow a CONFIRMED binding — metadata.email_verified_for
naming the address being granted on. `gen_dev_key.py mint` wrote no provenance at
all, so an operator minting a key at a terminal with the production DSN produced
a row that no rule could ever recognise:

  * it sat in reconcile-keys' legacy_unverified_paid_keys audit permanently —
    8 of the 52 rows found on 2026-09-11 came from this path, and they were the
    ONLY reason that list still contained anything unexplained;
  * the checkout webhook's tier write is not upgrade-only, so it is what carries
    a pro→enterprise PLAN CHANGE. It could never reach a key minted here.

A hand-mint is the same evidence as `entitlement_reconcile_manual`, which is on
the backfill's proven list. The gap was provenance, not trust.

These drive the REAL cmd_mint — the module is imported and `_connect` replaced,
so the assertions read the parameters the shipped function actually sends.

ONE IMPLEMENTATION. `dchub-mcp-v2.1/gen_dev_key.py` is a symlink to this file
(mode 120000), after a vendored copy missed the 2026-08-16 revoke fix for six
weeks. test_revoke_tool_has_one_implementation.py pins that; this file asserts
the mint behaviour through whichever path is invoked, so a copy that came back
would have to carry it too.
"""
import importlib
import json
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class _Cursor:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.sink.append((" ".join(sql.split()), params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


@pytest.fixture
def mint(monkeypatch):
    # The module exits at import without a DSN. Nothing connects: _connect is
    # replaced before any call.
    monkeypatch.setenv("NEON_DATABASE_URL", "postgresql://stub:stub@127.0.0.1:1/stub")
    mod = importlib.import_module("gen_dev_key")
    assert os.path.realpath(mod.__file__) == os.path.realpath(
        os.path.join(ROOT, "gen_dev_key.py")), "not the shipped file"

    sink = []

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cursor(sink)

    monkeypatch.setattr(mod, "_connect", lambda: _Conn())

    def run(email="Alice@Example.COM", tier="paid", note=None):
        mod.cmd_mint(types.SimpleNamespace(email=email, tier=tier, note=note))
        inserts = [(s, p) for s, p in sink if s.startswith("INSERT INTO mcp_dev_keys")]
        assert len(inserts) == 1, f"expected one INSERT, got {len(inserts)}"
        sql, params = inserts[0]
        return types.SimpleNamespace(sql=sql, params=params,
                                     email=params[2], tier=params[3],
                                     metadata=json.loads(params[4]))
    return types.SimpleNamespace(mod=mod, run=run, sink=sink)


def test_a_hand_mint_names_the_address_it_is_bound_to(mint):
    out = mint.run(email="Alice@Example.COM", tier="paid")
    assert out.metadata["email_verified_for"] == "alice@example.com", (
        "a minted key carries no proof of its own binding, so no email match "
        "can ever lift its tier — including a plan change on a real payment")
    assert out.metadata["email_verified_via"] == "cli_mint"
    assert out.metadata["source"] == "cli_mint"


def test_the_marker_is_lowercased_so_every_consumer_matches_it(mint):
    """Each grant compares LOWER(marker) to LOWER(address). A marker stored in
    the typed case still matches those — but the reconcile AUDIT compares the
    marker to the row's own email, and a key whose column keeps mixed case
    would read as unproven forever. Store it lowered once, here."""
    out = mint.run(email="  MiXeD@Example.Com  ")
    assert out.metadata["email_verified_for"] == "mixed@example.com"
    assert out.metadata["email_verified_for"] == out.metadata["email_verified_for"].lower()
    assert out.metadata["email_verified_for"].strip() == out.metadata["email_verified_for"]


@pytest.mark.parametrize("tier", ["free", "paid", "enterprise"])
def test_every_tier_is_stamped_not_only_the_paid_ones(mint, tier):
    """On a free mint the marker means a later genuine payment upgrades THIS
    key rather than issuing the buyer a second one. It grants nothing an
    operator could not already do — anyone who can run this can pass
    --tier paid."""
    out = mint.run(tier=tier)
    assert out.tier == tier
    assert out.metadata["email_verified_for"] == "alice@example.com"


def test_a_note_still_survives_alongside_the_stamp(mint):
    out = mint.run(note="partner eval key")
    assert out.metadata["note"] == "partner eval key"
    assert out.metadata["email_verified_for"] == "alice@example.com"


def test_no_marker_is_written_for_an_empty_address(mint):
    """--email is required, so this is defence in depth: an empty marker beside
    an empty email column reads as 'proven' to the audit's equality test, which
    would hide the row rather than describe it."""
    out = mint.run(email="   ")
    assert "email_verified_for" not in out.metadata
    assert out.metadata["source"] == "cli_mint"


def test_the_stamp_reaches_the_row_the_insert_writes(mint):
    """The metadata must ride the INSERT itself — a stamp computed and then not
    passed is the shape of a fix that changes nothing."""
    out = mint.run()
    assert "INSERT INTO mcp_dev_keys" in out.sql
    assert "metadata" in out.sql and out.params[4] == json.dumps(out.metadata)
    assert out.params[0].startswith("dch_live_")


def test_the_bundle_path_is_the_same_implementation():
    """A vendored copy of this CLI missed the revoke fix for six weeks. If the
    symlink is ever materialised back into a regular file, the mint stamp is
    one of the behaviours that would silently drift."""
    bundle = os.path.join(ROOT, "dchub-mcp-v2.1", "gen_dev_key.py")
    if not os.path.exists(bundle):
        pytest.skip("bundle copy not present in this checkout")
    assert os.path.realpath(bundle) == os.path.realpath(
        os.path.join(ROOT, "gen_dev_key.py")), (
        "dchub-mcp-v2.1/gen_dev_key.py is no longer the root implementation — "
        "the mint stamp now exists in only one of two reachable paths")
