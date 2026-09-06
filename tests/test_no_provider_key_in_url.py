"""A provider credential must never be interpolated into a URL query string.

★ WHY, measured 2026-09-06. ai_wars_automation._call_google built its request
as `...:generateContent?key={key}`. Google AI Studio accepts that form, so it
worked — and Cloudflare's AI Gateway records the full request PATH, so every
call wrote GOOGLE_AI_KEY into the gateway log in plaintext. Reading those logs
with a freshly-granted AI Gateway Read token showed the live key in 4 rows.
Query strings also reach proxy logs, browser history and Referer headers; the
gateway is simply where it was caught.

The header form (`x-goog-api-key`, `Authorization`, `X-API-Key`) is logged by
none of them, which is why this is a rule and not a preference.

SCOPE, deliberately narrow to stay actionable:
  · only EXTERNAL hosts — dchub.cloud's own `/upgrade?key={api_key}` hands a
    user their OWN key over TLS and is not this defect;
  · only f-strings whose literal text ends in a secret-ish `?param=` right
    before an interpolation. A hard-coded `?key=test-key` in a test is inert
    and is not flagged.
  · urlencode() dict building IS covered, by a second detector below — it
    was added after that gap hid three live sites from the first one.
  · %-formatting and .format() are still NOT seen. Real gaps, stated so they
    are not mistaken for a clean sweep.
"""
import ast
import collections
import functools
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# The literal immediately BEFORE an interpolation, ending in a secret param.
_SECRET_PARAM = re.compile(
    r"[?&](key|api[-_]?key|access[-_]?token|auth[-_]?token|token|secret|"
    r"password|passwd|pwd|sig|signature)=\Z", re.I)

# Hosts we control: handing a user their own key over TLS is a different thing.
_OURS = ("dchub.cloud", "localhost", "127.0.0.1", "0.0.0.0", "railway.internal")

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
              "site-packages", ".mypy_cache", ".pytest_cache", "build", "dist"}


def _is_external(url_head: str) -> bool:
    m = re.search(r"https?://([^/\s'\"]+)", url_head)
    if not m:
        return False                      # relative path — not an outbound URL
    host = m.group(1).lower()
    return not any(o in host for o in _OURS)


def scan_source(src: str, label: str):
    """Return [(label, lineno, param)] for secrets interpolated into a URL."""
    hits = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return hits
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        head = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                head += part.value
                continue
            # an interpolation: does the literal just before it name a secret?
            m = _SECRET_PARAM.search(head)
            if m and _is_external(head):
                hits.append((label, node.lineno, m.group(1)))
            head += "\x00"                # opaque placeholder, keeps offsets sane
    return hits


def _query_bound_urlencode_args(tree):
    """Names passed to a urlencode() whose RESULT lands in a query string.

    ★ urlencode IS NOT A QUERY-STRING SIGNAL ON ITS OWN. An OAuth token
    exchange form-encodes client_secret/password into the request BODY, which
    is correct and must not be flagged — a first draft of this check reported
    routes/ercot_realtime.py and linkedin_poster.py as leaks. What makes it a
    leak is the result being concatenated into a URL, so that is what we look
    for: a "?" or "&" literal beside the call, either directly or through the
    variable the call was assigned to.
    """
    def has_q(node):
        return any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and ("?" in n.value or "&" in n.value) for n in ast.walk(node))

    def calls(node):
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                fn = n.func
                if (isinstance(fn, ast.Name) and fn.id == "urlencode") or \
                   (isinstance(fn, ast.Attribute) and fn.attr == "urlencode"):
                    yield n

    direct, via_var, qs_vars = set(), {}, set()
    for node in ast.walk(tree):
        # (a) urlencode() sitting inside a concat / f-string that has ? or &
        if isinstance(node, (ast.BinOp, ast.JoinedStr)) and has_q(node):
            for c in calls(node):
                for a in c.args[:1]:
                    if isinstance(a, ast.Name):
                        direct.add(a.id)
        # (b) q = urlencode(params)   ... later   f"{base}?{q}"
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            for c in calls(node.value):
                for a in c.args[:1]:
                    if isinstance(a, ast.Name):
                        via_var.setdefault(node.targets[0].id, set()).add(a.id)
        if isinstance(node, (ast.BinOp, ast.JoinedStr)) and has_q(node):
            for n in ast.walk(node):
                if isinstance(n, ast.Name):
                    qs_vars.add(n.id)
    for var, params in via_var.items():
        if var in qs_vars:
            direct |= params
    return direct


