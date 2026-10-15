"""WP3: the custom-data workspace in the Streamlit app, driven through AppTest.

Uploads use AppTest's real ``file_uploader`` support (``set_value`` registers the bytes with
Streamlit's uploaded-file manager); nothing is mocked and no local file is read by the app.
"""

import hashlib
import json
from pathlib import Path

import pytest

from geopilot.decision import DecisionInputError

ROOT = Path(__file__).resolve().parents[1]
POINTS = (b"\xef\xbb\xbfref,east,north,label\nA,-0.095,51.505,inside west\nB,-0.1,51.51,west edge\n"
          b"C,-0.085,51.51,overlap\nD,,,no coordinate\nE,abc,51.5,bad x\n")
ZONES = json.dumps({"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"region_code": "west", "region_label": "West region"},
     "geometry": {"type": "Polygon", "coordinates": [[[-0.1, 51.5], [-0.08, 51.5], [-0.08, 51.52], [-0.1, 51.52],
                                                      [-0.1, 51.5]]]}},
    {"type": "Feature", "properties": {"region_code": "east", "region_label": "East region"},
     "geometry": {"type": "MultiPolygon", "coordinates": [
         [[[-0.09, 51.5], [-0.07, 51.5], [-0.07, 51.52], [-0.09, 51.52], [-0.09, 51.5]],
          [[-0.075, 51.505], [-0.072, 51.505], [-0.072, 51.508], [-0.075, 51.508], [-0.075, 51.505]]],
         [[[-0.06, 51.5], [-0.05, 51.5], [-0.05, 51.51], [-0.06, 51.51], [-0.06, 51.5]]]]}}]}).encode("utf-8")


def tag(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:8]


def app(lang="en"):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=120).run()
    at.selectbox(key="lang").set_value(lang).run()
    at.radio(key="workspace").set_value("custom").run()
    assert not at.exception
    return at


def load_points(at, data=POINTS, x="east", y="north"):
    at.file_uploader(key="cw_points_file").set_value(("points.csv", data, "text/csv")).run()
    at.selectbox(key=f"cw_pmap_x_{tag(data)}").set_value(x)
    at.selectbox(key=f"cw_pmap_y_{tag(data)}").set_value(y)
    at.selectbox(key=f"cw_pmap_id_{tag(data)}").set_value("ref")
    at.text_input(key="cw_points_source").set_value("survey_2026")
    at.run()
    at.checkbox(key="cw_points_crs_ok_EPSG:4326").check().run()


def load_zones(at, data=ZONES):
    at.file_uploader(key="cw_zones_file").set_value(("regions.geojson", data, "application/geo+json")).run()
    at.selectbox(key=f"cw_zmap_id_{tag(data)}").set_value("region_code")
    at.selectbox(key=f"cw_zmap_name_{tag(data)}").set_value("region_label")
    at.text_input(key="cw_zones_source").set_value("regions_2026")
    at.run()
    at.checkbox(key="cw_zones_crs_ok_EPSG:4326").check().run()


def run_workflow(at):
    load_points(at)
    load_zones(at)
    at.button(key="cw_run").click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]


def metrics(at):
    return {m.label: m.value for m in at.metric}


def test_london_stays_default_and_navigation_round_trips():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=120).run()
    assert at.radio(key="workspace").value == "london" and metrics(at)["总覆盖"] == "44.7 %"
    at.radio(key="workspace").set_value("custom").run()
    assert not at.exception and at.title[0].value == "GeoPilot · 自定义数据工作区"
    assert "总覆盖" not in metrics(at) and not at.get("plotly_chart")
    at.radio(key="workspace").set_value("london").run()
    assert not at.exception and metrics(at)["总覆盖"] == "44.7 %"


