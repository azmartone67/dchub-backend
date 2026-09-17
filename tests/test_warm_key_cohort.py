"""The warm-key cohort: who gave us an email and never bought.

Measured 2026-09-17, `mcp_dev_keys WHERE status='active'`:
    identified 540 · free 113 · paid 47 · enterprise 6
Five conversions in 30d — web-direct 3, organic-direct 1, value-harness 1 —
NONE from the agent channel, whose reach is 66 agents / ~462 calls per week.
~493 bound addresses have never paid and nothing read them.

WHAT THESE PIN — the failure modes that would make the list worse than useless:
  * the admin gate fails CLOSED, including when its own import breaks. This
    endpoint returns every address we hold;
  * `api_key` is never in the output. The cohort is addresses, not credentials;
  * `mailable` is the ONLY number an outreach plan may size itself on, and the
    removed_* counts PARTITION the cohort so nothing is double-counted or lost
    between the total and it;
  * a failed sub-read names itself instead of publishing 0 — "0 engaged" and
    "we could not read engagement" demand opposite responses;
  * the paid set is READ from tier_registry, so a new paid tier cannot silently
    start receiving upsell mail.
"""
import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = (ROOT / "routes" / "warm_key_cohort.py").read_text(encoding="utf-8")

import routes.warm_key_cohort as wk  # noqa: E402

# ★ EXTRACT THE STATEMENT, NEVER A BYTE WINDOW. The first version of these
# assertions sliced SRC[i:i+1400] from a comment marker; adding two columns
# and a comment pushed the clause they checked out of the window and four
# guards failed for a reason that had nothing to do with the SQL. A fixed
# slice measures LENGTH, not content.
def _sql_literal(containing: str) -> str:
    """The triple-quoted SQL literal in warm_key_cohort.py that contains
    `containing`. Fails loudly rather than returning a partial match."""
    parts = SRC.split('"""')
    hits = [p for i, p in enumerate(parts) if i % 2 == 1 and containing in p]
    assert len(hits) == 1, (
        f"{containing!r} matched {len(hits)} SQL literals — anchor is ambiguous")
    return hits[0]


COHORT_SQL = _sql_literal("keys_held")



def row(email, **kw):
    r = {"email": email, "tier": kw.get("tier", "identified"),
         "domain": wk._domain(email), "domain_kind": wk._domain_kind(email),
         "days_since_bind": kw.get("days", 10),
         "age_bucket": wk._bucket_days(kw.get("days", 10)),
         "keys_held": 1, "calls_after_bind": kw.get("calls", 0),
         "last_call": "", "top_tool_wall": kw.get("tool", ""),
         "wall_hits": kw.get("hits", 0),
         "already_paid": kw.get("paid", False),
         "suppressed": kw.get("supp", False),
         "marketing_opt_in": kw.get("optin", False),
         "name": kw.get("name", "")}
    return r


COHORT = [
    row("buyer@acmepower.com", calls=4, tool="analyze_site", hits=9,
        optin=True, name="A Buyer"),
    row("dev@hyperscale.io", calls=0, tool="get_interconnection_queue", hits=3),
    row("someone@gmail.com", calls=2, tool="get_grid_intelligence", hits=1),
    row("cold@utility.co.uk", days=240, calls=0),
    row("identify-probe@dchub.cloud", calls=1),        # ours
    row("jonathan@dchub.cloud", calls=30),             # ours
    row("already@customer.com", paid=True, calls=5),   # a customer
    row("bounced@olddomain.com", supp=True, calls=1),  # suppressed
    # ★ OVERLAP ROWS. Without these the removed_* ordering is never exercised:
    # every row falls in exactly one category by accident, so dropping a
    # `domain_kind != "ours"` guard is a no-op and the partition test passes
    # vacuously. Caught by mutation — the fixture was the weak part, not the
    # assertion.
    row("comp@dchub.cloud", paid=True, calls=2),       # ours AND a customer
    row("old-probe@dchub.cloud", supp=True),           # ours AND suppressed
    row("churned@ex.com", paid=True, supp=True),       # customer AND suppressed
    # ★ THE SHAPE THAT LEAKED IN PRODUCTION: tier='paid' on the key itself.
    # The old fixture only ever set already_paid=True, which is the DERIVED
    # flag — so it never exercised the tier vocabulary that was actually wrong.
    row("customer@realco.com", tier="paid", paid=True, calls=9),
    row("ent@bigco.com", tier="enterprise", paid=True, calls=3),
    row("maya.chen@dchubmail.com", calls=6),           # ours, seeded domain
    row("azmartone+qa0807@gmail.com", calls=55),       # ours, plus-tagged QA
]


