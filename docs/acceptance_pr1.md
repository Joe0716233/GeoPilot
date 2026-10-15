# PR #1 人工浏览器验收与部署交接

状态：`BROWSER_UPLOAD_DOWNLOAD_NOT_TESTED`。Chrome 上传因扩展文件 URL 权限受限；内置浏览器的
按钮及实际文件输入选择器均未成功。没有修改安全权限，也没有注入文件来绕过选择器。
AppTest 的真实 UploadedFileManager、下载内容读取和独立 GIS 核算是单独证据，不是浏览器全流程 PASS。

## 自动化与独立复核记录（2026-10-15）

- Codex 独立完整 pytest：313 passed，原 68 项测试未修改（245 项新增）；pip check 与 git diff --check 通过。
- London 离线 Demo 通过，完整 run_all JSON 与升级前基线逐字节相同。
- 独立 Shapely/PyProj 归属复核通过：六条自定义记录、EPSG:27700 转换、163 条 OSM 记录中 84 内部/79 外部。
- examples/audit_coverage.py 通过：独立全网格、WGS84 Geod 距离、覆盖权重、评分、贪心选择和约束，
  包括默认充电 63、保守充电 5、公厕 28；选址均为 C074/C051/C090。
- AppTest 实际上传六行自定义文件、字段映射、双 CRS 确认、地图、CSV/JSON 内容、原始 ID 和旧结果清除通过。
  缺坐标、重复 ID、非法 CRS、未映射设施拒绝和会话隔离均有自动化覆盖。
- 真实浏览器确认隐私说明、两种设施页面/地图、access 计数和筛选；修复并复核场景切换后的控件/结果同步。
  充电保守过滤和公厕未知 access 选项在切换时明确重置，浏览器往返检查通过。
- 浏览器文件选择器未成功，所以真实上传、下载到磁盘及读取该浏览器下载文件仍未验收，不能标为 PASS。
- Claude 实际模型 claude-opus-5-5；MCP 长任务断连后恢复同一会话完成主要修改，随后触及会话额度上限。
  Codex 独立复核并完成场景状态、部署依赖、额外回归和交接修正；未更换账户或购买额度。

## 浏览器步骤与预期

从仓库根目录使用 Python 3.12/3.13，安装 `requirements.txt` 后执行：

```text
streamlit run app/streamlit_app.py --server.address 127.0.0.1 --server.port 8517
```

在正常浏览器打开 `http://127.0.0.1:8517/`，进入“自定义数据（上传）”。样本均在
`examples/data/acceptance/`，是虚构记录和简单几何，不含个人数据。

1. 上传 `acceptance_points.csv`，来源填 `acceptance_points`。映射 ID→`asset_ref`、X→`east_custom`、
   Y→`north_custom`、名称→`site_label`、类别→`kind_raw`、声明区域→`claimed_area`、需求权重→`need_units`。
2. 上传 `acceptance_zones.geojson`，来源填 `acceptance_zones`。区域 ID→`region_code`，名称→`region_label`。
   两个输入均填 `EPSG:4326`，分别勾选确认。边界含 Polygon、MultiPolygon 和独立岛。
3. 执行空间分配。原始/加载记录均为 6，缺坐标 1，拒绝 0，参与分配 5。地图应显示点、两区域及岛。
4. 点击“下载结果 CSV”，在浏览器实际保存后，用文本编辑器或 CSV 阅读器打开下载文件。
   必须有 6 个数据行，`original_id` 与 `source_id` 保留 orig-A 至 orig-F；核对下表，不能把边界或
   重叠记录自动归为唯一内部区域。另下载 JSON，核对来源、CRS、字段映射和摘要。

| ID | 状态 | 唯一归属 / 匹配 |
|---|---|---|
| orig-A | ASSIGNED | west |
| orig-B | ON_BOUNDARY | 匹配 west；不设置唯一内部归属 |
| orig-C | MULTIPLE_MATCHES | east;west；不设置唯一归属 |
| orig-D | OUTSIDE_ALL_ZONES | 无 |
| orig-E | NO_COORDINATE | 无 |
| orig-F | ASSIGNED | east（MultiPolygon 岛） |

