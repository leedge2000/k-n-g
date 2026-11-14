# 粮油经济本体 · Kùzu 落地要点

> 定位：把 `kernel/object_types.json`（25 对象 + 4 关系实体）与 `relation_types.json`（34 语义边）翻成物理 DDL。
> 政策包另加 9 张 Node 表与 7 张 Rel 表，见 §4。
> 原则：**SQLite 是权威记录，Kùzu 是图投影**。写一律先落 SQLite，再单向同步到 Kùzu；Kùzu 只读，不接受业务写。

---

## 1. 写入方向与一致性

```
业务写 → SQLite（权威：属性、审核状态、版本、证据）
              │
              └─ 同步器（按 ChangeLog 增量） → Kùzu（图投影：节点 id + 高频过滤列 + 边）
```

| 规则 | 说明 |
|---|---|
| 单向 | 只允许 SQLite → Kùzu。禁止从 Kùzu 反推属性。 |
| 幂等 | 同步器对每个节点执行 `MERGE`，按 `id` 覆盖；重复执行结果一致（门 G3-2）。 |
| 可重建 | 删除 `ly.kuzu` 后可从 SQLite 全量重建，重建前后节点/边计数必须一致。 |
| 不同步大文本 | `Document.fullText`、`DocumentSegment.segmentText` 不进 Kùzu，只留 `id` 与 `checksum`。全文检索仍由现有 RAG 层负责。 |
| 不同步 DataPoint.value | `DataPoint` 量大，Kùzu 只存 `id/seriesId/periodId/qualityFlag`，数值查询回 SQLite。 |

---

## 2. Node 表（29）

