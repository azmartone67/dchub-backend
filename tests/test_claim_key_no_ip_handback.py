"""POST /api/v1/keys/claim no longer hands back an existing key by IP.

WHAT THIS GUARDS
----------------
claim_key had a re-mint block (`2026-07-10 leak #1: RE-MINT ESCAPE`) that, when
this IP already held a GATED unbound `claim_api` key, returned that SAME key to
the caller — matched on `metadata->>'ip'` ALONE, under any client_name and any
UA. `ip` is a coarse fingerprint: on a shared egress (NAT/CGNAT/VPN/cloud) a
different caller presents the same ip, so the block could return a key minted
for someone else's session.

The block is removed. A gated identity now falls through to the counter-carry
mint below — the same mechanism the auto-mint door uses: a FRESH `dch_live_` key
is minted, seeded with this network's carried unbound count, so it is born past
the bind gate. Re-minting still does not reset the gate, WITHOUT handing back a
credential. The `(client_name, ip)` dedupe (the intended multi-agent-shared-host
feature) and the unused-key cap are untouched.

Like tests/test_auto_trial_key_probe.py, this pulls the real `claim_key` out of
the source with `ast` and executes it against a stub cursor — a BEHAVIOURAL test
of the shipped handler, not a grep. The cursor is PRIMED to serve a stranger's
key if the removed SELECT ever runs again; the asserts below prove the handler
never asks for it.
"""
import ast
import datetime as _dt
import json as _json
import pathlib
import secrets as _secrets
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SRC = ROOT / "flask_mcp_endpoints.py"
TEXT = SRC.read_text()
TREE = ast.parse(TEXT)

# The real bind-gate threshold, so the test is not coupled to the literal 10.
from routes.auto_trial import TRIAL_FREE_CALLS_UNBOUND as GATE  # noqa: E402

STRANGER = "dch_live_" + "S" * 32   # a gated key the removed block would hand back
OWNKEY = "dch_live_" + "O" * 32     # the caller's own key on the same (client, ip)


def _claim_fn():
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == "claim_key":
            node.decorator_list = []        # strip @mcp_bp.post so it execs alone
            return node
    raise AssertionError("claim_key not found in flask_mcp_endpoints.py")


def _module_fn(name):
    """Pull a module-level helper out of the SAME tree and exec it alongside the
    handler.

    This harness execs claim_key against an explicit `ns`, so every free name the
    handler reaches for has to be supplied. A helper added to the module is a NEW
    free name and the handler raises NameError on it — which is how
    `_advertised_daily` broke all four tests here.

    Extracted rather than stubbed on purpose: a stub would answer whatever the
    test wanted and drift from the real helper the moment either changed. The
    point of this file is that it runs the REAL function.
    """
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
        # r-partner-meter: a helper can read a module-level CONSTANT, which is an
        # Assign, not a FunctionDef — _partner_egress reads
        # _PARTNER_EGRESS_DEFAULT. Supply those the same way.
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node
    raise AssertionError(f"{name} not found in flask_mcp_endpoints.py")


# ── the removed SELECT's signature, in one place ─────────────────────────────
def _escape_query_issued(cur):
    """The RE-MINT ESCAPE SELECT returned a gated key by ip. Its signature is the
    `validate_calls >= %s` filter with a newest-first LIMIT 1 — distinct from the
    carry-forward MAX() aggregate and from the (client_name, ip) dedupe."""
    return any("validate_calls')::int, 0) >= %s" in q and "order by created_at desc" in q
               for q, _ in cur.queries)


