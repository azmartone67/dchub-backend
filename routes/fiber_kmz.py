"""KMZ/KML → fiber route records (2026-09-03).

Pure parsing: bytes in, dicts out. No database, no network, no Flask — so it
is testable directly and reusable by the CLI ingester
(tools/ingest_fiber_kmz.py) and by any future upload endpoint.

★ WHY THIS EXISTS. fiber_routes holds 64,836 routes, but the lane that WRITES
most of them (infrastructure_discovery._save_route) never writes the
`coordinates` column at all — it stores start/end points only, which is why so
much of the table is 2-point synthetic segments. Measured 2026-09-03: Uniti
69 routes / 9 multi-vertex / max 5 points; Crown Castle 125 / 22 / 6;
123Net 8 / 8 / 5. The carriers that DO look right on the map (Zayo, max
27,388 vertices; Bluebird, 1,053; Peninsula Fiber Network, 1,569) are the ones
loaded from surveyed KMZ.

So the way to make a carrier real on the map is to ingest its surveyed
geometry — which is exactly what the white-glove workflow produces, and what
the archive of old KMZ files already contains.

★ IDENTITY. Each route is fingerprinted from its OWN geometry and name, not
from the crawl that found it or the file it arrived in. Re-ingesting the same
KMZ produces the same uids and writes nothing new; re-exporting the same route
into a differently-named file still dedups. This follows the rule established
by migrations/2026-08-12_fiber_route_upstream_uid.sql — identity comes from the
asset, never from where we were standing when we saw it.

KML shapes handled, all verified against a real 1.7MB carrier export
(1,664 placemarks / 619 LineStrings / 4 MultiGeometry):
  - default-namespaced KML 2.2 and un-namespaced KML
  - <LineString>, <LinearRing>, and <MultiGeometry> containing either
  - <ExtendedData><SchemaData><SimpleData name="Owner">…  (Esri exports)
  - <ExtendedData><Data name="…"><value>…                 (Google Earth)
  - enclosing <Folder><name> as fallback context
Points and Polygons are deliberately IGNORED — this table is routes.
"""
from __future__ import annotations

import hashlib
import io
import math
import re
import xml.etree.ElementTree as ET
import zipfile

__all__ = ["parse_kml_bytes", "parse_kmz_bytes", "parse_bytes", "route_uid",
           "haversine_miles", "OWNER_KEYS", "TYPE_KEYS"]

# ExtendedData keys that name the carrier, in priority order. Lowercased on
# comparison; the first one present wins.
OWNER_KEYS = ("owner", "provider", "carrier", "operator", "company",
              "ownername", "owner_name")
# ExtendedData keys that describe what the line IS.
TYPE_KEYS = ("type", "cabletype", "cable_type", "routetype", "route_type",
             "category", "status")

_LOCAL = re.compile(r"\{.*\}")


def _tag(el) -> str:
    """Local tag name, namespace stripped."""
    return _LOCAL.sub("", el.tag)


def _find_all(root, name):
    for el in root.iter():
        if _tag(el) == name:
            yield el


def _text(el):
    return (el.text or "").strip() if el is not None else ""


def _parse_coord_text(txt):
    """KML <coordinates> → [[lng, lat], ...].

    KML is whitespace-separated `lng,lat[,alt]` triples, and real exports use
    spaces, newlines and tabs interchangeably. Altitude is dropped. Anything
    unparseable or out of range is skipped rather than poisoning the route —
    one bad vertex must not stretch a route's bounding box across the planet.
    """
    out = []
    for tok in (txt or "").replace("\n", " ").replace("\t", " ").split():
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        try:
            lng, lat = float(parts[0]), float(parts[1])
        except (TypeError, ValueError):
            continue
        if not (-180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0):
            continue
        if math.isnan(lng) or math.isnan(lat):
            continue
        out.append([lng, lat])
    return out


def _extended_data(pm):
    """Flatten a Placemark's ExtendedData into {lowercased key: value}."""
    out = {}
    for el in pm.iter():
        t = _tag(el)
        if t == "SimpleData":
            k = (el.get("name") or "").strip().lower()
            if k:
                out.setdefault(k, _text(el))
        elif t == "Data":
            k = (el.get("name") or "").strip().lower()
            if not k:
                continue
            for child in el:
                if _tag(child) == "value":
                    out.setdefault(k, _text(child))
    return out


def _pick(data, keys):
    for k in keys:
        v = (data.get(k) or "").strip()
        if v:
            return v
    return ""


