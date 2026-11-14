from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EntitySpec:
    entity_type: str
    table: str
    pk: str
    title_field: str | None = None
    status_field: str | None = "status"
    knowledge_type: str | None = None
    id_prefix: str = ""


SPECS = {
    spec.entity_type: spec
    for spec in [
        EntitySpec("来源文档", "SourceDoc", "docId", "title", "status", id_prefix="SOU"),
        EntitySpec("文档段落", "SourceDoc_Segment", "segmentId", "segmentText", None),
        EntitySpec("政策法规", "Policy", "policyId", "policyTitle", "reviewStatus", "政策法规", "POL"),
        EntitySpec("政策条款", "Policy_Clause", "clauseId", "clauseTitle", None),
        EntitySpec("指标定义", "Indicator", "indicatorId", "indicatorName", "status", id_prefix="IND"),
        EntitySpec("指标数据", "IvdData", "valueId", None, "status", "农业经济", "IVD"),
        EntitySpec("年鉴记录", "YrbYearbook", "recordId", None, "status", "行业年鉴", "YRB"),
        EntitySpec("农业情报", "Intel", "intelId", "intelTitle", "status", "全球农业情报", "INT"),
        EntitySpec("品种", "CmdCommodity", "commodityId", "commodityName", "status", id_prefix="CMD"),
        EntitySpec("产品", "Product", "productId", "productName", "status", id_prefix="PRD"),
        EntitySpec("地区", "Region", "regionId", "regionName", "status", id_prefix="REG"),
        EntitySpec("企业", "Enterprise", "enterpriseId", "enterpriseName", "status", id_prefix="ENT"),
        EntitySpec("本体概念", "Concept", "conceptId", "conceptName", "status", id_prefix="CON"),
        EntitySpec("知识关系", "Relation", "relationId", None, "status", id_prefix="REL"),
        EntitySpec("质量治理记录", "QisRecord", "issueId", "issueType", "handleStatus", id_prefix="QIS"),
        EntitySpec("采集源", "CrsSource", "crawlSourceId", "sourceName", "enabledFlag", id_prefix="CRS"),
        EntitySpec("采集执行记录", "CrsSource_CrawlRun", "runId", "runStatus", "runStatus", id_prefix="RUN"),
        EntitySpec("知识分类", "KtcCategory", "categoryId", "categoryName", "status", id_prefix="KTC"),
        EntitySpec("统计源", "StatisticalSource", "sourceId", "sourceName", None, id_prefix="OBJ-PUB-SS"),
        EntitySpec("时间期间", "TimePeriod", "periodId", "label", None, id_prefix="OBJ-PUB-TP"),
        EntitySpec("计量单位", "UnitOfMeasure", "unitId", "unitCode", None, id_prefix="OBJ-PUB-UN"),
        EntitySpec("指标序列", "IndicatorSeries", "seriesId", "seriesId", "status", id_prefix="OBJ-PUB-IN"),
        EntitySpec("价格数据点", "PriceDataPoint", "pointId", "pointId", None, id_prefix="OBJ-PUB-DP"),
        EntitySpec("事件类型", "EventType", "eventTypeId", "typeName", None, id_prefix="OBJ-PUB-EV"),
        EntitySpec("事件", "Event", "eventId", "eventTitle", None, id_prefix="OBJ-PUB-EV"),
        EntitySpec("因子", "Factor", "factorId", "factorName", "status", id_prefix="FAC"),
        EntitySpec("断言", "Assertion", "assertionId", "assertionId", "status", id_prefix="AST"),
        EntitySpec("价格传导", "PriceTransmissionLink", "linkId", "linkId", None, id_prefix="LNK-PT"),
        EntitySpec("形态规格", "CommodityForm", "formId", "formName", "status", id_prefix="OBJ-PUB-CM"),
        EntitySpec("市场场所", "MarketPlace", "placeId", "placeName", "status", id_prefix="OBJ-PUB-MK"),
        EntitySpec("证据", "Evidence", "evidenceId", "evidenceId", None, id_prefix="OBJ-PUB-ED"),
        EntitySpec("政策影响", "ImpactLink", "linkId", "linkId", "status", id_prefix="LNK-IM"),
        EntitySpec("因子驱动", "FactorDriveLink", "factorId", "factorId", "status", id_prefix=""),
        EntitySpec("事件因子关联", "EventFactorLink", "eventId", "eventId", "status", id_prefix=""),
    ]
}
KNOWLEDGE_TYPES = ("政策法规", "指标数据", "年鉴记录", "农业情报", "知识关系")
MASTER_TYPES = ("品种", "产品", "地区", "企业", "本体概念")
MERGEABLE_TYPES = set(MASTER_TYPES)

