# 阶段 2b 口径句清单 · 冻结版 v2

> 每条都是**单一句式、可判定真伪**的规则。冻结后实现层不得改写。
> 依据来源：`方案定稿.md` §4.2-4.6（对象类型）、§5（判定算法）、§8（2b 阶段补注：本阶段只做到 NormativeStatement，不编译规则；vaguenessFlag 必须在这一步打）。

## 变更记录

| 版本 | 日期 | 改动 | 理由与影响面 |
| --- | --- | --- | --- |
| v1 | 2026-11-15 | 冻结首版，覆盖 POL0001 三条价格条款 | — |
| v2 | 2026-11-16 | 修订 R2-1-1、R2-1-5、R2-1-6、R5-1-4、R6-1-1 | POL0009（高标准农田）推广时暴露：原实现只产出 `facts.targetObject` 一种条件，"禁止在25度以上坡耕地…"与"严禁将污水…排放…"无法表达；"到2030年力争…13.5亿亩"被误判 DEFINITION；"（…除外）"括注被误抽为例外。影响面：pack2b 的条件/分类/例外抽取、pack2c 的 `SUPPORTED_FACT_PATHS`，POL0001 三条条款输出不变，全量测试重跑通过 |

---

## §0 范围锁定（非规则性，仅声明）

- **R0-0-1** 本阶段**只做到 `NormativeStatement`**，不编译 Nemo 规则、不生成 `RuleCompilation`、不产出情形测试。
- **R0-0-2** 本阶段**不写判定器**（判定器升级到 `NormativeStatement` 粒度属下一阶段，2c 完成）；结论层 v1 继续用 `Policy_Clause` 粒度。
- **R0-0-3** 本阶段新增表：`NormativeStatement`、`ClauseCondition`、`ClauseException`、`ClauseConsequence`、`RuleParameter`、`ClauseRelation`（方案定稿 §4.2-4.7）。
- **R0-0-4** 本阶段**不做**"定义展开"——种子里无定义表，且定义展开属编译前置的语义归一，2c 再做。

---

## §1 条款切分 → 规范陈述

**R1-1-1** 切分起点：`Policy_Clause.text`（原文，不改写）；按中文全角/半角分号 `；;` 和句号 `。` 拆句。
**R1-1-2** 拆句边界保护：若一句内含引号「…」或括号（…）/（…），句内标点不切，引号/括号外的标点才切。
**R1-1-3** 单条陈述的最小单元：一条陈述 = 一个主语 + 一个模态（应/须/可/不得/有权/应当）+ 一个动作短语。
**R1-1-4** 切分后若某子句**缺模态**（纯事实陈述如"2025年早籼稻最低收购价为X元/50公斤"），归入 `DEFINITION` 类型，仍算一条 `NormativeStatement`。
**R1-1-5** 一条 `Policy_Clause` 可产出 **0..N** 条 `NormativeStatement`；N=0 时该条款**不进入**2b 输出（跳过而非报错）。
**R1-1-6** 切分产出每条必须带 `charStart` / `charEnd`（在父条款 `text` 内偏移），可回链。

---

## §2 statementType 分类（六选一对齐方案定稿 §4.2）

