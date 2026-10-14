"""The bundled London sample scenario: loading and one-call runners for the three modules.

The sample combines two real historic open-data layers (GLA borough boundaries, GLA rapid
charging points) with clearly synthetic inputs (boundary probes, an operator feed derived
from the historic names, demand points). See DATA_SOURCES.md.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from geopilot.data import Entity, Zone, load_claims, load_entities, load_zones
from geopilot.decision import DecisionConfig, plan_locations
from geopilot.resolve import ResolveConfig, resolve_entities
from geopilot.verify import VerifyConfig, summarise, verify_batch

SAMPLE_NOTICE = ("Real inputs are historic open data (GLA, retrieved 2026-09-27) and may be out of date. "
                 "Probes, the operator feed and demand points are synthetic. Results illustrate the "
                 "method and are not evidence about current London charging provision.")


def default_data_dir() -> Path:
    """examples/data/london next to the source tree; override with GEOPILOT_DATA_DIR."""
    override = os.environ.get("GEOPILOT_DATA_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "examples" / "data" / "london"


@dataclass
class LondonSample:
    data_dir: Path
    zones: list[Zone]
    chargers: list[Entity]
    probes: list[Entity]
    feed: list[Entity]
    planning_context: list[Entity]
    claims: dict[str, str]
    study_zone_id: str
    manifest: dict = field(default_factory=dict)

    @property
    def study_zone(self) -> Zone:
        return next(z for z in self.zones if z.zone_id == self.study_zone_id)


def load_london_sample(data_dir: str | Path | None = None) -> LondonSample:
    directory = Path(data_dir) if data_dir else default_data_dir()
    required = ["boroughs_subset.geojson", "gla_chargers_subset.csv", "synthetic_boundary_probes.csv",
                "synthetic_operator_feed.csv", "synthetic_demand.csv", "manifest.json"]
    missing = [name for name in required if not (directory / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"London sample data not found in {directory} (missing: {', '.join(missing)}). "
            "Run from a repository checkout or pass --data-dir / set GEOPILOT_DATA_DIR.")
    zones = load_zones(directory / "boroughs_subset.geojson", source="gla_boroughs")
    chargers = load_entities(directory / "gla_chargers_subset.csv", source="gla_chargers")
    probes = load_entities(directory / "synthetic_boundary_probes.csv", source="synthetic_probe")
    feed = load_entities(directory / "synthetic_operator_feed.csv", source="synthetic_operator_feed")
    demand = load_entities(directory / "synthetic_demand.csv", source="synthetic_demand")
    claims = {c.key: c.district for c in chargers}
    claims.update(load_claims(directory / "synthetic_boundary_probes.csv", source="synthetic_probe"))
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    return LondonSample(directory, zones, chargers, probes, feed, demand, claims,
                        manifest["study_zone"], manifest)


def run_verify(sample: LondonSample, config: VerifyConfig = VerifyConfig()) -> dict:
    entities = sample.chargers + sample.probes
    results = verify_batch(entities, sample.claims, sample.zones, config)
    return {"module": "GeoVerify", "notice": SAMPLE_NOTICE,
            "parameters": {"review_boundary_m": config.review_boundary_m,
                           "fail_outside_m": config.fail_outside_m},
            "summary": summarise(results), "results": results}


def run_resolve(sample: LondonSample, config: ResolveConfig = ResolveConfig()) -> dict:
    result = resolve_entities(sample.chargers + sample.feed, config)
    return {"module": "GeoResolve", "notice": SAMPLE_NOTICE,
            "parameters": {"match_score": config.match_score, "review_score": config.review_score,
                           "radius_m": config.radius_m},
            "summary": result.summary, "pairs": result.pairs, "clusters": result.clusters,
            "canonical": [e.model_dump(exclude={"properties"}) for e in result.canonical],
            "limitations": list(result.limitations)}


def run_decision(sample: LondonSample, config: DecisionConfig = DecisionConfig()) -> dict:
    entities = sample.chargers + sample.planning_context
    plan = plan_locations(sample.study_zone, entities, config)
    return {"module": "GeoDecision", "notice": SAMPLE_NOTICE, "study_zone": sample.study_zone_id, **plan}


def run_all(sample: LondonSample) -> dict:
    return {"verify": run_verify(sample), "resolve": run_resolve(sample), "decision": run_decision(sample)}
