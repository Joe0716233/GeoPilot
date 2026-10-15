"""WP4: EV-charging and public-toilet coverage examples through one generic plan_coverage."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from pyproj import Geod
from shapely.geometry import Point

import geopilot.coverage_examples as ce
from geopilot.decision import (DecisionInputError, EligibilityPolicy, RoleMapping, ScenarioConfig, apply_eligibility,
                               plan_coverage)
from geopilot.reporting import tabular_views, to_csv, to_json

ROOT = Path(__file__).resolve().parents[1]
OSM = ROOT / "examples" / "data" / "coverage" / "osm_facilities_southwark.json"
GEOD = Geod(ellps="WGS84")


@pytest.fixture(scope="module")
def inputs():
    return ce.load_coverage_inputs()


@pytest.fixture(scope="module")
def results(inputs):
    return {key: ce.run_coverage_example(key, inputs=inputs) for key in ce.EXAMPLES}


def metres(a, b):
    return GEOD.inv(a[0], a[1], b[0], b[1])[2]


def test_bundled_osm_file_is_the_reviewed_source(inputs):
    assert hashlib.sha256(OSM.read_bytes()).hexdigest() == \
        "c1ce55576fd2afef94b75633f1577ab8f60cd18ddf4e7dbf1ab0881bd443e482"
    assert len(inputs.facilities) == 163
    assert {c: sum(e.category == c for e in inputs.facilities) for c in ("charging_station", "toilets")} == \
        {"charging_station": 63, "toilets": 100}
    assert all(p.license_note == "ODbL 1.0; © OpenStreetMap contributors"
               for e in inputs.facilities for p in e.provenance)
    assert sum(e.properties["coordinate_kind"] == "OSM_BOUNDING_BOX_CENTER_PROXY" for e in inputs.facilities) == 18
    assert inputs.zone.zone_id == "Southwark" and len(inputs.demand_context) == 227


def test_both_examples_go_through_the_same_plan_coverage(monkeypatch, inputs):
    calls = []

    def spy(zone, entities, config):
        calls.append((zone.zone_id, {e.category for e in entities}, config))
        return plan_coverage(zone, entities, config)
    monkeypatch.setattr(ce, "plan_coverage", spy)
    for key in ce.EXAMPLES:
        ce.run_coverage_example(key, inputs=inputs)
    (zone_a, cats_a, cfg_a), (zone_b, cats_b, cfg_b) = calls
    assert zone_a == zone_b == "Southwark"
    assert cats_a == {"charging_station", "population", "residential", "commercial", "transit"}
    assert cats_b == {"toilets", "population", "residential", "commercial", "transit"}
    same = ("demand_weight_field", "weights", "target_count", "grid_step_m", "service_radius_m",
            "min_existing_distance_m", "min_new_site_distance_m", "method", "allow_zero_existing", "limits")
    assert all(getattr(cfg_a, f) == getattr(cfg_b, f) for f in same)
    assert cfg_a.roles.categories("existing_facility") == ("charging_station",)
    assert cfg_b.roles.categories("existing_facility") == ("toilets",)


def test_example_results_match_independent_evidence(results):
    ev, wc = results["ev_charging"], results["public_toilets"]
    for r, facilities, feasible, existing, added in ((ev, 63, 105, 14350, 7460), (wc, 28, 110, 6010, 7180)):
        assert (r["status"], r["existing_facility_count"], r["demand_point_count"]) == ("SUCCESS", facilities, 140)
        assert (r["generated_candidate_count"], r["feasible_candidate_count"]) == (119, feasible)
        assert [s["id"] for s in r["selected"]] == ["C074", "C051", "C090"]
        assert (r["existing_demand_weight_covered"], r["new_incremental_demand_weight_covered"]) == (existing, added)
        assert r["final_total_demand_weight_covered"] == existing + added and r["total_demand_weight"] == 38160
        assert r["config"]["weights"] == {"demand_weight": 1.0, "residential": 0.0, "commercial": 0.0, "transit": 0.0}
        assert (r["config"]["grid_step_m"], r["config"]["service_radius_m"], r["config"]["min_existing_distance_m"],
                r["config"]["min_new_site_distance_m"], r["config"]["target_count"]) == (500, 500, 250, 600, 3)
        assert r["example"]["demand_units"] == "synthetic demand units"


def test_coverage_math_checked_with_geodesic_distances(results, inputs):
    demand = [e for e in inputs.demand_context
              if e.category == "population" and inputs.zone.shape.covers(Point(e.lon, e.lat))]
    weight = {e.key: float(e.properties["population"]) for e in demand}
    for r in results.values():
        facilities = [(row["lon"], row["lat"]) for row in r["inputs"] if row["role"] == "existing_facility"]
        sites = [(s["lon"], s["lat"]) for s in r["selected"]]
        base = {e.key for e in demand if any(metres((e.lon, e.lat), f) <= 500 for f in facilities)}
        final = base | {e.key for e in demand if any(metres((e.lon, e.lat), s) <= 500 for s in sites)}
        assert sum(weight[k] for k in base) == r["existing_demand_weight_covered"]
        assert sum(weight[k] for k in final) == r["final_total_demand_weight_covered"]
        for c in r["candidates"]:
            assert inputs.zone.shape.covers(Point(c["lon"], c["lat"]))
            assert min(metres((c["lon"], c["lat"]), f) for f in facilities) >= 250 - 1
        for i, a in enumerate(sites):
            assert all(metres(a, b) >= 600 - 1 for b in sites[i + 1:])


@pytest.mark.parametrize("params", [
    {"target_count": 5, "grid_step_m": 400, "service_radius_m": 900, "min_existing_distance_m": 700,
     "min_new_site_distance_m": 1000},
    {"target_count": 2, "grid_step_m": 750, "service_radius_m": 300, "min_existing_distance_m": 0,
     "min_new_site_distance_m": 1500},
])
def test_every_configured_distance_is_respected(params, inputs):
    for key in ce.EXAMPLES:
        r = ce.run_coverage_example(key, inputs=inputs, **params)
        assert {k: r["config"][k] for k in params} == params
        facilities = [(row["lon"], row["lat"]) for row in r["inputs"] if row["role"] == "existing_facility"]
        sites = [(s["lon"], s["lat"]) for s in r["selected"]]
        assert len(sites) <= params["target_count"]
        for c in r["candidates"]:
            assert min(metres((c["lon"], c["lat"]), f) for f in facilities) >= params["min_existing_distance_m"] - 1
        for i, a in enumerate(sites):
            assert all(metres(a, b) >= params["min_new_site_distance_m"] - 1 for b in sites[i + 1:])
        for s in r["selected"]:
            covered = {row["key"] for row in r["inputs"] if row["role"] == "weighted_demand" and row["status"] == "USED"
                       and metres((row["lon"], row["lat"]), (s["lon"], s["lat"])) <= params["service_radius_m"]}
            assert covered == set(s["covered_demand_ids"])


def test_toilet_access_policy_audit(results, inputs):
    eligibility = results["public_toilets"]["eligibility"]
    assert eligibility["counts"] == {"records": 100, "ELIGIBLE": 28, "EXCLUDED_RESTRICTED": 18,
                                     "EXCLUDED_OTHER_VALUE": 0, "EXCLUDED_UNKNOWN": 54, "INCLUDED_UNKNOWN": 0}
    audit = eligibility["audit"]
    restricted = sorted(a["value"] for a in audit if a["decision"] == "EXCLUDED_RESTRICTED")
    assert restricted == ["customers"] * 10 + ["permit"] * 6 + ["private"] * 2
    assert sorted({a["value"] for a in audit if a["decision"] == "ELIGIBLE"}) == ["permissive", "yes"]
    assert all(a["value"] is None for a in audit if a["decision"] == "EXCLUDED_UNKNOWN")
    by_id = {e.source_id: e for e in inputs.facilities}
    for a in audit:
        assert a["category"] == "toilets" and a["source_id"].startswith(("node/", "way/")) and a["reason"]
        assert (a["lon"], a["lat"]) == (by_id[a["source_id"]].lon, by_id[a["source_id"]].lat)
    assert results["ev_charging"]["eligibility"] is None
    assert results["ev_charging"]["example"]["facilities_used"] == 63
    opted = ce.run_coverage_example("public_toilets", inputs=inputs, include_unknown_access=True)
    assert opted["existing_facility_count"] == 82 and opted["eligibility"]["counts"]["INCLUDED_UNKNOWN"] == 54
    assert opted["eligibility"]["warnings"] and "may not be usable" in opted["eligibility"]["warnings"][0]
    notes = " ".join(results["public_toilets"]["example"]["notes"])
    assert "not validated" in notes and "synthetic demand units" in notes


def test_raw_categories_tags_and_provenance_are_preserved(results, inputs):
    before = [e.model_dump() for e in inputs.facilities]
    r = ce.run_coverage_example("public_toilets", inputs=inputs)
    assert [e.model_dump() for e in inputs.facilities] == before
    source = {e.key: e for e in inputs.facilities}
    rows = [row for row in r["inputs"] if row["role"] == "existing_facility"]
    assert len(rows) == 28
    for row in rows:
        original = source[row["key"]]
        assert row["category"] == "toilets" and row["properties"] == original.properties
        assert row["provenance"] == [p.model_dump() for p in original.provenance]
    outside = [row for row in results["ev_charging"]["inputs"]
               if row["role"] == "existing_facility" and not row["inside_study_area"]]
    assert outside and all(row["status"] == "USED" for row in outside)    # outside facilities still count


def test_missing_mapping_and_unsupported_method_are_refused(inputs):
    config = ce.example_config("ev_charging")
    with pytest.raises(DecisionInputError) as exc:                      # toilets are not mapped in the EV config
        plan_coverage(inputs.zone, list(inputs.facilities) + list(inputs.demand_context), config)
    assert exc.value.code == "UNMAPPED_ROLE" and exc.value.context["categories"] == ["toilets"]
    partial = ScenarioConfig(roles=RoleMapping({"charging_station": "existing_facility", "population": "weighted_demand"}),
                             demand_weight_field="population")
    with pytest.raises(DecisionInputError) as exc:
        plan_coverage(inputs.zone, [e for e in inputs.facilities if e.category == "charging_station"]
                      + list(inputs.demand_context), partial)
    assert exc.value.code == "UNMAPPED_ROLE" and exc.value.context["categories"] == ["commercial", "residential",
                                                                                    "transit"]
    for method in ("p_median", "travel_time_coverage"):
        with pytest.raises(DecisionInputError) as exc:
            ce.run_coverage_example("public_toilets", inputs=inputs, method=method)
        assert exc.value.code == "INVALID_SCENARIO_CONFIG" and exc.value.context["supported"] == ["weighted_coverage"]
    with pytest.raises(ValueError, match="unknown coverage example"):
        ce.run_coverage_example("libraries", inputs=inputs)


def test_examples_are_deterministic_and_exportable(inputs, results):
    for key, r in results.items():
        again = ce.run_coverage_example(key, inputs=inputs)
        assert to_json(again) == to_json(r)
        views = tabular_views("coverage", r)
        assert [row["selected_rank"] for row in views["coverage_selected"]] == [1, 2, 3]
        assert len(views["coverage_inputs"]) == len(r["inputs"])
        for rows in views.values():
            to_csv(rows)
    assert len(tabular_views("coverage", results["public_toilets"])["coverage_facility_eligibility"]) == 100


def test_modified_or_missing_bundle_is_refused(tmp_path):
    target = tmp_path / "coverage"
    shutil.copytree(OSM.parent, target)
    data = json.loads((target / ce.OSM_FILE).read_text(encoding="utf-8"))
    data[0]["category"] = "edited"
    (target / ce.OSM_FILE).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match its manifest"):
        ce.load_coverage_inputs(target)
    with pytest.raises(FileNotFoundError, match="GEOPILOT_COVERAGE_DATA_DIR"):
        ce.load_coverage_inputs(tmp_path / "missing")


def test_legacy_london_decision_is_unchanged(sample):
    from geopilot.scenario import run_decision
    d = run_decision(sample)
    assert (d["existing_station_count"], d["status"], d["actual_count"]) == (42, "SUCCESS", 3)
    assert (d["config"]["service_radius_m"], d["config"]["min_existing_distance_m"], d["config"]["grid_step_m"]) == (
        900, 700, 500)
    assert d["final_total_coverage_pct"] == pytest.approx(44.7, abs=0.05)


# --- final review regressions ---

@pytest.mark.parametrize("kwargs, fragment", [
    ({"eligible_values": "yes"}, "eligible_values must be a set, list or tuple"),
    ({"eligible_values": None}, "eligible_values must be a set, list or tuple"),
    ({"eligible_values": {"yes": True}}, "eligible_values must be a set, list or tuple"),
    ({"eligible_values": {"yes", 1}}, "entries must be non-blank strings"),
    ({"eligible_values": ["yes", " "]}, "entries must be non-blank strings"),
    ({"restricted_values": "no"}, "restricted_values must be a set, list or tuple"),
    ({"property_path": "raw_tags"}, "property_path must be a list or tuple"),
    ({"property_path": ("raw_tags", "")}, "property_path must be a list or tuple"),
    ({"property_path": [1]}, "property_path must be a list or tuple"),
])
def test_eligibility_policy_rejects_wrong_container_types(kwargs, fragment):
    base = {"attribute": "access", "eligible_values": frozenset({"yes"}), "property_path": ("raw_tags",)}
    with pytest.raises(DecisionInputError, match=fragment) as exc:
        EligibilityPolicy(**{**base, **kwargs})
    assert exc.value.code == "INVALID_SCENARIO_CONFIG"
    assert EligibilityPolicy("access", ["Yes"], property_path=["raw_tags"]).property_path == ("raw_tags",)


def test_apply_eligibility_checks_its_input_like_the_core(inputs):
    from geopilot.decision import ResourceLimits
    toilets = [e for e in inputs.facilities if e.category == "toilets"]
    for entities, code in ((iter(toilets), "INVALID_SCENARIO_CONFIG"), (toilets + [{"category": "toilets"}],
                                                                         "INVALID_ENTITY"),
                           (toilets + toilets[:1], "INVALID_ENTITY")):
        with pytest.raises(DecisionInputError) as exc:
            apply_eligibility(entities, ce.TOILET_ACCESS_POLICY)
        assert exc.value.code == code
    with pytest.raises(DecisionInputError) as exc:
        apply_eligibility(toilets, ce.TOILET_ACCESS_POLICY, ResourceLimits(max_entities=50))
    assert exc.value.code == "RESOURCE_LIMIT_EXCEEDED"
    with pytest.raises(DecisionInputError) as exc:
        apply_eligibility(toilets, ce.TOILET_ACCESS_POLICY, {"max_entities": 50})
    assert exc.value.code == "INVALID_SCENARIO_CONFIG"


def test_explicit_weights_are_never_replaced_by_defaults(inputs):
    for weights in ({}, {"demand_weight": 0.0, "residential": 0.0, "commercial": 0.0, "transit": 0.0},
                    {"demand_weight": 1.0}):
        with pytest.raises(DecisionInputError) as exc:
            ce.run_coverage_example("ev_charging", inputs=inputs, weights=weights)
        assert exc.value.code == "INVALID_SCENARIO_CONFIG"
    assert dict(ce.example_config("ev_charging", weights=None).weights) == ce.DEFAULT_WEIGHTS


def test_unknown_example_key_is_a_stable_config_error(inputs):
    for key in ("libraries", None, ["ev_charging"]):
        for call in (lambda: ce.example_config(key), lambda: ce.run_coverage_example(key, inputs=inputs)):
            with pytest.raises(DecisionInputError) as exc:
                call()
            assert exc.value.code == "INVALID_SCENARIO_CONFIG" and exc.value.context["supported"] == list(ce.EXAMPLES)


def test_caller_limits_reach_coverage_and_eligibility(inputs):
    from geopilot.decision import ResourceLimits
    with pytest.raises(DecisionInputError) as exc:
        ce.run_coverage_example("ev_charging", inputs=inputs, limits=ResourceLimits(max_grid_cells=50))
    assert exc.value.code == "RESOURCE_LIMIT_EXCEEDED" and exc.value.context["quantity"] == "candidate grid cells"
    with pytest.raises(DecisionInputError) as exc:                    # 100 toilets > 50 before any filtering
        ce.run_coverage_example("public_toilets", inputs=inputs, limits=ResourceLimits(max_entities=50))
    assert exc.value.code == "RESOURCE_LIMIT_EXCEEDED" and exc.value.context["quantity"] == "input entities"


# --- EV access transparency and optional conservative filter ---

def test_ev_default_is_unchanged_and_reports_raw_access_counts(results):
    ev = results["ev_charging"]
    assert ev["eligibility"] is None and ev["example"]["access_filter"] == "none"
    assert (ev["existing_facility_count"], ev["final_total_demand_weight_covered"]) == (63, 21810)
    summary = ev["access_summary"]
    assert (summary["records"], summary["confirmed"], summary["known_restricted"], summary["other_values"],
            summary["unknown"]) == (63, 5, 4, 0, 54)
    assert summary["values"] == {"(missing)": 54, "customers": 1, "no": 2, "taxi": 1, "yes": 5}
    assert summary["filter_applied"] is False and "do not establish current public availability" in summary["note"]
    assert "not a statement about public availability" in " ".join(ev["example"]["notes"])
    assert ce.EXAMPLES["ev_charging"].eligibility is None                         # contract kept
    assert ce.CoverageExample("k", "t", "t", "c", None, ()).optional_eligibility is None   # old 6-field form works


def test_ev_conservative_access_filter(inputs):
    before = [e.model_dump() for e in inputs.facilities]
    strict = ce.run_coverage_example("ev_charging", inputs=inputs, conservative_access=True)
    assert [e.model_dump() for e in inputs.facilities] == before                 # raw records untouched
    assert hashlib.sha256(OSM.read_bytes()).hexdigest() == \
        "c1ce55576fd2afef94b75633f1577ab8f60cd18ddf4e7dbf1ab0881bd443e482"
    assert strict["example"]["access_filter"] == "conservative" and strict["access_summary"]["filter_applied"]
    assert strict["eligibility"]["counts"] == {"records": 63, "ELIGIBLE": 5, "EXCLUDED_RESTRICTED": 4,
                                               "EXCLUDED_OTHER_VALUE": 0, "EXCLUDED_UNKNOWN": 54,
                                               "INCLUDED_UNKNOWN": 0}
    assert strict["eligibility"]["policy"]["restricted"] == ["customers", "no", "permit", "private", "taxi"]
    audit = strict["eligibility"]["audit"]
    assert sorted(a["value"] for a in audit if a["decision"] == "EXCLUDED_RESTRICTED") == ["customers", "no", "no",
                                                                                           "taxi"]
    assert all(a["category"] == "charging_station" and a["source_id"].startswith(("node/", "way/")) for a in audit)
    used = [row for row in strict["inputs"] if row["role"] == "existing_facility"]
    assert len(used) == strict["existing_facility_count"] == 5
    assert all(row["properties"]["raw_tags"]["access"] in {"yes", "permissive"} for row in used)
    assert all(row["provenance"][0]["license_note"] == "ODbL 1.0; © OpenStreetMap contributors" for row in used)
    # Coverage of the 5 kept facilities, rechecked with geodesic distances.
    demand = [e for e in inputs.demand_context
              if e.category == "population" and inputs.zone.shape.covers(Point(e.lon, e.lat))]
    facilities = [(row["lon"], row["lat"]) for row in used]
    base = sum(float(e.properties["population"]) for e in demand
               if any(metres((e.lon, e.lat), f) <= 500 for f in facilities))
    assert base == strict["existing_demand_weight_covered"] == 3150
    assert strict["final_total_demand_weight_covered"] == 11620
    assert ce.run_coverage_example("ev_charging", inputs=inputs, conservative_access=True) == strict   # deterministic


def test_ev_conservative_filter_unknown_opt_in_and_unlisted_values(inputs):
    opted = ce.run_coverage_example("ev_charging", inputs=inputs, conservative_access=True, include_unknown_access=True)
    assert opted["existing_facility_count"] == 59 and opted["eligibility"]["counts"]["INCLUDED_UNKNOWN"] == 54
    assert opted["eligibility"]["warnings"]
    # An access value that is neither confirmed nor a known restriction is still excluded.
    charger = next(e for e in inputs.facilities if e.category == "charging_station")
    odd = charger.model_copy(update={"source_id": "node/test", "properties": {"raw_tags": {"access": "destination"}}})
    checked = apply_eligibility([odd], ce.EV_CONSERVATIVE_ACCESS_POLICY)
    assert checked.kept == () and checked.audit[0]["decision"] == "EXCLUDED_OTHER_VALUE"


def test_conservative_access_flag_is_validated_and_does_not_change_toilets(inputs, results):
    with pytest.raises(DecisionInputError) as exc:
        ce.run_coverage_example("ev_charging", inputs=inputs, conservative_access="yes")
    assert exc.value.code == "INVALID_SCENARIO_CONFIG"
    toilets = ce.run_coverage_example("public_toilets", inputs=inputs, conservative_access=True)
    assert to_json(toilets) == to_json(results["public_toilets"])
    assert toilets["access_summary"]["confirmed"] == 28 and toilets["access_summary"]["known_restricted"] == 18


# --- generic eligibility helper ---

def test_eligibility_policy_is_generic_and_validated(inputs):
    chargers = [e for e in inputs.facilities if e.category == "charging_station"]
    policy = EligibilityPolicy("ACCESS".lower(), frozenset({" Yes "}), frozenset({"no"}), property_path=("raw_tags",))
    result = apply_eligibility(chargers, policy)
    assert result.counts == {"records": 63, "ELIGIBLE": 5, "EXCLUDED_RESTRICTED": 2, "EXCLUDED_OTHER_VALUE": 2,
                             "EXCLUDED_UNKNOWN": 54, "INCLUDED_UNKNOWN": 0}
    assert sorted(a["value"] for a in result.audit if a["decision"] == "EXCLUDED_OTHER_VALUE") == ["customers", "taxi"]
    for bad in ({"attribute": " ", "eligible_values": frozenset({"yes"})},
                {"attribute": "access", "eligible_values": frozenset()},
                {"attribute": "access", "eligible_values": frozenset({"yes"}), "restricted_values": frozenset({"YES"})},
                {"attribute": "access", "eligible_values": frozenset({"yes"}), "include_unknown": "yes"}):
        with pytest.raises(DecisionInputError) as exc:
            EligibilityPolicy(**bad)
        assert exc.value.code == "INVALID_SCENARIO_CONFIG"
