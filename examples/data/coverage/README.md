# Coverage example data (OpenStreetMap, ODbL)

`osm_facilities_southwark.json` holds 163 OpenStreetMap records: 63 `amenity=charging_station` and 100
`amenity=toilets`. They were retrieved on 2026-10-14 through the Overpass API for the bounding box of the
bundled Southwark boundary.

**This file is a separate database under the Open Database License (ODbL) 1.0. It is not covered by
GeoPilot's MIT licence.**

© OpenStreetMap contributors. Data available under the Open Database License:
<https://opendatacommons.org/licenses/odbl/1-0/>. See also <https://www.openstreetmap.org/copyright>.

If you redistribute this file, or a database derived from it, you must keep it under ODbL with this
attribution, and you must make any derivative database available under the same licence.

## Format and caveats

- **Format:** each record is a GeoPilot `Entity`. The OSM tags are unchanged in `properties.raw_tags`.
  `properties` also records the OSM type and ID and `coordinate_kind`, and each record carries a
  provenance entry (`source: osm`, `source_id: node/<id>` or `way/<id>`).
- **Coordinates:** 18 records are OSM ways whose coordinate is the centre of their bounding box, not an
  entrance (`OSM_BOUNDING_BOX_CENTER_PROXY`).
- **Location:** 84 of the 163 points lie inside the Southwark polygon. The others lie inside its bounding box,
  and the examples keep them as existing facilities.
- **Accuracy:** tags are as mapped by contributors. Current existence, opening hours, fees, operator, power,
  indoor location and wheelchair access are not verified.
- **Integrity:** `manifest.json` records the SHA-256 checksum, and the loader refuses a modified file.

The examples also reuse two files from `../london/`:

- the Southwark boundary from `boroughs_subset.geojson` (OGL v3.0)
- `synthetic_demand.csv`, whose `population` field is used as synthetic demand units, not observed demand
