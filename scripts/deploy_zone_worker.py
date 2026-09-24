#!/usr/bin/env python3
"""Deploy worker.js to the Cloudflare ZONE worker `dchubapiproxy`, then prove it.

Run by .github/workflows/deploy-zone-worker.yml. HTTP goes through `requests`
(scripts/regression_lint.py bans urllib.request.urlopen repo-wide); everything
else is stdlib.

★ WHY THIS EXISTS. worker.js fronts dchub.cloud/mcp and all of api.dchub.cloud,
and until this script its only deploy was a human paste into the Cloudflare
dashboard — no merge performed it. So every worker.js merge sat unshipped until
someone remembered, and the brain's check_zone_worker_version_drift filed
`zone_worker_commit_not_pasted` for it, over and over.

★ WHAT IT NEVER DOES. It never runs `wrangler deploy`. A wrangler.toml deploy
re-declares bindings, routes and the compatibility date from a file, and
whatever the file omits is dropped from the edge in front of the whole API. The
secret_text bindings cannot be read back, so a dropped one is unrecoverable
from here. This script only ever writes through ONE endpoint:

    PUT /accounts/{acct}/workers/scripts/dchubapiproxy/content

which replaces the script's modules and leaves bindings, compatibility date,
cron schedules and routes alone. (`--method full` is the dispatch-only
fallback: a full script PUT whose bindings are `inherit` entries DERIVED FROM
THE LIVE SETTINGS at run time, never a hardcoded list. The list written down
on 2026-09-01 named 3 secrets, the brief for this script named 4, and live had
5 when it was written — HUBSPOT_API_KEY had been added since.)

★ WHEN IT REFUSES (exit 2). Before any write it reads the live script and
refuses unless the repo is strictly ahead AND the live bytes are a version of
worker.js some commit contains:

    live version ahead of the repo   production would be reverted (the
                                     detector's zone_worker_deployed_ahead_of_repo)
    same numeric version, other label, or same version with other content
                                     ambiguous; the header could not prove
                                     the deploy afterwards anyway
    live bytes in no commit          someone pasted code into the dashboard
                                     without committing it (or without a
                                     bump); deploying would erase it
    live script has != 1 module      a one-module upload would drop the rest

The "in no commit" test compares git BLOB IDS: every blob id worker.js has
ever had is readable from tree objects alone (`git log --raw`), so a blobless
clone answers it without downloading 50 copies of a 430 KB file.

★ AFTER A WRITE it verifies, and fails loudly (exit 5) on any miss:
  1. the script Cloudflare now holds is byte-identical to the repo's worker.js
  2. bindings (type+name), compatibility date/flags and cron schedules are
     unchanged from the pre-deploy snapshot
  3. X-DC-Worker-Version on BOTH api.dchub.cloud/api/v1/stats and
     dchub.cloud/mcp (cache-busted) reads the repo's WORKER_VERSION, twice in
     a row, within --verify-timeout seconds (default 300)

Every run writes its pre-deploy snapshot (the live script bytes, settings,
schedules, deployments) to --out, which the workflow uploads as the rollback
artifact, plus ROLLBACK.md naming the version to roll back to.

Exit codes: 0 ok · 1 usage/config · 2 refused · 3 token cannot read or write
the script · 4 upload rejected · 5 post-deploy verification failed.

Local read-only use (prints the plan, writes nothing to Cloudflare):
    CLOUDFLARE_API_TOKEN=… CLOUDFLARE_ACCOUNT_ID=… \\
      python3 scripts/deploy_zone_worker.py plan --out /tmp/zw
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass

import requests

SCRIPT_NAME = "dchubapiproxy"
MAIN_MODULE = "worker.js"
CF_API = "https://api.cloudflare.com/client/v4"

# Both hosts the zone worker fronts. /mcp is what the brain detector probes;
# api.dchub.cloud/api/v1/stats is the path the 4.9.73 paste was verified on.
# ★ NEVER dchub.cloud/api/*: that is the Pages worker, which stamps the SAME
# header name with its own 5.x version.
PROBE_URLS = (
    "https://api.dchub.cloud/api/v1/stats",
    "https://dchub.cloud/mcp",
)
# Cloudflare's browser-integrity check answers a Python client's default UA
# with 403 `error code: 1010`. The dchub/verify/probe tokens keep this out of demand
# metrics (server.mjs _INTERNAL_SELF_TAG and friends match on them).
PROBE_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like "
            "Gecko) Chrome/126.0 Safari/537.36 dchub-zone-deploy-verify-probe/1")

EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_TOKEN, EXIT_UPLOAD, EXIT_VERIFY = 0, 1, 2, 3, 4, 5

# Same strict shape as scripts/check_worker_version_bump.sh: the bump gate
# fails any worker.js change that does not keep this exact line, so a looser
# pattern here could only ever match something the gate would have refused.
_VERSION_RE = re.compile(rb"^const WORKER_VERSION = '([^']*)'", re.M)


class Refused(Exception):
    """A pre-write check said no. Carries the Decision."""


class TokenProblem(Exception):
    pass


# ── pure helpers ─────────────────────────────────────────────────────

def extract_version(src: bytes) -> str | None:
    m = _VERSION_RE.search(src)
    return m.group(1).decode("utf-8", "replace") if m else None


def version_core(v: str) -> tuple[int, ...]:
    """'4.9.70-capacity-search' -> (4, 9, 70). Must agree with
    routes/brain_consistency_radar._version_core — the detector and this
    deploy have to mean the same thing by "ahead" (a test pins that)."""
    parts: list[int] = []
    for tok in str(v).split("."):
        num = ""
        for ch in tok:
            if ch.isdigit():
                num += ch
            else:
                break
        if num == "":
            break
        parts.append(int(num))
    return tuple(parts)


def git_blob_id(data: bytes) -> str:
    """The id `git hash-object` gives these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def live_blob_candidates(live: bytes) -> set[str]:
    """Blob ids the live bytes could have had as a committed file. The dashboard
    editor has been seen to differ from the commit by trailing newline only
    (2026-09-01: 4 bytes), which is not uncommitted code."""
    body = live.replace(b"\r\n", b"\n")
    variants = {live, body, body.rstrip(b"\n"), body.rstrip(b"\n") + b"\n"}
    return {git_blob_id(v) for v in variants}


