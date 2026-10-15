"""Custom-data workspace page: upload points and boundaries, map fields, confirm CRSs, assign zones.

Uploads are read as bytes from ``st.file_uploader`` only. The content passes through the Streamlit
server and is processed in that server's memory for this session. The application does not
intentionally write uploaded content to disk or put it in a shared cache, and it does not record
filenames. The hosting environment (server logs, backups, network) is outside the application's
control. The result lives in this session's ``st.session_state`` under the fingerprint of the inputs
that produced it, and is discarded as soon as any input changes.
"""

from __future__ import annotations

from math import cos, radians

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import coverage_ui
from geopilot.reporting import to_csv, to_json
from geopilot.workspace import (MAX_UPLOAD_MB, PointMapping, UploadError, ZoneMapping, describe_crs, export_rows,
                                kind_from_filename, load_boundaries, load_points, parse_upload, run_assignment,
                                run_metadata, workflow_fingerprint)

RESULT_KEY = "cw_result"
ASSIGN_COLOURS = {"ASSIGNED": "#2b9b70", "ON_BOUNDARY": "#e5a63b", "MULTIPLE_MATCHES": "#8e44ad",
                  "OUTSIDE_ALL_ZONES": "#d74f46", "NO_COORDINATE": "#8b929d", "REJECTED": "#444444"}
STATUS_ZH = {"ASSIGNED": "已分配", "ON_BOUNDARY": "位于边界", "MULTIPLE_MATCHES": "多区域匹配",
             "OUTSIDE_ALL_ZONES": "不在任何区域", "NO_COORDINATE": "无坐标", "REJECTED": "已拒绝"}
POINT_LABELS = {"id": ("记录 ID", "Record ID"), "name": ("名称", "Name"), "address": ("地址", "Address"),
                "category": ("类别", "Category"), "claimed_zone": ("声明区域", "Claimed zone"),
                "demand_weight": ("需求权重", "Demand weight")}


def _show_error(t, exc: ValueError) -> None:
    code = getattr(exc, "code", "ERROR")
    detail = getattr(exc, "detail", str(exc))
    st.error(t(f"**{code}**：{detail}", f"**{code}**: {detail}"))

    context = getattr(exc, "context", None)
    if context:
        with st.expander(t("完整错误详情（用于修正输入）", "Full error details (to correct the input)")):
            st.json(context)


def _strings(frame: pd.DataFrame) -> pd.DataFrame:
    """Uploaded values can mix types within a column; show object columns as text."""
    for column in frame.columns:
        if frame[column].dtype == object:
            frame[column] = frame[column].map(lambda v: "" if v is None else str(v))
    return frame


def _column(t, label: tuple[str, str], headers: list[str], key: str, required: bool):
    if required:
        return st.selectbox(t(*label), headers, index=None, key=key, placeholder=t("请选择列", "Choose a column"))
    return st.selectbox(t(*label), [None, *headers], index=0, key=key,
                        format_func=lambda v: t("（不映射，视为缺失）", "(not mapped: absent)") if v is None else v)


def _reset_confirmation_on_new_bytes(prefix: str, digest: str) -> None:
    """Different file bytes need a fresh CRS confirmation; the same bytes (any filename) keep it."""
    marker = f"{prefix}_crs_digest"
    if st.session_state.get(marker) != digest:
        for key in [k for k in st.session_state if str(k).startswith(f"{prefix}_crs_ok_")]:
            del st.session_state[key]
        st.session_state[marker] = digest


