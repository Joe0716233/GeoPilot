"""Small GIS primitives with declared coordinate systems and structured output.

Inputs and outputs are WGS 84 lon/lat (EPSG:4326). Metric quantities are computed either as
WGS 84 geodesic distances or in a local azimuthal-equidistant projection centred on the
analysis location, whose distance distortion is negligible at city scale (well below 0.1 %
within a few tens of kilometres). Distances are straight-line, never travel distance.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from math import isfinite

import numpy as np
import shapely
from pyproj import Geod, Transformer
from shapely.geometry import Point, mapping, shape
from shapely.geometry.base import BaseGeometry

WGS84 = "EPSG:4326"
# Local projections are only trusted over modest extents.
MAX_LOCAL_EXTENT_M = 100_000.0
DENSIFY_DEG = 0.001  # ~70-110 m; boundary densification step before projecting
_GEOD = Geod(ellps="WGS84")


def check_lonlat(lon: float, lat: float) -> None:
    if isinstance(lon, bool) or isinstance(lat, bool):
        raise ValueError("coordinates must be numbers")
    if not (isfinite(lon) and isfinite(lat)):
        raise ValueError("coordinates must be finite")
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError(f"coordinates outside WGS 84 range: lon={lon}, lat={lat}")


def transform_geometry(geom: BaseGeometry, transformer: Transformer) -> BaseGeometry:
    def apply(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])
    return shapely.transform(geom, apply)


@dataclass(frozen=True)
class LocalProjection:
    """Azimuthal-equidistant metric projection centred on (lon0, lat0); units are metres."""

    lon0: float
    lat0: float
    _forward: Transformer
    _inverse: Transformer

    def project(self, lon: float, lat: float) -> tuple[float, float]:
        return self._forward.transform(lon, lat)

    def unproject(self, x: float, y: float) -> tuple[float, float]:
        return self._inverse.transform(x, y)

    def project_geometry(self, geom: BaseGeometry) -> BaseGeometry:
        return transform_geometry(geom, self._forward)

    def unproject_geometry(self, geom: BaseGeometry) -> BaseGeometry:
        return transform_geometry(geom, self._inverse)


@lru_cache(maxsize=2048)
def local_projection(lon0: float, lat0: float) -> LocalProjection:
    check_lonlat(lon0, lat0)
    crs = f"+proj=aeqd +lat_0={lat0!r} +lon_0={lon0!r} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    return LocalProjection(lon0, lat0, Transformer.from_crs(WGS84, crs, always_xy=True),
                           Transformer.from_crs(crs, WGS84, always_xy=True))


@lru_cache(maxsize=64)
def _crs_transformer(source_crs: str, target_crs: str) -> Transformer:
    return Transformer.from_crs(source_crs, target_crs, always_xy=True)


def transform_xy(x: float, y: float, source_crs: str, target_crs: str = WGS84) -> tuple[float, float]:
    """Transform one coordinate pair; axis order is always (x/lon/easting, y/lat/northing)."""
    if source_crs.upper() == target_crs.upper():
        return x, y
    out = _crs_transformer(source_crs, target_crs).transform(x, y)
    if not all(isfinite(v) for v in out):
        raise ValueError(f"coordinate ({x}, {y}) cannot be transformed from {source_crs} to {target_crs}")
    return out


def geodesic_distance_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """WGS 84 ellipsoidal distance in metres."""
    check_lonlat(lon1, lat1)
    check_lonlat(lon2, lat2)
    return _GEOD.inv(lon1, lat1, lon2, lat2)[2]


def _require_polygon(polygon_geojson: dict) -> BaseGeometry:
    polygon = shape(polygon_geojson)
    if polygon.geom_type not in {"Polygon", "MultiPolygon"} or polygon.is_empty or not polygon.is_valid:
        raise ValueError("a valid, non-empty Polygon or MultiPolygon is required")
    return polygon


def point_polygon_relation(lon: float, lat: float, polygon: BaseGeometry) -> dict:
    """Containment and metric distance to the polygon boundary for a shapely polygon."""
    check_lonlat(lon, lat)
    point = Point(lon, lat)
    local = local_projection(lon, lat)
    # Edges are straight in lon/lat; densify so the projected boundary follows them faithfully.
    boundary = shapely.segmentize(polygon.boundary, DENSIFY_DEG)
    boundary_m = local.project_geometry(boundary).distance(Point(0.0, 0.0))
    inside = bool(polygon.covers(point))
    return {"inside_or_boundary": inside,
            "on_boundary": bool(polygon.boundary.distance(point) == 0.0),
            "distance_to_boundary_m": boundary_m,
            "signed_distance_m": boundary_m if inside else -boundary_m,
            "crs": WGS84}


def point_in_polygon(lon: float, lat: float, polygon_geojson: dict) -> dict:
    """Is the point inside (boundary counts as inside) and how far is it from the boundary?

    ``signed_distance_m`` is positive inside and negative outside.
    """
    result = point_polygon_relation(lon, lat, _require_polygon(polygon_geojson))
    return {**result, "distance_to_boundary_m": round(result["distance_to_boundary_m"], 2),
            "signed_distance_m": round(result["signed_distance_m"], 2)}


def buffer_point(lon: float, lat: float, distance_m: float) -> dict:
    """Circular buffer (polygon approximation, 64 segments per quarter) of a given radius in metres."""
    check_lonlat(lon, lat)
    if isinstance(distance_m, bool) or not isfinite(distance_m) or distance_m < 0:
        raise ValueError("distance_m must be a finite non-negative number")
    local = local_projection(lon, lat)
    circle = Point(0.0, 0.0).buffer(distance_m, quad_segs=64)
    return {"geometry": mapping(local.unproject_geometry(circle)), "crs": WGS84, "radius_m": distance_m}


def nearest_point(lon: float, lat: float, candidates: list[dict]) -> dict | None:
    """Nearest candidate (dicts with ``lon``/``lat``) by geodesic distance; ties keep the first."""
    check_lonlat(lon, lat)
    best = None
    for index, item in enumerate(candidates):
        d = geodesic_distance_m(lon, lat, float(item["lon"]), float(item["lat"]))
        if best is None or d < best[0]:
            best = (d, index, item)
    if best is None:
        return None
    return {"candidate": best[2], "distance_m": round(best[0], 2), "distance_kind": "geodesic_straight_line"}


def check_local_extent(geom: BaseGeometry, local: LocalProjection) -> None:
    minx, miny, maxx, maxy = local.project_geometry(geom).bounds
    if max(abs(minx), abs(maxx), abs(miny), abs(maxy)) > MAX_LOCAL_EXTENT_M:
        raise ValueError("study area is too large for a single local metric projection (> 100 km from centre)")
