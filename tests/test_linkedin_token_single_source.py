"""★ THE GUARD THAT GENERALISES — one source for the LinkedIn token.

There are two LinkedIn credentials and they drift:

    LINKEDIN_ACCESS_TOKEN   a Railway env var, set by hand. Nothing refreshes
                            it. Goes stale or gets revoked silently.
    the Neon DB row         maintained by the refresh cron with a
                            refresh_token. Self-sustaining.

Any code path that reads the ENV var directly dies the moment they diverge —
and dies SILENTLY, because the DB-first paths keep working. Four incidents:

  2026-07-31  auto-publish drain 401'd (EXPIRED_ACCESS_TOKEN, post 105426)
              while the DB token had 13 days left and a refresh_token.
  2026-08-15  the image upload read env while the post read DB → every post
              published with its card silently stripped (image_attached FALSE
              on all 30 rows of /api/v1/linkedin-quad/status). PR #2718.
  2026-08-15  seven more live paths on the same revoked env var: the comment
              publisher, thread publisher, DM sender, spike responder,
              marketing_engine publish_now/repost_now, and
              linkedin_autopost.get_valid_token — which returned the env var
              FIRST and said so in its docstring.
  2026-08-15  intelligence_engine / dchub_daily_automation /
              infrastructure_discovery bound the token at IMPORT time, so even
              rotating the env var needed a restart.

The 2026-07-31 fix was right but its guard pinned a HAND-LISTED four
functions, so everything added afterwards re-grew the env read freely. This
guard instead asserts the INVARIANT over every live module: if you read
LINKEDIN_ACCESS_TOKEN from the environment, you are on the allowlist or the
build fails.

Pure AST, no imports of the modules under test, no DB, no network.
"""
import ast
import glob
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TOK = "LINKEDIN_ACCESS_TOKEN"

# Modules on the live request/scheduler path. routes/* is globbed so a NEW
# route file is covered the day it lands — that is the whole point.
LIVE_TOP_LEVEL = [
    "content_publisher.py", "linkedin_poster.py", "linkedin_autopost.py",
    "intelligence_engine.py", "dchub_daily_automation.py",
    "infrastructure_discovery.py",
]

# Modules that are allowed to read the env var directly, each with the reason.
# A function is keyed "<path>::<name>"; "<module>" means module-level code.
ALLOWED = {
    # The accessor itself, and its documented env fallback.
    "routes/li_token.py::li_access_token":      "the accessor",
    "routes/li_token.py::token_source_drift":   "reports env-vs-DB drift",
    "content_publisher.py::_li_access_token":   "DB-first helper's env fallback",
    "linkedin_poster.py::<module>":             "LINKEDIN_ACCESS_TOKEN_ENV constant",
    "linkedin_poster.py::post_to_linkedin":     "fallback after _get_valid_token()",
    # Diagnostics whose whole JOB is to report on the env var. These MUST keep
    # reading it directly or they stop being able to see the drift.
    "routes/linkedin_whoami_proxy.py::whoami":       "env-token health probe",
    "routes/marketing_engine.py::linkedin_whoami":   "env-token health probe",
    "routes/marketing_engine.py::linkedin_token_test": "env-token health probe",
    "routes/integrations_health.py::_check_linkedin":  "integration health probe",
    "routes/linkedin_token_reset.py::reset_from_env":  "seeds env -> DB by design",
    "routes/linkedin_token_reset.py::status":          "reports env_var_set",
    # Status-only module constants. NOT used for auth any more — the auth-path
    # guard below is what actually holds that line.
    "intelligence_engine.py::<module>":         "status constant only",
    "dchub_daily_automation.py::<module>":      "status constant only",
    "linkedin_autopost.py::<module>":           "legacy fallback in get_valid_token",
}

# Modules excluded from the scan because the MAIN app never imports them. The
# test below re-checks that claim every run — wiring one up fails the build
# and tells you to convert it first.
#
# services/ is deliberately not listed and not scanned: services/daily is a
# SEPARATE deployable (installed by install_daily.py) with its own env and its
# own credentials — LINKEDIN_AUTHOR_URN, a person URN, not the company page
# this repo's publishers use. Its poster.py is imported by its own app.py, so
# it is not dormant *within that service*; it is simply out of scope for the
# main app's token plumbing. The check below is what keeps that true.
DORMANT = ["news_publisher.py", "publish_routes.py", "post_announcement.py"]


def _is_environ(node):
    return isinstance(node, ast.Attribute) and node.attr == "environ"


def _reads_env_token(node) -> bool:
    """A REAL os.environ read of the token. Mentions in comments and
    docstrings do not count — an early version of this scan matched its own
    explanatory comments and reported false positives."""
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if (n.func.attr == "get" and _is_environ(n.func.value)) or n.func.attr == "getenv":
                if n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == TOK:
                    return True
        if isinstance(n, ast.Subscript) and _is_environ(n.value):
            if isinstance(n.slice, ast.Constant) and n.slice.value == TOK:
                return True
    return False


