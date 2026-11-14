# 阶段 4 联动与扩面口径清单（冻结稿）

> 适用范围：`kg-system` 阶段 4「联动与扩面」。
> 上游依据：`ontology_ly/方案定稿.md` L353（桥接、跨工作台跳转、时间滑块、触发状态、扩品种），
> `ontology_ly/STAGE0-执行分解.md` §5（桥一/桥二/桥三）、§3.11.4（ImpactLink 字段）。
> 本清单冻结后即为阶段 4 的实现与验收依据；阶段 5 的扩面与运营能力不在本清单内。

## 0. 边界

本期交付：三桥落地、CommodityForm 与 MarketPlace 建表并回填、事件触发状态、时间滑块、跨工作台深链、扩 4 个品种序列。

不做：
- 任何 HTTP 写路由（沿用阶段 3 只读口径，写入只经种子与测试）
- 外部实时行情、CSV 批量导入
- Evidence 段落级定位校验（G2-4 属文档门，留待阶段 5 联调）
- LogisticsNode、CalendarScheme、SubstitutionRelation 建表
- 生猪/猪肉序列的 CommodityForm 回填（FormType 无畜产品枚举，显式豁免）

交付形态：后端服务 + 6 张新表 + 5 条新增只读路由 + 3 条既有路由扩展 + `market.html` 联动改造 + `index.html` 深链入口。

## 1. 类型账：零新增本体类型，新增 6 张物理表

阶段 4 不新增任何本体类型（`25 个对象类型 + 4 个关系实体类型 = 29`），只把内核中尚未落地的类型补齐：

| 内核类型 | 类型来源 | 落地表 | 角色 |
|---|---|---|---|
| CommodityForm | object_types | `CommodityForm` | 形态规格；与旧 `Product` 并存，`Product` 保持只读兼容 |
| MarketPlace | object_types | `MarketPlace` | 市场/交易场所，供 `CHANNEL` 维度引用 |
| Evidence | object_types | `Evidence` | 证据，桥一/桥三共用 |
| ImpactLink | relation_types（RELATION_ENTITY） | `ImpactLink` | 桥一：Event/Policy → Commodity/Factor |
| DRIVES | relation_types（edge，带属性） | `FactorDriveLink` | 桥二：Factor → IndicatorSeries |
| ACTS_ON_FACTOR | relation_types（edge） | `EventFactorLink` | 桥二：Event → Factor |

`FactorDriveLink` 与 `EventFactorLink` 是边表，不是对象类型；不写 `id_registry`，用复合主键，避免自造 ID 前缀。

全部表 `tableRole = MASTER`，自动获得 `createdBy/createdAt/updatedBy/updatedAt` 审计列。

## 2. 表结构

除注明外 `nullable = true`；`PK` 为主键；标注「扩展列」的是展示/运维列，不写进内核类型。

### 2.1 CommodityForm（前缀 OBJ-PUB-CM）

| 列 | 类型 | 约束 |
|---|---|---|
| formId | TEXT | PK |
| commodityId | TEXT | NOT NULL，FK → `CmdCommodity.commodityId` |
| formType | TEXT | NOT NULL，枚举 `FormType` |
| derivedFromFormId | TEXT | FK → `CommodityForm.formId`，不得自指 |
| standardCode | TEXT | |
| grade | TEXT | NOT NULL，默认空串 |
| qualityParams | TEXT | JSON 字符串，键名受控 |
| formName | TEXT | NOT NULL，**扩展列** |
| status | TEXT | NOT NULL，默认「有效」，**扩展列** |

唯一约束：`(commodityId, formType, grade)`。`grade` 默认空串，避免 SQLite 中 NULL 不参与唯一比较。

### 2.2 MarketPlace（前缀 OBJ-PUB-MK）

