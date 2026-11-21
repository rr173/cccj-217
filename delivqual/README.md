# delivqual — 离线数据交付资格核验器

数据团队把多个采购来源加工成几份交付表，每个来源可能提供多个授权选项。本工具读取
**加工关系**、**实际贡献证据** 和 **本题定义的机器可读授权条款**，为一次交付求出
**全局一致的来源选项与义务**，或指出 **可复查的真实冲突**。

本工具只执行给定的合成规则：不解释真实许可证文本，不作法律判断，不重新计算上游
业务值（推导事实由输入方提供，本工具核验其结构、路径与授权）。

无任何第三方依赖（纯 Python 3.8+，内置 CDCL SAT 求解器）。

## 快速开始

```sh
cd delivqual
# 主演练：R 直交付、G 聚合交付同一源 S
./dq plan --sources examples/main_drill/sources.json \
          --graph  examples/main_drill/graph_v1.json \
          --request examples/main_drill/req_RG.json
# ⇒ infeasible（S 不能同时选 X=raw 与 Y=aggregate）

./dq plan --sources examples/main_drill/sources.json \
          --graph  examples/main_drill/graph_v2.json \
          --request examples/main_drill/req_RG.json
# ⇒ feasible：S/X + T/A，notice 为 n-SX 与 n-TA（不含未使用的 S/Y）

# 一键复跑全部验收
./run_all.sh
```

四个子命令（`./dq` 等价于 `python3 -m delivqual`，也可用
`PYTHONPATH=. python3 -m delivqual`）：

| 命令 | 作用 | 退出码 |
|---|---|---|
| `validate`   | 校验输入结构、引用、环、原子登记 | 0 通过 / 10 有错 |
| `plan`       | 求全局一致计划或冲突证书 | 0 可行 / 11 不可行 / 12 unknown |
| `explain-row`| 解释某一输出行的推导与义务 | 0 |
| `verify-plan`| 只根据原资料与 plan 独立核实全部合同 | 0 有效 / 13 拒绝 / 12 unknown |

`plan` 支持 `--budget-seconds S` 与 `--budget-conflicts N`：预算耗尽返回
`unknown`（退出码 12），**绝不谎报可行或不可行**。

## 输入格式

### sources.json —— 源登记与机器可读授权条款

```jsonc
{
  "kind": "sources", "version": 1,
  "sources": [
    {
      "source_id": "S", "snapshot": "s1",        // 源 = (source_id, snapshot)，snapshot 不可变
      "rows": ["s-r1", "s-r2"],                  // 可引用的原行 ID 清单
      "options": [
        {
          "option_id": "X",
          "ops": ["raw"],                        // 允许的操作集合，词汇：raw/aggregate/join/union/project
          "audiences": ["dept-a"],               // 受众集合（有限原样字符串）
          "geos": ["cn"],                        // 地域集合
          "time": [0, 1000],                     // 有效整数时间区间 [lo, hi]
          "notices": ["n-SX"],                   // 必须附带的 notice_id 集合
          "classifications": ["report"],         // 允许的输出分类
          "mutex": [{"source_id": "T", "snapshot": "t1", "option_id": "A"}]  // 互斥选项清单
        }
      ]
    }
  ]
}
```

### graph.json —— 加工无环图 + 实际贡献表达式 + 输出表

```jsonc
{
  "kind": "graph", "version": 1, "graph_id": "main-drill-v1",
  "nodes": [                                    // 加工节点，无环
    {"node_id": "scanS", "op": "scan", "inputs": []},
    {"node_id": "aggS",  "op": "aggregate", "inputs": ["scanS"]}
    // op ∈ scan / union_all / join / project / aggregate / deduplicate
    // 元数：scan=0，project/aggregate/deduplicate=1，join/union_all≥2
  ],
  "tables": [                                   // 输出表挂在终态加工节点上
    {"table_id": "G", "node_id": "aggS", "rows": ["G1"]}
  ],
  "contribs": [                                 // 实际贡献表达式（可共享节点的 DAG）
    {"id": "cS1", "type": "source", "source_id": "S", "snapshot": "s1", "row_id": "s-r1"},
    {"id": "cG1", "type": "all", "edges": [   // all = 必须共同使用
      {"child": "cS1", "via_node": "aggS", "via_row": "G1"}   // 每条边必须指向加工节点与行引用
    ]},
    {"id": "cG2", "type": "any", "edges": [   // any = 任一条充分推导（只能来自显式 deduplicate）
      {"child": "cViaS", "via_node": "dedG", "via_row": "G2"},
      {"child": "cViaT", "via_node": "dedG", "via_row": "G2"}
    ]}
  ],
  "outputs": [                                  // 每个最终输出行挂一棵贡献表达式
    {"table": "G", "row": "G1", "expr": "cG1"}
  ]
}
```

