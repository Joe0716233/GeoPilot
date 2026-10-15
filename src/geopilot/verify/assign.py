"""Zone assignment: which supplied zones contain each point? Independent of claim verification.

``assign_zones`` needs no claimed zone. For every input record it lists every supplied zone whose
polygon covers the point, with the relation ``INSIDE`` (interior) or ``BOUNDARY`` (on an outer
edge, a hole edge or an island edge). It never picks a "first" match. Matches are ordered by
``zone_id``.

Status precedence (first rule that applies):

1. ``NO_COORDINATE``      the record has no lon/lat; it is kept, with no matches.
2. ``OUTSIDE_ALL_ZONES``  no supplied zone covers the point.
3. ``MULTIPLE_MATCHES``   two or more zones cover it, whatever the relations; this outranks
                          ``ON_BOUNDARY``, so a point on a boundary shared by two zones is
                          ``MULTIPLE_MATCHES`` with both matches marked ``BOUNDARY``.
4. ``ON_BOUNDARY``        exactly one zone covers it and the point is on that zone's boundary.
5. ``ASSIGNED``           exactly one zone covers it, in its interior.

Only ``ASSIGNED`` sets ``assigned_zone_id``. The relations are exact Shapely ``covers``/``touches``
predicates on the supplied WGS 84 coordinates and polygons: there is no tolerance, and no
coordinate error or boundary-version difference is measured or modelled. Inputs in other CRSs
must be converted on the way in with the loaders' ``input_crs`` argument.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, fields

import shapely
from shapely.geometry import Point
from shapely.prepared import prep

from geopilot.data.schema import Entity, Zone
from geopilot.tools.spatial import WGS84

ASSIGN_STATUSES = ("ASSIGNED", "MULTIPLE_MATCHES", "ON_BOUNDARY", "OUTSIDE_ALL_ZONES", "NO_COORDINATE")
ASSIGN_ERROR_CODES = ("INVALID_ZONES", "INVALID_ENTITY", "INVALID_ASSIGN_CONFIG", "RESOURCE_LIMIT_EXCEEDED")
STATUS_PRECEDENCE = ("NO_COORDINATE > OUTSIDE_ALL_ZONES > MULTIPLE_MATCHES > ON_BOUNDARY > ASSIGNED; "
                     "MULTIPLE_MATCHES outranks ON_BOUNDARY, so a shared boundary lists every zone as BOUNDARY.")
ASSIGN_UNCERTAINTY_NOTE = ("Exact point-in-polygon predicates on the supplied coordinates and zone geometry, with "
                           "no tolerance. Coordinate error and boundary-version differences are neither measured "
                           "nor modelled; ASSIGNED is not a guarantee that the real-world location is in the zone.")


class AssignInputError(ValueError):
    """Refused zone-assignment input. ``code`` is one of ``ASSIGN_ERROR_CODES``."""

    def __init__(self, code: str, detail: str, **context):
        if code not in ASSIGN_ERROR_CODES:
            raise AssertionError(f"unknown assignment error code {code!r}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.context = context

    def __reduce__(self):
        return _rebuild_error, (self.code, self.detail, self.context)


def _rebuild_error(code: str, detail: str, context: dict) -> AssignInputError:
    return AssignInputError(code, detail, **context)


@dataclass(frozen=True)
class AssignConfig:
    """Work bounds (each may only be lowered below its default) and optional original-coordinate fields.

    ``original_x_field``/``original_y_field`` name the ``Entity.properties`` keys that hold the
    coordinates as uploaded (the loaders keep the raw row there); ``original_crs`` labels them.
    """

    max_records: int = 100_000
    max_zones: int = 10_000
    max_pairs: int = 5_000_000
    max_zone_vertices: int = 200_000
    max_total_zone_vertices: int = 2_000_000
    original_x_field: str | None = None
    original_y_field: str | None = None
    original_crs: str | None = None

    def __post_init__(self):
        for f in fields(self):
            if not f.name.startswith("max_"):
                continue
            value = getattr(self, f.name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= f.default:
                raise AssignInputError("INVALID_ASSIGN_CONFIG", f"{f.name} must be an integer between 1 and {f.default}")
        for name in ("original_x_field", "original_y_field", "original_crs"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise AssignInputError("INVALID_ASSIGN_CONFIG", f"{name} must be a non-blank string or None")
        if (self.original_x_field is None) != (self.original_y_field is None):
            raise AssignInputError("INVALID_ASSIGN_CONFIG", "original_x_field and original_y_field go together")


def _bound(what: str, amount: int, limit: int, hint: str) -> None:
    if amount > limit:
        raise AssignInputError("RESOURCE_LIMIT_EXCEEDED", f"{what} = {amount} exceeds the limit {limit}; {hint}",
                               quantity=what, amount=amount, limit=limit)


def _check_zones(zones, config: AssignConfig) -> list[Zone]:
    if not isinstance(zones, (list, tuple)):
        raise AssignInputError("INVALID_ZONES", "zones must be a list or tuple of Zone records (see load_zones)")
    if not zones:
        raise AssignInputError("INVALID_ZONES", "no zones supplied; every point would be OUTSIDE_ALL_ZONES. "
                                                "Load the zone layer with load_zones first")
    _bound("zones", len(zones), config.max_zones, "split the zone layer")
    for index, zone in enumerate(zones):
        if not isinstance(zone, Zone):
            raise AssignInputError("INVALID_ZONES", f"zone #{index} is a {type(zone).__name__}, not a Zone; "
                                                    "load zones with load_zones so geometry is validated")
        try:
            zone.shape
        except AttributeError:  # built without validation (e.g. Zone.model_construct)
            raise AssignInputError("INVALID_ZONES", f"zone #{index} ({zone.zone_id!r}) has unvalidated geometry; "
                                                    "construct it normally or load it with load_zones") from None
    duplicates = sorted(k for k, n in Counter(z.zone_id for z in zones).items() if n > 1)
    if duplicates:
        raise AssignInputError("INVALID_ZONES", "duplicate zone IDs: " + ", ".join(duplicates) +
                               "; zone IDs must be unique so every match is unambiguous", zone_ids=duplicates)
    total = 0
    for zone in zones:
        vertices = int(shapely.get_num_coordinates(zone.shape))
        _bound(f"vertices of zone {zone.zone_id}", vertices, config.max_zone_vertices, "simplify the zone polygon")
        total += vertices
    _bound("total zone vertices", total, config.max_total_zone_vertices, "simplify or split the zone layer")
    return sorted(zones, key=lambda z: z.zone_id)


def _check_entities(entities, config: AssignConfig) -> None:
    if not isinstance(entities, (list, tuple)):
        raise AssignInputError("INVALID_ENTITY", "entities must be a list or tuple of Entity records")
    _bound("records", len(entities), config.max_records, "split the input")
    seen: set[str] = set()
    for index, entity in enumerate(entities):
        if not isinstance(entity, Entity):
            raise AssignInputError("INVALID_ENTITY", f"record #{index} is a {type(entity).__name__}, not an Entity")
        if entity.key in seen:
            raise AssignInputError("INVALID_ENTITY", f"duplicate entity key: {entity.key}", keys=[entity.key])
        seen.add(entity.key)


def _original(entity: Entity, config: AssignConfig) -> dict | None:
    if config.original_x_field is None:
        return None
    x = entity.properties.get(config.original_x_field)
    y = entity.properties.get(config.original_y_field)
    if x in (None, "") and y in (None, ""):
        return None
    return {"x": x, "y": y, "crs": config.original_crs, "fields": [config.original_x_field, config.original_y_field]}


def _status(matches: list[dict]) -> tuple[str, str]:
    relations = {m["relation"] for m in matches}
    if not matches:
        return "OUTSIDE_ALL_ZONES", "OUTSIDE_ALL_SUPPLIED_ZONES"
    if len(matches) > 1:
        reason = ("INSIDE_SEVERAL_ZONES" if relations == {"INSIDE"} else
                  "ON_SHARED_BOUNDARY" if relations == {"BOUNDARY"} else "INSIDE_ONE_ON_BOUNDARY_OF_ANOTHER")
        return "MULTIPLE_MATCHES", reason
    if relations == {"BOUNDARY"}:
        return "ON_BOUNDARY", "ON_BOUNDARY_OF_ONE_ZONE"
    return "ASSIGNED", "INSIDE_ONE_ZONE"


def assign_zones(entities: list[Entity] | tuple[Entity, ...], zones: list[Zone] | tuple[Zone, ...],
                 config: AssignConfig | None = None) -> dict:
    """Assign every record to all supplied zones covering it; one result per input, in input order."""
    config = AssignConfig() if config is None else config
    if not isinstance(config, AssignConfig):
        raise AssignInputError("INVALID_ASSIGN_CONFIG", "config must be an AssignConfig or None")
    ordered = _check_zones(zones, config)
    _check_entities(entities, config)
    located = sum(1 for e in entities if e.has_point)
    _bound("record x zone pairs", located * len(ordered), config.max_pairs, "split the input or the zone layer")
    tests = [(zone, zone.shape.bounds, prep(zone.shape)) for zone in ordered]
    results = []
    for entity in entities:
        matches = []
        if entity.has_point:
            x, y = entity.lon, entity.lat
            point = Point(x, y)
            for zone, (minx, miny, maxx, maxy), prepared in tests:
                if minx <= x <= maxx and miny <= y <= maxy and prepared.covers(point):
                    matches.append({"zone_id": zone.zone_id, "zone_name": zone.name,
                                    "relation": "BOUNDARY" if prepared.touches(point) else "INSIDE"})
            status, reason = _status(matches)
        else:
            status, reason = "NO_COORDINATE", "NO_COORDINATE"
        results.append({"record_id": entity.key, "source": entity.source, "source_id": entity.source_id,
                        "name": entity.name, "category": entity.category,
                        "lon": entity.lon, "lat": entity.lat, "crs": WGS84,
                        "original_coordinates": _original(entity, config),
                        "status": status, "reason_code": reason,
                        "assigned_zone_id": matches[0]["zone_id"] if status == "ASSIGNED" else None,
                        "match_count": len(matches), "matches": matches,
                        "provenance": [p.model_dump() for p in entity.provenance]})
    counts = Counter(r["status"] for r in results)
    return {"module": "GeoAssign", "results": results,
            "summary": {"records": len(results), "zones": len(ordered),
                        "status_counts": {s: counts.get(s, 0) for s in ASSIGN_STATUSES}},
            "status_precedence": STATUS_PRECEDENCE, "uncertainty_note": ASSIGN_UNCERTAINTY_NOTE,
            "crs": WGS84}