| 列 | 类型 | 约束 |
|---|---|---|
| placeId | TEXT | PK |
| placeName | TEXT | NOT NULL，**扩展列** |
| marketType | TEXT | NOT NULL，枚举 `MarketType` |
| regionId | TEXT | NOT NULL，FK → `Region.regionId` |
| operatorOrgId | TEXT | 留空：`Organization` 表未建，不伪造引用 |
| exchangeCode | TEXT | |
| tradingHours | TEXT | |
| status | TEXT | NOT NULL，默认「有效」，**扩展列** |

唯一约束：部分唯一索引 `(exchangeCode) WHERE marketType='FUTURES' AND exchangeCode IS NOT NULL`。

### 2.3 Evidence（前缀 OBJ-PUB-ED）

| 列 | 类型 | 约束 |
|---|---|---|
| evidenceId | TEXT | PK |
| targetType | TEXT | NOT NULL，枚举 `ObjectTypeName` |
| targetId | TEXT | NOT NULL |
| documentId | TEXT | NOT NULL，FK → `SourceDoc.docId` |
| segmentId | TEXT | 本期种子留空 |
| quote | TEXT | NOT NULL，长度 1—200 |
| quoteHash | TEXT | NOT NULL，`sha1(quote)` 小写十六进制 |
| pageNo | INTEGER | |
| evidenceType | TEXT | NOT NULL，枚举 `EvidenceType` |
| strength | REAL | NOT NULL，0—1 |
| verifiedBy | TEXT | |
| verifiedAt | TEXT | |

唯一约束：`(targetType, targetId, quoteHash)`。
本期只校验文档存在与哈希一致，不校验 quote 能否在 `DocumentSegment.text` 中定位（见 §0）。

### 2.4 ImpactLink（前缀 LNK-IM）

| 列 | 类型 | 约束 |
|---|---|---|
| linkId | TEXT | PK |
| sourceType | TEXT | NOT NULL，枚举 `ImpactSourceType`（EVENT / POLICY） |
| sourceId | TEXT | NOT NULL；EVENT → `Event.eventId`，POLICY → `Policy.policyId` |
| targetType | TEXT | NOT NULL，枚举 `ImpactTargetType`；**本包收紧为 COMMODITY / FACTOR**，禁止 SERIES |
| targetId | TEXT | NOT NULL |
| impactDirection | TEXT | NOT NULL，枚举 `ImpactDirection` |
| impactMagnitude | TEXT | 枚举 `ImpactMagnitude`；仅 `assertedByType=MARKET_PACK` 可填 |
| lagDays | INTEGER | ≥0 |
| mechanism | TEXT | |
| assertedByType | TEXT | 枚举 `AssertedByType` |
| assertedById | TEXT | |
| version | INTEGER | NOT NULL，默认 1，**扩展列**（版本链） |
| status | TEXT | NOT NULL，默认「有效」，**扩展列** |

`evidenceIds` 不落库，由 `Evidence(targetType='ImpactLink', targetId=linkId)` 派生，避免双写不一致。

禁止直连 SERIES 的理由：`STAGE0-执行分解.md` §5.4「政策包想影响价格，只能走 Event→Factor + Factor→Series」；内核字段表虽允许 SERIES，本包按 §5.4 收紧，属门禁而非返工。

### 2.5 FactorDriveLink（复合主键）

| 列 | 类型 | 约束 |
|---|---|---|
| factorId | TEXT | NOT NULL，FK → `Factor.factorId` |
| seriesId | TEXT | NOT NULL，FK → `IndicatorSeries.seriesId` |
| elasticity | REAL | |
| lagDays | INTEGER | ≥0 |
| samplePeriodId | TEXT | FK → `TimePeriod.periodId` |
| version | INTEGER | NOT NULL，默认 1，**扩展列** |
| status | TEXT | NOT NULL，默认「有效」，**扩展列** |

`PK (factorId, seriesId)`；`elasticity` 与 `lagDays` 至少一项非空。

### 2.6 EventFactorLink（复合主键）

