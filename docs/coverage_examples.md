# Coverage examples: EV charging stations and public toilets

`geopilot.coverage_examples.run_coverage_example(key, ...)` runs one of two built-in examples. Both
examples go through `geopilot.decision.plan_coverage` with the same method (`weighted_coverage`) and the
same algorithm. The code has no facility-specific branch.

```python
from geopilot.coverage_examples import load_coverage_inputs, run_coverage_example

inputs = load_coverage_inputs()              # bundled files; the OSM file is checked against its SHA-256
ev = run_coverage_example("ev_charging", inputs=inputs)
wc = run_coverage_example("public_toilets", inputs=inputs, service_radius_m=500, include_unknown_access=False)
```

| | `ev_charging` | `public_toilets` |
|---|---|---|
| Facilities | 63 OSM `charging_station`, all used as tagged | 100 OSM `toilets`; 28 eligible |
| Eligibility | none | `EligibilityPolicy("access", {yes, permissive}, {customers, permit, private, no})` on `raw_tags`; unknown excluded unless opted in (warning) |
| Selected sites (defaults) | C074, C051, C090 | C074, C051, C090 |
| Demand weight covered (existing + new = final) | 14350 + 7460 = 21810 of 38160 | 6010 + 7180 = 13190 of 38160 |

## Shared inputs and defaults

- **Study area:** the bundled Southwark boundary.
- **Demand:** the bundled synthetic points. The `population` field is used as synthetic demand units.
- **Context:** `residential`, `commercial` and `transit` points are mapped as context with weight 0.
- **Defaults:** target 3, grid 500 m, radius 500 m, minimum distance to existing facilities 250 m,
  minimum distance between new sites 600 m.

Every parameter can be overridden with a keyword argument, for example `method=` (only
`weighted_coverage` is accepted).

## Output

The result is the `plan_coverage` output plus two additions:

- `example`: the title, notes, source digests and `demand_units`
- `eligibility`: the policy, counts, warnings, and a per-record audit with source ID, category, raw
  `access` value, decision, reason and coordinates

`tabular_views("coverage", result)` gives the CSV tables: selected sites, candidates, input audit and
facility eligibility.

## Eligibility filter

`geopilot.decision.EligibilityPolicy` and `apply_eligibility` are generic. They filter any entities by one
attribute value and audit every decision. They are a step before `plan_coverage`, not part of the coverage
algorithm.

## Caveats

- **Existing facilities outside the study area** are kept and can cover demand inside it.
- **Coordinates:** 18 OSM ways use the centre of their bounding box as the coordinate.
- **Tags:** tags are as mapped by contributors. Availability, opening hours, fees, indoor location and
  wheelchair access are not verified.
- **Licence:** the OSM data is a separate ODbL 1.0 database (© OpenStreetMap contributors); see
  `DATA_SOURCES.md` and `examples/data/coverage/README.md`.
