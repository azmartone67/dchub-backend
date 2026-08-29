#!/usr/bin/env python3
"""Rebuild data/geo/us_state_boundaries.json.gz from the Census source.

Run this only to adopt a new Census vintage. State boundaries do not move, so
the committed artifact is expected to sit unchanged for years — this script
exists so the artifact is REPRODUCIBLE, not because it needs running.

    python3 scripts/build_us_state_boundaries.py

Source: the Census cartographic boundary file at 1:500,000, the highest-
resolution generalised boundary Census publishes (~150 m positional error).
TIGER/Line proper is ~10x larger for accuracy nobody siting a data centre can
use.

The shapefile reader is written out longhand because this repo has no
geopandas, no GDAL and no shapefile package, and adding one to a Flask image
for a build step that runs once every few years is the wrong trade.
"""
import io
import gzip
import json
import os
import struct
import urllib.request
import zipfile

VINTAGE = "2023"
RESOLUTION = "500k"
SOURCE_URL = ("https://www2.census.gov/geo/tiger/GENZ%s/shp/cb_%s_us_state_%s.zip"
              % (VINTAGE, VINTAGE, RESOLUTION))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "geo", "us_state_boundaries.json.gz")

# 1e-4 degrees ~= 11 m of latitude. Quantising is what makes the delta encoding
# compress; the residual sits far inside the source's own ~150 m generalisation,
# so it costs no accuracy that was ever there.
SCALE = 10000
SHAPE_TYPE_POLYGON = 5


def read_dbf(buf):
    """Attribute rows from the dBase III table (the .dbf half of a shapefile)."""
    n_records, header_len, record_len = struct.unpack("<I H H", buf[4:12])
    fields, off = [], 32
    while buf[off] != 0x0D:                       # 0x0D terminates the field list
        descriptor = buf[off:off + 32]
        fields.append((descriptor[:11].split(b"\x00")[0].decode("ascii"),
                       descriptor[16]))
        off += 32
    rows, pos = [], header_len
    for _ in range(n_records):
        row, p = {}, pos + 1                      # +1 skips the deletion flag
        for name, width in fields:
            row[name] = buf[p:p + width].decode("latin-1").strip()
            p += width
        rows.append(row)
        pos += record_len
    return rows


def read_shp(buf):
    """Rings per record from the .shp half. Ring = [(lon, lat), ...]."""
    pos, end_of_file, out = 100, len(buf), []     # 100-byte file header
    while pos < end_of_file:
        _record_number, content_len = struct.unpack(">I I", buf[pos:pos + 8])
        pos += 8
        record_end = pos + content_len * 2        # length is in 16-bit words
        if struct.unpack("<I", buf[pos:pos + 4])[0] != SHAPE_TYPE_POLYGON:
            out.append([])
            pos = record_end
            continue
        p = pos + 4 + 32                          # skip shape type + bbox
        n_parts, n_points = struct.unpack("<I I", buf[p:p + 8])
        p += 8
        parts = list(struct.unpack("<%dI" % n_parts, buf[p:p + 4 * n_parts]))
        p += 4 * n_parts
        xy = struct.unpack("<%dd" % (n_points * 2), buf[p:p + 16 * n_points])
        parts.append(n_points)
        out.append([[(xy[2 * j], xy[2 * j + 1]) for j in range(parts[i], parts[i + 1])]
                    for i in range(n_parts)])
        pos = record_end
    return out


def encode(rings):
    """Quantise to 1/SCALE degrees and delta-encode into one flat int list.

    gzip compresses runs of small integers far better than full-precision
    decimal strings: 1.9 MB -> 660 KB, against 5.5 MB -> 1.4 MB for the obvious
    [[lon, lat], ...] shape.
    """
    out = []
    for ring in rings:
        flat, prev_x, prev_y = [], 0, 0
        for lon, lat in ring:
            x, y = round(lon * SCALE), round(lat * SCALE)
            flat.append(x - prev_x)
            flat.append(y - prev_y)
            prev_x, prev_y = x, y
        out.append(flat)
    return out


def main():
    print("fetching %s" % SOURCE_URL)
    with urllib.request.urlopen(SOURCE_URL, timeout=180) as resp:
        archive = zipfile.ZipFile(io.BytesIO(resp.read()))
    shp = dbf = None
    for name in archive.namelist():
        if name.endswith(".shp"):
            shp = archive.read(name)
        elif name.endswith(".dbf"):
            dbf = archive.read(name)
    if shp is None or dbf is None:
        raise SystemExit("archive is missing .shp or .dbf")

    attrs, geoms = read_dbf(dbf), read_shp(shp)
    if len(attrs) != len(geoms):
        raise SystemExit("attribute/geometry count mismatch")

    payload = {
        "source": "US Census Bureau cartographic boundary file cb_%s_us_state_%s"
                  % (VINTAGE, RESOLUTION),
        "source_url": SOURCE_URL,
        "vintage": VINTAGE,
        "resolution": RESOLUTION,
        "scale": SCALE,
        "encoding": "per-ring flat [dx, dy, ...] deltas of round(deg * scale), "
                    "first delta from (0, 0); lon before lat",
        "areas": {},
    }
    for attr, rings in zip(attrs, geoms):
        code = attr.get("STUSPS", "").strip().upper()
        if code and rings:
            payload["areas"][code] = {"name": attr.get("NAME", ""),
                                      "rings": encode(rings)}

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    # mtime=0 so rebuilding identical input produces an identical file rather
    # than a spurious diff.
    with open(OUT, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(raw)
    print("wrote %s — %d areas, %.0f KB"
          % (OUT, len(payload["areas"]), os.path.getsize(OUT) / 1024))


if __name__ == "__main__":
    main()
