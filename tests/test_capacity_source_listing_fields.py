"""Capacity Source listing fields: the reserved `detail` keys an admin writes,
the typed fields the teaser and the full view project from them, and the
delivery_type / available_by feed filters.

Exercised as requests against the real blueprint, the way
test_pocket_listings_wall_and_leads.py does it: `main` is stubbed empty so the
tier resolver's optional imports fail the way they do without a database,
identity is a signed JWT, reads sit behind the module's own _db_* seams, and
admin writes run the routes' real INSERT / UPDATE against a stand-in cursor
that fails on any statement it does not expect. The clock the field rules read
(`_now`) is pinned, so freshness boundaries do not move with the day the suite
runs.

What these pin:
  * a valid listing round-trips from create to teaser to full view, stored
    normalized: trimmed text, JSON numbers, mw_schedule sorted by date;
  * each rule refuses with the field it names and writes nothing, including a
    detail sent as a JSON string and a PATCH or PUT that replaces detail;
  * a listing without the reserved keys stores and reads as before;
  * freshness at 30, 31, 90 and 91 days, and a missing, invalid or
    future-dated verification reading unverified;
  * an undisclosed provider name appears in no feed, teaser or full view,
    generic detail included;
  * colocation is written and read only on a listing whose delivery_type is
    colocation, and only the full view carries it: never the teaser, the feed
    or the generic detail;
  * available falls back to the earliest date of a valid mw_schedule, as
    stored, and an explicit available, available_date, energization or
    delivery wins;
  * a malformed filter is refused before any query, and valid filters reach
    the SQL as bound parameters.
"""
import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("flask")
import jwt  # noqa: E402 — PyJWT is a runtime dependency (routes/auth_routes.py)
from flask import Flask  # noqa: E402

import routes.exclusive_listings as el  # noqa: E402

JWT_SECRET = "capacity-source-fields-test-secret-0123456789"  # secretscan:allow (test placeholder)
INTERNAL_KEY = "internal-gateway-key-for-field-tests"  # secretscan:allow (test placeholder)
ADMIN_KEY = "admin-key-for-field-tests-0123456789"  # secretscan:allow (test placeholder)
ADMIN = {"X-Admin-Key": ADMIN_KEY}
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
HIDDEN_PROVIDER = "Quiet Harbor Infrastructure Partners"

VERIFICATION = {"verified_by": "DC Hub listings desk", "verified_at": "2026-09-01",
                "method": "site_visit"}

# What an admin might send: untrimmed text, numbers as strings, dates out of order.
VALID_DETAIL = {
    "available": "Q2 2027",
    "delivery_type": " powered_shell ",
    "mw_schedule": [{"date": "2028-01", "mw": 60}, {"date": " 2027-06-15 ", "mw": "20"},
                    {"date": "2027-09", "mw": 40.0}],
    "power": {"utility": "  Oncor   Electric ", "substation": "Seagoville 345kV",
              "interconnection_stage": "agreement_executed"},
    "price": {"low": 120, "high": "145.5", "unit": "usd_per_kw_month"},
    "provider": {"name": " Lone Star Data Partners ", "disclosed": True},
    "verification": {"verified_by": "DC Hub listings desk",
                     "verified_at": "2026-09-01T09:30:00-05:00", "method": "document_review"},
}
NORMALIZED = {
    "delivery_type": "powered_shell",
    "mw_schedule": [{"date": "2027-06-15", "mw": 20}, {"date": "2027-09", "mw": 40},
                    {"date": "2028-01", "mw": 60}],
    "power": {"utility": "Oncor Electric", "substation": "Seagoville 345kV",
              "interconnection_stage": "agreement_executed"},
    "price": {"low": 120, "high": 145.5, "unit": "usd_per_kw_month"},
    "provider": {"name": "Lone Star Data Partners", "disclosed": True},
    "verification": {"verified_by": "DC Hub listings desk",
                     "verified_at": "2026-09-01T14:30:00.000000+00:00",
                     "method": "document_review"},
}

# The keys the teaser and the full view carried before the reserved fields existed.
TEASER_KEYS = {"id", "slug", "title", "summary", "status", "access_required", "locked",
               "lock_reason", "market", "state", "country", "capacity_mw", "available",
               "created_at", "updated_at", "expires_at", "url"}
FULL_KEYS = TEASER_KEYS | {"latitude", "longitude", "asking_price", "asking_currency", "detail"}
# 2026-09-15: search by size and location adds capacity_kw and region, and
# co-marketing adds update_cadence, to every teaser.
# 2026-09-16: the two facts a headline capacity hides — the largest single
# contiguous block and the smallest chunk the provider will contract — are
# teaser-level too, because they decide whether a listing fits at all.
NEW_TEASER_KEYS = {"delivery_type", "freshness", "provider", "capacity_kw", "region",
                   "update_cadence", "contiguous_kw", "min_contract_kw"}
NEW_FULL_KEYS = NEW_TEASER_KEYS | {"colocation", "mw_schedule", "power", "price", "verification"}
NO_CADENCE = {"update_cadence": None, "next_update_due": None, "overdue": False}
UNVERIFIED = {"state": "unverified", "verified_at": None, "age_days": None, **NO_CADENCE}


def _decode_jsonb(values):
    """What Postgres hands back for the ::jsonb parameters: parsed JSON."""
    for key in ("detail", "contact"):
        if isinstance(values.get(key), str):
            values[key] = json.loads(values[key])
    return values


class _Cursor:
    """Runs the admin routes' INSERT and UPDATE against env.listings; any other
    statement fails the test."""

    def __init__(self, env):
        self.env, self._one = env, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.env.statements.append(s)
        if s.startswith("INSERT INTO exclusive_listings ("):
            cols = s[s.index("(") + 1:s.index(")")].split(", ")
            row = _decode_jsonb(dict(zip(cols, params, strict=True)))
            row.update(id=len(self.env.listings) + 1, created_at=NOW, updated_at=NOW)
            self.env.listings.append(row)
            self._one = (row["id"], row["slug"])
        elif s.startswith("UPDATE exclusive_listings SET "):
            head = "UPDATE exclusive_listings SET "
            assignments = s[len(head):s.index(" WHERE id = %s")].split(", ")
            cols = [a.split(" = ")[0] for a in assignments if "%s" in a]
            *values, lid = params
            row = next((r for r in self.env.listings if r["id"] == lid), None)
            if row is not None:
                row.update(_decode_jsonb(dict(zip(cols, values, strict=True))), updated_at=NOW)
            self._one = (row["id"], row["slug"], row["status"], row["tier_required"]) if row else None
        else:
            raise AssertionError(f"unexpected SQL: {s[:90]}")

    def fetchone(self):
        return self._one