def _crs(t, prefix: str, what: tuple[str, str], digest: str):
    _reset_confirmation_on_new_bytes(prefix, digest)
    text = st.text_input(t(f"{what[0]}坐标参考系（仅支持 EPSG:<代码>，二维地理或投影坐标系）",
                           f"{what[1]} CRS (EPSG:<code> only; 2-D geographic or projected)"), "EPSG:4326",
                         key=f"{prefix}_crs",
                         help=t("只接受 EPSG 代码，不接受 WKT、PROJ 字符串或文件。GCJ-02 / BD-09 不受支持。",
                                "Only EPSG codes are accepted, not WKT, PROJ strings or files. GCJ-02 / BD-09 are "
                                "not supported."))
    try:
        code, name = describe_crs(text)
    except UploadError as exc:
        _show_error(t, exc)
        return None, False
    # The confirmation key includes the code, so changing the CRS always needs a fresh confirmation.
    confirmed = st.checkbox(t(f"我确认{what[0]}坐标为 {code}（{name}）：x = 经度/东坐标，y = 纬度/北坐标",
                              f"I confirm the {what[1].lower()} coordinates are {code} ({name}): "
                              "x = longitude/easting, y = latitude/northing"),
                            value=False, key=f"{prefix}_crs_ok_{code}")
    if not confirmed:
        st.info(t("请确认坐标参考系后再载入。", "Confirm the coordinate reference system to load the file."))
    return text, confirmed


def _upload(t, prefix: str, types: list[str], label: tuple[str, str]):
    # Per-widget cap so the server refuses oversized payloads before buffering them (also in config.toml).
    uploaded = st.file_uploader(t(*label), type=types, key=f"{prefix}_file", max_upload_size=MAX_UPLOAD_MB)
    source = st.text_input(t("数据来源标签（必填，例如 council_2026）", "Source label (required, e.g. council_2026)"),
                           key=f"{prefix}_source")
    if uploaded is None:
        return None, source
    try:
        table = parse_upload(uploaded.getvalue(), kind_from_filename(uploaded.name))
    except UploadError as exc:
        _show_error(t, exc)
        return None, source
    st.caption(t(f"{table.kind.upper()}，{len(table.rows)} 条记录，SHA-256 {table.sha256[:12]}…",
                 f"{table.kind.upper()}, {len(table.rows)} records, SHA-256 {table.sha256[:12]}…"))
    return table, source


def _points_section(t):
    st.subheader(t("1. 点位文件（CSV 或 Point GeoJSON）", "1. Points (CSV or Point GeoJSON)"))
    table, source = _upload(t, "cw_points", ["csv", "geojson", "json"], ("上传点位文件", "Upload point file"))
    if table is None:
        return None
    headers, tag = list(table.headers), table.sha256[:8]
    chosen = {}
    if table.kind == "csv":
        chosen["x"] = _column(t, ("x 列（经度/东坐标）", "x column (longitude/easting)"), headers,
                              f"cw_pmap_x_{tag}", True)
        chosen["y"] = _column(t, ("y 列（纬度/北坐标）", "y column (latitude/northing)"), headers,
                              f"cw_pmap_y_{tag}", True)
    else:
        st.caption(t("GeoJSON 点位使用每个 Point 几何的坐标。", "GeoJSON points use each Point geometry's coordinates."))
    for name, label in POINT_LABELS.items():
        chosen[name] = _column(t, label, headers, f"cw_pmap_{name}_{tag}", False)
    crs, confirmed = _crs(t, "cw_points", ("点位", "Point"), table.sha256)
    if crs is None or not confirmed:
        return None
    try:
        points = load_points(table, PointMapping(**chosen), crs=crs, crs_confirmed=confirmed, source_label=source)
    except UploadError as exc:
        _show_error(t, exc)
        return None
    _quality(t, points)
    return points


