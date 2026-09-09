"""A session-scoped readonly may never be issued on a POOLED connection.

MEASURED 2026-09-09, deployment 025707ba, by the probe from #4283/#4287/#4291:

    SESSION table=discovered_platforms tx_read_only=off default_read_only=off pid=16711
    SESSION table=discovered_platforms tx_read_only=ON  default_read_only=ON  pid=16714
    SESSION table=agent_requests       tx_read_only=off default_read_only=off pid=16728
    SESSION table=discovered_platforms tx_read_only=ON  default_read_only=ON  pid=16714

One backend read-only every time it surfaced, its neighbours fine, dsn_options=''
— a runtime SET stuck to a single backend. `conn.set_session(readonly=True)`
issues SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY, and on Neon's
`-pooler` endpoint (PgBouncer, TRANSACTION pooling) that survives the client
disconnect on the shared SERVER backend. Every later client handed it fails
25006 — swallowed, so nothing pages — and it outlives our redeploys because the
state is PgBouncer's, not ours.

★ THIS GUARD MUST BE ABLE TO FAIL. It is an ABSENCE assertion over a repo scan,
  and "no occurrences" reads identical to "the scanner is broken". So it carries
  a FLOOR (the scan must still find the call sites it is policing) and a
  must-fail CONTROL (a known-bad snippet must be flagged).
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "tests"}

# Every site that existed when this landed. The floor is deliberately the real
# number: if the scan finds fewer, it stopped seeing code it is meant to police.
KNOWN_SITE_FLOOR = 9


def scan_text(text):
    """(sites, violations) for one file's source, read as AST — never as text.

    A site is a real `x.set_session(readonly=True)` CALL. It is a VIOLATION
    unless the nearest psycopg2.connect above it wraps its DSN in direct_dsn().

    ★ AST, not substring, and the first version of this file proves why: a
      plain text scan flagged routes/_session_dsn.py — the module that FIXES
      this — because its docstring quotes the pattern it exists to describe. A
      guard that cannot tell code from prose punishes the explanation.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return 0, 0   # the AST gate in pre-merge.yml owns unparseable files

    sites, connects = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "set_session":
            if any(k.arg == "readonly" and getattr(k.value, "value", None) is True
                   for k in node.keywords):
                sites.append(node.lineno)
        if (isinstance(f, ast.Attribute) and f.attr == "connect"
                and isinstance(f.value, ast.Name) and f.value.id == "psycopg2"):
            first = node.args[0] if node.args else None
            wrapped = (isinstance(first, ast.Call)
                       and isinstance(first.func, ast.Name)
                       and first.func.id == "direct_dsn")
            connects.append((node.lineno, wrapped))

    violations = 0
    for line in sites:
        prior = [c for c in connects if c[0] <= line]
        if not prior or not max(prior, key=lambda c: c[0])[1]:
            violations += 1
    return len(sites), violations


def _repo_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def _scan_repo():
    sites = 0
    offenders = []
    for path in _repo_files():
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        s, v = scan_text(text)
        sites += s
        if v:
            offenders.append(os.path.relpath(path, ROOT))
    return sites, offenders


# ── the control: the scanner must catch the shape it is looking for ─────────
_BAD = '''
def f():
    dsn = os.environ.get("DATABASE_URL")
    c = psycopg2.connect(dsn, connect_timeout=8)
    c.set_session(readonly=True, autocommit=True)
'''

_GOOD = '''
def f():
    dsn = os.environ.get("DATABASE_URL")
    from routes._session_dsn import direct_dsn
    c = psycopg2.connect(direct_dsn(dsn), connect_timeout=8)
    c.set_session(readonly=True, autocommit=True)
'''


def test_control_the_scanner_flags_a_pooled_readonly_session():
    sites, violations = scan_text(_BAD)
    assert (sites, violations) == (1, 1), \
        "the scanner cannot see the defect it exists to catch"


def test_control_the_scanner_accepts_the_fixed_shape():
    assert scan_text(_GOOD) == (1, 0), "the scanner rejects the correct form"


# ── the floor: a scan that finds nothing is not a passing scan ──────────────
def test_the_scan_still_reaches_the_call_sites_it_polices():
    sites, _ = _scan_repo()
    assert sites >= KNOWN_SITE_FLOOR, (
        f"found only {sites} set_session(readonly=True) sites, expected at least "
        f"{KNOWN_SITE_FLOOR} — the scan stopped seeing code, so a clean result "
        f"below means nothing")


# ── the assertion itself ────────────────────────────────────────────────────
def test_no_readonly_session_on_a_pooled_connection():
    _, offenders = _scan_repo()
    assert offenders == [], (
        "session-scoped readonly on a pooled DSN — it poisons the shared "
        "PgBouncer backend for every later client: " + ", ".join(offenders))


# ── direct_dsn itself ───────────────────────────────────────────────────────
def test_direct_dsn_rewrites_only_the_host():
    from routes._session_dsn import direct_dsn
    # a password that CONTAINS the token: a naive str.replace corrupts it
    got = direct_dsn("postgresql://u:p-poolerX@ep-a-pooler.c-2.aws.neon.tech/db")
    assert got == "postgresql://u:p-poolerX@ep-a.c-2.aws.neon.tech/db"


def test_direct_dsn_leaves_everything_else_alone():
    from routes._session_dsn import direct_dsn
    for u in ("postgresql://u:p@ep-a.c-2.aws.neon.tech/db",
              "postgresql://localhost/db", "", None):
        assert direct_dsn(u) == u


def test_direct_dsn_keeps_the_port():
    from routes._session_dsn import direct_dsn
    assert direct_dsn("postgresql://ep-a-pooler.c-2.aws.neon.tech:5432/db") == \
        "postgresql://ep-a.c-2.aws.neon.tech:5432/db"