def parse_multipart(content_type: str, body: bytes) -> list[dict]:
    """Split a multipart/form-data body into [{name, filename, data}].

    Cloudflare's GET of a module script returns this shape; the trailing CRLF
    before each delimiter belongs to the delimiter, not to the file."""
    m = re.search(r'boundary="?([^";\s]+)"?', content_type or "")
    if not m:
        raise ValueError(f"no multipart boundary in content-type {content_type!r}")
    delim = b"--" + m.group(1).encode()
    parts = []
    for chunk in body.split(delim)[1:]:
        if chunk.startswith(b"--"):
            break  # closing delimiter
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        disp = next((ln for ln in head.decode("utf-8", "replace").split("\r\n")
                     if ln.lower().startswith("content-disposition:")), "")
        name = re.search(r'\bname="([^"]*)"', disp)
        fname = re.search(r'\bfilename="([^"]*)"', disp)
        parts.append({"name": name.group(1) if name else None,
                      "filename": fname.group(1) if fname else None,
                      "data": data})
    return parts


def build_multipart(metadata: dict, module_bytes: bytes) -> tuple[bytes, str]:
    """metadata part + one ES module part named AND filenamed worker.js.

    ★ Cloudflare resolves main_module by the part's FILENAME, not its field
    name: a filename of anything else uploads fine and then fails 10021 "No
    such module: worker.js" (measured 2026-09-01)."""
    boundary = "zw" + uuid.uuid4().hex
    b = boundary.encode()
    out = [
        b"--" + b + b"\r\n",
        b'Content-Disposition: form-data; name="metadata"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json.dumps(metadata).encode(), b"\r\n",
        b"--" + b + b"\r\n",
        f'Content-Disposition: form-data; name="{MAIN_MODULE}"; '
        f'filename="{MAIN_MODULE}"\r\n'.encode(),
        b"Content-Type: application/javascript+module\r\n\r\n",
        module_bytes, b"\r\n",
        b"--" + b + b"--\r\n",
    ]
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def full_put_metadata(settings: dict) -> dict:
    """Metadata for the `--method full` fallback, mirrored from LIVE settings.

    Every live binding goes back as `inherit`, so a secret added in the
    dashboard since anyone last wrote a list down is kept rather than dropped."""
    meta = {
        "main_module": MAIN_MODULE,
        "compatibility_date": settings.get("compatibility_date"),
        "compatibility_flags": settings.get("compatibility_flags") or [],
        "bindings": [{"type": "inherit", "name": b["name"]}
                     for b in settings.get("bindings") or []],
    }
    for key in ("logpush", "tail_consumers"):
        if settings.get(key) is not None:
            meta[key] = settings[key]
    return meta