| 列 | 类型 | 约束 |
|---|---|---|
| eventId | TEXT | NOT NULL，FK → `Event.eventId` |
| factorId | TEXT | NOT NULL，FK → `Factor.factorId` |
| version | INTEGER | NOT NULL，默认 1，**扩展列** |
| status | TEXT | NOT NULL，默认「有效」，**扩展列** |

`PK (eventId, factorId)`。

## 3. Scope 承载变更

`SERIES_SCOPE` 由阶段 3 的 4 维（COMMODITY / GEOGRAPHY / UNIT_BASIS / TAX_BASIS）扩展为 **5 维**，补入 `CHANNEL`：

| 序列 | CHANNEL 成员 |
|---|---|
| IN-0001 河南稻谷收购价 | OBJ-PUB-MK-0001 河南产地收购市场 |
| IN-0002 全国玉米收购价 | OBJ-PUB-MK-0002 全国产地收购市场 |
| IN-0003 生猪养殖成本 | OBJ-PUB-MK-0002 |
| IN-0004 猪肉零售价 | OBJ-PUB-MK-0003 全国农产品批发市场 |
| IN-0005 小麦收购价 | OBJ-PUB-MK-0002 |
| IN-0006 大豆收购价 | OBJ-PUB-MK-0002 |
| IN-0007 豆油批发价 | OBJ-PUB-MK-0003 |
| IN-0008 菜籽油批发价 | OBJ-PUB-MK-0003 |

`CHANNEL` 维度白名单本是 `MarketPlace`，`scope.py` 无需改代码，只需确保表与行存在。`TIME_WINDOW` 仍不写入 `SERIES_SCOPE`。

## 4. 种子

### 4.1 主数据扩充

`seed/business.json` 新增 1 条品种：

| commodityId | commodityCode | commodityName | commodityCategory |
|---|---|---|---|
| CMD0008 | RAPESEED-OIL | 菜籽油 | 食用植物油 |

其余品种复用 `CMD0001 稻谷`、`CMD0002 小麦`、`CMD0003 玉米`、`CMD0004 大豆`、`CMD0006 生猪`、`CMD0007 猪肉`。

### 4.2 CommodityForm（8 条）

| formId | commodityId | formType | derivedFromFormId | formName |
|---|---|---|---|---|
| OBJ-PUB-CM-0001 | CMD0001 | RAW_GRAIN | | 稻谷原粮 |
| OBJ-PUB-CM-0002 | CMD0002 | RAW_GRAIN | | 小麦原粮 |
| OBJ-PUB-CM-0003 | CMD0003 | RAW_GRAIN | | 玉米原粮 |
| OBJ-PUB-CM-0004 | CMD0004 | RAW_GRAIN | | 大豆原粮 |
| OBJ-PUB-CM-0005 | CMD0001 | PROCESSED_GRAIN | OBJ-PUB-CM-0001 | 大米 |
| OBJ-PUB-CM-0006 | CMD0002 | PROCESSED_GRAIN | OBJ-PUB-CM-0002 | 小麦粉 |
| OBJ-PUB-CM-0007 | CMD0004 | REFINED_OIL | OBJ-PUB-CM-0004 | 豆油 |
| OBJ-PUB-CM-0008 | CMD0008 | REFINED_OIL | | 菜籽油 |

`OBJ-PUB-CM-0005/0006/0007` 对应旧 `PRD0001/PRD0002/PRD0003`，仅新建 formId，不迁移旧 ID，`Product` 表保持只读兼容。

### 4.3 MarketPlace（3 条）

| placeId | placeName | marketType | regionId |
|---|---|---|---|
| OBJ-PUB-MK-0001 | 河南产地收购市场 | SPOT | REG0004 |
| OBJ-PUB-MK-0002 | 全国产地收购市场 | SPOT | REG0001 |
| OBJ-PUB-MK-0003 | 全国农产品批发市场 | WHOLESALE | REG0001 |

### 4.4 序列回填与扩品种