def scan_source_urlencode(src: str, label: str):
    """Credentials smuggled into a query string via a dict + urlencode().

    ★ THIS IS THE GAP THAT HID THREE SITES. The f-string detector walks
    JoinedStr only, so `params = {"api_key": KEY}; url = base + "?" +
    urlencode(params)` was invisible to it — and that shape was live in
    eia_retirements.py and both discovery scripts while the f-string list read
    as complete. A guard that reports a clean sweep over a subset of the shapes
    it claims to cover is worse than none, because it gets believed.

    A secret-named key counts only when its VALUE IS A VARIABLE (a literal is a
    test fixture) and the dict is urlencoded INTO A URL, not into a body.
    """
    hits = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return hits
    wanted = _query_bound_urlencode_args(tree)
    if not wanted:
        return hits
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in wanted \
                and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                        and _SECRET_PARAM.search("?" + k.value + "=")
                        and isinstance(v, (ast.Name, ast.Attribute))):
                    hits.append((label, k.lineno, k.value))
    return hits


# ── shape 3: requests(..., params={"api_key": KEY}) ─────────────────────────
# requests builds the query string from params=, so this leaks exactly like a
# hand-built URL — and neither detector above can see it. Found 2026-09-06,
# AFTER #4008 declared the sweep complete, which is the third time a "complete"
# list here turned out to cover only the shapes someone had thought of.
_HTTP_VERBS = {"get", "post", "put", "request", "patch", "delete"}

# Call sites where the provider genuinely offers no header auth. Each entry is
# a decision with evidence, not a shrug.
_NO_HEADER_AUTH = {
    # AbstractAPI ignores X-Api-Key entirely — with a bogus header it still
    # answers {"api_key": ["This is a required argument."]} (verified
    # 2026-09-06). Their design; the key must ride in the query.
    ("routes/signup_enrichment.py", "api_key"),
}


def _scope_nodes(root):
    """Walk one scope WITHOUT descending into nested function/class bodies.

    ★ THIS IS LOAD-BEARING. A plain ast.walk from the module makes every
    `params = {...}` in the file visible to every call in the file, and a name
    as common as `params` collides constantly: an early draft of this check
    reported enhancements/site_scoring.py:363 — a WattTime call that already
    uses a Bearer header — because a DIFFERENT function's params dict shared
    the name. test_a_dict_in_another_function_does_not_match pins it.
    """
    out = []
    stack = list(ast.iter_child_nodes(root))
    while stack:
        n = stack.pop()
        out.append(n)
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(n))
    return out


def scan_source_params(src: str, label: str):
    """Credentials handed to requests(params=...), which become a query string."""
    hits = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return hits
    scopes = [tree] + [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for sc in scopes:
        nodes = _scope_nodes(sc)
        local = {n.targets[0].id: n.value for n in nodes
                 if isinstance(n, ast.Assign) and len(n.targets) == 1
                 and isinstance(n.targets[0], ast.Name)
                 and isinstance(n.value, ast.Dict)}
        for n in nodes:
            if not isinstance(n, ast.Call):
                continue
            fn = n.func
            verb = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if verb not in _HTTP_VERBS:
                continue
            for kw in n.keywords:
                if kw.arg != "params":
                    continue
                d = kw.value if isinstance(kw.value, ast.Dict) \
                    else local.get(getattr(kw.value, "id", ""))
                if d is None:
                    continue
                for k, v in zip(d.keys, d.values):
                    if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                            and _SECRET_PARAM.search("?" + k.value + "=")
                            and isinstance(v, (ast.Name, ast.Attribute))):
                        hits.append((label, n.lineno, k.value))
    return hits


def _hit_is_ai_host(hit) -> bool:
    """Re-read the flagged line to decide whether it targets an AI provider."""
    f, ln, _param = hit
    try:
        lines = (_ROOT / f).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    window = " ".join(lines[max(0, ln - 1):ln + 6])
    return any(h in window for h in _AI_HOSTS)


def _python_files():
    for p in _ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        yield p


@functools.lru_cache(maxsize=1)   # four tests, one walk of 2,400+ files
def scan_repo():
    hits, enc, prm, files, fstrings = [], [], [], 0, 0
    for p in _python_files():
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files += 1
        try:
            fstrings += sum(1 for n in ast.walk(ast.parse(src))
                            if isinstance(n, ast.JoinedStr))
        except SyntaxError:
            pass
        rel = str(p.relative_to(_ROOT))
        hits += scan_source(src, rel)
        enc += scan_source_urlencode(src, rel)
        prm += scan_source_params(src, rel)
    return tuple(hits), files, fstrings, tuple(enc), tuple(prm)


