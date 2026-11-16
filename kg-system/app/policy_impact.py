"""第五层业务主线 · 政策影响链（只读）

实现需求 5.7 的两个查询：
- ``impacts``：品种 × 地区 × 时点的当前政策影响清单；
- ``policy_targets``：单条政策的影响对象清单。

边界（与需求文档一致）
1. 时效判定全部委托 ``PolicyService.status_map``，不在本模块重复实现，避免同一政策在不同接口给出不同答案；
2. 首期强度只输出「依据不足」，不输出数值区间（区间口径见 5.6，暂不启用）；
3. 每条方向都必须能回指政策条款原文与原文链接；缺证据的方向一律降为「依据不足」。

数据现状说明：``Enterprise`` 主数据为空，查询二的企业清单返回空集并附注，不虚构企业。
"""
from __future__ import annotations

from typing import Any

from app.db import Database
from app.policy import PolicyService, target_day_checked

DIRECTION_LABELS = {"POSITIVE": "正向", "NEGATIVE": "负向"}
DIRECTION_UNKNOWN = "依据不足"
DIRECTION_LABELS_ALL = ("正向", "负向", "依据不足")
STRENGTH_LABEL = "依据不足"
STRENGTH_BASIS = "首期只输出方向，不做强度量化"

# 5.3 政策工具分类 → 主要影响变量。measureType 取自 Policy_Clause，按语义归类。
TOOL_IMPACT_VARIABLE = {
    "最低收购价": "价格",
    "价格调控": "价格",
    "收储调控": "价格、库存",
    "产能目标": "产量、供给",
    "生产结构": "产量、供给",
    "油料供给": "产量、供给",
    "种业创新": "产量、供给",
    "收益保障": "成本、利润",
    "农田建设": "产能（耕地基础）",
    "建设目标": "产能（耕地基础）",
    "耕地保护": "产能（耕地基础）",
    "耕地保护目标": "产能（耕地基础）",
    "节粮减损": "需求（原粮消耗）",
    "节约目标": "需求（原粮消耗）",
    "机收减损": "需求（原粮消耗）",
    "加工减损": "需求（原粮消耗）",
    "用途管制": "供给能力（耕地数量与质量）",
    "空间禁限": "供给能力（耕地数量与质量）",
    "设施保护": "供给能力（耕地数量与质量）",
    "污染防治": "供给能力（耕地数量与质量）",
    "认定管理": "供给能力（耕地数量与质量）",
    "质量管理": "供给能力（耕地数量与质量）",
    "占补平衡": "供给能力（耕地数量与质量）",
    "黑土地保护": "供给能力（耕地数量与质量）",
    "责任落实": "供给能力（耕地数量与质量）",
    "责任考核": "供给能力（耕地数量与质量）",
    "监测预警": "监测与合规",
    "统计调查": "监测与合规",
    "违法惩治": "监测与合规",
    "食堂管理": "监测与合规",
    "宣传监管": "监测与合规",
}


