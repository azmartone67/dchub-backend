# dchub Platform — Index + Reserve

Two new sub-products for **dchub.cloud**, designed in the strategy brief that ships alongside this scaffold:

- **dchub Index** — a certified lease & purchase comp database, head-to-head against Hawk Swap.
- **dchub Reserve** — a gated, off-market marketplace for excess capacity. Pocket listings to a vetted buyer pool.

Both share a single facility graph, a single identity layer, and a cross-product flywheel: closing a Reserve listing automatically generates a Tier-2 verified Comp in Index.

## Run on Replit

1. Import this repo into a new Python Replit.
2. The `.replit` file already wires the Run button to `uvicorn`.
3. Hit **Run**. The app will:
   - create a SQLite database at `/tmp/dchub.db`
   - seed 5 facilities, 6 comps across all four verification tiers, and 4 listings across all four discretion modes
   - serve the home page, Index UI, Reserve UI, and `/docs` (Swagger).

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8080
open http://localhost:8080
```

## Run the smoke tests

```bash
pip install -r requirements.txt
pytest -q
```

The smoke tests validate the most important guarantees:

- Anonymous viewers see only Public listings, with operator contact info redacted.
- Buyer tier gates are enforced (a Silver buyer cannot see Pocket; a Channel buyer cannot see Gated).
- Pocket listings are visible only to manually-invited buyers.
- Whisper listings never appear in any browse response — they're matched out-of-band.
- Operator comp submissions create REPORTED comps by default and escalate to VERIFIED when a document hash is supplied.

## Endpoints

| Path | Purpose |
|---|---|
| `/` | Landing page |
| `/index` | Index UI — comp filters + KPIs |
| `/reserve` | Reserve UI — discretion-aware listings, demo identity switcher |
| `/docs` | OpenAPI / Swagger |
| `GET /api/index/comps` | Filterable comp list (market, operator, tier, etc.) |
| `GET /api/index/stats` | Aggregate KPIs |
| `GET /api/index/comps/{id}` | Single comp |
| `GET /api/reserve/listings` | Browse listings — visibility depends on `X-DCHUB-Buyer-Key` header |
| `GET /api/reserve/listings/{id}` | Single listing |
| `POST /api/reserve/listings` | Operator creates a listing (DRAFT) |
| `POST /api/reserve/listings/{id}/activate` | Operator publishes |
| `POST /api/reserve/listings/{id}/interest` | Buyer expresses interest (NDA gate enforced for Gated) |
| `POST /api/reserve/listings/{id}/close` | Operator closes — auto-generates Tier-2 Comp |
| `POST /api/operators/comps` | Operator submits a Comp to Index |
| `GET /api/operators/me` | Operator profile + Q-credit count |
| `GET /api/_demo/keys` | **Demo only.** Returns seeded API keys for the UI identity switcher. |

## Data model (highlights)

- `Facility` — physical site, joinable to dchub.cloud's existing facility graph by `external_id`.
- `Comp` — one transaction record. Carries `verification_tier` (1=Certified … 4=Inferred), `source_kind`, `confirmations`, optional `document_hash`.
- `Listing` — one off-market offer. Carries `discretion` (public/gated/pocket/whisper), `min_buyer_tier`, optional `invited_buyer_ids`.
- `Buyer`, `Operator` — separate identity tables, each with an `api_key`. Buyer tier (`platinum`/`gold`/`silver`/`channel`) drives Reserve visibility.

## Critical visibility rules (enforced in `app/auth.py`)

| Discretion | Anonymous | Silver | Gold | Platinum |
|---|---|---|---|---|
| Public  | ✓ (redacted) | ✓ | ✓ | ✓ |
| Gated   | ✗ | ✓ (NDA on click) | ✓ | ✓ |
| Pocket  | ✗ | ✗ | invited only | invited only |
| Whisper | ✗ | ✗ | ✗ | match-only |

The Channel (broker) tier sits below Silver and only sees Public; brokers participate by introducing their own clients into the buyer pool.

## What's NOT in this scaffold (intentionally)

- Real auth (use Auth0 / WorkOS / Clerk in production)
- Email + deal threading (use Postmark / Resend + a deals table with messages)
- Document hash + redacted-PDF storage (S3 + KMS, not in scope here)
- Ingestion pipelines for filings/news/permits (these are scheduled jobs that should call dchub's existing endpoints)
- Payment & escrow for success fees (Stripe Connect)
- Production database — SQLite is fine for Replit demo; swap to Postgres or NeonDB before launch

## Where this connects to existing dchub.cloud

`Facility.external_id` is the join key. Once Index runs in production, `app/seed.py` should be replaced by an ingestion job that calls `mcp__dchub__search_facilities` to pull the existing facility graph and `mcp__dchub__get_pipeline` for forward-looking supply context. Listings on Reserve should reference an existing Facility row by its `external_id`.

## Naming

The scaffold uses `Index` and `Reserve` as proposed in the strategy doc (§3.1, §3.2). Easy to rebrand — the name only appears in `static/*.html`, `main.py` titles, and the README.
