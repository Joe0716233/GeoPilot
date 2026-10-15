"""Coverage screening pages: built-in EV-charging / public-toilet examples and coverage on uploaded data.

Both call ``geopilot.decision.plan_coverage`` (the examples through ``run_coverage_example``) with the
same weighted-coverage method. Only the bundled, read-only example inputs are cached (``st.cache_resource``);
uploaded content is never put in a cache.
Uploaded-data results live in ``st.session_state`` under a fingerprint of every input and parameter.
"""

from __future__ import annotations

import re
from math import cos, radians, sqrt

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from geopilot.coverage_examples import (COMMON_NOTES, DEFAULT_PARAMETERS, DEFAULT_WEIGHTS, EXAMPLES,
                                        load_coverage_inputs, run_coverage_example)
from geopilot.decision import DEMAND_METRIC, SUPPORTED_METHODS, ResourceLimits, Role, RoleMapping, ScenarioConfig
from geopilot.reporting import tabular_views, to_csv, to_json
from geopilot.workspace import coverage_exclusions, coverage_fingerprint, run_coverage, workflow_fingerprint

RESULT_KEY = "cv_result"
# Stricter than the library defaults for a shared web app (both the examples and coverage on uploads).
APP_COVERAGE_LIMITS = ResourceLimits(max_entities=20_000, max_grid_cells=20_000, max_target_count=50,
                                     max_pair_evaluations=2_000_000, max_greedy_evaluations=5_000_000,
                                     max_zone_vertices=50_000)
ROLE_LABELS = {None: ("（不映射）", "(not mapped)"), "existing_facility": ("现有设施", "existing facility"),
               "weighted_demand": ("加权需求", "weighted demand"), "context": ("上下文", "context"),
               "ignore": ("忽略（审计中保留）", "ignore (kept in audit)")}
EXAMPLE_NOTES_ZH = {
    "ev_charging": ["默认按原标签使用全部 63 条已保存的 OSM 充电站记录；其 access 标签（5 个 yes、2 个 no、1 个 customers、"
                    "1 个 taxi、54 个缺失）不做筛选，因此默认结果并不说明这些设施对公众开放。可选的保守 access 筛选只保留 "
                    "access=yes/permissive。"],
    "public_toilets": ["仅 access=yes / permissive 的厕所计为可用（100 个中 28 个）；customers/permit/private（18 个）被排除；"
                       "54 个没有 access 标签的默认排除，除非你明确选择纳入。", "未核验开放时间、收费、是否室内及无障碍条件。"]}
COMMON_NOTES_ZH = ["需求权重为合成需求单位（内置合成“人口”点），不是观测到的充电或如厕需求。",
                   "设施为 2026-10-14 保存的 OpenStreetMap 记录（ODbL 1.0，© OpenStreetMap contributors），未核验其当前是否存在或可用。",
                   "位于 Southwark 之外但在其外包矩形内的现有设施会被保留：它们可以覆盖研究区内的需求。",
                   "163 条记录中有 18 条（OSM way）使用外包矩形中心作为坐标，而非入口位置。"]


def _show_error(t, exc: ValueError) -> None:
    code = getattr(exc, "code", "ERROR")
    st.error(t(f"**{code}**：{getattr(exc, 'detail', str(exc))}", f"**{code}**: {getattr(exc, 'detail', str(exc))}"))

    context = getattr(exc, "context", None)
    if context:
        with st.expander(t("完整错误详情（用于修正输入）", "Full error details (to correct the input)")):
            st.json(context)


def _strings(frame: pd.DataFrame) -> pd.DataFrame:
    for column in frame.columns:
        if frame[column].dtype == object:
            frame[column] = frame[column].map(lambda v: "" if v is None else str(v))
    return frame