```sql
-- 品类域
CREATE NODE TABLE CommodityCategory(
  id STRING, name STRING, code STRING, parentCategoryId STRING,
  categoryType STRING, level INT64, path STRING, agrovocUri STRING,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING,
  PRIMARY KEY(id));

CREATE NODE TABLE Commodity(
  id STRING, name STRING, categoryId STRING, commodityCode STRING,
  stapleType STRING, customsCode STRING, isBt BOOLEAN,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING,
  PRIMARY KEY(id));

CREATE NODE TABLE CommodityForm(
  id STRING, name STRING, commodityId STRING, formType STRING,
  derivedFromFormId STRING, standardCode STRING, grade STRING,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING,
  PRIMARY KEY(id));

-- 地理域
CREATE NODE TABLE Region(
  id STRING, name STRING, regionCode STRING, regionLevel STRING,
  parentRegionId STRING, isoCode STRING, isProductionArea BOOLEAN,
  isConsumptionArea BOOLEAN, domainCode STRING, validFrom DATE, validTo DATE,
  recordedAt TIMESTAMP, supersededAt TIMESTAMP, status STRING, version INT64,
  confidence DOUBLE, extractedBy STRING, authority STRING, mergedIntoId STRING,
  PRIMARY KEY(id));

CREATE NODE TABLE MarketPlace(
  id STRING, name STRING, marketType STRING, regionId STRING,
  operatorOrgId STRING, exchangeCode STRING, domainCode STRING,
  validFrom DATE, validTo DATE, recordedAt TIMESTAMP, supersededAt TIMESTAMP,
  status STRING, version INT64, confidence DOUBLE, extractedBy STRING,
  authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE LogisticsNode(
  id STRING, name STRING, nodeType STRING, regionId STRING,
  capacity DOUBLE, capacityUnitId STRING, geo STRING, domainCode STRING,
  validFrom DATE, validTo DATE, recordedAt TIMESTAMP, supersededAt TIMESTAMP,
  status STRING, version INT64, confidence DOUBLE, extractedBy STRING,
  authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

-- 主体域
CREATE NODE TABLE Organization(
  id STRING, name STRING, orgType STRING, parentOrgId STRING,
  regionId STRING, domainCode STRING, validFrom DATE, validTo DATE,
  recordedAt TIMESTAMP, supersededAt TIMESTAMP, status STRING, version INT64,
  confidence DOUBLE, extractedBy STRING, authority STRING, mergedIntoId STRING,
  PRIMARY KEY(id));

CREATE NODE TABLE Enterprise(
  id STRING, name STRING, enterpriseType STRING, regionId STRING,
  creditCode STRING, isListed BOOLEAN, capacity DOUBLE, capacityUnitId STRING,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE Person(
  id STRING, name STRING, personRole STRING, affiliationOrgId STRING,
  title STRING, domainCode STRING, validFrom DATE, validTo DATE,
  recordedAt TIMESTAMP, supersededAt TIMESTAMP, status STRING, version INT64,
  confidence DOUBLE, extractedBy STRING, authority STRING, mergedIntoId STRING,
  PRIMARY KEY(id));

-- 时间域
CREATE NODE TABLE TimePeriod(
  id STRING, name STRING, periodType STRING, startDate DATE, endDate DATE,
  calendarSchemeId STRING, label STRING, domainCode STRING, validFrom DATE,
  validTo DATE, recordedAt TIMESTAMP, supersededAt TIMESTAMP, status STRING,
  version INT64, confidence DOUBLE, extractedBy STRING, authority STRING,
  mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE CalendarScheme(
  id STRING, name STRING, schemeType STRING, startMonth INT64,
  startDay INT64, appliesToCategoryId STRING, regionId STRING,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

-- 指标域
CREATE NODE TABLE UnitOfMeasure(
  id STRING, name STRING, unitCode STRING, dimension STRING,
  baseUnitCode STRING, factor DOUBLE, precision INT64, currencyCode STRING,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE Indicator(
  id STRING, name STRING, indicatorCode STRING, indicatorType STRING,
  unitId STRING, defaultFrequency STRING, statisticalSourceId STRING,
  isDerived BOOLEAN, calculationFormula STRING, direction STRING,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE IndicatorSeries(
  id STRING, name STRING, indicatorId STRING, commodityFormId STRING,
  scopeId STRING, frequency STRING, statisticalSourceId STRING,
  externalSeriesCode STRING, priceBasis STRING, taxBasis STRING,
  currencyCode STRING, startPeriodId STRING, endPeriodId STRING,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE DataPoint(
  id STRING, seriesId STRING, periodId STRING, unitId STRING,
  publishedAt TIMESTAMP, revisionNo INT64, qualityFlag STRING,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE StatisticalSource(
  id STRING, name STRING, sourceType STRING, orgId STRING,
  releasePattern STRING, reliabilityScore DOUBLE, revisionPolicy STRING,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

-- 事件 / 文档 / 证据
CREATE NODE TABLE Event(
  id STRING, name STRING, eventTypeId STRING, eventTime TIMESTAMP,
  eventScale STRING, severity STRING, parentEventId STRING, scopeId STRING,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE EventType(
  id STRING, name STRING, eventCategory STRING, isPolicyTool BOOLEAN,
  parentEventTypeId STRING, domainCode STRING, validFrom DATE, validTo DATE,
  recordedAt TIMESTAMP, supersededAt TIMESTAMP, status STRING, version INT64,
  confidence DOUBLE, extractedBy STRING, authority STRING, mergedIntoId STRING,
  PRIMARY KEY(id));

CREATE NODE TABLE Document(
  id STRING, name STRING, docType STRING, issuingOrgId STRING,
  docNo STRING, publishDate DATE, checksum STRING, authorityLevel STRING,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE DocumentSegment(
  id STRING, documentId STRING, segmentIndex INT64, checksum STRING,
  segmentType STRING, domainCode STRING, validFrom DATE, validTo DATE,
  recordedAt TIMESTAMP, supersededAt TIMESTAMP, status STRING, version INT64,
  confidence DOUBLE, extractedBy STRING, authority STRING, mergedIntoId STRING,
  PRIMARY KEY(id));

CREATE NODE TABLE Evidence(
  id STRING, name STRING, targetType STRING, targetId STRING,
  segmentId STRING, quote STRING, quoteHash STRING, charStart INT64,
  charEnd INT64, evidenceType STRING, strength DOUBLE, assertedById STRING,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

-- 维度 / 口径 / 因子 / 断言
-- Dimension 与 Scope 的 DDL 由 ontology_ly/tools/build_kuzu.py 从 kernel 派生并自动应用，
-- 本文件仅同步展示；内核新增字段而工具未映射时构建会直接失败，避免文档与代码漂移。
CREATE NODE TABLE Dimension(
  id STRING, label STRING, dimensionCode STRING, valueObjectType STRING[],
  multiValued BOOLEAN, hierarchical BOOLEAN, inlineEnumRef STRING, inlineAttribute STRING,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, mergedIntoId STRING,
  updatedAt TIMESTAMP, PRIMARY KEY(id));

CREATE NODE TABLE Scope(
  id STRING, label STRING, scopeCode STRING, scopeType STRING,
  membersJson STRING, isOpen BOOLEAN, normalizedKey STRING,
  supersetOfIds STRING[], memberCount INT64,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, mergedIntoId STRING,
  updatedAt TIMESTAMP, PRIMARY KEY(id));

CREATE NODE TABLE Factor(
  id STRING, name STRING, factorCategory STRING, direction STRING,
  mechanism STRING, scopeId STRING, proposerPack STRING,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE Assertion(
  id STRING, name STRING, subjectType STRING, subjectId STRING,
  predicate STRING, objectValue STRING, horizon STRING, stanceStrength STRING,
  targetPeriodId STRING, scopeId STRING, assertedByType STRING,
  assertedById STRING, restatesToId STRING, probabilityStated DOUBLE,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

-- 关系实体化（4）
CREATE NODE TABLE TransformationProcess(
  id STRING, name STRING, processType STRING, inputFormIds STRING[],
  outputFormIds STRING[], yieldRatesJson STRING, capacity DOUBLE,
  capacityUnitId STRING, regionId STRING, domainCode STRING, validFrom DATE,
  validTo DATE, recordedAt TIMESTAMP, supersededAt TIMESTAMP, status STRING,
  version INT64, confidence DOUBLE, extractedBy STRING, authority STRING,
  mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE SubstitutionRelation(
  id STRING, name STRING, fromFormId STRING, toFormId STRING,
  direction STRING, substitutionRatio DOUBLE, triggerCondition STRING,
  scopeId STRING, domainCode STRING, validFrom DATE, validTo DATE,
  recordedAt TIMESTAMP, supersededAt TIMESTAMP, status STRING, version INT64,
  confidence DOUBLE, extractedBy STRING, authority STRING, mergedIntoId STRING,
  PRIMARY KEY(id));

CREATE NODE TABLE PriceTransmissionLink(
  id STRING, name STRING, fromSeriesId STRING, toSeriesId STRING,
  transmissionType STRING, elasticity DOUBLE, lagDays INT64, regimeNote STRING,
  samplePeriodId STRING, scopeId STRING, domainCode STRING, validFrom DATE,
  validTo DATE, recordedAt TIMESTAMP, supersededAt TIMESTAMP, status STRING,
  version INT64, confidence DOUBLE, extractedBy STRING, authority STRING,
  mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE ImpactLink(
  id STRING, name STRING, sourceType STRING, sourceId STRING,
  targetType STRING, targetId STRING, impactDirection STRING,
  impactMagnitude STRING, lagDays INT64, mechanism STRING,
  assertedByType STRING, assertedById STRING, domainCode STRING,
  validFrom DATE, validTo DATE, recordedAt TIMESTAMP, supersededAt TIMESTAMP,
  status STRING, version INT64, confidence DOUBLE, extractedBy STRING,
  authority STRING, mergedIntoId STRING, PRIMARY KEY(id));
```

