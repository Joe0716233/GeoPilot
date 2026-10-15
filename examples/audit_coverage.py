"""Audit bundled coverage examples independently of the GIS engine's calculations.

Run offline from the checkout: python examples/audit_coverage.py
Uses direct PyProj transformations for the full grid and WGS84 Geod distances for
coverage, scores, eligibility, constraints and greedy selection. No screening
internals or GeoPilot spatial helpers are imported. This checks arithmetic and
source preservation, not real facility availability or observed demand.
"""
from __future__ import annotations

import json
from collections import Counter
from math import floor

import numpy as np
import shapely
from pyproj import Geod, Transformer
from shapely.geometry import Point

from geopilot.coverage_examples import load_coverage_inputs, run_coverage_example

GEOD = Geod(ellps="WGS84")


def distance(left: dict, right: dict) -> float:
    return GEOD.inv(left["lon"], left["lat"], right["lon"], right["lat"])[2]


def independent_grid(zone) -> list[dict]:
    centre = zone.shape.centroid
    forward = Transformer.from_crs(
        "EPSG:4326",
        f"+proj=aeqd +lat_0={centre.y!r} +lon_0={centre.x!r} +datum=WGS84 +units=m +no_defs",
        always_xy=True,
    )
    polygon = shapely.transform(
        zone.shape,
        lambda xy: np.column_stack(forward.transform(xy[:, 0], xy[:, 1])),
    )
    inverse = Transformer.from_crs(forward.target_crs, "EPSG:4326", always_xy=True)
    minx, miny, maxx, maxy = polygon.bounds
    cells = []
    for i in range(floor((maxx - minx) / 500) + 1):
        for j in range(floor((maxy - miny) / 500) + 1):
            x, y = minx + 250 + i * 500, miny + 250 + j * 500
            if polygon.covers(Point(x, y)):
                lon, lat = inverse.transform(x, y)
                cells.append({"id": f"C{len(cells) + 1:03d}", "lon": lon, "lat": lat})
    return cells


def audit(data, key: str, category: str, *, conservative_access: bool = False) -> tuple[dict, dict]:
    # Read eligibility directly from the unchanged source tags, not the filter helper.
    facilities = [
        e for e in data.facilities
        if e.category == category and (
            (category != "toilets" and not conservative_access)
            or e.properties["raw_tags"].get("access") in {"yes", "permissive"}
        )
    ]
    result = run_coverage_example(key, inputs=data, conservative_access=conservative_access)
    for parameter, expected in {
        "grid_step_m": 500, "service_radius_m": 500, "target_count": 3,
        "min_existing_distance_m": 250, "min_new_site_distance_m": 600,
    }.items():
        assert result["config"][parameter] == expected
    assert result["config"]["weights"] == {
        "demand_weight": 1.0, "residential": 0.0, "commercial": 0.0, "transit": 0.0,
    }
    assert set(result["existing_facility_ids"]) == {e.key for e in facilities}
    assert result["existing_facility_count"] == len(facilities)
    grid = independent_grid(data.zone)
    existing = [e.model_dump() for e in facilities]
    feasible = [c for c in grid if all(distance(c, e) >= 250 for e in existing)]
    assert len(grid) == result["generated_candidate_count"]
    assert len(feasible) == result["feasible_candidate_count"]
    assert {c["id"] for c in feasible} == {c["id"] for c in result["candidates"]}
    demand = [
        e.model_dump() for e in data.demand_context
        if e.category == "population" and data.zone.shape.covers(Point(e.lon, e.lat))
    ]
    weight = {f"{e['source']}:{e['source_id']}": float(e["properties"]["population"]) for e in demand}
    assert len(demand) == result["demand_point_count"] == 140
    assert sum(weight.values()) == result["total_demand_weight"]

    def covered(sites: list[dict]) -> set[str]:
        return {
            f"{e['source']}:{e['source_id']}" for e in demand
            if any(distance(e, site) <= 500 for site in sites)
        }

    baseline = covered(existing)
    final = covered(existing + result["selected"])
    assert sum(weight[k] for k in baseline) == result["existing_demand_weight_covered"]
    assert sum(weight[k] for k in final - baseline) == result["new_incremental_demand_weight_covered"]
    assert sum(weight[k] for k in final) == result["final_total_demand_weight_covered"]
    assert set(result["existing_covered_demand_ids"]) == baseline
    assert set(result["final_covered_demand_ids"]) == final
    originals = {e.key: e for e in facilities + list(data.demand_context)}
    for row in result["inputs"]:
        original = originals[row["key"]]
        assert row["category"] == original.category and row["source_id"] == original.source_id
        assert row["properties"] == original.properties
        assert row["provenance"] == [p.model_dump() for p in original.provenance]
    minimum_margin = float("inf")
    candidates = result["candidates"]
    for candidate in candidates:
        ids = covered([candidate])
        assert set(candidate["covered_demand_ids"]) == ids
        assert candidate["metrics"]["demand_weight"] == sum(weight[k] for k in ids)
        for metric in ("residential", "commercial", "transit"):
            assert candidate["metrics"][metric] == sum(
                1 for e in data.demand_context
                if e.category == metric and distance(candidate, e.model_dump()) <= 500
            )
        minimum_margin = min(minimum_margin, min(abs(distance(candidate, d) - 500) for d in demand))
    previous = set(baseline)
    for i, site in enumerate(result["selected"]):
        assert data.zone.shape.covers(Point(site["lon"], site["lat"]))
        assert all(distance(site, e) >= 250 - 0.1 for e in existing)
        assert all(distance(site, e) >= 600 - 0.1 for e in result["selected"][:i])
        newly = covered([site]) - previous
        assert set(site["newly_covered_demand_ids"]) == newly
        assert site["incremental_demand_weight"] == sum(weight[k] for k in newly)
        previous |= covered([site])
    maximum = max(sum(weight[k] for k in covered([c])) for c in candidates)
    scores = {c["id"]: round(sum(weight[k] for k in covered([c])) / maximum, 4) for c in candidates}
    assert all(c["score"] == scores[c["id"]] for c in candidates)
    remaining, chosen, already = list(candidates), [], set(baseline)
    while remaining and len(chosen) < 3:
        def marginal(candidate):
            return scores[candidate["id"]] + 0.35 * sum(
                weight[k] for k in covered([candidate]) - already
            ) / sum(weight.values())
        best = max(remaining, key=lambda c: (marginal(c), -int(c["id"][1:])))
        chosen.append(best["id"])
        already |= covered([best])
        remaining = [c for c in remaining if c["id"] != best["id"] and distance(c, best) >= 600]
    assert chosen == [c["id"] for c in result["selected"]]
    json.dumps(result, allow_nan=False)
    return {
        "status": "PASS", "facility_count": len(facilities), "demand_count": len(demand),
        "candidate_count": len(grid), "feasible_candidate_count": len(feasible), "selected": chosen,
        "weighted_coverage": [result[k] for k in (
            "existing_demand_weight_covered", "new_incremental_demand_weight_covered",
            "final_total_demand_weight_covered",
        )],
        "min_geodesic_threshold_margin_m": minimum_margin,
        "distance_method": "WGS84 Geod, independent of engine local Euclidean distances",
    }, result


