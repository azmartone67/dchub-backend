"""Every LinkedIn post must reach the DC Hub Substack — web-only by default.

The mirror rides the existing multiplatform amplifier: linkedin_posts is the
one ledger EVERY LinkedIn publish path writes to (linkedin_poster.
post_to_linkedin, content_publisher._post_to_linkedin, linkedin_autopost,
dchub_daily_automation, intelligence_engine all INSERT there), so hooking the
amplifier's sweep covers all of them instead of patching five call sites.

These drive the real functions with fakes rather than reading the source, so
each one fails when the behaviour it names changes. The one exception is
marked: the sweep predicate is asserted as SQL text because the query is
Postgres-specific and CI has no database to run it against.
"""
import importlib
import json
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def mod(monkeypatch):
    """Fresh module with no Substack env leaking in from the suite."""
    for var in ("SUBSTACK_SID", "SUBSTACK_USER_ID", "SUBSTACK_SEND_EMAIL",
                "SUBSTACK_DRAFT_ONLY", "SUBSTACK_AMPLIFY_DISABLE",
                "SUBSTACK_PUBLICATION_URL", "SUBSTACK_AUDIENCE",
                "MULTIPLATFORM_AMPLIFIER_DRY_RUN",
                "MULTIPLATFORM_AMPLIFIER_DISABLE"):
        monkeypatch.delenv(var, raising=False)
    m = importlib.import_module("routes.multiplatform_amplifier")
    m._SUBSTACK_USER_ID_CACHE.clear()
    return m


SAMPLE = ("DCPI Mover · 24h\n"
          "SPP queue depth fell 11% week over week.\n"
          "The next GW lands where curtailment and a short queue overlap — "
          "not in Ashburn.\n"
          "Source: DC Hub, CC-BY-4.0.")
LINK = "https://dchub.cloud/radar"


# ── Fakes ────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _FakeSession:
    """Stands in for the authenticated requests.Session."""

    def __init__(self, draft_id=4242, slug="dcpi-mover-24h"):
        self.calls = []
        self.draft_id = draft_id
        self.slug = slug

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        if url.endswith("/api/v1/drafts"):
            return _FakeResp(200, {"id": self.draft_id})
        if url.endswith(f"/drafts/{self.draft_id}/publish"):
            return _FakeResp(200, {"slug": self.slug})
        return _FakeResp(404, {}, f"unexpected POST {url}")

    def get(self, url, timeout=None):
        self.calls.append(("GET", url, None))
        if url.endswith("/prepublish"):
            return _FakeResp(200, {})
        return _FakeResp(404, {}, f"unexpected GET {url}")

    def published_body(self):
        for method, url, body in self.calls:
            if method == "POST" and url.endswith("/publish"):
                return body
        return None

    def draft_body(self):
        for method, url, body in self.calls:
            if method == "POST" and url.endswith("/api/v1/drafts"):
                return body
        return None


class _FakeCursor:
    def __init__(self, sql_log, candidates=()):
        self.sql_log = sql_log
        self.candidates = list(candidates)
        self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(str(sql).split())
        self.sql_log.append((self._last, params))

    def fetchone(self):
        if "COUNT(DISTINCT source_post_id)" in self._last:
            return (0,)
        if ("FROM multiplatform_amplifier_log" in self._last
                and "target_platform = %s" in self._last):
            return (1,)          # → already amplified
        return None

    def fetchall(self):
        if "FROM linkedin_posts lp" in self._last:
            return [(i,) for i in self.candidates]
        return []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, sql_log, candidates=()):
        self.sql_log = sql_log
        self.candidates = list(candidates)
        self.autocommit = True

    def cursor(self):
        return _FakeCursor(self.sql_log, self.candidates)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# ── The wiring ───────────────────────────────────────────────────────

def test_substack_is_a_default_target_with_a_framing(mod):
    """A platform in PLATFORMS_DEFAULT with no build_framings key is dropped
    in silence by `if plat not in framings: continue` — it would look
    configured and post nothing. Assert the two agree, both directions."""
    framings = mod.build_framings(SAMPLE, LINK)
    assert "substack" in mod.PLATFORMS_DEFAULT
    assert set(mod.PLATFORMS_DEFAULT) <= set(framings), (
        "platform(s) with no framing: "
        f"{set(mod.PLATFORMS_DEFAULT) - set(framings)}")
    assert set(framings) <= set(mod.PLATFORMS_DEFAULT), (
        "framing(s) no platform asks for: "
        f"{set(framings) - set(mod.PLATFORMS_DEFAULT)}")
    assert framings["substack"].strip()


def test_dispatcher_routes_substack_to_the_substack_poster(mod, monkeypatch):
    """The framing existing is not the same as the dispatcher sending it."""
    seen = {}
    monkeypatch.setattr(mod, "post_to_substack",
                        lambda content, link="", image_url="": seen.update(
                            content=content, link=link) or
                        {"ok": True, "url": "https://x/p/y", "error": ""})
    monkeypatch.setattr(mod, "_db_conn", lambda: _FakeConn([]))
    # _already_amplified must say "no" here, so give the cursor no prior row.
    monkeypatch.setattr(mod, "_already_amplified", lambda *a, **k: False)
    out = mod.amplify_to_all(source_post_id=101, source_text=SAMPLE,
                             source_link=LINK, platforms=("substack",))
    assert out["results"]["substack"]["status"] == "posted", out
    assert seen.get("content", "").startswith("DCPI Mover")
    assert seen.get("link") == LINK


