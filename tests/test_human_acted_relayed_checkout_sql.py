"""human_acted's relayed-checkout lane, EXECUTED against a real Postgres.

tests/test_human_acted_v7_relayed_checkout.py and
tests/test_human_acted_v8_counts_the_relayed_checkout.py prove the lane's
session identity (routes/handoff_definition.RELAYED_CHECKOUT_SESSION_ID) is
spelled once and read by every consumer. Neither can say what it COUNTS:
coalesce/nullif over NULL and '', `is distinct from`, the union's DISTINCT and
the exclusion's anchored regex only mean something on a real Postgres, and
until this file the first real execution of that SQL was production.

★ The clicks are written by the REAL /go/c/<token> endpoint, from tokens minted
by routes/checkout_click_tracker.mint_checkout_token, into a click table created
in the shape it has live — before session_id — and upgraded by the tracker's
own _ensure_table(). So one run proves the ALTER lands on the live shape (and
gives up under a held lock instead of queueing), the INSERT stores the session
a three-field token carried, and the builders count exactly the clicks they
claim to. Only states that endpoint cannot produce are written or aged by hand.

  C1   bare session ref S1                       counts, as S1 (the v8 identity)
  C2   k- key ref, token session S2              counts, as S2
  C3   another k- key, token session S1          counts once with C1
  C4   pk- key, token session S3                 counts, as S3
  C5   the SAME pk- key, token session S4        counts, as S4: sessions, not keys
  C6   pk- key, no session                       out: the exclusion would be vacuous
  C7   a- anonymous ref, no session              out: same reason
  C8   bare session ref of a declared operator   excluded, on the fallback identity
  C9   k- key, token session of an operator      excluded, on the token identity
  C10  bare session ref, probe UA                out: real-UA predicate
  C11  no ref, no session                        out: no identity at all
  C12  bare session ref S7, aged out             out: window
  C13  junk token                                unsigned: stamped, never counted
  C14  a third k- key, token session S2          counts once with C2
  D1   bare session ref S5, sig_ok false         unsigned (by hand)
  D2   k- key, session S8, sig_ok NULL           unsigned: NULL groups with false

Beside it, the relay lane: R1, S2 and C8's operator session opened /upgrade/h/
on a real UA; S3 and S5 sit in the high-intent table having opened nothing.

Set RELAYED_CHECKOUT_SQL_DSN to run it. CI passes the db-parity service DSN and
then asserts this file did not skip — a skipped proof is not a proof.
"""
import ast
import hashlib
import os
import pathlib
import threading
import time

import pytest

psycopg2 = pytest.importorskip("psycopg2")
flask = pytest.importorskip("flask")

from mcp_calls_deloop import (  # noqa: E402
    real_ua_predicate,
    self_traffic_session_prefixes,
)
from routes import handoff_definition as H  # noqa: E402
from routes._stripe_links import STRIPE_LINKS  # noqa: E402

DSN = os.environ.get("RELAYED_CHECKOUT_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="RELAYED_CHECKOUT_SQL_DSN not set — no Postgres to run against")

REPO = pathlib.Path(__file__).resolve().parents[1]
IV = "30 days"
SECRET = "relayed-checkout-sql-test-key"
REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) "
           "Gecko/20100101 Firefox/129.0")
PROBE_UA = "curl/8.7.1"
PRICING = "https://dchub.cloud/pricing"


def _hex(label):
    return hashlib.sha256(label.encode()).hexdigest()


S1, S2, S3, S4, S5, S6, S7, S8, R1 = (
    "5e55%04x-0000-4000-8000-%012x" % (i, i) for i in range(1, 10))
# Operator sessions are BUILT from the declared prefixes, never typed, so the
# fixture follows the seed and cannot test an exclusion the code does not make.
_OPS = self_traffic_session_prefixes()
OP_A = _OPS[0] + "-0000-4000-8000-00000000000a"
OP_B = (_OPS[1] if len(_OPS) > 1 else _OPS[0]) + "-0000-4000-8000-00000000000b"
KA, KB, KF, KG, KH = ("k-" + _hex(x) for x in ("ka", "kb", "kf", "kg", "kh"))
PKC, PKD = ("pk-" + _hex(x) for x in ("pkc", "pkd"))
AE = "a-" + _hex("ae")[:24]

