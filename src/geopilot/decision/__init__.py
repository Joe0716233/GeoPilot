"""GeoDecision: coverage-based site screening."""

from .coverage import (DEMAND_METRIC, METHOD_BOUNDARY, SUPPORTED_METHODS, Role, RoleMapping, ScenarioConfig,
                       plan_coverage)
from .engine import (DEFAULT_LIMITS, ERROR_CODES, MAX_DISTANCE_M, DecisionConfig, DecisionInputError, ResourceLimits,
                     generate_candidates, plan_locations)

__all__ = ["DEFAULT_LIMITS", "DEMAND_METRIC", "ERROR_CODES", "MAX_DISTANCE_M", "METHOD_BOUNDARY", "SUPPORTED_METHODS",
           "DecisionConfig", "DecisionInputError", "ResourceLimits", "Role", "RoleMapping", "ScenarioConfig",
           "generate_candidates", "plan_coverage", "plan_locations"]
