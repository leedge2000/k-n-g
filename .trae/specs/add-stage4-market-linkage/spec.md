# 阶段 4「联动与扩面」Spec

## Why

阶段 3 已交付市场价格包的最小闭环（10 表、6 条只读路由、`market.html`），但仍缺少三件事：
事件/政策与品种、因子之间没有可验证的影响链路；品种只有原粮粒度，没有形态规格与交易场所；前端没有时间轴，无法回答「某一时点哪些影响已经生效」。

## What Changes

- 新增 6 张物理表：`CommodityForm`、`MarketPlace`、`Evidence`、`ImpactLink`、`FactorDriveLink`、`EventFactorLink`（零新增本体类型）。
- 三桥落地：桥一 `ImpactLink`（Event/Policy → Commodity/Factor，强制 ≥1 Evidence）；桥二 `FactorDriveLink`（Factor → Series，elasticity/lagDays 至少一项）+ `EventFactorLink`（Event → Factor）；桥三 `Assertion` 强制 ≥1 Evidence。
- `SERIES_SCOPE` 由 4 维扩为 5 维，补入 `CHANNEL`（白名单 `MarketPlace`）。
- 扩品种：新增小麦、大豆、豆油、菜籽油 4 条序列（`OBJ-PUB-IN-0005`—`0008`）、4 个指标（`IND0013`—`0016`）、48 个数据点，并回填既有 4 条序列的 `commodityFormId`。
- 事件触发状态机：`PENDING/ANNOUNCED/EFFECTIVE/EXPIRED` 派生计算，不落库、无写接口；影响触发按 `asOf >= effectiveFrom + lagDays` 判定，POLICY 来源恒触发。
- G4-1—G4-8 门禁，聚合入口 `MarketService.validate_stage4()`，`initialize()` 种子后调用。
- 新增 5 条只读路由，扩展 3 条既有路由；`market.html` 增加时间滑块与三个新面板（形态渠道 / 因子驱动 / 政策影响）、事件状态徽章、深链参数；`index.html` 支持 `entityType`+`entityId` 启动定位并向市场页回链。
- **BREAKING（内部契约）**：`overview()` 的 `scopeDimensions` 由 4 维改为 5 维；`series_detail()` 的 `commodityForm` 由 `CmdCommodity` 行改为 `CommodityForm` 行（畜产品序列保留旧行）。

不做（已冻结的边界）：任何 HTTP 写路由、外部实时行情、CSV 导入、Evidence 段落级定位校验、`LogisticsNode`/`CalendarScheme`/`SubstitutionRelation` 建表、生猪/猪肉序列的形态回填。

## Impact

- Affected specs：市场价格包（`ontology_ly/packs/market/`）、本体内核映射（`seed/mapping.json`）、实体目录（`app/catalog.py`）。
- Affected code：
  - `seed/mapping.json`、`seed/business.json`
  - `app/market.py`（主战场：建表、种子、门禁、触发状态、查询）
  - `app/db.py`（复合主键支持：`FactorDriveLink`/`EventFactorLink` 为两列主键）
  - `app/catalog.py`、`app/main.py`
  - `web/market.html`、`web/index.html`
  - `tests/test_market.py`、新增 `tests/test_stage4.py`
- 依据：`ontology_ly/packs/market/stage4_联动与扩面口径清单.md`（冻结稿）。

## ADDED Requirements

### Requirement: 形态规格与交易场所落地

系统 SHALL 提供 `CommodityForm` 与 `MarketPlace` 两张主数据表，并以 `CommodityForm` 作为序列的品种粒度引用。

#### Scenario: 粮油序列指向形态规格
- **WHEN** 读取任一粮油类序列（`OBJ-PUB-IN-0001/0002/0005/0006/0007/0008`）
- **THEN** `commodityFormId` 命中 `CommodityForm.formId`，且该形态的 `commodityId` 与序列品种一致

#### Scenario: 畜产品序列显式豁免
- **WHEN** 读取 `OBJ-PUB-IN-0003/0004`
- **THEN** 允许 `commodityFormId` 仍为 `CmdCommodity.commodityId`，且该序列 ID 在 `LEGACY_FORM_EXEMPT` 中列明

#### Scenario: 期货交易所唯一
- **WHEN** 写入两条 `marketType='FUTURES'` 且 `exchangeCode` 相同的 `MarketPlace`
- **THEN** 被部分唯一索引拒绝

### Requirement: 桥一 影响链路

