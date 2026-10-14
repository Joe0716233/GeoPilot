import json
import math

import pytest

from geopilot.data import Entity
from geopilot.decision import DecisionConfig, generate_candidates, plan_locations
from geopilot.tools import geodesic_distance_m

from conftest import LAT0, LON0, SIDE_DEG, square

M_PER_DEG_LAT = geodesic_distance_m(LON0, LAT0, LON0, LAT0 + 1)


def demand(sid, lon, lat, pop):
    return Entity(source="d", source_id=sid, lon=lon, lat=lat, category="population", properties={"population": pop})


def station(sid, lon, lat):
    return Entity(source="s", source_id=sid, lon=lon, lat=lat, category="existing_station")


def test_candidates_are_inside_zone_and_on_the_requested_grid(zone_a):
    cands = generate_candidates(zone_a, 500)
    assert cands and all(zone_a.shape.covers(__import__("shapely.geometry", fromlist=["Point"]).Point(c["lon"], c["lat"]))
                         for c in cands)
    xs = sorted({round(c["x"], 3) for c in cands})
    assert all(b - a == pytest.approx(500, abs=1e-6) for a, b in zip(xs, xs[1:]))
    assert generate_candidates(zone_a, 500) == cands  # deterministic


def test_coverage_numbers_are_exact_on_a_small_case(zone_a):
    cx, cy = LON0 + SIDE_DEG / 2, LAT0 + SIDE_DEG / 2
    near_station = demand("near", cx, cy + 200 / M_PER_DEG_LAT, 100)   # within 900 m of the station
    far_a = demand("far_a", cx, cy - 1000 / M_PER_DEG_LAT, 300)         # ~1000 m south: outside 900 m
    outside = demand("out", cx, LAT0 + 0.5, 999)                       # outside zone: ignored
    existing = station("st", cx, cy)
    cfg = DecisionConfig(target_count=1, grid_step_m=250, service_radius_m=500, min_existing_distance_m=0)
    r = plan_locations(zone_a, [existing, near_station, far_a, outside], DecisionConfig(
        target_count=1, grid_step_m=250, service_radius_m=500, min_existing_distance_m=0))
    assert r["total_population"] == 400 and r["demand_point_count"] == 2
    assert r["existing_population_covered"] == 100          # 200 m <= 500 m
    assert r["new_incremental_population_covered"] == 300   # a site near far_a picks it up
    assert r["final_total_population_covered"] == 400
    assert r["final_total_coverage_pct"] == pytest.approx(100.0)
    assert r["status"] == "SUCCESS" and cfg.target_count == 1


def test_hard_constraints_hold(zone_a):
    cx, cy = LON0 + SIDE_DEG / 2, LAT0 + SIDE_DEG / 2
    existing = [station("st", cx, cy)]
    pts = [demand(str(i), LON0 + 0.002 + 0.0016 * (i % 10), LAT0 + 0.002 + 0.0035 * (i // 10), 50 + i) for i in range(30)]
    cfg = DecisionConfig(target_count=4, grid_step_m=200, service_radius_m=400,
                         min_existing_distance_m=300, min_new_site_distance_m=500)
    r = plan_locations(zone_a, existing + pts, cfg)
    assert r["selected"]
    for s in r["selected"]:
        assert geodesic_distance_m(s["lon"], s["lat"], cx, cy) >= 299  # allow projection tolerance
    sel = r["selected"]
    for i in range(len(sel)):
        for j in range(i + 1, len(sel)):
            assert geodesic_distance_m(sel[i]["lon"], sel[i]["lat"], sel[j]["lon"], sel[j]["lat"]) >= 499
    assert all(0 <= c["score"] <= 1 for c in r["candidates"])


def test_partial_status_is_reported_without_infeasibility_claim(zone_a):
    pts = [demand("1", LON0 + 0.01, LAT0 + 0.01, 100)]
    cfg = DecisionConfig(target_count=50, grid_step_m=500, service_radius_m=500, min_new_site_distance_m=5000)
    r = plan_locations(zone_a, pts, cfg)
    assert r["status"] == "PARTIAL" and r["actual_count"] == 1
    assert r["reason_code"] == "GREEDY_EXHAUSTED_COMPATIBLE_CANDIDATES"
    assert "no infeasibility claim" in r["message"]


def test_failed_when_existing_distance_excludes_everything(zone_a):
    cx, cy = LON0 + SIDE_DEG / 2, LAT0 + SIDE_DEG / 2
    cfg = DecisionConfig(target_count=1, min_existing_distance_m=50_000)
    r = plan_locations(zone_a, [station("st", cx, cy), demand("1", cx, cy, 10)], cfg)
    assert (r["status"], r["reason_code"]) == ("FAILED", "NO_CANDIDATE_PASSES_EXISTING_DISTANCE")


def test_zero_population_gives_no_percentages(zone_a):
    r = plan_locations(zone_a, [], DecisionConfig(target_count=1))
    assert r["final_total_coverage_pct"] is None and r["total_population"] == 0


def test_invalid_inputs(zone_a):
    with pytest.raises(ValueError, match="numeric 'population'"):
        plan_locations(zone_a, [Entity(source="d", source_id="1", lon=LON0 + .01, lat=LAT0 + .01, category="population")])
    with pytest.raises(ValueError, match="non-negative"):
        plan_locations(zone_a, [demand("1", LON0 + .01, LAT0 + .01, -5)])
    with pytest.raises(ValueError, match="missing coordinates"):
        plan_locations(zone_a, [Entity(source="d", source_id="1", category="population")])
    with pytest.raises(ValueError, match="duplicate"):
        plan_locations(zone_a, [station("1", LON0, LAT0), station("1", LON0, LAT0)])
    for kwargs in ({"target_count": 0}, {"target_count": True}, {"grid_step_m": 10}, {"service_radius_m": float("nan")},
                   {"min_existing_distance_m": -1}, {"weights": {"population": -1}}, {"weights": {"population": 0}},
                   {"weights": {"mystery": 1}}):
        with pytest.raises(ValueError):
            DecisionConfig(**kwargs)


def test_oversized_zone_is_rejected():
    huge = square("H", lon0=-3.0, lat0=50.0, side=3.0)
    with pytest.raises(ValueError, match="too large"):
        plan_locations(huge, [])

# --- regressions from independent review ---

def test_weight_sum_overflow_is_rejected():
    with pytest.raises(ValueError, match="finite, positive sum"):
        DecisionConfig(weights={"population": 1e308, "residential": 1e308})


def test_large_but_finite_weights_give_finite_scores(zone_a):
    cx, cy = LON0 + 0.01, LAT0 + 0.01
    entities = [demand("1", cx, cy, 100),
                Entity(source="r", source_id="1", lon=cx, lat=cy, category="residential")]
    cfg = DecisionConfig(target_count=2, weights={"population": 1e300, "residential": 1e300})
    r = plan_locations(zone_a, entities, cfg)
    assert r["candidates"] and all(0 <= c["score"] <= 1 for c in r["candidates"])
    assert all(math.isfinite(c["score"]) for c in r["candidates"])
    json.dumps(r, allow_nan=False)


def test_exports_reject_non_finite_numbers():
    from geopilot.reporting import to_csv, to_json
    with pytest.raises(ValueError):
        to_json({"x": float("nan")})
    with pytest.raises(ValueError):
        to_json({"x": float("inf")})
    with pytest.raises(ValueError):
        to_csv([{"x": float("nan")}])
