# GeoDecision on your own data: explicit roles

`plan_locations` is the London-sample entry point. It only accepts the categories
`existing_station`, `population`, `residential`, `commercial` and `transit`; any other category,
including a blank one, is refused with `UNMAPPED_ROLE`. It never treats unrecognised records as
"no existing facilities".

For any other data use `plan_coverage` with a `ScenarioConfig`. You don't need to rename your
categories: you tell GeoPilot what role each category plays.

```python
from geopilot.decision import Role, RoleMapping, ScenarioConfig, plan_coverage

roles = RoleMapping(
    {"toilet": Role.EXISTING_FACILITY, "household": Role.WEIGHTED_DEMAND,
     "bus stop": Role.CONTEXT, "note": Role.IGNORE},
    context_metrics={"bus stop": "bus_stops"})
config = ScenarioConfig(roles=roles, demand_weight_field="residents",
                        weights={"demand_weight": 0.8, "bus_stops": 0.2}, target_count=3)
result = plan_coverage(zone, entities, config)
```

| Role | Used as |
|---|---|
| `existing_facility` | Baseline coverage and the `min_existing_distance_m` exclusion. Facilities outside the study area still count. |
| `weighted_demand` | Demand points. The weight comes from `properties[demand_weight_field]`. Only points inside or on the study area are covered. |
| `context` | Counted within the service radius under a metric name (`context_metrics`, which defaults to the category). |
| `ignore` | Excluded. Coordinates are optional. The record is still listed in `exclusions` and `inputs`. |

Rules:

- **Weights:** `weights` keys are `demand_weight` and the context metric names. Every context
  metric needs an explicit weight (0 is allowed).
- **Demand weights:** each one must be a finite, non-negative number. Numeric strings such as
  CSV values are accepted. Missing, boolean, NaN, infinite and negative values are refused. This
  applies to every `weighted_demand` record, including those outside the study area. Records
  outside the study area are reported in `exclusions`, `demand_points_outside_study_area` and
  `demand_weight_outside_study_area`.
- **No existing facilities:** `allow_zero_existing=True` declares that the input has no
  existing-facility records. It is required when there are none and refused when there are
  some. The mapping can still name an `existing_facility` category, so the same mapping works
  for inputs with and without facilities.
- **Coordinates and keys:** duplicate entity keys are refused, including on ignored records.
  Records with any role other than `ignore` must have coordinates.
- **Distance from the study area:** existing facilities, context points and demand points
  inside the study area must lie within 100 km of the study-area centre (the trusted local
  metric projection). Points further away are refused with `INVALID_ENTITY`, so pre-filter the
  input to the study region. Demand points outside the study area are excluded without being
  projected, but their weights are still validated.
- **Distances:** `grid_step_m`, `service_radius_m`, `min_existing_distance_m` and
  `min_new_site_distance_m` must be finite and at most 500 km (`MAX_DISTANCE_M`).
  Percentages stay finite even for very large finite demand weights.

## Method boundary

`ScenarioConfig.method` must be `"weighted_coverage"`, the only method in `SUPPORTED_METHODS`.
This method screens candidate sites on a regular grid, greedily, for straight-line coverage of
weighted demand points within one service radius. Scores can optionally include counts of
context points. Any other method value is refused with `INVALID_SCENARIO_CONFIG`; the error
`context["supported"]` lists the supported methods. Routing, travel time, capacity, cost and
equity optimisation are not supported and are never approximated. The output repeats `method`
and `method_boundary`.

The output uses `demand_weight` naming. It includes:

- `total_demand_weight` and `*_demand_weight_covered`
- `covered_demand_ids` per candidate, and `newly_covered_demand_ids` per selected site
- `existing_facility_count` and `existing_facility_ids`
- `roles`, `context_metrics` and `role_summary` (inside, outside and no-coordinate counts per role)
- `exclusions`
- `inputs`: a per-record audit with the original category, properties and provenance, as copies

Inputs are not modified.

## Errors

Refusals raise `DecisionInputError`, a `ValueError` with stable `code`, `detail` and `context`
attributes:

| Code | Meaning |
|---|---|
| `UNMAPPED_ROLE` | An input category has no role. `context["categories"]` lists the categories. |
| `INVALID_ENTITY` | A record isn't an `Entity`, has a duplicate key, is missing coordinates, or is an active point beyond the trusted projection extent. |
| `INVALID_DEMAND_WEIGHT` | A demand weight is missing or invalid, or the weights sum to a non-finite value. `context["keys"]` lists the records. |
| `NO_DEMAND` | No demand points lie inside the study area, or their total weight is 0. |
| `INVALID_STUDY_AREA` | The study area isn't a `Zone`, is too large for one local projection (more than 100 km from the centre), or can't be projected. |
| `INVALID_SCENARIO_CONFIG` | A parameter, role mapping, weight or method is invalid, or the input's existing-facility records don't match `allow_zero_existing`. |
| `RESOURCE_LIMIT_EXCEEDED` | A work bound was exceeded (see below). |

## Resource bounds

`ResourceLimits` caps several quantities, and each cap is checked before the work it guards:

- input entities: `plan_locations` never reads more than the bound from an iterator
- study-area polygon vertices
- candidate grid cells: checked from the grid's bounding box before any cell is generated, including in `generate_candidates`
- `target_count`
- demand × facility and candidate × point pair evaluations: checked before any coverage list is built
- greedy evaluations

`ScenarioConfig.limits` and the keyword-only `plan_locations(..., limits=...)` can lower these
caps but not raise them above the defaults (`DEFAULT_LIMITS`). For example, a web app can pass
stricter caps. `DecisionConfig` checks `target_count` against the defaults.
