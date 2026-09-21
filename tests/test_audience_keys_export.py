"""The keyed-contact export: every tier, labelled, addresses only.

This endpoint exists because three sibling exports each answered a neighbouring
question and none answered "the keyed users, for a CRM import":
`audience_export` reads `users` (and excludes paid with a denylist),
`warm_key_cohort` reads `mcp_dev_keys` but drops paid by design, and
`crm_reverse_etl` reads captured lead events rather than key rows.

WHAT THESE PIN — the failure modes that would make the file dangerous:
  * the admin gate fails CLOSED, including when its own import breaks. This
    endpoint returns every address we hold;
  * `api_key` is never selected and never emitted. This is addresses, not
    credentials;
  * an UNRECOGNISED tier reads `unknown`, never `non_paying`. That is the whole
    lesson of `audience_export._PAID` — a 3-name denylist against a 7-name
    canon, which put 20 of 168 paying customers into a "free users" export;
  * a paying customer is EXPORTED (that is the point) but never relabelled as
    non-paying, and consent is echoed, never asserted;
  * one person holding several keys appears once, at the best tier they hold,
    over the widest activity window;
  * `skipped_no_email` is published. The count of keys with no address is the
    figure the sibling module's docstring got wrong by 19x.
"""
import datetime as dt
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = (ROOT / "routes" / "audience_keys_export.py").read_text(encoding="utf-8")

import routes.audience_keys_export as ake  # noqa: E402
import routes.warm_key_cohort as wk  # noqa: E402


# ★ EXTRACT THE STATEMENT, NEVER A BYTE WINDOW — a fixed slice measures length,
# not content, and drifts the moment a column or comment is added.
def _sql_literal(containing: str) -> str:
    parts = SRC.split('"""')
    hits = [p for i, p in enumerate(parts) if i % 2 == 1 and containing in p]
    assert len(hits) == 1, (
        f"{containing!r} matched {len(hits)} SQL literals — anchor is ambiguous")
    return hits[0]


EXPORT_SQL = _sql_literal("metadata->>'client_name'")
NO_EMAIL_SQL = _sql_literal("position('@' in email) <= 1")

UTC = dt.timezone.utc
NON_PAID = wk.is_non_paid_tier


# ───────────────────────── fake DB ─────────────────────────
# ★ DISPATCHES ON THE SQL IT IS GIVEN. A cursor that returns canned results in
# call order passes even when the code sends the wrong query to the wrong table,
# which is the one thing a DB fake is supposed to catch.
class _Cur:
    def __init__(self, keys, no_email, suppressed, fail=()):
        self._keys, self._no_email = keys, no_email
        self._supp, self._fail = suppressed, fail
        self._out = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "email_suppression" in s:
            if "suppression" in self._fail:
                raise RuntimeError("suppression table missing")
            self._out = [(e,) for e in self._supp]
        elif "position('@' in email) <= 1" in s:
            if "no_email" in self._fail:
                raise RuntimeError("no_email count failed")
            self._out = list(self._no_email)
        elif "metadata->>'client_name'" in s and "mcp_dev_keys" in s:
            self._out = list(self._keys)
        else:
            raise AssertionError(f"unexpected query: {s[:120]}")

    def fetchall(self):
        return self._out


class _Conn:
    def __init__(self, **kw):
        self._kw = kw
        self.rolled_back = 0

    def cursor(self):
        return _Cur(**self._kw)

    def rollback(self):
        self.rolled_back += 1


def key(email, tier="free", created="2026-08-01", used=None,
        client_name=None, session_id=None, opt_in=None):
    """One mcp_dev_keys row in the column order the export SELECTs."""
    def ts(v):
        return dt.datetime.fromisoformat(v).replace(tzinfo=UTC) if v else None
    return (email, tier, ts(created), ts(used), client_name, session_id, opt_in)


