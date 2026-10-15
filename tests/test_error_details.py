"""The UI must expose all rejected boundary reasons, including those beyond the summary limit."""
import json

from test_workspace_app import app, tag


def test_every_boundary_rejection_is_available_in_error_details():
    data = json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"region_code": f"bad-{i}"},
         "geometry": {"type": "Point", "coordinates": [0, 0]}}
        for i in range(12)
    ]}).encode()
    at = app()
    at.file_uploader(key="cw_zones_file").set_value(("invalid_regions.geojson", data, "application/geo+json")).run()
    at.selectbox(key=f"cw_zmap_id_{tag(data)}").set_value("region_code")
    at.text_input(key="cw_zones_source").set_value("invalid_boundaries")
    at.run()
    at.checkbox(key="cw_zones_crs_ok_EPSG:4326").check().run()
    assert not at.exception and any("INVALID_ZONES" in e.value for e in at.error)
    details = json.loads(at.json[0].value)
    assert len(details["problems"]) == 12
    assert all(any(f"bad-{i}" in reason for reason in details["problems"]) for i in range(12))
