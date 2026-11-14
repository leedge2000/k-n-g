from __future__ import annotations

import calendar
import hashlib
import json
import re
import unicodedata
from datetime import date, timedelta
from typing import Any, Iterable

from sqlalchemy import text

from .db import Database, quote, utc_now
from .scope import ScopeError, ScopeService, normalize_members, normalized_key

MARKET_TABLES = (
    "CommodityForm",
    "MarketPlace",
    "StatisticalSource",
    "TimePeriod",
    "UnitOfMeasure",
    "IndicatorSeries",
    "PriceDataPoint",
    "EventType",
    "Event",
    "Factor",
    "Assertion",
    "Evidence",
    "ImpactLink",
    "FactorDriveLink",
    "EventFactorLink",
    "PriceTransmissionLink",
)

SERIES_SCOPE = "SERIES_SCOPE"
SERIES_STATUS = "有效"
FACTOR_ACTIVE = "ACTIVE"

# FormType 无畜产品枚举，生猪/猪肉示例序列显式豁免形态回填
LEGACY_FORM_EXEMPT = {"OBJ-PUB-IN-0003", "OBJ-PUB-IN-0004"}

EVENT_PENDING = "PENDING"
EVENT_ANNOUNCED = "ANNOUNCED"
EVENT_EFFECTIVE = "EFFECTIVE"
EVENT_EXPIRED = "EXPIRED"
EVENT_STATE_LABELS = {
    EVENT_PENDING: "未公告",
    EVENT_ANNOUNCED: "已公告",
    EVENT_EFFECTIVE: "生效中",
    EVENT_EXPIRED: "已到期",
}

# 每条序列的 CHANNEL 维度成员（MarketPlace.placeId）
SERIES_CHANNEL = {
    "OBJ-PUB-IN-0001": "OBJ-PUB-MK-0001",
    "OBJ-PUB-IN-0002": "OBJ-PUB-MK-0002",
    "OBJ-PUB-IN-0003": "OBJ-PUB-MK-0002",
    "OBJ-PUB-IN-0004": "OBJ-PUB-MK-0003",
    "OBJ-PUB-IN-0005": "OBJ-PUB-MK-0002",
    "OBJ-PUB-IN-0006": "OBJ-PUB-MK-0002",
    "OBJ-PUB-IN-0007": "OBJ-PUB-MK-0003",
    "OBJ-PUB-IN-0008": "OBJ-PUB-MK-0003",
}

BULLISH_PREDICATES = {"WILL_RISE", "BULLISH", "SUPPORTS"}
BEARISH_PREDICATES = {"WILL_FALL", "BEARISH", "WEAKENS"}

SIMILARITY_THRESHOLD = 0.85

SEED_UNIT_T = "OBJ-PUB-UN-0001"
SEED_UNIT_KG = "OBJ-PUB-UN-0002"

SEED_REGIONS = [
    {"regionId": "REG0004", "regionCode": "CN-HA", "regionName": "河南省", "regionLevel": "省级",
     "parentRegionId": "REG0001", "adminCode": "410000", "faoRegion": "Asia", "isChongqing": 0, "status": "有效"},
]

SEED_COMMODITIES = [
    {"commodityId": "CMD0006", "commodityCode": "HOG-LIVE", "commodityName": "生猪", "commodityCategory": "畜产品",
     "description": "现宰活体生猪，作为猪肉零售价格的上游成本参照。", "status": "有效"},
    {"commodityId": "CMD0007", "commodityCode": "PORK", "commodityName": "猪肉", "commodityCategory": "畜产品",
     "description": "猪肉零售环节的商品形态。", "status": "有效"},
]

SEED_PLACES = [
    {"placeId": "OBJ-PUB-MK-0001", "placeName": "河南产地收购市场", "marketType": "SPOT",
     "regionId": "REG0004", "status": "有效"},
    {"placeId": "OBJ-PUB-MK-0002", "placeName": "全国产地收购市场", "marketType": "SPOT",
     "regionId": "REG0001", "status": "有效"},
    {"placeId": "OBJ-PUB-MK-0003", "placeName": "全国农产品批发市场", "marketType": "WHOLESALE",
     "regionId": "REG0001", "status": "有效"},
]

SEED_FORMS = [
    {"formId": "OBJ-PUB-CM-0001", "commodityId": "CMD0001", "formType": "RAW_GRAIN",
     "formName": "稻谷原粮", "grade": "", "status": "有效"},
    {"formId": "OBJ-PUB-CM-0002", "commodityId": "CMD0002", "formType": "RAW_GRAIN",
     "formName": "小麦原粮", "grade": "", "status": "有效"},
    {"formId": "OBJ-PUB-CM-0003", "commodityId": "CMD0003", "formType": "RAW_GRAIN",
     "formName": "玉米原粮", "grade": "", "status": "有效"},
    {"formId": "OBJ-PUB-CM-0004", "commodityId": "CMD0004", "formType": "RAW_GRAIN",
     "formName": "大豆原粮", "grade": "", "status": "有效"},
    {"formId": "OBJ-PUB-CM-0005", "commodityId": "CMD0001", "formType": "PROCESSED_GRAIN",
     "derivedFromFormId": "OBJ-PUB-CM-0001", "formName": "大米", "grade": "", "status": "有效"},
    {"formId": "OBJ-PUB-CM-0006", "commodityId": "CMD0002", "formType": "PROCESSED_GRAIN",
     "derivedFromFormId": "OBJ-PUB-CM-0002", "formName": "小麦粉", "grade": "", "status": "有效"},
    {"formId": "OBJ-PUB-CM-0007", "commodityId": "CMD0004", "formType": "REFINED_OIL",
     "derivedFromFormId": "OBJ-PUB-CM-0004", "formName": "豆油", "grade": "", "status": "有效"},
    {"formId": "OBJ-PUB-CM-0008", "commodityId": "CMD0008", "formType": "REFINED_OIL",
     "formName": "菜籽油", "grade": "", "status": "有效"},
]

SEED_SOURCES = [
    {"sourceId": "OBJ-PUB-SS-0001", "sourceName": "示例·粮油市场信息机构", "sourceType": "OTHER",
     "releasePattern": "月度", "reliabilityScore": 0.75, "revisionPolicy": "PERIODIC",
     "remark": "示例统计源，不代表真实机构发布。"},
    {"sourceId": "OBJ-PUB-SS-0002", "sourceName": "示例·农业农村主管部门", "sourceType": "MARA",
     "releasePattern": "月度", "reliabilityScore": 0.85, "revisionPolicy": "PERIODIC",
     "remark": "示例统计源，不代表真实机构发布。"},
    {"sourceId": "OBJ-PUB-SS-0003", "sourceName": "示例·统计部门", "sourceType": "NBS",
     "releasePattern": "月度", "reliabilityScore": 0.9, "revisionPolicy": "PERIODIC",
     "remark": "示例统计源，不代表真实机构发布。"},
    {"sourceId": "OBJ-PUB-SS-0004", "sourceName": "示例·农产品交易所", "sourceType": "EXCHANGE",
     "releasePattern": "日度", "reliabilityScore": 0.7, "revisionPolicy": "AD_HOC",
     "remark": "示例统计源，不代表真实机构发布。"},
]

SEED_UNITS = [
    {"unitId": SEED_UNIT_T, "unitCode": "CNY/t", "dimension": "PRICE", "baseUnitCode": "CNY/t",
     "factor": 1.0, "precision": 0, "currencyCode": "CNY"},
    {"unitId": SEED_UNIT_KG, "unitCode": "CNY/kg", "dimension": "PRICE", "baseUnitCode": "CNY/kg",
     "factor": 1.0, "precision": 2, "currencyCode": "CNY"},
]

SEED_INDICATORS = [
    {"indicatorId": "IND0009", "indicatorCode": "MKT-RICE-HENAN-FARM", "indicatorName": "稻谷河南收购价",
     "indicatorCategory": "市场价格", "unit": "元/吨", "frequency": "月度", "caliber": "河南稻谷农户出售价",
     "sourceOrg": "示例·粮油市场信息机构", "knowledgeCategoryId": "KTC0012", "industryStage": "收储", "status": "有效"},
    {"indicatorId": "IND0010", "indicatorCode": "MKT-CORN-CN-FARM", "indicatorName": "玉米全国收购价",
     "indicatorCategory": "市场价格", "unit": "元/吨", "frequency": "月度", "caliber": "全国玉米农户出售价",
     "sourceOrg": "示例·粮油市场信息机构", "knowledgeCategoryId": "KTC0012", "industryStage": "收储", "status": "有效"},
    {"indicatorId": "IND0011", "indicatorCode": "MKT-HOG-COST-CN", "indicatorName": "生猪养殖成本",
     "indicatorCategory": "成本效益", "unit": "元/公斤", "frequency": "月度", "caliber": "全国生猪养殖综合成本",
     "sourceOrg": "示例·农业农村主管部门", "knowledgeCategoryId": "KTC0012", "industryStage": "生产", "status": "有效"},
    {"indicatorId": "IND0012", "indicatorCode": "MKT-PORK-RETAIL-CN", "indicatorName": "猪肉零售价",
     "indicatorCategory": "市场价格", "unit": "元/公斤", "frequency": "月度", "caliber": "全国猪肉零售均价",
     "sourceOrg": "示例·统计部门", "knowledgeCategoryId": "KTC0012", "industryStage": "消费", "status": "有效"},
    {"indicatorId": "IND0013", "indicatorCode": "MKT-WHEAT-CN-FARM", "indicatorName": "小麦全国收购价",
     "indicatorCategory": "市场价格", "unit": "元/吨", "frequency": "月度", "caliber": "全国小麦农户出售价",
     "sourceOrg": "示例·粮油市场信息机构", "knowledgeCategoryId": "KTC0012", "industryStage": "收储", "status": "有效"},
    {"indicatorId": "IND0014", "indicatorCode": "MKT-SOYBEAN-CN-FARM", "indicatorName": "大豆全国收购价",
     "indicatorCategory": "市场价格", "unit": "元/吨", "frequency": "月度", "caliber": "全国大豆农户出售价",
     "sourceOrg": "示例·粮油市场信息机构", "knowledgeCategoryId": "KTC0012", "industryStage": "收储", "status": "有效"},
    {"indicatorId": "IND0015", "indicatorCode": "MKT-SOYOIL-CN-WHOLESALE", "indicatorName": "豆油全国批发价",
     "indicatorCategory": "市场价格", "unit": "元/吨", "frequency": "月度", "caliber": "全国豆油批发均价",
     "sourceOrg": "示例·农产品交易所", "knowledgeCategoryId": "KTC0012", "industryStage": "加工", "status": "有效"},
    {"indicatorId": "IND0016", "indicatorCode": "MKT-RAPEOIL-CN-WHOLESALE", "indicatorName": "菜籽油全国批发价",
     "indicatorCategory": "市场价格", "unit": "元/吨", "frequency": "月度", "caliber": "全国菜籽油批发均价",
     "sourceOrg": "示例·农产品交易所", "knowledgeCategoryId": "KTC0012", "industryStage": "加工", "status": "有效"},
]