class PolicyImpactError(ValueError):
    """影响链查询失败；status 对应 HTTP 语义（400 参数 / 404 不存在）。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class PolicyImpactService:
    def __init__(self, db: Database, policy_service: PolicyService):
        self.db = db
        self.policy_service = policy_service

    # ---------- 查询一：品种 × 地区 × 时点 ----------

    def impacts(self, commodity_id: str, region_id: str | None = None,
                as_of: str | None = None) -> dict[str, Any]:
        commodity = self._commodity(commodity_id)
        day = target_day_checked(as_of)
        as_of_text = day.isoformat()
        regions = self._regions()
        query_region = (region_id or "").strip() or None
        if query_region and query_region not in regions:
            raise PolicyImpactError(f"地区不存在：{query_region}", 404)

        verdicts = self.policy_service.status_map(as_of_text)
        policies = {
            str(row["policyId"]): row
            for row in self.db.fetch_all('SELECT * FROM "Policy" ORDER BY "policyId"')
        }
        clauses = self._clauses()
        links = self._policy_links()

        items: list[dict[str, Any]] = []
        for policy_id in sorted(policies):
            if verdicts.get(policy_id) != "CURRENT":
                continue
            policy = policies[policy_id]
            if not self._region_covers(policy.get("regionId"), query_region, regions):
                continue
            own_links = [
                link for link in links
                if link["sourceId"] == policy_id and link["targetType"] == "COMMODITY"
                and link["targetId"] == commodity["commodityId"]
            ]
            if own_links:
                for link in own_links:
                    items.append(self._link_item(link, policy, commodity, clauses, regions))
                continue
            # 无影响链但条款明确点名该品种：方向依据不足，原文仍可回溯
            matched = [
                clause for clause in clauses
                if clause["policyId"] == policy_id
                and self._mentions(clause.get("targetObject"), commodity)
            ]
            if matched:
                items.append(self._clause_item(matched[0], policy, commodity, regions))

        items.sort(key=lambda item: (item.get("effectiveDate") or "", item["policyId"]), reverse=True)
        counts = {label: 0 for label in DIRECTION_LABELS_ALL}
        for item in items:
            counts[item["direction"]] = counts.get(item["direction"], 0) + 1
        notes: list[str] = []
        if not items:
            notes.append(
                f"截至 {as_of_text}，{commodity['commodityName']} 没有现行有效的政策影响链，方向判为「依据不足」。"
            )
        return {
            "asOf": as_of_text,
            "commodity": {
                "commodityId": commodity["commodityId"],
                "commodityCode": commodity.get("commodityCode"),
                "commodityName": commodity.get("commodityName"),
                "commodityCategory": commodity.get("commodityCategory"),
            },
            "regionId": query_region,
            "regionName": regions.get(query_region, {}).get("regionName") if query_region else None,
            "total": len(items),
            "directionCounts": counts,
            "summaryDirection": self._summary_direction(items),
            "strength": STRENGTH_LABEL,
            "strengthBasis": STRENGTH_BASIS,
            "items": items,
            "notes": notes,
        }

    # ---------- 查询二：单条政策的影响对象 ----------

    def policy_targets(self, policy_id: str, as_of: str | None = None) -> dict[str, Any]:
        policy = self.db.fetch_one(
            'SELECT * FROM "Policy" WHERE "policyId"=:id', {"id": policy_id}
        )
        if not policy:
            raise PolicyImpactError(f"政策不存在：{policy_id}", 404)
        day = target_day_checked(as_of)
        as_of_text = day.isoformat()
        regions = self._regions()
        verdict = self.policy_service.status_map(as_of_text).get(str(policy["policyId"]), "UNPUBLISHED")
        base = {
            "asOf": as_of_text,
            "policy": self._policy_brief(policy, regions),
            "effective": verdict == "CURRENT",
            "effectiveStatus": verdict,
        }
        if verdict != "CURRENT":
            return {
                **base,
                "total": 0,
                "items": [],
                "targets": {"commodities": [], "regions": [], "industryStages": [], "enterpriseTypes": []},
                "enterprises": [],
                "notes": [f"政策在该时点的时效判定为 {verdict}，按需求 5.7 影响链同步失效，不输出对象清单。"],
            }

        clauses = self._clauses()
        links = self._policy_links(policy_id=str(policy["policyId"]))
        items = [self._link_item(link, policy, None, clauses, regions) for link in links]

        commodities: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if item["targetType"] != "COMMODITY" or item["targetId"] in seen:
                continue
            seen.add(item["targetId"])
            commodities.append({
                "commodityId": item["targetId"],
                "commodityName": item["targetName"],
                "direction": item["direction"],
                "directionRaw": item["directionRaw"],
                "toolType": item["toolType"],
                "lagDays": item["lagDays"],
            })

        stage = str(policy.get("industryStage") or "").strip()
        stage_values = [stage] if stage else []
        enterprises, enterprise_types = self._enterprises(str(policy.get("regionId") or ""))
        notes: list[str] = []
        if not items:
            notes.append("该政策尚未建立 ImpactLink 影响链，未输出品种方向。")
        if not enterprises:
            notes.append("企业主数据为空（Enterprise 0 条），未列出具体企业，也不虚构企业类型。")

        return {
            **base,
            "total": len(items),
            "items": items,
            "targets": {
                "commodities": commodities,
                "regions": [
                    {
                        "regionId": policy.get("regionId"),
                        "regionName": regions.get(str(policy.get("regionId") or ""), {}).get("regionName"),
                    }
                ] if policy.get("regionId") else [],
                "industryStages": stage_values,
                "enterpriseTypes": enterprise_types,
            },
            "enterprises": enterprises,
            "notes": notes,
        }

    # ---------- 条目组装 ----------

    def _link_item(self, link: dict[str, Any], policy: dict[str, Any],
                   commodity: dict[str, Any] | None, clauses: list[dict[str, Any]],
                   regions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        evidence = self._evidence_for(str(link.get("linkId") or ""))
        clause = self._clause_for(str(policy["policyId"]), evidence, clauses)
        if clause is None:
            clause = self._first_clause(str(policy["policyId"]), clauses)
        measure_type = str(clause.get("measureType") or "").strip() if clause else ""

        primary = evidence[0] if evidence else None
        quote = str(primary.get("quote") or "").strip() if primary else ""
        if not quote and clause:
            quote = str(clause.get("clauseText") or "")
        document_id = (primary.get("documentId") if primary else None) or policy.get("sourceDocId")
        source_link = self._source_link(document_id)

        direction = DIRECTION_LABELS.get(str(link.get("impactDirection") or "").upper(), DIRECTION_UNKNOWN)
        basis = "evidence"
        if not primary:
            direction = DIRECTION_UNKNOWN
            basis = "clause-only" if quote else "no-evidence"

        return {
            "policyId": policy["policyId"],
            "policyTitle": policy.get("policyTitle"),
            "docNumber": policy.get("docNumber"),
            "effectiveDate": policy.get("effectiveDate"),
            "policyStatus": policy.get("policyStatus"),
            "industryStage": policy.get("industryStage"),
            "regionId": policy.get("regionId"),
            "regionName": regions.get(str(policy.get("regionId") or ""), {}).get("regionName"),
            "targetType": link.get("targetType"),
            "targetId": link.get("targetId"),
            "targetName": commodity.get("commodityName") if commodity else self._target_name(link),
            "toolType": measure_type or None,
            "measureType": measure_type or None,
            "clauseId": clause.get("clauseId") if clause else None,
            "clauseTitle": clause.get("clauseTitle") if clause else None,
            "direction": direction,
            "directionRaw": link.get("impactDirection"),
            "impactVariable": TOOL_IMPACT_VARIABLE.get(measure_type, DIRECTION_UNKNOWN),
            "lagDays": link.get("lagDays"),
            "mechanism": link.get("mechanism"),
            "strength": STRENGTH_LABEL,
            "strengthBasis": STRENGTH_BASIS,
            "quote": quote or None,
            "sourceLink": source_link,
            "documentId": document_id,
            "segmentId": primary.get("segmentId") if primary else None,
            "evidenceCount": len(evidence),
            "evidenceType": primary.get("evidenceType") if primary else None,
            "linkId": link.get("linkId"),
            "assertedByType": link.get("assertedByType"),
            "assertedById": link.get("assertedById"),
            "basis": basis,
        }

    def _clause_item(self, clause: dict[str, Any], policy: dict[str, Any],
                     commodity: dict[str, Any], regions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        measure_type = str(clause.get("measureType") or "").strip()
        return {
            "policyId": policy["policyId"],
            "policyTitle": policy.get("policyTitle"),
            "docNumber": policy.get("docNumber"),
            "effectiveDate": policy.get("effectiveDate"),
            "policyStatus": policy.get("policyStatus"),
            "industryStage": policy.get("industryStage"),
            "regionId": policy.get("regionId"),
            "regionName": regions.get(str(policy.get("regionId") or ""), {}).get("regionName"),
            "targetType": "COMMODITY",
            "targetId": commodity["commodityId"],
            "targetName": commodity.get("commodityName"),
            "toolType": measure_type or None,
            "measureType": measure_type or None,
            "clauseId": clause.get("clauseId"),
            "clauseTitle": clause.get("clauseTitle"),
            "direction": DIRECTION_UNKNOWN,
            "directionRaw": None,
            "impactVariable": TOOL_IMPACT_VARIABLE.get(measure_type, DIRECTION_UNKNOWN),
            "lagDays": None,
            "mechanism": "政策条款涉及该品种，但尚未建立方向性影响链，方向判为「依据不足」。",
            "strength": STRENGTH_LABEL,
            "strengthBasis": STRENGTH_BASIS,
            "quote": str(clause.get("clauseText") or "") or None,
            "sourceLink": self._source_link(policy.get("sourceDocId")),
            "documentId": policy.get("sourceDocId"),
            "segmentId": None,
            "evidenceCount": 0,
            "evidenceType": "CLAUSE",
            "linkId": None,
            "assertedByType": None,
            "assertedById": None,
            "basis": "clause-only",
        }

    # ---------- 基础查询 ----------

    def _commodity(self, commodity_id: str) -> dict[str, Any]:
        key = str(commodity_id or "").strip()
        if not key:
            raise PolicyImpactError("commodityId 不能为空")
        row = self.db.fetch_one(
            'SELECT * FROM "CmdCommodity" WHERE "commodityId"=:id', {"id": key}
        )
        if not row:
            raise PolicyImpactError(f"品种不存在：{key}", 404)
        return row

    def _regions(self) -> dict[str, dict[str, Any]]:
        return {
            str(row["regionId"]): row
            for row in self.db.fetch_all('SELECT * FROM "Region" ORDER BY "regionId"')
        }

    def _clauses(self) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            'SELECT * FROM "Policy_Clause" ORDER BY "policyId", "clauseNo"'
        )

    def _policy_links(self, policy_id: str | None = None) -> list[dict[str, Any]]:
        sql = 'SELECT * FROM "ImpactLink" WHERE "sourceType"=\'POLICY\' AND "status"=\'有效\''
        params: dict[str, Any] = {}
        if policy_id:
            sql += ' AND "sourceId"=:id'
            params["id"] = policy_id
        sql += ' ORDER BY "linkId"'
        return self.db.fetch_all(sql, params)

    def _evidence_for(self, link_id: str) -> list[dict[str, Any]]:
        if not link_id:
            return []
        return self.db.fetch_all(
            'SELECT * FROM "Evidence" WHERE "targetType"=\'ImpactLink\' AND "targetId"=:id '
            'ORDER BY "evidenceId"',
            {"id": link_id},
        )

    def _source_link(self, document_id: Any) -> str | None:
        key = str(document_id or "").strip()
        if not key:
            return None
        row = self.db.fetch_one(
            'SELECT "sourceUrl" AS sourceUrl FROM "SourceDoc" WHERE "docId"=:id', {"id": key}
        )
        return str(row.get("sourceUrl") or "") or None if row else None

    def _target_name(self, link: dict[str, Any]) -> str | None:
        target_type = str(link.get("targetType") or "").upper()
        target_id = str(link.get("targetId") or "")
        if target_type == "COMMODITY":
            row = self.db.fetch_one(
                'SELECT "commodityName" AS name FROM "CmdCommodity" WHERE "commodityId"=:id',
                {"id": target_id},
            )
        elif target_type == "FACTOR":
            row = self.db.fetch_one(
                'SELECT "factorName" AS name FROM "Factor" WHERE "factorId"=:id', {"id": target_id}
            )
        elif target_type == "PRODUCT":
            row = self.db.fetch_one(
                'SELECT "productName" AS name FROM "Product" WHERE "productId"=:id', {"id": target_id}
            )
        else:
            row = None
        return str(row["name"]) if row and row.get("name") else None

    def _enterprises(self, policy_region: str) -> tuple[list[dict[str, Any]], list[str]]:
        rows = self.db.fetch_all('SELECT * FROM "Enterprise" ORDER BY "enterpriseId"')
        scoped = [
            row for row in rows
            if not policy_region or not row.get("regionId") or str(row.get("regionId")) == policy_region
        ]
        enterprises = [
            {
                "enterpriseId": row["enterpriseId"],
                "enterpriseName": row.get("enterpriseName"),
                "enterpriseType": row.get("enterpriseType"),
                "regionId": row.get("regionId"),
            }
            for row in scoped
        ]
        types: list[str] = []
        for row in scoped:
            value = str(row.get("enterpriseType") or "").strip()
            if value and value not in types:
                types.append(value)
        return enterprises, types

    def _policy_brief(self, policy: dict[str, Any], regions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {
            "policyId": policy["policyId"],
            "policyTitle": policy.get("policyTitle"),
            "docNumber": policy.get("docNumber"),
            "policyLevel": policy.get("policyLevel"),
            "issuer": policy.get("issuer"),
            "effectiveDate": policy.get("effectiveDate"),
            "expireDate": policy.get("expireDate"),
            "policyStatus": policy.get("policyStatus"),
            "reviewStatus": policy.get("reviewStatus"),
            "industryStage": policy.get("industryStage"),
            "regionId": policy.get("regionId"),
            "regionName": regions.get(str(policy.get("regionId") or ""), {}).get("regionName"),
            "sourceDocId": policy.get("sourceDocId"),
        }

    # ---------- 语义辅助 ----------

    @staticmethod
    def _clause_for(policy_id: str, evidence: list[dict[str, Any]],
                    clauses: list[dict[str, Any]]) -> dict[str, Any] | None:
        """优先用证据引文反查条款，保证「方向 → 原文」可逐字回切。"""
        own = [clause for clause in clauses if clause["policyId"] == policy_id]
        for row in evidence:
            text = str(row.get("quote") or "").strip()
            if not text:
                continue
            for clause in own:
                body = str(clause.get("clauseText") or "").strip()
                if body and (text == body or text in body or body in text):
                    return clause
        return None

    @staticmethod
    def _first_clause(policy_id: str, clauses: list[dict[str, Any]]) -> dict[str, Any] | None:
        own = [clause for clause in clauses if clause["policyId"] == policy_id]
        return own[0] if own else None

    @staticmethod
    def _mentions(target_object: Any, commodity: dict[str, Any]) -> bool:
        text = str(target_object or "")
        if not text:
            return False
        name = str(commodity.get("commodityName") or "").strip()
        return bool(name) and name in text

    @staticmethod
    def _region_covers(policy_region: Any, query_region: str | None,
                       regions: dict[str, dict[str, Any]]) -> bool:
        policy_key = str(policy_region or "").strip()
        if not query_region or not policy_key:
            return True
        if policy_key == query_region:
            return True
        row = regions.get(policy_key) or {}
        if str(row.get("regionLevel") or "") == "全球":
            return True
        cursor = regions.get(query_region) or {}
        seen: set[str] = set()
        while cursor:
            parent = str(cursor.get("parentRegionId") or "")
            if not parent or parent in seen:
                break
            if parent == policy_key:
                return True
            seen.add(parent)
            cursor = regions.get(parent) or {}
        return False

    @staticmethod
    def _summary_direction(items: list[dict[str, Any]]) -> str:
        if not items:
            return DIRECTION_UNKNOWN
        labels = {item["direction"] for item in items}
        if len(labels) == 1:
            return labels.pop()
        return "方向不一致（见 directionCounts）"
