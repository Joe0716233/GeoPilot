"""WP3: pure upload/workflow helpers (geopilot.workspace) on raw bytes."""

import hashlib
import json
import re

import pytest

from geopilot.reporting import to_json
from geopilot.tools.spatial import transform_xy
from geopilot.workspace import (EXPORT_COLUMNS, UPLOAD_ERROR_CODES, PointMapping, UploadError, UploadLimits,
                                ZoneMapping, describe_crs, export_rows, kind_from_filename, load_boundaries,
                                load_points, parse_upload, resolve_crs, run_assignment, run_metadata,
                                workflow_fingerprint)

POINTS_CSV = ("asset_ref,east_custom,north_custom,site_label,kind_raw,need_units\n"
              "orig-A,-0.095,51.505,inside west,toilets,10\n"
              "orig-B,-0.1,51.51,exact boundary,toilets,20\n"
              "orig-C,-0.085,51.51,overlap,toilets,30\n"
              "orig-D,-0.04,51.51,outside,toilets,40\n"
              "orig-E,,,no coordinate,toilets,50\n"
              "orig-F,-0.055,51.505,multipolygon island,toilets,60\n"
              "bad-xy,abc,51.51,invalid numeric,toilets,1\n"
              "dup,-0.095,51.505,duplicate1,toilets,1\n"
              "dup,-0.095,51.505,duplicate2,toilets,1\n"
              ",-0.095,51.505,missing id,toilets,1\n").encode("utf-8")
MAPPING = PointMapping(id="asset_ref", x="east_custom", y="north_custom", name="site_label", category="kind_raw",
                       demand_weight="need_units")