贡献表达式的结构规则（`validate` 强制）：

- 叶子只能是 `source` 原子，且必须指向已登记的 (source_id, snapshot, row_id)。
  **不存在“已脱敏”之类的免除机制**——未知节点类型一律拒绝。
- `all`（必须共同使用）：多条边必须指向**同一个** `join` 或 `aggregate` 节点
  （join 的真实贡献组合、aggregate 的分组输入）；单条边可经任意一元加工节点。
- `any`（充分替代）：≥2 条边且全部指向**同一个** `deduplicate` 节点。
  union_all 产生的同值行保持独立行，不得用 any 合并；相同值的替代推导不会被
  误作必须同时使用。
- 贡献图与加工图都必须无环；每条贡献边必须带 `via_node` + `via_row`，使不同
  操作路径可分别回读。

### request.json —— 交付请求

```jsonc
{
  "kind": "request", "version": 1, "request_id": "req-RG",
  "time": 100, "audience": "dept-a", "geo": "cn",
  "tables": [
    {"table_id": "R", "classification": "report"},
    {"table_id": "G", "classification": "report"}
  ]
}
```

## 语义规则（本题的合成规则）

- **全局唯一选项**：同一 (source_id, snapshot) 在一次交付中只选一个 option，
  穿过几条支路、交付几张表也不能分别选不同项；同一源被多表使用时要求合并。
- **路径操作需求**（按每条选定贡献路径确定）：
  - 路径含 `aggregate` ⇒ 需要 `aggregate`；否则在交付处需要 `raw`
    （raw 与 aggregate 是两种交付许可模式，不把 raw 强加到 aggregate 路径上）；
  - 路径中的 `join` / `union_all` / `project` 分别要求 `join` / `union` / `project`；
  - `deduplicate` 只记录充分替代，不新增操作权限，也不删除来源义务；
  - `scan` 不新增操作权限。
- **义务不被加工消除**：project 不移除已贡献来源；aggregate 不消除 notice、
  受众、地域、分类或互斥义务。
- **未使用支路不增义务**：只出现在未选支路中的源不产生任何要求。
- **可行交付**：为每个交付行挑一条充分推导，给用到的每个源选定一个全局一致
  option，满足全部路径操作、请求受众/地域/时间、各表输出分类与选项互斥。
- **notice**：实际所选源 option 的并集，附源、行和选择依据。
  不要求最少 notice 或最少来源——这不是优化题。

## plan 输出

```jsonc
{
  "kind": "plan", "version": 1,
  "status": "feasible",                    // feasible / infeasible / unknown
  "identity": {"sources_sha256": "...", "graph_sha256": "...", "request_sha256": "..."},
  "request": { /* 精确请求 */ },
  "options": [{"source_id": "S", "snapshot": "s1", "option_id": "X"}],
  "rows": [                                // 每行所选推导
    {"table": "R", "row": "R1",
     "derivation": {"root": "cR1", "nodes": ["..."], "edges": [
       {"parent": "cR1", "child": "cS1", "via_node": "scanS", "via_row": "R1"}]},
     "atoms": [{"source_id": "S", "snapshot": "s1", "row_id": "s-r1",
                "path_ops": [], "requires": ["raw"], "tables": ["R"]}]}
  ],
  "notices": [                             // 所选 option 的 notice 并集
    {"notice_id": "n-SX", "source_id": "S", "snapshot": "s1", "option_id": "X",
     "rows": {"rows": ["R1"], "total": 1, "truncated": false},
     "rationale": {"ops_required": ["raw"], "tables": ["R"], "...": "..."}}
  ],
  "conflict": null,
  "stats": {"sat_vars": 34, "sat_clauses": 41, "solve_seconds": 0.0005, "...": "..."}
}
```