REFERENCES = {
    "来源文档": [("crawlSourceId", "采集源", "采集来源"), ("knowledgeCategoryId", "知识分类", "所属分类")],
    "政策法规": [("regionId", "地区", "适用地区"), ("sourceDocId", "来源文档", "来源文档"), ("knowledgeCategoryId", "知识分类", "所属分类")],
    "指标数据": [("indicatorId", "指标定义", "对应指标"), ("regionId", "地区", "统计地区"), ("commodityId", "品种", "涉及品种"), ("sourceDocId", "来源文档", "来源文档"), ("knowledgeCategoryId", "知识分类", "所属分类")],
    "年鉴记录": [("indicatorId", "指标定义", "对应指标"), ("regionId", "地区", "统计地区"), ("commodityId", "品种", "涉及品种"), ("sourceDocId", "来源文档", "来源文档"), ("knowledgeCategoryId", "知识分类", "所属分类")],
    "农业情报": [("regionId", "地区", "涉及地区"), ("commodityId", "品种", "涉及品种"), ("sourceDocId", "来源文档", "来源文档"), ("knowledgeCategoryId", "知识分类", "所属分类")],
    "品种": [("parentCommodityId", "品种", "父品种"), ("conceptId", "本体概念", "映射概念")],
    "产品": [("commodityId", "品种", "原料品种"), ("enterpriseId", "企业", "生产企业")],
    "地区": [("parentRegionId", "地区", "上级地区")],
    "企业": [("regionId", "地区", "所在地区")],
    "本体概念": [("parentConceptId", "本体概念", "上位概念")],
    "知识分类": [("parentCategoryId", "知识分类", "上级分类")],
    "知识关系": [("sourceDocId", "来源文档", "来源文档")],
}

MERGE_REFERENCES = {
    "品种": [("IvdData", "commodityId"), ("YrbYearbook", "commodityId"), ("Intel", "commodityId"), ("Product", "commodityId"), ("CmdCommodity", "parentCommodityId")],
    "产品": [],
    "地区": [("Policy", "regionId"), ("IvdData", "regionId"), ("YrbYearbook", "regionId"), ("Intel", "regionId"), ("Enterprise", "regionId"), ("Region", "parentRegionId")],
    "企业": [("Product", "enterpriseId")],
    "本体概念": [("CmdCommodity", "conceptId"), ("Concept", "parentConceptId"), ("Concept_Synonym", "conceptId")],
}

TAG_BY_TABLE = {
    "SourceDoc": "source_doc", "SourceDoc_Segment": "segment", "CrsSource": "crawl_source",
    "CrsSource_CrawlRun": "crawl_run", "Policy": "policy", "Policy_Clause": "policy_clause",
    "Indicator": "indicator", "IvdData": "indval", "YrbYearbook": "yearbook", "Intel": "intel",
    "CmdCommodity": "commodity", "Product": "product", "Region": "region", "Enterprise": "enterprise",
    "Concept": "concept", "Concept_Synonym": "synonym", "Relation": "relation_fact",
    "QisRecord": "quality_record", "KtcCategory": "knowledge_category",
}
