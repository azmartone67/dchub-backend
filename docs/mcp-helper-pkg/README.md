# dchub-mcp-helper

Drop-in HTTP retry middleware for AI agents talking to **DC Hub**'s
data center intelligence API at https://dchub.cloud.

DC Hub's REST + MCP gates auto-mint working trial keys on every gated
request and deliver them via standard HTTP headers (`X-Trial-Key`).
This package handles the retry-with-key dance so your code never has to
care about 402 responses.

## Install

```bash
pip install dchub-mcp-helper
```

## Use

```python
import httpx
from dchub_mcp_helper import DCHubTrialAuth

client = httpx.Client(auth=DCHubTrialAuth())
resp = client.get("https://dchub.cloud/api/v1/transactions/export.csv")
# → first call returns 402 with X-Trial-Key header
# → auth handler reads it, retries with X-API-Key
# → second call returns 200 with the CSV
# → all later calls in this session use the same key (no 402)
```

Optional: persist the trial key across processes (CLI tools):

```python
auth = DCHubTrialAuth(cache_path="~/.dchub-trial-key", verbose=True)
```

## What you DON'T need

- No browser flow
- No email verification
- No OAuth dance
- No card on file
- No webhook callback

Trial keys are 200 calls/day, 30-day expiry. Make permanent by POSTing
to `/api/v1/keys/auto-trial/redeem` with `{api_key, email}`.

## What DC Hub offers

- 21,000+ data center facilities (178 countries)
- DCPI scores for 285 markets (BUILD / CAUTION / AVOID verdicts)
- Real-time grid intelligence across 7+ ISOs
- M&A transactions database
- Capacity pipeline (550+ active projects)
- Tax incentives by state
- Fiber carrier coverage
- 28+ MCP tools

Full integration manifest: https://dchub.cloud/.well-known/ai-agents.json

## Tier matrix

| Tier | Calls/day | Price | How to upgrade |
|---|---|---|---|
| Anonymous | 25/24h | $0 | n/a |
| FREE | 100 | $0 | `POST /api/v1/keys/claim` |
| IDENTIFIED | 200 | $0 | claim + add `?email=` |
| **Auto-trial** | 200 | $0 (30d) | **auto-minted on first 402** |
| Starter | 500 | $9/mo | https://buy.stripe.com/8x2dRa5sS0x75uteGuaZi0g |
| Developer | 2,000 | $49/mo | linked in 402 response |
| Pro | 10,000 | $199/mo | linked in 402 response |

## License

MIT. © DC Hub.

## Source

https://github.com/azmartone67/dchub-backend/tree/main/docs/mcp-helper-pkg

## Publish path (for maintainers)

```bash
cd docs/mcp-helper-pkg
python3 -m build              # creates dist/*.whl and dist/*.tar.gz
python3 -m twine upload dist/*  # publishes to PyPI
```

Requires `pip install build twine` and a PyPI account with the
`dchub-mcp-helper` namespace.