def _parameters(t, prefix: str) -> dict:
    d = DEFAULT_PARAMETERS
    cols = st.columns(5)
    params = {
        "target_count": int(cols[0].number_input(t("新增站点数", "New sites"), 1, 50, d["target_count"], 1,
                                                 key=f"{prefix}_target")),
        "grid_step_m": cols[1].number_input(t("网格步长（米）", "Grid step (m)"), 100.0, 5000.0,
                                            float(d["grid_step_m"]), 50.0, key=f"{prefix}_step"),
        "service_radius_m": cols[2].number_input(t("服务半径（米）", "Service radius (m)"), 50.0, 5000.0,
                                                 float(d["service_radius_m"]), 50.0, key=f"{prefix}_radius"),
        "min_existing_distance_m": cols[3].number_input(t("与现有设施最小距离（米）", "Min distance to existing (m)"),
                                                        0.0, 5000.0, float(d["min_existing_distance_m"]), 50.0,
                                                        key=f"{prefix}_min_exist"),
        "min_new_site_distance_m": cols[4].number_input(t("新站点之间最小距离（米）", "Min distance between new (m)"),
                                                        0.0, 5000.0, float(d["min_new_site_distance_m"]), 50.0,
                                                        key=f"{prefix}_min_new")}
    params["method"] = st.selectbox(t("方法（仅支持加权覆盖）", "Method (only weighted coverage is supported)"),
                                    SUPPORTED_METHODS, key=f"{prefix}_method")
    return params


def _outline(zone) -> list:
    traces, geom = [], zone.shape
    for polygon in (geom.geoms if geom.geom_type == "MultiPolygon" else [geom]):
        for ring in (polygon.exterior, *polygon.interiors):
            xs, ys = zip(*ring.coords)
            traces.append(go.Scatter(x=xs, y=ys, mode="lines", showlegend=False, line={"color": "#c0392b", "width": 2},
                                     hovertemplate=f"{zone.zone_id} · {zone.name}<extra></extra>"))
    return traces


def _map(t, result: dict, zone) -> go.Figure:
    traces = _outline(zone)
    candidates = result["candidates"]
    traces.append(go.Scatter(x=[c["lon"] for c in candidates], y=[c["lat"] for c in candidates], mode="markers",
                             name=t("可行候选点", "Feasible candidates"), marker={"color": "#9bb7d4", "size": 6}))
    rows = result["inputs"]
    demand = [r for r in rows if r["role"] == "weighted_demand" and r["status"] == "USED"]
    top = max((r["demand_weight"] for r in demand), default=0) or 1
    traces.append(go.Scatter(x=[r["lon"] for r in demand], y=[r["lat"] for r in demand], mode="markers",
                             name=t("需求点（合成需求单位）" if result.get("example") else "需求点",
                                    "Demand (synthetic units)" if result.get("example") else "Demand"),
                             marker={"color": "rgba(80,160,120,0.55)",
                                     "size": [4 + 14 * sqrt(r["demand_weight"] / top) for r in demand]},
                             text=[f"{r['source_id']}: {r['demand_weight']:g}" for r in demand],
                             hovertemplate="%{text}<extra></extra>"))
    outside = [r for r in rows if r["status"] == "OUTSIDE_STUDY_AREA" and r["lon"] is not None]
    if outside:
        traces.append(go.Scatter(x=[r["lon"] for r in outside], y=[r["lat"] for r in outside], mode="markers",
                                 name=t("研究区外需求（不计入）", "Demand outside study area (excluded)"),
                                 marker={"color": "#bbbbbb", "size": 5}))
    excluded = [a for a in (result.get("eligibility") or {}).get("audit", []) if a["decision"].startswith("EXCLUDED")]
    if excluded:
        traces.append(go.Scatter(x=[a["lon"] for a in excluded], y=[a["lat"] for a in excluded], mode="markers",
                                 name=t("被排除的设施", "Excluded facilities"),
                                 marker={"color": "#999999", "size": 7, "symbol": "x"},
                                 text=[f"{a['source_id']} · {a['category']} · {a['reason']}" for a in excluded],
                                 hovertemplate="%{text}<extra></extra>"))
    existing = [r for r in rows if r["role"] == "existing_facility"]
    traces.append(go.Scatter(x=[r["lon"] for r in existing], y=[r["lat"] for r in existing], mode="markers",
                             name=t("参与的现有设施", "Participating existing facilities"),
                             marker={"color": "#333333", "size": 9, "symbol": "square"},
                             text=[f"{r['source_id']} · {r['category']}" + (f" · {r['name']}" if r["name"] else "")
                                   for r in existing], hovertemplate="%{text}<extra></extra>"))
    selected = result["selected"]
    traces.append(go.Scatter(x=[c["lon"] for c in selected], y=[c["lat"] for c in selected], mode="markers+text",
                             name=t("入选站点", "Selected sites"), text=[str(c["rank"]) for c in selected],
                             textposition="top center",
                             marker={"color": "#d74f46", "size": 16, "line": {"color": "white", "width": 1}}))
    fig = go.Figure(traces)
    fig.update_layout(height=560, margin={"l": 10, "r": 10, "t": 10, "b": 10}, plot_bgcolor="#f7fafd",
                      xaxis_title=t("经度 (°)", "Longitude (°)"), yaxis_title=t("纬度 (°)", "Latitude (°)"),
                      legend={"orientation": "h", "y": 1.08})
    fig.update_yaxes(scaleanchor="x", scaleratio=1 / max(cos(radians(zone.shape.centroid.y)), 0.01))
    return fig


