"""独立暴力枚举器：枚举源选项与有限推导，判定可行性。

只用于小例验收与冲突证书复核（题目要求：不展开笛卡尔推导的是主求解路径；
这里是有预算的、独立实现的交叉检查）。
"""
from __future__ import annotations

import itertools
import time
from collections import defaultdict

from .derive import analyze_selection, required_permissions


def brute_feasible(sources, graph, request, req_rows=None, max_combos=200_000, deadline=None):
    """返回 (status, witness)：
    status ∈ feasible / infeasible / unknown / missing
    witness（可行时）= {"choices": {any_cid: edge_index}, "options": {key: option_id}}
    """
    if req_rows is None:
        req_rows = [(tid, row, cls) for (tid, cls) in request.tables
                    for row in graph.tables[tid]["rows"]]
    roots = []
    for (t, r, _c) in req_rows:
        expr = graph.outputs.get((t, r))
        if expr is None:
            return ("missing", {"row": (t, r)})
        roots.append((expr, t, r))

    # 可达 any 节点
    reach = set()
    stack = [e for (e, _t, _r) in roots]
    while stack:
        x = stack.pop()
        if x in reach:
            continue
        reach.add(x)
        stack.extend(e.child for e in graph.contribs[x].edges)
    anys = sorted(n for n in reach if graph.contribs[n].type == "any")
    fanout = [len(graph.contribs[n].edges) for n in anys]

    # 适用性预过滤
    def applicable(opt):
        return (request.audience in opt.audiences and request.geo in opt.geos
                and opt.tlo <= request.time <= opt.thi)

    cls_of = dict(request.tables)
    combos = 0
    ranges = [range(k) for k in fanout] or [range(1)]
    for choice_tuple in itertools.product(*ranges):
        combos += 1
        if combos > max_combos or (deadline and time.monotonic() > deadline):
            return ("unknown", None)
        choice = dict(zip(anys, choice_tuple))
        # 按选择展开所选子 DAG
        edges = []
        seen = set()
        stack = [e for (e, _t, _r) in roots]
        ok = True
        while stack and ok:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            node = graph.contribs[n]
            if node.type == "all":
                for e in node.edges:
                    edges.append((e.parent, e.child, e.via_node, e.via_row))
                    stack.append(e.child)
            elif node.type == "any":
                e = node.edges[choice[n]]
                edges.append((e.parent, e.child, e.via_node, e.via_row))
                stack.append(e.child)
        info, _atom_rows = analyze_selection(graph, roots, edges)
        # 每源合并要求
        src_req = defaultdict(lambda: {"ops": set(), "classes": set()})
        for cid in seen:
            node = graph.contribs[cid]
            if node.type != "source":
                continue
            r = src_req[node.source_key]
            r["ops"] |= required_permissions(info[cid])
            for t in info[cid]["tables"]:
                r["classes"].add(cls_of[t])
        # 每源可用 option（操作+分类+适用性）
        cand = {}
        for key, r in src_req.items():
            opts = [o for o in sources.by_key[key].options
                    if applicable(o) and r["ops"] <= o.ops and r["classes"] <= o.classifications]
            if not opts:
                cand = None
                break
            cand[key] = opts
        if cand is None:
            continue
        keys = sorted(cand)
        for assign in itertools.product(*[cand[k] for k in keys]):
            combos += 1
            if combos > max_combos or (deadline and time.monotonic() > deadline):
                return ("unknown", None)
            chosen = dict(zip(keys, assign))
            if _mutex_ok(chosen):
                return ("feasible", {
                    "choices": choice,
                    "options": {k: o.option_id for k, o in chosen.items()},
                })
    return ("infeasible", None)


def _mutex_ok(chosen):
    refs = {k: o.ref for k, o in chosen.items()}
    for k, o in chosen.items():
        for tgt in o.mutex:
            if tgt[:2] in refs and refs[tgt[:2]] == tgt:
                return False
    return True