class _Conn:
    def __init__(self, env):
        self.env = env

    def cursor(self):
        return _Cursor(self.env)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _row(detail=None, **over):
    """A stored listing, written straight to the stand-in store (no validation),
    the way a row written before the field rules looks."""
    row = {"id": 1, "slug": "dfw-40", "title": "Powered shell — DFW",
           "summary": "Energized next year.", "status": "pocket",
           "tier_required": "registered", "market": "Dallas", "state": "TX",
           "country": "US", "latitude": 32.776712, "longitude": -96.797012,
           "capacity_mw": 40.0, "asking_price": 1250000, "asking_currency": "USD",
           "detail": detail, "contact": None, "owner_id": None,
           "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
           "updated_at": datetime(2026, 9, 2, tzinfo=timezone.utc), "expires_at": None}
    row.update(over)
    return row


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setitem(sys.modules, "main", types.ModuleType("main"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for var in ("DCHUB_LEAD_LEDGER_SECRET", "DCHUB_SYNC_KEY", "INTERNAL_WORKER_SECRET",
                "ADMIN_INBOX_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", INTERNAL_KEY)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN_KEY)

    e = types.SimpleNamespace(listings=[], statements=[], list_calls=[], live_count=None)

    def list_listings(**kw):
        e.list_calls.append(kw)
        return [dict(r) for r in e.listings if r["status"] in ("pocket", "public")]

    def count_live():
        live = [r for r in e.listings if r["status"] in ("pocket", "public")]
        return len(live) if e.live_count is None else e.live_count

    monkeypatch.setattr(el, "_now", lambda: NOW)
    monkeypatch.setattr(el, "_conn", lambda: _Conn(e))
    monkeypatch.setattr(el, "_db_list_listings", list_listings)
    monkeypatch.setattr(el, "_db_count_live", count_live)
    monkeypatch.setattr(el, "_db_get_listing", lambda ident: next(
        (dict(r) for r in e.listings if str(r["id"]) == str(ident) or r["slug"] == str(ident)), None))
    # Terms acceptance, view recording and the disclosure block have their own
    # tests (test_pocket_listings_wall_and_leads.py,
    # test_capacity_source_deal_registration.py); here a signed-in buyer has
    # accepted the terms and holds no registration.
    monkeypatch.setattr(el, "_db_terms_accepted", lambda user_ref, version: True)
    monkeypatch.setattr(el, "_record_view", lambda row, v: None)
    monkeypatch.setattr(el, "_db_viewer_lead_events", lambda user_ref, listing_id: [])

    app = Flask(__name__)
    app.register_blueprint(el.exclusive_listings_bp)
    e.client = app.test_client()
    return e


def _bearer(user_id="u-buyer", email="buyer@fund.example"):
    token = jwt.encode({"user_id": user_id, "email": email, "plan": "free", "role": "user",
                        "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                       JWT_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _create(env, detail, **body):
    return env.client.post("/api/v1/admin/listings", headers=ADMIN,
                           json={"title": "Powered shell — DFW", "slug": "dfw-40",
                                 "status": "pocket", "detail": detail, **body})


def _fields(response):
    return [e["field"] for e in response.get_json().get("errors", [])]


def _schedule(*pairs):
    return [{"date": date, "mw": mw} for date, mw in pairs]


def _colocation(block, delivery_type="colocation"):
    return {"delivery_type": delivery_type, "colocation": block}


def _surfaces(env):
    """Every public answer that carries the listing: the feed and the detail
    route, anonymous and as a signed-in buyer who has accepted the terms."""
    return {
        "feed (anonymous)": env.client.get("/api/v1/listings"),
        "feed (signed in)": env.client.get("/api/v1/listings", headers=_bearer()),
        "teaser": env.client.get("/api/v1/listings/dfw-40"),
        "full view": env.client.get("/api/v1/listings/dfw-40", headers=_bearer()),
    }


# ── round trip ────────────────────────────────────────────────────────────

def test_every_reserved_key_has_a_rule():
    assert set(el._DETAIL_FIELD_CHECKS) == set(el._DETAIL_RESERVED_KEYS) == {
        "colocation", "contiguous_kw", "delivery_type", "min_contract_kw", "mw_schedule",
        "power", "price", "provider", "site", "update_cadence", "verification"}


def test_a_full_listing_round_trips_through_create_teaser_and_full_view(env):
    r = _create(env, VALID_DETAIL, market="Dallas", state="TX", capacity_mw=40)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["slug"] == "dfw-40" and len(env.statements) == 1

    stored = env.listings[0]["detail"]
    assert stored == {"available": "Q2 2027", **NORMALIZED}
    # Numbers are stored as JSON numbers, whole ones as integers.
    assert json.dumps(stored["mw_schedule"]) == (
        '[{"date": "2027-06-15", "mw": 20}, {"date": "2027-09", "mw": 40}, '
        '{"date": "2028-01", "mw": 60}]')
    assert json.dumps(stored["price"]) == '{"low": 120, "high": 145.5, "unit": "usd_per_kw_month"}'

    fresh = {"state": "fresh", "verified_at": "2026-09-01T14:30:00.000000+00:00", "age_days": 11,
             **NO_CADENCE}
    locked = env.client.get("/api/v1/listings/dfw-40").get_json()
    assert locked["locked"] is True
    teaser = locked["listing"]
    assert (teaser["delivery_type"], teaser["freshness"], teaser["provider"]) == (
        "powered_shell", fresh, {"name": "Lone Star Data Partners"})
    assert (teaser["capacity_kw"], teaser["region"], teaser["update_cadence"]) == (
        40000, "north_america", None)
    assert set(teaser) == TEASER_KEYS | NEW_TEASER_KEYS      # nothing walled on the card

    item = env.client.get("/api/v1/listings").get_json()["items"][0]
    assert {k: item[k] for k in NEW_TEASER_KEYS} == {k: teaser[k] for k in NEW_TEASER_KEYS}

    opened = env.client.get("/api/v1/listings/dfw-40", headers=_bearer()).get_json()
    assert opened["locked"] is False
    full = opened["listing"]
    assert set(full) == FULL_KEYS | NEW_FULL_KEYS
    for key in ("delivery_type", "mw_schedule", "price", "verification"):
        assert full[key] == NORMALIZED[key], key
    # The specs view (2026-09-15): power without its substation, and no
    # coordinates. The site is disclosed only once the provider accepts the
    # viewer's registration.
    assert full["power"] == {"utility": "Oncor Electric", "interconnection_stage": "agreement_executed"}
    assert (full["latitude"], full["longitude"]) == (None, None)
    assert "Seagoville" not in json.dumps(opened)
    assert full["provider"] == {"name": "Lone Star Data Partners", "disclosed": True}
    assert full["freshness"] == fresh
    assert full["detail"] == {"available": "Q2 2027"}        # reserved keys only as typed fields


# ── write rules ───────────────────────────────────────────────────────────

_REFUSALS = [
    pytest.param({"delivery_type": "warehouse"}, "detail.delivery_type", id="delivery_type outside the enum"),
    pytest.param({"delivery_type": 3}, "detail.delivery_type", id="delivery_type not text"),
    pytest.param({"mw_schedule": []}, "detail.mw_schedule", id="mw_schedule empty"),
    pytest.param({"mw_schedule": {"date": "2027-06", "mw": 10}}, "detail.mw_schedule",
                 id="mw_schedule not a list"),
    pytest.param({"mw_schedule": _schedule(*[(f"{2027 + i // 12}-{i % 12 + 1:02d}", i + 1)
                                             for i in range(25)])},
                 "detail.mw_schedule", id="mw_schedule of 25 entries"),
    pytest.param({"mw_schedule": ["2027-06"]}, "detail.mw_schedule[0]", id="entry not an object"),
    pytest.param({"mw_schedule": _schedule(("2027-13", 10))}, "detail.mw_schedule[0].date",
                 id="month 13"),
    pytest.param({"mw_schedule": _schedule(("2027-06", 10), ("2027-02-30", 20))},
                 "detail.mw_schedule[1].date", id="not a calendar day"),
    pytest.param({"mw_schedule": _schedule(("June 2027", 10))}, "detail.mw_schedule[0].date",
                 id="date in words"),
    pytest.param({"mw_schedule": _schedule((202706, 10))}, "detail.mw_schedule[0].date",
                 id="date as a number"),
    pytest.param({"mw_schedule": _schedule(("2027-06", 10), ("2027-09", 0))},
                 "detail.mw_schedule[1].mw", id="mw of zero"),
    pytest.param({"mw_schedule": _schedule(("2027-06", 10001))}, "detail.mw_schedule[0].mw",
                 id="mw over 10000"),
    pytest.param({"mw_schedule": _schedule(("2027-06", True))}, "detail.mw_schedule[0].mw",
                 id="mw as a boolean"),
    pytest.param({"mw_schedule": _schedule(("2027-06", "forty"))}, "detail.mw_schedule[0].mw",
                 id="mw not a number"),
    pytest.param({"mw_schedule": [{"date": "2027-06", "mw": 10, "note": "phase 1"}]},
                 "detail.mw_schedule[0].note", id="entry with another key"),
    pytest.param({"mw_schedule": _schedule(("2027-06", 10), ("2027-06", 20))},
                 "detail.mw_schedule[1].date", id="the same month twice"),
    pytest.param({"mw_schedule": _schedule(("2027-06-15", 10), ("2027-06-15", 20))},
                 "detail.mw_schedule[1].date", id="the same day twice"),
    pytest.param({"mw_schedule": _schedule(("2027-06", 10), ("2027-06-15", 20))},
                 "detail.mw_schedule[1].date", id="a month and a day inside it"),
    # Sorted, 2027-06 (20 MW) comes first, so the entry sent first is the one that falls.
    pytest.param({"mw_schedule": _schedule(("2028-01", 10), ("2027-06", 20))},
                 "detail.mw_schedule[0].mw", id="cumulative MW falling"),
    pytest.param({"power": {}}, "detail.power", id="power empty"),
    pytest.param({"power": "dual feed"}, "detail.power", id="power not an object"),
    pytest.param({"power": {"utility": "Oncor", "voltage_kv": 345}}, "detail.power.voltage_kv",
                 id="power with another key"),
    pytest.param({"power": {"utility": "   "}}, "detail.power.utility", id="utility blank"),
    pytest.param({"power": {"substation": "S" * 121}}, "detail.power.substation",
                 id="substation over 120 characters"),
    pytest.param({"power": {"interconnection_stage": "done"}}, "detail.power.interconnection_stage",
                 id="interconnection_stage outside the enum"),
    pytest.param({"price": {"on_request": False}}, "detail.price.on_request", id="on_request false"),
    pytest.param({"price": {"on_request": True, "low": 1, "high": 2, "unit": "usd_total"}},
                 "detail.price", id="on_request with a band"),
    pytest.param({"price": {}}, "detail.price", id="price empty"),
    pytest.param({"price": {"low": -1, "high": 2, "unit": "usd_total"}}, "detail.price.low",
                 id="low negative"),
    pytest.param({"price": {"low": 5, "high": 4, "unit": "usd_total"}}, "detail.price.high",
                 id="high below low"),
    pytest.param({"price": {"low": 1, "high": 2}}, "detail.price.unit", id="band without a unit"),
    pytest.param({"price": {"low": 1, "high": 2, "unit": "eur_per_kw"}}, "detail.price.unit",
                 id="unit outside the enum"),
    pytest.param({"price": {"low": 1, "high": 2, "unit": "usd_total", "currency": "USD"}},
                 "detail.price.currency", id="price with another key"),
    pytest.param({"provider": {"name": "Acme"}}, "detail.provider.disclosed",
                 id="provider without disclosed"),
    pytest.param({"provider": {"name": "Acme", "disclosed": "false"}}, "detail.provider.disclosed",
                 id="disclosed as text"),
    pytest.param({"provider": {"name": "", "disclosed": True}}, "detail.provider.name",
                 id="provider name empty"),
    pytest.param({"provider": {"name": "N" * 121, "disclosed": False}}, "detail.provider.name",
                 id="provider name over 120 characters"),
    pytest.param({"provider": "Acme"}, "detail.provider", id="provider not an object"),
    pytest.param({"verification": {"verified_by": "desk", "verified_at": "2026-09-01"}},
                 "detail.verification.method", id="verification without method"),
    pytest.param({"verification": {**VERIFICATION, "method": "phone_call"}},
                 "detail.verification.method", id="method outside the enum"),
    pytest.param({"verification": {"verified_at": "2026-09-01", "method": "site_visit"}},
                 "detail.verification.verified_by", id="verification without verified_by"),
    pytest.param({"verification": {**VERIFICATION, "verified_at": "last week"}},
                 "detail.verification.verified_at", id="verified_at not a date"),
    pytest.param({"verification": {**VERIFICATION, "verified_at": "2026-02-30"}},
                 "detail.verification.verified_at", id="verified_at not a calendar day"),
    pytest.param({"verification": {**VERIFICATION, "verified_at": "2026-09-14T12:00:01Z"}},
                 "detail.verification.verified_at", id="verified_at over a day ahead"),
    pytest.param(_colocation("1200 kW"), "detail.colocation", id="colocation not an object"),
    pytest.param(_colocation({}), "detail.colocation.kw_available", id="colocation without kw_available"),
    pytest.param(_colocation({"kw_available": 0}), "detail.colocation.kw_available",
                 id="kw_available of zero"),
    pytest.param(_colocation({"kw_available": 100001}), "detail.colocation.kw_available",
                 id="kw_available over 100000"),
    pytest.param(_colocation({"kw_available": True}), "detail.colocation.kw_available",
                 id="kw_available as a boolean"),
    pytest.param(_colocation({"kw_available": 900, "cabinets_available": -1}),
                 "detail.colocation.cabinets_available", id="cabinets_available negative"),
    pytest.param(_colocation({"kw_available": 900, "cabinets_available": 12.5}),
                 "detail.colocation.cabinets_available", id="cabinets_available not whole"),
    pytest.param(_colocation({"kw_available": 900, "cabinets_available": "a dozen"}),
                 "detail.colocation.cabinets_available", id="cabinets_available not a number"),
    pytest.param(_colocation({"kw_available": 900, "max_kw_per_cabinet": 0}),
                 "detail.colocation.max_kw_per_cabinet", id="max_kw_per_cabinet of zero"),
    pytest.param(_colocation({"kw_available": 900, "max_kw_per_cabinet": 300.5}),
                 "detail.colocation.max_kw_per_cabinet", id="max_kw_per_cabinet over 300"),
    pytest.param(_colocation({"kw_available": 900, "racks": 4}), "detail.colocation.racks",
                 id="colocation with another key"),
    pytest.param(_colocation({"kw_available": 900}, "powered_shell"), "detail.colocation",
                 id="colocation on a powered_shell listing"),
    pytest.param({"colocation": {"kw_available": 900}}, "detail.colocation",
                 id="colocation without a delivery_type"),
    pytest.param({"Provider": {"name": "Acme", "disclosed": False}}, "detail.Provider",
                 id="reserved key in other case"),
    pytest.param(["delivery_type", "land"], "detail", id="detail not an object"),
    # contiguous_kw / min_contract_kw: a number greater than 0, at most
    # 5,000,000, and the smallest contractable chunk never above the largest
    # contiguous block. These listings carry no capacity_mw, so the total bound
    # has nothing to measure against and only the named rule fires.
    pytest.param({"contiguous_kw": 0}, "detail.contiguous_kw", id="contiguous_kw of zero"),
    pytest.param({"contiguous_kw": -500}, "detail.contiguous_kw", id="contiguous_kw negative"),
    pytest.param({"contiguous_kw": 5_000_001}, "detail.contiguous_kw",
                 id="contiguous_kw over 5000000"),
    pytest.param({"contiguous_kw": "500 kW"}, "detail.contiguous_kw",
                 id="contiguous_kw not a number"),
    pytest.param({"contiguous_kw": True}, "detail.contiguous_kw",
                 id="contiguous_kw as a boolean"),
    pytest.param({"min_contract_kw": 0}, "detail.min_contract_kw", id="min_contract_kw of zero"),
    pytest.param({"min_contract_kw": 5_000_001}, "detail.min_contract_kw",
                 id="min_contract_kw over 5000000"),
    pytest.param({"min_contract_kw": "1 MW"}, "detail.min_contract_kw",
                 id="min_contract_kw not a number"),
    pytest.param({"contiguous_kw": 500, "min_contract_kw": 501}, "detail.min_contract_kw",
                 id="min_contract_kw above contiguous_kw"),
]


@pytest.mark.parametrize("detail,field", _REFUSALS)
def test_each_rule_refuses_with_the_field_it_names_and_writes_nothing(env, detail, field):
    r = _create(env, detail)
    j = r.get_json()
    assert (r.status_code, j.get("error")) == (400, "invalid_detail"), j
    assert _fields(r) == [field]
    assert j["message"].startswith(field + " ") and "\n" not in j["message"]
    assert env.statements == [] and env.listings == []


def test_a_refusal_counts_every_broken_field(env):
    r = _create(env, {"delivery_type": "shell", "price": {"low": 5, "high": 1, "unit": "usd_total"},
                      "provider": {"name": "Acme"}})
    assert _fields(r) == ["detail.delivery_type", "detail.price.high", "detail.provider.disclosed"]
    assert r.get_json()["message"].endswith("(and 2 more)")
    assert env.statements == []


@pytest.mark.parametrize("detail,stored", [
    pytest.param({"price": {"low": 0, "high": 0, "unit": "usd_total"}},
                 {"price": {"low": 0, "high": 0, "unit": "usd_total"}}, id="a band of zero"),
    pytest.param({"price": {"on_request": True, "unit": None}}, {"price": {"on_request": True}},
                 id="price on request"),
    pytest.param({"power": {"interconnection_stage": " energized ", "utility": None}},
                 {"power": {"interconnection_stage": "energized"}}, id="one power key"),
    pytest.param({"provider": {"name": "N" * 120, "disclosed": False}},
                 {"provider": {"name": "N" * 120, "disclosed": False}}, id="a name of 120 characters"),
    pytest.param({"delivery_type": None, "notes": "kept"}, {"notes": "kept"},
                 id="a reserved key set to null is dropped"),
    pytest.param({"mw_schedule": _schedule(("2027-07", 10), ("2027-06-30", 10))},
                 {"mw_schedule": _schedule(("2027-06-30", 10), ("2027-07", 10))},
                 id="flat MW across a month boundary"),
    pytest.param({"mw_schedule": _schedule(*[(f"{2027 + i // 12}-{i % 12 + 1:02d}", min(10000, 500 * (i + 1)))
                                             for i in range(24)])},
                 {"mw_schedule": _schedule(*[(f"{2027 + i // 12}-{i % 12 + 1:02d}", min(10000, 500 * (i + 1)))
                                             for i in range(24)])},
                 id="24 entries up to 10000 MW"),
    pytest.param({"verification": {**VERIFICATION, "verified_at": "2026-09-14T12:00:00Z"}},
                 {"verification": {**VERIFICATION, "verified_at": "2026-09-14T12:00:00.000000+00:00"}},
                 id="verified_at exactly a day ahead"),
    pytest.param({"verification": {**VERIFICATION, "verified_at": "2026-09-01T10:00:00"}},
                 {"verification": {**VERIFICATION, "verified_at": "2026-09-01T10:00:00.000000+00:00"}},
                 id="a naive date-time is UTC"),
    pytest.param({"verification": {**VERIFICATION, "verified_at": "2026-09-01T10:00:00.5+0530"}},
                 {"verification": {**VERIFICATION, "verified_at": "2026-09-01T04:30:00.500000+00:00"}},
                 id="an offset without a colon"),
    pytest.param({"verification": VERIFICATION},
                 {"verification": {**VERIFICATION, "verified_at": "2026-09-01T00:00:00.000000+00:00"}},
                 id="a date is midnight UTC"),
    pytest.param(_colocation({"kw_available": 100000, "cabinets_available": 0, "max_kw_per_cabinet": 300}),
                 _colocation({"kw_available": 100000, "cabinets_available": 0, "max_kw_per_cabinet": 300}),
                 id="colocation at its bounds"),
    pytest.param(_colocation({"kw_available": " 0.5 ", "cabinets_available": None,
                              "max_kw_per_cabinet": "7.50"}, " colocation "),
                 _colocation({"kw_available": 0.5, "max_kw_per_cabinet": 7.5}),
                 id="colocation without cabinets, numbers as text"),
])
def test_edge_values_are_accepted_and_stored_normalized(env, detail, stored):
    r = _create(env, detail)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert env.listings[0]["detail"] == stored


def test_a_detail_sent_as_a_json_string_is_validated_too(env):
    falling = json.dumps({"mw_schedule": _schedule(("2027-06", 20), ("2027-09", 10))})
    r = _create(env, falling)
    assert r.status_code == 400 and _fields(r) == ["detail.mw_schedule[1].mw"]
    for not_an_object in ("{mw_schedule: []}", "", '"land"', "[1, 2]"):
        r = _create(env, not_an_object)
        assert (r.status_code, _fields(r)) == (400, ["detail"]), not_an_object
    assert env.statements == []

    r = _create(env, json.dumps(VALID_DETAIL))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert env.listings[0]["detail"] == {"available": "Q2 2027", **NORMALIZED}   # normalized, not as sent


def test_patch_and_put_replacing_detail_are_validated_like_a_create(env):
    env.listings.append(_row(detail={"available": "Q2 2027"}))
    r = env.client.patch("/api/v1/admin/listings/1", headers=ADMIN, json={
        "title": "Renamed", "detail": {"price": {"low": 9, "high": 3, "unit": "usd_per_mw"}}})
    assert (r.status_code, r.get_json().get("error"), _fields(r)) == (400, "invalid_detail",
                                                                      ["detail.price.high"])
    r = env.client.put("/api/v1/admin/listings/1", headers=ADMIN,
                       json={"detail": json.dumps({"delivery_type": "shell"})})
    assert (r.status_code, _fields(r)) == (400, ["detail.delivery_type"])
    assert env.statements == [] and env.listings[0]["title"] == "Powered shell — DFW"

    r = env.client.patch("/api/v1/admin/listings/1", headers=ADMIN, json={"detail": json.dumps(
        {"price": {"on_request": True}, "delivery_type": " land ", "notes": "as sent"})})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert env.listings[0]["detail"] == {"price": {"on_request": True}, "delivery_type": "land",
                                         "notes": "as sent"}


# ── a listing without the reserved keys ───────────────────────────────────

LEGACY_DETAIL = {"available": "Q2 2027", "feeds": "dual feed", "_internal": "hidden-note",
                 "contact": "operator@private.example"}


def test_a_listing_without_the_reserved_keys_stores_as_sent(env):
    r = _create(env, LEGACY_DETAIL)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert env.listings[0]["detail"] == LEGACY_DETAIL
    r = env.client.patch("/api/v1/admin/listings/1", headers=ADMIN,
                         json={"detail": json.dumps(LEGACY_DETAIL)})
    assert r.status_code == 200 and env.listings[0]["detail"] == LEGACY_DETAIL
    r = env.client.patch("/api/v1/admin/listings/1", headers=ADMIN, json={"detail": None})
    assert r.status_code == 200 and env.listings[0]["detail"] is None


def test_a_listing_without_the_reserved_keys_reads_as_before(env):
    env.listings.append(_row(detail=LEGACY_DETAIL))
    teaser = env.client.get("/api/v1/listings/dfw-40").get_json()["listing"]
    assert {k: teaser[k] for k in TEASER_KEYS} == {
        "id": 1, "slug": "dfw-40", "title": "Powered shell — DFW", "summary": "Energized next year.",
        "status": "pocket", "access_required": "registered", "locked": True,
        "lock_reason": "sign_in_required", "market": "Dallas", "state": "TX", "country": "US",
        "capacity_mw": 40.0, "available": "Q2 2027",
        "created_at": "2026-09-01T00:00:00.000000+00:00",
        "updated_at": "2026-09-02T00:00:00.000000+00:00", "expires_at": None,
        "url": "https://dchub.cloud/listings/dfw-40"}
    assert {k: teaser[k] for k in NEW_TEASER_KEYS} == {
        "delivery_type": None, "freshness": UNVERIFIED, "provider": None,
        "capacity_kw": 40000, "region": "north_america", "update_cadence": None,
        "contiguous_kw": None, "min_contract_kw": None}

    full = env.client.get("/api/v1/listings/dfw-40", headers=_bearer()).get_json()["listing"]
    assert set(full) == FULL_KEYS | NEW_FULL_KEYS
    # Coordinates are site identity: the specs view carries the keys, as null
    # (2026-09-15), until the provider accepts the viewer's registration.
    assert (full["latitude"], full["longitude"], full["asking_price"], full["asking_currency"]) == (
        None, None, 1250000.0, "USD")
    assert full["detail"] == {"available": "Q2 2027", "feeds": "dual feed"}
    assert {k: full[k] for k in NEW_FULL_KEYS} == {
        "delivery_type": None, "freshness": UNVERIFIED, "provider": None, "mw_schedule": None,
        "power": None, "price": None, "verification": None, "colocation": None,
        "capacity_kw": 40000, "region": "north_america", "update_cadence": None,
        "contiguous_kw": None, "min_contract_kw": None}


def test_stored_values_that_break_the_rules_read_as_null_without_failing(env):
    """Rows written before the rules keep serving; only the broken fields go null."""
    env.listings.append(_row(detail={
        "delivery_type": "Powered Shell", "mw_schedule": [{"date": "soon", "mw": 20}],
        "power": {"utility": "Oncor"}, "price": {"low": 5, "high": 1, "unit": "usd_total"},
        "provider": {"name": "Acme", "disclosed": "yes"}, "verification": {"verified_by": "desk"},
        "colocation": {"kw_available": "lots"},
        "contiguous_kw": "half the hall", "min_contract_kw": 0}))
    for label, r in _surfaces(env).items():
        assert r.status_code == 200, label
    full = env.client.get("/api/v1/listings/dfw-40", headers=_bearer()).get_json()["listing"]
    assert {k: full[k] for k in NEW_FULL_KEYS} == {
        "delivery_type": None, "freshness": UNVERIFIED, "provider": None, "mw_schedule": None,
        "power": {"utility": "Oncor"}, "price": None, "verification": None, "colocation": None,
        "capacity_kw": 40000, "region": "north_america", "update_cadence": None,
        "contiguous_kw": None, "min_contract_kw": None}
    assert full["detail"] == {}


# ── freshness ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ago,state,age_days", [
    pytest.param(timedelta(0), "fresh", 0, id="verified now"),
    pytest.param(timedelta(days=30), "fresh", 30, id="30 days"),
    pytest.param(timedelta(days=30, hours=23, minutes=59), "fresh", 30, id="30 days 23 hours"),
    pytest.param(timedelta(days=31), "aging", 31, id="31 days"),
    pytest.param(timedelta(days=90), "aging", 90, id="90 days"),
    pytest.param(timedelta(days=91), "stale", 91, id="91 days"),
    pytest.param(-timedelta(hours=12), "fresh", 0, id="half a day ahead"),
])
def test_freshness_counts_whole_days_since_verification(env, ago, state, age_days):
    verified_at = NOW - ago
    env.listings.append(_row(detail={"verification": {**VERIFICATION,
                                                      "verified_at": verified_at.isoformat()}}))
    expected = {"state": state, "age_days": age_days,
                "verified_at": verified_at.strftime("%Y-%m-%dT%H:%M:%S.000000+00:00"), **NO_CADENCE}
    assert env.client.get("/api/v1/listings").get_json()["items"][0]["freshness"] == expected
    assert env.client.get("/api/v1/listings/dfw-40").get_json()["listing"]["freshness"] == expected


@pytest.mark.parametrize("detail", [
    pytest.param(None, id="no detail"),
    pytest.param({"available": "Q2 2027"}, id="no verification"),
    pytest.param({"verification": {**VERIFICATION, "verified_at": "2026-09-14T12:00:01Z"}},
                 id="verified_at over a day ahead"),
    pytest.param({"verification": {"verified_by": "desk", "verified_at": "2026-09-01"}},
                 id="verification without method"),
    pytest.param({"verification": "confirmed by phone"}, id="verification not an object"),
])
def test_a_missing_or_invalid_verification_reads_unverified(env, detail):
    env.listings.append(_row(detail=detail))
    assert env.client.get("/api/v1/listings").get_json()["items"][0]["freshness"] == UNVERIFIED
    full = env.client.get("/api/v1/listings/dfw-40", headers=_bearer()).get_json()["listing"]
    assert (full["freshness"], full["verification"]) == (UNVERIFIED, None)


# ── provider disclosure ───────────────────────────────────────────────────

def test_an_undisclosed_provider_name_is_served_nowhere(env):
    env.listings.append(_row(detail={
        "available": "Q2 2027", "provider": {"name": HIDDEN_PROVIDER, "disclosed": False},
        " Provider ": {"name": HIDDEN_PROVIDER}}))       # a stored case variant of the key, too
    surfaces = _surfaces(env)
    assert surfaces["full view"].get_json()["locked"] is False     # the full view really opened
    for label, r in surfaces.items():
        assert r.status_code == 200, label
        assert HIDDEN_PROVIDER not in r.get_data(as_text=True), label
    opened = surfaces["full view"].get_json()["listing"]
    assert opened["provider"] == {"name": None, "disclosed": False}
    assert opened["detail"] == {"available": "Q2 2027"}
    assert surfaces["teaser"].get_json()["listing"]["provider"] is None
    assert [i["provider"] for i in surfaces["feed (signed in)"].get_json()["items"]] == [None]


def test_a_disclosed_provider_name_is_served_on_every_surface(env):
    """Control for the test above: the same search finds the name on all four
    surfaces once the provider is disclosed."""
    env.listings.append(_row(detail={"provider": {"name": HIDDEN_PROVIDER, "disclosed": True}}))
    surfaces = _surfaces(env)
    assert surfaces["full view"].get_json()["listing"]["provider"] == {
        "name": HIDDEN_PROVIDER, "disclosed": True}
    assert surfaces["teaser"].get_json()["listing"]["provider"] == {"name": HIDDEN_PROVIDER}
    for label, r in surfaces.items():
        assert r.status_code == 200, label
        assert HIDDEN_PROVIDER in r.get_data(as_text=True), label


# ── colocation ────────────────────────────────────────────────────────────

COLOCATION = {"kw_available": 1200, "cabinets_available": 48, "max_kw_per_cabinet": 30}


def test_a_colocation_listing_round_trips_into_the_full_view_only(env):
    r = _create(env, {"available": "Now", "delivery_type": "colocation",
                      "colocation": {"kw_available": "1200", "cabinets_available": 48.0,
                                     "max_kw_per_cabinet": 30},
                      "price": {"low": 140, "high": 165, "unit": "usd_per_kw_month"}})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert json.dumps(env.listings[0]["detail"]["colocation"]) == (
        '{"kw_available": 1200, "cabinets_available": 48, "max_kw_per_cabinet": 30}')

    surfaces = _surfaces(env)
    opened = surfaces["full view"].get_json()
    assert opened["locked"] is False
    full = opened["listing"]
    assert full["colocation"] == COLOCATION
    assert (full["delivery_type"], full["price"]) == (
        "colocation", {"low": 140, "high": 165, "unit": "usd_per_kw_month"})
    assert full["detail"] == {"available": "Now"}            # not repeated as a generic key
    assert "kw_available" in surfaces["full view"].get_data(as_text=True)
    for label in ("feed (anonymous)", "feed (signed in)", "teaser"):
        r, body = surfaces[label], surfaces[label].get_json()
        listings = body["items"] if label.startswith("feed") else [body["listing"]]
        assert r.status_code == 200 and [i["slug"] for i in listings] == ["dfw-40"], label
        assert "colocation" not in listings[0], label
        assert "kw_available" not in r.get_data(as_text=True), label
    assert set(surfaces["teaser"].get_json()["listing"]) == TEASER_KEYS | NEW_TEASER_KEYS


def test_colocation_on_any_other_delivery_type_is_refused_on_create_and_update(env):
    for delivery_type in ("land", "powered_shell", "turnkey"):
        r = _create(env, _colocation(COLOCATION, delivery_type))
        assert (r.status_code, r.get_json().get("error"), _fields(r)) == (
            400, "invalid_detail", ["detail.colocation"]), delivery_type
    assert env.statements == [] and env.listings == []

    env.listings.append(_row(detail=_colocation(COLOCATION)))
    r = env.client.patch("/api/v1/admin/listings/1", headers=ADMIN,
                         json={"detail": _colocation(COLOCATION, "turnkey")})
    assert (r.status_code, _fields(r)) == (400, ["detail.colocation"])
    r = env.client.put("/api/v1/admin/listings/1", headers=ADMIN,
                       json={"detail": json.dumps({"colocation": COLOCATION})})
    assert (r.status_code, _fields(r)) == (400, ["detail.colocation"])
    assert env.statements == []

    # Control: the same block on a colocation listing is written.
    r = env.client.patch("/api/v1/admin/listings/1", headers=ADMIN,
                         json={"detail": _colocation(COLOCATION, " colocation ")})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert env.listings[0]["detail"] == _colocation(COLOCATION)


@pytest.mark.parametrize("detail,expected", [
    pytest.param(_colocation(COLOCATION), COLOCATION, id="on a colocation listing"),
    pytest.param(_colocation(COLOCATION, "turnkey"), None, id="on a turnkey listing"),
    pytest.param({"colocation": COLOCATION}, None, id="without a delivery_type"),
    pytest.param(_colocation({**COLOCATION, "max_kw_per_cabinet": 350}), None,
                 id="a stored value over a bound"),
])
def test_a_stored_colocation_reads_only_on_a_colocation_listing_within_the_rules(env, detail, expected):
    env.listings.append(_row(detail={**detail, " Colocation ": COLOCATION}))   # a stored case variant, too
    full = env.client.get("/api/v1/listings/dfw-40", headers=_bearer()).get_json()["listing"]
    assert full["colocation"] == expected
    assert full["detail"] == {}


# ── available ─────────────────────────────────────────────────────────────

LATE_FIRST = _schedule(("2027-06-15", 40), ("2026-12", 20))      # stored out of date order


@pytest.mark.parametrize("detail,available", [
    pytest.param({"mw_schedule": LATE_FIRST}, "2026-12", id="the earliest schedule date, as stored"),
    pytest.param({"mw_schedule": _schedule(("2027-06-15", 40))}, "2027-06-15", id="a schedule day"),
    pytest.param({"available": " Q2 2027 ", "mw_schedule": LATE_FIRST}, "Q2 2027", id="available wins"),
    pytest.param({"available_date": "2027-03-01", "mw_schedule": LATE_FIRST}, "2027-03-01",
                 id="available_date wins"),
    pytest.param({"energization": "Q4 2026", "mw_schedule": LATE_FIRST}, "Q4 2026", id="energization wins"),
    pytest.param({"delivery": 2028, "mw_schedule": LATE_FIRST}, "2028", id="delivery wins"),
    pytest.param({"available": " ", "mw_schedule": LATE_FIRST}, "2026-12", id="a blank available falls back"),
    pytest.param({"mw_schedule": [{"date": "soon", "mw": 20}]}, None, id="an invalid schedule gives none"),
    pytest.param({"mw_schedule": _schedule(("2027-06", 20), ("2027-09", 10))}, None,
                 id="a falling schedule gives none"),
])
def test_available_falls_back_to_the_earliest_schedule_date_and_an_explicit_key_wins(env, detail, available):
    env.listings.append(_row(detail=detail))
    for label, r in _surfaces(env).items():
        body = r.get_json()
        listing = body["items"][0] if label.startswith("feed") else body["listing"]
        assert (r.status_code, listing["available"]) == (200, available), label


# ── feed filters ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "delivery_type=warehouse", "delivery_type=LAND", "available_by=2027-13",
    "available_by=2027-6", "available_by=2027-02-30", "available_by=soon", "available_by=202706",
])
def test_a_malformed_filter_is_refused_before_any_query(env, query):
    r = env.client.get("/api/v1/listings?" + query)
    j = r.get_json()
    assert (r.status_code, j.get("error")) == (400, "invalid_request"), j
    if query.startswith("delivery_type"):
        assert j["allowed"] == ["land", "powered_shell", "turnkey", "colocation"]
    assert env.list_calls == []