不可行时 `conflict` 给出 **足以重现矛盾的来源/输出要求**（不要求最小，但全部由
可复查输入构成）：

```jsonc
"conflict": {
  "categories": ["操作未授权"],   // 操作未授权 / 时间、受众、地域不符 / 输出分类不符
                                  // 全局选项矛盾 / 缺失贡献 / 缺失可用选项
  "requirements": [{"id": "R2", "kind": "op", "require": "raw", "detail": "...", "..."}],
  "involved_sources": [{"source_id": "S", "snapshot": "s1"}],
  "certificate": {"rows": [{"table": "G", "row": "G1"}, {"table": "R", "row": "R1"}],
                  "note": "仅引用这些输出行（全部源与选项可用）即可重现矛盾"}
}
```

`verify-plan` 对可行 plan 重走每行推导、重算路径义务、重算 notice 并集逐项比对；
对不可行 plan，先核对证书中每条要求确为输入事实，再在证书引用的输出行子实例上
独立重判不可行（小规模用暴力枚举，否则重新求解，均有预算）。删掉一个 notice、
换一个 option、串到另一快照或另一请求，都会被拒绝。

## 设计要点

- **全局编译而非逐行拼接**：贡献 DAG 的每个节点/边只建一次变量；路径性质
  （含 aggregate / 无 aggregate / 含 join/union/project / 属于某表）以
  “存在一条所选路径”的单调方式沿所选边传播；同一源的全部原子共享同一组
  option 变量 ⇒ 跨支路、跨表自动合并要求。支持共享子表达式，**不预先展开
  笛卡尔推导**。
- **假设核即冲突证书**：每条“要求”子句挂一个激活字面量作为求解假设；
  UNSAT 时求解器返回的假设核就是一组可复查的矛盾要求，再按种类归类解释。
- **内置 CDCL SAT 求解器**（`delivqual/sat_cdcl.py`）：watched-literal、1-UIP、
  VSIDS、阶段保存、Luby 重启、假设求解与冲突核、冲突/时间双预算。
  返回 SAT 前整体复核模型；预算耗尽返回 unknown。
- **独立验收**：`delivqual/brute.py` 是独立实现的暴力枚举器（枚举源选项与
  有限推导），只用于小例对照与证书复核；`tests/sat_selftest.py` 用随机 CNF
  对照暴力判定检验求解器本身。

## 样例清单（examples/）

| 目录 | 内容 |
|---|---|
| `main_drill/` | 主演练：S 有 X(raw)/Y(aggregate)；v1：R、G 分别可行、R+G 不可行；v2：给 G 增加 T 的充分替代推导后 R+G 可行（S/X + T/A，notice 不含 S/Y） |
| `mutex/`      | 两源选项互斥：M1⊥N1 同表交付不可行（全局选项矛盾）；M1+N2 可行 |
| `diamond/`    | 同一贡献经共享菱形（自 join）重复出现并跨表复用，要求合并后选 D/P |
| `bad_any/`    | any 中掺入一条越权推导（join 未授权），求解器选合法支路；`graph_broken.json` 演示结构校验拒绝（未登记原行、“已脱敏”免除、any 非 dedup 产生、悬空边） |
| `mismatch/`   | 受众不符、时间不符（分别不可行），以及可行对照 |
| `missing/`    | 表行缺少贡献表达式 ⇒ 缺失贡献 |
| `gen_large.py`| 运行时生成大例（不落地预置大文件） |

## 复跑入口

```sh
./run_all.sh                 # 全部：校验样例 + 主演练 + 验收套件 + 大例基准
python3 tests/sat_selftest.py    # 求解器随机 CNF 对照（8000 用例）
python3 tests/acceptance.py      # 验收套件（小例/篡改/随机/大例/预算）
python3 examples/gen_large.py --out /tmp/large --sources 24 --rows 12000 --tables 4 --seed 7
```

最近一次大例基准（24 源 × 4 选项、21090 贡献节点、22901 贡献边、12000 输出行、
高度共享：12000 行收敛到 8868 个可达贡献节点、19 个源被实际使用）：

- SAT 规模：82936 变量 / 135029 子句 / 10081 假设
- 求解：feasible，约 3–5 s；独立 verify-plan：约 1 s
- 预算 `--budget-seconds 0` ⇒ `unknown`（不谎报）
