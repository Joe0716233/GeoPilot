import pytest

from geopilot.data import Entity
from geopilot.resolve import ResolveConfig, candidate_pairs, resolve_entities, score_pair

from conftest import LAT0, LON0


def ent(source, sid, name="", address="", lon=LON0, lat=LAT0, **kw):
    return Entity(source=source, source_id=sid, name=name, address=address, lon=lon, lat=lat,
                  category=kw.pop("category", "station"), **kw)


def test_identical_records_a_few_metres_apart_match():
    a = ent("A", "1", "Kilburn Station Car Park", "1 Christchurch Ave")
    b = ent("B", "9", "Kilburn Station Car Park", "1 Christchurch Ave", lat=LAT0 + 0.00005)
    pair = score_pair(a, b)
    assert pair["status"] == "MATCH" and pair["distance_m"] == pytest.approx(5.6, abs=0.2) and pair["score"] >= .78


def test_proximity_and_category_alone_never_match():
    a = ent("A", "1", "Alpha Garage", "10 High St")
    b = ent("B", "2", "Zebra Cinema", "99 Other Rd", lat=LAT0 + 0.00001)
    pair = score_pair(a, b)
    assert pair["status"] != "MATCH" and pair["score"] < .55


def test_nearby_incomplete_identity_goes_to_review():
    a = ent("A", "1", "Alpha Garage", "10 High St")
    b = ent("B", "2", "", "", lat=LAT0 + 0.00005)
    pair = score_pair(a, b)
    assert (pair["status"], pair["review_reason"]) == ("REVIEW", "NEARBY_INCOMPLETE_IDENTITY")


def test_shared_phone_can_override_weak_name():
    a = ent("A", "1", "Alpha Garage", "10 High St", phone="020 7946 0000")
    b = ent("B", "2", "Alpha Motors Ltd", "10 High Street", phone="02079460000", lat=LAT0 + 0.00005)
    assert score_pair(a, b)["features"]["phone"] == 1.0


def test_same_source_records_are_not_candidates():
    entities = [ent("A", "1", "X"), ent("A", "2", "X", lat=LAT0 + 0.00001)]
    assert candidate_pairs(entities) == []


def test_radius_blocks_distant_pairs():
    far = LAT0 + 0.01  # ~1.1 km
    entities = [ent("A", "1", "Same Name"), ent("B", "2", "Same Name", lat=far)]
    assert candidate_pairs(entities, 300) == []
    assert candidate_pairs(entities, 2000) == [(0, 1)]


def test_missing_coordinates_are_blocked_by_name_prefix_and_never_match_on_name_alone():
    a = ent("A", "1", "Riverside Charge Hub", "1 Pier Rd")
    b = Entity(source="B", source_id="2", name="Riverside Charge Hub", address="1 Pier Rd", category="station")
    result = resolve_entities([a, b])
    [pair] = result.pairs
    assert pair["distance_m"] is None and pair["status"] != "MATCH"


def test_same_source_conflict_prevents_merge():
    # Two A records both match one B record: B can join only one; the other pair is demoted.
    a1 = ent("A", "1", "Hub One", "5 Road")
    a2 = ent("A", "2", "Hub One", "5 Road", lat=LAT0 + 0.00002)
    b = ent("B", "1", "Hub One", "5 Road", lat=LAT0 + 0.00001)
    result = resolve_entities([a1, a2, b])
    assert all(len({k.split(":")[0] for k in c["member_keys"]}) == c["size"] for c in result.clusters)
    assert any(p["fusion_reason"] == "SAME_SOURCE_CONFLICT" for p in result.pairs)
    assert result.summary["canonical_records"] == 2


def test_merge_keeps_provenance_and_aliases():
    a = ent("A", "1", "Kilburn Station Car Park", "1 Christchurch Ave")
    b = ent("B", "9", "Kilburn Station Carpark", "1 Christchurch Avenue", lat=LAT0 + 0.00005)
    result = resolve_entities([a, b])
    [merged] = result.canonical
    assert {p.source for p in merged.provenance} == {"A", "B"} and len(merged.aliases) == 1
    assert result.summary["merged_clusters"] == 1 and result.clusters[0]["size"] == 2


def test_resolution_is_deterministic_and_order_independent():
    entities = [ent("A", str(i), f"Site {i}", f"{i} Road", lon=LON0 + i * 0.01) for i in range(5)] + \
               [ent("B", str(i), f"Site {i}", f"{i} Road", lon=LON0 + i * 0.01, lat=LAT0 + 0.00005) for i in range(5)]
    first = resolve_entities(entities)
    second = resolve_entities(list(reversed(entities)))
    assert [c["member_keys"] for c in first.clusters] == [c["member_keys"] for c in second.clusters]
    assert first.summary["merged_clusters"] == 5


def test_duplicate_keys_and_bad_config_rejected():
    with pytest.raises(ValueError, match="unique"):
        resolve_entities([ent("A", "1"), ent("A", "1")])
    for kwargs in ({"match_score": .5, "review_score": .6}, {"match_score": 1.2}, {"radius_m": 0},
                   {"radius_m": float("nan")}):
        with pytest.raises(ValueError):
            ResolveConfig(**kwargs)

# --- regressions from independent review ---

def test_zero_review_threshold_keeps_scores_in_unit_interval():
    a = Entity(source="a", source_id="1", lon=0, lat=0)
    b = Entity(source="b", source_id="1", lon=0, lat=0)
    pair = score_pair(a, b, ResolveConfig(review_score=0))
    assert 0 <= pair["score"] <= 1
    assert pair["status"] != "MATCH"


def test_proximity_only_pair_is_never_match_even_with_low_match_threshold():
    a = Entity(source="a", source_id="1", lon=0, lat=0, category="station")
    b = Entity(source="b", source_id="1", lon=0, lat=0, category="station")
    cfg = ResolveConfig(match_score=0.1, review_score=0.0)
    pair = score_pair(a, b, cfg)
    assert 0 <= pair["score"] <= 1 and pair["score"] >= cfg.match_score
    assert (pair["status"], pair["review_reason"]) == ("REVIEW", "INSUFFICIENT_IDENTITY_EVIDENCE")
    result = resolve_entities([a, b], cfg)
    assert result.summary["merged_clusters"] == 0 and result.summary["canonical_records"] == 2
