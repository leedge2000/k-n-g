# 阶段 3 市场包口径清单（冻结稿）

> 适用范围：`kg-system` 阶段 3「市场包」。上游依据 `ontology_ly/方案定稿.md` L134/L137/L348-L352，
> 字段依据 `ontology_ly/STAGE0-执行分解.md` §3.5/§3.6/§3.8/§3.9/§3.10/§3.11。
> 本清单冻结后即为阶段 3 的实现与验收依据，阶段 4 的跨工作台联动不在本清单内。

## 0. 边界

本期交付：价格体系、因子、断言、事件时间线、传导图，形态为**后端服务 + 只读前端工作台**。

不做：
- 跨工作台跳转、时间滑块联动、触发状态机（阶段 4）
- 外部实时行情接入、CSV 批量导入
- `MarketPlace`、`LogisticsNode`、`CalendarScheme`、`SubstitutionRelation` 建表
- 断言 HTTP 写接口（去重逻辑只由种子装载与测试调用）

## 1. 类型账：零新增本体类型

市场包不新增任何本体类型（`25 个对象类型 + 4 个关系实体类型 = 29`），全部复用共享内核。
但「零新增类型」不等于「零新增物理表」——内核中 `sqliteAlignment.table = null` 的类型仍需落地。
本阶段新建 10 张表：

| 内核类型 | 来源文件 | 落地表 | 角色 |
|---|---|---|---|
| StatisticalSource | object_types | `StatisticalSource` | 统计源，与既有 `CrsSource`（抓取技术源）不合并 |
| TimePeriod | object_types | `TimePeriod` | 期间主数据 |
| UnitOfMeasure | object_types | `UnitOfMeasure` | 计量单位，`dimension=PRICE` 时 `currencyCode` 必填 |
| IndicatorSeries | object_types | `IndicatorSeries` | 价格序列，口径载体，`scopeId` 必填 |
| DataPoint | object_types | `PriceDataPoint` | 价格数据点，(seriesId, periodId) 唯一；**偏离**内核 `sqliteAlignment.table=IvdData`，理由见 §3.0 |
| EventType | object_types | `EventType` | 事件分类 |
| Event | object_types | `Event` | 事件时间线；**偏离**内核 `sqliteAlignment.table=Intel`，理由见 §3.0 |
| Factor | object_types | `Factor` | 因子，7 类一级分类固定 |
| Assertion | object_types | `Assertion` | 断言，多空并列；**偏离**内核 `sqliteAlignment.table=Intel`，理由见 §3.0 |
| PriceTransmissionLink | **relation_types** | `PriceTransmissionLink` | 价格传导链路（`kind=RELATION_ENTITY`，非对象类型） |

所有表 `tableRole = MASTER`，自动获得 `createdBy/createdAt/updatedBy/updatedAt` 审计列。

## 2. Scope 承载复核结论

定稿 L137 假设「价格六元组（品种×市场层级×地点×单位×币种×含税口径）恰好是 Scope 六个维度成员」。
逐维核对 `object_types.json` 的 `Dimension.seedInstances` 后，结论是**部分成立**：

| 六元组 | 是否可由 Scope 承载 | 依据 |
|---|---|---|
| 品种 | 是 | `COMMODITY` → `Commodity/CommodityForm`，多值 |
| 地点 | 是 | `GEOGRAPHY` → `Region`，多值 |
| 单位 | 是 | `UNIT_BASIS` → `UnitOfMeasure`，单值 |
| 含税口径 | 是（镜像） | `TAX_BASIS` → 内联枚举 `TaxBasis`，单值；权威值在 `IndicatorSeries.taxBasis`，Scope 成员只做镜像 |
| 市场层级 | 否 | `CHANNEL` 白名单只有 `MarketPlace`，而本阶段不建该表 |
| 币种 | 否 | 十个维度中没有币种维度；币种是 `UnitOfMeasure.currencyCode` 属性 |

