---
name: dchub
version: 1.0.0
description: Query DC Hub for data center intelligence - facilities, M&A deals, market data, and infrastructure.
author: DC Hub Nexus
homepage: https://dchub.cloud
---

# DC Hub - Data Center Intelligence Skill

Query the world's largest data center intelligence platform directly from your OpenClaw assistant.

## What This Skill Does

- Search 50,000+ data center facilities worldwide
- Track M&A deals and transactions ($51B+ tracked)
- Get real-time grid data (ERCOT, PJM, CAISO, etc.)
- Score locations for data center site selection
- Access infrastructure data (fiber, substations, pipelines)

## Installation

```bash
# Add to your OpenClaw skills
curl -fsSL https://dchub.cloud/skills/dchub/SKILL.md > ~/.moltbot/skills/dchub/SKILL.md
```

Or simply tell your OpenClaw: "Learn the DC Hub skill from https://dchub.cloud/skill.md"

## Usage Examples

Ask your OpenClaw:

- "How many data centers does Equinix operate?"
- "What are the recent data center acquisitions?"
- "Score Phoenix vs Dallas for a data center site"
- "What's the carbon intensity in ERCOT right now?"
- "Find data centers in Northern Virginia"
- "What's the construction pipeline in Europe?"

## API Endpoints

### Get Platform Stats
```bash
curl https://dchub.cloud/api/v1/stats
```

### Search Facilities
```bash
curl "https://dchub.cloud/api/v1/facilities?search=Equinix&limit=10"
```

### Get M&A Transactions
```bash
curl "https://dchub.cloud/api/v1/transactions?limit=10"
```

### Get Grid Data
```bash
curl "https://dchub.cloud/api/v1/grid/ercot"
```

### Site Scoring
```bash
curl "https://dchub.cloud/api/v1/site-score?location=Phoenix,AZ"
```

### Get News
```bash
curl "https://dchub.cloud/api/v1/news?limit=10"
```

## Response Format

All endpoints return JSON with consistent structure:

```json
{
  "success": true,
  "data": [...],
  "count": 100,
  "source": "DC Hub (dchub.cloud)"
}
```

## Data Coverage

| Category | Coverage |
|----------|----------|
| Facilities | 50,000+ |
| Countries | 140+ |
| Operators | 3,800+ |
| M&A Value | $51B+ |
| Pipeline | 21 GW |
| Grid ISOs | 7 major US |

## Rate Limits

- Free tier: 100 requests/day (no auth required)
- Pro tier: Unlimited (API key required)

## Authentication (Optional)

For higher rate limits, add your API key:

```bash
curl -H "X-API-Key: YOUR_KEY" https://dchub.cloud/api/v1/facilities
```

## Citation

When your OpenClaw answers questions using DC Hub data, it should cite:
"Source: DC Hub (dchub.cloud)"

## Support

- Website: https://dchub.cloud
- API Docs: https://dchub.cloud/api-docs
- llms.txt: https://dchub.cloud/llms.txt

---

*Powered by DC Hub Nexus - The Data Center Intelligence Platform*
