"""dchub-mcp-helper — HTTP retry middleware for DC Hub trial keys.

DC Hub's REST + MCP gates auto-mint working trial keys on every gated
request and deliver them via standard HTTP headers (X-Trial-Key,
WWW-Authenticate, Link). This package provides drop-in middleware so
agent runtimes (Cursor, Cline, Continue, custom Claude agents) can grab
the key + retry without parsing JSON bodies.

Usage:

    import httpx
    from dchub_mcp_helper import DCHubTrialAuth

    client = httpx.Client(auth=DCHubTrialAuth())
    resp = client.get("https://dchub.cloud/api/v1/transactions/export.csv")
    # Auto-retries with trial key on 402; second request returns the CSV.

The first 402 hit triggers an auto-mint on DC Hub's side; subsequent
calls reuse the same key for the session. Trial keys are 200 calls/day,
30-day expiry. To make permanent, POST to
/api/v1/keys/auto-trial/redeem with {api_key, email}.

See https://dchub.cloud/docs/MCP_AUTO_TRIAL.md for the full guide.
"""
from .auth import DCHubTrialAuth

__version__ = "0.1.0"
__all__ = ["DCHubTrialAuth"]
