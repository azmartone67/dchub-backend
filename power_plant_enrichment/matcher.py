"""
Power Plant Coordinate Matcher
==============================
Reconciles plant records from EIA-860M and NASA NCCS against DC Hub's
existing power plant database. Handles deduplication, conflict resolution,
and coordinate quality scoring.

Integration point: Call merge_and_update() from your Flask sync endpoint
or scheduled task.
"""

import logging
import math
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Matching thresholds
NAME_SIMILARITY_THRESHOLD = 0.7   # Minimum fuzzy name match score
COORD_PROXIMITY_MILES = 2.0       # Max distance to consider same plant
CAPACITY_TOLERANCE_PCT = 0.20     # 20% capacity variance allowed for match


class PlantMatcher:
    """
    Matches and merges power plant data from multiple sources against
    DC Hub's existing database.
    """

    def __init__(self, db_connection=None):
        """
        Args:
            db_connection: Database connection or SQLAlchemy session.
                           If None, returns results without persisting.
        """
        self.db = db_connection
        self.stats = {
            "matched_existing": 0,
            "new_plants": 0,
            "coordinates_updated": 0,
            "capacity_updated": 0,
            "conflicts": 0,
            "skipped": 0,
        }

    def merge_sources(
        self,
        eia_plants: list[dict],
        nccs_plants: list[dict],
    ) -> list[dict]:
        """
        Merge EIA-860M and NCCS data, preferring EIA for coordinates
        (more authoritative) and NCCS for supplemental fields.

        Args:
            eia_plants: Plants from EIA-860M ingestion.
            nccs_plants: Plants from NCCS FeatureServer.

        Returns:
            Merged plant list with best available data per plant.
        """
        merged = {}

        # Index EIA plants by ID (primary source)
        for plant in eia_plants:
            key = plant.get("eia_plant_id") or _plant_key(plant)
            merged[key] = {**plant, "_sources": ["eia_860m"]}

        # Cross-reference NCCS plants
        nccs_matched = 0
        for nccs_plant in nccs_plants:
            match_key = self._find_match(nccs_plant, merged)

            if match_key:
                # Merge supplemental data from NCCS
                existing = merged[match_key]
                existing["_sources"].append("nccs_hifld")

                # Fill gaps (NCCS may have fields EIA doesn't)
                for field in ["hifld_object_id", "naics_code",
                              "transmission_lines", "prime_mover"]:
                    if nccs_plant.get(field) and not existing.get(field):
                        existing[field] = nccs_plant[field]

                # Validate coordinates match (flag discrepancies)
                if existing.get("latitude") and nccs_plant.get("latitude"):
                    dist = _haversine_miles(
                        existing["latitude"], existing["longitude"],
                        nccs_plant["latitude"], nccs_plant["longitude"]
                    )
                    if dist > COORD_PROXIMITY_MILES:
                        existing["_coord_discrepancy_miles"] = round(dist, 2)
                        logger.warning(
                            f"Coordinate discrepancy for {existing.get('name')}: "
                            f"{dist:.1f} miles between EIA and NCCS"
                        )

                nccs_matched += 1
            else:
                # New plant only in NCCS
                key = _plant_key(nccs_plant)
                merged[key] = {**nccs_plant, "_sources": ["nccs_hifld"]}

        logger.info(
            f"Merged {len(eia_plants)} EIA + {len(nccs_plants)} NCCS plants → "
            f"{len(merged)} unique ({nccs_matched} cross-matched)"
        )

        return list(merged.values())

    def match_against_dchub(
        self,
        enriched_plants: list[dict],
        existing_plants: list[dict],
    ) -> dict:
        """
        Match enriched plants against DC Hub's existing power_plants table.

        Args:
            enriched_plants: Merged plants from merge_sources().
            existing_plants: Current DC Hub plant records.

        Returns:
            Dict with 'updates', 'new', 'conflicts' lists.
        """
        result = {
            "updates": [],      # Existing plants to update
            "new": [],          # New plants to insert
            "conflicts": [],    # Records needing manual review
        }

        # Build spatial index of existing plants
        existing_index = _build_spatial_index(existing_plants)

        for plant in enriched_plants:
            lat = plant.get("latitude")
            lng = plant.get("longitude")
            name = plant.get("name", "")

            if not lat or not lng:
                self.stats["skipped"] += 1
                continue

            # Find nearest existing plant
            candidates = _find_nearby(existing_index, lat, lng,
                                       radius_miles=COORD_PROXIMITY_MILES)

            best_match = None
            best_score = 0

            for candidate in candidates:
                score = self._match_score(plant, candidate)
                if score > best_score and score >= NAME_SIMILARITY_THRESHOLD:
                    best_score = score
                    best_match = candidate

            if best_match:
                # Determine what to update
                updates = self._compute_updates(best_match, plant)
                if updates:
                    result["updates"].append({
                        "existing_id": best_match.get("id"),
                        "match_score": round(best_score, 3),
                        "updates": updates,
                        "sources": plant.get("_sources", []),
                    })
                    self.stats["matched_existing"] += 1
                    if "latitude" in updates or "longitude" in updates:
                        self.stats["coordinates_updated"] += 1
            else:
                # Check for ambiguous matches (multiple nearby plants)
                if len(candidates) > 1:
                    result["conflicts"].append({
                        "new_plant": plant,
                        "candidates": candidates[:5],
                        "reason": "multiple_nearby_matches",
                    })
                    self.stats["conflicts"] += 1
                else:
                    result["new"].append(plant)
                    self.stats["new_plants"] += 1

        logger.info(
            f"DC Hub matching complete: {self.stats['matched_existing']} matched, "
            f"{self.stats['new_plants']} new, {self.stats['conflicts']} conflicts"
        )

        return result

    def _find_match(self, plant: dict, index: dict) -> Optional[str]:
        """Find matching plant in index by EIA ID, name, or proximity."""
        # Try EIA ID match first
        eia_id = plant.get("eia_plant_id")
        if eia_id and eia_id in index:
            return eia_id

        # Try name + proximity match
        for key, existing in index.items():
            name_score = _name_similarity(
                plant.get("name", ""), existing.get("name", "")
            )
            if name_score < NAME_SIMILARITY_THRESHOLD:
                continue

            # Check proximity
            if existing.get("latitude") and plant.get("latitude"):
                dist = _haversine_miles(
                    existing["latitude"], existing["longitude"],
                    plant["latitude"], plant["longitude"]
                )
                if dist <= COORD_PROXIMITY_MILES:
                    return key

        return None

    def _match_score(self, plant: dict, candidate: dict) -> float:
        """
        Compute a match score (0-1) between an enriched plant
        and an existing DC Hub record.
        """
        score = 0.0
        weights = {"name": 0.4, "location": 0.3, "capacity": 0.2, "state": 0.1}

        # Name similarity
        name_sim = _name_similarity(
            plant.get("name", ""), candidate.get("name", "")
        )
        score += name_sim * weights["name"]

        # Proximity score (closer = higher)
        if plant.get("latitude") and candidate.get("latitude"):
            dist = _haversine_miles(
                plant["latitude"], plant["longitude"],
                candidate["latitude"], candidate["longitude"]
            )
            prox_score = max(0, 1 - (dist / COORD_PROXIMITY_MILES))
            score += prox_score * weights["location"]

        # Capacity similarity
        if plant.get("capacity_mw") and candidate.get("capacity_mw"):
            cap_ratio = min(plant["capacity_mw"], candidate["capacity_mw"]) / \
                        max(plant["capacity_mw"], candidate["capacity_mw"])
            score += cap_ratio * weights["capacity"]

        # State match
        if (plant.get("state", "").upper() ==
                candidate.get("state", "").upper()):
            score += weights["state"]

        return score

    def _compute_updates(self, existing: dict, enriched: dict) -> dict:
        """Determine which fields should be updated on the existing record."""
        updates = {}

        # Update coordinates if existing has None or low precision
        if enriched.get("latitude"):
            ex_lat = existing.get("latitude")
            ex_lng = existing.get("longitude")

            if ex_lat is None or ex_lng is None:
                updates["latitude"] = enriched["latitude"]
                updates["longitude"] = enriched["longitude"]
            else:
                # Check if enriched coords are more precise
                enriched_precision = _coord_precision(enriched["latitude"])
                existing_precision = _coord_precision(ex_lat)
                if enriched_precision > existing_precision:
                    updates["latitude"] = enriched["latitude"]
                    updates["longitude"] = enriched["longitude"]

        # Update capacity if missing or significantly different
        if enriched.get("capacity_mw") and not existing.get("capacity_mw"):
            updates["capacity_mw"] = enriched["capacity_mw"]

        # Fill in missing fields
        fill_fields = [
            "eia_plant_id", "energy_source", "technology",
            "balancing_authority", "county",
        ]
        for field in fill_fields:
            if enriched.get(field) and not existing.get(field):
                updates[field] = enriched[field]

        if updates:
            updates["enrichment_source"] = ",".join(enriched.get("_sources", []))
            updates["enriched_at"] = datetime.utcnow().isoformat()

        return updates

    def get_stats(self) -> dict:
        """Return matching statistics."""
        return dict(self.stats)


