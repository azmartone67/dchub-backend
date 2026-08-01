"""ISONE_PASSWORD leaked verbatim into public GitHub Actions logs.

Every .github/workflows/data-pulse.yml run printed the ISO orchestrator
response body, which carried:

    {"error":"InvalidURL: nonnumeric port:
      '<password>@webservices.iso-ne.com'","iso":"ISONE","status":"error"}

Two failures in one line of code:

  1. CREDENTIAL EXPOSURE. Because the value was built into a string by
     our own code rather than passed through GitHub secrets, Actions'
     secret masking never redacted it.

  2. BROKEN FEED. http.client.InvalidURL is an HTTPException, NOT an
     OSError, so it escaped fetch_first_working's except clause and
     aborted the whole URL chain — the working EIA fallback below it
     was never tried. ISO-NE returned zero rows on every pull.

Note the trap this locks in: the offending line ALREADY percent-encoded
both credentials with urllib.parse.quote(). That does not help, because
urllib.request's Request._parse() calls unquote() on the netloc before
http.client ever sees it. Encoding the credential is not a fix; keeping
it out of the URL is.

Tests never import main.py (or the Flask/psycopg2 blueprint chain) — the
real functions are pulled out of source with `ast` and run against stubs.
"""
import ast
import datetime
import http.client
import os
import re
import urllib.request
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IC = os.path.join(ROOT, "routes", "_iso_common.py")
ISONE = os.path.join(ROOT, "routes", "iso_isone.py")

# Synthetic stand-ins. The real credential is NEVER committed here; it is
# being rotated out of Railway as a separate ops step.
FAKE_USER = "isone-test-user@example.com"   # ISO-NE usernames are emails
FAKE_PASS = "Sentinel-PW-9x!Qz"             # non-numeric, contains "!"