| seriesId | 品种 | commodityFormId | priceBasis | 点值区间（元/吨） |
|---|---|---|---|---|
| OBJ-PUB-IN-0001 | CMD0001 | OBJ-PUB-CM-0001 | EX_FARM | 2580—2660（已有） |
| OBJ-PUB-IN-0002 | CMD0003 | OBJ-PUB-CM-0003 | EX_FARM | 2310—2430（已有） |
| OBJ-PUB-IN-0003 | CMD0006 | `CMD0006`（豁免） | EX_FARM | 13.9—14.8（已有，元/公斤） |
| OBJ-PUB-IN-0004 | CMD0007 | `CMD0007`（豁免） | RETAIL | 25.5—27.8（已有，元/公斤） |
| OBJ-PUB-IN-0005 | CMD0002 | OBJ-PUB-CM-0002 | EX_FARM | 2770—2850 |
| OBJ-PUB-IN-0006 | CMD0004 | OBJ-PUB-CM-0004 | EX_FARM | 5160—5320 |
| OBJ-PUB-IN-0007 | CMD0004 | OBJ-PUB-CM-0007 | WHOLESALE | 9180—9350 |
| OBJ-PUB-IN-0008 | CMD0008 | OBJ-PUB-CM-0008 | WHOLESALE | 10420—10600 |

新增 4 个指标：IND0013 小麦全国收购价、IND0014 大豆全国收购价、IND0015 豆油全国批发价、IND0016 菜籽油全国批发价；均为 `MONTH`、`INCLUSIVE`、`CNY`、`OBJ-PUB-UN-0001`（元/吨），覆盖 2024-01—2024-12 共 12 点。

`commodityFormId` 回填规则：粮油类序列（IN-0001/0002/0005/0006/0007/0008）必须指向 `CommodityForm`；畜产品示例序列（IN-0003/0004）保留 `CmdCommodity.commodityId`，在代码常量 `LEGACY_FORM_EXEMPT` 中显式列明。

### 4.5 因子、事件、断言、桥边

新增因子：FAC0006 压榨利润（COST）、FAC0007 油脂库存（SUPPLY）、FAC0008 进口成本（TRADE）。

新增事件：

| eventId | 标题 | eventTypeId | eventTime | effectiveFrom | effectiveTo |
|---|---|---|---|---|---|
| OBJ-PUB-EV-1004 | 小麦最低收购价执行预案 | OBJ-PUB-EV-0001 | 2024-05-25 | 2024-06-01 | 2024-09-30 |
| OBJ-PUB-EV-1005 | 进口大豆关税调整 | OBJ-PUB-EV-0002 | 2024-03-10 | 2024-04-01 | 2024-12-31 |
| OBJ-PUB-EV-1006 | 菜籽油轮换收储 | OBJ-PUB-EV-0001 | 2024-07-05 | 2024-07-10 | 2024-10-31 |

新增断言：AST-0003（大豆 WILL_RISE 5500）、AST-0004（菜籽油 WILL_FALL 10200），各配 1 条 Evidence。

桥一 ImpactLink（4 条，每条 1 条 Evidence）：

| linkId | sourceType | sourceId | targetType | targetId | 方向 |
|---|---|---|---|---|---|
| LNK-IM-0001 | EVENT | OBJ-PUB-EV-1001 | FACTOR | FAC0005 | POSITIVE |
| LNK-IM-0002 | EVENT | OBJ-PUB-EV-1004 | FACTOR | FAC0005 | POSITIVE |
| LNK-IM-0003 | EVENT | OBJ-PUB-EV-1005 | FACTOR | FAC0008 | POSITIVE |
| LNK-IM-0004 | POLICY | POL0001 | COMMODITY | CMD0001 | POSITIVE |

桥二：

- `EventFactorLink`：EV-1001→FAC0005、EV-1004→FAC0005、EV-1005→FAC0008、EV-1006→FAC0007
- `FactorDriveLink`：FAC0001→IN-0005（elasticity 0.28）、FAC0003→IN-0008（elasticity 0.20, lag 7）、FAC0006→IN-0007（elasticity 0.42, lag 14）、FAC0007→IN-0007（elasticity -0.35, lag 7）、FAC0008→IN-0006（lag 21）