def binding_shape(settings: dict) -> list[tuple[str, str]]:
    return sorted((b.get("type", ""), b.get("name", ""))
                  for b in settings.get("bindings") or [])


def settings_shape(settings: dict, schedules: list) -> dict:
    """The parts of the live configuration a content deploy must not move."""
    return {
        "bindings": binding_shape(settings),
        "compatibility_date": settings.get("compatibility_date"),
        "compatibility_flags": sorted(settings.get("compatibility_flags") or []),
        "schedules": sorted(s.get("cron", "") for s in schedules or []),
    }


def will_write(event: str, ref: str, dry_run_input: str, armed: str) -> tuple[bool, str]:
    """Is this run allowed to write to production at all?

    pull_request     never — a PR run is the read-only preflight
    workflow_dispatch only with dry_run=false, and only from refs/heads/main
                     (a real deploy of an unmerged branch is refused, not dried)
    push             only when vars.ZONE_WORKER_AUTO_DEPLOY is exactly "1"
    """
    if event == "workflow_dispatch":
        if str(dry_run_input).strip().lower() != "false":
            return False, "workflow_dispatch with dry_run=true"
        if ref != "refs/heads/main":
            raise Refused(Decision(
                "refuse", "dispatch_not_from_main",
                f"A real deploy was requested from {ref!r}. Production only "
                "ever gets main's worker.js; dispatch from main, or keep "
                "dry_run=true to preview a branch."))
        return True, "workflow_dispatch from main with dry_run=false"
    if event == "push":
        if ref != "refs/heads/main":
            return False, f"push to {ref}, not main"
        if str(armed).strip() != "1":
            return False, ("vars.ZONE_WORKER_AUTO_DEPLOY is not '1' — merges "
                           "preview only until the owner arms it")
        return True, "push to main with vars.ZONE_WORKER_AUTO_DEPLOY=1"
    return False, f"{event or 'local'} runs are read-only"


@dataclass
class Decision:
    action: str  # deploy | noop | refuse
    code: str
    reason: str