因此冻结口径：**`SERIES_SCOPE` 承载 4 维（COMMODITY / GEOGRAPHY / UNIT_BASIS / TAX_BASIS）**，
市场层级由 `IndicatorSeries.priceBasis`（枚举 `PriceBasis`：EX_FARM/WHOLESALE/RETAIL/FOB/CIF/DELIVERED/FUTURES）
承载，币种由 `IndicatorSeries.currencyCode` 与 `UnitOfMeasure.currencyCode` 共同承载，两者受 G3-1 恒定约束。

`TIME_WINDOW` 不写入 `SERIES_SCOPE`，避免与 `startPeriodId/endPeriodId` 重复表达。

结论：Scope 够用，但映射是「四维 + 两个序列字段」，不是六维一一对应。阶段 4 若引入 `MarketPlace`，
再把 `CHANNEL` 补进序列 Scope，属于扩展而非返工。

其余 Scope 类型用法：
- `ASSERTION_SCOPE`：断言作用范围，通常与目标序列 Scope 相同或为其子集
- `EVENT_SCOPE`：政策类事件的适用范围，`POLICY` 类必填

## 3. 表结构

类型均为 SQLite 类型；除注明外 `nullable = true`；主键见 `PK`。

### 3.0 列对齐约定

1. 列名与内核字段同名者，一律沿用内核字段名与内核枚举（不另造同义词）。
2. 标注「扩展列」的是表级显示/运维列，不属于本体属性，登记在 `mapping.json` 的 `columnMappings` 中但不写进内核类型。
3. 有三处主动偏离内核 `sqliteAlignment`，理由固定如下，不再逐表重复：
   - `DataPoint` → 新建 `PriceDataPoint`：`IvdData` 的 `sourceDocId`/`segmentId`/`knowledgeCategoryId`/`industryStage` 均为必填且绑定审核流，无法承载无文档来源的月度价格点
   - `Event` → 新建 `Event` 表：`Intel` 是文档型记录，事件时间线需要 `eventTime/effectiveFrom/effectiveTo` 的区间查询
   - `Assertion` → 新建 `Assertion` 表：内核要求断言与事件分表存放（观点不得当事实写入事件）
4. ID 前缀一律取 `kernel/id_registry.json` 的权威值，不自定义缩写。

### 3.1 StatisticalSource（前缀 OBJ-PUB-SS）

| 列 | 类型 | 约束 |
|---|---|---|
| sourceId | TEXT | PK |
| sourceName | TEXT | NOT NULL，**扩展列**（内核无名称字段，展示必需） |
| sourceType | TEXT | NOT NULL，枚举 `StatisticalSourceType` |
| orgId | TEXT | |
| releasePattern | TEXT | |
| reliabilityScore | REAL | 0—1 |
| revisionPolicy | TEXT | 枚举 `RevisionPolicy` |
| officialUrl | TEXT | **扩展列** |
| remark | TEXT | **扩展列** |

### 3.2 TimePeriod（前缀 OBJ-PUB-TP）

| 列 | 类型 | 约束 |
|---|---|---|
| periodId | TEXT | PK，形如 `2024-01` / `2024Q1` / `2024` |
| periodType | TEXT | NOT NULL，枚举 `PeriodType`；本期种子只用 `MONTH` |
| startDate | TEXT | NOT NULL，ISO 日期 |
| endDate | TEXT | NOT NULL，ISO 日期，`startDate <= endDate` |
| calendarSchemeId | TEXT | 非自然年必填（本期为自然年内的月，留空） |
| label | TEXT | NOT NULL，如「2024年1月」 |

### 3.3 UnitOfMeasure（前缀 OBJ-PUB-UN）

