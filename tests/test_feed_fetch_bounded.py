"""Guard: no feed fetch in this repo may be unbounded.

WHAT THIS PINS
──────────────
`feedparser.parse(url)` does its own network I/O and feedparser 6.0.12 accepts
no `timeout=`. There is nothing to bound it with. Measured in production
2026-08-26 on `worker:deals` (#3208): one dead feed URL consumed **696.6s of a
712.9s run**, and a catch-up run on unchanged code the same morning burned
**1771.6s** on the same feed before the scheduler's 1800s hard timeout killed
the thread. The cost is not fixed — it is unbounded.

#3208 fixed one of the twelve call sites, inside `crawler_scheduler.py`. This
guard covers the whole repo, so the next one cannot land unbounded.

★ WHY A DEAD FEED LOOKS HEALTHY, AND WHY THE FETCH RAISES. `feedparser.parse`
swallows transport failures and returns an empty feed. `feeds.reuters.com`,
which does not resolve at all, therefore read as "0 entries" for months instead
of as a dead source. Measured 2026-08-26 across the 77 feed URLs configured in
this repo: **33 of them produce zero entries**, and the old path reported every
one as a benign empty feed. `util.feed_fetch` raises instead.

★★ WHY THE `(connect, read)` TUPLE. `requests` applies a SCALAR `timeout=N` to
connect AND read alike, so a dead host burns the entire read budget just
failing to open a socket.

★★★ WHY A WALL-CLOCK DEADLINE ON TOP. `requests`' read timeout is BETWEEN
BYTES, not total. A host dripping one byte every 2s under a 3s read budget
never times out — measured, `test_drip_host_...` below. Without
FEED_TOTAL_TIMEOUT_SECONDS this module would still be unbounded.

★ NOT `socket.setdefaulttimeout()`. It is process-global and the worker runs
the brain, the watchdog and the scheduler in sibling threads.

THE CONTRACT
────────────
  F1. Repo-wide census: no `feedparser.parse()` receives a bare URL. The only
      exempt file is util/feed_fetch.py, the one place allowed to hand over
      bytes it has already fetched under budget.
  F2. The census is NON-VACUOUS — a floor on files scanned, and every call site
      converted by this PR still present by name. A scan that finds nothing is
      byte-identical to a scan that finds nothing wrong.
  F3. util.feed_fetch passes a (connect, read) TUPLE to requests.get, connect
      strictly smaller.
  F4. Every budget constant is CLAMPED. The env overrides exist to mitigate an
      incident without a deploy; they must not be able to re-open the hole.
  F5. No NEW socket.setdefaulttimeout() call sites. Two pre-date this guard and
      are pinned by name, so the allowlist is counted rather than trusted.
  F6. parse_feed passes response_headers carrying BOTH content-type AND
      content-location. Measured: content-location alone makes feedparser apply
      the HTTP text/* default charset of iso-8859-1 and mojibake every UTF-8
      feed in the repo.
  F7. BEHAVIOURAL: a host that accepts and never answers raises inside the read
      budget.
  F8. BEHAVIOURAL: a host that drips forever raises inside the TOTAL budget —
      the case the read timeout alone does not bound.
  F9. BEHAVIOURAL: a non-2xx raises rather than returning an empty feed.
  F11. BEHAVIOURAL: a slow REDIRECT CHAIN gives up inside the total budget.
      `requests` re-applies timeout= to every hop and follows 30 by default, so
      the tuple alone bounds one hop, not the fetch.
  F12. The body and the redirect chain share ONE deadline object. Giving each a
      fresh FEED_TOTAL_TIMEOUT_SECONDS makes the "total" a per-phase budget.
  F10. BEHAVIOURAL FIDELITY: parse_feed(url) and feedparser.parse(url) agree on
      encoding, entry count, titles and RESOLVED relative links, through a
      redirect. This is the property the response_headers exist to preserve.

Modules are parsed with `ast`, never imported: importing news_engine or
crawler_scheduler pulls Flask, the DB and the network at module scope.
util.feed_fetch itself IS imported — it is stdlib-only at module scope by
design, and F3/F4/F7-F10 exercise the real thing.
"""
import ast
import io
import os
import pathlib
import socket
import threading
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "util" / "feed_fetch.py"

_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "tests",
              "site-packages", "build", "dist", ".claude", ".pytest_cache",
              # nested worktree copies of this repo; not real source
              "wt-base", "base-be-tmp"}

