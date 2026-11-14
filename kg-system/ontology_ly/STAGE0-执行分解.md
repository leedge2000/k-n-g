# 粮油经济本体 · Stage 0 执行分解

> 定位：**共享内核地基**的逐字段落地契约。仅覆盖 Stage 0，不含阶段 1（检索基线）/ 2（政策包）/ 3（市场包）/ 4（联动）的实施细节。
> 性质：**方案文档，不含代码**。所有 JSON 片段是「约定格式」，不是可执行配置。
> 已定三项前提：① Scope 建成显式对象；② 图库直接上 Kùzu；③ 只出方案。

---

## 0. 范围与交付

### 0.1 本阶段做什么

| 编号 | 交付 | 完成判据 |
|---|---|---|
| D1 | 内核 25 个对象类型 + 4 个关系实体化类型的逐字段定义 | 每个字段有类型、是否必填、取值来源；每个类型有 ID 前缀 |
| D2 | 受控词表（枚举）全集 | 所有 enum 有值域与默认值，无 `Other` 兜底 |
| D3 | 三座桥的接口契约 | 谁产出哪条边、能写什么不能写什么，可判定 |
| D4 | 四份清单 | 24 条能力问题 + 100 条术语种子 |
| D5 | 五道校验门 + 反模式码 | 每道门有可判定规则；每个码有触发条件 |
| D6 | 接口规范 | `asOf` 语义唯一；图/SQL 边界清晰 |
| D7 | Kùzu 落地要点 | Node/Rel 表清单、双时间索引、路径上限、幂等策略 |

### 0.2 本阶段**不**做什么

- 不做 OWL / RDFS / 推理机 / 公理约束。语义只靠**字段 + 枚举 + 校验门**表达。
- 政策条款的 Datalog 求值（Nemo）不属于本体推理，不在本阶段范围，见 §10 第 13 项。
- 不建 `DimensionMember` 类型（避免与 `Region` / `Enterprise` / `Commodity` 重复建模）。
- 不建「每个抽到的实体一个对象类型」。对象类型 = schema，实例 = 数据。
- 不引入 ontology 文件格式（`.ttl` / `.owl` / `.rdf`）。唯一真相是 JSON 定义 + Kùzu DDL。
- 不做全文索引引擎替换（向量与 BM25 仍由现有 RAG 层负责，Kùzu 只做图）。

### 0.3 类型数量收敛说明

早前口径写「30—40 个对象类型」，本轮收敛为 **25 内核 + 4 关系实体化 = 29**。收敛原因：

1. 把「质量标准 / 等级」并入 `CommodityForm` 的属性（`standardCode` + `grade` + `qualityParams`），不单建 `QualitySpec`。
2. 把「事件参与方」做成 `Event` 上的 `actorIds` 多值 + `PARTICIPATES_IN` 边，不单建 `EventParticipant`。
3. 把「断言-证据」做成 `Assertion --[:SUPPORTED_BY]--> Evidence` 的带属性边，不单建 `AssertionEvidence`。
4. 「企业角色」并入 `Enterprise.enterpriseType` + `Enterprise --[:OPERATES_AT]--> MarketPlace/LogisticsNode`，不单建 `EnterpriseRole`。

多出来的 4 个全部是**关系实体化**（带参数、需版本、需独立证据），符合三条判别式。

---

## 1. 六条设计铁律

后续所有字段定义都服从这六条，冲突时以此为准。

| # | 铁律 | 判据 |
|---|---|---|
| R1 | **双时间强制** | 每个对象都有 `validFrom/validTo`（业务时间）与 `recordedAt/supersededAt`（记录时间）。缺一不可入库。 |
| R2 | **Scope 显式** | 任何带「品种×地区×市场×口径」语义的对象，必须引用 `scopeId`，不自建维度字段。 |
| R3 | **证据强制** | 每个实例至少一条 `Evidence`（人工录入可标 `DIRECT` + `HUMAN_VERIFIED`）。无证据的实例只能进「草稿区」。 |
| R4 | **包不越权** | 扩展包只能新增实例与新边，**不得修改内核对象的属性定义**，不得新增内核枚举值（需走内核评审）。 |
| R5 | **关系实体化三条判别式** | 参数 ≥2 且有业务含义 / 需有效期或版本链 / 需独立证据。满足任一即建独立类型，否则用带属性的边。 |
| R6 | **口径不外推** | 单位、价格口径、统计来源三者必须同时确定才允许比较；缺一即拒绝跨序列运算。 |

---

## 2. 通用字段集（所有对象共享）

下列字段在每个对象类型上都存在，后文各类型表**只列差异字段**，不再重复。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 主键，按 §4 规范生成，写入即冻结 |
| `name` | string | 是 | 中文名 |
| `alias` | string[] | 否 | 别名/简称/英文名，供实体链接召回 |
| `code` | string | 否 | 业务编码（品种码、行政区划码、文号等） |
| `description` | string | 否 | 一句话说明 |
| `domainCode` | enum | 是 | `PUB` / `S1`~`S7`，见 §4.2 |
| `validFrom` | date | 是 | 业务生效日 |
| `validTo` | date | 否 | 业务失效日，`null` = 至今有效 |
| `recordedAt` | datetime | 是 | 本系统首次入库时间（记录时间起点） |
| `supersededAt` | datetime | 否 | 被新版本取代的**派生缓存**，真值以 `SUPERSEDES/AMENDS/EXTENDS` 边为准 |
| `status` | enum | 是 | `ACTIVE` / `MERGED` / `RETIRED` / `DRAFT` |
| `version` | int | 是 | 从 1 起，每次实质修订 +1 |
| `evidenceIds` | string[] | 是 | 至少 1 条 `Evidence.id`；`DRAFT` 状态除外 |
| `confidence` | float | 是 | 0—1，人工录入=1.0 |
| `extractedBy` | enum | 是 | `RULE` / `LLM` / `HUMAN` / `IMPORT` |
| `authority` | enum | 是 | 命名权威：`LY`（自建）/ `AGROVOC` / `GB` / `ISO` / `CUSTOMS` |
| `mergedIntoId` | string | 否 | `status=MERGED` 时指向保留项 |

> 通用字段的 JSON 表述统一放在 `attributes` 之外（作为对象的一级键），避免与业务属性混淆。Kùzu 建表时按列落地。

---

## 3. 内核对象定义（25 + 4）

### 3.1 品类域（3）

