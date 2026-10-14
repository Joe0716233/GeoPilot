"""Rebuild the bundled London sample under examples/data/london/ deterministically.

Real inputs (publisher-owned, see DATA_SOURCES.md):
  --boroughs  GLA "London Boroughs" GeoJSON (EPSG:4326, fields name / gss_code), OGL v3.0
  --chargers  GLA "Rapid_charging_points.gpkg" (Creative Commons Attribution)

Synthetic inputs are generated here with fixed seeds and are NOT observations of London:
boundary probes, an operator feed derived from the historic charger names, and demand points.

Usage:
  # Offline: regenerate only the synthetic files from the bundled real subsets (no other inputs needed)
  python scripts/prepare_data.py --synthetic-only
  # Rebuild everything from files you hold; --retrieved-at is the date you obtained them (YYYY-MM-DD)
  python scripts/prepare_data.py --boroughs london_boroughs.geojson --chargers Rapid_charging_points.gpkg \
      --retrieved-at 2026-09-27
  # Optional, network: fetch current official files first (labelled with today's UTC date)
  python scripts/prepare_data.py --download raw_dir
Re-running with identical inputs produces byte-identical outputs. Downloads are mutable publisher files and
may differ from the bundled 2026-09-27 snapshot; the bundled sample is pinned by manifest.json hashes.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import math
import random
import sqlite3
import sys
import urllib.request
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import Point, shape

from geopilot.tools.spatial import local_projection

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "examples" / "data" / "london"
SEED = 20261014
BOROUGHS_URL = ("https://gis.london.gov.uk/arcgis/rest/services/apps/webmap_context_layer/MapServer/3/"
                "query?where=1%3D1&outFields=*&outSR=4326&f=geojson")
CHARGERS_URL = ("https://data.london.gov.uk/download/2lzpg/8ef9c743-c01d-4329-8239-8f858ff4de53/"
                "Rapid_charging_points.gpkg")
SAMPLE_BOROUGHS = ["Camden", "City of London", "Hackney", "Islington", "Kensington and Chelsea",
                   "Lambeth", "Southwark", "Tower Hamlets", "Westminster"]
STUDY_ZONE = "Southwark"
ALIASES = {"Richmond": "Richmond upon Thames", "Hammersmith & Fulham": "Hammersmith and Fulham"}
PROBE_BOROUGHS = ["Camden", "Islington", "Lambeth", "Southwark", "Westminster"]
PROBE_OFFSETS_M = [100.0, 10.0, -30.0, -150.0]  # positive = inside the claimed borough
FEED_VARIANTS = 7


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def round_coords(obj, places=6):
    if isinstance(obj, (list, tuple)):
        return [round_coords(v, places) for v in obj]
    return round(obj, places) if isinstance(obj, float) else obj


def field(properties: dict, wanted: str):
    lookup = {k.lower(): v for k, v in properties.items()}
    return lookup.get(wanted.lower())


def build_boroughs(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    features = {}
    for feature in data["features"]:
        props = feature.get("properties") or {}
        name = field(props, "name") or field(props, "borough")
        if name in SAMPLE_BOROUGHS:
            geom = shape(feature["geometry"])
            if not geom.is_valid:
                raise ValueError(f"source geometry for {name} is invalid")
            features[name] = {"type": "Feature",
                              "properties": {"zone_id": name, "name": name,
                                             "gss_code": field(props, "gss_code") or ""},
                              "geometry": {"type": feature["geometry"]["type"],
                                           "coordinates": round_coords(feature["geometry"]["coordinates"])}}
    missing = [b for b in SAMPLE_BOROUGHS if b not in features]
    if missing:
        raise ValueError("boroughs missing from source: " + ", ".join(missing))
    return [features[n] for n in sorted(features)]


def read_chargers(path: Path) -> tuple[list[dict], float]:
    to_wgs = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT fid, borough, easting, northing, latitude, longtitude, sitename, siteid "
                          "FROM Rapid_charging_points ORDER BY fid").fetchall()
    out, worst = [], 0.0
    from geopilot.tools.spatial import geodesic_distance_m
    for fid, borough, easting, northing, lat, lon, name, site_id in rows:
        plon, plat = to_wgs.transform(float(easting), float(northing))
        gap = geodesic_distance_m(float(lon), float(lat), plon, plat)
        if gap > 10:
            raise ValueError(f"fid {fid}: lon/lat columns and easting/northing disagree by {gap:.1f} m")
        worst = max(worst, gap)
        label = (borough or "").strip()
        out.append({"source_id": str(fid), "name": (name or "").strip(), "address": (name or "").strip(),
                    "category": "existing_station", "lon": float(lon), "lat": float(lat), "city": "London",
                    "district": ALIASES.get(label, label), "site_id": str(site_id or "")})
    return out, worst


def jitter(rng: random.Random, lon: float, lat: float, metres: float) -> tuple[float, float]:
    local = local_projection(lon, lat)
    angle = rng.uniform(0, 2 * math.pi)
    return local.unproject(metres * math.cos(angle), metres * math.sin(angle))


def build_probes(boroughs: dict[str, dict]) -> list[dict]:
    rows = []
    for name in PROBE_BOROUGHS:
        geom = shape(boroughs[name]["geometry"])
        ring = list(geom.exterior.coords)
        for slot, fraction in enumerate((0.15, 0.40, 0.65, 0.90)):
            i = int(len(ring) * fraction)
            (lon, lat), (lon2, lat2) = ring[i], ring[(i + 3) % len(ring)]
            local = local_projection(lon, lat)
            x2, y2 = local.project(lon2, lat2)
            norm = math.hypot(x2, y2)
            if norm < 1:
                continue
            nx, ny = -y2 / norm, x2 / norm
            probe = Point(*local.unproject(nx, ny))  # 1 m along candidate normal
            if not geom.covers(probe):
                nx, ny = -nx, -ny  # make positive offsets point into the borough
            for offset in PROBE_OFFSETS_M:
                plon, plat = local.unproject(nx * offset, ny * offset)
                plon, plat = round(plon, 6), round(plat, 6)
                if geom.covers(Point(plon, plat)) != (offset > 0):
                    continue  # concave boundary put the probe on the wrong side; drop rather than mislabel
                rows.append({"source_id": f"P-{name[:3].upper()}-{slot + 1}-{int(offset):+d}",
                             "name": f"Synthetic probe {name} {slot + 1} ({offset:+.0f} m)",
                             "category": "probe", "lon": f"{plon:.6f}", "lat": f"{plat:.6f}",
                             "claimed_zone": name, "intended_offset_m": f"{offset:.1f}"})
    return rows


def build_feed(chargers: list[dict]) -> list[dict]:
    rng = random.Random(SEED)
    rows = []
    swaps = (("Road", "Rd"), ("Street", "St"), ("Avenue", "Ave"), ("Station", "Stn"))
    for n, row in enumerate(sorted(chargers, key=lambda r: int(r["source_id"]))[:28]):
        variant = n % FEED_VARIANTS
        lon, lat = row["lon"], row["lat"]
        name, address, note = row["name"], row["address"], "copy"
        if variant == 0:
            lon, lat = jitter(rng, lon, lat, 8)
            note = "same name, ~8 m offset"
        elif variant == 1:
            lon, lat = jitter(rng, lon, lat, 15)
            name = name.upper()
            address = address.lower()
            note = "case change, ~15 m offset"
        elif variant == 2:
            for long_form, short in swaps:
                name, address = name.replace(long_form, short), address.replace(long_form, short)
            lon, lat = jitter(rng, lon, lat, 20)
            note = "abbreviated suffixes, ~20 m offset"
        elif variant == 3:
            lon, lat = jitter(rng, lon, lat, 10)
            name, address = "Synthetic Neighbour Cafe Car Park", ""
            note = "different place ~10 m away, no address"
        elif variant == 4:
            lon, lat = jitter(rng, lon, lat, 240)
            note = "same name but ~240 m away"
        elif variant == 5:
            lon = lat = ""
            note = "same name, coordinate missing"
        else:
            lon, lat = jitter(rng, lon, lat, 30)
            name = "Charging point - " + name
            note = "prefixed name, ~30 m offset"
        has_point = lon != ""
        rows.append({"source_id": f"F{n + 1:03d}", "name": name, "address": address,
                     "category": "existing_station", "city": "London",
                     "lon": f"{lon:.6f}" if has_point else "", "lat": f"{lat:.6f}" if has_point else "",
                     "derived_from_gla_source_id": row["source_id"], "construction_note": note})
    return rows


def build_demand(zone_geom) -> list[dict]:
    rng = random.Random(SEED + 1)
    minx, miny, maxx, maxy = zone_geom.bounds
    centre = zone_geom.centroid
    local = local_projection(centre.x, centre.y)

    def sample_inside(around=None, sigma_m=0.0):
        while True:
            if around is None:
                lon, lat = rng.uniform(minx, maxx), rng.uniform(miny, maxy)
            else:
                x, y = local.project(*around)
                lon, lat = local.unproject(rng.gauss(x, sigma_m), rng.gauss(y, sigma_m))
            if zone_geom.covers(Point(lon, lat)):
                return lon, lat

    hubs = [sample_inside() for _ in range(4)]
    rows = []

    def add(category, count, prefix, population=None):
        for i in range(count):
            hub = hubs[i % len(hubs)] if i % 5 < 3 else None
            lon, lat = sample_inside(hub, 700.0) if hub else sample_inside()
            rows.append({"source_id": f"{prefix}{i + 1:03d}", "name": f"Synthetic {category} point {i + 1}",
                         "category": category, "lon": f"{lon:.6f}", "lat": f"{lat:.6f}",
                         "population": str(rng.randrange(40, 520, 10)) if population else ""})

    add("population", 140, "D", population=True)
    add("residential", 45, "R")
    add("commercial", 30, "K")
    add("transit", 12, "T")
    return rows


OUTPUT_NAMES = ["boroughs_subset.geojson", "gla_chargers_subset.csv", "synthetic_boundary_probes.csv",
                "synthetic_operator_feed.csv", "synthetic_demand.csv"]
CHARGER_FIELDS = ["source_id", "name", "address", "category", "lon", "lat", "city", "district", "site_id"]


def download(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    targets = {"london_boroughs.geojson": BOROUGHS_URL, "Rapid_charging_points.gpkg": CHARGERS_URL}
    for filename, url in targets.items():
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https URLs
            (directory / filename).write_bytes(response.read())
    return directory / "london_boroughs.geojson", directory / "Rapid_charging_points.gpkg"


def write_synthetic(out: Path, features: list[dict], chargers: list[dict]) -> dict[str, int]:
    """Write the three synthetic files from borough features and charger rows (string lon/lat)."""
    by_name = {f["properties"]["name"]: f for f in features}
    probes = build_probes(by_name)
    write_csv(out / "synthetic_boundary_probes.csv",
              ["source_id", "name", "category", "lon", "lat", "claimed_zone", "intended_offset_m"], probes)
    feed = build_feed([{**c, "lon": float(c["lon"]), "lat": float(c["lat"])} for c in chargers])
    write_csv(out / "synthetic_operator_feed.csv",
              ["source_id", "name", "address", "category", "city", "lon", "lat",
               "derived_from_gla_source_id", "construction_note"], feed)
    demand = build_demand(shape(by_name[STUDY_ZONE]["geometry"]))
    write_csv(out / "synthetic_demand.csv", ["source_id", "name", "category", "lon", "lat", "population"], demand)
    return {"synthetic_boundary_probes": len(probes), "synthetic_operator_feed": len(feed),
            "synthetic_demand": len(demand)}


def write_manifest(out: Path, real_inputs: dict, counts: dict) -> dict:
    manifest = {
        "generated_by": "scripts/prepare_data.py", "seed": SEED, "study_zone": STUDY_ZONE,
        "note": ("Real inputs are publisher files as retrieved on the stated date; later downloads may differ. "
                 "Only the listed output files are hashed."),
        "real_inputs": real_inputs,
        "outputs": {n: {"sha256": sha256(out / n), "bytes": (out / n).stat().st_size} for n in OUTPUT_NAMES},
        "record_counts": counts,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                       encoding="utf-8", newline="\n")
    return manifest


def synthetic_only(out: Path) -> dict:
    """Offline regeneration of synthetic files from the bundled real subsets in ``out``."""
    manifest_path = out / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"{manifest_path} not found; --synthetic-only needs the bundled sample directory")
    previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name in ("boroughs_subset.geojson", "gla_chargers_subset.csv"):
        path = out / name
        if not path.is_file() or sha256(path) != previous["outputs"][name]["sha256"]:
            raise SystemExit(f"{name} is missing or does not match manifest.json; refusing to regenerate")
    features = json.loads((out / "boroughs_subset.geojson").read_text(encoding="utf-8"))["features"]
    with (out / "gla_chargers_subset.csv").open(encoding="utf-8", newline="") as handle:
        chargers = list(csv.DictReader(handle))
    counts = write_synthetic(out, features, chargers)
    counts = {"boroughs": len(features), "gla_chargers_subset": len(chargers), **counts}
    return write_manifest(out, previous["real_inputs"], counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--boroughs", type=Path)
    parser.add_argument("--chargers", type=Path)
    parser.add_argument("--download", type=Path, help="directory to fetch both official files into first")
    parser.add_argument("--retrieved-at", help="date (YYYY-MM-DD) the supplied source files were obtained")
    parser.add_argument("--synthetic-only", action="store_true",
                        help="regenerate synthetic files offline from the bundled real subsets in --out")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    if args.retrieved_at:
        try:
            datetime.date.fromisoformat(args.retrieved_at)
        except ValueError:
            parser.error("--retrieved-at must be YYYY-MM-DD")
    if args.synthetic_only:
        if args.boroughs or args.chargers or args.download:
            parser.error("--synthetic-only cannot be combined with source inputs")
        manifest = synthetic_only(args.out)
        print(json.dumps(manifest["record_counts"]))
        return 0
    if args.download:
        args.boroughs, args.chargers = download(args.download)
        retrieved_at = args.retrieved_at or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    else:
        retrieved_at = args.retrieved_at
        if not retrieved_at:
            parser.error("--retrieved-at is required when supplying --boroughs/--chargers files")
    if not (args.boroughs and args.chargers and args.boroughs.is_file() and args.chargers.is_file()):
        parser.error("provide --boroughs and --chargers files (or --download DIR, or --synthetic-only)")

    features = build_boroughs(args.boroughs)
    shapes = {f["properties"]["name"]: shape(f["geometry"]) for f in features}
    all_chargers, worst_gap = read_chargers(args.chargers)
    chargers = [c for c in all_chargers
                if c["district"] in shapes or any(g.covers(Point(c["lon"], c["lat"])) for g in shapes.values())]
    for c in chargers:
        c["lon"], c["lat"] = f"{c['lon']:.6f}", f"{c['lat']:.6f}"
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "boroughs_subset.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")) + "\n",
        encoding="utf-8", newline="\n")
    write_csv(out / "gla_chargers_subset.csv", CHARGER_FIELDS, chargers)
    counts = {"boroughs": len(features), "gla_chargers_subset": len(chargers),
              **write_synthetic(out, features, chargers)}
    real_inputs = {
        "boroughs": {"publisher": "Greater London Authority / Ordnance Survey", "licence": "OGL v3.0",
                     "retrieved_at": retrieved_at, "source_sha256": sha256(args.boroughs)},
        "chargers": {"publisher": "Greater London Authority",
                     "licence": "Creative Commons Attribution (version not stated by publisher)",
                     "retrieved_at": retrieved_at, "source_sha256": sha256(args.chargers),
                     "max_lonlat_vs_bng_disagreement_m": round(worst_gap, 3),
                     "source_record_count": len(all_chargers)},
    }
    manifest = write_manifest(out, real_inputs, counts)
    print(json.dumps(manifest["record_counts"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
