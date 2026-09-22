"""worker.js KV response cache: only a credential-free request may fill it.

A KV entry written for a URL is served to every later caller of that URL (STEP 1
fresh, STEP 3 stale), and its key drops the credential query params. So a write
is safe only when the request carried no caller credential AND the origin allows
a shared copy (Cache-Control is neither private nor no-store).

These tests EXECUTE the shipped code under node — the predicates and each write
site's own statements, extracted verbatim — rather than restating a condition
here, so they fail if the behaviour regresses however it is spelled.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(REPO, "worker.js")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available on this host"
)

PREDICATES = (
    "const KV_KEY_STRIP_PARAMS",
    "const CREDENTIAL_HEADER_NAME",
    "const CREDENTIAL_COOKIE_NAME",
    "function carriesCallerCredential(",
    "function originAllowsSharedStore(",
)

# Headers the origin reads that carry no caller identity. Everything else it
# reads must be detected by carriesCallerCredential — add a name here only after
# checking the origin does not resolve a caller, tier or entitlement from it.
BENIGN_HEADERS = {
    "accept", "cf-connecting-ip", "cf-ipcountry", "cf-ipregioncode",
    "cf-region-code", "cf-verified-bot", "content-encoding", "dnt",
    "if-none-match", "origin", "referer", "referrer", "sec-fetch-user",
    "user-agent", "x-agent-name", "x-client-name", "x-content-gzip",
    "x-country", "x-dc-probe", "x-forwarded-for", "x-mcp-platform",
    "x-force",  # a run flag on a route that has already checked admin auth
    # Webhook signatures: POST-only routes, and the KV cache is GET-only.
    "stripe-signature", "svix-id", "svix-signature", "svix-timestamp",
}
BENIGN_COOKIES = set()


def _src():
    with open(WORKER, encoding="utf-8") as f:
        return f.read()


def _decl(src, start):
    """A top-level one-line const, or a function through its closing '}'."""
    lines = src.splitlines()
    try:
        i = next(n for n, ln in enumerate(lines) if ln.startswith(start))
    except StopIteration:
        raise AssertionError(f"worker.js: no line starts with {start!r}")
    if lines[i].rstrip().endswith(";"):
        return lines[i]
    j = next(n for n in range(i + 1, len(lines)) if lines[n].rstrip() == "}")
    return "\n".join(lines[i:j + 1])


def _predicates(src):
    return "\n".join(_decl(src, p) for p in PREDICATES)


def _node(js):
    out = subprocess.run(["node", "--input-type=module", "-e", js],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"node failed: {out.stderr}"
    return json.loads(out.stdout)


def _credential(cases):
    """Run the REAL carriesCallerCredential over [{headers, query}] cases."""
    js = _predicates(_src()) + f"""
