"""Built-in coverage examples: EV charging stations and public toilets in Southwark, one shared method.

Both examples call the same ``plan_coverage`` function with the same weighted-coverage algorithm:

- **Facilities:** saved OpenStreetMap records (``examples/data/coverage/osm_facilities_southwark.json``,
  ODbL 1.0, © OpenStreetMap contributors, retrieved 2026-10-14). The two examples differ only in which
  category they select (``charging_station`` or ``toilets``) and in a generic ``EligibilityPolicy`` on
  the OSM ``access`` tag. The toilet example always applies its policy. The EV example uses all 63
  records by default and applies a conservative access policy only when ``conservative_access=True``.
  Every result carries an ``access_summary`` of the raw tags.
- **Demand and context:** the bundled synthetic points (``examples/data/london/synthetic_demand.csv``).
  The ``population`` field is used as *synthetic demand units*, not observed demand.
- **Study area:** the bundled Southwark boundary (``examples/data/london/boroughs_subset.geojson``).

Facilities outside Southwark (but inside its bounding box) are kept. They can cover demand inside the
study area and are counted as existing facilities. Nothing here verifies current availability, opening
hours, fees, indoor access or wheelchair access.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from geopilot.data import Entity, Zone, load_entities, load_zones
from geopilot.decision import (DEFAULT_LIMITS, DEMAND_METRIC, DecisionInputError, EligibilityPolicy, ResourceLimits,
                               Role, RoleMapping, ScenarioConfig, apply_eligibility, plan_coverage)
from geopilot.scenario import default_data_dir

OSM_FILE = "osm_facilities_southwark.json"
DEMAND_FIELD = "population"
CONTEXT_CATEGORIES = ("residential", "commercial", "transit")
DEFAULT_PARAMETERS = {"target_count": 3, "grid_step_m": 500, "service_radius_m": 500, "min_existing_distance_m": 250,
                      "min_new_site_distance_m": 600}
DEFAULT_WEIGHTS = {DEMAND_METRIC: 1.0, "residential": 0.0, "commercial": 0.0, "transit": 0.0}
COMMON_NOTES = (
    "Demand weights are synthetic demand units (the bundled synthetic 'population' points), not observed demand "
    "for charging or toilets.",
    "Facilities are OpenStreetMap records saved on 2026-10-14 (ODbL 1.0, © OpenStreetMap contributors). Their "
    "current existence and availability are not verified.",
    "Existing facilities outside Southwark but inside its bounding box are kept: they can cover demand inside "
    "the study area.",
    "18 of the 163 saved records use a bounding-box centre as the coordinate (OSM ways), not an entrance.",
)


@dataclass(frozen=True)
class CoverageExample:
    key: str
    title_en: str
    title_zh: str
    facility_category: str
    eligibility: EligibilityPolicy | None          # always applied (None: every record of the category is used)
    notes: tuple[str, ...]
    optional_eligibility: EligibilityPolicy | None = None   # applied only with conservative_access=True


TOILET_ACCESS_POLICY = EligibilityPolicy(attribute="access", eligible_values=frozenset({"yes", "permissive"}),
                                         restricted_values=frozenset({"customers", "permit", "private", "no"}),
                                         property_path=("raw_tags",))
# Opt-in for the EV example: only confirmed public access counts; known restrictions and missing tags are excluded.
EV_CONSERVATIVE_ACCESS_POLICY = EligibilityPolicy(
    attribute="access", eligible_values=frozenset({"yes", "permissive"}),
    restricted_values=frozenset({"no", "customers", "taxi", "private", "permit"}), property_path=("raw_tags",))
EXAMPLES = {
    "ev_charging": CoverageExample(
        "ev_charging", "Electric vehicle charging stations", "电动汽车充电站", "charging_station", None,
        ("By default all 63 saved amenity=charging_station records are used as tagged. Their access tags (5 'yes', "
         "2 'no', 1 'customers', 1 'taxi', 54 missing) are not filtered, so the default result is not a statement "
         "about public availability. The optional conservative access filter keeps only access=yes/permissive.",),
        EV_CONSERVATIVE_ACCESS_POLICY),
    "public_toilets": CoverageExample(
        "public_toilets", "Public toilets", "公共厕所", "toilets", TOILET_ACCESS_POLICY,
        ("Only access=yes or access=permissive toilets are eligible (28 of 100). customers/permit/private (18) are "
         "excluded, and the 54 with no access tag are excluded unless you opt in.",
         "Opening hours, fees, indoor location and wheelchair access are not validated.")),
}


def coverage_data_dir() -> Path:
    """examples/data/coverage next to the source tree; override with GEOPILOT_COVERAGE_DATA_DIR."""
    override = os.environ.get("GEOPILOT_COVERAGE_DATA_DIR")
    return Path(override) if override else Path(__file__).resolve().parents[2] / "examples" / "data" / "coverage"


@dataclass(frozen=True)
class CoverageInputs:
    zone: Zone
    facilities: tuple[Entity, ...]        # all 163 saved OSM records, categories and provenance untouched
    demand_context: tuple[Entity, ...]    # synthetic population + context points
    sources: dict


def load_coverage_inputs(coverage_dir: str | Path | None = None, london_dir: str | Path | None = None) -> CoverageInputs:
    """Load the bundled example inputs, checking the OSM file against its manifest hash."""
    coverage_dir = Path(coverage_dir) if coverage_dir else coverage_data_dir()
    london_dir = Path(london_dir) if london_dir else default_data_dir()
    osm_path, manifest_path = coverage_dir / OSM_FILE, coverage_dir / "manifest.json"
    if not osm_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError(f"coverage example data not found in {coverage_dir}; run from a repository checkout "
                                "or set GEOPILOT_COVERAGE_DATA_DIR")
    raw = osm_path.read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest["files"][OSM_FILE]["sha256"]
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError(f"{OSM_FILE} does not match its manifest SHA-256; the bundled ODbL file was modified")
    facilities = tuple(Entity(**record) for record in json.loads(raw.decode("utf-8")))
    london_manifest = json.loads((london_dir / "manifest.json").read_text(encoding="utf-8"))
    zones = load_zones(london_dir / "boroughs_subset.geojson", source="gla_boroughs")
    zone = next(z for z in zones if z.zone_id == london_manifest["study_zone"])
    demand = tuple(load_entities(london_dir / "synthetic_demand.csv", source="synthetic_demand"))
    return CoverageInputs(zone, facilities, demand, {
        "facilities": {"file": f"examples/data/coverage/{OSM_FILE}", "sha256": expected, "records": len(facilities),
                       "license": "ODbL 1.0", "attribution": "© OpenStreetMap contributors",
                       "retrieved_at": manifest["files"][OSM_FILE]["retrieved_at"]},
        "demand": {"file": "examples/data/london/synthetic_demand.csv", "nature": "synthetic demand units"},
        "study_zone": {"file": "examples/data/london/boroughs_subset.geojson", "zone_id": zone.zone_id}})


def _example(key: str) -> CoverageExample:
    if not isinstance(key, str) or key not in EXAMPLES:
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", f"unknown coverage example {key!r}; choose one of "
                                 + ", ".join(EXAMPLES), supported=list(EXAMPLES))
    return EXAMPLES[key]


def example_config(key: str, *, weights: dict | None = None, **parameters) -> ScenarioConfig:
    """The ScenarioConfig of an example: its facility category as existing facilities, shared defaults.

    ``weights=None`` means the defaults; any explicit value (including ``{}``) is validated as given.
    """
    example = _example(key)
    roles = RoleMapping({example.facility_category: Role.EXISTING_FACILITY, DEMAND_FIELD: Role.WEIGHTED_DEMAND,
                         **{c: Role.CONTEXT for c in CONTEXT_CATEGORIES}})
    return ScenarioConfig(roles=roles, demand_weight_field=DEMAND_FIELD,
                          weights=DEFAULT_WEIGHTS if weights is None else weights,
                          **{**DEFAULT_PARAMETERS, **parameters})


def access_summary(facilities: list[Entity] | tuple[Entity, ...], policy: EligibilityPolicy, *,
                   filter_applied: bool, limits: ResourceLimits = DEFAULT_LIMITS) -> dict:
    """Counts of the raw access tags of ``facilities`` under ``policy``; describes the data, filters nothing."""
    counts = apply_eligibility(list(facilities), EligibilityPolicy(
        policy.attribute, policy.eligible_values, policy.restricted_values, False, policy.property_path),
        limits).counts
    values: dict[str, int] = {}
    for entity in facilities:
        node = entity.properties
        for step in policy.property_path:
            node = node.get(step) if isinstance(node, dict) else None
        raw = node.get(policy.attribute) if isinstance(node, dict) else None
        label = "(missing)" if raw is None or not str(raw).strip() else str(raw)
        values[label] = values.get(label, 0) + 1
    return {"attribute": ".".join((*policy.property_path, policy.attribute)), "records": counts["records"],
            "confirmed": counts["ELIGIBLE"], "known_restricted": counts["EXCLUDED_RESTRICTED"],
            "other_values": counts["EXCLUDED_OTHER_VALUE"], "unknown": counts["EXCLUDED_UNKNOWN"],
            "confirmed_values": sorted(policy.eligible_values), "restricted_values": sorted(policy.restricted_values),
            "values": dict(sorted(values.items())), "filter_applied": filter_applied,
            "note": "Counts of OpenStreetMap access tags as saved; they do not establish current public availability."}


def run_coverage_example(key: str, *, inputs: CoverageInputs | None = None, include_unknown_access: bool = False,
                         weights: dict | None = None, conservative_access: bool = False, **parameters) -> dict:
    """Run one example through ``plan_coverage``; adds the eligibility audit and example metadata.

    ``parameters`` are ``ScenarioConfig`` fields (distances, target, method, ``limits``); ``limits`` also
    bounds the eligibility step. ``conservative_access=True`` applies the example's optional access policy
    (EV: only access=yes/permissive; known restrictions and, unless ``include_unknown_access``, missing tags
    excluded); the toilet example always applies its access policy. ``access_summary`` always reports the
    raw access-tag counts, whether or not a filter was applied.
    """
    example = _example(key)
    if not isinstance(conservative_access, bool):
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", "conservative_access must be True or False")
    config = example_config(key, weights=weights, **parameters)
    inputs = inputs or load_coverage_inputs()
    candidates = [e for e in inputs.facilities if e.category == example.facility_category]
    active = example.eligibility or (example.optional_eligibility if conservative_access else None)
    summary = access_summary(candidates, example.eligibility or example.optional_eligibility,
                             filter_applied=active is not None, limits=config.limits)
    eligibility = None
    if active is not None:
        policy = EligibilityPolicy(active.attribute, active.eligible_values, active.restricted_values,
                                   include_unknown_access, active.property_path)
        checked = apply_eligibility(candidates, policy, config.limits)
        candidates = list(checked.kept)
        audit = [{**row, "lon": e.lon, "lat": e.lat} for row, e in
                 zip(checked.audit, [f for f in inputs.facilities if f.category == example.facility_category])]
        eligibility = {"policy": {"attribute": "raw_tags.access", "eligible": sorted(policy.eligible_values),
                                  "restricted": sorted(policy.restricted_values),
                                  "include_unknown": policy.include_unknown},
                       "counts": checked.counts, "warnings": list(checked.warnings), "audit": audit}
    result = plan_coverage(inputs.zone, candidates + list(inputs.demand_context), config)
    return {**result, "example": {"key": key, "title": example.title_en, "facility_category": example.facility_category,
                                  "facility_records": sum(1 for e in inputs.facilities
                                                          if e.category == example.facility_category),
                                  "facilities_used": len(candidates), "notes": [*example.notes, *COMMON_NOTES],
                                  "demand_units": "synthetic demand units", "sources": inputs.sources,
                                  "access_filter": "conservative" if active is not None else "none"},
            "eligibility": eligibility, "access_summary": summary, "study_zone_id": inputs.zone.zone_id}
