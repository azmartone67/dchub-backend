"""A demo listing is not supply.

MEASURED IN PRODUCTION, 2026-09-21. GET /api/v1/listings returned three rows:
one real Phoenix listing and the two DFW samples --
`sample-listing-dfw-40-mw-powered-shell-demo` (40 MW) and
`sample-listing-dfw-1-2-mw-colocation-demo` (1.2 MW). get_market_intel for
Dallas published them as

    "capacity_source": {"live_listings": 2, "mw": 41.2, ...}

under a note reading "Each listing is reached through a DC Hub deal
registration", and POST /api/v1/listings/<demo slug>/intro passed its status
gate -- all three rows carry status 'pocket', so status alone can never
separate a sample from the real listing beside it. A buyer could register a
deal against a site with no provider behind it to accept.

What these pin:
  * the two production demo slugs are not in the feed, its count or the MW the
    summary publishes -- the 41.2 MW total is the regression this file exists
    to catch;
  * a demo cannot be registered for and has no detail page, because
    _db_get_listing() reads by slug with NO liveness filter, so hiding demos
    from the catalogue alone would leave them registrable by a slug that was
    public on a teaser card;
  * detail.demo marks a demo whatever its slug is, so a backfill can retire
    the slug convention without touching the rule;
  * the SQL half and the Python half of the rule agree row for row;
  * the predicate carries no `%` and no " AND " -- it has two consumers that
    escape it oppositely (see _DEMO_EXCLUDE_SQL).

Storage is a stand-in for Postgres behind the module's own _fetch seam, in the
style of test_capacity_source_summary.py: it evaluates the WHERE clause the
module actually sends, so the exclusion is exercised from the SQL rather than
restated here.
"""
import json
import re
import sys
import types
from datetime import datetime, timezone

import pytest

pytest.importorskip("flask")
from flask import Flask  # noqa: E402

import routes.exclusive_listings as el  # noqa: E402

REAL = "phoenix-2-mw-colocation-available-now"
DEMO_SHELL = "sample-listing-dfw-40-mw-powered-shell-demo"
DEMO_COLO = "sample-listing-dfw-1-2-mw-colocation-demo"

_STATEMENT_RE = re.compile(
    r"^SELECT (?P<cols>.+?) FROM exclusive_listings WHERE (?P<where>.+?)"
    r"(?: ORDER BY updated_at DESC)?(?P<limit> LIMIT %s)?$")
_DETAIL_TEXT_RE = re.compile(r"^detail->>'(?P<key>[a-z_]+)'$")
_STATUS = "status IN ('public', 'pocket')"
_EXPIRY = "(expires_at IS NULL OR expires_at > NOW())"


def _demo_row(row):
    """el._DEMO_EXCLUDE_SQL evaluated against a row.

    Written from the SQL's own semantics -- detail->>'demo' as text, and the
    two LOWER(slug) tests -- rather than by calling el._is_demo_listing(), so
    the stand-in cannot agree with the production rule by construction.
    """
    flag = (row.get("detail") or {}).get("demo")
    flag = "" if flag is None else (flag if isinstance(flag, str) else json.dumps(flag))
    if flag.lower() in ("true", "t", "1", "yes"):
        return True
    slug = (row.get("slug") or "").lower()
    return slug.startswith("sample-listing-") or slug.endswith("-demo")


def _listing(slug, **over):
    row = {"id": abs(hash(slug)) % 100000, "slug": slug, "title": slug, "summary": None,
           "status": "pocket", "tier_required": "registered", "market": "Dallas-Fort Worth",
           "state": "TX", "country": "US", "latitude": None, "longitude": None,
           "capacity_mw": 10.0, "asking_price": None, "asking_currency": "USD",
           "detail": {"delivery_type": "powered_shell"}, "contact": None, "owner_id": None,
           "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
           "updated_at": datetime(2026, 9, 2, tzinfo=timezone.utc), "expires_at": None}
    row.update(over)
    return row


# The production catalogue as measured, one real listing and the DFW pair.
PRODUCTION = [
    _listing(REAL, market="Phoenix", state="AZ", capacity_mw=2.0,
             detail={"delivery_type": "colocation", "colocation": {"kw_available": 2000}}),
    _listing(DEMO_SHELL, capacity_mw=40.0),
    _listing(DEMO_COLO, capacity_mw=1.2,
             detail={"delivery_type": "colocation", "colocation": {"kw_available": 500}}),
]


class _Store:
    def __init__(self, rows):
        self.rows, self.statements = list(rows), []

    def fetch(self, sql, params, cols):
        statement = " ".join(sql.split())
        self.statements.append(statement)
        m = _STATEMENT_RE.match(statement)
        assert m, f"unexpected SQL: {statement[:200]}"
        rows = list(self.rows)
        for predicate in m.group("where").split(" AND "):
            if predicate == _STATUS:
                rows = [r for r in rows if r["status"] in ("public", "pocket")]
            elif predicate == _EXPIRY:
                now = datetime.now(timezone.utc)
                rows = [r for r in rows if r["expires_at"] is None or r["expires_at"] > now]
            elif predicate == el._DEMO_EXCLUDE_SQL:
                rows = [r for r in rows if not _demo_row(r)]
            elif predicate == "slug = %s":
                rows = [r for r in rows if r["slug"] == params[0]]
            elif predicate == "id = %s":
                rows = [r for r in rows if r["id"] == params[0]]
            else:
                raise AssertionError(f"unexpected predicate: {predicate}")
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        if m.group("limit"):
            rows = rows[:params[-1]]
        exprs = [e.strip() for e in m.group("cols").split(",")]
        if exprs == ["COUNT(*)"]:
            return [dict(zip(cols, (len(rows),)))]
        return [dict(zip(cols, (self._value(r, e) for e in exprs))) for r in rows]

    @staticmethod
    def _value(row, expr):
        text = _DETAIL_TEXT_RE.match(expr)
        if text:
            value = (row.get("detail") or {}).get(text.group("key"))
            return value if value is None or isinstance(value, str) else json.dumps(value)
        assert expr in row, f"unexpected column: {expr}"
        return row[expr]


