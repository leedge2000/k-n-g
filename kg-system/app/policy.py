"""阶段 2a · 政策现行有效查询

asOf 语义只在这里实现一次：任何入口要判断「某政策在某时点是否有效」都必须复用本模块，
避免同一问题在不同接口给出不同答案。

判定顺序（命中即返回，不做加权评分）
1. 审核状态不是「已发布」→ UNPUBLISHED，草稿与待审不参与时效判断；
2. 政策状态为已废止/失效 → WITHDRAWN，不看日期，状态是人工确认的结论；
3. effectiveDate 有值但无法解析 → UNKNOWN_DATE，不猜生效时间；
   effectiveDate 为空 → 视为长期有效（自始生效），继续走后续判断；
4. asOf 早于 effectiveDate → NOT_YET_EFFECTIVE；
5. expireDate 早于 asOf → EXPIRED（expireDate 存在但无法解析同样判 UNKNOWN_DATE）；
6. 版本链上存在生效的后继版本 → SUPERSEDED（后继以 supersedesPolicyId 指回本行）；
7. 其余 → CURRENT。

适用范围
- Policy.scopeId 指向 OntologyScope.scopeId；查询时先按范围过滤再判定时效，counts 只统计该范围内的政策。
- 未填 scopeId 的政策视为「不限范围」，按任意范围查询都会保留。
- 命中条件是「政策范围覆盖查询范围」（ScopeService.is_superset），政策只覆盖某省时不会被全国范围查询命中。
- 版本链索引始终用全量政策构建，避免后继版本因范围不同而被漏判。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.db import Database
from app.scope import ScopeService

PUBLISHED_REVIEW_STATUS = "已发布"

# 人工确认过的终态，出现即不再参与时效判断
TERMINAL_POLICY_STATUSES = ("已废止", "失效")

EFFECTIVE_STATUSES = (
    "CURRENT",
    "SUPERSEDED",
    "EXPIRED",
    "NOT_YET_EFFECTIVE",
    "WITHDRAWN",
    "UNPUBLISHED",
    "UNKNOWN_DATE",
)


def default_as_of() -> str:
    """缺省按东八区当天计算，避免服务器 UTC 与业务口径差一天。"""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).date().isoformat()


def parse_date(value: Any) -> date | None:
    """只接受 YYYY-MM-DD（允许带时间后缀），其余一律视为无日期，不推断。"""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


class PolicyService:
    def __init__(self, db: Database, scope_service: ScopeService | None = None):
        self.db = db
        self.scope_service = scope_service

    def effective(self, as_of: str | None = None, limit: int = 200,
                  scope_id: str | None = None) -> dict[str, Any]:
        target = (as_of or "").strip() or default_as_of()
        target_day = parse_date(target)
        if target_day is None or len(target) != 10:
            raise ValueError(f"asOf 必须是 YYYY-MM-DD 日期，收到：{as_of}")

        query_scope = (scope_id or "").strip() or None
        if query_scope:
            if self.scope_service is None:
                raise ValueError("未接入范围注册表，无法按 scopeId 过滤")
            if not self.scope_service.scope(query_scope):
                raise ValueError(f"范围不存在：{query_scope}")

        rows = self.db.fetch_all('SELECT * FROM "Policy" ORDER BY "policyId"')
        # supersedesPolicyId 指回上一版本，按被取代方归组，判断某行是否已有后继生效
        # 版本链必须用全量构建：后继版本可能不在本次范围过滤结果里
        successors: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            parent = row.get("supersedesPolicyId")
            if parent:
                successors.setdefault(str(parent), []).append(row)

        if query_scope:
            rows = [row for row in rows if self._covers(row.get("scopeId"), query_scope)]

        counts = {status: 0 for status in EFFECTIVE_STATUSES}
        current: list[dict[str, Any]] = []
        for row in rows:
            verdict = self._classify(row, target_day, successors.get(str(row["policyId"]), []))
            counts[verdict] += 1
            if verdict == "CURRENT":
                current.append(self._item(row))

        current.sort(key=lambda item: (item["effectiveDate"] or "", item["policyId"]), reverse=True)
        items = current[:limit]
        for index, item in enumerate(items, 1):
            item["citation"] = f"P{index}"
        citation_index = self.citation_index(items)
        return {
            "asOf": target_day.isoformat(),
            "scopeId": query_scope,
            "total": len(current),
            "counts": counts,
            "items": items,
            "citationIndex": citation_index,
        }

    def _covers(self, policy_scope: Any, query_scope: str) -> bool:
        """政策范围为空表示不限范围；否则要求政策范围覆盖查询范围。"""
        policy_scope_id = str(policy_scope or "").strip()
        if not policy_scope_id:
            return True
        if self.scope_service is None:
            return False
        return self.scope_service.is_superset(policy_scope_id, query_scope)

    @staticmethod
    def _classify(row: dict[str, Any], target_day: date, successors: list[dict[str, Any]]) -> str:
        if (row.get("reviewStatus") or "") != PUBLISHED_REVIEW_STATUS:
            return "UNPUBLISHED"
        if (row.get("policyStatus") or "") in TERMINAL_POLICY_STATUSES:
            return "WITHDRAWN"
        raw_effective = row.get("effectiveDate")
        effective = parse_date(raw_effective)
        if raw_effective and str(raw_effective).strip() and effective is None:
            return "UNKNOWN_DATE"
        # 空生效日期视为长期有效（自始生效），不参与「尚未生效」判断
        if effective is not None and effective > target_day:
            return "NOT_YET_EFFECTIVE"
        raw_expire = row.get("expireDate")
        expire = parse_date(raw_expire)
        if raw_expire and str(raw_expire).strip() and expire is None:
            return "UNKNOWN_DATE"
        if expire is not None and expire < target_day:
            return "EXPIRED"
        for successor in successors:
            if (successor.get("reviewStatus") or "") != PUBLISHED_REVIEW_STATUS:
                continue
            successor_effective = parse_date(successor.get("effectiveDate"))
            if successor_effective is not None and successor_effective <= target_day:
                return "SUPERSEDED"
        return "CURRENT"

    @staticmethod
    def _item(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "policyId": row["policyId"],
            "policyTitle": row.get("policyTitle"),
            "docNumber": row.get("docNumber"),
            "policyLevel": row.get("policyLevel"),
            "issuer": row.get("issuer"),
            "effectiveDate": row.get("effectiveDate"),
            "expireDate": row.get("expireDate"),
            "policyStatus": row.get("policyStatus"),
            "reviewStatus": row.get("reviewStatus"),
            "version": row.get("version"),
            "supersedesPolicyId": row.get("supersedesPolicyId"),
            "scopeId": row.get("scopeId"),
            "regionId": row.get("regionId"),
            "sourceDocId": row.get("sourceDocId"),
        }

    @staticmethod
    def citation_index(items: list[dict[str, Any]]) -> dict[str, str]:
        """把 citation 反向映射到 policyId；items 须已赋 citation。"""
        return {
            str(item.get("citation") or ""): str(item["policyId"])
            for item in items
            if item.get("citation")
        }
