"""LinkedIn token durability — 2026-07-23.

Locks the two decision points that make the LinkedIn posting token DURABLE so
the media feed can't silently go dark when the ~60-day access token lapses:

  1. PRESERVE-REFRESH-TOKEN (routes.linkedin_token_reset._resolve_refresh_write)
     — reset-from-env must never clobber a real refresh_token with '' / NULL.
     A real refresh_token is minted ONLY by the OAuth callback; the reset path
     historically wrote '' and silently disarmed auto-refresh forever.

  2. REFRESH DECISION GATE (routes.linkedin_token_reset.refresh_decision)
     — the proactive cron refreshes when <10d to expiry AND a usable
     refresh_token + client creds exist; alerts LOUDLY when <7d with NO usable
     refresh_token (the cron cannot save it); skips otherwise.

DB-free / network-free: both are pure functions. Never imports main
(pre-merge CI has no DB / JWT_SECRET).
"""
import os
import sys
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

ltr = pytest.importorskip("routes.linkedin_token_reset")  # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)


def _days(n):
    """expires_at n days from NOW."""
    return NOW + datetime.timedelta(days=n)


# ── 1) preserve-refresh-token ────────────────────────────────────────────────
class TestResolveRefreshWrite:
    def test_preserve_existing_when_none_supplied(self):
        # The core bug: a reset with no supplied token must KEEP the real one.
        assert ltr._resolve_refresh_write("real-rt-abc", None) == "real-rt-abc"

    def test_preserve_existing_when_empty_string_supplied(self):
        # reset-from-env used to write '' here — must NOT clobber the real token.
        assert ltr._resolve_refresh_write("real-rt-abc", "") == "real-rt-abc"

    def test_preserve_existing_when_whitespace_supplied(self):
        assert ltr._resolve_refresh_write("real-rt-abc", "   ") == "real-rt-abc"

    def test_supplied_new_token_wins(self):
        assert ltr._resolve_refresh_write("old-rt", "new-rt") == "new-rt"

    def test_supplied_token_seeds_when_none_existing(self):
        assert ltr._resolve_refresh_write(None, "new-rt") == "new-rt"
        assert ltr._resolve_refresh_write("", "new-rt") == "new-rt"

    def test_supplied_is_stripped(self):
        assert ltr._resolve_refresh_write(None, "  new-rt  ") == "new-rt"

    def test_never_stores_empty_string(self):
        # Neither existing nor supplied -> NULL (None), NEVER '' — an empty
        # string reads as "present" downstream and poisons the usable() check.
        assert ltr._resolve_refresh_write(None, None) is None
        assert ltr._resolve_refresh_write("", "") is None

    def test_usable_refresh_treats_empty_and_none_as_absent(self):
        assert ltr._usable_refresh(None) is False
        assert ltr._usable_refresh("") is False
        assert ltr._usable_refresh("   ") is False
        assert ltr._usable_refresh("real-rt") is True


# ── 2) proactive refresh decision gate ───────────────────────────────────────
class TestRefreshDecision:
    def test_refresh_when_within_10d_and_token_present(self):
        action, _ = ltr.refresh_decision(NOW, _days(8), has_refresh=True, has_creds=True)
        assert action == "refresh"

    def test_refresh_at_expiry_boundary(self):
        action, _ = ltr.refresh_decision(NOW, _days(10), has_refresh=True, has_creds=True)
        assert action == "refresh"

    def test_refresh_when_already_expired_but_token_present(self):
        # Worth one attempt — the refresh_token may still be valid.
        action, _ = ltr.refresh_decision(NOW, _days(-1), has_refresh=True, has_creds=True)
        assert action == "refresh"

    def test_skip_when_far_from_expiry(self):
        action, _ = ltr.refresh_decision(NOW, _days(30), has_refresh=True, has_creds=True)
        assert action == "skip"

    def test_skip_when_no_creds_even_if_token_present(self):
        # Cannot call LinkedIn without client_id/secret; and a token IS present
        # so this is not the loud-alert case either.
        action, _ = ltr.refresh_decision(NOW, _days(5), has_refresh=True, has_creds=False)
        assert action == "skip"

    def test_alert_when_within_7d_and_no_usable_refresh_token(self):
        action, _ = ltr.refresh_decision(NOW, _days(5), has_refresh=False, has_creds=True)
        assert action == "alert"

    def test_alert_when_expired_and_no_refresh_token(self):
        action, _ = ltr.refresh_decision(NOW, _days(-2), has_refresh=False, has_creds=True)
        assert action == "alert"

    def test_no_refresh_between_7_and_10_days_is_skip_not_alert(self):
        # Not yet inside the loud-alert window — don't cry wolf at 8 days.
        action, _ = ltr.refresh_decision(NOW, _days(8), has_refresh=False, has_creds=True)
        assert action == "skip"

    def test_skip_when_no_expiry_recorded(self):
        action, reason = ltr.refresh_decision(NOW, None, has_refresh=True, has_creds=True)
        assert action == "skip"
        assert reason == "no_expiry_recorded"
