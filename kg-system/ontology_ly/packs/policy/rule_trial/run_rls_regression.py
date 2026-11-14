"""阶段 2 收口：规则文本以 `.rls` 为唯一真相，直接加载并跑 11 情形回归。

在此之前规则由 judge_nemo_multi.rules_text() 在内存里拼装；本脚本改为：

1. 从 packs/policy/rules/wheat_minprice.rls 读规则（磁盘产物，可版本化）；
2. 用 cases_multi.json 生成事实层（复用同一份事实函数，避免双份实现）；
3. 与 rules_text() 做等式校验——语句集改了但 `.rls` 未重编译时立即报错，
   防止规则文件与条款模型悄悄漂移。

事实层与结论层仍在 Python，规则层完全交给 Datalog，三段分工不变。
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

import nmo_python as nemo

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))
import judge_nemo_multi as base  # noqa: E402

RULES_FILE = HERE.parent / "rules" / "wheat_minprice.rls"
OUT_FILE = HERE / "result_rls.json"


def load_rules() -> str:
    text = RULES_FILE.read_text(encoding="utf-8")
    drift = text.strip() != base.rules_text().strip()
    if drift:
        raise SystemExit(
            f"规则文件与条款模型不一致：{RULES_FILE.name} 未随 statements_multi.json 重编译"
        )
    return text


def run(cases: list[dict], rules: str) -> tuple[list[dict], float, list[str]]:
    prog = nemo.load_string(base.fact_rules(base.facts_tables(cases)) + "\n" + rules)
    engine = nemo.NemoEngine(prog)
    t0 = time.perf_counter()
    engine.reason()

    fires: dict[str, set[str]] = {}
    for row in engine.result("fires"):
        fires.setdefault(base.unq(row[0]), set()).add(base.unq(row[1]))
    overridden: dict[str, dict[str, set[str]]] = {}
    for row in engine.result("overridden_by"):
        overridden.setdefault(base.unq(row[0]), {}).setdefault(base.unq(row[1]), set()).add(base.unq(row[2]))
    cycles = sorted({base.unq(r[0]) for r in engine.result("cycle")})
    elapsed = (time.perf_counter() - t0) * 1000

    results = []
    for case in cases:
        cid = case["id"]
        verdicts = base.resolve(case, fires.get(cid, set()), overridden.get(cid, {}))
        ok = all(verdicts[k]["outcome"] == v for k, v in case["expect"].items())
        results.append({"id": cid, "verdicts": verdicts, "consistent": ok})
    return results, elapsed, cycles


def main() -> None:
    rules = load_rules()
    results, elapsed, cycles = run(base.CASES, rules)
    hit = sum(r["consistent"] for r in results)

    for r in results:
        mark = "OK" if r["consistent"] else "XX"
        print(f"{r['id']:<5}{mark:<4}" + "  ".join(f"{k}={v['outcome']}" for k, v in r["verdicts"].items()))
        if not r["consistent"]:
            print(f"      期望 {base.CASES[int(r['id'][2:]) - 1]['expect']}")

    print(f"\n.rls 直接加载回归 {hit}/{len(results)}；Nemo 判定耗时 {elapsed:.1f} ms；成环 {cycles or '无'}")

    OUT_FILE.write_text(
        json.dumps(
            {"engine": "Nemo 0.10.1", "rulesFile": RULES_FILE.name,
             "passed": hit, "total": len(results),
             "elapsedMs": round(elapsed, 1), "cases": results},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    if hit != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
