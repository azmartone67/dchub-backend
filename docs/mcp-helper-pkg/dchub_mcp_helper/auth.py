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
        if response.status_code != 402:
            return  # not a paywall; nothing to do

        # Read trial key from response headers (preferred) or body fallback.
        trial_key = response.headers.get("x-trial-key")
        if not trial_key:
            # Body fallback for the MCP gatekeeper's JSON-RPC return
            try:
                body = response.json()
                trial_key = body.get("auto_trial_key")
            except Exception:
                trial_key = None
        if not trial_key:
            return  # no key offered — surface the 402 to the caller

        self._save_key(trial_key)
        request.headers["X-API-Key"] = trial_key
        yield request  # retry with the key
