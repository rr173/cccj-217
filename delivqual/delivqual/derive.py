"""在所选贡献子 DAG 上计算路径性质（plan 提取 / verify / explain 共用）。

语义约定（与编译器编码一致）：
  - 路径含 aggregate ⇒ 需要 aggregate；存在不含 aggregate 的路径 ⇒ 交付处需要 raw；
  - 路径含 join / union_all / project ⇒ 分别需要 join / union / project；
  - scan 与 deduplicate 不新增操作权限；
  - 共享节点被多条所选路径到达时，要求按“存在路径”合并（并集）。
"""
from __future__ import annotations

from collections import defaultdict, deque

from .model import EDGE_OP_REQUIREMENT


def prune_selection(graph, roots, esel_true):
    """从 SAT 模型剪出最小充分选择。

    roots: [cid]；esel_true: {(parent, child, via_node, via_row)}。
    all 节点取全部边；any 节点取第一条模型中选中的边。
    返回 (nodes, edges)：nodes=[cid]，edges=[(parent, child, via_node, via_row)]。
    """
    nodes = []
    seen = set()
    edges = []
    stack = list(roots)
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        nodes.append(n)
        node = graph.contribs[n]
        if node.type == "all":
            for e in node.edges:
                edges.append((e.parent, e.child, e.via_node, e.via_row))
                stack.append(e.child)
        elif node.type == "any":
            chosen = None
            for e in node.edges:
                if (e.parent, e.child, e.via_node, e.via_row) in esel_true:
                    chosen = e
                    break
            if chosen is None:
                raise AssertionError(f"模型与 any 节点 {n} 不一致：没有选中的边")
            edges.append((chosen.parent, chosen.child, chosen.via_node, chosen.via_row))
            stack.append(chosen.child)
    return nodes, edges


def analyze_selection(graph, roots, sel_edges):
    """在所选子 DAG 上自根向下传播路径性质。

    roots: [(cid, table, row)]；sel_edges: [(parent, child, via_node, via_row)]
    返回 (info, atom_rows)：
      info[cid] = {"aggP","aggF","hasJ","hasU","hasP","tables":set}
      atom_rows[atom_cid] = {(table, row)}
    """
    children = defaultdict(list)
    nodes = set()
    for (p, c, vn, vr) in sel_edges:
        children[p].append((c, vn))
        nodes.add(p)
        nodes.add(c)
    for (cid, _t, _r) in roots:
        nodes.add(cid)
    # 拓扑序
    indeg = {n: 0 for n in nodes}
    for p in nodes:
        for (c, _vn) in children[p]:
            indeg[c] += 1
    dq = deque([n for n in nodes if indeg[n] == 0])
    order = []
    while dq:
        x = dq.popleft()
        order.append(x)
        for (c, _vn) in children[x]:
            indeg[c] -= 1
            if indeg[c] == 0:
                dq.append(c)
    if len(order) != len(nodes):
        raise AssertionError("所选子图存在环（输入校验应当已经拒绝）")

    info = {n: {"aggP": False, "aggF": False, "hasJ": False, "hasU": False, "hasP": False,
                "tables": set()} for n in nodes}
    for (cid, t, _r) in roots:
        info[cid]["aggF"] = True
        info[cid]["tables"].add(t)
    for n in order:
        fn = info[n]
        for (c, vn) in children[n]:
            op = graph.nodes[vn].op
            fc = info[c]
            fc["tables"] |= fn["tables"]
            if op == "aggregate":
                fc["aggP"] = True
            else:
                fc["aggF"] = fc["aggF"] or fn["aggF"]
            fc["aggP"] = fc["aggP"] or fn["aggP"]
            req = EDGE_OP_REQUIREMENT[op]
            if req == "join":
                fc["hasJ"] = True
            elif req == "union":
                fc["hasU"] = True
            elif req == "project":
                fc["hasP"] = True
            fc["hasJ"] = fc["hasJ"] or fn["hasJ"]
            fc["hasU"] = fc["hasU"] or fn["hasU"]
            fc["hasP"] = fc["hasP"] or fn["hasP"]

    # 每个节点的原子集合（自底向上，共享节点只算一次），再反查每个原子的输出行
    atomset = {}
    for n in reversed(order):
        node = graph.contribs[n]
        if node.type == "source":
            atomset[n] = {n}
        else:
            s = set()
            for (c, _vn) in children[n]:
                s |= atomset[c]
            atomset[n] = s
    atom_rows = defaultdict(set)
    for (cid, t, r) in roots:
        for a in atomset[cid]:
            atom_rows[a].add((t, r))
    return info, atom_rows


def required_permissions(node_flags):
    """把单个原子的路径性质翻译成所需操作权限集合。"""
    ops = set()
    if node_flags["aggF"]:
        ops.add("raw")
    if node_flags["aggP"]:
        ops.add("aggregate")
    if node_flags["hasJ"]:
        ops.add("join")
    if node_flags["hasU"]:
        ops.add("union")
    if node_flags["hasP"]:
        ops.add("project")
    return ops