def render_coverage_result(t, result: dict, zone, prefix: str) -> None:
    pct = lambda v: "n/a" if v is None else f"{v:.1f} %"  # noqa: E731
    (st.success if result["status"] == "SUCCESS" else st.warning)(result["message"])
    cols = st.columns(4)
    cols[0].metric(t("参与的现有设施", "Participating facilities"), result["existing_facility_count"])
    cols[1].metric(t("研究区内需求点", "Demand points in study area"), result["demand_point_count"])
    cols[2].metric(t("需求权重合计", "Total demand weight"), f"{result['total_demand_weight']:g}")
    cols[3].metric(t("上下文点", "Context points"), result["context_point_count"])
    # Disjoint stages: eligibility-excluded facilities and unusable upload rows never reach plan_coverage,
    # whose own exclusions are ignored records and demand outside the study area.
    ineligible = sum(1 for a in (result.get("eligibility") or {}).get("audit", [])
                     if a["decision"].startswith("EXCLUDED"))
    unusable = len((result.get("upload") or {}).get("excluded_rows", []))
    core = len(result["exclusions"])
    cols = st.columns(4)
    cols[0].metric(t("排除/忽略的记录（全部环节）", "Excluded or ignored records (all stages)"), ineligible + unusable + core,
                   help=t(f"资格筛选排除 {ineligible}；不可用的上传行 {unusable}；覆盖计算中忽略或位于研究区外 {core}",
                          f"eligibility filter {ineligible}; unusable uploaded rows {unusable}; ignored or outside the "
                          f"study area in the coverage run {core}"))
    cols[1].metric(t("研究区外需求点", "Demand outside study area"), result["demand_points_outside_study_area"])
    cols[2].metric(t("网格候选点", "Grid candidates"), result["generated_candidate_count"])
    cols[3].metric(t("可行候选点", "Feasible candidates"), result["feasible_candidate_count"])
    cols = st.columns(3)
    cols[0].metric(t("现有设施覆盖", "Covered by existing"), pct(result["existing_coverage_pct"]),
                   help=f"{result['existing_demand_weight_covered']:g} / {result['total_demand_weight']:g}")
    cols[1].metric(t("新增站点带来的覆盖", "Added by new sites"), pct(result["new_incremental_coverage_pct"]),
                   help=f"{result['new_incremental_demand_weight_covered']:g}")
    cols[2].metric(t("总覆盖", "Total covered"), pct(result["final_total_coverage_pct"]),
                   help=f"{result['final_total_demand_weight_covered']:g}")
    st.plotly_chart(_map(t, result, zone), width="stretch", config={"displaylogo": False}, key=f"{prefix}_map")
    st.caption(t(f"方法：{result['method']}。覆盖 = 本地米制投影中直线距离 ≤ {result['coverage_radius_m']:g} 米（不是出行时间）；"
                 "研究区外的现有设施同样计入覆盖。贪心筛选不声称最优。",
                 f"Method: {result['method']}. Coverage = straight-line distance ≤ {result['coverage_radius_m']:g} m in a "
                 "local metric projection (not travel time); existing facilities outside the study area also cover "
                 "demand inside it. Greedy screening makes no optimality claim."))
    views = tabular_views("coverage", result)
    if views["coverage_selected"]:
        st.dataframe(pd.DataFrame(views["coverage_selected"]), hide_index=True, width="stretch")
    if result.get("eligibility"):
        audit = result["eligibility"]["audit"]
        excluded = sum(1 for a in audit if a["decision"].startswith("EXCLUDED"))
        unknown_in = sum(1 for a in audit if a["decision"] == "INCLUDED_UNKNOWN")
        with st.expander(t(f"设施资格审计（排除 {excluded} 条；按选择纳入 access 未知的 {unknown_in} 条）",
                           f"Facility eligibility audit ({excluded} excluded; {unknown_in} with unknown access "
                           "included by opt-in)")):
            st.dataframe(_strings(pd.DataFrame(audit)[["source_id", "category", "name", "value", "decision",
                                                       "reason"]]), hide_index=True, width="stretch")
    with st.expander(t("逐条输入审计（角色、状态、原始类别）", "Per-record input audit (role, status, original category)")):
        st.dataframe(_strings(pd.DataFrame(views["coverage_inputs"])), hide_index=True, width="stretch")
    cols = st.columns(1 + len(views))
    cols[0].download_button(t("下载 JSON（含运行配置）", "Download JSON (with run config)"), to_json(result),
                            "coverage.json", "application/json", key=f"{prefix}_dl_json")
    for col, (name, rows) in zip(cols[1:], views.items()):
        col.download_button(t(f"下载 {name}.csv", f"Download {name}.csv"), to_csv(rows), f"{name}.csv", "text/csv",
                            key=f"{prefix}_dl_{name}")


