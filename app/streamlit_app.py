"""Integrated offline GeoPilot demo: GeoVerify, GeoResolve and GeoDecision on the London sample.

Run from the repository root:  streamlit run app/streamlit_app.py
No network access or map tiles are needed; maps are plain lon/lat plots.

The page is Simplified Chinese by default; a sidebar selector switches to English. Only display text is
translated: result fields, export files and all numerical results are identical in both languages.
"""

from __future__ import annotations

from math import cos, radians

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from geopilot.decision import DecisionConfig
from geopilot.reporting import tabular_views, to_csv, to_json
from geopilot.resolve import ResolveConfig
from geopilot.scenario import (SAMPLE_NOTICE, load_london_sample, run_decision, run_resolve,
                               run_verify)
from geopilot.verify import VerifyConfig

REPO_URL = "https://github.com/Joe0716233/GeoPilot"
STATUS_COLOURS = {"PASS": "#2b9b70", "MATCH": "#2b9b70", "REVIEW": "#e5a63b", "FAIL": "#d74f46",
                  "NO_MATCH": "#8b929d", "UNRESOLVED": "#8b929d"}

# Chinese display names for status codes (the codes themselves stay visible and unchanged in data).
STATUS_ZH = {"PASS": "通过", "REVIEW": "待复核", "FAIL": "不一致", "UNRESOLVED": "无法判定",
             "MATCH": "强候选", "NO_MATCH": "不匹配"}
REASON_ZH = {
    "CLAIM_CONFIRMED": "点位位于所声明行政区内，且距边界超过复核边距",
    "INSIDE_NEAR_BOUNDARY": "位于所声明行政区内，但距边界在复核边距以内",
    "OUTSIDE_NEAR_BOUNDARY": "位于所声明行政区外，但距边界在不一致阈值以内",
    "CLAIM_CONTRADICTED": "位于另一个已提供的行政区内，与声明矛盾",
    "OUTSIDE_ALL_SUPPLIED_ZONES": "不在任何已提供的行政区内（可能只是区域未包含在样例中）",
    "OVERLAPPING_ZONES": "同时落入多个重叠区域",
    "UNKNOWN_CLAIMED_ZONE": "所声明的区域不在已提供的区域列表中",
    "NO_CLAIM": "记录没有声明区域",
    "NO_COORDINATE": "记录没有坐标",
}
UNCERTAINTY_ZH = ("距离为所给坐标到所给边界几何的直线米数；未建模坐标误差，也未建模不同边界版本之间的差异。")
RESOLVE_LIMITS_ZH = [
    "分数是固定权重的启发式分数且没有标注数据；MATCH 只是候选，不代表已确认同一实体。",
    "相距很近的点可能是不同站点；REVIEW / NO_MATCH 的配对不会被自动合并。",
    "没有坐标的记录只能通过名称前 4 个字符分块进行比较。",
]
DECISION_LIMITS_ZH = [
    "采用本地米制投影中的直线（欧氏）距离，不是出行时间，也不是路网可达性。",
    "需求点与上下文点由调用方提供；内置样例中的需求点是合成数据。",
    "未建模电网容量、土地权属、地块、规划许可或成本约束。",
    "贪心选址只是筛选启发式，不声称最优；未选满目标数量并不证明不存在可行方案。",
]

st.set_page_config(page_title="GeoPilot v1.0", layout="wide")

with st.sidebar:
    lang = st.selectbox("语言 / Language", ["zh", "en"], key="lang",
                        format_func={"zh": "简体中文", "en": "English"}.get)


def t(zh: str, en: str) -> str:
    return zh if lang == "zh" else en


def status_name(code: str) -> str:
    return f"{STATUS_ZH[code]} {code}" if lang == "zh" else code


@st.cache_resource
def sample():
    return load_london_sample()


def zone_traces(zones, highlight=None):
    traces = []
    for zone in zones:
        geom = zone.shape
        polygons = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
        for polygon in polygons:
            xs, ys = zip(*polygon.exterior.coords)
            on = zone.zone_id == highlight
            traces.append(go.Scatter(x=xs, y=ys, mode="lines", name=zone.name, showlegend=False,
                                     line={"color": "#c0392b" if on else "#6085a5", "width": 2.5 if on else 1},
                                     fill="toself", fillcolor="rgba(192,57,43,0.08)" if on else "rgba(130,175,220,0.10)",
                                     hovertemplate=zone.name + "<extra></extra>"))
    return traces