@pytest.fixture
def gather(monkeypatch):
    def run(keys, no_email=(), suppressed=(), fail=()):
        conn = _Conn(keys=keys, no_email=no_email, suppressed=suppressed,
                     fail=fail)
        monkeypatch.setattr(ake, "_conn", lambda: conn)
        monkeypatch.setattr(ake, "_release", lambda c, error=False: None)
        return ake._gather()
    return run


# ───────────────────── the gate returns everything ─────────────────────
def test_admin_gate_fails_closed_when_its_own_import_breaks(monkeypatch):
    import routes.audience_export as ax

    def boom():
        raise ImportError("gate module unavailable")

    monkeypatch.setattr(ax, "_admin_ok", boom)
    assert ake._admin_ok() is False


def test_gate_is_called_not_copied():
    assert "from routes.audience_export import _admin_ok" in SRC
    # A second hand-rolled comparison is how one of the two ends up weaker.
    assert "compare_digest" not in SRC
    assert "DCHUB_ADMIN_KEY" not in SRC


# ───────────────────── credentials never leave ─────────────────────
def test_api_key_is_never_selected_or_emitted():
    assert "api_key" not in EXPORT_SQL
    assert "api_key" not in ake._CSV_FIELDS


def test_export_sql_reads_the_fields_the_csv_promises():
    for frag in ("metadata->>'client_name'", "metadata->>'session_id'",
                 "last_used_at", "created_at", "status = 'active'"):
        assert frag in EXPORT_SQL, frag
    # Emailless (anonymous) keys are excluded from the export itself...
    assert "position('@' in email) > 1" in EXPORT_SQL
    # ...and counted by the sibling query, not silently dropped.
    assert "COUNT(*)" in NO_EMAIL_SQL


# ───────── the denylist lesson: unknown tiers must not read non_paying ─────────
@pytest.mark.parametrize("tier", ["free", "identified", "anonymous", "anon",
                                  "", "none"])
def test_known_non_paid_tiers_read_non_paying(tier):
    assert ake._payment_status(tier, NON_PAID) == "non_paying"


@pytest.mark.parametrize("tier", ["paid", "enterprise", "PAID", " Enterprise "])
def test_paid_tiers_read_paying(tier):
    assert ake._payment_status(tier, NON_PAID) == "paying"


@pytest.mark.parametrize("tier", ["platinum", "team", "starter", "developer",
                                  "research_seed", "founding", "pro",
                                  "whatever_ships_next"])
def test_unrecognised_tier_reads_unknown_never_non_paying(tier):
    # ★ THE GUARD. Every name here is either a live registry plan that
    # mcp_dev_keys.tier does not speak, or a tier nobody has added yet. Reading
    # any of them as "non_paying" is how a paying customer lands in an upsell
    # segment — `audience_export` does exactly this with a 3-name denylist.
    assert ake._payment_status(tier, NON_PAID) == "unknown"