# ─── Utility functions ───────────────────────────────────────────────

def _plant_key(plant: dict) -> str:
    """Generate a unique key for deduplication."""
    name = (plant.get("name") or "unknown").lower().strip()
    state = (plant.get("state") or "XX").upper()
    lat = round(plant.get("latitude", 0, 0) or 0, 3)
    lng = round(plant.get("longitude", 0, 0) or 0, 3)
    return f"{state}:{name}:{lat}:{lng}"


def _name_similarity(a: str, b: str) -> float:
    """
    Simple name similarity using token overlap.
    For production, consider rapidfuzz or python-Levenshtein.
    """
    if not a or not b:
        return 0.0

    # Normalize
    a_tokens = set(a.lower().replace(",", " ").replace("-", " ").split())
    b_tokens = set(b.lower().replace(",", " ").replace("-", " ").split())

    # Remove common noise words
    noise = {"llc", "inc", "corp", "co", "the", "plant", "station",
             "generating", "generation", "power", "energy", "solar",
             "wind", "farm", "facility"}
    a_tokens -= noise
    b_tokens -= noise

    if not a_tokens or not b_tokens:
        # Fall back to raw comparison if all tokens were noise
        return 1.0 if a.lower().strip() == b.lower().strip() else 0.3

    intersection = a_tokens & b_tokens
    union = a_tokens | b_tokens

    return len(intersection) / len(union) if union else 0.0


