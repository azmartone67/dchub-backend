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


def row(email, **kw):
    r = {"email": email, "tier": kw.get("tier", "identified"),
         "domain": wk._domain(email), "domain_kind": wk._domain_kind(email),
         "days_since_bind": kw.get("days", 10),
         "age_bucket": wk._bucket_days(kw.get("days", 10)),
         "keys_held": 1, "calls_after_bind": kw.get("calls", 0),
         "last_call": "", "top_tool_wall": kw.get("tool", ""),
         "wall_hits": kw.get("hits", 0),
         "already_paid": kw.get("paid", False),
         "suppressed": kw.get("supp", False)}
    return r


COHORT = [
    row("buyer@acmepower.com", calls=4, tool="analyze_site", hits=9),
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
    i = SRC.index("THE COHORT")
    seg = SRC[i:i + 1200]
    assert "api_key" not in seg.split("GROUP BY")[0], (
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
    i = SRC.index("THE COHORT")
    seg = SRC[i:i + 1200]
    assert "email IS NOT NULL AND email <> ''" in seg
    assert "position('@' in email) > 1" in seg


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
def test_the_paid_set_is_read_from_the_registry_not_typed():
    """★ A new paid tier must not silently start receiving upsell mail."""
    import tier_registry as tr
    paid = wk._paid_tiers()
    for t in (tr.paid_plans() or []):
        assert str(t).lower() in paid, f"{t} is paid and not excluded"
    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "_paid_tiers")
    body = ast.get_source_segment(SRC, fn) or ""
    assert "tier_registry" in body and "paid_plans" in body


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
