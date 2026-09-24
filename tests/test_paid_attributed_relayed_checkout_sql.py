"""paid_attributed's relayed-checkout lane, EXECUTED against a real Postgres.

r-paid-join (2026-09-14). routes/handoff_definition joins a paid Checkout
Session to the /go/c/ click that sold it (client_reference_id = ref) and
counts the session that click carried.
tests/test_paid_attributed_joins_relayed_checkout.py pins the composition;
only a real Postgres shows what the SQL COUNTS: the latest-click pick, the
lookback, livemode, the union's DISTINCT and the exclusion's anchored regex.

★ Clicks go through the REAL /go/c/<token> endpoint from tokens minted by
routes/checkout_click_tracker.mint_checkout_token. Payments go through the
REAL webhook writer, routes/checkout_payment_refs.record_checkout_payment,
fed Checkout-Session-shaped dicts. The v1 pack row goes through
routes/mcp_conversion_plays.grant_credit_pack. Only states those writers
cannot produce are written or aged by hand.

  P1   pk- pack click, session S1; paid                counts, as S1
  P2   k- Pro click, session S2; paid                  counts, as S2
  P3   one pk- key clicked from S3, later from S4      counts ONCE, as S4
  P4   bare session ref S5; paid; a Fix E row for S5   counts once across lanes
  P5   pk- click with no session; paid                 counts, as the ref itself (2026-09-23)
  P6   a- anonymous ref; paid                          out: same reason
  P7   k- click from a declared operator session       excluded; in incl_self
  P8   the click came AFTER the payment                out: it did not sell it
  P9   the click is older than the lookback            out
  P10  livemode false                                  out
  P11  payment_status unpaid                           never recorded
  P12  probe-UA click; paid                            out: real-UA predicate
  P13  P1 delivered again                              one row (idempotent)
  P14  payment outside the window                      out
  P16  unsigned click (by hand); paid                  out: sig_ok
  P17  k- sub_key click with no session; paid          counts, as the ref itself (2026-09-23)
  V1   pack granted to session S15 (mcp_topups)        counts (v1 lane)
  P18  pk- click, stranger session; OPERATOR paid      excluded (v4 payer); in incl_self
       (email written Az.Martone+qa@GoogleMail.com: normalisation, not the literal)
  P19  pk- click, no session; operator in caller_id    excluded (v4 payer); in incl_self
  V2   pack granted to S20; operator paid              excluded from the v1 lane too
  P21  pk- click, stranger session; stranger paid      counts
  P22  pk- click on a dchub.cloud page; paid           out: a website sale, not a relay
                                                       (r-site-gate-clicks, 2026-09-24)

Set PAID_ATTRIBUTED_SQL_DSN to run it. CI passes the db-parity service DSN
and then asserts this file did not skip.
"""
import ast
import hashlib
import os
import pathlib

import pytest

psycopg2 = pytest.importorskip("psycopg2")
flask = pytest.importorskip("flask")

from routes._audience_identity import (  # noqa: E402
    normalize_email,
    normalized_email_sql,
    operator_emails,
)
from mcp_calls_deloop import (  # noqa: E402
    real_ua_predicate,
    self_traffic_session_prefixes,
)
from routes import handoff_definition as H  # noqa: E402

DSN = os.environ.get("PAID_ATTRIBUTED_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="PAID_ATTRIBUTED_SQL_DSN not set — no Postgres to run against")

REPO = pathlib.Path(__file__).resolve().parents[1]
IV = "30 days"
SECRET = "paid-attributed-sql-test-key"
REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) "
           "Gecko/20100101 Firefox/129.0")
PROBE_UA = "curl/8.7.1"


def _hex(label):
    return hashlib.sha256(label.encode()).hexdigest()


S1, S2, S3, S4, S5, S8, S9, S10, S12, S14, S15, S16, S18, S20, S21, S22 = (
    "5a1d%04x-0000-4000-8000-%012x" % (i, i)
    for i in (1, 2, 3, 4, 5, 8, 9, 10, 12, 14, 15, 16, 18, 20, 21, 22))
# The operator session is BUILT from the declared prefixes, never typed.
_OPS = self_traffic_session_prefixes()
OP = _OPS[0] + "-0000-4000-8000-0000000000aa"
PK1, PK3, PK5, PK18, PK19, PK21, PK22 = (
    "pk-" + _hex(x) for x in ("pk1", "pk3", "pk5", "pk18", "pk19", "pk21", "pk22"))
