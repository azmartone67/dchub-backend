"""Guard: the deals crawler must not hold a DB connection across feed I/O.

WHAT THIS PINS
──────────────
`worker:deals` ran 712.9s on 2026-08-26 and ended
`error: SSL connection has been closed unexpectedly`. Measured from the
dchub-worker logs for that run, per-feed:

    08:00:18.605  www.datacenterdynamics.com    20 entries       0.5s
    08:00:22.250  www.datacenterknowledge.com   50 entries       0.6s
    08:00:25.400  www.prnewswire.com            20 entries       0.2s
    08:12:04.976  www.businesswire.com          Errno 104      696.6s   ← 97.7%
    08:12:08.037  feeds.reuters.com              0 entries       0.0s
    08:12:11.038  ERROR: SSL connection has been closed unexpectedly

★★★ THE DAMAGE WAS THE DISCARD, NOT THE SLOWNESS. psycopg2.connect() ran
BEFORE the feed loop and the only commit() ran after it, so the connection sat
idle for the whole 696.6s stall. Neon's proxy closes idle connections well
inside that, and the terminal commit() then threw away every row the run had
inserted. The 08:00Z run logged "✅ Deal: Nvidia" at 08:00:18.655 — a line that
prints only when cur.rowcount is truthy, i.e. a genuinely NEW row — and that
row is not in `deals`. Last row this crawler actually persisted:
2026-08-17 18:15:29Z, eight days earlier.

★★ WHY IT STALLED. The businesswire URL carried a literal `%s`, an
uninterpolated Python format placeholder shipped as a URL. feedparser.parse(url)
does its own network I/O with NO timeout and feedparser 6.x accepts no
`timeout=`, so there was nothing to bound it. socket.setdefaulttimeout() is not
an option either — it is process-global and this worker runs the brain, the
watchdog and the scheduler in sibling threads.

★ WHY A DEAD FEED LOOKED HEALTHY. feeds.reuters.com does not resolve, and it
cost 0.0s: feedparser.parse(url) swallows transport failures and returns an
empty feed, so a retired source reads as "0 entries" rather than as an error.

THE CONTRACT
────────────
  D1. No FEEDS entry contains a `%` format placeholder.
  D2. FEEDS names no host already established as retired for this path.
  D3. Feed bytes reach feedparser through _fetch_feed_bytes(), never as a raw
      URL handed to feedparser.parse().
  D4. _fetch_feed_bytes passes a (connect, read) TUPLE, connect strictly
      smaller — a dead host costs the handshake, not the read budget.
  D5. ★★★ psycopg2.connect() happens AFTER the feed loop has finished.
  D6. commit() happens per feed, inside the write loop — never once after
      everything, where one late failure discards all earlier feeds.
  D7. BEHAVIOURAL: against a host that accepts the connection and then never
      answers, _fetch_feed_bytes raises inside its read budget.

The scheduler is parsed with `ast`, never imported: importing crawler_scheduler
pulls in Flask, the DB and the network at module scope. D7 still exercises the
REAL production source by exec'ing the extracted function on its own.
"""
import ast
import io
import pathlib
import socket
import threading
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULER_SRC = REPO_ROOT / "crawler_scheduler.py"

RETIRED_HOSTS = ("businesswire.com", "feeds.reuters.com")


@pytest.fixture(scope="module")
def tree():
    return ast.parse(io.open(SCHEDULER_SRC, encoding="utf-8").read())


def _func(tree, name):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"{name}() is gone from {SCHEDULER_SRC.name}. This guard cannot pass "
        f"vacuously — if the function was renamed, retarget it here."
    )


def _calls(node, dotted):
    """Every Call to `dotted` ('a.b' or 'f') lexically inside `node`."""
    out = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if "." in dotted:
            obj, attr = dotted.split(".", 1)
            if (isinstance(f, ast.Attribute) and f.attr == attr
                    and isinstance(f.value, ast.Name) and f.value.id == obj):
                out.append(n)
        elif isinstance(f, ast.Name) and f.id == dotted:
            out.append(n)
    return out