def test_valid_filters_reach_the_query_and_live_count_comes_from_the_count(env):
    env.live_count = 3        # three live listings, none of which the filter returns
    j = env.client.get("/api/v1/listings?delivery_type=turnkey&available_by=2027-06-15").get_json()
    assert (j["ok"], j["count"], j["program"]["status"]) == (True, 0, "live")
    call = env.list_calls[-1]
    assert (call["delivery_type"], call["available_by"]) == ("turnkey", "2027-06-15")
    # Control: unfiltered, the live count is the rows returned, so the same store reads upcoming.
    j = env.client.get("/api/v1/listings").get_json()
    assert (j["program"]["status"], env.list_calls[-1]["delivery_type"],
            env.list_calls[-1]["available_by"]) == ("upcoming", None, None)


def test_the_filters_are_bound_parameters_of_the_listing_query(monkeypatch):
    seen = []
    monkeypatch.setattr(el, "_fetch", lambda sql, params, cols: seen.append(
        (" ".join(sql.split()), list(params))) or [])
    el._db_list_listings(state="TX", delivery_type="turnkey", available_by="2027-06-15", limit=7)
    el._db_list_listings(state="TX", limit=7)
    (sql, params), (plain_sql, plain_params) = seen
    where = sql.split(" WHERE ", 1)[1]
    assert params == ["TX", "turnkey", "2027-06", 7]          # available_by compares by month
    assert where.count("%s") == 4
    assert "detail->>'delivery_type' = %s" in where
    assert "jsonb_array_elements(" in where and "<= %s)" in where
    # The control names the two FILTER predicates it expects to be absent. It
    # used to test for the substring "detail", which stopped meaning "no filter
    # predicate" the moment the always-on demo exclusion (_DEMO_EXCLUDE_SQL)
    # put a detail->>'demo' test in every live WHERE clause. That predicate
    # binds no parameter, which is what `plain_params` above still proves.
    plain_where = plain_sql.split(" WHERE ", 1)[1]
    assert plain_params == ["TX", 7]
    assert "detail->>'delivery_type'" not in plain_where
    assert "jsonb_array_elements(" not in plain_where