# ── Web-only publishing ──────────────────────────────────────────────

def test_publish_does_not_email_subscribers_by_default(mod, monkeypatch):
    """★ The decision this feature turns on. LinkedIn publishes 4x/day; the
    mirror goes live on the web WITHOUT a newsletter send unless asked."""
    sess = _FakeSession()
    monkeypatch.setattr(mod, "_substack_session", lambda: sess)
    monkeypatch.setenv("SUBSTACK_USER_ID", "77")

    out = mod.post_to_substack(mod.frame_substack(SAMPLE, LINK), LINK)

    assert out["ok"] is True, out
    assert out["url"] == "https://dchubcloud.substack.com/p/dcpi-mover-24h"
    body = sess.published_body()
    assert body is not None, "no publish call was made"
    assert body["send"] is False, f"mirror emailed the list: {body}"
    assert body["share_automatically"] is False


def test_send_email_env_turns_the_newsletter_send_on(mod, monkeypatch):
    """The opposite arm — proves the flag is read, not just defaulted."""
    sess = _FakeSession()
    monkeypatch.setattr(mod, "_substack_session", lambda: sess)
    monkeypatch.setenv("SUBSTACK_USER_ID", "77")
    monkeypatch.setenv("SUBSTACK_SEND_EMAIL", "1")

    out = mod.post_to_substack(mod.frame_substack(SAMPLE, LINK), LINK)

    assert out["ok"] is True, out
    assert sess.published_body()["send"] is True


def test_draft_only_stops_before_publishing(mod, monkeypatch):
    sess = _FakeSession()
    monkeypatch.setattr(mod, "_substack_session", lambda: sess)
    monkeypatch.setenv("SUBSTACK_USER_ID", "77")
    monkeypatch.setenv("SUBSTACK_DRAFT_ONLY", "1")

    out = mod.post_to_substack(mod.frame_substack(SAMPLE, LINK), LINK)

    assert out["ok"] is True and out["error"] == "draft_only"
    assert sess.published_body() is None, "draft-only mode published anyway"


# ── Payload shape ────────────────────────────────────────────────────

def test_draft_carries_the_post_text_as_prosemirror(mod, monkeypatch):
    """draft_body is a JSON *string* of a ProseMirror doc — a plain string
    body is accepted by the POST and renders as an empty post."""
    sess = _FakeSession()
    monkeypatch.setattr(mod, "_substack_session", lambda: sess)
    monkeypatch.setenv("SUBSTACK_USER_ID", "77")

    mod.post_to_substack(mod.frame_substack(SAMPLE, LINK), LINK)
    draft = sess.draft_body()

    assert draft["draft_title"] == "DCPI Mover · 24h"
    assert draft["draft_bylines"] == [{"id": 77, "is_guest": False}]
    doc = json.loads(draft["draft_body"])
    assert doc["type"] == "doc"
    texts = [n["content"][0]["text"] for n in doc["content"] if n.get("content")]
    assert any("curtailment and a short queue overlap" in t for t in texts), texts
    assert any(LINK in t for t in texts), "source link missing from the body"


def test_body_never_emits_an_empty_content_array(mod):
    """Substack's schema rejects a paragraph with content: [] — an empty
    post has to be a bare paragraph node."""
    doc = mod._substack_body_doc(["", "   "])
    assert doc["content"] == [{"type": "paragraph"}]


def test_cookie_accepts_every_shape_a_browser_copy_produces(mod, monkeypatch):
    bare = "s%3AabcDEF.ghiJKL"
    for raw, expected in ((bare, bare),
                          (f"substack.sid={bare}", bare),
                          (f"ajs_anonymous_id=zz; substack.sid={bare}; x=1", bare)):
        monkeypatch.setenv("SUBSTACK_SID", raw)
        assert mod._substack_cookies().get("substack.sid") == expected, raw


