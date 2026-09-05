"""Our own outbound calls must be nameable, on every HTTP stack we use.

★ WHY. Measured at the Cloudflare edge 2026-09-05, 7 days, 546,632 successful
POSTs to /mcp:

    OURS (branded UA or our own network)      192,513  35.2%
    generic UA on a network OUR agent uses    270,329  49.5%
    genuinely unattributed                     68,641  12.6%
    identifiable EXTERNAL MCP client           15,149   2.8%

Half the traffic was unreadable, so NO growth number on /mcp could be
interpreted. http_ua_default was already working where it was imported
(DCHub/1.0 appears 31,132 times from Railway) — it had two holes:

  1. httpx was never patched at all. python-httpx2/2.7.0 (30,760) and
     python-httpx/0.28.1 (15,815) — ~46k calls it could not see.
  2. Only main.py imported it, so standalone processes ran without it.

★ THE PROOF THE UNATTRIBUTED TRAFFIC IS OURS: DCHub-CatalogSync/1.0 appears
from BOTH Railway AND Microsoft (Azure), and Microsoft ASN alone is 199,799 of
the "unattributed" block.
"""
import importlib
import os

import pytest


@pytest.fixture()
def mod(monkeypatch):
    monkeypatch.setenv("DCHUB_ROLE", "web")
    import http_ua_default
    return importlib.reload(http_ua_default)


def test_the_role_is_in_the_user_agent(mod):
    """An anonymous DCHub/1.0 cannot be told apart from any other caller."""
    assert mod._UA == "DCHub-web/1.0 (+https://dchub.cloud)"


def test_the_dchub_prefix_is_preserved(mod):
    """Every existing self-traffic filter keys on the DCHub prefix — adding a
    role must not orphan them."""
    assert mod._UA.startswith("DCHub")


def test_falls_back_when_no_role_is_set(monkeypatch):
    monkeypatch.delenv("DCHUB_ROLE", raising=False)
    monkeypatch.delenv("RAILWAY_SERVICE_NAME", raising=False)
    import http_ua_default
    m = importlib.reload(http_ua_default)
    assert m._UA.startswith("DCHub") and "//" not in m._UA.split("(")[0]


def test_requests_default_is_named(mod):
    import requests.utils
    assert requests.utils.default_user_agent() == mod._UA


def test_urllib_default_is_named(mod):
    import urllib.request
    assert ("User-Agent", mod._UA) in urllib.request._opener.addheaders


# ── the ~46k gap ─────────────────────────────────────────────────────────
def test_httpx_client_is_named(mod):
    httpx = pytest.importorskip("httpx")
    assert httpx.Client().headers.get("user-agent") == mod._UA


def test_httpx_async_client_is_named(mod):
    httpx = pytest.importorskip("httpx")
    assert httpx.AsyncClient().headers.get("user-agent") == mod._UA


def test_an_explicit_user_agent_always_wins(mod):
    """Registry probes deliberately send a browser UA. Never overwrite one."""
    httpx = pytest.importorskip("httpx")
    c = httpx.Client(headers={"User-Agent": "explicit-probe/9"})
    assert c.headers.get("user-agent") == "explicit-probe/9"


# ── the standalone-process gap ───────────────────────────────────────────
def test_standalone_schedulers_import_the_default():
    """crawler_scheduler runs WITHOUT main.py, so it must import this itself or
    every call it makes goes out anonymous."""
    src = open("crawler_scheduler.py", encoding="utf-8").read()
    assert "import http_ua_default" in src, (
        "crawler_scheduler runs standalone; without this import its outbound "
        "calls are Python-urllib/* and unattributable at the edge")
