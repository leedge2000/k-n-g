# 阶段 4「联动与扩面」验收检查单

## 数据与映射

- [x] `seed/mapping.json` 含 `M-MAP-MKT-011`—`M-MAP-MKT-016` 六条映射，`Database.tables` 中出现 6 张新表（共 6 张，总表数 43）
- [x] 6 张新表均为 `MASTER`，含 `createdBy/createdAt/updatedBy/updatedAt` 审计列
- [x] `CmdCommodity` 存在 `CMD0008 菜籽油`
- [x] `MarketPlace` 上存在 `uq_marketplace_futures_code` 部分唯一索引（`marketType='FUTURES' AND exchangeCode IS NOT NULL`）
- [x] `Evidence` 上存在 `uq_evidence_target_quote` 唯一索引 `(targetType,targetId,quoteHash)`

## 实体目录

- [x] `/api/kg/entities/{type}/{pk}` 对形态规格、市场场所、证据、政策影响、因子驱动、事件因子关联均返回 200 且标题正确

## 种子与幂等

- [x] `CommodityForm` 8 条、`MarketPlace` 3 条、`Evidence` 10 条（6 条断言 + 4 条影响各 1 条）、`ImpactLink` 4 条、`FactorDriveLink` 5 条、`EventFactorLink` 4 条
- [x] 序列 8 条、指标 16 条、数据点 96 条、事件 6 条、因子 8 条、有效断言 6 条、价格传导 4 条
- [x] `SERIES_SCOPE` 每条序列含且仅含 1 个 `CHANNEL` 成员，且指向的 `MarketPlace` 存在
- [x] 连续执行两次 `seed_market()`，12 张市场表计数不变

## 门禁

- [x] G4-1 粮油序列指向 `CommodityForm` 且品种一致；`LEGACY_FORM_EXEMPT`（IN-0003/0004）生效
- [x] G4-2 每条 `SERIES_SCOPE` 恰含 1 个 `CHANNEL` 且 `MarketPlace` 存在
- [x] G4-3 每条影响 ≥1 证据；EVENT/POLICY 均不直连 SERIES；`impactMagnitude` 仅 `MARKET_PACK` 可填
- [x] G4-4 驱动边 `elasticity` 或 `lagDays` 至少一项非空；桥二两端存在
- [x] G4-5 每条断言 ≥1 证据
- [x] G4-6 `effectiveFrom <= effectiveTo`、`lagDays >= 0`、`version >= 1`
- [x] G4-7 `quote` 长度 1—200、`quoteHash=sha1(quote)`、`documentId` 存在（`SourceDoc` 非空时严格校验，空库跳过）、三元组唯一
- [x] G4-8 序列频率/币种/税基/单位与 Scope 一致，月度数据点期间无缺口
- [x] `validate_stage4()` 在 `seed_market()` 末尾被调用（`initialize()` 对已升级到 5 维的库复核），失败抛 `MarketError`

## 触发状态

- [x] `OBJ-PUB-EV-1001`（announce 2024-02-18 / effective 2024-02-20 / 至 2024-06-30）：`2024-02-01`→`PENDING`、`2024-06-15`→`EFFECTIVE`、`2024-08-15`→`EXPIRED`、`2025-01-31`→`EXPIRED`
- [x] `ANNOUNCED` 由 `OBJ-PUB-EV-1004` 在 `2024-05-25` 验证（announce 2024-05-20 / effective 2024-06-01）
- [x] `lagDays=14` 的 `LNK-IM-0003` 在 `2024-04-14` 未触发、`2024-04-15` 触发
- [x] `sourceType='POLICY'` 的 `LNK-IM-0004` 在 `asOf=2023-01-01` 也恒触发
- [x] 状态为派生值，`Event` 表无状态列

## 路由

- [x] `GET /api/market/commodity-forms?commodityId=` 可用（`total=8`）
- [x] `GET /api/market/places?marketType=` 可用（`total=3`）
- [x] `GET /api/market/impacts` 支持 `sourceType/sourceId/targetType/targetId/asOf`，含 `triggered/eventState/evidenceCount`
- [x] `GET /api/market/evidence?targetType=&targetId=` 缺参返回 400
- [x] `GET /api/market/factors/{factorId}` 返回 `drives`，不存在返回 404
- [x] `overview` 返回 5 维 `scopeDimensions`、`periods`（12）、`asOf`、`counts.commodityForms/places/impacts`
- [x] `series/{seriesId}` 返回 `commodityForm/marketPlace/factors/impacts/assertions[].evidenceIds`
- [x] `timeline?asOf=` 每条事件含 `state/stateLabel/triggeredImpactCount`
- [x] `/openapi.json` 中 `/api/market/*` 无写方法（仅 `get`）

## 前端

- [x] `market.html` 有 `#asof-range`、`#asof-label`，拖动后四处重载且区间不变
- [x] `market.html` 有 `#form-chips`、`#drive-panel`、`#impact-panel`、`#evidence-box`；事件卡片有四态徽章
- [x] `market.html` 深链 `series/asOf/tab/commodity/indicator/policy/event` 逐级回退，非法参数落到首条序列与最新期间
- [x] `index.html?entityType=品种&entityId=CMD0008` 在 `bootstrap()` 中解析并自动进入探索视图打开详情
- [x] 详情页按类型出现「查看市场行情」链接（品种/产品→`commodity`、指标数据→`indicator`、政策法规→`policy`）
- [x] `GET /index.html` 与 `GET /market.html` 均 200（`/index.html` 已补路由）

## 收口

- [x] `python -m pytest -q` → `138 passed, 3 skipped`
- [x] `python ontology_ly/tools/validate_stage0.py` → `score=100 error=0 warn=0`
- [x] 无临时脚本、调试输出残留（仅保留 `data/grain_oil.db` 这一既有运行库）
