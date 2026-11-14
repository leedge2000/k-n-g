# 阶段 0 · 规则路线试验报告

> 样本：《小麦和稻谷最低收购价执行预案》第五条第一款
> 试验目录：`packs/policy/rule_trial/`
> 日期：2026-11-13
> 结论一句话：**Datalog 能承载本条款的判定逻辑，但「UNABLE」与「证明链」必须留在 Python 层；规则引擎选 Soufflé 为主、pyDatalog 为开发期后备。**

---

## 1. 试验目标与判定口径

验三件事，不验性能军备竞赛：

| 序号 | 要回答的问题 | 判定口径 |
|---|---|---|
| Q1 | 一条真实政策条款能否用 Datalog 表达 | 四类条件（区域集合 / 时间窗 / 数值阈值 / 等级阈值）+ 一个例外 + 一个后果，全部落到谓词 |
| Q2 | 三态结论（APPLICABLE / NOT_APPLICABLE / UNABLE）是否可稳定复现 | 7 个情形逐条对照人工标注，全对才算通过 |
| Q3 | 两路实现的工作量与可维护性差异 | 编码行数、规则条数、回译难度、单次判定耗时 |

**样本条款结构化**：`statement_wheat_minprice.json`（9 类型条款模型中的 `NormativeStatement` + `ClauseCondition` + `ClauseException` + `RuleParameter` + `ClauseConsequence` 五个类型的实例化）。

四个前提条件与一个例外：

| 编号 | 判据 | 事实来源 |
|---|---|---|
| C1-1 | 区域 ∈ {河北, 江苏, 安徽, 山东, 河南, 湖北} | 图谱属性 |
| C1-2 | 日期 ∈ 06-01 ~ 09-30 | 情形输入 |
| C1-3 | 到库价 < 最低收购价（1.19 元/斤，示例值） | 时序值（先算成布尔） |
| C1-4 | 等级 ≤ 3 等（数字越小等级越高） | 图谱属性 |
| E1 | 该省已启动地方收储 → 本条不单独触发 | 情形输入 |

> 参数 1.19 元/斤为试验示例值，正式使用前必须按原文核对；这一点已写进 JSON 的 `note` 字段，不靠口头约定。

---

## 2. 环境实况与一处阻塞

| 项 | 实测结果 |
|---|---|
| Python | 3.12.10（MSC v.1943 64 bit），全局无 pyDatalog |
| pyDatalog | 临时 venv `%TEMP%\lytrial_venv` 安装成功，Python 3.12 下无 `collections.Mapping` 兼容问题 |
| WSL | 仅 `docker-desktop` 发行版，无常规 Linux 发行版 |
| Soufflé 原生 | `where souffle` 无输出，Windows 无官方构建 |
| Docker | 引擎 29.8.2 在跑，但出网代理指向 `socks5://127.0.0.1:3080`，该端口拒绝连接 |

**阻塞**：`docker pull soufflelang/souffle` 与镜像站 `docker.m.daocloud.io` 均在代理层失败，因此 **Soufflé 版只完成到「程序与事实齐备、可结构核对」，未取得执行结果与耗时**。这是环境问题，不是规则表达问题；解除方式见 §8。

---

## 3. 两路实现

### 3.1 共同分工（两路一致）

```
情形输入 ──► Python 求值层 ──► 派生布尔事实 ──► Datalog 判定 ──► 三态结论
             (eval_leaf)        (facts_tables)     (RULES)        (judge)
                  │                                                   ▲
                  └──────────── UNABLE 判定 + 证明链组装 ─────────────┘
```

- **数值与集合比较前置**：`price < 1.19`、`grade ≤ 3`、`date ∈ 窗口`、`region ∈ 六省` 全部在 Python 算成布尔事实，Datalog 只做组合。这符合《方案定稿》§2.3 对「Datalog 无算术」的对策，不是临时绕路。
- **事实完备性前置**：`fact_complete(C)` 由 Python 判定并作为输入事实注入。Datalog 里「事实缺失」与「事实不成立」不可区分，所以 UNABLE 只能这样表达。
- **证明链在 Python 组装**：Datalog 求值结果不含 why-provenance，`proof_chain()` 按条件顺序逐步输出 `{step, ruleId, conditionId, leftOperand, comparator, right, factRef, clausePath, quote}`。

