"""域抽取器字段映射测试（RED → GREEN）。

验证抽取器产出的结构化行字段与表结构对齐，不依赖网络。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.db import Database
from app.extractors import (
    EXTRACTORS, IntelExtractor, MarketIndexExtractor, PolicyExtractor,
    RelationDeriver, YearbookExtractor, get_extractor,
)


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/test.db", Path("seed"))
    database.initialize()
    database.seed()
    return database


POLICY_LIST_HTML = """
<html><body>
<ul>
  <li><a href="/zhengce/content/202611/content_7085661.htm">关于做好2026年秋粮收购工作的通知</a></li>
  <li><a href="/zhengce/content/202611/content_7085662.htm">某无关政策标题</a></li>
  <li><a href="/zhengce/content/202611/content_7085663.htm">2026年小麦最低收购价格公布</a></li>
</ul>
</body></html>
"""

POLICY_DETAIL_HTML = """
<html><head><title>关于做好2026年秋粮收购工作的通知</title></head>
<body><div class="pages_content">
<p>农业农村部 国家粮食和物资储备局</p>
<p>农发〔2026〕12号</p>
<p>2026年11月10日发布</p>
<p>为切实做好2026年秋粮收购工作，保护种粮农民利益...</p>
</div></body></html>
"""


def test_policy_extractor_filters_keywords_and_extracts_fields():
    extractor = PolicyExtractor()
    source = {"crawlSourceId": "CRS9001", "sourceName": "国务院",
              "knowledgeCategoryId": "KTC0146", "sourceUrl": "https://www.gov.cn/zhengce/zuixin/"}
    api_json = json.dumps([
        {"TITLE": "关于做好2026年秋粮收购工作的通知",
         "URL": "https://www.gov.cn/zhengce/content/202611/content_7085661.htm",
         "DOCRELPUBTIME": "2026-11-10"},
        {"TITLE": "某无关政策标题", "URL": "https://www.gov.cn/x/1.htm", "DOCRELPUBTIME": "2026-11-09"},
        {"TITLE": "2026年小麦最低收购价格公布",
         "URL": "https://www.gov.cn/zhengce/content/202611/content_7085663.htm",
         "DOCRELPUBTIME": "2026-11-08"},
    ], ensure_ascii=False)

    def fake_fetch(url):
        if url.endswith("ZUIXINZHENGCE.json"):
            return 200, api_json
        return 200, POLICY_DETAIL_HTML

    result = extractor.extract(source, POLICY_LIST_HTML, "SOU9999", "tester", fake_fetch)
    # 第二条不含粮油关键词，应被过滤
    tables = [t for t, _ in result.records]
    assert tables.count("Policy") == 2
    assert len(result.sub_documents) == 2
    policy = dict(result.records[0][1])
    assert policy["policyTitle"] == "关于做好2026年秋粮收购工作的通知"
    assert policy["docNumber"] == "农发〔2026〕12号"
    assert policy["issuer"] == "农业农村部"
    assert policy["effectiveDate"] == "2026-11-10"
    assert policy["reviewStatus"] == "已发布"
    assert policy["regionId"] == "REG0001"
    assert policy["knowledgeCategoryId"] == "KTC0146"


MARKET_INDEX_HTML = """
<html><body>
<table>
<tr><td>日期</td><td>2026年11月14日</td></tr>
<tr><td>粮油产品批发价格指数</td><td>125.36</td></tr>
<tr><td>粮食价格指数</td><td>128.45</td></tr>
<tr><td>食用油价格指数</td><td>118.92</td></tr>
</table>
</body></html>
"""


def test_market_index_extractor_produces_indicator_and_ivddata():
    extractor = MarketIndexExtractor()
    source = {"crawlSourceId": "CRS9002", "sourceName": "农业农村部",
              "knowledgeCategoryId": "KTC0034",
              "sourceUrl": "https://data.moa.gov.cn/nyb/pc/200zs.jsp"}
    api_json = (
        '[["2026年11月14日","2026年11月14日","2026年11月14日"],'
        '["粮油产品批发价格指数","粮食价格指数","食用油价格指数"],'
        '["125.36","128.45","118.92"]]'
    )

    def fake_fetch(url):
        if url.endswith("/200zs"):
            return 200, api_json
        return 200, ""

    result = extractor.extract(source, MARKET_INDEX_HTML, "SOU9999", "tester", fake_fetch)
    tables = [t for t, _ in result.records]
    assert tables.count("Indicator") == 3
    assert tables.count("IvdData") == 3
    indicators = [dict(v) for t, v in result.records if t == "Indicator"]
    assert any(i["indicatorCode"] == "MKT-GRAIN-OIL-INDEX" for i in indicators)
    ivd = [dict(v) for t, v in result.records if t == "IvdData"]
    assert ivd[0]["period"] == "2026-11-14"
    assert ivd[0]["value"] == 125.36
    assert ivd[0]["commodityId"] == "CMD0005"
    assert ivd[0]["status"] == "已发布"


def test_yearbook_extractor_parses_table_rows():
    extractor = YearbookExtractor()
    source = {"crawlSourceId": "CRS9003", "sourceName": "国家统计局",
              "knowledgeCategoryId": "KTC0201"}
    html = """
    <table>
    <tr><th>指标</th><th>年份</th><th>数值</th></tr>
    <tr><td>粮食总产量</td><td>2024</td><td>70650</td></tr>
    <tr><td>小麦产量</td><td>2024</td><td>13800</td></tr>
    </table>
    """
    result = extractor.extract(source, html, "SOU9999", "tester", lambda u: (200, ""))
    tables = [t for t, _ in result.records]
    assert tables.count("YrbYearbook") == 2
    yrb = dict(result.records[0][1])
    assert yrb["statYear"] == 2024
    assert yrb["value"] == 70650.0
    assert yrb["status"] == "已发布"


def test_intel_extractor_filters_and_extracts():
    extractor = IntelExtractor()
    source = {"crawlSourceId": "CRS9004", "sourceName": "农业农村部",
              "knowledgeCategoryId": "KTC0246", "sourceUrl": "https://www.moa.gov.cn/"}
    list_html = """
    <ul>
      <li><a href="/xw/1.htm">2026年11月粮油市场供需形势分析</a></li>
      <li><a href="/xw/2.htm">某无关新闻</a></li>
    </ul>
    """
    detail_html = """
    <html><head><title>2026年11月粮油市场供需形势分析</title></head>
    <body><p>2026年11月15日 来源：农业农村部</p><p>本月粮油市场供应充足...</p></body>
    </html>
    """
    result = extractor.extract(source, list_html, "SOU9999", "tester", lambda u: (200, detail_html))
    tables = [t for t, _ in result.records]
    assert tables.count("Intel") == 1
    intel = dict(result.records[0][1])
    assert intel["intelTitle"] == "2026年11月粮油市场供需形势分析"
    assert intel["status"] == "已发布"
    assert intel["publishDate"] == "2026-11-15"


def test_relation_deriver_uses_existing_entities(db):
    extractor = RelationDeriver()
    # 插入一条带 commodityId + regionId 的 IvdData
    with db.engine.begin() as conn:
        db.insert("IvdData", {
            "valueId": "IVD9999", "indicatorId": "IND0001",
            "regionId": "REG0001", "commodityId": "CMD0001",
            "periodType": "年度", "period": "2024", "value": 1.0,
            "qualityFlag": "官方核验", "sourceDocId": "SOU0001",
            "segmentId": "SOU0001-S001", "knowledgeCategoryId": "KTC0010",
            "industryStage": "生产", "status": "已发布",
        }, conn, actor="test")
    source = {"crawlSourceId": "CRS9005", "sourceName": "关系派生"}
    result = extractor.extract(source, "", "SOU9999", "tester", lambda u: (200, ""), db)
    tables = [t for t, _ in result.records]
    assert tables.count("Relation") >= 1
    rel = dict(result.records[0][1])
    assert rel["subjectEntityType"] == "品种"
    assert rel["predicate"] == "主产于"
    assert rel["objectEntityType"] == "地区"
    assert rel["confidence"] == 1.0


def test_get_extractor_registry():
    assert set(EXTRACTORS.keys()) == {
        "policy_gov", "market_index_moa", "yearbook_stats", "intel_agri", "relation_derive",
    }
    assert get_extractor("market_index_moa") is not None
    assert get_extractor("nonexistent") is None
    assert get_extractor("") is None
    assert get_extractor(None) is None
