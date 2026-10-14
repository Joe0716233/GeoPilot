"""GeoDecision: transparent coverage-based site screening on a metric grid.

Method (all distances are straight-line metres in a local azimuthal-equidistant projection
centred on the study zone; no travel time, road network, parcel, grid-connection, ownership
or planning-permission constraint is modelled):

1. Candidate sites are the centres of a regular ``grid_step_m`` grid that lie inside the zone.
2. Candidates closer than ``min_existing_distance_m`` to an existing station are dropped.
3. Each candidate gets min-max-free component scores: weighted metric / maximum over candidates
   for population within ``service_radius_m`` and counts of residential, commercial and transit
   points within the same radius, combined by normalised ``weights``.
4. Sites are chosen greedily by ``score + 0.35 * newly covered population share``; chosen sites
   exclude candidates closer than ``min_new_site_distance_m``.

The greedy heuristic is not an optimiser and carries no optimality or feasibility guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import floor, hypot, isfinite

from shapely.geometry import Point

from geopilot.data.schema import Entity, Zone
from geopilot.tools.spatial import check_local_extent, local_projection

METRICS = ("population", "residential", "commercial", "transit")
COVERAGE_BONUS = 0.35
LIMITATIONS = (
    "Straight-line (Euclidean) distances in a local metric projection; not travel time or network access.",
    "Demand and context points are whatever the caller supplies; the bundled sample is synthetic.",
    "No grid capacity, land ownership, parcel, planning or cost constraints are modelled.",
    "Greedy selection is a screening heuristic; it makes no optimality claim and an unfilled "
    "target is not proof that no solution exists.",
)


def _count(config_value, name: str, minimum: float) -> None:
    if isinstance(config_value, bool) or not isinstance(config_value, (int, float)) or not isfinite(config_value):
        raise ValueError(f"{name} must be a finite number")
    if config_value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")


@dataclass(frozen=True)
class DecisionConfig:
    target_count: int = 3
    grid_step_m: float = 500
    service_radius_m: float = 900
    min_existing_distance_m: float = 700
    min_new_site_distance_m: float = 600
    weights: dict[str, float] = field(default_factory=lambda: {
        "population": .45, "residential": .20, "commercial": .20, "transit": .15})

    def __post_init__(self):
        if isinstance(self.target_count, bool) or not isinstance(self.target_count, int) or self.target_count < 1:
            raise ValueError("target_count must be an integer >= 1")
        _count(self.grid_step_m, "grid_step_m", 50)
        _count(self.service_radius_m, "service_radius_m", 50)
        _count(self.min_existing_distance_m, "min_existing_distance_m", 0)
        _count(self.min_new_site_distance_m, "min_new_site_distance_m", 0)
        if set(self.weights) - set(METRICS):
            raise ValueError("unsupported metric in weights; allowed: " + ", ".join(METRICS))
        for value in self.weights.values():
            _count(value, "weight", 0)
        total = sum(self.weights.values())
        if not isfinite(total) or total <= 0:
            raise ValueError("weights must have a finite, positive sum")


def generate_candidates(zone: Zone, step_m: float) -> list[dict]:
    """Grid-cell centres inside (or on the boundary of) the zone, in projected metres."""
    local = local_projection(zone.shape.centroid.x, zone.shape.centroid.y)
    check_local_extent(zone.shape, local)
    polygon = local.project_geometry(zone.shape)
    minx, miny, maxx, maxy = polygon.bounds
    cols = int(floor((maxx - minx) / step_m)) + 1
    rows = int(floor((maxy - miny) / step_m)) + 1
    candidates = []
    for i in range(cols):
        x = minx + step_m / 2 + i * step_m
        for j in range(rows):
            y = miny + step_m / 2 + j * step_m
            if polygon.covers(Point(x, y)):
                lon, lat = local.unproject(x, y)
                candidates.append({"id": f"C{len(candidates) + 1:03d}", "lon": lon, "lat": lat, "x": x, "y": y})
    return candidates


def _population(entity: Entity) -> float:
    try:
        value = float(entity.properties["population"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"population demand point {entity.key} needs a numeric 'population' property") from None
    if not isfinite(value) or value < 0:
        raise ValueError(f"population of {entity.key} must be finite and non-negative")
    return value


def plan_locations(zone: Zone, entities: list[Entity], config: DecisionConfig = DecisionConfig()) -> dict:
    """Rank grid candidates and greedily select sites for new population coverage."""
    centre = zone.shape.centroid
    local = local_projection(centre.x, centre.y)
    check_local_extent(zone.shape, local)
    seen: set[str] = set()
    projected = []
    for entity in entities:
        if entity.key in seen:
            raise ValueError(f"duplicate entity key: {entity.key}")
        seen.add(entity.key)
        if not entity.has_point:
            raise ValueError(f"missing coordinates: {entity.key}")
        x, y = local.project(entity.lon, entity.lat)
        projected.append((entity, x, y))
    r2 = float(config.service_radius_m) ** 2
    existing = [(x, y) for e, x, y in projected if e.category == "existing_station"]
    demand = [(e, x, y) for e, x, y in projected
              if e.category == "population" and zone.shape.covers(Point(e.lon, e.lat))]
    pop = {e.key: _population(e) for e, _, _ in demand}
    total = sum(pop.values())
    if not isfinite(total):
        raise ValueError("total population must be finite")
    existing_ids = {e.key for e, x, y in demand
                    if any((x - ex) ** 2 + (y - ey) ** 2 <= r2 for ex, ey in existing)}
    candidates = generate_candidates(zone, config.grid_step_m)
    valid = []
    for c in candidates:
        x, y = c["x"], c["y"]
        nearest = min((hypot(x - ex, y - ey) for ex, ey in existing), default=None)
        if nearest is not None and nearest < config.min_existing_distance_m:
            continue
        covered = [e.key for e, ex, ey in demand if (x - ex) ** 2 + (y - ey) ** 2 <= r2]
        metrics = {"population": sum(pop[k] for k in covered)}
        for category in ("residential", "commercial", "transit"):
            metrics[category] = sum(1 for e, ex, ey in projected
                                    if e.category == category and (x - ex) ** 2 + (y - ey) ** 2 <= r2)
        valid.append({**c, "metrics": metrics, "covered_population_ids": covered,
                      "nearest_existing_m": round(nearest, 2) if nearest is not None else None})
    maxima = {k: max((c["metrics"][k] for c in valid), default=0) for k in config.weights}
    weight_total = sum(config.weights.values())
    for c in valid:
        comps = {k: (c["metrics"][k] / maxima[k] if maxima[k] else 0.0) for k in config.weights}
        c["components"] = {k: round(v, 4) for k, v in comps.items()}
        c["score"] = round(sum(config.weights[k] * comps[k] for k in config.weights) / weight_total, 4)
    valid.sort(key=lambda c: (-c["score"], c["id"]))

    def marginal(c: dict, covered_ids: set[str]) -> float:
        new = sum(pop[i] for i in c["covered_population_ids"] if i not in covered_ids)
        return c["score"] + COVERAGE_BONUS * (new / total if total else 0.0)

    selected, covered_ids, remaining = [], set(existing_ids), list(valid)
    while remaining and len(selected) < config.target_count:
        best = max(remaining, key=lambda c: (marginal(c, covered_ids), -int(c["id"][1:])))
        newly = set(best["covered_population_ids"]) - covered_ids
        selected.append({**best, "rank": len(selected) + 1,
                         "incremental_population": sum(pop[i] for i in sorted(newly)),
                         "selection_value": round(marginal(best, covered_ids), 4)})
        covered_ids |= set(best["covered_population_ids"])
        remaining = [c for c in remaining if c is not best and
                     (c["x"] - best["x"]) ** 2 + (c["y"] - best["y"]) ** 2 >= config.min_new_site_distance_m ** 2]
    existing_cov = sum(pop[i] for i in existing_ids)
    incremental = sum(pop[i] for i in covered_ids - existing_ids)
    pct = (lambda v: 100 * v / total if total else None)  # noqa: E731
    status = "SUCCESS" if len(selected) == config.target_count else "PARTIAL" if selected else "FAILED"
    reason = None
    if status != "SUCCESS":
        reason = ("NO_GRID_CANDIDATES" if not candidates else
                  "NO_CANDIDATE_PASSES_EXISTING_DISTANCE" if not valid else "GREEDY_EXHAUSTED_COMPATIBLE_CANDIDATES")
    return {
        "status": status, "reason_code": reason,
        "requested_count": config.target_count, "actual_count": len(selected),
        "message": (f"Requested {config.target_count}; selected {len(selected)}. " +
                    ("All requested sites selected." if status == "SUCCESS" else
                     "Hard distance constraints were preserved; no infeasibility claim is made.")),
        "generated_candidate_count": len(candidates), "feasible_candidate_count": len(valid),
        "candidates": valid, "selected": selected,
        "existing_station_count": len(existing), "demand_point_count": len(demand),
        "total_population": total,
        "existing_population_covered": existing_cov,
        "new_incremental_population_covered": incremental,
        "final_total_population_covered": existing_cov + incremental,
        "existing_coverage_pct": pct(existing_cov),
        "new_incremental_coverage_pct": pct(incremental),
        "final_total_coverage_pct": pct(existing_cov + incremental),
        "coverage_radius_m": config.service_radius_m, "distance_kind": "local_projected_euclidean",
        "coverage_boundary": "distance <= radius; demand inside or on the study boundary",
        "config": {"target_count": config.target_count, "grid_step_m": config.grid_step_m,
                   "service_radius_m": config.service_radius_m,
                   "min_existing_distance_m": config.min_existing_distance_m,
                   "min_new_site_distance_m": config.min_new_site_distance_m,
                   "weights": dict(config.weights)},
        "limitations": list(LIMITATIONS),
    }
