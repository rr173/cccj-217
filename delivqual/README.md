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

四个核验子命令加一个离线构建命令（`./dq` 等价于 `python3 -m delivqual`，也可用
`PYTHONPATH=. python3 -m delivqual`）：

| 命令 | 作用 | 退出码 |
|---|---|---|
| `validate`   | 校验输入结构、引用、环、原子登记 | 0 通过 / 10 有错 |
| `plan`       | 求全局一致计划或冲突证书 | 0 可行 / 11 不可行 / 12 unknown |
| `explain-row`| 解释某一输出行的推导与义务 | 0 |
| `verify-plan`| 只根据原资料与 plan 独立核实全部合同 | 0 有效 / 13 拒绝 / 12 unknown |
| `build-totals`| 从两份明细生成部门汇总表与匹配的行级贡献图 | 0 成功 / 10 输入不符 / 2 IO 错误 |
| `quarantine` | 按失效原行清单隔离交付包：撤下失去充分凭据的记录 | 0 成功 / 10 输入不符 / 2 IO 错误 |
| `stitch-lineage` | 按接续清单把分阶段落盘的图接成终端报表的一张行级贡献图 | 0 成功 / 10 无法接通或输入不符 / 2 IO 错误 |

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

## build-totals：从明细到部门汇总（含可回查证据图）

`build-totals` 不扩展授权规则或求解器：它从两份实际明细**计算数值与证据**，产出的
`graph.json` 完全沿用上面的既有结构，可直接被 `validate` / `plan` /
`verify-plan` / `explain-row` 读取。

```sh
./dq build-totals \
  --sources examples/build_totals/sources.json \
  --purchase examples/build_totals/purchase.jsonl        `# 采购明细` \
  --attribution examples/build_totals/attribution.jsonl  `# 归属明细` \
  --purchase-source P --purchase-snapshot p2026 \
  --attribution-source A --attribution-snapshot a2026 \
  --out-dir /tmp/totals-out     # 必须此前不存在；成功后目录才可见
# ⇒ /tmp/totals-out/totals.jsonl + graph.json
```

### 算账规则（精确整数，无浮点）

- 采购行 `{row_id, join_key, invoice_id, cents}`；归属行
  `{row_id, join_key, department}`。标识与部门为非空字符串、按原值比较；
  `cents` 为有符号整数（布尔值拒绝）。同一输入内 `row_id` 唯一且必须在所绑定
  快照的 `rows` 清单中；两份输入绑定到**两个不同**的已登记源快照（两侧可用相同
  字面行号，因为原子由 source_id+snapshot+row_id 共同定位）。
- 两侧按 `join_key` 内连接，未匹配原行不计入；连接结果投影为
  `(department, invoice_id, cents)`，完全相同的三元组只计一次，再按部门对
  `cents` 求精确整数和。**不同发票的等额事实各计一次**；部门内事实相加为零仍
  输出该部门；没有匹配时输出空表与空图结果。
- `totals.jsonl` 每行 `{row_id, department, total_cents}`，`row_id` 为稳定编号
  （部门名排序后的 `T0001`…），每个 ID 在图中恰好对应一个根（表 id 固定为
  `totals`，挂在 `aggregate` 终态节点上）。

### 证据图怎样回查（含线性压缩）

生成的加工主干为 `scan →（每等价类一个）deduplicate → join → project →
（事实级）deduplicate → union_all → aggregate`，贡献表达式仍是
`source/all/any`，全部规则由既有 `validate` 强制：

- **逻辑等价类**：同一侧、同一 `join_key` 且非行号字段完全相同的原行归为一类。
  类内每行都是该类输出的充分替代凭据，用显式 `deduplicate` + `any` 表达；类的
  输出只物化一次。一次真实连接是“采购类 × 归属类”两个类输出的 `all` 共同贡献
  （两条边同指 `join` 节点），**不枚举 m×n 个物理行配对**。
- **替代只经显式去重**：相同三元组经不同配对/不同键得出时，只能通过事实级
  `deduplicate` + `any` 合并；等额但发票不同的事实各自独立，绝不合成一个；
  不同键的两行不可能出现在同一连接证据中（连接只在类内容的键相等时生成）。
- 审计展开语义：`all` = 共同贡献（笛卡尔组合，一次连接展开为一对真实原行），
  `any` = 充分替代（任选一条）。测试套件用独立暴力实现逐事实核对：图中每条可
  展开选择都是同键真实物理配对，配对集合与暴力结果逐对相等。

### via_row 引用格式（无统一占位行号）

| 形式 | 出现的边 | 解读 |
|---|---|---|
| `p-r3` / `a-r2`（原行号字面值） | scan、类级 deduplicate 的 any 边 | 该阶段的真实输入行；any 边指类内一条在去重后存活的候选原行 |
| `C:<16位哈希>` | 类包装 → 去重节点 | 等价类输出行；哈希由类业务内容（join_key 与非行号字段）确定，沿子 any 可展开到全部成员原行 |
| `J:<哈希>#L` / `#R` | 连接 `all` 的两条边 | 该连接输出行的左/右侧；哈希由两个类的内容确定，子树可展开到该侧任一成员 |
| `J:<哈希>` | project 边、事实级去重 any 边 | 一对类连接投影出的三元组行 |
| `F:<哈希>` | aggregate 边 | 去重后的业务事实（聚合输入）；哈希由 `(department, invoice_id, cents)` 确定 |
| `T0001`… | 输出表行号 | 汇总行，即 `totals.jsonl` 的 `row_id` |