---

## 3. Rel 表（34）

Kùzu 允许一张 Rel 表声明多组 FROM/TO，正好对应语义边的多态。

```sql
CREATE REL TABLE HAS_SUBCATEGORY(FROM CommodityCategory TO CommodityCategory);
CREATE REL TABLE BELONGS_TO_CATEGORY(FROM Commodity TO CommodityCategory);
CREATE REL TABLE HAS_FORM(FROM Commodity TO CommodityForm);
CREATE REL TABLE FORM_DERIVED_FROM(FROM CommodityForm TO CommodityForm);
CREATE REL TABLE REGION_PARENT(FROM Region TO Region);
CREATE REL TABLE PLACE_LOCATED_IN(FROM MarketPlace TO Region, FROM LogisticsNode TO Region, FROM Enterprise TO Region, FROM Organization TO Region);
CREATE REL TABLE MARKET_OPERATED_BY(FROM MarketPlace TO Organization);
CREATE REL TABLE ORG_PARENT(FROM Organization TO Organization);
CREATE REL TABLE ORG_IN_REGION(FROM Organization TO Region);
CREATE REL TABLE PERSON_AFFILIATED_WITH(FROM Person TO Organization);
CREATE REL TABLE CALENDAR_APPLIES_TO(FROM CalendarScheme TO CommodityCategory, FROM CalendarScheme TO Region);
CREATE REL TABLE PERIOD_USES_CALENDAR(FROM TimePeriod TO CalendarScheme);
CREATE REL TABLE HAS_SERIES(FROM Indicator TO IndicatorSeries);
CREATE REL TABLE UNIT_OF(FROM Indicator TO UnitOfMeasure);
CREATE REL TABLE SERIES_FOR_FORM(FROM IndicatorSeries TO CommodityForm);
CREATE REL TABLE SERIES_FROM_SOURCE(FROM IndicatorSeries TO StatisticalSource);
CREATE REL TABLE HAS_POINT(FROM IndicatorSeries TO DataPoint);
CREATE REL TABLE POINT_FOR_PERIOD(FROM DataPoint TO TimePeriod);
CREATE REL TABLE SCOPED_BY(FROM IndicatorSeries TO Scope, FROM DataPoint TO Scope, FROM Assertion TO Scope, FROM Factor TO Scope, FROM Event TO Scope, FROM NormativeStatement TO Scope, FROM PriceTransmissionLink TO Scope, FROM SubstitutionRelation TO Scope);
CREATE REL TABLE EVENT_OF_TYPE(FROM Event TO EventType);
CREATE REL TABLE EVENT_INVOLVES_ACTOR(FROM Event TO Organization, FROM Event TO Enterprise, FROM Event TO Person, FROM Event TO Region);
CREATE REL TABLE SUPERSEDES(FROM Document TO Document, FROM PolicyClause TO PolicyClause, FROM IndicatorSeries TO IndicatorSeries, FROM Factor TO Factor, FROM Event TO Event);
CREATE REL TABLE AMENDS(FROM Document TO Document, FROM PolicyClause TO PolicyClause);
CREATE REL TABLE EXTENDS(FROM Document TO Document, FROM PolicyClause TO PolicyClause);
CREATE REL TABLE DOC_ISSUED_BY(FROM Document TO Organization);
CREATE REL TABLE HAS_SEGMENT(FROM Document TO DocumentSegment);
CREATE REL TABLE EVIDENCE_FROM(FROM Evidence TO DocumentSegment);
CREATE REL TABLE ASSERTED_BY(FROM Assertion TO Organization, FROM Assertion TO Person);
CREATE REL TABLE SUPPORTED_BY(FROM Assertion TO Evidence, strength DOUBLE, supportType STRING);
CREATE REL TABLE IMPACTS(FROM Event TO Commodity, FROM Event TO CommodityForm, FROM Event TO IndicatorSeries, FROM Event TO Factor, FROM Event TO Region, FROM ImpactLink TO Commodity);
CREATE REL TABLE ACTS_ON_FACTOR(FROM Event TO Factor, FROM NormativeStatement TO Factor);
CREATE REL TABLE DRIVES(FROM Factor TO IndicatorSeries);
CREATE REL TABLE MEASURED_BY(FROM Factor TO Indicator);
```

