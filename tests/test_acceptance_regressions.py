"""Acceptance: an unmapped facility must not silently become a zero-existing baseline."""

from test_coverage_app import by_label, prepare_upload
from test_workspace_app import app


def test_unmapped_facility_is_explicitly_refused_in_uploaded_coverage():
    at = app()
    prepare_upload(at)
    by_label(at.selectbox, "Role of category 'toilet'").set_value(None).run()
    by_label(at.checkbox, "I acknowledge these rows are excluded from coverage").check().run()
    assert at.button(key="cv_run").disabled
    assert any("INVALID_SCENARIO_CONFIG" in e.value and "no category is mapped to existing_facility" in e.value
               for e in at.error)
    # Declaring zero existing facilities must not bypass the unmapped-category refusal either.
    at.checkbox(key="cv_zero").check().run()
    at.button(key="cv_run").click().run()
    assert not at.exception
    assert any("UNMAPPED_ROLE" in e.value and "toilet" in e.value for e in at.error)
    assert "cv_result" not in at.session_state
    assert not [d for d in at.download_button if d.key and d.key.startswith("cv_dl")]


def test_scenario_switch_resets_access_opt_ins_and_results_together():
    from test_coverage_app import examples
    from test_workspace_app import metrics
    at = examples()
    at.checkbox(key="ce_conservative_access").check().run()
    assert metrics(at)["Participating facilities"] == "5"
    at.radio(key="ce_example").set_value("public_toilets").run()
    assert not at.checkbox(key="ce_include_unknown").value
    assert metrics(at)["Participating facilities"] == "28"
    at.checkbox(key="ce_include_unknown").check().run()
    assert metrics(at)["Participating facilities"] == "82"
    at.radio(key="ce_example").set_value("ev_charging").run()
    assert not at.checkbox(key="ce_conservative_access").value
    assert not [w for w in at.checkbox if w.key == "ce_include_unknown"]
    assert metrics(at)["Participating facilities"] == "63"
    at.radio(key="ce_example").set_value("public_toilets").run()
    assert not at.checkbox(key="ce_include_unknown").value
    assert metrics(at)["Participating facilities"] == "28"
    assert not at.error and not at.exception
