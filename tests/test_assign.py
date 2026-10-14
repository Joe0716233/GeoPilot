"""WP2: claim-free zone assignment (geopilot.verify.assign_zones)."""

import json
import pickle

import pytest

from geopilot.data import Entity, Zone, load_entities, load_zones
from geopilot.data.schema import Provenance
from geopilot.tools.spatial import transform_xy
from geopilot.verify import (ASSIGN_ERROR_CODES, ASSIGN_STATUSES, AssignConfig, AssignInputError, assign_zones,
                             verify_batch)

from conftest import LAT0, LON0, SIDE_DEG, point, square

MID = LON0 + SIDE_DEG / 2, LAT0 + SIDE_DEG / 2


def polygon(zone_id, rings, name=None):
    return Zone(zone_id=zone_id, name=name or f"Zone {zone_id}", source="test",
                geometry={"type": "Polygon", "coordinates": rings})


def ring(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


def one(entity, zones, config=None):
    out = assign_zones([entity], zones, config)
    assert out["summary"]["records"] == 1
    return out["results"][0]


def code_of(excinfo):
    assert isinstance(excinfo.value, ValueError) and excinfo.value.code in ASSIGN_ERROR_CODES
    return excinfo.value.code


def test_single_zone_interior(zone_a):
    r = one(point("p", *MID, name="Kiosk", category="toilets"), [zone_a])
    assert (r["status"], r["reason_code"], r["assigned_zone_id"]) == ("ASSIGNED", "INSIDE_ONE_ZONE", "A")
    assert r["matches"] == [{"zone_id": "A", "zone_name": "A", "relation": "INSIDE"}]
    assert (r["record_id"], r["source"], r["source_id"], r["name"], r["category"]) == ("t:p", "t", "p", "Kiosk",
                                                                                       "toilets")
    assert (r["lon"], r["lat"], r["crs"]) == (MID[0], MID[1], "EPSG:4326")


def test_overlap_keeps_every_match_and_assigns_none(zone_a):
    overlap = square("B", lon0=LON0 + 0.005, lat0=LAT0 + 0.005)
    for zones in ([zone_a, overlap], [overlap, zone_a]):          # input order does not matter
        r = one(point("p", LON0 + 0.01, LAT0 + 0.01), zones)
        assert (r["status"], r["reason_code"]) == ("MULTIPLE_MATCHES", "INSIDE_SEVERAL_ZONES")
        assert [m["zone_id"] for m in r["matches"]] == ["A", "B"] and r["assigned_zone_id"] is None


def test_shared_boundary_is_multiple_with_both_boundary_relations(zone_a):
    east = square("B", lon0=LON0 + SIDE_DEG)                     # shares A's east edge
    r = one(point("p", LON0 + SIDE_DEG, MID[1]), [east, zone_a])
    assert (r["status"], r["reason_code"]) == ("MULTIPLE_MATCHES", "ON_SHARED_BOUNDARY")
    assert [(m["zone_id"], m["relation"]) for m in r["matches"]] == [("A", "BOUNDARY"), ("B", "BOUNDARY")]


def test_inside_one_zone_and_on_boundary_of_another(zone_a):
    inner = polygon("C", [ring(MID[0], LAT0 + 0.002, LON0 + 0.018, LAT0 + 0.018)])
    r = one(point("p", MID[0], MID[1]), [zone_a, inner])         # on C's west edge, interior of A
    assert (r["status"], r["reason_code"]) == ("MULTIPLE_MATCHES", "INSIDE_ONE_ON_BOUNDARY_OF_ANOTHER")
    assert [(m["zone_id"], m["relation"]) for m in r["matches"]] == [("A", "INSIDE"), ("C", "BOUNDARY")]


def test_single_boundary_outside_and_missing_coordinate(zone_a):
    edge = one(point("e", MID[0], LAT0), [zone_a])
    assert (edge["status"], edge["reason_code"], edge["assigned_zone_id"]) == ("ON_BOUNDARY",
                                                                               "ON_BOUNDARY_OF_ONE_ZONE", None)
    assert edge["matches"] == [{"zone_id": "A", "zone_name": "A", "relation": "BOUNDARY"}]
    outside = one(point("o", LON0 - 0.01, LAT0), [zone_a])
    assert (outside["status"], outside["reason_code"], outside["matches"]) == ("OUTSIDE_ALL_ZONES",
                                                                               "OUTSIDE_ALL_SUPPLIED_ZONES", [])
    missing = one(Entity(source="t", source_id="m", name="no xy"), [zone_a])
    assert (missing["status"], missing["lon"], missing["matches"], missing["name"]) == ("NO_COORDINATE", None,
                                                                                         [], "no xy")


def test_holes():
    holed = polygon("H", [ring(LON0, LAT0, LON0 + 0.02, LAT0 + 0.02),
                          ring(LON0 + 0.005, LAT0 + 0.005, LON0 + 0.015, LAT0 + 0.015)])
    assert one(point("in_hole", LON0 + 0.01, LAT0 + 0.01), [holed])["status"] == "OUTSIDE_ALL_ZONES"
    assert one(point("hole_edge", LON0 + 0.005, LAT0 + 0.01), [holed])["status"] == "ON_BOUNDARY"
    assert one(point("solid", LON0 + 0.002, LAT0 + 0.002), [holed])["status"] == "ASSIGNED"


def test_multipolygon_islands():
    islands = Zone(zone_id="M", name="Mainland and island", source="test", geometry={
        "type": "MultiPolygon", "coordinates": [[ring(LON0, LAT0, LON0 + 0.01, LAT0 + 0.01)],
                                                [ring(LON0 + 0.03, LAT0, LON0 + 0.04, LAT0 + 0.01)]]})
    r = one(point("island", LON0 + 0.035, LAT0 + 0.005), [islands])
    assert (r["status"], r["assigned_zone_id"], r["matches"][0]["zone_name"]) == ("ASSIGNED", "M",
                                                                                  "Mainland and island")
    assert one(point("sea", LON0 + 0.02, LAT0 + 0.005), [islands])["status"] == "OUTSIDE_ALL_ZONES"
    assert one(point("shore", LON0 + 0.03, LAT0 + 0.005), [islands])["status"] == "ON_BOUNDARY"


def test_results_follow_input_order_and_summary_counts(zone_a):
    entities = [point("1", *MID), Entity(source="t", source_id="2"), point("3", LON0 - 1, LAT0)]
    out = assign_zones(entities, [zone_a])
    assert [r["record_id"] for r in out["results"]] == ["t:1", "t:2", "t:3"]
    assert out["summary"]["status_counts"] == {"ASSIGNED": 1, "MULTIPLE_MATCHES": 0, "ON_BOUNDARY": 0,
                                               "OUTSIDE_ALL_ZONES": 1, "NO_COORDINATE": 1}
    assert set(out["summary"]["status_counts"]) == set(ASSIGN_STATUSES)
    assert "MULTIPLE_MATCHES outranks ON_BOUNDARY" in out["status_precedence"]
    assert "not a guarantee" in out["uncertainty_note"]
    json.dumps(out, allow_nan=False)
    assert assign_zones(entities, [zone_a]) == out


def test_source_and_provenance_are_preserved_and_inputs_untouched(zone_a):
    entity = Entity(source="osm", source_id="n42", lon=MID[0], lat=MID[1], name="WC", category="toilets",
                    provenance=[Provenance(source="osm", source_id="n42", retrieved_at="2026-10-01",
                                           license_note="ODbL")])
    before = entity.model_dump()
    r = one(entity, [zone_a])
    assert r["provenance"] == [{"source": "osm", "source_id": "n42", "retrieved_at": "2026-10-01",
                                "license_note": "ODbL"}]
    r["provenance"][0]["source"] = "edited"
    assert entity.model_dump() == before


def test_claim_free_assignment_leaves_verify_no_claim_semantics_unchanged(zone_a):
    entity = point("p", *MID)
    verified = verify_batch([entity], {}, [zone_a])[0]
    assert (verified["status"], verified["reason_code"]) == ("UNRESOLVED", "NO_CLAIM")
    assert one(entity, [zone_a])["status"] == "ASSIGNED"


# --- loaders: explicit CRS conversion and custom columns ---

def test_explicit_crs_conversion_through_existing_loaders(tmp_path):
    west, east = (LON0, LAT0, LON0 + 0.02, LAT0 + 0.02), (LON0 + 0.02, LAT0, LON0 + 0.04, LAT0 + 0.02)

    def bng_ring(x0, y0, x1, y1):
        return [list(transform_xy(x, y, "EPSG:4326", "EPSG:27700")) for x, y in ring(x0, y0, x1, y1)]
    (tmp_path / "zones.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"code": code, "label": label},
         "geometry": {"type": "Polygon", "coordinates": [bng_ring(*box)]}}
        for code, label, box in (("W", "West", west), ("E", "East", east))]}), encoding="utf-8")
    sites = {"w1": (LON0 + 0.01, LAT0 + 0.01), "e1": (LON0 + 0.03, LAT0 + 0.01), "far": (LON0 + 0.1, LAT0 + 0.01)}
    lines = ["ref,label,kind,easting,northing"]
    for ref, (lon, lat) in sites.items():
        e, n = transform_xy(lon, lat, "EPSG:4326", "EPSG:27700")
        lines.append(f"{ref},Site {ref},toilets,{e:.3f},{n:.3f}")
    lines.append("nocoord,Site nocoord,toilets,,")
    (tmp_path / "points.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    zones = load_zones(tmp_path / "zones.geojson", source="bng_zones", columns={"zone_id": "code", "name": "label"},
                       input_crs="EPSG:27700")
    entities = load_entities(tmp_path / "points.csv", source="upload", input_crs="EPSG:27700",
                             columns={"source_id": "ref", "name": "label", "category": "kind",
                                      "lon": "easting", "lat": "northing"})
    out = assign_zones(entities, zones, AssignConfig(original_x_field="easting", original_y_field="northing",
                                                     original_crs="EPSG:27700"))
    by_id = {r["source_id"]: r for r in out["results"]}
    assert (by_id["w1"]["status"], by_id["w1"]["assigned_zone_id"]) == ("ASSIGNED", "W")
    assert (by_id["e1"]["status"], by_id["e1"]["assigned_zone_id"]) == ("ASSIGNED", "E")
    assert by_id["far"]["status"] == "OUTSIDE_ALL_ZONES" and by_id["nocoord"]["status"] == "NO_COORDINATE"
    assert by_id["w1"]["lon"] == pytest.approx(sites["w1"][0], abs=1e-6)
    assert by_id["w1"]["lat"] == pytest.approx(sites["w1"][1], abs=1e-6)
    orig = by_id["w1"]["original_coordinates"]
    assert orig["crs"] == "EPSG:27700" and orig["fields"] == ["easting", "northing"]
    assert orig["x"] == lines[1].split(",")[3] and orig["y"] == lines[1].split(",")[4]   # raw, as uploaded
    assert by_id["nocoord"]["original_coordinates"] is None
    assert by_id["w1"]["matches"][0]["zone_name"] == "West"