#### 3.1.1 `CommodityCategory` 品类类目
ID 前缀 `OBJ-CM`。树形，最多 4 层。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parentCategoryId` | ref CommodityCategory | 否 | 根为 `null` |
| `categoryType` | enum | 是 | `GRAIN_OIL`（粮油根）/`GRAIN`（谷物）/`OILSEED`（油料）/`OIL`（油脂）/`BYPRODUCT`（副产物）/`FOOD`（食品） |
| `level` | int | 是 | 1—4 |
| `path` | string | 是 | 物化路径，如 `粮油/谷物/小麦`，由 `parentCategoryId` 派生 |
| `agrovocUri` | string | 否 | 上位词映射，仅根节点与一级节点建 |

#### 3.1.2 `Commodity` 品种
ID 前缀 `OBJ-CM`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `categoryId` | ref CommodityCategory | 是 | |
| `commodityCode` | string | 是 | 全局唯一短码，如 `SOYBEAN` / `CORN` |
| `scientificName` | string | 否 | 拉丁学名 |
| `stapleType` | enum | 是 | `RATION`（口粮）/`FEED`（饲料粮）/`OIL`（油料）/`INDUSTRIAL`（工业用）/`OTHER` |
| `customsCode` | string | 否 | HS 编码（多为 4—8 位前缀，非精确到单一品种） |
| `isBt` | bool | 否 | 是否转基因（仅大豆/玉米/菜籽用） |

#### 3.1.3 `CommodityForm` 形态规格
同一品种的不同可交易形态。ID 前缀 `OBJ-CM`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `commodityId` | ref Commodity | 是 | |
| `formType` | enum | 是 | `RAW_GRAIN` / `PROCESSED_GRAIN` / `CRUDE_OIL` / `REFINED_OIL` / `MEAL` / `STARCH` / `SEED` |
| `derivedFromFormId` | ref CommodityForm | 否 | 由哪个形态加工而来 |
| `standardCode` | string | 否 | 国标/行标号，如 `GB 1351` |
| `grade` | string | 否 | 等级，如 `一等` / `四级` |
| `qualityParams` | json | 否 | `{水分, 杂质, 含油率, 蛋白, 酸价, ...}`，键名受控 |

### 3.2 地理域（3）

#### 3.2.1 `Region` 行政区 / 经济区
ID 前缀 `OBJ-RG`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `regionCode` | string | 是 | 行政区划码或自定义码 |
| `regionLevel` | enum | 是 | `COUNTRY` / `PROVINCE` / `CITY` / `COUNTY` / `ECONOMIC_ZONE` |
| `parentRegionId` | ref Region | 否 | |
| `isoCode` | string | 否 | 国家用 `ISO 3166-1 alpha-2` |
| `isProductionArea` | bool | 否 | 主产区标记 |
| `isConsumptionArea` | bool | 否 | 主销区标记 |

#### 3.2.2 `MarketPlace` 市场 / 交易场所
ID 前缀 `OBJ-MK`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `marketType` | enum | 是 | `FUTURES` / `WHOLESALE` / `SPOT` / `AUCTION` / `PORT` |
| `regionId` | ref Region | 是 | |
| `operatorOrgId` | ref Organization | 否 | 交易所/市场管理方 |
| `exchangeCode` | string | 否 | 如 `DCE` / `ZCE` / `CBOT` |
| `tradingHours` | string | 否 | 自由文本 |

#### 3.2.3 `LogisticsNode` 物流节点
ID 前缀 `OBJ-LG`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `nodeType` | enum | 是 | `PORT` / `WAREHOUSE` / `RAIL_HUB` / `BORDER` / `MILL` |
| `regionId` | ref Region | 是 | |
| `capacity` | float | 否 | 设计能力 |
| `capacityUnitId` | ref UnitOfMeasure | 否 | |
| `geo` | string | 否 | `"lon,lat"` |

### 3.3 主体域（3）

#### 3.3.1 `Organization` 机构
政府部门 / 协会 / 研究机构 / 媒体 / 国际组织 / 交易所。ID 前缀 `OBJ-OR`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `orgType` | enum | 是 | `GOVERNMENT` / `ASSOCIATION` / `RESEARCH` / `MEDIA` / `INTERNATIONAL` / `EXCHANGE` |
| `parentOrgId` | ref Organization | 否 | |
| `regionId` | ref Region | 否 | |
| `functionScope` | enum[] | 否 | `RESERVE`（储备管理）/`PRICE_MONITOR`（价格监测）/`TRADE_ADMIN`（进出口管理）/`STATISTICS`（统计发布）/`STANDARD`（标准制定）/`SUBSIDY`（补贴） |

#### 3.3.2 `Enterprise` 企业
ID 前缀 `OBJ-EN`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `enterpriseType` | enum | 是 | `STATE_RESERVE` / `PROCESSOR` / `TRADER` / `BREEDER` / `FEED` / `RETAIL` / `LOGISTICS` |
| `regionId` | ref Region | 是 | 注册地 |
| `creditCode` | string | 否 | 统一社会信用代码 |
| `isListed` | bool | 否 | |
| `capacity` | float | 否 | 加工/仓储能力 |
| `capacityUnitId` | ref UnitOfMeasure | 否 | |

#### 3.3.3 `Person` 人物
ID 前缀 `OBJ-PS`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `personRole` | enum | 是 | `OFFICIAL` / `EXPERT` / `ANALYST` / `SPOKESPERSON` |
| `affiliationOrgId` | ref Organization | 否 | |
| `title` | string | 否 | 职务 |

### 3.4 时间域（2）

#### 3.4.1 `TimePeriod` 时间期间
ID 前缀 `OBJ-TP`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `periodType` | enum | 是 | `DAY` / `WEEK` / `DECADE`（旬）/ `MONTH` / `QUARTER` / `HALF_YEAR` / `YEAR` / `MARKETING_YEAR` / `GRAIN_YEAR` / `POLICY_CYCLE` |
| `startDate` | date | 是 | |
| `endDate` | date | 是 | |
| `calendarSchemeId` | ref CalendarScheme | 否 | 非自然年必填 |
| `label` | string | 是 | 展示用，如 `2025/26年度` |

#### 3.4.2 `CalendarScheme` 年度口径
ID 前缀 `OBJ-TP`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schemeType` | enum | 是 | `CALENDAR_YEAR` / `GRAIN_YEAR` / `MARKETING_YEAR` |
| `startMonth` | int | 是 | 1—12 |
| `startDay` | int | 是 | |
| `appliesToCategoryId` | ref CommodityCategory | 否 | 作物收割年度按品类区分 |
| `regionId` | ref Region | 否 | 不同国家年度起止不同 |
| `note` | string | 否 | |

### 3.5 指标域（5）

#### 3.5.1 `UnitOfMeasure` 计量单位与换算
ID 前缀 `OBJ-UN`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `unitCode` | string | 是 | `t` / `kt` / `10kt`(万吨) / `CNY/t` / `USD/t` / `USC/bu` / `%` / `ha` |
| `dimension` | enum | 是 | `MASS` / `VOLUME` / `AREA` / `CURRENCY` / `RATIO` / `PRICE` |
| `baseUnitCode` | string | 否 | 换算基准 |
| `factor` | float | 否 | 到基准的系数（1 万吨 = 10000 t） |
| `precision` | int | 否 | 展示小数位 |
| `currencyCode` | string | 否 | 价格类必填，`CNY` / `USD` |

#### 3.5.2 `Indicator` 指标定义
ID 前缀 `OBJ-IN`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `indicatorCode` | string | 是 | 全局唯一 |
| `indicatorType` | enum | 是 | `PRICE` / `VOLUME` / `AREA` / `RATIO` / `INDEX` / `STOCK` / `CAPACITY` |
| `unitId` | ref UnitOfMeasure | 是 | |
| `defaultFrequency` | enum | 是 | 同 `TimePeriod.periodType` |
| `statisticalSourceId` | ref StatisticalSource | 否 | 派生指标可为 `null` |
| `isDerived` | bool | 是 | |
| `calculationFormula` | string | 否 | `isDerived=true` 时必填，仅描述，不执行 |
| `direction` | enum | 否 | `HIGHER_IS_TIGHTER`（越高越紧）等方向语义，供解释用 |

#### 3.5.3 `IndicatorSeries` 指标序列
**口径载体**。ID 前缀 `OBJ-IN`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `indicatorId` | ref Indicator | 是 | |
| `commodityFormId` | ref CommodityForm | 否 | |
| `scopeId` | ref Scope | 是 | 承载地区 / 市场 / 税基 / 贸易口径 |
| `frequency` | enum | 是 | |
| `statisticalSourceId` | ref StatisticalSource | 是 | |
| `externalSeriesCode` | string | 否 | 外部数据源序列号 |
| `priceBasis` | enum | 否 | `EX_FARM`（收购）/`WHOLESALE`/`RETAIL`/`FOB`/`CIF`/`DELIVERED`（到厂）/`FUTURES` |
| `taxBasis` | enum | 否 | `INCLUSIVE`（含税）/`EXCLUSIVE` |
| `currencyCode` | string | 否 | |
| `startPeriodId` | ref TimePeriod | 否 | |
| `endPeriodId` | ref TimePeriod | 否 | |

#### 3.5.4 `DataPoint` 数据点
ID 前缀 `OBJ-DP`（量大，用 `seriesId+periodId` 组合唯一约束）。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `seriesId` | ref IndicatorSeries | 是 | |
| `periodId` | ref TimePeriod | 是 | |
| `value` | float | 是 | |
| `unitId` | ref UnitOfMeasure | 是 | 必须与序列单位一致 |
| `publishedAt` | datetime | 是 | 发布时间（≠ 期间） |
| `revisionNo` | int | 是 | 从 0 起，修订一次 +1 |
| `qualityFlag` | enum | 是 | `NORMAL` / `ESTIMATED` / `PROVISIONAL` / `REVISED` / `MISSING` |