# ── the gate ─────────────────────────────────────────────────────────────
def test_the_admin_gate_fails_closed_when_its_own_import_breaks(monkeypatch):
    """★ This endpoint returns every address we hold. An ImportError that
    opened it would be a silent full-list disclosure."""
    monkeypatch.setitem(sys.modules, "routes.audience_export", None)
    assert wk._admin_ok() is False


def test_both_routes_refuse_without_the_gate(monkeypatch):
    import flask
    monkeypatch.setattr(wk, "_admin_ok", lambda: False)
    # _gather must not even be reached.
    monkeypatch.setattr(wk, "_gather", lambda: (_ for _ in ()).throw(
        AssertionError("gathered before the gate passed")))
    app = flask.Flask("warm-test")
    app.register_blueprint(wk.warm_key_cohort_bp)
    cl = app.test_client()
    for path in ("/api/v1/admin/audience/warm-keys",
                 "/api/v1/admin/audience/warm-keys.csv"):
        r = cl.get(path)
        assert r.status_code == 403, path


# ── never a credential ───────────────────────────────────────────────────
def test_api_key_is_never_returned():
    """★ The cohort is addresses, not credentials."""
    assert "api_key" not in wk._CSV_FIELDS
    for r in COHORT:
        assert "api_key" not in r
    # And the cohort SELECT does not even read it into a row.
    assert "api_key" not in COHORT_SQL.split("GROUP BY")[0], (
        "the cohort query selects api_key")


def test_the_engagement_read_joins_on_the_key_but_projects_only_the_email():
    i = SRC.index("calls_after")
    seg = SRC[i - 200:i + 700]
    assert "l.api_key = k.api_key" in seg, "the join lost its key"
    assert "COUNT(l.api_key)" in seg, "counting something other than calls"
    assert "l.timestamp > k.created_at" in seg, (
        "'after bind' is not enforced — every call would count")


# ── the arithmetic behind `mailable` ─────────────────────────────────────
def test_mailable_removes_ours_customers_and_suppressed():
    m = [r["email"] for r in COHORT if wk._mailable(r)]
    assert "buyer@acmepower.com" in m
    assert "someone@gmail.com" in m
    assert "identify-probe@dchub.cloud" not in m
    assert "jonathan@dchub.cloud" not in m
    assert "already@customer.com" not in m
    assert "bounced@olddomain.com" not in m
    assert len(m) == 4, m


def test_the_removed_counts_partition_the_cohort():
    """★ The basis sentence CLAIMS they sum to the total with nothing
    double-counted. If they did not, every filter's effect would be
    unreadable and the list would be sized off the wrong number."""
    s = wk.summarize(COHORT)
    total = (s["removed_ours"] + s["removed_already_paid"]
             + s["removed_suppressed"] + s["mailable"])
    assert total == s["cohort_total"] == len(COHORT), (
        f"{s['removed_ours']}+{s['removed_already_paid']}"
        f"+{s['removed_suppressed']}+{s['mailable']} != {s['cohort_total']}")


