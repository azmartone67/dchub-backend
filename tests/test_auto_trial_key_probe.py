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
        # 2026-09-02: mint-time binds now mirror into mcp_dev_keys so a later
        # Stripe payment can lift THAT key's tier. Recorded so a test can assert
        # the mirror FIRED, not merely that it did not crash.
        self.mirrored = []

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
        # This harness execs mint_trial_for_request ALONE, so every module-level
        # name it touches must be declared here — an undeclared one surfaces as a
        # bare NameError swallowed into {"_fell_through": "NameError"}.
        "_mirror_trial_to_mcp_dev_keys": lambda k, e: cur.mirrored.append((k, e)),
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
    # ★ the bind must also MIRROR into mcp_dev_keys — without it a later Stripe
    # payment by this email has no row to lift and the key stays free.
    assert cur.mirrored, "a mint-time bind must mirror into mcp_dev_keys"
    assert cur.mirrored[0][1] == "Ops@Example.COM ", cur.mirrored


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
        # This harness execs mint_trial_for_request ALONE, so every module-level
        # name it touches must be declared here — an undeclared one surfaces as a
        # bare NameError swallowed into {"_fell_through": "NameError"}.
        "_mirror_trial_to_mcp_dev_keys": lambda k, e: cur.mirrored.append((k, e)),
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

# ── ordering + the removed cross-UA block ────────────────────────────

def test_presented_probe_precedes_the_mint_and_the_ip_reuse_is_gone():
    """Order is the point: behind any reuse the presented-key probe would only
    ever see requests it already missed AND that held a key — every case it
    exists for — so it must run first, ahead of the carry-forward mint.

    Both ip_hash re-mint blocks that used to sit between them are gone now: the
    cross-UA gated return (an existing key by ip alone) and the
    same-(ip_hash, request_ua) reuse (an existing key — a bound one included —
    by network attributes). A caller without its key falls through to the fresh
    born-gated mint. This is the structural companion to the behavioural
    hand-back tests below."""
    i_probe = TEXT.find('"reuse_basis": "presented_key"')
    i_carry = TEXT.find("CARRY THE COUNTER FORWARD")
    assert i_probe > 0 and i_carry > 0
    assert i_probe < i_carry, "presented-key probe must precede the carry mint"
    # neither ip_hash handback may come back — they are the whole bug this closes
    assert "leak #1: RE-MINT ESCAPE" not in TEXT, (
        "the cross-UA gated re-mint (an existing key by ip alone) must stay gone")
    assert "Check for existing recent trial key for this caller" not in TEXT, (
        "the same-(ip_hash, request_ua) reuse must stay removed")
    assert "request_ua = %s" not in TEXT, (
        "the (ip_hash, request_ua) reuse SELECT predicate must stay removed")


# ── a gated network no longer hands back a key it was not shown ──────────
#
# The removed block returned an existing GATED unbound key to whoever POSTed
# auto-mint from the same ip_hash under ANY user agent — so on a shared egress
# IP a second caller received the first caller's live key. These execute the
# real function against a cursor that WOULD serve that stranger's key if the
# code asked for it, and assert it never does: the caller gets a fresh key,
# born past the gate via the existing carry-forward path.

EXISTING  = "dch_trial_" + "E" * 32     # a stranger's live gated key
EXISTING2 = "dch_trial_" + "F" * 32     # the caller's own key, same ip+ua


class _Cur2:
    def __init__(self, *, carried, expires, ua_match=None):
        self.carried = carried          # gate-carry MAX(call_count) for this ip
        self.expires = expires
        self.ua_match = ua_match         # block-3 (ip+ua) answer, or None
        self.queries = []
        self.inserted = []               # api_keys minted via INSERT
        self.insert_params = []
        self._last = None

    def execute(self, sql, params=None):
        q = " ".join(sql.split()).lower()
        self.queries.append((q, params))
        if q.startswith("update"):
            self._last = None
            return
        if q.startswith("insert into auto_trial_keys"):
            self.inserted.append(params[0])
            self.insert_params.append(params)
            self._last = (self.expires,)
            return
        if "max(coalesce(call_count, 0))" in q:            # gate-carry (this table)
            self._last = (self.carried,)
            return
        if "from mcp_dev_keys" in q:                        # gate-carry (claim keys)
            self._last = (0,)
            return
        if "coalesce(call_count, 0) >= %s" in q and "order by minted_at desc" in q:
            # ★ TRAP: the exact query of the removed cross-UA gated-return block.
            # A mutant that re-adds it reads THIS — a stranger's key — and the
            # behavioural asserts below then fail, killing the mutant.
            self._last = (EXISTING, self.expires)
            return
        if "where api_key = %s and expires_at > now()" in q:   # presented-key probe
            self._last = None
            return
        if "request_ua = %s" in q:                          # block-3 legacy ip+ua
            self._last = (self.ua_match, self.expires) if self.ua_match else None
            return
        self._last = None

    def fetchone(self):
        return self._last

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run2(*, carried, ua="a-fresh-ua", ua_match=None, presented=None,
          operator_email=""):
    import datetime as _dt
    import secrets as _secrets
    expires = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=5)
    cur = _Cur2(carried=carried, expires=expires, ua_match=ua_match)
    conn = _Conn(cur)
    headers = {"User-Agent": ua}
    if presented:
        headers["X-API-Key"] = presented
    req = _Req(headers)
    ns = {
        "os": types.SimpleNamespace(environ={}),
        "hashlib": hashlib, "secrets": _secrets, "request": req,
        "_conn": lambda: conn, "_ensure_schema": lambda c: None,
        "_mirror_trial_to_mcp_dev_keys":
            lambda k, e: cur.__dict__.setdefault("mirrored", []).append((k, e)),
        "note_swallowed_write": lambda *a, **k: None,
        "TRIAL_FREE_CALLS_UNBOUND": 5, "TRIAL_DAILY_CALLS": 50,
        "TRIAL_DAILY_UNBOUND": 10, "TRIAL_DAYS": 7, "datetime": _dt,
    }
    mod = ast.Module(body=[_extract("mint_trial_for_request")], type_ignores=[])
    exec(compile(mod, str(SRC), "exec"), ns)              # noqa: S102
    try:
        out = ns["mint_trial_for_request"](req=req, tool_name="t",
                                           client_name="c",
                                           operator_email=operator_email)
    except Exception as e:                                # pragma: no cover
        out = {"_fell_through": type(e).__name__, "_err": repr(e)}
    return out, cur