#### 3.5.5 `StatisticalSource` 统计来源
ID 前缀 `OBJ-SS`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `sourceType` | enum | 是 | `CUSTOMS` / `NBS` / `MARA` / `NDRC` / `USDA` / `FAO` / `EXCHANGE` / `COMPANY` / `OTHER` |
| `orgId` | ref Organization | 否 | |
| `releasePattern` | string | 否 | 如「每月 14 日前后」 |
| `reliabilityScore` | float | 否 | 0—1 |
| `revisionPolicy` | enum | 否 | `NO_REVISION` / `PERIODIC` / `AD_HOC` |

### 3.6 事件域（2）

#### 3.6.1 `Event` 事件
ID 前缀 `OBJ-EV`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `eventTypeId` | ref EventType | 是 | |
| `eventTime` | date | 是 | 发生时间 |
| `announceTime` | datetime | 否 | 公布时间（政策类关键） |
| `effectiveFrom` | date | 否 | 政策生效日 |
| `effectiveTo` | date | 否 | 政策到期日 |
| `eventScale` | enum | 是 | `NATIONAL` / `REGIONAL` / `LOCAL` |
| `scopeId` | ref Scope | 否 | 适用范围（政策类必填） |
| `actorOrgIds` | ref Organization[] | 否 | 发布/参与主体 |
| `severity` | enum | 否 | `HIGH` / `MEDIUM` / `LOW` |
| `parentEventId` | ref Event | 否 | 事件链上级 |

#### 3.6.2 `EventType` 事件类型
ID 前缀 `OBJ-EV`。**受控词表，Stage 0 只建类型不建实例**。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `eventCategory` | enum | 是 | `POLICY` / `MARKET` / `SUPPLY` / `DEMAND` / `TRADE` / `WEATHER` / `DISASTER` / `CORPORATE` |
| `isPolicyTool` | bool | 是 | 是否属于政策工具 |
| `toolVerb` | string | 否 | 政策工具动词，如「启动最低收购价收购」 |
| `defaultImpactDirection` | enum | 否 | `POSITIVE` / `NEGATIVE` / `UNCERTAIN`（对价格） |

### 3.7 文档与证据域（3）

#### 3.7.1 `Document` 文档
ID 前缀 `OBJ-DC`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `docType` | enum | 是 | `POLICY_DOC` / `REPORT` / `NEWS` / `STATISTIC_BULLETIN` / `ANNOUNCEMENT` / `LAW` / `STANDARD` |
| `issuerOrgId` | ref Organization | 否 | |
| `docNo` | string | 否 | 文号 |
| `publishDate` | date | 是 | |
| `effectiveDate` | date | 否 | |
| `url` | string | 否 | |
| `filePath` | string | 否 | 本地原件路径 |
| `checksum` | string | 是 | sha256，去重键 |
| `pageCount` | int | 否 | |
| `authorityLevel` | enum | 否 | `NATIONAL_LAW` / `STATE_COUNCIL` / `MINISTRY` / `PROVINCIAL` / `MUNICIPAL` / `OTHER` |

#### 3.7.2 `DocumentSegment` 段落
ID 前缀 `OBJ-DS`。与现有 `SourceDoc_Segment` 对齐，不重建。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `documentId` | ref Document | 是 | |
| `segmentNo` | int | 是 | |
| `text` | string | 是 | |
| `pageNo` | int | 否 | |
| `charStart` / `charEnd` | int | 是 | 全局偏移 |
| `segmentType` | enum | 是 | `TITLE` / `BODY` / `TABLE` / `FOOTNOTE` / `ANNEX` / `HEADER` |
| `embeddingRef` | string | 否 | 向量索引中的主键 |

#### 3.7.3 `Evidence` 证据
ID 前缀 `OBJ-ED`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `targetType` | enum | 是 | 被证明的对象类型（29 取一） |
| `targetId` | string | 是 | |
| `documentId` | ref Document | 是 | |
| `segmentId` | ref DocumentSegment | 否 | 精确到段时必有 |
| `quote` | string | 是 | 摘句，≤200 字 |
| `quoteHash` | string | 是 | sha1(quote)，去重 |
| `pageNo` | int | 否 | |
| `evidenceType` | enum | 是 | `DIRECT`（直接陈述）/`DERIVED`（推算）/`CITED`（转引）/`HUMAN_VERIFIED` |
| `strength` | float | 是 | 0—1，`DIRECT`+原文=1.0 |
| `verifiedBy` | string | 否 | |
| `verifiedAt` | datetime | 否 | |

### 3.8 维度与范围域（2）

#### 3.8.1 `Dimension` 维度定义
ID 前缀 `DIM`。**Stage 0 只建 10 个固定维度，不允许扩展包新增。**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `dimensionCode` | enum | 是 | 见下表 |
| `valueObjectType` | enum | 是 | 该维度取值来自哪个对象类型 |
| `multiValued` | bool | 是 | 是否允许多值 |
| `hierarchical` | bool | 是 | 是否可沿父子展开 |

固定 10 维：

| dimensionCode | 含义 | valueObjectType | 多值 | 层级 |
|---|---|---|---|---|
| `COMMODITY` | 品种/形态 | Commodity / CommodityForm | 是 | 是 |
| `GEOGRAPHY` | 地区 | Region | 是 | 是 |
| `ACTOR` | 主体 | Organization / Enterprise / Person | 是 | 否 |
| `CHANNEL` | 市场渠道 | MarketPlace | 是 | 否 |
| `TIME_WINDOW` | 适用时间窗 | TimePeriod | 否 | 否 |
| `UNIT_BASIS` | 单位与价格口径 | UnitOfMeasure | 否 | 否 |
| `POLICY_TOOL` | 政策工具 | EventType（`isPolicyTool=true`） | 是 | 是 |
| `TAX_BASIS` | 税基 | 内联枚举（含税/不含税） | 否 | 否 |
| `QUALITY_GRADE` | 质量等级 | CommodityForm.`grade` | 否 | 是 |
| `TRADE_FLOW` | 贸易流向 | 内联枚举（进口/出口/内贸/转口） | 否 | 否 |

#### 3.8.2 `Scope` 范围组合
ID 前缀 `SCP`。**显式对象的核心。**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `scopeCode` | string | 是 | 可读短码，如 `POL-MINPRICE-WHEAT-2024` |
| `scopeType` | enum | 是 | `POLICY_SCOPE` / `SERIES_SCOPE` / `EVENT_SCOPE` / `ASSERTION_SCOPE` |
| `members` | array | 是 | `[{dimensionCode, objectType, objectId}]`，按 `dimensionCode` 升序存 |
| `isOpen` | bool | 是 | 是否含「全国所有」「全部品种」等开放语义 |
| `normalizedKey` | string | 是 | `sha1(scopeType + 排序后的 dimensionCode:objectId 列表)`，用于集合比较与去重 |
| `supersetOf` | ref Scope[] | 否 | 显式上下位，便于「政策范围⊇价格序列范围」判断 |

**关键约束**：`normalizedKey` 相同即视为同一 Scope，禁止重复建；`members` 中每个 `objectType` 必须与 `Dimension.valueObjectType` 兼容，否则门 2 拒绝。

### 3.9 因子域（1）

#### 3.9.1 `Factor` 驱动因子
ID 前缀 `FAC`。类型定义在内核，**实例由扩展包提案、内核评审发布**。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `factorCode` | string | 是 | 全局唯一 |
| `factorCategory` | enum | 是 | 一级 7 类，固定：`SUPPLY` / `DEMAND` / `COST` / `POLICY` / `TRADE` / `FINANCE` / `RELATED`（关联品） |
| `factorSubCategory` | string | 是 | 二级，受控词表，可扩展 |
| `defaultDirection` | enum | 是 | 对该品类价格的默认方向：`UP` / `DOWN` / `NEUTRAL` / `UNCERTAIN` |
| `measuredByIndicatorId` | ref Indicator | 否 | 可定量化的因子必填（因子≠指标，但可被指标度量） |
| `scopeId` | ref Scope | 否 | 因子作用范围 |
| `mechanism` | string | 否 | 作用机理一句话 |
| `proposedBy` | enum | 是 | `KERNEL` / `POLICY_PACK` / `MARKET_PACK` |