def figure(traces, lat0):
    fig = go.Figure(traces)
    fig.update_layout(height=560, margin={"l": 10, "r": 10, "t": 10, "b": 10}, plot_bgcolor="#f7fafd",
                      xaxis_title=t("经度 (°)", "Longitude (°)"), yaxis_title=t("纬度 (°)", "Latitude (°)"),
                      legend={"orientation": "h", "y": 1.06})
    # Equal metres per axis unit: one degree of longitude is cos(lat) times one of latitude.
    fig.update_yaxes(scaleanchor="x", scaleratio=1 / cos(radians(lat0)))
    return fig


def downloads(module: str, result: dict) -> None:
    views = tabular_views(module, result)
    cols = st.columns(1 + len(views))
    cols[0].download_button(t("下载 JSON", "Download JSON"), to_json(result), f"{module}.json",
                            "application/json", key=f"{module}-json")
    for col, (name, rows) in zip(cols[1:], views.items()):
        col.download_button(t(f"下载 {name}.csv", f"Download {name}.csv"), to_csv(rows), f"{name}.csv",
                            "text/csv", key=f"{name}-csv")


def decision_message(d: dict) -> str:
    if lang == "en":
        return d["message"]
    if d["status"] == "SUCCESS":
        return f"计划选出 {d['requested_count']} 个站点，已全部选出。"
    return (f"计划选出 {d['requested_count']} 个站点，实际选出 {d['actual_count']} 个。"
            "已保持硬性距离约束；这并不表示数学上不存在可行方案。")


st.title(t("GeoPilot v1.0 · 空间核验、实体消歧与选址筛选", "GeoPilot v1.0 · spatial verification, resolution and screening"))
st.caption(t(f"演示项目，源码与说明： [{REPO_URL}]({REPO_URL})", f"Demo project; source and documentation: [{REPO_URL}]({REPO_URL})"))
st.warning(t(
    "**数据与结论边界：** 真实输入为历史开放数据（GLA，获取日期 2026-09-27），可能已过时；边界探针、运营商数据源和需求点"
    "均为**合成数据**。结果仅用于演示方法，**不代表**当前伦敦充电设施的可用性，也不是任何真实选址的可行性、准确性或"
    "已验证结论。", SAMPLE_NOTICE))
try:
    data = sample()
except FileNotFoundError as exc:
    st.error(str(exc))
    st.stop()
centre_lat = data.study_zone.shape.centroid.y

with st.sidebar:
    st.header(t("参数", "Parameters"))
    st.subheader("GeoVerify")
    review_m = st.number_input(t("边界复核边距（米）", "Review margin at boundary (m)"), 0.0, 1000.0, 20.0, 5.0, key="review_m")
    fail_m = st.number_input(t("区域外超过多少米判为不一致", "Fail beyond (m outside)"), 0.0, 5000.0, 50.0, 10.0, key="fail_m")
    st.subheader("GeoResolve")
    radius = st.number_input(t("候选半径（米）", "Candidate radius (m)"), 10.0, 2000.0, 300.0, 10.0, key="radius")
    match_s = st.slider(t("MATCH 分数 ≥", "MATCH score ≥"), 0.5, 1.0, 0.78, 0.01, key="match_s")
    review_s = st.slider(t("REVIEW 分数 ≥", "REVIEW score ≥"), 0.0, 0.95, 0.55, 0.01, key="review_s")
    st.subheader("GeoDecision")
    target = st.number_input(t("新增站点数", "New sites"), 1, 15, 3, key="target")
    service = st.number_input(t("服务半径（米）", "Service radius (m)"), 100.0, 3000.0, 900.0, 50.0, key="service")
    min_exist = st.number_input(t("与现有站点最小距离（米）", "Min distance to existing (m)"), 0.0, 5000.0, 700.0, 50.0, key="min_exist")
    min_new = st.number_input(t("新站点之间最小距离（米）", "Min distance between new (m)"), 0.0, 5000.0, 600.0, 50.0, key="min_new")
    step = st.number_input(t("网格步长（米）", "Grid step (m)"), 100.0, 2000.0, 500.0, 50.0, key="step")
    st.subheader(t("评分权重", "Score weights"))
    weight_labels = {"population": t("人口", "Population"), "residential": t("居住", "Residential"),
                     "commercial": t("商业", "Commercial"), "transit": t("交通", "Transit")}
    weights = {k: st.slider(weight_labels[k], 0.0, 1.0, v, 0.05, key=f"w_{k}")
               for k, v in {"population": .45, "residential": .20, "commercial": .20, "transit": .15}.items()}