# ── stub scaffolding ─────────────────────────────────────────────────────────
class _Cur:
    def __init__(self, *, dedupe=None, anon_dedupe=None, unused=None,
                 carry_live=0, carry_trial=0):
        self.dedupe = dedupe             # (created_at, api_key, tier) or None
        self.anon_dedupe = anon_dedupe
        self.unused = list(unused or [])  # rows for the unused-key cap
        self.carry_live = carry_live      # MAX(validate_calls) over claim keys on ip
        self.carry_trial = carry_trial    # MAX(call_count) over trial keys on ip
        self.queries = []
        self.inserted = []                # params of INSERT INTO mcp_dev_keys
        self._last = None
        self._all = []

    def execute(self, sql, params=None):
        q = " ".join(sql.split()).lower()
        self.queries.append((q, params))
        # ★ TRAP: the removed ip-only gated handback. If it ever runs again it
        # reads a STRANGER's key here, and the behavioural asserts below fail.
        if "validate_calls')::int, 0) >= %s" in q and "order by created_at desc" in q:
            self._last = (STRANGER, "free", GATE + 2)
            self._all = [self._last]
            return
        if "insert into mcp_dev_keys" in q:
            self.inserted.append(params)
            self._last, self._all = None, []
            return
        if q.startswith("update"):
            self._last, self._all = None, []
            return
        if "max(coalesce((metadata->>'validate_calls')" in q:   # carry-forward (claim)
            self._last = (self.carry_live,)
            return
        if "max(coalesce(call_count, 0))" in q:                 # carry-forward (trial)
            self._last = (self.carry_trial,)
            return
        if "count(*) over ()" in q and "last_used_at is null" in q:   # unused-key cap
            self._all = list(self.unused)
            self._last = self.unused[0] if self.unused else None
            return
        if "metadata->>'client_name' = %s" in q:               # (client_name, ip) dedupe
            self._last = self.dedupe
            return
        if "client_name' is null" in q:                        # anon (ip-only) dedupe
            self._last = self.anon_dedupe
            return
        self._last, self._all = None, []

    def fetchone(self):
        return self._last

    def fetchall(self):
        return self._all

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _ConnCtx:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _Pool:
    def __init__(self, cur):
        self._cur = cur

    def connection(self):
        return _ConnCtx(self._cur)


class _Req:
    def __init__(self, body=None, ip="9.9.9.9", ua="node"):
        self._body = body or {}
        self.headers = {"CF-Connecting-IP": ip, "User-Agent": ua}
        self.remote_addr = ip

    def get_json(self, silent=False):
        return self._body


def _run(*, body, cur, ip="9.9.9.9", pool=None):
    """Execute the real claim_key against the stub cursor — or against `pool`, a
    real database (tests/test_claim_unused_key_cap_sql.py). Returns (json,
    status, cur)."""
    req = _Req(body=body, ip=ip)
    # a no-op stand-in for the confirmation-email module imported inside the fn
    fake_verif = types.ModuleType("routes.mcp_key_email_verification")
    fake_verif.offer_confirmation = lambda *a, **k: None
    saved = sys.modules.get("routes.mcp_key_email_verification")
    sys.modules["routes.mcp_key_email_verification"] = fake_verif

    import re as _re
    ns = {
        "request": req,
        "os": types.SimpleNamespace(environ={}),
        "jsonify": lambda **kw: dict(kw),        # (dict, status) tuples come back raw
        "_kc_re": _re,
        "_pool": pool or _Pool(cur),
        "secrets": _secrets,
        "json": _json,
        "datetime": _dt.datetime,
        "timezone": _dt.timezone,
        "_restamp_claim_session": lambda *a, **k: None,
        "_inherit_paid_tier": lambda *a, **k: 0,
        "_streak_ladder_text": lambda: "",
    }
    mod = ast.Module(
        body=[_module_fn("_advertised_daily"),
              _module_fn("_PARTNER_EGRESS_DEFAULT"),
              _module_fn("_partner_egress"),
              _module_fn("_partner_meter_scope"),
              _claim_fn()],
        type_ignores=[])
    exec(compile(mod, str(SRC), "exec"), ns)      # noqa: S102 — the point
    try:
        out = ns["claim_key"]()
    finally:
        if saved is not None:
            sys.modules["routes.mcp_key_email_verification"] = saved
        else:
            sys.modules.pop("routes.mcp_key_email_verification", None)
    assert isinstance(out, tuple), out
    return out[0], out[1], cur