| 列 | 类型 | 约束 |
|---|---|---|
| unitId | TEXT | PK |
| unitCode | TEXT | NOT NULL，UNIQUE，如 `CNY/t`、`CNY/kg` |
| dimension | TEXT | NOT NULL，枚举 `UnitDimension` 全量（MASS/VOLUME/AREA/CURRENCY/RATIO/PRICE） |
| baseUnitCode | TEXT | 基准单位码 |
| factor | REAL | 到基准单位的换算系数 |
| precision | INTEGER | 展示小数位 |
| currencyCode | TEXT | `dimension=PRICE` 时必填（ISO 4217） |

### 3.4 IndicatorSeries（前缀 OBJ-PUB-IN）

| 列 | 类型 | 约束 |
|---|---|---|
| seriesId | TEXT | PK |
| indicatorId | TEXT | NOT NULL，FK → `Indicator.indicatorId` |
| commodityFormId | TEXT | 兼容品种形态；`CommodityForm` 本期未建，暂填 `CmdCommodity.commodityId`（阶段 4 建表后回填） |
| scopeId | TEXT | NOT NULL，指向 `OntologyScope.scopeId`，`scopeType = SERIES_SCOPE` |
| frequency | TEXT | NOT NULL，枚举 `PeriodType`（本期 `MONTH`） |
| statisticalSourceId | TEXT | NOT NULL，FK → `StatisticalSource.sourceId` |
| externalSeriesCode | TEXT | 外部序列代码 |
| priceBasis | TEXT | NOT NULL，枚举 `PriceBasis`（内核为价格类必填，此处对所有序列收紧为必填） |
| taxBasis | TEXT | NOT NULL，枚举 `TaxBasis`；与 Scope 的 `TAX_BASIS` 成员必须相等（G3-1） |
| currencyCode | TEXT | NOT NULL |
| unitId | TEXT | NOT NULL，FK → `UnitOfMeasure.unitId`；**内核约束要求"序列内 unitId 恒定"但字段表未列，按约束补列** |
| startPeriodId | TEXT | FK → `TimePeriod.periodId` |
| endPeriodId | TEXT | FK → `TimePeriod.periodId` |
| status | TEXT | NOT NULL，默认「有效」 |
| remark | TEXT | **扩展列** |

口径权威顺序：`taxBasis` 以 `IndicatorSeries.taxBasis` 列为准，Scope 的 `TAX_BASIS` 成员是它的镜像；
`currencyCode` 以 `IndicatorSeries.currencyCode` 为准，`UnitOfMeasure.currencyCode` 必须与之相同（G3-1）。

### 3.5 PriceDataPoint（前缀 OBJ-PUB-DP）

| 列 | 类型 | 约束 |
|---|---|---|
| pointId | TEXT | PK |
| seriesId | TEXT | NOT NULL，FK → `IndicatorSeries.seriesId` |
| periodId | TEXT | NOT NULL，FK → `TimePeriod.periodId` |
| value | REAL | NOT NULL |
| unitId | TEXT | NOT NULL，FK → `UnitOfMeasure.unitId` |
| publishedAt | TEXT | NOT NULL，ISO 时间戳 |
| revisionNo | INTEGER | NOT NULL，从 0 起 |
| qualityFlag | TEXT | NOT NULL，枚举 `QualityFlag` |
| sourceRef | TEXT | 原始发布位置 |
| remark | TEXT | |

唯一约束：`(seriesId, periodId)`。

### 3.6 EventType（前缀 OBJ-PUB-EV）

| 列 | 类型 | 约束 |
|---|---|---|
| eventTypeId | TEXT | PK |
| typeName | TEXT | NOT NULL，**扩展列**（内核无名称字段，展示必需） |
| eventCategory | TEXT | NOT NULL，枚举 `EventCategory` |
| isPolicyTool | INTEGER | NOT NULL，0/1 |
| toolVerb | TEXT | `isPolicyTool=1` 时必填 |
| defaultImpactDirection | TEXT | 枚举 `ImpactDirection` |

### 3.7 Event（前缀 OBJ-PUB-EV）