### 3.2 pyDatalog 版（`judge_pydatalog.py`，202 行）

规则只有 2 条：

```prolog
region_ok(C) <= case_exists(C) & in_region(C, R) & allowed_region(R)
applicable(C) <= case_exists(C) & region_ok(C) & within_window(C) &
                 price_below_floor(C) & quality_ok(C) & ~reserve_active(C)
```

其余 200 行是：样本与参数加载、7 情形定义、`eval_leaf` 求值、六步 `judge`、证明链、1000 条基准、Soufflé 事实导出。

### 3.3 Soufflé 版（`souffle/wheat_minprice.dl`，52 行）

8 个输入关系 + 4 个输出关系 + 4 条规则：

```prolog
region_ok(C) :- case_exists(C), in_region(C, R), allowed_region(R).
applicable(C) :- case_exists(C), fact_complete(C), region_ok(C), within_window(C),
                 price_below_floor(C), quality_ok(C), !reserve_active(C).
unable(C) :- case_exists(C), !fact_complete(C).
not_applicable(C) :- case_exists(C), fact_complete(C), !applicable(C).
```

输入事实由 pyDatalog 脚本的 `emit_souffle_facts()` 导出（制表符分隔、无表头），**两路读同一份 `cases.json`、同一套派生语义**，保证对比公平。

---

## 4. 试验结果

### 4.1 三态判定：7/7 一致

| 情形 | 场景 | 期望 | 实际 | 命中原因 |
|---|---|---|---|---|
| TC1 | 河南 7 月 到库价 1.15 三等 无地方收储 | APPLICABLE | APPLICABLE | 四前提成立且无例外 |
| TC2 | 到库价 1.25 高于 1.19 | NOT_APPLICABLE | NOT_APPLICABLE | C1-3 不成立 |
| TC3 | 广东不在六省 | NOT_APPLICABLE | NOT_APPLICABLE | C1-1 不成立 |
| TC4 | 10 月超执行期 | NOT_APPLICABLE | NOT_APPLICABLE | C1-2 不成立 |
| TC5 | 到库价缺失 | UNABLE | UNABLE | 缺事实：到库价 |
| TC6 | 该省已启动地方收储 | NOT_APPLICABLE | NOT_APPLICABLE | E1 例外排除 |
| TC7 | 等级 4 等 | NOT_APPLICABLE | NOT_APPLICABLE | C1-4 不成立 |

**7/7 一致**，且 5 条 NOT_APPLICABLE 分别由 4 个不同条件 + 1 个例外触发，说明判定顺序没有「一条不成立就短路到同一个原因」的糊弄。

产物：`result_pydatalog.json`（含每条情形的结论、原因、证明链）。

### 4.2 耗时

| 规模 | pyDatalog | Cozo 0.7.6 | Nemo 0.10.1 | Soufflé |
|---|---|---|---|---|
| 7 个情形（含解析与加载） | 11.5 ms | **2.3 ms** | **0.6 ms** | 未取得（已放弃） |
| 1000 条合成事实 | 153.8 ms | **35.4 ms** | **1.7 ms** | 未取得（已放弃） |

说明：pyDatalog 的耗时含 `create_terms` + `load` 文本解析；Cozo 的耗时含 `db.run`（解析+求值）；Nemo 的解析在 `load_string` 阶段（计时器外），因此其数字偏乐观，但量级差异已经足够明显。

### 4.3 事实表核对（Soufflé 侧结构自检）

| 事实表 | 行数 | 是否符合预期 |
|---|---|---|
| `allowed_region` | 6 | ✓ 六省 |
| `case_exists` | 7 | ✓ |
| `fact_complete` | 6 | ✓ TC5 缺价格被剔除 |
| `in_region` | 7 | ✓ |
| `within_window` | 6 | ✓ TC4 超期被剔除 |
| `price_below_floor` | 5 | ✓ TC2 价高、TC5 缺失被剔除 |
| `quality_ok` | 6 | ✓ TC7 四等被剔除 |
| `reserve_active` | 1 | ✓ 仅 TC6 |

