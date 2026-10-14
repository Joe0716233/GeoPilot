"""WP1: explicit data roles, strict refusals and resource bounds for GeoDecision."""

import json
import pickle

import pytest

from geopilot.data import Entity
from geopilot.data.schema import Provenance
from geopilot.decision import (DEFAULT_LIMITS, ERROR_CODES, MAX_DISTANCE_M, DecisionConfig, DecisionInputError,
                               ResourceLimits, Role, RoleMapping, ScenarioConfig, generate_candidates, plan_coverage,
                               plan_locations)
from geopilot.decision import engine
from geopilot.reporting import to_json
from geopilot.scenario import run_decision
from geopilot.tools import geodesic_distance_m

from conftest import LAT0, LON0, SIDE_DEG, square

M_PER_DEG_LAT = geodesic_distance_m(LON0, LAT0, LON0, LAT0 + 1)
CX, CY = LON0 + SIDE_DEG / 2, LAT0 + SIDE_DEG / 2
TOILET_ROLES = RoleMapping({"toilet": Role.EXISTING_FACILITY, "household": "weighted_demand",
                            "bus stop": "context", "note": "ignore"}, context_metrics={"bus stop": "bus_stops"})


def toilet(sid, lon=CX, lat=CY):
    return Entity(source="osm", source_id=sid, lon=lon, lat=lat, category="toilet", name=f"WC {sid}",
                  properties={"amenity": "toilets", "fee": "no"},
                  provenance=[Provenance(source="osm", source_id=sid, retrieved_at="2026-10-01")])


def household(sid, lon, lat, residents):
    return Entity(source="census", source_id=sid, lon=lon, lat=lat, category="household",
                  properties={"residents": residents})


def cfg(**kw):
    kw.setdefault("roles", TOILET_ROLES)
    kw.setdefault("demand_weight_field", "residents")
    kw.setdefault("weights", {"demand_weight": 1.0, "bus_stops": 0.0})
    return ScenarioConfig(**kw)


def small_case():
    return [toilet("t1"),
            household("near", CX, CY + 200 / M_PER_DEG_LAT, 100),
            household("far", CX, CY - 1000 / M_PER_DEG_LAT, "300"),   # loader-style numeric string
            household("out", CX, LAT0 + 0.5, 999),                    # outside the study area
            Entity(source="osm", source_id="b1", lon=CX, lat=CY, category="bus stop"),
            Entity(source="misc", source_id="n1", category="note")]  # ignored, no coordinates


def code_of(excinfo):
    assert isinstance(excinfo.value, ValueError) and excinfo.value.code in ERROR_CODES
    return excinfo.value.code


# --- legacy entry point ---

def test_legacy_native_toilets_are_refused_not_treated_as_zero_baseline(zone_a):
    for entities in ([toilet("t1")],
                     [toilet("t1"), household("h", CX, CY, 10)],
                     [toilet("t1"), Entity(source="d", source_id="p", lon=CX, lat=CY, category="population",
                                           properties={"population": 10})]):
        with pytest.raises(DecisionInputError) as exc:
            plan_locations(zone_a, entities, DecisionConfig(target_count=1))
        assert code_of(exc) == "UNMAPPED_ROLE" and "toilet" in exc.value.detail
        assert "plan_coverage" in exc.value.detail and "toilet" in exc.value.context["categories"]


def test_legacy_blank_and_unknown_categories_are_refused(zone_a):
    blank = Entity(source="x", source_id="1", lon=CX, lat=CY)
    for entity in (blank, Entity(source="x", source_id="2", lon=CX, lat=CY, category="Population")):
        with pytest.raises(DecisionInputError) as exc:
            plan_locations(zone_a, [entity])
        assert code_of(exc) == "UNMAPPED_ROLE"


