"""Generic coverage screening with explicit data roles.

``plan_coverage`` runs the same screening algorithm as ``plan_locations`` on arbitrary point
data. Every ``Entity.category`` in the input must be declared in a ``RoleMapping``:

- ``existing_facility``: already-built sites; they define baseline coverage and the
  ``min_existing_distance_m`` exclusion (inside or outside the study area).
- ``weighted_demand``: demand points whose weight is read from
  ``ScenarioConfig.demand_weight_field``; only points inside or on the study area are covered.
- ``context``: points counted within the service radius under a named metric
  (``RoleMapping.context_metrics``, default: the category) that ``weights`` can score.
- ``ignore``: excluded from the analysis but listed in the output audit.

Nothing is renamed: inputs keep their own categories, properties and provenance, and the output
reports ``demand_weight`` metrics plus a per-record audit. Refusals raise ``DecisionInputError``.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from math import isfinite
from numbers import Real

from shapely.geometry import Point
from shapely.prepared import prep

from geopilot.data.schema import Entity, Zone

from geopilot.tools.spatial import MAX_LOCAL_EXTENT_M

from .engine import (DEFAULT_LIMITS, LEGACY_CATEGORY_ROLES, LIMITATIONS, DecisionInputError, FrozenMap,
                     ResourceLimits, _check_entities, _check_screening, _check_weights, _pct, _screen, _study_area,
                     unmapped_error)

DEMAND_METRIC = "demand_weight"
# The only decision method implemented. Anything else (routing, travel time, capacity, cost or
# equity optimisation, p-median, ...) is refused rather than approximated by this one.
SUPPORTED_METHODS = ("weighted_coverage",)
METHOD_BOUNDARY = ("weighted_coverage: greedy screening of regular-grid candidate sites for straight-line "
                   "coverage of weighted demand points within one service radius, scored with optional "
                   "context-point counts. Not routing, travel time, capacity, cost or equity optimisation.")
_METRIC_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


class Role(StrEnum):
    EXISTING_FACILITY = "existing_facility"
    WEIGHTED_DEMAND = "weighted_demand"
    CONTEXT = "context"
    IGNORE = "ignore"


def _config_error(detail: str, **context) -> DecisionInputError:
    return DecisionInputError("INVALID_SCENARIO_CONFIG", detail, **context)


def _listing(items: list[str], limit: int = 10) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


@dataclass(frozen=True)
class RoleMapping:
    """Role of each input category (exact ``Entity.category`` match); unlisted categories are refused.

    ``context_metrics`` names the metric each ``context`` category is counted under; categories
    sharing a metric are counted together. Names are lowercase identifiers other than
    ``demand_weight``. Both mappings are stored as read-only copies.
    """

    roles: Mapping[str, Role | str]
    context_metrics: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.roles, Mapping) or not self.roles:
            raise _config_error("roles must be a non-empty mapping of category -> role")
        roles: dict[str, Role] = {}
        for category, role in self.roles.items():
            if not isinstance(category, str) or not category.strip():
                raise _config_error("role mapping categories must be non-blank strings")
            try:
                roles[category] = Role(role.lower() if isinstance(role, str) else role)
            except ValueError:
                raise _config_error(f"category {category!r}: unknown role {role!r}; use one of "
                                    + ", ".join(Role)) from None
        if not isinstance(self.context_metrics, Mapping):
            raise _config_error("context_metrics must be a mapping of context category -> metric name")
        for category in self.context_metrics:
            if roles.get(category) is not Role.CONTEXT:
                raise _config_error(f"context_metrics names {category!r}, which is not mapped to the context role")
        metrics: dict[str, str] = {}
        for category, role in roles.items():
            if role is Role.CONTEXT:
                metric = self.context_metrics.get(category, category)
                if not isinstance(metric, str) or not _METRIC_NAME.fullmatch(metric) or metric == DEMAND_METRIC:
                    raise _config_error(
                        f"context category {category!r} needs a metric name in context_metrics (lowercase "
                        f"letters, digits and '_', starting with a letter, not {DEMAND_METRIC!r}); got {metric!r}")
                metrics[category] = metric
        object.__setattr__(self, "roles", FrozenMap(roles))
        object.__setattr__(self, "context_metrics", FrozenMap(metrics))

    def role_of(self, category: str) -> Role | None:
        return self.roles.get(category)

    def categories(self, role: Role | str) -> tuple[str, ...]:
        role = Role(role)  # accept the plain string value too, never silently match nothing
        return tuple(sorted(c for c, r in self.roles.items() if r is role))

    @property
    def metric_names(self) -> tuple[str, ...]:
        """Context metric names, sorted; the scoreable metrics are these plus ``demand_weight``."""
        return tuple(sorted(set(self.context_metrics.values())))

    @classmethod
    def legacy(cls) -> "RoleMapping":
        """The London-sample categories (existing_station, population, residential, commercial, transit)."""
        return cls(dict(LEGACY_CATEGORY_ROLES))


@dataclass(frozen=True)
class ScenarioConfig:
    """Parameters of one generic coverage run.

    ``method`` must be one of ``SUPPORTED_METHODS`` (see ``METHOD_BOUNDARY``). ``weights`` keys are
    ``demand_weight`` and the role mapping's context metrics; every context metric needs an
    explicit weight (0 allowed). ``allow_zero_existing=True`` declares that the input holds no
    existing-facility records: it is required when there are none and refused when there are
    some, so a mapping that names an existing-facility category can be reused either way.
    """

    roles: RoleMapping
    demand_weight_field: str = DEMAND_METRIC
    weights: Mapping[str, float] = field(default_factory=lambda: {DEMAND_METRIC: 1.0})
    allow_zero_existing: bool = False
    method: str = "weighted_coverage"
    target_count: int = 3
    grid_step_m: float = 500
    service_radius_m: float = 900
    min_existing_distance_m: float = 700
    min_new_site_distance_m: float = 600
    limits: ResourceLimits = DEFAULT_LIMITS

    def __post_init__(self):
        roles = self.roles
        if isinstance(roles, Mapping):
            roles = RoleMapping(roles)
            object.__setattr__(self, "roles", roles)
        if not isinstance(roles, RoleMapping):
            raise _config_error("roles must be a RoleMapping")
        if self.method not in SUPPORTED_METHODS:
            raise _config_error(f"unsupported decision method {self.method!r}; GeoDecision supports only "
                                + ", ".join(SUPPORTED_METHODS) + ". " + METHOD_BOUNDARY,
                                method=str(self.method), supported=list(SUPPORTED_METHODS))
        if not isinstance(self.limits, ResourceLimits):
            raise _config_error("limits must be a ResourceLimits")
        _check_screening(self, self.limits)
        if not isinstance(self.demand_weight_field, str) or not self.demand_weight_field.strip():
            raise _config_error("demand_weight_field must be a non-blank property name")
        if not isinstance(self.allow_zero_existing, bool):
            raise _config_error("allow_zero_existing must be True or False")
        if not roles.categories(Role.WEIGHTED_DEMAND):
            raise _config_error("no category is mapped to weighted_demand; coverage needs demand")
        if not self.allow_zero_existing and not roles.categories(Role.EXISTING_FACILITY):
            raise _config_error("no category is mapped to existing_facility; map one, or set "
                                "allow_zero_existing=True to declare a zero-baseline scenario")
        weights = _check_weights(self.weights, (DEMAND_METRIC, *roles.metric_names))
        unweighted = [m for m in roles.metric_names if m not in weights]
        if unweighted:
            raise _config_error("context metrics without a weight: " + ", ".join(unweighted) +
                                "; give each an explicit weight (0 is allowed) or map the category to ignore")
        object.__setattr__(self, "weights", weights)


def _parse_weight(entity: Entity, name: str) -> tuple[float | None, str | None]:
    if name not in entity.properties:
        return None, "missing"
    raw = entity.properties[name]
    if isinstance(raw, bool):
        return None, "boolean"
    if isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return None, f"not numeric: {raw!r}"
    elif isinstance(raw, Real):
        try:
            value = float(raw)
        except (OverflowError, TypeError, ValueError):
            return None, f"not representable: {raw!r}"
    else:
        return None, f"unsupported type {type(raw).__name__}"
    if not isfinite(value):
        return None, f"non-finite: {raw!r}"
    if value < 0:
        return None, f"negative: {raw!r}"
    return value, None


def plan_coverage(zone: Zone, entities: list[Entity] | tuple[Entity, ...], config: ScenarioConfig) -> dict:
    """Screen new sites for weighted-demand coverage using an explicit role mapping.

    Raises ``DecisionInputError`` with code UNMAPPED_ROLE, INVALID_ENTITY, INVALID_DEMAND_WEIGHT,
    NO_DEMAND, INVALID_STUDY_AREA, INVALID_SCENARIO_CONFIG or RESOURCE_LIMIT_EXCEEDED instead of
    returning a result built on input it could not interpret. Inputs are not modified.
    """
    if not isinstance(config, ScenarioConfig):
        raise _config_error("config must be a ScenarioConfig")
    local, _ = _study_area(zone, config.limits)
    if not isinstance(entities, (list, tuple)):
        raise _config_error("entities must be a list or tuple of Entity records")
    _check_entities(entities, config.limits)
    roles, weight_field = config.roles, config.demand_weight_field
    unmapped = Counter(e.category for e in entities if roles.role_of(e.category) is None)
    if unmapped:
        raise unmapped_error(unmapped, "Add each category to the RoleMapping as existing_facility, weighted_demand, "
                                       "context or ignore (ignore excludes it and lists it in the audit).")
    missing = [e.key for e in entities if roles.role_of(e.category) is not Role.IGNORE and not e.has_point]
    if missing:
        raise DecisionInputError("INVALID_ENTITY", f"missing coordinates: {_listing(missing)}; supply lon/lat or "
                                                   "map the category to ignore", keys=missing)
    weights, bad = {}, {}
    for e in entities:
        if roles.role_of(e.category) is Role.WEIGHTED_DEMAND:
            value, problem = _parse_weight(e, weight_field)
            if problem:
                bad[e.key] = problem
            else:
                weights[e.key] = value
    if bad:
        raise DecisionInputError(
            "INVALID_DEMAND_WEIGHT", f"property {weight_field!r} must be a finite, non-negative number on every "
            f"weighted_demand record, inside or outside the study area: "
            f"{_listing([f'{k} ({p})' for k, p in bad.items()])}", field=weight_field, keys=list(bad))
    if not isfinite(sum(weights.values())):
        raise DecisionInputError("INVALID_DEMAND_WEIGHT", "the sum of demand weights is not finite; rescale them",
                                 field=weight_field)

    # Active points must lie where the local metric projection is trusted; demand outside the
    # study area is excluded (but its weight was validated above), so it is not projected.
    covers = prep(zone.shape).covers
    inside_of, projected, far = {}, {}, []
    for e in entities:
        if not e.has_point:
            continue
        inside_of[e.key] = inside = bool(covers(Point(e.lon, e.lat)))
        role = roles.role_of(e.category)
        if role is Role.IGNORE or (role is Role.WEIGHTED_DEMAND and not inside):
            continue
        x, y = local.project(e.lon, e.lat)
        if not (isfinite(x) and isfinite(y)) or max(abs(x), abs(y)) > MAX_LOCAL_EXTENT_M:
            far.append(e.key)
        else:
            projected[e.key] = (x, y)
    if far:
        raise DecisionInputError(
            "INVALID_ENTITY", f"{_listing(far)} lie more than {MAX_LOCAL_EXTENT_M / 1000:g} km from the study-area "
            "centre, beyond the trusted local metric projection; pre-filter the input to the study region",
            keys=far, max_distance_m=MAX_LOCAL_EXTENT_M)
    existing_records = sum(1 for e in entities if roles.role_of(e.category) is Role.EXISTING_FACILITY)
    if not existing_records and not config.allow_zero_existing:
        raise _config_error("the input holds no existing_facility records (mapped categories: " +
                            (", ".join(roles.categories(Role.EXISTING_FACILITY)) or "none") + "); supply them, "
                            "or set allow_zero_existing=True to declare a zero-baseline scenario")
    if existing_records and config.allow_zero_existing:
        raise _config_error(f"allow_zero_existing=True declares no existing facilities, but the input holds "
                            f"{existing_records} existing_facility records; set allow_zero_existing=False to use "
                            "them, or remove them from the input", existing_records=existing_records)
    inside_demand = [k for k, w in weights.items() if inside_of[k]]
    if not inside_demand:
        outside = len(weights)
        raise DecisionInputError("NO_DEMAND", f"no weighted_demand record lies inside or on study area "
                                              f"{zone.zone_id} ({outside} outside); there is nothing to cover",
                                 outside_count=outside)
    if sum(weights[k] for k in inside_demand) <= 0:
        raise DecisionInputError("NO_DEMAND", f"total {weight_field!r} of the {len(inside_demand)} demand records "
                                              f"inside study area {zone.zone_id} is 0; coverage shares are undefined",
                                 outside_count=len(weights) - len(inside_demand))

    existing, existing_keys, demand, context, inputs, exclusions = [], [], [], [], [], []
    summary = {role.value: {"records": 0, "inside_study_area": 0, "outside_study_area": 0,
                            "without_coordinates": 0} for role in Role}
    outside_weight = 0.0
    for e in entities:
        role = roles.role_of(e.category)
        inside = inside_of.get(e.key)
        counts = summary[role.value]
        counts["records"] += 1
        counts["without_coordinates" if inside is None else
               "inside_study_area" if inside else "outside_study_area"] += 1
        status, metric = "USED", None
        if role is Role.IGNORE:
            status = "IGNORED"
            exclusions.append({"key": e.key, "category": e.category, "role": role.value,
                               "reason": "IGNORED_BY_ROLE_MAPPING", "has_point": e.has_point})
        else:
            x, y = projected.get(e.key, (None, None))
            if role is Role.EXISTING_FACILITY:
                existing.append((x, y))
                existing_keys.append(e.key)
            elif role is Role.CONTEXT:
                metric = roles.context_metrics[e.category]
                context.append((metric, x, y))
            else:
                metric = DEMAND_METRIC
                if inside:
                    demand.append((e.key, x, y, weights[e.key]))
                else:
                    status = "OUTSIDE_STUDY_AREA"
                    outside_weight += weights[e.key]
                    exclusions.append({"key": e.key, "category": e.category, "role": role.value,
                                       "reason": "DEMAND_OUTSIDE_STUDY_AREA", "demand_weight": weights[e.key]})
        inputs.append({"key": e.key, "source": e.source, "source_id": e.source_id, "name": e.name,
                       "category": e.category, "role": role.value, "metric": metric, "status": status,
                       "lon": e.lon, "lat": e.lat, "inside_study_area": inside,
                       "demand_weight": weights.get(e.key),
                       "properties": copy.deepcopy(e.properties),
                       "provenance": [p.model_dump() for p in e.provenance]})

    outside_count = summary[Role.WEIGHTED_DEMAND.value]["outside_study_area"]
    total = sum(w for *_, w in demand)
    run = _screen(zone, existing, demand, context, roles.metric_names, config, config.weights, config.limits,
                  demand_metric=DEMAND_METRIC, ids_key="covered_demand_ids",
                  incremental_key="incremental_demand_weight")
    selected = [{**s, "newly_covered_demand_ids": newly} for s, newly in zip(run["selected"], run["newly"])]
    existing_cov, incremental = run["existing_cov"], run["incremental"]
    pct = (lambda v: _pct(v, total))  # noqa: E731
    return {
        "method": config.method, "method_boundary": METHOD_BOUNDARY,
        "status": run["status"], "reason_code": run["reason"],
        "requested_count": config.target_count, "actual_count": len(selected),
        "message": run["message"],
        "generated_candidate_count": len(run["candidates"]), "feasible_candidate_count": len(run["valid"]),
        "candidates_excluded_by_existing_distance": len(run["candidates"]) - len(run["valid"]),
        "candidates": run["valid"], "selected": selected,
        "existing_facility_count": len(existing), "existing_facility_ids": existing_keys,
        "zero_existing_declared": config.allow_zero_existing,
        "demand_point_count": len(demand), "demand_points_outside_study_area": outside_count,
        "demand_weight_outside_study_area": outside_weight,
        "context_point_count": len(context),
        "demand_weight_field": weight_field,
        "total_demand_weight": total,
        "existing_demand_weight_covered": existing_cov,
        "new_incremental_demand_weight_covered": incremental,
        "final_total_demand_weight_covered": existing_cov + incremental,
        "existing_coverage_pct": pct(existing_cov),
        "new_incremental_coverage_pct": pct(incremental),
        "final_total_coverage_pct": pct(existing_cov + incremental),
        "existing_covered_demand_ids": sorted(run["existing_ids"]),
        "final_covered_demand_ids": sorted(run["covered_ids"]),
        "roles": {c: r.value for c, r in roles.roles.items()},
        "context_metrics": dict(roles.context_metrics),
        "role_summary": summary,
        "exclusions": exclusions,
        "inputs": inputs,
        "coverage_radius_m": config.service_radius_m, "distance_kind": "local_projected_euclidean",
        "coverage_boundary": "distance <= radius; demand inside or on the study boundary",
        "config": {"method": config.method, "target_count": config.target_count, "grid_step_m": config.grid_step_m,
                   "service_radius_m": config.service_radius_m,
                   "min_existing_distance_m": config.min_existing_distance_m,
                   "min_new_site_distance_m": config.min_new_site_distance_m,
                   "weights": dict(config.weights), "demand_weight_field": weight_field,
                   "allow_zero_existing": config.allow_zero_existing, "limits": asdict(config.limits)},
        "limitations": list(LIMITATIONS),
    }