try:
    verify = run_verify(data, VerifyConfig(review_m, fail_m))
    resolve = run_resolve(data, ResolveConfig(match_s, review_s, radius))
    decision = run_decision(data, DecisionConfig(int(target), step, service, min_exist, min_new, weights))
except ValueError as exc:
    st.error(t(f"参数无效：{exc}", f"Invalid parameters: {exc}"))
    st.stop()

tab_v, tab_r, tab_d, tab_about = st.tabs(
    ["GeoVerify", "GeoResolve", "GeoDecision", t("数据与局限", "Data & limits")])

with tab_v:
    st.subheader(t("每个点是否位于其所声明的行政区内？", "Is each point inside the borough it claims?"))
    summary = verify["summary"]
    cols = st.columns(4)
    for col, status in zip(cols, ("PASS", "REVIEW", "FAIL", "UNRESOLVED")):
        col.metric(status_name(status), summary["status_counts"][status])
    entities = {e.key: e for e in data.chargers + data.probes}
    traces = zone_traces(data.zones)
    for status in ("PASS", "REVIEW", "FAIL", "UNRESOLVED"):
        rows = [r for r in verify["results"] if r["status"] == status]
        traces.append(go.Scatter(
            x=[entities[r["record_id"]].lon for r in rows], y=[entities[r["record_id"]].lat for r in rows],
            mode="markers", name=status_name(status),
            marker={"color": STATUS_COLOURS[status], "size": 8, "line": {"color": "white", "width": 1}},
            text=[t(f"{r['record_id']}<br>声明区域：{r['claimed_zone']}<br>带符号距离 {r['signed_distance_m']} 米",
                    f"{r['record_id']}<br>claimed {r['claimed_zone']}<br>signed distance {r['signed_distance_m']} m")
                  for r in rows], hovertemplate="%{text}<extra></extra>"))
    st.plotly_chart(figure(traces, centre_lat), width="stretch", config={"displaylogo": False})
    st.caption(t("带符号距离：在所声明行政区内为正，区域外为负，单位为直线米数。" + UNCERTAINTY_ZH,
                 "Signed distance: positive inside the claimed borough, negative outside, in straight-line metres. "
                 + summary["uncertainty_note"]))
    frame = pd.DataFrame(verify["results"]).drop(columns=["uncertainty_note", "review_boundary_m", "fail_outside_m"])
    frame["actual_zones"] = frame["actual_zones"].map(", ".join)
    if lang == "zh":
        frame = frame.rename(columns={
            "record_id": "记录编号", "name": "名称", "claimed_zone": "声明区域", "actual_zones": "实际所在区域",
            "signed_distance_m": "带符号距离（米）", "boundary_distance_m": "到边界距离（米）",
            "status": "状态", "reason_code": "原因代码"})
    status_col = t("状态", "status")
    shown = st.multiselect(t("按状态筛选", "Status filter"), ["PASS", "REVIEW", "FAIL", "UNRESOLVED"],
                           ["REVIEW", "FAIL"], key="verify_filter")
    st.dataframe(frame[frame[status_col].isin(shown)], hide_index=True, width="stretch")
    if lang == "zh":
        with st.expander("原因代码说明"):
            st.table(pd.DataFrame({"原因代码": list(REASON_ZH), "含义": list(REASON_ZH.values())}))
    downloads("verify", verify)

