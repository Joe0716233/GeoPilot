"""WP4: coverage examples page and coverage on uploaded data, driven through AppTest (real uploads)."""

import pytest

from geopilot.coverage_examples import run_coverage_example
from test_workspace_app import ROOT, app, load_zones, metrics, tag

COVERAGE_CSV = (b"ref,east,north,kind,need\nT1,-0.095,51.505,toilet,\nH1,-0.094,51.506,household,120\n"
                b"H2,-0.085,51.51,household,80\nH3,-0.075,51.515,household,50\nB1,-0.096,51.507,bus stop,\n"
                b"X1,abc,51.5,household,5\nN1,,,household,7\n")


def by_label(widgets, label):
    return next(w for w in widgets if w.label == label)


def examples(lang="en"):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=120).run()
    at.selectbox(key="lang").set_value(lang).run()
    at.radio(key="workspace").set_value("examples").run()
    assert not at.exception and not at.error, [e.value for e in at.error]
    return at


def pct(value):
    return f"{value:.1f} %"


def test_examples_switch_between_ev_charging_and_toilets():
    at = examples()
    m = metrics(at)
    assert (m["Participating facilities"], m["Demand points in study area"], m["Grid candidates"],
            m["Feasible candidates"], m["Covered by existing"], m["Total covered"]) == (
        "63", "140", "119", "105", "37.6 %", "57.2 %")
    assert any("synthetic demand units" in w.value for w in at.warning)
    at.radio(key="ce_example").set_value("public_toilets").run()
    m = metrics(at)
    assert not at.exception
    assert (m["Facility records"], m["Eligible (used)"], m["Restricted (excluded)"], m["Access unknown"]) == (
        "100", "28", "18", "54")
    assert (m["Participating facilities"], m["Feasible candidates"], m["Covered by existing"], m["Total covered"]) == (
        "28", "110", "15.7 %", "34.6 %")
    assert any("not validated" in w.value for w in at.warning)
    assert len(at.get("plotly_chart")) == 1
    assert [d.label for d in at.download_button] == [
        "Download JSON (with run config)", "Download coverage_selected.csv", "Download coverage_candidates.csv",
        "Download coverage_inputs.csv", "Download coverage_facility_eligibility.csv"]
    assert at.selectbox(key="ce_method").options == ["weighted_coverage"]


def test_unknown_access_opt_in_warns_and_changes_the_facility_set():
    at = examples()
    at.radio(key="ce_example").set_value("public_toilets").run()
    at.checkbox(key="ce_include_unknown").check().run()
    m = metrics(at)
    assert not at.exception and m["Participating facilities"] == "82" and m["Eligible (used)"] == "82"
    assert any("unknown access" in w.value for w in at.warning)
    assert m["Excluded or ignored records (all stages)"] == "18"
    assert any(e.label == "Facility eligibility audit (18 excluded; 54 with unknown access included by opt-in)"
               for e in at.expander)


def test_excluded_count_includes_eligibility_exclusions():
    at = examples()
    assert metrics(at)["Excluded or ignored records (all stages)"] == "0"
    at.radio(key="ce_example").set_value("public_toilets").run()
    assert metrics(at)["Excluded or ignored records (all stages)"] == "72"                   # 18 restricted + 54 unknown
    assert any(e.label == "Facility eligibility audit (72 excluded; 0 with unknown access included by opt-in)"
               for e in at.expander)


def test_high_cost_example_parameters_are_refused_by_app_limits():
    at = examples()
    for key, value in (("ce_step", 100.0), ("ce_radius", 5000.0), ("ce_target", 50)):
        at.number_input(key=key).set_value(value)
    at.run()
    assert not at.exception
    assert any("RESOURCE_LIMIT_EXCEEDED" in e.value and "greedy evaluations" in e.value for e in at.error)
    at.radio(key="workspace").set_value("london").run()
    assert not at.exception and metrics(at)["Total covered"] == "44.7 %"


def test_example_parameters_are_applied_through_the_shared_function():
    at = examples()
    for key, value in (("ce_radius", 900.0), ("ce_min_exist", 700.0), ("ce_target", 5)):
        at.number_input(key=key).set_value(value)
    at.run()
    expected = run_coverage_example("ev_charging", service_radius_m=900, min_existing_distance_m=700, target_count=5)
    m = metrics(at)
    assert m["Total covered"] == pct(expected["final_total_coverage_pct"])
    assert m["Feasible candidates"] == str(expected["feasible_candidate_count"])


def test_examples_page_in_chinese_and_navigation_back_to_london():
    at = examples("zh")
    m = metrics(at)
    assert m["参与的现有设施"] == "63" and m["总覆盖"] == "57.2 %"
    assert any("合成需求单位" in w.value for w in at.warning)
    at.radio(key="workspace").set_value("london").run()
    assert not at.exception and metrics(at)["总覆盖"] == "44.7 %"