SEED_SERIES = [
    {"seriesId": "OBJ-PUB-IN-0001", "indicatorId": "IND0009", "commodityFormId": "OBJ-PUB-CM-0001",
     "scopeCode": "SER-RICE-HENAN-EXFARM", "scopeLabel": "河南稻谷收购价口径",
     "regionId": "REG0004", "frequency": "MONTH", "statisticalSourceId": "OBJ-PUB-SS-0001",
     "priceBasis": "EX_FARM", "taxBasis": "INCLUSIVE", "currencyCode": "CNY", "unitId": SEED_UNIT_T,
     "remark": "示例序列：河南稻谷农户出售价。"},
    {"seriesId": "OBJ-PUB-IN-0002", "indicatorId": "IND0010", "commodityFormId": "OBJ-PUB-CM-0003",
     "scopeCode": "SER-CORN-CN-EXFARM", "scopeLabel": "全国玉米收购价口径",
     "regionId": "REG0001", "frequency": "MONTH", "statisticalSourceId": "OBJ-PUB-SS-0001",
     "priceBasis": "EX_FARM", "taxBasis": "INCLUSIVE", "currencyCode": "CNY", "unitId": SEED_UNIT_T,
     "remark": "示例序列：全国玉米农户出售价。"},
    {"seriesId": "OBJ-PUB-IN-0003", "indicatorId": "IND0011", "commodityFormId": "CMD0006",
     "scopeCode": "SER-HOG-COST-CN", "scopeLabel": "全国生猪养殖成本口径",
     "regionId": "REG0001", "frequency": "MONTH", "statisticalSourceId": "OBJ-PUB-SS-0002",
     "priceBasis": "EX_FARM", "taxBasis": "INCLUSIVE", "currencyCode": "CNY", "unitId": SEED_UNIT_KG,
     "remark": "示例序列：生猪养殖综合成本。"},
    {"seriesId": "OBJ-PUB-IN-0004", "indicatorId": "IND0012", "commodityFormId": "CMD0007",
     "scopeCode": "SER-PORK-RETAIL-CN", "scopeLabel": "全国猪肉零售价口径",
     "regionId": "REG0001", "frequency": "MONTH", "statisticalSourceId": "OBJ-PUB-SS-0003",
     "priceBasis": "RETAIL", "taxBasis": "INCLUSIVE", "currencyCode": "CNY", "unitId": SEED_UNIT_KG,
     "remark": "示例序列：猪肉零售环节价格。"},
    {"seriesId": "OBJ-PUB-IN-0005", "indicatorId": "IND0013", "commodityFormId": "OBJ-PUB-CM-0002",
     "scopeCode": "SER-WHEAT-CN-EXFARM", "scopeLabel": "全国小麦收购价口径",
     "regionId": "REG0001", "frequency": "MONTH", "statisticalSourceId": "OBJ-PUB-SS-0001",
     "priceBasis": "EX_FARM", "taxBasis": "INCLUSIVE", "currencyCode": "CNY", "unitId": SEED_UNIT_T,
     "remark": "示例序列：全国小麦农户出售价。"},
    {"seriesId": "OBJ-PUB-IN-0006", "indicatorId": "IND0014", "commodityFormId": "OBJ-PUB-CM-0004",
     "scopeCode": "SER-SOYBEAN-CN-EXFARM", "scopeLabel": "全国大豆收购价口径",
     "regionId": "REG0001", "frequency": "MONTH", "statisticalSourceId": "OBJ-PUB-SS-0001",
     "priceBasis": "EX_FARM", "taxBasis": "INCLUSIVE", "currencyCode": "CNY", "unitId": SEED_UNIT_T,
     "remark": "示例序列：全国大豆农户出售价。"},
    {"seriesId": "OBJ-PUB-IN-0007", "indicatorId": "IND0015", "commodityFormId": "OBJ-PUB-CM-0007",
     "scopeCode": "SER-SOYOIL-CN-WHOLESALE", "scopeLabel": "全国豆油批发价口径",
     "regionId": "REG0001", "frequency": "MONTH", "statisticalSourceId": "OBJ-PUB-SS-0004",
     "priceBasis": "WHOLESALE", "taxBasis": "INCLUSIVE", "currencyCode": "CNY", "unitId": SEED_UNIT_T,
     "remark": "示例序列：全国豆油批发环节价格。"},
    {"seriesId": "OBJ-PUB-IN-0008", "indicatorId": "IND0016", "commodityFormId": "OBJ-PUB-CM-0008",
     "scopeCode": "SER-RAPEOIL-CN-WHOLESALE", "scopeLabel": "全国菜籽油批发价口径",
     "regionId": "REG0001", "frequency": "MONTH", "statisticalSourceId": "OBJ-PUB-SS-0004",
     "priceBasis": "WHOLESALE", "taxBasis": "INCLUSIVE", "currencyCode": "CNY", "unitId": SEED_UNIT_T,
     "remark": "示例序列：全国菜籽油批发环节价格。"},
]

SEED_VALUES = {
    "OBJ-PUB-IN-0001": [2620, 2640, 2650, 2660, 2650, 2640, 2630, 2620, 2610, 2600, 2590, 2580],
    "OBJ-PUB-IN-0002": [2380, 2400, 2420, 2430, 2410, 2390, 2370, 2360, 2340, 2330, 2320, 2310],
    "OBJ-PUB-IN-0003": [14.2, 14.1, 14.0, 13.9, 14.0, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 14.8],
    "OBJ-PUB-IN-0004": [26.5, 26.2, 25.8, 25.5, 25.6, 25.9, 26.3, 26.6, 26.9, 27.2, 27.5, 27.8],
    "OBJ-PUB-IN-0005": [2780, 2775, 2782, 2795, 2805, 2798, 2808, 2820, 2810, 2830, 2840, 2850],
    "OBJ-PUB-IN-0006": [5190, 5180, 5205, 5220, 5210, 5240, 5260, 5250, 5280, 5270, 5300, 5320],
    "OBJ-PUB-IN-0007": [9210, 9195, 9230, 9250, 9225, 9260, 9280, 9270, 9300, 9290, 9320, 9330],
    "OBJ-PUB-IN-0008": [10460, 10440, 10480, 10510, 10490, 10530, 10560, 10540, 10580, 10550, 10590, 10600],
}

SEED_EVENT_TYPES = [
    {"eventTypeId": "OBJ-PUB-EV-0001", "typeName": "最低收购价政策", "eventCategory": "POLICY",
     "isPolicyTool": 1, "toolVerb": "启动最低收购价收购", "defaultImpactDirection": "POSITIVE"},
    {"eventTypeId": "OBJ-PUB-EV-0002", "typeName": "进口配额调整", "eventCategory": "TRADE",
     "isPolicyTool": 0, "defaultImpactDirection": "UNCERTAIN"},
    {"eventTypeId": "OBJ-PUB-EV-0003", "typeName": "主产区天气预警", "eventCategory": "SUPPLY",
     "isPolicyTool": 0, "defaultImpactDirection": "NEGATIVE"},
    {"eventTypeId": "OBJ-PUB-EV-0004", "typeName": "市场监管提示", "eventCategory": "MARKET",
     "isPolicyTool": 0, "defaultImpactDirection": "UNCERTAIN"},
]

SEED_EVENTS = [
    {"eventId": "OBJ-PUB-EV-1001", "eventTypeId": "OBJ-PUB-EV-0001", "eventTitle": "稻谷托市收购启动",
     "eventTime": "2024-02-20", "announceTime": "2024-02-18T10:00:00Z", "effectiveFrom": "2024-02-20",
     "effectiveTo": "2024-06-30", "eventScale": "REGIONAL", "scopeRef": "OBJ-PUB-IN-0001", "severity": "MEDIUM",
     "remark": "示例事件：用于展示政策类事件时间线。"},
    {"eventId": "OBJ-PUB-EV-1002", "eventTypeId": "OBJ-PUB-EV-0002", "eventTitle": "玉米进口配额阶段性调整",
     "eventTime": "2024-06-15", "announceTime": "2024-06-14T09:00:00Z", "effectiveFrom": "2024-07-01",
     "eventScale": "NATIONAL", "scopeRef": "OBJ-PUB-IN-0002", "severity": "LOW",
     "remark": "示例事件：用于展示贸易类事件时间线。"},
    {"eventId": "OBJ-PUB-EV-1003", "eventTypeId": "OBJ-PUB-EV-0003", "eventTitle": "主产区干旱预警",
     "eventTime": "2024-08-01", "announceTime": "2024-08-01T08:00:00Z", "effectiveFrom": "2024-08-01",
     "effectiveTo": "2024-08-31", "eventScale": "REGIONAL", "scopeRef": "OBJ-PUB-IN-0002", "severity": "HIGH",
     "remark": "示例事件：用于展示供给类事件时间线。"},
    {"eventId": "OBJ-PUB-EV-1004", "eventTypeId": "OBJ-PUB-EV-0001", "eventTitle": "小麦最低收购价执行预案",
     "eventTime": "2024-05-25", "announceTime": "2024-05-20T09:00:00Z", "effectiveFrom": "2024-06-01",
     "effectiveTo": "2024-09-30", "eventScale": "REGIONAL", "scopeRef": "OBJ-PUB-IN-0005", "severity": "MEDIUM",
     "remark": "示例事件：小麦托市收购执行预案。"},
    {"eventId": "OBJ-PUB-EV-1005", "eventTypeId": "OBJ-PUB-EV-0002", "eventTitle": "进口大豆关税调整",
     "eventTime": "2024-03-10", "announceTime": "2024-03-08T09:00:00Z", "effectiveFrom": "2024-04-01",
     "effectiveTo": "2024-12-31", "eventScale": "NATIONAL", "scopeRef": "OBJ-PUB-IN-0006", "severity": "MEDIUM",
     "remark": "示例事件：进口大豆关税税率调整。"},
    {"eventId": "OBJ-PUB-EV-1006", "eventTypeId": "OBJ-PUB-EV-0001", "eventTitle": "菜籽油轮换收储",
     "eventTime": "2024-07-05", "announceTime": "2024-07-03T09:00:00Z", "effectiveFrom": "2024-07-10",
     "effectiveTo": "2024-10-31", "eventScale": "NATIONAL", "scopeRef": "OBJ-PUB-IN-0008", "severity": "LOW",
     "remark": "示例事件：菜籽油轮换收储安排。"},
]

SEED_FACTORS = [
    {"factorId": "FAC0001", "factorCode": "FAC-WEATHER-YIELD", "factorName": "天气与单产",
     "factorCategory": "SUPPLY", "factorSubCategory": "气候条件", "defaultDirection": "UP",
     "measuredByIndicatorId": "IND0010", "mechanism": "示例因子：主产区干旱会压缩玉米供给并抬升价格。",
     "proposedBy": "MARKET_PACK", "status": FACTOR_ACTIVE},
    {"factorId": "FAC0002", "factorCode": "FAC-FEED-COST", "factorName": "饲料成本",
     "factorCategory": "COST", "factorSubCategory": "养殖投入", "defaultDirection": "UP",
     "measuredByIndicatorId": "IND0011", "mechanism": "示例因子：玉米等饲料原料价格上行会推高养殖成本。",
     "proposedBy": "MARKET_PACK", "status": FACTOR_ACTIVE},
    {"factorId": "FAC0003", "factorCode": "FAC-FESTIVAL-DEMAND", "factorName": "节日消费",
     "factorCategory": "DEMAND", "factorSubCategory": "消费季节性", "defaultDirection": "UP",
     "measuredByIndicatorId": "IND0012", "mechanism": "示例因子：节前备货会阶段性提升猪肉零售需求。",
     "proposedBy": "MARKET_PACK", "status": FACTOR_ACTIVE},
    {"factorId": "FAC0004", "factorCode": "FAC-IMPORT-QUOTA", "factorName": "进口配额",
     "factorCategory": "TRADE", "factorSubCategory": "贸易政策", "defaultDirection": "UNCERTAIN",
     "measuredByIndicatorId": "IND0010", "mechanism": "示例因子：配额调整通过进口补充量影响国内价格。",
     "proposedBy": "MARKET_PACK", "status": FACTOR_ACTIVE},
    {"factorId": "FAC0005", "factorCode": "FAC-MIN-PRICE-POLICY", "factorName": "最低收购价政策",
     "factorCategory": "POLICY", "factorSubCategory": "价格支持", "defaultDirection": "UP",
     "measuredByIndicatorId": "IND0009", "mechanism": "示例因子：托市收购为稻谷价格提供下限支撑。",
     "proposedBy": "MARKET_PACK", "status": FACTOR_ACTIVE},
    {"factorId": "FAC0006", "factorCode": "FAC-CRUSH-MARGIN", "factorName": "压榨利润",
     "factorCategory": "COST", "factorSubCategory": "加工收益", "defaultDirection": "UP",
     "measuredByIndicatorId": "IND0015", "mechanism": "示例因子：压榨利润改善会提升油厂开工，增加豆油供给并影响批发价。",
     "proposedBy": "MARKET_PACK", "status": FACTOR_ACTIVE},
    {"factorId": "FAC0007", "factorCode": "FAC-OIL-INVENTORY", "factorName": "油脂库存",
     "factorCategory": "SUPPLY", "factorSubCategory": "库存水平", "defaultDirection": "DOWN",
     "measuredByIndicatorId": "IND0015", "mechanism": "示例因子：油脂库存回补增加可供量，对批发价形成压制。",
     "proposedBy": "MARKET_PACK", "status": FACTOR_ACTIVE},
    {"factorId": "FAC0008", "factorCode": "FAC-IMPORT-COST", "factorName": "进口成本",
     "factorCategory": "TRADE", "factorSubCategory": "到岸成本", "defaultDirection": "UP",
     "measuredByIndicatorId": "IND0014", "mechanism": "示例因子：进口大豆到岸成本抬升会传导至国内大豆收购价。",
     "proposedBy": "MARKET_PACK", "status": FACTOR_ACTIVE},
]

