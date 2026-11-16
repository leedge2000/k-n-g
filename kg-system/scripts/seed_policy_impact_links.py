"""为一级政策补齐 POLICY→COMMODITY 影响链与原文证据（一次性数据作业）。

边界
- 只写 ImpactLink / Evidence / ChangeLog，不动政策、条款与既有 EVENT 链路；
- 写库前先做 sqlite 在线备份，全部写入在同一事务内完成；
- 方向只表达「对 5.3 影响变量」的方向，不推断价格涨跌，强度统一留空（首期只输出方向）；
- assertedBy / verifiedBy 必须写 AI-Agent:DeepSeek-V4.1-Flash，不冒充人工专家复核。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("data/grain_oil.db")
BACKUP_DIR = Path("data/backups")
AGENT = "AI-Agent:DeepSeek-V4.1-Flash"

# (linkId, policyId, commodityId, direction, lagDays, clauseId, mechanism)
LINKS = [
    (
        "LNK-IM-0005", "POL0005", "CMD0005", "POSITIVE", 0, "POL0005-C001",
        "粮食产量稳定目标锁定播种面积与供给预期，对「产量、供给」变量为正向；不据此判断价格涨跌。",
    ),
    (
        "LNK-IM-0006", "POL0005", "CMD0004", "POSITIVE", None, "POL0005-C002",
        "巩固提升大豆产能、扩大油料多元化供给，对「产量、供给」变量为正向；不据此判断价格涨跌。",
    ),
    (
        "LNK-IM-0007", "POL0008", "CMD0005", "POSITIVE", None, "POL0008-C003",
        "粮食与谷物播种面积目标约束稳定生产基础，对「产量、供给」变量为正向；不据此判断价格涨跌。",
    ),
    (
        "LNK-IM-0008", "POL0008", "CMD0004", "POSITIVE", None, "POL0008-C009",
        "高油高产大豆育种与产业化应用提升单产与品质，对「产量、供给」变量为正向；不据此判断价格涨跌。",
    ),
    (
        "LNK-IM-0009", "POL0009", "CMD0005", "POSITIVE", None, "POL0009-C001",
        "高标准农田建设提升耕地基础产能，对「产能（耕地基础）」变量为正向；建设分期释放，未给出量化时滞。",
    ),
    (
        "LNK-IM-0010", "POL0010", "CMD0005", "NEGATIVE", 0, "POL0010-C003",
        "引导粮食适度加工、提高出品率，降低单位成品原粮消耗，对「需求（原粮消耗）」变量为负向；不据此判断价格涨跌。",
    ),
    (
        "LNK-IM-0011", "POL0011", "CMD0005", "POSITIVE", None, "POL0011-C001",
        "耕地保有量与永久基本农田保护目标约束耕地数量底线，对「产能（耕地基础）」变量为正向；不据此判断价格涨跌。",
    ),
]

# linkId → (evidenceId, documentId)
EVIDENCE = {
    "LNK-IM-0005": ("OBJ-PUB-ED-0011", "SOU0014"),
    "LNK-IM-0006": ("OBJ-PUB-ED-0012", "SOU0014"),
    "LNK-IM-0007": ("OBJ-PUB-ED-0013", "SOU0017"),
    "LNK-IM-0008": ("OBJ-PUB-ED-0014", "SOU0017"),
    "LNK-IM-0009": ("OBJ-PUB-ED-0015", "SOU0018"),
    "LNK-IM-0010": ("OBJ-PUB-ED-0016", "SOU0019"),
    "LNK-IM-0011": ("OBJ-PUB-ED-0017", "SOU0020"),
}

# 既有 POLICY 链路补正：种子里的示例quote换成条款原文
LEGACY = {
    "linkId": "LNK-IM-0004",
    "evidenceId": "OBJ-PUB-ED-0010",
    "clauseId": "POL0001-C002",
    "documentId": "SOU0001",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def quote_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def find_segment(conn: sqlite3.Connection, doc_id: str, text: str) -> str | None:
    rows = conn.execute(
        'SELECT "segmentId","segmentText" FROM "SourceDoc_Segment" WHERE "docId"=? ORDER BY "segmentNo"',
        (doc_id,),
    ).fetchall()
    for segment_id, segment_text in rows:
        if text and text in str(segment_text or ""):
            return segment_id
    return None


def backup(conn: sqlite3.Connection) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"grain_oil_policy_impact_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    with sqlite3.connect(target) as dest:
        conn.backup(dest)
    return target


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    stamp = now_iso()

    clauses = {
        row["clauseId"]: dict(row)
        for row in conn.execute('SELECT * FROM "Policy_Clause"')
    }
    policies = {
        row["policyId"]: dict(row)
        for row in conn.execute('SELECT * FROM "Policy"')
    }
    commodities = {
        row["commodityId"]: dict(row)
        for row in conn.execute('SELECT * FROM "CmdCommodity"')
    }

    # 先校验，任何一处对不上都不写库
    for link_id, policy_id, commodity_id, direction, _, clause_id, _ in LINKS:
        assert policy_id in policies, f"政策不存在：{policy_id}"
        assert commodity_id in commodities, f"品种不存在：{commodity_id}"
        assert clause_id in clauses, f"条款不存在：{clause_id}"
        assert direction in {"POSITIVE", "NEGATIVE"}, direction
        assert clauses[clause_id]["policyId"] == policy_id, f"{clause_id} 不属于 {policy_id}"
        quote = str(clauses[clause_id]["clauseText"] or "")
        assert quote and len(quote) <= 200, f"{clause_id} 引文长度不合规：{len(quote)}"

    legacy_clause = clauses[LEGACY["clauseId"]]
    assert legacy_clause["policyId"] == "POL0001"
    assert conn.execute(
        'SELECT 1 FROM "ImpactLink" WHERE "linkId"=? AND "sourceType"=\'POLICY\' AND "sourceId"=\'POL0001\'',
        (LEGACY["linkId"],),
    ).fetchone()

    existing_links = {
        row[0] for row in conn.execute('SELECT "linkId" FROM "ImpactLink"')
    }
    existing_evidence = {
        row[0] for row in conn.execute('SELECT "evidenceId" FROM "Evidence"')
    }
    for link_id, *_ in LINKS:
        assert link_id not in existing_links, f"链路已存在：{link_id}"
    for evidence_id, _ in EVIDENCE.values():
        assert evidence_id not in existing_evidence, f"证据已存在：{evidence_id}"

    backup_path = backup(conn)
    print(f"备份完成：{backup_path}")

    try:
        with conn:
            for link_id, policy_id, commodity_id, direction, lag_days, clause_id, mechanism in LINKS:
                quote = str(clauses[clause_id]["clauseText"])
                conn.execute(
                    'INSERT INTO "ImpactLink" ("linkId","sourceType","sourceId","targetType","targetId",'
                    '"impactDirection","impactMagnitude","lagDays","mechanism","assertedByType",'
                    '"assertedById","version","status","createdBy","createdAt","updatedBy","updatedAt") '
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        link_id, "POLICY", policy_id, "COMMODITY", commodity_id, direction, None,
                        lag_days, mechanism, "AI_AGENT", AGENT, 1, "有效", AGENT, stamp, AGENT, stamp,
                    ),
                )
                evidence_id, doc_id = EVIDENCE[link_id]
                segment_id = find_segment(conn, doc_id, quote)
                conn.execute(
                    'INSERT INTO "Evidence" ("evidenceId","targetType","targetId","documentId","segmentId",'
                    '"quote","quoteHash","pageNo","evidenceType","strength","verifiedBy","verifiedAt",'
                    '"createdBy","createdAt","updatedBy","updatedAt") '
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        evidence_id, "ImpactLink", link_id, doc_id, segment_id, quote, quote_hash(quote),
                        None, "CITED", 0.0, AGENT, stamp, AGENT, stamp, AGENT, stamp,
                    ),
                )
                payload = {
                    "policyId": policy_id,
                    "commodityId": commodity_id,
                    "clauseId": clause_id,
                    "impactDirection": direction,
                    "impactVariableNote": "方向仅指对5.3影响变量，不代表价格涨跌",
                    "strength": "依据不足",
                    "quote": quote,
                    "segmentId": segment_id,
                    "assertedById": AGENT,
                    "boundary": "AI 代理裁定，非人工专家复核",
                }
                conn.execute(
                    'INSERT INTO "ChangeLog" ("changeId","entityType","entityId","changeType","updatedAt","payload") '
                    "VALUES (?,?,?,?,?,?)",
                    (f"CL-IMPACT-{link_id}", "ImpactLink", link_id, "create", stamp,
                     json.dumps(payload, ensure_ascii=False)),
                )
                conn.execute(
                    'INSERT INTO "ChangeLog" ("changeId","entityType","entityId","changeType","updatedAt","payload") '
                    "VALUES (?,?,?,?,?,?)",
                    (f"CL-EVIDENCE-{evidence_id}", "Evidence", evidence_id, "create", stamp,
                     json.dumps(payload, ensure_ascii=False)),
                )
                print(f"{link_id} {policy_id}->{commodity_id} {direction} clause={clause_id} segment={segment_id}")

            # 既有 POL0001 → 稻谷：把示例引文替换为条款原文，方向与机制同步补正
            legacy_quote = str(legacy_clause["clauseText"])
            legacy_segment = find_segment(conn, LEGACY["documentId"], legacy_quote)
            conn.execute(
                'UPDATE "Evidence" SET "quote"=?,"quoteHash"=?,"documentId"=?,"segmentId"=?,"strength"=0.0,'
                '"verifiedBy"=?,"verifiedAt"=?,"updatedBy"=?,"updatedAt"=? WHERE "evidenceId"=?',
                (
                    legacy_quote, quote_hash(legacy_quote), LEGACY["documentId"], legacy_segment,
                    AGENT, stamp, AGENT, stamp, LEGACY["evidenceId"],
                ),
            )
            conn.execute(
                'UPDATE "ImpactLink" SET "mechanism"=?,"updatedBy"=?,"updatedAt"=? WHERE "linkId"=?',
                (
                    "最低收购价对稻谷价格形成下限支撑，作用于「价格」变量；强度仍标注依据不足。",
                    AGENT, stamp, LEGACY["linkId"],
                ),
            )
            conn.execute(
                'INSERT INTO "ChangeLog" ("changeId","entityType","entityId","changeType","updatedAt","payload") '
                "VALUES (?,?,?,?,?,?)",
                (
                    "CL-IMPACT-FIX-LNK-IM-0004", "ImpactLink", LEGACY["linkId"], "update", stamp,
                    json.dumps({
                        "clauseId": LEGACY["clauseId"],
                        "evidenceId": LEGACY["evidenceId"],
                        "quote": legacy_quote,
                        "segmentId": legacy_segment,
                        "assertedById": AGENT,
                        "boundary": "示例引文替换为条款原文；AI 代理裁定，非人工专家复核",
                    }, ensure_ascii=False),
                ),
            )
        print("事务提交成功")
    except Exception:
        conn.rollback()
        raise

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    policy_links = conn.execute(
        'SELECT COUNT(*) FROM "ImpactLink" WHERE "sourceType"=\'POLICY\''
    ).fetchone()[0]
    covered = conn.execute(
        'SELECT COUNT(DISTINCT "sourceId") FROM "ImpactLink" WHERE "sourceType"=\'POLICY\''
    ).fetchone()[0]
    orphan = conn.execute(
        'SELECT COUNT(*) FROM "ImpactLink" l WHERE l."sourceType"=\'POLICY\' AND NOT EXISTS ('
        'SELECT 1 FROM "Evidence" e WHERE e."targetType"=\'ImpactLink\' AND e."targetId"=l."linkId")'
    ).fetchone()[0]
    print(f"integrity={integrity} fkErrors={len(fk_errors)} policyLinks={policy_links} "
          f"coveredPolicies={covered} linksWithoutEvidence={orphan}")
    conn.close()


if __name__ == "__main__":
    main()