def decide(*, repo_version: str | None, repo_blob: str,
           live_version: str | None, live_blobs: set[str],
           history_blobs: set[str], live_module_count: int,
           live_entrypoint: str | None = MAIN_MODULE) -> Decision:
    if live_entrypoint not in (None, MAIN_MODULE):
        return Decision("refuse", "live_entrypoint_differs",
                        f"The live script's entry module is {live_entrypoint!r}, "
                        f"not {MAIN_MODULE!r}. Uploading worker.js as the main "
                        "module would change what runs; inspect it first.")
    if live_module_count != 1:
        return Decision("refuse", "live_not_single_module",
                        f"The live script has {live_module_count} modules. This "
                        "deploy uploads worker.js alone, which would drop the "
                        "others. Inspect the live script before deploying.")
    if not repo_version:
        return Decision("refuse", "repo_version_missing",
                        "worker.js has no `const WORKER_VERSION = '…'` line, so "
                        "the deploy could never be verified by its header.")
    if repo_blob in live_blobs:
        return Decision("noop", "already_live",
                        f"Cloudflare already runs this exact worker.js ({repo_version}).")
    if not live_version:
        return Decision("refuse", "live_version_unreadable",
                        "The live script declares no WORKER_VERSION this "
                        "script can read, so 'ahead' cannot be judged.")
    if live_version == repo_version:
        return Decision("refuse", "same_version_different_content",
                        f"Live and repo both declare {repo_version!r} but the "
                        "bytes differ. Either the dashboard copy was edited "
                        "without a bump or the repo was; bump WORKER_VERSION "
                        "in a commit that contains the intended code.")
    rc, lc = version_core(repo_version), version_core(live_version)
    if not rc or not lc:
        return Decision("refuse", "version_unparseable",
                        f"Cannot order {live_version!r} (live) against "
                        f"{repo_version!r} (repo).")
    if lc > rc:
        return Decision("refuse", "live_ahead_of_repo",
                        f"Live runs {live_version!r}, AHEAD of the repo's "
                        f"{repo_version!r}. Deploying would revert production "
                        "(zone_worker_deployed_ahead_of_repo). Commit the live "
                        "script first; to roll back on purpose, commit the old "
                        "code under a NEW, higher version.")
    if lc == rc:
        return Decision("refuse", "suffix_mismatch",
                        f"Live {live_version!r} and repo {repo_version!r} share "
                        "a numeric version with different labels.")
    if not (live_blobs & history_blobs):
        return Decision("refuse", "live_content_not_in_any_commit",
                        f"Live declares {live_version!r} but its bytes match no "
                        "version of worker.js in this repo's history — "
                        "something was pasted into the dashboard and never "
                        "committed. Deploying would erase it. Commit the live "
                        "script (it is in this run's artifact), then re-run.")
    return Decision("deploy", "repo_ahead",
                    f"Deploy {repo_version} over live {live_version}.")


# ── I/O ──────────────────────────────────────────────────────────────

class Http:
    """(status, lowercased headers, body bytes) for any request; status 0 when
    nothing answered. Tests swap in a fake with the same signature."""

    def request(self, method: str, url: str, headers: dict | None = None,
                body: bytes | None = None, timeout: float = 30):
        try:
            r = requests.request(method, url, headers=headers or {}, data=body,
                                 timeout=timeout, allow_redirects=False)
        except requests.RequestException as e:
            return 0, {}, str(e).encode()
        return r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content


def git_history_blobs(repo_dir: str, path: str = MAIN_MODULE) -> set[str]:
    """Every blob id `path` has had in HEAD's history. Reads trees only, so it
    works in a `--filter=blob:none` clone without fetching a single blob.
    --no-renames matters: rename detection would need the blob contents."""
    out = subprocess.run(
        ["git", "-C", repo_dir, "log", "--format=", "--raw", "--no-abbrev",
         "--no-renames", "HEAD", "--", path],
        check=True, capture_output=True, text=True).stdout
    blobs: set[str] = set()
    for line in out.splitlines():
        if line.startswith(":"):
            fields = line.split("\t", 1)[0].split()
            blobs.update(b for b in fields[2:4] if b.strip("0"))
    return blobs


def _cf_errors(body: bytes) -> str:
    try:
        d = json.loads(body or b"{}")
        errs = d.get("errors") or []
        return "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errs) or body[:300].decode("utf-8", "replace")
    except ValueError:
        return body[:300].decode("utf-8", "replace")