def test_legacy_errors_carry_codes_and_keep_messages(zone_a):
    with pytest.raises(DecisionInputError, match="numeric 'population'") as exc:
        plan_locations(zone_a, [Entity(source="d", source_id="1", lon=CX, lat=CY, category="population")])
    assert exc.value.code == "INVALID_DEMAND_WEIGHT"
    with pytest.raises(DecisionInputError, match="too large") as exc:
        plan_locations(square("H", lon0=-3.0, lat0=50.0, side=3.0), [])
    assert exc.value.code == "INVALID_STUDY_AREA"
    with pytest.raises(DecisionInputError) as exc:
        DecisionConfig(target_count=DEFAULT_LIMITS.max_target_count + 1)
    assert exc.value.code == "RESOURCE_LIMIT_EXCEEDED"
    with pytest.raises(DecisionInputError) as exc:
        DecisionConfig(grid_step_m=10)
    assert exc.value.code == "INVALID_SCENARIO_CONFIG"


# --- generic entry point: results and audit ---

def test_generic_toilets_coverage_is_exact_and_uses_demand_weight_names(zone_a):
    entities = small_case()
    before = [e.model_dump() for e in entities]
    r = plan_coverage(zone_a, entities, cfg(target_count=1, grid_step_m=250, service_radius_m=500,
                                           min_existing_distance_m=0))
    assert [e.model_dump() for e in entities] == before
    assert r["status"] == "SUCCESS" and r["actual_count"] == 1
    assert r["existing_facility_count"] == 1 and r["existing_facility_ids"] == ["osm:t1"]
    assert r["zero_existing_declared"] is False
    assert r["total_demand_weight"] == 400 and r["demand_point_count"] == 2
    assert r["existing_demand_weight_covered"] == 100
    assert r["new_incremental_demand_weight_covered"] == 300
    assert r["final_total_coverage_pct"] == pytest.approx(100.0)
    assert r["existing_covered_demand_ids"] == ["census:near"]
    assert r["final_covered_demand_ids"] == ["census:far", "census:near"]
    site = r["selected"][0]
    assert site["covered_demand_ids"] and site["newly_covered_demand_ids"] == ["census:far"]
    assert site["incremental_demand_weight"] == 300 and set(site["metrics"]) == {"demand_weight", "bus_stops"}
    text = to_json(r)
    assert "population" not in text and "existing_station" not in text
    json.loads(text)


def test_generic_audit_preserves_original_records_and_reports_exclusions(zone_a):
    entities = small_case()
    r = plan_coverage(zone_a, entities, cfg(target_count=1, min_existing_distance_m=0))
    audit = {row["key"]: row for row in r["inputs"]}
    assert len(audit) == len(entities)
    wc = audit["osm:t1"]
    assert (wc["category"], wc["role"], wc["status"]) == ("toilet", "existing_facility", "USED")
    assert wc["properties"] == {"amenity": "toilets", "fee": "no"}
    assert wc["provenance"][0]["retrieved_at"] == "2026-10-01"
    assert audit["osm:b1"]["metric"] == "bus_stops" and audit["census:far"]["demand_weight"] == 300
    assert audit["misc:n1"]["status"] == "IGNORED" and audit["misc:n1"]["inside_study_area"] is None
    assert audit["census:out"]["status"] == "OUTSIDE_STUDY_AREA"
    reasons = {x["key"]: x["reason"] for x in r["exclusions"]}
    assert reasons == {"misc:n1": "IGNORED_BY_ROLE_MAPPING", "census:out": "DEMAND_OUTSIDE_STUDY_AREA"}
    assert r["demand_points_outside_study_area"] == 1 and r["demand_weight_outside_study_area"] == 999
    assert r["role_summary"]["weighted_demand"] == {"records": 3, "inside_study_area": 2,
                                                    "outside_study_area": 1, "without_coordinates": 0}
    assert r["role_summary"]["ignore"]["without_coordinates"] == 1
    assert r["roles"]["toilet"] == "existing_facility" and r["context_metrics"] == {"bus stop": "bus_stops"}
    # The audit is a copy: editing it cannot reach the caller's records.
    wc["properties"]["fee"] = "yes"
    wc["provenance"][0]["source"] = "edited"
    assert entities[0].properties["fee"] == "no" and entities[0].provenance[0].source == "osm"