def _feeds_list(fn):
    for n in ast.walk(fn):
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id == "FEEDS"):
            assert isinstance(n.value, ast.List), "FEEDS is no longer a list literal"
            return [e.value for e in n.value.elts if isinstance(e, ast.Constant)]
    raise AssertionError("FEEDS assignment not found inside _run_deals_crawler")


def _module_int(tree, name):
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name
                and isinstance(node.value, ast.Constant)):
            return node.value.value
    raise AssertionError(f"module-level {name} not found")


# ── Tripwire ────────────────────────────────────────────────────────────────

def test_the_structures_this_file_inspects_still_exist(tree):
    """★ Against a VACUOUS pass. Every assertion below walks one of these; if a
    refactor moves them, the checks would find nothing and silently pass."""
    fn = _func(tree, "_run_deals_crawler")
    assert _feeds_list(fn), "FEEDS is empty — every feed check below is vacuous"
    assert _calls(fn, "psycopg2.connect"), "no psycopg2.connect in _run_deals_crawler"
    assert _calls(fn, "conn.commit"), "no conn.commit in _run_deals_crawler"
    assert _calls(_func(tree, "_fetch_feed_bytes"), "requests.get"), \
        "no requests.get in _fetch_feed_bytes"


# ── D1 / D2: the feed list ──────────────────────────────────────────────────

def test_no_feed_url_carries_a_format_placeholder(tree):
    """D1: the businesswire entry was `.../rss/home/%srss=G7` — a `%s` that was
    never interpolated. Nothing in a literal feed URL should look like one."""
    for url in _feeds_list(_func(tree, "_run_deals_crawler")):
        assert "%" not in url, (
            f"{url!r} contains a '%' — an uninterpolated format placeholder "
            f"shipped as a URL is what cost this lane 696.6s per run"
        )


def test_no_retired_feed_hosts(tree):
    """D2: both were measured dead on 2026-08-26 and neither is recoverable by
    editing the URL — www.businesswire.com resets this path with OR without the
    `%s` repaired, and feed_health already carries Reuters with is_active=0
    after 6,520 failures."""
    for url in _feeds_list(_func(tree, "_run_deals_crawler")):
        for host in RETIRED_HOSTS:
            assert host not in url, f"{host} is retired but still in FEEDS: {url!r}"


# ── D3 / D4: the fetch is bounded ───────────────────────────────────────────

def test_feedparser_never_receives_a_raw_url(tree):
    """D3: feedparser.parse(url) does unbounded network I/O and swallows the
    failure. It must be handed BYTES that _fetch_feed_bytes already fetched."""
    parses = _calls(_func(tree, "_run_deals_crawler"), "feedparser.parse")
    assert parses, "no feedparser.parse call found — retarget this guard"
    for call in parses:
        assert call.args, "feedparser.parse() called with no argument"
        arg = call.args[0]
        assert isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) \
            and arg.func.id == "_fetch_feed_bytes", (
                "feedparser.parse must receive _fetch_feed_bytes(...), got "
                f"{ast.dump(arg)[:120]} — that is an unbounded fetch"
            )


def test_fetch_passes_a_connect_read_tuple_with_connect_smaller(tree):
    """D4: `requests` applies a SCALAR timeout= to connect AND read alike, so a
    dead host burns the whole read budget on the handshake."""
    gets = _calls(_func(tree, "_fetch_feed_bytes"), "requests.get")
    assert len(gets) == 1, f"expected exactly one requests.get, found {len(gets)}"
    kw = {k.arg: k.value for k in gets[0].keywords}
    assert "timeout" in kw, "requests.get has no timeout= at all"
    assert isinstance(kw["timeout"], ast.Tuple) and len(kw["timeout"].elts) == 2, \
        "timeout must be a (connect, read) tuple, not a scalar"
    names = [e.id for e in kw["timeout"].elts if isinstance(e, ast.Name)]
    assert len(names) == 2, "timeout tuple must be built from the named constants"
    connect, read = (_module_int(tree, n) for n in names)
    assert connect < read, f"connect ({connect}s) must be < read ({read}s)"
    assert 0 < connect <= 15, f"connect timeout {connect}s is not a handshake budget"