def _load(path, names, preload=None):
    """Exec selected top-level defs/assignments from `path`."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"os": os, "re": re, "datetime": datetime.datetime,
          "timezone": datetime.timezone, "urllib": urllib,
          "http": http}
    ns.update(preload or {})
    wanted, found = set(names), set()
    for node in tree.body:
        nm = None
        if isinstance(node, ast.FunctionDef):
            nm = node.name
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            nm = node.targets[0].id
        if nm in wanted:
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         path, "exec"), ns)
            found.add(nm)
    missing = wanted - found
    assert not missing, f"not found in {os.path.basename(path)}: {missing}"
    return ns


def _scrub_secrets():
    ns = _load(IC, ["SECRET_ENV_KEYS", "SECRET_ENV_PAIRS", "_MIN_REDACTABLE",
                    "_RE_USERINFO", "_RE_SECRET_PARAM", "scrub_secrets"])
    return ns["scrub_secrets"]


def _isone_url_builder():
    return _load(ISONE, ["_today", "_yesterday", "_isone_urls"])["_isone_urls"]


def _urls_with_creds():
    """Run the real _isone_urls() with credentials present."""
    build = _isone_url_builder()
    with patch.dict(os.environ, {"ISONE_USERNAME": FAKE_USER,
                                 "ISONE_PASSWORD": FAKE_PASS,
                                 "EIA_API_KEY": "EIAKEY-SENTINEL"},
                    clear=False):
        return build()


def _as_url(entry):
    """Entries are either a bare URL or (url, headers)."""
    return entry[0] if isinstance(entry, (tuple, list)) else entry


# ── root cause: credentials must never enter the URL ──────────────────

def test_isone_urls_never_embed_credentials():
    """The exact regression: no URL may carry the password in any form."""
    from urllib.parse import quote
    for entry in _urls_with_creds():
        url = _as_url(entry)
        for form in (FAKE_PASS, quote(FAKE_PASS), quote(FAKE_PASS, safe=""),
                     FAKE_USER, quote(FAKE_USER, safe="")):
            assert form not in url, f"credential leaked into URL: {url!r}"
        assert "@webservices.iso-ne.com" not in url


def test_isone_basic_auth_is_sent_as_a_header():
    """Dropping the userinfo must not silently drop authentication."""
    import base64
    hdrs = {}
    for entry in _urls_with_creds():
        if isinstance(entry, (tuple, list)) and "webservices.iso-ne.com" in entry[0]:
            hdrs = entry[1] or {}
    assert "Authorization" in hdrs, "ISO-NE call lost its Basic auth"
    scheme, _, token = hdrs["Authorization"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(token).decode() == f"{FAKE_USER}:{FAKE_PASS}"


def test_isone_urls_survive_urllib_host_parsing():
    """The crash itself: every URL must parse through the real stack.

    urllib.request.Request unquotes the netloc, then http.client rfind()s
    the last ':' and treats the tail as a port. This is what produced
    "nonnumeric port: '<password>@webservices.iso-ne.com'".
    """
    for entry in _urls_with_creds():
        url = _as_url(entry)
        host = urllib.request.Request(url).host
        assert FAKE_PASS not in host, f"password reachable via .host: {host!r}"
        # Constructing the connection parses host/port; no socket is opened.
        http.client.HTTPSConnection(host, timeout=1)


def test_isone_source_has_no_userinfo_url_construction():
    """Fence: the userinfo form must not come back, quoted or not."""
    src = open(ISONE, encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert not re.search(r'f"https://\{[^"]*\}:\{[^"]*\}@', code), \
        "credentials are being embedded in a URL again"


# ── leak vector: error strings printed to CI ──────────────────────────

def test_scrub_secrets_redacts_the_observed_production_error():
    """The literal string that reached the public data-pulse log."""
    scrub = _scrub_secrets()
    msg = (f"InvalidURL: nonnumeric port: "
           f"'{FAKE_USER}:{FAKE_PASS}@webservices.iso-ne.com'")
    out = scrub(msg, environ={"ISONE_USERNAME": FAKE_USER,
                              "ISONE_PASSWORD": FAKE_PASS})
    assert FAKE_PASS not in out
    assert FAKE_USER not in out
    assert "***" in out
    # Still has to be useful for debugging.
    assert "nonnumeric port" in out


def test_scrub_secrets_redacts_percent_encoded_form():
    """urllib may hand back either the raw or the encoded credential."""
    from urllib.parse import quote
    scrub = _scrub_secrets()
    env = {"ISONE_PASSWORD": FAKE_PASS}
    out = scrub(f"failed for https://u:{quote(FAKE_PASS)}@host/x", environ=env)
    assert quote(FAKE_PASS) not in out and FAKE_PASS not in out


def test_scrub_secrets_redacts_the_basic_auth_token():
    """The base64 blob IS the credential once we send it as a header."""
    import base64
    scrub = _scrub_secrets()
    token = base64.b64encode(f"{FAKE_USER}:{FAKE_PASS}".encode()).decode()
    out = scrub(f"401 rejected Authorization: Basic {token}",
                environ={"ISONE_USERNAME": FAKE_USER,
                         "ISONE_PASSWORD": FAKE_PASS})
    assert token not in out


def test_scrub_secrets_redacts_eia_key_in_free_text():
    """fetch_first_working embeds whole URLs in its `last=` message."""
    scrub = _scrub_secrets()
    msg = ("all URLs failed; last=https://api.eia.gov/v2/x/?api_key="
           "EIAKEY-SENTINEL&frequency=hourly: HTTP 403")
    out = scrub(msg, environ={"EIA_API_KEY": "EIAKEY-SENTINEL"})
    assert "EIAKEY-SENTINEL" not in out
    assert "HTTP 403" in out


def test_scrub_secrets_structural_pass_catches_unknown_credentials():
    """A credential from no env var we know about is still stripped."""
    scrub = _scrub_secrets()
    out = scrub("fetch https://someone@mail.com:hunter2@example.com/f.json",
                environ={})
    assert "hunter2" not in out
    assert "example.com" in out


def test_scrub_secrets_leaves_clean_text_alone():
    scrub = _scrub_secrets()
    msg = "TimeoutError: timed out after 6s"
    assert scrub(msg, environ={"ISONE_PASSWORD": FAKE_PASS}) == msg


def test_scrub_secrets_handles_empty_and_non_string():
    scrub = _scrub_secrets()
    assert scrub(None) is None
    assert scrub("") == ""
    # An unset credential must not turn into a match-everything empty string.
    assert scrub("abc", environ={"ISONE_PASSWORD": ""}) == "abc"


def test_scrub_url_email_username_does_not_leak_password():
    """scrub_url split on the FIRST '@'. ISO-NE usernames are emails, so
    the password stayed in the half it treated as the host."""
    ns = _load(IC, ["scrub_url"])
    url = f"https://{FAKE_USER}:{FAKE_PASS}@webservices.iso-ne.com/x.json"
    out = ns["scrub_url"](url)
    assert FAKE_PASS not in out, f"scrub_url leaked the password: {out!r}"
    assert "webservices.iso-ne.com" in out


# ── broken feed: a bad URL must not abort the fallback chain ──────────

def test_malformed_url_does_not_abort_the_url_chain():
    """InvalidURL is an HTTPException, not an OSError. It used to escape
    the loop, so every fallback URL after it went untried."""
    ns = _load(IC, ["SECRET_ENV_KEYS", "SECRET_ENV_PAIRS", "_MIN_REDACTABLE",
                    "_RE_USERINFO", "_RE_SECRET_PARAM", "scrub_secrets",
                    "scrub_attempt", "fetch_first_working"],
               preload={"urllib": urllib, "http": http})
    bad = f"https://user:{FAKE_PASS}@webservices.iso-ne.com/x.json"
    try:
        ns["fetch_first_working"]([bad], timeout=1, total_budget=3)
    except RuntimeError as e:
        # Reached the end of the list => the loop kept going, and the
        # message it raises is scrubbed. Note the env is NOT patched
        # here: scrub_attempt must redact using the URL's own userinfo,
        # with no help from SECRET_ENV_KEYS.
        assert "all URLs failed" in str(e)
        assert FAKE_PASS not in str(e)
    except http.client.InvalidURL:
        raise AssertionError("InvalidURL still aborts the whole URL chain")


def test_scrub_attempt_redacts_without_any_env_help():
    """The schemeless InvalidURL message, scrubbed from the URL alone."""
    ns = _load(IC, ["SECRET_ENV_KEYS", "SECRET_ENV_PAIRS", "_MIN_REDACTABLE",
                    "_RE_USERINFO", "_RE_SECRET_PARAM", "scrub_secrets",
                    "scrub_attempt"])
    from urllib.parse import quote
    url = (f"https://{quote(FAKE_USER)}:{quote(FAKE_PASS)}"
           "@webservices.iso-ne.com/api/v1.1/genfuelmix/current.json")
    msg = (f"{url}: InvalidURL: nonnumeric port: "
           f"'{FAKE_PASS}@webservices.iso-ne.com'")
    out = ns["scrub_attempt"](url, msg)
    assert FAKE_PASS not in out
    assert quote(FAKE_PASS) not in out
    assert "webservices.iso-ne.com" in out
