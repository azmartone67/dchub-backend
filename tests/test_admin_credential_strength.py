"""No admin gate may accept a weak or unreviewed credential (2026-08-07).

The bug: routes/jobs_routes._require_admin_key built its accepted set as
[DCHUB_ADMIN_KEY, ADMIN_SECRET] and accepted EITHER. On production
ADMIN_SECRET was a guessable product-name-plus-year literal, so all 30
admin-gated /api/jobs/* endpoints — backup, db-backup, autopilot,
content-publish — were reachable by guessing a product name plus the year.
Verified live: X-API-Key: <literal> against /api/jobs/status returned 200.

The 07-31 DCHUB_ADMIN_KEY rotation missed it because ADMIN_SECRET is a
different env var — the Cloudflare Worker's own secret, copied onto the
backend service and wired in as a second lane.

These tests fence three distinct ways the class comes back:
  1. VALUE   — a weak credential in any accepted env var must be inert.
  2. NAME    — the set of env vars that can satisfy an admin gate is pinned,
               so re-adding ADMIN_SECRET (or anything new) fails until reviewed.
  3. LOCKOUT — the strength filter must not reject the real credentials, or
               the fix trades an open door for an outage.

They drive the REAL functions, not source text, except where the point IS the
source (the allow-list pin), which is parsed with ast rather than grepped.
"""

import ast
import pathlib

# A 64-hex stand-in shaped like the real DCHUB_ADMIN_KEY. Fake, but must clear
# the same strength bar the real one does.
STRONG = "a3f" + "9d4e1c" * 10 + "b7"

# Same SHAPE as the credential that was live-exploitable, but deliberately NOT
# that value: this repo is public and the real one is still the Cloudflare
# Worker's secret until it is rotated. The real value is pinned by sha256 in
# util/admin_auth._COMPROMISED_SHA256, and test_known_compromised_value_is_
# refused below proves that pin is load-bearing without printing it.
WEAK_LIVE_LITERAL = "dchub_admin_2027"

_REPO = pathlib.Path(__file__).resolve().parent.parent

# Env vars reviewed and permitted to satisfy an admin gate. Adding a name here
# is a security review, not a refactor. ADMIN_SECRET is deliberately absent.
_REVIEWED_ADMIN_ENV_VARS = {
    "DCHUB_ADMIN_KEY",    # 64-hex, rotated 2026-07-31, the intended credential
    "DCHUB_INTERNAL_KEY", # 64-char service-to-service key
    "INTERNAL_KEY",       # legacy alias of the above, empty in production
    "ADMIN_API_KEY",      # 32-hex; exposure tracked in the P0#2 rotation batch
}


def _admin_gate_modules():
    return [_REPO / "main.py"] + sorted((_REPO / "routes").glob("*.py"))


# --------------------------------------------------------------------------
# 1. VALUE — a weak credential must not open anything
# --------------------------------------------------------------------------

def test_weak_admin_secret_no_longer_opens_job_endpoints(monkeypatch):
    """The reported vulnerability, encoded. With ADMIN_SECRET set to a
    credential of the offending shape, the jobs gate must refuse it."""
    import flask
    import routes.jobs_routes as jr
    monkeypatch.setenv("DCHUB_ADMIN_KEY", STRONG)
    monkeypatch.setenv("ADMIN_SECRET", WEAK_LIVE_LITERAL)
    app = flask.Flask(__name__)
    for header in ("X-API-Key", "X-Admin-Key"):
        with app.test_request_context("/api/jobs/status", method="GET",
                                      headers={header: WEAK_LIVE_LITERAL}):
            err = jr._require_admin_key()
            assert err is not None and err[1] == 401, (
                "ADMIN_SECRET='%s' still authenticated via %s — the weak "
                "parallel admin credential is back" % (WEAK_LIVE_LITERAL, header))


def test_weak_admin_secret_rejected_via_query_param(monkeypatch):
    """The gate also reads ?admin_key= and ?key=; both must refuse it."""
    import flask
    import routes.jobs_routes as jr
    monkeypatch.setenv("DCHUB_ADMIN_KEY", STRONG)
    monkeypatch.setenv("ADMIN_SECRET", WEAK_LIVE_LITERAL)
    app = flask.Flask(__name__)
    for param in ("admin_key", "key"):
        with app.test_request_context(
                "/api/jobs/status?%s=%s" % (param, WEAK_LIVE_LITERAL)):
            err = jr._require_admin_key()
            assert err is not None and err[1] == 401, (
                "weak credential accepted via ?%s=" % param)