### 3.10 断言域（1）

#### 3.10.1 `Assertion` 断言
ID 前缀 `AST`。**多空并存，不合并。**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `assertionType` | enum | 是 | `FORECAST`（预测）/`JUDGEMENT`（判断）/`ESTIMATE`（估计）/`CAUSAL`（归因）/`POLICY_INTENT`（政策意图） |
| `subjectType` | enum | 是 | 断言的落点类型 |
| `subjectId` | string | 是 | 通常为 Commodity / IndicatorSeries / Factor / Event |
| `predicate` | enum | 是 | `WILL_RISE` / `WILL_FALL` / `BULLISH` / `BEARISH` / `CAUSES` / `SUPPORTS` / `WEAKENS` / `UNCHANGED` |
| `objectValue` | float | 否 | 定量断言的目标值 |
| `objectUnitId` | ref UnitOfMeasure | 否 | `objectValue` 存在时必填 |
| `rangeLow` / `rangeHigh` | float | 否 | 区间断言 |
| `targetPeriodId` | ref TimePeriod | 是 | 断言所指期间 |
| `scopeId` | ref Scope | 是 | 断言作用范围 |
| `assertedByType` | enum | 是 | `ORGANIZATION` / `PERSON` |
| `assertedById` | string | 是 | |
| `assertedAt` | date | 是 | 断言发表日 |
| `horizon` | enum | 是 | `SHORT`（≤1 季）/`MEDIUM`（≤1 年）/`LONG`（>1 年） |
| `rationale` | string | 否 | 理由摘要 ≤300 字 |
| `stanceStrength` | enum | 否 | `STRONG` / `MODERATE` / `WEAK`（原话强度，不折算概率） |

**去重规则（替代 `restatesTo`）**：`(assertedById, targetPeriodId, subjectId, predicate)` 四元相同 + 命题相似度 ≥0.85 → 合并计一条，保留最早 `assertedAt`；跨机构一律并列保留。

### 3.11 关系实体化（4）

满足 R5 三条判别式，独立建类型。

#### 3.11.1 `TransformationProcess` 加工转化
ID 前缀 `LNK-TR`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `inputFormIds` | ref CommodityForm[] | 是 | 如「进口大豆」 |
| `outputFormIds` | ref CommodityForm[] | 是 | 如「豆油 + 豆粕」 |
| `processType` | enum | 是 | `CRUSHING` / `MILLING` / `REFINING` / `FEED_MIX` / `STARCH` |
| `yieldRates` | json | 是 | `{outputFormId: 出率}`，合计可 <1（损耗） |
| `capacity` | float | 否 | |
| `capacityUnitId` | ref UnitOfMeasure | 否 | |
| `regionId` | ref Region | 否 | 区域性工艺差异 |

#### 3.11.2 `SubstitutionRelation` 替代关系
ID 前缀 `LNK-SB`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `fromFormId` | ref CommodityForm | 是 | |
| `toFormId` | ref CommodityForm | 是 | |
| `direction` | enum | 是 | `ONE_WAY` / `TWO_WAY` |
| `substitutionRatio` | float | 是 | 替代比（营养当量或工艺当量） |
| `triggerCondition` | string | 否 | 如「价差 > 300 元/吨」 |
| `scopeId` | ref Scope | 是 | 替代关系随地区/时段变化 |

#### 3.11.3 `PriceTransmissionLink` 价格传导
ID 前缀 `LNK-PT`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `fromSeriesId` | ref IndicatorSeries | 是 | |
| `toSeriesId` | ref IndicatorSeries | 是 | |
| `transmissionType` | enum | 是 | `COST_PUSH` / `DEMAND_PULL` / `ARBITRAGE` / `POLICY` |
| `elasticity` | float | 否 | 传导系数 |
| `lagDays` | int | 否 | |
| `regimeNote` | string | 否 | 政策周期不同则传导机制不同，必须写明样本期 |
| `samplePeriodId` | ref TimePeriod | 是 | 估计所用样本期 |

#### 3.11.4 `ImpactLink` 政策影响
ID 前缀 `LNK-IM`。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `sourceType` | enum | 是 | `EVENT` / `POLICY` |
| `sourceId` | string | 是 | |
| `targetType` | enum | 是 | `COMMODITY` / `FACTOR` / `SERIES` |
| `targetId` | string | 是 | |
| `impactDirection` | enum | 是 | `POSITIVE` / `NEGATIVE` / `UNCERTAIN` |
| `impactMagnitude` | enum | 否 | `STRONG` / `MODERATE` / `WEAK`；**政策包不得填写，只允许市场包反验后回填** |
| `lagDays` | int | 否 | |
| `mechanism` | string | 否 | |
| `assertedByType` / `assertedById` | enum / string | 否 | 谁给出这条影响判断 |

---

## 4. ID 规范与域码

### 4.1 ID 格式

```
<前缀>-<域码>-<语义片段>[-<限定>]
```

- 前缀：`OBJ`（对象）/ `LNK`（关系实体）/ `DIM`（维度）/ `SCP`（范围）/ `FAC`（因子）/ `AST`（断言）。
- 域码：2—4 位全大写，全模型唯一（§4.2）。
- 语义片段：大写到 `A-Z0-9-`，≤40 字符，不含年份除非该对象本身按年度独立存在（如年度最低收购价预案）。
- 限定：可选，用于消歧（`V2` / `2024` / `CN`）。
- **写入即冻结**：任何字段可改，`id` 不可改。合并走 `status=MERGED` + `mergedIntoId`。

示例：
```
OBJ-PUB-SOYBEAN
OBJ-S3-SOYBEAN-CRUDE-OIL
OBJ-S4-MINPRICE-WHEAT-2024
DIM-PUB-GEOGRAPHY
SCP-POL-MINPRICE-WHEAT-2024
FAC-PUB-SUPPLY-PORT-STOCK
LNK-S3-CRUSH-SOYBEAN
AST-S6-2025Q1-SOYBEAN-BULL-CITIC
```

### 4.2 域码分配

| 域码 | 名称 | 归口 | 典型对象 |
|---|---|---|---|
| `PUB` | 共享内核 | 内核组 | 品类、地理、主体、时间、单位、维度、文档、证据 |
| `S1` | 口粮 | 政策包 + 市场包 | 稻谷、小麦、大米、面粉 |
| `S2` | 饲料粮 | 政策包 + 市场包 | 玉米、高粱、大麦、DDGS |
| `S3` | 油脂油料 | 政策包 + 市场包 | 大豆、菜籽、棕榈、花生、棉籽、豆油、豆粕 |
| `S4` | 政策与储备 | 政策包 | 政策文件、托市、临储、轮换、拍卖 |
| `S5` | 进出口与贸易 | 政策包 + 市场包 | 关税配额、滑准税、海关数据、到港 |
| `S6` | 价格与期货 | 市场包 | 现货序列、期货合约、基差、价差 |
| `S7` | 加工产业链 | 市场包 | 压榨、精炼、饲料配方、开工率 |

**分配铁律**：
- 一个对象只归一个域码。跨域引用用边，不用双域码。
- `PUB` 下的对象属性由内核组独占定义，扩展包只读。
- 新增域码需内核评审，且不得与现有码前缀冲突。

---

## 5. 三座桥的接口契约

「桥」= 允许跨包写入的**唯一**通道。桥以外的跨包写入一律拒绝。

### 5.1 桥一：政策事件桥

| 项 | 规定 |
|---|---|
| 产出方 | 政策研究包 |
| 允许写的边 | `Event -[:GOVERNS_SCOPE]-> Scope`；`Event -[:IMPACTS]-> Commodity/Factor`（生成 `ImpactLink`） |
| 允许写的字段 | `impactDirection`、`lagDays`、`mechanism`、`assertedBy*` |
| **禁止** | 写 `impactMagnitude`（留给市场包反验）；改 `Event` 的 `eventTypeId` 之外的内核属性 |
| 必填 | 每条 `ImpactLink` 至少 1 条 `Evidence`，且 `quote` 必须含方向词或工具动词 |
| 反模式 | `AP_POLICY_MAGNITUDE`（政策包越权写强度） |

