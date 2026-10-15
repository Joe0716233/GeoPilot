# Zone assignment without claims

`geopilot.verify.assign_zones(entities, zones, config=None)` reports which supplied zones
contain each point. It needs no claimed zone and is separate from claim verification:
`verify_batch` and `verify_entity` are unchanged, and a record without a claim is still
`UNRESOLVED` / `NO_CLAIM` there.

```python
from geopilot.data import load_entities, load_zones
from geopilot.verify import AssignConfig, assign_zones

zones = load_zones("wards.geojson", source="wards", columns={"zone_id": "ward_code", "name": "ward_name"},
                   input_crs="EPSG:27700")
points = load_entities("assets.csv", source="upload", input_crs="EPSG:27700",
                       columns={"source_id": "asset_ref", "lon": "easting", "lat": "northing"})
result = assign_zones(points, zones, AssignConfig(original_x_field="easting", original_y_field="northing",
                                                  original_crs="EPSG:27700"))
```

## Output

The function returns one result per input record, in input order. Each result contains:

- `record_id`, `source`, `source_id`, `name` and `category`
- `lon` and `lat` in EPSG:4326
- `original_coordinates`: the raw uploaded values, when `AssignConfig` names the property fields
- `status`, `reason_code` and `assigned_zone_id`
- `matches`: every covering zone, as `zone_id`, `zone_name` and `relation` (`INSIDE` or `BOUNDARY`), sorted by `zone_id`
- `provenance`

The return value also includes `summary.status_counts`, `status_precedence` and
`uncertainty_note`.

## Statuses

Statuses follow this precedence; the first rule that applies wins:

| Status | Meaning |
|---|---|
| `NO_COORDINATE` | The record has no lon/lat. It is kept in the results, with no matches. |
| `OUTSIDE_ALL_ZONES` | No supplied zone covers the point. |
| `MULTIPLE_MATCHES` | Two or more zones cover the point (overlapping zones, a shared boundary, or inside one zone and on the edge of another). |
| `ON_BOUNDARY` | Exactly one zone covers the point, and the point is on that zone's boundary (outer edge, hole edge or island edge). |
| `ASSIGNED` | Exactly one zone covers the point, in its interior. |

`MULTIPLE_MATCHES` outranks `ON_BOUNDARY`. A point on a boundary shared by two zones is
therefore `MULTIPLE_MATCHES`, with both matches marked `BOUNDARY`. Only `ASSIGNED` sets
`assigned_zone_id`; GeoPilot never picks the first match on your behalf. Zones can be
Polygons or MultiPolygons, with holes and islands.

## Uncertainty

Relations come from exact Shapely `covers` and `touches` predicates on the supplied WGS 84
coordinates and geometry, with no tolerance. Coordinate error and boundary-version differences
are neither measured nor modelled. `ASSIGNED` does not guarantee that the real-world location is
in the zone. To convert other CRSs, use the loaders' `input_crs` argument.

## Errors and bounds

Refusals raise `AssignInputError`, a `ValueError` with `code`, `detail` and `context`
attributes:

| Code | Meaning |
|---|---|
| `INVALID_ZONES` | No zones were supplied, an item isn't a validated `Zone`, or zone IDs are duplicated. An empty zone list is refused rather than labelling every point `OUTSIDE_ALL_ZONES`. |
| `INVALID_ENTITY` | The input isn't a list or tuple, an item isn't an `Entity`, or entity keys are duplicated. |
| `INVALID_ASSIGN_CONFIG` | A configuration value is invalid. |
| `RESOURCE_LIMIT_EXCEEDED` | A work bound was exceeded (see below). |

Malformed GeoJSON never becomes a `Zone`: `load_zones` and `Zone` reject it with an error that
lists the bad rows.

`AssignConfig` caps the following quantities, and each cap is checked before any
point-in-polygon test:

- records
- zones
- located-record × zone pairs
- vertices per zone
- total zone vertices

Each cap can be lowered but not raised above its default.