# ── the checker must be able to SEE a violation ──────────────────────────────
_BAD = (
    "url = f'https://gateway.ai.cloudflare.com/v1/a/b/google-ai-studio"
    "/v1beta/models/x:generateContent?key={key}'\n")
_OURS_OK = "url = f'https://dchub.cloud/upgrade?key={api_key}'\n"
_HEADER_OK = (
    "r = requests.post('https://api.example.com/v1/x',\n"
    "                  headers={'x-goog-api-key': key})\n")
_LITERAL_OK = "url = f'https://api.example.com/v1/{thing}?key=not-a-real-secret'\n"


def test_the_checker_flags_the_shape_that_actually_shipped():
    """Positive control. Without this, an over-narrow regex would make every
    assertion below vacuously green."""
    hits = scan_source(_BAD, "<synthetic>")
    assert len(hits) == 1 and hits[0][2].lower() == "key", hits


def test_the_checker_does_not_flag_our_own_upgrade_link():
    assert scan_source(_OURS_OK, "<synthetic>") == []


def test_the_checker_does_not_flag_a_header_or_an_inert_literal():
    assert scan_source(_HEADER_OK, "<synthetic>") == []
    assert scan_source(_LITERAL_OK, "<synthetic>") == []


# ── the repo itself ──────────────────────────────────────────────────────────
# ★ A REPO-WIDE SCAN THAT FINDS NOTHING IS VACUOUSLY GREEN. If a bad glob, a
# moved test root or an exclude-list edit made this examine zero files, every
# assertion below would still pass. Floors sit at ~80% of the counts MEASURED
# 2026-09-06 (2,445 files, 28,669 f-strings) so ordinary churn does not trip
# them, but a scan that has stopped seeing the tree does.
_MIN_FILES = 1950
_MIN_FSTRINGS = 22900

# ── absolute rule vs. ratchet ────────────────────────────────────────────────
# AI providers are an ABSOLUTE ban: this is the surface where the leak was
# actually observed, into logs WE own and retain (Cloudflare AI Gateway keeps
# up to 10M rows, readable by any AI-Gateway-Read token).
_AI_HOSTS = ("generativelanguage.googleapis.com", "gateway.ai.cloudflare.com",
             "api.anthropic.com", "api.openai.com", "api.x.ai",
             "api.mistral.ai", "api.cohere.ai", "api.groq.com")

# Everything else was a RATCHET while the api.eia.gov sites were converted.
# All 15 are DONE (2026-09-06) — 12 found by the f-string detector plus 3
# the urlencode detector found once it existed. Each sends X-Api-Key now,
# verified live against api.eia.gov:
#   no key -> API_KEY_MISSING ; bogus header -> API_KEY_INVALID
# so the list is empty and the rule is absolute everywhere. Re-populating this
# is a deliberate act that has to be argued for in a PR, which is the point.
_KNOWN_DEBT: dict[tuple[str, str], int] = {}


def test_the_scan_actually_reads_the_repo():
    """Floors, so a broken glob cannot make the rules below vacuously green."""
    _, files, fstrings, _, _ = scan_repo()
    assert files >= _MIN_FILES, (
        f"scanned only {files} python files (floor {_MIN_FILES}) — this guard "
        "is not reading the repo any more, so its green means nothing")
    assert fstrings >= _MIN_FSTRINGS, (
        f"parsed only {fstrings} f-strings (floor {_MIN_FSTRINGS}) — the walk "
        "is not reaching the code it claims to check")


def test_no_ai_provider_key_is_ever_in_a_url():
    """ABSOLUTE. This is the surface the leak was observed on."""
    hits, _, _, _, _ = scan_repo()
    bad = [h for h in hits if _hit_is_ai_host(h)]
    assert not bad, (
        "AI provider credential in a URL query string — it will be written "
        "verbatim into the AI Gateway log:\n" + "\n".join(
            f"  {f}:{ln}  ?{p}=<interpolated>" for f, ln, p in bad))


def test_no_new_credential_in_url_outside_the_known_debt():
    """RATCHET. A new site fails — including a new one in a file already on
    the list, which a name-only allowlist would have waved through."""
    hits, _, _, _, _ = scan_repo()
    seen = collections.Counter((f, p) for f, _ln, p in hits)
    extra = []
    for key, n in seen.items():
        allowed = _KNOWN_DEBT.get(key, 0)
        if n > allowed:
            extra.append(f"  {key[0]}  ?{key[1]}=  ({n} occurrences, "
                         f"{allowed} recorded)")
    assert not extra, (
        "NEW credential-in-URL site (put the key in a header instead):\n"
        + "\n".join(extra))


