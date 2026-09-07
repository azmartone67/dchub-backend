"""Two heartbeat vitals blocks that failed as SILENCE, not as errors.

THE DEFECTS (both measured live against production 2026-09-07, on the
`brain_heartbeat` payload and the prod database):

1. media_quality.published_7d was structurally unmeasurable.
   `social_media_posts.published_at` is a TEXT column holding mixed
   'YYYY-MM-DD HH:MI+00' / 'YYYY-MM-DDTHH:MIZ' formats. The block compared it
   straight to a timestamptz:

       AND published_at >= (NOW() - INTERVAL '7 days')

   Run verbatim against prod that is not a slow query, it is an error:

       ERROR:  operator does not exist: text >= timestamp with time zone

   It was wrapped in a bare `except Exception: _pub = 0`, so the counter had
   NEVER once succeeded in the life of the block. _pub was permanently 0, so
   reject_rate was permanently _blocked/(_blocked+0) == 1.0, and the heartbeat
   carried a standing alert:

       "high media reject rate 100% (top: thin_or_offbrand)"

   while LinkedIn was in fact publishing. With the cast added, the identical
   query returned 6 for the same window, and media_review_log showed 14
   `published_review` rows over 7 days. The true rate was ~95%, not 100%.
   routes/media_master_shell.py::tier1_measure already casts this exact column
   and carries a comment warning about it — the correct form existed 40 lines
   into another file.

   ★ WHY None AND NOT 0. The fix is not only the cast. A COUNT that could not
   RUN is not a count of zero. Reporting 0 let a broken query impersonate a
   dead feed, which is what pinned the alert at "100%" and made the failure
   invisible for as long as it existed. So: unmeasurable -> published_7d None,
   reject_rate None, status "unknown" — never a number that reads as a verdict.

2. layer5 probed a table that does not exist, and said nothing.
   It gated on `to_regclass('public.brain_proposed_code')`. The real table is
   `brain_proposed_code_fixes` and its timestamp column is `proposed_at`, not
   `created_at`. to_regclass returned NULL, so the `if` never fired — and
   because the miss was an untaken branch rather than a raise, the block
   emitted NO "layer5" key and NO error. The live heartbeat payload simply had
   no layer5 in it, and nothing anywhere said why. The brain's vitals were
   blind to the brain's own code-proposal arm.

★ The stub cursor below encodes the REAL Postgres rule (text >= timestamptz
  raises) rather than assuming the query text. That is what makes these tests
  fail against the pre-fix source instead of merely describing it.

★ Stdlib only — CI installs pytest/requests/flask/pyyaml/psycopg2/psycopg/
  Unidecode/Pillow/feedparser and nothing else (.github/workflows/pre-merge.yml).
"""
import datetime as _dt
import re

import pytest

from routes import brain_autopilot as ba


# ── a cursor that behaves like Postgres, not like a fixture ──────────────────

class _PGLikeCursor:
    """Answers the heartbeat's queries; raises where real Postgres raises."""

    #: text >= timestamptz has no operator in Postgres. A cast is required.
    _UNCAST_TEXT_COMPARE = re.compile(
        r"published_at\s*>=", re.I)
    _HAS_CAST = re.compile(r"published_at\s*::\s*timestamptz", re.I)

    def __init__(self, *, linkedin_published=6, table_exists=True,
                 proposals_24h=7):
        self.linkedin_published = linkedin_published
        self.table_exists = table_exists
        self.proposals_24h = proposals_24h
        self._result = None

    def execute(self, sql, *a, **k):
        s = " ".join(str(sql).split())

        if "social_media_posts" in s:
            if self._UNCAST_TEXT_COMPARE.search(s) and not self._HAS_CAST.search(s):
                raise RuntimeError(
                    "operator does not exist: text >= timestamp with time zone")
            self._result = (self.linkedin_published,)
            return

        if "media_review_log" in s:
            # Three blocked rows, all low-quality -> thin_or_offbrand.
            self._result = [("low quality score 41 < 55",)] * 3
            self._rows = self._result
            return

        if "to_regclass" in s:
            name = "brain_proposed_code_fixes" if self.table_exists else None
            self._result = (name,)
            return

        if "brain_proposed_code_fixes" in s:
            if "created_at" in s:
                raise RuntimeError(
                    'column "created_at" does not exist')
            self._result = (self.proposals_24h,
                            _dt.datetime(2026, 9, 6, 12, 0, 0))
            return

        self._result = (0,)

    def fetchone(self):
        r = self._result
        return r if isinstance(r, tuple) else (r[0] if r else None)

    def fetchall(self):
        return self._result if isinstance(self._result, list) else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.autocommit = True

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def _media_quality(monkeypatch, cursor):
    """Run only the media_quality/layer5 vitals against a stub connection."""
    monkeypatch.setattr(ba, "_conn", lambda: _Conn(cursor))
    out = ba._compute_heartbeat_sync()
    return out