def test_acceptance_style_upload_with_custom_columns(tmp_path):
    (tmp_path / "regions.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"region_code": "west", "region_label": "West region"},
         "geometry": {"type": "Polygon", "coordinates": [ring(-0.1, 51.5, -0.08, 51.52)]}},
        {"type": "Feature", "properties": {"region_code": "east", "region_label": "East and island"},
         "geometry": {"type": "MultiPolygon", "coordinates": [[ring(-0.09, 51.5, -0.07, 51.52)],
                                                              [ring(-0.06, 51.5, -0.05, 51.51)]]}}]}),
        encoding="utf-8")
    (tmp_path / "assets.csv").write_text(
        "asset_ref,east_custom,north_custom,site_label,kind_raw\n"
        "orig-A,-0.095,51.505,inside west,toilets\norig-B,-0.1,51.51,exact boundary,toilets\n"
        "orig-C,-0.085,51.51,overlap,toilets\norig-D,-0.04,51.51,outside,toilets\n"
        "orig-E,,,no coordinate,toilets\norig-F,-0.055,51.505,multipolygon island,toilets\n", encoding="utf-8")
    zones = load_zones(tmp_path / "regions.geojson", source="regions",
                       columns={"zone_id": "region_code", "name": "region_label"})
    entities = load_entities(tmp_path / "assets.csv", source="upload",
                             columns={"source_id": "asset_ref", "lon": "east_custom", "lat": "north_custom",
                                      "name": "site_label", "category": "kind_raw"})
    statuses = {r["source_id"]: r["status"] for r in assign_zones(entities, zones)["results"]}
    assert statuses == {"orig-A": "ASSIGNED", "orig-B": "ON_BOUNDARY", "orig-C": "MULTIPLE_MATCHES",
                        "orig-D": "OUTSIDE_ALL_ZONES", "orig-E": "NO_COORDINATE", "orig-F": "ASSIGNED"}


