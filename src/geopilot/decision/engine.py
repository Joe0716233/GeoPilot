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

``plan_locations`` is the London-sample entry point and only understands the categories in
``LEGACY_CATEGORY_ROLES``; any other category (including blank) is refused with
``UNMAPPED_ROLE``. Arbitrary data goes through ``geopilot.decision.coverage.plan_coverage``
with an explicit role mapping. Both share ``_screen`` below. Refusals raise
``DecisionInputError`` (a ``ValueError``) with a stable ``code``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from itertools import islice
from math import floor, hypot, isfinite

import shapely
from shapely.geometry import Point
from shapely.prepared import prep

from geopilot.data.schema import Entity, Zone
from geopilot.tools.spatial import MAX_LOCAL_EXTENT_M, check_local_extent, local_projection

METRICS = ("population", "residential", "commercial", "transit")
COVERAGE_BONUS = 0.35
# Upper bound for every distance parameter: well beyond any distance inside one trusted local
# projection (<= MAX_LOCAL_EXTENT_M from its centre), and small enough that squaring cannot overflow.
MAX_DISTANCE_M = 500_000.0
LIMITATIONS = (
    "Straight-line (Euclidean) distances in a local metric projection; not travel time or network access.",
    "Demand and context points are whatever the caller supplies; the bundled sample is synthetic.",
    "No grid capacity, land ownership, parcel, planning or cost constraints are modelled.",
    "Greedy selection is a screening heuristic; it makes no optimality claim and an unfilled "
    "target is not proof that no solution exists.",
)
# Categories plan_locations accepts without an explicit mapping, and the role each plays.
LEGACY_CATEGORY_ROLES = {"existing_station": "existing_facility", "population": "weighted_demand",
                         "residential": "context", "commercial": "context", "transit": "context"}
LEGACY_CONTEXT_METRICS = ("residential", "commercial", "transit")

ERROR_CODES = ("UNMAPPED_ROLE", "INVALID_DEMAND_WEIGHT", "INVALID_STUDY_AREA", "INVALID_SCENARIO_CONFIG",
               "RESOURCE_LIMIT_EXCEEDED", "INVALID_ENTITY", "NO_DEMAND")


