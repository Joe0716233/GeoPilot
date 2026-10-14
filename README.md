# GeoPilot

GeoPilot v1.0 is a small, offline-capable Python toolkit that answers three practical questions about
geospatial records:

1. **GeoVerify** – does a point really sit inside the zone its record claims?
2. **GeoResolve** – do records from two sources describe the same place?
3. **GeoDecision** – given demand points and existing sites, which grid locations are the best screening
   candidates for new sites under explicit distance constraints?

It exists because location data from different systems is routinely inconsistent: wrong region labels,
duplicated sites, ad-hoc coordinate systems and opaque "best site" scores. GeoPilot keeps every step explicit:
projected metric distances, named reason codes, conservative statuses, and limitation statements next to
every result.

## Live Demo

**<https://geopilot-gis.streamlit.app/>** – the integrated app hosted on Streamlit Community Cloud, running on
the bundled sample data. The page opens in Simplified Chinese; use the sidebar language selector
(语言 / Language) to switch to English. It is a demonstration on historic open data and synthetic inputs, not a
statement about current London charging provision. The app may take a moment to wake up if it has been idle.

> **Scope.** v1.0 is a deterministic, rule-based library with a demo app. It contains **no LLM or autonomous
> agent**, performs **no live data access**, and makes **no claim of accuracy, current charger availability or
> real-site feasibility**. The bundled London data is partly historic open data and partly synthetic; see
> [Limitations](#london-sample-data-and-limitations).

## Capabilities

| Module | Input | Output |
| --- | --- | --- |
| `geopilot.verify` | Points, claimed zone ids, zone polygons | `PASS` / `REVIEW` / `FAIL` / `UNRESOLVED` per record, signed metric distance to the claimed boundary, reason code |
| `geopilot.resolve` | Points/records from several sources | Scored candidate pairs (`MATCH` / `REVIEW` / `NO_MATCH`), feature breakdown, conservative clusters with provenance |
| `geopilot.decision` | Zone polygon, existing stations, demand/context points | Feasible grid candidates with component scores, greedily selected sites, existing vs incremental coverage |
| `geopilot.tools` | lon/lat, polygons | Geodesic distance, signed point-in-polygon, metric buffer, nearest point, CRS transform |
| `geopilot.data` | CSV / GeoJSON (optionally in another CRS) | Validated `Entity` / `Zone` records with all failures reported together |

Results can be exported as JSON and CSV from the CLI demo and the app.

## Technology stack

Python ≥ 3.12 · shapely 2 · pyproj · pydantic 2 · RapidFuzz · Streamlit + Plotly (demo app) · pytest.

## Repository structure

```
src/geopilot/
  data/       schema, CSV/GeoJSON loaders, CRS conversion, quality checks
  verify/     GeoVerify
  resolve/    GeoResolve
  decision/   GeoDecision
  tools/      reusable GIS operations
  scenario.py bundled London sample loader and one-call runners
  reporting.py JSON/CSV export
app/streamlit_app.py      integrated offline demo
examples/run_demo.py      standalone CLI demo (--module verify|resolve|decision|all)
examples/data/london/     bundled sample data + manifest.json
scripts/prepare_data.py   deterministic data preparation
tests/                    pytest suite
docs/screenshots/         application screenshots
DATA_SOURCES.md           sources, licences, attribution
```

## Installation

Linux / macOS (POSIX shell):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt      # pinned versions used for development and CI
pip install -e . --no-deps
```

Windows PowerShell (calls the virtual environment's Python explicitly, so no activation script or
execution-policy change is needed):

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install -e . --no-deps
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe examples/run_demo.py --module all
.venv/Scripts/python.exe -m streamlit run app/streamlit_app.py
```

`requirements.txt` ends its header with an editable `-e .` entry, so installing it from the repository root
already installs the local `geopilot` package (this is what hosted deployments that read only that file
rely on); the separate `pip install -e . --no-deps` step above is therefore harmless and optional.

`pip install -e ".[app,test]"` also works if you prefer unpinned compatible ranges.

## Run the sample demo (offline)

```bash
python examples/run_demo.py --module all
python examples/run_demo.py --module verify   --export-dir out --format csv
python examples/run_demo.py --module resolve  --match-score 0.85 --export-dir out
python examples/run_demo.py --module decision --target-count 5 --service-radius-m 1200 --export-dir out
```

`--format` is `json`, `csv` or `both`. Run `--help` for every parameter. Invalid parameters exit with
status 2 and a message.

Streamlit app (no network or map tiles needed):

```bash
streamlit run app/streamlit_app.py
```

The page is in Simplified Chinese by default; the sidebar language selector (语言 / Language) switches to
English without resetting parameters. Only display text is translated; exported files, field names and all
numerical results are identical in both languages.

The sidebar exposes the GeoVerify margins, the GeoResolve candidate radius and score thresholds, and the
GeoDecision parameters and score weights. The CLI flags are listed by `python examples/run_demo.py --help`
and are a different, smaller set (for example, no candidate-radius or weight flags).
The **GeoVerify** and **GeoDecision** tabs show a plain lon/lat map plus metrics and tables; the **GeoResolve**
tab shows metrics, a scored-pair table and a cluster table, with no map. Each of the three analysis tabs has
JSON and CSV download buttons. A fourth tab summarises data sources and limits. Source data is read from
`examples/data/london` of a checkout; set `GEOPILOT_DATA_DIR` to use another folder with the same files.

## Run the tests

```bash
python -m pytest -q
```

The suite covers CRS and metric-distance arithmetic against analytic values, invalid geometry/ID/parameter
handling, status thresholds, resolution conflicts and determinism, exact coverage numbers, data-manifest
hashes, regeneration of the synthetic files, CLI exit codes, exports and a headless render of the app.

## Interpreting spatial outputs

* **Coordinates** are WGS 84 lon/lat (EPSG:4326). Other CRSs are converted on load via `input_crs`.
* **Distances** are straight-line metres: geodesic for point pairs, and a local azimuthal-equidistant
  projection centred on the analysis location for boundary and coverage tests. They are not travel distance
  or time.
* **GeoVerify `signed_distance_m`**: positive inside the claimed zone, negative outside; `0` on the boundary.
  Defaults: `REVIEW` within 20 m of the boundary, `FAIL` more than 50 m outside. These are review margins
  you choose, not measured coordinate error. `PASS` means *consistent with the supplied boundary*, not *true*.
  `OUTSIDE_ALL_SUPPLIED_ZONES` means the point is in none of the zones provided, which may simply be a zone
  missing from your set.
* **GeoResolve scores** (0–1) combine name, address, distance, category, phone and URL similarity with fixed
  weights and are always within [0, 1]. `MATCH` (≥ 0.78) is a strong *candidate*; there are no labelled
  pairs, so no precision/recall is reported. A pair with neither a loosely similar name nor a shared
  phone/URL can reach `REVIEW` at most (`INSUFFICIENT_IDENTITY_EVIDENCE`), whatever the thresholds. Records
  are merged only when every cross-pair matches and no source would contribute two records.
* **Exports** are strict JSON (no `NaN`/`Infinity`); a non-finite value raises an error instead of being written.
* **GeoDecision**: candidates are grid-cell centres inside the zone. A site is feasible if it is at least
  `min_existing_distance_m` from every existing station. Coverage means demand within `service_radius_m`.
  Selection is greedy (score plus 0.35 × share of newly covered population), a screening heuristic with no
  optimality guarantee. `PARTIAL`/`FAILED` mean the constraints and heuristic found fewer sites than
  requested, not that no solution exists.

## London sample data and limitations

`examples/data/london/` contains nine central boroughs (OGL v3.0) and 42 historic rapid-charging records
from the Greater London Authority (Creative Commons Attribution, version not stated by the publisher),
retrieved 2026-09-27, plus generated test files: boundary probes (computed from the borough outlines), an
operator feed (adapted from the GLA records with deliberate offsets and spelling changes) and
demand/context points inside Southwark. The generated files remain subject to the OGL / CC BY obligations of
the data they were built from. Full provenance, modifications and attribution:
[DATA_SOURCES.md](DATA_SOURCES.md).

* The charger data is historic and says nothing about present availability.
* Probe, feed and demand results are properties of the synthetic generator, not London observations.
* On the 42 real records, borough labels agree with the bundled polygons for 40 and 2 fall within a few
  metres of a boundary and are flagged for review. This compares two open files; it is not independent
  ground truth.
* No road network, parcel, ownership, grid-capacity, planning or cost data is used; the site screening is a
  method demonstration, not a recommendation for any real location.

## Screenshots

Captured from the running Streamlit app on the bundled sample data (default parameters).

![GeoVerify: full-screen lon/lat map of the sample points coloured by status over the borough outlines](docs/screenshots/verify.jpg)

*GeoVerify – the full-screen map of the sample points coloured by verification status over the borough outlines.*

![GeoResolve: metrics and the scored candidate-pair table](docs/screenshots/resolve.jpg)

*GeoResolve – the normal page with the pair metrics and the scored candidate-pair table (this tab has no map).*

![GeoDecision: full-screen lon/lat map of Southwark with candidates, existing stations, demand and three selected sites](docs/screenshots/decision.jpg)

*GeoDecision – the full-screen map of Southwark showing feasible candidates, historic stations, synthetic
demand and the three selected screening sites.*

**Data attribution for the screenshots:** Contains public sector information licensed under the Open Government Licence v3.0. Contains data from the Greater London Authority and
Ordnance Survey Open Data ([London Boroughs](https://data.london.gov.uk/dataset/london-boroughs-e55pw),
[OGL v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)); charger records from the
Greater London Authority under a Creative Commons Attribution licence
([Electric Vehicle Charging Site](https://data.london.gov.uk/dataset/electric-vehicle-charging-site-2lzpg)).
Modifications and full notices: [DATA_SOURCES.md](DATA_SOURCES.md).

## Roadmap (planned, not implemented)

* **v2.0** – optional LLM-assisted interface: natural-language task intake and explanation on top of the
  existing deterministic tools, with every tool call validated and logged.
* **v3.0** – HTTP API and product integration: service endpoints, batch jobs and persistent result storage.

## Licence

Code: MIT (`LICENSE`). Data: not MIT. The bundled GLA/Ordnance Survey data, and the test files generated
from it, keep the publishers' OGL v3.0 and Creative Commons Attribution obligations; see `DATA_SOURCES.md`.
