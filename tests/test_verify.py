import pytest

from geopilot.data import Entity
from geopilot.tools import geodesic_distance_m
from geopilot.verify import VerifyConfig, summarise, verify_batch, verify_entity

from conftest import LAT0, LON0, SIDE_DEG, point, square

MID_LON = LON0 + SIDE_DEG / 2


def lat_at(metres_from_south_edge: float) -> float:
    # One metre of latitude, measured geodesically at the edge.
    return LAT0 + metres_from_south_edge / geodesic_distance_m(LON0, LAT0, LON0, LAT0 + 1)


def test_pass_review_fail_thresholds(zone_a):
    cfg = VerifyConfig(review_boundary_m=20, fail_outside_m=50)
    zones = [zone_a]
    cases = [(100, "PASS", "CLAIM_CONFIRMED"), (10, "REVIEW", "INSIDE_NEAR_BOUNDARY"),
             (-30, "REVIEW", "OUTSIDE_NEAR_BOUNDARY"), (-80, "FAIL", "OUTSIDE_ALL_SUPPLIED_ZONES")]
    for offset, status, reason in cases:
        r = verify_entity(point("p", MID_LON, lat_at(offset)), "A", zones, cfg)
        assert (r["status"], r["reason_code"]) == (status, reason), offset
        assert abs(r["signed_distance_m"]) == pytest.approx(abs(offset), abs=0.5)
        assert (r["signed_distance_m"] > 0) == (offset > 0)


def test_boundary_point_is_review_not_pass(zone_a):
    r = verify_entity(point("p", MID_LON, LAT0), "A", [zone_a])
    assert r["status"] == "REVIEW" and r["boundary_distance_m"] == pytest.approx(0, abs=0.01)


def test_threshold_edges_are_inclusive_for_review(zone_a):
    r = verify_entity(point("p", MID_LON, lat_at(20.0)), "A", [zone_a], VerifyConfig(20.0 + 0.2, 50))
    assert r["status"] == "REVIEW"
    r = verify_entity(point("p", MID_LON, lat_at(21.0)), "A", [zone_a], VerifyConfig(20.0, 50))
    assert r["status"] == "PASS"


def test_contradicted_when_inside_another_zone(zone_a):
    zone_b = square("B", lon0=LON0 + SIDE_DEG + 0.01)
    inside_b = point("p", zone_b.shape.centroid.x, zone_b.shape.centroid.y)
    r = verify_entity(inside_b, "A", [zone_a, zone_b])
    assert (r["status"], r["reason_code"], r["actual_zones"]) == ("FAIL", "CLAIM_CONTRADICTED", ["B"])


def test_overlapping_zones_need_review(zone_a):
    overlap = square("B", lon0=LON0 + 0.005, lat0=LAT0 + 0.005)
    r = verify_entity(point("p", MID_LON, LAT0 + 0.01), "A", [zone_a, overlap])
    assert (r["status"], r["reason_code"]) == ("REVIEW", "OVERLAPPING_ZONES")


def test_unresolved_cases(zone_a):
    assert verify_entity(Entity(source="t", source_id="x"), "A", [zone_a])["reason_code"] == "NO_COORDINATE"
    assert verify_entity(point("p", MID_LON, LAT0 + 0.01), "", [zone_a])["reason_code"] == "NO_CLAIM"
    r = verify_entity(point("p", MID_LON, LAT0 + 0.01), "Nope", [zone_a])
    assert (r["status"], r["reason_code"]) == ("UNRESOLVED", "UNKNOWN_CLAIMED_ZONE")
    assert r["boundary_distance_m"] is None


def test_config_validation():
    for bad in (-1, float("nan"), float("inf"), True, "5"):
        with pytest.raises(ValueError):
            VerifyConfig(review_boundary_m=bad)
    with pytest.raises(ValueError):
        VerifyConfig(fail_outside_m=-5)


def test_batch_and_summary(zone_a):
    entities = [point("1", MID_LON, lat_at(100)), point("2", MID_LON, lat_at(-500)), Entity(source="t", source_id="3")]
    claims = {"t:1": "A", "t:2": "A"}
    results = verify_batch(entities, claims, [zone_a])
    s = summarise(results)
    assert s["status_counts"] == {"PASS": 1, "REVIEW": 0, "FAIL": 1, "UNRESOLVED": 1} and s["records"] == 3
    with pytest.raises(ValueError, match="unique"):
        verify_batch(entities, claims, [zone_a, zone_a])