K2, K7, K8, K9, K10, K12, K14, K16, K17 = (
    "k-" + _hex(x) for x in ("k2", "k7", "k8", "k9", "k10", "k12", "k14", "k16", "k17"))
AE6 = "a-" + _hex("ae6")[:24]

DDL = """
DROP TABLE IF EXISTS mcp_checkout_clicks, mcp_checkout_payments,
                     mcp_session_upgrades, mcp_topups, mcp_trial_emails,
                     mcp_conversions CASCADE;
-- The payer columns v4's operator check reads; the live names (2026-09-24).
CREATE TABLE mcp_conversions (
    id                 BIGSERIAL PRIMARY KEY,
    caller_id          TEXT,
    user_email         TEXT,
    stripe_session_id  TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- main.py creates this inline in the webhook (Fix E); the live shape.
CREATE TABLE mcp_session_upgrades (
    mcp_session_id     TEXT PRIMARY KEY,
    user_id            TEXT,
    user_email         TEXT,
    plan               TEXT,
    stripe_session_id  TEXT,
    stripe_customer_id TEXT,
    amount_cents       INTEGER,
    upgraded_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_at        TIMESTAMPTZ
);
"""

# (label, plan, ref, token session, user agent); every one goes through /go/c/.
_CLICKS = [
    ("P1", "metered", PK1, S1, REAL_UA),
    ("P2", "pro", K2, S2, REAL_UA),
    ("P3a", "metered", PK3, S3, REAL_UA),
    ("P3b", "metered", PK3, S4, REAL_UA),
    ("P4", "pro", S5, "", REAL_UA),
    ("P5", "metered", PK5, "", REAL_UA),
    ("P6", "metered", AE6, "", REAL_UA),
    ("P7", "pro", K7, OP, REAL_UA),
    ("P8", "pro", K8, S8, REAL_UA),
    ("P9", "pro", K9, S9, REAL_UA),
    ("P10", "pro", K10, S10, REAL_UA),
    ("P12", "pro", K12, S12, PROBE_UA),
    ("P14", "pro", K14, S14, REAL_UA),
    ("P17", "pro", K17, "", REAL_UA),
    ("P18", "metered", PK18, S18, REAL_UA),
    ("P19", "metered", PK19, "", REAL_UA),
    ("P21", "metered", PK21, S21, REAL_UA),
    ("P22", "metered", PK22, S22, REAL_UA, "https://dchub.cloud/land-power-map"),
]

# The OPERATOR address is built from operator_emails(), never typed, then
# disguised the ways normalize_email undoes (case, dots, plus-tag, alias).
_OP_EMAIL = sorted(operator_emails())[0]
_OP_LOCAL, _OP_DOMAIN = _OP_EMAIL.split("@")
_OP_DISGUISED = ((_OP_LOCAL[:2] + "." + _OP_LOCAL[2:]).title() + "+qa@"
                 + ("GoogleMail.com" if _OP_DOMAIN == "gmail.com" else _OP_DOMAIN.upper()))
# (stripe session, user_email, caller_id)
_CONVERSIONS = [
    ("cs_test_p18", _OP_DISGUISED, "5a1d-anon-hash"),
    ("cs_test_p19", None, _OP_EMAIL),
    ("cs_test_v2", _OP_EMAIL, None),
    ("cs_test_p21", "buyer@example.org", "buyer@example.org"),
    ("cs_test_p22", "site-buyer@example.org", "site-buyer@example.org"),
]


def _checkout(cs_id, cref, mode="payment", status="paid", livemode=True, amount=1000):
    """The fields of a checkout.session.completed data.object the writer reads."""
    return {"id": cs_id, "object": "checkout.session", "client_reference_id": cref,
            "mode": mode, "payment_status": status, "livemode": livemode,
            "amount_subtotal": amount, "amount_total": amount, "currency": "usd"}