既有 `PriceTransmissionLink` 增补 1 条：IN-0006 大豆 → IN-0007 豆油（COST_PUSH，elasticity 0.65，lag 15）。

## 5. 触发状态机

`asOf` 取当前所选期间的 `endDate`（如选 2024-08 → `2024-08-31`）。事件状态为**派生值**，不落库、不提供写接口：

| 状态 | 判定 |
|---|---|
| PENDING 未公告 | `announceTime` 为空或 `announceTime > asOf` |
| ANNOUNCED 已公告 | `announceTime <= asOf < effectiveFrom`（`effectiveFrom` 为空时取 `eventTime`） |
| EFFECTIVE 生效中 | `effectiveFrom <= asOf` 且（`effectiveTo` 为空或 `asOf <= effectiveTo`） |
| EXPIRED 已到期 | `effectiveTo` 非空且 `effectiveTo < asOf` |

影响触发判定：
- `sourceType=EVENT`：来源事件状态为 `EFFECTIVE`，且 `asOf >= effectiveFrom + lagDays`（lagDays 为空按 0）
- `sourceType=POLICY`：恒为已触发（政策文件无生效区间字段）

状态机不引入新枚举文件，状态常量定义在 `app/market.py`：`EVENT_PENDING/EVENT_ANNOUNCED/EVENT_EFFECTIVE/EVENT_EXPIRED`。

## 6. 门禁（G4-1 — G4-8）

| 编号 | 校验 | 失败行为 |
|---|---|---|
| G4-1 | 粮油类序列的 `commodityFormId` 必须存在于 `CommodityForm`，且 `form.commodityId` 等于序列品种；`LEGACY_FORM_EXEMPT` 内的序列跳过 | `MarketError`，附 seriesId 与期望品种 |
| G4-2 | 每条 `SERIES_SCOPE` 必须含且仅含 1 个 `CHANNEL` 成员，且 `MarketPlace` 存在 | `MarketError` |
| G4-3 | 每条 ImpactLink 至少 1 条 Evidence；`sourceType=EVENT` 时 `targetType` 不得为 `SERIES`；`impactMagnitude` 非空时 `assertedByType` 必须为 `MARKET_PACK` | `MarketError`（AP_POLICY_MAGNITUDE） |
| G4-4 | 每条 FactorDriveLink 的 `elasticity` 或 `lagDays` 至少一项非空；两端对象存在；EventFactorLink 两端存在 | `MarketError` |
| G4-5 | 每条 Assertion 至少 1 条 Evidence（`targetType='Assertion'`） | `MarketError` |
| G4-6 | `effectiveFrom <= effectiveTo`（两者非空时）；`lagDays >= 0`；`version >= 1` | `MarketError` |
| G4-7 | `quote` 长度 1—200；`quoteHash = sha1(quote)`；`documentId` 存在于 `SourceDoc`；`(targetType,targetId,quoteHash)` 唯一 | `MarketError` |
| G4-8 | 新增序列 `frequency='MONTH'`、`currencyCode`/`taxBasis`/`unitId` 与 Scope 成员一致；数据点期间连续覆盖 `startPeriodId`—`endPeriodId`，无缺口 | `MarketError` |

门禁聚合入口 `MarketService.validate_stage4()`，由 `initialize()` 在种子装载后调用，测试可直接调用。

## 7. API 契约（全部只读）

新增 5 条：

| 路由 | 说明 |
|---|---|
| `GET /api/market/commodity-forms` | 形态列表，支持 `commodityId` 过滤；返回 `formId/formName/formType/commodityId/derivedFromFormId` |
| `GET /api/market/places` | 市场列表，支持 `marketType` 过滤；返回 `placeId/placeName/marketType/regionId` |
| `GET /api/market/impacts` | 支持 `sourceType/sourceId/targetType/targetId/asOf`；每条含 `triggered`、`eventState`、`evidenceCount` |
| `GET /api/market/evidence` | 必填 `targetType`、`targetId`；返回证据列表与 `quoteHash` |
| `GET /api/market/factors/{factorId}` | 因子详情 + `drives`（含 elasticity/lagDays/series 标题） |

