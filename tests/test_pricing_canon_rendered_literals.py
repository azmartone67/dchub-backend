"""No SERVED string may quote a monthly price the canon does not charge.

WHY THIS EXISTS. https://dchub.cloud/api/v1/redeem/<mcp-session-id> — the
email-capture page an agent sends its human to — rendered its own pricing
ladder as hardcoded HTML inside routes/redeem_routes.py:

    <span class="tier-name">Pro <span class="price">$199/mo</span></span>
    <span class="tier-detail">2,000 calls/day + Pro-only tools</span>

$199 was retired TWICE before that page was read: 199 -> 299 at r-reprice
(2026-06-19) and 299 -> 99 at r-price-collapse (2026-09-05). Verified still
live on 2026-09-18:

    $ curl -sSL "https://dchub.cloud/api/v1/redeem/1aa6536d-...-e89ba266e781?_=$(date +%s)"
    HTTP 200 ... <span class="price">$199/mo</span>

A conversion page quoted double the real price for 13 days. 21 other files
under routes/ carried the same stale ladder — /connect, /claim, the DCPI and
state briefs, the paywall hints an agent reads on every 403.

WHY THE EXISTING GUARDS MISSED IT. tests/test_route_unlock_prices_read_canon
scans `*_usd_month` DICT KEYS; test_tier_price_label_canonical pins
_stripe_links.TIER_PRICE_LABEL; test_worker_prices_match_tier_registry pins
the worker. None of them look at PROSE — and every one of these surfaces
carried its price in prose or in HTML, never in a numeric field.

WHAT IT PINS. Every `$N/mo` (or `/month`) inside a string CONSTANT that can
reach a reader must name a price tier_registry actually charges. Comments and
docstrings are exempt on purpose: a comment recording that Pro *used to be*
$199 is history, and history must stay writable.

WHAT IT DELIBERATELY DOES NOT PIN. It does not ban a correctly-valued typed
literal outright. `$99/mo` typed into a template passes today and silently
becomes a lie at the next reprice — which is exactly how this drifted — so
the ladder on the redeem page is DERIVED (see routes/redeem_routes.py
_ladder_rows_html), and test_the_redeem_ladder_is_derived_not_typed below
pins that specific page against retyping. A repo-wide typed-literal ban is a
separate, larger change: 99 correctly-valued literals remain under routes/.
"""
import ast
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tier_registry  # noqa: E402

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_ROUTES = os.path.join(_REPO, "routes")

# Directories that never reach a reader: tests, vendored code, build output,
# and this repo's own tooling. Everything else under the repo root is scanned.
_SKIP_DIRS = {
    "tests", "node_modules", "venv", ".venv", "__pycache__", "build", "dist",
    "migrations", "archive", "scratch", "htmlcov", "site-packages",
}

# Served JSON manifests. These carry a typed price string that no Python
# expression can derive, so the guard is the only thing standing between them
# and a reprice. (The LIVE /.well-known/mcp.json is rendered by the Cloudflare
# worker — verified 2026-09-18, x-dc-worker-version: 4.9.71 — so these repo
# copies are not the served artifact today. They are scanned anyway: a stale
# price in a file named `.well-known/` is a loaded gun.)
_JSON_MANIFESTS = (
    ".well-known/mcp.json",
    "static/.well-known/mcp.json",
    "registries/modelcontextprotocol.io.json",
)

_PRICE_RE = re.compile(r"\$\s?([\d,]+)\s*/\s*mo(?:nth)?\b", re.IGNORECASE)

# ★ FLOOR. A scan that matches nothing passes vacuously. This is how many
# `$N/mo` literals the walker must still SEE across the repo. MEASURED at 222
# on 2026-09-18 after the canon sweep (30 x $9, 146 x $49, 38 x $99, 3 x $699
# plus the 5 allowlisted non-DC-Hub figures). The floor sits below that so
# ordinary copy edits do not trip it, but losing the scan's reach does — if
# the walker stops seeing the repo, this is what says so.
_MIN_LITERALS_SEEN = 180

