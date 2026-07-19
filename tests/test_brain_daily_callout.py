"""tests/test_brain_daily_callout.py — daily morning callout email.

Post-mortem context (2026-07-18): the public /press page froze at
2026-06-22 for ~26 days while the DB gained releases daily; every press
detector measured the DB end, so nothing fired. These tests pin the two
halves of the fix:

  · the public-surface staleness math (edge date vs DB date) — the exact
    blindness that let the stall run silent;
  · the send-path guards (kill switch, recipient, provider, per-day claim)
    so the callout can be trusted to fire once each morning and to say so
    legibly when it can't.

NO real network, NO DB — sections and senders are stubbed; pure logic
otherwise. Run: python3 -m pytest tests/test_brain_daily_callout.py -v
"""
import datetime

import pytest

dc = pytest.importorskip("routes.brain_daily_callout")


# ── newest_date_in_html ───────────────────────────────────────────────

def test_newest_date_picks_max():
    html = "x 2026-05-12 y 2026-06-22 z 2026-06-01"
    assert dc.newest_date_in_html(html) == datetime.date(2026, 6, 22)


def test_newest_date_skips_future_and_ancient():
    future = (datetime.date.today()
              + datetime.timedelta(days=30)).isoformat()
    html = f"a {future} b 2019-01-01 c 2026-06-22"
    assert dc.newest_date_in_html(html) == datetime.date(2026, 6, 22)


def test_newest_date_none_on_no_dates():
    assert dc.newest_date_in_html("no dates here") is None
    assert dc.newest_date_in_html("") is None
    assert dc.newest_date_in_html(None) is None


def test_newest_date_skips_invalid_calendar_dates():
    # 2026-02-31 matches the regex shape but is not a real date.
    assert dc.newest_date_in_html("2026-02-31 2026-03-01") == \
        datetime.date(2026, 3, 1)


# ── press_surface_report staleness verdicts ───────────────────────────

def _patch_db_newest(monkeypatch, newest):
    """Stub the episode_analytics read helpers press_surface_report uses."""
    import routes.episode_analytics as ea

    class _Ctx:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ea, "_read_db", lambda: _Ctx())
    monkeypatch.setattr(ea, "_rows", lambda conn, sql, args=None: [(newest,)])


def test_surface_stale_when_page_trails_db(monkeypatch):
    """The July stall scenario: DB fresh, page frozen 26 days back."""
    _patch_db_newest(monkeypatch, datetime.date(2026, 7, 18))
    report = dc.press_surface_report(fetch=lambda url: "newest 2026-06-22")
    assert report["db_newest"] == datetime.date(2026, 7, 18)
    assert len(report["pages"]) == len(dc.PUBLIC_PRESS_PAGES)
    for page in report["pages"]:
        assert page["stale"] is True
        assert page["lag_days"] == 26


def test_surface_fresh_within_threshold(monkeypatch):
    _patch_db_newest(monkeypatch, datetime.date(2026, 7, 18))
    report = dc.press_surface_report(fetch=lambda url: "posted 2026-07-17")
    assert all(p["stale"] is False for p in report["pages"])


def test_surface_unreachable_is_error_not_stale(monkeypatch):
    """A fetch failure must never manufacture a stall (UNKNOWN, not red)."""
    _patch_db_newest(monkeypatch, datetime.date(2026, 7, 18))
    report = dc.press_surface_report(fetch=lambda url: None)
    for page in report["pages"]:
        assert page["stale"] is False
        assert page["error"] == "unreachable"


def test_surface_dateless_page_with_db_rows_is_stale(monkeypatch):
    """A reachable page showing NO date while the DB has releases is a
    broken surface, not an unknown."""
    _patch_db_newest(monkeypatch, datetime.date(2026, 7, 18))
    report = dc.press_surface_report(fetch=lambda url: "<html>empty</html>")
    assert all(p["stale"] is True for p in report["pages"])


# ── send-path guards ──────────────────────────────────────────────────

def _arm_send_env(monkeypatch):
    monkeypatch.setenv("BRAIN_DAILY_CALLOUT_ENABLED", "1")
    monkeypatch.setenv("BRAIN_DIGEST_EMAIL", "op@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")


def test_kill_switch_blocks_send(monkeypatch):
    monkeypatch.setenv("BRAIN_DAILY_CALLOUT_ENABLED", "0")
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("composed"))
    monkeypatch.setattr(dc, "compose_daily_callout", boom)
    assert dc.send_daily_callout() == {"sent": False, "skipped": "disabled"}


def test_default_is_enabled(monkeypatch):
    monkeypatch.delenv("BRAIN_DAILY_CALLOUT_ENABLED", raising=False)
    assert dc._enabled() is True


