"""plan：求解全局一致的来源选项与义务，或给出可复查的冲突。"""
from __future__ import annotations

import time
from collections import defaultdict

from .common import rows_field, sha256_of
from .compiler import Compiler
from .derive import analyze_selection, prune_selection, required_permissions
from .sat_cdcl import CDCL

CATEGORY_BY_KIND = {
    "op": "操作未授权",
    "applicability": "时间/受众/地域不符",
    "class": "输出分类不符",
    "mutex": "全局选项矛盾",
    "alo": "缺失可用选项",
    "missing": "缺失贡献",
}

# 核最小化的工作量上限（超出则保留非最小核；题目允许非最小冲突集）
_MINIMIZE_MAX_CLAUSES = 200_000
_MINIMIZE_MAX_SECONDS = 8.0


def plan(sources, graph, request, req_rows=None, budget_conflicts=None, budget_seconds=None,
         sources_obj=None, graph_obj=None, request_obj=None, self_check=True):
    """返回 plan dict（status ∈ feasible / infeasible / unknown）。"""
    t0 = time.monotonic()
    comp = Compiler(sources, graph, request, req_rows=req_rows).build()
    stats = {
        "rows_requested": len(comp.req_rows),
        "rows_missing_contribution": len(comp.missing),
        "contrib_nodes_reachable": len(comp.sel),
        "contrib_edges": len(comp.edges),
        "sources_used_candidates": len(comp.used_sources),
        "sat_vars": comp.nvars,
        "sat_clauses": len(comp.clauses),
        "sat_assumptions": len(comp.acts),
    }
    identity = {
        "sources_sha256": sha256_of(sources_obj) if sources_obj is not None else None,
        "graph_sha256": sha256_of(graph_obj) if graph_obj is not None else None,
        "request_sha256": sha256_of(request_obj) if request_obj is not None else None,
    }
    base = {
        "kind": "plan",
        "version": 1,
        "identity": identity,
        "request": request_obj,
        "budget": {"conflicts": budget_conflicts, "seconds": budget_seconds},
    }

    if not comp.roots and comp.missing:
        # 没有任何可交付行
        return _infeasible_plan(base, comp, [], stats, t0)

    solver = CDCL(comp.nvars)
    for cl in comp.clauses:
        solver.add_clause(cl)
    acts = sorted(comp.acts)
    status = solver.solve(assumptions=acts, conf_budget=budget_conflicts, seconds=budget_seconds)
    stats.update(solver.stats)
    stats["solve_seconds"] = round(time.monotonic() - t0, 6)

    if status == "unknown":
        p = dict(base)
        p.update({
            "status": "unknown",
            "reason": "预算耗尽（既不断言可行也不断言不可行）",
            "options": [], "rows": [], "notices": [], "conflict": None,
            "stats": stats,
        })
        return p

    if status == "unsat":
        core = list(solver.core or [])
        core = _minimize_core(comp, core, budget_seconds)
        return _infeasible_plan(base, comp, core, stats, t0)

    if comp.missing:
        # 其余行虽可满足，但缺失贡献的行使整体交付不可行
        return _infeasible_plan(base, comp, [], stats, t0)

    # ---------------- SAT：提取并剪枝 ----------------
    model = solver.model()
    esel_true = set()
    for idx, (p_, c, vn, vr, _op) in enumerate(comp.edges):
        if model[comp.esel[idx]]:
            esel_true.add((p_, c, vn, vr))
    root_ids = [expr for (expr, _t, _r, _c) in comp.roots]
    nodes, edges = prune_selection(graph, root_ids, esel_true)
    roots_tr = [(expr, tid, row) for (expr, tid, row, _c) in comp.roots]
    info, atom_rows = analyze_selection(graph, roots_tr, edges)

    # 每个被用源的全局合并要求
    src_req = defaultdict(lambda: {"ops": set(), "tables": set(), "atoms": []})
    for cid in nodes:
        node = graph.contribs[cid]
        if node.type != "source":
            continue
        key = node.source_key
        r = src_req[key]
        r["ops"] |= required_permissions(info[cid])
        r["tables"] |= info[cid]["tables"]
        r["atoms"].append(cid)

    # 从模型取每个被用源的 option
    chosen = {}
    for key in src_req:
        ovs = [o for o in sources.by_key[key].options if model[comp.optvar[(key, o.option_id)]]]
        if len(ovs) != 1:
            raise AssertionError(f"内部错误：源 {key} 在模型中有 {len(ovs)} 个 option")
        chosen[key] = ovs[0]

    # 每行交付明细
    cls_of = dict(request.tables)
    plan_rows = []
    children_of = defaultdict(list)
    for (p_, c, vn, vr) in edges:
        children_of[p_].append((c, vn, vr))
    for (expr, tid, row, _c) in comp.roots:
        sub_nodes, sub_edges = _sub_selection(graph, expr, children_of)
        r_info, _ = analyze_selection(graph, [(expr, tid, row)], sub_edges)
        atoms = []
        for cid in sub_nodes:
            node = graph.contribs[cid]
            if node.type != "source":
                continue
            f = r_info[cid]
            atoms.append({
                "source_id": node.source_key[0],
                "snapshot": node.source_key[1],
                "row_id": node.row_id,
                "path_ops": _path_ops(f),
                "requires": sorted(required_permissions(f)),
                "tables": sorted(f["tables"]),
            })
        atoms.sort(key=lambda a: (a["source_id"], a["snapshot"], a["row_id"]))
        plan_rows.append({
            "table": tid, "row": row,
            "derivation": {
                "root": expr,
                "nodes": sorted(sub_nodes),
                "edges": [{"parent": p_, "child": c, "via_node": vn, "via_row": vr}
                          for (p_, c, vn, vr) in sorted(sub_edges)],
            },
            "atoms": atoms,
        })

    # notice：实际所选源 option 的并集，附源、行和选择依据
    notices = []
    for key in sorted(chosen):
        opt = chosen[key]
        rows = set()
        for cid in src_req[key]["atoms"]:
            rows |= atom_rows.get(cid, set())
        classes = sorted(cls_of[t] for t in src_req[key]["tables"])
        rationale = {
            "rows": rows_field(rows),
            "ops_required": sorted(src_req[key]["ops"]),
            "tables": sorted(src_req[key]["tables"]),
            "classifications": classes,
            "reason": (f"源 {key[0]}@{key[1]} 被 {len(rows)} 个交付行使用，"
                       f"需要操作 {sorted(src_req[key]['ops'])}，"
                       f"故选 option {opt.option_id}"),
        }
        for n in opt.notices:
            notices.append({
                "notice_id": n,
                "source_id": key[0],
                "snapshot": key[1],
                "option_id": opt.option_id,
                "rows": rationale["rows"],
                "rationale": rationale,
            })
    notices.sort(key=lambda x: (x["notice_id"], x["source_id"], x["option_id"]))

    p = dict(base)
    p.update({
        "status": "feasible",
        "options": [{"source_id": k[0], "snapshot": k[1], "option_id": chosen[k].option_id}
                    for k in sorted(chosen)],
        "rows": plan_rows,
        "notices": notices,
        "conflict": None,
        "stats": stats,
    })

    if self_check:
        from .verify import verify_plan
        verdict = verify_plan(sources, graph, request, p,
                              sources_obj=sources_obj, graph_obj=graph_obj, request_obj=request_obj)
        if verdict["verdict"] != "valid":
            raise AssertionError(f"内部错误：生成的 plan 未通过自校验: {verdict['reasons']}")
        stats["self_verify_seconds"] = verdict["seconds"]
    stats["total_seconds"] = round(time.monotonic() - t0, 6)
    return p