class Cloudflare:
    def __init__(self, http: Http, token: str, account: str):
        self.http, self.token = http, token
        self.base = f"{CF_API}/accounts/{account}/workers/scripts/{SCRIPT_NAME}"

    def _h(self, extra: dict | None = None) -> dict:
        h = {"Authorization": f"Bearer {self.token}"}
        h.update(extra or {})
        return h

    def get(self, suffix: str):
        return self.http.request("GET", self.base + suffix, self._h())

    def get_json(self, suffix: str, what: str) -> dict:
        status, _, body = self.get(suffix)
        if status in (401, 403):
            raise TokenProblem(
                f"GET {what} answered {status} ({_cf_errors(body)}). The token "
                "needs Account → Workers Scripts → Read (and Edit to deploy).")
        if status != 200:
            raise TokenProblem(f"GET {what} answered {status}: {_cf_errors(body)}")
        return json.loads(body).get("result") or {}

    def read_script(self) -> tuple[list[dict], dict]:
        """(parts, headers) of the live script. ★ This is the token check:
        the first thing any run does, and read-only."""
        status, headers, body = self.get("/content/v2")
        if status in (401, 403):
            raise TokenProblem(
                f"GET /workers/scripts/{SCRIPT_NAME}/content/v2 answered {status} "
                f"({_cf_errors(body)}). CLOUDFLARE_API_TOKEN is missing "
                "Account → Workers Scripts → Read on this account (deploying "
                "also needs Workers Scripts → Edit). Nothing was changed.")
        if status == 404:
            raise TokenProblem(
                f"Script {SCRIPT_NAME!r} not found on this account (404: "
                f"{_cf_errors(body)}). Check CLOUDFLARE_ACCOUNT_ID.")
        if status != 200:
            raise TokenProblem(f"GET script answered {status}: {_cf_errors(body)}")
        return parse_multipart(headers.get("content-type", ""), body), headers

    def put_content(self, module: bytes):
        body, ctype = build_multipart({"main_module": MAIN_MODULE}, module)
        return self.http.request("PUT", self.base + "/content",
                                 self._h({"Content-Type": ctype}), body, timeout=120)

    def put_full(self, module: bytes, settings: dict):
        body, ctype = build_multipart(full_put_metadata(settings), module)
        return self.http.request("PUT", self.base,
                                 self._h({"Content-Type": ctype}), body, timeout=120)


def probe_versions(http: Http, now: float) -> dict[str, str | None]:
    seen = {}
    for url in PROBE_URLS:
        _, headers, _ = http.request(
            "GET", f"{url}?_={int(now * 1000)}",
            {"User-Agent": PROBE_UA, "Cache-Control": "no-cache"}, timeout=20)
        seen[url] = headers.get("x-dc-worker-version")
    return seen


def wait_for_header(http: Http, want: str, timeout: float, interval: float,
                    sleep=time.sleep, clock=time.time, log=print) -> tuple[bool, dict]:
    """Poll every PROBE_URL until each reads `want` on two consecutive rounds."""
    deadline = clock() + timeout
    streak = {u: 0 for u in PROBE_URLS}
    seen: dict = {}
    while True:
        seen = probe_versions(http, clock())
        for u, v in seen.items():
            streak[u] = streak[u] + 1 if v == want else 0
        log(f"  header: " + ", ".join(f"{u.split('//')[1]}={v}" for u, v in seen.items()))
        if all(n >= 2 for n in streak.values()):
            return True, seen
        if clock() >= deadline:
            return False, seen
        sleep(interval)


# ── the run ──────────────────────────────────────────────────────────

