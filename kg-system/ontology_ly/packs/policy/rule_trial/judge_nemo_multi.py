"""阶段 2 · 多条款优先级与 PREVAILS_OVER 冲突消解（Nemo 路线）

在阶段 0 单条款骨架（事实层 / 规则层 / 结论层）之上，验证三件事：

1. 多条 NormativeStatement 同时成立时，`prevails(A, B)` 能否在 Datalog 内做分层否定消解；
2. 优先级链（S3 > S2 > S1）能否靠递归传递闭包求解，而不必在 Python 里手写 DFS；
3. `prevails` 成环时能否被检测出来，而不是静默给出错误结论。

结论层新增第四态 OVERRIDDEN：它与 NOT_APPLICABLE 业务含义不同——
前者是「本条成立，但另有更高优先级条款生效」，后者是「本条前提不成立」。
"""
from __future__ import annotations

import json
import pathlib
import time
from collections import defaultdict

import nmo_python as nemo

HERE = pathlib.Path(__file__).parent
PACK = json.loads((HERE / "statements_multi.json").read_text(encoding="utf-8"))
STATEMENTS: list[dict] = PACK["statements"]
PREVAILS: list[list[str]] = PACK["prevailsOver"]
CASES = json.loads((HERE / "cases_multi.json").read_text(encoding="utf-8"))["cases"]

FACT_LABEL = {
    "region": "区域", "date": "日期", "price": "到库价", "grade": "等级",
    "local_reserve_active": "地方收储是否启动",
    "province_rule_published": "省级细则是否发布",
    "emergency_level": "应急响应等级",
}


# ---------------------------------------------------------------- 事实层

def param(stmt: dict, name: str) -> float:
    return float(next(p["paramValue"] for p in stmt["parameters"] if p["paramName"] == name))


def allowed_regions(stmt: dict) -> set[str]:
    for c in stmt["conditions"]:
        if c["factKey"] == "region":
            return set(c["rightValue"].split(","))
    return set()


def eval_leaf(leaf: dict, case: dict, stmt: dict) -> bool | None:
    """叶子条件前置为布尔。数值/日期/集合比较一律在此完成，Datalog 只做组合。

    emergency_level 是唯一允许缺省的叶子：没有应急响应时该事实本就为空，
    语义是「无响应」而非「未知」，因此缺失直接判 False 而非 None。
    """
    key = leaf["factKey"]
    val = case.get(key)
    if key == "emergency_level":
        return False if val is None else float(val) <= param(stmt, leaf["paramRef"])
    if val is None:
        return None
    if key == "region":
        return val in allowed_regions(stmt)
    if key == "date":
        md = (int(val[5:7]), int(val[8:10]))
        lo, hi = leaf["rightValue"], leaf["rightValueUpper"]
        return (int(lo[:2]), int(lo[3:])) <= md <= (int(hi[:2]), int(hi[3:]))
    if key == "price":
        return float(val) < param(stmt, leaf["paramRef"])
    if key == "grade":
        return float(val) <= param(stmt, leaf["paramRef"])
    if key in ("local_reserve_active", "province_rule_published"):
        return bool(val)
    raise KeyError(f"未登记的事实键 {key}")


def pred_for(fact_key: str, sid: str) -> str:
    """把叶子映射成注入 Datalog 的一元谓词；带条款后缀保证不同阈值互不串味。"""
    s = sid.lower()
    return {
        "region": "region_ok",                     # 导出谓词，由 allowed_region 派生
        "date": f"within_window_{s}",
        "price": f"price_below_{s}",
        "grade": f"quality_ok_{s}",
        "local_reserve_active": "reserve_active",
        "province_rule_published": "province_rule_published",
        "emergency_level": f"emergency_trigger_{s}",
    }[fact_key]


