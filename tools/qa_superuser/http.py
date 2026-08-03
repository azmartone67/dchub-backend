#!/usr/bin/env python3
"""Transport for the QA super-user: a self-identifying HTTP client and a
dependency-free MCP streamable-HTTP client.

**Why not the Node MCP SDK.** The known-good probe recipe requires the script to
live inside ``~/dchub-mcp-server/`` so ``node_modules`` resolves, which makes it
unrunnable from this repo's CI. The blocker for a plain client was believed to be
that "raw curl initialize 400s" — it does, but not because the protocol needs a
SDK. Streamable HTTP requires ``Accept: application/json, text/event-stream``;
with both media types the handshake returns 200 and a session id. Verified live
against https://dchub.cloud/mcp (server ``DC Hub Intelligence`` 2.11.1) before
this file was written. So the whole agent seat is reachable from stdlib Python.

**Self-identification is not politeness, it is correctness.** Shell #38 lost a
whole probe run because self-exclusion keyed on the ``platform`` tag, which the
MCP server OVERWRITES — the filter excluded nothing while the SQL read perfectly.
Exclusion must key on the User-Agent. Every request this tool makes carries
``QA_UA`` so the platform's own analytics can subtract us; if you add a new call
path, route it through here rather than hand-rolling a request.

Cloudflare also bot-blocks the default ``Python-urllib`` UA with a 403, so an
un-identified probe would produce a fleet of phantom failures.
"""
from __future__ import annotations

import json
import time

import requests

# ★ requests, not urllib. `regression_lint.py` blocks urllib.request.urlopen
# repo-wide and offers no exemption mechanism, and the house rule is explicit:
# never disable a guard to make your own change land. It reads better here
# anyway — requests does not raise on 4xx/5xx, and an HTTP error status is an
# OBSERVATION about the surface (frequently the exact thing under test) rather
# than an exception to unwrap.
#
# The one thing lost is stdlib-only portability. The runner installs requests
# explicitly (see qa-superuser.yml), and it is pinned in requirements.txt at the
# version prod runs.

# ★ The exclusion token. Shells and analytics MUST filter on this substring to
# keep probe traffic out of reach/usage numbers. Bump the version, never the name.
QA_UA_TOKEN = "dchub-qa-superuser"
QA_UA = f"{QA_UA_TOKEN}/1.0 (+https://github.com/azmartone67/dchub-backend qa-superuser)"

MCP_PROTOCOL = "2025-06-18"


def body_text(r) -> str:
    """Decode a response body as UTF-8 unless the server actually declared a charset.

    ★ requests follows RFC 2616 and falls back to **ISO-8859-1** for any ``text/*``
    response that carries no ``charset`` parameter. Our MCP endpoint answers
    ``text/event-stream`` bare, so ``r.text`` silently latin-1-mangles every
    multi-byte character in the payload.

    That is not cosmetic. A 342 KB ``tools/list`` body decoded this way died in
    ``json.loads`` with "Unterminated string" 276 KB in, and the probe reported
    BLIND — intermittently, and only for the largest responses, which is precisely
    the shape of a bug that gets written off as flakiness. urllib did not have this
    behaviour, so the migration to requests introduced it.
    """
    if "charset=" in (r.headers.get("Content-Type") or "").lower():
        return r.text
    return r.content.decode("utf-8", "replace")


class Unreachable(Exception):
    """The surface could not be observed. Callers MUST turn this into BLIND.

    Deliberately distinct from any assertion failure so that "we could not look"
    can never be miscoded as "we looked and it was wrong".
    """


def fetch(url: str, *, headers: dict | None = None, timeout: int = 30,
          method: str = "GET", data: bytes | None = None,
          retries: int = 2) -> tuple[int, dict, str]:
    """Fetch a URL. Returns (status, headers, body_text).

    A 4xx/5xx is RETURNED, not raised — an HTTP error is an observation about the
    surface and is often exactly what we are testing. Only a transport failure
    (DNS, TLS, timeout, connection reset) raises Unreachable, because that is the
    case where we genuinely learned nothing.
    """
    h = {"User-Agent": QA_UA}
    if headers:
        h.update(headers)
    last = ""
    for attempt in range(retries + 1):
        try:
            r = requests.request(method, url, headers=h, data=data,
                                 timeout=timeout, allow_redirects=True)
            # No raise_for_status(): a 4xx/5xx is the observation, not an error.
            return r.status_code, dict(r.headers), body_text(r)
        except requests.RequestException as e:
            # Transport-level only (DNS, TLS, timeout, reset) — we saw nothing.
            last = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise Unreachable(f"{url}: {last}")


