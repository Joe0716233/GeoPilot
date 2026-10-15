"""JSON and CSV export of module results."""

from __future__ import annotations

import csv
import io
import json
import math
from pathlib import Path
from typing import Any


def to_json(obj: Any) -> str:
    # allow_nan=False: NaN/Infinity are not valid JSON, so fail loudly instead of writing them.
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False) + "\n"


def _cell(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite number cannot be exported")
    if isinstance(value, (list, tuple)):
        return ";".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return "" if value is None else value


def to_csv(rows: list[dict]) -> str:
    """Flat CSV; nested lists are ';'-joined and dicts are JSON-encoded in the cell."""
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(v) for k, v in row.items()})
    return buffer.getvalue()


def tabular_views(module: str, result: dict) -> dict[str, list[dict]]:
    """The row sets exported as CSV for each module result."""
    if module == "verify":
        return {"verify_results": result["results"]}
    if module == "resolve":
        pairs = [{"left": p["left"], "right": p["right"], "distance_m": p["distance_m"],
                  "score": p["score"], "status": p["status"],
                  "review_reason": p["review_reason"], "fusion_reason": p["fusion_reason"],
                  **{f"feature_{k}": v for k, v in p["features"].items()}} for p in result["pairs"]]
        return {"resolve_pairs": pairs, "resolve_clusters": result["clusters"]}
    if module == "decision":
        def site(c: dict) -> dict:
            return {"id": c["id"], "rank": c.get("rank"), "lon": round(c["lon"], 6), "lat": round(c["lat"], 6),
                    "score": c["score"], "nearest_existing_m": c["nearest_existing_m"],
                    "incremental_population": c.get("incremental_population"),
                    **{f"metric_{k}": v for k, v in c["metrics"].items()}}
        return {"decision_selected": [site(c) for c in result["selected"]],
                "decision_candidates": [site(c) for c in result["candidates"]]}
    if module == "coverage":  # plan_coverage output (optionally with an "eligibility" audit)
        ranks = {c["id"]: c["rank"] for c in result["selected"]}

        def coverage_site(c: dict) -> dict:
            return {"id": c["id"], "selected_rank": ranks.get(c["id"]), "lon": round(c["lon"], 6),
                    "lat": round(c["lat"], 6), "score": c["score"], "nearest_existing_m": c["nearest_existing_m"],
                    "incremental_demand_weight": c.get("incremental_demand_weight"),
                    "covered_demand_points": len(c["covered_demand_ids"]),
                    **{f"metric_{k}": v for k, v in c["metrics"].items()}}
        fields = ("key", "source", "source_id", "name", "category", "role", "metric", "status", "lon", "lat",
                  "inside_study_area", "demand_weight")
        views = {"coverage_selected": [coverage_site(c) for c in result["selected"]],
                 "coverage_candidates": [coverage_site(c) for c in result["candidates"]],
                 "coverage_inputs": [{k: row[k] for k in fields} for row in result["inputs"]]}
        if result.get("eligibility"):
            views["coverage_facility_eligibility"] = result["eligibility"]["audit"]
        return views
    raise ValueError(f"unknown module: {module}")


def export_result(module: str, result: dict, directory: str | Path, fmt: str = "both") -> list[Path]:
    """Write ``<module>.json`` and/or CSV tables into ``directory``; returns the written paths."""
    if fmt not in {"json", "csv", "both"}:
        raise ValueError("fmt must be json, csv or both")
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if fmt in {"json", "both"}:
        path = out / f"{module}.json"
        path.write_text(to_json(result), encoding="utf-8", newline="\n")
        written.append(path)
    if fmt in {"csv", "both"}:
        for name, rows in tabular_views(module, result).items():
            path = out / f"{name}.csv"
            path.write_text(to_csv(rows), encoding="utf-8", newline="\n")
            written.append(path)
    return written
