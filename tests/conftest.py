import sys
from pathlib import Path

import pytest

from geopilot.data import Entity, Zone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

LON0, LAT0 = -0.10, 51.50
SIDE_DEG = 0.02  # ~1.4 km east-west, ~2.2 km north-south


def square(zone_id: str, lon0: float = LON0, lat0: float = LAT0, side: float = SIDE_DEG) -> Zone:
    ring = [[lon0, lat0], [lon0 + side, lat0], [lon0 + side, lat0 + side], [lon0, lat0 + side], [lon0, lat0]]
    return Zone(zone_id=zone_id, name=zone_id, geometry={"type": "Polygon", "coordinates": [ring]}, source="test")


def point(source_id: str, lon, lat, **kw) -> Entity:
    kw.setdefault("source", "t")
    return Entity(source_id=source_id, lon=lon, lat=lat, **kw)


@pytest.fixture
def zone_a() -> Zone:
    return square("A")


@pytest.fixture
def sample():
    from geopilot.scenario import load_london_sample
    return load_london_sample()