### 5.2 桥二：因子桥

| 项 | 规定 |
|---|---|
| 类型归属 | `Factor` 类型定义在内核，扩展包不得改枚举 |
| 实例归属 | 包内可「提案」`Factor` 实例（`status=DRAFT` + `proposedBy`），内核评审后置 `ACTIVE` |
| 政策包可写 | `Event -[:ACTS_ON_FACTOR]-> Factor`（政策作用于哪个因子） |
| 市场包可写 | `Factor -[:DRIVES]-> IndicatorSeries`（因子驱动哪条序列）、`Factor -[:MEASURED_BY]-> Indicator` |
| **禁止** | 政策包写 `DRIVES`（会绕过价格反验）；市场包写 `ACTS_ON_FACTOR`（会侵占政策语义） |
| 必填 | `DRIVES` 边必须有 `elasticity` 或 `lagDays` 至少一项，否则门 2 拒绝 |
| 反模式 | `AP_ORPHAN_FACTOR`（因子无任何 `DRIVES`/`ACTS_ON_FACTOR` 边） |

### 5.3 桥三：断言桥

| 项 | 规定 |
|---|---|
| 产出方 | 任何包均可产出 `Assertion` |
| 必填三件套 | `scopeId` + `assertedById` + ≥1 条 `SUPPORTED_BY` 边指向 `Evidence` |
| 并存规则 | 同 `(subjectId, predicate, targetPeriodId)` 下多机构并列，**永不合并**；同机构同期间重复按 §3.10 去重规则处理 |
| 禁止 | 无 `scopeId` 的断言；把断言当事实写入内核对象属性 |
| 一致性 | 断言引用的事件/序列/因子必须存在，否则 `REF_UNRESOLVED` |
| 反模式 | `AP_ASSERT_NO_EVIDENCE`、`AP_DUP_ASSERT` |

### 5.4 桥的公共约束（三桥共用）

1. **不改内核 schema**：扩展包只能在 `seed/` 之外的 `ontology_ly/packs/<pack>/` 下新增实例文件，不得改 `kernel/*.json`。
2. **不跨桥绕行**：政策包想影响价格，只能走 `Event→Factor`（桥二）+ `Factor→Series`（市场包，桥二），不能直接 `Event→Series`。
3. **双向闭合**：桥产生的边必须有反向可查路径（`SUPERSEDES` 反向 `SUPERSEDED_BY` 在查询层派生，不落库）。
4. **版本化**：桥产生的每条边带 `version` 与 `evidenceIds`，修订即新增版本，不覆盖。

---

## 6. 四份清单

### 6.1 能力问题（24 条，验收契约）

**A · 供需与平衡（6）**

| 编号 | 问题 | 依赖对象 | 图路径上限 |
|---|---|---|---|
| A1 | 某品种某年度国内产量、进口量、消费量、期末库存分别是多少？ | DataPoint / IndicatorSeries / TimePeriod | 2 |
| A2 | 某品种的产需缺口有多大，缺口靠进口还是库存补？ | 同上 + TradeFlow | 3 |
| A3 | 某品种的压榨/加工产出结构与出率是多少？ | TransformationProcess | 2 |
| A4 | 某地区某品种的库存消费比近五年变化？ | DataPoint + Region + TimePeriod | 2 |
| A5 | 某品种的替代品有哪些，替代比是多少？ | SubstitutionRelation | 2 |
| A6 | 某年度饲料粮需求中，玉米/高粱/大麦各占多少？ | DataPoint + Commodity | 2 |

**B · 政策与储备（6）**

| 编号 | 问题 | 依赖对象 | 图路径上限 |
|---|---|---|---|
| B1 | 某品种当前生效的最低收购价政策是哪个文件，价格多少？ | Event + Scope + DataPoint | 2 |
| B2 | 某政策适用于哪些品种、哪些地区、哪些主体？ | Event + Scope.members | 1 |
| B3 | 某政策从发布到目前经历了几次修订/替代/延期？ | Event + SUPERSEDES/AMENDS/EXTENDS | 4 |
| B4 | 某时点（asOf）有效的储备政策集合是什么？ | Event + 双时间过滤 | 1 |
| B5 | 某政策的收储/投放规模与节奏？ | Event + DataPoint | 2 |
| B6 | 两个政策文件的适用范围是否重叠？ | Scope.normalizedKey 集合比较 | 0（纯 Scope 运算） |

**C · 价格与传导（6）**

| 编号 | 问题 | 依赖对象 | 图路径上限 |
|---|---|---|---|
| C1 | 某品种某市场某口径的价格序列是什么？ | IndicatorSeries + Scope | 1 |
| C2 | 两个价格序列的口径差异在哪（地区/税/交货方式）？ | Scope 差异集 | 0 |
| C3 | 某价格变化向上下游传导的路径与滞后期？ | PriceTransmissionLink | 3 |
| C4 | 某政策发布后，目标品种价格在 N 日内变化多少？ | ImpactLink + DataPoint | 3 |
| C5 | 某品种的期货与现货基差走势？ | IndicatorSeries ×2 | 1 |
| C6 | 某品种价格的主要驱动因子排序？ | Factor + DRIVES + 边参数 | 2 |

**D · 分歧与证据（6）**

| 编号 | 问题 | 依赖对象 | 图路径上限 |
|---|---|---|---|
| D1 | 对某品种某期间的价格判断，机构间分歧是什么？ | Assertion（跨机构并列） | 1 |
| D2 | 某条断言的原文出处与页码？ | Assertion→Evidence→DocumentSegment | 2 |
| D3 | 某断言的证据强度与发布时点？ | Assertion + Evidence.strength | 1 |
| D4 | 某机构历史断言的准确率如何？ | Assertion + DataPoint 事后比对 | 3 |
| D5 | 同一事件，不同机构的解读差异？ | Event + Assertion | 2 |
| D6 | 某结论涉及几次转引（CITED 链）？ | Evidence.evidenceType 链 | 3 |

> 验收方式：每条能力问题必须能在 29 个对象类型 + 34 条边上找到一条**不超过其路径上限**的可答路径；找不到即 Stage 0 不通过。

### 6.2 术语种子（100 条）

结构（`terms/ly_terms.json` 数组元素）：

```json
{
  "termId": "TRM-LY-0001",
  "term": "托市",
  "canonical": "最低收购价执行预案",
  "termType": "POLICY_TOOL",
  "synonyms": ["托市收购", "最低收购价收购"],
  "domainCode": "S4",
  "agrovocUri": null,
  "authority": "LY",
  "note": "口语；正式文书中不出现"
}
```

`termType` 枚举：`POLICY_TOOL` / `POLICY_TERM` / `MARKET_TERM` / `UNIT_TERM` / `ORG_ALIAS` / `COMMODITY_ALIAS` / `PROCESS_TERM` / `REGULATION_TERM`

配额（合计 100）：

| 类别 | 条数 | 样例 |
|---|---|---|
| 政策工具 | 30 | 托市、临储、轮换、抛储、竞价销售、定向拍卖、最低收购价、目标价格补贴、生产者补贴、进口配额、滑准税、储备棉投放 |
| 品种口语别名 | 20 | 黄豆、苞米、谷子、菜籽、棕榈、豆粕、麸皮、稻子、麦子、花生仁 |
| 机构别名 | 10 | 中储粮、国储、中粮、益海、九三、北大荒、五得利 |
| 市场术语 | 20 | 到厂价、平舱价、车板价、离岸价、现货升贴水、基差、盘面、港口库存、开机率、压榨利润 |
| 计量与口径 | 10 | 万吨、蒲式耳、美分/蒲、出油率、出粕率、容重、水分、杂质 |
| 加工与品质 | 10 | 压榨、浸出、一级油、四级油、脱胶、精炼、膨化 |