with tab_r:
    st.subheader(t("历史 GLA 记录与合成运营商数据源是否描述同一站点？",
                   "Do the historic GLA records and the synthetic operator feed describe the same sites?"))
    s = resolve["summary"]
    cols = st.columns(4)
    cols[0].metric(t("候选配对", "Candidate pairs"), s["candidate_pairs"])
    cols[1].metric(status_name("MATCH"), s["status_counts"]["MATCH"])
    cols[2].metric(status_name("REVIEW"), s["status_counts"]["REVIEW"])
    cols[3].metric(status_name("NO_MATCH"), s["status_counts"]["NO_MATCH"])
    st.caption(t("MATCH 只表示在固定启发式评分下的强候选，**不代表已确认为同一实体**；没有人工标注真值，"
                 "因此不报告精确率或召回率。",
                 "MATCH = strong candidate under a fixed heuristic score, not confirmed identity; no labelled truth "
                 "exists, so no precision or recall is reported."))
    pairs = pd.DataFrame([{"left": p["left"], "right": p["right"], "status": p["status"], "score": p["score"],
                           "distance_m": p["distance_m"], "name": p["features"]["name"],
                           "address": p["features"]["address"], "reason": p["review_reason"] or p["fusion_reason"]}
                          for p in resolve["pairs"]])
    clusters = pd.DataFrame(resolve["clusters"])
    clusters["member_keys"] = clusters["member_keys"].map(", ".join)
    if lang == "zh":
        pairs = pairs.rename(columns={"left": "左侧记录", "right": "右侧记录", "status": "状态",
                                      "score": "启发式分数", "distance_m": "距离（米）", "name": "名称相似度",
                                      "address": "地址相似度", "reason": "原因"})
        clusters = clusters.rename(columns={"canonical_key": "代表记录", "member_keys": "成员记录",
                                            "size": "成员数", "needs_review": "需复核"})
    st.dataframe(pairs, hide_index=True, width="stretch")
    st.markdown(t("**聚类（代表记录及其成员）**", "**Clusters (canonical record and members)**"))
    only_multi = st.checkbox(t("仅显示成员多于 1 个的聚类", "Only clusters with more than one member"), True,
                             key="only_multi")
    size_col = t("成员数", "size")
    st.dataframe(clusters[clusters[size_col] > 1] if only_multi else clusters, hide_index=True, width="stretch")
    st.caption(t("局限：" + " ".join(RESOLVE_LIMITS_ZH), "Limits: " + " ".join(resolve["limitations"])))
    downloads("resolve", resolve)

