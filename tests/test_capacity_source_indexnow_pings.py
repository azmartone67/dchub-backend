"""Capacity Source: an admin write tells the search engines, off-request.

A listing page is crawlable and sitemapped, but a sitemap only ANSWERS a
crawler — it never summons one. IndexNow pushes the other way: Bing takes the
URL within minutes, and ChatGPT and Perplexity both read Bing's index, so this
is the difference between a listing an AI can cite today and one it finds
whenever the crawl schedule gets around to it.

Exercised as REQUESTS against the real blueprint, the way
test_capacity_source_listing_fields.py does it: `main` is stubbed empty, admin
writes run the routes' real INSERT / UPDATE / DELETE against a stand-in cursor
that fails on any statement it does not expect, and the ping runs on the real
`_dispatch` thread — never a synchronous stand-in, because "does not block the
admin response" is one of the properties under test and a synchronous stand-in
would quietly delete it.

What these pin:
  * a create, an update and a delete each hand IndexNow the listing's own page
    AND the index page, the delete included — /listings/<slug> 404s once the
    listing is withdrawn, and a recrawl is how that URL leaves the index
    instead of sitting there looking live;
  * an unconfigured key skips before a socket is opened, and the admin write
    still succeeds;
  * a repeat inside the dedupe window collapses to nothing, and the same URL
    goes again once the window has passed;
  * a failing endpoint does not raise and a hanging one does not delay the
    admin response;
  * the key reaches no log line and no response body, even when the endpoint
    quotes it back in its rejection.
"""
import logging
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("flask")
from flask import Flask  # noqa: E402

import routes.exclusive_listings as el  # noqa: E402
import routes.indexnow as indexnow  # noqa: E402

ADMIN_KEY = "admin-key-for-indexnow-tests-0123456789"  # secretscan:allow (test placeholder)
ADMIN = {"X-Admin-Key": ADMIN_KEY}
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)

LISTING_URL = "https://dchub.cloud/listings/dfw-40"
INDEX_URL = "https://dchub.cloud/listings"

# How long a test waits for the off-request ping thread. Generous on purpose: it
# bounds a PASS, never a fail-fast, so a loaded CI box costs seconds and not a
# red run.
_PING_WAIT_S = 10.0
_SETTLE_S = 2.0


def _fake_key():
    """A stand-in IndexNow key, assembled at run time.

    Never written as a literal. `scripts/check_no_leaked_credentials.py` stops
    key-SHAPED strings in committed files, and a real IndexNow key is 32 hex
    characters — so a literal of that shape here would be indistinguishable
    from the thing the scanner exists to catch.
    """
    return "ab" * 16


def _row(**over):
    row = {"id": 1, "slug": "dfw-40", "title": "Powered shell — DFW",
           "summary": "Energized next year.", "status": "pocket",
           "tier_required": "registered", "market": "Dallas", "state": "TX",
           "country": "US", "latitude": None, "longitude": None,
           "capacity_mw": 40.0, "asking_price": None, "asking_currency": "USD",
           "detail": None, "contact": None, "owner_id": None,
           "created_at": NOW, "updated_at": NOW, "expires_at": None}
    row.update(over)
    return row