5. 改用 `acceptance_invalid.csv`，重新确认字段和 CRS：10 行原始输入，加载 7、拒绝 3、缺坐标 1、
   参与分配 6。`bad-xy` 是 NON_NUMERIC_COORDINATE，两个 `dup` 都拒绝为 DUPLICATE_ID；空 ID 行保留并
   标记临时 ID，`original_id` 为空。导出 CSV 仍须包含全部 10 行，不能丢弃异常行。
6. CRS 改为 `EPSG:0` 或 `GCJ-02`：显示 INVALID_CRS 或 UNSUPPORTED_CRS，禁止执行，不崩溃。
   恢复有效 CRS 后需重新勾选确认。更改文件内容、映射、来源或确认状态，旧地图、结果和下载必须清除；
   新文件内容必须重新确认 CRS。
7. 使用 `acceptance_coverage.csv`（映射 ref/east/north/kind/need）及相同边界。选择 west，
   household→weighted_demand、bus stop→context，故意不映射 toilet。确认排除两条不可用记录后，
   必须报 INVALID_SCENARIO_CONFIG（没有类别映射为 existing_facility）并禁止运行，不能默认为零现有设施。
   即使勾选零基线后尝试运行，也必须报 UNMAPPED_ROLE；映射 toilet→existing_facility 并取消零基线后可运行；
   west 内需求权重应为 200，现有设施 1，现有覆盖为 60.0%。
8. 切换“覆盖分析示例（充电站 / 公厕）”：充电默认参与 63（保留原假设），access 确认标签 5、受限 4、
   未知 54；未过滤不代表公众可用。勾选保守过滤后参与 5，未知默认排除。切换公厕后原始 100、
   默认参与 28、受限 18、未知 54；默认总覆盖 34.6%。返回充电默认总覆盖应为 57.2%。

把浏览器版本、实测提交、六行导出文件、异常结果和截图一并记录后，才能将浏览器上传/下载项标为 PASS。
无需将真实私密数据上传到公开演示服务。

## 实际部署观察（只读，2026-10-14）

正式应用 `https://geopilot-gis.streamlit.app/` 的 Manage app 面板显示：

- 仓库 `Joe0716233/GeoPilot`；分支 `main`；启动文件 `app/streamlit_app.py`。
- 日志显示 Python 3.13.16；uv 使用根目录 requirements.txt，而非 pyproject.toml 解析部署依赖。
- 日志显示平台把 PyArrow 25.0.1 替换成 24.0.0。本 PR 将锁定值对齐到已运行的 24.0.0。
- 日志已有 GitHub 拉取后自动 “Updated app!” 的记录。

[Streamlit 官方部署说明](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)
说明已部署源文件更新会自动同步，依赖变化会触发重新安装。因此 Owner 合并到 main 可能直接更新生产；
本次只提交开发分支，不合并、不重启、不修改云端设置。

示例 JSON、manifest、署名、London 输入均已纳入 Git。requirements 的本地包安装项 `-e .` 支持 src 布局，
正式功能用仓库相对路径，不依赖本机盘符。单文件服务器/控件/解析器均限 5 MiB；点位 20,000、区域 2,000、
空间配对 2,000,000；覆盖另有限制网格、顶点和贪心计算量。这适合受限演示，未完成免费云端多用户并发压力测试。
上传经过服务器，应用不主动持久化上传内容或写入共享缓存，平台日志/备份/网络行为不作绝对保证。

如 Owner 合并后失败，可在 GitHub **撤销本次合并**，让 main 恢复到已知正常
`309aeb236f20b084d5038c342373867b9cd29b06` 的源树，平台将同步该回退。不要强推改写历史。
这是供 Owner 决策的回滚办法，本次没有执行回滚，也没有实测生产回滚。v1.0.0 标签和 Release 保持原状。