def _minted_metadata(cur):
    assert cur.inserted, "no INSERT into mcp_dev_keys ran"
    return _json.loads(cur.inserted[0][3])


# ── the fix, stated as behaviour ─────────────────────────────────────────────

def test_a_gated_ip_no_longer_hands_back_an_existing_key():
    """A caller on a network already past the gate, under a client_name with no
    prior key, must be MINTED a fresh key — never handed the gated key the
    cursor holds for that ip."""
    cur = _Cur(carry_live=GATE + 2)          # ip crossed the gate; dedupe misses
    j, status, cur = _run(body={"client_name": "a-new-agent"}, cur=cur)
    assert status == 200
    assert j["api_key"].startswith("dch_live_"), j
    assert j["api_key"] != STRANGER, "the gated key was handed back by ip"
    assert j.get("reused") is not True, "a fresh mint, not a re-use"
    assert cur.inserted and cur.inserted[0][0] == j["api_key"], "must mint fresh"
    assert not _escape_query_issued(cur), (
        "the ip-only gated-return SELECT ran — the removed block is back")


def test_the_fresh_key_is_born_past_the_gate():
    """Closing the hand-back must not reopen the gate: the fresh key carries the
    seeded meter and says bind_required, so the very next validate refuses it."""
    cur = _Cur(carry_live=GATE + 2)
    j, _, cur = _run(body={"client_name": "a-new-agent"}, cur=cur)
    assert j.get("bind_required") is True
    assert j.get("gate") == "bind_email_required"
    md = _minted_metadata(cur)
    assert md.get("validate_calls") == GATE + 2, md
    assert md.get("gate_carried_from_identity") is True, md


def test_a_clean_ip_gets_a_clean_fresh_key():
    """Below the gate the carry seeds nothing: the caller gets an ordinary fresh
    key, still never a stranger's."""
    cur = _Cur(carry_live=0, carry_trial=0)
    j, _, cur = _run(body={"client_name": "a-new-agent"}, cur=cur)
    assert j["api_key"].startswith("dch_live_")
    assert j["api_key"] != STRANGER
    assert j.get("bind_required") is None, j
    md = _minted_metadata(cur)
    assert "validate_calls" not in md, "no gate seed below the gate"
    assert not _escape_query_issued(cur)


def test_same_client_and_ip_still_reuses_its_own_key():
    """Surgical: only the ip-only handback was removed. The (client_name, ip)
    dedupe that returns a returning agent's OWN key is unchanged — even on a
    gated ip, a same-client caller gets ITS key back, not a stranger's, and not
    a fresh mint."""
    now = _dt.datetime.now(_dt.timezone.utc)
    cur = _Cur(dedupe=(now, OWNKEY, "free"), carry_live=GATE + 2)
    j, status, cur = _run(body={"client_name": "steady-agent"}, cur=cur)
    assert status == 200
    assert j["api_key"] == OWNKEY
    assert j.get("reused") is True
    assert not cur.inserted, "a (client_name, ip) match must reuse, not mint"
    assert not _escape_query_issued(cur)


def test_the_unused_key_cap_still_hands_back_an_unused_key():
    """The other intended reuse — the r-unused-key-cap enumeration guard — must
    survive the change: at/over the cap the newest UNUSED key is returned."""
    cur = _Cur(unused=[(OWNKEY, "free"), ("dch_live_" + "U" * 32, "free"),
                       ("dch_live_" + "V" * 32, "free")])   # >= default cap of 3
    j, status, cur = _run(body={"client_name": "a-new-agent"}, cur=cur)
    assert status == 200
    assert j.get("gate") == "unused_key_cap"
    assert j["api_key"] == OWNKEY               # the newest unused key
    assert not cur.inserted, "over the cap must reuse, not mint"
    assert not _escape_query_issued(cur)


