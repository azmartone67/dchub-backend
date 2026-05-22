"""
DC Hub API Client — Data Center Intelligence
Drop this file into any Python project for instant access to 20,534+ data centers.
Zero dependencies (stdlib only). Works with Python 3.7+.

Usage:
    from dchub import DCHub
    hub = DCHub()
    results = hub.search("Equinix", country="US", limit=5)
    for f in results["facilities"]:
        print(f["name"], f["city"], f["country"])

Moltbook agents: set DCHUB_MOLTBOOK_TOKEN env var for higher rate limits.
Portal: https://dchub.cloud/agent-portal
Docs:   https://dchub.cloud/api-docs
"""

import json
import os
import urllib.request
import urllib.parse

BASE = "https://dchub.cloud"

class DCHub:
    def __init__(self, moltbook_token=None):
        self.token = moltbook_token or os.environ.get("DCHUB_MOLTBOOK_TOKEN")

    def _get(self, path, params=None):
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url)
        if self.token:
            req.add_header("X-Moltbook-Identity", self.token)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    def search(self, query=None, country=None, limit=20):
        """Search 20,534+ data center facilities."""
        return self._get("/api/agent/facilities", {"q": query, "country": country, "limit": limit})

    def transactions(self, limit=50, deal_type=None):
        """M&A deals, acquisitions, investments ($324B+ tracked)."""
        return self._get("/api/transactions", {"limit": limit, "deal_type": deal_type})

    def news(self, limit=20):
        """Latest industry news from 40+ sources."""
        return self._get("/api/news", {"limit": limit})

    def markets(self):
        """List all tracked data center markets."""
        return self._get("/api/v1/markets/list")

    def compare_markets(self, *names):
        """Compare markets side by side. Example: compare_markets("dallas", "phoenix")"""
        return self._get("/api/v1/markets/compare", {"markets": ",".join(names)})

    def energy_prices(self):
        """Real-time LMP energy pricing by ISO region."""
        return self._get("/api/v1/lmp/prices")

    def pipeline(self):
        """Data centers under construction or planned."""
        return self._get("/api/v1/pipeline")

    def stats(self):
        """Platform statistics: facilities, countries, providers."""
        return self._get("/api/agent/stats")

    def capabilities(self):
        """Full machine-readable capability spec (for agent discovery)."""
        return self._get("/api/agent/capabilities")

    def whoami(self):
        """Verify Moltbook auth and check your agent profile."""
        return self._get("/api/agent/whoami")


if __name__ == "__main__":
    hub = DCHub()

    print("=== DC Hub API Client ===\n")

    s = hub.stats()
    st = s.get("stats", {})
    print(f"Facilities: {st.get('total_facilities', '?'):,}")
    print(f"Countries:  {st.get('countries_covered', '?')}")
    print(f"Providers:  {st.get('providers_tracked', '?')}\n")

    print("--- Top 5 Equinix facilities (US) ---")
    r = hub.search("Equinix", country="US", limit=5)
    for f in r.get("facilities", []):
        print(f"  {f.get('name', '?'):40s} {f.get('city', '?')}, {f.get('country', '?')}")

    print(f"\n--- Latest 3 M&A deals ---")
    tx = hub.transactions(limit=3)
    for d in tx.get("transactions", []):
        print(f"  {d.get('title', d.get('deal_name', '?'))}")

    print(f"\n--- Latest 3 news headlines ---")
    n = hub.news(limit=3)
    for a in n.get("articles", []):
        print(f"  {a.get('title', '?')[:80]}")

    print(f"\nDocs: https://dchub.cloud/api-docs")
    print(f"Portal: https://dchub.cloud/agent-portal")
