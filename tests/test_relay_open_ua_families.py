"""The filtered relay opens must say WHAT they are, not just how many.

`relay_open_provenance` publishes probe_ua = 142 of 174 (30d, 2026-09-07) and
stops. That number cannot answer the question it raises, and the answer decides
opposite work:

  link_unfurl      Slack/Discord/iMessage fetch a URL the moment it is pasted
                   into a channel a HUMAN reads. The link was delivered, and
                   human_acted:0 is a false zero.
  scripting_client nobody received it; delivery is still the problem and the
                   work goes back into the envelope.

Both sat in one bucket. These tests execute the family regexes against real
user-agent strings rather than asserting about the SQL text.
"""
import ast
import re
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "flask_mcp_endpoints.py"


def _family_sql() -> str:
    src = SRC.read_text(encoding="utf-8")
    i = src.index("_ua_family = (")
    j = src.index("ua_families = []", i)
    ns: dict = {}
    exec(compile(src[i:j], str(SRC), "exec"), ns)
    return ns["_ua_family"]


def _classify(ua: str) -> str:
    """Apply the shipped CASE the way Postgres would: first match wins."""
    sql = _family_sql()
    if not ua:
        return "no_user_agent"
    order = re.findall(r"~\* '(\(.*?\))' then '(\w+)'", sql)
    assert order, "could not extract the family regexes from the shipped CASE"
    for rx, name in order:
        if re.search(rx, ua, re.I):
            return name
    return "unclassified"


REAL_AGENTS = [
    ("Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)", "link_unfurl"),
    ("Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)", "link_unfurl"),
    ("WhatsApp/2.23.20.0 A", "link_unfurl"),
    ("facebookexternalhit/1.1", "link_unfurl"),
    ("LinkedInBot/1.0 (compatible; Mozilla/5.0)", "link_unfurl"),
    ("python-requests/2.31.0", "scripting_client"),
    ("curl/8.4.0", "scripting_client"),
    ("Go-http-client/2.0", "scripting_client"),
    ("Mozilla/5.0 (X11) HeadlessChrome/120.0.0.0", "headless_browser"),
    ("dchub-healer/1.0", "internal_or_monitor"),
    ("UptimeRobot/2.0", "internal_or_monitor"),
    ("Mozilla/5.0 (compatible; SomeCrawler/1.0; +http://x)", "other_bot"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15", "unclassified"),
    ("", "no_user_agent"),
]


def test_real_user_agents_land_in_the_right_family():
    for ua, want in REAL_AGENTS:
        got = _classify(ua)
        assert got == want, f"{ua!r} -> {got}, expected {want}"


def test_applebot_is_not_counted_as_a_link_unfurl():
    """★ Applebot serves iMessage previews AND Apple's search crawl, and the UA
    cannot distinguish them. link_unfurl is the bucket that would justify "the
    links ARE reaching humans" — the single conclusion this histogram exists to
    test — so an ambiguous agent must not inflate it."""
    assert _classify("Mozilla/5.0 (compatible; Applebot/0.1)") != "link_unfurl"


def test_the_case_carries_no_literal_percent():
    """★ A literal % in a predicate here took the live handoff-funnel down
    inside one deploy: the caller builds its SQL with `sql % iv` and Python
    raised "not enough arguments for format string". ILIKE patterns are banned
    in this position; the families are regex."""
    sql = _family_sql()
    assert "%" not in sql, f"literal % in the family CASE: {sql[:160]!r}"
    assert "ilike" not in sql.lower(), "ILIKE reintroduces the % that breaks % iv"


def test_families_are_mutually_exclusive_by_construction():
    """First-match-wins, so a UA matching two regexes takes the earlier one.
    The published basis claims the families sum to provenance.total, which is
    only true if every row lands in exactly one."""
    sql = _family_sql()
    order = [n for _rx, n in re.findall(r"~\* '(\(.*?\))' then '(\w+)'", sql)]
    assert len(order) == len(set(order)), f"a family name is emitted twice: {order}"
    assert "else 'unclassified' end" in sql, (
        "no ELSE branch — a row matching nothing would be NULL and silently "
        "drop out of the histogram, breaking the sums-to-total claim")


def test_the_histogram_reports_which_families_pass_the_filter():
    """A family count alone does not say whether those opens are being
    discarded. The point is the join between the two."""
    src = SRC.read_text(encoding="utf-8")
    assert "passes_real_ua" in src, (
        "the histogram does not report how many of each family survive the "
        "real-UA predicate, which is the whole comparison")
    # ★ THE NAME IS NOT THE VALUE. The first version of this test asserted only
    # that the string "passes_real_ua" appeared, and a mutation replacing the
    # whole expression with `0 as passes_real_ua` sailed through it — the
    # column would have published zero for every family while looking present.
    # Bind the DERIVATION: it must be a filtered count over the real-UA
    # predicate the funnel actually applies.
    i = src.index("as family, count(*) as opens")
    col = src[i:src.index("group by 1", i)]
    assert "_ro_real" in col, (
        f"passes_real_ua is not computed from the real-UA predicate: {col!r}")
    assert re.search(r"count\(\*\)\s*filter", col), (
        f"passes_real_ua is not a filtered count: {col!r}")
    assert '"relay_open_ua_families_basis"' in src, "published without a basis"


def test_it_is_published_by_assignment_not_dict_update():
    """★ out.update() on a response dict made /api/v1/ai/reach opaque to the
    API response contract guard earlier today — 18 keys dropped out of
    coverage. Same shape, same rule."""
    src = SRC.read_text(encoding="utf-8")
    assert '"relay_open_ua_families": ua_families,' in src
    # ★ SCOPED TO THE FUNNEL BUILDER, not the module. The first version of this
    # test walked the whole file and failed on an out.update() at line 3241
    # that is pre-existing on origin/main, in an unrelated function, and none
    # of my business. A guard that fails on code the change never touched is
    # not a stricter guard, it is a broken one.
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_win"), None)
    assert fn is not None, "_win (the funnel window builder) not found"
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "out"):
            raise AssertionError(
                "out.update() in the funnel builder makes the payload "
                "statically opaque to the API response contract guard")