def test_generic_matches_legacy_on_the_london_sample(sample):
    legacy = run_decision(sample)
    weights = {"demand_weight": .45, "residential": .20, "commercial": .20, "transit": .15}
    generic = plan_coverage(sample.study_zone, sample.chargers + sample.planning_context,
                            ScenarioConfig(roles=RoleMapping.legacy(), demand_weight_field="population",
                                           weights=weights))
    assert [s["id"] for s in generic["selected"]] == [s["id"] for s in legacy["selected"]]
    assert generic["total_demand_weight"] == legacy["total_population"]
    assert generic["final_total_coverage_pct"] == pytest.approx(legacy["final_total_coverage_pct"])
    assert generic["existing_facility_count"] == legacy["existing_station_count"] == 42
    assert [c["score"] for c in generic["candidates"]] == [c["score"] for c in legacy["candidates"]]


def test_explicit_zero_existing_declaration(zone_a):
    roles = RoleMapping({"household": "weighted_demand"})
    r = plan_coverage(zone_a, [household("h", CX, CY, 50)],
                      ScenarioConfig(roles=roles, demand_weight_field="residents", allow_zero_existing=True,
                                     target_count=1))
    assert r["status"] == "SUCCESS" and r["existing_facility_count"] == 0 and r["zero_existing_declared"]
    assert r["existing_demand_weight_covered"] == 0 and r["final_total_coverage_pct"] == pytest.approx(100.0)
    with pytest.raises(DecisionInputError) as exc:      # undeclared zero baseline in the mapping
        ScenarioConfig(roles=roles, demand_weight_field="residents")
    assert code_of(exc) == "INVALID_SCENARIO_CONFIG" and "allow_zero_existing=True" in exc.value.detail


def test_reusable_mapping_with_zero_existing_records(zone_a):
    # The toilet mapping names an existing_facility category; this input simply has no toilets.
    households = [household("h", CX, CY, 50)]
    r = plan_coverage(zone_a, households, cfg(allow_zero_existing=True, target_count=1))
    assert r["status"] == "SUCCESS" and r["existing_facility_count"] == 0 and r["zero_existing_declared"]
    with pytest.raises(DecisionInputError) as exc:      # same input, zero baseline not declared
        plan_coverage(zone_a, households, cfg())
    assert code_of(exc) == "INVALID_SCENARIO_CONFIG" and "allow_zero_existing=True" in exc.value.detail
    with pytest.raises(DecisionInputError) as exc:      # declared zero, but facilities are present
        plan_coverage(zone_a, small_case(), cfg(allow_zero_existing=True))
    assert code_of(exc) == "INVALID_SCENARIO_CONFIG" and exc.value.context["existing_records"] == 1
    assert "allow_zero_existing=False" in exc.value.detail


# --- generic entry point: refusals ---

def test_generic_unmapped_and_blank_categories(zone_a):
    for extra in (Entity(source="x", source_id="1", lon=CX, lat=CY, category="kiosk"),
                  Entity(source="x", source_id="1", lon=CX, lat=CY),
                  Entity(source="x", source_id="1", category="Toilet")):      # no fuzzy/case matching
        with pytest.raises(DecisionInputError) as exc:
            plan_coverage(zone_a, small_case() + [extra], cfg())
        assert code_of(exc) == "UNMAPPED_ROLE" and exc.value.context["categories"] == [extra.category]


@pytest.mark.parametrize("value", [None, True, False, float("nan"), float("inf"), "inf", "NaN", -1, "-0.5",
                                   "", "many", [1], 10 ** 400])
def test_invalid_demand_weights_are_refused(zone_a, value):
    with pytest.raises(DecisionInputError) as exc:
        plan_coverage(zone_a, small_case() + [household("bad", CX, CY, value)], cfg())
    assert code_of(exc) == "INVALID_DEMAND_WEIGHT" and exc.value.context["keys"] == ["census:bad"]