# ── site, update_cadence, capacity_kw and region (2026-09-15) ─────────────

SITE = {"name": "Seagoville Campus", "address": "100 Industrial Blvd", "city": "Seagoville",
        "postal_code": "75159", "parcel_id": "APN 42-17"}


@pytest.mark.parametrize("detail,field", [
    pytest.param({"site": "Seagoville"}, "detail.site", id="site not an object"),
    pytest.param({"site": {}}, "detail.site", id="site empty"),
    pytest.param({"site": {"name": "Campus", "county": "Dallas"}}, "detail.site.county",
                 id="site with another key"),
    pytest.param({"site": {"postal_code": 75159}}, "detail.site.postal_code", id="postal_code not text"),
    pytest.param({"site": {"address": "   "}}, "detail.site.address", id="address blank"),
    pytest.param({"site": {"parcel_id": "P" * 121}}, "detail.site.parcel_id",
                 id="parcel_id over 120 characters"),
    pytest.param({"Site": {"name": "Campus"}}, "detail.Site", id="site in other case"),
    pytest.param({"update_cadence": "daily"}, "detail.update_cadence", id="update_cadence outside the enum"),
    pytest.param({"update_cadence": 7}, "detail.update_cadence", id="update_cadence not text"),
])
def test_site_and_update_cadence_refuse_with_the_field_they_name(env, detail, field):
    r = _create(env, detail)
    assert (r.status_code, r.get_json().get("error"), _fields(r)) == (400, "invalid_detail", [field])
    assert env.statements == [] and env.listings == []