# ── structural companion: the removed SELECT must stay gone ──────────────────

def test_the_removed_escape_select_stays_removed():
    """Behavioural tests above prove the handler does not ask; this pins that the
    ip-only gated-return SELECT is not reintroduced verbatim in the source."""
    assert "leak #1: RE-MINT ESCAPE" not in TEXT, (
        "the RE-MINT ESCAPE comment is back — check the block came with it")
    # the escape's own filter — a gated key selected by ip via a validate_calls
    # threshold. The carry-forward uses MAX() (no `>= %s`), so this predicate is
    # unique to the removed handback.
    assert "validate_calls')::int, 0) >= %s" not in TEXT, (
        "the ip-only gated-return SELECT predicate is back in the source")


# ── r-partner-meter: per-WORKSPACE allowance for a verified partner ──────────
#
# A hosted catalogue proxies every customer through one egress, so an IP-metered
# allowance is shared by all of them: customer #1 works, everyone after is born
# gated. For AnythingMCP — client_name `anythingmcp/<workspace-id>`, egress
# 104.248.242.235 — the carry must scope to the workspace instead.
#
# Asserted on the query the REAL handler issues and what it binds. Per-workspace
# isolation lives in the WHERE clause; if the carry is keyed on client_name,
# workspace B cannot inherit workspace A's count, whatever those counts are.

PARTNER_IP = "104.248.242.235"
WS_A = "anythingmcp/clx1a2b3c4d5e6f7"


def _carry_queries(cur):
    return [(q, p) for q, p in cur.queries
            if "max(coalesce((metadata->>'validate_calls')" in q]


def _trial_queries(cur):
    return [(q, p) for q, p in cur.queries
            if "max(coalesce(call_count, 0))" in q]


def test_verified_partner_traffic_is_metered_per_workspace():
    cur = _Cur()
    _run(body={"client_name": WS_A}, cur=cur, ip=PARTNER_IP)
    carry = _carry_queries(cur)
    assert carry, "no carry query ran at all"
    q, params = carry[0]
    assert "metadata->>'client_name' = %s" in q, (
        "verified partner traffic was not scoped to its workspace")
    assert "metadata->>'ip' = %s" not in q, (
        "the partner carry still keys on the shared egress IP — every "
        "workspace behind it draws on one allowance")
    assert WS_A in params and PARTNER_IP not in params


def test_partner_traffic_skips_the_ip_hashed_trial_carry():
    """auto_trial_keys can only be matched by IP. Matching the partner's shared
    egress there would re-import exactly the collapse this removes."""
    cur = _Cur()
    _run(body={"client_name": WS_A}, cur=cur, ip=PARTNER_IP)
    assert not _trial_queries(cur), (
        "partner traffic still reads the IP-keyed trial carry")


def test_the_prefix_alone_from_any_other_address_is_not_trusted():
    """Otherwise anyone could send 'anythingmcp/x' and draw on — or burn — an
    allowance attached to the partner's name."""
    cur = _Cur()
    _run(body={"client_name": WS_A}, cur=cur, ip="203.0.113.9")
    q, params = _carry_queries(cur)[0]
    assert "metadata->>'ip' = %s" in q, (
        "a partner prefix from an undeclared address was trusted")
    assert "203.0.113.9" in params


def test_a_bare_prefix_with_no_workspace_id_is_not_partner_traffic():
    cur = _Cur()
    _run(body={"client_name": "anythingmcp/"}, cur=cur, ip=PARTNER_IP)
    q, _ = _carry_queries(cur)[0]
    assert "metadata->>'ip' = %s" in q, (
        "a prefix with no workspace id was scoped as a workspace")


def test_a_non_partner_name_from_the_partner_address_is_metered_by_ip():
    """The address alone is not the credential either."""
    cur = _Cur()
    _run(body={"client_name": "someone-else"}, cur=cur, ip=PARTNER_IP)
    q, _ = _carry_queries(cur)[0]
    assert "metadata->>'ip' = %s" in q


