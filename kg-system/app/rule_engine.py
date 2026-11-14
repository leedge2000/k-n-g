"""阶段 2c · 规则引擎层

抽象接口 RuleEngine（Protocol），当前实现 Cozo 路线，预留 Nemo 接口。
铁律：本模块**只做规则求值**，不做政策阈值硬编码——阈值由 NormativeStatement →
RuleParameter 在 Pack2c 编译期翻译成 ruleExpression 注入。

分工（延续阶段 0 试验结论）：
  Python 层 · eval_leaves  ：叶子条件求值（数值/日期/集合比较）→ 布尔事实表
  Datalog 层 · run_program ：规则组合 + 分层否定 + 优先级消解
  Python 层 · final_judge  ：三态最终判定（UNABLE / NOT_APPLICABLE / APPLICABLE / OVERRIDDEN）

铁律（方案 §4.13 + 风险表）：
  · 规则文本只存在 CompiledRule.ruleExpression，代码里不得出现政策阈值常量
  · 二级政策（policyLevel != 1）不生成 CompiledRule
  · CompiledRule.confirmationStatus != CONFIRMED 的规则不参与推理
"""
from __future__ import annotations

import json
import re
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------- 抽象接口

@runtime_checkable
class RuleEngine(Protocol):
    """规则引擎抽象——Cozo 与 Nemo 都实现这个接口。"""

    def run(self, program: str) -> list[dict[str, Any]]:
        """执行完整规则程序，返回结果行。"""
        ...


# ---------------------------------------------------------------- Cozo 实现

class CozoEngine:
    """Cozo 0.7.x 内嵌引擎实现。"""

    def __init__(self) -> None:
        try:
            from pycozo.client import Client  # noqa: F401
        except ImportError:  # pragma: no cover
            raise RuntimeError("pycozo 未安装：pip install cozo-embedded")

    def run(self, program: str) -> list[dict[str, Any]]:
        from pycozo.client import Client
        db = Client()
        df = db.run(program)
        if df is None or len(df) == 0:
            return []
        return df.to_dict(orient="records")


# ---------------------------------------------------------------- Nemo 实现

class NemoEngine:
    """Nemo 0.10.x 内嵌引擎实现。"""

    def __init__(self) -> None:
        try:
            import nmo_python as nemo
        except ImportError:  # pragma: no cover
            raise RuntimeError("nemo-python 未安装：pip install nemo-python==0.10.1")
        self._nemo = nemo
        self._engine = None

    def run(self, program: str) -> list[dict[str, Any]]:
        # Nemo 的 Rust 句柄不可跨线程析构；替换前在执行线程内显式释放。
        self.close()
        self._engine = self._nemo.NemoEngine(self._nemo.load_string(program))
        self._engine.reason()
        return self.result("output")

    def result(self, predicate: str) -> list[dict[str, Any]]:
        if self._engine is None:
            raise RuntimeError("请先执行 run()")
        return [
            {f"c{i}": str(value).strip('"') for i, value in enumerate(row)}
            for row in self._engine.result(predicate)
        ]

    def close(self) -> None:
        self._engine = None


# ---------------------------------------------------------------- 规则程序构造器

class ProgramBuilder:
    """把叶子条件求值的派生事实 + 规则文本，拼成引擎可执行的完整程序。"""

    def __init__(self, engine: RuleEngine | None = None) -> None:
        self.engine: RuleEngine = engine or NemoEngine()

    # ---------------------------------------------------------- 派生事实 → 常量规则

    def const_rel(self, name: str, arity: int, rows: list[tuple[Any, ...]]) -> str:
        """把派生事实写成常量规则；空表以哨兵值保证否定可求值。"""
        if not rows:
            rows = [tuple("__NONE__" for _ in range(arity))]
        if isinstance(self.engine, NemoEngine):
            return "\n".join(
                f"{name}(" + ", ".join(json.dumps(v, ensure_ascii=False) for v in row) + ") ."
                for row in rows
            )
        body = ", ".join(
            "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in row) + "]"
            for row in rows
        )
        head = ", ".join(f"v{i}" for i in range(arity))
        return f"{name}[{head}] <- [{body}]"

    def build(self, derived_facts: dict[str, list[tuple[Any, ...]]], rules_text: str) -> str:
        """拼接常量事实 + 规则文本 → 完整程序。"""
        consts = [
            self.const_rel(pred, len(rows[0]) if rows else 1, rows)
            for pred, rows in derived_facts.items()
        ]
        return "\n".join(consts) + "\n" + rules_text + "\n"

    # ---------------------------------------------------------- 执行

    def run(self, derived_facts: dict[str, list[tuple[Any, ...]]], rules_text: str) -> list[dict[str, Any]]:
        program = self.build(derived_facts, rules_text)
        return self.engine.run(program)


