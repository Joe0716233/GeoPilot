"""Standalone offline demo of GeoVerify, GeoResolve and GeoDecision on the bundled London sample.

  python examples/run_demo.py --module all
  python examples/run_demo.py --module verify --export-dir out --format csv
  python examples/run_demo.py --module decision --target-count 5 --service-radius-m 1200

Requires only the installed ``geopilot`` package and the bundled files in examples/data/london.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from geopilot.decision import DecisionConfig
from geopilot.reporting import export_result
from geopilot.resolve import ResolveConfig
from geopilot.scenario import (SAMPLE_NOTICE, load_london_sample, run_decision, run_resolve,
                               run_verify)
from geopilot.verify import VerifyConfig


def print_verify(r: dict) -> None:
    s = r["summary"]
    print("\n== GeoVerify: borough claim check ==")
    print(f"records: {s['records']}   status counts: {s['status_counts']}")
    print(f"reasons: {s['reason_counts']}")
    for row in r["results"]:
        if row["status"] in {"FAIL", "REVIEW"} and row["record_id"].startswith("gla_chargers"):
            print(f"  {row['status']:<7} {row['record_id']:<18} claimed={row['claimed_zone']!r} "
                  f"actual={row['actual_zones']} signed_distance_m={row['signed_distance_m']} ({row['reason_code']})")
    print("note:", s["uncertainty_note"])


def print_resolve(r: dict) -> None:
    s = r["summary"]
    print("\n== GeoResolve: GLA historic chargers vs synthetic operator feed ==")
    print(f"input records: {s['input_records']}  candidate pairs: {s['candidate_pairs']}  "
          f"status counts: {s['status_counts']}")
    print(f"canonical records: {s['canonical_records']}  merged clusters: {s['merged_clusters']}  "
          f"clusters needing review: {s['clusters_needing_review']}")
    for pair in r["pairs"][:8]:
        print(f"  {pair['status']:<8} score={pair['score']:.3f} d={pair['distance_m']} m  "
              f"{pair['left']} <-> {pair['right']} {pair['review_reason'] or pair['fusion_reason'] or ''}")
    print("limits:", " ".join(r["limitations"]))


def print_decision(r: dict) -> None:
    print(f"\n== GeoDecision: site screening in {r['study_zone']} (synthetic demand) ==")
    print(f"status: {r['status']}  {r['message']}")
    print(f"candidates: {r['generated_candidate_count']} generated, {r['feasible_candidate_count']} feasible; "
          f"existing stations: {r['existing_station_count']}; demand points: {r['demand_point_count']}")
    for c in r["selected"]:
        print(f"  #{c['rank']} {c['id']} lon={c['lon']:.5f} lat={c['lat']:.5f} score={c['score']:.3f} "
              f"new_population={c['incremental_population']:.0f} nearest_existing_m={c['nearest_existing_m']}")
    pct = lambda v: "n/a" if v is None else f"{v:.1f}%"  # noqa: E731
    print(f"synthetic population covered within {r['coverage_radius_m']} m straight-line: "
          f"existing {pct(r['existing_coverage_pct'])} -> with new sites {pct(r['final_total_coverage_pct'])}")
    print("limits:", " ".join(r["limitations"]))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--module", choices=["verify", "resolve", "decision", "all"], default="all")
    p.add_argument("--data-dir", type=Path, help="override examples/data/london")
    p.add_argument("--export-dir", type=Path, help="write results here")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--review-boundary-m", type=float, default=20.0)
    p.add_argument("--fail-outside-m", type=float, default=50.0)
    p.add_argument("--match-score", type=float, default=0.78)
    p.add_argument("--review-score", type=float, default=0.55)
    p.add_argument("--target-count", type=int, default=3)
    p.add_argument("--service-radius-m", type=float, default=900)
    p.add_argument("--min-existing-distance-m", type=float, default=700)
    p.add_argument("--min-new-site-distance-m", type=float, default=600)
    p.add_argument("--grid-step-m", type=float, default=500)
    args = p.parse_args(argv)
    try:
        sample = load_london_sample(args.data_dir)
        modules = ["verify", "resolve", "decision"] if args.module == "all" else [args.module]
        results = {}
        if "verify" in modules:
            results["verify"] = run_verify(sample, VerifyConfig(args.review_boundary_m, args.fail_outside_m))
            print_verify(results["verify"])
        if "resolve" in modules:
            results["resolve"] = run_resolve(sample, ResolveConfig(args.match_score, args.review_score))
            print_resolve(results["resolve"])
        if "decision" in modules:
            results["decision"] = run_decision(sample, DecisionConfig(
                target_count=args.target_count, grid_step_m=args.grid_step_m,
                service_radius_m=args.service_radius_m,
                min_existing_distance_m=args.min_existing_distance_m,
                min_new_site_distance_m=args.min_new_site_distance_m))
            print_decision(results["decision"])
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("\n" + SAMPLE_NOTICE)
    if args.export_dir:
        for name, result in results.items():
            for path in export_result(name, result, args.export_dir, args.format):
                print("wrote", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