@st.cache_resource
def _example_inputs():
    return load_coverage_inputs()     # bundled, read-only example files only


def _reset_example_access() -> None:
    # Conditional widgets can otherwise restore a previous server value while displaying their default.
    # Each scenario switch starts from its documented access assumptions; opt-ins must be explicit again.
    st.session_state["ce_conservative_access"] = False
    st.session_state["ce_include_unknown"] = False


def render_examples(t) -> None:
    st.title(t("GeoPilot · 覆盖分析示例", "GeoPilot · coverage examples"))
    key = st.radio(t("示例", "Example"), list(EXAMPLES), key="ce_example", horizontal=True,
                   format_func=lambda k: t(EXAMPLES[k].title_zh, EXAMPLES[k].title_en), on_change=_reset_example_access)
    example = EXAMPLES[key]
    notes = t(EXAMPLE_NOTES_ZH[key] + COMMON_NOTES_ZH, [*example.notes, *COMMON_NOTES])
    st.warning("\n".join(f"* {note}" for note in notes))
    st.caption(t("两个示例调用同一个 plan_coverage 函数与同一套加权覆盖算法；唯一差别是选用的设施类别和通用的资格筛选。",
                 "Both examples call the same plan_coverage function and weighted-coverage algorithm; they differ "
                 "only in the facility category and a generic eligibility filter."))
    params = _parameters(t, "ce")
    cols = st.columns(4)
    weights = {DEMAND_METRIC: cols[0].number_input(t("权重：需求", "Weight: demand"), 0.0, 1.0,
                                                   DEFAULT_WEIGHTS[DEMAND_METRIC], 0.05, key="ce_w_demand")}
    for col, metric in zip(cols[1:], ("residential", "commercial", "transit")):
        weights[metric] = col.number_input(t(f"权重：{metric}", f"Weight: {metric}"), 0.0, 1.0,
                                           DEFAULT_WEIGHTS[metric], 0.05, key=f"ce_w_{metric}")
    conservative = False
    if example.optional_eligibility is not None:
        conservative = st.checkbox(
            t("保守的 access 筛选：仅计入 access=yes/permissive；排除已知受限（no/customers/taxi/private/permit）及缺失标签",
              "Conservative access filter: count only access=yes/permissive; exclude known restrictions "
              "(no/customers/taxi/private/permit) and missing tags"), value=False, key="ce_conservative_access")
    include_unknown = False
    if example.eligibility is not None or conservative:
        include_unknown = st.checkbox(t("纳入 access 标签缺失的设施（不推荐）", "Include facilities with no access tag "
                                        "(not recommended)"), value=False, key="ce_include_unknown")
        if include_unknown:
            st.warning(t("已纳入 access 未知的设施：它们可能并不对公众开放，覆盖结果会因此偏高。",
                         "Facilities with unknown access are included: they may not be open to the public, so "
                         "coverage may be overstated."))
    try:
        inputs = _example_inputs()
        result = run_coverage_example(key, inputs=inputs, include_unknown_access=include_unknown, weights=weights,
                                      conservative_access=conservative, limits=APP_COVERAGE_LIMITS, **params)
    except (ValueError, FileNotFoundError) as exc:
        _show_error(t, exc)
        return
    summary = result["access_summary"]
    if result["eligibility"]:
        c = result["eligibility"]["counts"]
        cols = st.columns(4)
        cols[0].metric(t("设施记录", "Facility records"), c["records"])
        cols[1].metric(t("可用（计入）", "Eligible (used)"), c["ELIGIBLE"] + c["INCLUDED_UNKNOWN"])
        cols[2].metric(t("受限（排除）", "Restricted (excluded)"), c["EXCLUDED_RESTRICTED"] + c["EXCLUDED_OTHER_VALUE"])
        cols[3].metric(t("access 未知", "Access unknown"), c["EXCLUDED_UNKNOWN"] + c["INCLUDED_UNKNOWN"])
    else:
        st.info(t(f"默认按 OSM 标签原样使用全部 {summary['records']} 条记录；这不表示它们对公众开放。"
                  "可勾选上方的保守 access 筛选。",
                  f"By default all {summary['records']} records are used as tagged; this does not mean they are "
                  "publicly available. Tick the conservative access filter above to keep only confirmed access."))
        cols = st.columns(4)
        cols[0].metric(t("设施记录（全部计入）", "Facility records (all used)"), summary["records"])
        cols[1].metric(t("access=yes/permissive", "Access yes/permissive"), summary["confirmed"])
        cols[2].metric(t("已知受限 access", "Known restricted access"), summary["known_restricted"])
        cols[3].metric(t("access 缺失/未知", "Access missing/unknown"), summary["unknown"])
    st.caption(t("OSM access 标签计数：", "OSM access tag counts: ")
               + ", ".join(f"{t('缺失', 'missing') if value == '(missing)' else value} {count}"
                           for value, count in summary["values"].items())
               + t("。仅为已保存的 OSM 标签，不证明当前可公开使用。", f". {summary['note']}"))
    render_coverage_result(t, result, inputs.zone, "ce")