据此手推 Soufflé 结论应为 `applicable={TC1}`、`unable={TC5}`、`not_applicable={TC2,TC3,TC4,TC6,TC7}`，与 pyDatalog 一致；**但这是推演，不是执行证据**，报告按这个口径如实标注。

---

## 5. 五维对比

| 维度 | pyDatalog | Soufflé | 判定 |
|---|---|---|---|
| 编码工作量 | 规则 2 条；但需 Python 注入事实、处理空谓词否定、手写 `ask` 解析（引擎相关约 35 行） | 规则 4 条 + 12 个 `.decl` + 8 个 `.input` / 3 个 `.output`，52 行自包含 | Soufflé 更规整 |
| 可读性 | `<=` / `&` / `~`，Python 风格，业务人员可读 | `:-` / `,` / `!`，教科书 Datalog | Soufflé 更接近规则文献 |
| 回译难度 | 低（规则短，但事实注入散在 Python） | 低（规则集中，回译目标明确） | 平手，Soufflé 略优 |
| 1000 事实耗时 | 153.8 ms | 未实测 | 待补 |
| 可执行性 | 装上就跑，纯 Python，零运维 | 需外部二进制 / 容器，Windows 无原生构建 | **pyDatalog 明显占优** |
| 证明链 | 引擎不提供，Python 组装 | 引擎不提供，Python 组装 | 平手（都不解决） |

---

## 6. 三条硬发现

1. **UNABLE 是 Datalog 表达能力之外的东西**，不是写法问题。缺事实与事实为假在引擎里同形，所以必须引入 `fact_complete(caseId)` 输入事实，把「完备性」交给 Python。这条已从「设计假设」升级为「实测结论」，后续所有规则编译都必须带这一层。
2. **pyDatalog 对被否定的空谓词会抛 `Predicate without definition`**，必须注入哨兵事实（本次用 `reserve_active('__NONE__')`）。Soufflé 无此问题（空关系参与否定正常）。这是后备引擎的已知坑，写进模板规约。
3. **算术外置是常态而非权宜**。本条款 4 个条件里 3 个（日期窗、价格阈值、等级阈值）都得先算成布尔，Datalog 侧只剩「与/或/非」。这验证了《方案定稿》§2.3 的分层：计算函数负责数值，规则引擎负责组合，两者不越界。

---

## 7. 可复用规则模板

四层骨架，后续一级政策条款照此编写：

```
L0 事实层（Python 注入，禁止在 Datalog 里算）
   case_exists(caseId)
   fact_complete(caseId)          // 必备：承载 UNABLE
   in_region(caseId, region)       // 集合判断
   within_window(caseId)           // 时间窗
   price_below_floor(caseId)       // 数值阈值
   quality_ok(caseId)              // 等级阈值
   exception_active(caseId)        // 例外

L1 派生层（把叶子条件组合成中间谓词，保持规则可读）
   region_ok(C) :- case_exists(C), in_region(C, R), allowed_region(R).

L2 判定层（一条规则对应一条 NormativeStatement，名字 = statementId 的短码）
   applicable(C) :- fact_complete(C), region_ok(C), within_window(C), ...,
                    !exception_active(C).

L3 结论层（三态齐全，缺一不可）
   unable(C) :- case_exists(C), !fact_complete(C).
   not_applicable(C) :- case_exists(C), fact_complete(C), !applicable(C).
```

**编写规约 5 条**：

1. 规则里不出现算术、不出现字符串处理、不出现日期解析——这些一律进 L0。
2. 每条 `applicable` 规则必须配套 `unable` 与 `not_applicable`，且三者的判定域互斥、合起来覆盖 `case_exists` 全集。
3. 否定只作用于 L0 的布尔事实或 L1 的派生谓词，禁止跨层否定（保证可分层、可终止）。
4. 例外一律单独成谓词再否定，不写进 `applicable` 的条件串里（否则回译时读不出「除……外」）。
5. 若出现优先级冲突（`PREVAILS_OVER`），先落 `prevails(ruleA, ruleB)` 事实再分层否定，不在规则体里写 if-else。

---

## 8. 选型结论与待办