_PAYMENTS = [
    ("P1", _checkout("cs_test_p1", PK1)),
    ("P2", _checkout("cs_test_p2", K2, mode="subscription", amount=9900)),
    ("P3", _checkout("cs_test_p3", PK3)),
    ("P4", _checkout("cs_test_p4", S5, mode="subscription", amount=9900)),
    ("P5", _checkout("cs_test_p5", PK5)),
    ("P6", _checkout("cs_test_p6", AE6)),
    ("P7", _checkout("cs_test_p7", K7, mode="subscription", amount=9900)),
    ("P8", _checkout("cs_test_p8", K8, mode="subscription", amount=9900)),
    ("P9", _checkout("cs_test_p9", K9, mode="subscription", amount=9900)),
    ("P10", _checkout("cs_test_p10", K10, mode="subscription", amount=9900, livemode=False)),
    ("P11", _checkout("cs_test_p11", K2, status="unpaid")),
    ("P12", _checkout("cs_test_p12", K12, mode="subscription", amount=9900)),
    ("P13", _checkout("cs_test_p1", PK1)),
    ("P14", _checkout("cs_test_p14", K14, mode="subscription", amount=9900)),
    ("P16", _checkout("cs_test_p16", K16, mode="subscription", amount=9900)),
    ("P17", _checkout("cs_test_p17", K17, mode="subscription", amount=9900)),
    ("P18", _checkout("cs_test_p18", PK18)),
    ("P19", _checkout("cs_test_p19", PK19)),
    ("P21", _checkout("cs_test_p21", PK21)),
    ("P22", _checkout("cs_test_p22", PK22)),
]


def _endpoint_executors(cur):
    """The funnel's own `one` and `row`, read out of flask_mcp_endpoints.

    Parsed, never imported: the module opens a DB pool at import.
    """
    tree = ast.parse((REPO / "flask_mcp_endpoints.py").read_text(encoding="utf-8"))
    funnel = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "handoff_funnel")
    win = next(n for n in funnel.body
               if isinstance(n, ast.FunctionDef) and n.name == "_win")
    defs = [n for n in win.body
            if isinstance(n, ast.FunctionDef) and n.name in ("one", "row")]
    assert sorted(d.name for d in defs) == ["one", "row"], [d.name for d in defs]
    ns = {"cur": cur}
    exec(compile(ast.Module(body=defs, type_ignores=[]),
                 "flask_mcp_endpoints.py", "exec"), ns)
    return ns["one"], ns["row"]