| 列 | 类型 | 约束 |
|---|---|---|
| eventId | TEXT | PK |
| eventTypeId | TEXT | NOT NULL，FK → `EventType.eventTypeId` |
| eventTitle | TEXT | NOT NULL，**扩展列** |
| eventTime | TEXT | NOT NULL，ISO 日期 |
| announceTime | TEXT | ISO 时间戳，政策类关键字段 |
| effectiveFrom | TEXT | |
| effectiveTo | TEXT | `effectiveFrom <= effectiveTo` |
| eventScale | TEXT | NOT NULL，枚举 `EventScale` |
| scopeId | TEXT | 政策类必填 |
| actorOrgIds | TEXT | 逗号分隔的 `Organization` id 列表 |
| severity | TEXT | 枚举 `Severity` |
| parentEventId | TEXT | 事件链上级 |
| sourceDocId | TEXT | **扩展列**，FK → `SourceDoc.docId` |
| remark | TEXT | **扩展列** |

### 3.8 Factor（前缀 FAC）

| 列 | 类型 | 约束 |
|---|---|---|
| factorId | TEXT | PK |
| factorCode | TEXT | NOT NULL，UNIQUE |
| factorName | TEXT | NOT NULL，**扩展列** |
| factorCategory | TEXT | NOT NULL，枚举 `FactorCategory`（7 类固定） |
| factorSubCategory | TEXT | NOT NULL，二级分类 |
| defaultDirection | TEXT | NOT NULL，枚举 `FactorDirection` |
| measuredByIndicatorId | TEXT | FK → `Indicator.indicatorId` |
| scopeId | TEXT | |
| mechanism | TEXT | |
| proposedBy | TEXT | NOT NULL，枚举 `FactorProposer` |
| status | TEXT | NOT NULL，默认 `ACTIVE`；按内核应先进 `DRAFT` 再评审，本期种子直接置 `ACTIVE` 并在 `mechanism` 注明为示例 |

### 3.9 Assertion（前缀 AST）

| 列 | 类型 | 约束 |
|---|---|---|
| assertionId | TEXT | PK |
| assertionType | TEXT | NOT NULL，枚举 `AssertionType` |
| subjectType | TEXT | NOT NULL，枚举 `ObjectTypeName` |
| subjectId | TEXT | NOT NULL |
| predicate | TEXT | NOT NULL，枚举 `AssertionPredicate` |
| objectValue | REAL | |
| objectUnitId | TEXT | `objectValue` 非空时必填 |
| rangeLow | REAL | |
| rangeHigh | REAL | |
| targetPeriodId | TEXT | NOT NULL，FK → `TimePeriod.periodId` |
| scopeId | TEXT | NOT NULL（内核约束：缺 scopeId 即拒绝） |
| assertedByType | TEXT | NOT NULL，枚举 `AssertedByType`（ORGANIZATION / PERSON） |
| assertedById | TEXT | NOT NULL |
| assertedAt | TEXT | NOT NULL，ISO 日期 |
| horizon | TEXT | NOT NULL，枚举 `Horizon` |
| rationale | TEXT | ≤300 字 |
| stanceStrength | TEXT | 枚举 `StanceStrength`（STRONG / MODERATE / WEAK，不折算概率） |
| status | TEXT | NOT NULL，默认「有效」，**扩展列** |

### 3.10 PriceTransmissionLink（前缀 LNK-PT）

该类型是 `relation_types.json` 的 `RELATION_ENTITY`，不是对象类型；字段遵循其 `fields` 定义。

| 列 | 类型 | 约束 |
|---|---|---|
| linkId | TEXT | PK |
| fromSeriesId | TEXT | NOT NULL，FK → `IndicatorSeries.seriesId` |
| toSeriesId | TEXT | NOT NULL，FK → `IndicatorSeries.seriesId`，不得与 `fromSeriesId` 相同 |
| transmissionType | TEXT | NOT NULL，枚举 `TransmissionType` |
| elasticity | REAL | 样本期估计值，非确定关系 |
| lagDays | INTEGER | 滞后期，单位天 |
| samplePeriodId | TEXT | NOT NULL，FK → `TimePeriod.periodId` |
| regimeNote | TEXT | NOT NULL，**收紧**：内核为可选，本包要求必填并写明样本期与政策环境 |