**结论（已于 §9 修正）：Nemo 为主、Cozo 为备选、pyDatalog 退出。**

- 主引擎选 Nemo：与 Soufflé 同属「声明式规则 → 关系代数执行」路线，且为本体推理专用，pip 原生装、Windows 直接跑。
- 备选 Cozo：规则更贴近 Python 风格，若后续需要把推理结果直接落库/做图遍历，它更顺手。
- pyDatalog 退出：库停更于 2018，不兼容 Python 3.12，且性能落后两个数量级。
- 各引擎都不负责 UNABLE 与证明链，这部分是服务层职责，属既定架构，不因引擎选择而变。

**待办**：

| 项 | 状态 | 解除办法 |
|---|---|---|
| Soufflé 实测（7 情形 + 1000 事实耗时） | 已作废 | 代理已修好，但 Soufflé 版本停更、Windows 无原生构建，改由 §9 的 Nemo / Cozo 替代，不再补测 |
| 条款参数核对 | 待办 | 最低收购价、执行期、六省名单按原文逐条核对，替换示例值 |
| 多条款组合（含 `PREVAILS_OVER`） | 已完成（见 §10） | 在 Nemo 上验通 11/11，新增第四态 `OVERRIDDEN` |

---

## 9. Soufflé 替代方案实测（2026-11-13 增补）

Soufflé 的工程形态已明显落后：最后正式版 2.4.1（2022），Windows 无原生构建，必须走 Docker，
且 Docker Hub 上 `soufflelang/souffle` 仓库并不存在。因此改为实测「能 pip 装上、Windows 原生跑」的现代引擎。

### 9.1 候选与筛选

| 引擎 | 形态 | Windows 可行性 | 结论 |
|---|---|---|---|
| **Nemo 0.10.1** | Rust，本体推理器（德累斯顿 KB 组），`pip install nemo-python` | ✅ `cp312-win_amd64` 轮子 | **入选，主引擎** |
| **Cozo 0.7.6** | Rust，内嵌图/关系数据库 + Datalog，`pip install pycozo cozo_embedded` | ✅ 纯 pip | **入选，备选** |
| DuckDB 2.x | 列存 SQL，用递归 CTE 表达 Datalog | ✅ 有轮子 | 保留为「不想引引擎」时的兜底，未实测 |
| Scallop | Rust/Python，可微 Datalog | 需自行验证 | 若后续要做概率/可微推理再看 |
| Mangle (Google) | Go，类型化 Datalog | 需自编译二进制 | 类型系统好，但分发成本高 |
| DDlog / LogicBlox / RDFox | 已停更 / 商业授权 | — | 排除 |

### 9.2 实测结果

两路实现（`judge_cozo.py`、`judge_nemo.py`）与 pyDatalog **共用同一份事实层与结论层**
（`facts_tables` / `judge` 直接 import），只替换中间的规则求值段：

| 项 | Cozo | Nemo |
|---|---|---|
| 规则行数 | 4 行（含 `?` 查询头） | 3 行 |
| 三态一致 | **7/7** | **7/7** |
| 7 情形耗时 | 2.3 ms | 0.6 ms |
| 1000 事实耗时 | 35.4 ms | 1.7 ms |
| 事实注入方式 | 常量规则 `rel[..] <- [[..]]` | 事实行 `rel("x") .` |
| 否定语法 | `not reserve_active[c]` | `~reserve_active(?c)` |
| 空谓词否定 | 需哨兵占位 | 需哨兵占位 |
| 额外能力 | 内嵌存储、图遍历、聚合、`pycozo` 可持久化 | 存在规则、聚合、数据类型、`@import` 外部表 |

**结论修正：主引擎由 Soufflé 改为 Nemo，Cozo 为备选，pyDatalog 退出（库已停更于 2018，且不兼容 Python 3.12）。**

### 9.3 为什么 Nemo 比 Soufflé 更合本体场景

1. **同一技术谱系**：Nemo 前身是 VLog，专门做本体/知识图谱上的规则推理，原生支持存在规则与聚合，
   而 Soufflé 是通用 Datalog，本体特性要靠手工编码。