with tab_d:
    st.subheader(t(f"在 {decision['study_zone']} 基于合成需求点筛选站点",
                   f"Screening sites in {decision['study_zone']} on synthetic demand"))
    (st.success if decision["status"] == "SUCCESS" else st.warning)(decision_message(decision))
    cols = st.columns(3)
    pct = lambda v: "n/a" if v is None else f"{v:.1f} %"  # noqa: E731
    cols[0].metric(t("现有站点覆盖（合成人口）", "Covered by existing (synthetic pop.)"), pct(decision["existing_coverage_pct"]))
    cols[1].metric(t("新增站点带来的覆盖", "Added by new sites"), pct(decision["new_incremental_coverage_pct"]))
    cols[2].metric(t("总覆盖", "Total covered"), pct(decision["final_total_coverage_pct"]))
    zone = data.study_zone
    traces = zone_traces([zone], highlight=zone.zone_id)
    traces.append(go.Scatter(x=[c["lon"] for c in decision["candidates"]], y=[c["lat"] for c in decision["candidates"]],
                             mode="markers", name=t("可行候选点", "Feasible candidate"),
                             marker={"color": "#9bb7d4", "size": 6}))
    traces.append(go.Scatter(x=[e.lon for e in data.chargers], y=[e.lat for e in data.chargers], mode="markers",
                             name=t("现有站点（GLA 历史数据）", "Existing (historic GLA)"),
                             marker={"color": "#555", "size": 9, "symbol": "square"}))
    demand = [e for e in data.planning_context if e.category == "population"]
    traces.append(go.Scatter(x=[e.lon for e in demand], y=[e.lat for e in demand], mode="markers",
                             name=t("合成需求点", "Synthetic demand"),
                             marker={"color": "rgba(80,160,120,0.5)",
                                     "size": [max(4, float(e.properties["population"]) / 40) for e in demand]},
                             hovertemplate=t("人口 %{text}", "population %{text}") + "<extra></extra>",
                             text=[e.properties["population"] for e in demand]))
    traces.append(go.Scatter(x=[c["lon"] for c in decision["selected"]], y=[c["lat"] for c in decision["selected"]],
                             mode="markers+text", name=t("入选站点", "Selected site"),
                             text=[str(c["rank"]) for c in decision["selected"]], textposition="top center",
                             marker={"color": "#d74f46", "size": 16, "line": {"color": "white", "width": 1}}))
    st.plotly_chart(figure(traces, centre_lat), width="stretch", config={"displaylogo": False})
    st.caption(t(f"覆盖 = 本地米制投影中直线距离 ≤ {decision['coverage_radius_m']:.0f} 米（不是出行时间）。"
                 + " ".join(DECISION_LIMITS_ZH[1:3]),
                 f"Coverage = straight-line distance ≤ {decision['coverage_radius_m']:.0f} m in a local metric "
                 "projection (not travel time). " + " ".join(decision["limitations"][1:3])))
    if decision["selected"]:
        zh_metric = {"population": "半径内人口（合成）", "residential": "半径内居住点数",
                     "commercial": "半径内商业点数", "transit": "半径内交通点数"}
        rows = []
        for c in decision["selected"]:
            if lang == "zh":
                rows.append({"排名": c["rank"], "站点": c["id"], "经度": round(c["lon"], 6),
                             "纬度": round(c["lat"], 6), "评分": c["score"],
                             "新增覆盖人口": c["incremental_population"],
                             "到最近现有站点（米）": c["nearest_existing_m"],
                             **{zh_metric[k]: v for k, v in c["metrics"].items()}})
            else:
                rows.append({"rank": c["rank"], "site": c["id"], "lon": round(c["lon"], 6),
                             "lat": round(c["lat"], 6), "score": c["score"],
                             "new_population": c["incremental_population"],
                             "nearest_existing_m": c["nearest_existing_m"],
                             **{f"{k}_within_radius": v for k, v in c["metrics"].items()}})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    downloads("decision", decision)

with tab_about:
    if lang == "zh":
        st.markdown(f"""
**真实输入（历史开放数据）：** GLA 行政区边界（OGL v3.0）与 GLA 快速充电点（Creative Commons Attribution）。
**合成输入：** 边界探针、运营商数据源、需求点与上下文点。

* 样例：{len(data.zones)} 个行政区、{len(data.chargers)} 条历史充电点记录，研究区域为 **{data.study_zone_id}**。
* 仅使用直线米制距离；不含路网、出行时间、成本、权属或规划约束。
* 阈值是用户选择的复核边距，不是实测的坐标误差；`PASS` 仅表示与所给边界几何一致，不表示坐标真实。
* GeoResolve 的分数是启发式分数，MATCH 不是已确认的同一实体。
* 结果**不代表**当前充电设施的可用性、可行性或准确性，也不是选址建议；需求点为合成数据。

数据来源、许可与署名见 `DATA_SOURCES.md`。项目主页：[{REPO_URL}]({REPO_URL})。
""")
    else:
        st.markdown(f"""
**Real inputs (historic open data):** GLA borough boundaries (OGL v3.0) and GLA rapid charging points
(Creative Commons Attribution). **Synthetic inputs:** boundary probes, operator feed, demand and context points.

* Sample: {len(data.zones)} boroughs, {len(data.chargers)} historic charger records, study zone **{data.study_zone_id}**.
* Straight-line metric distances only; no road network, travel time, cost, ownership or planning constraints.
* Thresholds are user-chosen review margins, not measured coordinate uncertainty.
* Results do not describe current charging provision and are not site recommendations.

See `DATA_SOURCES.md` for sources, licences and attribution. Project page: [{REPO_URL}]({REPO_URL}).
""")
    st.json(data.manifest, expanded=False)
