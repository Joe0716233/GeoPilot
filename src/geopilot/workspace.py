"""Custom-data workspace: uploaded bytes -> validated entities and zones -> claim-free zone assignment.

Everything here works on raw bytes passed in by the caller (in the app, bytes uploaded to the
Streamlit server and held in its memory for the session). These functions never read a local path,
write uploaded content to disk, cache it, or record an uploaded filename. What the hosting
environment does (server logs, backups, network) is outside their control. The steps are:

1. ``parse_upload(data, kind)``: size-bounded UTF-8 (BOM allowed) CSV or GeoJSON parsing into a
   ``RawTable``. File-level problems (blank or duplicate CSV headers, broken JSON, wrong GeoJSON
   type, too many records) raise ``UploadError``; row-level problems are kept on the row.
2. ``load_points(table, PointMapping, crs=..., crs_confirmed=..., source_label=...)``: explicit
   field mapping, explicit and confirmed input CRS, per-row LOADED/REJECTED records with reasons,
   generated IDs marked as such, duplicates rejected, rows without coordinates kept.
3. ``load_boundaries(table, ZoneMapping, ...)``: Polygon/MultiPolygon features into ``Zone``s;
   any bad feature refuses the whole layer (a silently missing zone would mislabel points).
4. ``run_assignment(points, zones)``: ``assign_zones`` on the loaded points, joined back to every
   input row so rejected rows stay visible as ``REJECTED``; ``export_rows``, ``run_metadata`` and
   ``workflow_fingerprint`` support exports and stale-result detection.

CRSs are accepted only as confirmed ``EPSG:<code>`` values for 2-D geographic or projected
systems. GCJ-02 and BD-09 are refused by name: PyProj has no transformation for them and
GeoPilot does not guess one.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, fields
from math import isfinite

from pydantic import ValidationError
from pyproj import CRS
from pyproj.exceptions import CRSError, ProjError
from shapely.errors import GEOSException
from shapely.geometry import mapping as geojson_mapping
from shapely.geometry import shape

from geopilot.data.schema import Entity, Provenance, Zone
from geopilot.decision import ScenarioConfig, plan_coverage
from geopilot.tools.spatial import WGS84, _crs_transformer, transform_geometry, transform_xy
from geopilot.verify import (ASSIGN_STATUSES, STATUS_PRECEDENCE, AssignConfig, AssignInputError,
                             assign_zones)
from geopilot.verify.assign import ASSIGN_UNCERTAINTY_NOTE

UPLOAD_ERROR_CODES = ("INVALID_FILE", "INVALID_MAPPING", "INVALID_SOURCE_LABEL", "INVALID_CRS", "UNSUPPORTED_CRS",
                      "CRS_NOT_CONFIRMED", "INVALID_ZONES", "RESOURCE_LIMIT_EXCEEDED", "UNACKNOWLEDGED_EXCLUSIONS")
ROW_STATUSES = ("LOADED", "REJECTED")
POINT_FIELDS = ("id", "x", "y", "name", "address", "category", "claimed_zone", "demand_weight")
_BLOCKED_CRS = re.compile(r"gcj|bd-?09|baidu|mars", re.IGNORECASE)
_EPSG = re.compile(r"epsg:(\d{4,6})", re.IGNORECASE)
_MAX_LABEL = 80
# Upload cap in MB (Streamlit counts 1 MB = 1024 * 1024 bytes); the app passes it to st.file_uploader and
# .streamlit/config.toml sets the same server.maxUploadSize, so oversized payloads are refused before parsing.
MAX_UPLOAD_MB = 5


class UploadError(ValueError):
    """Refused upload or workflow input. ``code`` is one of ``UPLOAD_ERROR_CODES``."""

    def __init__(self, code: str, detail: str, **context):
        if code not in UPLOAD_ERROR_CODES:
            raise AssertionError(f"unknown upload error code {code!r}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.context = context

    def __reduce__(self):
        return _rebuild_error, (self.code, self.detail, self.context)


def _rebuild_error(code: str, detail: str, context: dict) -> UploadError:
    return UploadError(code, detail, **context)


@dataclass(frozen=True)
class UploadLimits:
    """Per-upload work bounds, checked before the work they guard. Values may only be lowered."""

    max_file_bytes: int = MAX_UPLOAD_MB * 1024 * 1024
    max_records: int = 20_000
    max_zones: int = 2_000
    max_zone_vertices: int = 200_000
    max_total_zone_vertices: int = 500_000
    max_pairs: int = 2_000_000

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= f.default:
                raise UploadError("INVALID_FILE", f"{f.name} must be an integer between 1 and {f.default}")


DEFAULT_UPLOAD_LIMITS = UploadLimits()


def _bound(what: str, amount: int, limit: int, hint: str) -> None:
    if amount > limit:
        raise UploadError("RESOURCE_LIMIT_EXCEEDED", f"{what} = {amount} exceeds the limit {limit}; {hint}",
                          quantity=what, amount=amount, limit=limit)


def _listing(items: list[str], limit: int = 10) -> str:
    return "; ".join(items[:limit]) + (f"; and {len(items) - limit} more" if len(items) > limit else "")


# --- step 1: bytes -> RawTable ---

@dataclass(frozen=True)
class RawRow:
    """One input record. ``values`` are the CSV cells / GeoJSON properties as uploaded."""

    row_number: int
    values: dict
    geometry: object = None
    problem: tuple[str, str] | None = None


@dataclass(frozen=True)
class RawTable:
    kind: str
    headers: tuple[str, ...]
    rows: tuple[RawRow, ...]
    sha256: str
    size_bytes: int
    blank_lines_skipped: int = 0


def kind_from_filename(name: str) -> str:
    """``csv`` or ``geojson`` from an upload's extension; the name itself is not kept."""
    suffix = name.rsplit(".", 1)[-1].lower() if isinstance(name, str) and "." in name else ""
    if suffix == "csv":
        return "csv"
    if suffix in {"geojson", "json"}:
        return "geojson"
    raise UploadError("INVALID_FILE", "unsupported file type; upload a .csv, .geojson or .json file")