SEED_ASSERTIONS = [
    {"assertionId": "AST-0001", "assertionType": "FORECAST", "subjectType": "IndicatorSeries",
     "subjectRef": "OBJ-PUB-IN-0001", "predicate": "WILL_RISE", "objectValue": 2800.0,
     "objectUnitId": SEED_UNIT_T, "targetPeriodId": "2024-12", "scopeRef": "OBJ-PUB-IN-0001",
     "assertedByType": "ORGANIZATION", "assertedById": "ORG-EXAMPLE-01", "assertedAt": "2024-09-10",
     "horizon": "SHORT", "rationale": "示例观点：托市收购与饲料需求共同支撑稻谷价格，预计年末温和上行。",
     "stanceStrength": "MODERATE", "status": SERIES_STATUS},
    {"assertionId": "AST-0002", "assertionType": "FORECAST", "subjectType": "IndicatorSeries",
     "subjectRef": "OBJ-PUB-IN-0001", "predicate": "WILL_FALL", "objectValue": 2500.0,
     "objectUnitId": SEED_UNIT_T, "targetPeriodId": "2024-12", "scopeRef": "OBJ-PUB-IN-0001",
     "assertedByType": "ORGANIZATION", "assertedById": "ORG-EXAMPLE-02", "assertedAt": "2024-09-12",
     "horizon": "SHORT", "rationale": "示例观点：稻谷库存偏高且进口补充充足，预计年末价格回落。",
     "stanceStrength": "MODERATE", "status": SERIES_STATUS},
    {"assertionId": "AST-0003", "assertionType": "FORECAST", "subjectType": "IndicatorSeries",
     "subjectRef": "OBJ-PUB-IN-0004", "predicate": "WILL_RISE", "objectValue": 30.0,
     "objectUnitId": SEED_UNIT_KG, "targetPeriodId": "2024-12", "scopeRef": "OBJ-PUB-IN-0004",
     "assertedByType": "ORGANIZATION", "assertedById": "ORG-EXAMPLE-03", "assertedAt": "2024-10-08",
     "horizon": "SHORT", "rationale": "示例观点：猪肉零售价格将温和上行，幅度有限。",
     "stanceStrength": "WEAK", "status": SERIES_STATUS},
    {"assertionType": "FORECAST", "subjectType": "IndicatorSeries",
     "subjectRef": "OBJ-PUB-IN-0004", "predicate": "WILL_RISE", "objectValue": 30.0,
     "objectUnitId": SEED_UNIT_KG, "targetPeriodId": "2024-12", "scopeRef": "OBJ-PUB-IN-0004",
     "assertedByType": "ORGANIZATION", "assertedById": "ORG-EXAMPLE-03", "assertedAt": "2024-10-09",
     "horizon": "SHORT", "rationale": "示例观点 猪肉零售价格将温和上行 幅度有限",
     "stanceStrength": "WEAK", "status": SERIES_STATUS},
    {"assertionId": "AST-0005", "assertionType": "ESTIMATE", "subjectType": "IndicatorSeries",
     "subjectRef": "OBJ-PUB-IN-0003", "predicate": "UNCHANGED", "objectValue": 14.5,
     "objectUnitId": SEED_UNIT_KG, "targetPeriodId": "2024-12", "scopeRef": "OBJ-PUB-IN-0003",
     "assertedByType": "ORGANIZATION", "assertedById": "ORG-EXAMPLE-01", "assertedAt": "2024-10-15",
     "horizon": "SHORT", "rationale": "示例观点：养殖成本预计保持平稳。",
     "stanceStrength": "MODERATE", "status": SERIES_STATUS},
    {"assertionId": "AST-0006", "assertionType": "FORECAST", "subjectType": "IndicatorSeries",
     "subjectRef": "OBJ-PUB-IN-0006", "predicate": "WILL_RISE", "objectValue": 5500.0,
     "objectUnitId": SEED_UNIT_T, "targetPeriodId": "2024-12", "scopeRef": "OBJ-PUB-IN-0006",
     "assertedByType": "ORGANIZATION", "assertedById": "ORG-EXAMPLE-02", "assertedAt": "2024-09-18",
     "horizon": "SHORT", "rationale": "进口成本抬升叠加饲料需求恢复，预计大豆收购价年末上行至 5500 元/吨附近。",
     "stanceStrength": "MODERATE", "status": SERIES_STATUS},
    {"assertionId": "AST-0007", "assertionType": "FORECAST", "subjectType": "IndicatorSeries",
     "subjectRef": "OBJ-PUB-IN-0008", "predicate": "WILL_FALL", "objectValue": 10200.0,
     "objectUnitId": SEED_UNIT_T, "targetPeriodId": "2024-12", "scopeRef": "OBJ-PUB-IN-0008",
     "assertedByType": "ORGANIZATION", "assertedById": "ORG-EXAMPLE-03", "assertedAt": "2024-09-20",
     "horizon": "SHORT", "rationale": "轮换收储结束后油脂库存回补，预计菜籽油批发价回落至 10200 元/吨附近。",
     "stanceStrength": "MODERATE", "status": SERIES_STATUS},
]

SEED_LINKS = [
    {"linkId": "LNK-PT-0001", "fromRef": "OBJ-PUB-IN-0003", "toRef": "OBJ-PUB-IN-0004",
     "transmissionType": "COST_PUSH", "elasticity": 0.65, "lagDays": 30, "samplePeriodId": "2024-12",
     "regimeNote": "示例传导：基于2024-01至2024-12月度数据估计，非确定关系。"},
    {"linkId": "LNK-PT-0002", "fromRef": "OBJ-PUB-IN-0002", "toRef": "OBJ-PUB-IN-0003",
     "transmissionType": "COST_PUSH", "elasticity": 0.42, "lagDays": 45, "samplePeriodId": "2024-12",
     "regimeNote": "示例传导：玉米价格向养殖成本传导，滞后约一个半月。"},
    {"linkId": "LNK-PT-0003", "fromRef": "OBJ-PUB-IN-0002", "toRef": "OBJ-PUB-IN-0001",
     "transmissionType": "ARBITRAGE", "elasticity": 0.18, "lagDays": 20, "samplePeriodId": "2024-12",
     "regimeNote": "示例传导：玉米与稻谷的替代套利关系，弹性较弱。"},
    {"linkId": "LNK-PT-0004", "fromRef": "OBJ-PUB-IN-0006", "toRef": "OBJ-PUB-IN-0007",
     "transmissionType": "COST_PUSH", "elasticity": 0.65, "lagDays": 15, "samplePeriodId": "2024-12",
     "regimeNote": "示例传导：大豆收购价经压榨环节向豆油批发价传导，滞后约两周。"},
]

SEED_EVIDENCE = [
    {"evidenceId": "OBJ-PUB-ED-0001", "targetType": "Assertion", "targetId": "AST-0001",
     "documentId": "SOU0001", "quote": "托市收购启动后，主产区稻谷收购价获得阶段性支撑。",
     "pageNo": 2, "evidenceType": "CITED", "strength": 0.78},
    {"evidenceId": "OBJ-PUB-ED-0002", "targetType": "Assertion", "targetId": "AST-0002",
     "documentId": "SOU0003", "quote": "稻谷库存偏高且进口补充充足，年末价格存在回落压力。",
     "pageNo": 3, "evidenceType": "CITED", "strength": 0.66},
    {"evidenceId": "OBJ-PUB-ED-0003", "targetType": "Assertion", "targetId": "AST-0003",
     "documentId": "SOU0002", "quote": "节前消费备货带动猪肉零售价格小幅走高。",
     "pageNo": 4, "evidenceType": "DIRECT", "strength": 0.72},
    {"evidenceId": "OBJ-PUB-ED-0004", "targetType": "Assertion", "targetId": "AST-0005",
     "documentId": "SOU0002", "quote": "饲料成本与养殖投入保持稳定，生猪养殖成本预计持平。",
     "pageNo": 5, "evidenceType": "DIRECT", "strength": 0.81},
    {"evidenceId": "OBJ-PUB-ED-0005", "targetType": "Assertion", "targetId": "AST-0006",
     "documentId": "SOU0004", "quote": "进口大豆成本抬升，饲料需求恢复，收购价预计上行。",
     "pageNo": 6, "evidenceType": "CITED", "strength": 0.69},
    {"evidenceId": "OBJ-PUB-ED-0006", "targetType": "Assertion", "targetId": "AST-0007",
     "documentId": "SOU0004", "quote": "轮换收储结束，油脂库存回补，菜籽油批发价承压。",
     "pageNo": 7, "evidenceType": "CITED", "strength": 0.64},
    {"evidenceId": "OBJ-PUB-ED-0007", "targetType": "ImpactLink", "targetId": "LNK-IM-0001",
     "documentId": "SOU0001", "quote": "最低收购价执行预案为稻谷价格提供下限支撑。",
     "pageNo": 8, "evidenceType": "CITED", "strength": 0.83},
    {"evidenceId": "OBJ-PUB-ED-0008", "targetType": "ImpactLink", "targetId": "LNK-IM-0002",
     "documentId": "SOU0002", "quote": "小麦最低收购价执行预案明确托市收购区间与执行时间。",
     "pageNo": 9, "evidenceType": "CITED", "strength": 0.76},
    {"evidenceId": "OBJ-PUB-ED-0009", "targetType": "ImpactLink", "targetId": "LNK-IM-0003",
     "documentId": "SOU0004", "quote": "进口大豆关税调整将改变到岸成本与进口节奏。",
     "pageNo": 10, "evidenceType": "DERIVED", "strength": 0.62},
    {"evidenceId": "OBJ-PUB-ED-0010", "targetType": "ImpactLink", "targetId": "LNK-IM-0004",
     "documentId": "SOU0001", "quote": "政策明确稻谷最低收购价水平，稳定种粮收益预期。",
     "pageNo": 11, "evidenceType": "CITED", "strength": 0.87},
]

SEED_IMPACT_LINKS = [
    {"linkId": "LNK-IM-0001", "sourceType": "EVENT", "sourceId": "OBJ-PUB-EV-1001",
     "targetType": "FACTOR", "targetId": "FAC0005", "impactDirection": "POSITIVE", "lagDays": 0,
     "mechanism": "示例联动：稻谷托市收购直接强化最低收购价因子。",
     "assertedByType": "MARKET_PACK", "assertedById": "MARKET_PACK", "version": 1, "status": "有效"},
    {"linkId": "LNK-IM-0002", "sourceType": "EVENT", "sourceId": "OBJ-PUB-EV-1004",
     "targetType": "FACTOR", "targetId": "FAC0005", "impactDirection": "POSITIVE", "lagDays": 7,
     "mechanism": "示例联动：小麦执行预案落地后托市效应约一周开始显现。",
     "assertedByType": "MARKET_PACK", "assertedById": "MARKET_PACK", "version": 1, "status": "有效"},
    {"linkId": "LNK-IM-0003", "sourceType": "EVENT", "sourceId": "OBJ-PUB-EV-1005",
     "targetType": "FACTOR", "targetId": "FAC0008", "impactDirection": "POSITIVE", "lagDays": 14,
     "mechanism": "示例联动：关税调整经到岸成本传导，约两周后影响进口成本因子。",
     "assertedByType": "MARKET_PACK", "assertedById": "MARKET_PACK", "version": 1, "status": "有效"},
    {"linkId": "LNK-IM-0004", "sourceType": "POLICY", "sourceId": "POL0001",
     "targetType": "COMMODITY", "targetId": "CMD0001", "impactDirection": "POSITIVE", "lagDays": 0,
     "mechanism": "示例联动：稻谷最低收购价政策直接作用于稻谷品种。",
     "assertedByType": "MARKET_PACK", "assertedById": "MARKET_PACK", "version": 1, "status": "有效"},
]

SEED_FACTOR_DRIVES = [
    {"factorId": "FAC0001", "seriesId": "OBJ-PUB-IN-0005", "elasticity": 0.28, "lagDays": 0,
     "samplePeriodId": "2024-12", "version": 1, "status": "有效"},
    {"factorId": "FAC0003", "seriesId": "OBJ-PUB-IN-0008", "elasticity": 0.20, "lagDays": 7,
     "samplePeriodId": "2024-12", "version": 1, "status": "有效"},
    {"factorId": "FAC0006", "seriesId": "OBJ-PUB-IN-0007", "elasticity": 0.42, "lagDays": 14,
     "samplePeriodId": "2024-12", "version": 1, "status": "有效"},
    {"factorId": "FAC0007", "seriesId": "OBJ-PUB-IN-0007", "elasticity": -0.35, "lagDays": 7,
     "samplePeriodId": "2024-12", "version": 1, "status": "有效"},
    {"factorId": "FAC0008", "seriesId": "OBJ-PUB-IN-0006", "lagDays": 21,
     "samplePeriodId": "2024-12", "version": 1, "status": "有效"},
]

SEED_EVENT_FACTORS = [
    {"eventId": "OBJ-PUB-EV-1001", "factorId": "FAC0005", "version": 1, "status": "有效"},
    {"eventId": "OBJ-PUB-EV-1004", "factorId": "FAC0005", "version": 1, "status": "有效"},
    {"eventId": "OBJ-PUB-EV-1005", "factorId": "FAC0008", "version": 1, "status": "有效"},
    {"eventId": "OBJ-PUB-EV-1006", "factorId": "FAC0007", "version": 1, "status": "有效"},
]


