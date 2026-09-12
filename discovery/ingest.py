"""
DC Hub Discovery Data Ingestion Script
Reads JSON output files from the daily scheduled task and upserts
records into the Neon database. Idempotent - safe to run multiple times.

Usage:
    python ingest.py                    # ingest today's data
    python ingest.py --date 2026-03-28  # ingest specific date
    python ingest.py --dir /path/to/json
"""
import argparse
import json
import os
import sys
from datetime import datetime, date
from pathlib import Path
from db import get_conn
from psycopg2.extras import Json

DEFAULT_DIR = os.getenv("DISCOVERY_DATA_DIR", "./data")


def load_json(filepath):
    p = Path(filepath)
    if not p.exists():
        print(f"  Warning: File not found: {filepath}")
        return None
    with open(p) as f:
        return json.load(f)


def ingest_intelligence_index(cur, data, fetched_at):
    idx = data.get("dc_hub_intelligence_index", data)
    cur.execute("""
        INSERT INTO intelligence_index
            (fetched_at, pulse_score, version, agent_queries_24h,
             active_integrations, unique_facilities_queried_24h, raw_json)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT ((fetched_at::date)) DO UPDATE SET
            pulse_score = EXCLUDED.pulse_score,
            raw_json = EXCLUDED.raw_json
    """, (fetched_at, idx.get("global_pulse_score"), idx.get("version"),
          idx.get("total_agent_queries_24h"), idx.get("active_integrations"),
          idx.get("network_effect", {}).get("unique_facilities_queried_24h"),
          Json(data)))
    return 1


def ingest_news(cur, articles, fetched_at):
    count = 0
    for a in articles:
        cur.execute("""
            INSERT INTO news_articles (title, source, published_at, category, summary, url, relevance_score, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (title, published_at) DO NOTHING
        """, (a.get("title"), a.get("source"), a.get("published_at"),
              a.get("category"), a.get("summary"), a.get("url"),
              a.get("relevance_score"), fetched_at))
        count += 1
    return count


def ingest_power(cur, records, fetched_at):
    count = 0
    for r in records:
        cur.execute("""
            INSERT INTO infrastructure_power
                (dchub_id, type, name, lat, lon, capacity_mw, voltage_kv,
                 fuel_type, operator, source_market, distance_km, status, raw_json, fetched_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (type, name, lat, lon) DO UPDATE SET
                capacity_mw=EXCLUDED.capacity_mw, voltage_kv=EXCLUDED.voltage_kv,
                operator=EXCLUDED.operator, raw_json=EXCLUDED.raw_json, fetched_at=EXCLUDED.fetched_at
        """, (r.get("id"), r.get("type"), r.get("name"), r.get("lat"), r.get("lon"),
              r.get("capacity_mw"), r.get("voltage_kv"), r.get("fuel_type"),
              r.get("operator"), r.get("source_market"), r.get("distance_km"),
              r.get("status"), Json(r), fetched_at))
        count += 1
    return count


def ingest_gas(cur, records, fetched_at):
    count = 0
    for r in records:
        cur.execute("""
            INSERT INTO infrastructure_gas
                (dchub_id, name, operator, lat, lon, diameter_inches,
                 pressure_psi, capacity, source_market, distance_km, status, raw_json, fetched_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (name, lat, lon) DO UPDATE SET
                operator=EXCLUDED.operator, raw_json=EXCLUDED.raw_json, fetched_at=EXCLUDED.fetched_at
        """, (r.get("id"), r.get("name"), r.get("operator"), r.get("lat"), r.get("lon"),
              r.get("diameter_inches"), r.get("pressure_psi"), r.get("capacity"),
              r.get("source_market"), r.get("distance_km"), r.get("status"),
              Json(r), fetched_at))
        count += 1
    return count


def ingest_fiber(cur, records, fetched_at):
    count = 0
    for r in records:
        cur.execute("""
            INSERT INTO infrastructure_fiber
                (dchub_id, carrier, route_name, route_type, geojson,
                 distance_km, endpoint_a, endpoint_b, lit_capacity, raw_json, fetched_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (carrier, route_name) DO UPDATE SET
                geojson=EXCLUDED.geojson, raw_json=EXCLUDED.raw_json, fetched_at=EXCLUDED.fetched_at
        """, (r.get("id"), r.get("carrier"), r.get("route_name"), r.get("route_type"),
              Json(r.get("geojson")), r.get("distance_km"), r.get("endpoint_a"),
              r.get("endpoint_b"), r.get("lit_capacity"), Json(r), fetched_at))
        count += 1
    return count