def test_the_query_failure_path_leaves_an_empty_list_not_a_crash():
    """The funnel endpoint is fail-soft by contract — a histogram is a
    diagnostic and must never 5xx the stage counts."""
    src = SRC.read_text(encoding="utf-8")
    i = src.index("ua_families = []")
    seg = src[i:src.index('"relay_open_ua_families"')]
    # ★ This used to require a `rollback` in the except. That assertion encoded
    # the BUG: the rollback was `c.rollback()`, and there is no `c` in _win —
    # so it could never have run either. The histogram now shares `cur` with
    # every other query in the function, whose transaction it does not own.
    # What fail-soft must mean here is: never raise, and never go quiet.
    assert "except Exception as _uae:" in seg, (
        "the histogram query is not wrapped fail-soft")
    assert "ua_families = []" in seg and "ua_families_error =" in seg, (
        "the failure path must leave an empty list AND say why it is empty")


def _win_fn():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_win"), None)
    assert fn is not None, "_win not found in flask_mcp_endpoints.py"
    return fn


def test_every_name_the_histogram_reads_is_actually_bound():
    """★ THE BUG THIS EXISTS FOR. The first version ran `with c.cursor()`.
    There is no `c` in _win — every working query goes through one(sql), which
    closes over `cur`. It raised NameError on every call, the blanket except
    swallowed it, and the endpoint published `relay_open_ua_families: []` beside
    `relay_open_provenance.total: 178` for an hour. A histogram of nothing next
    to a count of 178 reads as "measured, and empty".

    No test asserted about the SQL could catch that — the SQL was fine. What was
    wrong was a name that did not exist. So: bind-check the block."""
    import builtins
    fn = _win_fn()
    src = SRC.read_text(encoding="utf-8")

    # the histogram statements, by line range
    start = src[:src.index("ua_families = []")].count("\n") + 1
    end = src[:src.index('"relay_open_ua_families": ua_families,')].count("\n") + 1

    bound = {a.arg for a in fn.args.args}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        bound.add(sub.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(n.name)
            bound.update(a.arg for a in n.args.args)
        elif isinstance(n, ast.For):
            for sub in ast.walk(n.target):
                if isinstance(sub, ast.Name):
                    bound.add(sub.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                bound.add((al.asname or al.name).split(".")[0])
        elif isinstance(n, ast.withitem) and n.optional_vars is not None:
            for sub in ast.walk(n.optional_vars):
                if isinstance(sub, ast.Name):
                    bound.add(sub.id)
        elif isinstance(n, ast.comprehension):
            for sub in ast.walk(n.target):
                if isinstance(sub, ast.Name):
                    bound.add(sub.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)

    # module-level globals are legitimate too
    mod = ast.parse(src)
    for n in mod.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    bound.add(t.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                bound.add((al.asname or al.name).split(".")[0])

    unbound = set()
    for n in ast.walk(fn):
        if (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                and start <= n.lineno <= end
                and n.id not in bound
                and not hasattr(builtins, n.id)):
            unbound.add(n.id)
    assert not unbound, (
        f"the histogram block reads name(s) that are never bound in _win or at "
        f"module scope: {sorted(unbound)} — this is how `c.cursor()` shipped a "
        f"NameError that the except swallowed into an empty published list")


def test_an_empty_histogram_publishes_its_reason():
    """`[]` must never again be indistinguishable from "no rows"."""
    src = SRC.read_text(encoding="utf-8")
    assert '"relay_open_ua_families_error": ua_families_error,' in src, (
        "an empty histogram publishes no reason, so a crash and a genuinely "
        "empty window look identical to every consumer")
    # ★ BIND THE VALUE, NOT THE NAME. Asserting the string
    # "ua_families_error =" appears is satisfied by `ua_families_error = None`,
    # which is exactly the silence this field exists to end — the same
    # name-vs-derivation miss as the passes_real_ua check above.
    tree = ast.parse(src)
    handlers = [h for h in ast.walk(tree)
                if isinstance(h, ast.ExceptHandler) and h.name == "_uae"]
    assert handlers, "no `except Exception as _uae` handler found"
    derived = False
    for h in handlers:
        for node in ast.walk(h):
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", "") == "ua_families_error" for t in node.targets):
                if any(isinstance(n, ast.Name) and n.id == "_uae"
                       for n in ast.walk(node.value)):
                    derived = True
    assert derived, (
        "ua_families_error is assigned in the except branch but not DERIVED "
        "from the exception — an empty histogram would still publish no reason")