def _quality(t, points) -> None:
    s = points.summary
    labels = [("original", "原始记录", "Original"), ("loaded", "已载入", "Loaded"), ("rejected", "已拒绝", "Rejected"),
              ("missing_coordinate", "缺坐标", "Missing coordinate"), ("missing_id", "缺 ID（已生成临时 ID）",
                                                                     "Missing ID (temporary ID generated)"),
              ("duplicate_id", "重复 ID", "Duplicate ID"), ("participating", "参与分配", "Participating")]
    cols = st.columns(len(labels))
    for col, (key, zh, en) in zip(cols, labels):
        col.metric(t(zh, en), s[key])
    rejected = [r for r in points.records if r["status"] == "REJECTED"]
    if rejected:
        st.warning(t(f"{len(rejected)} 条记录被拒绝；它们不参与分配，但会以 REJECTED 状态和原因保留在导出 CSV 中。",
                     f"{len(rejected)} records were rejected. They take no part in the assignment but stay in the "
                     "CSV export with status REJECTED and a reason."))
        st.dataframe(_strings(pd.DataFrame(rejected)[["row_number", "source_id", "original_id", "original_x",
                                                      "original_y", "reason_code", "reason"]]),
                     hide_index=True, width="stretch")
    with st.expander(t("逐条记录的载入结果", "Per-record load results")):
        st.dataframe(_strings(pd.DataFrame(points.records)), hide_index=True, width="stretch")


def _zones_section(t):
    st.subheader(t("2. 边界文件（Polygon/MultiPolygon GeoJSON）", "2. Boundaries (Polygon/MultiPolygon GeoJSON)"))
    table, source = _upload(t, "cw_zones", ["geojson", "json"], ("上传边界文件", "Upload boundary file"))
    if table is None:
        return None
    headers, tag = list(table.headers), table.sha256[:8]
    zone_id = _column(t, ("区域 ID 属性", "Zone ID property"), headers, f"cw_zmap_id_{tag}", True)
    zone_name = _column(t, ("区域名称属性", "Zone name property"), headers, f"cw_zmap_name_{tag}", False)
    crs, confirmed = _crs(t, "cw_zones", ("边界", "Boundary"), table.sha256)
    if crs is None or not confirmed:
        return None
    try:
        zones = load_boundaries(table, ZoneMapping(zone_id, zone_name), crs=crs, crs_confirmed=confirmed,
                                source_label=source)
    except UploadError as exc:
        _show_error(t, exc)
        return None
    cols = st.columns(2)
    cols[0].metric(t("区域数", "Zones"), zones.summary["zones"])
    cols[1].metric(t("顶点总数", "Total vertices"), zones.summary["total_vertices"])
    return zones


def _map(t, zones, rows) -> go.Figure:
    traces = []
    for zone in zones.zones:
        geom = zone.shape
        for polygon in (geom.geoms if geom.geom_type == "MultiPolygon" else [geom]):
            for ring in (polygon.exterior, *polygon.interiors):      # holes drawn as their own outlines
                xs, ys = zip(*ring.coords)
                traces.append(go.Scatter(x=xs, y=ys, mode="lines", showlegend=False, line={"color": "#6085a5"},
                                         hovertemplate=f"{zone.zone_id} · {zone.name}<extra></extra>"))
    for status, colour in ASSIGN_COLOURS.items():
        shown = [r for r in rows if r["status"] == status and r["lon"] is not None]
        if not shown:
            continue
        text = [f"{r['source_id']}" + (f" ({t('原始 ID', 'original ID')}: {r['original_id']})"
                                       if r["original_id"] not in (None, "") else f" ({t('临时 ID', 'temporary ID')})")
                + f"<br>{status}<br>" + (", ".join(f"{m['zone_id']} {m['zone_name']} [{m['relation']}]"
                                                   for m in r["matches"]) or t("无匹配区域", "no zone"))
                for r in shown]
        traces.append(go.Scatter(x=[r["lon"] for r in shown], y=[r["lat"] for r in shown], mode="markers",
                                 name=t(f"{STATUS_ZH[status]} {status}", status), text=text,
                                 hovertemplate="%{text}<extra></extra>",
                                 marker={"color": colour, "size": 9, "line": {"color": "white", "width": 1}}))
    lat0 = sum(z.shape.centroid.y for z in zones.zones) / len(zones.zones)
    fig = go.Figure(traces)
    fig.update_layout(height=520, margin={"l": 10, "r": 10, "t": 10, "b": 10}, plot_bgcolor="#f7fafd",
                      xaxis_title=t("经度 (°)", "Longitude (°)"), yaxis_title=t("纬度 (°)", "Latitude (°)"),
                      legend={"orientation": "h", "y": 1.06})
    fig.update_yaxes(scaleanchor="x", scaleratio=1 / max(cos(radians(lat0)), 0.01))
    return fig


