"""阶段 1 · 别名表与实体链接冷启动

输入（只读）
    terms/ly_terms.json          100 条术语种子（term / canonical / synonyms）
    kernel/object_types.json     25 个内核类型（typeName / labelZh）
    kernel/enums.json            封闭词表（枚举取值的业务中文名，如「港口」「例外」）
    capability/questions.json    30 条能力问题（仅用于覆盖度评测，不参与建表）

输出
    terms/alias_index.json       归一化表面形式 -> 候选目标

四条约束
1. 归一化只做 NFKC（全角转半角）、casefold、去空白；不做繁简转换与模糊匹配，
   避免把「看起来像」当成「就是」。
2. 归一化后长度 < 2 的表面形式不入索引，单字（油、粮、麦）歧义过高。
3. 同一表面形式指向多个目标时保留全部候选并记入 conflicts，不自动裁决；
   歧义是数据问题，必须回术语种子修，而不是在链接器里赌概率。
4. 不写生成时间戳，保证同一输入重跑的输出逐字节一致（对应门 3 的 G3-2 幂等重跑）。
"""
from __future__ import annotations

import json
import pathlib
import unicodedata
from collections import defaultdict

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
TERMS_PATH = ROOT / "terms" / "ly_terms.json"
TYPES_PATH = ROOT / "kernel" / "object_types.json"
ENUMS_PATH = ROOT / "kernel" / "enums.json"
QUESTIONS_PATH = ROOT / "capability" / "questions.json"
OUT_PATH = ROOT / "terms" / "alias_index.json"

MIN_LENGTH = 2