const cases = {json.dumps(cases)};
console.log(JSON.stringify(cases.map((c) => {{
  const url = new URL('https://api.dchub.cloud/api/v1/facilities' + (c.query || ''));
  return carriesCallerCredential(new Request(url, {{ headers: c.headers || {{}} }}), url);
}})));
"""
    return _node(js)


CREDENTIALED = [
    {"headers": {"X-API-Key": "k"}},
    {"query": "?api_key=k"},
    {"headers": {"Authorization": "Bearer t"}},
    {"headers": {"Authorization": "t"}},          # origin strips "Bearer " and decodes the rest
    {"headers": {"Authorization": "Basic dTpw"}},
    {"headers": {"X-Admin-Key": "k"}},
    {"headers": {"X-Internal-Key": "k"}},
    {"headers": {"X-Payment": "p"}},
    {"headers": {"Mcp-Session-Id": "s"}},
    {"query": "?token=t"},
    {"query": "?admin_key=k"},
    {"query": "?key=k"},
    {"query": "?session_id=s"},
    {"headers": {"Cookie": "dchub_token=t"}},
    {"headers": {"Cookie": "_ga=GA1.2.3; dchub_refresh=r"}},
    {"headers": {"Cookie": "auth_token=t"}},
    {"headers": {"Cookie": "token=t"}},
    {"headers": {"Cookie": "dchub_admin=1"}},
    {"headers": {"Cookie": "dch_sid=s"}},
]

# Shaped like real anonymous traffic, so a predicate that matches everything
# (and would switch the cache off) fails here.
CREDENTIAL_FREE = [
    {},
    {"query": "?limit=5&state=VA"},
    {"query": "?api_key="},                       # empty: nothing to resolve
    {"headers": {
        "Accept": "application/json", "Accept-Language": "en-US",
        "User-Agent": "Mozilla/5.0", "Referer": "https://dchub.cloud/",
        "Origin": "https://dchub.cloud", "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site", "Cache-Control": "no-cache",
        "If-None-Match": '"abc"', "CF-Connecting-IP": "203.0.113.9",
        "X-Forwarded-For": "203.0.113.9", "DNT": "1",
        "X-MCP-Platform": "claude",
    }},
    {"headers": {"Cookie": "_ga=GA1.2.3; __cf_bm=x; cf_clearance=y"}},
]


def test_every_credential_channel_is_detected():
    got = _credential(CREDENTIALED)
    missed = [c for c, hit in zip(CREDENTIALED, got) if not hit]
    assert not missed, f"carriesCallerCredential missed: {missed}"


def test_credential_free_traffic_is_not_mistaken_for_a_caller():
    got = _credential(CREDENTIAL_FREE)
    flagged = [c for c, hit in zip(CREDENTIAL_FREE, got) if hit]
    assert not flagged, f"flagged as credentialed (would bypass the cache): {flagged}"


def _origin_reads(kind):
    """Header or cookie names the origin reads, from every non-test .py file."""
    pat = re.compile(r"request\.%s\.get\(\s*['\"]([A-Za-z0-9_.-]+)['\"]" % kind)
    names = set()
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs
                   if not d.startswith(".") and d not in {"tests", "node_modules", "venv"}]
        for fn in files:
            if fn.endswith(".py"):
                with open(os.path.join(root, fn), encoding="utf-8", errors="ignore") as f:
                    names.update(pat.findall(f.read()))
    return names


def test_every_header_and_cookie_the_origin_reads_is_detected_or_benign():
    headers = {n.lower() for n in _origin_reads("headers")}
    cookies = _origin_reads("cookies")
    # Floors: a scan that silently finds nothing would pass everything.
    assert len(headers) >= 30, f"header scan found only {sorted(headers)}"
    assert len(cookies) >= 10, f"cookie scan found only {sorted(cookies)}"
    h_todo = sorted(headers - BENIGN_HEADERS)
    c_todo = sorted(cookies - BENIGN_COOKIES)
    got = _credential([{"headers": {h: "x"}} for h in h_todo]
                      + [{"headers": {"Cookie": f"{c}=x"}} for c in c_todo])
    missed = [n for n, hit in zip(h_todo + [f"cookie:{c}" for c in c_todo], got) if not hit]
    assert not missed, (
        f"the origin reads {missed} but carriesCallerCredential does not treat "
        "them as a credential, so a request carrying one could fill the shared "
        "KV entry. Widen CREDENTIAL_HEADER_NAME / CREDENTIAL_COOKIE_NAME, or — "
        "only if it carries no caller identity — add it to BENIGN_* here.")


def test_origin_cache_control_decides_whether_a_shared_copy_is_allowed():
    ccs = ["", "public, max-age=600", "max-age=300, s-maxage=900",
           "private", "PRIVATE, max-age=0, must-revalidate", "private, no-store",
           "no-store", "no-store, no-cache, must-revalidate, max-age=0"]
    js = _predicates(_src()) + f"""
console.log(JSON.stringify({json.dumps(ccs)}.map((cc) => originAllowsSharedStore(
  new Response('x', {{ headers: cc ? {{ 'Cache-Control': cc }} : {{}} }})))));
"""
    assert _node(js) == [True, True, True, False, False, False, False, False]


# ── the write sites, executed ────────────────────────────────────────────────

def _nth_write_site(src, n):
    """The nth `let cacheClone = null;` through its `return result;`, verbatim."""
    lines = src.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip() == "let cacheClone = null;"]
    assert len(starts) == 2, (
        f"expected the STEP 2 and STEP 2.5 KV write sites, found {len(starts)} — "
        "a new write site must be gated like these and covered here")
    i = starts[n]
    j = next(k for k in range(i + 1, len(lines)) if lines[k].strip() == "return result;")
    return "\n".join(lines[i:j + 1])


def _has_credential_line(src):
    hits = [ln.strip() for ln in src.splitlines() if ln.strip().startswith("const hasCredential =")]
    assert len(hits) == 1, f"expected one hasCredential definition, found {hits}"
    return hits[0]


# (headers, query, origin Cache-Control, expect a KV write)
WRITE_CASES = [
    ({}, "", "public, max-age=600", True),        # control: the harness sees writes
    ({}, "", "", True),
    ({"X-API-Key": "k"}, "", "public, max-age=600", False),
    ({"Authorization": "Bearer t"}, "", "public, max-age=600", False),
    ({"Cookie": "dchub_token=t"}, "", "public, max-age=600", False),
    ({"X-Admin-Key": "k"}, "", "public, max-age=600", False),
    ({}, "&admin_key=k", "public, max-age=600", False),
    ({}, "", "private, max-age=0, must-revalidate", False),
    ({}, "", "no-store, no-cache, must-revalidate, max-age=0", False),
]


def _run_write_site(n, resp_var):
    src = _src()
    js = _predicates(src) + f"""