def test_site_and_update_cadence_are_stored_normalized(env):
    r = _create(env, {"site": {"name": "  Seagoville   Campus ", "city": "Seagoville", "address": None},
                      "update_cadence": " weekly "})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert env.listings[0]["detail"] == {"site": {"name": "Seagoville Campus", "city": "Seagoville"},
                                         "update_cadence": "weekly"}
    r = _create(env, {"site": SITE}, slug="dfw-41")
    assert r.status_code == 200 and env.listings[1]["detail"] == {"site": SITE}


IDENTITY_DETAIL = {
    "available": "Q3 2027", "feeds": "dual feed", "site": SITE,
    " City ": "Seagoville", "ADDRESS": "100 Industrial Blvd", "zip": "75159", "apn": "42-17",
    "Site_Name": "Seagoville Campus", "facility_name": "SGV-1", "street_address": "9 Rail Spur",
    "site_address": "Gate 4", "postal_code": "75159", "parcel": "P-77", "parcel_id": "Q-88",
    "provider_city": "Plano",
    "power": {"utility": "Oncor", "substation": "Seagoville 345kV", "interconnection_stage": "energized"},
    "provider": {"name": HIDDEN_PROVIDER, "disclosed": False},
}


def test_the_specs_view_serves_no_site_identity_on_any_surface(env):
    env.listings.append(_row(detail=IDENTITY_DETAIL))
    surfaces = _surfaces(env)
    opened = surfaces["full view"].get_json()
    assert opened["locked"] is False
    full = opened["listing"]
    assert full["detail"] == {"available": "Q3 2027", "feeds": "dual feed"}
    assert full["power"] == {"utility": "Oncor", "interconnection_stage": "energized"}
    assert (full["latitude"], full["longitude"]) == (None, None) and "site" not in full
    assert opened["disclosure"]["released"] is False
    for label, r in surfaces.items():
        body = r.get_data(as_text=True)
        assert r.status_code == 200, label
        for secret in ("Seagoville", "Industrial Blvd", "75159", "42-17", "SGV-1", "Rail Spur",
                       "Gate 4", "P-77", "Q-88", "Plano", HIDDEN_PROVIDER, "32.77", "96.79"):
            assert secret not in body, (label, secret)