def normalize(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def load_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def target_key(target: dict) -> str:
    """按业务对象去重，而不是按术语条目去重。

    同一对象允许有多个同义入口（如 玉米 与 苞米 分列两条种子），这类重复是设计意图，
    不算歧义；只有指向不同对象才是需要消歧的真冲突。
    """
    return f'{target["kind"]}:{target.get("targetType")}:{target.get("targetIdHint") or target["label"]}'


def build_entries() -> tuple[dict[str, dict], list[dict], list[dict]]:
    """收集两种来源的所有表面形式，返回 (索引, 真冲突, 同义入口组)。"""
    surface_map: dict[str, dict] = {}
    term_ids_by_surface: dict[str, set[str]] = defaultdict(set)

    def add(surface: str, target: dict, source: str) -> None:
        normalized = normalize(surface)
        if len(normalized) < MIN_LENGTH:
            return
        slot = surface_map.setdefault(normalized, {"surface": surface, "targets": {}})
        # 同一目标可能由多个表面形式命中；保留最短的原始写法做展示
        if len(surface) < len(slot["surface"]):
            slot["surface"] = surface
        key = target_key(target)
        # targetKey 写进产物，下游（实体链接、对齐映射）不必重复推导同一套键规则
        slot["targets"].setdefault(key, {**target, "targetKey": key, "source": source})

    types = load_json(TYPES_PATH)["types"]
    type_labels = {obj["typeName"]: obj["labelZh"] for obj in types}
    type_surfaces = {
        normalize(text) for obj in types for text in (obj["typeName"], obj["labelZh"])
    }
    # 术语种子的 targetType 可能指向枚举而不是对象类型（如 PriceBasis / ProcessType），
    # 这类目标的中文标签只在 enums.json 里，补齐后下游展示才不会退化成英文类型名。
    type_labels.update(
        {name: spec.get("label") or name for name, spec in load_json(ENUMS_PATH)["enums"].items()}
    )

    terms = load_json(TERMS_PATH)["terms"]
    for term in terms:
        target = {
            "kind": "term",
            "targetType": term.get("targetType"),
            "targetIdHint": term.get("targetIdHint"),
            "label": term["canonical"],
            "typeLabel": type_labels.get(term.get("targetType")) or term.get("targetType"),
            "termType": term.get("termType"),
            "domainCode": term.get("domainCode"),
        }
        for surface in [term["term"], term["canonical"], *term.get("synonyms", [])]:
            term_ids_by_surface[normalize(surface)].add(term["termId"])
            add(surface, target, "ly_terms")

    for obj in types:
        target = {
            "kind": "type",
            "targetId": obj["typeName"],
            "targetType": obj["typeName"],
            "label": obj["labelZh"],
            "typeLabel": obj["labelZh"],
            "domainCode": obj.get("domainCode"),
        }
        add(obj["typeName"], target, "object_types")
        add(obj["labelZh"], target, "object_types")

    # 枚举取值的中文名是问句里最常出现的业务词（港口、例外、后果、取代、批发市场），
    # 只补类型名与术语种子会漏掉这一层，导致结构性问题问句零命中。
    for enum_name, spec in load_json(ENUMS_PATH)["enums"].items():
        for item in spec.get("values", []):
            if not isinstance(item, dict) or not item.get("value"):
                continue
            label = item.get("label") or ""
            normalized = normalize(label)
            if len(normalized) < MIN_LENGTH:
                continue
            # ObjectTypeName 大部分取值就是类型名本身，只保留类型标签之外的说法（前提条件、例外）
            if enum_name == "ObjectTypeName" and normalized in type_surfaces:
                continue
            # 术语种子已经把同一枚举值收录成 term 目标时，不再登记重复的 enum 目标
            existing = surface_map.get(normalized, {}).get("targets", {})
            if any(
                t.get("targetType") == enum_name and t.get("targetIdHint") == item["value"]
                for t in existing.values()
            ):
                continue
            add(
                label,
                {
                    "kind": "enum",
                    "targetType": enum_name,
                    "targetIdHint": item["value"],
                    "label": label,
                    "typeLabel": spec.get("label") or enum_name,
                },
                "enums",
            )

    conflicts = []
    duplicates = []
    for normalized, slot in surface_map.items():
        if len(slot["targets"]) > 1:
            conflicts.append({
                "surface": slot["surface"],
                "normalized": normalized,
                "targets": sorted(slot["targets"].values(), key=target_key),
            })
        term_ids = term_ids_by_surface.get(normalized, set())
        if len(term_ids) > 1:
            duplicates.append({
                "surface": slot["surface"],
                "termIds": sorted(term_ids),
                "target": sorted(slot["targets"])[0] if len(slot["targets"]) == 1 else None,
            })
    conflicts.sort(key=lambda c: c["normalized"])
    duplicates.sort(key=lambda d: d["surface"])
    return surface_map, conflicts, duplicates


def scan(text: str, surfaces: list[tuple[str, str]]) -> list[str]:
    """最长优先匹配，命中后跳过该片段，避免「最低收购价」被「收购」重复计入。"""
    normalized = normalize(text)
    hits: list[str] = []
    covered = [False] * len(normalized)
    for normalized_surface, display in surfaces:
        start = 0
        while True:
            pos = normalized.find(normalized_surface, start)
            if pos < 0:
                break
            end = pos + len(normalized_surface)
            if not any(covered[pos:end]):
                hits.append(display)
                for i in range(pos, end):
                    covered[i] = True
            start = pos + 1
    return hits


def evaluate(surface_map: dict[str, dict]) -> list[dict]:
    questions = load_json(QUESTIONS_PATH)["questions"]
    surfaces = sorted(
        ((normalized, slot["surface"]) for normalized, slot in surface_map.items()),
        key=lambda item: (-len(item[0]), item[0]),
    )
    report = []
    for question in questions:
        text = " ".join(filter(None, [question.get("question"), question.get("answerShape")]))
        hits = scan(text, surfaces)
        targets = {
            target_key(target)
            for hit in hits
            for target in surface_map[normalize(hit)]["targets"].values()
        }
        report.append({
            "id": question["id"],
            "group": question["group"],
            "hits": hits,
            "distinctTargets": len(targets),
        })
    return report


def main() -> None:
    surface_map, conflicts, duplicates = build_entries()
    coverage = evaluate(surface_map)

    indexed = sorted(surface_map.values(), key=lambda s: normalize(s["surface"]))
    enum_targets = sum(
        1 for slot in indexed for target in slot["targets"].values() if target["kind"] == "enum"
    )
    payload = {
        "schemaVersion": "0.1.0",
        "stage": "Stage 1",
        "note": "由 tools/build_alias_index.py 生成，勿手改。表面形式来自术语种子、内核类型名与枚举取值标签；"
                "按业务对象（targetType + targetIdHint）去重，同一对象的多个同义入口不算歧义；"
                "指向不同对象时保留全部候选、不自动裁决，须回 terms/ly_terms.json 或 kernel/enums.json 补上下文后重跑。",
        "normalization": "NFKC + casefold + 去空白；不做繁简转换与模糊匹配",
        "minLength": MIN_LENGTH,
        "stats": {
            "termSeeds": len(load_json(TERMS_PATH)["terms"]),
            "objectTypes": len(load_json(TYPES_PATH)["types"]),
            "enumValues": enum_targets,
            "surfaces": len(indexed),
            "ambiguousSurfaces": len(conflicts),
            "synonymEntryGroups": len(duplicates),
            "questionCount": len(coverage),
            "questionsWithZeroHits": [c["id"] for c in coverage if not c["hits"]],
        },
        "conflicts": conflicts,
        "synonymEntryGroups": duplicates,
        "entries": [
            {
                "surface": slot["surface"],
                "normalized": normalized,
                "targets": [slot["targets"][key] for key in sorted(slot["targets"])],
            }
            for normalized, slot in ((normalize(s["surface"]), s) for s in indexed)
        ],
        "questionCoverage": coverage,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    stats = payload["stats"]
    print(f"术语种子 {stats['termSeeds']} 条，本体类型 {stats['objectTypes']} 个，枚举取值 {stats['enumValues']} 个")
    print(f"别名表面形式 {stats['surfaces']} 个，真歧义 {stats['ambiguousSurfaces']} 个，"
          f"同义入口组 {stats['synonymEntryGroups']} 组")
    for conflict in conflicts:
        names = "、".join(
            f'{t["targetType"]}:{t.get("targetIdHint") or t["label"]}' for t in conflict["targets"]
        )
        print(f"  歧义 {conflict['surface']}: {names}")
    zero = stats["questionsWithZeroHits"]
    print(f"能力问题 {stats['questionCount']} 条，零命中 {len(zero)} 条：{zero or '无'}")
    print(f"输出 {OUT_PATH}")


if __name__ == "__main__":
    main()