@pytest.fixture(scope="module")
def db():
    from routes import checkout_click_tracker as T
    from routes import checkout_payment_refs as CPR
    import routes.mcp_conversion_plays as mcp

    for sid in (S1, S2, S3, S4, S5, S8, S9, S10, S12, S14, S15, S16, S18, S20, S21, S22):
        assert not sid.lower().startswith(tuple(p.lower() for p in _OPS)), sid

    mp = pytest.MonkeyPatch()
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    try:
        mp.setenv("DATABASE_URL", DSN)
        mp.setenv("DCHUB_INTERNAL_KEY", SECRET)
        mp.setattr(T, "_SCHEMA_READY", [False])
        mp.setattr(CPR, "_SCHEMA_READY", [False])
        mp.setattr(mcp, "_conn", lambda: psycopg2.connect(DSN))
        cur = conn.cursor()
        cur.execute(DDL)
        out = {"tracker_schema": T._ensure_table(),
               "payments_schema": CPR.ensure_schema(),
               "payments_schema_again": CPR.ensure_schema(),
               "topups_schema": mcp.init_schema()}

        # ── clicks, through the real endpoint ──────────────────────────────
        app = flask.Flask("paid-attributed-sql")
        app.register_blueprint(T.checkout_click_bp)
        client = app.test_client()
        out["location"] = {}
        for label, plan, ref, sid, ua, *referer in _CLICKS:
            token = T.mint_checkout_token(plan, ref, sid)
            assert token, (label, ref, sid)
            headers = {"User-Agent": ua}
            if referer:
                headers["Referer"] = referer[0]
            r = client.get("/go/c/" + token, headers=headers)
            out["location"][label] = (r.status_code, r.headers.get("Location"))
        # States the endpoint cannot produce: an unsigned click, and ages.
        cur.execute("INSERT INTO mcp_checkout_clicks"
                    " (plan, ref, ref_kind, sig_ok, user_agent, session_id)"
                    " VALUES ('pro', %s, 'sub_key', false, %s, %s)", (K16, REAL_UA, S16))
        for ref, sid, age in ((PK3, S3, "2 hours"), (PK3, S4, "1 hour"),
                              (K9, S9, "8 days"), (K14, S14, "41 days")):
            cur.execute("UPDATE mcp_checkout_clicks SET clicked_at = now() - interval %s"
                        " WHERE ref = %s AND session_id = %s", (age, ref, sid))

        # ── payments, through the real webhook writer ──────────────────────
        out["recorded"] = {label: CPR.record_checkout_payment(s) for label, s in _PAYMENTS}
        # P8 was paid three hours before its click; P14 forty days ago.
        cur.execute("UPDATE mcp_checkout_payments SET paid_at = now() - interval '3 hours'"
                    " WHERE stripe_session_id = 'cs_test_p8'")
        cur.execute("UPDATE mcp_checkout_payments SET paid_at = now() - interval '40 days'"
                    " WHERE stripe_session_id = 'cs_test_p14'")

        # ── the v1 lanes ────────────────────────────────────────────────────
        # Fix E writes this row inline in main.py's webhook; by hand here.
        cur.execute("INSERT INTO mcp_session_upgrades (mcp_session_id, plan,"
                    " stripe_session_id, amount_cents) VALUES (%s, 'pro', 'cs_test_p4', 9900) ON CONFLICT DO NOTHING",
                    (S5,))
        out["grant"] = mcp.grant_credit_pack(
            "test-key-paid-attributed-v1", S15, 1000,
            stripe_session_id="cs_test_v1", source="pack10", price_cents=1000)
        out["grant_v2"] = mcp.grant_credit_pack(
            "test-key-paid-attributed-v2", S20, 1000,
            stripe_session_id="cs_test_v2", source="pack10", price_cents=1000)
        cur.executemany("INSERT INTO mcp_conversions (stripe_session_id, user_email,"
                        " caller_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", _CONVERSIONS)

        # normalize_email vs normalized_email_sql, row by row.
        cases = [_OP_EMAIL, _OP_DISGUISED, "  A.B+x@GoogleMail.com ", "a.b@gmail.com",
                 "A.B+tag@Example.org", "a.b@example.org", "x@y@gmail.com",
                 "no-at-sign", "", None, "Foo+@gmail.com", "f.o.o@googlemail.com"]
        cur.execute("SELECT v.e, " + normalized_email_sql("v.e")
                    + " FROM unnest(%s::text[]) WITH ORDINALITY v(e, n) ORDER BY n",
                    (cases,))
        out["normalized"] = [(e, got, normalize_email(e)) for e, got in cur.fetchall()]

        cur.execute("SELECT " + real_ua_predicate("v.ua")
                    + " FROM (VALUES (%s), (%s)) v(ua)", (REAL_UA, PROBE_UA))
        out["ua_verdicts"] = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT stripe_session_id, client_reference_id, ref_kind, mode,"
                    " amount_total, livemode FROM mcp_checkout_payments")
        out["stored"] = {r[0]: r[1:] for r in cur.fetchall()}

        # Executed raw FIRST so a SQL error surfaces with Postgres's message —
        # the endpoint's executors swallow errors into None.
        built = {
            "headline": H.paid_attributed_count_sql(IV),
            "headline_incl": H.paid_attributed_count_sql(IV, include_self_traffic=True),
            "relayed": H.paid_relayed_checkout_count_sql(IV),
            "relayed_incl": H.paid_relayed_checkout_count_sql(IV, include_self_traffic=True),
            "v1": H.paid_attributed_v1_sql(IV),
        }
        for name, sql in built.items():
            cur.execute(sql)
            out[name] = cur.fetchone()[0]
        cur.execute(H.relayed_checkout_payments_sql(IV))
        out["payments_raw"] = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        cur.execute("SELECT pay.stripe_session_id, " + H.paid_relayed_click_session_sql()
                    + " FROM mcp_checkout_payments pay")
        out["attributed_to"] = dict(cur.fetchall())
        one, row = _endpoint_executors(cur)
        out["published"] = {name: one(sql) for name, sql in built.items()}
        out["payments"] = row(H.relayed_checkout_payments_sql(IV))
        cur.close()
        yield out
    finally:
        conn.close()
        mp.undo()


def test_the_fixture_user_agents_split_where_the_predicate_does(db):
    assert db["ua_verdicts"] == [True, False]


def test_the_schemas_the_lanes_read_exist(db):
    assert db["tracker_schema"] is True
    assert db["payments_schema"] is True and db["payments_schema_again"] is True
    assert db["topups_schema"] is True
    assert db["grant"]["ok"] is True
    assert db["grant_v2"]["ok"] is True


