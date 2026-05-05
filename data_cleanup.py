"""
DC Hub Data Quality Cleanup Script
Run once to fix known data issues, then schedule weekly.

Usage (Replit shell):
    python data_cleanup.py --dry-run     # preview changes
    python data_cleanup.py               # apply fixes
    python data_cleanup.py --report      # generate quality report only
"""

import json
import hashlib
import re
import sys
import argparse
from datetime import datetime
from typing import Dict, List, Any, Tuple, Set
from collections import defaultdict


# ---------------------------------------------------------------------------
# 1. Transaction deduplication
# ---------------------------------------------------------------------------

def deduplicate_transactions(transactions: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """
    Remove duplicate transactions. Matches on:
    - Same buyer + market + similar value (within 10%)
    - Same buyer + same project description
    Returns (cleaned, duplicates_removed)
    """
    seen = {}  # fingerprint -> transaction
    duplicates = []

    for txn in transactions:
        # Build fingerprint from key fields
        buyer = (txn.get("buyer") or "").lower().strip()
        market = (txn.get("market") or "").lower().strip()
        value = txn.get("value") or 0
        date = txn.get("date") or ""
        notes = (txn.get("notes") or "").lower()

        # Fingerprint 1: buyer + market + value range
        value_bucket = round(value / max(value * 0.1, 1))  # 10% bucket
        fp1 = f"{buyer}|{market}|{value_bucket}"

        # Fingerprint 2: buyer + key phrases from notes
        key_phrases = re.findall(r'\b(?:campus|phase|site|project|build|gw|mw)\b', notes)
        fp2 = f"{buyer}|{'|'.join(sorted(set(key_phrases)))}" if key_phrases else None

        is_dupe = False
        for fp in [fp1, fp2]:
            if fp and fp in seen:
                existing = seen[fp]
                # Keep the one with more complete data
                existing_completeness = sum(1 for v in existing.values() if v)
                new_completeness = sum(1 for v in txn.values() if v)

                if new_completeness > existing_completeness:
                    duplicates.append(existing)
                    seen[fp] = txn
                else:
                    duplicates.append(txn)
                is_dupe = True
                break

        if not is_dupe:
            for fp in [fp1, fp2]:
                if fp:
                    seen[fp] = txn

    # Rebuild clean list preserving order
    dupe_ids = {id(d) for d in duplicates}
    cleaned = [t for t in transactions if id(t) not in dupe_ids]

    return cleaned, duplicates


# ---------------------------------------------------------------------------
# 2. Normalize transaction IDs
# ---------------------------------------------------------------------------

def normalize_transaction_ids(transactions: List[Dict]) -> List[Dict]:
    """
    Normalize all transaction IDs to a consistent format:
    deal-{buyer}-{market}-{date}
    Preserves old ID as `legacy_id`.
    """
    for txn in transactions:
        old_id = txn.get("id", "")

        # Skip if already normalized
        if re.match(r'^deal-[a-z0-9-]+-\d{4}$', old_id):
            continue

        buyer = (txn.get("buyer") or "unknown").lower()
        buyer = re.sub(r'[^a-z0-9]', '-', buyer).strip('-')[:30]

        market = (txn.get("market") or "global").lower()
        market = re.sub(r'[^a-z0-9]', '-', market).strip('-')[:20]

        date = (txn.get("date") or "undated")[:4]  # year only

        new_id = f"deal-{buyer}-{market}-{date}"

        # Handle collisions
        collision_count = 0
        test_id = new_id
        seen_ids = {t.get("id") for t in transactions if t is not txn}
        while test_id in seen_ids:
            collision_count += 1
            test_id = f"{new_id}-{collision_count}"
        new_id = test_id

        txn["legacy_id"] = old_id
        txn["id"] = new_id

    return transactions


# ---------------------------------------------------------------------------
# 3. Fix suspicious transaction values
# ---------------------------------------------------------------------------

# These are cumulative capex announcements, NOT individual deal values
CAPEX_ANNOUNCEMENT_PATTERNS = [
    {"buyer": "Microsoft", "seller": "OpenAI", "threshold": 100000},
    {"buyer": "Oracle", "seller": "OpenAI", "threshold": 100000},
    {"buyer": "Amazon", "seller": "AWS", "threshold": 100000},
    {"buyer": "Google", "threshold": 100000},
    {"buyer": "xAI", "seller": "xAI", "threshold": 50000},
    {"buyer": "Alibaba", "threshold": 50000},
]


def flag_capex_announcements(transactions: List[Dict]) -> List[Dict]:
    """
    Flag transactions that are cumulative capex announcements
    (not individual M&A deals) and tag them properly.
    """
    for txn in transactions:
        value = txn.get("value") or 0
        buyer = (txn.get("buyer") or "").lower()
        seller = (txn.get("seller") or "").lower()

        for pattern in CAPEX_ANNOUNCEMENT_PATTERNS:
            p_buyer = pattern["buyer"].lower()
            p_seller = pattern.get("seller", "").lower()
            threshold = pattern["threshold"]

            if (p_buyer in buyer and
                (not p_seller or p_seller in seller) and
                value >= threshold):

                txn["type"] = "capex_announcement"
                txn["_flag"] = "cumulative_capex"
                txn["_note"] = (
                    f"Value ${value:,.0f}M appears to be a cumulative capex "
                    f"announcement, not an individual transaction. "
                    f"Excluded from M&A totals."
                )
                break

    return transactions


# ---------------------------------------------------------------------------
# 4. Fill null/empty fields
# ---------------------------------------------------------------------------

def fill_missing_fields(records: List[Dict], record_type: str) -> Tuple[List[Dict], int]:
    """Fill in missing fields with sensible defaults and flags."""
    fixes = 0

    for record in records:
        if record_type == "facility":
            if not record.get("city"):
                # Try to extract from name
                name = record.get("name", "")
                state = record.get("state", "")
                if state and state in name:
                    parts = name.split(state)
                    if len(parts) > 0:
                        city_guess = parts[0].split()[-1] if parts[0].split() else None
                        if city_guess and len(city_guess) > 2:
                            record["city"] = city_guess
                            record["_city_inferred"] = True
                            fixes += 1

            if not record.get("provider") or record["provider"] == "":
                name = record.get("name", "")
                # Try to extract provider from facility name
                known_providers = [
                    "Meta", "Google", "Amazon", "Microsoft", "Oracle", "Equinix",
                    "Digital Realty", "CoreWeave", "xAI", "AWS", "Aligned",
                    "QTS", "Switch", "Vantage", "CyrusOne", "NTT", "STACK",
                ]
                for provider in known_providers:
                    if provider.lower() in name.lower():
                        record["provider"] = provider
                        record["_provider_inferred"] = True
                        fixes += 1
                        break

            if not record.get("slug"):
                name = record.get("name", "")
                slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:60]
                record["slug"] = slug
                fixes += 1

        elif record_type == "transaction":
            if not record.get("region"):
                market = (record.get("market") or "").lower()
                if any(s in market for s in ["virginia", "texas", "ohio", "indiana",
                                              "california", "oregon", "chicago",
                                              "memphis", "denver", "atlanta",
                                              "new jersey", "wisconsin", "global"]):
                    record["region"] = "North America"
                elif any(s in market for s in ["sweden", "iceland", "uk", "israel",
                                                "germany", "france", "nordics"]):
                    record["region"] = "EMEA"
                elif any(s in market for s in ["tokyo", "singapore", "korea",
                                                "sydney", "india"]):
                    record["region"] = "APAC"
                else:
                    record["region"] = "Global"
                record["_region_inferred"] = True
                fixes += 1

            if not record.get("notes") and record.get("buyer"):
                txn_type = record.get("type", "unknown")
                buyer = record.get("buyer", "Unknown")
                seller = record.get("seller", "")
                value = record.get("value")
                market = record.get("market", "")
                val_str = f" — ${value:,.0f}M" if value else ""
                seller_str = f" / {seller}" if seller else ""
                record["notes"] = f"{buyer}{seller_str} {txn_type} in {market}{val_str}"
                record["_notes_generated"] = True
                fixes += 1

    return records, fixes