def test_missing_weight_and_weights_outside_study_area_are_validated(zone_a):
    no_field = Entity(source="census", source_id="nf", lon=CX, lat=CY, category="household",
                      properties={"population": 5})
    outside_bad = household("far_bad", CX, LAT0 + 0.5, -3)
    for extra in (no_field, outside_bad):
        with pytest.raises(DecisionInputError) as exc:
            plan_coverage(zone_a, small_case() + [extra], cfg())
        assert code_of(exc) == "INVALID_DEMAND_WEIGHT" and exc.value.context["keys"] == [extra.key]
    with pytest.raises(DecisionInputError) as exc:      # configurable field name
        plan_coverage(zone_a, small_case(), cfg(demand_weight_field="households"))
    assert code_of(exc) == "INVALID_DEMAND_WEIGHT" and len(exc.value.context["keys"]) == 3
    with pytest.raises(DecisionInputError) as exc:
        plan_coverage(zone_a, [toilet("t"), household("a", CX, CY, 1e308), household("b", CX, CY, 1e308)], cfg())
    assert code_of(exc) == "INVALID_DEMAND_WEIGHT"


def test_missing_or_zero_demand_is_refused(zone_a):
    with pytest.raises(DecisionInputError) as exc:
        plan_coverage(zone_a, [toilet("t"), household("out", CX, LAT0 + 0.5, 10)], cfg())
    assert code_of(exc) == "NO_DEMAND" and exc.value.context["outside_count"] == 1
    with pytest.raises(DecisionInputError) as exc:
        plan_coverage(zone_a, [toilet("t")], cfg())
    assert code_of(exc) == "NO_DEMAND"
    with pytest.raises(DecisionInputError) as exc:
        plan_coverage(zone_a, [toilet("t"), household("a", CX, CY, 0), household("b", CX, CY, "0")], cfg())
    assert code_of(exc) == "NO_DEMAND" and "is 0" in exc.value.detail


def test_entity_validation_covers_ignored_records(zone_a):
    ignored_dup = Entity(source="misc", source_id="n1", category="note")   # same key as an ignored record
    with pytest.raises(DecisionInputError, match="duplicate") as exc:
        plan_coverage(zone_a, small_case() + [ignored_dup], cfg())
    assert code_of(exc) == "INVALID_ENTITY"
    with pytest.raises(DecisionInputError, match="missing coordinates") as exc:
        plan_coverage(zone_a, [Entity(source="osm", source_id="t", category="toilet")] + small_case()[1:], cfg())
    assert code_of(exc) == "INVALID_ENTITY" and exc.value.context["keys"] == ["osm:t"]
    for bad in ({"not": "an entity"}, None):
        with pytest.raises(DecisionInputError) as exc:
            plan_coverage(zone_a, small_case() + [bad], cfg())
        assert code_of(exc) == "INVALID_ENTITY"
    with pytest.raises(DecisionInputError) as exc:
        plan_coverage(zone_a, iter(small_case()), cfg())
    assert code_of(exc) == "INVALID_SCENARIO_CONFIG"


def test_invalid_study_area(zone_a):
    for zone in (square("H", lon0=-3.0, lat0=50.0, side=3.0), {"type": "Polygon"}, None):
        with pytest.raises(DecisionInputError) as exc:
            plan_coverage(zone, small_case(), cfg())
        assert code_of(exc) == "INVALID_STUDY_AREA"
    with pytest.raises(DecisionInputError, match="too large"):
        generate_candidates(square("H", lon0=-3.0, lat0=50.0, side=3.0), 500)