def test_sql_email_normalisation_matches_python(db):
    bad = [(e, got, want) for e, got, want in db["normalized"] if got != want]
    assert not bad, bad
    assert db["normalized"][1][1] == _OP_EMAIL, (
        "the disguised operator address must normalise to the operator's")


def test_every_click_went_through_the_endpoint_to_stripe(db):
    for label, (status, location) in db["location"].items():
        assert status == 302, label
        assert "client_reference_id=" in (location or ""), (label, location)


def test_the_writer_records_paid_sessions_once(db):
    rec = db["recorded"]
    assert rec["P11"] == {"ok": False, "skipped": "not_paid"}
    assert rec["P1"]["ok"] and rec["P1"]["idempotent"] is False
    assert rec["P13"]["ok"] and rec["P13"]["idempotent"] is True
    assert "cs_test_p11" not in db["stored"]
    assert len(db["stored"]) == 18
    assert db["stored"]["cs_test_p1"] == (PK1, "pack_key", "payment", 1000, True)
    assert db["stored"]["cs_test_p2"][:2] == (K2, "sub_key")
    assert db["stored"]["cs_test_p4"][:2] == (S5, "session")
    assert db["stored"]["cs_test_p6"][:2] == (AE6, "anon")
    assert db["stored"]["cs_test_p10"][-1] is False


def test_each_payment_is_attributed_to_the_latest_qualifying_click(db):
    got = db["attributed_to"]
    assert got["cs_test_p1"] == S1
    assert got["cs_test_p2"] == S2
    assert got["cs_test_p3"] == S4          # the later of the two sessions
    assert got["cs_test_p4"] == S5          # a bare session ref is its own session
    assert got["cs_test_p7"] == OP          # attributed; the exclusion is the headline's job
    # r-paid-attributed-keyed-refs (2026-09-23): a pack_key/sub_key click with
    # NO session now attributes to the ref itself, not None.
    assert got["cs_test_p5"] == PK5
    assert got["cs_test_p17"] == K17
    # v4: attribution is unchanged; the operator exclusion is the headline's job.
    assert got["cs_test_p18"] == S18
    assert got["cs_test_p19"] == PK19
    assert got["cs_test_p21"] == S21
    for cs in ("cs_test_p6",                # anon ref: still deliberately out
               "cs_test_p8",                # click after payment
               "cs_test_p9",                # click outside the lookback
               "cs_test_p12",               # probe UA
               "cs_test_p16",               # unsigned
               "cs_test_p22"):              # pressed on dchub.cloud: a website sale
        assert got[cs] is None, cs


def test_the_headline_counts_each_paying_session_once(db):
    # S1, S2, S4 and S5 from the relayed lane (S5 also has its Fix E row),
    # S15 from the pack grant, plus PK5 and K17 since 2026-09-23 (session-less
    # pack_key/sub_key refs now attribute to the ref itself). OP is excluded;
    # P10 is not live; P14 is old.
    # v4 (2026-09-24): P18, P19 and V2 were paid by an operator mailbox and
    # leave the headline (no session prefix could have caught P19 or S18/S20);
    # the stranger P21 counts.
    assert db["headline"] == 8
    assert db["headline_incl"] == 12
    assert db["relayed"] == 7
    assert db["relayed_incl"] == 10


def test_v1_could_not_see_a_keyed_purchase(db):
    """The figure v1 published over the same rows: S5's upgrade row plus S15's
    pack, plus S20's (V2) — v1 had no exclusion, so the operator's pack counts
    there. None of the key-bound purchases (P1, P2, P3) reaches it."""
    assert db["v1"] == 3


def test_the_ceiling_and_the_attributable_subset(db):
    assert db["payments_raw"] == {
        "payments": 16,                    # P10 not live, P11 unrecorded, P13 dup, P14 old
        "matched_a_relayed_click": 12,     # not P8 (after), P12 (probe), P16 (unsigned),
                                           # P22 (pressed on dchub.cloud)
        "attributable_to_a_session": 10,   # P1 P2 P3 P4 P5 P7 P17 P18 P19 P21
    }


def test_the_endpoints_executors_publish_what_the_sql_counts(db):
    for name in ("headline", "headline_incl", "relayed", "relayed_incl", "v1"):
        assert db["published"][name] == db[name], name
    assert db["payments"] == db["payments_raw"]