# --- coverage on uploaded data ---

def prepare_upload(at, data=COVERAGE_CSV):
    at.file_uploader(key="cw_points_file").set_value(("points.csv", data, "text/csv")).run()
    for field, column in (("x", "east"), ("y", "north"), ("id", "ref"), ("category", "kind"),
                          ("demand_weight", "need")):
        at.selectbox(key=f"cw_pmap_{field}_{tag(data)}").set_value(column)
    at.text_input(key="cw_points_source").set_value("survey_2026")
    at.run()
    at.checkbox(key="cw_points_crs_ok_EPSG:4326").check().run()
    load_zones(at)
    by_label(at.selectbox, "Study zone").set_value("west")
    for category, role in (("toilet", "existing_facility"), ("household", "weighted_demand"), ("bus stop", "context")):
        by_label(at.selectbox, f"Role of category '{category}'").set_value(role)
    at.number_input(key="cv_target").set_value(1)
    at.run()
    assert not at.exception


def test_uploaded_coverage_needs_acknowledgement_then_runs():
    at = app()
    prepare_upload(at)
    assert by_label(at.selectbox, "Demand weight field").value == "need"        # from the point mapping
    assert at.button(key="cv_run").disabled                                       # 2 rows not yet acknowledged
    assert any("cannot take part in coverage" in w.value for w in at.warning)
    by_label(at.checkbox, "I acknowledge these rows are excluded from coverage").check().run()
    at.button(key="cv_run").click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]
    m = metrics(at)
    assert (m["Participating facilities"], m["Demand points in study area"], m["Total demand weight"],
            m["Demand outside study area"], m["Context points"]) == ("1", "2", "200", "1", "1")
    assert m["Covered by existing"] == "60.0 %"
    assert m["Excluded or ignored records (all stages)"] == "3"     # X1, N1 (unusable rows) + H3 outside west
    limits = at.session_state["cv_result"]["result"]["config"]["limits"]
    assert (limits["max_grid_cells"], limits["max_pair_evaluations"], limits["max_target_count"]) == (
        20_000, 2_000_000, 50)                                        # the app's caps, not the library defaults
    labels = [d.label for d in at.download_button]
    assert "Download JSON (with run config)" in labels and "Download coverage_inputs.csv" in labels
    assert len(at.get("plotly_chart")) == 1
    result = at.session_state["cv_result"]["result"]
    assert result["config"]["service_radius_m"] == 500 and result["roles"]["toilet"] == "existing_facility"
    assert [r["source_id"] for r in result["upload"]["excluded_rows"]] == ["X1", "N1"]


@pytest.mark.parametrize("change", ["role", "weight", "radius", "zone", "ack", "zero_existing"])
def test_any_coverage_change_discards_the_old_result(change):
    at = app()
    prepare_upload(at)
    by_label(at.checkbox, "I acknowledge these rows are excluded from coverage").check().run()
    at.button(key="cv_run").click().run()
    assert "cv_result" in at.session_state
    if change == "role":
        by_label(at.selectbox, "Role of category 'bus stop'").set_value("ignore").run()
    elif change == "weight":
        by_label(at.number_input, "Weight: demand").set_value(0.5).run()
    elif change == "radius":
        at.number_input(key="cv_radius").set_value(800.0).run()
    elif change == "zone":
        by_label(at.selectbox, "Study zone").set_value("east").run()
    elif change == "ack":
        by_label(at.checkbox, "I acknowledge these rows are excluded from coverage").uncheck().run()
    else:
        at.checkbox(key="cv_zero").check().run()
    assert not at.exception
    assert "cv_result" not in at.session_state
    assert not [d for d in at.download_button if d.key and d.key.startswith("cv_dl")]
    assert any("previous coverage result" in w.value for w in at.warning)


def test_unmapped_category_is_refused_on_run():
    at = app()
    prepare_upload(at)
    by_label(at.selectbox, "Role of category 'bus stop'").set_value(None).run()
    assert any("still unmapped: bus stop" in i.value for i in at.info)
    by_label(at.checkbox, "I acknowledge these rows are excluded from coverage").check().run()
    at.button(key="cv_run").click().run()
    assert not at.exception and any("UNMAPPED_ROLE" in e.value and "bus stop" in e.value for e in at.error)
    assert "cv_result" not in at.session_state


def test_zero_existing_declaration_contradiction_is_reported():
    at = app()
    prepare_upload(at)
    by_label(at.checkbox, "I acknowledge these rows are excluded from coverage").check()
    at.checkbox(key="cv_zero").check().run()
    at.button(key="cv_run").click().run()
    assert not at.exception
    assert any("INVALID_SCENARIO_CONFIG" in e.value and "allow_zero_existing" in e.value for e in at.error)