补齐规则：
- Stage 0 先出 **100 条高频种子**（覆盖 A/B/C/D 四组问题的提问用词）。
- 其余在阶段 1「实体链接失败样本」中增量补，全量预估 300—800 条。
- 每条必须能映射到 `canonical`（指向内核对象或 `EventType.toolVerb`）；映射不上的一律标 `termType=POLICY_TERM` 待人工裁决。

---

## 7. 五道校验门 + 反模式码

> 编号以 `gates/rules.json` 为唯一真相，下表与之一一对应。同一门规则下的并列子检查用 `a/b/c` 后缀（如 `G3-3a`），父编号即门规则编号。旧稿编号 → 现行编号的映射见 §7.7。

### 7.1 门 0 · 结构门（逐条记录级）

| 规则 | 名称 | 判据 |
|---|---|---|
| G0-1 | 交付物齐全 | 15 项交付物存在且可被 `json.load` 解析 |
| G0-2 | 主键唯一且合规 | `id` 全集唯一，且匹配 `^(OBJ|LNK|DIM|SCP|FAC|AST)-[A-Z]{2,4}-[A-Z0-9-]+$` |
| G0-3 | 公共字段完整 | §2 的 `commonFields` 必填项均非空 |
| G0-4 | 引用可解析 | 每个 `ref` 指向的目标 `id` 存在且类型与声明一致 |
| G0-5 | 枚举值合法 | 每个 `enum` 取值属于 `enums.json` 对应词表 |
| G0-6 | 无兜底枚举 | `OTHER` / `DEFAULT` 值必须带 `explicitOnly=true` |
| G0-7 | 字段值类型匹配 | 取值形态与声明类型一致（date / datetime / float / ref / enum） |
| G0-8 | 动作命名非 CRUD | `actions.json` 中仅由 CRUD 动词构成的动作占比 ≤ 40%，超出记 WARN（`AP_CRUD`） |

### 7.2 门 1 · 片级门

| 规则 | 名称 | 判据 |
|---|---|---|
| G1-1 | 证据非空 | 非 `DRAFT` 实例 `evidenceIds >= 1` |
| G1-2 | 原文可定位 | `Evidence.quote` 能在 `DocumentSegment.text` 中定位，或 `charStart/charEnd` 落在段落内 |
| G1-3 | 类型已注册 | 抽取类型属于内核 29 类型或已启用扩展包 |
| G1-4 | 置信度与来源组合合法 | `HUMAN`=1.0；`RULE`≤0.9；`LLM` 须有模型标识与提示词版本 |
| G1-5 | 版本链无环 | `SUPERSEDES` / `AMENDS` / `EXTENDS` 不得成环 |

### 7.3 门 2 · 语义门（Stage 0 最重要）

| 规则 | 名称 | 判据 |
|---|---|---|
| G2-1 | 双时间完整 | `validFrom`/`recordedAt` 非空；`validTo >= validFrom`；`supersededAt >= recordedAt` |
| G2-2 | Scope 必填 | `scopeRequired` 类型必须 `scopeId` 非空且可解析 |
| G2-3 | 口径可比 | 跨序列运算两侧 `dimension`/`priceBasis`/`taxBasis`/`currencyCode` 全同 |
| G2-4 | 桥不越权 | 写入角色与目标必须命中桥契约 `allowedWrites`，命中 `forbidden` 即 FAIL |
| G2-5 | 关系实体化判别式 | 参数 ≥2 / 需有效期 / 需独立证据，满足任一须建关系实体 |
| G2-6 | 断言不写事实 | `Assertion` 及其下游不得修改内核对象属性值 |
| G2-7 | 边端点类型合法 | 边 FROM/TO 类型命中 `fromTypes`/`toTypes` 白名单 |
| G2-8 | 条款条件可解析 | `leftOperand` 为已定义属性路径或已注册 `indicatorCode`；规则须 `CONFIRMED` |
| G2-9 | 退场可解释 | `RETIRED` 须有版本入边或已过期，四选一成立 |
| G2-10 | 有效期重叠须解释 | 同口径多条记录有效期不得无故重叠，重叠须由 `AMENDS`/`SUPERSEDES` 解释 |
| G2-11 | Scope 维度合法性 | `members[].objectType` 与 `Dimension.valueObjectType` 兼容；`multiValued=false` 至多 1 个成员 |
| G2-12 | 断言去重契约完整 | 断言桥 `deduplication` 的键、阈值 0.85、`neverMerge`、`onDuplicate` 均非空，且登记动作后置断言引用去重键（`AP_DUP_ASSERT`） |

### 7.4 门 3 · 全量门

| 规则 | 名称 | 判据 |
|---|---|---|
| G3-1 | 引用完整性 | 全图无悬挂引用；`MERGED` 无 `ACTIVE` 残留；无孤立 `Evidence` |
| G3-2 | 幂等重跑 | 同输入重复执行输出规范化 hash 一致 |
| G3-3 | 预算未超 | 各类计数落在 `budget` 的 lower 与 hardUpper 之间 |
| G3-4 | 能力问题路径可达 | `requiredTypes`/`requiredEdges` 均存在，且路径 ≤ 该问题 `maxHops`。上限按 `capability/questions.json` 的 `hopPolicy` 分档：Stage 0 问题超 `stage0Limit` 判 FAIL；Stage 2 问题超 `stage2Limit` 记 WARN，介于两档之间记 PASS 并注明使用管理员开关上限 |
| G3-5 | 业务编码全局唯一 | `commodityCode`/`factorCode` 全局唯一；子域引用指向 `PUB`；`factorCategory` 七类固定 |
| G3-6 | 域码归属唯一 | 每类型 `domainCode` 唯一且与 `id_registry` 一致（§4.2） |
| G3-7 | Scope 跨包一致 | 同实体跨包 `normalizedKey` 一致，不一致以内核为准并告警 |

### 7.5 门 4 · 评分门

评分公式沿用 OBM 口径：

```
score = max(0, min(100, 100 - n_err*12 - n_warn*2))
```

通过线：**≥80 且 0 error**。

门内规则：

| 规则 | 名称 | 判据 |
|---|---|---|
| G4-1 | 评分折算 | 按公式计算；`n_err == 0` 且 `score >= 80` 判通过，否则阻塞发布 |
| G4-2 | 反模式码两级登记合规 | CORE 集合与 §7.6 逐条一致；每码 `tier ∈ {CORE, EXTENSION}`；无重复码；CORE+EXTENSION 合计 11—20 |
| G4-3 | 反模式码接线完整 | 每条 CORE 码的 `detectedBy` 至少指向一条门规则；为空记 WARN。`detectedBy` 指向不存在的规则编号（悬空接线）记 FAIL |

预算检查（Stage 0 基线，非硬上限），实际由 **G3-3** 执行：

| 指标 | 下限 | 建议 | 硬上限 |
|---|---|---|---|
| 内核对象类型 | 25 | 25 | 29 |
| 关系实体化类型 | 4 | 4 | 6 |
| `Dimension` | 10 | 10 | 10（固定） |
| 能力问题 | 24 | 24 | 32 |
| 术语种子 | 100 | 100 | 150 |
| 反模式码（CORE / CORE+EXT） | 11 | 11 / 19 | 20 |

### 7.6 反模式码（11 条）

| 码 | 触发条件 | 处理 |
|---|---|---|
| `AP_SCOPE_LEAK` | Scope 成员 `objectType` 与维度定义不兼容 | error，拒绝入库 |
| `AP_DIM_CONFLICT` | 同一 `dimensionCode` 在两包出现不同 `valueObjectType` | error |
| `AP_ORPHAN_FACTOR` | `Factor` 无任何 `DRIVES` / `ACTS_ON_FACTOR` 边 | warn |
| `AP_ASSERT_NO_EVIDENCE` | `Assertion` 无 `SUPPORTED_BY` 边 | error |
| `AP_DUP_ASSERT` | 同机构同期间同命题重复计数 | warn，自动去重；Stage 0 由 G2-12 校验去重契约完整性 |
| `AP_UNIT_MISMATCH` | 同序列数据点单位不一致 | error |
| `AP_TIME_OVERLAP` | 有效期重叠且无 `AMENDS`/`SUPERSEDES` 解释 | warn |
| `AP_ID_REUSE` | 同一 ID 对应不同语义 | error |
| `AP_POLICY_MAGNITUDE` | 政策包写 `impactMagnitude` | error |
| `AP_CRUD` | 动作/行为命名中 CRUD 词占比 > 40% | warn（沿用 OBM）；Stage 0 由 G0-8 判定 |
| `AP_COVERAGE` | 某能力问题不存在合规可答路径 | error |