const env = {{ DCHUB_CACHE: {{}} }};
const pathname = '/api/v1/facilities';
const isGet = true;
const tier = {{ browserMaxAge: 600, edgeTtl: 900, kvFreshTtl: 900, kvStaleTtl: 86400 }};
const _pkc = false;
const startTime = Date.now();
const attempts = 1;
const WORKER_VERSION = 'test';
const addCORS = (r) => r;
const cacheControlFor = () => null;
const assetCachePut = () => {{}};
const kvIsCacheable = () => true;
const kvCacheKey = (u) => 'kv:' + u;
let stored = 0;
const kvCacheStore = async () => {{ stored++; }};

async function site(headers, query, cc) {{
  const url = new URL('https://api.dchub.cloud/api/v1/facilities?limit=1' + query);
  const request = new Request(url, {{ headers }});
  {_has_credential_line(src)}
  const {resp_var} = new Response('{{"ok":1}}', {{ status: 200,
    headers: Object.assign({{ 'content-type': 'application/json' }}, cc ? {{ 'Cache-Control': cc }} : {{}}) }});
  const waits = [];
  const ctx = {{ waitUntil: (p) => waits.push(p) }};
  const before = stored;
  await (async () => {{
{_nth_write_site(src, n)}
  }})();
  await Promise.all(waits);
  return stored > before;
}}

const cases = {json.dumps([[h, q, cc] for h, q, cc, _ in WRITE_CASES])};
const out = [];
for (const [h, q, cc] of cases) out.push(await site(h, q, cc));
console.log(JSON.stringify(out));
"""
    return _node(js)


@pytest.mark.parametrize("n,resp_var,label", [
    (0, "resp", "STEP 2 (Railway proxy)"),
    (1, "renderResp", "STEP 2.5 (Render failover)"),
])
def test_write_site_fills_kv_only_for_credential_free_shareable_responses(n, resp_var, label):
    got = _run_write_site(n, resp_var)
    want = [w for *_, w in WRITE_CASES]
    wrong = [(c[:3], g) for c, g in zip(WRITE_CASES, got) if g != c[3]]
    assert got == want, f"{label}: KV write decision wrong for {wrong}"


def test_flask_html_lane_write_is_gated_the_same_way():
    src = _src()
    lines = src.splitlines()
    i = next(n for n, ln in enumerate(lines) if "buf.byteLength < 2_000_000" in ln)
    j = next(k for k in range(i, len(lines)) if lines[k].rstrip().endswith(") {"))
    cond = " ".join(ln.strip() for ln in lines[i:j + 1])
    assert cond.startswith("if (") and cond.endswith(") {"), cond
    cond = cond[len("if ("):-len(") {")]
    js = _predicates(src) + f"""
const env = {{ DCHUB_CACHE: {{}} }};
const buf = new ArrayBuffer(10);
const cases = {json.dumps([[h, q, cc] for h, q, cc, _ in WRITE_CASES])};
console.log(JSON.stringify(cases.map(([headers, query, cc]) => {{
  const url = new URL('https://api.dchub.cloud/facility/x?a=1' + query);
  const request = new Request(url, {{ headers }});
  const seoResp = new Response('<html></html>', {{ status: 200,
    headers: cc ? {{ 'Cache-Control': cc }} : {{}} }});
  return !!({cond});
}})));
"""
    got = _node(js)
    want = [w for *_, w in WRITE_CASES]
    assert got == want, f"Flask HTML lane KV write decision: got {got}, want {want}"


def test_every_kv_cache_store_call_site_is_accounted_for():
    """Seeder (keyless by construction), Flask HTML lane, STEP 2, STEP 2.5."""
    calls = [ln.strip() for ln in _src().splitlines()
             if "kvCacheStore(" in ln and "function kvCacheStore(" not in ln]
    assert len(calls) == 4, (
        f"kvCacheStore call sites changed ({len(calls)}): {calls}. A new site "
        "must be gated by carriesCallerCredential + originAllowsSharedStore "
        "and covered in this file.")
