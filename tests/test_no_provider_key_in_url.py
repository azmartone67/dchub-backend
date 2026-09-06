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
  · it does NOT see %-formatting, .format(), or urlencode() dict building.
    Those are real gaps; this catches the shape that actually shipped.
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
    hits, files, fstrings = [], 0, 0
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
        hits += scan_source(src, str(p.relative_to(_ROOT)))
    return tuple(hits), files, fstrings


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

# Everything else is a RATCHET, not a blessing. These 12 EIA call sites bake
# the key into a URL that is built in one place and fetched in another (three
# of them append to a list of URLs consumed elsewhere), so converting them is a
# plumbing change that cannot be exercised without a live EIA key — not
# something to smuggle into a security fix.
#
# ★ THEY ARE NOT EXEMPT ON TECHNICAL GROUNDS. Verified live 2026-09-06:
#   no key            -> API_KEY_MISSING
#   X-Api-Key header  -> API_KEY_INVALID   <- the header IS read
# so every one of these is fixable.
#
# ★ COUNTED PER FILE, not just named. An earlier draft keyed this on
# (file, param) alone — and a mutation proved it: adding a BRAND NEW leaking
# URL to water_drought_routes.py, a file already on the list, kept all 7 tests
# green. A named-file allowlist silently covers every future defect in that
# file. The counts below are asserted EXACTLY, so one more occurrence fails and
# one fewer fails too (the list must shrink when a site is fixed, not rot).
_KNOWN_DEBT = {
    ("capacity_headroom_api.py", "api_key"): 2,
    ("eia_860m.py", "api_key"): 1,
    ("eia_retirements.py", "api_key"): 1,
    ("fix_items_1_3.py", "api_key"): 1,
    ("iso_grid_adapters.py", "api_key"): 1,
    ("routes/iso_isone.py", "api_key"): 2,
    ("routes/iso_spp.py", "api_key"): 1,
    ("scripts/dchub_discovery_patch_v2_1.py", "api_key"): 1,
    ("scripts/dchub_master_discovery_v2.py", "api_key"): 1,
    ("water_drought_routes.py", "api_key"): 1,
}


def test_the_scan_actually_reads_the_repo():
    """Floors, so a broken glob cannot make the rules below vacuously green."""
    _, files, fstrings = scan_repo()
    assert files >= _MIN_FILES, (
        f"scanned only {files} python files (floor {_MIN_FILES}) — this guard "
        "is not reading the repo any more, so its green means nothing")
    assert fstrings >= _MIN_FSTRINGS, (
        f"parsed only {fstrings} f-strings (floor {_MIN_FSTRINGS}) — the walk "
        "is not reaching the code it claims to check")


def test_no_ai_provider_key_is_ever_in_a_url():
    """ABSOLUTE. This is the surface the leak was observed on."""
    hits, _, _ = scan_repo()
    bad = [h for h in hits if _hit_is_ai_host(h)]
    assert not bad, (
        "AI provider credential in a URL query string — it will be written "
        "verbatim into the AI Gateway log:\n" + "\n".join(
            f"  {f}:{ln}  ?{p}=<interpolated>" for f, ln, p in bad))


def test_no_new_credential_in_url_outside_the_known_debt():
    """RATCHET. A new site fails — including a new one in a file already on
    the list, which a name-only allowlist would have waved through."""
    hits, _, _ = scan_repo()
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
    hits, _, _ = scan_repo()
    seen = collections.Counter((f, p) for f, _ln, p in hits)
    stale = [f"  {k[0]}  ?{k[1]}=  (recorded {n}, now {seen.get(k, 0)})"
             for k, n in _KNOWN_DEBT.items() if seen.get(k, 0) < n]
    assert not stale, (
        "recorded as known debt but no longer present that many times — "
        "tighten _KNOWN_DEBT:\n" + "\n".join(stale))