def test_no_hardcoded_paid_denylist_in_this_module():
    """A collection of REGISTRY PLAN names in this module's code re-imports the
    bug its docstring is about.

    ★ Scanned over the AST, not the text. The first version grepped SRC and
    failed on the module's own docstring, which quotes
    `("pro","founding","enterprise")` in order to explain the defect — a comment
    that names its subject became its subject. Prose describing a denylist is
    not a denylist; a tuple of plan names is. `mcp_dev_keys.tier` only ever
    holds free/identified/paid/enterprise, so any of these names inside a
    literal here means the registry's vocabulary was imported by mistake.
    """
    import ast

    banned = {"starter", "developer", "research_seed", "founding", "team", "pro"}
    for node in ast.walk(ast.parse(SRC)):
        if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            continue
        names = {e.value for e in node.elts
                 if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        hit = names & banned
        assert not hit, (
            f"registry plan name(s) {sorted(hit)} in a literal at line "
            f"{node.lineno} — read the tier and label it, do not enumerate plans")


# ───────────────────── every tier is exported ─────────────────────
def test_paying_customers_are_exported_and_labelled_not_dropped(gather):
    rows, meta = gather([
        key("buyer@acmepower.com", tier="paid", created="2026-05-01"),
        key("big@hyperscale.com", tier="enterprise", created="2026-04-01"),
        key("lead@startup.io", tier="identified", created="2026-09-01"),
    ])
    assert not meta.get("error")
    got = {r["email"]: r for r in rows}
    assert set(got) == {"buyer@acmepower.com", "big@hyperscale.com",
                        "lead@startup.io"}
    assert got["buyer@acmepower.com"]["payment_status"] == "paying"
    assert got["big@hyperscale.com"]["payment_status"] == "paying"
    assert got["lead@startup.io"]["payment_status"] == "non_paying"
    assert meta["by_payment_status"] == {"paying": 2, "non_paying": 1}


# ───────────────────── one person, several keys ─────────────────────
def test_multiple_keys_collapse_to_best_tier_and_widest_window(gather):
    rows, meta = gather([
        # Newest first, as the SQL orders them.
        key("dev@acmepower.com", tier="paid", created="2026-09-01",
            used="2026-09-19", client_name=None, session_id="sess-new"),
        key("dev@acmepower.com", tier="free", created="2026-03-04",
            used="2026-09-20", client_name="claude-code", session_id="sess-old"),
    ])
    assert len(rows) == 1
    r = rows[0]
    assert r["keys_held"] == 2
    assert r["key_tier"] == "paid"            # best tier held
    assert r["payment_status"] == "paying"
    assert r["created_at"].startswith("2026-03-04")   # first ever seen
    assert r["last_seen_at"].startswith("2026-09-20")  # latest on any key
    # Context is backfilled from the other key rather than left blank.
    assert r["mcp_platform"] == "claude-code"
    assert r["dchub_session_ref"] == "sess-new"


def test_a_free_key_never_downgrades_an_enterprise_row(gather):
    rows, _ = gather([
        key("cfo@hyperscale.com", tier="free", created="2026-09-10"),
        key("cfo@hyperscale.com", tier="enterprise", created="2026-01-02"),
    ])
    assert len(rows) == 1
    assert rows[0]["key_tier"] == "enterprise"
    assert rows[0]["payment_status"] == "paying"


# ───────────────────── platform is a hint, not an identity ─────────────────────
def test_uuid_shaped_client_name_is_dropped_not_shipped_as_a_platform():
    v, src = ake._platform("3f2a1b9c-1111-4222-8333-abcdefabcdef")
    assert v == ""
    assert src == "dropped_uuid_shaped"


def test_platform_records_that_it_is_caller_supplied():
    v, src = ake._platform("pentest7")
    assert v == "pentest7"
    assert "unvalidated" in src
    assert ake._platform(None) == ("", "none_recorded")
    assert ake._platform("  ") == ("", "none_recorded")


def test_long_client_name_is_truncated():
    v, _ = ake._platform("x" * 400)
    assert len(v) == 80


# ───────────────────── consent is echoed, never asserted ─────────────────────
def test_marketing_opt_in_is_echoed_from_the_stored_flag(gather):
    rows, meta = gather([
        key("a@acmepower.com", opt_in="true"),
        key("b@acmepower.com", opt_in=None),
        key("c@acmepower.com", opt_in="false"),
    ])
    got = {r["email"]: r["marketing_opt_in"] for r in rows}
    assert got == {"a@acmepower.com": "true", "b@acmepower.com": "false",
                   "c@acmepower.com": "false"}
    assert meta["marketing_opt_in_true"] == 1
    assert "not marketing consent" in meta["consent_note"]


# ───────────────────── filters publish what they removed ─────────────────────
def test_internal_and_suppressed_are_removed_and_counted_separately(gather):
    rows, meta = gather(
        [key("ops@dchub.cloud"), key("qa+test@acmepower.com"),
         key("real@acmepower.com"), key("gone@acmepower.com")],
        suppressed=["gone@acmepower.com"])
    assert [r["email"] for r in rows] == ["real@acmepower.com"]
    assert meta["skipped_internal_or_operator"] == 2
    assert meta["skipped_suppressed"] == 1
    # The counts PARTITION the input: nothing is lost between the two numbers.
    assert (meta["rows"] + meta["skipped_internal_or_operator"]
            + meta["skipped_suppressed"]) == meta["keys_with_email"]


def test_skipped_no_email_is_published_per_tier(gather):
    _, meta = gather([key("real@acmepower.com")],
                     no_email=[("identified", 519), ("free", 100)])
    assert meta["skipped_no_email"] == 619
    assert meta["skipped_no_email_by_tier"] == {"identified": 519, "free": 100}


def test_a_failed_subread_names_itself_instead_of_publishing_zero(gather):
    # "0 keys without an email" and "we could not count them" demand opposite
    # responses, so a failure must never arrive as a number.
    rows, meta = gather([key("real@acmepower.com")], fail=("no_email",))
    assert len(rows) == 1
    assert meta["skipped_no_email"] is None
    assert meta["skipped_no_email_by_tier"] is None
    assert "no_email" in meta["errors"]


def test_suppression_read_failure_does_not_silently_unsuppress(gather):
    rows, meta = gather([key("real@acmepower.com")],
                        suppressed=["real@acmepower.com"], fail=("suppression",))
    # The row survives (we cannot prove it is suppressed) but the failure is
    # named, so nobody reads skipped_suppressed=0 as "nothing was suppressed".
    assert len(rows) == 1
    assert "suppression" in meta["errors"]


def test_no_db_is_an_error_not_an_empty_export(monkeypatch):
    monkeypatch.setattr(ake, "_conn", lambda: None)
    rows, meta = ake._gather()
    assert rows == []
    assert meta["error"] == "no_db"


def test_identity_rules_are_borrowed_not_copied():
    """This module must hold NO copy of the internal/operator rule.

    ★ Why this is a guard and not a style note: the operator's own address is a
    consumer mailbox carrying no dchub marker, so `_INTERNAL_MARKERS` never
    matched it and it entered contact lists as a prospect. The fix lives in
    routes/_audience_identity.is_operator_email and reaches this module only
    because `_is_internal` is the SAME object warm_key_cohort exports. A third
    copy here would silently opt out of that fix and of the next one.
    """
    is_internal, domain, domain_kind, is_non_paid = ake._helpers()
    assert is_internal is wk._is_internal
    assert domain is wk._domain
    assert domain_kind is wk._domain_kind
    assert is_non_paid is wk.is_non_paid_tier
    for copied in ("_INTERNAL_MARKERS", "NON_PAID_TIERS", "_CONSUMER_DOMAINS"):
        assert copied not in SRC, f"{copied} copied — import the rule, don't fork it"


def test_classifier_import_failure_refuses_rather_than_guessing(monkeypatch):
    def boom():
        raise ImportError("warm_key_cohort unavailable")

    monkeypatch.setattr(ake, "_helpers", boom)
    rows, meta = ake._gather()
    assert rows == []
    assert "classifiers unavailable" in meta["error"]


# ───────────────────── shape of the file itself ─────────────────────
def test_csv_leads_with_the_six_columns_a_crm_import_asks_for():
    assert ake._CSV_FIELDS[:6] == [
        "email", "key_tier", "mcp_platform", "dchub_session_ref",
        "created_at", "last_seen_at"]


def test_rows_are_newest_first(gather):
    rows, _ = gather([key("c@acmepower.com", created="2026-01-01"),
                      key("a@acmepower.com", created="2026-09-01"),
                      key("b@acmepower.com", created="2026-05-01")])
    assert [r["email"] for r in rows] == ["a@acmepower.com", "b@acmepower.com",
                                          "c@acmepower.com"]
