"""free-users.csv must not be able to return a paying customer.

MEASURED 2026-09-20, live, `GET /api/v1/admin/audience/free-users.csv` → 168
rows. Their `users.plan` values:

    free 148 · developer 15 · starter 4 · research_seed 1

All 20 non-`free` rows shipped inside a file called free-users, because the
filter was the denylist `_PAID = ("pro", "founding", "enterprise")` and
`users.plan` holds four more paid names than that. Cross-joining the export
against `/api/v1/admin/crm/export.csv` on lower(email) put **6 rows with
`event_type='paid_conversion'`** in the send — a `developer` and a `starter`
among them. The module docstring promised "paid users ... excluded so the send
is clean."

WHAT THESE PIN:
  * the filter is an ALLOWLIST — in Python AND in the SQL. A denylist is wrong
    again the next time a plan is added, and wrong silently;
  * the paid set the allowlist is checked against is READ FROM tier_registry,
    so adding a paid plan to TIERS extends this test rather than defeating it;
  * a plan not positively known free is labelled `unknown`, never `free`;
  * what the filter removed is published per plan name, so a new name is
    visible instead of silently mislabelled;
  * the operator's own address is excluded from BOTH exports, by name, because
    a consumer mailbox carries no marker for a substring list to match.
"""
import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = (ROOT / "routes" / "audience_export.py").read_text(encoding="utf-8")

import routes.audience_export as ae            # noqa: E402
import routes.warm_key_cohort as wk            # noqa: E402
from routes._audience_identity import (        # noqa: E402
    is_operator_email, normalize_email)
from tier_registry import TIERS, paid_plan_names  # noqa: E402


# ★ EXTRACT THE STATEMENT, NEVER A BYTE WINDOW — a fixed slice measures length,
# not content, and this file is thick with commentary quoting the same SQL.
def _sql_literal(containing: str) -> str:
    parts = SRC.split('"""')
    hits = [p for i, p in enumerate(parts) if i % 2 == 1 and containing in p]
    assert len(hits) == 1, (
        f"{containing!r} matched {len(hits)} SQL literals — anchor is ambiguous")
    return hits[0]


COHORT_SQL = _sql_literal("ORDER BY created_at DESC NULLS LAST")
EXCLUDED_SQL = _sql_literal("GROUP BY 1 ORDER BY 2 DESC")

# The vocabulary the live export actually returned. The INPUTS are measured;
# the expectation for each is derived from the registry, never restated here.
LIVE_PLAN_VOCABULARY = ("free", "developer", "starter", "research_seed")


# ── the predicate ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("plan", LIVE_PLAN_VOCABULARY)
def test_every_plan_the_live_export_returned_is_classified(plan):
    """★ THE REGRESSION. developer / starter / research_seed are paid."""
    assert ae.is_non_paid_plan(plan) is (not TIERS[plan]["paid"]), plan


@pytest.mark.parametrize("plan", sorted(paid_plan_names()))
def test_no_plan_the_registry_calls_paid_can_reach_a_free_export(plan):
    """★ Derived from TIERS, so a paid plan added tomorrow is covered today.
    `_PAID` named three of these seven."""
    assert ae.is_non_paid_plan(plan) is False, plan
    assert ae.plan_label(plan) == "unknown", plan


def test_an_unknown_plan_is_excluded_not_included():
    """The asymmetry that decides the filter's direction: under-mailing costs a
    lead, over-mailing pitches an upgrade to someone who already bought."""
    for unknown in ("platinum", "whatever", "growth", "plan_2027"):
        assert ae.is_non_paid_plan(unknown) is False, unknown
        assert ae.plan_label(unknown) == "unknown", unknown


def test_a_row_with_no_plan_set_is_free_and_says_free():
    """Blank/NULL is a signup that never chose a plan — that IS known free,
    unlike an unrecognised name."""
    for blank in (None, "", "   ", "none"):
        assert ae.is_non_paid_plan(blank) is True, repr(blank)
        assert ae.plan_label(blank) == "free", repr(blank)


def test_the_allowlist_is_derived_from_the_registry_not_typed():
    assert ae.NON_PAID_PLANS == frozenset(
        {n.lower() for n, s in TIERS.items() if not s.get("paid")}
        | {"", "none"})
    assert not (ae.NON_PAID_PLANS & set(paid_plan_names()))


def test_the_fallback_is_fail_closed():
    """A fallback that guesses WIDE re-creates the bug it stands in for. It
    must be a subset — it can only ever under-mail."""
    assert ae._FALLBACK_NON_PAID_PLANS <= ae.NON_PAID_PLANS
    assert not (ae._FALLBACK_NON_PAID_PLANS & set(paid_plan_names()))