def _text(data, limits: UploadLimits) -> str:
    if not isinstance(data, (bytes, bytearray)):
        raise UploadError("INVALID_FILE", "upload content must be bytes")
    _bound("file bytes", len(data), limits.max_file_bytes, "upload a smaller file")
    if not data.strip():
        raise UploadError("INVALID_FILE", "the file is empty")
    try:
        return bytes(data).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadError("INVALID_FILE", f"the file is not UTF-8 (byte {exc.start}); save it as UTF-8 and "
                                          "upload it again") from None


def _parse_csv(text: str, limits: UploadLimits) -> tuple[tuple[str, ...], list[RawRow], int]:
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)  # malformed quoting is an error, not a guess
    try:
        header = next(reader, None)
        if not header:
            raise UploadError("INVALID_FILE", "the CSV has no header row")
        headers = tuple(h.strip() for h in header)
        blank = [str(i + 1) for i, h in enumerate(headers) if not h]
        if blank:
            raise UploadError("INVALID_FILE", "blank column header in column " + ", ".join(blank) +
                              "; give every column a name")
        duplicated = sorted(h for h, n in Counter(headers).items() if n > 1)
        if duplicated:
            raise UploadError("INVALID_FILE", "duplicate column headers: " + ", ".join(duplicated) +
                              "; column names must be unique", headers=duplicated)
        rows, skipped = [], 0
        for cells in reader:
            if not cells:
                skipped += 1
                continue
            _bound("records", len(rows) + 1, limits.max_records, "split the file")
            number = len(rows) + 1
            values = dict(zip(headers, cells))
            problem = None
            if len(cells) != len(headers):
                problem = ("ROW_WIDTH_MISMATCH", f"expected {len(headers)} fields, found {len(cells)}; check "
                                                 "delimiters and quoting on CSV line " + str(reader.line_num))
            rows.append(RawRow(number, values, None, problem))
    except csv.Error as exc:
        raise UploadError("INVALID_FILE", f"unreadable CSV near line {reader.line_num}: {exc}") from None
    return headers, rows, skipped


class _StrictJsonError(ValueError):
    pass


def _reject_constant(name: str):
    raise _StrictJsonError(f"non-standard JSON constant {name}; JSON has no NaN or Infinity")


def _finite_float(text: str) -> float:
    value = float(text)
    if not isfinite(value):
        raise _StrictJsonError(f"number {text[:40]} is too large to be a finite number")
    return value