def facts_tables(cases: list[dict]) -> dict[str, list[tuple]]:
    tables: dict[str, list[tuple]] = {
        "allowed_region": [(r,) for r in sorted(set().union(*(allowed_regions(s) for s in STATEMENTS)))],
        "case_exists": [], "in_region": [], "reserve_active": [], "province_rule_published": [],
        "prevails": [(a, b) for a, b in PREVAILS],
    }
    for st in STATEMENTS:
        sid = st["shortId"]
        for cond in st["conditions"] + st["exceptions"]:
            key = cond["factKey"]
            if key == "region":                    # region 走 region_ok 派生，不单独注表
                continue
            tables.setdefault(pred_for(key, sid), [])
    for case in cases:
        cid = case["id"]
        tables["case_exists"].append((cid,))
        if case.get("region") is not None:
            tables["in_region"].append((cid, case["region"]))
        for st in STATEMENTS:
            sid = st["shortId"]
            for cond in st["conditions"]:
                key = cond["factKey"]
                if key == "region":
                    continue
                if eval_leaf(cond, case, st) is True:
                    tables[pred_for(key, sid)].append((cid,))
            for exc in st["exceptions"]:
                if eval_leaf(exc, case, st) is True:
                    tables[pred_for(exc["factKey"], sid)].append((cid,))
    return tables


# ---------------------------------------------------------------- 规则层

def rules_text() -> str:
    lines = [
        "region_ok(?c) :- in_region(?c, ?r), allowed_region(?r) .",
    ]
    for st in STATEMENTS:
        sid = st["shortId"]
        body = ["case_exists(?c)"]
        for cond in st["conditions"]:
            p = pred_for(cond["factKey"], sid)
            body.append(f"{p}(?c)")
        for exc in st["exceptions"]:
            body.append(f"~{pred_for(exc['factKey'], sid)}(?c)")
        lines.append(f'fires(?c, "{sid}") :- ' + ", ".join(body) + " .")
    lines += [
        # 优先级传递闭包：S3 > S2 与 S2 > S1 可推出 S3 > S1
        "prevails_t(?a, ?b) :- prevails(?a, ?b) .",
        "prevails_t(?a, ?c) :- prevails(?a, ?b), prevails_t(?b, ?c) .",
        # 覆盖关系只在「覆盖者本身成立」时产生
        'overridden_by(?c, ?a, ?b) :- fires(?c, ?b), prevails_t(?b, ?a) .',
        "overridden(?c, ?a) :- overridden_by(?c, ?a, ?b) .",
        'effective(?c, ?s) :- fires(?c, ?s), ~overridden(?c, ?s) .',
        # 配置层错误：优先级成环
        "cycle(?a) :- prevails_t(?a, ?a) .",
    ]
    return "\n".join(lines)


def quote(v: str) -> str:
    return '"' + str(v).replace('"', '\\"') + '"'


def fact_rules(tables: dict[str, list[tuple]]) -> str:
    lines: list[str] = []
    for pred, rows in tables.items():
        if not rows:                            # 空谓词占位，避免否定时报未定义
            lines.append(f"{pred}({quote('__NONE__')}) .")
            continue
        for row in rows:
            lines.append(f"{pred}({', '.join(quote(v) for v in row)}) .")
    return "\n".join(lines)


# ---------------------------------------------------------------- 结论层

def unq(v) -> str:
    return str(v).strip('"')


def base_verdict(stmt: dict, case: dict, fired: set[str]) -> tuple[str, str, list[str]]:
    """条款内判定：确认状态 → 模糊用语 → 缺事实 → 条件树 → 例外 → 引擎校验。"""
    sid = stmt["shortId"]
    s = stmt["statement"]
    if s.get("confirmationStatus") != "CONFIRMED":
        return "UNABLE", "规则未确认", []
    if s.get("vaguenessFlag"):
        return "UNABLE", "含模糊用语", []
    missing = [FACT_LABEL[k] for k in stmt["requiredFacts"] if case.get(k) is None]
    if missing:
        return "UNABLE", "缺事实：" + "、".join(missing), missing
    failed = [c["conditionId"] for c in stmt["conditions"] if eval_leaf(c, case, stmt) is False]
    if failed:
        return "NOT_APPLICABLE", "前提不满足：" + "、".join(failed), []
    for exc in stmt["exceptions"]:
        if eval_leaf(exc, case, stmt) is True:
            return "NOT_APPLICABLE", f"被例外排除：{exc['exceptionId']}", []
    if sid not in fired:
        return "UNABLE", "规则冲突：引擎与条件树结论不一致（需人工裁决）", []
    return "APPLICABLE", "条件全部成立且无例外触发", []