系统 SHALL 将 `ImpactLink` 作为事件/政策对品种或因子的唯一影响入口，且每条影响至少有一条 `Evidence` 支撑。

#### Scenario: 缺少证据的影响被拒
- **WHEN** 存在 `ImpactLink` 无对应 `Evidence(targetType='ImpactLink')`
- **THEN** `validate_stage4()` 抛 `MarketError` 并给出 `linkId`

#### Scenario: 政策不得直连价格序列
- **WHEN** `ImpactLink.sourceType='POLICY'` 且 `targetType='SERIES'`
- **THEN** 门禁失败（沿 `STAGE0-执行分解.md` §5.4）

#### Scenario: 仅市场包可写影响强度
- **WHEN** `impactMagnitude` 非空但 `assertedByType != 'MARKET_PACK'`
- **THEN** 门禁失败（`AP_POLICY_MAGNITUDE`）

### Requirement: 桥二 因子驱动与事件因子关联

系统 SHALL 用 `FactorDriveLink`（Factor → Series）与 `EventFactorLink`（Event → Factor）表达传导链，二者的端点都必须存在。

#### Scenario: 驱动边必带弹性或滞后
- **WHEN** 某条 `FactorDriveLink` 的 `elasticity` 与 `lagDays` 同时为空
- **THEN** 门禁失败并给出 `factorId/seriesId`

### Requirement: 桥三 断言证据

系统 SHALL 要求每条 `Assertion` 至少有一条 `Evidence(targetType='Assertion')`。

#### Scenario: 无证据断言被拒
- **WHEN** 存在断言无证据
- **THEN** 门禁失败并给出 `assertionId`

### Requirement: 事件触发状态与影响触发

系统 SHALL 依据 `asOf`（默认取最新期间的 `endDate`）派生事件状态，并据此判断影响是否已触发；状态不落库。

#### Scenario: 四态派生
- **WHEN** 以 `2024-02-01`、`2024-06-15`、`2024-08-15`、`2025-01-31` 分别求 `OBJ-PUB-EV-1001` 状态
- **THEN** 依次得到 `PENDING`、`ANNOUNCED`、`EFFECTIVE`、`EXPIRED`

#### Scenario: 滞后天数影响触发
- **WHEN** 来源事件为 `EFFECTIVE` 且 `asOf < effectiveFrom + lagDays`
- **THEN** 该影响 `triggered=false`

#### Scenario: 政策来源恒触发
- **WHEN** `sourceType='POLICY'`
- **THEN** `triggered=true`

### Requirement: 只读查询扩展

系统 SHALL 新增 5 条只读路由并扩展 3 条既有路由，且不新增任何写方法。

#### Scenario: 新路由可用
- **WHEN** 请求 `/api/market/commodity-forms`、`/places`、`/impacts`、`/evidence`、`/factors/{factorId}`
- **THEN** 返回规定字段；`/impacts` 每条含 `triggered`、`eventState`、`evidenceCount`

#### Scenario: 无写路由
- **WHEN** 读取 `/openapi.json`
- **THEN** `/api/market/*` 下不存在 POST/PUT/PATCH/DELETE

### Requirement: 前端时间轴与深链

系统 SHALL 在 `market.html` 提供 as-of 滑块与深链参数解析，在 `index.html` 提供 `entityType`+`entityId` 启动定位与市场页回链。

#### Scenario: 滑块驱动四处重载
- **WHEN** 拖动 `#asof-range`
- **THEN** 150ms 防抖后重载曲线、事件、断言、影响，且不改动起止月份区间

#### Scenario: 深链定位
- **WHEN** 打开 `market.html?series=OBJ-PUB-IN-0005&asOf=2024-08`
- **THEN** 选中该序列、滑块定位到 2024-08；参数非法时回退首条序列且页面正常渲染

## MODIFIED Requirements

### Requirement: `/api/market/overview`

新增 `periods`（`periodId/label/startDate/endDate`）、`asOf` 回显与 `counts.commodityForms/places/impacts`；`scopeDimensions` 改为 5 维（含 `CHANNEL`）。

### Requirement: `/api/market/series/{seriesId}`

新增 `commodityForm`、`marketPlace`、`factors`、`impacts`、`assertions[].evidenceIds`。

### Requirement: `/api/market/timeline`

接受 `asOf` 参数；每条事件新增 `state`、`stateLabel`、`triggeredImpactCount`。

## REMOVED Requirements

无。