所有哈希均为**内容寻址**（规范化 JSON 的 sha256 前 16 位），只与业务值有关：
仅改变任一输入文件的行序，`totals.jsonl`、`graph.json` 与全部引用逐字节不变。

### 失败与交付语义

- 绑定未登记/两份输入同快照、重复行号、行号不在快照清单、坏记录（坏 JSON、
  字段缺失/多余、空字符串、布尔或非整数 `cents`）一律退出 10，错误带文件名与
  行号。
- 输出目录已存在则拒绝（退出 10）且不改写；先写同级临时目录、两份文件都成功后
  一次性 `os.replace` 改名，任何失败都清理临时目录，调用者读不到半份交付。
- 重复键大样例（两侧各数千行、非行号字段全同）下，图随原行数**线性**增长：
  连接贡献节点数 = 键数而非物理配对数；30 万物理配对在测试中压缩为 6000 输入行
  量级的图，同时仍可展开回全部 30 万个真实配对。大样例运行时生成，不提交大数据。

## quarantine：把已归档交付包隔离成可替换旧包的副本

供应方确认部分**原始记录**有误后，数据管理员需要一份隔离副本：数值仍有完整凭据的
记录原样留下，失去充分凭据的记录撤下，并向收件人说明每条撤下的可复查理由。这不是
重算报表——一个总额原来含三笔事实、其中一笔无可靠证据时，撤下的是整条记录而不是
减掉那笔后沿用原行 ID。

```sh
./dq quarantine \
  --sources examples/quarantine/sources.json        `# 原源登记（只读，不修改、不作授权决定）` \
  --graph   examples/quarantine/graph.json          `# 原贡献图（只读）` \
  --records examples/quarantine/records.jsonl       `# 交付记录，每行 {table, row, data}` \
  --invalidations examples/quarantine/invalidations.json `# 失效原行清单（JSON 数组）` \
  --out-dir /tmp/quarantine-out     # 必须此前不存在；成功后目录才可见