def test_custom_workflow_end_to_end():
    at = app()
    run_workflow(at)
    m = metrics(at)
    assert (m["Original"], m["Loaded"], m["Rejected"], m["Missing coordinate"], m["Participating"]) == (
        "5", "4", "1", "1", "3")
    assert (m["ASSIGNED"], m["ON_BOUNDARY"], m["MULTIPLE_MATCHES"], m["NO_COORDINATE"], m["REJECTED"]) == (
        "1", "1", "1", "1", "1")
    assert m["OUTSIDE_ALL_ZONES"] == "0" and m["Zones"] == "2"
    assert any("rejected" in w.value for w in at.warning)
    assert len(at.get("plotly_chart")) == 1
    assert [d.label for d in at.download_button] == ["Download results CSV", "Download run metadata JSON"]
    results = next(d.value for d in at.dataframe if "zone_names" in d.value.columns)
    assert list(results["status"]) == ["ASSIGNED", "ON_BOUNDARY", "MULTIPLE_MATCHES", "NO_COORDINATE", "REJECTED"]
    assert results.loc[results["source_id"] == "C", "zone_names"].item() == "East region;West region"
    result = at.session_state["cw_result"]
    assert "points.csv" not in result["json"] and hashlib.sha256(POINTS).hexdigest() in result["json"]
    assert "REJECTED,NON_NUMERIC_COORDINATE" in result["csv"]


def test_chinese_workspace_labels():
    at = app("zh")
    run_workflow(at)
    m = metrics(at)
    assert m["已拒绝 REJECTED"] == "1" and m["位于边界 ON_BOUNDARY"] == "1" and m["参与分配"] == "3"
    assert [d.label for d in at.download_button] == ["下载结果 CSV", "下载运行元数据 JSON"]


@pytest.mark.parametrize("data, code", [(b"a,b,a\n1,2,3\n", "INVALID_FILE"), (b"\xff\xfe\x00", "INVALID_FILE"),
                                        (b"ref,east,north\n", "INVALID_FILE")])
def test_upload_errors_are_shown_and_page_stays_usable(data, code):
    at = app()
    at.file_uploader(key="cw_points_file").set_value(("bad.csv", data, "text/csv")).run()
    assert not at.exception and any(code in e.value for e in at.error)
    assert at.button(key="cw_run").disabled
    at.radio(key="workspace").set_value("london").run()
    assert not at.exception and metrics(at)["Total covered"] == "44.7 %"


def test_mapping_crs_and_layer_errors_are_reported():
    at = app()
    load_points(at, x="east", y="east")
    assert any("INVALID_MAPPING" in e.value and "two different columns" in e.value for e in at.error)
    at.text_input(key="cw_points_crs").set_value("GCJ-02").run()
    assert any("UNSUPPORTED_CRS" in e.value for e in at.error)
    bad_zones = json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"region_code": "P"}, "geometry": {"type": "Point", "coordinates": [0, 0]}}]
    }).encode()
    at.file_uploader(key="cw_zones_file").set_value(("z.geojson", bad_zones, "application/geo+json")).run()
    at.selectbox(key=f"cw_zmap_id_{tag(bad_zones)}").set_value("region_code")
    at.text_input(key="cw_zones_source").set_value("z")
    at.run()
    at.checkbox(key="cw_zones_crs_ok_EPSG:4326").check().run()
    assert not at.exception and any("INVALID_ZONES" in e.value for e in at.error)


def test_non_standard_json_upload_is_an_error_not_a_crash():
    bad = (b'{"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"ref": "a"}, '
           b'"geometry": {"type": "Point", "coordinates": [NaN, 0]}}]}')
    at = app()
    at.file_uploader(key="cw_points_file").set_value(("points.geojson", bad, "application/geo+json")).run()
    assert not at.exception and any("INVALID_FILE" in e.value and "NaN" in e.value for e in at.error)
    assert at.button(key="cw_run").disabled


def test_uploaders_carry_the_size_cap():
    from geopilot.workspace import MAX_UPLOAD_MB
    at = app()
    assert [u.proto.max_upload_size_mb for u in at.file_uploader] == [MAX_UPLOAD_MB, MAX_UPLOAD_MB]


@pytest.mark.parametrize("change", ["unconfirm_crs", "invalid_mapping", "clear_file"])
def test_failed_validation_also_discards_the_result(change):
    at = app()
    run_workflow(at)
    if change == "unconfirm_crs":
        at.checkbox(key="cw_zones_crs_ok_EPSG:4326").uncheck().run()
    elif change == "invalid_mapping":
        at.selectbox(key=f"cw_pmap_y_{tag(POINTS)}").set_value("east").run()
        assert any("INVALID_MAPPING" in e.value for e in at.error)
    else:
        at.file_uploader(key="cw_points_file").clear().run()
    assert not at.exception
    assert "cw_result" not in at.session_state and not at.download_button and not at.get("plotly_chart")
    assert at.button(key="cw_run").disabled