@pytest.mark.parametrize("kwargs, fragment", [
    ({"roles": {}}, "non-empty"),
    ({"roles": RoleMapping.legacy()}, "unsupported metric"),
    ({"weights": {"demand_weight": 1.0}}, "without a weight"),
    ({"weights": {"demand_weight": 1.0, "bus_stops": 0, "population": 1}}, "unsupported metric"),
    ({"weights": {"demand_weight": 0, "bus_stops": 0}}, "positive sum"),
    ({"weights": {"demand_weight": True, "bus_stops": 0}}, "finite number"),
    ({"demand_weight_field": " "}, "demand_weight_field"),
    ({"allow_zero_existing": "no"}, "allow_zero_existing"),
    ({"target_count": 0}, "target_count"),
    ({"service_radius_m": float("inf")}, "service_radius_m"),
    ({"limits": {"max_entities": 5}}, "ResourceLimits"),
])
def test_scenario_config_validation(kwargs, fragment):
    with pytest.raises(DecisionInputError, match=fragment) as exc:
        cfg(**kwargs)
    assert exc.value.code == "INVALID_SCENARIO_CONFIG"


@pytest.mark.parametrize("roles, metrics, fragment", [
    ({"toilet": "existing_station"}, {}, "unknown role"),
    ({" ": "ignore"}, {}, "non-blank"),
    ({"toilet": "existing_facility"}, {"toilet": "x"}, "not mapped to the context role"),
    ({"bus stop": "context"}, {}, "needs a metric name"),
    ({"stops": "context"}, {"stops": "demand_weight"}, "needs a metric name"),
])
def test_role_mapping_validation(roles, metrics, fragment):
    with pytest.raises(DecisionInputError, match=fragment) as exc:
        RoleMapping(roles, context_metrics=metrics)
    assert exc.value.code == "INVALID_SCENARIO_CONFIG"


def test_plan_coverage_requires_scenario_config(zone_a):
    with pytest.raises(DecisionInputError) as exc:
        plan_coverage(zone_a, small_case(), DecisionConfig())
    assert code_of(exc) == "INVALID_SCENARIO_CONFIG"


# --- resource bounds ---

def test_candidate_grid_is_bounded_before_generation(monkeypatch):
    big = square("B", lon0=-1.0, lat0=51.0, side=1.6)   # ~110 x 178 km, within the local-projection limit

    def boom(*_args, **_kwargs):
        raise AssertionError("grid cells were generated")
    monkeypatch.setattr(engine, "Point", boom)
    with pytest.raises(DecisionInputError) as exc:
        generate_candidates(big, 50)
    assert code_of(exc) == "RESOURCE_LIMIT_EXCEEDED" and exc.value.context["limit"] == DEFAULT_LIMITS.max_grid_cells
    for step in (0, -5, float("nan"), True):
        with pytest.raises(DecisionInputError) as exc:
            generate_candidates(big, step)
        assert code_of(exc) == "INVALID_SCENARIO_CONFIG"


@pytest.mark.parametrize("limits, quantity", [
    (ResourceLimits(max_entities=3), "input entities"),
    (ResourceLimits(max_grid_cells=10), "candidate grid cells"),
    (ResourceLimits(max_pair_evaluations=50), "candidate x point pairs"),
    (ResourceLimits(max_greedy_evaluations=5), "greedy evaluations"),
    (ResourceLimits(max_zone_vertices=4), "study area vertices"),
])
def test_generic_resource_limits(zone_a, limits, quantity):
    with pytest.raises(DecisionInputError) as exc:
        plan_coverage(zone_a, small_case(), cfg(limits=limits, grid_step_m=250))
    assert code_of(exc) == "RESOURCE_LIMIT_EXCEEDED" and exc.value.context["quantity"] == quantity


def test_resource_limits_cannot_be_raised_and_target_is_bounded():
    with pytest.raises(DecisionInputError) as exc:
        ResourceLimits(max_entities=DEFAULT_LIMITS.max_entities + 1)
    assert exc.value.code == "INVALID_SCENARIO_CONFIG"
    for bad in (0, True, 2.5):
        with pytest.raises(DecisionInputError):
            ResourceLimits(max_grid_cells=bad)
    with pytest.raises(DecisionInputError) as exc:
        cfg(target_count=11, limits=ResourceLimits(max_target_count=10))
    assert exc.value.code == "RESOURCE_LIMIT_EXCEEDED"


