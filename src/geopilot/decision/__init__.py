"""GeoDecision: coverage-based site screening."""

from .engine import DecisionConfig, generate_candidates, plan_locations

__all__ = ["DecisionConfig", "generate_candidates", "plan_locations"]
