# Synthetic acceptance inputs

These small, invented records and simple geometries are MIT-licensed test fixtures. They contain no
personal data and make no claim about actual facilities. Coordinates are EPSG:4326 (longitude, latitude).
Use them with [the manual acceptance checklist](../../../docs/acceptance_pr1.md).

- `acceptance_points.csv`: six original IDs, custom headers, one missing coordinate.
- `acceptance_invalid.csv`: the six rows plus one nonnumeric coordinate, two duplicate IDs and one missing ID.
- `acceptance_zones.geojson`: overlapping Polygon/MultiPolygon, including a separate island.
- `acceptance_coverage.csv`: facility, weighted demand and context roles, plus two unusable rows.