# ★ The only file allowed to call feedparser.parse() on something other than a
#   bounded fetch — it IS the bounded fetch. Counted, not trusted: widening
#   this set to silence a red build changes a number a reviewer can see.
_EXEMPT = frozenset({"util/feed_fetch.py"})

# Function names whose return value is known-bounded feed bytes.
# `_fetch_feed_bytes` is crawler_scheduler's local helper from #3208.
_BOUNDED_FETCHERS = frozenset({"_fetch_feed_bytes", "fetch_feed_bytes"})

# socket.setdefaulttimeout is process-global. These two pre-date this guard;
# neither is a feed fetch. Pinned so a NEW one is caught, and counted so
# quietly appending to the list is visible.
# ★ observability_routes sets 1.5s inside a ThreadPoolExecutor worker and never
#   restores it — a live leak onto every socket in that process, flagged but
#   deliberately not fixed here.
_KNOWN_GLOBAL_SOCKET_TIMEOUT_FILES = frozenset({
    "routes/email_validation.py",
    "routes/observability_routes.py",
})

# Every call site converted off the unbounded path. Subset check, not equality:
# a NEW bounded feed reader is fine, losing one of these is not.
_CONVERTED_SITES = frozenset({
    "ai_agent.py", "deal_ingestion_scheduler.py", "deal_scraper.py",
    "discovery_engine_v3.py", "discovery_nexus.py", "fix_news_empty.py",
    "news_aggregator.py", "news_engine.py", "routes/jobs_routes.py",
    "static/news_html_fix.py", "transactions_news_api.py",
})

# A hop COUNT, not a time budget, so it is not routed through _budget(). Named
# rather than silently skipped — see test_every_budget_constant_is_clamped.
_NOT_ENV_TUNABLE = frozenset({"FEED_MAX_REDIRECTS"})

# Measured 1,387 on 2026-08-26. A collapse detector, not a pin.
_MIN_FILES_SCANNED = 1100