# ⇒ /tmp/quarantine-out/{rows.jsonl, graph.json, withdrawals.json}
```

### 输入契约

- **交付记录 JSONL**：每行恰好是 `{"table", "row", "data"}`，前两项是与图对应的字符串，
  `data` 是已有业务内容（本工具不解释其中字段）。记录与原图 `outputs` 所列
  `(table, row)` **一一对应**：缺、多、重复（哪怕出现在文件最后一行）都是输入错误；
  乱序允许，只要集合相等且不重复。每条图列表行都必须有贡献根，包括最终会被撤下的记录。
- **失效清单**：`[{"source_id", "snapshot", "row_id"}, ...]`，以三项原值共同定位。
  重复项视为同一次失效；引用未登记源快照/原行是输入错误；已登记但未参与本次交付的
  失效行不影响结果（计入 stats 的 `invalidations_unused`）。

### 隔离判定（结构性凭据规则，不重算业务值）

- `source` 叶子命中失效三元组即失效；
- **`all`（共同贡献）**：任一必需分支失效即整节点失效——少一条都不能成立；
- **`any`（充分替代）**：只有全部替代边都失效才失效。

记录行只要还存在至少一条**完全不使用失效原行**的完整推导，就按原输入顺序、原记录
内容（原 `(table, row)` 与 `data`）保留在 `rows.jsonl`；否则从副本与图的表行中一起
撤下。

### 隔离图（可继续交给 `dq validate` / `dq plan`）

- 贡献表达式表达**恰好全部**仍然健全的推导，而不是每行只挑一条当前可用路径：
  另一条健全路径可能是以后资格核验唯一可用的选择，故 `all`/`any` 的存活边全部保留。
- 存活边保留原加工步骤（`via_node`）与 `via_row`；只剩一条替代的 `any` 节点**不**被
  直接接到更上层（仍挂在原 `deduplicate` 节点上），避免丢失路径。`validate` 对带
  `quarantine` 标记的图允许这种单边 `any`，普通图仍要求 ≥2 条替代边。
- 结果中不留从保留记录到不了的贡献节点；加工节点与**表定义全部保留**（即使某表
  所有行都撤下，表仍以空行清单存在）。
- 新图带 `quarantine` 标记（含原图/失效清单哈希与撤下身份），`graph_id` 与原图不同；
  保留记录仍用原有 `(table, row)` 与原贡献根 id。

### withdrawals.json：收件人可独立复查的撤下凭据

```jsonc
{
  "kind": "quarantine-withdrawals", "version": 1,
  "binding": {                       // 绑定原图、源登记与失效清单的内容哈希
    "origin_graph_sha256": "...", "sources_sha256": "...",
    "invalidation_sha256": "..." },
  "invalidations": [ /* 去重排序后的失效三元组 */ ],
  "invalidation_entries_input": 3,  // 清单原始条目数（重复项也计数）
  "withdrawals": [                  // 每条撤下记录及其原贡献根
    {"table": "R", "row": "R1", "origin_root": "cR1", "reason": "r-all-..."} ],
  "reasons": [                      // 共享的理由 DAG（按 id 去重，不逐行复制证据树）
    {"id": "r-src-...", "kind": "invalidated_source",
     "contrib_id": "cS1", "source_id": "S", "snapshot": "s1", "row_id": "s-r1"},
    {"id": "r-all-...", "kind": "required_branch_failed", "contrib_id": "cR1",
     "failed_edge": {"child": "cS1", "via_node": "scanS", "via_row": "R1",
                     "reason": "r-src-..."}},
    {"id": "r-any-...", "kind": "all_alternatives_failed", "contrib_id": "cR2",
     "edges": [ {"child": "...", "via_node": "dedR", "via_row": "R2", "reason": "..."}, ... ]}
  ]
}
```

**只用原图 + 失效清单的复查方法**（测试套件中的独立复查器就是这样做的）：

1. 核对 `binding` 三个哈希与手上的原图、sources、清单（去重排序）一致；
2. 对原图独立做一次活性求值（source 命中清单即死；`all` 任一分支死则死；
   `any` 全死才死），撤下集合必须与 `withdrawals` 完全一致；
3. 沿每条撤下记录的 `reason` 链走原图的**具体边**：
   - `invalidated_source` 的三元组必须确实在失效清单中；
   - `required_branch_failed`（共同贡献）指出一条已失效的必需边，该边必须是原图真实边；
   - `all_alternatives_failed`（充分替代）必须**逐条**覆盖原图该节点的每条替代边并各附理由。
4. 理由只允许引用从撤下根可达的失效节点（不附与撤下无关的节点）；同一失效贡献节点
   在所有撤下记录间共享同一个理由 id，不按输出行重复复制。没有撤下记录时仍交付
   `withdrawals: []`、`reasons: []` 的空凭据。

### 流式、规模与原子交付

- 交付记录可能远大于血缘图：处理时逐行校验、逐行复制载荷，内存只索引记录身份
  （`(table, row)` 字符串），不累积整份 `data` 载荷；测试用 30MB 载荷验证峰值内存
  增长远小于载荷总量。
- 活性求值是贡献 DAG 上的一次线性扫描，共享节点只算一次；不枚举任何可能的完整
  推导，理由 DAG 也不按输出重复遍历同一共享子图（运行时样例中 800 条撤下记录共享
  同一个根理由，理由节点数与输出数无关）。
- 目标目录必须此前不存在；先写同级临时目录，三份文件都成功后一次性 `os.replace`
  改名。任何输入不一致（含文件最后一行才发现的重复/无效记录）或写入失败都清理临时
  目录、绝不发布残缺副本，也不修改任何原文件或已有目标。

## stitch-lineage：把分阶段落盘的血缘接成终端报表的一张行级贡献图

采购数据分阶段归档：下游计算读取的是上游已经落盘的报表，终端报表的血缘追踪常常停在
“读取中间表”这一步。中间表**不是**新的原始来源，不能为它登记替代原来源的许可。
`stitch-lineage` 只处理血缘：沿清单把各阶段的图接成一张从终端输出行可直接追踪到真实
原行的贡献图，**不重跑各阶段的业务计算，也不读取或交付报表载荷**。

```sh
./dq stitch-lineage \
  --sources examples/stitch/sources.json      `# 全阶段共用的真实原行登记（只读）` \
  --manifest examples/stitch/manifest.json    `# 终端阶段 + 各阶段图路径/摘要 + 扫描绑定` \
  --out /tmp/stitch/stitched.json             `# 必须此前不存在；失败不留半份文件`