def test_the_key_records_which_meter_it_was_carried_from():
    """Auditable: a key must say whether its allowance came from a workspace
    or from an IP, or a metering dispute cannot be settled from the row."""
    cur = _Cur()
    _run(body={"client_name": WS_A}, cur=cur, ip=PARTNER_IP)
    assert _minted_metadata(cur).get("meter_scope") == "partner:anythingmcp/"
    cur2 = _Cur()
    _run(body={"client_name": "someone-else"}, cur=cur2, ip="9.9.9.9")
    assert _minted_metadata(cur2).get("meter_scope") == "ip"


def test_a_workspace_past_the_gate_carries_its_count_into_the_new_key():
    """The query being scoped correctly is not enough — its RESULT has to land.

    Written after a real bug in the first draft of this change: an automated
    re-indent left `_cf_live = ...` inside the non-partner branch. The partner
    carry query still ran, bound to the right client_name — so every
    query-shape test above passed — but `_cf_live` was never assigned on that
    path, `max(_cf_live, _cf_trial)` raised NameError, the broad `except` turned
    it into a carry of 0, and a workspace already past the gate was reborn with
    a fresh allowance on every re-mint. It compiled. It passed. It was wrong.
    """
    cur = _Cur(carry_live=GATE + 2)
    _run(body={"client_name": WS_A}, cur=cur, ip=PARTNER_IP)
    meta = _minted_metadata(cur)
    assert meta.get("validate_calls") == GATE + 2, (
        f"the workspace's carried count did not reach the minted key "
        f"(validate_calls={meta.get('validate_calls')!r}); a workspace past "
        f"the gate would re-mint its way to a fresh allowance")


# ── the unused-key cap must not merge partner workspaces ─────────────────────
#
# Over the cap, the handler hands back the newest unused key from the caller's
# IP. Behind a partner's single egress that key belongs to ANOTHER workspace.
# The stub cursor serves its primed rows to any cap query whatever the WHERE
# clause says, so only the handler's own decision can keep workspace 4 off them.
# (Which keys the cap counts is proven against Postgres in
# tests/test_claim_unused_key_cap_sql.py.)

def _three_unused_keys():
    """Newest first, shaped like the cap's SELECT: key, tier, count, client."""
    return [("dch_live_" + c * 32, "identified", 3, "agent-" + c) for c in "CBA"]


def test_a_fourth_partner_workspace_gets_its_own_new_key():
    unused = _three_unused_keys()
    cur = _Cur(unused=unused)
    j, status, cur = _run(body={"client_name": "anythingmcp/ws4"}, cur=cur,
                          ip=PARTNER_IP)
    assert status == 200
    assert j["api_key"] not in {r[0] for r in unused}, (
        "a verified partner workspace was handed a key minted for another caller")
    assert j.get("gate") != "unused_key_cap"
    assert cur.inserted, "the workspace was not minted a key of its own"
    assert j["api_key"] == cur.inserted[0][0]
    assert _minted_metadata(cur)["client_name"] == "anythingmcp/ws4"


@pytest.mark.parametrize("client_name, ip", [
    ("someone-else", PARTNER_IP),        # the partner's address, not its name
    ("anythingmcp/ws4", "203.0.113.9"),  # its name, not its address
    ("a-new-agent", "9.9.9.9"),          # neither
], ids=["partner-address-only", "partner-name-only", "no-partner"])
def test_a_non_partner_caller_still_hits_the_cap(client_name, ip):
    unused = _three_unused_keys()
    cur = _Cur(unused=unused)
    j, status, cur = _run(body={"client_name": client_name}, cur=cur, ip=ip)
    assert status == 200
    assert j.get("gate") == "unused_key_cap"
    assert j["api_key"] == unused[0][0], "must return the NEWEST unused key"
    assert not cur.inserted, "over the cap must reuse, not mint"
