# DC Hub Nexus - Data Center Intelligence Platform

## Overview

DC Hub Nexus is a comprehensive data center intelligence platform designed to aggregate, discover, and serve information about data center facilities globally. It automates data discovery, provides real-time news, market intelligence, and offers an API for data consumption. The platform's core purpose is to deliver real-time data center capacity tracking, site selection tools, and market intelligence to users, aiming to become a leading intelligence platform for the data center industry.

## User Preferences

Preferred communication style: Simple, everyday language.

## CRITICAL RULES (see replit-instructions.txt)

- **OAuth**: NEVER modify Google OAuth redirect_uri, HMAC state, or callback handling. redirect_uri MUST be `https://dchub.cloud/api/auth/google/callback`.
- **Database**: NEVER change Neon PostgreSQL connection, table names, or column names. Core tables: facilities, deals, news_articles, capacity_pipeline, ecosystem_companies, users.
- **API Routes**: NEVER rename, move, or remove existing routes. Frontend and Cloudflare Worker depend on exact paths.
- **CORS**: All API responses MUST include `Access-Control-Allow-Origin: *`.
- **JWT**: NEVER change signing key, payload structure, or token storage format (`dchub_token` / `dchub_session` in localStorage).
- **Before any change**: Ask "Does this modify an existing route, table schema, or auth flow?" If YES → confirm with user first.
- **Allowed**: Adding NEW facilities/news, running discovery, adding NEW routes, creating indexes.
- **Architecture**: Frontend = Cloudflare Pages, API Proxy = Cloudflare Worker (reads Neon directly for GET), Backend = this Replit Flask app (writes, auth, AI, MCP).

## System Architecture

### Backend Architecture

The backend is built with Flask, combining a multi-source discovery engine and a REST API server. Key components include:
-   **Discovery Engine:** Aggregates data from 15+ sources and 60+ RSS feeds.
-   **API Server:** Provides public and authenticated endpoints.
-   **Jobs API (Feb 2026):** All background tasks converted to one-shot POST `/api/jobs/*` endpoints with `DCHUB_ADMIN_KEY` auth. External cron triggers: news-refresh, discovery, global-intel, ecosystem, evolution, outreach, promotion, content-publish, ai-wars. Only health watchdog, Neon keepalive, and periodic GC remain as in-process daemon threads.
-   **Deep Learning Engine:** AI-powered system for pattern learning, transaction detection, capacity tracking, and market trend analysis.
-   **Self-Learning Discovery:** Autonomously crawls the web for new data sources, including an API Auto-Discovery & Registration Engine for government/industry data APIs, and a KMZ Auto-Discovery Engine for fiber route data.
-   **AI Agent & Orchestrator:** Manages AI features, coordinates AI agents, performs market sentiment analysis, predictive intelligence, and anomaly detection.
-   **Transactions API:** Curated database of verified M&A deals.
-   **Intelligence Engine:** Manages automated marketing, alerts, and content generation.
-   **Evolution Engine:** Autonomous self-improvement system for continuous learning and quality assurance.
-   **MCP Server:** Exposes DC Hub data for various AI models via a Model Context Protocol.
-   **Infrastructure Discovery:** Tracks fiber routes, DC properties, construction permits, and power substations.
-   **Global Intelligence Agent:** Enhances self-learning with international data discovery and capacity pipeline tracking.
-   **Agent Hub:** Multi-agent system for sales lead qualification, data enrichment, and social media posting.
-   **AI Interconnection System:** Provides learning and citation endpoints for AI platforms with usage tracking.
-   **Competitive Intelligence Suite:** Offers real estate, fiber network, SEC EDGAR, competitor, job posting, and construction permit intelligence.
-   **Site Risk Assessment Suite:** Provides USGS Water Risk, USGS Seismic Risk, FEMA Natural Hazards, NOAA Climate Intelligence, and a Composite DC Site Risk Score with multi-site comparison capabilities.

**Design Patterns:** Modular API with Flask Blueprints, background threading for discovery, queue-based webhook delivery, and in-memory caching with SQLite persistence.

### Data Storage

The platform uses **Neon PostgreSQL** exclusively as the sole production database. All reads and writes go through `db_utils.py`, which provides `get_db()`, `get_read_db()`, and `get_bg_db()` — all return a `PGConnectionWrapper` that transparently translates SQLite-style SQL (? placeholders, LIKE→ILIKE, datetime functions, DATETIME→TIMESTAMP, AUTOINCREMENT→SERIAL, INSERT OR IGNORE/REPLACE, PRAGMA→no-op) to PostgreSQL syntax. SQLite has been fully removed from the production path (Feb 2026). The only exception is `ai_tracking.db` which uses a separate SQLite file for non-critical AI crawler visit tracking.

