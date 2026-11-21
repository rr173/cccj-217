"""explain-row：解释单个输出行的交付情况（所选推导、路径义务、来源选项）。"""
from __future__ import annotations

from collections import defaultdict

from .derive import analyze_selection
from .solver import plan as solve_plan


def explain_row(sources, graph, request, table, row, budget_seconds=30.0):
    lines = []
    cls_of = dict(request.tables)
    if table not in cls_of:
        return [f"错误：表 {table!r} 不在交付请求中"]
    if row not in graph.tables.get(table, {"rows": []})["rows"]:
        return [f"错误：行 {row!r} 不在表 {table} 的行清单中"]
    lines.append(f"== 行 {table}/{row} 的交付说明 ==")
    lines.append(f"请求: time={request.time} audience={request.audience} geo={request.geo} "
                 f"表={[(t, c) for t, c in request.tables]}")

    p = solve_plan(sources, graph, request, budget_seconds=budget_seconds, self_check=True)
    lines.append(f"整体状态: {p['status']}")

    if p["status"] == "unknown":
        lines.append("预算耗尽，无法确定。")
        return lines

    if p["status"] == "infeasible":
        conflict = p["conflict"]
        lines.append(f"冲突类别: {', '.join(conflict['categories'])}")
        # 该行可达原子
        root = graph.outputs.get((table, row))
        if root is None:
            lines.append(f"行 {table}/{row} 没有登记的贡献表达式（缺失贡献）。")
            return lines
        reach_atoms = set()
        stack = [root]
        seen = set()
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            node = graph.contribs[x]
            if node.type == "source":
                reach_atoms.add(x)
            stack.extend(e.child for e in node.edges)
        related = [q for q in conflict["requirements"]
                   if q.get("atom_cid") in reach_atoms
                   or q.get("table") == table and q.get("row") == row]
        if related:
            lines.append("与该行相关的冲突要求:")
            for q in related:
                lines.append(f"  - [{q['id']}] {q['detail']}")
        else:
            lines.append("冲突要求不直接涉及该行（该行本身可交付，但全局交付不可行）。")
            for q in conflict["requirements"][:10]:
                lines.append(f"  - [{q['id']}] {q['detail']}")
        return lines

    # feasible：找到该行的交付明细
    entry = next((e for e in p["rows"] if e["table"] == table and e["row"] == row), None)
    if entry is None:
        lines.append("内部错误：可行 plan 中缺少该行")
        return lines
    der = entry["derivation"]
    lines.append(f"所选推导（根 {der['root']}，{len(der['nodes'])} 个节点，{len(der['edges'])} 条边）:")
    children = defaultdict(list)
    for e in der["edges"]:
        children[e["parent"]].append(e)
    ref_count = defaultdict(int)
    for e in der["edges"]:
        ref_count[e["child"]] += 1

    def walk(cid, depth, prefix):
        node = graph.contribs[cid]
        shared = "  (共享)" if ref_count[cid] > 1 else ""
        if node.type == "source":
            lines.append(f"{prefix}原子 {cid}: 源 {node.source_key[0]}@{node.source_key[1]} 行 {node.row_id}{shared}")
            return
        lines.append(f"{prefix}{node.type} 节点 {cid}{shared}")
        for e in children[cid]:
            op = graph.nodes[e["via_node"]].op
            lines.append(f"{prefix}  └─ 边经由 {e['via_node']}({op}) 行引用 {e['via_row']}")
            walk(e["child"], depth + 1, prefix + "     ")

    walk(der["root"], 0, "  ")
    lines.append("来源义务:")
    for a in entry["atoms"]:
        lines.append(f"  - {a['source_id']}@{a['snapshot']} 行 {a['row_id']}: "
                     f"路径操作={a['path_ops'] or ['(无)']} ⇒ 需要权限 {a['requires']}")
    lines.append("所选 option:")
    for o in p["options"]:
        key = (o["source_id"], o["snapshot"])
        if any(a["source_id"] == key[0] and a["snapshot"] == key[1] for a in entry["atoms"]):
            opt = sources.option_index[(key[0], key[1], o["option_id"])]
            lines.append(f"  - {key[0]}@{key[1]} ⇒ {o['option_id']} "
                         f"(ops={sorted(opt.ops)}, notices={list(opt.notices)})")
    rel_notices = [n for n in p["notices"]
                   if any(a["source_id"] == n["source_id"] and a["snapshot"] == n["snapshot"]
                          for a in entry["atoms"])]
    lines.append("该行涉及的 notice:")
    for n in rel_notices:
        lines.append(f"  - {n['notice_id']} ← {n['source_id']}@{n['snapshot']}#{n['option_id']}")
    return lines
