"""阶段 0 规则路线试验 · Cozo 路线（Soufflé 替代方案）

与 judge_pydatalog.py 共用同一份条款、同一份情形、同一套派生事实函数，
只把「规则求值」这一段换成 Cozo 的 Datalog 引擎，用于验证引擎可替换性。

Cozo：Rust 实现、pip 直接装、单进程内嵌、支持分层否定与聚合，
无编译期、无镜像、Windows 原生轮子。
"""
from __future__ import annotations

import json
import pathlib
import time

from pycozo.client import Client

from judge_pydatalog import CASES, facts_tables, judge  # 复用事实层与结论层

HERE = pathlib.Path(__file__).parent

# 规则层：与 souffle/wheat_minprice.dl 一一对应，只是换了语法。
RULES = """
region_ok[c] := in_region[c, r], allowed_region[r]
applicable[c] := case_exists[c], region_ok[c], within_window[c],
                 price_below_floor[c], quality_ok[c], not reserve_active[c]
?[c] := applicable[c]
"""


def const_rel(name: str, arity: int, rows: list[tuple]) -> str:
    """把派生事实写成 Cozo 常量规则。空表也要声明，否则否定会报未定义。"""
    if not rows:
        # Cozo 的常量规则不支持零行，用一个不可能命中的占位值占住谓词
        rows = [tuple("__NONE__" for _ in range(arity))]
    body = ", ".join("[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in row) + "]" for row in rows)
    head = ", ".join(f"v{i}" for i in range(arity))
    return f"{name}[{head}] <- [{body}]"


def program(cases: list[dict]) -> str:
    parts = [const_rel(pred, len(rows[0]) if rows else 1, rows) for pred, rows in facts_tables(cases).items()]
    return "\n".join(parts) + "\n" + RULES


def run(cases: list[dict]) -> tuple[list[dict], float]:
    db = Client()
    t0 = time.perf_counter()
    df = db.run(program(cases))
    elapsed = (time.perf_counter() - t0) * 1000
    applicable_ids = set(df.iloc[:, 0].tolist()) if len(df) else set()

    results = []
    for c in cases:
        verdict = judge(c, applicable_ids)
        results.append({**c, **verdict, "datalog_applicable": c["id"] in applicable_ids,
                        "proof": [] if verdict["outcome"] == "UNABLE" else []})
    return results, elapsed


def main() -> None:
    results, elapsed = run(CASES)
    hit = sum(r["outcome"] == r["expect"] for r in results)
    for r in results:
        print(f"{r['id']:<5}{r['outcome']:<16}{r['expect']:<16}{'✓' if r['outcome'] == r['expect'] else '✗':<4}{r['reason']}")
    print(f"\n三态一致 {hit}/{len(results)}；Cozo 判定耗时 {elapsed:.1f} ms（{len(CASES)} 个情形）")

    bulk = [{"id": f"B{i:04d}", "region": "HENAN" if i % 3 else "GUANGDONG",
             "date": "2025-07-15", "price": 1.15 if i % 2 else 1.25,
             "grade": 3 if i % 5 else 4, "local_reserve_active": False, "expect": None} for i in range(1000)]
    _, bulk_ms = run(bulk)
    print(f"1000 条事实：Cozo 判定耗时 {bulk_ms:.1f} ms")

    (HERE / "result_cozo.json").write_text(
        json.dumps({"engine": "Cozo 0.7.6", "cases": results, "elapsedMs": round(elapsed, 1),
                    "bulk1000Ms": round(bulk_ms, 1)}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
