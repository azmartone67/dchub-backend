"""httpx Auth implementation that handles DC Hub trial-key flow."""
from __future__ import annotations

import httpx


class DCHubTrialAuth(httpx.Auth):
    """Auto-grab DC Hub trial keys from HTTP 402 responses and retry.

    On the first 402 from a DC Hub endpoint, this auth handler:
      1. Reads `X-Trial-Key` header (RFC-8288-style delivery DC Hub uses)
      2. Saves it in memory for the session
      3. Retries the same request with `X-API-Key: <trial_key>`
      4. Returns the successful response

    Subsequent requests in the same session use the saved key directly
    so there's only ever one 402 per process.

    Optional kwargs:
      cache_path:  if set, persist the trial key to this file path
                   between processes. Useful for CLI tools where each
                   invocation is a fresh Python process.
      verbose:     if True, prints a one-line notice when the trial
                   key is captured. Off by default.

    Example:

        client = httpx.Client(auth=DCHubTrialAuth(verbose=True))
        client.get("https://dchub.cloud/api/v1/transactions/export.csv")
        # → first call returns 402 with X-Trial-Key header
        # → auth handler reads it, retries with X-API-Key
        # → second call returns 200 with the CSV
        # → all later calls in this session use the same key (no 402)
    """

    requires_request_body = False
    requires_response_body = False

    def __init__(self, cache_path: str | None = None, verbose: bool = False):
        self.api_key: str | None = None
        self.cache_path = cache_path
        self.verbose = verbose
        # Load any persisted key
        if cache_path:
            try:
                import pathlib
                p = pathlib.Path(cache_path)
                if p.exists():
                    key = p.read_text().strip()
                    if key.startswith("dch_"):
                        self.api_key = key
            except Exception:
                pass  # cache miss is fine; we'll re-mint on first 402

    def _save_key(self, key: str) -> None:
        self.api_key = key
        if self.verbose:
            print(f"[dchub_mcp_helper] captured trial key {key[:24]}...")
        if self.cache_path:
            try:
                import pathlib
                pathlib.Path(self.cache_path).write_text(key)
            except Exception:
                pass

    def auth_flow(self, request: httpx.Request):
        if self.api_key:
            request.headers["X-API-Key"] = self.api_key
        response = yield request

        # ── Path A: standard 402 paywall (REST + MCP gates) ──
        if response.status_code == 402:
            trial_key = response.headers.get("x-trial-key")
            if not trial_key:
                # Body fallback for MCP gatekeeper JSON-RPC envelope
                try:
                    body = response.json()
                    trial_key = body.get("auto_trial_key")
                except Exception:
                    trial_key = None
            if trial_key:
                self._save_key(trial_key)
                request.headers["X-API-Key"] = trial_key
                yield request  # retry with the key
            return

        # ── Path B (v0.1.1): soft-paywall HTTP 200 + `_gated: true` ──
        # DC Hub's soft-paywall pattern returns 200 with a truncated
        # list + _gated:true marker instead of 402. The body shape is:
        #   {"scores": [...10 of 285...], "count": 10,
        #    "_gated": true, "_total_available": 285, ...}
        # Without this branch, callers hitting bulk endpoints (DCPI,
        # tax-incentives, grid-intelligence) directly never see a 402
        # → never get a trial key minted → stay capped at the preview
        # forever. This branch detects the gate marker, mints a trial
        # key from a known-402 endpoint, then re-fires the original
        # request with the key in X-API-Key.
        if self.api_key:
            # Already have a key; gate fired anyway → endpoint requires
            # higher tier than IDENTIFIED. Surface the response as-is.
            return
        if response.status_code != 200:
            return
        try:
            body = response.json()
            if not isinstance(body, dict) or not body.get("_gated"):
                return  # not soft-paywalled; pass through
        except Exception:
            return  # not JSON; pass through

        # Mint a trial key by hitting a known IDENTIFIED-gate endpoint
        # in a one-off request inside the auth_flow. httpx.Auth doesn't
        # natively support side requests, so we yield a synthetic
        # request, capture its 402+X-Trial-Key, then re-fire original.
        mint_req = httpx.Request(
            "GET",
            "https://dchub.cloud/api/v1/transactions/export.csv",
            headers={"User-Agent": "dchub-mcp-helper/0.1.1"},
        )
        mint_resp = yield mint_req
        trial_key = mint_resp.headers.get("x-trial-key")
        if not trial_key:
            return  # mint failed; surface original soft-paywalled response
        self._save_key(trial_key)
        # Re-fire original with the new key
        request.headers["X-API-Key"] = trial_key
        yield request