DDL = """
DROP TABLE IF EXISTS mcp_checkout_clicks;
DROP TABLE IF EXISTS relay_opens;
DROP TABLE IF EXISTS mcp_high_intent_sessions;
-- The click table as the tracker created it before 2026-09-13: NO session_id.
-- CREATE TABLE IF NOT EXISTS never alters an existing table, so this is the
-- shape the ALTER has to upgrade in production.
CREATE TABLE mcp_checkout_clicks (
    id           SERIAL PRIMARY KEY,
    clicked_at   TIMESTAMPTZ DEFAULT NOW(),
    plan         TEXT,
    ref          TEXT,
    ref_kind     TEXT,
    sig_ok       BOOLEAN DEFAULT TRUE,
    ip           TEXT,
    user_agent   TEXT,
    referrer     TEXT
);
CREATE INDEX ix_mcc_ts   ON mcp_checkout_clicks(clicked_at DESC);
CREATE INDEX ix_mcc_ref  ON mcp_checkout_clicks(ref, clicked_at DESC);
CREATE INDEX ix_mcc_plan ON mcp_checkout_clicks(plan, clicked_at DESC);
-- Only the columns the relay lane reads, with their production types.
CREATE TABLE mcp_high_intent_sessions (
    mcp_session_id             TEXT NOT NULL,
    first_hit_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    human_view_first_opened_at TIMESTAMPTZ,
    human_view_first_ua        TEXT
);
CREATE TABLE relay_opens (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ DEFAULT NOW(),
    session_id TEXT,
    user_agent TEXT,
    valid      BOOLEAN
);
"""

# (label, ref, token session, user agent); every one goes through /go/c/.
_ROUTED = [
    ("C1", S1, "", REAL_UA),
    ("C2", KA, S2, REAL_UA),
    ("C3", KB, S1, REAL_UA),
    ("C4", PKC, S3, REAL_UA),
    ("C5", PKC, S4, REAL_UA),
    ("C6", PKD, "", REAL_UA),
    ("C7", AE, "", REAL_UA),
    ("C8", OP_A, "", REAL_UA),
    ("C9", KF, OP_B, REAL_UA),
    ("C10", S6, "", PROBE_UA),
    ("C11", "", "", REAL_UA),
    ("C12", S7, "", REAL_UA),
    ("C14", KH, S2, REAL_UA),
]


def _column_present(cur):
    cur.execute("SELECT count(*) FROM information_schema.columns"
                " WHERE table_name = 'mcp_checkout_clicks'"
                " AND column_name = 'session_id'")
    return cur.fetchone()[0] == 1


