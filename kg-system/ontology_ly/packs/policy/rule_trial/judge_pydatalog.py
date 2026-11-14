"""阶段 0 规则路线试验 · pyDatalog 路线

一份样本条款（小麦最低收购价执行预案 第五条第一款），七类情形，三种结论。
分工：
  - pyDatalog 负责「规则成立与否」的声明式判定；
  - Python 负责「事实是否齐全」（UNABLE 语义，Datalog 无法表达）与「证明链」组装；
  - 数值比较（价格 < 最低收购价）先由函数算成布尔事实再注入，规避 Datalog 无算术的局限。
"""
from __future__ import annotations

import json
import pathlib
import time

HERE = pathlib.Path(__file__).parent
STMT = json.loads((HERE / "statement_wheat_minprice.json").read_text(encoding="utf-8"))

ALLOWED_REGIONS = set((next(c for c in STMT["conditions"] if c["conditionId"] == "C1-1")["rightValue"]).split(","))
FLOOR_PRICE = next(p["paramValue"] for p in STMT["parameters"] if p["paramName"] == "最低收购价")
MIN_GRADE = next(p["paramValue"] for p in STMT["parameters"] if p["paramName"] == "质量标准")

# 七类情形：唯一起源是 cases.json，Soufflé 路线复用同一份事实
CASES = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))["cases"]

PLACEHOLDER = {"region": "地区", "date": "日期", "price": "到库价", "grade": "等级", "local_reserve_active": "地方收储是否启动"}


def eval_leaf(leaf: dict, case: dict) -> bool | None:
    key = leaf["factKey"]
    val = case.get(key)
    if val is None:
        return None
    if key == "region":
        return val in ALLOWED_REGIONS
    if key == "date":
        md = (int(val[5:7]), int(val[8:10]))
        return (6, 1) <= md <= (9, 30)
    if key == "price":
        return float(val) < FLOOR_PRICE          # 数值比较在函数层完成
    if key == "grade":
        return float(val) <= float(MIN_GRADE)     # 三等及以上 = 等级数字 ≤ 3
    if key == "local_reserve_active":
        return bool(val)
    raise KeyError(f"未登记的事实键 {key}")


TERMS = ("C, R, allowed_region, case_exists, in_region, within_window, "
         "price_below_floor, quality_ok, reserve_active, region_ok, applicable")

RULES = [
    "region_ok(C) <= case_exists(C) & in_region(C, R) & allowed_region(R)",
    "applicable(C) <= case_exists(C) & region_ok(C) & within_window(C) & "
    "price_below_floor(C) & quality_ok(C) & ~reserve_active(C)",
]

LEAF_PREDICATES = (("date", "within_window"), ("price", "price_below_floor"),
                   ("grade", "quality_ok"), ("local_reserve_active", "reserve_active"))


def facts_tables(cases: list[dict]) -> dict[str, list[tuple]]:
    """把每条情形预先算成派生布尔事实表；两路引擎共用同一份语义。"""
    tables: dict[str, list[tuple]] = {
        "allowed_region": [(r,) for r in sorted(ALLOWED_REGIONS)],
        "case_exists": [], "fact_complete": [], "in_region": [], "within_window": [],
        "price_below_floor": [], "quality_ok": [], "reserve_active": [],
    }
    leaves = {x["factKey"]: x for x in STMT["conditions"] if x.get("operator") == "LEAF"}
    leaves.update({x["factKey"]: x for x in STMT["exceptions"]})
    for c in cases:
        cid = c["id"]
        tables["case_exists"].append((cid,))
        # 事实完备性由 Python 判定（Datalog 无法区分「缺失」与「不成立」）
        if all(c.get(k) is not None for k in STMT["requiredFacts"]):
            tables["fact_complete"].append((cid,))
        if c.get("region") is not None:
            tables["in_region"].append((cid, c["region"]))
        for key, pred in LEAF_PREDICATES:
            leaf = leaves.get(key)
            if leaf is None or c.get(key) is None:
                continue
            if eval_leaf(leaf, c):
                tables[pred].append((cid,))
    return tables


def datalog_program(cases: list[dict]) -> str:
    """把派生布尔事实注入 Datalog；算术与集合判断已在 Python 侧完成。"""
    # pyDatalog 限制：被否定的谓词必须非空，否则 ask 时报 "Predicate without definition"。
    # 注入一条永不命中的哨兵事实，保证 ~reserve_active(C) 在无例外场景下也能求值。
    facts = ["+ reserve_active('__NONE__')"]
    for pred, rows in facts_tables(cases).items():
        if pred == "fact_complete":          # 完备性只在 Python 判定，pyDatalog 侧不注入
            continue
        for row in rows:
            facts.append("+ " + pred + "(" + ",".join(f"'{v}'" for v in row) + ")")
    return "\n".join(RULES + facts)