> `EVIDENCED_BY` 不建 28 张表：反向关系由 `Evidence(targetType, targetId)` 复合查询承担；节点侧 `evidenceIds` 作为冗余数组列供快速判空（门 G1-1）。

---

## 4. 政策包追加（9 Node + 7 Rel）

```sql
CREATE NODE TABLE PolicyClause(
  id STRING, name STRING, documentId STRING, clausePath STRING, clauseNo STRING,
  clauseLevel STRING, parentClauseId STRING, clauseType STRING, text STRING,
  charStart INT64, charEnd INT64, effectiveFrom DATE, effectiveTo DATE,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE NormativeStatement(
  id STRING, name STRING, clauseId STRING, statementType STRING,
  actorScopeId STRING, actionVerb STRING, objectFormIds STRING[],
  conditionId STRING, exceptionIds STRING[], consequenceId STRING,
  parameterIds STRING[], vaguenessFlag BOOLEAN, vaguenessNote STRING,
  confirmationStatus STRING, confirmedBy STRING, confirmedAt TIMESTAMP,
  domainCode STRING, validFrom DATE, validTo DATE, recordedAt TIMESTAMP,
  supersededAt TIMESTAMP, status STRING, version INT64, confidence DOUBLE,
  extractedBy STRING, authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE ClauseCondition(
  id STRING, name STRING, statementId STRING, parentConditionId STRING,
  operator STRING, leftOperand STRING, comparator STRING, rightValue STRING,
  rightValueUpper STRING, rightUnitId STRING, rightIndicatorId STRING,
  factSource STRING, regionalScopeId STRING, domainCode STRING,
  validFrom DATE, validTo DATE, recordedAt TIMESTAMP, supersededAt TIMESTAMP,
  status STRING, version INT64, confidence DOUBLE, extractedBy STRING,
  authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE ClauseException(
  id STRING, name STRING, statementId STRING, conditionId STRING,
  exceptionType STRING, note STRING, domainCode STRING, validFrom DATE,
  validTo DATE, recordedAt TIMESTAMP, supersededAt TIMESTAMP, status STRING,
  version INT64, confidence DOUBLE, extractedBy STRING, authority STRING,
  mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE ClauseConsequence(
  id STRING, name STRING, statementId STRING, consequenceType STRING,
  triggerEventTypeId STRING, magnitude DOUBLE, magnitudeUnitId STRING,
  description STRING, domainCode STRING, validFrom DATE, validTo DATE,
  recordedAt TIMESTAMP, supersededAt TIMESTAMP, status STRING, version INT64,
  confidence DOUBLE, extractedBy STRING, authority STRING, mergedIntoId STRING,
  PRIMARY KEY(id));

CREATE NODE TABLE RuleParameter(
  id STRING, name STRING, statementId STRING, paramName STRING,
  paramValue DOUBLE, unitId STRING, effectiveFrom DATE, effectiveTo DATE,
  regionScopeId STRING, domainCode STRING, validFrom DATE, validTo DATE,
  recordedAt TIMESTAMP, supersededAt TIMESTAMP, status STRING, version INT64,
  confidence DOUBLE, extractedBy STRING, authority STRING, mergedIntoId STRING,
  PRIMARY KEY(id));

CREATE NODE TABLE ClauseRelation(
  id STRING, name STRING, fromClauseId STRING, toClauseId STRING,
  relationType STRING, priorityBasis STRING, resolved BOOLEAN,
  externalRef STRING, domainCode STRING, validFrom DATE, validTo DATE,
  recordedAt TIMESTAMP, supersededAt TIMESTAMP, status STRING, version INT64,
  confidence DOUBLE, extractedBy STRING, authority STRING, mergedIntoId STRING,
  PRIMARY KEY(id));

CREATE NODE TABLE CompiledRule(
  id STRING, name STRING, statementId STRING, ruleExpression STRING,
  compilerVersion STRING, ontologyVersion STRING, backTranslation STRING,
  backTranslationMatch BOOLEAN, testcaseIds STRING[],
  confirmationStatus STRING, dependsOnClauseIds STRING[], domainCode STRING,
  validFrom DATE, validTo DATE, recordedAt TIMESTAMP, supersededAt TIMESTAMP,
  status STRING, version INT64, confidence DOUBLE, extractedBy STRING,
  authority STRING, mergedIntoId STRING, PRIMARY KEY(id));

CREATE NODE TABLE RuleTestCase(
  id STRING, name STRING, statementId STRING, scenarioName STRING,
  factsJson STRING, expectedOutcome STRING, expectedReason STRING,
  authoredBy STRING, domainCode STRING, validFrom DATE, validTo DATE,
  recordedAt TIMESTAMP, supersededAt TIMESTAMP, status STRING, version INT64,
  confidence DOUBLE, extractedBy STRING, authority STRING, mergedIntoId STRING,
  PRIMARY KEY(id));

CREATE REL TABLE HAS_CLAUSE(FROM Document TO PolicyClause);
CREATE REL TABLE HAS_STATEMENT(FROM PolicyClause TO NormativeStatement);
CREATE REL TABLE HAS_CONDITION(FROM NormativeStatement TO ClauseCondition, FROM ClauseCondition TO ClauseCondition);
CREATE REL TABLE HAS_EXCEPTION(FROM NormativeStatement TO ClauseException);
CREATE REL TABLE HAS_CONSEQUENCE(FROM NormativeStatement TO ClauseConsequence);
CREATE REL TABLE CLAUSE_RELATES(FROM PolicyClause TO PolicyClause);
CREATE REL TABLE ACTIVATES(FROM CompiledRule TO EventType, activationStatus STRING);
```