# --- refusals ---

def test_zone_input_errors(zone_a):
    entities = [point("p", *MID)]
    bad_inputs = [[], (), None, zone_a, [{"zone_id": "A", "geometry": {}}], [zone_a, square("A")],
                  [Zone.model_construct(zone_id="X", name="X", geometry={}, source="t")]]
    for zones in bad_inputs:
        with pytest.raises(AssignInputError) as exc:
            assign_zones(entities, zones)
        assert code_of(exc) == "INVALID_ZONES"
    with pytest.raises(AssignInputError, match="duplicate zone IDs: A") as exc:
        assign_zones(entities, [zone_a, square("A", lon0=0.5)])
    assert exc.value.context["zone_ids"] == ["A"]
    with pytest.raises(ValueError):                 # malformed geometry never becomes a Zone
        polygon("bad", [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]])


def test_entity_input_errors(zone_a):
    for entities in ([point("p", *MID), point("p", *MID)], [point("p", *MID), {"lon": 0}], None,
                     iter([point("p", *MID)])):
        with pytest.raises(AssignInputError) as exc:
            assign_zones(entities, [zone_a])
        assert code_of(exc) == "INVALID_ENTITY"
    ignored_dup = [Entity(source="t", source_id="x"), Entity(source="t", source_id="x")]   # no coordinates either
    with pytest.raises(AssignInputError, match="duplicate"):
        assign_zones(ignored_dup, [zone_a])
    assert assign_zones([], [zone_a])["summary"]["records"] == 0


