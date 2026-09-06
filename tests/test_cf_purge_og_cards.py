"""The public OG-card purge must not become a public purge-anything.

Card designs change, and since #3938 gave /api/v1/og/* a 7-day edge TTL a
redesign no longer reaches LinkedIn until that TTL expires — #3979 re-skinned
the whole fleet and would have served the old art for a week. This endpoint is
the missing step, and it recurs on every card change.

It is PUBLIC, like purge/markets-fix and purge/frontend-static. That is only
safe because of three properties, which are what these tests pin:

  1. Nothing the CALLER sends reaches CF. A public endpoint that purges what it
     is handed lets anyone evict any path on the zone.
  2. The list is DERIVED from the og:image each page actually publishes — a
     hardcoded list rots the first time a headline is edited, because a card URL
     embeds the title.
  3. Only OUR hosts and OUR card paths are ever purged, whatever a page claims.

No network: `requests` is stubbed throughout.
"""
import re

import pytest

flask = pytest.importorskip("flask")


def _page(og):
    return f'<html><head><meta property="og:image" content="{og}"></head></html>'


class _Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status
        self.headers = {"content-type": "application/json"}

    def json(self):
        return {"success": True, "result": {"id": "zone"}}


@pytest.fixture
def cf(monkeypatch):
    from routes import cf_purge as m
    monkeypatch.setattr(m, "_CF_API_TOKEN", "stub-token")
    monkeypatch.setattr(m, "_CF_ZONE_ID", "stub-zone")
    m._og_purge_last[0] = 0.0          # clear the cooldown between tests
    return m


@pytest.fixture
def client(cf):
    app = flask.Flask(__name__)
    app.register_blueprint(cf.cf_purge_bp)
    return app.test_client()


def _wire(monkeypatch, cf, og_for_page, sent):
    """Stub requests: page GETs return an og:image; the CF POST records files."""
    import requests

    def fake_get(url, **kw):
        path = "/" + url.split("dchub.cloud", 1)[-1].lstrip("/")
        return _Resp(_page(og_for_page(path)))

    def fake_post(url, **kw):
        sent.extend((kw.get("json") or {}).get("files", []))
        return _Resp()

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)


CARD = "https://api.dchub.cloud/api/v1/og/dynamic.png?style=editorial&title=X"


# ── 1. the caller cannot steer the purge ───────────────────────────────────

def test_caller_supplied_urls_are_ignored(monkeypatch, cf, client):
    """The whole safety argument. A body or query naming another path must not
    reach CF — otherwise this is a public 'evict anything' button."""
    sent = []
    _wire(monkeypatch, cf, lambda p: CARD, sent)
    r = client.post("/api/v1/cf/purge/og-cards?url=https://dchub.cloud/pricing",
                    json={"urls": ["https://dchub.cloud/",
                                   "https://dchub.cloud/enterprise"],
                          "url": "https://evil.example/x"})
    assert r.status_code == 200
    for hostile in ("https://dchub.cloud/pricing", "https://dchub.cloud/enterprise",
                    "https://evil.example/x"):
        assert hostile not in sent, f"caller-supplied URL reached CF: {hostile}"
    assert sent, "nothing was purged at all"


def test_only_our_hosts_and_card_paths_are_purged(monkeypatch, cf, client):
    """A page can point og:image anywhere. Off-allowlist targets are dropped
    and REPORTED, not silently skipped."""
    sent = []
    hostile = "https://evil.example/api/v1/og/dynamic.png?style=editorial"
    _wire(monkeypatch, cf, lambda p: hostile if p == "/news" else CARD, sent)
    r = client.get("/api/v1/cf/purge/og-cards")
    body = r.get_json()
    assert hostile not in sent, "purged a third-party host"
    assert any("allowlist" in n for n in body["notes"]), \
        f"the dropped URL was not reported: {body['notes']}"


