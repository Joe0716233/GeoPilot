"""CSV and GeoJSON adapters with explicit CRS handling.

Malformed records are reported together in one ``ValueError`` rather than silently dropped.
"""

from __future__ import annotations

import csv
import json
from math import isfinite
from pathlib import Path

from shapely.geometry import Point, mapping, shape

from geopilot.tools.spatial import WGS84, _crs_transformer, transform_geometry, transform_xy

from .schema import Entity, Zone

DEFAULT_CRS = WGS84


def _rows(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if suffix in {".geojson", ".json"}:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("type") != "FeatureCollection":
            raise ValueError("GeoJSON input must be a FeatureCollection")
        rows = []
        for feature in data.get("features", []):
            row = dict(feature.get("properties") or {})
            row["geometry"] = feature.get("geometry")
            rows.append(row)
        return rows
    raise ValueError(f"unsupported input format: {suffix or '(none)'}; use .csv or .geojson")


def _number(value) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"non-finite number: {value!r}")
    return number


def _duplicates(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    dup: set[str] = set()
    for item in ids:
        (dup if item in seen else seen).add(item)
    return sorted(dup)


def load_entities(path: str | Path, *, source: str, columns: dict[str, str] | None = None,
                  input_crs: str = DEFAULT_CRS) -> list[Entity]:
    """Load point records. ``columns`` maps Entity fields to source column names.

    CSV coordinates come from the ``lon``/``lat`` columns (read as x/y in ``input_crs``);
    GeoJSON uses the Point geometry. Output coordinates are always EPSG:4326.
    """
    columns = columns or {}
    entities: list[Entity] = []
    errors: list[str] = []
    for number, row in enumerate(_rows(Path(path)), start=1):
        def get(field: str):
            return row.get(columns.get(field, field))
        try:
            geometry = row.get("geometry")
            x = y = None
            if geometry:
                point = shape(geometry)
                if not isinstance(point, Point) or point.is_empty:
                    raise ValueError("geometry is not a Point")
                x, y = point.x, point.y
            else:
                x, y = _number(get("lon")), _number(get("lat"))
            if (x is None) != (y is None):
                raise ValueError("only one of lon/lat supplied")
            if x is not None:
                x, y = transform_xy(x, y, input_crs, WGS84)
            text = lambda f: str(get(f) if get(f) is not None else "").strip()  # noqa: E731
            entities.append(Entity(
                source=source, source_id=text("source_id") or str(number),
                name=text("name"), address=text("address"), category=text("category"),
                lon=x, lat=y, city=text("city"), district=text("district"),
                phone=text("phone"), url=text("url"),
                properties={k: v for k, v in row.items() if k != "geometry"}))
        except (ValueError, TypeError) as exc:
            errors.append(f"row {number}: {exc}")
    if errors:
        raise ValueError("invalid entity records:\n" + "\n".join(errors))
    duplicates = _duplicates([e.key for e in entities])
    if duplicates:
        raise ValueError("duplicate entity IDs: " + ", ".join(duplicates))
    return entities


def load_zones(path: str | Path, *, source: str, columns: dict[str, str] | None = None,
               input_crs: str = DEFAULT_CRS) -> list[Zone]:
    """Load polygon zones, validating geometry and converting to EPSG:4326."""
    columns = columns or {}
    zones: list[Zone] = []
    errors: list[str] = []
    for number, row in enumerate(_rows(Path(path)), start=1):
        try:
            if not row.get("geometry"):
                raise ValueError("missing geometry")
            geom = shape(row["geometry"])
            if input_crs.upper() != WGS84:
                geom = transform_geometry(geom, _crs_transformer(input_crs, WGS84))
            props = {k: v for k, v in row.items() if k != "geometry"}
            zones.append(Zone(
                zone_id=str(row.get(columns.get("zone_id", "zone_id")) or number),
                name=str(row.get(columns.get("name", "name")) or row.get(columns.get("zone_id", "zone_id")) or number),
                type=str(row.get(columns.get("type", "type")) or "area"),
                geometry=mapping(geom), source=source, properties=props))
        except (ValueError, TypeError, AttributeError) as exc:
            errors.append(f"row {number}: {exc}")
    if errors:
        raise ValueError("invalid zone records:\n" + "\n".join(errors))
    duplicates = _duplicates([z.zone_id for z in zones])
    if duplicates:
        raise ValueError("duplicate zone IDs: " + ", ".join(duplicates))
    return zones


def load_claims(path: str | Path, *, source: str, id_column: str = "source_id",
                zone_column: str = "claimed_zone") -> dict[str, str]:
    """Load ``entity key -> claimed zone id`` from a CSV of claims."""
    claims: dict[str, str] = {}
    for number, row in enumerate(_rows(Path(path)), start=1):
        identifier = (row.get(id_column) or "").strip()
        if not identifier:
            raise ValueError(f"claims row {number}: missing {id_column}")
        key = f"{source}:{identifier}"
        if key in claims:
            raise ValueError(f"duplicate claim for {key}")
        claims[key] = (row.get(zone_column) or "").strip()
    return claims