def test_the_partition_holds_because_every_cohort_row_has_an_email():
    """The arithmetic above is only total if a row without an address cannot
    exist — enforced in SQL, not in Python, so it is pinned in SQL."""
    assert "email IS NOT NULL AND email <> ''" in COHORT_SQL
    assert "position('@' in email) > 1" in COHORT_SQL


def test_engaged_and_vanished_partition_the_mailable_set():
    s = wk.summarize(COHORT)
    assert (s["mailable_engaged_after_bind"]
            + s["mailable_bound_and_vanished"]) == s["mailable"]
    # ★ THE SPLIT THAT DECIDES WHETHER THIS LIST IS WORTH WORKING.
    assert s["mailable_engaged_after_bind"] == 2   # acmepower, gmail
    assert s["mailable_bound_and_vanished"] == 2   # hyperscale, utility.co.uk


def test_the_aggregate_only_profiles_mailable_rows():
    """Profiling the whole cohort would put our own probe addresses into the
    domain/age/wall distributions an outreach plan is written from."""
    s = wk.summarize(COHORT)
    assert sum(s["mailable_by_domain_kind"].values()) == s["mailable"]
    assert "ours" not in s["mailable_by_domain_kind"]
    assert sum(s["mailable_by_age"].values()) == s["mailable"]


def test_the_top_walls_name_tools_not_counts_of_everything():
    s = wk.summarize(COHORT)
    assert s["mailable_top_walls"].get("analyze_site") == 1
    assert s["mailable_top_walls"].get("get_interconnection_queue") == 1
    # the suppressed row's tool must not appear
    assert sum(s["mailable_top_walls"].values()) <= s["mailable"]


# ── classification ──────────────────────────────────────────────────────
@pytest.mark.parametrize("email,kind", [
    ("a@acmepower.com", "corporate"),
    ("a@gmail.com", "consumer"),
    ("a@proton.me", "consumer"),
    ("probe@dchub.cloud", "ours"),
    ("test@whatever.com", "ours"),
    ("noreply@acme.com", "ours"),
    ("notanemail", "unknown"),
])
def test_domain_kind(email, kind):
    assert wk._domain_kind(email) == kind


@pytest.mark.parametrize("days,bucket", [
    (0, "0-7d"), (7, "0-7d"), (8, "8-30d"), (30, "8-30d"),
    (31, "31-90d"), (180, "91-180d"), (181, "180d+"), (None, "unknown")])
def test_age_buckets(days, bucket):
    assert wk._bucket_days(days) == bucket


# ── honesty on failure ──────────────────────────────────────────────────
# ★★ THE TEST THAT WAS VACUOUS, AND WHY.
#
# The first version asserted "every tier_registry.paid_plans() value is in our
# exclusion set". True, and useless: mcp_dev_keys.tier does not USE that
# vocabulary. It stores free/identified/paid/enterprise, and the literal
# 'paid' is not a plan name — so the exclusion matched nothing and 26 paying
# customers reached `mailable` in production. The assertion pointed at the
# wrong producer, exactly like the code it was guarding.
#
# The replacement pins the vocabulary THE COLUMN ACTUALLY HOLDS, measured, and
# requires an unknown value to be excluded.

# Measured 2026-09-17 from keys_by_tier (mcp_dev_keys, status='active').
MEASURED_TIER_VALUES = {"free": 113, "identified": 540, "paid": 47,
                        "enterprise": 6}


@pytest.mark.parametrize("tier", sorted(MEASURED_TIER_VALUES))
def test_every_tier_the_column_actually_holds_is_classified(tier):
    """★ THE REGRESSION. 'paid' and 'enterprise' must not be mailable."""
    mailable_tier = wk.is_non_paid_tier(tier)
    assert mailable_tier == (tier in ("free", "identified")), tier


def test_an_unknown_tier_is_excluded_not_included():
    """★ The asymmetry that decides the direction of the filter: under-mailing
    costs a lead, over-mailing pitches an upgrade to a customer."""
    for unknown in ("team", "research_seed", "admin", "platinum", "whatever"):
        assert wk.is_non_paid_tier(unknown) is False, unknown


