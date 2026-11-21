"""把数据贡献与路径义务编译成全局选择约束（SAT）。

设计要点：
  - 贡献表达式是带共享的 DAG；每个节点/边只建一次变量，绝不展开笛卡尔推导。
  - 路径性质（含 aggregate / 无 aggregate / 含 join / 含 union / 含 project / 属于某表）
    以单调“存在一条所选路径”的方式沿所选边传播，天然支持共享节点的多路径合并。
  - 同一 (source_id, snapshot) 的全部原子共享同一组 option 变量 ⇒ 全局一致选项，
    跨支路、跨表自动合并要求，而不是逐行检查后拼接。
  - 每条“要求”子句挂一个激活字面量作为假设；UNSAT 时求解器返回的假设核
    就是一组足以重现矛盾的、可复查的输入要求。
"""
from __future__ import annotations

from collections import defaultdict, deque

from .model import EDGE_OP_REQUIREMENT

FLAG_NAMES = ("aggP", "aggF", "hasJ", "hasU", "hasP")


class Compiler:
    def __init__(self, sources, graph, request, req_rows=None):
        """
        req_rows: 可选的显式 [(table, row, classification)]；缺省按 request.tables 展开。
        """
        self.sources = sources
        self.graph = graph
        self.request = request
        if req_rows is None:
            req_rows = [
                (tid, row, cls)
                for (tid, cls) in request.tables
                for row in graph.tables[tid]["rows"]
            ]
        self.req_rows = list(req_rows)
        self.missing = []          # [(table, row)] 无贡献表达式
        self.nvars = 0
        self.clauses = []
        self.acts = {}             # act_var -> detail dict
        # 变量映射
        self.sel = {}              # cid -> var
        self.esel = {}             # edge_index -> var
        self.flag = {}             # (cid, flag) -> var
        self.inT = {}              # (cid, table) -> var
        self.optvar = {}           # (source_key, option_id) -> var
        # 结构
        self.edges = []            # [(parent, child, via_node, via_row, op)]
        self.node_tables = defaultdict(set)   # cid -> {table}
        self.atom_rows = defaultdict(set)     # atom cid -> {(table,row)}（截断记录）
        self.roots = []            # [(cid, table, row, classification)]

    # ------------------------------------------------------------- 基础

    def _var(self):
        self.nvars += 1
        return self.nvars

    def _cl(self, lits):
        self.clauses.append(list(lits))

    def _act(self, lits, detail):
        a = self._var()
        detail = dict(detail)
        detail["id"] = f"R{len(self.acts) + 1}"
        self.acts[a] = detail
        self.clauses.append([-a] + list(lits))
        return a

    # ------------------------------------------------------------- 构建

    def build(self):
        g = self.graph
        # 1. 根
        for (tid, row, cls) in self.req_rows:
            expr = g.outputs.get((tid, row))
            if expr is None:
                self.missing.append((tid, row))
                continue
            self.roots.append((expr, tid, row, cls))

        # 2. 可达性与“属于哪些被请求表”
        reachable = set()
        stack = [r for (r, _, _, _) in self.roots]
        root_tables = defaultdict(set)
        root_rows = defaultdict(set)
        for (expr, tid, row, _cls) in self.roots:
            root_tables[expr].add(tid)
            root_rows[expr].add((tid, row))
        while stack:
            x = stack.pop()
            if x in reachable:
                continue
            reachable.add(x)
            stack.extend(e.child for e in g.contribs[x].edges)
        # 自根向下传播表/行归属（DAG 上的并集；行集合截断记录）
        order = self._topo(reachable)
        for cid in order:
            for t in root_tables.get(cid, ()):
                self.node_tables[cid].add(t)
            for r in root_rows.get(cid, ()):
                if len(self.atom_rows[cid]) < 64:
                    self.atom_rows[cid].add(r)
            node = g.contribs[cid]
            for e in node.edges:
                if e.child in reachable:
                    self.node_tables[e.child] |= self.node_tables[cid]
                    if len(self.atom_rows[e.child]) < 64:
                        self.atom_rows[e.child] |= self.atom_rows[cid]

        # 3. 变量
        for cid in reachable:
            self.sel[cid] = self._var()
            for f in FLAG_NAMES:
                self.flag[(cid, f)] = self._var()
            for t in self.node_tables[cid]:
                self.inT[(cid, t)] = self._var()
        for cid in reachable:
            for e in g.contribs[cid].edges:
                if e.child in reachable:
                    idx = len(self.edges)
                    self.edges.append((e.parent, e.child, e.via_node, e.via_row,
                                       g.nodes[e.via_node].op))
                    self.esel[idx] = self._var()
        used_sources = sorted({g.contribs[c].source_key for c in reachable
                               if g.contribs[c].type == "source"})
        self.used_sources = used_sources
        for key in used_sources:
            for opt in self.sources.by_key[key].options:
                self.optvar[(key, opt.option_id)] = self._var()
        self.finalize_edge_map()

        sel, esel, flag, inT = self.sel, self.esel, self.flag, self.inT

        # 4. 根单元子句
        # 注意：一个节点可以同时是某行的根、又是另一行推导中的子节点，
        # 因此根处只下正向单元（aggF / inT），不下负向单元；
        # 路径性质一律靠正向单调传播，提取时另行重算，不依赖模型中的标记值。
        for (expr, tid, row, _cls) in self.roots:
            self._cl([sel[expr]])
            self._cl([flag[(expr, "aggF")]])
            self._cl([inT[(expr, tid)]])

        # 5. 结构子句
        for cid in reachable:
            node = g.contribs[cid]
            if node.type == "all":
                for e in node.edges:
                    if e.child in reachable:
                        self._cl([-sel[cid], esel[self._edge_index(e)]])
            elif node.type == "any":
                vs = [esel[self._edge_index(e)] for e in node.edges if e.child in reachable]
                if vs:
                    self._cl([-sel[cid]] + vs)
                else:
                    self._cl([-sel[cid]])
        for idx, (p, c, _vn, _vr, op) in enumerate(self.edges):
            ev = esel[idx]
            self._cl([-ev, sel[p]])
            self._cl([-ev, sel[c]])
            # 路径性质传播（单调：存在一条所选路径）
            if op == "aggregate":
                self._cl([-ev, flag[(c, "aggP")]])
            else:
                self._cl([-ev, -flag[(p, "aggF")], flag[(c, "aggF")]])
            self._cl([-ev, -flag[(p, "aggP")], flag[(c, "aggP")]])
            req = EDGE_OP_REQUIREMENT[op]
            if req == "join":
                self._cl([-ev, flag[(c, "hasJ")]])
            elif req == "union":
                self._cl([-ev, flag[(c, "hasU")]])
            elif req == "project":
                self._cl([-ev, flag[(c, "hasP")]])
            for f_src, f_dst in (("hasJ", "hasJ"), ("hasU", "hasU"), ("hasP", "hasP")):
                self._cl([-ev, -flag[(p, f_src)], flag[(c, f_dst)]])
            for t in self.node_tables[c]:
                if (p, t) in inT:
                    self._cl([-ev, -inT[(p, t)], inT[(c, t)]])

        # 6. 每个源至多选一个 option
        for key in used_sources:
            ovs = [self.optvar[(key, o.option_id)] for o in self.sources.by_key[key].options]
            for i in range(len(ovs)):
                for j in range(i + 1, len(ovs)):
                    self._cl([-ovs[i], -ovs[j]])

        # 7. 原子要求（挂激活字面量）
        req = self.request
        for cid in reachable:
            node = g.contribs[cid]
            if node.type != "source":
                continue
            key = node.source_key
            src = self.sources.by_key[key]
            anchor = {
                "source_id": key[0], "snapshot": key[1], "row_id": node.row_id,
                "atom_cid": cid,
                "rows": sorted(self.atom_rows.get(cid, []))[:20],
            }
            ovs_all = [self.optvar[(key, o.option_id)] for o in src.options]
            # 被使用 ⇒ 至少能选出一个 option
            self._act([-sel[cid]] + ovs_all,
                      {"kind": "alo", "detail": f"源 {key[0]}@{key[1]} 行 {node.row_id} 被使用，必须能选出该源的某个 option",
                       **anchor})
            # 操作权限
            for f, op, desc in (
                ("aggF", "raw", "经不含 aggregate 的路径交付"),
                ("aggP", "aggregate", "经含 aggregate 的路径交付"),
                ("hasJ", "join", "路径含 join"),
                ("hasU", "union", "路径含 union_all"),
                ("hasP", "project", "路径含 project"),
            ):
                ovs = [self.optvar[(key, o.option_id)] for o in src.options if op in o.ops]
                self._act([-sel[cid], -flag[(cid, f)]] + ovs,
                          {"kind": "op", "require": op,
                           "detail": f"源 {key[0]}@{key[1]} 行 {node.row_id} {desc} ⇒ 其 option 须许可 {op}",
                           **anchor})
            # 输出分类
            for t in self.node_tables[cid]:
                cls = dict(self.request.tables)[t]
                ovs = [self.optvar[(key, o.option_id)] for o in src.options if cls in o.classifications]
                self._act([-sel[cid], -inT[(cid, t)]] + ovs,
                          {"kind": "class", "table": t, "classification": cls,
                           "detail": f"源 {key[0]}@{key[1]} 行 {node.row_id} 用于表 {t}（分类 {cls}）⇒ 其 option 须许可该分类",
                           **anchor})

        # 8. 适用性（时间/受众/地域）——对被使用源的 option 做排除
        for key in used_sources:
            for o in self.sources.by_key[key].options:
                reasons = []
                if req.audience not in o.audiences:
                    reasons.append(f"受众 {req.audience!r} 不在 {sorted(o.audiences)}")
                if req.geo not in o.geos:
                    reasons.append(f"地域 {req.geo!r} 不在 {sorted(o.geos)}")
                if not (o.tlo <= req.time <= o.thi):
                    reasons.append(f"时间 {req.time} 不在区间 [{o.tlo}, {o.thi}]")
                if reasons:
                    self._act([-self.optvar[(key, o.option_id)]],
                              {"kind": "applicability",
                               "source_id": key[0], "snapshot": key[1], "option_id": o.option_id,
                               "detail": f"选项 {key[0]}@{key[1]}#{o.option_id} 被请求排除: " + "；".join(reasons),
                               "reasons": reasons})

        # 9. 选项互斥
        seen_pairs = set()
        for key in used_sources:
            for o in self.sources.by_key[key].options:
                for tgt in o.mutex:
                    if tgt not in self.sources.option_index:
                        continue
                    pair = frozenset((o.ref, tgt))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    if (tgt[:2], tgt[2]) and tgt[:2] in self.sources.by_key:
                        v1 = self.optvar.get((o.source_key, o.option_id))
                        v2 = self.optvar.get((tgt[:2], tgt[2]))
                        if v1 is not None and v2 is not None:
                            self._act([-v1, -v2],
                                      {"kind": "mutex",
                                       "left": list(o.ref), "right": list(tgt),
                                       "detail": f"选项 {o.ref} 与 {tgt} 互斥，不能同时选用"})
        return self

    def _edge_index(self, e):
        # edges 按构建顺序登记；用 (parent, child, via_node, via_row) 定位
        return self._edge_map[(e.parent, e.child, e.via_node, e.via_row)]

    def _topo(self, reachable):
        g = self.graph
        indeg = {c: 0 for c in reachable}
        for c in reachable:
            for e in g.contribs[c].edges:
                if e.child in reachable:
                    indeg[e.child] += 1
        dq = deque([c for c in reachable if indeg[c] == 0])
        order = []
        while dq:
            x = dq.popleft()
            order.append(x)
            for e in g.contribs[x].edges:
                if e.child in reachable:
                    indeg[e.child] -= 1
                    if indeg[e.child] == 0:
                        dq.append(e.child)
        return order

    # 在 build 前初始化边索引
    @property
    def edge_map(self):
        return self._edge_map

    def finalize_edge_map(self):
        self._edge_map = {}
        for idx, (p, c, vn, vr, _op) in enumerate(self.edges):
            self._edge_map[(p, c, vn, vr)] = idx