def _coord_precision(coord: float) -> int:
    """Count decimal places in a coordinate (proxy for precision)."""
    s = str(coord)
    if "." in s:
        return len(s.split(".")[1])
    return 0


def _haversine_miles(lat1, lng1, lat2, lng2) -> float:
    """Haversine distance in miles."""
    R = 3959
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _build_spatial_index(plants: list[dict]) -> dict:
    """
    Build a simple grid-based spatial index for fast proximity lookups.
    Groups plants into ~0.1 degree cells (~7 mile grid).
    """
    index = {}
    for plant in plants:
        lat = plant.get("latitude")
        lng = plant.get("longitude")
        if lat is None or lng is None:
            continue
        cell = (round(lat, 1), round(lng, 1))
        if cell not in index:
            index[cell] = []
        index[cell].append(plant)
    return index


def _find_nearby(spatial_index: dict, lat: float, lng: float,
                 radius_miles: float = 2.0) -> list[dict]:
    """Find plants near a point using the spatial index."""
    candidates = []
    # Check surrounding cells
    for dlat in [-0.1, 0, 0.1]:
        for dlng in [-0.1, 0, 0.1]:
            cell = (round(lat + dlat, 1), round(lng + dlng, 1))
            for plant in spatial_index.get(cell, []):
                dist = _haversine_miles(lat, lng,
                                        plant["latitude"], plant["longitude"])
                if dist <= radius_miles:
                    candidates.append(plant)
    return candidates