---

## 5. 索引与查询模式

### 5.1 索引

Kùzu 对主键自动建哈希索引；**通用二级索引支持随版本变化**，因此高频过滤字段一律冗余进主键或用辅助表，不依赖 `CREATE INDEX`。

| 场景 | 做法 |
|---|---|
| Scope 去重 | `Scope.id` 由 `normalizedKey` 派生（`SCP-PUB-` + 前 16 位 sha1 大写），主键即唯一约束 |
| 证据反查目标 | 不建边，查询 `MATCH (e:Evidence) WHERE e.targetType=$t AND e.targetId=$i RETURN e` |
| 双时间过滤 | 每张表冗余 `validFrom/validTo/recordedAt`，`WHERE` 直接下推 |
| 序列取点 | `DataPoint.id = seriesId \|\| '@' \|\| periodId`，主键即组合唯一 |

### 5.2 asOf 查询（业务时间）

```cypher
// 某时点有效的指标序列
MATCH (s:IndicatorSeries)
WHERE s.validFrom <= date($asOf) AND (s.validTo IS NULL OR s.validTo >= date($asOf))
RETURN s.id, s.priceBasis, s.taxBasis;
```

### 5.3 双时间查询（系统时间）

```cypher
// 当时系统认为有效的版本
MATCH (s:IndicatorSeries)
WHERE s.recordedAt <= timestamp($asOf)
  AND (s.supersededAt IS NULL OR s.supersededAt > timestamp($asOf))
RETURN s.id, s.version;
```