def test_the_filter_is_an_allowlist_in_sql_too():
    """A Python predicate that fails safe is no help if the SQL still
    enumerates what to exclude."""
    assert "= ANY(%s)" in COHORT_SQL, "the cohort SQL is still an exclusion list"
    assert "<> ALL(%s)" not in COHORT_SQL.split("GROUP BY")[0]
    # and the already-paid read is inverted the same way
    j = SRC.index("SELECT DISTINCT lower(trim(email)) FROM mcp_dev_keys")
    assert "<> ALL(%s)" in SRC[j:j + 320], (
        "already_paid still enumerates paid tiers")


def test_the_registry_vocabulary_is_kept_only_as_a_comment():
    """It is not the filter — it does not speak this column's language."""
    assert "_REGISTRY_PLAN_NAMES_FOR_REFERENCE" in SRC
    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "is_non_paid_tier")
    body = ast.get_source_segment(SRC, fn) or ""
    assert "paid_plans" not in body, "the broken source is back in the filter"
    assert "NON_PAID_TIERS" in body


def test_what_the_inversion_removed_is_published():
    """★ A new paid tier must be VISIBLE, not silently dropped — otherwise the
    list shrinks one day and nobody knows why."""
    assert '"excluded_unknown_or_paid_tier"' in SRC
    assert '"non_paid_tiers"' in SRC


def test_a_failed_subread_names_itself_instead_of_publishing_zero(monkeypatch):
    """★ '0 engaged' and 'we could not read engagement' want opposite
    responses. The route must say which it is."""
    import flask
    monkeypatch.setattr(wk, "_admin_ok", lambda: True)
    monkeypatch.setattr(wk, "_gather",
                        lambda: (COHORT, {"errors": {"calls_after_bind": "boom"}}))
    app = flask.Flask("warm-test")
    app.register_blueprint(wk.warm_key_cohort_bp)
    j = app.test_client().get("/api/v1/admin/audience/warm-keys").get_json()
    assert j["ok"] is True
    assert j["partial_reads"]["calls_after_bind"] == "boom"
    assert "UNKNOWN" in j["partial_note"]


def test_a_cohort_read_failure_is_an_error_not_an_empty_list(monkeypatch):
    import flask
    monkeypatch.setattr(wk, "_admin_ok", lambda: True)
    monkeypatch.setattr(wk, "_gather", lambda: ([], {"error": "no_db"}))
    app = flask.Flask("warm-test")
    app.register_blueprint(wk.warm_key_cohort_bp)
    j = app.test_client().get("/api/v1/admin/audience/warm-keys").get_json()
    assert j["ok"] is False and j["error"] == "no_db"
    assert "mailable" not in j, "an unreadable cohort published a size"


def test_the_basis_names_the_other_table_so_the_two_are_not_confused():
    """audience_export reads `users`; this reads mcp_dev_keys. Conflating them
    is how 'we already export our audience' hides a whole population."""
    s = wk.summarize(COHORT)
    assert "mcp_dev_keys" in s["basis"]
    assert "audience_export" in s["basis"] and "users" in s["basis"]


def test_the_csv_carries_only_mailable_rows(monkeypatch):
    import flask
    monkeypatch.setattr(wk, "_admin_ok", lambda: True)
    monkeypatch.setattr(wk, "_gather", lambda: (COHORT, {}))
    app = flask.Flask("warm-test")
    app.register_blueprint(wk.warm_key_cohort_bp)
    body = app.test_client().get(
        "/api/v1/admin/audience/warm-keys.csv").get_data(as_text=True)
    assert "buyer@acmepower.com" in body
    for excluded in ("dchub.cloud", "already@customer.com",
                     "bounced@olddomain.com"):
        assert excluded not in body, f"{excluded} reached the outreach CSV"