class DecisionInputError(ValueError):
    """Refused decision input. ``code`` is one of ``ERROR_CODES``; ``detail`` says what to fix."""

    def __init__(self, code: str, detail: str, **context):
        if code not in ERROR_CODES:
            raise AssertionError(f"unknown decision error code {code!r}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.context = context

    def __reduce__(self):
        return _rebuild_error, (self.code, self.detail, self.context)


def _rebuild_error(code: str, detail: str, context: dict) -> DecisionInputError:
    return DecisionInputError(code, detail, **context)


class FrozenMap(Mapping):
    """Read-only, hashable, picklable copy of a mapping (used for config dict fields)."""

    __slots__ = ("_data",)

    def __init__(self, data=()):
        self._data = dict(data)

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __hash__(self) -> int:
        return hash(frozenset(self._data.items()))

    def __repr__(self) -> str:
        return f"FrozenMap({self._data!r})"

    def __reduce__(self):
        return FrozenMap, (dict(self._data),)


@dataclass(frozen=True)
class ResourceLimits:
    """Work bounds checked before the expensive step they guard. Values may only be lowered."""

    max_entities: int = 100_000
    max_grid_cells: int = 200_000
    max_target_count: int = 500
    max_pair_evaluations: int = 25_000_000
    max_greedy_evaluations: int = 25_000_000
    max_zone_vertices: int = 200_000

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= f.default:
                raise DecisionInputError("INVALID_SCENARIO_CONFIG",
                                         f"{f.name} must be an integer between 1 and {f.default}")


DEFAULT_LIMITS = ResourceLimits()


def _bound(what: str, amount: int, limit: int, hint: str) -> None:
    if amount > limit:
        raise DecisionInputError("RESOURCE_LIMIT_EXCEEDED", f"{what} = {amount} exceeds the limit {limit}; {hint}",
                                 quantity=what, amount=amount, limit=limit)


def _count(config_value, name: str, minimum: float, maximum: float | None = None) -> None:
    try:
        finite = isfinite(config_value) if isinstance(config_value, (int, float)) else False
    except OverflowError:
        finite = False
    if isinstance(config_value, bool) or not finite:
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", f"{name} must be a finite number")
    if config_value < minimum:
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", f"{name} must be >= {minimum}")
    if maximum is not None and config_value > maximum:
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", f"{name} must be <= {maximum:g} m")


def _check_screening(cfg, limits: ResourceLimits) -> None:
    """Shared validation of the numeric screening parameters of DecisionConfig/ScenarioConfig."""
    if isinstance(cfg.target_count, bool) or not isinstance(cfg.target_count, int) or cfg.target_count < 1:
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", "target_count must be an integer >= 1")
    _bound("target_count", cfg.target_count, limits.max_target_count, "request fewer sites")
    _count(cfg.grid_step_m, "grid_step_m", 50, MAX_DISTANCE_M)
    _count(cfg.service_radius_m, "service_radius_m", 50, MAX_DISTANCE_M)
    _count(cfg.min_existing_distance_m, "min_existing_distance_m", 0, MAX_DISTANCE_M)
    _count(cfg.min_new_site_distance_m, "min_new_site_distance_m", 0, MAX_DISTANCE_M)


def _pct(value: float, total: float) -> float | None:
    """100 * value / total without overflowing when both are huge but finite."""
    if not total:
        return None
    scaled = 100 * value
    return scaled / total if isfinite(scaled) else 100 * (value / total)


def _check_weights(weights, allowed) -> FrozenMap:
    if not isinstance(weights, Mapping):
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", "weights must be a mapping metric -> weight")
    if set(weights) - set(allowed):
        raise DecisionInputError("INVALID_SCENARIO_CONFIG",
                                 "unsupported metric in weights; allowed: " + ", ".join(allowed))
    for value in weights.values():
        _count(value, "weight", 0)
    total = sum(weights.values())
    if not isfinite(total) or total <= 0:
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", "weights must have a finite, positive sum")
    return FrozenMap(weights)


@dataclass(frozen=True)
class DecisionConfig:
    target_count: int = 3
    grid_step_m: float = 500
    service_radius_m: float = 900
    min_existing_distance_m: float = 700
    min_new_site_distance_m: float = 600
    weights: Mapping[str, float] = field(default_factory=lambda: {
        "population": .45, "residential": .20, "commercial": .20, "transit": .15})

    def __post_init__(self):
        _check_screening(self, DEFAULT_LIMITS)
        # Read-only copy: later edits to the caller's dict cannot change a validated config.
        object.__setattr__(self, "weights", _check_weights(self.weights, METRICS))


def _study_area(zone: Zone, limits: ResourceLimits = DEFAULT_LIMITS):
    """Local projection and projected polygon of the zone; refusals carry INVALID_STUDY_AREA."""
    if not isinstance(zone, Zone):
        raise DecisionInputError("INVALID_STUDY_AREA", "the study area must be a geopilot Zone")
    _bound("study area vertices", int(shapely.get_num_coordinates(zone.shape)), limits.max_zone_vertices,
           "simplify the study-area polygon")
    centre = zone.shape.centroid
    try:
        local = local_projection(centre.x, centre.y)
        check_local_extent(zone.shape, local)
        polygon = local.project_geometry(zone.shape)
    except DecisionInputError:
        raise
    except ValueError as exc:
        raise DecisionInputError("INVALID_STUDY_AREA", str(exc), zone_id=zone.zone_id) from None
    if polygon.is_empty or not all(isfinite(v) for v in polygon.bounds):
        raise DecisionInputError("INVALID_STUDY_AREA", f"zone {zone.zone_id} cannot be projected to local metres",
                                 zone_id=zone.zone_id)
    return local, polygon


def generate_candidates(zone: Zone, step_m: float, *, limits: ResourceLimits = DEFAULT_LIMITS) -> list[dict]:
    """Grid-cell centres inside (or on the boundary of) the zone, in projected metres.

    The grid size is checked against ``limits.max_grid_cells`` before any cell is generated.
    """
    if not isinstance(limits, ResourceLimits):
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", "limits must be a ResourceLimits")
    try:  # a huge Python int is not representable as a float and must not overflow below
        finite = isinstance(step_m, (int, float)) and not isinstance(step_m, bool) and isfinite(float(step_m))
    except OverflowError:
        finite = False
    if not finite or step_m <= 0:
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", "grid step must be a finite, representable number > 0")
    local, polygon = _study_area(zone, limits)
    minx, miny, maxx, maxy = polygon.bounds
    spans = ((maxx - minx) / step_m, (maxy - miny) / step_m)
    if not all(isfinite(s) for s in spans):  # e.g. a subnormal step: the grid would be infinitely large
        raise DecisionInputError("RESOURCE_LIMIT_EXCEEDED", f"grid step {step_m!r} m gives an unbounded number of "
                                 "grid cells; increase grid_step_m", quantity="candidate grid cells",
                                 amount=None, limit=limits.max_grid_cells)
    cols = int(floor(spans[0])) + 1
    rows = int(floor(spans[1])) + 1
    _bound("candidate grid cells", cols * rows, limits.max_grid_cells,
           "increase grid_step_m or use a smaller study area")
    inside = prep(polygon)
    candidates = []
    for i in range(cols):
        x = minx + step_m / 2 + i * step_m
        for j in range(rows):
            y = miny + step_m / 2 + j * step_m
            if inside.covers(Point(x, y)):
                lon, lat = local.unproject(x, y)
                candidates.append({"id": f"C{len(candidates) + 1:03d}", "lon": lon, "lat": lat, "x": x, "y": y})
    return candidates


def _screen(zone: Zone, existing: list, demand: list, context: list, context_metrics: tuple, cfg,
            weights: Mapping[str, float], limits: ResourceLimits, *, demand_metric: str, ids_key: str,
            incremental_key: str) -> dict:
    """The screening algorithm, independent of input categories.

    ``existing``: [(x, y)]; ``demand``: [(key, x, y, weight)] already restricted to the zone;
    ``context``: [(metric, x, y)]. Every metric named in ``weights`` must be ``demand_metric``
    or appear in ``context_metrics``.
    """
    r2 = float(cfg.service_radius_m) ** 2
    weight_of = {key: w for key, _, _, w in demand}
    total = sum(weight_of.values())
    _bound("demand x existing pairs", len(demand) * len(existing), limits.max_pair_evaluations,
           "reduce the number of demand points or existing facilities")
    existing_ids = {key for key, x, y, _ in demand
                    if any((x - ex) ** 2 + (y - ey) ** 2 <= r2 for ex, ey in existing)}
    candidates = generate_candidates(zone, cfg.grid_step_m, limits=limits)
    _bound("candidate x point pairs", len(candidates) * (len(existing) + len(demand) + len(context)),
           limits.max_pair_evaluations, "increase grid_step_m or reduce the number of input points")
    valid = []
    for c in candidates:
        x, y = c["x"], c["y"]
        nearest = min((hypot(x - ex, y - ey) for ex, ey in existing), default=None)
        if nearest is not None and nearest < cfg.min_existing_distance_m:
            continue
        covered = [key for key, ex, ey, _ in demand if (x - ex) ** 2 + (y - ey) ** 2 <= r2]
        metrics = {demand_metric: sum(weight_of[k] for k in covered), **dict.fromkeys(context_metrics, 0)}
        for metric, ex, ey in context:
            if (x - ex) ** 2 + (y - ey) ** 2 <= r2:
                metrics[metric] += 1
        valid.append({**c, "metrics": metrics, ids_key: covered,
                      "nearest_existing_m": round(nearest, 2) if nearest is not None else None})
    maxima = {k: max((c["metrics"][k] for c in valid), default=0) for k in weights}
    weight_total = sum(weights.values())
    for c in valid:
        comps = {k: (c["metrics"][k] / maxima[k] if maxima[k] else 0.0) for k in weights}
        c["components"] = {k: round(v, 4) for k, v in comps.items()}
        c["score"] = round(sum(weights[k] * comps[k] for k in weights) / weight_total, 4)
    valid.sort(key=lambda c: (-c["score"], c["id"]))
    _bound("greedy evaluations", cfg.target_count * (len(valid) + sum(len(c[ids_key]) for c in valid)),
           limits.max_greedy_evaluations, "request fewer sites or increase grid_step_m")

    def marginal(c: dict, covered_ids: set[str]) -> float:
        new = sum(weight_of[i] for i in c[ids_key] if i not in covered_ids)
        return c["score"] + COVERAGE_BONUS * (new / total if total else 0.0)

    selected, newly_lists, covered_ids, remaining = [], [], set(existing_ids), list(valid)
    while remaining and len(selected) < cfg.target_count:
        best = max(remaining, key=lambda c: (marginal(c, covered_ids), -int(c["id"][1:])))
        newly = sorted(set(best[ids_key]) - covered_ids)
        selected.append({**best, "rank": len(selected) + 1,
                         incremental_key: sum(weight_of[i] for i in newly),
                         "selection_value": round(marginal(best, covered_ids), 4)})
        newly_lists.append(newly)
        covered_ids |= set(best[ids_key])
        remaining = [c for c in remaining if c is not best and
                     (c["x"] - best["x"]) ** 2 + (c["y"] - best["y"]) ** 2 >= cfg.min_new_site_distance_m ** 2]
    status = "SUCCESS" if len(selected) == cfg.target_count else "PARTIAL" if selected else "FAILED"
    reason = None
    if status != "SUCCESS":
        reason = ("NO_GRID_CANDIDATES" if not candidates else
                  "NO_CANDIDATE_PASSES_EXISTING_DISTANCE" if not valid else "GREEDY_EXHAUSTED_COMPATIBLE_CANDIDATES")
    return {"status": status, "reason": reason, "candidates": candidates, "valid": valid, "selected": selected,
            "newly": newly_lists, "total": total, "existing_ids": existing_ids, "covered_ids": covered_ids,
            "existing_cov": sum(weight_of[i] for i in existing_ids),
            "incremental": sum(weight_of[i] for i in covered_ids - existing_ids),
            "message": (f"Requested {cfg.target_count}; selected {len(selected)}. " +
                        ("All requested sites selected." if status == "SUCCESS" else
                         "Hard distance constraints were preserved; no infeasibility claim is made."))}


def _population(entity: Entity) -> float:
    try:
        if isinstance(entity.properties["population"], bool):
            raise TypeError("boolean population")
        value = float(entity.properties["population"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise DecisionInputError("INVALID_DEMAND_WEIGHT",
                                 f"population demand point {entity.key} needs a numeric 'population' property",
                                 keys=[entity.key]) from None
    if not isfinite(value) or value < 0:
        raise DecisionInputError("INVALID_DEMAND_WEIGHT", f"population of {entity.key} must be finite and non-negative",
                                 keys=[entity.key])
    return value


def _check_entities(entities: list, limits: ResourceLimits) -> None:
    """Bounded count, Entity type and unique keys over every record, including ignored ones."""
    _bound("input entities", len(entities), limits.max_entities, "split the input or pre-filter it")
    seen: set[str] = set()
    for entity in entities:
        if not isinstance(entity, Entity):
            raise DecisionInputError("INVALID_ENTITY", f"inputs must be geopilot Entity records, got {type(entity).__name__}")
        if entity.key in seen:
            raise DecisionInputError("INVALID_ENTITY", f"duplicate entity key: {entity.key}", keys=[entity.key])
        seen.add(entity.key)


def unmapped_error(counts: Counter, allowed: str) -> DecisionInputError:
    listed = ", ".join(f"{c!r} ({n} records)" for c, n in sorted(counts.items()))
    return DecisionInputError(
        "UNMAPPED_ROLE", f"no decision role for categor{'y' if len(counts) == 1 else 'ies'} {listed}. {allowed}",
        categories=sorted(counts))


def plan_locations(zone: Zone, entities: list[Entity], config: DecisionConfig = DecisionConfig(), *,
                   limits: ResourceLimits | None = None) -> dict:
    """Rank grid candidates and greedily select sites for new population coverage.

    Only the categories in ``LEGACY_CATEGORY_ROLES`` are accepted; use ``plan_coverage`` with a
    ``RoleMapping`` for any other data. ``limits`` (default ``DEFAULT_LIMITS``) can only lower the
    work bounds; ``config.target_count`` must also respect it.
    """
    limits = DEFAULT_LIMITS if limits is None else limits
    if not isinstance(limits, ResourceLimits):
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", "limits must be a ResourceLimits")
    _bound("target_count", config.target_count, limits.max_target_count, "request fewer sites")
    local, _ = _study_area(zone, limits)
    entities = list(islice(entities, limits.max_entities + 1))  # never materialise more than the bound
    _check_entities(entities, limits)
    unknown = Counter(e.category for e in entities if e.category not in LEGACY_CATEGORY_ROLES)
    if unknown:
        raise unmapped_error(unknown, "plan_locations only accepts " + ", ".join(LEGACY_CATEGORY_ROLES) +
                             "; call plan_coverage with a RoleMapping (existing_facility, weighted_demand, "
                             "context, ignore) to use other categories.")
    projected = []
    for entity in entities:
        if not entity.has_point:
            raise DecisionInputError("INVALID_ENTITY", f"missing coordinates: {entity.key}", keys=[entity.key])
        x, y = local.project(entity.lon, entity.lat)
        projected.append((entity, x, y))
    existing = [(x, y) for e, x, y in projected if e.category == "existing_station"]
    demand = [(e.key, x, y, _population(e)) for e, x, y in projected
              if e.category == "population" and zone.shape.covers(Point(e.lon, e.lat))]
    context = [(e.category, x, y) for e, x, y in projected if e.category in LEGACY_CONTEXT_METRICS]
    if not isfinite(sum(w for *_, w in demand)):
        raise DecisionInputError("INVALID_DEMAND_WEIGHT", "total population must be finite")
    run = _screen(zone, existing, demand, context, LEGACY_CONTEXT_METRICS, config, config.weights, limits,
                  demand_metric="population", ids_key="covered_population_ids",
                  incremental_key="incremental_population")
    total, existing_cov, incremental = run["total"], run["existing_cov"], run["incremental"]
    pct = (lambda v: _pct(v, total))  # noqa: E731
    return {
        "status": run["status"], "reason_code": run["reason"],
        "requested_count": config.target_count, "actual_count": len(run["selected"]),
        "message": run["message"],
        "generated_candidate_count": len(run["candidates"]), "feasible_candidate_count": len(run["valid"]),
        "candidates": run["valid"], "selected": run["selected"],
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
