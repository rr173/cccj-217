"""verify-plan：只根据原资料（sources/graph/request）与 plan 独立核实全部合同。

  - feasible plan：重走每行推导、重算路径义务与全局选项要求、重算 notice 并集，
    与 plan 逐项比对；任何删 notice / 换 option / 串快照 / 串请求都会被发现。
  - infeasible plan：核对冲突证书中每条要求确为输入事实，并在证书引用的
    输出行子实例上独立重判不可行（小实例用暴力枚举，否则重新求解，均有预算）。
  - unknown plan：只核验身份，不断言任何结论。
"""
from __future__ import annotations

import time
from collections import defaultdict

from .common import canonical, rows_field, sha256_of
from .derive import analyze_selection, required_permissions
from .model import PERMISSION_OPS


def verify_plan(sources, graph, request, plan, sources_obj=None, graph_obj=None,
                request_obj=None, budget_seconds=30.0):
    t0 = time.monotonic()
    reasons = []

    def done(verdict):
        return {"verdict": verdict, "reasons": reasons,
                "seconds": round(time.monotonic() - t0, 6)}

    if not isinstance(plan, dict) or plan.get("kind") != "plan":
        reasons.append("plan 必须是 {\"kind\": \"plan\", ...}")
        return done("invalid")
    ident = plan.get("identity") or {}
    if sources_obj is not None:
        if ident.get("sources_sha256") != sha256_of(sources_obj):
            reasons.append("sources 身份哈希不匹配（规则/来源资料被更换或篡改）")
    if graph_obj is not None:
        if ident.get("graph_sha256") != sha256_of(graph_obj):
            reasons.append("graph 身份哈希不匹配（贡献图被更换或篡改）")
    if request_obj is not None:
        if ident.get("request_sha256") != sha256_of(request_obj):
            reasons.append("request 身份哈希不匹配（请求被更换或篡改）")
        if canonical(plan.get("request")) != canonical(request_obj):
            reasons.append("plan 内嵌请求与给定请求不一致")
    if reasons:
        return done("invalid")

    status = plan.get("status")
    if status == "feasible":
        return _verify_feasible(sources, graph, request, plan, t0)
    if status == "infeasible":
        return _verify_infeasible(sources, graph, request, plan, t0, budget_seconds)
    if status == "unknown":
        return done("unknown")
    reasons.append(f"未知 plan status: {status!r}")
    return done("invalid")


# ---------------------------------------------------------------------------
# feasible

