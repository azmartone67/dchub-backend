"""The presented-key re-mint probe in routes/auto_trial.py.

WHAT IT GUARDS
--------------
mint_trial_for_request had two re-mint probes and both keyed on
`request_ip_hash` (the legacy one also on `request_ua`). The 2026-06-19 comment
on that probe already conceded the gap — "Web hosts on ROTATING egress IPs still
won't match" — and that gap turned out to BE the funnel:

    5,205 mint rows  ->  289 distinct keys  ->  14 distinct user agents
    one UA, the literal string "node": 263 keys, 4,412 rows, 0 email binds ever

A generic UA on rotating egress presents a fresh (ip_hash, ua) every call, so
both probes miss, a new key is minted, the unbound counter resets, and the bind
gate is never reachable. The agent is not anonymous though — it is holding the
key we just gave it. The probe reads that key.

The function is pulled out of the source with `ast` and executed against stubs,
per the repo rule that no test imports main.py in-process. That means these are
BEHAVIOURAL tests of the shipped code, not greps over it — if the probe stops
firing, or fires on the wrong input, a named case goes red.
"""

import ast
import hashlib
import pathlib
import sys
import types

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "routes" / "auto_trial.py"
TEXT = SRC.read_text()
TREE = ast.parse(TEXT)


def _extract(name):
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in auto_trial.py")


# ── stub scaffolding ─────────────────────────────────────────────────

class _Cur:
    """A cursor that answers the probe's SELECT and records everything."""

    def __init__(self, key_row=None, ip_row=None):
        self.key_row = key_row          # answer for the presented-key probe
        self.ip_row = ip_row            # answer for the ip_hash probes
        self.queries = []
        self._last = None
        self.updates = []

    def execute(self, sql, params=None):
        self.queries.append((" ".join(sql.split()), params))
        low = sql.lower()
        if low.strip().startswith("update"):
            self.updates.append(params)
            self._last = None
            return
        if "where api_key = %s and expires_at > now()" in " ".join(low.split()):
            self._last = self.key_row
        elif "from auto_trial_keys" in low:
            self._last = self.ip_row
        else:
            self._last = None

    def fetchone(self):
        return self._last

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def close(self):
        pass


class _Req:
    def __init__(self, headers=None, ip="1.2.3.4"):
        self.headers = headers or {}
        self.remote_addr = ip


def _run(presented=None, key_row=None, ip_row=None, env=None,
         operator_email="", ua="node"):
    """Execute the real mint_trial_for_request against stubs.

    Returns (result_dict, cursor) so a test can assert on both the answer and
    on whether a mint path was even reached."""
    import datetime as _dt

    cur = _Cur(key_row=key_row, ip_row=ip_row)
    conn = _Conn(cur)

    headers = {"User-Agent": ua}
    if presented:
        headers["X-API-Key"] = presented
    req = _Req(headers)

    fake_os = types.SimpleNamespace(environ=dict(env or {}))

    minted = {"n": 0}

    def _mint_marker(*a, **k):
        minted["n"] += 1
        raise _StopMint()

    class _StopMint(Exception):
        """Raised by the stubbed schema step so a test can tell that execution
        fell THROUGH the probe into the real mint path, without running it."""

    ns = {
        "os": fake_os,
        "hashlib": hashlib,
        "request": req,
        "_conn": lambda: conn,
        "_ensure_schema": lambda c: None,
        "note_swallowed_write": lambda *a, **k: None,
        "TRIAL_FREE_CALLS_UNBOUND": 5,
        "TRIAL_DAILY_CALLS": 50,
        "TRIAL_DAILY_UNBOUND": 10,
        "TRIAL_DAYS": 7,
        "datetime": _dt,
    }
    mod = ast.Module(body=[_extract("mint_trial_for_request")], type_ignores=[])
    exec(compile(mod, str(SRC), "exec"), ns)          # noqa: S102 — the point
    try:
        out = ns["mint_trial_for_request"](req=req, tool_name="t",
                                           client_name="c",
                                           operator_email=operator_email)
    except Exception as e:  # the stub DB runs out of answers past the probe
        out = {"_fell_through": type(e).__name__}
    return out, cur


def _live_key_row(api_key="dch_trial_ABC", calls=99, bound=False):
    import datetime as _dt
    return (api_key,
            _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=5),
            calls, bound)


# ── the probe fires on a presented key ───────────────────────────────

def test_presented_key_is_returned_instead_of_minting_a_new_one():
    """The whole fix: an agent holding dch_trial_ABC gets dch_trial_ABC back."""
    out, cur = _run(presented="dch_trial_ABC", key_row=_live_key_row())
    assert out.get("api_key") == "dch_trial_ABC"
    assert out.get("reused") is True
    assert out.get("reuse_basis") == "presented_key"


def test_gated_unbound_key_demands_a_bind():
    """Past the unbound allowance, the answer must carry the bind gate — this is
    the step that was unreachable for the rotating-IP cohort."""
    out, _ = _run(presented="dch_trial_ABC",
                  key_row=_live_key_row(calls=99, bound=False))
    assert out.get("bind_required") is True
    assert out.get("gate") == "bind_email_required"
    assert "auto-trial/bind" in out.get("bind_endpoint", "")
    # and it must tell the agent to ASK rather than invent an address
    assert "never" in out.get("operator_action", "").lower()