# ── consent is not reachability (r-consent, 2026-09-17) ─────────────────
def test_sendable_is_a_strict_subset_of_mailable():
    """★ Having an address is not permission to use it. Grok asked for
    'marketing_opt_in = true' rows; the answer has to be a different number
    from `mailable`, or the distinction is decorative."""
    s = wk.summarize(COHORT)
    assert s["sendable_with_consent"] <= s["mailable"]
    assert (s["sendable_with_consent"] + s["mailable_without_consent"]
            == s["mailable"])
    # exactly one fixture row carries consent
    assert s["sendable_with_consent"] == 1


def test_consent_alone_does_not_make_a_customer_sendable():
    """An opted-in address that already pays is still not an upsell target."""
    r = row("payer@co.com", optin=True, paid=True)
    assert wk._mailable(r) is False and wk._sendable(r) is False


def test_consent_alone_does_not_beat_suppression():
    r = row("bounced@co.com", optin=True, supp=True)
    assert wk._sendable(r) is False


def test_an_opted_in_row_of_ours_is_still_excluded():
    r = row("probe@dchub.cloud", optin=True)
    assert wk._sendable(r) is False


def test_the_csv_is_consent_gated_by_default(monkeypatch):
    """★★ THE FILE SOMEONE PASTES INTO A SENDING TOOL. Its default must be the
    set that may lawfully receive mail."""
    import flask
    monkeypatch.setattr(wk, "_admin_ok", lambda: True)
    monkeypatch.setattr(wk, "_gather", lambda: (COHORT, {}))
    app = flask.Flask("warm-test")
    app.register_blueprint(wk.warm_key_cohort_bp)
    cl = app.test_client()
    body = cl.get("/api/v1/admin/audience/warm-keys.csv").get_data(as_text=True)
    assert "buyer@acmepower.com" in body            # consented
    assert "someone@gmail.com" not in body          # reachable, NOT consented
    # the analysis view is opt-in and labels every row
    any_body = cl.get(
        "/api/v1/admin/audience/warm-keys.csv?consent=any").get_data(as_text=True)
    assert "someone@gmail.com" in any_body
    assert "marketing_opt_in" in any_body.splitlines()[0]


def test_the_json_rows_are_consent_gated_by_default_and_say_so(monkeypatch):
    import flask
    monkeypatch.setattr(wk, "_admin_ok", lambda: True)
    monkeypatch.setattr(wk, "_gather", lambda: (COHORT, {}))
    app = flask.Flask("warm-test")
    app.register_blueprint(wk.warm_key_cohort_bp)
    cl = app.test_client()
    j = cl.get("/api/v1/admin/audience/warm-keys").get_json()
    assert j["rows_consent_gated"] is True
    assert all(r["marketing_opt_in"] for r in j["rows"])
    assert "may be emailed" in j["rows_note"]
    j2 = cl.get("/api/v1/admin/audience/warm-keys?consent=any").get_json()
    assert j2["rows_consent_gated"] is False
    assert "analysis only" in j2["rows_note"]
    assert "do not send" in j2["rows_note"]


def test_consent_is_read_from_the_metadata_flag_not_inferred():
    """★ It is written ONLY by the double-opt-in confirm click. A derived
    consent — 'they bound an email, so they agreed' — is the exact error this
    endpoint must not make."""
    assert "metadata->>'marketing_opt_in' = 'true'" in COHORT_SQL
    assert "bool_or(" in COHORT_SQL, (
        "consent must be true for ANY of the person's keys")
    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "_sendable")
    body = ast.get_source_segment(SRC, fn) or ""
    assert '_mailable(r) and bool(r.get("marketing_opt_in"))' in body


def test_the_basis_names_consent_as_the_blocker():
    s = wk.summarize(COHORT)
    assert "REACHABILITY, never permission" in s["basis"]
    assert "double-opt-in" in s["basis"]