def haversine_miles(coords):
    """Great-circle length of a polyline, in miles."""
    if not coords or len(coords) < 2:
        return 0.0
    total = 0.0
    for (lng1, lat1), (lng2, lat2) in zip(coords, coords[1:]):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = p2 - p1
        dl = math.radians(lng2 - lng1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        total += 3958.7613 * 2 * math.asin(min(1.0, math.sqrt(a)))
    return round(total, 3)


def route_uid(name, provider, coords):
    """Stable fingerprint for one physical route.

    Derived from the geometry itself plus the carrier and name, so the SAME
    route re-ingested from a re-exported file dedups, while two genuinely
    different segments with the same name stay distinct. Coordinates are
    rounded to ~1m (5dp) before hashing so a re-export that shifts the last
    decimal place does not mint a second row for one line.
    """
    geo = ";".join("%.5f,%.5f" % (c[0], c[1]) for c in coords)
    basis = "|".join([(provider or "").strip().lower(),
                      (name or "").strip().lower(), geo])
    return hashlib.md5(basis.encode("utf-8")).hexdigest()


def parse_kml_bytes(raw, default_provider=None, default_route_type="metro"):
    """Parse KML bytes into route dicts. Never raises on malformed content
    below the root — a placemark that cannot be read is skipped, and the rest
    of the file still ingests.

    Raises ET.ParseError only if the document itself is not XML, which the
    caller should surface: silently returning [] for an unreadable file is the
    "healthy, nothing new" lie that hid the fiber discovery lane for 73 days.
    """
    root = ET.fromstring(raw)
    routes = []

    # Folder context: nearest enclosing <Folder>/<Document> name, used when a
    # placemark has no name of its own.
    folder_of = {}
    for parent in root.iter():
        if _tag(parent) in ("Folder", "Document"):
            fname = ""
            for child in parent:
                if _tag(child) == "name":
                    fname = _text(child)
                    break
            if fname:
                for pm in parent.iter():
                    if _tag(pm) == "Placemark":
                        folder_of.setdefault(id(pm), fname)

    for pm in _find_all(root, "Placemark"):
        try:
            name = ""
            for child in pm:
                if _tag(child) == "name":
                    name = _text(child)
                    break
            data = _extended_data(pm)
            provider = _pick(data, OWNER_KEYS) or default_provider or ""
            rtype = _pick(data, TYPE_KEYS) or ""
            folder = folder_of.get(id(pm), "")

            # Every LineString/LinearRing under this placemark, including the
            # ones inside a MultiGeometry. Each becomes its own route: they are
            # separate physical spans and a bounding box over all of them would
            # claim ground the fibre never touches.
            for geom in pm.iter():
                if _tag(geom) not in ("LineString", "LinearRing"):
                    continue
                coords = []
                for c in geom.iter():
                    if _tag(c) == "coordinates":
                        coords = _parse_coord_text(c.text)
                        break
                if len(coords) < 2:
                    continue
                label = name or folder or "Fiber route"
                routes.append({
                    "name": label[:200],
                    "provider": (provider or "Unknown")[:100],
                    "route_type": (rtype or default_route_type)[:50],
                    "coordinates": coords,
                    "start_lng": coords[0][0], "start_lat": coords[0][1],
                    "end_lng": coords[-1][0], "end_lat": coords[-1][1],
                    "min_lng": min(c[0] for c in coords),
                    "max_lng": max(c[0] for c in coords),
                    "min_lat": min(c[1] for c in coords),
                    "max_lat": max(c[1] for c in coords),
                    "distance_miles": haversine_miles(coords),
                    "vertices": len(coords),
                    "folder": folder,
                    "upstream_uid": route_uid(label, provider, coords),
                })
        except Exception:
            # One unreadable placemark must not sink the file.
            continue
    return routes


def parse_kmz_bytes(raw, **kw):
    """Parse a .kmz (a zip holding one or more .kml). Every KML member is
    parsed and the results concatenated."""
    out = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        members = [n for n in z.namelist() if n.lower().endswith(".kml")]
        for n in members:
            out.extend(parse_kml_bytes(z.read(n), **kw))
    return out


def parse_bytes(raw, **kw):
    """Dispatch on content, not on filename: a .kml that is really a zip (and
    the reverse) is common in files that have been round-tripped through
    Earth/Esri tooling."""
    if raw[:2] == b"PK":
        return parse_kmz_bytes(raw, **kw)
    return parse_kml_bytes(raw, **kw)