def _sources():
    """Every non-test .py in the repo, as (relpath, tree)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                tree = ast.parse(io.open(path, encoding="utf-8").read())
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            out.append((os.path.relpath(path, REPO_ROOT), tree))
    return out


@pytest.fixture(scope="module")
def sources():
    return _sources()


def _attr_call(node, obj, attr):
    f = node.func
    return (isinstance(f, ast.Attribute) and f.attr == attr
            and isinstance(f.value, ast.Name) and f.value.id == obj)


def _any_call_named(node, attr):
    """Any call to `attr`, whatever it is bound to.

    ★ Mutation-found. Keying on `socket.setdefaulttimeout` matched the module
    name literally, so `import socket as _s; _s.setdefaulttimeout(5)` survived
    the guard untouched — and so would `from socket import setdefaulttimeout`.
    The function is what is dangerous, not the name it was reached through.
    """
    f = node.func
    return ((isinstance(f, ast.Attribute) and f.attr == attr)
            or (isinstance(f, ast.Name) and f.id == attr))


def _census(sources):
    """(unbounded, bounded_local, via_helper, global_timeout_files)."""
    unbounded, bounded, helper, gtimeout = [], [], [], set()
    for rel, tree in sources:
        rel = rel.replace(os.sep, "/")
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            if _any_call_named(n, "setdefaulttimeout"):
                gtimeout.add(rel)
            if _attr_call(n, "feedparser", "parse"):
                if rel in _EXEMPT:
                    continue
                arg = n.args[0] if n.args else None
                ok = (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                      and arg.func.id in _BOUNDED_FETCHERS)
                (bounded if ok else unbounded).append((rel, n.lineno))
            if _attr_call(n, "feed_fetch", "parse_feed") \
                    or _attr_call(n, "feed_fetch", "fetch_feed_bytes") \
                    or _attr_call(n, "feed_fetch", "fetch_feed_response"):
                helper.append((rel, n.lineno))
    return unbounded, bounded, helper, gtimeout


# ── F2: tripwire ────────────────────────────────────────────────────────────

def test_the_census_actually_sees_the_repo(sources):
    """★ Against a VACUOUS pass. Every check below walks this scan; if a skip
    list or a path assumption collapses it, they would all find nothing and
    silently agree."""
    assert len(sources) >= _MIN_FILES_SCANNED, (
        f"the census scanned only {len(sources)} python files (floor "
        f"{_MIN_FILES_SCANNED}) — it is no longer looking at this repo, and "
        f"every assertion in this file is now vacuous"
    )
    assert HELPER.is_file(), f"{HELPER} is gone — retarget this guard"
    _, _, helper, _ = _census(sources)
    assert helper, "no feed_fetch call sites found at all — retarget this guard"


def test_every_converted_call_site_is_still_bounded(sources):
    """F2: the thirteen sites this guard was written for. A subset check — a new
    bounded reader is fine, losing one of these is the regression."""
    _, _, helper, _ = _census(sources)
    seen = {rel for rel, _ in helper}
    missing = _CONVERTED_SITES - seen
    assert not missing, (
        f"{sorted(missing)} no longer call util.feed_fetch. Either the feed "
        f"read was deleted (update _CONVERTED_SITES) or it regressed to an "
        f"unbounded fetch."
    )


# ── F1: the census itself ───────────────────────────────────────────────────

def test_no_feedparser_parse_receives_a_bare_url(sources):
    """F1: `feedparser.parse(url)` is the unbounded call. It must be handed
    bytes some bounded fetcher already retrieved."""
    unbounded, _, _, _ = _census(sources)
    assert not unbounded, (
        "unbounded feedparser.parse() call sites:\n" +
        "\n".join(f"  {r}:{l}" for r, l in sorted(unbounded)) +
        "\nfeedparser 6.x accepts no timeout=. Use util.feed_fetch.parse_feed()."
    )


def test_the_exemption_list_has_not_been_widened(sources):
    """F1: the escape hatch is one file. Counted, because the cheapest way to
    make this file green is to add a name to _EXEMPT."""
    assert _EXEMPT == frozenset({"util/feed_fetch.py"}), (
        f"_EXEMPT was widened to {sorted(_EXEMPT)} — only the bounded fetcher "
        f"itself may hand raw bytes to feedparser"
    )
    assert len(_BOUNDED_FETCHERS) == 2, (
        f"_BOUNDED_FETCHERS grew to {sorted(_BOUNDED_FETCHERS)}; every name "
        f"here is asserted to be bounded WITHOUT being checked"
    )
    for rel in _EXEMPT:
        assert (REPO_ROOT / rel).is_file(), f"exempt file {rel} does not exist"


# ── F5: the process-global escape hatch ─────────────────────────────────────

def test_no_new_process_global_socket_timeout(sources):
    """F5: socket.setdefaulttimeout() bounds a feed fetch by bounding every
    other socket in the process, including the brain's and the watchdog's."""
    _, _, _, gtimeout = _census(sources)
    new = gtimeout - _KNOWN_GLOBAL_SOCKET_TIMEOUT_FILES
    assert not new, (
        f"{sorted(new)} call socket.setdefaulttimeout(). It is PROCESS-GLOBAL "
        f"and the worker runs the brain, the watchdog and the scheduler in "
        f"sibling threads. Pass an explicit per-call timeout instead."
    )
    assert len(_KNOWN_GLOBAL_SOCKET_TIMEOUT_FILES) == 2, (
        "the pre-existing allowlist grew; that is the check being edited "
        "rather than the code"
    )


# ── F3 / F4 / F6: the helper's shape ────────────────────────────────────────

@pytest.fixture(scope="module")
def helper_tree():
    return ast.parse(io.open(HELPER, encoding="utf-8").read())


def _func(tree, name):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() is gone from util/feed_fetch.py — retarget")


def test_requests_get_uses_a_connect_read_tuple(helper_tree):
    """F3: a scalar timeout= covers connect AND read alike, so a dead host
    burns the whole read budget on the handshake."""
    fn = _func(helper_tree, "fetch_feed_response")
    gets = [n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and (_attr_call(n, "requests", "get") or _attr_call(n, "session", "get"))]
    assert len(gets) == 1, f"expected one GET call, found {len(gets)}"
    kw = {k.arg: k.value for k in gets[0].keywords}
    assert "timeout" in kw, "requests.get has no timeout= at all"
    assert isinstance(kw["timeout"], ast.Tuple) and len(kw["timeout"].elts) == 2, \
        "timeout must be a (connect, read) tuple, not a scalar"
    names = [e.id for e in kw["timeout"].elts if isinstance(e, ast.Name)]
    assert names == ["FEED_CONNECT_TIMEOUT_SECONDS", "FEED_READ_TIMEOUT_SECONDS"], \
        f"timeout tuple must be (connect, read) from the named constants, got {names}"
    assert kw.get("stream") is not None and getattr(kw["stream"], "value", False) is True, \
        "stream=True is required — the wall-clock deadline needs iter_content"
    assert "hooks" in kw, (
        "no response hook — nothing checks the deadline BETWEEN redirect hops, "
        "so a slow chain multiplies the per-request budget"
    )

    import util.feed_fetch as ff
    assert ff.FEED_CONNECT_TIMEOUT_SECONDS < ff.FEED_READ_TIMEOUT_SECONDS, (
        f"connect ({ff.FEED_CONNECT_TIMEOUT_SECONDS}s) must be < read "
        f"({ff.FEED_READ_TIMEOUT_SECONDS}s)"
    )
    assert 0 < ff.FEED_CONNECT_TIMEOUT_SECONDS <= 15, "connect is not a handshake budget"


def test_the_redirect_chain_is_capped(helper_tree):
    """F11, structural half: requests follows 30 redirects by default and gives
    each one a fresh timeout budget."""
    import util.feed_fetch as ff
    fn = _func(helper_tree, "fetch_feed_response")
    assigns = [n for n in ast.walk(fn)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Attribute) and t.attr == "max_redirects"
                       for t in n.targets)]
    assert assigns, (
        "nothing sets session.max_redirects — the chain runs to requests' "
        "default of 30, each hop with its own (connect, read) budget"
    )
    assert 0 < ff.FEED_MAX_REDIRECTS <= 10, \
        f"FEED_MAX_REDIRECTS is {ff.FEED_MAX_REDIRECTS}; no real feed needs that"


def test_the_body_and_the_redirect_chain_share_one_deadline(helper_tree):
    """F12. ★ Mutation-found — this file passed 15/15 without it.

    `_read_body(resp, time.monotonic() + FEED_TOTAL_TIMEOUT_SECONDS, ...)` looks
    identical in review to `_read_body(resp, deadline, ...)` and is not: the
    redirect chain gets the full budget and then the body gets another one, so
    the documented 60s total silently becomes 120s. Every behavioural test still
    passes, because each phase is bounded on its own. The invariant is that both
    phases read the SAME name.
    """
    fn = _func(helper_tree, "fetch_feed_response")

    guards = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Name) and n.func.id == "_deadline_guard"]
    assert len(guards) == 1, f"expected one _deadline_guard(), found {len(guards)}"
    assert guards[0].args and isinstance(guards[0].args[0], ast.Name), \
        "_deadline_guard must be handed a named deadline, not an expression"
    deadline_name = guards[0].args[0].id

    reads = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_read_body"]
    assert len(reads) == 1, f"expected one _read_body(), found {len(reads)}"
    assert len(reads[0].args) >= 2, "_read_body called without a deadline"
    arg = reads[0].args[1]
    assert isinstance(arg, ast.Name) and arg.id == deadline_name, (
        f"_read_body gets {ast.dump(arg)[:70]} but the hop guard got "
        f"`{deadline_name}`. A second deadline is a second budget — the total "
        f"stops being a total."
    )


def test_every_budget_constant_is_clamped(helper_tree):
    """F4: an env override that can exceed the ceiling re-opens the hole."""
    import util.feed_fetch as ff

    wired = {}
    for node in helper_tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.startswith("FEED_")
                and node.targets[0].id not in _NOT_ENV_TUNABLE):
            call = node.value
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) \
                    and call.func.id == "int" and call.args:
                call = call.args[0]
            assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name) \
                and call.func.id == "_budget", (
                    f"{node.targets[0].id} is not assigned from _budget(...) — "
                    f"it is not clamped"
                )
            assert len(call.args) == 3, (
                f"{node.targets[0].id} = _budget(...) without a ceiling argument"
            )
            wired[node.targets[0].id] = call
    assert set(wired) == {"FEED_CONNECT_TIMEOUT_SECONDS", "FEED_READ_TIMEOUT_SECONDS",
                          "FEED_TOTAL_TIMEOUT_SECONDS", "FEED_MAX_BYTES"}, \
        f"budget constants changed: {sorted(wired)}"
    assert _NOT_ENV_TUNABLE == frozenset({"FEED_MAX_REDIRECTS"}), (
        f"_NOT_ENV_TUNABLE was widened to {sorted(_NOT_ENV_TUNABLE)} — the "
        f"cheapest way to dodge the clamp check is to add a name to it"
    )

    # ★ Mutation-found: proving _budget clamps to the ceiling it is HANDED says
    #   nothing about the ceiling. `_MAX_READ = 1_000_000.0` re-opens the hole
    #   with every other assertion in this file still green. A ceiling that can
    #   be raised without limit is not a ceiling.
    for name, cap in (("_MAX_CONNECT", 60), ("_MAX_READ", 300),
                      ("_MAX_TOTAL", 600), ("_MAX_MAX_BYTES", 256 * 1024 * 1024)):
        actual = getattr(ff, name)
        assert 0 < actual <= cap, (
            f"{name} is {actual}, above the {cap} this guard allows. The env "
            f"override clamps to it, so this value IS the worst case a single "
            f"dead feed can cost."
        )
    assert ff._MAX_CONNECT < ff._MAX_READ <= ff._MAX_TOTAL, \
        "ceilings must stay ordered connect < read <= total"

    # ...and the clamp actually clamps.
    assert ff._budget("DCHUB_NO_SUCH_VAR", 7.0, 99.0) == 7.0, "default not honoured"
    os.environ["DCHUB_FEED_CLAMP_PROBE"] = "999999"
    try:
        assert ff._budget("DCHUB_FEED_CLAMP_PROBE", 5.0, 30.0) == 30.0, \
            "an over-ceiling env value was NOT clamped — the hole is re-openable"
        os.environ["DCHUB_FEED_CLAMP_PROBE"] = "-1"
        assert ff._budget("DCHUB_FEED_CLAMP_PROBE", 5.0, 30.0) == 5.0, \
            "a non-positive env value was accepted"
        os.environ["DCHUB_FEED_CLAMP_PROBE"] = "not-a-number"
        assert ff._budget("DCHUB_FEED_CLAMP_PROBE", 5.0, 30.0) == 5.0, \
            "a junk env value was accepted"
    finally:
        os.environ.pop("DCHUB_FEED_CLAMP_PROBE", None)


def test_parse_feed_passes_both_response_headers(helper_tree):
    """F6: content-location alone makes feedparser fall back to the HTTP
    text/* default charset of iso-8859-1 and mojibake every UTF-8 feed.
    The two keys are not independent — see test_parse_feed_matches_... ."""
    fn = _func(helper_tree, "parse_feed")
    parses = [n for n in ast.walk(fn)
              if isinstance(n, ast.Call) and _attr_call(n, "feedparser", "parse")]
    assert len(parses) == 1, f"expected one feedparser.parse, found {len(parses)}"
    kw = {k.arg: k.value for k in parses[0].keywords}
    assert "response_headers" in kw, "parse_feed drops response_headers entirely"
    hdrs = kw["response_headers"]
    assert isinstance(hdrs, ast.Dict), "response_headers must be a dict literal"
    keys = {k.value for k in hdrs.keys if isinstance(k, ast.Constant)}
    assert keys == {"content-location", "content-type"}, (
        f"response_headers carries {sorted(keys)}; it must carry BOTH "
        f"content-location (relative-link base) and content-type (charset). "
        f"Passing content-location alone mis-decodes UTF-8 feeds."
    )


# ── F7 / F8 / F9 / F10: behavioural ─────────────────────────────────────────

def _serve(handler):
    """A raw socket server running `handler(conn)` per connection."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    stop = threading.Event()
    conns = []

    def _accept():
        srv.settimeout(0.3)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            conns.append(conn)
            threading.Thread(target=handler, args=(conn, stop), daemon=True).start()

    threading.Thread(target=_accept, daemon=True).start()
    return f"http://127.0.0.1:{srv.getsockname()[1]}/rss", stop, srv, conns


def _teardown(stop, srv, conns):
    stop.set()
    for c in conns:
        try:
            c.close()
        except OSError:
            pass
    srv.close()


@pytest.fixture
def blackhole():
    """Accepts the connection and never answers. Exercises the READ budget —
    the one a connect timeout would not bound."""
    url, stop, srv, conns = _serve(lambda c, s: s.wait(120))
    yield url
    _teardown(stop, srv, conns)


@pytest.fixture
def drip():
    """Answers, then sends one byte at a time forever, faster than the read
    budget. ★ requests' read timeout is BETWEEN BYTES — it never fires here."""
    def handler(c, stop):
        try:
            c.recv(65536)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/rss+xml\r\n"
                      b"Transfer-Encoding: chunked\r\n\r\n")
            while not stop.is_set():
                c.sendall(b"1\r\nx\r\n")
                stop.wait(0.4)
        except OSError:
            pass
    url, stop, srv, conns = _serve(handler)
    yield url
    _teardown(stop, srv, conns)


@pytest.fixture
def notfound():
    def handler(c, stop):
        try:
            c.recv(65536)
            c.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass
    url, stop, srv, conns = _serve(handler)
    yield url
    _teardown(stop, srv, conns)


def _within(fn, limit):
    """Run `fn()` in a daemon thread and REFUSE to outlive `limit` seconds.

    ★ Without this, the mutation that matters most — deleting the deadline in
    _read_body — makes the drip test HANG rather than fail. A hung guard is not
    a red guard: it reports as a CI job timeout, which reads as flake, and it
    cost this harness a 10-minute run before it was added. Returns the raised
    exception, or raises if nothing was raised.
    """
    box = {}

    def go():
        try:
            box["value"] = fn()
        except BaseException as e:          # noqa: BLE001 — reporting, not handling
            box["exc"] = e

    t = threading.Thread(target=go, daemon=True)
    t0 = time.monotonic()
    t.start()
    t.join(limit)
    if t.is_alive():
        raise AssertionError(
            f"the fetch was STILL RUNNING after {limit}s — it is unbounded. "
            f"That is the defect this whole file exists to prevent."
        )
    if "exc" not in box:
        raise AssertionError(
            f"the fetch returned {box.get('value')!r} instead of raising, after "
            f"{time.monotonic() - t0:.1f}s"
        )
    return box["exc"], time.monotonic() - t0


def test_host_that_never_answers_gives_up_inside_the_read_budget(monkeypatch, blackhole):
    """F7. Budgets are injected, so this proves the code READS the constants
    rather than that any particular value is right."""
    import util.feed_fetch as ff
    monkeypatch.setattr(ff, "FEED_CONNECT_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(ff, "FEED_READ_TIMEOUT_SECONDS", 3)

    exc, elapsed = _within(lambda: ff.fetch_feed_bytes(blackhole), 12)

    assert elapsed < 12, (
        f"sat on a silent host for {elapsed:.1f}s with a 3s read budget — the "
        f"fetch is unbounded again"
    )
    assert "timeout" in type(exc).__name__.lower(), \
        f"expected a timeout, got {type(exc).__name__}: {exc}"


def test_drip_host_gives_up_inside_the_total_budget(monkeypatch, drip):
    """F8. ★★★ The case the (connect, read) tuple alone does NOT bound: bytes
    keep arriving, so the between-bytes read timeout never fires. Without
    FEED_TOTAL_TIMEOUT_SECONDS this host holds the caller forever."""
    import util.feed_fetch as ff
    monkeypatch.setattr(ff, "FEED_CONNECT_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(ff, "FEED_READ_TIMEOUT_SECONDS", 5)   # > the 0.4s drip
    monkeypatch.setattr(ff, "FEED_TOTAL_TIMEOUT_SECONDS", 3)

    exc, elapsed = _within(lambda: ff.fetch_feed_bytes(drip), 12)

    assert isinstance(exc, ff.FeedReadTimeout), \
        f"expected FeedReadTimeout, got {type(exc).__name__}: {exc}"
    assert elapsed < 12, f"drip host held the fetch for {elapsed:.1f}s"


def test_oversized_body_is_refused(monkeypatch, drip):
    """F8, the other half of the deadline: a body that never ends."""
    import util.feed_fetch as ff
    monkeypatch.setattr(ff, "FEED_CONNECT_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(ff, "FEED_READ_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(ff, "FEED_TOTAL_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(ff, "FEED_MAX_BYTES", 2)

    exc, _ = _within(lambda: ff.fetch_feed_bytes(drip), 12)
    assert isinstance(exc, ff.FeedTooLarge), \
        f"expected FeedTooLarge, got {type(exc).__name__}: {exc}"


def test_non_2xx_raises_instead_of_reading_as_an_empty_feed(notfound):
    """F9. ★ This is the whole reason the fetch is loud: feedparser.parse(url)
    turns a 404 into "0 entries", which is how a retired source reads as a
    quiet one. Measured 2026-08-26: 33 of the 77 feed URLs configured in this
    repo return zero entries, 23 of them on an outright HTTP 404."""
    import util.feed_fetch as ff
    exc, _ = _within(lambda: ff.fetch_feed_bytes(notfound), 12)
    assert "404" in str(exc), f"expected the 404 to surface, got {exc!r}"


def test_slow_redirect_chain_gives_up_inside_the_total_budget(monkeypatch):
    """F11, behavioural. ★ Each hop is a fresh request and gets a fresh
    `timeout=`, so twelve slow hops cost twelve budgets. Only the shared
    deadline stops that."""
    import http.server
    import socketserver

    import util.feed_fetch as ff

    hops = 12

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            n = int(self.path.strip("/").split("h")[-1] or 0)
            time.sleep(0.4)                      # every hop costs
            if n < hops:
                self.send_response(302)
                self.send_header("Location", f"/h{n + 1}")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    srv.allow_reuse_address = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]

    monkeypatch.setattr(ff, "FEED_CONNECT_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(ff, "FEED_READ_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(ff, "FEED_TOTAL_TIMEOUT_SECONDS", 1)
    try:
        exc, elapsed = _within(lambda: ff.fetch_feed_bytes(base + "/h0"), 15)
        assert elapsed < 15, f"the redirect chain ran {elapsed:.1f}s"
        # Either bound is a pass; both exist because they fail differently — the
        # deadline stops a SLOW chain, the hop cap stops a FAST infinite one.
        assert isinstance(exc, ff.FeedReadTimeout) or "redirect" in str(exc).lower(), (
            f"expected the chain to be cut short, got {type(exc).__name__}: {exc}"
        )
    finally:
        srv.shutdown()
        srv.server_close()


def test_parse_feed_matches_feedparser_parse_url_exactly():
    """F10. ★★★ The fidelity property the response_headers exist to preserve.

    Handing feedparser BYTES silently drops the base URI it resolves relative
    links against AND the charset. Measured against feedparser 6.0.12 with a
    UTF-8 body whose XML declaration carries no encoding=:

        response_headers=        encoding     title     <link>/rel/path
        ──────────────────────────────────────────────────────────────
        (omitted)                utf-8        café      '/rel/path'  unresolved
        content-location only    iso-8859-1   cafÃ©     resolved     MOJIBAKE
        content-location + type  utf-8        café      resolved     correct

    feedparser is imported HARD, not importorskip'd: it is in requirements.txt
    and a skipped fidelity test is a silent green (the Pillow precedent in
    .github/workflows/pre-merge.yml).
    """
    import http.server
    import socketserver

    import feedparser

    from util.feed_fetch import parse_feed

    body = ('<?xml version="1.0"?><rss version="2.0"><channel>'
            '<title>Café Feed</title><link>https://ex.test/</link>'
            '<item><title>Smørrebrød opens DC</title><link>/news/1</link></item>'
            '<item><title>plain</title><link>https://abs.test/2</link></item>'
            '</channel></rss>').encode("utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redir":
                self.send_response(302)
                self.send_header("Location", "/deep/feed.xml")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    srv.allow_reuse_address = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]

    def snap(f):
        return {
            "encoding": f.encoding,
            "bozo": bool(f.bozo),
            "titles": [e.get("title") for e in f.entries],
            "links": [e.get("link") for e in f.entries],
        }

    try:
        for path in ("/feed.xml", "/redir"):
            native = snap(feedparser.parse(base + path))
            bounded = snap(parse_feed(base + path))
            assert native == bounded, (
                f"{path}: parse_feed diverged from feedparser.parse(url)\n"
                f"  feedparser.parse(url) = {native}\n"
                f"  parse_feed(url)       = {bounded}"
            )
        # ...and the properties that would silently vanish are actually present,
        # so the comparison above cannot pass by both sides being degraded.
        assert native["encoding"] == "utf-8", "charset was lost"
        assert native["titles"][0] == "Smørrebrød opens DC", "body was mis-decoded"
        assert native["links"][0].startswith("http://127.0.0.1:"), \
            "relative <link> was not resolved against the feed URL"
        assert native["bozo"] is False
    finally:
        srv.shutdown()
        srv.server_close()