def test_a_disclosed_provider_name_still_shows_in_the_specs_view(env):
    """Control for the test above: provider.disclosed is the provider's own
    opt-in, so its name stays on every surface while the site stays hidden."""
    env.listings.append(_row(detail={**IDENTITY_DETAIL,
                                     "provider": {"name": HIDDEN_PROVIDER, "disclosed": True}}))
    for label, r in _surfaces(env).items():
        body = r.get_data(as_text=True)
        assert HIDDEN_PROVIDER in body and "Seagoville" not in body, label


@pytest.mark.parametrize("cadence,ago,overdue", [
    pytest.param("real_time", timedelta(days=2), False, id="real_time at 2 days"),
    pytest.param("real_time", timedelta(days=2, seconds=1), True, id="real_time past 2 days"),
    pytest.param("weekly", timedelta(days=9), False, id="weekly at 9 days"),
    pytest.param("weekly", timedelta(days=9, seconds=1), True, id="weekly past 9 days"),
    pytest.param("monthly", timedelta(days=35), False, id="monthly at 35 days"),
    pytest.param("monthly", timedelta(days=35, seconds=1), True, id="monthly past 35 days"),
])
def test_update_cadence_marks_a_listing_overdue_past_its_allowance(env, cadence, ago, overdue):
    verified_at = NOW - ago
    env.listings.append(_row(detail={"update_cadence": cadence, "verification": {
        **VERIFICATION, "verified_at": verified_at.isoformat()}}))
    due = verified_at + timedelta(days={"real_time": 2, "weekly": 9, "monthly": 35}[cadence])
    feed = env.client.get("/api/v1/listings").get_json()["items"]
    full = env.client.get("/api/v1/listings/dfw-40", headers=_bearer()).get_json()["listing"]
    assert [i["slug"] for i in feed] == ["dfw-40"]            # a badge, never a filter
    for listing in (feed[0], full):
        freshness = listing["freshness"]
        assert listing["update_cadence"] == freshness["update_cadence"] == cadence
        assert freshness["overdue"] is overdue
        assert freshness["next_update_due"] == due.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def test_update_cadence_without_a_verification_is_not_overdue(env):
    env.listings.append(_row(detail={"update_cadence": "weekly"}))
    freshness = env.client.get("/api/v1/listings").get_json()["items"][0]["freshness"]
    assert freshness == {**UNVERIFIED, "update_cadence": "weekly"}