def _write(out: str, name: str, data) -> None:
    os.makedirs(out, exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(os.path.join(out, name), mode) as fh:
        fh.write(data if isinstance(data, (bytes, str)) else json.dumps(data, indent=2, sort_keys=True))


def _gh_output(**kv) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as fh:
            for k, v in kv.items():
                fh.write(f"{k}={v}\n")


def _summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(text + "\n")


def _rollback_md(account_hint: str, prev_versions: list, live_version: str | None) -> str:
    vid = prev_versions[0]["version_id"] if prev_versions else "<see deployments.json>"
    return f"""# Rolling back dchubapiproxy

Pre-deploy live version: `{live_version}` — Cloudflare version id `{vid}`.

Fastest (restores that exact script AND its settings): Cloudflare dashboard →
Workers & Pages → {SCRIPT_NAME} → Deployments → version `{vid}` → Rollback.

Same thing by API (per Cloudflare's Deployments API):

    curl -X POST -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \\
      -H "Content-Type: application/json" \\
      "{CF_API}/accounts/{account_hint}/workers/scripts/{SCRIPT_NAME}/deployments" \\
      --data '{{"strategy":"percentage","versions":[{{"version_id":"{vid}","percentage":100}}]}}'

The pre-deploy script bytes are `predeploy_worker.js` in this artifact.
Then verify: `curl -sI "https://api.dchub.cloud/api/v1/stats?_=$(date +%s)" | grep -i x-dc-worker-version`
"""


def run(mode: str, *, repo_dir: str, out: str, http: Http, token: str,
        account: str, method: str = "content", verify_timeout: float = 300,
        verify_interval: float = 10, history_blobs: set[str] | None = None,
        sleep=time.sleep, clock=time.time, log=print) -> int:
    with open(os.path.join(repo_dir, MAIN_MODULE), "rb") as fh:
        repo_bytes = fh.read()
    repo_version = extract_version(repo_bytes)
    repo_blob = git_blob_id(repo_bytes)
    cf = Cloudflare(http, token, account)

    # 1. Token check + snapshot. Read-only; every run does it, PRs included.
    try:
        parts, script_headers = cf.read_script()
        settings = cf.get_json("/settings", "settings")
        schedules = cf.get_json("/schedules", "schedules").get("schedules") or []
    except TokenProblem as e:
        log(f"::error::{e}")
        _summary(f"### ❌ zone worker: token cannot read the script\n\n{e}")
        _gh_output(action="token_problem")
        return EXIT_TOKEN
    try:
        deployments = cf.get_json("/deployments", "deployments").get("deployments") or []
    except TokenProblem as e:  # rollback id is a convenience; the bytes are the artifact
        log(f"::warning::{e}")
        deployments = []
    # author_email stays out of the artifact: this repo is public.
    deployments = [{k: d.get(k) for k in ("id", "created_on", "source", "versions")}
                   for d in deployments]

    modules = [p for p in parts if p.get("filename") or p.get("name")]
    live_bytes = modules[0]["data"] if len(modules) == 1 else b""
    live_version = extract_version(live_bytes) if live_bytes else None
    live_blobs = live_blob_candidates(live_bytes) if live_bytes else set()
    if history_blobs is None:
        history_blobs = git_history_blobs(repo_dir)
    decision = decide(repo_version=repo_version, repo_blob=repo_blob,
                      live_version=live_version, live_blobs=live_blobs,
                      history_blobs=history_blobs, live_module_count=len(modules),
                      live_entrypoint=script_headers.get("cf-entrypoint"))

    prev_versions = (deployments[0].get("versions") or []) if deployments else []
    before = settings_shape(settings, schedules)
    _write(out, "predeploy_worker.js", live_bytes)
    _write(out, "predeploy_settings.json", {"shape": before,
                                            "compatibility_date": settings.get("compatibility_date")})
    _write(out, "predeploy_deployments.json", deployments)
    _write(out, "ROLLBACK.md", _rollback_md("$CLOUDFLARE_ACCOUNT_ID", prev_versions, live_version))
    plan = {"mode": mode, "method": method, "decision": asdict(decision),
            "repo_version": repo_version, "repo_blob": repo_blob,
            "live_version": live_version, "live_blob": git_blob_id(live_bytes),
            "live_blob_in_history": bool(live_blobs & history_blobs),
            "history_blob_count": len(history_blobs),
            "rollback_version_ids": [v.get("version_id") for v in prev_versions],
            "config": before}
    _write(out, "plan.json", plan)
    _gh_output(action=decision.action, repo_version=repo_version or "",
               live_version=live_version or "")

    log(f"repo  worker.js {repo_version}  blob {repo_blob[:12]}")
    log(f"live  worker.js {live_version}  blob {git_blob_id(live_bytes)[:12]}"
        f"  (committed: {plan['live_blob_in_history']}, {len(history_blobs)} historical blobs)")
    log(f"live  bindings {', '.join(n for _, n in before['bindings'])}; "
        f"compat {before['compatibility_date']}; crons {before['schedules']}")
    log(f"=> {decision.action.upper()} [{decision.code}] {decision.reason}")
    _summary(f"### zone worker `{SCRIPT_NAME}` — {mode}\n\n"
             f"| | version |\n|---|---|\n| repo | `{repo_version}` |\n| live | `{live_version}` |\n\n"
             f"**{decision.action}** (`{decision.code}`): {decision.reason}")

    if decision.action == "refuse":
        log(f"::error::Refusing: {decision.reason}")
        return EXIT_REFUSED
    if mode == "plan" or decision.action == "noop":
        return EXIT_OK

    # 2. The one write.
    log(f"PUT {'full script' if method == 'full' else '/content'} ({len(repo_bytes)} bytes)")
    if method == "full":
        status, _, body = cf.put_full(repo_bytes, settings)
    else:
        status, _, body = cf.put_content(repo_bytes)
    if status in (401, 403):
        log(f"::error::Upload answered {status} ({_cf_errors(body)}). The token "
            "can read the script but lacks Account → Workers Scripts → Edit. "
            "Nothing was changed.")
        return EXIT_TOKEN
    if status != 200:
        log(f"::error::Upload answered {status}: {_cf_errors(body)}. A rejected "
            "upload changes nothing; production is still on "
            f"{live_version}.")
        return EXIT_UPLOAD

    # 3. Verify: bytes, configuration, then the header on both hosts.
    failures = []
    try:
        after_parts, _ = cf.read_script()
        after_settings = cf.get_json("/settings", "settings")
        after_sched = cf.get_json("/schedules", "schedules").get("schedules") or []
    except TokenProblem as e:
        failures.append(f"could not re-read the script after upload: {e}")
        after_parts, after_settings, after_sched = [], settings, schedules
    if after_parts:
        if len(after_parts) != 1 or git_blob_id(after_parts[0]["data"]) != repo_blob:
            failures.append("the script Cloudflare holds after the upload is not "
                            "byte-identical to the repo's worker.js")
    after = settings_shape(after_settings, after_sched)
    for key in before:
        if before[key] != after[key]:
            failures.append(f"{key} CHANGED: {before[key]} -> {after[key]}")
    _write(out, "postdeploy_settings.json", {"shape": after})

    ok, seen = wait_for_header(http, repo_version, verify_timeout, verify_interval,
                               sleep=sleep, clock=clock, log=log)
    if not ok:
        failures.append(f"X-DC-Worker-Version did not read {repo_version!r} on "
                        f"every probe within {int(verify_timeout)}s: {seen}")
    if failures:
        for f in failures:
            log(f"::error::{f}")
        log("::error::Deploy NOT verified. Rollback: see ROLLBACK.md in this "
            f"run's artifact (pre-deploy version ids {plan['rollback_version_ids']}).")
        _summary("#### ❌ NOT verified\n\n" + "\n".join(f"- {f}" for f in failures)
                 + "\n\nRollback instructions are in the run artifact (ROLLBACK.md).")
        return EXIT_VERIFY
    log(f"VERIFIED: {repo_version} live on {', '.join(PROBE_URLS)}; "
        "bytes, bindings, compatibility and crons unchanged.")
    _summary(f"#### ✅ deployed and verified `{repo_version}`")
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=("plan", "deploy", "should-write"))
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--out", default="zone-worker-deploy")
    ap.add_argument("--method", choices=("content", "full"), default="content")
    ap.add_argument("--verify-timeout", type=float, default=300)
    ap.add_argument("--verify-interval", type=float, default=10)
    # should-write inputs (the workflow passes github context through env)
    ap.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    ap.add_argument("--ref", default=os.environ.get("GITHUB_REF", ""))
    ap.add_argument("--dry-run-input", default="true")
    ap.add_argument("--armed", default="")
    a = ap.parse_args(argv)

    if a.mode == "should-write":
        try:
            ok, why = will_write(a.event, a.ref, a.dry_run_input, a.armed)
        except Refused as e:
            print(f"::error::{e.args[0].reason}")
            _gh_output(write="false")
            return EXIT_REFUSED
        print(f"write={str(ok).lower()}: {why}")
        _gh_output(write=str(ok).lower())
        return EXIT_OK

    if a.mode == "deploy":
        # Belt to the workflow's braces: the deploy mode re-derives permission
        # itself, so a mis-wired `if:` cannot turn a PR run into a write.
        try:
            ok, why = will_write(a.event, a.ref, a.dry_run_input, a.armed)
        except Refused as e:
            print(f"::error::{e.args[0].reason}")
            return EXIT_REFUSED
        if not ok:
            print(f"::error::deploy mode refused: {why}")
            return EXIT_REFUSED

    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not token or not account:
        print("::error::CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID must both be set.")
        return EXIT_USAGE
    return run(a.mode, repo_dir=a.repo_dir, out=a.out, http=Http(), token=token,
               account=account, method=a.method, verify_timeout=a.verify_timeout,
               verify_interval=a.verify_interval)


if __name__ == "__main__":
    sys.exit(main())