def test_legacy_entry_is_bounded_too(zone_a):
    pulled = []

    def stations():
        for i in range(10_000):
            pulled.append(i)
            yield Entity(source="s", source_id=str(i), lon=CX, lat=CY, category="existing_station")
    with pytest.raises(DecisionInputError) as exc:
        plan_locations(zone_a, stations(), limits=ResourceLimits(max_entities=2))
    assert code_of(exc) == "RESOURCE_LIMIT_EXCEEDED" and len(pulled) == 3   # never materialised past the bound
    with pytest.raises(DecisionInputError) as exc:
        plan_locations(zone_a, [], DecisionConfig(target_count=5), limits=ResourceLimits(max_target_count=3))
    assert code_of(exc) == "RESOURCE_LIMIT_EXCEEDED"
    with pytest.raises(DecisionInputError) as exc:
        plan_locations(zone_a, [], limits=ResourceLimits(max_zone_vertices=4))
    assert code_of(exc) == "RESOURCE_LIMIT_EXCEEDED" and exc.value.context["quantity"] == "study area vertices"
    with pytest.raises(DecisionInputError) as exc:
        plan_locations(zone_a, [], limits={"max_entities": 1})
    assert code_of(exc) == "INVALID_SCENARIO_CONFIG"


# --- review repairs: numeric extremes, far points, method boundary ---

@pytest.mark.parametrize("name", ["grid_step_m", "service_radius_m", "min_existing_distance_m",
                                  "min_new_site_distance_m"])
def test_distance_parameters_are_bounded(name):
    for make in (DecisionConfig, cfg):
        with pytest.raises(DecisionInputError) as exc:
            make(**{name: 1e308})
        assert exc.value.code == "INVALID_SCENARIO_CONFIG" and name in exc.value.detail
        make(**{name: MAX_DISTANCE_M})          # the bound itself is accepted


def test_huge_finite_demand_gives_finite_percentages(zone_a):
    generic = plan_coverage(zone_a, [toilet("t"), household("h", CX, CY, 1e308)], cfg(target_count=1))
    assert generic["existing_coverage_pct"] == pytest.approx(100.0)
    json.dumps(generic, allow_nan=False, default=str)
    legacy = plan_locations(zone_a, [Entity(source="s", source_id="1", lon=CX, lat=CY, category="existing_station"),
                                     Entity(source="d", source_id="1", lon=CX, lat=CY, category="population",
                                            properties={"population": 1e308})], DecisionConfig(target_count=1))
    assert legacy["existing_coverage_pct"] == pytest.approx(100.0)
    json.dumps(legacy, allow_nan=False)


def test_far_active_points_are_refused_but_far_demand_is_only_excluded(zone_a):
    far = (CX + 5.0, CY)                        # ~350 km east of the study area
    for entity in (toilet("far", *far), Entity(source="osm", source_id="bf", lon=far[0], lat=far[1],
                                               category="bus stop")):
        with pytest.raises(DecisionInputError) as exc:
            plan_coverage(zone_a, small_case() + [entity], cfg())
        assert code_of(exc) == "INVALID_ENTITY" and exc.value.context["keys"] == [entity.key]
        assert "km from the study-area centre" in exc.value.detail
    r = plan_coverage(zone_a, small_case() + [household("remote", *far, 7)], cfg(min_existing_distance_m=0))
    assert r["demand_points_outside_study_area"] == 2 and r["demand_weight_outside_study_area"] == 999 + 7
    with pytest.raises(DecisionInputError) as exc:      # its weight is still validated
        plan_coverage(zone_a, small_case() + [household("remote", *far, "n/a")], cfg())
    assert code_of(exc) == "INVALID_DEMAND_WEIGHT"
    ignored_far = Entity(source="misc", source_id="nf", lon=far[0], lat=far[1], category="note")
    assert plan_coverage(zone_a, small_case() + [ignored_far], cfg())["status"] in {"SUCCESS", "PARTIAL"}