@pytest.mark.parametrize("detail,capacity_mw,kw", [
    pytest.param({"delivery_type": "colocation", "colocation": {"kw_available": 1200}}, 40.0, 1200,
                 id="colocation by kw_available"),
    pytest.param({"delivery_type": "colocation"}, 40.0, None, id="colocation without kw_available"),
    pytest.param({"delivery_type": "powered_shell"}, 40.5, 40500, id="a MW listing"),
    pytest.param(None, 0.25, 250, id="no delivery type"),
    pytest.param({"delivery_type": "land"}, None, None, id="no capacity"),
])
def test_capacity_kw_sizes_colocation_by_kw_and_everything_else_by_mw(env, detail, capacity_mw, kw):
    env.listings.append(_row(detail=detail, capacity_mw=capacity_mw))
    assert env.client.get("/api/v1/listings").get_json()["items"][0]["capacity_kw"] == kw


# ── contiguous_kw and min_contract_kw ─────────────────────────────────────
# The two facts a headline capacity hides. A buyer reads them as what the
# listing can deliver, so a combination that would mislead is refused on write
# rather than stored (2026-09-16).

@pytest.mark.parametrize("detail,capacity_mw,field", [
    pytest.param({"delivery_type": "powered_shell", "contiguous_kw": 2001}, 2.0,
                 "detail.contiguous_kw", id="contiguous_kw above capacity_mw * 1000"),
    pytest.param({"delivery_type": "powered_shell", "min_contract_kw": 2001}, 2.0,
                 "detail.min_contract_kw", id="min_contract_kw above capacity_mw * 1000"),
    pytest.param({"delivery_type": "colocation", "colocation": {"kw_available": 2000},
                  "contiguous_kw": 2001}, 40.0,
                 "detail.contiguous_kw", id="contiguous_kw above the colocation space"),
    pytest.param({"delivery_type": "colocation", "colocation": {"kw_available": 2000},
                  "min_contract_kw": 2001}, 40.0,
                 "detail.min_contract_kw", id="min_contract_kw above the colocation space"),
])
def test_a_block_larger_than_the_listing_total_is_refused(env, detail, capacity_mw, field):
    r = _create(env, detail, capacity_mw=capacity_mw)
    assert (r.status_code, r.get_json().get("error")) == (400, "invalid_detail"), r.get_json()
    assert _fields(r) == [field]
    assert env.statements == [] and env.listings == []