def main() -> None:
    data = load_coverage_inputs()
    charging, default_ev = audit(data, "ev_charging", "charging_station")
    strict_charging, strict_ev = audit(data, "ev_charging", "charging_station", conservative_access=True)
    ev_tags = Counter(e.properties["raw_tags"].get("access", "UNKNOWN")
                      for e in data.facilities if e.category == "charging_station")
    assert ev_tags == {"yes": 5, "no": 2, "customers": 1, "taxi": 1, "UNKNOWN": 54}
    assert default_ev["eligibility"] is None and charging["facility_count"] == 63
    assert strict_charging["facility_count"] == 5
    assert strict_ev["eligibility"]["counts"]["EXCLUDED_RESTRICTED"] == 4
    assert strict_ev["eligibility"]["counts"]["EXCLUDED_UNKNOWN"] == 54
    assert default_ev["access_summary"]["known_restricted"] == 4
    assert default_ev["access_summary"]["unknown"] == 54
    toilets, result = audit(data, "public_toilets", "toilets")
    counts = Counter(
        e.properties["raw_tags"].get("access", "UNKNOWN")
        for e in data.facilities if e.category == "toilets"
    )
    assert sum(counts.values()) == 100
    assert counts["yes"] + counts["permissive"] == 28
    assert sum(counts[v] for v in ("private", "customers", "permit")) == 18
    assert counts["UNKNOWN"] == 54
    assert result["eligibility"]["counts"]["EXCLUDED_UNKNOWN"] == 54
    assert result["eligibility"]["counts"]["EXCLUDED_RESTRICTED"] == 18
    print(json.dumps({
        "charging": charging, "charging_conservative": strict_charging, "toilets": toilets,
        "charging_access": {"all": 63, "yes_tags": 5, "restricted": 4, "unknown": 54,
                            "current_public_availability": "NOT_ESTABLISHED"},
        "toilet_access": {"all": 100, "eligible": 28, "restricted": 18, "unknown": 54,
                          "hours_fee_wheelchair": "NOT_ESTABLISHED"},
    }, indent=2))


if __name__ == "__main__":
    main()