def test_under_the_allowance_the_key_still_works_without_binding():
    """This closes a re-mint, it must not close access. An agent mid-task with
    calls left keeps going."""
    out, _ = _run(presented="dch_trial_ABC",
                  key_row=_live_key_row(calls=1, bound=False))
    assert out.get("api_key") == "dch_trial_ABC"
    assert out.get("bind_required") is None
    assert out.get("daily_calls") == 10


def test_supplying_an_email_binds_on_the_spot_and_lifts_the_gate():
    out, cur = _run(presented="dch_trial_ABC",
                    key_row=_live_key_row(calls=99, bound=False),
                    operator_email="Ops@Example.COM ")
    assert cur.updates, "an operator email must be written through"
    assert cur.updates[0][0] == "ops@example.com", "email must be normalised"
    assert out.get("bind_required") is None
    assert out.get("daily_calls") == 50


def test_already_bound_key_reports_the_full_allowance():
    out, _ = _run(presented="dch_trial_ABC",
                  key_row=_live_key_row(calls=999, bound=True))
    assert out.get("daily_calls") == 50
    assert out.get("bind_required") is None


# ── the probe does NOT fire where it must not ────────────────────────

def test_no_presented_key_falls_through_to_the_existing_behaviour():
    """A genuinely new agent presents nothing and must be untouched by this."""
    out, cur = _run(presented=None, key_row=_live_key_row())
    assert out.get("reuse_basis") != "presented_key"
    joined = " ".join(q for q, _ in cur.queries)
    assert "expires_at > now()" not in joined.lower() or "request_ip_hash" in joined.lower()


@pytest.mark.parametrize("junk", [
    "sk_live_stripe_secret",
    "Bearer something",
    "' OR 1=1 --",
    "random-string",
    "dch_" + "x" * 200,
])
def test_only_our_own_key_shapes_are_ever_looked_up(junk):
    """Never take an arbitrary caller-supplied string to the database."""
    out, cur = _run(presented=junk, key_row=_live_key_row())
    assert out.get("reuse_basis") != "presented_key", \
        f"{junk!r} must not be treated as one of our keys"


def test_expired_key_does_not_short_circuit_the_mint():
    """key_row None models 'no live row for that key' — the agent should be
    allowed a fresh trial rather than being stuck."""
    out, _ = _run(presented="dch_trial_OLD", key_row=None)
    assert out.get("reuse_basis") != "presented_key"


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF"])
def test_kill_switch_disables_the_probe(val):
    out, _ = _run(presented="dch_trial_ABC", key_row=_live_key_row(),
                  env={"AUTO_TRIAL_KEY_PROBE": val})
    assert out.get("reuse_basis") != "presented_key"


def test_bearer_header_is_accepted_as_a_presented_key():
    """Some clients send the key as Authorization: Bearer rather than X-API-Key."""
    import datetime as _dt
    cur = _Cur(key_row=_live_key_row())
    conn = _Conn(cur)
    req = _Req({"User-Agent": "node", "Authorization": "Bearer dch_trial_ABC"})
    ns = {
        "os": types.SimpleNamespace(environ={}),
        "hashlib": hashlib, "request": req, "_conn": lambda: conn,
        "_ensure_schema": lambda c: None,
        "note_swallowed_write": lambda *a, **k: None,
        "TRIAL_FREE_CALLS_UNBOUND": 5, "TRIAL_DAILY_CALLS": 50,
        "TRIAL_DAILY_UNBOUND": 10, "TRIAL_DAYS": 7, "datetime": _dt,
    }
    mod = ast.Module(body=[_extract("mint_trial_for_request")], type_ignores=[])
    exec(compile(mod, str(SRC), "exec"), ns)          # noqa: S102
    try:
        out = ns["mint_trial_for_request"](req=req)
    except Exception as e:
        out = {"_fell_through": type(e).__name__}
    assert out.get("reuse_basis") == "presented_key"


def test_bots_are_still_skipped_before_any_of_this():
    """The bot guard must stay AHEAD of the probe — a crawler presenting a
    stray key must not be handed one back."""
    out, _ = _run(presented="dch_trial_ABC", key_row=_live_key_row(),
                  ua="Mozilla/5.0 (compatible; Googlebot/2.1)")
    assert out.get("ok") is False and out.get("bot") is True


# ── ordering: the probe is worthless if it runs after the ip probes ──

def test_probe_precedes_the_ip_hash_probes_in_source_order():
    """Order is the entire point. Behind the ip_hash probes this would only ever
    see requests those already missed AND that happened to hold a key — which is
    every case it was written for, so a reorder would silently un-fix it."""
    i_probe = TEXT.find('"reuse_basis": "presented_key"')
    i_leak1 = TEXT.find("leak #1: RE-MINT ESCAPE")
    i_legacy = TEXT.find("Check for existing recent trial key for this caller")
    assert i_probe > 0 and i_leak1 > 0 and i_legacy > 0
    assert i_probe < i_leak1 < i_legacy, \
        "presented-key probe must run before both ip_hash probes"
