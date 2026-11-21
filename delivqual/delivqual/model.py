"""数据模型与结构校验（validate）。

三类输入：
  sources.json  —— 源登记：source_id+snapshot、可引用原行、授权选项（本题定义的机器可读条款）
  graph.json    —— 加工无环图 + 实际贡献表达式（source/all/any，边带 via_node/via_row）+ 输出表
  request.json  —— 交付请求：时间、受众、地域、选定输出表及每表输出分类

本模块只做结构与引用校验，不做任何授权判断，也不解释真实许可证文本。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 选项中允许声明的操作权限词汇
PERMISSION_OPS = ("raw", "aggregate", "join", "union", "project")
# 加工图节点 op 词汇
GRAPH_OPS = ("scan", "union_all", "join", "project", "aggregate", "deduplicate")
# 加工 op -> 需要的操作权限（scan / deduplicate 不新增权限）
EDGE_OP_REQUIREMENT = {
    "join": "join",
    "union_all": "union",
    "project": "project",
    "aggregate": "aggregate",
    "scan": None,
    "deduplicate": None,
}
# 加工节点输入元数约束
ARITY = {
    "scan": (0, 0),
    "project": (1, 1),
    "aggregate": (1, 1),
    "deduplicate": (1, 1),
    "join": (2, None),
    "union_all": (2, None),
}


@dataclass
class Option:
    source_key: tuple          # (source_id, snapshot)
    option_id: str
    ops: frozenset             # PERMISSION_OPS 子集
    audiences: frozenset
    geos: frozenset
    tlo: int
    thi: int
    notices: tuple
    classifications: frozenset
    mutex: tuple               # ((source_id, snapshot, option_id), ...)

    @property
    def ref(self):
        return (self.source_key[0], self.source_key[1], self.option_id)


@dataclass
class Source:
    key: tuple                 # (source_id, snapshot)
    rows: frozenset
    options: list              # [Option]


@dataclass
class Sources:
    by_key: dict               # key -> Source
    option_index: dict         # (source_id, snapshot, option_id) -> Option


@dataclass
class PNode:
    node_id: str
    op: str
    inputs: tuple


@dataclass
class CEdge:
    parent: str
    child: str
    via_node: str
    via_row: str


@dataclass
class CNode:
    cid: str
    type: str                  # source / all / any
    source_key: tuple = None   # type == source
    row_id: str = None
    edges: tuple = ()          # [CEdge] for all/any


@dataclass
class Graph:
    graph_id: str
    nodes: dict                # node_id -> PNode
    tables: dict               # table_id -> {"node_id": str, "rows": [str]}
    contribs: dict             # cid -> CNode
    outputs: dict              # (table, row) -> cid
    parents: dict              # cid -> [cid]（反向引用，便于回读）


@dataclass
class Request:
    request_id: str
    time: int
    audience: str
    geo: str
    tables: list               # [(table_id, classification)]，保持请求顺序


# ---------------------------------------------------------------------------
# 小工具

def _is_str(x):
    return isinstance(x, str)


def _str_list(x):
    return isinstance(x, list) and all(_is_str(i) for i in x)


def _check_acyclic(nodes, edges_of, what, errors):
    """Kahn 拓扑判环。nodes: id 集合；edges_of: id -> [后继 id]。"""
    indeg = {n: 0 for n in nodes}
    for n in nodes:
        for m in edges_of(n):
            if m in indeg:
                indeg[m] += 1
    stack = [n for n, d in indeg.items() if d == 0]
    seen = 0
    while stack:
        x = stack.pop()
        seen += 1
        for m in edges_of(x):
            if m in indeg:
                indeg[m] -= 1
                if indeg[m] == 0:
                    stack.append(m)
    if seen != len(nodes):
        remaining = sorted(n for n, d in indeg.items() if d > 0)
        errors.append(f"{what} 存在环，涉及节点: {remaining[:10]}{'...' if len(remaining) > 10 else ''}")


# ---------------------------------------------------------------------------
# sources.json

def validate_sources(obj):
    """返回 (Sources|None, errors)。"""
    errors = []
    if not isinstance(obj, dict) or obj.get("kind") != "sources":
        return None, ["顶层必须是 {\"kind\": \"sources\", ...}"]
    raw_sources = obj.get("sources")
    if not isinstance(raw_sources, list):
        return None, ["sources 必须是数组"]

    by_key = {}
    option_index = {}
    pending_mutex = []  # (option_ref, target_ref, where)

    for si, s in enumerate(raw_sources):
        where = f"sources[{si}]"
        if not isinstance(s, dict):
            errors.append(f"{where}: 必须是对象")
            continue
        sid, snap = s.get("source_id"), s.get("snapshot")
        if not _is_str(sid) or not _is_str(snap):
            errors.append(f"{where}: source_id 与 snapshot 必须是非空字符串")
            continue
        key = (sid, snap)
        if key in by_key:
            errors.append(f"{where}: 重复的 (source_id, snapshot) = {key}")
            continue
        rows = s.get("rows")
        if not _str_list(rows) or len(set(rows)) != len(rows):
            errors.append(f"{where}.rows: 必须是不重复字符串数组（可引用原行 ID 清单）")
            rows = []
        raw_opts = s.get("options")
        if not isinstance(raw_opts, list):
            errors.append(f"{where}.options: 必须是数组")
            raw_opts = []
        options = []
        seen_opt = set()
        for oi, o in enumerate(raw_opts):
            ow = f"{where}.options[{oi}]"
            if not isinstance(o, dict):
                errors.append(f"{ow}: 必须是对象")
                continue
            oid = o.get("option_id")
            if not _is_str(oid):
                errors.append(f"{ow}: option_id 必须是字符串")
                continue
            if oid in seen_opt:
                errors.append(f"{ow}: 源 {key} 内 option_id 重复: {oid}")
                continue
            seen_opt.add(oid)
            ops = o.get("ops")
            if not _str_list(ops) or not set(ops) <= set(PERMISSION_OPS):
                errors.append(f"{ow}.ops: 必须是 {list(PERMISSION_OPS)} 的子集，得到 {ops!r}")
                ops = []
            audiences = o.get("audiences")
            geos = o.get("geos")
            classifications = o.get("classifications")
            if not _str_list(audiences):
                errors.append(f"{ow}.audiences: 必须是字符串数组")
                audiences = []
            if not _str_list(geos):
                errors.append(f"{ow}.geos: 必须是字符串数组")
                geos = []
            if not _str_list(classifications):
                errors.append(f"{ow}.classifications: 必须是字符串数组")
                classifications = []
            t = o.get("time")
            if not (isinstance(t, list) and len(t) == 2 and all(isinstance(v, int) and not isinstance(v, bool) for v in t)):
                errors.append(f"{ow}.time: 必须是 [lo, hi] 整数区间")
                t = [0, 0]
            elif t[0] > t[1]:
                errors.append(f"{ow}.time: 区间下界 {t[0]} 大于上界 {t[1]}")
            notices = o.get("notices")
            if not _str_list(notices):
                errors.append(f"{ow}.notices: 必须是字符串数组")
                notices = []
            mutex = o.get("mutex")
            if not isinstance(mutex, list):
                errors.append(f"{ow}.mutex: 必须是数组")
                mutex = []
            mutex_refs = []
            for mi, m in enumerate(mutex):
                mw = f"{ow}.mutex[{mi}]"
                if not (isinstance(m, dict) and _is_str(m.get("source_id")) and _is_str(m.get("snapshot")) and _is_str(m.get("option_id"))):
                    errors.append(f"{mw}: 互斥项必须是 {{source_id, snapshot, option_id}}")
                    continue
                ref = (m["source_id"], m["snapshot"], m["option_id"])
                mutex_refs.append(ref)
                pending_mutex.append(((sid, snap, oid), ref, mw))
            opt = Option(
                source_key=key, option_id=oid, ops=frozenset(ops),
                audiences=frozenset(audiences), geos=frozenset(geos),
                tlo=t[0], thi=t[1], notices=tuple(notices),
                classifications=frozenset(classifications), mutex=tuple(mutex_refs),
            )
            options.append(opt)
            option_index[(sid, snap, oid)] = opt
        by_key[key] = Source(key=key, rows=frozenset(rows), options=options)

    # 互斥引用必须指向已登记选项
    for src_ref, tgt_ref, mw in pending_mutex:
        if tgt_ref not in option_index:
            errors.append(f"{mw}: 互斥目标 {tgt_ref} 不是已登记的选项")
        elif tgt_ref[:2] == src_ref[:2]:
            # 同源选项本就互斥（一个源只选一项），声明无意义但不构成错误
            pass

    if errors:
        return None, errors
    return Sources(by_key=by_key, option_index=option_index), errors


# ---------------------------------------------------------------------------
# graph.json

def validate_graph(obj, sources: Sources):
    """返回 (Graph|None, errors, warnings)。"""
    errors, warnings = [], []
    if not isinstance(obj, dict) or obj.get("kind") != "graph":
        return None, ["顶层必须是 {\"kind\": \"graph\", ...}"], warnings
    graph_id = obj.get("graph_id")
    if not _is_str(graph_id):
        errors.append("graph_id 必须是字符串")
        graph_id = "?"

    # ---- 加工节点 ----
    nodes = {}
    for ni, n in enumerate(obj.get("nodes") or []):
        nw = f"nodes[{ni}]"
        if not isinstance(n, dict):
            errors.append(f"{nw}: 必须是对象")
            continue
        nid, op = n.get("node_id"), n.get("op")
        inputs = n.get("inputs")
        if not _is_str(nid):
            errors.append(f"{nw}: node_id 必须是字符串")
            continue
        if nid in nodes:
            errors.append(f"{nw}: node_id 重复: {nid}")
            continue
        if op not in GRAPH_OPS:
            errors.append(f"{nw}: op 必须是 {list(GRAPH_OPS)} 之一，得到 {op!r}")
            continue
        if not _str_list(inputs):
            errors.append(f"{nw}: inputs 必须是字符串数组")
            inputs = []
        lo, hi = ARITY[op]
        if len(inputs) < lo or (hi is not None and len(inputs) > hi):
            errors.append(f"{nw}: op={op} 的输入个数应为 {lo}" + (f"~{hi}" if hi != lo else "") + f"，得到 {len(inputs)}")
        nodes[nid] = PNode(node_id=nid, op=op, inputs=tuple(inputs))
    for n in nodes.values():
        for inp in n.inputs:
            if inp not in nodes:
                errors.append(f"加工节点 {n.node_id}: 输入 {inp} 不存在")
    _check_acyclic(set(nodes), lambda x: [i for i in nodes[x].inputs if i in nodes], "加工图", errors)

    # ---- 输出表 ----
    tables = {}
    for ti, t in enumerate(obj.get("tables") or []):
        tw = f"tables[{ti}]"
        if not isinstance(t, dict):
            errors.append(f"{tw}: 必须是对象")
            continue
        tid = t.get("table_id")
        if not _is_str(tid):
            errors.append(f"{tw}: table_id 必须是字符串")
            continue
        if tid in tables:
            errors.append(f"{tw}: table_id 重复: {tid}")
            continue
        node_id = t.get("node_id")
        if node_id not in nodes:
            errors.append(f"{tw}: node_id {node_id!r} 不是已登记加工节点")
        rows = t.get("rows")
        if not _str_list(rows) or len(set(rows)) != len(rows):
            errors.append(f"{tw}.rows: 必须是不重复字符串数组")
            rows = []
        tables[tid] = {"node_id": node_id, "rows": list(rows)}

    # ---- 贡献表达式 ----
    contribs = {}
    for ci, c in enumerate(obj.get("contribs") or []):
        cw = f"contribs[{ci}]"
        if not isinstance(c, dict):
            errors.append(f"{cw}: 必须是对象")
            continue
        cid, ctype = c.get("id"), c.get("type")
        if not _is_str(cid):
            errors.append(f"{cw}: id 必须是字符串")
            continue
        if cid in contribs:
            errors.append(f"{cw}: 贡献节点 id 重复: {cid}")
            continue
        if ctype == "source":
            sid, snap, row_id = c.get("source_id"), c.get("snapshot"), c.get("row_id")
            if not (_is_str(sid) and _is_str(snap) and _is_str(row_id)):
                errors.append(f"{cw}: source 原子必须含 source_id/snapshot/row_id 字符串")
                continue
            key = (sid, snap)
            src = sources.by_key.get(key) if sources else None
            if src is None:
                errors.append(f"{cw}: 原子引用未登记的源 {key}")
                continue
            if row_id not in src.rows:
                errors.append(f"{cw}: 原子引用源 {key} 未登记的原行 {row_id!r}")
                continue
            contribs[cid] = CNode(cid=cid, type="source", source_key=key, row_id=row_id)
        elif ctype in ("all", "any"):
            raw_edges = c.get("edges")
            if not isinstance(raw_edges, list):
                errors.append(f"{cw}: {ctype} 节点必须含 edges 数组")
                continue
            edges = []
            for ei, e in enumerate(raw_edges):
                ew = f"{cw}.edges[{ei}]"
                if not isinstance(e, dict):
                    errors.append(f"{ew}: 必须是对象")
                    continue
                child, via_node, via_row = e.get("child"), e.get("via_node"), e.get("via_row")
                ok = True
                if not _is_str(child):
                    errors.append(f"{ew}: child 必须是字符串")
                    ok = False
                if via_node not in nodes:
                    errors.append(f"{ew}: via_node {via_node!r} 不是已登记加工节点（每条贡献边必须指向加工节点）")
                    ok = False
                if not _is_str(via_row) or via_row == "":
                    errors.append(f"{ew}: via_row 必须是非空字符串（每条贡献边必须带行引用）")
                    ok = False
                if ok:
                    edges.append(CEdge(parent=cid, child=child, via_node=via_node, via_row=via_row))
            # 语义约束：any 只能来自显式 deduplicate；all 的多条边必须同属一个 join/aggregate
            if ctype == "any":
                if len(edges) < 2:
                    errors.append(f"{cw}: any 节点至少需要 2 条替代边")
                via_ops = {nodes[e.via_node].op for e in edges if e.via_node in nodes}
                via_ids = {e.via_node for e in edges}
                if via_ops != {"deduplicate"} or len(via_ids) != 1:
                    errors.append(
                        f"{cw}: any（充分替代）只能由显式 deduplicate 产生，其全部边必须指向同一个 deduplicate 节点；"
                        f"实际 via_node={sorted(via_ids)}"
                    )
            else:  # all
                if len(edges) < 1:
                    errors.append(f"{cw}: all 节点至少需要 1 条边")
                if len(edges) >= 2:
                    via_ids = {e.via_node for e in edges}
                    via_ops = {nodes[e.via_node].op for e in edges if e.via_node in nodes}
                    if len(via_ids) != 1 or not via_ops <= {"join", "aggregate"}:
                        errors.append(
                            f"{cw}: all（必须共同使用）的多条边必须指向同一个 join 或 aggregate 节点；"
                            f"实际 via_node={sorted(via_ids)}"
                        )
            contribs[cid] = CNode(cid=cid, type=ctype, edges=tuple(edges))
        else:
            errors.append(
                f"{cw}: 未知贡献节点类型 {ctype!r}；只允许 source/all/any。"
                "注意：不存在“已脱敏”之类的免除机制，不可信表达式不能免除来源。"
            )

    # 边的 child 必须存在
    for c in contribs.values():
        for e in c.edges:
            if e.child not in contribs:
                errors.append(f"贡献节点 {c.cid}: 边指向不存在的子节点 {e.child!r}")
    _check_acyclic(
        set(contribs),
        lambda x: [e.child for e in contribs[x].edges if e.child in contribs],
        "贡献表达式图", errors,
    )

    parents = {cid: [] for cid in contribs}
    for c in contribs.values():
        for e in c.edges:
            if e.child in parents:
                parents[e.child].append(c.cid)

    # ---- 输出行 ----
    outputs = {}
    for oi, o in enumerate(obj.get("outputs") or []):
        ow = f"outputs[{oi}]"
        if not isinstance(o, dict):
            errors.append(f"{ow}: 必须是对象")
            continue
        table, row, expr = o.get("table"), o.get("row"), o.get("expr")
        if table not in tables:
            errors.append(f"{ow}: 引用不存在的表 {table!r}")
            continue
        if row not in tables[table]["rows"]:
            errors.append(f"{ow}: 行 {row!r} 不在表 {table} 的行清单中")
            continue
        if expr not in contribs:
            errors.append(f"{ow}: expr {expr!r} 不是已登记贡献节点")
            continue
        if (table, row) in outputs:
            errors.append(f"{ow}: 输出行 ({table}, {row}) 重复登记")
            continue
        outputs[(table, row)] = expr

    # 表内行缺少贡献：警告（plan 阶段按“缺失贡献”处理）
    for tid, tdef in tables.items():
        for r in tdef["rows"]:
            if (tid, r) not in outputs:
                warnings.append(f"表 {tid} 行 {r} 没有登记的贡献表达式（交付时构成缺失贡献）")
    # 不可达贡献节点：警告
    reachable = set()
    stack = list(outputs.values())
    while stack:
        x = stack.pop()
        if x in reachable:
            continue
        reachable.add(x)
        stack.extend(e.child for e in contribs[x].edges if e.child in contribs)
    for cid in contribs:
        if cid not in reachable:
            warnings.append(f"贡献节点 {cid} 从任何输出行都不可达（冗余）")

    if errors:
        return None, errors, warnings
    return Graph(graph_id=graph_id, nodes=nodes, tables=tables, contribs=contribs,
                 outputs=outputs, parents=parents), errors, warnings


# ---------------------------------------------------------------------------
# request.json

def validate_request(obj, graph: Graph):
    """返回 (Request|None, errors)。"""
    errors = []
    if not isinstance(obj, dict) or obj.get("kind") != "request":
        return None, ["顶层必须是 {\"kind\": \"request\", ...}"]
    rid = obj.get("request_id")
    if not _is_str(rid):
        errors.append("request_id 必须是字符串")
        rid = "?"
    t = obj.get("time")
    if not (isinstance(t, int) and not isinstance(t, bool)):
        errors.append("time 必须是整数")
        t = 0
    audience, geo = obj.get("audience"), obj.get("geo")
    if not _is_str(audience):
        errors.append("audience 必须是字符串")
        audience = "?"
    if not _is_str(geo):
        errors.append("geo 必须是字符串")
        geo = "?"
    tables = []
    seen = set()
    raw_tables = obj.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        errors.append("tables 必须是非空数组")
        raw_tables = []
    for ti, tb in enumerate(raw_tables):
        tw = f"tables[{ti}]"
        if not isinstance(tb, dict):
            errors.append(f"{tw}: 必须是对象")
            continue
        tid, cls = tb.get("table_id"), tb.get("classification")
        if graph is not None and tid not in graph.tables:
            errors.append(f"{tw}: 表 {tid!r} 在加工图中不存在")
            continue
        if not _is_str(cls) or cls == "":
            errors.append(f"{tw}: classification 必须是非空字符串")
            continue
        if tid in seen:
            errors.append(f"{tw}: 表 {tid} 被重复选择")
            continue
        seen.add(tid)
        tables.append((tid, cls))
    if errors:
        return None, errors
    return Request(request_id=rid, time=t, audience=audience, geo=geo, tables=tables), errors
