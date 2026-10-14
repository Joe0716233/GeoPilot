"""GeoResolve: explainable candidate blocking, scoring and conservative entity grouping.

Scores are a fixed weighted heuristic over name, address, distance, category, phone and URL
similarity. ``MATCH`` means "strong candidate under this heuristic", not confirmed identity:
no ground-truth labels are used and no precision/recall is claimed. Proximity alone can never
produce a MATCH, and groups are only merged when every cross-pair is itself a MATCH and no
source would contribute two records to one group.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from math import cos, isfinite, radians

from rapidfuzz.fuzz import ratio
from shapely.geometry import Point, box
from shapely.strtree import STRtree

from geopilot.data.schema import Entity, Provenance
from geopilot.tools.spatial import geodesic_distance_m

WEIGHTS = {"name": .36, "address": .25, "spatial": .20, "category": .08, "phone": .07, "url": .04}
LIMITATIONS = (
    "Scores are heuristic and unlabelled; MATCH is a candidate, not confirmed identity.",
    "Nearby points can be different sites; REVIEW/NO_MATCH pairs are never merged automatically.",
    "Records without coordinates are only compared through a 4-character name-prefix block.",
)


@dataclass(frozen=True)
class ResolveConfig:
    match_score: float = .78
    review_score: float = .55
    radius_m: float = 300.0

    def __post_init__(self):
        for name in ("match_score", "review_score", "radius_m"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        if not 0 <= self.review_score < self.match_score <= 1:
            raise ValueError("resolve thresholds must satisfy 0 <= review < match <= 1")
        if self.radius_m <= 0:
            raise ValueError("radius_m must be positive")


@dataclass
class ResolveResult:
    pairs: list[dict]
    canonical: list[Entity]
    clusters: list[dict]
    summary: dict
    limitations: tuple[str, ...] = field(default=LIMITATIONS)


def normalise(value: str) -> str:
    return re.sub(r"[^\w]+", "", value.casefold())


def distance_m(a: Entity, b: Entity) -> float | None:
    if not (a.has_point and b.has_point):
        return None
    return geodesic_distance_m(a.lon, a.lat, b.lon, b.lat)


def candidate_pairs(entities: list[Entity], radius_m: float = 300.0) -> list[tuple[int, int]]:
    """Index pairs from different sources within ``radius_m`` (R-tree), plus name-prefix blocks
    for pairs where at least one record has no coordinate."""
    if not isfinite(radius_m) or radius_m <= 0:
        raise ValueError("radius_m must be positive")
    spatial = [i for i, e in enumerate(entities) if e.has_point]
    tree = STRtree([Point(entities[i].lon, entities[i].lat) for i in spatial]) if spatial else None
    prefix: dict[str, list[int]] = defaultdict(list)
    result: set[tuple[int, int]] = set()
    for i, e in enumerate(entities):
        candidates: set[int] = set()
        if tree is not None and e.has_point:
            # Generous degree window (floored cos avoids blow-up near the poles); exact test below.
            lat_span = radius_m / 110_000
            lon_span = radius_m / (111_000 * max(cos(radians(e.lat)), .01))
            window = box(e.lon - lon_span, e.lat - lat_span, e.lon + lon_span, e.lat + lat_span)
            candidates.update(spatial[int(n)] for n in tree.query(window) if spatial[int(n)] < i)
        key = normalise(e.name)[:4]
        if key:
            candidates.update(j for j in prefix[key] if not (e.has_point and entities[j].has_point))
            prefix[key].append(i)
        for j in candidates:
            other = entities[j]
            if other.source == e.source:
                continue
            d = distance_m(e, other)
            if d is None or d <= radius_m:
                result.add((j, i))
    return sorted(result)


def score_pair(a: Entity, b: Entity, config: ResolveConfig = ResolveConfig()) -> dict:
    name = ratio(normalise(a.name), normalise(b.name)) / 100 if a.name and b.name else 0.0
    address = ratio(normalise(a.address), normalise(b.address)) / 100 if a.address and b.address else 0.0
    distance = distance_m(a, b)
    spatial = max(0.0, 1 - distance / config.radius_m) if distance is not None else 0.0
    category = float(bool(a.category and b.category and normalise(a.category) == normalise(b.category)))
    phone = float(bool(a.phone and b.phone and normalise(a.phone) == normalise(b.phone)))
    url = float(bool(a.url and b.url and a.url.strip().casefold().rstrip("/") == b.url.strip().casefold().rstrip("/")))
    features = {"name": round(name, 3), "address": round(address, 3), "spatial": round(spatial, 3),
                "category": category, "phone": phone, "url": url}
    score = (WEIGHTS["name"] * name + WEIGHTS["address"] * address + WEIGHTS["spatial"] * spatial
             + WEIGHTS["category"] * category + WEIGHTS["phone"] * phone + WEIGHTS["url"] * url)
    score = min(max(score, 0.0), 1.0)
    status = "MATCH" if score >= config.match_score else "REVIEW" if score >= config.review_score else "NO_MATCH"
    review_reason = None
    # Proximity and shared category alone are not identity evidence: without a name that is at least
    # loosely similar, or a shared phone/URL, a pair can reach REVIEW at most, never MATCH.
    if status == "MATCH" and name < .35 and not (phone or url):
        status, review_reason = "REVIEW", "INSUFFICIENT_IDENTITY_EVIDENCE"
    if status == "NO_MATCH" and distance is not None and distance <= 25 and category:
        if not (a.name and b.name and a.address and b.address):
            status, review_reason = "REVIEW", "NEARBY_INCOMPLETE_IDENTITY"
    return {"left": a.key, "right": b.key, "features": features,
            "distance_m": round(distance, 2) if distance is not None else None,
            "score": round(score, 3), "status": status, "review_reason": review_reason,
            "fusion_reason": None}


def resolve_entities(entities: list[Entity], config: ResolveConfig = ResolveConfig()) -> ResolveResult:
    keys = [e.key for e in entities]
    if len(keys) != len(set(keys)):
        raise ValueError("source and source_id must identify unique records")
    index = {k: i for i, k in enumerate(keys)}
    scored: dict[tuple[int, int], dict] = {}
    pairs = []
    for i, j in candidate_pairs(entities, config.radius_m):
        pair = score_pair(entities[i], entities[j], config)
        scored[(i, j)] = pair
        pairs.append(pair)

    groups: dict[int, set[int]] = {i: {i} for i in range(len(entities))}
    member_of = {i: i for i in range(len(entities))}

    def pair_status(i: int, j: int) -> str:
        key = (min(i, j), max(i, j))
        if key not in scored:
            scored[key] = score_pair(entities[key[0]], entities[key[1]], config)
        return scored[key]["status"]

    for pair in sorted((p for p in pairs if p["status"] == "MATCH"),
                       key=lambda p: (-p["score"], p["left"], p["right"])):
        a, b = member_of[index[pair["left"]]], member_of[index[pair["right"]]]
        if a == b:
            continue
        sources = [entities[i].source for i in groups[a] | groups[b]]
        if len(sources) != len(set(sources)):
            pair["status"], pair["fusion_reason"] = "REVIEW", "SAME_SOURCE_CONFLICT"
            continue
        if any(pair_status(i, j) != "MATCH" for i in groups[a] for j in groups[b]):
            pair["status"], pair["fusion_reason"] = "REVIEW", "TRANSITIVE_CONFLICT"
            continue
        groups[a] |= groups.pop(b)
        for i in groups[a]:
            member_of[i] = a

    review_keys = {k for p in pairs if p["status"] == "REVIEW" for k in (p["left"], p["right"])}
    canonical, clusters = [], []
    for members in groups.values():
        group = [entities[i] for i in sorted(members)]
        chosen = max(group, key=lambda e: (e.has_point, len(e.address), len(e.name), e.key))
        provenance, seen = [], set()
        for entity in group:
            for item in entity.provenance or [Provenance(source=entity.source, source_id=entity.source_id)]:
                marker = (item.source, item.source_id, item.retrieved_at, item.license_note)
                if marker not in seen:
                    seen.add(marker)
                    provenance.append(item)
        aliases = sorted({e.name for e in group if e.name and e.name != chosen.name})
        merged = chosen.model_copy(update={"provenance": provenance, "aliases": aliases})
        canonical.append(merged)
        member_keys = sorted(e.key for e in group)
        clusters.append({"canonical_key": merged.key, "member_keys": member_keys,
                         "size": len(member_keys),
                         "needs_review": bool(review_keys & set(member_keys))})
    canonical.sort(key=lambda e: e.key)
    clusters.sort(key=lambda c: c["canonical_key"])
    counts = Counter(p["status"] for p in pairs)
    summary = {"input_records": len(entities), "candidate_pairs": len(pairs),
               "status_counts": {s: counts.get(s, 0) for s in ("MATCH", "REVIEW", "NO_MATCH")},
               "canonical_records": len(canonical),
               "merged_clusters": sum(1 for c in clusters if c["size"] > 1),
               "clusters_needing_review": sum(1 for c in clusters if c["needs_review"]),
               "match_score": config.match_score, "review_score": config.review_score,
               "radius_m": config.radius_m}
    pairs.sort(key=lambda p: (-p["score"], p["left"], p["right"]))
    return ResolveResult(pairs=pairs, canonical=canonical, clusters=clusters, summary=summary)