> 上表 11 条为 **CORE**，是 Stage 0 的交付口径（§10 第 8 项按 11 条计数）。另有 8 条 **EXTENSION** 码（`AP_BRIDGE_ILLEGAL_WRITE`、`AP_ACTIVATION_ASSUMED`、`AP_ASSERTION_AS_FACT`、`AP_ORPHAN_EVIDENCE`、`AP_DOUBLE_TIME_MISSING`、`AP_CATCHALL_ENUM`、`AP_RELATION_PARAM_LEAK`、`AP_FCN_RULE_MIXED`）已接线到 G0—G2 的门规则，两级合计 19 条，不超过硬上限 20。级别与接线关系以 `gates/rules.json` 的 `antiPatterns.codes[].tier` 与 `detectedBy` 为唯一真相。

### 7.7 编号映射（旧稿 → 现行）

早期草稿的规则编号与 `gates/rules.json` 语义不同（同一编号指向不同规则）。现行编号以 `gates/rules.json` 为唯一真相，旧稿到现行的对应关系如下，供查旧文档时对照。旧稿中 9 条独有检查已按**追加编号**并入 JSON（G0-7、G0-8、G1-5、G2-10、G2-11、G2-12、G3-5、G3-6、G3-7），不重编号已有规则，以免破坏 `capability/questions.json` 的 `gate` 字段与反模式码 `detectedBy` 的引用。

| 旧稿编号 | 旧稿含义 | 现行编号 |
|---|---|---|
| G0-1 | 主键唯一 + 公共字段完整 | G0-2 + G0-3 |
| G0-2 | 枚举合法 + 无兜底枚举 | G0-5 + G0-6 |
| G0-3 | 字段值类型匹配 | G0-7 |
| G0-4 | 业务编码唯一 | G3-5 |
| G0-5 / G0-6 | 双时间字段完整 | G2-1 |
| G1-1 | 引用可解析 | G0-4 |
| G1-2 | 边端点类型合法 | G2-7 |
| G1-3 | 引用完整性 | G3-1 |
| G1-4 | 版本链无环 | G1-5 |
| G2-1 | 口径可比 | G2-3 |
| G2-2 | 有效期重叠须解释 | G2-10 |
| G2-3 | 证据非空 | G1-1 |
| G2-4 | 原文可定位 | G1-2 |
| G2-5 | Scope 维度合法性 | G2-11 |
| G2-6 | 能力问题路径可达 | G3-4 |
| G2-7 | 桥不越权 | G2-4 |
| G3-1 | Scope 跨包一致 | G3-7 |
| G3-2 + G3-3 | 业务编码全局唯一（品种码 / 因子码） | G3-5 |
| G3-4 | 域码归属唯一 | G3-6 |

> 校验器 `tools/validate_stage0.py` 输出的规则 id 已按本表对齐；新增检查沿用同一门内的 `a/b/c` 后缀（如 `G3-3a` 内核类型计数、`G3-3b` 关系实体计数）。

---

## 8. 接口规范

### 8.1 `asOf` 语义（唯一实现）

| 参数 | 行为 | 缓存 |
|---|---|---|
| 缺省 / `asOf=null` | 不做时间过滤，返回当前全部有效+失效记录 | **允许缓存** |
| `asOf=<ISO8601>` | 双时间过滤：`validFrom <= asOf <= coalesce(validTo, ∞)` **且** `recordedAt <= asOf` **且** `(supersededAt is null or supersededAt > asOf)` | **禁止缓存**（`Cache-Control: no-store`） |
| `asOf` 格式非法 | 400 | — |

**为什么默认 `null` 而不是 `now`**：默认 `now` 会让每次请求时间戳不同，导致缓存键无限膨胀、全量失效。`now` 语义由调用方显式传入。

### 8.2 图查询与 SQL 的分工

| 查询类型 | 走哪 | 说明 |
|---|---|---|
| 对象 + 邻接（1—2 跳） | Kùzu | 典型：某品种的形态、某政策的适用范围 |
| 变长路径（3—5 跳） | Kùzu `[*1..5]` | 传导链、事件链、替代链 |
| 集合比较（Scope 交并差） | Kùzu + 应用层 | `normalizedKey` 集合运算在应用层 |
| 时序聚合（同比/环比/均值） | SQLite | `DataPoint` 聚合不进 Kùzu |
| 全文/向量检索 | 现有 RAG 层 | Kùzu 只存 `embeddingRef` 指针 |
| 审核流 / 抽取运行 / 本体版本 | SQLite | 元数据与审计 |

**路径上限**：以 `capability/questions.json` 的 `hopPolicy` 为唯一真相——`stage0Limit=5`（Stage 0 交付按此验收，超出判 FAIL）、`stage2Limit=6`（Stage 2 问题仅在管理员开启后生效，超出 6 才记 WARN）。默认 `maxHops=3`，请求可显式指定至在上限内；超过上限返回 400 并附建议。

### 8.3 结果契约

所有图查询结果必须携带：

```json
{
  "items": [ ... ],
  "evidenceIds": ["OBJ-ED-..."],
  "confidence": 0.86,
  "asOf": null,
  "truncated": false
}
```

无 `evidenceIds` 的结果只能出现在 `DRAFT` 视图，不能进入正式问答。

---

## 9. Kùzu 落地要点

### 9.1 Node / Rel 表清单

- **Node Table**：29 张（25 内核 + 4 关系实体化）。主键统一 `id STRING`。
- **Rel Table**：按边语义建，预计 34 条（沿用现有 `PREDICATES` + 新增桥边）。

核心边：

| 边 | FROM → TO | 带属性 |
|---|---|---|
| `BELONGS_TO_CATEGORY` | Commodity → CommodityCategory | 否 |
| `HAS_FORM` | Commodity → CommodityForm | 否 |
| `DERIVED_FROM` | CommodityForm → CommodityForm | 否 |
| `LOCATED_IN` | MarketPlace/LogisticsNode → Region | 否 |
| `GOVERNS_SCOPE` | Event → Scope | 否 |
| `IMPACTS` | Event → Commodity/Factor | 是（`impactDirection`, `lagDays`） |
| `ACTS_ON_FACTOR` | Event → Factor | 否 |
| `DRIVES` | Factor → IndicatorSeries | 是（`elasticity`, `lagDays`） |
| `MEASURED_BY` | Factor → Indicator | 否 |
| `HAS_SERIES` | Indicator → IndicatorSeries | 否 |
| `HAS_POINT` | IndicatorSeries → DataPoint | 否 |
| `FOR_PERIOD` | DataPoint → TimePeriod | 否 |
| `SCOPED_BY` | IndicatorSeries/Assertion/Event → Scope | 否 |
| `SUPPORTED_BY` | Assertion → Evidence | 是（`strength`） |
| `EVIDENCED_BY` | 任意对象 → Evidence | 否 |
| `FROM_DOC` | Evidence → Document | 否 |
| `FROM_SEGMENT` | Evidence → DocumentSegment | 否 |
| `SUPERSEDES` | Event → Event | 是（`supersededAt`, `evidenceIds`） |
| `AMENDS` | Event → Event | 是 |
| `EXTENDS` | Event → Event | 是（`newEffectiveTo`） |
| `ASSERTED_BY` | Assertion → Organization/Person | 否 |
| `PRICE_TO` | IndicatorSeries → IndicatorSeries | 是（`elasticity`, `lagDays`, `transmissionType`） |

### 9.2 双时间落地

- `validFrom` / `validTo` / `recordedAt` / `supersededAt` 作为 Node 表普通列。
- 对 `(validFrom)`、`(validTo)`、`(recordedAt)` 各建索引；`asOf` 查询用范围扫描。
- `supersededAt` 只作派生缓存，实际过滤优先用 `SUPERSEDES/AMENDS/EXTENDS` 边。