# ── the SQL ───────────────────────────────────────────────────────────────
def test_the_filter_is_an_allowlist_in_sql_too():
    """A Python predicate that fails safe is no help if the SQL still
    enumerates what to leave out — the SQL is what the rows come from."""
    assert "= ANY(%s)" in COHORT_SQL, "the cohort SQL is still an exclusion list"
    assert "<> ALL(%s)" not in COHORT_SQL
    # the excluded-bucket read is the complement, and inverted on purpose
    assert "<> ALL(%s)" in EXCLUDED_SQL


def test_the_sql_and_the_predicate_are_given_the_same_set():
    """Two layers reading two different lists is the drift this replaces."""
    assert "non_paid = sorted(NON_PAID_PLANS)" in SRC
    assert "(non_paid,)" in SRC


# ── what it removed is published ──────────────────────────────────────────
def test_what_the_allowlist_removed_is_published():
    """★ A new plan name must be VISIBLE, not silently dropped — otherwise the
    list shrinks one day and nobody knows why."""
    assert '"excluded_unknown_or_paid_plan"' in SRC
    assert '"non_paid_plans"' in SRC
    assert '"X-Excluded-Unknown-Or-Paid-Plan"' in SRC


# ── the operator ──────────────────────────────────────────────────────────
def test_the_operator_address_is_not_a_prospect_in_either_export():
    """`azmartone@gmail.com` has no dchub marker in it, so warm_key_cohort's
    substring list matched none of it and it entered the cohort as a lead."""
    assert is_operator_email("azmartone@gmail.com") is True
    assert wk._is_internal("azmartone@gmail.com") is True
    assert wk._domain_kind("azmartone@gmail.com") == "ours"
    assert not any(m in "azmartone@gmail.com" for m in wk._INTERNAL_MARKERS), (
        "a marker now matches it — this test no longer pins what it claims")


@pytest.mark.parametrize("addr", [
    "AZMartone@Gmail.com", " azmartone+news@gmail.com ",
    "azm.artone@googlemail.com", "azmartone+qa@GOOGLEMAIL.com"])
def test_the_operator_exclusion_survives_plus_tags_case_and_dots(addr):
    assert is_operator_email(addr) is True, addr
    assert normalize_email(addr) == "azmartone@gmail.com"


@pytest.mark.parametrize("addr", [
    "buyer@acmepower.com", "azmartone@example.org", "notazmartone@gmail.com",
    "azmartone@gmail.com.evil.test", "", None])
def test_the_operator_exclusion_does_not_swallow_strangers(addr):
    assert is_operator_email(addr) is False, addr


def _code_string_literals(src: str):
    """Every string literal that is CODE — docstrings excluded, and comments
    never enter the AST at all.

    ★ Written this way after the first version grepped the raw text and failed
    on the comment that EXPLAINS the rule: a comment quoting its subject became
    its subject. The thing being banned is a second live copy of the address,
    which is a literal in code; prose naming it is the documentation.
    """
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


def test_the_operator_rule_is_imported_by_both_not_copied():
    """The last copy of a shared rule survives on the busiest hop."""
    for name in ("audience_export", "warm_key_cohort"):
        src = (ROOT / "routes" / f"{name}.py").read_text(encoding="utf-8")
        assert "from routes._audience_identity import" in src, name
        copied = [t for t in _code_string_literals(src) if "azmartone" in t]
        assert not copied, f"{name} carries a live copy of the address: {copied}"


def test_that_copy_check_can_actually_see_a_copy():
    """A ban that has only ever been satisfied is not a ban. Pin BOTH sides:
    a literal in code is caught, the comment explaining it is not."""
    caught = _code_string_literals(
        'X = ("azmartone@gmail.com",)  # noqa\n')
    assert any("azmartone" in t for t in caught)
    ignored = _code_string_literals(
        '"""Doc naming azmartone@gmail.com."""\n# and azmartone@gmail.com\nY = 1\n')
    assert not any("azmartone" in t for t in ignored)


def test_the_env_override_can_only_widen_the_operator_set(monkeypatch):
    """An empty or typoed var must not be able to REMOVE the operator."""
    for val in ("", "   ", "not-an-address", "teammate@dchub.io"):
        monkeypatch.setenv("DCHUB_OPERATOR_EMAILS", val)
        assert is_operator_email("azmartone@gmail.com") is True, val
    monkeypatch.setenv("DCHUB_OPERATOR_EMAILS", "teammate@dchub.io")
    assert is_operator_email("teammate@dchub.io") is True


