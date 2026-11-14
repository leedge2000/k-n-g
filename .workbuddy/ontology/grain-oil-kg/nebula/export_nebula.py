# -*- coding: utf-8 -*-
"""
SQLite → NebulaGraph 导入 CSV 导出器

用法:
    python export_nebula.py <sqlite_db> [out_dir]

输出:
    <out_dir>/tags/<tag>.csv    首列为 :VID，取自主表/子表内部主键而非业务编码
    <out_dir>/edges/<edge>.csv  首两列为 :SRC_VID,:DST_VID，第三列 :RANK

导入:
    nebula-importer --config nebulaimporter.yaml
"""
import csv
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.environ.get("ONT_ENGINE", r"c:\Users\Lenovo\.trae-cn\skills\ontology-app-builder\engine")
ONT_YAML = os.environ.get("ONT_YAML", os.path.join(HERE, "..", "yaml"))
sys.path.insert(0, ENGINE)

from loader import OntologyModel  # noqa: E402

# 聚合根/子实体 -> Nebula Tag
TAG_MAP = {
    "SourceDoc": "source_doc", "Segment": "segment",
    "CrsSource": "crawl_source", "CrawlRun": "crawl_run",
    "Policy": "policy", "Clause": "policy_clause",
    "Indicator": "indicator", "IvdData": "indval",
    "YrbYearbook": "yearbook", "Intel": "intel",
    "CmdCommodity": "commodity", "Product": "product",
    "Region": "region", "Enterprise": "enterprise",
    "Concept": "concept", "Synonym": "synonym",
    "Relation": "kg_relation", "QisRecord": "quality_record",
    "KtcCategory": "knowledge_category",
}

# (源聚合根, 外键属性, 目标聚合根) -> 边类型
EDGE_MAP = {
    ("SourceDoc", "crawlSourceId"): "produced_by_crawl",
    ("SourceDoc", "knowledgeCategoryId"): "in_category",
    ("Policy", "sourceDocId"): "from_doc",
    ("Policy", "regionId"): "applies_to_region",
    ("Policy", "knowledgeCategoryId"): "in_category",
    ("IvdData", "indicatorId"): "of_indicator",
    ("IvdData", "regionId"): "in_region",
    ("IvdData", "commodityId"): "of_commodity",
    ("IvdData", "sourceDocId"): "from_doc",
    ("IvdData", "knowledgeCategoryId"): "in_category",
    ("YrbYearbook", "indicatorId"): "of_indicator",
    ("YrbYearbook", "regionId"): "in_region",
    ("YrbYearbook", "commodityId"): "of_commodity",
    ("YrbYearbook", "sourceDocId"): "from_doc",
    ("YrbYearbook", "knowledgeCategoryId"): "in_category",
    ("Intel", "regionId"): "in_region",
    ("Intel", "commodityId"): "of_commodity",
    ("Intel", "sourceDocId"): "from_doc",
    ("Intel", "knowledgeCategoryId"): "in_category",
    ("CmdCommodity", "parentCommodityId"): "parent_of",
    ("CmdCommodity", "conceptId"): "maps_to_concept",
    ("Product", "commodityId"): "processed_from",
    ("Product", "enterpriseId"): "made_by",
    ("Region", "parentRegionId"): "parent_of",
    ("Enterprise", "regionId"): "located_in",
    ("Concept", "parentConceptId"): "parent_of",
    ("Relation", "sourceDocId"): "from_doc",
    ("CrsSource", "knowledgeCategoryId"): "in_category",
    ("Indicator", "knowledgeCategoryId"): "in_category",
    ("KtcCategory", "parentCategoryId"): "parent_of",
}

TYPE_MAP = {"Integer": int, "Decimal": float, "Money": float, "Boolean": int}


def val(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def ensure(p):
    os.makedirs(p, exist_ok=True)


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "data", "domain.db")
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "csv")
    tags_dir, edges_dir = os.path.join(out, "tags"), os.path.join(out, "edges")
    ensure(tags_dir)
    ensure(edges_dir)

    model = OntologyModel(os.path.abspath(ONT_YAML))
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    counts = {}

    # ---------- 主表 → Tag
    for a in model.aggregates:
        tag = TAG_MAP[a["alias"]]
        table = model.table_name(a)
        pk = model.pk_column(a)
        cols = [c for c in model.columns(a)]
        attrs = [c["name"] for c in cols]
        rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
        path = os.path.join(tags_dir, f"{tag}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([":VID"] + attrs)
            for r in rows:
                w.writerow([val(r[pk])] + [val(r[c]) for c in attrs])
        counts[tag] = len(rows)

        # 子实体 → Tag + 关系边
        for e in a.get("entities", []):
            etag = TAG_MAP[e["alias"]]
            etable = f"{table}_{e['alias']}"
            erows = conn.execute(f'SELECT * FROM "{etable}"').fetchall()
            eattrs = [c["name"] for c in e["attributes"]]
            epk = e.get("localId") or eattrs[0]
            with open(os.path.join(tags_dir, f"{etag}.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([":VID", pk] + eattrs)
                for r in erows:
                    w.writerow([val(r[epk]), val(r[pk])] + [val(r[c]) for c in eattrs])
            counts[etag] = len(erows)

            edge = {"Segment": "has_segment", "Clause": "has_clause",
                    "Synonym": "has_synonym", "CrawlRun": "has_run"}[e["alias"]]
            with open(os.path.join(edges_dir, f"{edge}.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([":SRC_VID", ":DST_VID", ":RANK"])
                for r in erows:
                    w.writerow([val(r[pk]), val(r[epk]), 0])
            counts[edge] = len(erows)

    # ---------- 外键 → 边
    buckets = {}
    for a in model.aggregates:
        for c in model.columns(a):
            if not c.get("ref"):
                continue
            key = (a["alias"], c["name"])
            edge = EDGE_MAP.get(key)
            if not edge:
                print(f"  [WARN] 未定义边映射: {key}")
                continue
            buckets.setdefault(edge, []).append((model.table_name(a), model.pk_column(a), c["name"], key))
    for edge, specs in buckets.items():
        rows_out = []
        for table, pk, fk, key in specs:
            for r in conn.execute(f'SELECT "{pk}","{fk}" FROM "{table}" WHERE "{fk}" IS NOT NULL AND "{fk}"<>\'\''):
                rows_out.append((val(r[0]), val(r[1])))
        with open(os.path.join(edges_dir, f"{edge}.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([":SRC_VID", ":DST_VID", ":RANK"])
            for i, (s, d) in enumerate(rows_out):
                w.writerow([s, d, 0])
        counts[edge] = len(rows_out)

    # ---------- 知识关系三元组 → kg_relation 边
    triple = conn.execute(
        "SELECT relationId, subjectEntityId, objectEntityId, predicate, confidence, status "
        "FROM Relation WHERE status='已发布'").fetchall()
    with open(os.path.join(edges_dir, "kg_relation.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([":SRC_VID", ":DST_VID", ":RANK", "relationId", "predicate", "confidence"])
        for i, r in enumerate(triple):
            w.writerow([val(r[1]), val(r[2]), i, val(r[0]), val(r[3]), val(r[4])])
    counts["kg_relation"] = len(triple)

    conn.close()
    print(f"导出完成 → {out}")
    for k, v in counts.items():
        print(f"  {k:<20} {v}")


if __name__ == "__main__":
    main()
