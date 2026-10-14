"""GeoDecision: coverage-based site screening."""

from .coverage import (DEMAND_METRIC, METHOD_BOUNDARY, SUPPORTED_METHODS, Role, RoleMapping, ScenarioConfig,
                       plan_coverage)
from .eligibility import ELIGIBILITY_DECISIONS, EligibilityPolicy, EligibilityResult, apply_eligibility
from .engine import (DEFAULT_LIMITS, ERROR_CODES, MAX_DISTANCE_M, DecisionConfig, DecisionInputError, ResourceLimits,
                     generate_candidates, plan_locations)

__all__ = ["DEFAULT_LIMITS", "DEMAND_METRIC", "ELIGIBILITY_DECISIONS", "ERROR_CODES", "MAX_DISTANCE_M",
           "METHOD_BOUNDARY", "SUPPORTED_METHODS", "DecisionConfig", "DecisionInputError", "EligibilityPolicy",
           "EligibilityResult", "ResourceLimits", "Role", "RoleMapping", "ScenarioConfig", "apply_eligibility",
           "generate_candidates", "plan_coverage", "plan_locations"]