@pytest.mark.parametrize("detail,capacity_mw", [
    pytest.param({"delivery_type": "powered_shell", "contiguous_kw": 2000,
                  "min_contract_kw": 2000}, 2.0, id="both exactly the total"),
    pytest.param({"delivery_type": "colocation", "colocation": {"kw_available": 2000},
                  "contiguous_kw": 500, "min_contract_kw": 500}, 40.0,
                 id="a colocation listing bounded by its own space, not the row"),
    pytest.param({"delivery_type": "powered_shell", "contiguous_kw": 5_000_000}, None,
                 id="no capacity_mw, so no total to exceed"),
])
def test_a_block_within_the_listing_total_is_stored(env, detail, capacity_mw):
    r = _create(env, detail, capacity_mw=capacity_mw)
    assert r.status_code == 200, r.get_data(as_text=True)
    stored = env.listings[0]["detail"]
    for key in ("contiguous_kw", "min_contract_kw"):
        if key in detail:
            assert stored[key] == detail[key]


def test_the_owners_two_listings_store_and_read_back_on_an_anonymous_teaser(env):
    """The JSON a provider sends, and what an anonymous caller then sees: both
    facts travel with the headline, on the teaser and on the full view."""
    colo = {"delivery_type": "colocation", "colocation": {"kw_available": 2000},
            "contiguous_kw": 500}
    assert _create(env, colo, capacity_mw=2.0).status_code == 200
    teaser = env.client.get("/api/v1/listings").get_json()["items"][0]
    assert (teaser["capacity_mw"], teaser["capacity_kw"]) == (2.0, 2000)
    assert (teaser["contiguous_kw"], teaser["min_contract_kw"]) == (500, None)
    # A teaser is what an anonymous caller gets, so it is locked and carries
    # them anyway: they decide whether it is worth registering at all.
    assert teaser["locked"] is True

    env.listings.clear()
    env.statements.clear()
    shell = {"delivery_type": "powered_shell", "min_contract_kw": 1000}
    assert _create(env, shell, capacity_mw=40.0).status_code == 200
    teaser = env.client.get("/api/v1/listings").get_json()["items"][0]
    assert (teaser["contiguous_kw"], teaser["min_contract_kw"]) == (None, 1000)
    full = env.client.get("/api/v1/listings/dfw-40", headers=_bearer()).get_json()["listing"]
    assert (full["contiguous_kw"], full["min_contract_kw"]) == (None, 1000)
    # Reserved keys are typed fields, never repeated in the generic object.
    assert "min_contract_kw" not in (full["detail"] or {})


def test_a_patch_bounds_a_block_against_the_capacity_the_write_leaves_on_the_row(env):
    """A PATCH that sets only `detail` is bounded by the STORED capacity; one
    that sets capacity_mw in the same request is bounded by the new value."""
    env.listings.append(_row(detail={"delivery_type": "powered_shell"}, capacity_mw=2.0))
    over = {"detail": {"delivery_type": "powered_shell", "contiguous_kw": 2001}}
    r = env.client.patch("/api/v1/admin/listings/1", headers=ADMIN, json=over)
    assert (r.status_code, _fields(r)) == (400, ["detail.contiguous_kw"]), r.get_json()
    r = env.client.patch("/api/v1/admin/listings/1", headers=ADMIN,
                         json={**over, "capacity_mw": 3.0})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert env.listings[0]["detail"]["contiguous_kw"] == 2001


@pytest.mark.parametrize("country,region", [
    ("US", "north_america"), (" us ", "north_america"), ("United States", "north_america"),
    ("PR", "north_america"), ("MX", "north_america"), ("Germany", "europe"), ("gb", "europe"),
    ("ZA", "middle_east_africa"), ("SG", "asia_pacific"), ("Brazil", "latin_america"),
    ("Atlantis", None), ("", None), (None, None),
])
def test_teaser_region_reads_codes_and_names(env, country, region):
    env.listings.append(_row(detail=None, country=country))
    assert env.client.get("/api/v1/listings").get_json()["items"][0]["region"] == region