**Connection Pool Architecture (Feb 2026):**
-   **Single pool:** One shared pool (1-8 connections, 30s statement timeout) for all requests and background tasks. Total max: 8 connections (well within Neon's 100 limit).
-   **Connection checkout tracking:** Every connection checkout records thread name, stack trace, and timestamp in `_active_checkouts` dict for leak detection.
-   **Forced connection reclaim:** Background thread checks every 30s, forcibly kills and returns connections held > 60 seconds. Logs offending stack trace for debugging.
-   **Circuit breaker:** After 3 consecutive connectivity failures, all requests fail fast for 30s instead of hanging, then auto-recover. Only real connectivity errors trip the breaker (not constraint violations or statement timeouts).
-   **10s acquisition timeout:** If all pool connections are in use, new requests fail fast after 10s instead of hanging indefinitely.
-   **Non-blocking logging:** MCP gateway logging (log_request, log_discovered_platform, learn_pattern) and AI access logging use `try_get_db()` which instantly returns None if pool is busy, preventing logging from starving core API requests.
-   **NEON_DATABASE_URL validation:** Startup validates URL starts with `postgresql://` or `postgres://` before overriding DATABASE_URL; invalid values are skipped with a warning.
-   **Stale connection validation:** Every connection is tested with `SELECT 1` before use; stale connections are discarded and replaced.
-   **Graceful degradation:** Key endpoints (`/api/v1/stats`, `/api/status`) cache last-good data and serve it when DB is unreachable.
-   **Health endpoint:** `/api/health/db` returns real-time pool stats, circuit breaker state, memory usage, leaked connections detail, active checkout count, and Neon connection limits.
-   **Request timeout logging:** Requests exceeding 30s are logged as SLOW REQUEST warnings.
-   **Auto-rollback:** Both `execute()` and `executemany()` auto-rollback on errors to prevent dirty connections being returned to pool.
-   **Split CORS policy:** Authenticated endpoints (`/api/auth/*`, `/api/stripe/*`, `/api/v2/alerts`, `/api/ai-usage/*`) use `Access-Control-Allow-Origin: https://dchub.cloud` with `Access-Control-Allow-Credentials: true`. All other public read endpoints use `Access-Control-Allow-Origin: *` without credentials. Foreign origins on auth endpoints are rejected.

### Frontend Architecture

The frontend uses **Vanilla HTML/CSS/JavaScript** and features a dashboard (`index.html`) with interactive Leaflet maps and MarkerCluster. It employs a dark theme and responsive grid layout. A **User Dashboard** (`dashboard.html`) provides authenticated users with plan details, API keys, usage stats, and billing portal access. Authentication is handled via JWT tokens.

### API Design

The API offers:
-   **Public Endpoints:** For aggregate statistics, facility lists, search, and news feeds.
-   **Authenticated Endpoints:** For data submission, webhook registration, and triggering discovery runs.
-   **Intelligence Endpoints:** For market statistics, alerts, and subscriptions.
-   **Evolution Engine Endpoints:** For learning status and AI teaching.
-   **Infrastructure Endpoints (v1 & v2):** Comprehensive access to fiber routes, properties, permits, substations, and various government data layers.
-   **External Data Integration Endpoints:** Specific endpoints for EIA Energy API, Grid Status API, FCC Broadband Map API, and EPA Envirofacts/FLIGHT API.

### API Tier Gating (Monetization)

The platform implements tiered API access control:
-   **Free Plan:** Limited calls and basic access.
-   **Pro Plan:** Increased calls, includes energy endpoints.
-   **Enterprise Plan:** Full API access and priority support.
Freemium AI Discovery Endpoints provide limited data without authentication, with full data available for Pro/Enterprise users. Protected endpoints require Pro or higher plans.

### Discovery Pipeline

The discovery engine operates in tiered cycles:
-   **Tier 1 (Daily):** Free APIs like PeeringDB, OpenStreetMap, Wikidata, SEC EDGAR.
-   **Tier 2 (Weekly):** Web scraping from sources like Cloudscene, DatacenterHawk.
-   **Tier 3 (Every 15 min):** RSS news feeds for real-time announcements.
Data deduplication uses source-specific IDs and SHA256 hashing.

## External Dependencies

### Python Packages

-   `flask`, `flask-cors`, `flask-compress`
-   `requests`
-   `beautifulsoup4`, `lxml`
-   `feedparser`
-   `anthropic`
-   `apscheduler`
-   `python-dateutil`
-   `gunicorn`

### External APIs & Data Sources

-   **PeeringDB**
-   **OpenStreetMap/Overpass**
-   **Wikidata**
-   **SEC EDGAR**
-   **RSS Feeds:** Data Center Dynamics, Data Center Knowledge, Data Center Frontier, TechCrunch, The Register.
-   **EIA Energy API** (U.S. Energy Information Administration)
-   **Grid Status API** (Real-time ISO/RTO Data)
-   **FCC Broadband Map API** (Broadband Coverage Data)
-   **Various Government Data Sources:** 40+ sources for infrastructure data (e.g., HIFLD, ISO/RTO, etc.)

### Optional Integrations

-   **Anthropic API**
-   **Webhook Subscribers**

### Frontend CDN Dependencies

-   Leaflet.js 1.9.4
-   Leaflet.markercluster
-   Google Fonts