def _sub_selection(graph, root, children_of):
    """全局所选子 DAG 中从 root 可达的部分。"""
    nodes, edges, seen = [], [], set()
    stack = [root]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        nodes.append(n)
        for (c, vn, vr) in children_of.get(n, ()):
            edges.append((n, c, vn, vr))
            stack.append(c)
    return nodes, edges


def _path_ops(f):
    ops = []
    if f["aggP"]:
        ops.append("aggregate")
    if f["hasJ"]:
        ops.append("join")
    if f["hasU"]:
        ops.append("union")
    if f["hasP"]:
        ops.append("project")
    return ops


# ---------------------------------------------------------------------------
# 不可行：冲突集与分类

def _infeasible_plan(base, comp, core, stats, t0):
    requirements = []
    categories = []
    involved_sources = set()
    cert_rows = set()
    truncated = False

    for act in core:
        d = comp.acts[act]
        requirements.append({k: v for k, v in d.items()})
        cat = CATEGORY_BY_KIND.get(d["kind"], d["kind"])
        if cat not in categories:
            categories.append(cat)
        if "source_id" in d:
            involved_sources.add((d["source_id"], d.get("snapshot")))
        if "left" in d:
            involved_sources.add((d["left"][0], d["left"][1]))
            involved_sources.add((d["right"][0], d["right"][1]))
        # 证书行：能到达该要求原子的全部请求行
        atom_cid = d.get("atom_cid")
        if atom_cid is not None:
            rows = comp.atom_rows.get(atom_cid, set())
            if len(rows) >= 64:
                truncated = True
            cert_rows |= rows

    for (tid, row) in comp.missing:
        requirements.append({
            "id": f"M{len(requirements) + 1}",
            "kind": "missing", "table": tid, "row": row,
            "detail": f"输出行 {tid}/{row} 没有登记的贡献表达式（缺失贡献）",
        })
        if "缺失贡献" not in categories:
            categories.append("缺失贡献")
        cert_rows.add((tid, row))

    if not core and not comp.missing:
        categories.append("结构不可行")

    if truncated:
        # 原子被过多行共享，无法精确枚举引用行 ⇒ 证书引用全部请求行
        cert_rows = {(t, r) for (t, r, _c) in comp.req_rows}
        cert_note = "冲突原子被超过 64 个输出行共享，证书引用全部请求行"
    else:
        cert_note = "仅引用这些输出行（全部源与选项可用）即可重现矛盾"

    conflict = {
        "categories": categories,
        "requirements": requirements,
        "involved_sources": [{"source_id": s, "snapshot": sn} for (s, sn) in sorted(involved_sources)],
        "certificate": {
            "rows": [{"table": t, "row": r} for (t, r) in sorted(cert_rows)],
            "note": cert_note,
        },
    }
    p = dict(base)
    p.update({
        "status": "infeasible",
        "options": [], "rows": [], "notices": [],
        "conflict": conflict,
        "stats": stats,
    })
    stats["total_seconds"] = round(time.monotonic() - t0, 6)
    return p


def _minimize_core(comp, core, budget_seconds):
    """删除法最小化假设核（有工作量上限；非最小也合法）。"""
    if not core or len(comp.clauses) > _MINIMIZE_MAX_CLAUSES:
        return core
    deadline = time.monotonic() + min(_MINIMIZE_MAX_SECONDS,
                                      budget_seconds if budget_seconds else _MINIMIZE_MAX_SECONDS)
    work = list(core)
    i = 0
    while i < len(work):
        if time.monotonic() > deadline:
            break
        cand = work[:i] + work[i + 1:]
        s = CDCL(comp.nvars)
        for cl in comp.clauses:
            s.add_clause(cl)
        st = s.solve(assumptions=cand)
        if st == "unsat":
            work = list(s.core or cand)
            i = 0
        else:
            i += 1
    return work