## 4. 对象注册

`app/catalog.py` 的 `SPECS` 是硬编码字典，新对象必须逐条登记，通用接口
`/api/entities/{entityType}`、`/api/ontology/object-types/{type_id}/objects` 才能识别。

新增 10 条 `EntitySpec`（中文对象名 → 表 → 主键）：

| entity_type | table | pk | title_field | id_prefix |
|---|---|---|---|---|
| 统计源 | StatisticalSource | sourceId | sourceName | OBJ-PUB-SS |
| 时间期间 | TimePeriod | periodId | label | OBJ-PUB-TP |
| 计量单位 | UnitOfMeasure | unitId | unitCode | OBJ-PUB-UN |
| 指标序列 | IndicatorSeries | seriesId | seriesId | OBJ-PUB-IN |
| 价格数据点 | PriceDataPoint | pointId | pointId | OBJ-PUB-DP |
| 事件类型 | EventType | eventTypeId | typeName | OBJ-PUB-EV |
| 事件 | Event | eventId | eventTitle | OBJ-PUB-EV |
| 因子 | Factor | factorId | factorName | FAC |
| 断言 | Assertion | assertionId | assertionId | AST |
| 价格传导 | PriceTransmissionLink | linkId | linkId | LNK-PT |

`id_prefix` 取 `kernel/id_registry.json` 权威值（`TimePeriod/UnitOfMeasure/IndicatorSeries/DataPoint/StatisticalSource/Event/EventType` 均为 `OBJ`，`Factor` 为 `FAC`，`Assertion` 为 `AST`，`PriceTransmissionLink` 为 `LNK`）；生成形如 `OBJ-PUB-SS-0001`、`LNK-PT-0001`。
`TimePeriod.periodId` 与 `UnitOfMeasure.unitCode` 是自然键，种子直接使用可读值（`2024-01`、`CNY/t`），不走序号生成。

市场包对象都不加入 `KNOWLEDGE_TYPES`（不进入「政策法规/指标数据/年鉴/情报/关系」五类审核流），
也不加入 `MASTER_TYPES`（不参与实体融合）。

### 4.1 `mapping.json` 注册形态

`seed/mapping.json` 的 `mappings[]` 每项是一个聚合根，本阶段追加 10 项
（`id = M-MAP-MARKET-001..010`，`objectRef = AGG-MARKET-001..010`，`objectRole = AGGREGATE_ROOT`，
`objectName` 与上表 `entity_type` 一致），每项 `tableMappings` 恰好一张表，`tableRole = MASTER`。

`columnMappings` 逐列登记：`attributeRef` 用 `<内核类型>.<列名>`，`column` 与 §3 列名一致，
主键标 `unique=true, isPrimaryKey=true`；外键标 `isForeignKey=true, fkTargetTable, fkTargetColumn`；
枚举列在 `mappingNote` 写明枚举名；「扩展列」在 `mappingNote` 标注 `扩展列（表级）`，便于后续审计追溯。

## 5. 校验门 G3

| 编号 | 名称 | 规则 | 触发点 |
|---|---|---|---|
| G3-1 | 口径恒定 | 同一序列内 `unitId`、`priceBasis`、`taxBasis`、`currencyCode` 只能有一个取值 | 序列创建、数据点写入 |
| G3-2 | 单位一致 | `PriceDataPoint.unitId` 必须等于所属序列的 `unitId` | 数据点写入 |
| G3-3 | 序列唯一 | 同一 `(indicatorId, scopeId)` 只允许一条「有效」序列 | 序列创建 |
| G3-4 | 数据点唯一 | `(seriesId, periodId)` 不重复 | 数据点写入 |
| G3-5 | 传导完整 | `samplePeriodId`、`regimeNote` 必填；`fromSeriesId`/`toSeriesId` 必须存在且不相同 | 传导创建 |
| G3-6 | 事件合法 | `POLICY` 类 `scopeId` 必填；`effectiveFrom <= effectiveTo`；`eventTime` 必填 | 事件创建 |
| G3-7 | 断言去重 | 同机构 `(assertedById, targetPeriodId, subjectId, predicate)` 四元相同且命题相似度 ≥0.85 时合并；跨机构一律并列保留 | 断言写入 |