2. **分发同 Python 生态**：轮子直接装，服务进程内调用，无容器、无编译、无跨进程序列化。
3. **规则文本仍是纯声明式资产**：`CompiledRule.ruleExpression` 可继续按文本版本化管理，
   引擎可替换性已在本次试验中被证明（换引擎只改 3–4 行规则 + 一次 import）。
4. **性能反超**：即便按最保守口径，Nemo 的 1000 事实耗时也仅为 pyDatalog 的 1/90。

### 9.4 风险与未验项

- Nemo 0.10 移除了 `@output` 指令，结果需通过 `engine.result("predicate")` 按谓词取；API 变动较快，
  需在 `requirements` 里锁版本（本次锁 `nemo-python==0.10.1`）。
- Nemo 返回值是带引号的字面量字符串（`'"TC1"'`），需统一剥壳，已在 `judge_nemo.py` 处理。
- **已由 §10 补验**：多条款优先级组合、`PREVAILS_OVER` 冲突消解（11/11 通过，新增第四态 `OVERRIDDEN`）。
- **仍未验证**：存在规则（本体类推理，`∃`）在 Nemo 上的写法，留到接入真实本体类层次时补。
- 依赖已落地：`duckdb 2.0.0`、`pycozo 0.7.6`、`cozo_embedded 0.7.6`、`nemo-python 0.10.1`。

---

## 10. 多条款优先级与 PREVAILS_OVER 实测（2026-11-13 增补）

§9 只验证了「一条条款能否被正确求值」。真实政策场景是**多条条款同时对同一情形成立**，
必须回答：谁生效、谁被覆盖、优先级成环怎么办。本节在 Nemo 上补齐这三问。

### 10.1 设计

三条款、两跳优先级链，`prevailsOver` 以独立元数据声明，不写进规则体：

| 条款 | 性质 | 触发要点 | 优先级 |
|---|---|---|---|
| S1 | 真实样本（同阶段 0） | 六省 + 6.1–9.30 + 价 < 1.19 + 等级 ≤ 3，例外 E1 地方临储 | 最低 |
| S2 | 试验构造（省级细则） | 同上但价 < 1.20，且省级细则已发布 | 中 |
| S3 | 试验构造（应急调控） | 执行期内 + 应急响应等级 ≤ II | 最高 |

```json
"prevailsOver": [["S2", "S1"], ["S3", "S2"]]
```

**只声明相邻两跳，S3 > S1 由引擎的传递闭包推出**——这是本节要验的核心能力。

结论集由三态扩展为四态：

| 结论 | 含义 | 与旧三态的关系 |
|---|---|---|
| APPLICABLE | 条件成立且最终生效 | 不变 |
| NOT_APPLICABLE | 本条前提不成立 | 不变 |
| UNABLE | 事实不全 / 规则冲突，不可裁决 | 不变 |
| **OVERRIDDEN** | **本条成立，但被更高优先级条款替代** | 新增 |

`OVERRIDDEN` 不能并入 `NOT_APPLICABLE`：前者要告诉用户「另有规则生效」并指出生效方，
后者只说明「本条不适用」。合并会丢掉业务上最关键的处置指引。

### 10.2 规则写法

```prolog
fires(?c, "S1") :- case_exists(?c), region_ok(?c), within_window_s1(?c),
                   price_below_s1(?c), quality_ok_s1(?c), ~reserve_active(?c) .

prevails_t(?a, ?b) :- prevails(?a, ?b) .
prevails_t(?a, ?c) :- prevails(?a, ?b), prevails_t(?b, ?c) .   // 传递闭包
overridden_by(?c, ?a, ?b) :- fires(?c, ?b), prevails_t(?b, ?a) .
overridden(?c, ?a) :- overridden_by(?c, ?a, ?b) .
effective(?c, ?s) :- fires(?c, ?s), ~overridden(?c, ?s) .
cycle(?a) :- prevails_t(?a, ?a) .
```

要点：
- `prevails` 是**数据**（可随条款版本更新），`overridden_by` 是**规则**（求值方式固定），两者分离后，政策优先级调整只改 JSON 不改程序。
- 条件谓词带条款后缀（`price_below_s1` / `price_below_s2`），避免两条款阈值不同（1.19 vs 1.20）时串味。
- 传递闭包写成线性递归 `prevails(?a,?b), prevails_t(?b,?c)`，Nemo 可直接求值。

