"""GeoVerify: check a claimed zone membership against metric boundary evidence.

Statuses are deliberately conservative:

* ``PASS``       point is inside the claimed zone by more than ``review_boundary_m``.
* ``REVIEW``     near a boundary (inside or outside), or inside several overlapping zones.
* ``FAIL``       point is outside the claimed zone by more than ``fail_outside_m``.
* ``UNRESOLVED`` cannot be evaluated (no coordinate, no claim, unknown claimed zone).

The tolerances are user-chosen review margins, not measured coordinate uncertainty. A PASS
means "geometrically consistent with the claim", not that the source coordinate is correct.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import isfinite

from shapely.geometry import Point

from geopilot.data.schema import Entity, Zone
from geopilot.tools.spatial import point_polygon_relation

STATUSES = ("PASS", "REVIEW", "FAIL", "UNRESOLVED")
UNCERTAINTY_NOTE = ("Distances are straight-line metres from the supplied coordinate to the supplied "
                    "boundary geometry; neither coordinate error nor boundary-version differences "
                    "are modelled.")


@dataclass(frozen=True)
class VerifyConfig:
    review_boundary_m: float = 20.0
    fail_outside_m: float = 50.0

    def __post_init__(self):
        for name in ("review_boundary_m", "fail_outside_m"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number of metres")


def _record(entity: Entity, claimed: str, config: VerifyConfig, **updates) -> dict:
    base = {"record_id": entity.key, "name": entity.name, "claimed_zone": claimed,
            "actual_zones": [], "signed_distance_m": None, "boundary_distance_m": None,
            "status": "UNRESOLVED", "reason_code": "",
            "review_boundary_m": config.review_boundary_m, "fail_outside_m": config.fail_outside_m,
            "uncertainty_note": UNCERTAINTY_NOTE}
    base.update(updates)
    return base


def verify_entity(entity: Entity, claimed_zone_id: str, zones: list[Zone],
                  config: VerifyConfig = VerifyConfig()) -> dict:
    claimed = (claimed_zone_id or "").strip()
    if not entity.has_point:
        return _record(entity, claimed, config, reason_code="NO_COORDINATE")
    if not claimed:
        return _record(entity, claimed, config, reason_code="NO_CLAIM")
    claimed_zone = next((z for z in zones if z.zone_id == claimed), None)
    if claimed_zone is None:
        return _record(entity, claimed, config, reason_code="UNKNOWN_CLAIMED_ZONE")
    point = Point(entity.lon, entity.lat)
    actual = sorted(z.zone_id for z in zones if z.shape.covers(point))
    relation = point_polygon_relation(entity.lon, entity.lat, claimed_zone.shape)
    distance = relation["distance_to_boundary_m"]
    fields = {"actual_zones": actual, "boundary_distance_m": round(distance, 2),
              "signed_distance_m": round(relation["signed_distance_m"], 2)}
    if len(actual) > 1:
        return _record(entity, claimed, config, **fields, status="REVIEW", reason_code="OVERLAPPING_ZONES")
    if claimed in actual:
        if distance <= config.review_boundary_m:
            return _record(entity, claimed, config, **fields, status="REVIEW", reason_code="INSIDE_NEAR_BOUNDARY")
        return _record(entity, claimed, config, **fields, status="PASS", reason_code="CLAIM_CONFIRMED")
    if distance <= config.fail_outside_m:
        return _record(entity, claimed, config, **fields, status="REVIEW", reason_code="OUTSIDE_NEAR_BOUNDARY")
    reason = "CLAIM_CONTRADICTED" if actual else "OUTSIDE_ALL_SUPPLIED_ZONES"
    return _record(entity, claimed, config, **fields, status="FAIL", reason_code=reason)


def verify_batch(entities: list[Entity], claimed_zones: dict[str, str], zones: list[Zone],
                 config: VerifyConfig = VerifyConfig()) -> list[dict]:
    ids = [z.zone_id for z in zones]
    if len(ids) != len(set(ids)):
        raise ValueError("zone IDs must be unique")
    return [verify_entity(e, claimed_zones.get(e.key, ""), zones, config) for e in entities]


def summarise(results: list[dict]) -> dict:
    counts = Counter(r["status"] for r in results)
    reasons = Counter(r["reason_code"] for r in results)
    return {"records": len(results), "status_counts": {s: counts.get(s, 0) for s in STATUSES},
            "reason_counts": dict(sorted(reasons.items())), "uncertainty_note": UNCERTAINTY_NOTE}