def test_weak_value_in_a_permitted_var_is_still_refused(monkeypatch):
    """Defence in depth: even the *reviewed* DCHUB_ADMIN_KEY is refused if
    someone sets it to something guessable — so the fence does not depend on
    nobody ever weakening the one remaining credential."""
    import flask
    import routes.jobs_routes as jr
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "admin2026")
    app = flask.Flask(__name__)
    with app.test_request_context("/api/jobs/status",
                                  headers={"X-API-Key": "admin2026"}):
        err = jr._require_admin_key()
        assert err is not None and err[1] == 401, (
            "a weak DCHUB_ADMIN_KEY was accepted")


# --------------------------------------------------------------------------
# 3. LOCKOUT — the real credentials must keep working
# --------------------------------------------------------------------------

def test_strong_admin_key_still_authenticates(monkeypatch):
    """The 32 scheduled workflows send DCHUB_ADMIN_KEY; they must not break."""
    import flask
    import routes.jobs_routes as jr
    monkeypatch.setenv("DCHUB_ADMIN_KEY", STRONG)
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    app = flask.Flask(__name__)
    for header in ("X-API-Key", "X-Admin-Key"):
        with app.test_request_context("/api/jobs/status",
                                      headers={header: STRONG}):
            assert jr._require_admin_key() is None, (
                "the legitimate admin key was rejected via %s — this would "
                "401 every scheduled job" % header)


def test_strength_check_accepts_real_credential_shapes():
    """Every credential SHAPE this repo issues must clear the bar. A strength
    rule that rejects real keys is an outage, not a fix.

    These are synthetic values with the same length and alphabet as the real
    ones — this repo is public, so no live credential appears here. The real
    values were checked against this function separately before it shipped.
    """
    from util.admin_auth import is_weak_credential
    for label, shape in (
        ("64-hex admin key",     "7b2e" + "4a9c1f36" * 7 + "d05e"),
        ("64-char base62 key",   "Qk7mZr2VpL9wXt4BnH6sJd3Yg8Fc5TvA" * 2),
        ("dchub_live_ API key",  "dchub_live_" + "9c41ab72" * 6),
        ("32-hex admin api key", "1d84f0" + "6b39ec52" * 3 + "a7"),
        ("base64url secret",     "z-K4TnWq_RmXbP7yHscE2vJdLgNfA9UeCtY6ZoQiBx0"),
    ):
        assert is_weak_credential(shape) is None, (
            "the %s shape was rejected — this would lock out a real "
            "credential" % label)


def test_strength_check_rejects_weak_shapes():
    """Each entry mirrors the SHAPE of a weak value found in the production
    environment on 2026-08-07, without reproducing the value itself."""
    from util.admin_auth import is_weak_credential
    for label, shape in (
        ("product name + year",      WEAK_LIVE_LITERAL),
        ("capitalised word + digits + punctuation", "Wordcountry12!"),
        ("single dictionary word",   "hourly"),
        ("two digits",               "42"),
        ("bare 'admin'",             "admin"),
        ("common password",          "password"),
        ("placeholder",              "changeme"),
        ("long but no entropy",      "a" * 40),
        ("padded dictionary word",   "adminadminadminadminadmin"),
        ("empty", ""), ("whitespace", "   "), ("unset", None),
    ):
        assert is_weak_credential(shape) is not None, (
            "weak credential accepted (%s)" % label)


def test_known_compromised_value_is_refused_by_hash():
    """The credential that was live-exploitable is pinned by sha256 in
    util/admin_auth, so it stays rejected even if the length floor is lowered.

    The value is not written here (public repo). Instead this proves the hash
    pin is populated and load-bearing: a value whose digest is in the set is
    refused even though it would otherwise sail past every other rule.
    """
    import hashlib
    from util import admin_auth
    assert admin_auth._COMPROMISED_SHA256, (
        "the known-compromised pin is empty — the hash lane is vacuous")
    strong_but_banned = "Xq7" + "2mB9vKd4" * 7 + "Lz1"
    assert admin_auth.is_weak_credential(strong_but_banned) is None, (
        "fixture is not otherwise-strong, so the next assertion proves nothing")
    digest = hashlib.sha256(strong_but_banned.encode()).hexdigest()
    banned = frozenset(admin_auth._COMPROMISED_SHA256 | {digest})
    monkey = admin_auth._COMPROMISED_SHA256
    try:
        admin_auth._COMPROMISED_SHA256 = banned
        assert admin_auth.is_weak_credential(strong_but_banned) == (
            "known-compromised value"), "the sha256 denylist is not consulted"
    finally:
        admin_auth._COMPROMISED_SHA256 = monkey


