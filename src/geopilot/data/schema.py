"""Shared records.

All stored coordinates are WGS 84 longitude/latitude (EPSG:4326, degrees, x = lon, y = lat).
Use the loaders' ``input_crs`` argument to convert other coordinate systems on the way in.
"""

from __future__ import annotations

from math import isfinite
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry


class Provenance(BaseModel):
    source: str
    source_id: str
    retrieved_at: str | None = None
    license_note: str | None = None


class Entity(BaseModel):
    """A point-like record (charger, shop, demand point...) from one named source."""

    source: str
    source_id: str
    name: str = ""
    address: str = ""
    category: str = ""
    lon: float | None = None
    lat: float | None = None
    city: str = ""
    district: str = ""
    phone: str = ""
    url: str = ""
    provenance: list[Provenance] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source", "source_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("source and source_id must be non-empty")
        return value

    @field_validator("lon")
    @classmethod
    def _check_lon(cls, value: float | None) -> float | None:
        if value is not None and not (isfinite(value) and -180 <= value <= 180):
            raise ValueError("longitude must be finite and within [-180, 180]")
        return value

    @field_validator("lat")
    @classmethod
    def _check_lat(cls, value: float | None) -> float | None:
        if value is not None and not (isfinite(value) and -90 <= value <= 90):
            raise ValueError("latitude must be finite and within [-90, 90]")
        return value

    @model_validator(mode="after")
    def _both_or_neither(self) -> "Entity":
        if (self.lon is None) != (self.lat is None):
            raise ValueError("lon and lat must be supplied together")
        return self

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    @property
    def has_point(self) -> bool:
        return self.lon is not None and self.lat is not None


class Zone(BaseModel):
    """A named polygonal area (administrative zone, study area) in EPSG:4326."""

    zone_id: str
    name: str
    type: str = "area"
    geometry: dict[str, Any]
    source: str
    properties: dict[str, Any] = Field(default_factory=dict)
    _shape: BaseGeometry = PrivateAttr()

    @field_validator("zone_id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("zone_id must be non-empty")
        return value

    @model_validator(mode="after")
    def _validate_geometry(self) -> "Zone":
        try:
            geom = shape(self.geometry)
        except Exception as exc:  # shapely raises several types for malformed GeoJSON
            raise ValueError(f"zone {self.zone_id}: unreadable geometry ({exc})") from exc
        if geom.geom_type not in {"Polygon", "MultiPolygon"} or geom.is_empty or not geom.is_valid:
            raise ValueError(f"zone {self.zone_id}: a valid, non-empty Polygon or MultiPolygon is required")
        minx, miny, maxx, maxy = geom.bounds
        if not (-180 <= minx and maxx <= 180 and -90 <= miny and maxy <= 90):
            raise ValueError(f"zone {self.zone_id}: coordinates are not valid EPSG:4326 lon/lat")
        self._shape = geom
        return self

    @property
    def shape(self) -> BaseGeometry:
        return self._shape


class Issue(BaseModel):
    record_id: str
    code: str
    detail: str