**R2-1-1** `OBLIGATION`：模态为「应/须/应当/必须/责成/要求」→ 义务。
**R2-1-1a（v2）** 「要 + 处置动词（及时/严格/切实/认真/按照/按/在/于/将/加强/落实/确保/建立/完善）」视为弱义务语态，归 `OBLIGATION`；判定顺序在 `PERMISSION` 之前，故"经批准允许占用…要及时落实补建"不再被判为许可。
**R2-1-2** `PROHIBITION`：模态为「不得/禁止/禁止性规定/严禁」→ 禁止。
**R2-1-3** `PERMISSION`：模态为「可/可以/有权/允许」→ 许可。
**R2-1-4** `AUTHORIZATION`：模态为「由…批准/经…核准/需…同意/授权…」→ 授权；与 PERMISSION 区别：主体是机构、动作是行政许可类。
**R2-1-5** `DEFINITION`：缺模态、纯事实陈述（价格表、数量表、定义性说明）→ 定义。
**R2-1-5a（v2）** `DEFINITION` 的"纯事实陈述"必须命中显式事实动词「为/定为/规定为/是指」；**仅"含数字"不再构成充分条件**，否则"到2030年，力争累计建成…13.5亿亩"会被误判为定义。
**R2-1-6** `TARGET`：显式含「目标/指标/定额/上限/下限」且带量值 → 目标。
**R2-1-6a（v2）** 未出现上述关键词，但同句含「力争/确保/到 XXXX 年」且带量值（如"到2030年，力争累计建成高标准农田13.5亿亩"），同样归 `TARGET`。
**R2-1-7** 同一句若能归入多类，按优先级 `DEFINITION > TARGET > PROHIBITION > OBLIGATION > PERMISSION > AUTHORIZATION`（从"可判定性最确定"到"需要机构语义"）选首个命中的；禁止同时归入多类。

---

## §3 vaguenessFlag 打标（方案定稿 §4.2 必须在本阶段打）

**R3-1-1** `vaguenessFlag = true` 当且仅当陈述文本命中下列词表**任一**：`适当`、`必要时`、`有关部门`、`原则上`、`及时`、`视情况`、`酌情`、`合理`、`相应`、`必要`（方案定稿 §4.2 例举 + 当前结论层 v1 正在用的 VAGUE_TERMS 字典合并）。
**R3-1-2** `vaguenessNote` 必须**逐词列出命中项**，逗号分隔；例：`"适当,视情况"`。
**R3-1-3** 量词的"适当增加/适当减少"也算模糊；机构的"有关部门"默认模糊，但"国家发改委"等具体机构名不算。
**R3-1-4** 词表**不扩展**（本阶段冻结这 10 词）；后续需加词必须升级口径句清单并评审。

---

## §4 confirmationStatus 初始值

**R4-1-1** 本阶段抽取的所有 `NormativeStatement.confirmationStatus` **一律初始化为 `DRAFT`**；严禁自动填 `CONFIRMED`。
**R4-1-2** `confirmedBy` / `confirmedAt` 本阶段**一律为 null**；专家签字流程属 2c 阶段，不提前。
**R4-1-3** 结论层**只读 `CONFIRMED` 的 statement**（方案定稿 §5 判定算法第 1 步），所以本阶段抽取的 statement 在 2c 之前不会被用于判定——这是预期行为。

---

## §5 ClauseCondition 条件树结构

**R5-1-1** 条件树是**纯逻辑骨架**（AND/OR/NOT + LEAF）；本阶段**只抽根节点**和叶子，不做深层嵌套（方案定稿 §5 允许"抽取时先做扁平层"）。
**R5-1-2** `LEAF` 节点 `operator="LEAF"` 时必须填 `leftOperand` + `comparator` + 右侧值；其他三操作符填 `parentConditionId`（根节点 `parentConditionId=null`）。
**R5-1-3** 本阶段 `factSource` **一律填 `USER_INPUT`**（图谱属性 / 时序值类型属 2c 编译阶段才解析的）。
**R5-1-4** `leftOperand` 规范化：品种类条件统一用 `"facts.targetObject"`，地域类用 `"facts.regionCode"`，机构类用 `"facts.issuer"`——方案定稿 §4.3 允许"属性路径或指标码"，但本阶段只抽用户输入相关。
**R5-1-4a（v2）** 条件抽取按"语义最具体优先"三级降级，命中即停：
  1. 空间禁限类 → `facts.landType`，comparator `∈`，右侧为条款点名的区域类型（`25度以上坡耕地`、`严格管控类耕地`、`生态保护红线`、`退耕还林还草还湖还牧区域`、`生态脆弱区`、`滩涂`），须由「禁止/不得/严禁/严格限制/不能」引导；
  2. 污染防治类 → `facts.wasteType`，comparator `∈`，右侧为「严禁/禁止/不得 + 将…、…、… + 排放/倾倒/存放/堆放」中被点名的废弃物；
  3. 对象类 → `facts.targetObject`，comparator `=`，右侧为条款 `targetObject`（兜底）。