# ── D5 / D6: the connection is not open across the network ──────────────────

def test_db_connection_opens_after_the_feed_loop(tree):
    """D5: ★★★ the data-loss fix. The connection must not exist while feeds are
    being fetched."""
    fn = _func(tree, "_run_deals_crawler")
    loops = [n for n in ast.walk(fn)
             if isinstance(n, ast.For) and isinstance(n.iter, ast.Name)
             and n.iter.id == "FEEDS"]
    assert len(loops) == 1, f"expected one `for ... in FEEDS` loop, found {len(loops)}"
    connects = _calls(fn, "psycopg2.connect")
    assert len(connects) == 1, f"expected one psycopg2.connect, found {len(connects)}"
    assert connects[0].lineno > loops[0].end_lineno, (
        f"psycopg2.connect is at line {connects[0].lineno}, but the feed loop "
        f"runs to line {loops[0].end_lineno} — the connection is open across "
        f"feed I/O again, which is exactly what discarded every row"
    )


def test_every_commit_is_inside_a_loop(tree):
    """D6: one commit after everything means a single late failure discards the
    feeds that already succeeded."""
    fn = _func(tree, "_run_deals_crawler")
    commits = _calls(fn, "conn.commit")
    assert commits, "no conn.commit found — retarget this guard"
    in_loop = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.For):
            for c in _calls(n, "conn.commit"):
                in_loop.add(c.lineno)
    for c in commits:
        assert c.lineno in in_loop, (
            f"conn.commit() at line {c.lineno} is outside every loop — that is "
            f"the commit-once-at-the-end shape this guard exists to prevent"
        )


# ── D7: behavioural ─────────────────────────────────────────────────────────

@pytest.fixture
def blackhole():
    """A socket that ACCEPTS and then never answers. Exercises the READ budget,
    which is the one a connect timeout alone would not bound."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    held = []
    stop = threading.Event()

    def _hold():
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                held.append(conn)          # accepted, deliberately never written to
            except OSError:
                pass

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.getsockname()[1]}/rss"
    stop.set()
    t.join(timeout=2)
    for c in held:
        try: c.close()
        except OSError: pass
    srv.close()


def test_fetch_gives_up_on_a_host_that_never_answers(tree, blackhole):
    """D7: runs the REAL _fetch_feed_bytes source, extracted from
    crawler_scheduler.py and exec'd on its own so the module is never imported.
    Timeouts are injected as globals, so this proves the function READS the
    constants rather than that any particular value is correct."""
    src = ast.get_source_segment(
        io.open(SCHEDULER_SRC, encoding="utf-8").read(),
        _func(tree, "_fetch_feed_bytes"),
    )
    assert src and "requests.get" in src, "could not extract _fetch_feed_bytes source"

    ns = {"FEED_CONNECT_TIMEOUT_SECONDS": 2, "FEED_READ_TIMEOUT_SECONDS": 2}
    exec(compile(src, str(SCHEDULER_SRC), "exec"), ns)

    started = time.monotonic()
    with pytest.raises(Exception) as exc:
        ns["_fetch_feed_bytes"](blackhole)
    elapsed = time.monotonic() - started

    assert elapsed < 15, (
        f"_fetch_feed_bytes sat on a silent host for {elapsed:.1f}s with a 2s "
        f"read budget — the fetch is unbounded again"
    )
    assert "timeout" in type(exc.value).__name__.lower() or "timeout" in str(exc.value).lower(), \
        f"expected a timeout, got {type(exc.value).__name__}: {exc.value}"
