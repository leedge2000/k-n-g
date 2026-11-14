"""阶段 0 规则路线试验 · Nemo 路线（Soufflé 替代方案）

Nemo 是德国德累斯顿 KB 组的 Rust 本体推理器，与 Soufflé 同属「规则编译成关系代数再执行」
的路线，但分发方式是 pip 轮子（Windows 原生），无需 Docker、无需编译。
它是本试验中与「本体知识图谱 + 政策规则」场景最贴合的候选。

三段分工与 judge_cozo.py 完全一致：事实层与结论层复用 judge_pydatalog.py，
只替换中间的 Datalog 求值。
"""
from __future__ import annotations

import json
import pathlib
import time

import nmo_python as nemo

from judge_pydatalog import CASES, facts_tables, judge

HERE = pathlib.Path(__file__).parent

# 与 souffle/wheat_minprice.dl 一一对应；Nemo 用 ~ 表示分层否定
RULES = """
region_ok(?c) :- in_region(?c, ?r), allowed_region(?r) .
applicable(?c) :- case_exists(?c), region_ok(?c), within_window(?c),
                  price_below_floor(?c), quality_ok(?c), ~reserve_active(?c) .
"""


def quote(v: str) -> str:
    return '"' + str(v).replace('"', '\\"') + '"'


def fact_rules(cases: list[dict]) -> str:
    lines: list[str] = []
    for pred, rows in facts_tables(cases).items():
        if not rows:                       # 空谓词占位，避免否定时报未定义
            lines.append(f"{pred}({quote('__NONE__')}) .")
            continue
        for row in rows:
            lines.append(f"{pred}({', '.join(quote(v) for v in row)}) .")
    return "\n".join(lines)


def run(cases: list[dict]) -> tuple[list[dict], float]:
    prog = nemo.load_string(fact_rules(cases) + "\n" + RULES)
    engine = nemo.NemoEngine(prog)
    t0 = time.perf_counter()
    engine.reason()
    rows = list(engine.result("applicable"))
    elapsed = (time.perf_counter() - t0) * 1000
    applicable_ids = {str(r[0]).strip('"') for r in rows}

    results = []
    for c in cases:
        verdict = judge(c, applicable_ids)
        results.append({**c, **verdict, "datalog_applicable": c["id"] in applicable_ids, "proof": []})
    return results, elapsed


def main() -> None:
    results, elapsed = run(CASES)
    hit = sum(r["outcome"] == r["expect"] for r in results)
    for r in results:
        print(f"{r['id']:<5}{r['outcome']:<16}{r['expect']:<16}{'OK' if r['outcome'] == r['expect'] else 'XX':<4}{r['reason']}")
    print(f"\n三态一致 {hit}/{len(results)}；Nemo 判定耗时 {elapsed:.1f} ms（{len(CASES)} 个情形）")

    bulk = [{"id": f"B{i:04d}", "region": "HENAN" if i % 3 else "GUANGDONG",
             "date": "2025-07-15", "price": 1.15 if i % 2 else 1.25,
             "grade": 3 if i % 5 else 4, "local_reserve_active": False, "expect": None} for i in range(1000)]
    _, bulk_ms = run(bulk)
    print(f"1000 条事实：Nemo 判定耗时 {bulk_ms:.1f} ms")

    (HERE / "result_nemo.json").write_text(
        json.dumps({"engine": f"Nemo {nemo.__doc__ or '0.10.1'}", "cases": results,
                    "elapsedMs": round(elapsed, 1), "bulk1000Ms": round(bulk_ms, 1)},
                   ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
