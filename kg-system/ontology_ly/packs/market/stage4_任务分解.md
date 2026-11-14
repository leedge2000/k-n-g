# 阶段 4 任务分解

> 依据：`ontology_ly/packs/market/stage4_联动与扩面口径清单.md`（冻结稿）。
> 执行顺序即下表顺序；每项完成需通过对应验证。

## T1 种子与映射：6 张新表登记

- `seed/mapping.json` 追加 6 条映射：`CommodityForm`、`MarketPlace`、`Evidence`、`ImpactLink`、`FactorDriveLink`、`EventFactorLink`（编号 `M-MAP-MKT-011`—`M-MAP-MKT-016`）。
- 登记列映射、主键、`tableRole=MASTER`；`FactorDriveLink`/`EventFactorLink` 标复合主键。
- `seed/business.json` 新增 `CMD0008 菜籽油`。

验证：启动初始化后 6 张表存在，审计列齐全。

## T2 实体登记

- `app/catalog.py` 新增 6 条 `EntitySpec`：形态规格、市场场所、证据、政策影响、因子驱动、事件因子关联。
- 主键与标题字段按冻结稿 §2：`formId/formName`、`placeId/placeName`、`evidenceId/evidenceId`、`linkId/linkId`、`(factorId,seriesId)/(factorId)`、`(eventId,factorId)/(eventId)`。

验证：`/api/entities/{entityType}` 能检索到新类型。

## T3 服务层：建表、种子与幂等

- `app/market.py` 扩展 `MARKET_TABLES` 与 `initialize()`，建 6 张表及部分唯一索引。
- 新增常量：`LEGACY_FORM_EXEMPT`、4 个事件状态、`SEED_FORMS/PLACES/EVIDENCE/IMPACT_LINKS/FACTOR_DRIVES/EVENT_FACTORS`。
- 新增种子：8 形态、3 市场、4 指标、4 序列、48 数据点、3 因子、3 事件、2 断言、4 影响、8 桥边、1 传导。
- 回填既有 4 条序列的 `commodityFormId` 与 `CHANNEL` Scope 成员；种子幂等（`INSERT OR REPLACE` 或先查后写）。

验证：连续执行两次 `seed_market()`，各表计数不变。

## T4 门禁 G4-1 — G4-8

- `MarketService.validate_stage4()` 聚合 8 个门；`initialize()` 在种子后调用。
- 失败统一抛 `MarketError`，消息含编号、对象 ID 与修复提示。
- G4-1 读取 `LEGACY_FORM_EXEMPT`；G4-3 拦截 `POLICY_PACK` 写强度；G4-8 检查数据点期间连续性。

验证：`tests/test_stage4.py` 中每门通过/失败各 1 条。

## T5 触发状态与影响查询

- `event_state(event, as_of)`：四态派生；`as_of` 默认取最新期间 `endDate`。
- `impact_triggered(link, as_of)`：EVENT 看状态 + `lagDays`；POLICY 恒触发。
- `list_impacts(...)`、`factor_detail(factorId)`、`list_evidence(...)`、`list_forms()`、`list_places()`。
- 扩展 `overview()`、`series_detail()`、`timeline()` 的返回结构。

验证：状态与触发边界用例通过。

## T6 路由接线

- `app/main.py` 新增 5 条只读路由：`/api/market/commodity-forms`、`/places`、`/impacts`、`/evidence`、`/factors/{factorId}`。
- 3 条既有路由透传 `asOf` 并返回新字段。
- 不新增任何 POST/PUT/DELETE。

验证：`/openapi.json` 中写方法数量不变。

## T7 market.html 联动改造

- 新增 as-of 滑块与标签，接入四处重载（曲线/事件/断言/影响），150ms 防抖。
- 新增形态渠道、因子驱动、政策影响三个面板；事件状态徽章；政策原文链接。
- 解析深链参数 `series/asOf/tab/policy/commodity/indicator/event`。
- 保留原区间输入，明确其与 as-of 的分工。

验证：浏览器手工走查 + 静态页 200。

## T8 index.html 深链入口

- 启动解析 `entityType` + `entityId`，自动进入图谱探索并打开详情。
- `showDetail` 按类型注入「查看市场行情」链接（品种/产品、指标数据、政策法规）。
- 不改变既有详情请求与结构。

验证：四类实体各点一次，market.html 能按参数定位。

## T9 测试与收口

- 新增 `tests/test_stage4.py`（约 20 条）。
- 修订 `tests/test_market.py` 中受扩容影响的计数断言。
- 运行 `python -m pytest -q` 与 `python ontology_ly/tools/validate_stage0.py`。
- 清理临时脚本与调试输出。

验收：全量测试通过，定义层 `score=100 error=0 warn=0`。