def get_json(url: str, **kw) -> tuple[int, dict | list | None]:
    """GET and parse JSON. Unparseable body -> (status, None), never an exception.

    A non-JSON body from a JSON endpoint is itself a finding the caller may want
    to report, so it must not be swallowed as an error.
    """
    status, _hdrs, body = fetch(url, **kw)
    try:
        return status, json.loads(body)
    except Exception:  # noqa: BLE001
        return status, None


class MCPSession:
    """A live MCP session over streamable HTTP, from one seat.

    Usage mirrors what a real agent client does: initialize -> notifications/
    initialized -> tools/list -> tools/call. Nothing here is dchub-specific; the
    dchub-specific assertions live in probe_mcp.py.
    """

    def __init__(self, url: str, api_key: str | None = None, timeout: int = 60):
        self.url = url.strip().rstrip("/")   # ★ .strip() — a trailing newline in a
        # Railway env value became %0a and raised InvalidURL at urlopen() time,
        # twelve patched call sites ago. Treat any env value that becomes a URL as
        # untrusted text.
        self.api_key = (api_key or "").strip() or None
        self.timeout = timeout
        self.session_id: str | None = None
        self.server_info: dict = {}
        # ``instructions`` is a SIBLING of serverInfo in the initialize result,
        # not a field inside it. Reading it from server_info silently yields ""
        # and every check built on it degrades to "nothing to compare against"
        # — a false GAUGE where a real parity check was intended.
        self.instructions: str = ""
        self._mid = 0

    # -- wire ---------------------------------------------------------------
    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            # Both media types are mandatory for streamable HTTP. One alone 400s.
            "Accept": "application/json, text/event-stream",
            "User-Agent": QA_UA,
            "MCP-Protocol-Version": MCP_PROTOCOL,
        }
        if self.api_key:
            h["X-API-Key"] = self.api_key
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    @staticmethod
    def _parse(body: str):
        """Parse either an SSE frame or a bare JSON body.

        The server answers ``initialize`` as ``text/event-stream`` even for a
        single response, so a naive ``json.loads`` on the body fails and would
        look like a protocol error.
        """
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except Exception:  # noqa: BLE001
                    continue
        try:
            return json.loads(body)
        except Exception:  # noqa: BLE001
            return None

    def _rpc(self, method: str, params: dict, notify: bool = False):
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            self._mid += 1
            payload["id"] = self._mid
        try:
            r = requests.post(self.url, data=json.dumps(payload).encode(),
                              headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as e:
            raise Unreachable(f"mcp {method}: {type(e).__name__}: {e}") from e
        sid = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        return r.status_code, self._parse(body_text(r))

    # -- protocol -----------------------------------------------------------
    def open(self) -> "MCPSession":
        status, d = self._rpc("initialize", {
            "protocolVersion": MCP_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": QA_UA_TOKEN, "version": "1.0"},
        })
        if status != 200 or not d or "result" not in d:
            raise Unreachable(f"mcp initialize -> HTTP {status} {str(d)[:200]}")
        self.server_info = d["result"].get("serverInfo", {}) or {}
        self.instructions = d["result"].get("instructions") or ""
        self._rpc("notifications/initialized", {}, notify=True)
        return self

    def list_tools(self) -> list[dict]:
        status, d = self._rpc("tools/list", {})
        if status != 200 or not d:
            raise Unreachable(f"mcp tools/list -> HTTP {status}")
        return (d.get("result") or {}).get("tools", []) or []

    def call(self, name: str, arguments: dict) -> dict:
        """Call a tool and return the RAW result envelope.

        ★ Returns the whole envelope on purpose. Shell #49 proved an absence with
        a probe that searched only ``content[].text`` while the thing it was
        looking for lived in ``structuredContent`` — and #39 nearly debugged a
        working fix because ``for_your_human`` is nested by ``_collapseEnvELOPE``
        rather than top-level. Callers get everything and must say in ``basis``
        where they looked.
        """
        status, d = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if not d:
            raise Unreachable(f"mcp tools/call {name} -> HTTP {status}, unparseable")
        env = d.get("result") or {}
        if "error" in d:
            # A JSON-RPC error is a real observation about the tool, not a
            # transport failure — hand it back tagged so probes can assert on it.
            env = {"_jsonrpc_error": d["error"]}
        env["_http_status"] = status
        return env


def envelope_text(env: dict) -> str:
    """Concatenate the human-readable text blocks of a tool result."""
    out = []
    for c in env.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            out.append(c.get("text") or "")
    return "".join(out)


def envelope_all(env: dict) -> str:
    """The WHOLE envelope as one searchable string — text plus structured.

    Use this for "is X offered anywhere?" questions. Use the specific field for
    "is X in the right place?" questions, and say which in ``basis``.
    """
    return json.dumps(env, sort_keys=True, default=str)