```

### 接续清单（manifest）字段

```jsonc
{
  "kind": "stitch-manifest", "version": 1,
  "terminal": "terminal",                     // 唯一终端阶段名（必须出现在 stages 中）
  "stages": [                                 // 各阶段的唯一名称、图文件路径及文件字节 SHA-256
    {"name": "alpha", "graph": "alpha.json", "sha256": "<64位十六进制>"},
    {"name": "beta",  "graph": "beta.json",  "sha256": "..."},
    {"name": "terminal", "graph": "terminal.json", "sha256": "..."}
  ],
  // 相对路径以清单所在目录为准；一个扫描节点只能有一个目标；
  // 同一张中间表可以被多个扫描节点读取（各自一条绑定）。
  "bindings": [
    {"stage": "beta", "scan": "scanS",
     "producer_stage": "alpha", "producer_table": "M"},
    {"stage": "terminal", "scan": "scanN",
     "producer_stage": "beta", "producer_table": "N"}
  ]
}
```

### 阶段图里怎样指出接缝

各阶段内部沿用现有图格式，只新增一种**仅作输入**的贡献叶：

```jsonc
{"id": "该叶的局部ID", "type": "import", "scan": "本阶段扫描节点ID", "row": "读到的上游行ID"}
```

- 普通 `source` 叶仍然指向 `sources.json` 中的真实原行；`import` 叶不是来源登记，
  它必须经清单绑定定位到上游表中**恰好对应**的已登记贡献根，不能按全局同名行猜测。
- 各段可以独立编号：图名、加工 ID、表达式 ID、行 ID 在不同段重复都不是冲突；
  结果图用 `阶段名::局部id` 作为全局 id，把不同阶段的同名节点分开，阶段名不得含 `::`。
- 清单绑定的四种无法接通情形全部拒绝并指出位置：扫描未绑定、绑定找不到目标表、
  行不在目标表、行虽列在表中却没有贡献根；另有文件字节 SHA-256 不符（同名图被替换）
  与跨阶段依赖环检查。

### 接好后的结果图

- 结果仍只有终端阶段原来的表、行及其贡献根：**表 id 与行 id 不变**；从这些根继续追踪
  可以穿过任意层已物化的中间表直至真实原行。
- 上游的共同贡献（`all`）与充分替代（`any`）是实际被读取那一行的凭据：不择一保留、
  不把两条凭据混成一条，也不为每条下游路径拷贝一棵上游树——同一 `(生产表, 行)` 的
  上游子 DAG 在结果中只接一次，多处引用共享同一棵树。
- 加工图也连通：读中间表的 scan 在结果中经一个新的**无权限含义**的 `stitch` 连接步骤
  （`scan → stitch → 生产表节点`）找到实际生产该表的节点；不凭空增加投影/连接/聚合。
  原有加工步骤及贡献边的 `via_node`/`via_row` 原样保留、带阶段前缀可回查；结果还留下
  实际接续的阶段摘要（含每阶段图字节 SHA-256）与每条扫描绑定的生产表。
- 路径操作许可由现有合同沿真实加工边判定：某原行在上游已聚合、又参加下游连接时，
  结果中同时继承 `aggregate` 与 `join`，不会因中间表落盘而断成 `raw`。
- 上游可以直接采用 `quarantine` 交付的图：仅剩一个健康选项的单边 `any` 仍接通那条
  路径（结果图带 quarantine 标记），不找回已撤下的行或边。
- 结果是标准 `graph.json`（不含 `import` 叶），只依赖它自己和原 `sources.json`，
  可直接交给 `dq validate` / `dq plan` / `dq verify-plan`，不再需要任何阶段文件。

### 发布前检查与失败语义

发布前检查所读阶段的内容摘要、接缝绑定、跨阶段依赖环以及输入/输出图结构：任何一项
不过都返回非零退出（输入契约/无法接通退出 10，IO 错误退出 2）并指出无法接通的位置；
不修改任何输入、不覆盖已有输出，结果先写同级临时文件再一次性 `os.replace` 改名，
读者看不到尚未生成完整的结果文件。

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
| `build_totals/` | `build-totals` 小例：同一事实跨键、等额不同发票、零和部门、未匹配行；含 request 供 validate/plan 复跑 |
| `quarantine/`  | `quarantine` 共用凭据小例：必需分支失效后撤下、共享失效原子被多条撤下引用、多条健全替代在不同请求下只有一条可用于 plan、单边 any 保留、已登记未使用的失效行 |
| `stitch/`     | `stitch-lineage` 三阶段接续小例：两生产阶段复用同名局部 id，终端经中间阶段回读一行并在另一支路再次引用；含 sources/三阶段图/manifest/request |
| `gen_large.py`| 运行时生成大例（不落地预置大文件） |

## 复跑入口

```sh
./run_all.sh                 # 全部：校验样例 + 主演练 + 验收套件 + build-totals + quarantine + 大例基准
python3 -I tests/sat_selftest.py    # 求解器随机 CNF 对照（8000 用例）
python3 tests/acceptance.py      # 验收套件（小例/篡改/随机/大例/预算）
python3 -I tests/build_totals.py # build-totals 套件（暴力对照/证据展开/换序/非法行/线性压缩/原子交付）
python3 -I tests/quarantine.py   # quarantine 套件（凭据复查/共享理由/线性规模/流式/原子交付）
python3 -I tests/stitch.py       # stitch-lineage 套件（手写整体血缘对照/接缝错误/quarantine 上游/线性规模/原子交付）
# stitch-lineage 一条可复跑命令（结果图可继续交给 validate/plan/verify-plan）：
./dq stitch-lineage --sources examples/stitch/sources.json \
                    --manifest examples/stitch/manifest.json \
                    --out /tmp/stitched.json
python3 examples/gen_large.py --out /tmp/large --sources 24 --rows 12000 --tables 4 --seed 7
```

最近一次大例基准（24 源 × 4 选项、21090 贡献节点、22901 贡献边、12000 输出行、
高度共享：12000 行收敛到 8868 个可达贡献节点、19 个源被实际使用）：

- SAT 规模：82936 变量 / 135029 子句 / 10081 假设
- 求解：feasible，约 3–5 s；独立 verify-plan：约 1 s
- 预算 `--budget-seconds 0` ⇒ `unknown`（不谎报）
