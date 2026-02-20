# DC Hub Nexus - Replit Deployment Guide

## 🚀 Quick Deploy to Replit

### Step 1: Create New Replit
1. Go to [replit.com](https://replit.com) and sign in
2. Click **+ Create Repl**
3. Choose **Python** template
4. Name it: `dc-hub-nexus`
5. Click **Create Repl**

### Step 2: Upload Files
Upload ALL these files to your Replit:
- `main.py` (entry point)
- `discovery_nexus.py` (discovery engine)
- `api_server.py` (REST API)
- `requirements.txt` (dependencies)
- `.replit` (run configuration)
- `replit.nix` (system packages)

### Step 3: Run
Click the green **Run** button. Replit will:
1. Install dependencies automatically
2. Initialize the database
3. Run initial discovery (fetches from PeeringDB)
4. Start the API server

### Step 4: Get Your API URL
Your API will be available at:
```
https://dc-hub-nexus.YOUR-USERNAME.repl.co
```

## 📡 API Endpoints

### Public (No Auth Required)
```
GET  /                       - API info
GET  /health                 - Health check
GET  /api/v1/stats          - Aggregate statistics
GET  /api/v1/facilities     - List facilities (paginated)
GET  /api/v1/facilities/:id - Get single facility
GET  /api/v1/search?q=      - Search facilities
GET  /api/v1/announcements  - List announcements
```

### Discovery Control
```
GET  /api/v1/discovery/status - Check discovery status
POST /api/v1/discovery/run    - Trigger discovery
     Body: {"mode": "quick|full|news"}
```

### Authenticated (API Key Required)
```
POST   /api/v1/facilities   - Submit new facility
PUT    /api/v1/facilities/:id - Update facility
POST   /api/v1/keys         - Create API key
GET    /api/v1/export       - Full data export
```

## 🔗 Connecting Your Frontend

Update your frontend's `NexusAPI.config.baseUrl`:

```javascript
const NexusAPI = {
    config: {
        baseUrl: 'https://dc-hub-nexus.YOUR-USERNAME.repl.co',
        // ...
    },
    // ...
};
```

## 🔧 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DCHUB_RUN_DISCOVERY` | `1` | Run discovery on startup |
| `DCHUB_DISCOVERY_MODE` | `quick` | Initial run mode (quick/full/none) |
| `PORT` | `5000` | Server port (Replit sets this) |

## 📊 Data Sources

### Tier 1 - Free APIs (Auto-enabled)
- **PeeringDB** - 5,000+ facilities with IX data
- **OpenStreetMap** - 10,000+ tagged data centers
- **Wikidata** - Structured entity data
- **SEC EDGAR** - REIT filings

### Tier 2 - Web Scraping (Enabled)
- Cloudscene, DatacenterHawk, Provider sites

### Tier 3 - News (Enabled)
- RSS feeds from DCD, DCK, DCF

## 🔄 Automatic Discovery

The server runs discovery automatically:
- **On startup**: Quick discovery
- **Every 6 hours**: Scheduled quick discovery
- **Manual trigger**: POST to `/api/v1/discovery/run`

## 💡 Tips

1. **Keep Replit Awake**: Use UptimeRobot to ping `/health` every 5 minutes
2. **Scale Up**: Upgrade to Replit Hacker plan for always-on
3. **Custom Domain**: Add your domain in Replit settings
4. **Monitor**: Check `/api/v1/stats` for data growth

## 📁 File Structure
```
dc-hub-nexus/
├── main.py              # Entry point (combines engine + API)
├── discovery_nexus.py   # Data discovery from 15+ sources
├── api_server.py        # REST API server
├── requirements.txt     # Python dependencies
├── .replit             # Replit run configuration
├── replit.nix          # System dependencies
└── dc_nexus.db         # SQLite database (auto-created)
```

## 🆘 Troubleshooting

**API returns demo data?**
- Discovery hasn't completed yet. Wait 1-2 minutes.

**CORS errors?**
- Flask-CORS is enabled. Check your frontend URL.

**Rate limited?**
- PeeringDB allows 100 requests/5 min. Discovery respects this.

**Database locked?**
- Stop any running discovery before restarting.

---

Built for [DC Hub](https://dchub.cloud) 🏢⚡