def _parse_geojson(text: str, limits: UploadLimits) -> tuple[tuple[str, ...], list[RawRow]]:
    try:
        # Strict JSON: Python's json module would otherwise accept NaN/Infinity and turn 1e999 into inf.
        data = json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)
    except json.JSONDecodeError as exc:
        raise UploadError("INVALID_FILE", f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from None
    except _StrictJsonError as exc:
        raise UploadError("INVALID_FILE", f"invalid JSON: {exc}") from None
    except ValueError as exc:  # e.g. an integer longer than Python's int-string conversion limit
        raise UploadError("INVALID_FILE", f"invalid JSON number: {str(exc).split(';')[0]}") from None
    except RecursionError:
        raise UploadError("INVALID_FILE", "the JSON is nested too deeply") from None
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise UploadError("INVALID_FILE", "GeoJSON input must be a FeatureCollection")
    features = data.get("features")
    if not isinstance(features, list):
        raise UploadError("INVALID_FILE", "the FeatureCollection has no 'features' list")
    _bound("records", len(features), limits.max_records, "split the file")
    headers: dict[str, None] = {}
    rows = []
    for number, feature in enumerate(features, start=1):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            rows.append(RawRow(number, {}, None, ("NOT_A_FEATURE", "the item is not a GeoJSON Feature")))
            continue
        props = feature.get("properties")
        if props is None:
            props = {}
        if not isinstance(props, dict):
            rows.append(RawRow(number, {}, feature.get("geometry"),
                               ("INVALID_PROPERTIES", "'properties' must be an object or null")))
            continue
        for key in props:
            if not key.strip():
                raise UploadError("INVALID_FILE", f"feature {number} has a blank property name")
            headers.setdefault(key, None)
        rows.append(RawRow(number, props, feature.get("geometry")))
    return tuple(headers), rows


def parse_upload(data: bytes, kind: str, limits: UploadLimits = DEFAULT_UPLOAD_LIMITS) -> RawTable:
    """Parse uploaded bytes (``kind`` ``csv`` or ``geojson``) without interpreting any field."""
    if not isinstance(limits, UploadLimits):
        raise UploadError("INVALID_FILE", "limits must be an UploadLimits")
    if kind not in {"csv", "geojson"}:
        raise UploadError("INVALID_FILE", "kind must be 'csv' or 'geojson'")
    text = _text(data, limits)
    skipped = 0
    if kind == "csv":
        headers, rows, skipped = _parse_csv(text, limits)
    else:
        headers, rows = _parse_geojson(text, limits)
    if not rows:
        raise UploadError("INVALID_FILE", "the file has no data records")
    return RawTable(kind, headers, tuple(rows), hashlib.sha256(bytes(data)).hexdigest(), len(data), skipped)


# --- CRS and labels ---

def describe_crs(value: str) -> tuple[str, str]:
    """``("EPSG:<code>", name)`` for a supported 2-D geographic/projected EPSG CRS, else ``UploadError``."""
    if not isinstance(value, str) or not value.strip():
        raise UploadError("INVALID_CRS", "enter a coordinate reference system as EPSG:<code>, e.g. EPSG:4326")
    text = value.strip()
    if _BLOCKED_CRS.search(re.sub(r"[\s_]", "", text)):
        raise UploadError("UNSUPPORTED_CRS", f"{text!r} looks like GCJ-02 or BD-09. These obfuscated systems have no "
                                             "PyProj transformation, and GeoPilot will not guess one. Convert the data to "
                                             "WGS 84 (EPSG:4326) or CGCS2000 with an authorised tool first", crs=text)
    match = _EPSG.fullmatch(text)
    if not match:
        raise UploadError("INVALID_CRS", f"{text!r} is not of the form EPSG:<code> (e.g. EPSG:4326, EPSG:27700)",
                          crs=text)
    code = f"EPSG:{int(match.group(1))}"
    try:
        crs = CRS.from_epsg(int(match.group(1)))
    except CRSError:
        raise UploadError("INVALID_CRS", f"{code} is not a known EPSG coordinate reference system", crs=code) from None
    if not (crs.is_geographic or crs.is_projected) or len(crs.axis_info) != 2:
        raise UploadError("INVALID_CRS", f"{code} ({crs.name}) is not a 2-D geographic or projected CRS", crs=code)
    return code, crs.name


def resolve_crs(value: str, confirmed: bool) -> str:
    """The canonical EPSG code, only after the user has explicitly confirmed it."""
    code, name = describe_crs(value)
    if confirmed is not True:
        raise UploadError("CRS_NOT_CONFIRMED", f"confirm that the coordinates are in {code} ({name}) before loading",
                          crs=code)
    return code


def _label(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UploadError("INVALID_SOURCE_LABEL", "enter a source label naming where this data came from")
    if len(value.strip()) > _MAX_LABEL:
        raise UploadError("INVALID_SOURCE_LABEL", f"the source label must be at most {_MAX_LABEL} characters")
    return value.strip()


def _cell(value) -> str:
    return "" if value is None else str(value).strip()


# --- step 2: points ---

@dataclass(frozen=True)
class PointMapping:
    """Which uploaded column holds each field; ``None`` means the field is absent (never guessed).

    CSV needs ``x`` and ``y`` (two different columns, x = longitude/easting, y = latitude/northing);
    GeoJSON takes coordinates from each Point geometry, so ``x``/``y`` must stay unmapped.
    """

    id: str | None = None
    x: str | None = None
    y: str | None = None
    name: str | None = None
    address: str | None = None
    category: str | None = None
    claimed_zone: str | None = None
    demand_weight: str | None = None


@dataclass(frozen=True)
class PointLoad:
    entities: tuple[Entity, ...]
    records: tuple[dict, ...]
    summary: dict
    claims: dict
    mapping: PointMapping
    crs: str
    source_label: str
    kind: str
    sha256: str
    size_bytes: int


def _check_point_mapping(table: RawTable, mapping: PointMapping) -> None:
    if not isinstance(mapping, PointMapping):
        raise UploadError("INVALID_MAPPING", "mapping must be a PointMapping")
    missing = [f"{f} -> {getattr(mapping, f)!r}" for f in POINT_FIELDS
               if getattr(mapping, f) is not None and getattr(mapping, f) not in table.headers]
    if missing:
        raise UploadError("INVALID_MAPPING", "mapped columns not found in the file: " + ", ".join(missing) +
                          "; available columns: " + ", ".join(table.headers))
    if table.kind == "csv":
        if mapping.x is None or mapping.y is None:
            raise UploadError("INVALID_MAPPING", "a CSV point file needs both an x (longitude/easting) and a y "
                                                 "(latitude/northing) column")
        if mapping.x == mapping.y:
            raise UploadError("INVALID_MAPPING", f"x and y are both mapped to {mapping.x!r}; choose two different "
                                                 "columns")
    elif mapping.x is not None or mapping.y is not None:
        raise UploadError("INVALID_MAPPING", "GeoJSON points take coordinates from each Point geometry; leave x and "
                                             "y unmapped")


def _number(value, allow_text: bool) -> tuple[float | None, tuple[str, str] | None]:
    """CSV cells are text; GeoJSON coordinates must be JSON numbers (never booleans or strings)."""
    if isinstance(value, bool):
        return None, ("BOOLEAN_COORDINATE", f"coordinate {value!r} is a boolean, not a number")
    if isinstance(value, str) and not allow_text:
        return None, ("NON_NUMERIC_COORDINATE", f"coordinate {value!r} is a string; GeoJSON needs JSON numbers")
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None, ("NON_NUMERIC_COORDINATE", f"coordinate {value!r} is not a number")
    elif isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:
            return None, ("NON_FINITE_COORDINATE", "coordinate is too large to be a finite number")
    else:
        return None, ("NON_NUMERIC_COORDINATE", f"coordinate of type {type(value).__name__} is not a number")
    if not isfinite(number):
        return None, ("NON_FINITE_COORDINATE", f"coordinate {value!r} is not finite")
    return number, None


def _raw_xy(row: RawRow, table: RawTable, mapping: PointMapping):
    """(original_x, original_y, problem); (None, None, None) means no coordinate."""
    if table.kind == "csv":
        x, y = _cell(row.values.get(mapping.x)), _cell(row.values.get(mapping.y))
        if not x and not y:
            return None, None, None
        if not x or not y:
            return x or None, y or None, ("ONE_SIDED_COORDINATE", "only one of x/y is filled; give both or neither")
        return x, y, None
    geometry = row.geometry
    if geometry is None:
        return None, None, None
    if not isinstance(geometry, dict):
        return None, None, ("INVALID_GEOMETRY", "the geometry must be a GeoJSON object or null")
    if geometry.get("type") != "Point":
        return None, None, ("UNSUPPORTED_GEOMETRY_TYPE", f"geometry type {geometry.get('type')!r} is not supported "
                                                         "for points; use Point geometries (or null for no location)")
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2 or coords[0] is None or coords[1] is None:
        return None, None, ("INVALID_GEOMETRY", "a Point needs [x, y] coordinates; use a null geometry for no location")
    return coords[0], coords[1], None


def _generated_ids(rows, raw_ids: set[str]) -> dict[int, str]:
    prefix = "generated-row-"
    while True:
        ids = {row.row_number: f"{prefix}{row.row_number}" for row in rows}
        if not set(ids.values()) & raw_ids:
            return ids
        prefix = "_" + prefix


def load_points(table: RawTable, mapping: PointMapping, *, crs: str, crs_confirmed: bool, source_label: str,
                limits: UploadLimits = DEFAULT_UPLOAD_LIMITS) -> PointLoad:
    """Map, validate and convert uploaded point rows into ``Entity`` records plus a per-row audit."""
    if not isinstance(table, RawTable):
        raise UploadError("INVALID_FILE", "table must come from parse_upload")
    _bound("records", len(table.rows), limits.max_records, "split the file")
    _check_point_mapping(table, mapping)
    label = _label(source_label)
    code = resolve_crs(crs, crs_confirmed)

    raw_id = {row.row_number: (None if mapping.id is None else row.values.get(mapping.id)) for row in table.rows}
    clean_id = {n: _cell(v) for n, v in raw_id.items()}
    present = {v for v in clean_id.values() if v}
    counts = Counter(v for v in clean_id.values() if v)
    generated = _generated_ids([r for r in table.rows if not clean_id[r.row_number]], present)

    entities, records, claims = [], [], {}
    for row in table.rows:
        sid = clean_id[row.row_number] or generated[row.row_number]
        original_id = raw_id[row.row_number]
        raw_x, raw_y, xy_problem = _raw_xy(row, table, mapping)
        record = {"row_number": row.row_number, "status": "LOADED", "reason_code": "", "reason": "",
                  "record_id": None, "source": label, "source_id": sid, "original_id": original_id,
                  "id_generated": row.row_number in generated, "original_x": raw_x, "original_y": raw_y,
                  "input_crs": code, "lon": None, "lat": None, "has_coordinate": False}
        problem = row.problem
        if problem is None and counts.get(sid, 0) > 1 and row.row_number not in generated:
            problem = ("DUPLICATE_ID", f"ID {sid!r} appears on {counts[sid]} rows; IDs must be unique, so every copy is "
                                       "rejected")
        problem = problem or xy_problem
        x = y = None
        if problem is None:
            if raw_x is not None:
                x, problem = _number(raw_x, table.kind == "csv")
                if problem is None:
                    y, problem = _number(raw_y, table.kind == "csv")
            if problem is None and x is not None:
                try:
                    lon, lat = transform_xy(x, y, code, WGS84)
                except (ValueError, ArithmeticError, ProjError) as exc:
                    problem = ("COORDINATE_TRANSFORM_FAILED", f"cannot convert from {code}: {exc}")
                else:
                    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                        problem = ("COORDINATE_OUT_OF_RANGE", f"converted to lon={lon:.6g}, lat={lat:.6g}, outside "
                                                              f"WGS 84; check the CRS ({code}) and whether x/y are swapped")
                    else:
                        record.update(lon=lon, lat=lat, has_coordinate=True)
        if problem is not None:
            record.update(status="REJECTED", reason_code=problem[0], reason=problem[1], lon=None, lat=None,
                          has_coordinate=False)
            records.append(record)
            continue
        entity = Entity(
            source=label, source_id=sid, lon=record["lon"], lat=record["lat"],
            name=_cell(row.values.get(mapping.name)) if mapping.name else "",
            address=_cell(row.values.get(mapping.address)) if mapping.address else "",
            category=_cell(row.values.get(mapping.category)) if mapping.category else "",
            properties=copy.deepcopy(row.values),
            provenance=[Provenance(source=label, source_id=_cell(original_id) or sid)])
        record["record_id"] = entity.key
        if mapping.claimed_zone:
            claims[entity.key] = _cell(row.values.get(mapping.claimed_zone))
        entities.append(entity)
        records.append(record)

    loaded = [r for r in records if r["status"] == "LOADED"]
    summary = {"original": len(records), "loaded": len(loaded), "rejected": len(records) - len(loaded),
               "missing_coordinate": sum(1 for r in loaded if not r["has_coordinate"]),
               "missing_id": len(generated), "duplicate_id": sum(n for n in counts.values() if n > 1),
               "participating": sum(1 for r in loaded if r["has_coordinate"]),
               "blank_lines_skipped": table.blank_lines_skipped,
               "reason_counts": dict(sorted(Counter(r["reason_code"] for r in records
                                                    if r["status"] == "REJECTED").items()))}
    return PointLoad(tuple(entities), tuple(records), summary, claims, mapping, code, label, table.kind,
                     table.sha256, table.size_bytes)


# --- step 3: boundaries ---

@dataclass(frozen=True)
class ZoneMapping:
    """Zone ID property (required) and display-name property (optional; defaults to the ID)."""

    id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class ZoneLoad:
    zones: tuple[Zone, ...]
    summary: dict
    mapping: ZoneMapping
    crs: str
    source_label: str
    sha256: str
    size_bytes: int


def _positions(value, depth: int) -> tuple[int, str | None]:
    """Count positions in nested coordinate arrays (depth 1 = list of positions); report a problem."""
    if not isinstance(value, list) or not value:
        return 0, "coordinates must be non-empty arrays"
    if depth == 0:
        if len(value) < 2:
            return 0, "a position needs at least x and y"
        for number in value:
            if isinstance(number, bool):
                return 0, "boolean coordinate"
            if not isinstance(number, (int, float)):
                return 0, "non-numeric coordinate"
            try:
                if not isfinite(number):
                    return 0, "non-finite coordinate"
            except OverflowError:
                return 0, "coordinate too large"
        return 1, None
    total = 0
    for item in value:
        count, problem = _positions(item, depth - 1)
        if problem:
            return 0, problem
        total += count
    return total, None


def load_boundaries(table: RawTable, mapping: ZoneMapping, *, crs: str, crs_confirmed: bool, source_label: str,
                    limits: UploadLimits = DEFAULT_UPLOAD_LIMITS) -> ZoneLoad:
    """Polygon/MultiPolygon features into validated ``Zone``s; any bad feature refuses the layer."""
    if not isinstance(table, RawTable):
        raise UploadError("INVALID_FILE", "table must come from parse_upload")
    if table.kind != "geojson":
        raise UploadError("INVALID_FILE", "boundaries must be a GeoJSON FeatureCollection of Polygon/MultiPolygon "
                                          "features")
    if not isinstance(mapping, ZoneMapping):
        raise UploadError("INVALID_MAPPING", "mapping must be a ZoneMapping")
    if mapping.id is None:
        raise UploadError("INVALID_MAPPING", "choose the property that holds the zone ID")
    missing = [f"{f} -> {getattr(mapping, f)!r}" for f in ("id", "name")
               if getattr(mapping, f) is not None and getattr(mapping, f) not in table.headers]
    if missing:
        raise UploadError("INVALID_MAPPING", "mapped properties not found: " + ", ".join(missing) +
                          "; available properties: " + ", ".join(table.headers))
    label = _label(source_label)
    code = resolve_crs(crs, crs_confirmed)
    _bound("zones", len(table.rows), limits.max_zones, "split the boundary layer")

    problems, parsed, total_vertices = [], [], 0
    ids = Counter(_cell(row.values.get(mapping.id)) for row in table.rows)
    for row in table.rows:
        zone_id = _cell(row.values.get(mapping.id))
        where = f"feature {row.row_number}" + (f" ({zone_id})" if zone_id else "")
        if row.problem:
            problems.append(f"{where}: {row.problem[1]}")
            continue
        if not zone_id:
            problems.append(f"{where}: blank zone ID in {mapping.id!r}")
            continue
        if ids[zone_id] > 1:
            problems.append(f"{where}: zone ID {zone_id!r} appears {ids[zone_id]} times")
            continue
        geometry = row.geometry
        kind = geometry.get("type") if isinstance(geometry, dict) else None
        if kind not in {"Polygon", "MultiPolygon"}:
            problems.append(f"{where}: geometry type {kind!r} is not Polygon or MultiPolygon")
            continue
        count, problem = _positions(geometry.get("coordinates"), 2 if kind == "Polygon" else 3)
        if problem:
            problems.append(f"{where}: {problem}")
            continue
        _bound(f"vertices of zone {zone_id}", count, limits.max_zone_vertices, "simplify the polygon")
        total_vertices += count
        _bound("total zone vertices", total_vertices, limits.max_total_zone_vertices, "simplify or split the layer")
        parsed.append((row, zone_id, geometry))
    zones = []
    for row, zone_id, geometry in parsed:
        where = f"feature {row.row_number} ({zone_id})"
        try:
            geom = shape(geometry)
            if code != WGS84:
                geom = transform_geometry(geom, _crs_transformer(code, WGS84))
            if not all(isfinite(v) for v in geom.bounds):
                raise ValueError(f"cannot be converted from {code}")
            name = _cell(row.values.get(mapping.name)) if mapping.name else ""
            zones.append(Zone(zone_id=zone_id, name=name or zone_id, geometry=geojson_mapping(geom), source=label,
                              properties=copy.deepcopy(row.values)))
        except (ValueError, TypeError, AttributeError, GEOSException, ProjError) as exc:
            message = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
            problems.append(f"{where}: {message.rstrip('.') or type(exc).__name__}")
    if problems:
        raise UploadError("INVALID_ZONES", f"{len(problems)} boundary feature(s) cannot be used: " +
                          _listing(problems) + ". Fix them; no zone is dropped silently", problems=problems)
    return ZoneLoad(tuple(zones), {"zones": len(zones), "total_vertices": total_vertices}, mapping, code, label,
                    table.sha256, table.size_bytes)


# --- step 4: assignment, exports and fingerprint ---

def run_assignment(points: PointLoad, zones: ZoneLoad, limits: UploadLimits = DEFAULT_UPLOAD_LIMITS) -> dict:
    """``assign_zones`` on the loaded points, joined back to every input row (rejected rows included)."""
    if not isinstance(points, PointLoad) or not isinstance(zones, ZoneLoad):
        raise UploadError("INVALID_FILE", "run_assignment needs a PointLoad and a ZoneLoad")
    _bound("record x zone pairs", points.summary["participating"] * len(zones.zones), limits.max_pairs,
           "upload fewer points or zones")
    config = AssignConfig(max_records=limits.max_records, max_zones=limits.max_zones, max_pairs=limits.max_pairs,
                          max_zone_vertices=limits.max_zone_vertices,
                          max_total_zone_vertices=limits.max_total_zone_vertices)
    assignment = assign_zones(list(points.entities), list(zones.zones), config)
    by_key = {r["record_id"]: r for r in assignment["results"]}
    rows = []
    for record in points.records:
        result = by_key.get(record["record_id"]) if record["status"] == "LOADED" else None
        matches = result["matches"] if result else []
        rows.append({**record,
                     "status": result["status"] if result else "REJECTED",
                     "reason_code": result["reason_code"] if result else record["reason_code"],
                     "load_status": record["status"],
                     "assigned_zone_id": result["assigned_zone_id"] if result else None,
                     "matches": matches})
    counts = Counter(r["status"] for r in rows)
    return {"rows": rows, "assignment": assignment,
            "summary": {"records": len(rows), "zones": len(zones.zones),
                        "status_counts": {s: counts.get(s, 0) for s in (*ASSIGN_STATUSES, "REJECTED")}}}


EXPORT_COLUMNS = ("row_number", "source", "source_id", "original_id", "id_generated", "original_x", "original_y",
                  "input_crs", "lon", "lat", "load_status", "status", "reason_code", "reason", "assigned_zone_id",
                  "zone_ids", "zone_names", "relations")


def export_rows(run: dict) -> list[dict]:
    """One flat row per uploaded record, in upload order; rejected rows have status REJECTED."""
    out = []
    for row in run["rows"]:
        matches = row["matches"]
        out.append({"row_number": row["row_number"], "source": row["source"], "source_id": row["source_id"],
                    "original_id": row["original_id"], "id_generated": row["id_generated"],
                    "original_x": row["original_x"], "original_y": row["original_y"], "input_crs": row["input_crs"],
                    "lon": row["lon"], "lat": row["lat"], "load_status": row["load_status"],
                    "status": row["status"], "reason_code": row["reason_code"], "reason": row["reason"],
                    "assigned_zone_id": row["assigned_zone_id"],
                    "zone_ids": ";".join(m["zone_id"] for m in matches),
                    "zone_names": ";".join(m["zone_name"] for m in matches),
                    "relations": ";".join(m["relation"] for m in matches)})
    return out


def workflow_fingerprint(points: PointLoad, zones: ZoneLoad, limits: UploadLimits = DEFAULT_UPLOAD_LIMITS) -> str:
    """Digest of everything that determines an assignment: bytes digests, mappings, CRSs, labels, limits."""
    payload = {"points": {"sha256": points.sha256, "kind": points.kind, "mapping": asdict(points.mapping),
                          "crs": points.crs, "source": points.source_label},
               "zones": {"sha256": zones.sha256, "mapping": asdict(zones.mapping), "crs": zones.crs,
                         "source": zones.source_label},
               "limits": asdict(limits)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def run_metadata(points: PointLoad, zones: ZoneLoad, run: dict, limits: UploadLimits = DEFAULT_UPLOAD_LIMITS) -> dict:
    """Run description for the JSON download: inputs by digest (no filenames), CRSs, mappings, counts."""
    return {"workflow": "custom_zone_assignment", "fingerprint": workflow_fingerprint(points, zones, limits),
            "points": {"source_label": points.source_label, "format": points.kind, "sha256": points.sha256,
                       "bytes": points.size_bytes, "input_crs": points.crs, "mapping": asdict(points.mapping),
                       "quality": points.summary},
            "boundaries": {"source_label": zones.source_label, "sha256": zones.sha256, "bytes": zones.size_bytes,
                           "input_crs": zones.crs, "mapping": asdict(zones.mapping), "summary": zones.summary},
            "output_crs": WGS84, "limits": asdict(limits), "summary": run["summary"],
            "status_precedence": STATUS_PRECEDENCE, "uncertainty_note": ASSIGN_UNCERTAINTY_NOTE,
            "notes": ["Uploaded filenames are not recorded; inputs are identified by SHA-256 of their bytes.",
                      "Rejected rows are listed with status REJECTED and a reason in the CSV export."]}


# --- step 5: coverage screening on uploaded data ---

def coverage_exclusions(points: PointLoad) -> list[dict]:
    """Rows that cannot take part in coverage: rejected rows and loaded rows without coordinates."""
    return [{"row_number": r["row_number"], "source_id": r["source_id"], "original_id": r["original_id"],
             "reason_code": r["reason_code"] if r["status"] == "REJECTED" else "NO_COORDINATE",
             "reason": r["reason"] if r["status"] == "REJECTED" else "loaded without coordinates"}
            for r in points.records if r["status"] == "REJECTED" or not r["has_coordinate"]]


def _scenario_payload(config: ScenarioConfig) -> dict:
    return {"roles": {c: str(r) for c, r in config.roles.roles.items()},
            "context_metrics": dict(config.roles.context_metrics), "demand_weight_field": config.demand_weight_field,
            "weights": dict(config.weights), "allow_zero_existing": config.allow_zero_existing,
            "method": config.method, "target_count": config.target_count, "grid_step_m": config.grid_step_m,
            "service_radius_m": config.service_radius_m, "min_existing_distance_m": config.min_existing_distance_m,
            "min_new_site_distance_m": config.min_new_site_distance_m, "limits": asdict(config.limits)}


def coverage_fingerprint(points: PointLoad, zones: ZoneLoad, study_zone_id: str, config: ScenarioConfig,
                         acknowledge_exclusions: bool, limits: UploadLimits = DEFAULT_UPLOAD_LIMITS) -> str:
    """Digest of the uploads, study zone, every scenario parameter and the exclusion acknowledgement."""
    payload = {"workflow": workflow_fingerprint(points, zones, limits), "study_zone_id": study_zone_id,
               "scenario": _scenario_payload(config), "acknowledge_exclusions": acknowledge_exclusions is True}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def run_coverage(points: PointLoad, zones: ZoneLoad, study_zone_id: str, config: ScenarioConfig, *,
                 acknowledge_exclusions: bool) -> dict:
    """``plan_coverage`` on uploaded points inside one uploaded zone.

    Rejected rows and rows without coordinates cannot take part; when there are any, the caller
    must pass ``acknowledge_exclusions=True`` (they are listed in ``upload.excluded_rows``).
    ``DecisionInputError`` from ``plan_coverage`` (e.g. UNMAPPED_ROLE) propagates unchanged.
    """
    if not isinstance(points, PointLoad) or not isinstance(zones, ZoneLoad):
        raise UploadError("INVALID_FILE", "run_coverage needs a PointLoad and a ZoneLoad")
    zone = next((z for z in zones.zones if z.zone_id == study_zone_id), None)
    if zone is None:
        raise UploadError("INVALID_ZONES", f"study zone {study_zone_id!r} is not in the uploaded boundaries")
    excluded = coverage_exclusions(points)
    if excluded and acknowledge_exclusions is not True:
        raise UploadError("UNACKNOWLEDGED_EXCLUSIONS",
                          f"{len(excluded)} uploaded rows (rejected or without coordinates) would be left out of the "
                          "coverage analysis; acknowledge their exclusion first", rows=len(excluded))
    result = plan_coverage(zone, [e for e in points.entities if e.has_point], config)
    return {**result, "study_zone_id": study_zone_id,
            "upload": {"points_sha256": points.sha256, "points_source": points.source_label, "points_crs": points.crs,
                       "boundaries_sha256": zones.sha256, "boundaries_source": zones.source_label,
                       "boundaries_crs": zones.crs, "point_mapping": asdict(points.mapping),
                       "excluded_rows": excluded, "exclusions_acknowledged": bool(excluded)}}


__all__ = ["DEFAULT_UPLOAD_LIMITS", "EXPORT_COLUMNS", "MAX_UPLOAD_MB", "POINT_FIELDS", "ROW_STATUSES", "UPLOAD_ERROR_CODES",
           "PointLoad", "PointMapping", "RawRow", "RawTable", "UploadError", "UploadLimits", "ZoneLoad", "ZoneMapping",
           "AssignInputError", "coverage_exclusions", "coverage_fingerprint", "run_coverage", "describe_crs",
           "export_rows", "kind_from_filename", "load_boundaries", "load_points",
           "parse_upload", "resolve_crs", "run_assignment", "run_metadata", "workflow_fingerprint"]