### 5.4 路径查询上限

所有路径查询必须写 `*1..N`，`N <= 5`（对应 budget `capabilityMaxHops`），并显式加节点类型约束，禁止裸 `MATCH p=(a)-[*]-(b)`。

---

## 6. 幂等同步

```cypher
MERGE (n:Commodity {id: $id})
SET n.name = $name, n.categoryId = $categoryId, n.commodityCode = $commodityCode,
    n.validFrom = date($validFrom), n.validTo = CASE WHEN $validTo IS NULL THEN NULL ELSE date($validTo) END,
    n.recordedAt = timestamp($recordedAt), n.version = $version, n.status = $status;

MATCH (a:Commodity {id: $fromId}), (b:CommodityCategory {id: $toId})
MERGE (a)-[:BELONGS_TO_CATEGORY]->(b);
```

边没有属性时用 `MERGE` 保证不重复；有属性时（`SUPPORTED_BY`、`IMPACTLink` 相关边）用 `MERGE` + `SET`，并在同步前先删同名旧边，避免属性漂移。

---

## 7. 迁移映射（与现有 SQLite 对齐）

| 现有表 | 目标 Node | 备注 |
|---|---|---|
| `KtcCategory` | `CommodityCategory` | 主键前缀 `KTC` → `OBJ-CM` |
| `CmdCommodity` | `Commodity` | 前缀 `CMD` → `OBJ-CM` |
| `Product` | `CommodityForm` | 前缀 `PRD` → `OBJ-CM` |
| `Region` | `Region` | 前缀 `REG` → `OBJ-RG` |
| `Enterprise` | `Enterprise` | 前缀 `ENT` → `OBJ-EN` |
| `Indicator` | `Indicator` | 前缀 `IND` → `OBJ-IN` |
| `IvdData` / `YrbYearbook` | `DataPoint` | 前缀 `IVD`/`YRB` → `OBJ-DP`；需先补 `IndicatorSeries` 与 `TimePeriod` |
| `Intel` | `Event` + `Assertion` | 前缀 `INT` 停用；按内容二分，事件走 `Event`，观点走 `Assertion` |
| `SourceDoc` | `Document` | 前缀 `SOU` → `OBJ-DC`；`checksum` 用于去重 |
| `SourceDoc_Segment` | `DocumentSegment` | 复用原主键 |
| `Policy` | `Document`(docType=POLICY_DOC) | 前缀 `POL` → `OBJ-DC` |
| `Policy_Clause` | `PolicyClause` | 前缀 `CLA` → `OBJ-CL`；需补 `charStart/charEnd` |
| `Relation` | 无 | 按谓词归并到 34 条语义边，不迁移为主表 |
| `CrsSource` / `CrsSource_CrawlRun` / `QisRecord` / `Concept` / `Concept_Synonym` | 无 | 技术表与治理表，不进本体 |

迁移顺序：`UnitOfMeasure` → `Dimension`/`Scope` → `TimePeriod`/`CalendarScheme` → `CommodityCategory`/`Commodity`/`CommodityForm` → `Region` → `Organization`/`Enterprise`/`Person` → `Document`/`DocumentSegment`/`Evidence` → `Indicator`/`IndicatorSeries`/`DataPoint` → `Event`/`EventType`/`Assertion`/`Factor` → 关系实体 → 政策包。