def _endpoint_executors(cur):
    """The funnel's own `one` and `row`, read out of flask_mcp_endpoints.

    Parsed, never imported: the module opens a DB pool at import. `row` keys
    the published dict off cursor.description, so running the provenance
    builder through it proves the new subset reaches the payload by NAME, not
    merely that the SQL selects it.
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


def _hold_access_share(ready, seconds):
    """Hold the weakest lock a reader takes, for `seconds`, on its own session."""
    c = psycopg2.connect(DSN)
    try:
        with c.cursor() as cur:
            cur.execute("LOCK TABLE mcp_checkout_clicks IN ACCESS SHARE MODE")
            ready.set()
            time.sleep(seconds)
        c.commit()
    finally:
        c.close()


@pytest.fixture(scope="module")
def db():
    from routes import checkout_click_tracker as T

    for sid in (S1, S2, S3, S4, S5, S6, S7, S8, R1):
        assert not sid.lower().startswith(tuple(p.lower() for p in _OPS)), sid

    mp = pytest.MonkeyPatch()
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    try:
        mp.setenv("DATABASE_URL", DSN)
        mp.setenv("DCHUB_INTERNAL_KEY", SECRET)
        mp.setattr(T, "_SCHEMA_READY", [False])
        cur = conn.cursor()
        cur.execute(DDL)
        out = {"column_before": _column_present(cur)}

        # ── the ALTER, first under a held reader lock, then free ──────────
        ready = threading.Event()
        blocker = threading.Thread(target=_hold_access_share, args=(ready, 6))
        blocker.start()
        assert ready.wait(10), "the blocking session never took its lock"
        t0 = time.monotonic()
        out["ensure_blocked"] = T._ensure_table()
        out["ensure_blocked_s"] = time.monotonic() - t0
        out["column_after_blocked"] = _column_present(cur)
        blocker.join()
        out["ensure"] = T._ensure_table()
        out["ensure_again"] = T.ensure_schema()
        out["column_after"] = _column_present(cur)
        cur.execute("SELECT indexdef FROM pg_indexes"
                    " WHERE tablename = 'mcp_checkout_clicks'"
                    " AND indexname = 'ix_mcc_session'")
        out["index"] = [r[0] for r in cur.fetchall()]

        # ── clicks, through the real endpoint ──────────────────────────────
        app = flask.Flask("relayed-checkout-sql")
        app.register_blueprint(T.checkout_click_bp)
        client = app.test_client()
        out["location"] = {}
        for label, ref, sid, ua in _ROUTED:
            token = T.mint_checkout_token("metered", ref, sid)
            assert token, (label, ref, sid)
            r = client.get("/go/c/" + token, headers={"User-Agent": ua})
            out["location"][label] = (r.status_code, r.headers.get("Location"))
        r = client.get("/go/c/not-a-token", headers={"User-Agent": REAL_UA})
        out["location"]["C13"] = (r.status_code, r.headers.get("Location"))
        cur.execute("SELECT count(*) FROM mcp_checkout_clicks")
        out["routed_rows"] = cur.fetchone()[0]

        # ── states the endpoint cannot produce ─────────────────────────────
        cur.execute("UPDATE mcp_checkout_clicks"
                    " SET clicked_at = now() - interval '40 days' WHERE ref = %s",
                    (S7,))
        cur.execute(
            "INSERT INTO mcp_checkout_clicks"
            " (plan, ref, ref_kind, sig_ok, user_agent, session_id)"
            " VALUES ('metered', %s, 'session', false, %s, NULL),"
            "        ('metered', %s, 'sub_key', NULL, %s, %s)",
            (S5, REAL_UA, KG, REAL_UA, S8))

        # ── the relay lane ──────────────────────────────────────────────────
        for sid in (R1, S2, S3, S5, OP_A):
            cur.execute("INSERT INTO mcp_high_intent_sessions"
                        " (mcp_session_id, first_hit_at)"
                        " VALUES (%s, now() - interval '2 hours')", (sid,))
        for sid in (R1, S2, OP_A):
            cur.execute("INSERT INTO relay_opens (session_id, user_agent, valid)"
                        " VALUES (%s, %s, true)", (sid, REAL_UA))

        cur.execute("SELECT " + real_ua_predicate("v.ua")
                    + " FROM (VALUES (%s), (%s)) v(ua)", (REAL_UA, PROBE_UA))
        out["ua_verdicts"] = [r[0] for r in cur.fetchall()]

        cur.execute("SELECT ref, ref_kind, session_id, sig_ok FROM mcp_checkout_clicks")
        out["stored"] = {}
        for ref, kind, sid, ok in cur.fetchall():
            out["stored"].setdefault(ref, []).append((kind, sid, ok))

        # Executed raw FIRST so a SQL error surfaces with Postgres's message —
        # the endpoint's executors swallow errors into None.
        built = {
            "v7": H.human_acted_v7_count_sql(IV),
            "links": H.human_acted_v7_links_sql(IV),
            "v5": H.human_acted_v5_count_sql(IV),
            "v5_incl": H.human_acted_v5_count_sql(IV, include_self_traffic=True),
            "headline": H.human_acted_count_sql(IV),
            "headline_incl": H.human_acted_count_sql(IV, include_self_traffic=True),
        }
        for name, sql in built.items():
            cur.execute(sql)
            out[name] = cur.fetchone()[0]
        one, row = _endpoint_executors(cur)
        out["published"] = {name: one(sql) for name, sql in built.items()}
        out["provenance"] = row(H.relayed_checkout_provenance_sql(IV))
        cur.execute("SELECT s.mcp_session_id FROM mcp_high_intent_sessions s WHERE "
                    + H.human_acted_session_predicate("s"))
        out["acted"] = {r[0] for r in cur.fetchall()}
        cur.close()
        yield out
    finally:
        conn.close()
        mp.undo()


def test_the_fixture_user_agents_split_where_the_predicate_does(db):
    assert db["ua_verdicts"] == [True, False]


def test_the_tracker_adds_session_id_to_the_live_table_shape(db):
    assert db["column_before"] is False
    assert db["ensure"] is True and db["column_after"] is True
    assert db["ensure_again"] is True
    assert len(db["index"]) == 1 and "(session_id, clicked_at DESC)" in db["index"][0]


def test_a_held_lock_makes_the_alter_give_up_instead_of_queueing(db):
    """★ A reader held the table for 6s. The ALTER must fail soft inside its
    lock_timeout and leave the column unconfirmed — not wait the reader out,
    which is what an ALTER with no effective timeout does, with every /go/c/
    INSERT queued behind it."""
    assert db["ensure_blocked"] is False
    assert db["column_after_blocked"] is False
    assert db["ensure_blocked_s"] < 5, db["ensure_blocked_s"]


def test_the_endpoint_stores_the_session_a_three_field_token_carried(db):
    assert db["routed_rows"] == len(_ROUTED) + 1
    s = db["stored"]
    assert s[KA] == [("sub_key", S2, True)]
    assert sorted(s[PKC]) == [("pack_key", S3, True), ("pack_key", S4, True)]
    assert s[S1] == [("session", None, True)]
    assert s[PKD] == [("pack_key", None, True)]
    assert s[AE] == [("anon", None, True)]
    # C11 (minted with no ref) and C13 (a junk token) both stamp ref ''.
    assert sorted(s[""], key=lambda r: r[2]) == [("none", None, False),
                                                 ("none", None, True)]


def test_stripe_is_handed_the_ref_and_never_the_session(db):
    link = STRIPE_LINKS["metered"]
    sep = "&" if "?" in link else "?"
    loc = db["location"]
    assert loc["C2"] == (302, link + sep + "client_reference_id=" + KA)
    assert loc["C1"] == (302, link + sep + "client_reference_id=" + S1)
    assert loc["C11"] == (302, link)
    assert loc["C13"] == (302, PRICING)
    for label, (_code, location) in loc.items():
        for sid in (S2, S3, S4, OP_B):
            assert sid not in (location or ""), (label, location)


def test_v7_counts_each_session_once_and_only_where_the_exclusion_can_bind(db):
    # S1 (C1, C3) · S2 (C2, C14) · S3 (C4) · S4 (C5). Operator sessions OP_A
    # (bare ref) and OP_B (token session) are excluded; C6/C7/C11 have no
    # session identity; C10 is a probe; C12 is outside the window; C13, D1 and
    # D2 are unsigned.
    assert db["v7"] == 4


def test_the_links_ceiling_still_counts_distinct_refs(db):
    # S1 KA KB PKC PKD AE OP_A KF KH: one PKC across two sessions is one link.
    assert db["links"] == 9


def test_the_headline_unions_both_lanes_on_the_session(db):
    assert (db["v5"], db["v5_incl"]) == (2, 3)                 # {R1,S2} (+OP_A)
    assert db["headline"] == 5                                 # {R1,S1,S2,S3,S4}
    assert db["headline_incl"] == 7                            # + OP_A, OP_B
    assert db["headline_incl"] - db["headline"] == 2


def test_the_endpoint_publishes_exactly_what_the_builders_count(db):
    for name in ("v7", "links", "v5", "v5_incl", "headline", "headline_incl"):
        assert db["published"][name] == db[name], name


def test_the_provenance_block_partitions_and_publishes_every_subset(db):
    p = db["provenance"]
    assert p == {
        "total": 15,
        "probe_ua": 1,                                   # C10
        "unsigned_clicks": 3,                            # C13, D1, D2
        "minted_link_clicks": 11,                        # C1-C9, C11, C14
        "minted_link_clicks_deloopable": 8,              # C1-C5, C8, C9, C14
        "minted_link_clicks_no_ref": 1,                  # C11
        "minted_link_clicks_session_from_token": 6,      # C2-C5, C9, C14
    }, p
    assert p["probe_ua"] + p["unsigned_clicks"] + p["minted_link_clicks"] == p["total"]
    assert (p["minted_link_clicks_session_from_token"]
            <= p["minted_link_clicks_deloopable"] <= p["minted_link_clicks"])


def test_a_session_bound_only_by_its_token_did_not_abandon(db):
    """S3's only act is C4, a key click carrying S3 as its token session. S5's
    only click is unsigned."""
    assert db["acted"] == {R1, S2, S3, OP_A}