@pytest.mark.parametrize("layer", ["points", "zones"])
def test_replacement_upload_needs_a_fresh_crs_confirmation(layer):
    at = app()
    run_workflow(at)
    key, prefix = ("cw_points_file", "cw_points") if layer == "points" else ("cw_zones_file", "cw_zones")
    original, mime = (POINTS, "text/csv") if layer == "points" else (ZONES, "application/geo+json")
    name = "points.csv" if layer == "points" else "regions.geojson"
    replacement = original + (b"F,-0.095,51.506,new row\n" if layer == "points" else b"\n")
    assert at.checkbox(key=f"{prefix}_crs_ok_EPSG:4326").value
    at.file_uploader(key=key).set_value((name, replacement, mime)).run()           # different bytes
    assert not at.exception
    assert at.checkbox(key=f"{prefix}_crs_ok_EPSG:4326").value is False
    assert "cw_result" not in at.session_state and not at.download_button
    at.checkbox(key=f"{prefix}_crs_ok_EPSG:4326").check().run()
    renamed = ("renamed.csv" if layer == "points" else "renamed.geojson", replacement, mime)
    at.file_uploader(key=key).set_value(renamed).run()                              # same bytes, new name
    assert at.checkbox(key=f"{prefix}_crs_ok_EPSG:4326").value is True
    other = "cw_zones" if layer == "points" else "cw_points"
    assert at.checkbox(key=f"{other}_crs_ok_EPSG:4326").value is True               # the other layer is untouched


def test_crs_must_be_confirmed_again_after_it_changes():
    at = app()
    run_workflow(at)
    at.text_input(key="cw_points_crs").set_value("EPSG:4258").run()
    assert not at.checkbox(key="cw_points_crs_ok_EPSG:4258").value
    assert "cw_result" not in at.session_state and not at.download_button
    assert any("discarded" in w.value for w in at.warning)


@pytest.mark.parametrize("change", ["mapping", "bytes", "label", "boundary_mapping"])
def test_changing_inputs_invalidates_the_result_and_downloads(change):
    at = app()
    run_workflow(at)
    assert at.download_button
    if change == "mapping":
        at.selectbox(key=f"cw_pmap_name_{tag(POINTS)}").set_value("label").run()
    elif change == "bytes":
        changed = POINTS + b"F,-0.095,51.506,new row\n"
        at.file_uploader(key="cw_points_file").set_value(("points.csv", changed, "text/csv")).run()
    elif change == "label":
        at.text_input(key="cw_points_source").set_value("survey_2027").run()
    else:
        at.selectbox(key=f"cw_zmap_name_{tag(ZONES)}").set_value(None).run()
    assert not at.exception
    assert "cw_result" not in at.session_state and not at.download_button and not at.get("plotly_chart")
    assert any("discarded" in w.value for w in at.warning)
    if change == "bytes":                           # new bytes need their own mapping before a rerun
        load_points(at, data=changed)
    at.button(key="cw_run").click().run()          # running again gives a fresh, current result
    assert at.download_button and "cw_result" in at.session_state


def test_sessions_are_isolated():
    first, second = app(), app()
    run_workflow(first)
    second.run()
    assert "cw_result" not in second.session_state and not second.download_button
    assert second.file_uploader(key="cw_points_file").value is None
    first.run()
    assert "cw_result" in first.session_state and len(first.download_button) == 2


def test_london_widget_extremes_fit_the_app_limits():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=120).run()
    for key, value in (("target", 15), ("step", 100.0), ("service", 3000.0), ("min_exist", 0.0), ("min_new", 0.0)):
        at.number_input(key=key).set_value(value)
    at.run()
    assert not at.exception and not at.error and metrics(at)["总覆盖"].endswith("%")


def test_london_decision_refusal_is_contained(monkeypatch):
    import geopilot.scenario

    def refuse(*_args, **_kwargs):
        raise DecisionInputError("RESOURCE_LIMIT_EXCEEDED", "candidate grid cells = 99999 exceeds the limit 20000")
    monkeypatch.setattr(geopilot.scenario, "run_decision", refuse)
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=120).run()
    assert not at.exception
    assert any("RESOURCE_LIMIT_EXCEEDED" in e.value for e in at.error)
    assert metrics(at)["通过 PASS"] == "59"                          # other London tabs still render
    assert any("数据来源" in m.value for m in at.markdown)            # including the data & limits tab
    at.radio(key="workspace").set_value("custom").run()
    assert not at.exception and at.title[0].value == "GeoPilot · 自定义数据工作区"