@pytest.mark.parametrize("config, quantity", [
    (AssignConfig(max_records=1), "records"),
    (AssignConfig(max_zones=1), "zones"),
    (AssignConfig(max_pairs=3), "record x zone pairs"),
    (AssignConfig(max_zone_vertices=4), "vertices of zone A"),
    (AssignConfig(max_total_zone_vertices=9), "total zone vertices"),
])
def test_resource_bounds(zone_a, config, quantity):
    with pytest.raises(AssignInputError) as exc:
        assign_zones([point("1", *MID), point("2", *MID)], [zone_a, square("B", lon0=0.5)], config)
    assert code_of(exc) == "RESOURCE_LIMIT_EXCEEDED" and exc.value.context["quantity"] == quantity


def test_pair_bound_counts_only_located_records(zone_a):
    no_xy = [Entity(source="t", source_id=str(i)) for i in range(5)]
    out = assign_zones(no_xy + [point("p", *MID)], [zone_a], AssignConfig(max_pairs=1))
    assert out["summary"]["status_counts"]["NO_COORDINATE"] == 5


def test_config_validation_cannot_raise_hard_bounds():
    for kwargs in ({"max_records": AssignConfig().max_records + 1}, {"max_pairs": 0}, {"max_zones": True},
                   {"max_zone_vertices": 2.5}, {"original_x_field": "e"}, {"original_crs": " "},
                   {"original_x_field": "e", "original_y_field": 5}):
        with pytest.raises(AssignInputError) as exc:
            AssignConfig(**kwargs)
        assert exc.value.code == "INVALID_ASSIGN_CONFIG"
    with pytest.raises(AssignInputError) as exc:
        assign_zones([], [square("A")], {"max_records": 5})
    assert exc.value.code == "INVALID_ASSIGN_CONFIG"
    err = pickle.loads(pickle.dumps(AssignInputError("INVALID_ZONES", "x", zone_ids=["A"])))
    assert (err.code, err.detail, err.context) == ("INVALID_ZONES", "x", {"zone_ids": ["A"]})