# ── 1 · the publish count must actually be measurable ────────────────────────

def test_publish_count_is_measured_not_swallowed(monkeypatch):
    """The cast query runs, so published_7d is the real number."""
    out = _media_quality(monkeypatch, _PGLikeCursor(linkedin_published=6))
    mq = out.get("media_quality") or {}
    assert mq.get("published_7d") == 6, mq
    # 3 blocked, 6 published -> 0.333, nowhere near the phantom 1.0.
    assert mq.get("reject_rate") == pytest.approx(0.333, abs=0.001), mq
    assert mq.get("status") == "ok", mq


def test_reject_rate_is_never_100_percent_from_a_broken_query(monkeypatch):
    """★ THE REGRESSION. An unmeasurable publish count must not read as 100%."""
    out = _media_quality(monkeypatch, _PGLikeCursor(linkedin_published=0))
    mq = out.get("media_quality") or {}
    # A REAL zero is allowed to produce a real 1.0 — that is a true verdict.
    assert mq.get("published_7d") == 0, mq
    assert mq.get("reject_rate") == 1.0, mq


def test_unmeasurable_publish_count_reports_unknown_not_zero(monkeypatch):
    """If the query DOES fail, say so — do not report 0 published / 100%."""
    class _Broken(_PGLikeCursor):
        def execute(self, sql, *a, **k):
            if "social_media_posts" in str(sql):
                raise RuntimeError(
                    "operator does not exist: text >= timestamp with time zone")
            return super().execute(sql, *a, **k)

    out = _media_quality(monkeypatch, _Broken())
    mq = out.get("media_quality") or {}
    assert mq.get("published_7d") is None, mq
    assert mq.get("reject_rate") is None, mq
    assert mq.get("status") == "unknown", mq
    assert "unmeasurable" in (mq.get("alert") or ""), mq
    assert "operator does not exist" in (mq.get("published_7d_error") or ""), mq


# ── 2 · layer5 must never be silently absent ─────────────────────────────────

def test_layer5_reads_the_table_that_exists(monkeypatch):
    """★ THE REGRESSION. Probing brain_proposed_code (no such table) emitted
    no key at all; the real table is brain_proposed_code_fixes/proposed_at."""
    out = _media_quality(monkeypatch, _PGLikeCursor(proposals_24h=7))
    l5 = out.get("layer5")
    assert l5 is not None, "layer5 block emitted NOTHING — the original defect"
    assert l5.get("proposals_24h") == 7, l5
    assert l5.get("last_proposal", "").startswith("2026-09-06"), l5


def test_layer5_missing_table_is_loud_not_silent(monkeypatch):
    """A probe that finds no table must report the miss, not vanish."""
    out = _media_quality(monkeypatch, _PGLikeCursor(table_exists=False))
    l5 = out.get("layer5")
    assert l5 is not None, "a missing table must still emit layer5"
    assert "not found" in (l5.get("error") or ""), l5