扩展 3 条：

- `GET /api/market/overview`：新增 `periods`（`periodId/label/startDate/endDate`）、`asOf` 回显、`counts.commodityForms/places/impacts`
- `GET /api/market/series/{seriesId}`：新增 `commodityForm`、`marketPlace`、`factors`、`impacts`、`assertions[].evidenceIds`
- `GET /api/market/timeline`：新增 `asOf` 参数；每条事件新增 `state`、`stateLabel`、`triggeredImpactCount`

分页与错误口径沿用阶段 3：`pageSize` 上限 200，未知参数忽略，缺失对象返回 404。

## 8. 前端联动

### 8.1 market.html

1. 时间滑块：`#asof-range`（0—11）+ `#asof-label`；`state.asOf` 初值取最新期间；拖动后 150ms 防抖，重载曲线、事件、断言、影响四处。
2. 区间与 as-of 分工：起止月份输入继续作为「查看区间」，as-of 只决定事件状态、断言可见性与影响是否触发，不改曲线区间。
3. 新增面板：`#form-chips`（形态/渠道）、`#drive-panel`（因子驱动）、`#impact-panel`（政策影响，含方向、滞后、已触发徽章）。
4. 事件卡片增加状态徽章与「查看政策原文」链接。
5. 深链参数：`series`、`asOf`、`tab`、`policy`、`commodity`、`indicator`、`event`；加载时解析并定位，无匹配时回退到首条序列。
6. 主工作台回链：事件/序列卡片提供 `/index.html?entityType=...&entityId=...`。

### 8.2 index.html

1. 启动时解析 `entityType` + `entityId`，自动 `switchView('explorer')` 并 `showDetail`。
2. `showDetail` 内按类型注入「查看市场行情」链接：
   - 品种/产品 → `/market.html?commodity={id}`
   - 指标数据 → `/market.html?indicator={id}`
   - 政策法规 → `/market.html?policy={id}`
3. 链接仅在对应类型出现，不改变原有详情结构与请求。

## 9. 测试与验收

新增 `tests/test_stage4.py`，覆盖：

- 6 张表存在且审计列齐全
- G4-1 — G4-8 各 1 条通过 + 1 条失败用例
- 事件四态在 2024-02-01 / 2024-06-15 / 2024-08-15 / 2025-01-31 的派生结果
- 影响触发对 `lagDays` 的边界（lag=0、lag=14）
- 5 条新增路由与 3 条扩展路由的返回结构
- 种子幂等：连续执行两次，各表计数不变
- 深链参数缺失/非法时静态页仍可返回

同步修订 `tests/test_market.py` 中受种子扩容影响的计数断言（序列 4→8、事件 3→6、断言 2→4）。

验收标准：

1. `python -m pytest -q` 全绿（预计 117 + 新增 20 左右）
2. `python ontology_ly/tools/validate_stage0.py` → `score=100 error=0 warn=0`
3. 种子幂等、无 HTTP 写路由、`market.html` 与 `index.html` 深链双向可用

## 10. 已知偏离与豁免

| 项 | 处理 | 理由 |
|---|---|---|
| 生猪/猪肉序列不回填 commodityFormId | `LEGACY_FORM_EXEMPT` | `FormType` 无畜产品枚举，强行建形态会污染内核 |
| Evidence 不校验段落定位 | 只校验文档存在与哈希 | G2-4 属文档门，阶段 5 联调补 |
| MarketPlace.operatorOrgId 留空 | 不伪造引用 | `Organization` 表未建 |
| ImpactLink 禁止 targetType=SERIES | G4-3 收紧 | 遵循 §5.4，避免政策直连价格 |
| Product 与 CommodityForm 并存 | Product 只读兼容 | 避免迁移旧 ID 引发主数据断链 |