def test_unsupported_method_is_refused_with_method_boundary(zone_a):
    for method in ("p_median", "travel_time_coverage", "", None):
        with pytest.raises(DecisionInputError) as exc:
            cfg(method=method)
        assert code_of(exc) == "INVALID_SCENARIO_CONFIG" and exc.value.context["supported"] == ["weighted_coverage"]
        assert "Not routing, travel time" in exc.value.detail
    r = plan_coverage(zone_a, small_case(), cfg(method="weighted_coverage", target_count=1))
    assert r["method"] == "weighted_coverage" and r["config"]["method"] == "weighted_coverage"
    assert "Not routing" in r["method_boundary"]


def test_legacy_population_rejects_booleans_consistently(zone_a):
    for value in (True, False):
        entity = Entity(source="d", source_id="1", lon=CX, lat=CY, category="population",
                        properties={"population": value})
        with pytest.raises(DecisionInputError, match="numeric 'population'") as exc:
            plan_locations(zone_a, [entity])
        assert exc.value.code == "INVALID_DEMAND_WEIGHT"


# --- immutability and error stability ---

def test_configs_copy_their_mappings_and_are_read_only():
    roles = {"toilet": "existing_facility", "household": "weighted_demand"}
    weights = {"demand_weight": 1.0}
    mapping = RoleMapping(roles)
    config = ScenarioConfig(roles=mapping, weights=weights)
    roles["toilet"] = "ignore"
    weights["demand_weight"] = -1
    assert mapping.role_of("toilet") is Role.EXISTING_FACILITY and config.weights == {"demand_weight": 1.0}
    with pytest.raises(TypeError):
        config.weights["demand_weight"] = 2
    with pytest.raises(TypeError):
        mapping.roles["kiosk"] = Role.IGNORE
    legacy_weights = {"population": 1.0}
    legacy = DecisionConfig(weights=legacy_weights)
    legacy_weights["population"] = -1
    assert legacy.weights == {"population": 1.0}
    with pytest.raises(TypeError):
        DecisionConfig().weights["population"] = 0
    assert pickle.loads(pickle.dumps(config)) == config and hash(config) == hash(pickle.loads(pickle.dumps(config)))
    assert RoleMapping({"toilet": "EXISTING_FACILITY", "h": "weighted_demand"}).role_of("toilet") is Role.EXISTING_FACILITY


def test_error_is_a_stable_value_error():
    err = DecisionInputError("UNMAPPED_ROLE", "no role", categories=["toilet"])
    assert isinstance(err, ValueError) and str(err) == "UNMAPPED_ROLE: no role"
    again = pickle.loads(pickle.dumps(err))
    assert (again.code, again.detail, again.context) == ("UNMAPPED_ROLE", "no role", {"categories": ["toilet"]})
    with pytest.raises(AssertionError):
        DecisionInputError("SOMETHING_ELSE", "x")


# Codex independent preflight regressions: Python integers need not fit in a float.
@pytest.mark.parametrize("kwargs", [{"service_radius_m": 10**400}, {"weights": {"population": 10**400}}])
def test_unrepresentable_config_numbers_are_structured_errors(kwargs):
    with pytest.raises(DecisionInputError) as exc:
        DecisionConfig(**kwargs)
    assert exc.value.code == "INVALID_SCENARIO_CONFIG"


def test_unrepresentable_legacy_demand_is_a_structured_error(zone_a):
    entity = Entity(source="audit", source_id="huge", lon=CX, lat=CY, category="population",
                    properties={"population": 10**400})
    with pytest.raises(DecisionInputError) as exc:
        plan_locations(zone_a, [entity])
    assert exc.value.code == "INVALID_DEMAND_WEIGHT"