def ingest_facilities(cur, records, fetched_at):
    count = 0
    for r in records:
        cur.execute("""
            INSERT INTO facilities
                (dchub_id, name, provider, city, state, country, lat, lon,
                 status, capacity_mw, pue, tier_level, floor_space_sqft, raw_json, fetched_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (dchub_id) DO UPDATE SET
                name=EXCLUDED.name, provider=EXCLUDED.provider, status=EXCLUDED.status,
                capacity_mw=EXCLUDED.capacity_mw, raw_json=EXCLUDED.raw_json, fetched_at=EXCLUDED.fetched_at
        """, (r.get("id"), r.get("name"), r.get("provider"), r.get("city"),
              r.get("state"), r.get("country"), r.get("lat"), r.get("lon"),
              r.get("status"), r.get("capacity_mw"), r.get("pue"),
              r.get("tier_level"), r.get("floor_space_sqft"), Json(r), fetched_at))
        count += 1
    return count


def ingest_transactions(cur, records, fetched_at):
    count = 0
    for r in records:
        cur.execute("""
            INSERT INTO transactions
                (buyer, seller, deal_date, deal_type, value_usd, region, market, assets, raw_json, fetched_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (buyer, seller, deal_date) DO UPDATE SET
                value_usd=EXCLUDED.value_usd, raw_json=EXCLUDED.raw_json, fetched_at=EXCLUDED.fetched_at
        """, (r.get("buyer"), r.get("seller"), r.get("date"), r.get("deal_type"),
              r.get("value_usd"), r.get("region"), r.get("market"),
              r.get("assets"), Json(r), fetched_at))
        count += 1
    return count


def run_ingestion(data_dir, run_date):
    fetched_at = datetime.utcnow().isoformat()
    results = {}
    errors = []

    with get_conn() as conn:
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO discovery_runs (run_date, started_at, status)
            VALUES (%s, NOW() ON CONFLICT DO NOTHING, 'running')
            ON CONFLICT (run_date) DO UPDATE SET started_at=NOW(), status='running'
            RETURNING id
        """, (run_date,))
        run_id = cur.fetchone()["id"]

        # Intelligence Index
        try:
            data = load_json(f"{data_dir}/dchub-discovery-{run_date}.json")
            if data and "dc_hub_intelligence_index" in data:
                results["intelligence_index"] = ingest_intelligence_index(cur, data, fetched_at)
        except Exception as e:
            errors.append(f"intelligence_index: {e}")

        # News
        try:
            data = load_json(f"{data_dir}/market-news-{run_date}.json")
            if data:
                articles = data.get("articles", data if isinstance(data, list) else [])
                results["news"] = ingest_news(cur, articles, fetched_at)
        except Exception as e:
            errors.append(f"news: {e}")

        # Power
        try:
            data = load_json(f"{data_dir}/infrastructure-power-{run_date}.json")
            if data:
                records = data.get("records", data if isinstance(data, list) else [])
                results["power"] = ingest_power(cur, records, fetched_at)
        except Exception as e:
            errors.append(f"power: {e}")

        # Gas
        try:
            data = load_json(f"{data_dir}/infrastructure-gas-{run_date}.json")
            if data:
                records = data.get("records", data if isinstance(data, list) else [])
                results["gas"] = ingest_gas(cur, records, fetched_at)
        except Exception as e:
            errors.append(f"gas: {e}")

        # Fiber
        try:
            data = load_json(f"{data_dir}/infrastructure-fiber-{run_date}.json")
            if data:
                records = data.get("records", data if isinstance(data, list) else [])
                results["fiber"] = ingest_fiber(cur, records, fetched_at)
        except Exception as e:
            errors.append(f"fiber: {e}")

        # Facilities
        try:
            data = load_json(f"{data_dir}/facilities-{run_date}.json")
            if data:
                records = data.get("data", data if isinstance(data, list) else [])
                results["facilities"] = ingest_facilities(cur, records, fetched_at)
        except Exception as e:
            errors.append(f"facilities: {e}")

        # Update run status
        status = "completed" if not errors else "completed_with_errors"
        cur.execute("""
            UPDATE discovery_runs SET completed_at=NOW(), status=%s,
                records_inserted=%s, errors=%s,
                summary=%s
            WHERE id=%s
        """, (status, Json(results), Json(errors) if errors else None,
              f"Ingested {sum(results.values())} records across {len(results)} tables", run_id))
        cur.close()

    total = sum(results.values())
    print(f"Ingestion complete: {total} records across {len(results)} tables")
    if errors:
        print(f"Errors: {len(errors)}")
        for e in errors:
            print(f"  - {e}")
    return results, errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest DC Hub discovery data into Neon")
    parser.add_argument("--date", default=date.today().isoformat(), help="Date (YYYY-MM-DD)")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="Directory with JSON files")
    args = parser.parse_args()
    print(f"DC Hub Discovery Ingestion | Date: {args.date} | Dir: {args.dir}")
    results, errors = run_ingestion(args.dir, args.date)
    sys.exit(1 if errors else 0)