class MarketError(ValueError):
    """市场包校验失败；status 对应 HTTP 语义（400 参数 / 404 不存在 / 409 唯一性冲突）。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def similarity(left: str | None, right: str | None) -> float:
    """命题文本相似度：去标点后取汉字二元组 + 英文数字词，计算 Jaccard。"""
    first, second = _tokens(left), _tokens(right)
    if not first and not second:
        return 1.0
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def _tokens(value: str | None) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    tokens: set[str] = set()
    for chunk in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(chunk) == 1:
            tokens.add(chunk)
        else:
            tokens.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
    tokens.update(re.findall(r"[a-z0-9]+", text))
    return tokens


def _months(year: int) -> list[dict[str, Any]]:
    rows = []
    for month in range(1, 13):
        last_day = calendar.monthrange(year, month)[1]
        rows.append({
            "periodId": f"{year}-{month:02d}",
            "periodType": "MONTH",
            "startDate": f"{year}-{month:02d}-01",
            "endDate": f"{year}-{month:02d}-{last_day:02d}",
            "label": f"{year}年{month}月",
        })
    return rows


def _published_at(period_id: str) -> str:
    year, month = (int(part) for part in period_id.split("-"))
    publish = date(year, month, 1) + timedelta(days=35)
    return f"{publish.isoformat()}T00:00:00Z"


class MarketService:
    def __init__(self, db: Database, scope: ScopeService):
        self.db = db
        self.scope = scope

    # ---------- 初始化 ----------

    def initialize(self) -> None:
        missing = [table for table in MARKET_TABLES if table not in self.db.tables]
        if missing:
            raise RuntimeError(f"市场包物理表未注册：{', '.join(missing)}")
        self.scope.initialize()
        self.scope.sync_dimensions()
        with self.db.engine.begin() as conn:
            conn.execute(text(
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_marketplace_futures_code" '
                'ON "MarketPlace" ("exchangeCode") '
                'WHERE "marketType"=\'FUTURES\' AND "exchangeCode" IS NOT NULL'
            ))
            conn.execute(text(
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_evidence_target_quote" '
                'ON "Evidence" ("targetType","targetId","quoteHash")'
            ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS "idx_impact_source" '
                'ON "ImpactLink" ("sourceType","sourceId")'
            ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS "idx_evidence_target" '
                'ON "Evidence" ("targetType","targetId")'
            ))
        total = self.db.fetch_one('SELECT COUNT(*) AS total FROM "IndicatorSeries"') or {}
        if int(total.get("total") or 0) > 0:
            # 阶段 3 旧库仍是 4 维 Scope，会被 G4-2 拦下；先由 seed_market 升级，这里只复核已升级的库
            legacy = self.db.fetch_one(
                'SELECT COUNT(*) AS total FROM "OntologyScope" '
                'WHERE "scopeType"=:scopeType AND "members" NOT LIKE \'%CHANNEL%\'',
                {"scopeType": SERIES_SCOPE},
            ) or {}
            if int(legacy.get("total") or 0) == 0:
                self.validate_stage4()

    # ---------- 枚举与基础校验 ----------

    def _enum_values(self, name: str) -> set[str]:
        node = self.scope.enums.get(name) or {}
        return {str(item["value"]) for item in node.get("values") or []}

    def _require_enum(self, name: str, value: Any, label: str) -> str:
        text_value = str(value or "").strip()
        if text_value not in self._enum_values(name):
            raise MarketError(f"{label} 取值 {text_value or '空'} 不在枚举 {name} 内")
        return text_value

    def _require_row(self, table: str, key: str, value: Any, label: str) -> dict[str, Any]:
        row = self.db.fetch_one(
            f"SELECT * FROM {quote(table)} WHERE {quote(key)}=:value", {"value": value}
        )
        if not row:
            raise MarketError(f"{label}不存在：{value}", 404)
        return row

    def _series_scope(self, scope_id: str) -> dict[str, Any]:
        scope = self.scope.scope(scope_id)
        if not scope:
            raise MarketError(f"范围不存在：{scope_id}", 404)
        if scope["scopeType"] != SERIES_SCOPE:
            raise MarketError(f"范围 {scope_id} 不是 {SERIES_SCOPE}，不能承载指标序列")
        return scope

    @staticmethod
    def _member(scope: dict[str, Any], dimension: str) -> dict[str, Any] | None:
        return next((item for item in scope["members"] if item["dimensionCode"] == dimension), None)

    # ---------- 种子 ----------

    def _upsert_series_scope(
        self, members: list[dict[str, str]], scope_code: str, label: str, actor: str = "seed"
    ) -> dict[str, Any]:
        """序列范围升维：同一 scopeCode 成员变化时原地更新，保留 scopeId 引用。"""
        normalized = normalize_members(members)
        for member in normalized:
            self.scope._validate_member(member)
        self.scope._validate_multivalue(normalized)
        key = normalized_key(SERIES_SCOPE, normalized)
        existing = self.scope.find_scope_by_key(key)
        if existing:
            return existing
        conflicting = self.db.fetch_one(
            'SELECT * FROM "OntologyScope" WHERE "scopeCode"=:code', {"code": scope_code.upper()}
        )
        if not conflicting:
            return self.scope.upsert_scope(
                SERIES_SCOPE, normalized, scope_code, label, actor=actor
            )
        with self.db.engine.begin() as conn:
            conn.execute(text(
                'UPDATE "OntologyScope" SET "members"=:members, "normalizedKey"=:key, '
                '"memberCount"=:count, "label"=:label, "updatedBy"=:actor, "updatedAt"=:now '
                'WHERE "scopeId"=:id'
            ), {
                "members": json.dumps(normalized, ensure_ascii=False),
                "key": key,
                "count": len(normalized),
                "label": label or conflicting["scopeCode"],
                "actor": actor,
                "now": utc_now(),
                "id": conflicting["scopeId"],
            })
        return self.scope.scope(conflicting["scopeId"]) or conflicting

    def seed_market(self, actor: str = "seed") -> None:
        form_ids = {row["formId"] for row in SEED_FORMS}
        scopes: dict[str, str] = {}
        for spec in SEED_SERIES:
            commodity_id = spec["commodityFormId"]
            members = [
                {
                    "dimensionCode": "COMMODITY",
                    "objectType": "CommodityForm" if commodity_id in form_ids else "Commodity",
                    "objectId": commodity_id,
                },
                {"dimensionCode": "GEOGRAPHY", "objectType": "Region", "objectId": spec["regionId"]},
                {"dimensionCode": "UNIT_BASIS", "objectType": "UnitOfMeasure", "objectId": spec["unitId"]},
                {"dimensionCode": "TAX_BASIS", "objectType": "TaxBasis", "objectId": spec["taxBasis"]},
                {
                    "dimensionCode": "CHANNEL",
                    "objectType": "MarketPlace",
                    "objectId": SERIES_CHANNEL[spec["seriesId"]],
                },
            ]
            scope = self._upsert_series_scope(
                members, spec["scopeCode"], spec["scopeLabel"], actor=actor
            )
            scopes[spec["seriesId"]] = scope["scopeId"]

        periods = _months(2024)
        with self.db.engine.begin() as conn:
            for row in SEED_REGIONS:
                self.db.insert("Region", row, conn, actor=actor, upsert=True)
            for row in SEED_COMMODITIES:
                self.db.insert("CmdCommodity", row, conn, actor=actor, upsert=True)
            for row in SEED_FORMS:
                self.db.insert("CommodityForm", row, conn, actor=actor, upsert=True)
            for row in SEED_PLACES:
                self.db.insert("MarketPlace", row, conn, actor=actor, upsert=True)
            for row in SEED_INDICATORS:
                self.db.insert("Indicator", row, conn, actor=actor, upsert=True)
            for row in SEED_SOURCES:
                self.db.insert("StatisticalSource", row, conn, actor=actor, upsert=True)
            for row in periods:
                self.db.insert("TimePeriod", row, conn, actor=actor, upsert=True)
            for row in SEED_UNITS:
                self.db.insert("UnitOfMeasure", row, conn, actor=actor, upsert=True)
            for spec in SEED_SERIES:
                row = {key: value for key, value in spec.items() if key not in ("scopeCode", "scopeLabel", "regionId")}
                row["scopeId"] = scopes[spec["seriesId"]]
                row["startPeriodId"] = periods[0]["periodId"]
                row["endPeriodId"] = periods[-1]["periodId"]
                row["status"] = SERIES_STATUS
                self.db.insert("IndicatorSeries", row, conn, actor=actor, upsert=True)
            # 既有序列回填形态引用；upsert 不会更新已有行
            for series_id, form_id in (
                ("OBJ-PUB-IN-0001", "OBJ-PUB-CM-0001"),
                ("OBJ-PUB-IN-0002", "OBJ-PUB-CM-0003"),
            ):
                conn.execute(text(
                    'UPDATE "IndicatorSeries" SET "commodityFormId"=:form WHERE "seriesId"=:sid'
                ), {"form": form_id, "sid": series_id})
            for spec in SEED_SERIES:
                for index, value in enumerate(SEED_VALUES[spec["seriesId"]]):
                    point = {
                        "pointId": f"OBJ-PUB-DP-{spec['seriesId'][-4:]}-{index + 1:02d}",
                        "seriesId": spec["seriesId"],
                        "periodId": periods[index]["periodId"],
                        "value": float(value),
                        "unitId": spec["unitId"],
                        "publishedAt": _published_at(periods[index]["periodId"]),
                        "revisionNo": 0,
                        "qualityFlag": "NORMAL",
                        "sourceRef": "示例数据，不构成真实行情",
                    }
                    self.db.insert("PriceDataPoint", point, conn, actor=actor, upsert=True)
            for row in SEED_EVENT_TYPES:
                self.db.insert("EventType", row, conn, actor=actor, upsert=True)
            for row in SEED_FACTORS:
                self.db.insert("Factor", row, conn, actor=actor, upsert=True)

        for spec in SEED_EVENTS:
            payload = {key: value for key, value in spec.items() if key not in ("eventId", "scopeRef")}
            payload["eventId"] = spec["eventId"]
            payload["scopeId"] = scopes[spec["scopeRef"]]
            self.upsert_event(payload, actor=actor)

        for spec in SEED_ASSERTIONS:
            payload = {key: value for key, value in spec.items() if key not in ("subjectRef", "scopeRef")}
            payload["subjectId"] = spec["subjectRef"]
            payload["scopeId"] = scopes[spec["scopeRef"]]
            self.upsert_assertion(payload, actor=actor)

        for spec in SEED_LINKS:
            payload = {key: value for key, value in spec.items() if key not in ("fromRef", "toRef")}
            payload["fromSeriesId"] = spec["fromRef"]
            payload["toSeriesId"] = spec["toRef"]
            self.upsert_link(payload, actor=actor)

        with self.db.engine.begin() as conn:
            for spec in SEED_EVIDENCE:
                row = {**spec}
                row["quoteHash"] = hashlib.sha1(spec["quote"].encode("utf-8")).hexdigest()
                row["verifiedBy"] = "system"
                row["verifiedAt"] = "2024-11-01"
                self.db.insert("Evidence", row, conn, actor=actor, upsert=True)
            for row in SEED_IMPACT_LINKS:
                self.db.insert("ImpactLink", row, conn, actor=actor, upsert=True)
            for row in SEED_FACTOR_DRIVES:
                self.db.insert("FactorDriveLink", row, conn, actor=actor, upsert=True)
            for row in SEED_EVENT_FACTORS:
                self.db.insert("EventFactorLink", row, conn, actor=actor, upsert=True)

        self.validate_stage4()

    # ---------- 写入（仅供种子与测试调用，不暴露 HTTP 路由） ----------

    def upsert_series(self, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        required = ["indicatorId", "scopeId", "frequency", "statisticalSourceId", "priceBasis", "taxBasis", "currencyCode", "unitId"]
        missing = [field for field in required if not str(payload.get(field) or "").strip()]
        if missing:
            raise MarketError(f"指标序列缺少必填字段：{', '.join(missing)}")

        frequency = self._require_enum("PeriodType", payload["frequency"], "frequency")
        price_basis = self._require_enum("PriceBasis", payload["priceBasis"], "priceBasis")
        tax_basis = self._require_enum("TaxBasis", payload["taxBasis"], "taxBasis")
        currency = str(payload["currencyCode"]).strip().upper()

        self._require_row("Indicator", "indicatorId", payload["indicatorId"], "指标")
        self._require_row("StatisticalSource", "sourceId", payload["statisticalSourceId"], "统计源")
        unit = self._require_row("UnitOfMeasure", "unitId", payload["unitId"], "计量单位")
        scope = self._series_scope(str(payload["scopeId"]))

        tax_member = self._member(scope, "TAX_BASIS")
        if not tax_member or tax_member["objectId"] != tax_basis:
            raise MarketError("口径不一致：序列 taxBasis 必须与 Scope 的 TAX_BASIS 成员相等")
        unit_member = self._member(scope, "UNIT_BASIS")
        if not unit_member or unit_member["objectId"] != payload["unitId"]:
            raise MarketError("口径不一致：序列 unitId 必须与 Scope 的 UNIT_BASIS 成员相等")
        if str(unit.get("currencyCode") or "").upper() != currency:
            raise MarketError("口径不一致：序列 currencyCode 必须与计量单位 currencyCode 相同")

        for field in ("startPeriodId", "endPeriodId"):
            if payload.get(field):
                self._require_row("TimePeriod", "periodId", payload[field], "时间期间")

        series_id = str(payload.get("seriesId") or "").strip()
        if series_id and self.db.fetch_one(
            f"SELECT seriesId FROM {quote('IndicatorSeries')} WHERE seriesId=:id", {"id": series_id}
        ):
            return {"seriesId": series_id, "created": False}

        existing = self.db.fetch_one(
            'SELECT seriesId FROM "IndicatorSeries" WHERE indicatorId=:indicator AND scopeId=:scope '
            'AND status=:status AND seriesId<>:series',
            {"indicator": payload["indicatorId"], "scope": payload["scopeId"],
             "status": SERIES_STATUS, "series": series_id or ""},
        )
        if existing:
            raise MarketError(f"同一指标在同一口径下已存在有效序列：{existing['seriesId']}", 409)

        with self.db.engine.begin() as conn:
            new_id = series_id or self.db.next_id("IndicatorSeries", "OBJ-PUB-IN", conn)
            row = {
                "seriesId": new_id,
                "indicatorId": payload["indicatorId"],
                "commodityFormId": payload.get("commodityFormId"),
                "scopeId": payload["scopeId"],
                "frequency": frequency,
                "statisticalSourceId": payload["statisticalSourceId"],
                "externalSeriesCode": payload.get("externalSeriesCode"),
                "priceBasis": price_basis,
                "taxBasis": tax_basis,
                "currencyCode": currency,
                "unitId": payload["unitId"],
                "startPeriodId": payload.get("startPeriodId"),
                "endPeriodId": payload.get("endPeriodId"),
                "status": payload.get("status") or SERIES_STATUS,
                "remark": payload.get("remark"),
            }
            self.db.insert("IndicatorSeries", row, conn, actor=actor, upsert=True)
            self.db.log_change(conn, "指标序列", new_id, "CREATE", {"marketPack": "stage3"})
        return {"seriesId": new_id, "created": True}

    def upsert_point(self, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        required = ["seriesId", "periodId", "publishedAt"]
        missing = [field for field in required if not str(payload.get(field) or "").strip()]
        if missing:
            raise MarketError(f"价格数据点缺少必填字段：{', '.join(missing)}")
        if payload.get("value") is None:
            raise MarketError("价格数据点缺少必填字段：value")

        series = self._require_row("IndicatorSeries", "seriesId", payload["seriesId"], "指标序列")
        self._require_row("TimePeriod", "periodId", payload["periodId"], "时间期间")
        unit_id = payload.get("unitId") or series["unitId"]
        if unit_id != series["unitId"]:
            raise MarketError("单位不一致：数据点 unitId 必须与所属序列 unitId 相同")
        quality = self._require_enum("QualityFlag", payload.get("qualityFlag") or "NORMAL", "qualityFlag")

        point_id = str(payload.get("pointId") or "").strip()
        if point_id and self.db.fetch_one(
            "SELECT pointId FROM \"PriceDataPoint\" WHERE pointId=:id", {"id": point_id}
        ):
            return {"pointId": point_id, "created": False}

        existing = self.db.fetch_one(
            'SELECT pointId FROM "PriceDataPoint" WHERE seriesId=:series AND periodId=:period',
            {"series": payload["seriesId"], "period": payload["periodId"]},
        )
        if existing:
            raise MarketError(f"同一序列同一期间已存在数据点：{existing['pointId']}", 409)

        with self.db.engine.begin() as conn:
            new_id = point_id or self.db.next_id("PriceDataPoint", "OBJ-PUB-DP", conn)
            row = {
                "pointId": new_id,
                "seriesId": payload["seriesId"],
                "periodId": payload["periodId"],
                "value": float(payload["value"]),
                "unitId": unit_id,
                "publishedAt": payload["publishedAt"],
                "revisionNo": int(payload.get("revisionNo") or 0),
                "qualityFlag": quality,
                "sourceRef": payload.get("sourceRef"),
                "remark": payload.get("remark"),
            }
            self.db.insert("PriceDataPoint", row, conn, actor=actor, upsert=True)
        return {"pointId": new_id, "created": True}

    def upsert_event(self, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        required = ["eventTypeId", "eventTitle", "eventTime", "eventScale"]
        missing = [field for field in required if not str(payload.get(field) or "").strip()]
        if missing:
            raise MarketError(f"事件缺少必填字段：{', '.join(missing)}")

        event_type = self._require_row("EventType", "eventTypeId", payload["eventTypeId"], "事件类型")
        scale = self._require_enum("EventScale", payload["eventScale"], "eventScale")
        severity = payload.get("severity")
        if severity:
            self._require_enum("Severity", severity, "severity")
        if int(event_type.get("isPolicyTool") or 0) == 1 and not str(payload.get("scopeId") or "").strip():
            raise MarketError("政策类事件必须填写 scopeId")
        if payload.get("scopeId"):
            self._require_row("OntologyScope", "scopeId", payload["scopeId"], "范围")
        start, end = payload.get("effectiveFrom"), payload.get("effectiveTo")
        if start and end and start > end:
            raise MarketError("effectiveFrom 不得晚于 effectiveTo")

        event_id = str(payload.get("eventId") or "").strip()
        if event_id and self.db.fetch_one(
            f"SELECT eventId FROM {quote('Event')} WHERE eventId=:id", {"id": event_id}
        ):
            return {"eventId": event_id, "created": False}

        with self.db.engine.begin() as conn:
            new_id = event_id or self.db.next_id("Event", "OBJ-PUB-EV", conn)
            row = {
                "eventId": new_id,
                "eventTypeId": payload["eventTypeId"],
                "eventTitle": payload["eventTitle"],
                "eventTime": payload["eventTime"],
                "announceTime": payload.get("announceTime"),
                "effectiveFrom": start,
                "effectiveTo": end,
                "eventScale": scale,
                "scopeId": payload.get("scopeId"),
                "actorOrgIds": payload.get("actorOrgIds"),
                "severity": severity,
                "parentEventId": payload.get("parentEventId"),
                "sourceDocId": payload.get("sourceDocId"),
                "remark": payload.get("remark"),
            }
            self.db.insert("Event", row, conn, actor=actor, upsert=True)
        return {"eventId": new_id, "created": True}

    def upsert_assertion(self, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        required = ["assertionType", "subjectType", "subjectId", "predicate", "targetPeriodId",
                    "scopeId", "assertedByType", "assertedById", "assertedAt", "horizon"]
        missing = [field for field in required if not str(payload.get(field) or "").strip()]
        if missing:
            raise MarketError(f"断言缺少必填字段：{', '.join(missing)}")

        assertion_type = self._require_enum("AssertionType", payload["assertionType"], "assertionType")
        subject_type = self._require_enum("ObjectTypeName", payload["subjectType"], "subjectType")
        predicate = self._require_enum("AssertionPredicate", payload["predicate"], "predicate")
        by_type = self._require_enum("AssertedByType", payload["assertedByType"], "assertedByType")
        horizon = self._require_enum("Horizon", payload["horizon"], "horizon")
        stance = payload.get("stanceStrength")
        if stance:
            stance = self._require_enum("StanceStrength", stance, "stanceStrength")
        if payload.get("objectValue") is not None and not str(payload.get("objectUnitId") or "").strip():
            raise MarketError("objectValue 非空时必须填写 objectUnitId")

        self._require_row("TimePeriod", "periodId", payload["targetPeriodId"], "目标期间")
        self._require_row("OntologyScope", "scopeId", payload["scopeId"], "断言范围")

        assertion_id = str(payload.get("assertionId") or "").strip()
        if assertion_id and self.db.fetch_one(
            'SELECT assertionId FROM "Assertion" WHERE assertionId=:id', {"id": assertion_id}
        ):
            return {"assertionId": assertion_id, "merged": False, "created": False}

        candidates = self.db.fetch_all(
            'SELECT assertionId, rationale FROM "Assertion" WHERE assertedById=:by '
            'AND targetPeriodId=:period AND subjectId=:subject AND predicate=:predicate AND scopeId=:scope '
            'ORDER BY assertionId',
            {"by": payload["assertedById"], "period": payload["targetPeriodId"],
             "subject": payload["subjectId"], "predicate": predicate, "scope": payload["scopeId"]},
        )
        for candidate in candidates:
            if similarity(payload.get("rationale"), candidate.get("rationale")) >= SIMILARITY_THRESHOLD:
                with self.db.engine.begin() as conn:
                    self.db.update("Assertion", candidate["assertionId"], {
                        "rationale": payload.get("rationale"),
                        "assertedAt": payload["assertedAt"],
                    }, conn, actor=actor)
                return {"assertionId": candidate["assertionId"], "merged": True, "created": False}

        with self.db.engine.begin() as conn:
            new_id = assertion_id or self.db.next_id("Assertion", "AST", conn)
            row = {
                "assertionId": new_id,
                "assertionType": assertion_type,
                "subjectType": subject_type,
                "subjectId": payload["subjectId"],
                "predicate": predicate,
                "objectValue": payload.get("objectValue"),
                "objectUnitId": payload.get("objectUnitId"),
                "rangeLow": payload.get("rangeLow"),
                "rangeHigh": payload.get("rangeHigh"),
                "targetPeriodId": payload["targetPeriodId"],
                "scopeId": payload["scopeId"],
                "assertedByType": by_type,
                "assertedById": payload["assertedById"],
                "assertedAt": payload["assertedAt"],
                "horizon": horizon,
                "rationale": payload.get("rationale"),
                "stanceStrength": stance,
                "status": payload.get("status") or SERIES_STATUS,
            }
            self.db.insert("Assertion", row, conn, actor=actor, upsert=True)
        return {"assertionId": new_id, "merged": False, "created": True}

    def upsert_link(self, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        required = ["fromSeriesId", "toSeriesId", "transmissionType", "samplePeriodId", "regimeNote"]
        missing = [field for field in required if not str(payload.get(field) or "").strip()]
        if missing:
            raise MarketError(f"价格传导缺少必填字段：{', '.join(missing)}")
        if payload["fromSeriesId"] == payload["toSeriesId"]:
            raise MarketError("价格传导上下游序列不得相同", 409)

        transmission = self._require_enum("TransmissionType", payload["transmissionType"], "transmissionType")
        self._require_row("IndicatorSeries", "seriesId", payload["fromSeriesId"], "上游序列")
        self._require_row("IndicatorSeries", "seriesId", payload["toSeriesId"], "下游序列")
        self._require_row("TimePeriod", "periodId", payload["samplePeriodId"], "样本期间")

        link_id = str(payload.get("linkId") or "").strip()
        if link_id and self.db.fetch_one(
            'SELECT linkId FROM "PriceTransmissionLink" WHERE linkId=:id', {"id": link_id}
        ):
            return {"linkId": link_id, "created": False}

        with self.db.engine.begin() as conn:
            new_id = link_id or self.db.next_id("PriceTransmissionLink", "LNK-PT", conn)
            row = {
                "linkId": new_id,
                "fromSeriesId": payload["fromSeriesId"],
                "toSeriesId": payload["toSeriesId"],
                "transmissionType": transmission,
                "elasticity": payload.get("elasticity"),
                "lagDays": payload.get("lagDays"),
                "samplePeriodId": payload["samplePeriodId"],
                "regimeNote": payload["regimeNote"],
            }
            self.db.insert("PriceTransmissionLink", row, conn, actor=actor, upsert=True)
        return {"linkId": new_id, "created": True}

    # ---------- 状态机 ----------

    @staticmethod
    def _date_only(value: Any) -> date | None:
        text_value = str(value or "").strip()
        if not text_value:
            return None
        try:
            return date.fromisoformat(text_value[:10])
        except ValueError:
            return None

    @staticmethod
    def _month_end(year: int, month: int) -> date:
        return date(year, month, calendar.monthrange(year, month)[1])

    def latest_as_of(self) -> str:
        columns = self.db.columns.get("TimePeriod") or []
        if "endDate" in columns:
            row = self.db.fetch_one('SELECT MAX("endDate") AS endDate FROM "TimePeriod"') or {}
            parsed = self._date_only(row.get("endDate"))
            if parsed:
                return parsed.isoformat()
        row = self.db.fetch_one('SELECT MAX("periodId") AS periodId FROM "TimePeriod"') or {}
        period = str(row.get("periodId") or "")[:7]
        if re.fullmatch(r"\d{4}-\d{2}", period):
            year, month = (int(part) for part in period.split("-"))
            return self._month_end(year, month).isoformat()
        return date.today().isoformat()

    def _as_of_date(self, as_of: Any = None) -> date:
        text_value = str(as_of or "").strip()
        if not text_value:
            return date.today()
        if re.fullmatch(r"\d{4}-\d{2}", text_value):
            year, month = (int(part) for part in text_value.split("-"))
            return self._month_end(year, month)
        parsed = self._date_only(text_value)
        if not parsed:
            raise MarketError(f"asOf 取值不合法：{as_of}")
        return parsed

    @classmethod
    def _visible_by(cls, value: Any, as_of_date: date) -> bool:
        parsed = cls._date_only(value)
        return parsed is not None and parsed <= as_of_date

    def event_state(self, event: dict[str, Any], as_of: str | None = None) -> str:
        current = self._as_of_date(as_of)
        announce = self._date_only(event.get("announceTime"))
        effective_from = self._date_only(event.get("effectiveFrom")) or self._date_only(event.get("eventTime"))
        effective_to = self._date_only(event.get("effectiveTo"))
        if announce is None or announce > current:
            return EVENT_PENDING
        if effective_to is not None and effective_to < current:
            return EVENT_EXPIRED
        if effective_from is not None and effective_from <= current:
            return EVENT_EFFECTIVE
        return EVENT_ANNOUNCED

    @staticmethod
    def event_state_label(state: str) -> str:
        return EVENT_STATE_LABELS.get(state, state)

    def impact_triggered(self, link: dict[str, Any], as_of: str | None = None) -> bool:
        current = self._as_of_date(as_of)
        source_type = str(link.get("sourceType") or "").upper()
        if source_type == "POLICY":
            return True
        if source_type != "EVENT":
            return False
        event = self.db.fetch_one(
            'SELECT * FROM "Event" WHERE "eventId"=:id', {"id": link.get("sourceId")}
        )
        if not event or self.event_state(event, current.isoformat()) != EVENT_EFFECTIVE:
            return False
        base = self._date_only(event.get("effectiveFrom")) or self._date_only(event.get("eventTime"))
        if base is None:
            return False
        lag = link.get("lagDays")
        return current >= base + timedelta(days=int(lag or 0))

    # ---------- 查询 ----------

    def overview(self, as_of: str | None = None) -> dict[str, Any]:
        def count(sql: str) -> int:
            row = self.db.fetch_one(sql) or {}
            return int(row.get("total") or 0)

        as_of_date = self._as_of_date(as_of)
        visible_points = [
            row for row in self.db.fetch_all('SELECT periodId, publishedAt FROM "PriceDataPoint"')
            if self._visible_by(row.get("publishedAt"), as_of_date)
        ]
        visible_events = [
            row for row in self.db.fetch_all('SELECT announceTime FROM "Event"')
            if self._visible_by(row.get("announceTime"), as_of_date)
        ]
        visible_assertions = [
            row
            for row in self.db.fetch_all('SELECT assertedAt FROM "Assertion" WHERE status=\'有效\'')
            if self._visible_by(row.get("assertedAt"), as_of_date)
        ]
        latest_period = max(
            (row["periodId"] for row in visible_points), default=None
        )
        periods = self.db.fetch_all(
            'SELECT periodId, label, startDate, endDate FROM "TimePeriod" ORDER BY periodId'
        )
        return {
            "seriesCount": count('SELECT COUNT(*) AS total FROM "IndicatorSeries" WHERE status=\'有效\''),
            "pointCount": len(visible_points),
            "eventCount": len(visible_events),
            "assertionCount": len(visible_assertions),
            "factorCount": count('SELECT COUNT(*) AS total FROM "Factor" WHERE status=\'ACTIVE\''),
            "transmissionCount": count('SELECT COUNT(*) AS total FROM "PriceTransmissionLink"'),
            "scopeCount": count('SELECT COUNT(*) AS total FROM "OntologyScope" WHERE scopeType=\'SERIES_SCOPE\''),
            "latestPeriod": latest_period,
            "asOf": as_of_date.isoformat(),
            "periods": periods,
            "counts": {
                "commodityForms": count('SELECT COUNT(*) AS total FROM "CommodityForm"'),
                "places": count('SELECT COUNT(*) AS total FROM "MarketPlace"'),
                "impacts": count('SELECT COUNT(*) AS total FROM "ImpactLink"'),
            },
            "scopeDimensions": ["COMMODITY", "GEOGRAPHY", "UNIT_BASIS", "TAX_BASIS", "CHANNEL"],
            "priceBasisValues": sorted(self._enum_values("PriceBasis")),
        }

    def list_series(self) -> dict[str, Any]:
        rows = self.db.fetch_all(
            'SELECT s.*, i.indicatorName AS indicatorName, u.unitCode AS unitCode, '
            'COALESCE(fc.commodityName, c.commodityName) AS commodityName, src.sourceName AS sourceName '
            'FROM "IndicatorSeries" s '
            'LEFT JOIN "Indicator" i ON i.indicatorId=s.indicatorId '
            'LEFT JOIN "UnitOfMeasure" u ON u.unitId=s.unitId '
            'LEFT JOIN "CmdCommodity" c ON c.commodityId=s.commodityFormId '
            'LEFT JOIN "CommodityForm" cf ON cf.formId=s.commodityFormId '
            'LEFT JOIN "CmdCommodity" fc ON fc.commodityId=cf.commodityId '
            'LEFT JOIN "StatisticalSource" src ON src.sourceId=s.statisticalSourceId '
            'ORDER BY s.seriesId'
        )
        regions = {row["regionId"]: row["regionName"] for row in self.db.fetch_all('SELECT regionId, regionName FROM "Region"')}
        items = []
        for row in rows:
            scope = self.scope.scope(row["scopeId"]) if row.get("scopeId") else None
            geography = self._member(scope, "GEOGRAPHY") if scope else None
            latest = self.db.fetch_one(
                'SELECT periodId, value FROM "PriceDataPoint" WHERE seriesId=:id ORDER BY periodId DESC LIMIT 1',
                {"id": row["seriesId"]},
            ) or {}
            items.append({
                "seriesId": row["seriesId"],
                "indicatorId": row["indicatorId"],
                "indicatorName": row.get("indicatorName"),
                "commodityFormId": row.get("commodityFormId"),
                "commodityName": row.get("commodityName"),
                "regionId": geography["objectId"] if geography else None,
                "regionLabel": regions.get(geography["objectId"]) if geography else None,
                "scopeId": row.get("scopeId"),
                "scopeCode": scope["scopeCode"] if scope else None,
                "priceBasis": row.get("priceBasis"),
                "taxBasis": row.get("taxBasis"),
                "currencyCode": row.get("currencyCode"),
                "unitId": row.get("unitId"),
                "unitCode": row.get("unitCode"),
                "frequency": row.get("frequency"),
                "statisticalSourceId": row.get("statisticalSourceId"),
                "sourceName": row.get("sourceName"),
                "startPeriodId": row.get("startPeriodId"),
                "endPeriodId": row.get("endPeriodId"),
                "latestPeriod": latest.get("periodId"),
                "latestValue": latest.get("value"),
                "status": row.get("status"),
            })
        return {"items": items, "total": len(items)}

    def series_detail(self, series_id: str, as_of: str | None = None) -> dict[str, Any]:
        as_of_date = self._as_of_date(as_of)
        row = self.db.fetch_one(
            'SELECT s.*, i.indicatorName AS indicatorName, u.unitCode AS unitCode, '
            'COALESCE(fc.commodityName, c.commodityName) AS commodityName, src.sourceName AS sourceName '
            'FROM "IndicatorSeries" s '
            'LEFT JOIN "Indicator" i ON i.indicatorId=s.indicatorId '
            'LEFT JOIN "UnitOfMeasure" u ON u.unitId=s.unitId '
            'LEFT JOIN "CmdCommodity" c ON c.commodityId=s.commodityFormId '
            'LEFT JOIN "CommodityForm" cf ON cf.formId=s.commodityFormId '
            'LEFT JOIN "CmdCommodity" fc ON fc.commodityId=cf.commodityId '
            'LEFT JOIN "StatisticalSource" src ON src.sourceId=s.statisticalSourceId '
            'WHERE s.seriesId=:id',
            {"id": series_id},
        )
        if not row:
            raise MarketError(f"指标序列不存在：{series_id}", 404)

        scope = self.scope.scope(row["scopeId"]) if row.get("scopeId") else None
        points = [
            point
            for point in self.db.fetch_all(
                'SELECT * FROM "PriceDataPoint" WHERE seriesId=:id ORDER BY periodId',
                {"id": series_id},
            )
            if self._visible_by(point.get("publishedAt"), as_of_date)
        ]
        events = [
            event
            for event in self.db.fetch_all(
                'SELECT e.*, t.typeName AS typeName, t.eventCategory AS eventCategory, '
                't.defaultImpactDirection AS defaultImpactDirection, t.isPolicyTool AS isPolicyTool '
                'FROM "Event" e LEFT JOIN "EventType" t ON t.eventTypeId=e.eventTypeId '
                'WHERE e.scopeId=:scope ORDER BY e.eventTime',
                {"scope": row.get("scopeId")},
            )
            if self._visible_by(event.get("announceTime"), as_of_date)
        ]
        links = self.db.fetch_all(
            'SELECT * FROM "ImpactLink" WHERE "sourceType"=\'EVENT\' ORDER BY "linkId"'
        )
        for event in events:
            state = self.event_state(event, as_of_date.isoformat())
            event["state"] = state
            event["stateLabel"] = self.event_state_label(state)
            event["triggeredImpactCount"] = sum(
                1
                for link in links
                if link.get("sourceId") == event["eventId"]
                and self.impact_triggered(link, as_of_date.isoformat())
            )
        assertions = [
            assertion
            for assertion in self.db.fetch_all(
                'SELECT * FROM "Assertion" WHERE subjectId=:id ORDER BY targetPeriodId, assertionId',
                {"id": series_id},
            )
            if self._visible_by(assertion.get("assertedAt"), as_of_date)
        ]

        form = None
        if row.get("commodityFormId"):
            form_row = self.db.fetch_one(
                'SELECT * FROM "CommodityForm" WHERE "formId"=:id',
                {"id": row["commodityFormId"]},
            )
            if form_row:
                form = {
                    "formId": form_row["formId"],
                    "formName": form_row.get("formName"),
                    "formType": form_row.get("formType"),
                    "commodityId": form_row.get("commodityId"),
                    "commodityName": row.get("commodityName"),
                    "derivedFromFormId": form_row.get("derivedFromFormId"),
                    "standardCode": form_row.get("standardCode"),
                    "grade": form_row.get("grade"),
                    "status": form_row.get("status"),
                }

        place = None
        channel = self._member(scope, "CHANNEL") if scope else None
        if channel:
            place_row = self.db.fetch_one(
                'SELECT * FROM "MarketPlace" WHERE "placeId"=:id',
                {"id": channel["objectId"]},
            )
            if place_row:
                place = {
                    "placeId": place_row["placeId"],
                    "placeName": place_row.get("placeName"),
                    "marketType": place_row.get("marketType"),
                    "regionId": place_row.get("regionId"),
                    "exchangeCode": place_row.get("exchangeCode"),
                    "tradingHours": place_row.get("tradingHours"),
                    "status": place_row.get("status"),
                }

        factors = self.db.fetch_all(
            'SELECT d.*, f.factorName AS factorName, f.factorCategory AS factorCategory '
            'FROM "FactorDriveLink" d LEFT JOIN "Factor" f ON f.factorId=d.factorId '
            'WHERE d.seriesId=:id ORDER BY d.factorId',
            {"id": series_id},
        )
        commodity_id = form["commodityId"] if form else row.get("commodityFormId")
        impacts = (
            self.list_impacts(target_type="COMMODITY", target_id=commodity_id, as_of=as_of)["items"]
            if commodity_id else []
        )

        evidence_map: dict[str, list[str]] = {}
        for evidence in self.db.fetch_all(
            'SELECT "evidenceId","targetId" FROM "Evidence" WHERE "targetType"=\'Assertion\' '
            'ORDER BY "evidenceId"'
        ):
            evidence_map.setdefault(evidence["targetId"], []).append(evidence["evidenceId"])

        return {
            "asOf": as_of_date.isoformat(),
            "series": {
                "seriesId": row["seriesId"],
                "indicatorId": row["indicatorId"],
                "indicatorName": row.get("indicatorName"),
                "commodityFormId": row.get("commodityFormId"),
                "commodityName": row.get("commodityName"),
                "formName": (form or {}).get("formName") or row.get("commodityName"),
                "priceBasis": row.get("priceBasis"),
                "taxBasis": row.get("taxBasis"),
                "currencyCode": row.get("currencyCode"),
                "unitId": row.get("unitId"),
                "unitCode": row.get("unitCode"),
                "frequency": row.get("frequency"),
                "sourceName": row.get("sourceName"),
                "startPeriodId": row.get("startPeriodId"),
                "endPeriodId": row.get("endPeriodId"),
                "status": row.get("status"),
                "remark": row.get("remark"),
            },
            "commodityForm": form,
            "marketPlace": place,
            "scope": scope,
            "points": points,
            "events": events,
            "factors": factors,
            "impacts": impacts,
            "assertions": self._group_assertions(assertions, evidence_map),
        }

    def list_forms(self, commodity_id: str | None = None) -> dict[str, Any]:
        sql = (
            'SELECT "formId","formName","formType","commodityId","derivedFromFormId",'
            '"standardCode","grade","status" FROM "CommodityForm" WHERE 1=1'
        )
        params: dict[str, Any] = {}
        if commodity_id:
            sql += ' AND "commodityId"=:commodity'
            params["commodity"] = commodity_id
        sql += ' ORDER BY "formId"'
        items = self.db.fetch_all(sql, params)
        return {"total": len(items), "items": items}

    def list_places(self, market_type: str | None = None) -> dict[str, Any]:
        sql = (
            'SELECT p."placeId" AS placeId, p."placeName" AS placeName, p."marketType" AS marketType, '
            'p."regionId" AS regionId, COALESCE(r."regionName", p."regionId") AS regionName, '
            'p."exchangeCode" AS exchangeCode, p."tradingHours" AS tradingHours, p."status" AS status '
            'FROM "MarketPlace" p LEFT JOIN "Region" r ON r."regionId"=p."regionId" WHERE 1=1'
        )
        params: dict[str, Any] = {}
        if market_type:
            sql += ' AND p."marketType"=:market'
            params["market"] = market_type
        sql += ' ORDER BY p."placeId"'
        items = self.db.fetch_all(sql, params)
        return {"total": len(items), "items": items}

    def list_evidence(self, target_type: str | None = None, target_id: str | None = None) -> dict[str, Any]:
        if not target_type or not target_id:
            raise MarketError("targetType 与 targetId 必填", 400)
        items = self.db.fetch_all(
            'SELECT e."evidenceId" AS evidenceId, e."targetType" AS targetType, e."targetId" AS targetId, '
            'e."documentId" AS documentId, d."title" AS documentTitle, e."quote" AS quote, '
            'e."quoteHash" AS quoteHash, e."pageNo" AS pageNo, e."evidenceType" AS evidenceType, '
            'e."strength" AS strength, e."verifiedAt" AS verifiedAt '
            'FROM "Evidence" e LEFT JOIN "SourceDoc" d ON d."docId"=e."documentId" '
            'WHERE e."targetType"=:targetType AND e."targetId"=:targetId ORDER BY e."evidenceId"',
            {"targetType": target_type, "targetId": target_id},
        )
        return {"total": len(items), "items": items}

    def _impact_row(self, link: dict[str, Any], as_of_date: date) -> dict[str, Any]:
        source_type = link.get("sourceType")
        source_name = None
        event_state = None
        event_state_label = None
        if source_type == "EVENT":
            event = self.db.fetch_one(
                'SELECT * FROM "Event" WHERE "eventId"=:id', {"id": link.get("sourceId")}
            )
            if event:
                source_name = event.get("eventTitle")
                event_state = self.event_state(event, as_of_date.isoformat())
                event_state_label = self.event_state_label(event_state)
        elif source_type == "POLICY":
            policy = self.db.fetch_one(
                'SELECT "policyTitle" AS policyTitle FROM "Policy" WHERE "policyId"=:id',
                {"id": link.get("sourceId")},
            )
            if policy:
                source_name = policy.get("policyTitle")
            # 政策来源无时态，eventState 保持 None

        target_type = link.get("targetType")
        target_name = None
        if target_type == "COMMODITY":
            commodity = self.db.fetch_one(
                'SELECT "commodityName" AS commodityName FROM "CmdCommodity" WHERE "commodityId"=:id',
                {"id": link.get("targetId")},
            )
            target_name = commodity.get("commodityName") if commodity else None
        elif target_type == "FACTOR":
            factor = self.db.fetch_one(
                'SELECT "factorName" AS factorName FROM "Factor" WHERE "factorId"=:id',
                {"id": link.get("targetId")},
            )
            target_name = factor.get("factorName") if factor else None

        evidence = self.db.fetch_one(
            'SELECT COUNT(*) AS total FROM "Evidence" WHERE "targetType"=\'ImpactLink\' AND "targetId"=:id',
            {"id": link["linkId"]},
        ) or {}
        direction = link.get("impactDirection")
        return {
            "linkId": link["linkId"],
            "sourceType": source_type,
            "sourceId": link.get("sourceId"),
            "sourceName": source_name,
            "targetType": target_type,
            "targetId": link.get("targetId"),
            "targetName": target_name,
            "direction": direction,
            "impactDirection": direction,
            "impactMagnitude": link.get("impactMagnitude"),
            "lagDays": link.get("lagDays"),
            "mechanism": link.get("mechanism"),
            "assertedByType": link.get("assertedByType"),
            "assertedById": link.get("assertedById"),
            "evidenceCount": int(evidence.get("total") or 0),
            "eventState": event_state,
            "eventStateLabel": event_state_label,
            "triggered": self.impact_triggered(link, as_of_date.isoformat()),
        }

    def list_impacts(
        self,
        source_type: str | None = None,
        source_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        sql = 'SELECT * FROM "ImpactLink" WHERE 1=1'
        params: dict[str, Any] = {}
        if source_type:
            sql += ' AND "sourceType"=:sourceType'
            params["sourceType"] = source_type
        if source_id:
            sql += ' AND "sourceId"=:sourceId'
            params["sourceId"] = source_id
        if target_type:
            sql += ' AND "targetType"=:targetType'
            params["targetType"] = target_type
        if target_id:
            sql += ' AND "targetId"=:targetId'
            params["targetId"] = target_id
        sql += ' ORDER BY "linkId"'
        as_of_date = self._as_of_date(as_of)
        items = [self._impact_row(link, as_of_date) for link in self.db.fetch_all(sql, params)]
        return {"asOf": as_of_date.isoformat(), "total": len(items), "items": items}

    def factor_detail(self, factor_id: str, as_of: str | None = None) -> dict[str, Any]:
        factor = self._require_row("Factor", "factorId", factor_id, "因子")
        drives = self.db.fetch_all(
            'SELECT d."seriesId" AS seriesId, s."indicatorId" AS indicatorId, '
            'i."indicatorName" AS indicatorName, sc."label" AS seriesLabel, '
            'd."elasticity" AS elasticity, d."lagDays" AS lagDays, '
            'd."samplePeriodId" AS samplePeriodId '
            'FROM "FactorDriveLink" d '
            'LEFT JOIN "IndicatorSeries" s ON s."seriesId"=d."seriesId" '
            'LEFT JOIN "Indicator" i ON i."indicatorId"=s."indicatorId" '
            'LEFT JOIN "OntologyScope" sc ON sc."scopeId"=s."scopeId" '
            'WHERE d."factorId"=:id ORDER BY d."seriesId"',
            {"id": factor_id},
        )
        events = self.db.fetch_all(
            'SELECT e."eventId" AS eventId, e."eventTitle" AS eventTitle '
            'FROM "EventFactorLink" l LEFT JOIN "Event" e ON e."eventId"=l."eventId" '
            'WHERE l."factorId"=:id ORDER BY e."eventTime"',
            {"id": factor_id},
        )
        measured_by = None
        if factor.get("measuredByIndicatorId"):
            measured_by = self.db.fetch_one(
                'SELECT * FROM "Indicator" WHERE "indicatorId"=:id',
                {"id": factor["measuredByIndicatorId"]},
            )
        return {
            "factor": factor,
            "drives": drives,
            "events": events,
            "impacts": self.list_impacts(target_type="FACTOR", target_id=factor_id, as_of=as_of)["items"],
            "measuredBy": measured_by,
        }

    def timeline(self, from_period: str | None = None, to_period: str | None = None,
                 category: str | None = None, as_of: str | None = None) -> dict[str, Any]:
        start = self._period_date(from_period, "startDate")
        end = self._period_date(to_period, "endDate")
        sql = (
            'SELECT e.*, t.typeName AS typeName, t.eventCategory AS eventCategory, '
            't.defaultImpactDirection AS defaultImpactDirection, t.isPolicyTool AS isPolicyTool, '
            't.toolVerb AS toolVerb '
            'FROM "Event" e LEFT JOIN "EventType" t ON t.eventTypeId=e.eventTypeId WHERE 1=1'
        )
        params: dict[str, Any] = {}
        if start:
            sql += " AND e.eventTime>=:start"
            params["start"] = start
        if end:
            sql += " AND e.eventTime<=:end"
            params["end"] = end
        if category:
            sql += " AND t.eventCategory=:category"
            params["category"] = category
        sql += " ORDER BY e.eventTime, e.eventId"
        items = self.db.fetch_all(sql, params)
        as_of_date = self._as_of_date(as_of)
        items = [item for item in items if self._visible_by(item.get("announceTime"), as_of_date)]
        links = self.db.fetch_all(
            'SELECT * FROM "ImpactLink" WHERE "sourceType"=\'EVENT\' ORDER BY "linkId"'
        )
        for item in items:
            state = self.event_state(item, as_of_date.isoformat())
            item["state"] = state
            item["stateLabel"] = self.event_state_label(state)
            event_links = [link for link in links if link.get("sourceId") == item["eventId"]]
            item["triggeredImpactCount"] = sum(
                1 for link in event_links if self.impact_triggered(link, as_of_date.isoformat())
            )
        return {"asOf": as_of_date.isoformat(), "items": items, "total": len(items)}

    def assertions(self, subject_id: str | None = None, as_of: str | None = None) -> dict[str, Any]:
        sql = 'SELECT * FROM "Assertion" WHERE status=\'有效\''
        params: dict[str, Any] = {}
        if subject_id:
            sql += " AND subjectId=:subject"
            params["subject"] = subject_id
        sql += " ORDER BY subjectId, targetPeriodId, predicate, assertionId"
        as_of_date = self._as_of_date(as_of)
        rows = [
            row
            for row in self.db.fetch_all(sql, params)
            if self._visible_by(row.get("assertedAt"), as_of_date)
        ]
        evidence_map: dict[str, list[str]] = {}
        for evidence in self.db.fetch_all(
            'SELECT "evidenceId","targetId" FROM "Evidence" WHERE "targetType"=\'Assertion\' '
            'ORDER BY "evidenceId"'
        ):
            evidence_map.setdefault(evidence["targetId"], []).append(evidence["evidenceId"])
        groups = self._group_assertions(rows, evidence_map)
        return {"asOf": as_of_date.isoformat(), "items": groups, "total": len(groups)}

    def transmission_graph(self) -> dict[str, Any]:
        links = self.db.fetch_all(
            'SELECT l.*, fi.indicatorName AS fromLabel, ti.indicatorName AS toLabel, '
            'fu.unitCode AS fromUnit, tu.unitCode AS toUnit '
            'FROM "PriceTransmissionLink" l '
            'LEFT JOIN "IndicatorSeries" fs ON fs.seriesId=l.fromSeriesId '
            'LEFT JOIN "IndicatorSeries" ts ON ts.seriesId=l.toSeriesId '
            'LEFT JOIN "Indicator" fi ON fi.indicatorId=fs.indicatorId '
            'LEFT JOIN "Indicator" ti ON ti.indicatorId=ts.indicatorId '
            'LEFT JOIN "UnitOfMeasure" fu ON fu.unitId=fs.unitId '
            'LEFT JOIN "UnitOfMeasure" tu ON tu.unitId=ts.unitId '
            'ORDER BY l.linkId'
        )
        nodes: dict[str, dict[str, Any]] = {}
        edges = []
        for row in links:
            nodes.setdefault(row["fromSeriesId"], {
                "id": row["fromSeriesId"], "label": row.get("fromLabel") or row["fromSeriesId"],
                "unitCode": row.get("fromUnit"),
            })
            nodes.setdefault(row["toSeriesId"], {
                "id": row["toSeriesId"], "label": row.get("toLabel") or row["toSeriesId"],
                "unitCode": row.get("toUnit"),
            })
            edges.append({
                "linkId": row["linkId"],
                "from": row["fromSeriesId"],
                "to": row["toSeriesId"],
                "transmissionType": row["transmissionType"],
                "elasticity": row.get("elasticity"),
                "lagDays": row.get("lagDays"),
                "samplePeriodId": row["samplePeriodId"],
                "regimeNote": row.get("regimeNote"),
            })
        return {"nodes": list(nodes.values()), "edges": edges}

    def _period_date(self, period_id: str | None, field: str) -> str | None:
        if not period_id:
            return None
        row = self._require_row("TimePeriod", "periodId", period_id, "时间期间")
        return row.get(field)

    def _group_assertions(
        self, rows: Iterable[dict[str, Any]], evidence: dict[str, list[str]] | None = None
    ) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row["subjectId"], row["targetPeriodId"], row["predicate"])
            group = groups.setdefault(key, {
                "subjectId": row["subjectId"],
                "targetPeriodId": row["targetPeriodId"],
                "predicate": row["predicate"],
                "stance": self._stance(row["predicate"]),
                "scopeIds": [],
                "items": [],
            })
            if row["scopeId"] not in group["scopeIds"]:
                group["scopeIds"].append(row["scopeId"])
            group["items"].append({
                "assertionId": row["assertionId"],
                "assertedByType": row["assertedByType"],
                "assertedById": row["assertedById"],
                "objectValue": row.get("objectValue"),
                "objectUnitId": row.get("objectUnitId"),
                "rangeLow": row.get("rangeLow"),
                "rangeHigh": row.get("rangeHigh"),
                "rationale": row.get("rationale"),
                "assertedAt": row.get("assertedAt"),
                "horizon": row.get("horizon"),
                "stanceStrength": row.get("stanceStrength"),
                "status": row.get("status"),
                "evidenceIds": list((evidence or {}).get(row["assertionId"], [])),
            })
        return list(groups.values())

    @staticmethod
    def _stance(predicate: str) -> str:
        if predicate in BULLISH_PREDICATES:
            return "看多"
        if predicate in BEARISH_PREDICATES:
            return "看空"
        return "中性"

    # ---------- 阶段 4 门禁 ----------

    @staticmethod
    def _gate_failure(code: str, reason: str, target: str, fix: str) -> None:
        raise MarketError(f"{code} 失败：{reason}；{target}；修复：{fix}")

    def _series_commodity(self, series_row: dict[str, Any]) -> str | None:
        scope = self.scope.scope(series_row["scopeId"]) if series_row.get("scopeId") else None
        if not scope:
            return None
        member = self._member(scope, "COMMODITY")
        if not member:
            return None
        if member["objectType"] == "CommodityForm":
            form = self.db.fetch_one(
                'SELECT "commodityId" AS commodityId FROM "CommodityForm" WHERE "formId"=:id',
                {"id": member["objectId"]},
            )
            return form["commodityId"] if form else None
        return member["objectId"]

    @staticmethod
    def _month_span(start: str, end: str) -> list[str]:
        try:
            year, month = (int(part) for part in str(start)[:7].split("-"))
            end_year, end_month = (int(part) for part in str(end)[:7].split("-"))
        except (TypeError, ValueError):
            return []
        result: list[str] = []
        while (year, month) <= (end_year, end_month) and len(result) < 240:
            result.append(f"{year:04d}-{month:02d}")
            month += 1
            if month > 12:
                month = 1
                year += 1
        return result

    def _evidence_count(self, target_type: str, target_id: str) -> int:
        row = self.db.fetch_one(
            'SELECT COUNT(*) AS total FROM "Evidence" WHERE "targetType"=:type AND "targetId"=:id',
            {"type": target_type, "id": target_id},
        ) or {}
        return int(row.get("total") or 0)

    def validate_stage4(self) -> dict[str, Any]:
        gates: dict[str, bool] = {}

        # G4-1 形态回填完整且品种一致
        for row in self.db.fetch_all('SELECT * FROM "IndicatorSeries"'):
            series_id = row["seriesId"]
            if series_id in LEGACY_FORM_EXEMPT:
                continue
            form_id = row.get("commodityFormId")
            form = self.db.fetch_one(
                'SELECT "formId","commodityId" FROM "CommodityForm" WHERE "formId"=:id',
                {"id": form_id},
            ) if form_id else None
            if not form:
                self._gate_failure(
                    "G4-1", "指标序列缺少有效的形态引用", series_id,
                    "补齐 IndicatorSeries.commodityFormId 并指向 CommodityForm",
                )
            commodity = self._series_commodity(row)
            if commodity and form["commodityId"] != commodity:
                self._gate_failure(
                    "G4-1", f"形态品种 {form['commodityId']} 与范围品种 {commodity} 不一致", series_id,
                    "统一 Scope 的 COMMODITY 成员与形态所属品种",
                )
        gates["G4-1"] = True

        # G4-2 序列范围必须有且仅有一个有效渠道
        for scope_row in self.db.fetch_all(
            'SELECT "scopeId","scopeCode","members" FROM "OntologyScope" WHERE "scopeType"=:type',
            {"type": SERIES_SCOPE},
        ):
            try:
                members = json.loads(scope_row.get("members") or "[]")
            except json.JSONDecodeError:
                members = []
            channels = [
                item for item in members
                if str(item.get("dimensionCode") or "").upper() == "CHANNEL"
            ]
            if len(channels) != 1:
                self._gate_failure(
                    "G4-2", f"CHANNEL 维度成员数为 {len(channels)}", scope_row["scopeId"],
                    "为序列范围补一个 MarketPlace 渠道成员",
                )
            place = self.db.fetch_one(
                'SELECT "placeId" FROM "MarketPlace" WHERE "placeId"=:id',
                {"id": channels[0].get("objectId")},
            )
            if not place:
                self._gate_failure(
                    "G4-2", f"渠道市场 {channels[0].get('objectId')} 不存在", scope_row["scopeId"],
                    "补建 MarketPlace 或修正 CHANNEL 成员",
                )
        gates["G4-2"] = True

        # G4-3 影响边证据、目标类型与强度口径
        for link in self.db.fetch_all('SELECT * FROM "ImpactLink"'):
            link_id = link["linkId"]
            if not self._evidence_count("ImpactLink", link_id):
                self._gate_failure(
                    "G4-3", "影响边缺少证据", link_id, "补充 targetType=ImpactLink 的 Evidence",
                )
            if str(link.get("sourceType") or "").upper() in {"EVENT", "POLICY"} and \
                    str(link.get("targetType") or "").upper() == "SERIES":
                self._gate_failure(
                    "G4-3", "事件/政策影响边不得直接指向序列", link_id,
                    "改为指向 Factor 或 Commodity",
                )
            if link.get("impactMagnitude") not in (None, "") and \
                    str(link.get("assertedByType") or "").upper() != "MARKET_PACK":
                self._gate_failure(
                    "G4-3", "给出影响强度时须由 MARKET_PACK 断言", link_id,
                    "清空 impactMagnitude 或改 assertedByType=MARKET_PACK",
                )
        gates["G4-3"] = True

        # G4-4 驱动边完整性
        for drive in self.db.fetch_all('SELECT * FROM "FactorDriveLink"'):
            if drive.get("elasticity") is None and drive.get("lagDays") is None:
                self._gate_failure(
                    "G4-4", "因子驱动边弹性与滞后同时为空", f"{drive['factorId']}->{drive['seriesId']}",
                    "至少填写 elasticity 或 lagDays",
                )
            if not self.db.fetch_one(
                'SELECT 1 AS ok FROM "Factor" WHERE "factorId"=:id', {"id": drive["factorId"]}
            ):
                self._gate_failure(
                    "G4-4", "因子不存在", drive["factorId"], "补建 Factor",
                )
            if not self.db.fetch_one(
                'SELECT 1 AS ok FROM "IndicatorSeries" WHERE "seriesId"=:id', {"id": drive["seriesId"]}
            ):
                self._gate_failure(
                    "G4-4", "指标序列不存在", drive["seriesId"], "修正 FactorDriveLink.seriesId",
                )
        for link in self.db.fetch_all('SELECT * FROM "EventFactorLink"'):
            if not self.db.fetch_one(
                'SELECT 1 AS ok FROM "Event" WHERE "eventId"=:id', {"id": link["eventId"]}
            ):
                self._gate_failure("G4-4", "事件不存在", link["eventId"], "补建 Event")
            if not self.db.fetch_one(
                'SELECT 1 AS ok FROM "Factor" WHERE "factorId"=:id', {"id": link["factorId"]}
            ):
                self._gate_failure("G4-4", "因子不存在", link["factorId"], "补建 Factor")
        gates["G4-4"] = True

        # G4-5 有效断言必须有证据
        for assertion in self.db.fetch_all(
            'SELECT "assertionId" AS assertionId FROM "Assertion" WHERE "status"=\'有效\''
        ):
            if not self._evidence_count("Assertion", assertion["assertionId"]):
                self._gate_failure(
                    "G4-5", "有效断言缺少证据", assertion["assertionId"],
                    "补充 targetType=Assertion 的 Evidence",
                )
        gates["G4-5"] = True

        # G4-6 日期区间、滞后与版本
        for event in self.db.fetch_all('SELECT * FROM "Event"'):
            start = self._date_only(event.get("effectiveFrom"))
            end = self._date_only(event.get("effectiveTo"))
            if start and end and start > end:
                self._gate_failure(
                    "G4-6", "生效区间起止倒置", event["eventId"], "修正 effectiveFrom/effectiveTo",
                )
        for table, code in (("ImpactLink", "linkId"), ("FactorDriveLink", "factorId"), ("EventFactorLink", "eventId")):
            for row in self.db.fetch_all(f'SELECT * FROM "{table}"'):
                lag = row.get("lagDays")
                if lag is not None and int(lag) < 0:
                    self._gate_failure("G4-6", "lagDays 为负", f"{table}:{row.get(code)}", "改为非负整数")
                version = row.get("version")
                if version is None or int(version) < 1:
                    self._gate_failure("G4-6", "version 小于 1", f"{table}:{row.get(code)}", "version 从 1 起")
        gates["G4-6"] = True

        # G4-7 证据完整性与幂等键
        documents = {
            row["docId"] for row in self.db.fetch_all('SELECT "docId" FROM "SourceDoc"')
        }
        seen: set[tuple[str, str, str]] = set()
        for evidence in self.db.fetch_all('SELECT * FROM "Evidence"'):
            evidence_id = evidence["evidenceId"]
            quote_text = str(evidence.get("quote") or "")
            if not 1 <= len(quote_text) <= 200:
                self._gate_failure("G4-7", "引文字符数不在 1—200", evidence_id, "调整 quote 长度")
            expected = hashlib.sha1(quote_text.encode("utf-8")).hexdigest()
            if str(evidence.get("quoteHash") or "") != expected:
                self._gate_failure("G4-7", "quoteHash 与 quote 不匹配", evidence_id, "按 sha1(quote) 重算")
            # 文档注册表为空（未导入业务种子）时跳过存在性校验，避免空库误报
            if documents and str(evidence.get("documentId") or "") not in documents:
                self._gate_failure(
                    "G4-7", "来源文档不存在", f"{evidence_id}->{evidence.get('documentId')}",
                    "补建 SourceDoc 或修正 documentId",
                )
            key = (
                str(evidence.get("targetType") or ""),
                str(evidence.get("targetId") or ""),
                str(evidence.get("quoteHash") or ""),
            )
            if key in seen:
                self._gate_failure(
                    "G4-7", "同一目标存在重复证据", f"{key[0]}:{key[1]}", "去重或更换引文",
                )
            seen.add(key)
        gates["G4-7"] = True

        # G4-8 月度序列连续性与口径一致
        for row in self.db.fetch_all(
            'SELECT * FROM "IndicatorSeries" WHERE "frequency"=\'MONTH\''
        ):
            series_id = row["seriesId"]
            scope = self.scope.scope(row["scopeId"]) if row.get("scopeId") else None
            if not scope:
                self._gate_failure("G4-8", "序列范围不存在", series_id, "补建 Scope")
            unit_member = self._member(scope, "UNIT_BASIS")
            tax_member = self._member(scope, "TAX_BASIS")
            if not unit_member or unit_member["objectId"] != row.get("unitId"):
                self._gate_failure(
                    "G4-8", "单位与范围 UNIT_BASIS 成员不一致", series_id, "对齐 unitId 与 Scope 成员",
                )
            if not tax_member or tax_member["objectId"] != row.get("taxBasis"):
                self._gate_failure(
                    "G4-8", "税基与范围 TAX_BASIS 成员不一致", series_id, "对齐 taxBasis 与 Scope 成员",
                )
            unit = self.db.fetch_one(
                'SELECT "currencyCode" AS currencyCode FROM "UnitOfMeasure" WHERE "unitId"=:id',
                {"id": row.get("unitId")},
            )
            if not unit or str(unit.get("currencyCode") or "").upper() != str(row.get("currencyCode") or "").upper():
                self._gate_failure(
                    "G4-8", "币种与计量单位声明不一致", series_id, "对齐 currencyCode 与 UnitOfMeasure",
                )
            expected: list[str] = []
            if row.get("startPeriodId") and row.get("endPeriodId"):
                expected = self._month_span(row["startPeriodId"], row["endPeriodId"])
            actual = {
                point["periodId"] for point in self.db.fetch_all(
                    'SELECT "periodId" FROM "PriceDataPoint" WHERE "seriesId"=:id',
                    {"id": series_id},
                )
            }
            if not expected:
                expected = sorted(actual)
            missing = [period for period in expected if period not in actual]
            if missing:
                self._gate_failure(
                    "G4-8", f"月度数据缺口 {','.join(missing[:6])}", series_id,
                    "补齐期间内全部月度数据点",
                )
        gates["G4-8"] = True

        return {
            "gates": gates,
            "counts": {
                "series": len(self.db.fetch_all('SELECT "seriesId" FROM "IndicatorSeries"')),
                "commodityForms": len(self.db.fetch_all('SELECT "formId" FROM "CommodityForm"')),
                "places": len(self.db.fetch_all('SELECT "placeId" FROM "MarketPlace"')),
                "evidence": len(self.db.fetch_all('SELECT "evidenceId" FROM "Evidence"')),
                "impactLinks": len(self.db.fetch_all('SELECT "linkId" FROM "ImpactLink"')),
                "factorDrives": len(self.db.fetch_all('SELECT * FROM "FactorDriveLink"')),
                "eventFactors": len(self.db.fetch_all('SELECT * FROM "EventFactorLink"')),
                "assertions": len(self.db.fetch_all(
                    'SELECT "assertionId" FROM "Assertion" WHERE "status"=\'有效\''
                )),
            },
        }