def ring(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


def feature(props, geometry):
    return {"type": "Feature", "properties": props, "geometry": geometry}


def collection(*features):
    return json.dumps({"type": "FeatureCollection", "features": list(features)}).encode("utf-8")


REGIONS = collection(
    feature({"region_code": "west", "region_label": "West region"},
            {"type": "Polygon", "coordinates": [ring(-0.1, 51.5, -0.08, 51.52)]}),
    feature({"region_code": "east", "region_label": "East and island"},
            {"type": "MultiPolygon", "coordinates": [[ring(-0.09, 51.5, -0.07, 51.52)],
                                                     [ring(-0.06, 51.5, -0.05, 51.51)]]}))
ZONE_MAPPING = ZoneMapping(id="region_code", name="region_label")


def points(data=POINTS_CSV, mapping=MAPPING, kind="csv", crs="EPSG:4326", label="council_toilets", **kw):
    return load_points(parse_upload(data, kind), mapping, crs=crs, crs_confirmed=True, source_label=label, **kw)


def zones(data=REGIONS, mapping=ZONE_MAPPING, crs="EPSG:4326", label="regions"):
    return load_boundaries(parse_upload(data, "geojson"), mapping, crs=crs, crs_confirmed=True, source_label=label)


def code_of(excinfo):
    assert isinstance(excinfo.value, ValueError) and excinfo.value.code in UPLOAD_ERROR_CODES
    return excinfo.value.code


def by_id(load):
    return {r["source_id"]: r for r in load.records}


# --- parsing ---

def test_csv_with_bom_and_arbitrary_headers():
    table = parse_upload(b"\xef\xbb\xbf" + POINTS_CSV, "csv")
    assert table.headers[0] == "asset_ref" and len(table.rows) == 10
    assert table.sha256 == hashlib.sha256(b"\xef\xbb\xbf" + POINTS_CSV).hexdigest()
    assert (kind_from_filename("Points.CSV"), kind_from_filename("a.geojson"), kind_from_filename("b.json")) == \
        ("csv", "geojson", "geojson")


@pytest.mark.parametrize("data, fragment", [
    (b"a,b,a\n1,2,3\n", "duplicate column headers: a"),
    (b"a,,c\n1,2,3\n", "blank column header in column 2"),
    (b"\xff\xfeA\x00", "not UTF-8"),
    (b"", "empty"),
    (b"  \n", "empty"),
    (b"a,b\n", "no data records"),
    (b'a,b\n"unterminated,1\n', "unreadable CSV"),
    (b'a,b\n"x"y,1\n', "unreadable CSV"),
])
def test_csv_file_level_errors(data, fragment):
    with pytest.raises(UploadError, match=fragment) as exc:
        parse_upload(data, "csv")
    assert code_of(exc) == "INVALID_FILE"


def test_size_and_record_bounds_are_checked_before_parsing():
    with pytest.raises(UploadError) as exc:
        parse_upload(POINTS_CSV, "csv", UploadLimits(max_file_bytes=100))
    assert code_of(exc) == "RESOURCE_LIMIT_EXCEEDED" and exc.value.context["quantity"] == "file bytes"
    with pytest.raises(UploadError) as exc:
        parse_upload(POINTS_CSV, "csv", UploadLimits(max_records=3))
    assert code_of(exc) == "RESOURCE_LIMIT_EXCEEDED" and exc.value.context["quantity"] == "records"
    with pytest.raises(UploadError) as exc:
        parse_upload(collection(*[feature({}, None)] * 4), "geojson", UploadLimits(max_records=3))
    assert code_of(exc) == "RESOURCE_LIMIT_EXCEEDED"
    for bad in ({"max_file_bytes": 10 ** 9}, {"max_pairs": 0}, {"max_records": True}):
        with pytest.raises(UploadError):
            UploadLimits(**bad)


def test_bad_kinds_and_inputs():
    for name in ("data.xlsx", "noext", None):
        with pytest.raises(UploadError) as exc:
            kind_from_filename(name)
        assert code_of(exc) == "INVALID_FILE"
    for data, kind in ((POINTS_CSV.decode(), "csv"), (POINTS_CSV, "xlsx")):
        with pytest.raises(UploadError):
            parse_upload(data, kind)


@pytest.mark.parametrize("data, fragment", [
    (b"{not json", "invalid JSON at line 1"),
    (b'{"type": "Feature"}', "must be a FeatureCollection"),
    (b'{"type": "FeatureCollection"}', "no 'features' list"),
    (collection(feature({" ": 1}, None)), "blank property name"),
    (collection(), "no data records"),
])
def test_geojson_file_level_errors(data, fragment):
    with pytest.raises(UploadError, match=fragment) as exc:
        parse_upload(data, "geojson")
    assert code_of(exc) == "INVALID_FILE"


@pytest.mark.parametrize("coordinates", [b"[NaN, 0]", b"[Infinity, 0]", b"[0, -Infinity]", b"[1e309, 0]",
                                         b"[-1e999, 51.5]"])
def test_non_standard_or_unrepresentable_json_numbers_are_refused(coordinates):
    data = (b'{"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"id": "a"}, '
            b'"geometry": {"type": "Point", "coordinates": ' + coordinates + b"}}]}")
    with pytest.raises(UploadError, match="invalid JSON") as exc:
        parse_upload(data, "geojson")
    assert code_of(exc) == "INVALID_FILE"


def test_overlong_json_integer_is_refused():
    data = b'{"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"n": ' + b"9" * 5000 + b"}}]}"
    with pytest.raises(UploadError, match="invalid JSON number") as exc:
        parse_upload(data, "geojson")
    assert code_of(exc) == "INVALID_FILE"


def test_unterminated_quote_in_final_numeric_cell_is_refused():
    with pytest.raises(UploadError, match="unreadable CSV") as exc:
        parse_upload(b'id,x,y\na,0,"0', "csv")
    assert code_of(exc) == "INVALID_FILE"


def test_rejected_geojson_rows_export_without_crashing():
    data = collection(feature({"id": "ok"}, {"type": "Point", "coordinates": [-0.095, 51.505]}),
                      feature({"id": "big"}, {"type": "Point", "coordinates": [10 ** 400, 0]}),
                      feature({"id": "flag"}, {"type": "Point", "coordinates": [False, 0]}))
    run = run_assignment(points(data, PointMapping(id="id"), kind="geojson"), zones())
    from geopilot.reporting import to_csv
    text = to_csv(export_rows(run))
    assert "NON_FINITE_COORDINATE" in text and "BOOLEAN_COORDINATE" in text
    to_json(run_metadata(points(data, PointMapping(id="id"), kind="geojson"), zones(), run))


def test_upload_cap_matches_server_config():
    import tomllib
    from pathlib import Path
    from geopilot.workspace import MAX_UPLOAD_MB
    config = tomllib.loads((Path(__file__).resolve().parents[1] / ".streamlit" / "config.toml").read_text("utf-8"))
    assert config["server"]["maxUploadSize"] == MAX_UPLOAD_MB
    assert UploadLimits().max_file_bytes == MAX_UPLOAD_MB * 1024 * 1024
    cap = MAX_UPLOAD_MB * 1024 * 1024
    row = b"1," + b"x" * 297 + b"\n"                                     # 300 bytes
    body = b"a,b\n" + row * ((cap - 4) // len(row))
    assert len(parse_upload(body, "csv").rows) == (cap - 4) // len(row)   # just under the cap: parsed
    with pytest.raises(UploadError) as exc:
        parse_upload(b"x" * (cap + 1), "csv")                             # one byte over: refused unread
    assert code_of(exc) == "RESOURCE_LIMIT_EXCEEDED"


def test_raw_ids_keep_leading_zeros():
    load = points(b"id,x,y\n007,0,0\n7,1,1\n", PointMapping(id="id", x="x", y="y"))
    assert [(r["source_id"], r["original_id"], r["status"]) for r in load.records] == [
        ("007", "007", "LOADED"), ("7", "7", "LOADED")]
    assert load.entities[0].key == "council_toilets:007"


def test_deeply_nested_json_is_refused():
    with pytest.raises(UploadError, match="nested too deeply") as exc:
        parse_upload(b"[" * 100_000 + b"]" * 100_000, "geojson")
    assert code_of(exc) == "INVALID_FILE"


# --- point mapping and rows ---

@pytest.mark.parametrize("mapping, kind, fragment", [
    (PointMapping(x="east_custom", y="nope"), "csv", "not found in the file: y -> 'nope'"),
    (PointMapping(x="east_custom"), "csv", "needs both an x"),
    (PointMapping(x="east_custom", y="east_custom"), "csv", "both mapped to 'east_custom'"),
    (PointMapping(x="name"), "geojson", "leave x and y unmapped"),
])
def test_mapping_validation(mapping, kind, fragment):
    data = POINTS_CSV if kind == "csv" else collection(feature({"name": "a"}, None))
    with pytest.raises(UploadError, match=fragment) as exc:
        load_points(parse_upload(data, kind), mapping, crs="EPSG:4326", crs_confirmed=True, source_label="s")
    assert code_of(exc) == "INVALID_MAPPING"


def test_acceptance_style_rows_are_loaded_or_rejected_with_reasons():
    load = points()
    rows = by_id(load)
    assert load.summary == {"original": 10, "loaded": 7, "rejected": 3, "missing_coordinate": 1, "missing_id": 1,
                            "duplicate_id": 2, "participating": 6, "blank_lines_skipped": 0,
                            "reason_counts": {"DUPLICATE_ID": 2, "NON_NUMERIC_COORDINATE": 1}}
    assert rows["bad-xy"]["reason_code"] == "NON_NUMERIC_COORDINATE" and rows["bad-xy"]["original_x"] == "abc"
    assert [r["reason_code"] for r in load.records if r["source_id"] == "dup"] == ["DUPLICATE_ID"] * 2
    assert rows["orig-E"]["status"] == "LOADED" and not rows["orig-E"]["has_coordinate"]
    generated = rows["generated-row-10"]
    assert generated["id_generated"] and generated["original_id"] == "" and generated["status"] == "LOADED"
    assert [e.source_id for e in load.entities] == ["orig-A", "orig-B", "orig-C", "orig-D", "orig-E", "orig-F",
                                                    "generated-row-10"]


def test_optional_fields_are_absent_unless_mapped():
    data = b"id,x,y,name,zone\n1,0.5,0.5,Kiosk,Z1\n"
    plain = points(data, PointMapping(id="id", x="x", y="y"))
    assert plain.entities[0].name == "" and plain.claims == {}
    assert plain.entities[0].properties == {"id": "1", "x": "0.5", "y": "0.5", "name": "Kiosk", "zone": "Z1"}
    mapped = points(data, PointMapping(id="id", x="x", y="y", name="name", claimed_zone="zone"))
    assert mapped.entities[0].name == "Kiosk" and mapped.claims == {"council_toilets:1": "Z1"}
    nothing = points(data, PointMapping(x="x", y="y"))         # no ID column mapped: every ID is generated
    assert nothing.records[0]["id_generated"] and nothing.summary["missing_id"] == 1


def test_generated_ids_never_collide_with_original_ids():
    data = b"id,x,y\ngenerated-row-2,0,0\n,1,1\n"
    load = points(data, PointMapping(id="id", x="x", y="y"))
    assert [r["source_id"] for r in load.records] == ["generated-row-2", "_generated-row-2"]
    assert [r["id_generated"] for r in load.records] == [False, True]
    assert load.entities[1].provenance[0].source_id == "_generated-row-2"


def test_duplicates_compare_trimmed_ids_and_keep_raw_values():
    load = points(b"id,x,y\n A ,0,0\nA,1,1\nB,2,2\n", PointMapping(id="id", x="x", y="y"))
    assert [(r["original_id"], r["status"]) for r in load.records] == [(" A ", "REJECTED"), ("A", "REJECTED"),
                                                                      ("B", "LOADED")]
    assert load.summary["duplicate_id"] == 2


@pytest.mark.parametrize("x, y, reason", [
    ("1", "", "ONE_SIDED_COORDINATE"), ("", "1", "ONE_SIDED_COORDINATE"), ("one", "1", "NON_NUMERIC_COORDINATE"),
    ("nan", "1", "NON_FINITE_COORDINATE"), ("1", "inf", "NON_FINITE_COORDINATE"), ("500", "1", "COORDINATE_OUT_OF_RANGE"),
    ("1", "-91", "COORDINATE_OUT_OF_RANGE"), ("true", "1", "NON_NUMERIC_COORDINATE"),
])
def test_csv_coordinate_problems(x, y, reason):
    load = points(f"id,x,y\nr,{x},{y}\n".encode(), PointMapping(id="id", x="x", y="y"))
    record = load.records[0]
    assert (record["status"], record["reason_code"]) == ("REJECTED", reason) and record["reason"]
    assert load.entities == ()


def test_width_mismatch_rows_are_rejected_not_dropped():
    load = points(b"id,x,y\nA,0,0\nB,1,1,extra\nC,2\n", PointMapping(id="id", x="x", y="y"))
    assert [(r["source_id"], r["reason_code"]) for r in load.records] == [
        ("A", ""), ("B", "ROW_WIDTH_MISMATCH"), ("C", "ROW_WIDTH_MISMATCH")]
    assert "expected 3 fields, found 4" in load.records[1]["reason"]


def test_geojson_points():
    data = collection(
        feature({"ref": "ok"}, {"type": "Point", "coordinates": [0.5, 0.5]}),
        feature({"ref": "null"}, None),
        feature({"ref": "bool"}, {"type": "Point", "coordinates": [True, 0.5]}),
        feature({"ref": "line"}, {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}),
        feature({"ref": "short"}, {"type": "Point", "coordinates": [1]}),
        feature({"ref": "nullx"}, {"type": "Point", "coordinates": [None, 1]}),
        feature({"ref": "text"}, {"type": "Point", "coordinates": ["1", "1"]}),
        feature({"ref": "huge"}, {"type": "Point", "coordinates": [10 ** 400, 1]}),   # valid JSON integer
        feature({"ref": 7}, {"type": "Mystery"}),
        "not a feature",
        {"type": "Feature", "properties": [1], "geometry": None})
    load = points(data, PointMapping(id="ref"), kind="geojson")
    reasons = {r["source_id"]: r["reason_code"] for r in load.records}
    assert reasons == {"ok": "", "null": "", "bool": "BOOLEAN_COORDINATE", "line": "UNSUPPORTED_GEOMETRY_TYPE",
                       "short": "INVALID_GEOMETRY", "nullx": "INVALID_GEOMETRY", "text": "NON_NUMERIC_COORDINATE",
                       "huge": "NON_FINITE_COORDINATE", "7": "UNSUPPORTED_GEOMETRY_TYPE",
                       "generated-row-10": "NOT_A_FEATURE", "generated-row-11": "INVALID_PROPERTIES"}
    assert by_id(load)["null"]["status"] == "LOADED" and not by_id(load)["null"]["has_coordinate"]
    assert by_id(load)["7"]["original_id"] == 7


def test_source_label_and_provenance():
    load = points(label="  council_toilets  ")
    entity = load.entities[0]
    assert (entity.source, entity.key) == ("council_toilets", "council_toilets:orig-A")
    assert entity.provenance[0].model_dump() == {"source": "council_toilets", "source_id": "orig-A",
                                                 "retrieved_at": None, "license_note": None}
    assert entity.properties["need_units"] == "10"         # whole original row kept
    for label in ("", "   ", "x" * 81, None):
        with pytest.raises(UploadError) as exc:
            points(label=label)
        assert code_of(exc) == "INVALID_SOURCE_LABEL"


# --- CRS ---

def test_explicit_crs_conversion_and_confirmation():
    lon, lat = -0.095, 51.505
    e, n = transform_xy(lon, lat, "EPSG:4326", "EPSG:27700")
    load = points(f"id,e,n\nA,{e},{n}\n".encode(), PointMapping(id="id", x="e", y="n"), crs="epsg:27700")
    record = load.records[0]
    assert record["input_crs"] == "EPSG:27700" and record["original_x"] == str(e)
    assert (record["lon"], record["lat"]) == pytest.approx((lon, lat), abs=1e-7)
    assert describe_crs(" EPSG:27700 ") == ("EPSG:27700", "OSGB36 / British National Grid")
    with pytest.raises(UploadError) as exc:
        load_points(parse_upload(POINTS_CSV, "csv"), MAPPING, crs="EPSG:4326", crs_confirmed=False, source_label="s")
    assert code_of(exc) == "CRS_NOT_CONFIRMED"
    assert resolve_crs("EPSG:4326", True) == "EPSG:4326"
    with pytest.raises(UploadError):
        resolve_crs("EPSG:4326", "yes")                      # only an explicit True confirms


@pytest.mark.parametrize("value", ["GCJ-02", "gcj02", "GCJ_02", "BD-09", "bd09ll", "BD09MC", "Baidu", "Mars"])
def test_gcj02_and_bd09_are_refused_by_name(value):
    with pytest.raises(UploadError) as exc:
        resolve_crs(value, True)
    assert code_of(exc) == "UNSUPPORTED_CRS" and "will not guess" in exc.value.detail


@pytest.mark.parametrize("value", ["WGS84", "+proj=longlat +datum=WGS84", "EPSG:999999", "EPSG:5701", "EPSG:4979",
                                   "/etc/passwd", "", None, "EPSG:4326x"])
def test_invalid_crs_values(value):
    with pytest.raises(UploadError) as exc:
        describe_crs(value)
    assert code_of(exc) == "INVALID_CRS"


# --- boundaries ---

def test_boundaries_with_custom_properties_holes_and_islands():
    holed = collection(feature({"code": "H", "label": "Holed"},
                               {"type": "Polygon", "coordinates": [ring(0, 0, 1, 1), ring(0.4, 0.4, 0.6, 0.6)]}))
    load = zones(holed, ZoneMapping(id="code", name="label"))
    assert load.zones[0].shape.interiors and load.zones[0].name == "Holed"
    regions = zones()
    assert [(z.zone_id, z.name, z.shape.geom_type) for z in regions.zones] == [
        ("west", "West region", "Polygon"), ("east", "East and island", "MultiPolygon")]
    assert regions.summary == {"zones": 2, "total_vertices": 15}
    assert regions.zones[0].properties == {"region_code": "west", "region_label": "West region"}
    unnamed = zones(mapping=ZoneMapping(id="region_code"))
    assert unnamed.zones[0].name == "west"


def test_boundary_crs_conversion():
    bng = [list(transform_xy(x, y, "EPSG:4326", "EPSG:27700")) for x, y in ring(-0.1, 51.5, -0.08, 51.52)]
    load = zones(collection(feature({"code": "W"}, {"type": "Polygon", "coordinates": [bng]})),
                 ZoneMapping(id="code"), crs="EPSG:27700")
    assert load.crs == "EPSG:27700"
    assert load.zones[0].shape.bounds == pytest.approx((-0.1, 51.5, -0.08, 51.52), abs=1e-7)


@pytest.mark.parametrize("data, mapping, code, fragment", [
    (POINTS_CSV, ZONE_MAPPING, "INVALID_FILE", "must be a GeoJSON"),
    (REGIONS, ZoneMapping(), "INVALID_MAPPING", "zone ID"),
    (REGIONS, ZoneMapping(id="code"), "INVALID_MAPPING", "not found: id -> 'code'"),
    (collection(feature({"c": "A"}, {"type": "Polygon", "coordinates": [ring(0, 0, 1, 1)]}),
                feature({"c": "A"}, {"type": "Polygon", "coordinates": [ring(2, 0, 3, 1)]})),
     ZoneMapping(id="c"), "INVALID_ZONES", "zone ID 'A' appears 2 times"),
    (collection(feature({"c": " "}, {"type": "Polygon", "coordinates": [ring(0, 0, 1, 1)]})),
     ZoneMapping(id="c"), "INVALID_ZONES", "blank zone ID"),
    (collection(feature({"c": "P"}, {"type": "Point", "coordinates": [0, 0]})),
     ZoneMapping(id="c"), "INVALID_ZONES", "'Point' is not Polygon or MultiPolygon"),
    (collection(feature({"c": "N"}, None)), ZoneMapping(id="c"), "INVALID_ZONES", "None is not Polygon"),
    (collection(feature({"c": "B"}, {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [True, 1], [0, 0]]]})),
     ZoneMapping(id="c"), "INVALID_ZONES", "boolean coordinate"),
    (collection(feature({"c": "X"}, {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]})),
     ZoneMapping(id="c"), "INVALID_ZONES", "valid, non-empty Polygon"),
    (collection(feature({"c": "S"}, {"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]})),
     ZoneMapping(id="c"), "INVALID_ZONES", "feature 1 (S): A linearring requires at least 4 coordinates. Fix"),
])
def test_boundary_errors_refuse_the_layer(data, mapping, code, fragment):
    kind = "csv" if data is POINTS_CSV else "geojson"
    with pytest.raises(UploadError, match=re.escape(fragment)) as exc:
        load_boundaries(parse_upload(data, kind), mapping, crs="EPSG:4326", crs_confirmed=True, source_label="z")
    assert code_of(exc) == code


def test_boundary_vertex_and_zone_bounds():
    table = parse_upload(REGIONS, "geojson")
    for limits, quantity in ((UploadLimits(max_zone_vertices=4), "vertices of zone west"),
                             (UploadLimits(max_total_zone_vertices=9), "total zone vertices"),
                             (UploadLimits(max_zones=1), "zones")):
        with pytest.raises(UploadError) as exc:
            load_boundaries(table, ZONE_MAPPING, crs="EPSG:4326", crs_confirmed=True, source_label="z", limits=limits)
        assert code_of(exc) == "RESOURCE_LIMIT_EXCEEDED" and exc.value.context["quantity"] == quantity


# --- assignment, exports, fingerprint ---

def test_end_to_end_assignment_keeps_rejected_rows_in_export():
    p, z = points(), zones()
    run = run_assignment(p, z)
    statuses = {r["source_id"]: r["status"] for r in run["rows"]}
    assert statuses == {"orig-A": "ASSIGNED", "orig-B": "ON_BOUNDARY", "orig-C": "MULTIPLE_MATCHES",
                        "orig-D": "OUTSIDE_ALL_ZONES", "orig-E": "NO_COORDINATE", "orig-F": "ASSIGNED",
                        "bad-xy": "REJECTED", "dup": "REJECTED", "generated-row-10": "ASSIGNED"}
    rows = export_rows(run)
    assert len(rows) == 10 and tuple(rows[0]) == EXPORT_COLUMNS
    overlap = next(r for r in rows if r["source_id"] == "orig-C")
    assert (overlap["zone_ids"], overlap["zone_names"], overlap["relations"]) == (
        "east;west", "East and island;West region", "INSIDE;INSIDE")
    bad = next(r for r in rows if r["source_id"] == "bad-xy")
    assert (bad["status"], bad["load_status"], bad["reason_code"], bad["original_x"]) == (
        "REJECTED", "REJECTED", "NON_NUMERIC_COORDINATE", "abc")
    island = next(r for r in rows if r["source_id"] == "orig-F")
    assert (island["assigned_zone_id"], island["original_x"], island["input_crs"]) == ("east", "-0.055", "EPSG:4326")
    assert run["summary"]["status_counts"]["REJECTED"] == 3


def test_run_metadata_records_digests_crs_and_mapping_without_filenames():
    p, z = points(), zones()
    meta = run_metadata(p, z, run_assignment(p, z))
    text = to_json(meta)
    assert meta["points"]["sha256"] == hashlib.sha256(POINTS_CSV).hexdigest()
    assert meta["points"]["input_crs"] == "EPSG:4326" and meta["points"]["mapping"]["x"] == "east_custom"
    assert meta["boundaries"]["mapping"] == {"id": "region_code", "name": "region_label"}
    assert meta["points"]["quality"]["rejected"] == 3 and meta["fingerprint"] == workflow_fingerprint(p, z)
    assert ".csv" not in text and ".geojson" not in text


def test_fingerprint_tracks_every_relevant_input():
    base = workflow_fingerprint(points(), zones())
    assert workflow_fingerprint(points(), zones()) == base
    variants = [workflow_fingerprint(points(POINTS_CSV + b"extra,1,1,,,\n"), zones()),
                workflow_fingerprint(points(mapping=PointMapping(id="asset_ref", x="east_custom", y="north_custom")),
                                     zones()),
                workflow_fingerprint(points(crs="EPSG:4258"), zones()),
                workflow_fingerprint(points(label="other"), zones()),
                workflow_fingerprint(points(), zones(mapping=ZoneMapping(id="region_code"))),
                workflow_fingerprint(points(), zones(label="other")),
                workflow_fingerprint(points(), zones(), UploadLimits(max_pairs=100))]
    assert base not in variants and len(set(variants)) == len(variants)


COVERAGE_CSV = (b"ref,east,north,kind,need\nT1,-0.095,51.505,toilet,\nH1,-0.094,51.506,household,120\n"
                b"H2,-0.085,51.51,household,80\nH3,-0.075,51.515,household,50\nB1,-0.096,51.507,bus stop,\n"
                b"X1,abc,51.5,household,5\nN1,,,household,7\n")
COVERAGE_MAPPING = PointMapping(id="ref", x="east", y="north", category="kind", demand_weight="need")


def coverage_config(**kw):
    from geopilot.decision import RoleMapping, ScenarioConfig
    roles = RoleMapping({"toilet": "existing_facility", "household": "weighted_demand", "bus stop": "context"},
                        context_metrics={"bus stop": "bus_stop"})
    base = {"roles": roles, "demand_weight_field": "need", "weights": {"demand_weight": 1.0, "bus_stop": 0.0},
            "target_count": 1, "grid_step_m": 250, "service_radius_m": 500, "min_existing_distance_m": 0}
    return ScenarioConfig(**{**base, **kw})


def test_coverage_on_uploads_requires_acknowledged_exclusions():
    from geopilot.workspace import coverage_exclusions, run_coverage
    p, z = points(COVERAGE_CSV, COVERAGE_MAPPING), zones()
    excluded = coverage_exclusions(p)
    assert [(r["source_id"], r["reason_code"]) for r in excluded] == [("X1", "NON_NUMERIC_COORDINATE"),
                                                                     ("N1", "NO_COORDINATE")]
    with pytest.raises(UploadError) as exc:
        run_coverage(p, z, "west", coverage_config(), acknowledge_exclusions=False)
    assert code_of(exc) == "UNACKNOWLEDGED_EXCLUSIONS" and exc.value.context["rows"] == 2
    result = run_coverage(p, z, "west", coverage_config(), acknowledge_exclusions=True)
    assert result["status"] == "SUCCESS" and result["existing_facility_count"] == 1
    assert result["demand_point_count"] == 2 and result["demand_points_outside_study_area"] == 1   # H3 is in east only
    assert result["total_demand_weight"] == 200 and result["existing_demand_weight_covered"] == 120
    assert result["upload"]["excluded_rows"] == excluded and result["upload"]["exclusions_acknowledged"]
    assert {row["category"] for row in result["inputs"]} == {"toilet", "household", "bus stop"}
    to_json(result)
    with pytest.raises(UploadError) as exc:
        run_coverage(p, z, "north", coverage_config(), acknowledge_exclusions=True)
    assert code_of(exc) == "INVALID_ZONES"


def test_coverage_on_uploads_keeps_plan_coverage_refusals():
    from geopilot.decision import DecisionInputError, RoleMapping
    from geopilot.workspace import run_coverage
    p, z = points(COVERAGE_CSV, COVERAGE_MAPPING), zones()
    partial = coverage_config(roles=RoleMapping({"toilet": "existing_facility", "household": "weighted_demand"}),
                              weights={"demand_weight": 1.0})
    with pytest.raises(DecisionInputError) as exc:
        run_coverage(p, z, "west", partial, acknowledge_exclusions=True)
    assert exc.value.code == "UNMAPPED_ROLE" and exc.value.context["categories"] == ["bus stop"]
    with pytest.raises(DecisionInputError) as exc:
        run_coverage(p, z, "west", coverage_config(method="p_median"), acknowledge_exclusions=True)
    assert exc.value.code == "INVALID_SCENARIO_CONFIG"


def test_coverage_fingerprint_tracks_every_parameter():
    from geopilot.workspace import coverage_fingerprint
    p, z = points(COVERAGE_CSV, COVERAGE_MAPPING), zones()
    base = coverage_fingerprint(p, z, "west", coverage_config(), True)
    assert coverage_fingerprint(p, z, "west", coverage_config(), True) == base
    variants = [coverage_fingerprint(p, z, "east", coverage_config(), True),
                coverage_fingerprint(p, z, "west", coverage_config(), False),
                coverage_fingerprint(p, z, "west", coverage_config(weights={"demand_weight": 1.0, "bus_stop": 0.5}), True),
                coverage_fingerprint(p, z, "west", coverage_config(service_radius_m=600), True),
                coverage_fingerprint(p, z, "west", coverage_config(target_count=2), True),
                coverage_fingerprint(p, z, "west", coverage_config(grid_step_m=300), True),
                coverage_fingerprint(p, z, "west", coverage_config(min_existing_distance_m=10), True),
                coverage_fingerprint(p, z, "west", coverage_config(min_new_site_distance_m=10), True),
                coverage_fingerprint(points(COVERAGE_CSV, COVERAGE_MAPPING, label="other"), z, "west",
                                     coverage_config(), True)]
    assert base not in variants and len(set(variants)) == len(variants)


def test_assignment_pair_budget():
    with pytest.raises(UploadError) as exc:
        run_assignment(points(), zones(), UploadLimits(max_pairs=5))
    assert code_of(exc) == "RESOURCE_LIMIT_EXCEEDED" and exc.value.context["quantity"] == "record x zone pairs"