def _live_files():
    files = sorted(glob.glob(os.path.join(ROOT, "routes", "*.py")))
    files += [os.path.join(ROOT, f) for f in LIVE_TOP_LEVEL]
    return [f for f in files if os.path.exists(f)]


def _offenders():
    found = {}
    for path in _live_files():
        rel = os.path.relpath(path, ROOT)
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except Exception:
            continue
        in_fn = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _reads_env_token(node):
                    found[f"{rel}::{node.name}"] = True
                for sub in ast.walk(node):
                    in_fn.add(id(sub))
        for node in tree.body:
            if id(node) in in_fn:
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if _reads_env_token(node):
                found[f"{rel}::<module>"] = True
    return set(found)


def test_no_live_path_reads_the_env_token_outside_the_allowlist():
    """★ THE INVARIANT. Route the token through routes/li_token.li_access_token()
    (DB-first, env fallback). If a diagnostic genuinely needs the raw env var,
    add it to ALLOWED with a reason — deliberately, not by accident."""
    extra = sorted(_offenders() - set(ALLOWED))
    assert not extra, (
        "new direct read(s) of LINKEDIN_ACCESS_TOKEN on a live path:\n  "
        + "\n  ".join(extra)
        + "\n\nUse: from routes.li_token import li_access_token")


def test_the_allowlist_has_no_dead_entries():
    """An allowlist that outlives its offenders rots into permission nobody
    reviewed. Every entry must still correspond to a real read."""
    stale = sorted(set(ALLOWED) - _offenders())
    assert not stale, ("ALLOWED entries no longer read the env var (remove "
                       "them): " + ", ".join(stale))


def test_no_auth_header_is_built_from_the_stale_module_constant():
    """The status-only constants must stay status-only. This is the line that
    actually protects the three import-time-binding modules."""
    bad = []
    for name in ("intelligence_engine.py", "dchub_daily_automation.py",
                 "infrastructure_discovery.py", "linkedin_autopost.py"):
        p = os.path.join(ROOT, name)
        if not os.path.exists(p):
            continue
        for i, line in enumerate(open(p, encoding="utf-8"), 1):
            if "Bearer" in line and TOK in line:
                bad.append(f"{name}:{i}")
    assert not bad, ("auth header built from the stale import-time constant: "
                     + ", ".join(bad))


def test_the_standalone_daily_service_stays_out_of_the_main_app():
    """services/daily has its own credentials (LINKEDIN_AUTHOR_URN, a person
    URN). If the main app ever imports it, its token sourcing has to be
    reconciled with this one before that is safe."""
    importers = []
    for path in glob.glob(os.path.join(ROOT, "**", "*.py"), recursive=True):
        rel = os.path.relpath(path, ROOT)
        if rel.startswith(("tests/", "docs/", "services/")) or "site-packages" in rel:
            continue
        try:
            src = open(path, encoding="utf-8").read()
        except Exception:
            continue
        if "services.daily" in src or "from services import" in src:
            importers.append(rel)
    assert not importers, (
        "the main app now imports services/daily "
        f"({importers}) — reconcile its LinkedIn token sourcing first")


@pytest.mark.parametrize("mod", DORMANT)
def test_dormant_modules_are_still_dormant(mod):
    """These are skipped by the scan ONLY because nothing imports them. If one
    gets wired up, convert it to li_access_token() before this passes again."""
    stem = os.path.basename(mod)[:-3]
    importers = []
    for path in glob.glob(os.path.join(ROOT, "**", "*.py"), recursive=True):
        rel = os.path.relpath(path, ROOT)
        if rel.startswith(("tests/", "docs/", "services/")) or "site-packages" in rel:
            continue
        if rel == mod or os.path.basename(rel) == os.path.basename(mod):
            continue
        try:
            src = open(path, encoding="utf-8").read()
        except Exception:
            continue
        if f"import {stem}" in src or f"from {stem} " in src:
            importers.append(rel)
    assert not importers, (
        f"{mod} is no longer dormant (imported by {importers}) — it reads "
        f"{TOK} directly and must be converted to li_access_token() and added "
        "to the scan")


# ── behaviour of the accessor ────────────────────────────────────────────────
def test_accessor_prefers_db_and_falls_back_to_env(monkeypatch):
    import types
    from routes import li_token as lt

    fake = types.ModuleType("content_publisher")
    fake._li_access_token = lambda: "db-token"
    monkeypatch.setitem(sys.modules, "content_publisher", fake)
    monkeypatch.setenv(TOK, "env-token")
    assert lt.li_access_token() == "db-token"

    fake._li_access_token = lambda: ""          # DB empty -> env
    assert lt.li_access_token() == "env-token"

    def boom():
        raise RuntimeError("db down")
    fake._li_access_token = boom                # fail-OPEN, never darker
    assert lt.li_access_token() == "env-token"

    monkeypatch.delenv(TOK, raising=False)
    assert lt.li_access_token() == ""
