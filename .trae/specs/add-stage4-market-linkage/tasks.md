# 阶段 4「联动与扩面」任务清单

> 依据：`kg-system/ontology_ly/packs/market/stage4_联动与扩面口径清单.md`（冻结稿）。
> 全部任务在同一工作目录 `kg-system` 内执行。

- [x] Task 1: 种子与映射登记（6 张新表 + 1 条品种）
  - [x] SubTask 1.1: `seed/mapping.json` 追加 `M-MAP-MKT-011`—`M-MAP-MKT-016`，覆盖 `CommodityForm`、`MarketPlace`、`Evidence`、`ImpactLink`、`FactorDriveLink`、`EventFactorLink`，全部 `tableRole=MASTER`；边表 `primaryKey` 写复合键字符串并由 `app/db.py` 解析为表级 `PRIMARY KEY ("factorId","seriesId")`
  - [x] SubTask 1.2: `seed/business.json` 新增 `CMD0008 / RAPESEED-OIL / 菜籽油 / 食用植物油`
- [x] Task 2: 实体目录登记
  - [x] SubTask 2.1: `app/catalog.py` 新增 6 条 `EntitySpec`（形态规格、市场场所、证据、政策影响、因子驱动、事件因子关联），详情接口 `/api/kg/entities/{type}/{pk}` 实测 200
- [x] Task 3: 服务层建表与种子
  - [x] SubTask 3.1: `app/market.py` 扩展 `MARKET_TABLES` 至 16 张，`initialize()` 建 4 个索引（含 `MarketPlace.exchangeCode` 与 `Evidence(targetType,targetId,quoteHash)` 唯一索引）
  - [x] SubTask 3.2: 新增常量 `LEGACY_FORM_EXEMPT`、四态常量与标签、`SERIES_CHANNEL`、六组种子常量
  - [x] SubTask 3.3: 新增种子（8 形态 / 3 市场 / 4 指标 / 4 序列 / 48 点 / 3 因子 / 3 事件 / 2 断言 / 10 证据 / 4 影响 / 5 驱动 / 4 事件因子 / 1 传导），断言编号用 `AST-0006`、`AST-0007`
  - [x] SubTask 3.4: 回填 IN-0001→`OBJ-PUB-CM-0001`、IN-0002→`OBJ-PUB-CM-0003`；`SERIES_SCOPE` 扩为 5 维（`_upsert_series_scope` 对旧库原地升级，保留 `scopeId`）
  - [x] SubTask 3.5: 种子幂等（重复执行 12 张表计数不变）
- [x] Task 4: 门禁 G4-1—G4-8
  - [x] SubTask 4.1: 8 个校验与 `validate_stage4()`，统一抛 `MarketError`，消息含编号/对象 ID/修复提示
  - [x] SubTask 4.2: `seed_market()` 末尾调用 `validate_stage4()`；`initialize()` 在已有序列时也复核
- [x] Task 5: 触发状态与查询扩展
  - [x] SubTask 5.1: `event_state()`、`event_state_label()`、`impact_triggered()`、`latest_as_of()`
  - [x] SubTask 5.2: `list_forms()`、`list_places()`、`list_impacts()`、`list_evidence()`、`factor_detail()`
  - [x] SubTask 5.3: 扩展 `overview()`、`series_detail()`、`timeline()`
- [x] Task 6: 路由接线
  - [x] SubTask 6.1: `app/main.py` 新增 5 条只读路由并扩展 3 条既有路由（透传 `asOf`）；补 `/index.html` 路由
  - [x] SubTask 6.2: `/api/market/*` 无任何写方法（openapi 实测仅 get）
- [x] Task 7: `web/market.html` 联动改造
  - [x] SubTask 7.1: `#asof-range` / `#asof-label` / `#asof-reset`，150ms 防抖，驱动曲线/事件/断言/影响四处重载且不改区间
  - [x] SubTask 7.2: `#form-chips`、`#drive-panel`、`#impact-panel`、`#evidence-box`；事件状态徽章与政策回链
  - [x] SubTask 7.3: 深链 `series/asOf/tab/commodity/indicator/policy/event` 解析与逐级回退；主工作台回链
- [x] Task 8: `web/index.html` 深链入口
  - [x] SubTask 8.1: 启动解析 `entityType`+`entityId` → `switchView('explorer')` + `showDetail`
  - [x] SubTask 8.2: `showDetail` 按类型注入「查看市场行情」链接
- [x] Task 9: 测试与收口
  - [x] SubTask 9.1: 新增 `tests/test_stage4.py`（21 条：建表/审计列/计数/回填/Scope/8 门通过+7 类失败/四态/触发边界/幂等/8 路由/无写方法/静态页）
  - [x] SubTask 9.2: 修订 `tests/test_market.py` 的 5 条旧规模断言
  - [x] SubTask 9.3: `python -m pytest -q` → `138 passed, 3 skipped`
  - [x] SubTask 9.4: `python ontology_ly/tools/validate_stage0.py` → `score=100 error=0 warn=0`
  - [x] SubTask 9.5: 临时脚本/临时库已清理

# Task Dependencies

- Task 2 依赖 Task 1（表名确定后才登记目录）
- Task 3 依赖 Task 1
- Task 4、Task 5 依赖 Task 3
- Task 6 依赖 Task 3、Task 5
- Task 7、Task 8 依赖 Task 6
- Task 9 依赖 Task 1—Task 8