def resolve(case: dict, fired: set[str], overridden_by: dict[str, set[str]]) -> dict[str, dict]:
    """两遍判定：先算各条款自身结论，再做覆盖消解。

    关键约束：UNABLE 的条款既不可被覆盖，也无权覆盖他人——
    缺事实时不知道该条本会不会成立，任何优先级裁决都是越权。
    """
    base = {st["shortId"]: base_verdict(st, case, fired) for st in STATEMENTS}
    out: dict[str, dict] = {}
    for st in STATEMENTS:
        sid = st["shortId"]
        outcome, reason, missing = base[sid]
        by, effective = None, sid if outcome == "APPLICABLE" else None
        if outcome == "APPLICABLE":
            highs = {h for h in overridden_by.get(sid, set()) if base[h][0] == "APPLICABLE"}
            if highs:
                # 最终生效条款 = 覆盖者中自身不再被覆盖的那个
                top = [h for h in highs if not (overridden_by.get(h, set()) & highs)]
                effective = sorted(top or highs)[0]
                by = sorted(highs)[0]
                outcome, reason = "OVERRIDDEN", f"被更高优先级条款 {by} 覆盖，最终生效 {effective}"
        out[sid] = {"outcome": outcome, "reason": reason, "missing": missing,
                    "overriddenBy": by, "effective": effective}
    return out


# ---------------------------------------------------------------- 执行

def run(cases: list[dict]) -> tuple[list[dict], float, list[str]]:
    prog = nemo.load_string(fact_rules(facts_tables(cases)) + "\n" + rules_text())
    engine = nemo.NemoEngine(prog)
    t0 = time.perf_counter()
    engine.reason()
    fires = defaultdict(set)
    for row in engine.result("fires"):
        fires[unq(row[0])].add(unq(row[1]))
    ovr: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in engine.result("overridden_by"):
        ovr[unq(row[0])][unq(row[1])].add(unq(row[2]))
    cycles = sorted({unq(r[0]) for r in engine.result("cycle")})
    elapsed = (time.perf_counter() - t0) * 1000

    results = []
    for case in cases:
        cid = case["id"]
        verdicts = resolve(case, fires[cid], ovr[cid])
        ok = all(verdicts[k]["outcome"] == v for k, v in case["expect"].items())
        results.append({**case, "verdicts": verdicts, "consistent": ok})
    return results, elapsed, cycles


def cycle_probe() -> list[str]:
    """独立的成环检测：优先级元数据错误应在加载期一次性拒绝，而非逐情形判定。"""
    facts = fact_rules({"prevails": [("S2", "S1"), ("S1", "S2")]})
    prog = nemo.load_string(facts + "\n" + rules_text())
    engine = nemo.NemoEngine(prog)
    engine.reason()
    return sorted({unq(r[0]) for r in engine.result("cycle")})


def main() -> None:
    results, elapsed, cycles = run(CASES)
    hit = sum(r["consistent"] for r in results)
    for r in results:
        print(f"{r['id']:<5}{'OK' if r['consistent'] else 'XX':<4}" +
              "  ".join(f"{k}={v['outcome']}" for k, v in r["verdicts"].items()))
        if not r["consistent"]:
            print(f"      期望 {r['expect']}")
            for k, v in r["verdicts"].items():
                print(f"      {k} {v['outcome']}｜{v['reason']}")
    print(f"\n多条款一致 {hit}/{len(results)}；Nemo 判定耗时 {elapsed:.1f} ms")
    print(f"主情形集优先级成环：{cycles or '无'}")

    probe = cycle_probe()
    print(f"成环探测用例（注入 S1↔S2）：cycle = {probe or '未检出'}")

    bulk = [{**c, "id": f"B{i:04d}"} for i in range(250) for c in CASES[:4]]
    _, bulk_ms, _ = run([{**b, "expect": b["expect"]} for b in bulk])
    print(f"{len(bulk)} 条事实：Nemo 判定耗时 {bulk_ms:.1f} ms")

    (HERE / "result_nemo_multi.json").write_text(
        json.dumps({"engine": "Nemo 0.10.1", "statements": [s["shortId"] for s in STATEMENTS],
                    "prevailsOver": PREVAILS, "cases": results,
                    "elapsedMs": round(elapsed, 1), "bulkMs": round(bulk_ms, 1),
                    "cycleProbe": probe}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