def test_missing_cookie_returns_an_error_and_makes_no_network_call(mod, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("built a session with no SUBSTACK_SID")
    monkeypatch.setattr(mod.requests, "Session", _boom)
    out = mod.post_to_substack(SAMPLE, LINK)
    assert out["ok"] is False
    assert out["error"] == "no_substack_session"


def test_expired_session_is_reported_as_unauthenticated(mod, monkeypatch):
    """A dead cookie must not read as a content bug. /user/profile/self
    returning no id is what an expired SUBSTACK_SID looks like."""
    sess = _FakeSession()
    monkeypatch.setattr(mod, "_substack_session", lambda: sess)
    monkeypatch.setattr(mod, "_substack_user_id", lambda s: 0)
    out = mod.post_to_substack(SAMPLE, LINK)
    assert out["ok"] is False
    assert "unauthenticated" in out["error"]
    assert sess.draft_body() is None, "posted a draft with no byline"


# ── The re-log clobber ───────────────────────────────────────────────

def test_already_amplified_platform_is_not_re_logged(mod, monkeypatch):
    """_record upserts ON CONFLICT DO UPDATE SET status = EXCLUDED.status.
    Logging the 'skipped_cap' placeholder for a platform that already posted
    overwrites its real 'posted' row and blanks target_post_url — after which
    _already_amplified() and the sweep both read it as never sent, and the
    next sweep posts it a SECOND time."""
    sql_log = []
    monkeypatch.setattr(mod, "_db_conn", lambda: _FakeConn(sql_log))
    out = mod.amplify_to_all(source_post_id=55, source_text=SAMPLE,
                             source_link=LINK, platforms=("bluesky",))

    assert out["results"]["bluesky"]["error"] == "already_amplified"
    inserts = [s for s, _ in sql_log
               if "INSERT INTO multiplatform_amplifier_log" in s]
    assert inserts == [], f"clobbered its own posted row: {inserts}"


# ── The sweep predicate ──────────────────────────────────────────────

def test_sweep_skips_failed_linkedin_posts_and_backfills_substack(mod, monkeypatch):
    """SOURCE RATCHET, not an execution proof — the query is Postgres-specific
    (NOW(), INTERVAL, DEFAULT) and CI has no database. It asserts the two
    predicates are in the SQL the sweep actually sends:

      1. linkedin_posts logs FAILURES too (status='failed', posted_at
         defaulting to NOW()), so an unfiltered window mirrors posts LinkedIn
         refused.
      2. a post whose other platforms already went out still needs its
         Substack row, or the mirror never runs and never retries.
    """
    sql_log = []
    monkeypatch.setattr(mod, "_db_conn", lambda: _FakeConn(sql_log))
    mod.auto_sweep_recent()

    sweeps = [s for s, _ in sql_log if "FROM linkedin_posts lp" in s]
    assert len(sweeps) == 1, sweeps
    sweep = sweeps[0]
    assert "COALESCE(lp.status, 'success') = 'success'" in sweep, sweep
    assert "s.target_platform = 'substack'" in sweep, sweep
    assert "OR NOT EXISTS" in sweep, sweep


# ── Scoped fan-out ───────────────────────────────────────────────────

def test_sweep_scopes_the_fan_out_to_the_platforms_it_is_given(mod, monkeypatch):
    """★ Waking this lane must NOT also restart Bluesky and Mastodon, dark
    since 2026-06-07. The Substack mirror sweeps with platforms=("substack",);
    an unscoped pass-through would publish to two public accounts nobody
    asked to restart."""
    seen = {}
    monkeypatch.setattr(mod, "_db_conn", lambda: _FakeConn([], candidates=[611]))
    monkeypatch.setattr(mod, "amplify_to_all",
                        lambda source_post_id=0, platforms=None, **kw:
                        seen.update(pid=source_post_id, platforms=platforms)
                        or {"results": {}})

    out = mod.auto_sweep_recent(platforms=("substack",))

    assert out["swept"] == 1, out
    assert seen["pid"] == 611
    assert seen["platforms"] == ("substack",), seen
    assert out["platforms"] == ["substack"]


def test_sweep_defaults_to_every_platform_when_unscoped(mod, monkeypatch):
    """The opposite arm — the scoping is a parameter, not a hard-coded
    narrowing that would strand the other four platforms forever."""
    seen = {}
    monkeypatch.setattr(mod, "_db_conn", lambda: _FakeConn([], candidates=[611]))
    monkeypatch.setattr(mod, "amplify_to_all",
                        lambda source_post_id=0, platforms=None, **kw:
                        seen.update(platforms=platforms) or {"results": {}})

    mod.auto_sweep_recent()

    assert seen["platforms"] == tuple(mod.PLATFORMS_DEFAULT), seen


def test_endpoint_forwards_the_platforms_query_arg(mod, monkeypatch):
    """The workflow drives this over HTTP with ?platforms=substack — the
    scoping is worthless if the route drops it."""
    from flask import Flask

    seen = {}
    monkeypatch.setattr(mod, "_admin_or_cron_authorized", lambda: True)
    monkeypatch.setattr(mod, "auto_sweep_recent",
                        lambda platforms=None:
                        seen.update(platforms=platforms) or {"swept": 0})
    app = Flask(__name__)
    app.register_blueprint(mod.multiplatform_amplifier_bp)
    client = app.test_client()

    assert client.post(
        "/api/v1/admin/multiplatform/auto-sweep?platforms=substack"
    ).status_code == 200
    assert seen["platforms"] == ("substack",), seen

    client.post("/api/v1/admin/multiplatform/auto-sweep?platforms=substack,bluesky")
    assert seen["platforms"] == ("substack", "bluesky"), seen

    client.post("/api/v1/admin/multiplatform/auto-sweep")
    assert seen["platforms"] is None, seen