# Monthly dollar figures that are NOT a DC Hub subscription price. Each entry
# is (filename, dollars, why). Anything not listed here must match canon.
_NOT_OUR_PRICE = {
    ("routes/iso_orchestrator.py", 5): "a Mumbai VPS relay quote, not a DC Hub tier",
    ("routes/twitter_diagnostic.py", 100): "X/Twitter API Basic tier pricing",
    ("routes/twitter_diagnostic.py", 5000): "X/Twitter API Pro tier pricing",
    ("routes/deepdive_master_shell.py", 200): "an X/Twitter paid-tier estimate in shell copy",
    ("routes/campaign_halfprice_annual.py", 199): (
        "addressed to subscribers still ON the legacy $199 link — it quotes the "
        "RECIPIENT's current rate, which is why it is deliberately not canon"),
}


def _canon_dollars():
    """Every dollar figure DC Hub actually charges: monthly list prices, the
    enterprise "from" anchor, and the labels _stripe_links publishes (annual
    and the one-time pack). _stripe_links.TIER_PRICE_LABEL is itself pinned to
    tier_registry by tests/test_tier_price_label_canonical.py, so reading it
    here adds the annual/pack figures without adding a second source."""
    out = {v for v in tier_registry.TIER_PRICE_USD_MONTH.values()
           if isinstance(v, (int, float)) and v}
    out.add(tier_registry.ENTERPRISE_FROM_USD_YEAR)
    try:
        from routes._stripe_links import TIER_PRICE_LABEL, TIER_ONE_TIME_USD
        for label in TIER_PRICE_LABEL.values():
            for m in re.finditer(r"\$\s?([\d,]+)", str(label)):
                out.add(int(m.group(1).replace(",", "")))
        out.update(v for v in TIER_ONE_TIME_USD.values() if isinstance(v, int))
    except Exception:  # pragma: no cover - _stripe_links is always importable
        pass
    return out


