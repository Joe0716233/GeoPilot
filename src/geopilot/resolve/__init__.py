"""GeoResolve: cross-source entity resolution."""

from .engine import ResolveConfig, ResolveResult, candidate_pairs, resolve_entities, score_pair

__all__ = ["ResolveConfig", "ResolveResult", "candidate_pairs", "resolve_entities", "score_pair"]