**R5-1-4b（v2）** 三级均不命中时**不产条件**（`conditionId` 为空），禁止用 `facts.targetObject` 硬套不相关条款——空条件在 2c 编译期按 `NO_CONDITIONS` 跳过，语义上安全。
**R5-1-4c（v2）** `TARGET` 类陈述一律**不产适用条件**：目标回答的是"是否达标"，不是"规则是否适用"，二者不可共用一个条件槽；目标类陈述保留在陈述层供达成度评估，不进入 2c 规则编译。

---

## §6 ClauseException / ClauseConsequence / RuleParameter

**R6-1-1** 本阶段抽取 `ClauseException` **仅当**陈述里显式含「但/但是/除外/另有规定的除外/除非…」这类连词引导的独立子句；否则不产例外。
**R6-1-1a（v2）** 例外连词必须出现在**括号（…）/（…）/【…】与引号之外**才算独立子句；括注内的"（…除外）"属限定语，不抽例外（与 R1-1-2 的括号保护口径一致）。
**R6-1-2** 例外条件的 `operator` 一律按 LEAF 抽，不做嵌套（同 R5-1-1）。
**R6-1-3** `ClauseConsequence` 抽取**仅当**陈述显式含动作+量值（如"最低收购价为每50公斤128元"→ consequenceType=`MEASURE`，parameterIds 指向 RuleParameter）；纯定义类（`statementType=DEFINITION`）的量值进 RuleParameter 不进 Consequence。
**R6-1-4** `RuleParameter` 的 `paramValue` 必须能**解析成数字**；金额、比例、配额、期限、数量都算。
**R6-1-5** `ClauseRelation`（条款间关系，§4.7 的 `prevails` 等）本阶段**不产出**——无规则引擎输入，无法抽 PREVAILS_OVER；留空，等待 2c 编译时同时产生。

---

## §7 与应用库的回链

**R7-1-1** 每条 `NormativeStatement` 必须回链：`clauseId → Policy_Clause.clauseId`，`Policy_Clause.policyId → Policy.policyId`；三层链必须完整才能入库，缺任一环跳过。
**R7-1-2** `actorScopeId` 本阶段**一律填 null**（应用库无自动 scope 推断逻辑，Actor 维度挂 scope 属人工标注）。
**R7-1-3** `objectFormIds` 本阶段**一律填 null**（`CommodityForm` 表无种子数据，形态对齐靠后续 LLM）。
**R7-1-4** `regionalScopeId`（ClauseCondition 的）本阶段**一律填 null**。

---

## §8 不做什么（反向冻结，防止扩张）

**R8-0-1** 不跑 Datalog / Nemo / pyDatalog，不生成 `.rls` 文件。
**R8-0-2** 不抽 `ClauseRelation(PREVAILS_OVER)`。
**R8-0-3** 不解析 `DEFINITION` 类型的"定义展开"（如"中储粮"→"中国储备粮管理集团有限公司"），留空。
**R8-0-4** 不自动填 `confirmationStatus=CONFIRMED`，也不做专家签名字段。
**R8-0-5** 不升级结论层 v1 以消费 `NormativeStatement`（这是 2c 的活）。
**R8-0-6** 不自动推断 `actorScopeId / objectFormIds / regionalScopeId`（人工标注项全部留 null）。
**R8-0-7** 不往 `mapping.json` 里注册这 6 张新表之外的表（一次性 6 张，禁止逐张加）。

---

## §9 验收口径（三态一致率等，属 2c 验收，2b 阶段不跑）

- **2b 本阶段验收只看**：种子里 POL0001 三条 Policy_Clause 切分出的 NormativeStatement 数量、statementType 分布、vaguenessFlag 打标正确率；不跑三态判定。
- 指标（待 2c 填基线）：三态一致率、UNABLE 识别率、误判致命率。

---

## §10 口径句变更锁

- 本清单**冻结后**，实现层只能按句执行；若需改任一条，必须：
  1. 改本清单，给新版本号；
  2. 注明改动理由与影响面；
  3. 重跑所有相关测试。
