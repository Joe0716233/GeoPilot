import json
import math

import pytest
from pydantic import ValidationError

from geopilot.data import Entity, Zone, inspect_entities, load_claims, load_entities, load_zones
from geopilot.tools import (buffer_point, geodesic_distance_m, nearest_point, point_in_polygon,
                            transform_xy)
from geopilot.tools.spatial import check_lonlat
from shapely.geometry import shape

from conftest import LAT0, LON0, SIDE_DEG, square


def test_geodesic_distance_one_degree_latitude():
    # One degree of latitude at 51.5 N is ~111.25 km on WGS 84.
    assert geodesic_distance_m(0, 51, 0, 52) == pytest.approx(111_250, rel=2e-3)


def test_geodesic_distance_longitude_scales_with_cos_lat():
    d = geodesic_distance_m(0, 60, 1, 60)
    assert d == pytest.approx(111_320 * math.cos(math.radians(60)), rel=3e-3)


def test_point_in_polygon_signed_distance_matches_geodesic_geometry():
    poly = square("A").geometry
    inside = point_in_polygon(LON0 + SIDE_DEG / 2, LAT0 + 0.0005, poly)
    expected = geodesic_distance_m(LON0 + SIDE_DEG / 2, LAT0 + 0.0005, LON0 + SIDE_DEG / 2, LAT0)
    assert inside["inside_or_boundary"] and inside["signed_distance_m"] == pytest.approx(expected, abs=0.05)
    outside = point_in_polygon(LON0 + SIDE_DEG / 2, LAT0 - 0.0005, poly)
    assert not outside["inside_or_boundary"]
    assert outside["signed_distance_m"] == pytest.approx(-expected, abs=0.05)


def test_boundary_point_counts_inside_with_zero_distance():
    r = point_in_polygon(LON0 + 0.01, LAT0, square("A").geometry)
    assert r["inside_or_boundary"] and r["on_boundary"] and r["distance_to_boundary_m"] == pytest.approx(0, abs=0.01)


def test_point_in_polygon_rejects_invalid_polygon():
    bowtie = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}
    with pytest.raises(ValueError):
        point_in_polygon(0.5, 0.5, bowtie)
    with pytest.raises(ValueError):
        point_in_polygon(0.5, 0.5, {"type": "Point", "coordinates": [0, 0]})


def test_buffer_point_radius_is_metric():
    ring = shape(buffer_point(-0.1, 51.5, 500)["geometry"])
    radii = [geodesic_distance_m(-0.1, 51.5, x, y) for x, y in list(ring.exterior.coords)[::16]]
    assert max(abs(r - 500) for r in radii) < 1.0
    with pytest.raises(ValueError):
        buffer_point(-0.1, 51.5, -1)
    with pytest.raises(ValueError):
        buffer_point(-0.1, 51.5, float("nan"))


def test_nearest_point_and_empty():
    cands = [{"lon": -0.1, "lat": 51.51, "id": "far"}, {"lon": -0.1, "lat": 51.501, "id": "near"}]
    best = nearest_point(-0.1, 51.5, cands)
    assert best["candidate"]["id"] == "near"
    assert best["distance_m"] == pytest.approx(111.2, abs=0.5)
    assert nearest_point(-0.1, 51.5, []) is None


def test_check_lonlat_rejects_bad_values():
    for lon, lat in [(float("nan"), 0), (181, 0), (0, 91), (True, 0)]:
        with pytest.raises(ValueError):
            check_lonlat(lon, lat)


def test_transform_xy_bng_roundtrip():
    lon, lat = transform_xy(530000, 180000, "EPSG:27700")
    assert -0.2 < lon < 0.1 and 51.4 < lat < 51.6  # central London
    x, y = transform_xy(lon, lat, "EPSG:4326", "EPSG:27700")
    assert (x, y) == pytest.approx((530000, 180000), abs=0.01)


def test_entity_validation():
    with pytest.raises(ValidationError):
        Entity(source="s", source_id="1", lon=200, lat=0)
    with pytest.raises(ValidationError):
        Entity(source="s", source_id="1", lon=float("nan"), lat=0)
    with pytest.raises(ValidationError):
        Entity(source="s", source_id="1", lon=1.0)  # lat missing
    with pytest.raises(ValidationError):
        Entity(source="s", source_id=" ")
    assert Entity(source="s", source_id="1").has_point is False