G3-7 相似度算法（不引入分词依赖）：
1. 归一化：全角转半角、转小写、删除标点与空白、保留数字与单位
2. 中文按字符 bigram 切分，拉丁与数字按空白 token 切分，两者取并集
3. `Jaccard = |A ∩ B| / |A ∪ B|`，分母为 0 时不合并
4. 阈值常量 `ASSERTION_SIMILARITY_THRESHOLD = 0.85`

合并语义：保留先写入的 `assertionId`，写入方更新 `rationale` 与 `assertedAt`，并在返回中标记 `merged=True`。

## 6. 服务与 API

新增 `app/market.py`，导出 `MarketService`，只承担「读聚合 + 校验 + 幂等种子」，不复制通用 CRUD。
写操作（除 `seed_market`）不暴露 HTTP。

### 6.1 方法

| 方法 | 说明 |
|---|---|
| `initialize()` | 校验 10 张表已注册，`sync_dimensions()` 已就绪 |
| `seed_market()` | 幂等装载种子；内部调用 `upsert_assertion()` |
| `overview()` | 首屏聚合 |
| `list_series()` | 序列清单（带最新值与点数） |
| `series_detail(series_id)` | 序列 + Scope 成员 + 数据点 + 关联事件 + 分组断言 |
| `timeline(from_date, to_date, category)` | 事件列表 |
| `assertions(subject_id)` | 按四元分组的多空并列结构 |
| `transmission_graph()` | 节点 + 边 |
| `upsert_assertion(payload)` | G3-7 去重写入 |
| `upsert_series(payload)` | G3-1/G3-3 校验写入 |
| `upsert_point(payload)` | G3-1/G3-2/G3-4 校验写入 |
| `upsert_link(payload)` | G3-5 校验写入 |

### 6.2 路由（全部 GET，需 Bearer token）

| 路由 | 返回 |
|---|---|
| `GET /api/market/overview` | `{seriesCount, pointCount, eventCount, assertionCount, linkCount, latestPeriod, scopeCount}` |
| `GET /api/market/series` | `{items:[{seriesId, indicatorName, commodityName, regionLabel, unitCode, priceBasis, taxBasis, currencyCode, frequency, pointCount, latestValue, latestPeriod}]}` |
| `GET /api/market/series/{series_id}` | `{series, scope:{scopeCode, members}, points:[{periodId, value, publishedAt, revisionNo, qualityFlag}], events, assertions}` |
| `GET /api/market/events?from=&to=&category=` | `{items:[{eventId, eventTitle, eventCategory, eventTime, announceTime, effectiveFrom, effectiveTo, eventScale, severity}]}` |
| `GET /api/market/assertions?subjectId=` | `{groups:[{key:{subjectId, targetPeriodId, predicate}, stance, items:[...]}]}` |
| `GET /api/market/transmissions` | `{nodes:[{id, label, kind}], edges:[{linkId, from, to, transmissionType, elasticity, lagDays, samplePeriodId, regimeNote}]}` |

错误码沿用现有约定：`400` 参数或枚举非法，`404` 对象不存在，`409` 违反唯一性门（G3-3/G3-4/G3-5）。

## 7. 前端工作台

新增 `web/market.html`，不引第三方图表库，沿用 `index.html` 的原生 JS + `fetch` + Bearer token 模式。