def test_a_non_card_path_on_our_own_host_is_still_dropped(monkeypatch, cf, client):
    sent = []
    _wire(monkeypatch, cf, lambda p: "https://dchub.cloud/pricing", sent)
    client.get("/api/v1/cf/purge/og-cards")
    assert "https://dchub.cloud/pricing" not in sent, \
        "purged a page URL, not a card — this endpoint must only touch cards"


# ── 2. derived, not hardcoded ──────────────────────────────────────────────

def test_the_list_follows_the_published_og_image(monkeypatch, cf, client):
    """A card URL embeds the page title, so an edited headline changes the URL.
    The endpoint must purge what the page publishes NOW."""
    sent = []
    renamed = ("https://api.dchub.cloud/api/v1/og/dynamic.png"
               "?style=editorial&title=A+brand+new+headline")
    _wire(monkeypatch, cf, lambda p: renamed, sent)
    client.get("/api/v1/cf/purge/og-cards")
    assert renamed in sent, "did not purge the URL the page actually publishes"


def test_the_static_fallback_card_is_included(monkeypatch, cf, client):
    """/images/og-default.png is served on pages that never touch the generator,
    so a redesign does not regenerate it — but it still goes stale at the edge."""
    sent = []
    _wire(monkeypatch, cf, lambda p: CARD, sent)
    client.get("/api/v1/cf/purge/og-cards")
    assert "https://dchub.cloud/images/og-default.png" in sent


def test_a_page_that_fails_is_reported_not_swallowed(monkeypatch, cf, client):
    import requests
    sent = []

    def fake_get(url, **kw):
        if url.rstrip("/").endswith("dchub.cloud"):
            return _Resp("", status=503)
        return _Resp(_page(CARD))

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post",
                        lambda url, **kw: (sent.extend((kw.get("json") or {}).get("files", [])), _Resp())[1])
    body = client.get("/api/v1/cf/purge/og-cards").get_json()
    assert any("503" in n for n in body["notes"]), \
        f"a failed page read vanished from the report: {body['notes']}"


# ── 3. batching + cooldown ─────────────────────────────────────────────────

def test_more_than_thirty_urls_are_chunked(monkeypatch, cf, client):
    """CF rejects more than 30 files per purge call."""
    import requests
    batches = []
    monkeypatch.setattr(cf, "_OG_PAGES", tuple(f"/p{i}" for i in range(40)))
    monkeypatch.setattr(requests, "get",
                        lambda url, **kw: _Resp(_page(CARD + "&p=" + url[-3:])))
    monkeypatch.setattr(requests, "post",
                        lambda url, **kw: (batches.append(len((kw.get("json") or {}).get("files", []))), _Resp())[1])
    client.get("/api/v1/cf/purge/og-cards")
    assert batches, "nothing was sent"
    assert max(batches) <= 30, f"sent a batch of {max(batches)} — CF caps at 30"
    assert sum(batches) > 30, "the test did not actually exercise chunking"


def test_a_second_call_within_the_cooldown_does_not_re_purge(monkeypatch, cf, client):
    calls = []
    _wire(monkeypatch, cf, lambda p: CARD, calls)
    first = client.get("/api/v1/cf/purge/og-cards").get_json()
    n_after_first = len(calls)
    second = client.get("/api/v1/cf/purge/og-cards").get_json()
    assert first["ok"] is True
    assert second.get("skipped") == "cooldown"
    assert len(calls) == n_after_first, "the cooldown did not prevent a re-purge"
    # A cooldown is not a failure — purges are idempotent, so "already done" is
    # the honest answer and must not read as an error.
    assert second["ok"] is True


def test_the_endpoint_is_registered_and_public():
    from routes import cf_purge as m
    app = flask.Flask(__name__)
    app.register_blueprint(m.cf_purge_bp)
    rules = {str(r): sorted(r.methods & {"GET", "POST"})
             for r in app.url_map.iter_rules()}
    assert "/api/v1/cf/purge/og-cards" in rules
    assert rules["/api/v1/cf/purge/og-cards"] == ["GET", "POST"]
