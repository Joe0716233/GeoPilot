"""Explicit data-quality reporting for entity lists."""

from __future__ import annotations

from collections import Counter

from .schema import Entity, Issue


def inspect_entities(entities: list[Entity]) -> list[Issue]:
    """Report duplicate keys, missing coordinates and missing names without altering records."""
    issues: list[Issue] = []
    counts = Counter(e.key for e in entities)
    for entity in entities:
        if counts[entity.key] > 1:
            issues.append(Issue(record_id=entity.key, code="DUPLICATE_ID",
                                detail="source and source_id repeat"))
        if not entity.has_point:
            issues.append(Issue(record_id=entity.key, code="MISSING_COORDINATE", detail="no point supplied"))
        if not entity.name.strip():
            issues.append(Issue(record_id=entity.key, code="MISSING_NAME", detail="name is empty"))
    return issues