# ── end to end, through the real SQL ──────────────────────────────────────
# The fixture mirrors the live vocabulary. `paid_conversion` marks the rows
# that the 2026-09-20 CRM cross-join found in the send.
USERS_FIXTURE = [
    # email,                 name,          company,  plan,            created
    ("free1@acmepower.com",  "Ada Free",    "Acme",   "free",          "2026-07-01"),
    ("dev1@bigco.com",       "Dev Buyer",   "BigCo",  "developer",     "2026-07-02"),
    ("start1@bigco.com",     "Sam Starter", "BigCo",  "starter",       "2026-07-03"),
    ("seed@lab.edu",         "Res Seed",    "Lab",    "research_seed", "2026-07-04"),
    ("pro1@enterprise.com",  "Pat Pro",     "Ent",    "pro",           "2026-07-05"),
    ("noplan@acmepower.com", "Nora None",   "Acme",   None,            "2026-07-06"),
    ("azmartone@gmail.com",  "Operator",    "",       "free",          "2026-07-07"),
    ("bounced@acmepower.com", "Bo Unce",    "Acme",   "free",          "2026-07-08"),
]
PAID_CONVERSIONS = {"dev1@bigco.com", "start1@bigco.com"}
SUPPRESSED = {"bounced@acmepower.com"}


class _FakeCursor:
    """★ HONOURS THE SQL IT IS GIVEN. A fake that filters the fixture its own
    way proves nothing about the statement — it would pass against the denylist
    this file exists to remove. This one reads the OPERATOR out of the executed
    SQL and applies it to the parameter the code actually passed, so reverting
    to `<> ALL(list(_PAID))` changes what comes back."""

    def __init__(self):
        self.rows = []

    @staticmethod
    def _keep(sql, allowed, plan):
        p = (plan or "").strip().lower()
        if "= ANY(%s)" in sql:
            return p in allowed
        if "<> ALL(%s)" in sql:
            return p not in allowed
        raise AssertionError(f"plan filter has neither operator:\n{sql}")

    def execute(self, sql, params=None):
        if "email_suppression" in sql:
            self.rows = [(e,) for e in sorted(SUPPRESSED)]
            return
        allowed = set(params[0]) if params else set()
        kept = [r for r in USERS_FIXTURE if self._keep(sql, allowed, r[3])]
        if "GROUP BY 1 ORDER BY 2 DESC" in sql:
            counts = {}
            for r in kept:
                counts[(r[3] or "").strip().lower()] = \
                    counts.get((r[3] or "").strip().lower(), 0) + 1
            self.rows = sorted(counts.items(), key=lambda kv: -kv[1])
            return
        self.rows = kept

    def fetchall(self):
        return list(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def rollback(self):
        pass


@pytest.fixture()
def gathered(monkeypatch):
    monkeypatch.setattr(ae, "_conn", lambda: _FakeConn())
    monkeypatch.setattr(ae, "_return", lambda c, error=False: None)
    return ae._gather()


def test_the_export_returns_no_paying_customer(gathered):
    """★ THE REGRESSION, end to end. Six of the twenty mislabelled rows were
    `paid_conversion` in the CRM."""
    rows, _ = gathered
    shipped = {r["email"].lower() for r in rows}
    assert not (shipped & PAID_CONVERSIONS), (
        f"paying customers in a free-users export: {shipped & PAID_CONVERSIONS}")
    assert not any(TIERS.get(r["tier"], {}).get("paid") for r in rows)


def test_the_export_returns_exactly_the_rows_that_qualify(gathered):
    rows, _ = gathered
    assert {r["email"] for r in rows} == {"free1@acmepower.com",
                                          "noplan@acmepower.com"}


def test_no_shipped_row_is_labelled_free_without_being_free(gathered):
    rows, _ = gathered
    for r in rows:
        assert r["tier"] != "unknown", r
        assert ae.is_non_paid_plan(r["tier"]), r


def test_the_suppressed_and_operator_rows_are_removed_and_counted(gathered):
    rows, meta = gathered
    shipped = {r["email"] for r in rows}
    assert "bounced@acmepower.com" not in shipped
    assert "azmartone@gmail.com" not in shipped
    assert meta["suppressed_excluded"] == 1
    assert meta["operator_excluded"] == 1


def test_the_excluded_bucket_names_every_plan_that_was_removed(gathered):
    """★ A new plan name lands here and is VISIBLE, rather than shipping
    labelled free."""
    _, meta = gathered
    assert meta["excluded_unknown_or_paid_plan"] == {
        "developer": 1, "starter": 1, "research_seed": 1, "pro": 1}
    assert set(meta["non_paid_plans"]) == set(ae.NON_PAID_PLANS)