def _verify_feasible(sources, graph, request, plan, t0):
    reasons = []

    def done(verdict):
        return {"verdict": verdict, "reasons": reasons,
                "seconds": round(time.monotonic() - t0, 6)}

    cls_of = dict(request.tables)
    # 1. 请求的输出行集合
    want_rows = {(tid, row) for (tid, _c) in request.tables for row in graph.tables[tid]["rows"]}

    # 2. options 合法且同源不重复
    chosen = {}
    for o in plan.get("options") or []:
        ref = (o.get("source_id"), o.get("snapshot"), o.get("option_id"))
        opt = sources.option_index.get(ref)
        if opt is None:
            reasons.append(f"plan 选择了未登记的选项 {ref}")
            continue
        key = ref[:2]
        if key in chosen:
            reasons.append(f"源 {key} 被赋予了多个 option（{chosen[key].option_id} 与 {ref[2]}）")
        chosen[key] = opt

    # 3. 每行推导复核
    plan_rows = plan.get("rows") or []
    got_rows = set()
    all_edges = []
    roots_tr = []
    per_row_atoms = {}
    for entry in plan_rows:
        tid, row = entry.get("table"), entry.get("row")
        if (tid, row) not in want_rows:
            reasons.append(f"plan 交付了未请求的行 ({tid}, {row})")
            continue
        if (tid, row) in got_rows:
            reasons.append(f"行 ({tid}, {row}) 在 plan 中重复")
            continue
        got_rows.add((tid, row))
        der = entry.get("derivation") or {}
        root = der.get("root")
        expect_root = graph.outputs.get((tid, row))
        if expect_root is None:
            reasons.append(f"行 ({tid}, {row}) 在原资料中没有贡献表达式，plan 却给出推导")
            continue
        if root != expect_root:
            reasons.append(f"行 ({tid}, {row}) 的推导根 {root!r} 应为 {expect_root!r}")
            continue
        nodes = set(der.get("nodes") or [])
        edges = [(e.get("parent"), e.get("child"), e.get("via_node"), e.get("via_row"))
                 for e in (der.get("edges") or [])]
        # 边必须真实存在且节点闭合
        ok = True
        for (p, c, vn, vr) in edges:
            if p not in nodes or c not in nodes:
                reasons.append(f"行 ({tid},{row}): 边 ({p},{c}) 的端点不在推导节点集中")
                ok = False
                continue
            pn = graph.contribs.get(p)
            if pn is None or not any(
                    e.child == c and e.via_node == vn and e.via_row == vr for e in pn.edges):
                reasons.append(f"行 ({tid},{row}): 边 ({p}->{c} via {vn}/{vr}) 在原贡献图中不存在")
                ok = False
        if root not in nodes:
            reasons.append(f"行 ({tid},{row}): 推导根不在节点集中")
            ok = False
        # 选择有效性：all 全选、any 恰好一条、source 无出边
        edges_from = defaultdict(list)
        for e in edges:
            edges_from[e[0]].append(e)
        for n in nodes:
            node = graph.contribs.get(n)
            if node is None:
                reasons.append(f"行 ({tid},{row}): 节点 {n} 不存在")
                ok = False
                continue
            out = edges_from.get(n, [])
            if node.type == "source":
                if out:
                    reasons.append(f"行 ({tid},{row}): source 原子 {n} 不应有出边")
                    ok = False
            elif node.type == "all":
                need = {(e.child, e.via_node, e.via_row) for e in node.edges}
                have = {(c, vn, vr) for (_p, c, vn, vr) in out}
                if need != have:
                    reasons.append(f"行 ({tid},{row}): all 节点 {n} 必须包含全部边")
                    ok = False
            elif node.type == "any":
                if len(out) != 1:
                    reasons.append(f"行 ({tid},{row}): any 节点 {n} 必须恰好选择一条边，实际 {len(out)}")
                    ok = False
        # 闭包：从 root 可达恰好这些节点
        if ok:
            reach = set()
            stack = [root]
            while stack:
                x = stack.pop()
                if x in reach:
                    continue
                reach.add(x)
                stack.extend(c for (_p, c, _vn, _vr) in edges_from.get(x, []))
            if reach != nodes:
                reasons.append(f"行 ({tid},{row}): 推导包含从根不可达的冗余节点")
                ok = False
        if not ok:
            continue
        # 重算该行原子的路径性质并与 plan 比对
        info, _ = analyze_selection(graph, [(root, tid, row)], edges)
        atoms = []
        for cid in nodes:
            node = graph.contribs[cid]
            if node.type != "source":
                continue
            f = info[cid]
            atoms.append({
                "source_id": node.source_key[0],
                "snapshot": node.source_key[1],
                "row_id": node.row_id,
                "path_ops": _path_ops(f),
                "requires": sorted(required_permissions(f)),
                "tables": sorted(f["tables"]),
            })
        atoms.sort(key=lambda a: (a["source_id"], a["snapshot"], a["row_id"]))
        if atoms != entry.get("atoms"):
            reasons.append(f"行 ({tid},{row}): plan 所列原子/路径义务与重算结果不一致")
        per_row_atoms[(tid, row)] = atoms
        all_edges.extend(edges)
        roots_tr.append((root, tid, row))

    missing_rows = want_rows - got_rows
    if missing_rows:
        reasons.append(f"plan 缺少 {len(missing_rows)} 个请求行的交付: {sorted(missing_rows)[:5]}")

    if reasons:
        return done("invalid")

    # 4. 全局合并要求并核对所选 option
    g_info, atom_rows = analyze_selection(graph, roots_tr, all_edges)
    src_req = defaultdict(lambda: {"ops": set(), "tables": set(), "atoms": []})
    for cid, node in graph.contribs.items():
        if cid not in g_info or node.type != "source":
            continue
        key = node.source_key
        src_req[key]["ops"] |= required_permissions(g_info[cid])
        src_req[key]["tables"] |= g_info[cid]["tables"]
        src_req[key]["atoms"].append(cid)

    used = set(src_req)
    if set(chosen) != used:
        reasons.append(
            f"plan 的 option 覆盖与实际使用不符: 多选 {sorted(set(chosen) - used)}，"
            f"漏选 {sorted(used - set(chosen))}")
    for key in used & set(chosen):
        opt = chosen[key]
        r = src_req[key]
        lack = r["ops"] - opt.ops
        if lack:
            reasons.append(f"源 {key} 需要操作 {sorted(lack)}，但所选 option {opt.option_id} 未许可")
        for t in r["tables"]:
            cls = cls_of[t]
            if cls not in opt.classifications:
                reasons.append(f"源 {key} 用于表 {t}（分类 {cls}），但 option {opt.option_id} 未许可该分类")
        if request.audience not in opt.audiences:
            reasons.append(f"源 {key} 的 option {opt.option_id} 不覆盖请求受众 {request.audience!r}")
        if request.geo not in opt.geos:
            reasons.append(f"源 {key} 的 option {opt.option_id} 不覆盖请求地域 {request.geo!r}")
        if not (opt.tlo <= request.time <= opt.thi):
            reasons.append(f"源 {key} 的 option {opt.option_id} 的有效区间不含请求时间 {request.time}")

    # 5. 互斥
    for key, opt in chosen.items():
        for tgt in opt.mutex:
            if tgt[:2] in chosen and chosen[tgt[:2]].option_id == tgt[2]:
                reasons.append(f"选项 {opt.ref} 与 {tgt} 互斥却被同时选用")

    # 6. notice 并集重算比对
    expect_notices = []
    for key in sorted(used):
        opt = chosen.get(key)
        if opt is None:
            continue
        rows = set()
        for cid in src_req[key]["atoms"]:
            rows |= atom_rows.get(cid, set())
        rationale = {
            "rows": rows_field(rows),
            "ops_required": sorted(src_req[key]["ops"]),
            "tables": sorted(src_req[key]["tables"]),
            "classifications": sorted(cls_of[t] for t in src_req[key]["tables"]),
            "reason": (f"源 {key[0]}@{key[1]} 被 {len(rows)} 个交付行使用，"
                       f"需要操作 {sorted(src_req[key]['ops'])}，"
                       f"故选 option {opt.option_id}"),
        }
        for n in opt.notices:
            expect_notices.append({
                "notice_id": n, "source_id": key[0], "snapshot": key[1],
                "option_id": opt.option_id, "rows": rationale["rows"], "rationale": rationale,
            })
    expect_notices.sort(key=lambda x: (x["notice_id"], x["source_id"], x["option_id"]))
    got_notices = sorted(plan.get("notices") or [],
                         key=lambda x: (x.get("notice_id"), x.get("source_id"), x.get("option_id")))
    if canonical(got_notices) != canonical(expect_notices):
        reasons.append("notice 并集与重算结果不一致（notice 被增删或来源/行/依据被改动）")

    if reasons:
        return done("invalid")
    return done("valid")


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
# infeasible：证书核验