def test_no_recipient_skips(monkeypatch):
    monkeypatch.setenv("BRAIN_DAILY_CALLOUT_ENABLED", "1")
    monkeypatch.delenv("BRAIN_DIGEST_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_ALERT_EMAIL", raising=False)
    assert dc.send_daily_callout()["skipped"] == "no_recipient"


def test_no_provider_skips(monkeypatch):
    monkeypatch.setenv("BRAIN_DAILY_CALLOUT_ENABLED", "1")
    monkeypatch.setenv("BRAIN_DIGEST_EMAIL", "op@example.com")
    for k in ("RESEND_API_KEY", "DCHUB_RESEND_API_KEY", "SENDGRID_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert dc.send_daily_callout()["skipped"] == "no_provider"


def test_already_sent_today_skips(monkeypatch):
    _arm_send_env(monkeypatch)
    monkeypatch.setattr(dc, "_claim_today", lambda force=False: (False, None))
    assert dc.send_daily_callout()["skipped"] == "already_sent_today"


_STUB_DIGEST = {
    "generated_at": "2026-07-18T12:00:00+00:00",
    "surface": {"db_newest": datetime.date(2026, 7, 18), "pages": []},
    "stale_pages": [{"url": "https://dchub.cloud/press",
                     "newest_visible": "2026-06-22", "lag_days": 26,
                     "stale": True, "error": None}],
    "lanes": {"stalled": [{"key": "twitter_publish",
                           "label": "X/Twitter publishes",
                           "age_hours": 130.0,
                           "reasons": ["gap: newest activity 130.0h ago "
                                       "(expected max 120h)"],
                           "actuator": "X publisher: "
                                       "TWITTER_PUBLISHER_ENABLED"}],
              "unknown": [], "lanes_checked": 14, "error": None},
    "chronic": [{"issue": "brand_surface_dormant:bs_translator",
                 "url": "/x", "detector": "brand_radar",
                 "hours_open": 828.0, "reobs_per_day": 2.1,
                 "chronic_score": 1738.8}],
    "flow": {"opened_24h": 56, "resolved_24h": 390, "open_now": 67},
    "human": {"needs_human_merge": [
        {"number": 1612, "title": "fix: thing", "kind": "pr",
         "age_days": 3.0, "url": "https://github.com/x/1612"}],
        "drift_issue": {"number": 1580,
                        "title": "[white-glove] listing copy drift",
                        "url": "https://github.com/x/1580"},
        "error": None},
    "subject": "[DC Hub brain] daily callout — 2 silent pipelines, "
               "67 open findings",
}


def test_happy_path_sends_once(monkeypatch):
    _arm_send_env(monkeypatch)
    monkeypatch.setattr(dc, "_claim_today", lambda force=False: (True, None))
    monkeypatch.setattr(dc, "_stamp_sent", lambda *a, **k: None)
    monkeypatch.setattr(dc, "compose_daily_callout", lambda: dict(_STUB_DIGEST))
    calls = []
    import email_fallback

    def _fake_send(to, subject, html_content=None, text_content=None,
                   from_email=None, from_name=None):
        calls.append({"to": to, "subject": subject, "text": text_content})
        return True

    monkeypatch.setattr(email_fallback, "send_email_resilient", _fake_send)
    out = dc.send_daily_callout()
    assert out["sent"] is True and out["to"] == "op@example.com"
    assert len(calls) == 1
    assert "silent pipelines" in calls[0]["subject"]


def test_send_failure_releases_claim(monkeypatch):
    _arm_send_env(monkeypatch)
    monkeypatch.setattr(dc, "_claim_today", lambda force=False: (True, None))
    monkeypatch.setattr(dc, "compose_daily_callout", lambda: dict(_STUB_DIGEST))
    released = []
    monkeypatch.setattr(dc, "_release_claim", lambda: released.append(1))
    import email_fallback
    monkeypatch.setattr(email_fallback, "send_email_resilient",
                        lambda *a, **k: False)
    out = dc.send_daily_callout()
    assert out == {"sent": False, "error": "send_failed",
                   "to": "op@example.com"}
    assert released == [1]


# ── per-day claim path (live regressions 2026-07-19) ─────────────────
# Two prod bites in one morning: db_utils.PGCursorWrapper is NOT a
# context manager, AND its execute() SILENTLY DROPS DDL — the claim
# table never got created and the dedupe fail-opened (3 emails). The
# claim path now uses a direct psycopg2 write connection (_write_conn);
# these tests pin the SQL it issues and the duplicate-day skip.

class _PlainCursor:
    def __init__(self, log):
        self._log = log
        self.rowcount = 1

    def execute(self, sql, params=None):
        self._log.append(" ".join(str(sql).split())[:60])


class _PlainConn:
    def __init__(self, log):
        self._log = log

    def cursor(self, *a, **k):
        return _PlainCursor(self._log)

    def close(self):
        pass


def test_claim_today_creates_table_and_inserts(monkeypatch):
    log = []
    monkeypatch.setattr(dc, "_write_conn", lambda: _PlainConn(log))
    claimed, err = dc._claim_today()
    assert claimed is True and err is None
    assert any(s.startswith("CREATE TABLE") for s in log)
    assert any(s.startswith("INSERT INTO brain_daily_callout_log") for s in log)
    dc._stamp_sent("op@example.com", "subj")
    dc._release_claim()
    assert any(s.startswith("UPDATE brain_daily_callout_log") for s in log)
    assert any(s.startswith("DELETE FROM brain_daily_callout_log") for s in log)


def test_claim_today_duplicate_day_skips(monkeypatch):
    class _DupCursor(_PlainCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "INSERT" in str(sql):
                self.rowcount = 0  # ON CONFLICT DO NOTHING hit

    class _DupConn(_PlainConn):
        def cursor(self, *a, **k):
            return _DupCursor(self._log)

    monkeypatch.setattr(dc, "_write_conn", lambda: _DupConn([]))
    claimed, err = dc._claim_today()
    assert claimed is False
    # force=1 overrides the dedupe but still records
    claimed_forced, _ = dc._claim_today(force=True)
    assert claimed_forced is True


def test_claim_today_no_db_fails_open(monkeypatch):
    """A lost dedupe must beat a lost callout."""
    monkeypatch.setattr(dc, "_write_conn", lambda: None)
    claimed, err = dc._claim_today()
    assert claimed is True and err == "no_database_url"


# ── rendering: every line names its actuator ─────────────────────────

def test_render_text_names_actuators():
    text = dc.render_text(_STUB_DIGEST)
    # public-surface line: page + both dates + the actuator sentence
    assert "dchub.cloud/press" in text
    assert "2026-06-22" in text and "26d behind" in text
    assert "rebuild/deploy the public page" in text
    # stalled lane line names its actuator
    assert "TWITTER_PUBLISHER_ENABLED" in text
    # chronic line: issue + age + destination
    assert "brand_surface_dormant:bs_translator" in text
    # flow + human-gated
    assert "opened 56" in text and "resolved 390" in text
    assert "#1612" in text and "[white-glove] listing copy drift" in text
    # kill switch documented in the footer
    assert "BRAIN_DAILY_CALLOUT_ENABLED=0" in text


def test_render_text_healthy_day_is_calm():
    d = dict(_STUB_DIGEST)
    d["stale_pages"] = []
    d["lanes"] = {"stalled": [], "unknown": [], "lanes_checked": 14,
                  "error": None}
    d["human"] = {"needs_human_merge": [], "drift_issue": None, "error": None}
    text = dc.render_text(d)
    assert "SILENT PIPELINES (0)" in text
    assert "none — 14 lanes" in text
    assert "queue empty" in text


# ── radar detector wraps the same report ─────────────────────────────

def test_radar_detector_fires_on_stale_page(monkeypatch):
    radar = pytest.importorskip("routes.brain_consistency_radar")
    monkeypatch.setattr(dc, "press_surface_report", lambda: {
        "db_newest": datetime.date(2026, 7, 18),
        "pages": [{"url": "https://dchub.cloud/press",
                   "newest_visible": "2026-06-22", "lag_days": 26,
                   "stale": True, "error": None},
                  {"url": "https://dchub.cloud/dc-hub-media/",
                   "newest_visible": "2026-07-17", "lag_days": 1,
                   "stale": False, "error": None}],
        "error": None})
    findings = radar.check_press_public_surface_stale()
    assert len(findings) == 1
    f = findings[0]
    assert f["issue"] == "press_public_surface_stale"
    assert f["url"] == "https://dchub.cloud/press"
    assert f["count"] == 26
    assert "publish-to-edge" in f["detail"]


def test_radar_detector_quiet_when_fresh(monkeypatch):
    radar = pytest.importorskip("routes.brain_consistency_radar")
    monkeypatch.setattr(dc, "press_surface_report", lambda: {
        "db_newest": datetime.date(2026, 7, 18),
        "pages": [{"url": "https://dchub.cloud/press",
                   "newest_visible": "2026-07-18", "lag_days": 0,
                   "stale": False, "error": None}],
        "error": None})
    assert radar.check_press_public_surface_stale() == []
