# Screenshots

This folder holds screenshots of the Streamlit app (`streamlit run app/streamlit_app.py`) running on the
bundled sample data with default sidebar parameters, captured from a desktop browser. They are illustrations
of the interface, not validation evidence.

| File | Content |
| --- | --- |
| `verify.jpg` | GeoVerify: the app's full-screen map view of the borough outlines and sample points coloured by status |
| `resolve.jpg` | GeoResolve: the normal page view with the pair metrics and the scored candidate-pair table |
| `decision.jpg` | GeoDecision: the app's full-screen map view of Southwark with feasible candidates, historic stations, synthetic demand and the three selected sites |

Each image shows only part of its tab; the tabs also contain tables and JSON/CSV downloads that may not be
visible in the capture.

The two maps are plain lon/lat plots with no basemap tiles. The numbers visible in the images come from historic
open data and synthetic inputs and are not statements about current London charging provision.

## Data attribution

Contains public sector information licensed under the Open Government Licence v3.0.

Contains data from the Greater London Authority and Ordnance Survey Open Data
([London Boroughs](https://data.london.gov.uk/dataset/london-boroughs-e55pw), licensed under the
[Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)).
Charger records are from the Greater London Authority under a Creative Commons Attribution licence
([Electric Vehicle Charging Site](https://data.london.gov.uk/dataset/electric-vehicle-charging-site-2lzpg)).
Synthetic test values built from these sources do not remove the publishers' obligations. Modifications and the
complete notice are in [`../../DATA_SOURCES.md`](../../DATA_SOURCES.md).
