"""The founding cohort welcome greets the CUSTOMER, not their email address.

Before 2026-08-28 routes/founding_customers.py derived the greeting itself:

    first_name = (email.split("@")[0] or "there").split(".")[0].title()

That reads the address, not the person. It greeted founding customer #18 as
"Hi Mgelshteyn," and tj@karklins.com as "Hi Tj,". On #18 it is actively
dangerous rather than merely robotic: the Stripe cardholder name and the
account localpart are two different people, so a name guessed from the address
can open the email to the WRONG PERSON.

founder_note already had the correct derivation and had been shipping it
(#18's founder note, sent 13:38:05Z, correctly opened "Hi there,"). It is now
the ONE canonical implementation — `founder_note.first_name_for` — and the
founding sender delegates to it. Two copies is exactly how these drift apart.

Properties fenced here:
  1. first_name_for uses the Stripe name from users.name;
  2. it REJECTS a users.name that merely echoes the email localpart;
  3. it fail-softs to 'there' on empty/missing/exploding DB;
  4. the founding sender no longer derives a name from the address itself.

(1)-(3) are behavioural against a fake connection — no DATABASE_URL needed.
(4) is AST, pinned to the sender function node, not to a blob of literals.
"""

import ast
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import founder_note  # noqa: E402


# --------------------------------------------------------------- fake DB ---
class _FakeCur:
    def __init__(self, row):
        self._row = row
        self.executed = []

    def execute(self, sql, args=None):
        self.executed.append((sql, args))

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _FakeConn:
    def __init__(self, row):
        self.cur = _FakeCur(row)

    def cursor(self):
        return self.cur

    def close(self):
        pass


@pytest.fixture
def name_row(monkeypatch):
    """Install a fake users.name row; returns the holder so a test can flip it."""
    holder = {"row": None}
    monkeypatch.setattr(founder_note, "_get_conn",
                        lambda: _FakeConn(holder["row"]))
    return holder


# ------------------------------------------------- 1. uses the Stripe name ---
def test_uses_stripe_name_from_users_table(name_row):
    name_row["row"] = ("Daren Taubenfeld",)
    assert founder_note.first_name_for("mgelshteyn@sasa-holdings.com") == "Daren"


def test_strips_surrounding_whitespace(name_row):
    name_row["row"] = ("   Daren   Taubenfeld  ",)
    assert founder_note.first_name_for("mgelshteyn@sasa-holdings.com") == "Daren"


# ------------------------------------------- 2. rejects a localpart echo ---
@pytest.mark.parametrize("stored", ["mgelshteyn", "MGELSHTEYN", "Mgelshteyn"])
def test_rejects_name_that_merely_echoes_the_localpart(name_row, stored):
    """A users.name equal to the address localpart carries no more information
    than the address did — that is the exact string the old code produced."""
    name_row["row"] = (stored,)
    assert founder_note.first_name_for("mgelshteyn@sasa-holdings.com") == "there"


# ------------------------------------------------------ 3. fail-soft ----
@pytest.mark.parametrize("row", [None, ("",), ("   ",)])
def test_falls_back_to_there_when_no_usable_name(name_row, row):
    name_row["row"] = row
    assert founder_note.first_name_for("mgelshteyn@sasa-holdings.com") == "there"


def test_falls_back_to_there_when_db_raises(monkeypatch):
    def _boom():
        raise RuntimeError("neon unreachable")
    monkeypatch.setattr(founder_note, "_get_conn", _boom)
    assert founder_note.first_name_for("mgelshteyn@sasa-holdings.com") == "there"


def test_never_returns_the_raw_localpart_for_the_real_founding_cohort(name_row):
    """The regression, stated as the customers it actually hit."""
    name_row["row"] = None
    for addr, banned in (("mgelshteyn@sasa-holdings.com", "Mgelshteyn"),
                         ("tj@karklins.com", "Tj"),
                         ("rob@hedmarkholdings.com", "Rob"),
                         ("jim.hsieh@whitecap.com", "Jim")):
        assert founder_note.first_name_for(addr) != banned


# --------------------------------------- 4. the sender does not re-derive ---
SENDER = "routes/founding_customers.py"


def _func(rel, name):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    fns = [n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and n.name == name]
    assert fns, f"{name} not found in {rel} — test target moved, not passing"
    return fns[0]


def _greeting_assignment(fn):
    """The `first_name = ...` assignment node inside fn, and nothing else.

    Pinned to this one node on purpose: asserting over every literal in the
    function would let the naive derivation survive anywhere else in the body.
    """
    hits = [n for n in ast.walk(fn)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "first_name"
                    for t in n.targets)]
    assert len(hits) == 1, (
        "expected exactly one `first_name = ...` in send_founding_welcome_email, "
        f"found {len(hits)} — the greeting moved, this guard is not passing")
    return hits[0]


def test_sender_does_not_derive_the_greeting_from_the_address():
    node = _greeting_assignment(_func(SENDER, "send_founding_welcome_email"))
    src = ast.dump(node)
    assert "'@'" not in src and '"@"' not in src, (
        "send_founding_welcome_email is splitting the email address to build "
        "the greeting again — that is the bug this file exists to prevent")
    assert "title" not in src, (
        "greeting is being .title()-cased off the address again")


def test_sender_delegates_to_the_canonical_helper():
    node = _greeting_assignment(_func(SENDER, "send_founding_welcome_email"))
    called = {n.func.id for n in ast.walk(node)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_greeting_first_name" in called, (
        "the greeting must come from _greeting_first_name, which delegates to "
        f"founder_note.first_name_for; got calls: {sorted(called)}")


def test_the_canonical_helper_is_the_only_implementation():
    """_greeting_first_name must DELEGATE, not carry its own copy."""
    fn = _func(SENDER, "_greeting_first_name")
    imported = {a.name for n in ast.walk(fn)
                if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "first_name_for" in imported, (
        "_greeting_first_name no longer imports founder_note.first_name_for — "
        "a second copy of the derivation is how these two drift apart")