def _results_section(t, points, zones) -> None:
    st.subheader(t("3. 空间分配（无需声明区域）", "3. Spatial assignment (no claims needed)"))
    current = workflow_fingerprint(points, zones) if points is not None and zones is not None else None
    stored = st.session_state.get(RESULT_KEY)
    if stored is not None and stored["fingerprint"] != current:
        del st.session_state[RESULT_KEY]
        stored = None
        st.warning(t("输入、映射或坐标系已更改，之前的结果和下载已失效；请重新运行。",
                     "Inputs, mappings or CRS changed, so the previous result and its downloads were discarded. "
                     "Run again."))
    if current is None:
        st.info(t("请先上传并确认点位与边界文件。", "Upload and confirm both the point and the boundary file first."))
    if st.button(t("运行空间分配", "Run spatial assignment"), key="cw_run", type="primary", disabled=current is None):
        try:
            run = run_assignment(points, zones)
            stored = {"fingerprint": current, "run": run, "csv": to_csv(export_rows(run)),
                      "json": to_json(run_metadata(points, zones, run))}
            st.session_state[RESULT_KEY] = stored
        except ValueError as exc:  # UploadError, AssignInputError, or an export refusing a non-finite value
            _show_error(t, exc)
    if stored is None:
        return
    run = stored["run"]
    counts = run["summary"]["status_counts"]
    cols = st.columns(len(counts))
    for col, (status, count) in zip(cols, counts.items()):
        col.metric(t(f"{STATUS_ZH[status]} {status}", status), count)
    st.plotly_chart(_map(t, zones, run["rows"]), width="stretch", config={"displaylogo": False})
    st.caption(t("状态优先级：无坐标 > 不在任何区域 > 多区域匹配 > 位于边界 > 已分配。位于多个区域共享边界上的点为"
                 "多区域匹配，且各区域关系均为 BOUNDARY。判定为精确几何关系，不含容差；未建模坐标误差或边界版本差异。",
                 run["assignment"]["status_precedence"] + " " + run["assignment"]["uncertainty_note"]))
    table = pd.DataFrame(export_rows(run))
    st.dataframe(_strings(table), hide_index=True, width="stretch")
    cols = st.columns(2)
    cols[0].download_button(t("下载结果 CSV", "Download results CSV"), stored["csv"], "assignment_results.csv",
                            "text/csv", key="cw_dl_csv")
    cols[1].download_button(t("下载运行元数据 JSON", "Download run metadata JSON"), stored["json"],
                            "assignment_run.json", "application/json", key="cw_dl_json")


def render(t) -> None:
    st.title(t("GeoPilot · 自定义数据工作区", "GeoPilot · custom data workspace"))
    st.info(t("上传的文件会经过运行本应用的 Streamlit 服务器，并在服务器上本会话的内存中处理（每个文件上限 5 MB）。"
              "本应用不会主动将上传内容写入磁盘或共享缓存，也不记录文件名；结果只保存在本会话中。"
              "托管环境（服务器日志、备份、网络）不在本应用的控制范围内。"
              "请为每个文件明确选择字段并确认其坐标参考系；不支持 GCJ-02 / BD-09。",
              "Uploaded files pass through the Streamlit server running this app and are processed in that server's "
              "memory for this session (5 MB per file). The app does not intentionally write uploaded content to "
              "disk or to a shared cache, and does not record filenames; results are kept only in this session. "
              "The hosting environment (server logs, backups, network) is outside the app's control. "
              "Map each file's fields and confirm its coordinate reference system explicitly; GCJ-02 and BD-09 are "
              "not supported."))
    points = _points_section(t)
    st.divider()
    zones = _zones_section(t)
    st.divider()
    _results_section(t, points, zones)
    st.divider()
    coverage_ui.render_custom_coverage(t, points, zones)