def _env(monkeypatch, rows):
    monkeypatch.setitem(sys.modules, "main", types.ModuleType("main"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store = _Store(rows)
    monkeypatch.setattr(el, "_dsn", lambda: "postgresql://stand-in/listings")
    monkeypatch.setattr(el, "_fetch", store.fetch)
    monkeypatch.setattr(el, "_conn", lambda: (_ for _ in ()).throw(
        AssertionError("these tests reach storage only through _fetch")))
    monkeypatch.setattr(el, "_ensure_schema", lambda: None)
    el._SUMMARY_CACHE.update(at=None, value=None)
    app = Flask(__name__)
    app.register_blueprint(el.exclusive_listings_bp)
    store.client = app.test_client()
    return store


@pytest.fixture
def env(monkeypatch):
    store = _env(monkeypatch, PRODUCTION)
    yield store
    el._SUMMARY_CACHE.update(at=None, value=None)


# ── the 41.2 MW ───────────────────────────────────────────────────────────

def test_the_dfw_demo_pair_is_not_in_the_feed(env):
    r = env.client.get("/api/v1/listings")
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    assert [i["slug"] for i in j["items"]] == [REAL]
    assert j["count"] == 1


def test_the_summary_does_not_publish_the_demo_megawatts(env):
    r = env.client.get("/api/v1/listings/summary")
    assert r.status_code == 200, r.get_data(as_text=True)
    j = r.get_json()
    blob = json.dumps(j)
    # 41.2 is 40 + 1.2, the two demos; 43.2 would be the pair plus the real
    # listing. Neither may ever be published as live capacity again.
    # ISO timestamps are masked first: "generated_at": "…T07:27:41.210694…"
    # contains "41.2" by chance and failed CI at random (backend#5633 shard 2,
    # run 36226686561, 2026-09-26).
    figures = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
                     "<ts>", blob)
    assert "41.2" not in figures and "43.2" not in figures, blob
    assert j["live_count"] == 1, j
    assert j["total_mw"] == 2, j
    assert j["markets"] == [{"count": 1, "country": "US", "delivery_types": ["colocation"],
                             "market": "Phoenix", "mw": 2, "state": "AZ"}], j
    assert DEMO_SHELL not in blob and DEMO_COLO not in blob


# ── the registration gate ─────────────────────────────────────────────────

@pytest.mark.parametrize("slug", [DEMO_SHELL, DEMO_COLO])
def test_a_demo_listing_cannot_be_registered_for(env, slug):
    r = env.client.post(f"/api/v1/listings/{slug}/intro",
                        json={"name": "A Buyer", "email": "buyer@example.com",
                              "company": "Example Corp", "message": "50 MW please"})
    assert r.status_code == 404, r.get_data(as_text=True)
    assert r.get_json()["error"] == "not_found"


@pytest.mark.parametrize("slug", [DEMO_SHELL, DEMO_COLO])
def test_a_demo_listing_has_no_detail_page(env, slug):
    r = env.client.get(f"/api/v1/listings/{slug}")
    assert r.status_code == 404, r.get_data(as_text=True)


def test_the_real_listing_is_still_reachable(env):
    assert env.client.get(f"/api/v1/listings/{REAL}").status_code == 200


# ── the rule itself ───────────────────────────────────────────────────────

def test_the_detail_flag_marks_a_demo_whatever_its_slug(monkeypatch):
    """So a backfill can set detail.demo and retire the slug convention."""
    rows = [_listing("an-ordinary-looking-slug", detail={"demo": True}),
            _listing("another-ordinary-slug")]
    store = _env(monkeypatch, rows)
    j = store.client.get("/api/v1/listings").get_json()
    assert [i["slug"] for i in j["items"]] == ["another-ordinary-slug"]
    assert store.client.get("/api/v1/listings/an-ordinary-looking-slug").status_code == 404
    el._SUMMARY_CACHE.update(at=None, value=None)


@pytest.mark.parametrize("slug,detail,demo", [
    (DEMO_SHELL, None, True), (DEMO_COLO, None, True), (REAL, None, False),
    ("x-demo", None, True), ("SAMPLE-LISTING-UPPER", None, True),
    ("ordinary", {"demo": True}, True), ("ordinary", {"demo": "yes"}, True),
    ("ordinary", {"demo": False}, False), ("ordinary", {}, False), ("demonstrable", None, False),
])
def test_both_halves_of_the_rule_agree(slug, detail, demo):
    """The SQL hides it from the feed; the Python refuses the registration. A
    row the two disagree about is hidden from the catalogue and registrable
    through the slug anyway -- the exact hole this file closes."""
    row = _listing(slug, detail=detail)
    assert _demo_row(row) is demo
    assert el._is_demo_listing(row) is demo


def test_the_predicate_survives_both_escaping_paths():
    """_fetch() binds params so psycopg2 unescapes `%%`; the sitemap builder in
    main.py executes the joined clause with NO params, where `%%` survives
    literally. A predicate holding `%` is therefore wrong on one of the two.
    And callers split the joined clause on " AND " to recover predicates."""
    assert "%" not in el._DEMO_EXCLUDE_SQL
    assert " AND " not in el._DEMO_EXCLUDE_SQL
    assert el._DEMO_EXCLUDE_SQL in el._LIVE_WHERE