### 9.3 增量与幂等

- SQLite 为 **source of truth**（元数据、审核、版本、抽取运行记录）。
- Kùzu 为 **物化图视图**，可全量重建。
- 增量同步：按 `id + version` 对比，`MERGE` 语义写入（新增走 `CREATE`，修订走 `SET`，删除走 `status=RETIRED` 而非物理删）。
- 全量重建触发条件：schema 变更 / 校验门规则变更 / 一致性校验失败。

### 9.4 检索与向量

- Kùzu 0.6+ 自带 HNSW 向量索引，但**本方案不把主力向量检索迁入 Kùzu**：现有 RAG 层（hash/openai 向量 + BM25 + RRF）已可用，Kùzu 只存 `embeddingRef`。
- 若后续需要「图约束 + 向量」联合查询，再启用 Kùzu 向量索引，作为阶段 4 的优化项。

### 9.5 服务拆分

| 现状 | 目标 |
|---|---|
| `OntologyService` 单类，SQLite 内自建 4 表 | 拆为 `SqliteStore`（元数据/审核/版本/运行）+ `KuzuGraphStore`（对象/关系/路径） |
| 8 个 `/api/ontology/*` 接口 | 接口路径不变，内部按查询类型路由到两个 store |
| 前端 `data-view="ontology"` | 不变，仅在详情面板增加 `Evidence` 与 `asOf` 两个展示位 |

---

## 10. 交付物清单

| # | 文件（拟定） | 内容 | Stage 0 完成判据 |
|---|---|---|---|
| 1 | `ontology_ly/kernel/object_types.json` | 25 对象类型逐字段定义 | 门 0 全过 |
| 2 | `ontology_ly/kernel/relation_types.json` | 4 关系实体化 + 34 边定义 | 门 1 全过 |
| 3 | `ontology_ly/kernel/enums.json` | 全部受控词表 | 无 `Other`、无空值域 |
| 4 | `ontology_ly/kernel/id_registry.json` | 前缀表 + 域码表 | 与 §4 一致 |
| 5 | `ontology_ly/bridges/policy_event.json` 等 3 份 | 三座桥契约 | 每条允许/禁止可判定 |
| 6 | `ontology_ly/capability/questions.json` | 24 条能力问题 | 每条有可答路径 |
| 7 | `ontology_ly/terms/ly_terms.json` | 100 条术语种子 | 100% 可映射 canonical |
| 8 | `ontology_ly/gates/rules.json` | 五道门规则 + 11 反模式码 + 10 条预算 | 规则可机判 |
| 9 | `ontology_ly/schema/kuzu_ddl.md` | DDL 草案（29 Node + 34 Rel + 政策包 9 Node + 7 Rel） | 与 object/relation 定义一一对应 |
| 10 | `ontology_ly/kernel/functions.json` | 12 个 Function 签名（计算 / 校验两类） | 与 Rule 分离，失败语义为抛错或 NULL |
| 11 | `ontology_ly/kernel/actions.json` | 10 个 Action（受控写回）+ WhatIf 分离 | 每个动作有权限、前后置条件、可回滚标记 |
| 12 | `ontology_ly/terms/skos_mapping.json` | SKOS 映射边界 + OWL 导出触发条件 | 只映射 CommodityCategory / Term；导出只读快照 |
| 13 | `ontology_ly/packs/policy/clause_model.json`<br>`ontology_ly/packs/policy/rules/*.rls` | 条款级 9 类型 + 7 关系 + Datalog 引擎选型（Nemo 主 / Cozo 备）；规则文本落 `.rls` | 三态判定顺序与 `officiallyActivated` 字段化；`.rls` 可被 Nemo 加载并按 11 情形回归通过 |
| 14 | `ontology_ly/STAGE0-执行分解.md` | 本文件 | — |

> 第 10—13 项为《本体模型选型说明》对齐后新增：第 10、11 项落实 Palantir 的 Function 与 Action，第 12 项落实 SKOS/OWL 边界，第 13 项落实条款层规则表示。

**Stage 0 通过 = 上述 13 项全部落盘 + 门 0—门 4 跑通 + 24 条能力问题的路径检查全过。**

---

## 11. 决策记录

### 11.1 已确认（2026-11-13）

| # | 事项 | 决定 | 对后续的约束 |
|---|---|---|---|
| 1 | 价格反验政策阈值 | **阶段 2 小样本反验**（3—5 个品种），不挪到阶段 3 | 政策包 `ImpactLink.strength` 必须在阶段 2 给出值域与来源；桥一在阶段 2 即须可跑通 |
| 2 | 「四种退场」判定规则 | **自然到期单独成类，不建替代边** | 见 §11.2；图上不得出现「新文件发布」造成的假 `SUPERSEDES` |
| 3 | AGROVOC 版本与授权 | **固定版本 dump + CC-BY 4.0** | 只对品类根/一级节点、地理、单位做映射；形态/口径/政策工具一律 `authority=LY` |
| 4 | 推理结论免责边界 | **需要**，作为硬性展示要求 | 三态结论必须同时给出推理链（每步 `clausePath + quote`）与原文引用；`UNABLE` 必须列出缺失事实；界面固定免责声明；无链不出结论 |
| 5 | 一级政策清单确定方式 | **由本方案定规则**，见 §11.3 | 清单在阶段 2 建库时按此规则生成，不做事后人工增删 |

### 11.2 退场判定细则（决定 2 的落地）

四种退场，互斥：

| 类型 | 触发条件 | 图上的动作 |
|---|---|---|
| `SUPERSEDES` 替代 | 新文件正文**明示**「废止 / 停止执行 / 按新办法执行 / 替代」旧文件 | 建 `SUPERSEDES` 边；旧对象 `status=RETIRED`、`supersededAt` 落时间 |
| `AMENDS` 修订 | 新文件只改部分条款，须能定位到被修订条款 | 建 `AMENDS` 边到 `PolicyClause`；旧对象仍 `ACTIVE`，被改条款 `validTo` 落时间 |
| `EXTENDS` 延长 | 仅延长有效期，无实质条款变更 | 建 `EXTENDS` 边；旧对象 `validTo` 顺延 |
| `EXPIRE` 自然到期 | 旧文件到期且新文件正文未提「废止/替代」 | **不建任何替代边**；只把 `validTo` 置为到期日、`status=RETIRED` |

**关键判据**：「新文件发布 + 旧文件到期」≠ 替代。只有原文出现明示废止措辞才建 `SUPERSEDES`。判错的后果是图上画出假替代链，会让政策溯源一路指错——这比漏一条边严重得多。

### 11.3 一级政策清单确定方式（决定 5 的落地）

**三条件同时满足**才进一级清单：

1. 发文机关为中央部委及以上（国务院、国家发改委、国家粮食和物资储备局、财政部、农业农村部、海关总署）；
2. 设定全国统一规则或全国性参数（非地方试点、非单一企业批复）；
3. 含可量化条款（价格、数量、比例、期限、关税配额至少一项）。

不满足任一条 → 归二级：只做文档入库 + 条款抽取 + 原文检索，**不编译规则（Nemo `.rls`）**。

- 来源：上述部委的年度公告目录 + 国家粮食交易中心公告栏。
- 维护：清单版本化落在 `packs/policy/policy_inventory.md`（阶段 2 建立），变更走内核评审，运行时不支持增删。
- 一句话边界：一级管「算得出结论」，二级管「查得到原文」。

### 11.4 仍待确认

| # | 事项 | 暂定处理 |
|---|---|---|
| 1 | `TimePeriod` 的粮食年度起止（按品类不同） | 按建议先建 3 个 scheme（CALENDAR / GRAIN-CN / MARKETING-USDA），小麦 6/1—5/31、稻谷分省，用 `CalendarScheme.appliesToCategoryId` 承载；具体省界表在阶段 2 补 |

---

*本文件为 Stage 0 唯一基线。后续阶段 1—4 的设计文档均以本文件的 §2 通用字段、§4 ID 规范、§5 桥契约、§7 校验门为不可改前提。*