def test_accepted_admin_keys_drops_weak_and_keeps_strong(monkeypatch):
    from util.admin_auth import accepted_admin_keys
    monkeypatch.setenv("DCHUB_ADMIN_KEY", STRONG)
    monkeypatch.setenv("ADMIN_SECRET", WEAK_LIVE_LITERAL)
    keys = accepted_admin_keys(("DCHUB_ADMIN_KEY", "ADMIN_SECRET"))
    assert keys == {STRONG}, (
        "accepted_admin_keys returned %d keys; the weak one was not dropped"
        % len(keys))


# --------------------------------------------------------------------------
# 2. NAME — the accepted env vars are pinned to a reviewed set
# --------------------------------------------------------------------------

def _accepted_admin_key_call_args():
    """Every env-var name string passed to accepted_admin_keys(), by file."""
    found = []
    for path in _admin_gate_modules():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "accepted_admin_keys" or not node.args:
                continue
            arg = node.args[0]
            if not isinstance(arg, (ast.Tuple, ast.List, ast.Set)):
                raise AssertionError(
                    "%s passes a non-literal to accepted_admin_keys; this "
                    "fence can only review literal env-var names" % path.name)
            for elt in arg.elts:
                assert isinstance(elt, ast.Constant) and isinstance(elt.value, str), (
                    "%s: non-literal env var name in accepted_admin_keys" % path.name)
                found.append((path.name, elt.value))
    return found


# Modules with an admin gate that MUST assemble it via accepted_admin_keys.
# Without this pin the allow-list check is bypassable: hand-roll the accepted
# set in one module and the other call sites keep the fence green. That exact
# mutation survived the first version of this test.
_MODULES_REQUIRED_TO_USE_HELPER = {
    "jobs_routes.py",
    "devrel_targets.py",
    "upgrade_pool_outreach.py",
    "paywall_test.py",
    "visitor_intelligence.py",
}


def test_every_admin_gate_module_uses_the_strength_helper():
    """Known admin gates must not hand-roll their accepted set.

    Limitation, stated rather than hidden: this pins the modules that have an
    admin gate TODAY. A brand-new module that hand-rolls one is not caught
    here — the allow-list below only reviews what flows through the helper.
    """
    using = {f for f, _ in _accepted_admin_key_call_args()}
    missing = sorted(_MODULES_REQUIRED_TO_USE_HELPER - using)
    assert not missing, (
        "%s has an admin gate that no longer routes through "
        "accepted_admin_keys — its credentials are unchecked for strength "
        "and invisible to the allow-list fence below." % ", ".join(missing))


def test_admin_env_var_allowlist_is_pinned():
    """Every env var that can satisfy an admin gate must be pre-reviewed.
    Re-adding ADMIN_SECRET anywhere trips this."""
    found = _accepted_admin_key_call_args()
    assert found, (
        "no accepted_admin_keys call sites found — the fence is vacuous; the "
        "admin gates were probably refactored away from the helper")
    unreviewed = sorted({(f, n) for f, n in found
                         if n not in _REVIEWED_ADMIN_ENV_VARS})
    assert not unreviewed, (
        "unreviewed env var(s) wired into an admin gate: %s. Add to "
        "_REVIEWED_ADMIN_ENV_VARS only after confirming the value is "
        "high-entropy and the variable is meant to be an admin credential."
        % unreviewed)


def test_admin_secret_is_not_read_anywhere_in_request_paths():
    """ADMIN_SECRET must not be read by main.py or any routes/ module at all.

    Exact-match on the name so DCHUB_ADMIN_SECRET (a different, separately
    tracked variable) does not mask a real ADMIN_SECRET reintroduction.
    """
    offenders = []
    for path in _admin_gate_modules():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", None) == "get"):
                continue
            # os.environ.get("ADMIN_SECRET", ...) / environ.get(...)
            if not node.args:
                continue
            first = node.args[0]
            if (isinstance(first, ast.Constant)
                    and first.value == "ADMIN_SECRET"):
                offenders.append("%s:%d" % (path.name, node.lineno))
    assert not offenders, (
        "ADMIN_SECRET is read again in a request path at %s. It held a "
        "guessable literal on production and is the Cloudflare Worker's "
        "secret, not a backend admin credential." % ", ".join(offenders))