def _metric_name(category: str) -> str:
    name = re.sub(r"[^a-z0-9_]+", "_", category.lower()).strip("_")
    return name if re.fullmatch(r"[a-z][a-z0-9_]*", name or "") else f"m_{name}".rstrip("_")


def render_custom_coverage(t, points, zones) -> None:
    st.subheader(t("4. 覆盖筛选（加权覆盖）", "4. Coverage screening (weighted coverage)"))
    if points is None or zones is None:
        st.session_state.pop(RESULT_KEY, None)
        st.info(t("请先载入并确认点位与边界文件。", "Load and confirm both the point and the boundary file first."))
        return
    tag = workflow_fingerprint(points, zones)[:10]
    zone_names = {z.zone_id: z.name for z in zones.zones}
    zone_id = st.selectbox(t("研究区", "Study zone"), list(zone_names), index=None, key=f"cv_zone_{tag}",
                           format_func=lambda z: f"{z} · {zone_names[z]}", placeholder=t("请选择区域", "Choose a zone"))
    categories = sorted({e.category for e in points.entities if e.has_point})
    if "" in categories:
        st.warning(t("部分记录类别为空，无法分配角色；请在第 1 步映射类别列。",
                     "Some records have a blank category and cannot get a role; map a category column in step 1."))
    st.caption(t("为每个类别明确选择角色；未映射的类别会被拒绝（UNMAPPED_ROLE）。",
                 "Choose a role for every category explicitly; unmapped categories are refused (UNMAPPED_ROLE)."))
    roles, metrics = {}, {}
    for index, category in enumerate(c for c in categories if c):
        cols = st.columns(2)
        role = cols[0].selectbox(t(f"类别「{category}」的角色", f"Role of category '{category}'"),
                                 [None, *(r.value for r in Role)], index=0, key=f"cv_role_{tag}_{index}",
                                 format_func=lambda r: t(*ROLE_LABELS[r]))
        if role is not None:
            roles[category] = role
        if role == "context":
            metrics[category] = cols[1].text_input(t("上下文指标名", "Context metric name"), _metric_name(category),
                                                   key=f"cv_metric_{tag}_{index}")
    fields = list(dict.fromkeys(k for e in points.entities for k in e.properties))
    default_field = fields.index(points.mapping.demand_weight) if points.mapping.demand_weight in fields else None
    weight_field = st.selectbox(t("需求权重字段", "Demand weight field"), fields, index=default_field,
                                key=f"cv_field_{tag}", placeholder=t("请选择字段", "Choose a field"))
    cols = st.columns(1 + len(set(metrics.values())))
    weights = {DEMAND_METRIC: cols[0].number_input(t("权重：需求", "Weight: demand"), 0.0, 1.0, 1.0, 0.05,
                                                   key=f"cv_w_demand_{tag}")}
    for col, metric in zip(cols[1:], sorted(set(metrics.values()))):
        weights[metric] = col.number_input(t(f"权重：{metric}", f"Weight: {metric}"), 0.0, 1.0, 0.0, 0.05,
                                           key=f"cv_w_{tag}_{metric}")
    params = _parameters(t, "cv")
    allow_zero = st.checkbox(t("声明没有现有设施（零基线）", "Declare that there are no existing facilities (zero baseline)"),
                             value=False, key="cv_zero")
    excluded = coverage_exclusions(points)
    acknowledged = False
    if excluded:
        st.warning(t(f"{len(excluded)} 条上传记录（被拒绝或无坐标）不能参与覆盖分析。",
                     f"{len(excluded)} uploaded rows (rejected or without coordinates) cannot take part in coverage."))
        st.dataframe(_strings(pd.DataFrame(excluded)), hide_index=True, width="stretch")
        acknowledged = st.checkbox(t("我确认在覆盖分析中排除这些记录", "I acknowledge these rows are excluded from coverage"),
                                   value=False, key=f"cv_ack_{tag}")
    config = None
    if zone_id is None or not roles or weight_field is None:
        st.info(t("请选择研究区、至少一个类别角色和需求权重字段。",
                  "Choose a study zone, at least one category role and the demand weight field."))
    else:
        try:
            config = ScenarioConfig(roles=RoleMapping(roles, context_metrics=metrics), demand_weight_field=weight_field,
                                    weights=weights, allow_zero_existing=allow_zero, limits=APP_COVERAGE_LIMITS,
                                    **params)
        except ValueError as exc:
            _show_error(t, exc)
    current = (coverage_fingerprint(points, zones, zone_id, config, acknowledged) if config is not None else None)
    stored = st.session_state.get(RESULT_KEY)
    if stored is not None and stored["fingerprint"] != current:
        del st.session_state[RESULT_KEY]
        stored = None
        st.warning(t("覆盖输入或参数已更改，之前的覆盖结果和下载已失效；请重新运行。",
                     "Coverage inputs or parameters changed, so the previous coverage result and its downloads were "
                     "discarded. Run again."))
    unmapped = [c for c in categories if c and c not in roles]
    if unmapped and config is not None:
        st.info(t("仍未映射的类别：", "Categories still unmapped: ") + ", ".join(unmapped))
    blocked = current is None or (bool(excluded) and not acknowledged)
    if st.button(t("运行覆盖筛选", "Run coverage screening"), key="cv_run", type="primary", disabled=blocked):
        try:
            stored = {"fingerprint": current,
                      "result": run_coverage(points, zones, zone_id, config, acknowledge_exclusions=acknowledged)}
            st.session_state[RESULT_KEY] = stored
        except ValueError as exc:
            _show_error(t, exc)
    if stored is not None:
        zone = next(z for z in zones.zones if z.zone_id == stored["result"]["study_zone_id"])
        render_coverage_result(t, stored["result"], zone, "cv")
