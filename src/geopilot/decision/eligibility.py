"""Generic attribute-based eligibility filter for facility records, with a per-record audit.

``apply_eligibility`` reads one attribute (e.g. an OSM ``access`` tag) from each entity's properties
and keeps the records whose value is listed as eligible. It is a data-quality step before
``plan_coverage``, not part of the coverage algorithm, and knows nothing about facility types.

Each record gets one decision:

- ``ELIGIBLE``: the value is in ``eligible_values``.
- ``EXCLUDED_RESTRICTED``: the value is in ``restricted_values``.
- ``EXCLUDED_OTHER_VALUE``: the value is in neither list.
- ``EXCLUDED_UNKNOWN``: the attribute is missing or blank (the default for unknown values).
- ``INCLUDED_UNKNOWN``: the attribute is missing, but ``include_unknown=True`` opted in. This
  produces a warning, because the records may not actually be usable.

Values are compared after trimming whitespace and lower-casing. The original value is kept in the
audit.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from geopilot.data.schema import Entity

from .engine import DEFAULT_LIMITS, DecisionInputError, ResourceLimits, _check_entities

ELIGIBILITY_DECISIONS = ("ELIGIBLE", "EXCLUDED_RESTRICTED", "EXCLUDED_OTHER_VALUE", "EXCLUDED_UNKNOWN",
                         "INCLUDED_UNKNOWN")


@dataclass(frozen=True)
class EligibilityPolicy:
    """Keep records whose ``attribute`` (found under ``properties[path...]``) is an eligible value."""

    attribute: str
    eligible_values: frozenset[str]
    restricted_values: frozenset[str] = frozenset()
    include_unknown: bool = False
    property_path: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not isinstance(self.attribute, str) or not self.attribute.strip():
            raise DecisionInputError("INVALID_SCENARIO_CONFIG", "eligibility attribute must be a non-blank string")
        if not isinstance(self.include_unknown, bool):
            raise DecisionInputError("INVALID_SCENARIO_CONFIG", "include_unknown must be True or False")
        eligible = _strings(self.eligible_values, "eligible_values")
        restricted = _strings(self.restricted_values, "restricted_values")
        if not eligible:
            raise DecisionInputError("INVALID_SCENARIO_CONFIG", "eligible_values must not be empty")
        if eligible & restricted:
            raise DecisionInputError("INVALID_SCENARIO_CONFIG", "values cannot be both eligible and restricted: "
                                     + ", ".join(sorted(eligible & restricted)))
        # A bare string would otherwise be read character by character ("raw_tags" -> "r", "a", ...).
        if not isinstance(self.property_path, (list, tuple)) or not all(
                isinstance(step, str) and step.strip() for step in self.property_path):
            raise DecisionInputError("INVALID_SCENARIO_CONFIG", "property_path must be a list or tuple of non-blank "
                                                                "property names, e.g. ('raw_tags',)")
        object.__setattr__(self, "eligible_values", eligible)
        object.__setattr__(self, "restricted_values", restricted)
        object.__setattr__(self, "property_path", tuple(self.property_path))


def _strings(values, name: str) -> frozenset[str]:
    """Normalised value set; a bare string, a non-collection or a non-string/blank entry is a config error."""
    if isinstance(values, (str, bytes)) or not isinstance(values, (set, frozenset, list, tuple)):
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", f"{name} must be a set, list or tuple of strings, "
                                                            f"e.g. {{'yes'}}, not {type(values).__name__}")
    if not all(isinstance(v, str) and v.strip() for v in values):
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", f"{name} entries must be non-blank strings")
    return frozenset(_norm(v) for v in values)


@dataclass(frozen=True)
class EligibilityResult:
    kept: tuple[Entity, ...]
    audit: tuple[dict, ...]
    counts: dict
    warnings: tuple[str, ...]


def _norm(value) -> str:
    return str(value).strip().lower()


def _lookup(entity: Entity, policy: EligibilityPolicy):
    node = entity.properties
    for step in policy.property_path:
        node = node.get(step) if isinstance(node, dict) else None
    return node.get(policy.attribute) if isinstance(node, dict) else None


def apply_eligibility(entities: list[Entity] | tuple[Entity, ...], policy: EligibilityPolicy,
                      limits: ResourceLimits = DEFAULT_LIMITS) -> EligibilityResult:
    """Split ``entities`` by ``policy``; every record appears once in the audit, in input order.

    ``entities`` gets the same checks as the coverage core: a bounded list/tuple of ``Entity`` records with
    unique keys.
    """
    if not isinstance(policy, EligibilityPolicy):
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", "policy must be an EligibilityPolicy")
    if not isinstance(limits, ResourceLimits):
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", "limits must be a ResourceLimits")
    if not isinstance(entities, (list, tuple)):
        raise DecisionInputError("INVALID_SCENARIO_CONFIG", "entities must be a list or tuple of Entity records")
    _check_entities(entities, limits)
    kept, audit = [], []
    where = ".".join((*policy.property_path, policy.attribute))
    for entity in entities:
        raw = _lookup(entity, policy)
        value = "" if raw is None else _norm(raw)
        if not value:
            decision = "INCLUDED_UNKNOWN" if policy.include_unknown else "EXCLUDED_UNKNOWN"
            reason = f"{where} is not recorded" + ("; included by explicit opt-in" if policy.include_unknown
                                                   else "; excluded because eligibility is unknown")
        elif value in policy.eligible_values:
            decision, reason = "ELIGIBLE", f"{where}={raw!s} is eligible"
        elif value in policy.restricted_values:
            decision, reason = "EXCLUDED_RESTRICTED", f"{where}={raw!s} is restricted"
        else:
            decision, reason = "EXCLUDED_OTHER_VALUE", f"{where}={raw!s} is not an eligible value"
        if decision in {"ELIGIBLE", "INCLUDED_UNKNOWN"}:
            kept.append(entity)
        audit.append({"key": entity.key, "source": entity.source, "source_id": entity.source_id,
                      "name": entity.name, "category": entity.category, "attribute": where,
                      "value": raw, "decision": decision, "reason": reason})
    counts = Counter(a["decision"] for a in audit)
    counts = {"records": len(audit), **{d: counts.get(d, 0) for d in ELIGIBILITY_DECISIONS}}
    warnings = ()
    if counts["INCLUDED_UNKNOWN"]:
        warnings = (f"{counts['INCLUDED_UNKNOWN']} records with no recorded {where} were included by opt-in; "
                    "they may not be usable by the public.",)
    return EligibilityResult(tuple(kept), tuple(audit), counts, warnings)