# ---------------------------------------------------------------------------
# 5. Pipeline deduplication
# ---------------------------------------------------------------------------

def deduplicate_pipeline(pipeline: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Remove duplicate pipeline entries."""
    seen = {}
    dupes = []

    for project in pipeline:
        company = (project.get("company") or "").lower()
        proj_name = (project.get("project") or "").lower()
        market = (project.get("market") or "").lower()

        fp = f"{company}|{proj_name}|{market}"

        if fp in seen:
            existing = seen[fp]
            # Keep the one with later delivery date
            existing_date = existing.get("delivery", "")
            new_date = project.get("delivery", "")
            if new_date > existing_date:
                dupes.append(existing)
                seen[fp] = project
            else:
                dupes.append(project)
        else:
            seen[fp] = project

    cleaned = list(seen.values())
    return cleaned, dupes


# ---------------------------------------------------------------------------
# 6. Agent registry timestamp fix
# ---------------------------------------------------------------------------

def fix_agent_timestamps(agents: List[Dict]) -> List[Dict]:
    """
    Flag identical timestamps as needing real activity tracking.
    Add a schema for proper tracking.
    """
    timestamps = [a.get("last_active") for a in agents]
    all_same = len(set(timestamps)) <= 1

    if all_same:
        for agent in agents:
            agent["_timestamp_warning"] = "static_seed_data"
            agent["_needs_real_tracking"] = True
            # Add schema for real tracking
            agent["daily_calls"] = 0
            agent["weekly_calls"] = 0
            agent["last_real_activity"] = None

    return agents


# ---------------------------------------------------------------------------
# Quality report
# ---------------------------------------------------------------------------

def generate_quality_report(transactions, pipeline, facilities, agents) -> str:
    """Generate a data quality scorecard."""
    report = []
    report.append("=" * 60)
    report.append("DC HUB DATA QUALITY REPORT")
    report.append(f"Generated: {datetime.utcnow().isoformat()}")
    report.append("=" * 60)

    # Transactions
    report.append("\n📊 TRANSACTIONS")
    null_values = sum(1 for t in transactions if not t.get("value"))
    null_notes = sum(1 for t in transactions if not t.get("notes"))
    null_region = sum(1 for t in transactions if not t.get("region"))
    capex = sum(1 for t in transactions if t.get("_flag") == "cumulative_capex")
    report.append(f"  Total: {len(transactions)}")
    report.append(f"  Missing values: {null_values}")
    report.append(f"  Missing notes: {null_notes}")
    report.append(f"  Missing region: {null_region}")
    report.append(f"  Capex announcements (not real deals): {capex}")

    # Pipeline
    report.append("\n🏗️ PIPELINE")
    report.append(f"  Total projects: {len(pipeline)}")

    # Facilities
    report.append("\n🏢 FACILITIES")
    null_city = sum(1 for f in facilities if not f.get("city"))
    null_provider = sum(1 for f in facilities if not f.get("provider"))
    null_slug = sum(1 for f in facilities if not f.get("slug"))
    report.append(f"  Total: {len(facilities)}")
    report.append(f"  Missing city: {null_city}")
    report.append(f"  Missing provider: {null_provider}")
    report.append(f"  Missing slug: {null_slug}")

    # Agents
    report.append("\n🤖 AGENT REGISTRY")
    has_real_tracking = any(a.get("last_real_activity") for a in agents)
    report.append(f"  Total agents: {len(agents)}")
    report.append(f"  Real activity tracking: {'Yes' if has_real_tracking else '⚠️ NO — using seed data'}")

    report.append("\n" + "=" * 60)
    return "\n".join(report)


# ---------------------------------------------------------------------------
# Main execution
# ---------------------------------------------------------------------------

def run_cleanup(data: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """
    Run all cleanup steps on the data dict.
    Expects: {"transactions": [...], "pipeline": [...], "facilities": [...], "agents": [...]}
    """
    results = {"fixes": [], "stats": {}}

    # 1. Deduplicate transactions
    txns = data.get("transactions", [])
    txns_clean, txn_dupes = deduplicate_transactions(txns)
    results["fixes"].append(f"Removed {len(txn_dupes)} duplicate transactions")
    results["stats"]["txn_dupes_removed"] = len(txn_dupes)

    # 2. Normalize IDs
    txns_clean = normalize_transaction_ids(txns_clean)
    results["fixes"].append("Normalized all transaction IDs")

    # 3. Flag capex announcements
    txns_clean = flag_capex_announcements(txns_clean)
    capex_count = sum(1 for t in txns_clean if t.get("_flag") == "cumulative_capex")
    results["fixes"].append(f"Flagged {capex_count} capex announcements")

    # 4. Fill missing transaction fields
    txns_clean, txn_fills = fill_missing_fields(txns_clean, "transaction")
    results["fixes"].append(f"Filled {txn_fills} missing transaction fields")

    # 5. Deduplicate pipeline
    pipeline = data.get("pipeline", [])
    pipeline_clean, pipe_dupes = deduplicate_pipeline(pipeline)
    results["fixes"].append(f"Removed {len(pipe_dupes)} duplicate pipeline entries")

    # 6. Fill missing facility fields
    facilities = data.get("facilities", [])
    facilities_clean, fac_fills = fill_missing_fields(facilities, "facility")
    results["fixes"].append(f"Filled {fac_fills} missing facility fields")

    # 7. Fix agent timestamps
    agents = data.get("agents", [])
    agents_clean = fix_agent_timestamps(agents)
    results["fixes"].append("Flagged static agent timestamps for real tracking")

    # Build output
    cleaned_data = {
        "transactions": txns_clean,
        "pipeline": pipeline_clean,
        "facilities": facilities_clean,
        "agents": agents_clean,
    }

    # Generate report
    report = generate_quality_report(txns_clean, pipeline_clean,
                                     facilities_clean, agents_clean)
    results["report"] = report

    if not dry_run:
        results["cleaned_data"] = cleaned_data

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DC Hub Data Quality Cleanup")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes only")
    parser.add_argument("--report", action="store_true", help="Generate report only")
    parser.add_argument("--input", default="data.json", help="Input data file")
    parser.add_argument("--output", default="data_cleaned.json", help="Output file")
    args = parser.parse_args()

    # Load data
    try:
        with open(args.input) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"⚠️  No data file at {args.input}")
        print("Export your data first, or point --input to your data file.")
        print("\nExample: python data_cleanup.py --input exported_data.json")
        sys.exit(1)

    results = run_cleanup(data, dry_run=args.dry_run or args.report)

    print(results["report"])
    print("\nFixes applied:" if not args.dry_run else "\nFixes (dry run):")
    for fix in results["fixes"]:
        print(f"  ✅ {fix}")

    if not args.dry_run and not args.report:
        cleaned = results["cleaned_data"]
        with open(args.output, "w") as f:
            json.dump(cleaned, f, indent=2, default=str)
        print(f"\n💾 Cleaned data saved to {args.output}")
