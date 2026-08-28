"""Every customer-facing sender uses first_name_for, and it keeps real names.

Follow-up to #3266, which made `founder_note.first_name_for` canonical and fixed
`routes/founding_customers.py`. Two senders were outside that PR's scope and
still greeted people with the email address:

    usage_limit_emails.py         name = email.split('@')[0]
                                  -> "Heads up, mgelshteyn — you're close..."
    routes/upgrade_pool_outreach  name_guess = ...split("@")[0]...title()

And #3266's echo-rejection was unconditional, which discarded REAL names.
`tests/test_founding_greeting_name.py` owns the founding sender; this file owns
the narrowing and the two remaining callers. Behavioural where behaviour is the
claim, AST only for "does this sender still derive from the address".
"""

import ast
import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import founder_note  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


class _Cur:
    def __init__(self, row):
        self._row = row

    def execute(self, sql, args=None):
        pass

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _Conn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _Cur(self._row)

    def close(self):
        pass


def _stored(monkeypatch, name):
    row = None if name is None else (name,)
    monkeypatch.setattr(founder_note, "_get_conn", lambda: _Conn(row))


# ── the narrowing: #3266 threw away real names ────────────────────────────
def test_a_real_name_matching_the_localpart_is_kept(monkeypatch):
    """THE REGRESSION. alexander@ryex.net stores "Alexander Ting" and was
    greeted "Hi there" on main — verified against the live founding cohort,
    1 of 18. rob@, jim@, eren@ are the same ordinary address shape. A surname
    proves the value came from a real name field."""
    _stored(monkeypatch, "Alexander Ting")
    assert founder_note.first_name_for("alexander@ryex.net") == "Alexander"


def test_single_token_echo_of_the_localpart_is_still_rejected(monkeypatch):
    """The narrowing must not reopen what the echo check exists for: the
    cold-buyer path stores `customer_email.split('@')[0]` AS users.name."""
    _stored(monkeypatch, "alexander")
    assert founder_note.first_name_for("alexander@ryex.net") == "there"


def test_echo_is_caught_across_separator_and_digit_differences(monkeypatch):
    _stored(monkeypatch, "mgelshteyn")
    assert founder_note.first_name_for("m.gelshteyn@sasa.com") == "there"


def test_a_lone_initial_is_not_a_greeting(monkeypatch):
    _stored(monkeypatch, "M Gelshteyn")
    assert founder_note.first_name_for("mgelshteyn@sasa.com") == "there"


def test_fallback_none_lets_a_caller_drop_the_name(monkeypatch):
    _stored(monkeypatch, None)
    assert founder_note.first_name_for("x@y.com", fallback=None) is None
    assert founder_note.first_name_for("x@y.com") == "there"


# ── the two remaining senders ─────────────────────────────────────────────
def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


CALLERS = ["usage_limit_emails.py", "routes/upgrade_pool_outreach.py"]


def test_neither_sender_derives_a_greeting_from_the_address():
    for rel in CALLERS:
        src = _src(rel)
        assert "first_name_for" in src, f"{rel} is not using the canonical helper"
        for node in ast.walk(ast.parse(src)):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "split"):
                continue
            seg = ast.get_source_segment(src, node) or ""
            assert '"@"' not in seg and "'@'" not in seg, (
                f"{rel} splits on @ to build a name again: {seg}")


def test_usage_limit_headlines_render_with_and_without_a_name():
    """The claim is what the customer SEES, so render it. Parsing would also
    pass if `_greet` were undefined or the docstring had been demoted by an
    insert above it — both of which happened while writing this."""
    spec = importlib.util.spec_from_file_location(
        "_ule", os.path.join(ROOT, "usage_limit_emails.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["_ule"] = m
    spec.loader.exec_module(m)
    plan = sorted(m.THRESHOLDS)[0]
    cfg = m.THRESHOLDS[plan]
    for fn in (m._build_nudge_email, m._build_limit_hit_email):
        assert fn.__doc__, f"{fn.__name__} lost its docstring to an insert"
        named = str(fn("Daren", plan, cfg["daily_limit"], cfg))
        anon = str(fn(None, plan, cfg["daily_limit"], cfg))
        assert "Daren" in named, f"{fn.__name__} dropped a name it was given"
        assert "None" not in anon, (
            f"{fn.__name__} rendered the literal 'None' into customer copy")
        assert ", " not in anon.split("</h1>")[0].split("<h1>")[1], (
            f"{fn.__name__} left a dangling comma where the name was")