def proof_chain(case: dict) -> list[dict]:
    chain = []
    step = 0
    for cond in STMT["conditions"]:
        if not cond.get("leaf", cond["operator"] == "LEAF"):
            continue
        step += 1
        value = eval_leaf(cond, case)
        chain.append({
            "step": step,
            "ruleId": STMT["statement"]["statementId"],
            "conditionId": cond["conditionId"],
            "leftOperand": cond["leftOperand"],
            "comparator": cond["comparator"],
            "right": cond.get("rightValue") or cond.get("rightIndicatorId"),
            "factRef": f"{cond['factKey']}={case.get(cond['factKey'])}",
            "value": value,
            "clausePath": STMT["statement"]["clausePath"],
            "quote": STMT["statement"]["text"][:60] + "…",
        })
    return chain


def judge(case: dict, applicable_ids: set[str]) -> dict:
    """六步判定顺序：确认状态 → 模糊用语 → 条件树 → 例外 → 优先级 → APPLICABLE。"""
    stmt = STMT["statement"]
    if stmt["confirmationStatus"] != "CONFIRMED":
        return {"outcome": "UNABLE", "reason": "规则未确认", "missing": []}
    if stmt.get("vaguenessFlag"):
        return {"outcome": "UNABLE", "reason": "含模糊用语", "missing": []}

    missing = [PLACEHOLDER[k] for k in STMT["requiredFacts"] if case.get(k) is None]
    if missing:
        return {"outcome": "UNABLE", "reason": "缺事实：" + "、".join(missing), "missing": missing}

    failed = [c["conditionId"] for c in STMT["conditions"]
              if c["operator"] == "LEAF" and eval_leaf(c, case) is False]
    if failed:
        return {"outcome": "NOT_APPLICABLE", "reason": "前提不满足：" + "、".join(failed), "missing": []}

    for exc in STMT["exceptions"]:
        if eval_leaf(exc, case) is True:
            return {"outcome": "NOT_APPLICABLE", "reason": f"被例外排除：{exc['exceptionId']}", "missing": []}

    datalog_says = case["id"] in applicable_ids
    if not datalog_says:
        return {"outcome": "UNABLE", "reason": "规则冲突：引擎与条件树结论不一致（需人工裁决）", "missing": []}
    return {"outcome": "APPLICABLE", "reason": "条件全部成立且无例外触发", "missing": []}


def run(cases: list[dict]) -> list[dict]:
    from pyDatalog import pyDatalog  # 延迟导入：让事实层与结论层可被其它引擎复用

    pyDatalog.clear()
    pyDatalog.create_terms(TERMS)
    t0 = time.perf_counter()
    pyDatalog.load(datalog_program(cases))
    ans = pyDatalog.ask("applicable(C)")
    rows = getattr(ans, "answers", ans) or set()
    elapsed = (time.perf_counter() - t0) * 1000
    applicable_ids = {row[0] for row in rows}

    results = []
    for c in cases:
        verdict = judge(c, applicable_ids)
        results.append({**c, **verdict, "datalog_applicable": c["id"] in applicable_ids,
                        "proof": proof_chain(c) if verdict["outcome"] != "UNABLE" else []})
    return results, elapsed


def emit_souffle_facts(cases: list[dict], outdir: pathlib.Path) -> pathlib.Path:
    """导出 Soufflé 读入的 TSV 事实（制表符分隔、无表头），与 pyDatalog 同源。"""
    outdir.mkdir(parents=True, exist_ok=True)
    for pred, rows in facts_tables(cases).items():
        body = "\n".join("\t".join(str(v) for v in row) for row in rows)
        (outdir / f"{pred}.facts").write_text(body + ("\n" if body else ""), encoding="utf-8")
    return outdir


def main() -> None:
    results, elapsed = run(CASES)
    print(f"{'case':<5}{'结论':<16}{'期望':<16}{'一致':<6}原因")
    hit = 0
    for r in results:
        ok = r["outcome"] == r["expect"]
        hit += ok
        print(f"{r['id']:<5}{r['outcome']:<16}{r['expect']:<16}{'✓' if ok else '✗':<6}{r['reason']}")
    print(f"\n三态一致 {hit}/{len(results)}；pyDatalog 判定耗时 {elapsed:.1f} ms（{len(CASES)} 个情形）")

    # 规模基准：1000 条合成事实
    bulk = [{"id": f"B{i:04d}", "region": "HENAN" if i % 3 else "GUANGDONG",
             "date": "2025-07-15", "price": 1.15 if i % 2 else 1.25,
             "grade": 3 if i % 5 else 4, "local_reserve_active": False, "expect": None} for i in range(1000)]
    _, bulk_ms = run(bulk)
    print(f"1000 条事实：pyDatalog 判定耗时 {bulk_ms:.1f} ms")

    (HERE / "result_pydatalog.json").write_text(
        json.dumps({"engine": "pyDatalog", "cases": results, "elapsedMs": round(elapsed, 1),
                    "bulk1000Ms": round(bulk_ms, 1)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Soufflé 事实已导出：", emit_souffle_facts(CASES, HERE / "souffle" / "facts"))


if __name__ == "__main__":
    main()