def test_the_known_debt_list_does_not_rot():
    """If a listed site is fixed, its entry must be updated or removed —
    otherwise the list slowly stops describing anything and the ratchet
    loosens for free."""
    hits, _, _, _, _ = scan_repo()
    seen = collections.Counter((f, p) for f, _ln, p in hits)
    stale = [f"  {k[0]}  ?{k[1]}=  (recorded {n}, now {seen.get(k, 0)})"
             for k, n in _KNOWN_DEBT.items() if seen.get(k, 0) < n]
    assert not stale, (
        "recorded as known debt but no longer present that many times — "
        "tighten _KNOWN_DEBT:\n" + "\n".join(stale))


_BAD_ENC = (
    "from urllib.parse import urlencode\n"
    "params = {'api_key': EIA_API_KEY, 'frequency': 'monthly'}\n"
    "url = base + '?' + urlencode(params)\n")
_ENC_LITERAL_OK = (
    "from urllib.parse import urlencode\n"
    "params = {'api_key': 'test-fixture-key'}\n"
    "url = base + '?' + urlencode(params)\n")
_ENC_NO_URLENCODE_OK = "payload = {'api_key': EIA_API_KEY}\nrequests.post(u, json=payload)\n"


def test_the_urlencode_detector_sees_the_shape_that_hid_three_sites():
    hits = scan_source_urlencode(_BAD_ENC, "<synthetic>")
    assert len(hits) == 1 and hits[0][2] == "api_key", hits


def test_the_urlencode_detector_ignores_fixtures_and_json_bodies():
    assert scan_source_urlencode(_ENC_LITERAL_OK, "<synthetic>") == []
    assert scan_source_urlencode(_ENC_NO_URLENCODE_OK, "<synthetic>") == []


def test_no_credential_reaches_a_query_string_via_urlencode():
    _, _, _, enc, _ = scan_repo()
    assert not enc, "credential urlencoded into a query string:\n" + "\n".join(
        f"  {f}:{ln}  {param}" for f, ln, param in enc)


_BAD_PARAMS = ("r = requests.get(url, params={'api_key': EIA_API_KEY}, timeout=5)\n")
_BAD_PARAMS_VAR = ("p = {'api_key': KEY, 'x': 1}\n"
                   "r = requests.get(url, params=p, timeout=5)\n")
_PARAMS_OK = ("r = requests.get(url, params={'state': st}, "
              "headers={'X-Api-Key': KEY}, timeout=5)\n")
_PARAMS_FIXTURE_OK = "r = requests.get(url, params={'api_key': 'literal-fixture'})\n"
_SCOPE_COLLISION = (
    "def a(KEY):\n"
    "    params = {'api_key': KEY}\n"
    "    return requests.get(u, params=params, headers={})\n"
    "def b(lat):\n"
    "    params = {'latitude': lat}\n"
    "    return requests.get(u2, params=params, headers={'Authorization': 'Bearer x'})\n")


def test_the_params_detector_sees_both_inline_and_variable_dicts():
    assert len(scan_source_params(_BAD_PARAMS, "<s>")) == 1
    assert len(scan_source_params(_BAD_PARAMS_VAR, "<s>")) == 1


def test_the_params_detector_ignores_headers_and_fixtures():
    assert scan_source_params(_PARAMS_OK, "<s>") == []
    assert scan_source_params(_PARAMS_FIXTURE_OK, "<s>") == []


def test_a_dict_in_another_function_does_not_match():
    """★ The scope bug, pinned. Both functions call their local `params`; only
    the one that actually carries a credential may be reported. An ast.walk
    from the module makes this report TWO."""
    hits = scan_source_params(_SCOPE_COLLISION, "<s>")
    assert len(hits) == 1, hits


def test_no_credential_reaches_a_query_string_via_requests_params():
    _, _, _, _, prm = scan_repo()
    unexpected = [h for h in prm if (h[0], h[2]) not in _NO_HEADER_AUTH]
    assert not unexpected, (
        "credential handed to requests(params=...), which makes it a query "
        "string:\n" + "\n".join(f"  {f}:{ln}  params[{p}]"
                                 for f, ln, p in unexpected))