class _Cursor:
    """Runs the admin routes' INSERT, UPDATE and DELETE against env.listings;
    any other statement fails the test."""

    def __init__(self, env):
        self.env, self._one, self._all = env, None, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.env.statements.append(s)
        if s.startswith("INSERT INTO exclusive_listings ("):
            cols = s[s.index("(") + 1:s.index(")")].split(", ")
            row = _row(**dict(zip(cols, params, strict=True)))
            row["id"] = len(self.env.listings) + 1
            self.env.listings.append(row)
            self._one = (row["id"], row["slug"])
        elif s.startswith("UPDATE exclusive_listings SET "):
            head = "UPDATE exclusive_listings SET "
            assignments = s[len(head):s.index(" WHERE id = %s")].split(", ")
            cols = [a.split(" = ")[0] for a in assignments if "%s" in a]
            *values, lid = params
            row = next((r for r in self.env.listings if r["id"] == lid), None)
            if row is not None:
                row.update(dict(zip(cols, values, strict=True)))
            self._one = ((row["id"], row["slug"], row["status"], row["tier_required"])
                         if row else None)
        elif s.startswith("DELETE FROM exclusive_listings WHERE id = %s"):
            (lid,) = params
            gone = [r for r in self.env.listings if r["id"] == lid]
            self.env.listings[:] = [r for r in self.env.listings if r["id"] != lid]
            self._all = [(r["slug"],) for r in gone]
        else:
            raise AssertionError(f"unexpected SQL: {s[:90]}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


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


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setitem(sys.modules, "main", types.ModuleType("main"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setenv("DCHUB_INDEXNOW_KEY", _fake_key())
    # These tests exercise the REAL dispatch, so they lift the test-context
    # suppression the hook applies to every other test file in the suite. The
    # endpoint itself is a fake, so nothing leaves the process.
    monkeypatch.setenv(el._ALLOW_PING_IN_TESTS, "1")
    # The dedupe window is process-local state that OUTLIVES a test. Left dirty,
    # the index URL would read as "recently pinged" for whichever test ran
    # second, and the submission assertions would pass or fail on test ORDER.
    el._INDEXNOW_RECENT.clear()

    e = types.SimpleNamespace(
        listings=[], statements=[], submitted=[], pinged=threading.Event(),
        block=threading.Event(), behaviour=None,
        real_submit=indexnow.submit_to_indexnow)

    def fake_submit(urls):
        # Record BEFORE handing over, so a behaviour that raises or parks is
        # still visible to the test that asked for it.
        e.submitted.append(list(urls))
        e.pinged.set()
        if e.behaviour is None:
            return {"ok": True, "status": 200, "submitted": len(urls)}
        return e.behaviour(urls)

    monkeypatch.setattr(indexnow, "submit_to_indexnow", fake_submit)
    monkeypatch.setattr(el, "_now", lambda: NOW)
    monkeypatch.setattr(el, "_conn", lambda: _Conn(e))
    monkeypatch.setattr(el, "_db_get_listing", lambda ident: next(
        (dict(r) for r in e.listings
         if str(r["id"]) == str(ident) or r["slug"] == str(ident)), None))

    app = Flask(__name__)
    app.register_blueprint(el.exclusive_listings_bp)
    e.client = app.test_client()
    try:
        yield e
    finally:
        # Release anything parked by the hanging-endpoint test before monkeypatch
        # puts the module attributes back, so no daemon thread is still holding
        # this test's fake when the next test installs its own.
        e.block.set()
        el._INDEXNOW_RECENT.clear()


def _wait(env):
    """Block until the off-request ping has run, then hand back what it sent."""
    assert env.pinged.wait(_PING_WAIT_S), "the ping thread never reached IndexNow"
    return env.submitted


def _next_write(env):
    """Reset between two legs of a test: the question in those tests is WHETHER
    each verb pings, not how the window behaves, so each leg starts clean."""
    env.submitted.clear()
    env.pinged.clear()
    el._INDEXNOW_RECENT.clear()


def _until(predicate, seconds=_SETTLE_S):
    """Poll a predicate that a background thread will make true."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.02)
    return predicate()


def _create(env, **body):
    return env.client.post("/api/v1/admin/listings", headers=ADMIN,
                           json={"title": "Powered shell — DFW", "slug": "dfw-40",
                                 "status": "pocket", **body})


# ── every admin write reaches IndexNow ───────────────────────────────────

def test_admin_writes_submit_the_listing_page_and_the_index(env):
    """Create, update and withdraw each hand over BOTH URLs.

    The withdraw is the one that is easy to skip and the one that matters most
    to an index's accuracy: after the DELETE, /listings/dfw-40 is a 404, and
    only a recrawl request takes it back out of Bing.
    """
    assert _create(env).status_code == 200
    assert _wait(env)[0] == [LISTING_URL, INDEX_URL]

    _next_write(env)
    r = env.client.patch("/api/v1/admin/listings/1", headers=ADMIN,
                         json={"status": "public"})
    assert r.status_code == 200
    assert _wait(env)[0] == [LISTING_URL, INDEX_URL]

    _next_write(env)
    r = env.client.delete("/api/v1/admin/listings/1", headers=ADMIN)
    assert r.status_code == 200 and r.get_json()["deleted"] == 1
    assert _wait(env)[0] == [LISTING_URL, INDEX_URL], (
        "a withdrawn listing must still be submitted — its page now 404s, and "
        "the recrawl is what removes it from the index")


def test_a_put_submits_too(env):
    """PUT shares its route with PATCH; pin it so a future split cannot drop it."""
    assert _create(env).status_code == 200
    _wait(env)

    _next_write(env)
    assert env.client.put("/api/v1/admin/listings/1", headers=ADMIN,
                          json={"summary": "Energized sooner."}).status_code == 200
    assert _wait(env)[0] == [LISTING_URL, INDEX_URL]


# ── the test-context suppression ─────────────────────────────────────────

def test_a_test_run_never_announces_a_listing_to_a_search_engine(env, monkeypatch):
    """Every other test file that writes a listing must stay off the network.

    This is the property `tests/_no_network` enforces at the socket; pinning it
    here names WHY a production-shaped code path is held back in a test process,
    so a later reader does not "fix" the gate away.
    """
    monkeypatch.delenv(el._ALLOW_PING_IN_TESTS, raising=False)

    assert _create(env).status_code == 200

    assert not _until(lambda: bool(env.submitted), seconds=0.5)
    assert env.submitted == []


def test_only_the_test_context_holds_the_ping_back(monkeypatch):
    """The gate is keyed on the test context and on nothing else.

    Were it keyed on anything a serving process also has, the pings would be
    inert in production and this suite would still look green — so the lifted
    case is asserted, not just the suppressed one.
    """
    monkeypatch.delenv(el._ALLOW_PING_IN_TESTS, raising=False)
    assert el._ping_suppressed(), "a pytest process must be suppressed"

    monkeypatch.setenv(el._ALLOW_PING_IN_TESTS, "1")
    assert el._ping_suppressed() is None, (
        "with the test context lifted, nothing else may hold the ping back — a "
        "serving process has no pytest loaded and must reach IndexNow")


# ── an unconfigured key ──────────────────────────────────────────────────

def test_no_key_skips_before_any_request_is_made(monkeypatch):
    """The real client with the key set empty: no socket, no partial payload.

    Asserted against routes.indexnow itself rather than through a route, so the
    guard is pinned where it lives — at the one choke point every caller passes
    through, and not in one caller's private copy of it.
    """
    monkeypatch.setenv("DCHUB_INDEXNOW_KEY", "")
    opened = []
    monkeypatch.setattr(indexnow.urllib.request, "urlopen",
                        lambda *a, **k: opened.append(a))

    out = indexnow.submit_to_indexnow([LISTING_URL, INDEX_URL])

    assert opened == [], "no endpoint may be contacted without a key"
    assert out["skipped"] is True and out["reason"] == "no_key"
    assert out["ok"] is False and out["submitted"] == 0


def test_no_key_still_lets_the_admin_write_through(env, monkeypatch):
    """An unconfigured key is a skipped ping, never a failed admin call."""
    monkeypatch.setenv("DCHUB_INDEXNOW_KEY", "")
    monkeypatch.setattr(indexnow, "submit_to_indexnow", env.real_submit)
    opened = []
    monkeypatch.setattr(indexnow.urllib.request, "urlopen",
                        lambda *a, **k: opened.append(a))

    r = _create(env)

    assert r.status_code == 200 and r.get_json()["ok"] is True
    # Give the dispatched thread a real chance to make the call it must not make.
    assert not _until(lambda: bool(opened), seconds=1.0), (
        "an unconfigured key must not reach the network")


# ── the dedupe window ────────────────────────────────────────────────────

def test_repeat_inside_the_window_collapses_and_one_after_it_goes(env, monkeypatch):
    """The same URL twice inside the window is one submission; after it, two.

    A batch edit is the case this protects: twenty listings saved in a row would
    otherwise hand IndexNow the index page twenty times, and a host that
    resubmits unchanged URLs is exactly what the protocol de-prioritises.
    """
    monkeypatch.setattr(el, "_INDEXNOW_PING_WINDOW", timedelta(seconds=30))
    assert el._indexnow_fresh([LISTING_URL, INDEX_URL]) == [LISTING_URL, INDEX_URL]
    assert el._indexnow_fresh([LISTING_URL, INDEX_URL]) == [], (
        "a repeat inside the window must submit nothing at all")

    # A new URL alongside a repeated one: only the new one goes.
    other = "https://dchub.cloud/listings/phx-90"
    assert el._indexnow_fresh([other, INDEX_URL]) == [other]

    # Past the window the same URL is submittable again — a listing edited this
    # morning and again this afternoon has to be announced twice.
    monkeypatch.setattr(el, "_INDEXNOW_PING_WINDOW", timedelta(seconds=0.25))
    time.sleep(0.4)
    assert el._indexnow_fresh([LISTING_URL, INDEX_URL]) == [LISTING_URL, INDEX_URL]


def test_a_repeated_admin_write_pings_once(env):
    """Through the route, not only the helper: two saves, one submission."""
    assert _create(env).status_code == 200
    _wait(env)

    assert env.client.patch("/api/v1/admin/listings/1", headers=ADMIN,
                            json={"status": "public"}).status_code == 200
    # The second write's URLs are dropped before dispatch, so nothing new can
    # arrive; a settle window proves it stays that way rather than arriving late.
    assert not _until(lambda: len(env.submitted) > 1)
    assert env.submitted == [[LISTING_URL, INDEX_URL]]


# ── a bad endpoint never touches the admin call ──────────────────────────

def test_a_failing_endpoint_does_not_raise(env, caplog):
    """An exception on the ping thread is logged, and the write still reads 200."""
    caplog.set_level(logging.INFO, logger="routes.exclusive_listings")

    def blow_up(urls):
        raise RuntimeError("indexnow endpoint unreachable")

    env.behaviour = blow_up

    r = _create(env)

    assert r.status_code == 200 and r.get_json()["ok"] is True
    _wait(env)
    assert _until(lambda: "indexnow ping not accepted" in caplog.text)
    assert "RuntimeError" in caplog.text


def test_a_hanging_endpoint_does_not_delay_the_admin_response(env):
    """The edge gives an admin write 15 seconds; a ping may take far longer.

    The fake parks until this test releases it. Were the ping inline, the admin
    response could not come back before that release.
    """
    def park(urls):
        env.block.wait(30)
        return {"ok": True}

    env.behaviour = park

    started = time.monotonic()
    r = _create(env)
    elapsed = time.monotonic() - started

    assert r.status_code == 200
    assert elapsed < 2.0, (
        f"the admin write waited {elapsed:.1f}s on the search endpoint; the edge "
        f"gives the whole call 15s")
    _wait(env)                      # the ping really is in flight, and still parked
    assert not env.block.is_set()
    env.block.set()


# ── the key stays out of the record ──────────────────────────────────────

def test_the_key_reaches_no_log_line_and_no_response_body(env, caplog):
    """An engine that rejects a submission tends to quote the payload back.

    Whatever this logs is scrubbed, and the endpoint's own response body is not
    logged at all. The admin response carries no indexing detail either.
    """
    caplog.set_level(logging.INFO, logger="routes.exclusive_listings")
    key = _fake_key()
    env.behaviour = lambda urls: {
        "ok": False, "status": 403,
        "reason": f"key not valid: {key}",
        "error": f"<html>key {key} not found at keyLocation</html>",
        "endpoint": "https://www.bing.com/indexnow",
    }

    r = _create(env)

    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert key not in body and "indexnow" not in body.lower()

    _wait(env)
    # Anchor first: an empty log would make the "key is not in it" half
    # vacuously true.
    assert _until(lambda: "indexnow ping not accepted" in caplog.text), (
        "the rejection must be recorded — a silent failure is worse than a "
        "redacted one")
    assert "<redacted>" in caplog.text
    for record in caplog.records:
        assert key not in record.getMessage()
    assert key not in caplog.text