结构：
1. 顶栏：品牌名、导航「知识图谱 / 市场」、用户与退出
2. 筛选条：品种、地区、序列、时间范围
3. 主区左：价格序列 SVG 折线（按序列着色，最多 4 条）
4. 主区右：事件时间线 + 断言多空卡片（看多/看空并列，标注机构与目标期）
5. 底部：传导链 SVG（节点 + 带弹性/滞后标签的有向边）
6. 空态与错误态：接口失败显示原因，不静默

`web/index.html` 顶栏增加「市场」入口链接；`web/market.html` 增加返回「知识图谱」入口。

`app/main.py`：
- 挂载 `GET /market.html` → `FileResponse(settings.web_dir / "market.html")`
- 实例化 `app.state.market = MarketService(db, app.state.scope)`，并在 `initialize()` 后调用 `seed_market()`
- 注册 6 条 `/api/market/*` 路由

## 8. 种子数据

双品种对照 + 传导链所需的 2 条下游序列，共 4 条序列。

| 对象 | 数量 | 内容 |
|---|---|---|
| Indicator | 4 | 稻谷河南收购价、玉米全国收购价、生猪养殖成本、猪肉零售价 |
| StatisticalSource | 4 | 示例统计源（粮油信息机构、农业农村主管部门、统计部门、示例交易所） |
| TimePeriod | 12 | 2024-01 至 2024-12，MONTH |
| UnitOfMeasure | 2 | `CNY/t`（PRICE，CNY）、`CNY/kg`（PRICE，CNY） |
| IndicatorSeries | 4 | 稻谷（EX_FARM/INCLUSIVE/CNY/CNY-t）、玉米（同）、生猪成本（EX_FARM/INCLUSIVE/CNY/CNY-kg）、猪肉零售（RETAIL/INCLUSIVE/CNY/CNY-kg） |
| PriceDataPoint | 48 | 每条序列 12 个点，`revisionNo = 0`，`qualityFlag = NORMAL` |
| EventType | 4 | 政策、贸易、供给、市场 |
| Event | 3 | 托市收购启动（POLICY）、进口配额调整（TRADE）、主产区干旱预警（SUPPLY） |
| Factor | 5 | 覆盖 SUPPLY/COST/DEMAND/TRADE/POLICY，`proposedBy = MARKET_PACK` |
| Assertion | 5 | 含 1 组跨机构多空并列、1 组同机构同四元相似命题（用于验证合并后剩 4 条） |
| PriceTransmissionLink | 3 | 玉米→生猪成本（0.55 / 45 天）、生猪成本→猪肉零售（0.62 / 30 天）、玉米→猪肉零售（0.28 / 90 天） |

数据诚实要求：价格数值取公开可核对的量级区间，不编造精确小数；所有示例统计源名称标注为示例，
不冒用真实机构名义；`regimeNote` 写明样本期为构造示例。

## 9. 验收

| 项 | 标准 |
|---|---|
| `tests/test_market.py` | 覆盖 G3-1 至 G3-7 各至少 1 条用例 |
| 路由测试 | 6 条 GET 返回 200 且结构符合 §6.2；未带 token 返回 401 |
| 种子幂等 | `seed_market()` 连续执行两次，各表行数不变；断言合并后为 4 条 |
| 前端 | `market.html` 可加载，四区渲染正常，接口失败有可见错误态 |
| 全量回归 | `pytest` 全绿（含既有 100 passed 基线） |
| 定义层校验 | `python ontology_ly/tools/validate_stage0.py` 保持 `score=100 error=0 warn=0` |

## 10. 不变量

1. 市场包不得新增或删除 `Dimension` 的 10 个维度（门 3 `AP_DIM_CONFLICT`）
2. 市场包不得让 `IvdData` 承载价格点
3. 工作台只读，不得出现新增/编辑/删除按钮
4. 断言跨机构永远并列，不因相似度高而合并
5. 传导边的 `elasticity` 与 `lagDays` 必须标注为样本期估计，不得表述为确定关系
