# @dchub/mcp-helper

Drop-in `fetch()` middleware for AI agents talking to **DC Hub**'s
data center intelligence API at https://dchub.cloud.

DC Hub's REST + MCP gates auto-mint working trial keys on every gated
request and deliver them via standard HTTP headers (`X-Trial-Key`).
This package handles the retry-with-key dance so your code never has
to care about 402 responses.

## Install

```bash
npm install @dchub/mcp-helper
```

## Use

```javascript
import { createDCHubFetch } from '@dchub/mcp-helper';

const fetchDC = createDCHubFetch({ verbose: true });
const r = await fetchDC('https://dchub.cloud/api/v1/dcpi/scores');
const data = await r.json();
console.log(data.count, 'markets');  // 285
```

CommonJS:

```javascript
const { createDCHubFetch } = require('@dchub/mcp-helper');
const fetchDC = createDCHubFetch();
fetchDC('https://dchub.cloud/api/v1/dcpi/scores')
  .then(r => r.json())
  .then(d => console.log(d.count, 'markets'));
```

Persist the trial key across processes (CLIs):

```javascript
import { writeFileSync, readFileSync, existsSync } from 'fs';
import { homedir } from 'os';
import { join } from 'path';
import { createDCHubFetch } from '@dchub/mcp-helper';

const path = join(homedir(), '.dchub-trial-key');
const fetchDC = createDCHubFetch({
  apiKey: existsSync(path) ? readFileSync(path, 'utf-8').trim() : undefined,
  onTrialKey: (key) => writeFileSync(path, key),
  verbose: true,
});
```

## TypeScript

Types ship with the package — no `@types/*` install needed.

```typescript
import { createDCHubFetch, type DCHubFetch } from '@dchub/mcp-helper';

const fetchDC: DCHubFetch = createDCHubFetch({ verbose: true });
```

## What you DON'T need

- No browser flow
- No email verification
- No OAuth dance
- No card on file
- No webhook callback

Trial keys are 200 calls/day, 30-day expiry. Make permanent by POSTing
to `https://dchub.cloud/api/v1/keys/auto-trial/redeem` with `{api_key, email}`.

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

## What DC Hub offers

- 21,000+ data center facilities (178 countries)
- DCPI scores for 285 markets (BUILD / CAUTION / AVOID verdicts)
- Real-time grid intelligence across 7+ ISOs
- M&A transactions database (1,852+ tracked)
- Capacity pipeline (550+ active projects)
- Tax incentives by US state (50)
- Fiber carrier coverage
- 28+ MCP tools

Full integration manifest: https://dchub.cloud/.well-known/ai-agents.json

## License

MIT. © DC Hub.

## Sister package (Python)

```bash
pip install "git+https://github.com/azmartone67/dchub-backend.git#subdirectory=docs/mcp-helper-pkg"
```

Same API surface, httpx-based middleware.

## Publish (for maintainers)

```bash
cd docs/mcp-helper-npm
npm publish --access public
```

Requires `npm login` to a maintainer of the `@dchub` scope on npm.