def _verify_infeasible(sources, graph, request, plan, t0, budget_seconds):
    reasons = []

    def done(verdict):
        return {"verdict": verdict, "reasons": reasons,
                "seconds": round(time.monotonic() - t0, 6)}

    conflict = plan.get("conflict")
    if not conflict:
        reasons.append("infeasible plan 缺少 conflict 证书")
        return done("invalid")
    cert_rows = [(c.get("table"), c.get("row")) for c in (conflict.get("certificate") or {}).get("rows", [])]
    cls_of = dict(request.tables)
    req_rows = []
    for (t, r) in cert_rows:
        if t not in cls_of:
            reasons.append(f"证引用了未请求的表 {t!r}")
        elif r not in graph.tables[t]["rows"]:
            reasons.append(f"证书引用了表 {t} 中不存在的行 {r!r}")
        else:
            req_rows.append((t, r, cls_of[t]))
    # 每条要求必须是可复查的输入事实
    for q in conflict.get("requirements") or []:
        kind = q.get("kind")
        if kind in ("op", "class", "alo"):
            key = (q.get("source_id"), q.get("snapshot"))
            src = sources.by_key.get(key)
            if src is None or q.get("row_id") not in src.rows:
                reasons.append(f"要求 {q.get('id')}: 引用的源行 {key}#{q.get('row_id')} 未登记")
            if kind == "op" and q.get("require") not in PERMISSION_OPS:
                reasons.append(f"要求 {q.get('id')}: 未知操作权限 {q.get('require')!r}")
            if kind == "class" and cls_of.get(q.get("table")) != q.get("classification"):
                reasons.append(f"要求 {q.get('id')}: 表 {q.get('table')} 的分类与请求不符")
        elif kind == "applicability":
            ref = (q.get("source_id"), q.get("snapshot"), q.get("option_id"))
            opt = sources.option_index.get(ref)
            if opt is None:
                reasons.append(f"要求 {q.get('id')}: 引用未登记选项 {ref}")
            else:
                recomputed = []
                if request.audience not in opt.audiences:
                    recomputed.append("audience")
                if request.geo not in opt.geos:
                    recomputed.append("geo")
                if not (opt.tlo <= request.time <= opt.thi):
                    recomputed.append("time")
                if not recomputed:
                    reasons.append(f"要求 {q.get('id')}: 选项 {ref} 实际满足请求适用性，排除理由不成立")
        elif kind == "mutex":
            left, right = tuple(q.get("left") or ()), tuple(q.get("right") or ())
            lo = sources.option_index.get(left)
            ro = sources.option_index.get(right)
            if lo is None or ro is None:
                reasons.append(f"要求 {q.get('id')}: 互斥引用了未登记选项")
            elif not (right in lo.mutex or left in ro.mutex):
                reasons.append(f"要求 {q.get('id')}: {left} 与 {right} 之间没有登记的互斥")
        elif kind == "missing":
            if graph.outputs.get((q.get("table"), q.get("row"))) is not None:
                reasons.append(f"要求 {q.get('id')}: 行 ({q.get('table')},{q.get('row')}) 实际有贡献表达式")
        else:
            reasons.append(f"要求 {q.get('id')}: 未知种类 {kind!r}")
    if reasons:
        return done("invalid")
    if not req_rows and not any(q.get("kind") == "missing" for q in conflict.get("requirements") or []):
        reasons.append("证书没有引用任何输出行，无法独立重现矛盾")
        return done("invalid")

    # 在证书引用的子实例上独立重判
    from .brute import brute_feasible
    verdict = brute_feasible(sources, graph, request, req_rows=req_rows,
                             max_combos=60_000, deadline=t0 + budget_seconds)
    if verdict[0] in ("infeasible", "missing"):
        return done("valid")
    if verdict[0] == "feasible":
        # 暴力枚举找到了子实例的交付方式 ⇒ 证书不成立
        reasons.append("证书引用的子实例实际可行，冲突不成立")
        return done("invalid")
    # 暴力预算耗尽 ⇒ 用求解器重判子实例
    from .solver import plan as solve_plan
    sub = solve_plan(sources, graph, request, req_rows=req_rows,
                     budget_seconds=budget_seconds, self_check=False)
    st = sub["status"]
    if st == "infeasible":
        return done("valid")
    if st == "feasible":
        reasons.append("证书引用的子实例实际可行，冲突不成立")
        return done("invalid")
    return done("unknown")