# ---------------------------------------------------------------- 叶子条件求值（Python 层）

# 对应 ClauseCondition 的 operator / leftOperand / rightValue / factSource 契约
def eval_condition(
    condition: dict[str, Any],
    facts: dict[str, Any],
) -> bool | None:
    """求值单条 ClauseCondition（扁平 LEAF）。

    返回 True/False/None（None = 缺事实 → UNABLE 候选）。
    comparator: = ≠ ≤ ≥ < > ∈ ∉ contains
    factSource: USER_INPUT = 直接取 facts[leftOperand 最后一段]
                SCOPE = 取 scope 维
                POLICY_META = 取政策元数据
    """
    op = condition.get("operator") or "LEAF"
    if op != "LEAF":
        # 暂不支持复合（AND/OR/NOT）——2c 先做单条件 + 单例外 + 单参数骨架
        return None

    left_operand = str(condition.get("leftOperand") or "")
    comparator = str(condition.get("comparator") or "=")
    right_value = condition.get("rightValue")
    fact_source = str(condition.get("factSource") or "USER_INPUT")

    # 解析 leftOperand：facts.targetObject → 取 facts["targetObject"]
    #                     facts.Region.regionCode → 取 facts["Region.regionCode"]
    key = left_operand
    if "." in left_operand and left_operand.startswith("facts."):
        key = left_operand[len("facts."):]

    fact_val = facts.get(key)
    if fact_val is None:
        return None
    if comparator in ("≤", "<=", "≥", ">=", "<", ">") and not condition.get("rightUnitId"):
        return None

    return _compare(fact_val, comparator, right_value)


def _compare(fact_val: Any, comparator: str, right_value: Any) -> bool | None:
    """单值/集合/数值比较；无法安全求值时返回 None。"""
    if comparator in ("=", "=="):
        return str(fact_val) == str(right_value)
    if comparator in ("≠", "!="):
        return str(fact_val) != str(right_value)
    if comparator in ("∈", "in"):
        right_set = [v.strip() for v in str(right_value).split(",")]
        return str(fact_val) in right_set
    if comparator in ("≤", "<=", "≥", ">=", "<", ">"):
        try:
            left = float(fact_val)
            right = float(right_value)
        except (TypeError, ValueError):
            return None
        if comparator in ("≤", "<="):
            return left <= right
        if comparator in ("≥", ">="):
            return left >= right
        if comparator == "<":
            return left < right
        return left > right
    return None


# ---------------------------------------------------------------- 三态最终判定（Python 层）

def final_judge(
    *,
    condition_eval_results: list[bool | None],
    exception_eval_result: bool | None = None,
    exception_present: bool = False,
    rule_fired: bool = False,
) -> tuple[str, str]:
    """把叶子条件与 Datalog 结果合成三态；引擎不一致时不得猜成不适用。"""
    if any(v is None for v in condition_eval_results):
        missing = [i for i, v in enumerate(condition_eval_results) if v is None]
        return ("UNABLE", f"缺事实：条件 #{','.join(str(m) for m in missing)} 未提供")
    if exception_present and exception_eval_result is None:
        return ("UNABLE", "例外条件缺事实，无法排除例外")
    if any(v is False for v in condition_eval_results):
        return ("NOT_APPLICABLE", "至少一个条件不成立")
    if exception_eval_result is True:
        return ("NOT_APPLICABLE", "例外条款命中")
    if rule_fired:
        return ("APPLICABLE", "所有条件成立且无例外")
    if condition_eval_results and all(v is True for v in condition_eval_results):
        return ("UNABLE", "条件成立但引擎未触发规则，需检查规则一致性")
    return ("NOT_APPLICABLE", "规则前置条件未满足")