def test_zone_validation():
    bowtie = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}
    with pytest.raises(ValidationError):
        Zone(zone_id="z", name="z", geometry=bowtie, source="s")
    with pytest.raises(ValidationError):
        Zone(zone_id="z", name="z", geometry={"type": "Point", "coordinates": [0, 0]}, source="s")
    with pytest.raises(ValidationError):  # projected metres mistaken for degrees
        Zone(zone_id="z", name="z", source="s", geometry={"type": "Polygon", "coordinates": [
            [[530000, 180000], [531000, 180000], [531000, 181000], [530000, 181000], [530000, 180000]]]})


def test_load_entities_csv_with_projected_crs(tmp_path):
    path = tmp_path / "e.csv"
    path.write_text("id,x,y,label\na,530000,180000,One\n", encoding="utf-8")
    [e] = load_entities(path, source="s", columns={"source_id": "id", "lon": "x", "lat": "y", "name": "label"},
                        input_crs="EPSG:27700")
    assert e.name == "One" and -0.2 < e.lon < 0.1 and 51.4 < e.lat < 51.6


def test_load_entities_keeps_zero_coordinates(tmp_path):
    path = tmp_path / "e.csv"
    path.write_text("source_id,name,lon,lat\n1,Null Island,0,0\n", encoding="utf-8")
    [e] = load_entities(path, source="s")
    assert (e.lon, e.lat) == (0.0, 0.0)


def test_load_entities_reports_all_bad_rows_and_duplicates(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("source_id,name,lon,lat\n1,a,500,10\n2,b,abc,10\n3,c,1,\n", encoding="utf-8")
    with pytest.raises(ValueError) as err:
        load_entities(bad, source="s")
    assert "row 1" in str(err.value) and "row 2" in str(err.value) and "row 3" in str(err.value)
    dup = tmp_path / "dup.csv"
    dup.write_text("source_id,name,lon,lat\n1,a,0,0\n1,b,1,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_entities(dup, source="s")
    with pytest.raises(ValueError, match="unsupported"):
        load_entities(tmp_path / "x.txt", source="s")


def test_load_entities_geojson_and_non_point(tmp_path):
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"source_id": "p", "name": "P"},
         "geometry": {"type": "Point", "coordinates": [-0.1, 51.5]}}]}
    path = tmp_path / "p.geojson"
    path.write_text(json.dumps(fc), encoding="utf-8")
    assert load_entities(path, source="s")[0].lat == 51.5
    fc["features"][0]["geometry"] = {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}
    path.write_text(json.dumps(fc), encoding="utf-8")
    with pytest.raises(ValueError, match="not a Point"):
        load_entities(path, source="s")


def test_load_zones_converts_crs_and_rejects_invalid(tmp_path):
    ring = [[530000, 180000], [531000, 180000], [531000, 181000], [530000, 181000], [530000, 180000]]
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"zone_id": "bng", "name": "BNG box"},
         "geometry": {"type": "Polygon", "coordinates": [ring]}}]}
    path = tmp_path / "z.geojson"
    path.write_text(json.dumps(fc), encoding="utf-8")
    [z] = load_zones(path, source="s", input_crs="EPSG:27700")
    assert -0.2 < z.shape.centroid.x < 0.1
    with pytest.raises(ValueError):
        load_zones(path, source="s")  # metres interpreted as degrees


def test_load_claims_rejects_duplicates(tmp_path):
    path = tmp_path / "c.csv"
    path.write_text("source_id,claimed_zone\n1,A\n1,B\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_claims(path, source="s")


def test_inspect_entities_flags_issues():
    entities = [Entity(source="s", source_id="1", name=""), Entity(source="s", source_id="1", name="x", lon=0, lat=0)]
    codes = sorted(i.code for i in inspect_entities(entities))
    assert codes == ["DUPLICATE_ID", "DUPLICATE_ID", "MISSING_COORDINATE", "MISSING_NAME"]