def _module_doc_strings(tree):
    """Docstrings AND bare string statements — documentation, never served."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            out.add(id(node.value))
    return out


def _python_sources():
    """Every .py under the repo that can put text in front of a reader."""
    for root, dirs, files in os.walk(_REPO):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in sorted(files):
            if f.endswith(".py"):
                yield os.path.join(root, f)


_SCAN_CACHE = []


def _rendered_price_literals():
    """(repo-relative file, lineno, dollars) for every $N/mo a reader can see.

    Covers Python string constants that are not documentation, plus the
    served JSON manifests. Comments never reach this walker at all — the ast
    module drops them — which is deliberate: pricing HISTORY must stay
    writable in comments, only live copy is pinned.
    """
    if _SCAN_CACHE:
        return _SCAN_CACHE[0]
    seen = []
    for path in _python_sources():
        rel = os.path.relpath(path, _REPO)
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        docs = _module_doc_strings(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in docs:
                continue
            for m in _PRICE_RE.finditer(node.value):
                seen.append((rel, node.lineno, int(m.group(1).replace(",", ""))))

    for rel in _JSON_MANIFESTS:
        path = os.path.join(_REPO, rel)
        if not os.path.exists(path):
            continue
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            for m in _PRICE_RE.finditer(line):
                seen.append((rel, i, int(m.group(1).replace(",", ""))))
    _SCAN_CACHE.append(seen)   # the walk parses every .py in the repo; do it once
    return seen


def test_the_scan_can_still_see_the_price_literals():
    """The floor. Without it the next test is green on an empty match set."""
    seen = _rendered_price_literals()
    assert len(seen) >= _MIN_LITERALS_SEEN, (
        "only %d `$N/mo` literals found in servable strings across the repo "
        "(floor %d). Either the copy moved somewhere this walker cannot see — "
        "in which case this guard now protects nothing — or the regex broke."
        % (len(seen), _MIN_LITERALS_SEEN))


def test_the_floor_is_not_above_what_exists():
    """A floor set above the real count would make the guard permanently red
    and invite someone to lower it until it is vacuous. Keep it honest."""
    assert _MIN_LITERALS_SEEN <= len(_rendered_price_literals())


def test_no_served_string_quotes_a_price_we_do_not_charge():
    canon = _canon_dollars()
    offenders = []
    for name, lineno, dollars in _rendered_price_literals():
        if dollars in canon:
            continue
        if (name, dollars) in _NOT_OUR_PRICE:
            continue
        offenders.append("%s:%d  $%s/mo" % (name, lineno, format(dollars, ",")))
    assert not offenders, (
        "a served string quotes a monthly price DC Hub does not charge "
        "(canon = %s from tier_registry.TIER_PRICE_USD_MONTH):\n  %s\n"
        "Read the price from tier_registry.price_display(<tier>) instead of "
        "typing it. If the figure is genuinely not a DC Hub plan (a competitor's "
        "price, a vendor cost, a retired A/B arm named in copy), add it to "
        "_NOT_OUR_PRICE with the reason."
        % (sorted(canon), "\n  ".join(offenders)))


def test_the_allowlist_has_no_dead_entries():
    """An allowlist entry that no longer matches anything hides the next
    offender at that (file, price) the day someone reintroduces it."""
    canon = _canon_dollars()
    # Only NON-canon literals ever reach the allowlist. An entry whose price
    # is canonical is inert: it would be skipped one branch earlier.
    live = {(n, d) for n, _, d in _rendered_price_literals() if d not in canon}
    dead = sorted(k for k in _NOT_OUR_PRICE if k not in live)
    assert not dead, (
        "these _NOT_OUR_PRICE entries match nothing any more — delete them so "
        "they cannot silently pre-approve a future literal: %s" % (dead,))


# ── the redeem page specifically ─────────────────────────────────────

def test_the_redeem_ladder_is_derived_not_typed():
    """The page that started this. Its ladder must contain no typed price at
    all — a correct literal here is the same bug with a later fuse."""
    src = open(os.path.join(_ROUTES, "redeem_routes.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    docs = _module_doc_strings(tree)
    typed = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs):
            for m in _PRICE_RE.finditer(node.value):
                typed.append("line %d: %s" % (node.lineno, m.group(0)))
    assert not typed, (
        "routes/redeem_routes.py types a price into a served string: %s. "
        "Every figure on this page comes from tier_registry — see "
        "_ladder_rows_html()." % typed)


def test_the_rendered_ladder_equals_canon():
    """Behaviour, not shape: render the real page and read the prices off it."""
    from flask import Flask
    from routes.redeem_routes import redeem_bp

    app = Flask(__name__)
    app.register_blueprint(redeem_bp)
    resp = app.test_client().get(
        "/api/v1/redeem/1aa6536d-b1d4-24b4-74a8-e89ba266e781",
        headers={"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/127 Safari/537.36"})
    assert resp.status_code == 200, resp.status_code
    body = resp.get_data(as_text=True)

    assert "The ladder" in body, "the ladder did not render at all"
    assert "__" not in body.split("<style>")[0] or "__LADDER_ROWS__" not in body, \
        "a template token survived into the response"

    served = {int(m.group(1).replace(",", "")) for m in _PRICE_RE.finditer(body)}
    assert served, "the page rendered no price at all — the ladder is gone"
    assert served <= _canon_dollars(), (
        "the redeem page served prices that are not canon: %s (canon %s)"
        % (sorted(served - _canon_dollars()), sorted(_canon_dollars())))

    pro = tier_registry.price_display("pro")
    assert pro in body, "the live Pro price %s is not on the page" % pro
    assert "$199" not in body, "the retired $199 Pro price is still rendered"

    # The quotas next to each price must be canon too — the anonymous row
    # promised "10 calls/day · 2 results per query" for six weeks after
    # TIER_LIMITS moved anonymous to 5/5 (2026-08-03).
    anon = tier_registry.limits("anonymous")
    assert "%d calls/day · %d results per query" % (
        anon["mcp_daily"], anon["mcp_results"]) in body, (
        "the anonymous row does not match TIER_LIMITS")
    assert "%s calls/day + Pro-only tools" % format(
        tier_registry.calls_per_day("pro"), ",") in body, (
        "the Pro row's calls/day does not match TIER_LIMITS")


# ── bare dollar figures standing next to a tier name ─────────────────
#
# The `$N/mo` rule above does not see "Pro ($199)" or a price cell reading
# "$199+" with the "/mo" in a separate <small> tag. Both shipped: routes/
# paywall_hint_middleware.py variant C and routes/gating_matrix.py's tier
# card, plus routes/onboarding_page.py's "Starter $9 / Developer $49 / Pro
# $199" — all live on 2026-09-18, none matched by a `/mo` regex.
#
# So: any `$N` within 40 characters of a tier NAME must be a figure canon
# actually charges. The window keeps site valuations, $/kW figures and
# outreach amounts out of it — 159 bare `$N >= 100` literals exist in this
# repo and only the tier-adjacent ones are prices.

_DOLLAR_RE = re.compile(r"\$\s?(\d[\d,]*)")
_TIER_WORD_RE = re.compile(r"\b(pro|developer|starter|team|founding)\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]{0,400}>")
_WINDOW = 60

# ★ Proximity is measured on what a READER sees, not on the source bytes.
# routes/gating_matrix.py's tier card puts 38 characters of markup between
# "Pro" and its price cell and another tag between the price and its "/mo":
#
#   <div class="tier pro"><div class="name">Pro / Enterprise</div>
#   <div class="price">$199+<small ...>/mo</small></div>
#
# Neither the `/mo` rule nor a byte-distance window saw that — it was found
# by mutating the card and watching this file stay green. Stripping tags
# first puts "Pro / Enterprise" three characters from "$199+", where it is.


def _visible_text(text):
    """The string as a reader sees it: HTML tags collapsed to a space."""
    return _TAG_RE.sub(" ", text)

_TIER_ADJACENT_EXEMPT = {
    ("routes/twitter_diagnostic.py", 100): "X/Twitter API Basic tier",
    ("routes/twitter_diagnostic.py", 5000): "X/Twitter API Pro tier",
    ("routes/hyperscaler_brief.py", 100): "a competitor's $100K+/yr quote, not ours",
    ("tools/email_blast_developer_launch.py", 20): "a discount amount, not a price",
    ("stripe_simulation.py", 299): "a simulation scenario replaying a legacy charge",
    ("routes/campaign_halfprice_annual.py", 199): (
        "the half-price campaign email quotes the RECIPIENT's current $199 "
        "rate next to the Pro Annual offer — their price, not our list price"),
    ("routes/audit_closure_master_shell.py", 199): (
        "audit prose that exists to record the retired $199 link by number"),
    ("routes/audit_closure_master_shell.py", 299): (
        "audit prose that exists to record the retired $299 price by number"),
}

_ADJACENT_CACHE = []


def _tier_adjacent_dollars():
    """(file, lineno, dollars) for every $N standing next to a tier name."""
    if _ADJACENT_CACHE:
        return _ADJACENT_CACHE[0]
    found = []
    for path in _python_sources():
        rel = os.path.relpath(path, _REPO)
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        docs = _module_doc_strings(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in docs:
                continue
            text = _visible_text(node.value)
            # ★ Find the tier words in the FULL text and measure distance.
            # Slicing a window first cuts words in half: a 60-char slice
            # ending inside "produces" leaves a trailing "pro" that matches
            # \bpro\b, which is how a $/MW valuation range first tripped
            # this check.
            tier_spans = [w.span() for w in _TIER_WORD_RE.finditer(text)]
            for m in _DOLLAR_RE.finditer(text):
                amount = int(m.group(1).replace(",", ""))
                if not amount:
                    continue
                near = any(ws < m.end() + _WINDOW and m.start() - _WINDOW < we
                           for ws, we in tier_spans)
                if near:
                    found.append((rel, node.lineno, amount))
    _ADJACENT_CACHE.append(found)
    return found


def test_the_tier_adjacent_scan_sees_something():
    """Floor for the second walker."""
    found = _tier_adjacent_dollars()
    assert len(found) >= 40, (
        "only %d tier-adjacent dollar figures found (floor 40) — the walker "
        "has lost its reach, or the tier vocabulary changed." % len(found))


def test_no_dollar_next_to_a_tier_name_is_off_canon():
    canon = _canon_dollars()
    offenders = []
    for name, lineno, amount in _tier_adjacent_dollars():
        if amount in canon:
            continue
        if (name, amount) in _TIER_ADJACENT_EXEMPT:
            continue
        offenders.append("%s:%d  $%s" % (name, lineno, format(amount, ",")))
    assert not offenders, (
        "a dollar figure standing next to a tier name is not one DC Hub "
        "charges (canon = %s):\n  %s\nDerive it from tier_registry, or add "
        "it to _TIER_ADJACENT_EXEMPT with the reason it is not our price."
        % (sorted(canon), "\n  ".join(offenders)))


def test_the_tier_adjacent_allowlist_has_no_dead_entries():
    canon = _canon_dollars()
    live = {(n, d) for n, _, d in _tier_adjacent_dollars() if d not in canon}
    dead = sorted(k for k in _TIER_ADJACENT_EXEMPT if k not in live)
    assert not dead, (
        "these _TIER_ADJACENT_EXEMPT entries match nothing any more — delete "
        "them so they cannot pre-approve a future literal: %s" % (dead,))