def _block2_query_was_issued(cur):
    return any("coalesce(call_count, 0) >= %s" in q and "order by minted_at desc" in q
               for q, _ in cur.queries)


def _block3_query_was_issued(cur):
    # The (ip_hash, request_ua) reuse SELECT — the removed same-fingerprint
    # handback. Its signature is the `request_ua = %s` predicate.
    return any("request_ua = %s" in q for q, _ in cur.queries)


def test_a_gated_network_no_longer_hands_back_an_existing_key():
    """The fix, stated as behaviour: a caller that presents no key, on a network
    already past the free-call gate, must be MINTED a fresh key — never handed
    the existing one the cursor is holding for that ip."""
    out, cur = _run2(carried=8)   # 8 >= TRIAL_FREE_CALLS_UNBOUND(5) -> born gated
    assert out.get("api_key", "").startswith("dch_trial_"), out
    assert out.get("api_key") != EXISTING, "a stranger's key was handed back"
    assert out.get("reused") is False, "a fresh mint, not a re-use"
    assert cur.inserted and cur.inserted[0] == out["api_key"], "must mint fresh"
    assert not _block2_query_was_issued(cur), (
        "the cross-UA gated-return query ran — the removed block is back")


def test_the_fresh_key_is_born_past_the_gate():
    """Closing the hand-back must not reopen the free-call gate: the fresh key
    still demands a bind, and carries the seed the validator reads as gated."""
    out, cur = _run2(carried=8)
    assert out.get("bind_required") is True
    assert out.get("gate") == "bind_email_required"
    assert out.get("free_calls_unbound") == 5
    call_count, notes = cur.insert_params[0][7], cur.insert_params[0][8]
    assert call_count == 8, cur.insert_params[0]
    assert (notes or "").startswith("gate_carry:8"), notes


def test_a_partly_used_network_gets_a_clean_fresh_key_not_a_handback():
    """Below the gate the carry-forward seeds nothing, so the caller gets a
    clean fresh key. Still never the stranger's key."""
    out, cur = _run2(carried=2)   # 2 < 5 -> not carried
    assert out.get("api_key", "").startswith("dch_trial_")
    assert out.get("api_key") != EXISTING
    assert out.get("reused") is False
    assert out.get("bind_required") is None, out
    assert cur.insert_params[0][8] is None, "no gate_carry seed below the gate"
    assert not _block2_query_was_issued(cur)


def test_same_ip_and_ua_no_longer_hands_back_the_matching_key():
    """The (ip_hash, request_ua) reuse is removed. A caller that presents no key
    but shares a coarse (ip, ua) fingerprint with an existing key — a BOUND one
    included, since the removed SELECT had no bound/gated filter — is minted a
    FRESH key, never handed the matching one. The cursor here WOULD serve
    EXISTING2 if that SELECT still ran; the code must not ask for it."""
    out, cur = _run2(carried=8, ua_match=EXISTING2)
    assert out.get("api_key", "").startswith("dch_trial_"), out
    assert out.get("api_key") != EXISTING2, "the (ip, ua) match was handed back"
    assert out.get("reused") is False, "a fresh mint, not a re-use"
    assert cur.inserted and cur.inserted[0] == out["api_key"], "must mint fresh"
    assert not _block3_query_was_issued(cur), (
        "the (ip_hash, request_ua) reuse SELECT ran — the removed block is back")
    # and closing the hand-back must not reopen the gate: 8 >= 5 -> born gated
    assert out.get("bind_required") is True


def test_a_held_key_is_still_returned_through_the_presented_probe():
    """The presented-key probe is untouched: a caller that SHOWS its key still
    gets it back (that caller already holds it)."""
    out, _ = _run(presented="dch_trial_ABC", key_row=_live_key_row())
    assert out.get("api_key") == "dch_trial_ABC"
    assert out.get("reuse_basis") == "presented_key"