### 10.3 结果：11/11 一致

| 情形 | 场景 | S1 | S2 | S3 |
|---|---|---|---|---|
| TC1–TC7 | 阶段 0 基线（新增字段置空） | 与原 7/7 完全一致 | 均 NOT_APPLICABLE | 均 NOT_APPLICABLE |
| TC8 | S1 与 S2 同时成立 | **OVERRIDDEN**（by S2） | APPLICABLE | NOT_APPLICABLE |
| TC9 | 三条同时成立 | **OVERRIDDEN**（by S2，生效 S3） | **OVERRIDDEN**（by S3） | APPLICABLE |
| TC10 | 缺 price 且 S2 可触发 | UNABLE | UNABLE | NOT_APPLICABLE |
| TC11 | 价格 1.195 落在两门槛之间 | NOT_APPLICABLE | APPLICABLE | NOT_APPLICABLE |

TC1–TC7 全绿说明**加条款没有回退单条款语义**；TC8/TC9 说明 `prevails` 与传递闭包按预期生效；
TC10 说明 UNABLE 不会被覆盖；TC11 说明两条门槛不同的条款不会互相污染。

成环探测（独立用例，注入 `S1↔S2`）：`cycle = ['S1', 'S2']`，检出成功。

### 10.4 四条硬发现

1. **UNABLE 在覆盖关系上是双边不可让渡的。**
   被覆盖方缺事实时不能判 `OVERRIDDEN`（不知道它本会不会成立）；
   覆盖方缺事实时也不能去覆盖别人（TC10：S2 自身 UNABLE，S1 必须保持 UNABLE）。
   因此覆盖消解**必须在结论层做第二遍**，不能只靠 `overridden_by` 的 Datalog 结果——
   引擎只看得见「事实成立与否」，看不见「事实是否齐全」。

2. **条款内例外与条款间优先级是两套机制，不能混用。**
   TC6 里地方临储触发 S1 的 `E1` 例外，S1 判 `NOT_APPLICABLE`；
   TC8 里省级细则触发 `prevails`，S1 判 `OVERRIDDEN`。
   同样是「S1 不生效」，结论不同、处置不同：前者要说明「被例外排除」，后者要指向「生效条款是谁」。
   阶段 0 把例外写成 `~reserve_active(?c)` 是对的，但如果把它误当优先级，就会丢掉「谁取代了它」这条信息。

3. **优先级链交给引擎求闭包，比 Python 手写 DFS 更可控。**
   `S3 > S2`、`S2 > S1` 两条事实即可推出 `S3 > S1`；条款数增长时不需要改代码，
   且成环、重复声明、孤立节点都能在同一套规则里检出。

4. **成环是配置错误，应在加载期一次性拒绝，而不是逐情形判定。**
   本次把成环检测拆成独立用例（`cycle_probe`），而不是塞进 11 个情形。
   理由：`prevails` 是全局元数据，一旦成环，所有情形都不可信；逐情形重复报同一个错是噪音。
   服务层应在装载规则包时跑一次 `cycle`，非空即拒绝启动。

### 10.5 性能

| 规模 | Nemo | 说明 |
|---|---|---|
| 11 情形 × 3 条款 | 1.4 ms | 含 `reason()` 与三次结果集拉取 |
| 1000 条情形 × 3 条款 | 2.4 ms | 3000 次条款判定 |

条款数从 1 增到 3，1000 条规模耗时仅由 1.7 ms 增至 2.4 ms，说明多条款开销主要在事实注入而非规则求值。

### 10.6 未验项

- 存在规则（`∃` 本体类推理）仍未验，留到接入真实本体类层次时补。
- 条款数达到数十条时 `prevails_t` 的闭包规模会到 O(n²)，需要观察是否需要按条款族分片求值。
- 本次 S2/S3 为试验构造条款，优先级方向（省细则 > 国家预案、应急 > 常态）符合政策实践直觉，
  但**正式接入前必须用真实文号与条文替换**，不能沿用本节的示例表述。
