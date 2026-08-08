"""Guards for pk-/k- key-bound conversion ATTRIBUTION (#1660, 2026-07-18).

The main Stripe webhook's key-bound branches provision the purchasing key but
used to record mcp_conversions with caller_id=NULL and no attribution_signal_id
— so /unlock/k conversions landed 'unattributed' and agent conv_rate read 0%.
Locks the contract of mcp_signal_canonical.attribute_keybound_conversion:
never raises out of the live payment webhook, resolves the key from the ref
hash, flips the key's signals converted (by the r33-identity 'key=<prefix24>'
user_agent tag), and stamps the conversion row with the driving signal.

r-keybound-platform (2026-07-18, #1660 residual): also locks the per-key
call-history PLATFORM resolution (resolve_key_platform over mcp_call_log) —
when no signal matches, the conversion still gets caller_id (email → the
key's latest MCP session) and mcp_conversions.platform (the key's dominant
non-internal platform tag), each stamp COALESCE-only and independently
fail-soft so attribution can never fail the payment webhook.
"""
import os
import sys
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

msc = pytest.importorskip("mcp_signal_canonical")


# ── Fake DB plumbing ─────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, db):
        self.db = db
        self._rows = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.db.executed.append((" ".join(sql.split()), params))
        self._rows = self.db.dispatch(sql, params)
        self.rowcount = 1 if "update" in sql.lower() else len(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeDB:
    """Answers the helper's queries; records every statement it runs."""

    def __init__(self, key_row=None, trial_row=None, signal_row=None,
                 api_keys_email=None, call_log_key_row=None,
                 call_log_platform_row=None, call_log_session_row=None):
        self.executed = []
        self.key_row = key_row              # mcp_dev_keys (api_key, email)
        self.trial_row = trial_row          # auto_trial_keys (api_key, email)
        self.signal_row = signal_row        # best signal (id, caller_id)
        self.api_keys_email = api_keys_email
        self.call_log_key_row = call_log_key_row          # (api_key,)
        self.call_log_platform_row = call_log_platform_row  # (platform, n, last_at)
        self.call_log_session_row = call_log_session_row    # (session_id,)

    def dispatch(self, sql, params):
        s = " ".join(sql.split()).lower()
        if "from mcp_dev_keys" in s:
            return [self.key_row] if self.key_row else []
        if "from auto_trial_keys" in s:
            return [self.trial_row] if self.trial_row else []
        if "from api_keys" in s:
            return [(self.api_keys_email,)] if self.api_keys_email else []
        if "select id, caller_id from mcp_upgrade_signals" in s:
            return [self.signal_row] if self.signal_row else []
        if "distinct api_key from mcp_call_log" in s:
            return [self.call_log_key_row] if self.call_log_key_row else []
        if "from mcp_call_log l" in s and "group by" in s:
            return [self.call_log_platform_row] if self.call_log_platform_row else []
        if "select session_id from mcp_call_log" in s:
            return [self.call_log_session_row] if self.call_log_session_row else []
        return []

    # connection surface
    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def _arm(monkeypatch, db):
    monkeypatch.setattr(msc, "DSN", "postgres://fake")
    monkeypatch.setattr(msc, "psycopg2", object())  # just needs non-None

    @contextmanager
    def _fake_conn():
        yield db

    monkeypatch.setattr(msc, "_conn", _fake_conn)
    return db


# ── Fail-soft: the helper runs inside the LIVE payment webhook ──────────

def test_no_db_returns_dict_never_raises(monkeypatch):
    monkeypatch.setattr(msc, "DSN", "")
    out = msc.attribute_keybound_conversion(key_hash="a" * 64)
    assert out["ok"] is False and out["reason"] == "no_db"


def test_bad_hash_rejected(monkeypatch):
    monkeypatch.setattr(msc, "DSN", "postgres://fake")
    monkeypatch.setattr(msc, "psycopg2", object())
    for bad in (None, "", "zz", "a" * 63, "g" * 64, "pk-riding-junk"):
        out = msc.attribute_keybound_conversion(key_hash=bad)
        assert out["ok"] is False and out["reason"] == "bad_hash"


def test_db_error_swallowed(monkeypatch):
    monkeypatch.setattr(msc, "DSN", "postgres://fake")
    monkeypatch.setattr(msc, "psycopg2", object())

    @contextmanager
    def _boom():
        raise RuntimeError("conn down")
        yield  # pragma: no cover

    monkeypatch.setattr(msc, "_conn", _boom)
    out = msc.attribute_keybound_conversion(key_hash="a" * 64)
    assert out["ok"] is False and "error" in out


def test_mark_signals_converted_accepts_api_key_no_db(monkeypatch):
    monkeypatch.setattr(msc, "DSN", "")
    out = msc.mark_signals_converted(api_key="dch_live_x" + "0" * 20)
    assert out == {"total": 0, "reason": "no_db"}


# ── The attribution contract (fake DB) ──────────────────────────────────

KEY = "dch_live_0123456789abcdef01234567"  # secretscan:allow — synthetic fixture


def test_full_keybound_attribution_flow(monkeypatch):
    db = _arm(monkeypatch, _FakeDB(key_row=(KEY, None),
                                   signal_row=(42, "anon:deadbeef")))
    out = msc.attribute_keybound_conversion(
        key_hash="ab" * 16, buyer_email="Buyer@Example.com",
        stripe_ref="cs_test_1", stripe_customer_id="cus_1")

    assert out["ok"] is True
    assert out["key_resolved"] is True
    assert out["signal_id"] == 42
    assert out["stamped"] == 1

    sqls = [s for s, _ in db.executed]
    # key resolved from the ref hash (prefix LIKE covers pk-'s sha256[:32])
    assert any("from mcp_dev_keys" in s.lower() for s in sqls)
    # signals flipped by the r33-identity key tag — POSITION, not LIKE
    # (key prefixes contain '_', a LIKE single-char wildcard)
    flip = [(s, p) for s, p in db.executed
            if "update mcp_upgrade_signals" in s.lower() and "position(" in s.lower()]
    assert flip, "must flip signals matched by the key=<prefix24> user_agent tag"
    assert any(p and ("key=" + KEY[:24]) in p for _, p in flip)
    # buyer email backfilled onto the key's signals (event-order robustness)
    assert any(p and "buyer@example.com" in p for _, p in flip)
    # conversion row stamped with the driving signal
    stamp = [(s, p) for s, p in db.executed
             if "update mcp_conversions" in s.lower()]
    assert len(stamp) == 1
    s, p = stamp[0]
    assert "attribution_signal_id" in s and "coalesce" in s.lower()
    assert p == (42, "anon:deadbeef", "buyer@example.com", "cs_test_1")


def test_email_only_path_when_key_not_resolvable(monkeypatch):
    """dchub_ web keys are hash-only in api_keys → attribute via email."""
    db = _arm(monkeypatch, _FakeDB(api_keys_email="dev@corp.com",
                                   signal_row=(7, "dev@corp.com")))
    out = msc.attribute_keybound_conversion(
        key_hash="cd" * 32, stripe_ref="sub_test_9")
    assert out["ok"] is True
    assert out["key_resolved"] is True
    assert out["signal_id"] == 7 and out["stamped"] == 1
    stamp = [p for s, p in db.executed if "update mcp_conversions" in s.lower()]
    assert stamp == [(7, "dev@corp.com", "dev@corp.com", "sub_test_9")]


def test_no_matcher_is_clean_noop(monkeypatch):
    """Unknown hash + no buyer email → no signal query, no stamp, still ok."""
    db = _arm(monkeypatch, _FakeDB())
    out = msc.attribute_keybound_conversion(key_hash="ef" * 16)
    assert out["ok"] is True and out.get("reason") == "no_signal_matcher"
    assert out["stamped"] == 0 and out["signal_id"] is None
    assert not any("update mcp_conversions" in s.lower() for s, _ in db.executed)


def test_no_signal_found_does_not_stamp(monkeypatch):
    db = _arm(monkeypatch, _FakeDB(key_row=(KEY, None)))
    out = msc.attribute_keybound_conversion(
        key_hash="ab" * 16, stripe_ref="cs_test_2")
    assert out["ok"] is True
    assert out["signal_id"] is None and out["stamped"] == 0
    assert not any("update mcp_conversions" in s.lower() for s, _ in db.executed)


def test_stamp_only_coalesces_never_overwrites(monkeypatch):
    """An already-attributed row must keep its original attribution."""
    db = _arm(monkeypatch, _FakeDB(key_row=(KEY, None), signal_row=(9, "x")))
    msc.attribute_keybound_conversion(key_hash="ab" * 16, stripe_ref="cs_3")
    s = next(s for s, _ in db.executed if "update mcp_conversions" in s.lower())
    low = s.lower()
    assert "attribution_signal_id = coalesce(attribution_signal_id" in low
    assert "caller_id = coalesce(caller_id" in low
    assert "user_email = coalesce(user_email" in low


# ── #1660 residual: platform from per-key call history ──────────────────

def test_platform_from_call_history_stamps_without_signal(monkeypatch):
    """No signal at all → caller_id falls back to the key's latest MCP session
    and the dominant call-history platform is stamped (both COALESCE-only)."""
    db = _arm(monkeypatch, _FakeDB(
        key_row=(KEY, None),
        call_log_platform_row=("claude", 57, None),
        call_log_session_row=("sess-abc123",)))
    out = msc.attribute_keybound_conversion(key_hash="ab" * 16,
                                            stripe_ref="cs_plat_1")
    assert out["ok"] is True
    assert out["signal_id"] is None
    assert out["platform"] == "claude"
    assert out["stamped"] == 1 and out["platform_stamped"] == 1
    stamps = [(s, p) for s, p in db.executed
              if "update mcp_conversions" in s.lower()]
    assert len(stamps) == 2
    (s1, p1), (s2, p2) = stamps
    assert "caller_id" in s1
    assert p1 == (None, "sess-abc123", None, "cs_plat_1")
    assert "set platform = coalesce(nullif(platform, '')" in s2.lower()
    assert p2 == ("claude", "cs_plat_1")


def test_raw_key_recovered_from_call_log_when_hash_tables_miss(monkeypatch):
    """dchub_ web keys are hash-only in api_keys — but mcp_call_log stores the
    raw key, so the platform still resolves from the ref hash alone."""
    db = _arm(monkeypatch, _FakeDB(
        call_log_key_row=("dchub_live_0deadbeef",),
        call_log_platform_row=("mcp", 9, None),
        call_log_session_row=("sess-x",)))
    out = msc.attribute_keybound_conversion(key_hash="cd" * 32,
                                            stripe_ref="sub_plat_2")
    assert out["ok"] is True and out["key_resolved"] is True
    assert out["platform"] == "mcp"
    assert out["stamped"] == 1 and out["platform_stamped"] == 1
    stamp = [p for s, p in db.executed
             if "update mcp_conversions" in s.lower() and "caller_id" in s]
    assert stamp == [(None, "sess-x", None, "sub_plat_2")]


def test_call_history_failure_never_breaks_signal_attribution(monkeypatch):
    """mcp_call_log queries blowing up must not cost the signal stamp — the
    webhook-never-fails guarantee extends to the new resolution path."""
    class _Boom(_FakeDB):
        def dispatch(self, sql, params):
            if "mcp_call_log" in sql.lower():
                raise RuntimeError("call log unavailable")
            return super().dispatch(sql, params)

    _arm(monkeypatch, _Boom(key_row=(KEY, None), signal_row=(42, "anon:x")))
    out = msc.attribute_keybound_conversion(
        key_hash="ab" * 16, buyer_email="b@x.com", stripe_ref="cs_b1")
    assert out["ok"] is True
    assert out["signal_id"] == 42 and out["stamped"] == 1
    assert out["platform"] is None and out["platform_stamped"] == 0


def test_platform_stamp_failure_never_breaks_caller_stamp(monkeypatch):
    """Missing mcp_conversions.platform (schema migration pending on a fresh
    env) must not cost the caller/signal stamp — the platform arm is its own
    guarded statement."""
    class _NoCol(_FakeDB):
        def dispatch(self, sql, params):
            if "set platform" in " ".join(sql.split()).lower():
                raise RuntimeError('column "platform" does not exist')
            return super().dispatch(sql, params)

    _arm(monkeypatch, _NoCol(
        key_row=(KEY, None), signal_row=(9, "anon:y"),
        call_log_platform_row=("claude", 3, None)))
    out = msc.attribute_keybound_conversion(key_hash="ab" * 16,
                                            stripe_ref="cs_c1")
    assert out["ok"] is True
    assert out["stamped"] == 1
    assert out["platform"] == "claude" and out["platform_stamped"] == 0


def test_resolve_key_platform_no_db(monkeypatch):
    monkeypatch.setattr(msc, "DSN", "")
    out = msc.resolve_key_platform(key_hash="a" * 64)
    assert out == {"raw_key": None, "platform": None, "platform_calls": 0,
                   "last_session": None}


def test_resolve_key_platform_excludes_internal_tags(monkeypatch):
    """The dominant-platform SQL must carry the canonical internal-tag
    exclusion (the regex twin — bound-params-safe) and drop empty tags, so
    QA/probe traffic can never become a BD 'platform'."""
    db = _arm(monkeypatch, _FakeDB(call_log_platform_row=("claude", 5, None)))
    out = msc.resolve_key_platform(raw_key=KEY)
    assert out["platform"] == "claude" and out["platform_calls"] == 5
    q = next(s for s, _ in db.executed
             if "group by" in s.lower() and "mcp_call_log" in s.lower())
    assert "COALESCE(TRIM(l.platform), '') <> ''" in q
    assert "!~" in q  # regex predicate, never LIKE (the literal-% trap)
    assert "%" not in q.replace("%s", "")


# ── Webhook wiring: both branches must call the helper ──────────────────

def _main_src():
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
        return f.read()


def test_pk_pack_branch_calls_attribution():
    src = _main_src()
    i = src.find("stripe_webhook_pack5_keybound")
    assert i != -1, "pk- pack conversion insert missing"
    assert "attribute_keybound_conversion" in src[i:i + 4000], (
        "pk- pack branch no longer stamps key-bound attribution (#1660)")


def test_k_sub_branch_calls_attribution():
    src = _main_src()
    j = src.find("k- sub tier bind failed")
    assert j != -1, "k- sub tier branch missing"
    assert "attribute_keybound_conversion" in src[max(0, j - 4000):j], (
        "k- sub branch no longer stamps key-bound attribution (#1660)")


def _fme_src():
    with open(os.path.join(ROOT, "flask_mcp_endpoints.py"),
              encoding="utf-8") as f:
        return f.read()


def test_webhook_mcp_sibling_stamps_platform():
    """flask_mcp_endpoints.stripe_webhook_mcp (the disjoint sub-ledger writer)
    must stamp the call-history platform POST-credit, in its own try, so a
    resolution error can never fail the payment webhook (#1660 residual)."""
    src = _fme_src()
    i = src.find("def stripe_webhook_mcp")
    assert i != -1
    j = src.find("def dev_signup_form")
    body = src[i:j] if j != -1 else src[i:i + 40000]
    k = body.find("resolve_key_platform")
    assert k != -1, "sibling webhook no longer stamps key platform (#1660)"
    assert "stamp_conversion_platform" in body
    # post-credit: after the conversion INSERT and the signal write-back
    assert k > body.find("INSERT INTO mcp_conversions")
    assert k > body.find("mark_signals_converted")


def test_conversions_by_platform_reads_platform_column():
    """The BD read layer must consult mcp_conversions.platform between the
    signal join and the web/organic channel fallback (#1660 residual)."""
    src = _fme_src()
    i = src.find("no signal link: use the conversion's own channel")
    assert i != -1
    assert "NULLIF(LOWER(TRIM(c.platform)), '')" in src[max(0, i - 2000):i], (
        "conversions_by_platform_30d no longer falls back to the key-bound "
        "platform stamp (#1660)")
