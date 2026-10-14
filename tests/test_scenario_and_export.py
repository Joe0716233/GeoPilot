import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from geopilot.decision import DecisionConfig
from geopilot.reporting import export_result, to_csv
from geopilot.scenario import load_london_sample, run_all, run_decision, run_resolve, run_verify
from geopilot.verify import VerifyConfig

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "examples" / "data" / "london"


def test_manifest_hashes_match_bundled_files():
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    for name, info in manifest["outputs"].items():
        raw = (DATA / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == info["sha256"], name
        assert len(raw) == info["bytes"]
    assert manifest["real_inputs"]["boroughs"]["licence"] == "OGL v3.0"
    assert "version not stated" in manifest["real_inputs"]["chargers"]["licence"]


def test_sample_contents(sample):
    assert len(sample.zones) == 9 and sample.study_zone_id == "Southwark"
    assert len(sample.chargers) == 42 and len(sample.probes) >= 60 and len(sample.feed) == 28
    assert all(z.shape.is_valid for z in sample.zones)
    assert not any(e.name == "" for e in sample.chargers)


def test_synthetic_inputs_are_reproducible_from_bundled_real_inputs(sample):
    import prepare_data as pd_script
    by_name = {z.zone_id: {"geometry": z.geometry} for z in sample.zones}
    with (DATA / "synthetic_boundary_probes.csv").open(encoding="utf-8", newline="") as h:
        assert pd_script.build_probes(by_name) == list(csv.DictReader(h))
    with (DATA / "gla_chargers_subset.csv").open(encoding="utf-8", newline="") as h:
        chargers = [{**r, "lon": float(r["lon"]), "lat": float(r["lat"])} for r in csv.DictReader(h)]
    with (DATA / "synthetic_operator_feed.csv").open(encoding="utf-8", newline="") as h:
        assert pd_script.build_feed(chargers) == list(csv.DictReader(h))
    with (DATA / "synthetic_demand.csv").open(encoding="utf-8", newline="") as h:
        assert pd_script.build_demand(sample.study_zone.shape) == list(csv.DictReader(h))


def test_probe_statuses_follow_their_intended_offsets(sample):
    results = {r["record_id"]: r for r in run_verify(sample)["results"]}
    probes = {e.key: float(e.properties["intended_offset_m"]) for e in sample.probes}
    inside_far = [k for k, off in probes.items() if off == 100.0]
    assert sum(results[k]["status"] == "PASS" for k in inside_far) >= len(inside_far) * 0.8
    for key, off in probes.items():
        r = results[key]
        if off < 0:
            assert r["signed_distance_m"] is not None and r["signed_distance_m"] < 0
            assert abs(r["signed_distance_m"]) <= abs(off) + 1.0  # nearest edge can only be closer
            assert r["status"] != "PASS"
        else:
            assert r["signed_distance_m"] > 0 and r["signed_distance_m"] <= off + 1.0


def test_run_all_is_json_serialisable_and_deterministic(sample):
    first = json.dumps(run_all(sample), sort_keys=True, default=str)
    second = json.dumps(run_all(load_london_sample()), sort_keys=True, default=str)
    assert first == second


def test_resolve_on_sample_never_merges_same_source(sample):
    r = run_resolve(sample)
    for cluster in r["clusters"]:
        sources = [k.split(":")[0] for k in cluster["member_keys"]]
        assert len(sources) == len(set(sources))
    assert r["summary"]["status_counts"]["MATCH"] > 0
    # Designed decoys ("different place ~10 m away") must not be auto-matched.
    decoys = {e.key for e in sample.feed if "different place" in e.properties["construction_note"]}
    matched = {k for p in r["pairs"] if p["status"] == "MATCH" for k in (p["left"], p["right"])}
    assert decoys and not decoys & matched


def test_decision_on_sample_has_consistent_totals(sample):
    d = run_decision(sample)
    assert d["status"] == "SUCCESS" and d["actual_count"] == 3
    assert d["final_total_population_covered"] == pytest.approx(
        d["existing_population_covered"] + d["new_incremental_population_covered"])
    assert d["final_total_population_covered"] <= d["total_population"]
    assert sum(s["incremental_population"] for s in d["selected"]) == pytest.approx(
        d["new_incremental_population_covered"])
    bigger = run_decision(sample, DecisionConfig(target_count=5))
    assert bigger["final_total_population_covered"] >= d["final_total_population_covered"] - 1e-9


def test_export_json_and_csv(tmp_path, sample):
    result = run_verify(sample, VerifyConfig(25, 60))
    paths = export_result("verify", result, tmp_path, "both")
    assert {p.name for p in paths} == {"verify.json", "verify_results.csv"}
    assert json.loads((tmp_path / "verify.json").read_text(encoding="utf-8"))["parameters"]["review_boundary_m"] == 25
    with (tmp_path / "verify_results.csv").open(encoding="utf-8", newline="") as h:
        rows = list(csv.DictReader(h))
    assert len(rows) == len(sample.chargers) + len(sample.probes) and "signed_distance_m" in rows[0]
    with pytest.raises(ValueError):
        export_result("verify", result, tmp_path, "xml")


def test_csv_cell_encoding():
    text = to_csv([{"a": [1, 2], "b": {"k": 1}, "c": None}, {"a": "x", "d": 3}])
    rows = list(csv.DictReader(text.splitlines()))
    assert rows[0]["a"] == "1;2" and rows[0]["b"] == '{"k": 1}' and rows[0]["c"] == "" and rows[1]["d"] == "3"


def test_demo_cli_runs_and_exports(tmp_path):
    cmd = [sys.executable, str(ROOT / "examples" / "run_demo.py"), "--module", "all",
           "--export-dir", str(tmp_path), "--format", "both"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert "GeoVerify" in out.stdout and "GeoResolve" in out.stdout and "GeoDecision" in out.stdout
    assert "not evidence about current London charging provision" in out.stdout
    assert (tmp_path / "decision_selected.csv").is_file() and (tmp_path / "resolve.json").is_file()


def test_demo_cli_single_module_and_bad_parameter(tmp_path):
    one = subprocess.run([sys.executable, str(ROOT / "examples" / "run_demo.py"), "--module", "verify"],
                         capture_output=True, text=True)
    assert one.returncode == 0 and "GeoResolve" not in one.stdout
    bad = subprocess.run([sys.executable, str(ROOT / "examples" / "run_demo.py"), "--module", "decision",
                          "--target-count", "0"], capture_output=True, text=True)
    assert bad.returncode == 2 and "target_count" in bad.stderr


def test_missing_data_dir_message(tmp_path):
    with pytest.raises(FileNotFoundError, match="GEOPILOT_DATA_DIR"):
        load_london_sample(tmp_path)


def _app():
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=120).run()


def _metrics(at):
    return {m.label: m.value for m in at.metric}


# Numerical defaults must be identical in both languages; only labels differ.
ZH_DEFAULTS = {"通过 PASS": "59", "待复核 REVIEW": "18", "不一致 FAIL": "16", "无法判定 UNRESOLVED": "0",
               "候选配对": "40", "强候选 MATCH": "14", "不匹配 NO_MATCH": "8",
               "现有站点覆盖（合成人口）": "13.7 %", "新增站点带来的覆盖": "31.1 %", "总覆盖": "44.7 %"}
EN_DEFAULTS = {"PASS": "59", "FAIL": "16", "UNRESOLVED": "0", "Candidate pairs": "40", "MATCH": "14",
               "NO_MATCH": "8", "Covered by existing (synthetic pop.)": "13.7 %",
               "Added by new sites": "31.1 %", "Total covered": "44.7 %"}


def test_streamlit_app_defaults_to_chinese_with_unchanged_numbers():
    at = _app()
    assert not at.exception and not at.error
    assert at.selectbox(key="lang").value == "zh"
    metrics = _metrics(at)
    for label, value in ZH_DEFAULTS.items():
        if label == "待复核 REVIEW":
            continue  # GeoVerify (41) and GeoResolve (18) share this label; checked below
        assert metrics[label] == value, label
    review_values = sorted(m.value for m in at.metric if m.label == "待复核 REVIEW")
    assert review_values == ["18", "41"]
    page = " ".join(w.value for w in at.warning) + " ".join(m.value for m in at.markdown)
    assert "合成数据" in page and "不代表" in page
    assert at.title[0].value.startswith("GeoPilot v1.0")


def test_streamlit_app_language_switch_keeps_numbers_and_parameters():
    at = _app()
    at.selectbox(key="lang").set_value("en").run()
    assert not at.exception and not at.error
    metrics = _metrics(at)
    for label, value in EN_DEFAULTS.items():
        assert metrics[label] == value, label
    assert sorted(m.value for m in at.metric if m.label == "REVIEW") == ["18", "41"]
    assert any("synthetic" in w.value.lower() for w in at.warning)
    # Parameters survive switching language (stable widget keys) and results follow them.
    at.number_input(key="target").set_value(5).run()
    at.selectbox(key="lang").set_value("zh").run()
    assert not at.exception and at.number_input(key="target").value == 5
    assert _metrics(at)["总覆盖"] == "64.0 %"
    at.number_input(key="target").set_value(3).run()
    assert _metrics(at)["总覆盖"] == "44.7 %"


def test_streamlit_app_partial_message_is_localised_and_conservative():
    at = _app()
    at.number_input(key="min_exist").set_value(5000.0).run()
    assert not at.exception
    text = " ".join(w.value for w in at.warning)
    assert "实际选出" in text and "并不表示数学上不存在可行方案" in text
    at.selectbox(key="lang").set_value("en").run()
    assert any("no infeasibility claim" in w.value for w in at.warning)

# --- prepare_data provenance and offline regeneration (review repairs) ---

def _copy_bundle(tmp_path):
    import shutil
    target = tmp_path / "london"
    shutil.copytree(DATA, target)
    return target


def test_synthetic_only_regenerates_identically_offline(tmp_path):
    import prepare_data
    target = _copy_bundle(tmp_path)
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    (target / "synthetic_demand.csv").write_text("corrupted\n", encoding="utf-8")
    (target / "unrelated_notes.txt").write_text("not an output", encoding="utf-8")
    assert prepare_data.main(["--synthetic-only", "--out", str(target)]) == 0
    for name, raw in before.items():
        assert (target / name).read_bytes() == raw, name
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert "unrelated_notes.txt" not in manifest["outputs"]
    assert manifest["real_inputs"]["boroughs"]["retrieved_at"] == "2026-09-27"
    assert manifest["real_inputs"]["chargers"]["retrieved_at"] == "2026-09-27"


def test_synthetic_only_refuses_modified_real_subset(tmp_path):
    import prepare_data
    target = _copy_bundle(tmp_path)
    with (target / "gla_chargers_subset.csv").open("a", encoding="utf-8") as handle:
        handle.write("999,x,x,existing_station,0,0,London,Camden,1\n")
    with pytest.raises(SystemExit, match="does not match manifest"):
        prepare_data.main(["--synthetic-only", "--out", str(target)])


def test_supplied_source_files_require_explicit_retrieval_date(tmp_path):
    import prepare_data
    (tmp_path / "b.geojson").write_text("{}", encoding="utf-8")
    (tmp_path / "c.gpkg").write_bytes(b"")
    with pytest.raises(SystemExit):
        prepare_data.main(["--boroughs", str(tmp_path / "b.geojson"), "--chargers", str(tmp_path / "c.gpkg"),
                           "--out", str(tmp_path / "o")])
    with pytest.raises(SystemExit):
        prepare_data.main(["--synthetic-only", "--retrieved-at", "27/09/2026", "--out", str(tmp_path)])
