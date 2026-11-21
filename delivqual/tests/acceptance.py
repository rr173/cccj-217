"""验收套件：

  1. 全部小样例：validate → plan → 暴力枚举对照 → verify-plan → 专项断言
  2. 篡改测试：删 notice / 换 option / 串快照 / 串请求 / 删行 / 换推导 ⇒ verify 必须拒绝
  3. 随机小例：求解器与暴力枚举判定一致；可行计划与冲突证书均通过核验
  4. 运行时大例：≥20 源 ×4 选项、上万行贡献、高度共享；记录规模/状态/耗时
  5. 预算耗尽 ⇒ unknown（不谎报）

运行：python3 tests/acceptance.py
"""
from __future__ import annotations

import copy
import json
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivqual.brute import brute_feasible
from delivqual.common import load_json
from delivqual.model import validate_graph, validate_request, validate_sources
from delivqual.solver import plan as solve_plan
from delivqual.verify import verify_plan

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "examples")

FAILURES = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILURES.append(msg)


def load_case(d, sf, gf, rf):
    so = load_json(os.path.join(EX, d, sf))
    go = load_json(os.path.join(EX, d, gf))
    ro = load_json(os.path.join(EX, d, rf))
    sources, errs = validate_sources(so)
    assert not errs, errs
    graph, errs, _warns = validate_graph(go, sources)
    assert not errs, errs
    request, errs = validate_request(ro, graph)
    assert not errs, errs
    return so, go, ro, sources, graph, request


# ---------------------------------------------------------------------------
# 1. 小样例

CASES = [
    # (目录, sources, graph, request, 期望状态, 专项断言)
    ("main_drill", "sources.json", "graph_v1.json", "req_R.json", "feasible", None),
    ("main_drill", "sources.json", "graph_v1.json", "req_G.json", "feasible", None),
    ("main_drill", "sources.json", "graph_v1.json", "req_RG.json", "infeasible",
     lambda p: "操作未授权" in p["conflict"]["categories"]),
    ("main_drill", "sources.json", "graph_v2.json", "req_R.json", "feasible", None),
    ("main_drill", "sources.json", "graph_v2.json", "req_G.json", "feasible", None),
    ("main_drill", "sources.json", "graph_v2.json", "req_RG.json", "feasible",
     lambda p: {(o["source_id"], o["option_id"]) for o in p["options"]} == {("S", "X"), ("T", "A")}
     and {n["notice_id"] for n in p["notices"]} == {"n-SX", "n-TA"}),
    ("mutex", "sources.json", "graph.json", "req_both.json", "infeasible",
     lambda p: "全局选项矛盾" in p["conflict"]["categories"]),
    ("mutex", "sources.json", "graph.json", "req_m.json", "feasible", None),
    ("mutex", "sources.json", "graph.json", "req_both2.json", "feasible",
     lambda p: {(o["source_id"], o["option_id"]) for o in p["options"]} == {("M", "M1"), ("N", "N2")}),
    ("diamond", "sources.json", "graph.json", "req.json", "feasible",
     lambda p: {(o["source_id"], o["option_id"]) for o in p["options"]} == {("D", "P")}
     and {n["notice_id"] for n in p["notices"]} == {"n-DP"}),
    ("bad_any", "sources.json", "graph.json", "req.json", "feasible",
     lambda p: {(o["source_id"], o["option_id"]) for o in p["options"]} == {("A", "A1")}
     and {n["notice_id"] for n in p["notices"]} == {"n-AA"}),
    ("mismatch", "sources.json", "graph.json", "req_aud.json", "infeasible",
     lambda p: "时间/受众/地域不符" in p["conflict"]["categories"]),
    ("mismatch", "sources.json", "graph.json", "req_time.json", "infeasible",
     lambda p: "时间/受众/地域不符" in p["conflict"]["categories"]),
    ("mismatch", "sources.json", "graph.json", "req_ok.json", "feasible", None),
    ("missing", "sources.json", "graph.json", "req.json", "infeasible",
     lambda p: "缺失贡献" in p["conflict"]["categories"]),
]


def test_small_examples():
    print("== 1. 小样例：plan + 暴力对照 + verify ==")
    feasible_plans = []
    for (d, sf, gf, rf, expect, extra) in CASES:
        so, go, ro, sources, graph, request = load_case(d, sf, gf, rf)
        p = solve_plan(sources, graph, request, sources_obj=so, graph_obj=go, request_obj=ro)
        name = f"{d}/{gf}/{rf}"
        check(p["status"] == expect, f"{name}: 状态 {p['status']} == 期望 {expect}")
        # 与暴力枚举对照
        bst, _ = brute_feasible(sources, graph, request)
        bverdict = {"feasible": "feasible", "infeasible": "infeasible", "missing": "infeasible"}.get(bst)
        check(bverdict == p["status"], f"{name}: 暴力枚举 {bst} 与求解器一致")
        # verify-plan
        v = verify_plan(sources, graph, request, p, sources_obj=so, graph_obj=go, request_obj=ro)
        check(v["verdict"] == "valid", f"{name}: verify-plan {v['verdict']} == valid {v['reasons']}")
        if extra:
            check(bool(extra(p)), f"{name}: 专项断言")
        if p["status"] == "feasible":
            feasible_plans.append((name, so, go, ro, sources, graph, request, p))
        else:
            # 冲突证书引用行删减后应不再成立（对双行证书）
            cert_rows = p["conflict"]["certificate"]["rows"]
            if len(cert_rows) >= 2:
                p2 = copy.deepcopy(p)
                p2["conflict"]["certificate"]["rows"] = cert_rows[:1]
                v2 = verify_plan(sources, graph, request, p2, sources_obj=so, graph_obj=go, request_obj=ro)
                check(v2["verdict"] != "valid", f"{name}: 删减证书行后 verify 不再确认（{v2['verdict']}）")
    return feasible_plans


# ---------------------------------------------------------------------------
# 2. 篡改测试

def test_tamper(feasible_plans):
    print("== 2. 篡改测试：verify 必须拒绝 ==")
    for (name, so, go, ro, sources, graph, request, p) in feasible_plans:
        # 2.1 删除一个 notice
        if p["notices"]:
            q = copy.deepcopy(p)
            q["notices"] = q["notices"][:-1]
            v = verify_plan(sources, graph, request, q, sources_obj=so, graph_obj=go, request_obj=ro)
            check(v["verdict"] == "invalid", f"{name}: 删除一个 notice ⇒ 拒绝")
        # 2.2 换一个 option（同源另一选项）
        swapped = False
        q = copy.deepcopy(p)
        for o in q["options"]:
            src = sources.by_key[(o["source_id"], o["snapshot"])]
            others = [x for x in src.options if x.option_id != o["option_id"]]
            if others:
                o["option_id"] = others[0].option_id
                swapped = True
                break
        if swapped:
            v = verify_plan(sources, graph, request, q, sources_obj=so, graph_obj=go, request_obj=ro)
            check(v["verdict"] == "invalid", f"{name}: 更换 option ⇒ 拒绝")
        # 2.3 串到另一快照（改 plan 中 snapshot 字符串）
        q = copy.deepcopy(p)
        q["options"][0]["snapshot"] = q["options"][0]["snapshot"] + "-fake"
        v = verify_plan(sources, graph, request, q, sources_obj=so, graph_obj=go, request_obj=ro)
        check(v["verdict"] == "invalid", f"{name}: 篡改 option 快照 ⇒ 拒绝")
        # 2.4 串到另一请求（改时间）
        ro2 = copy.deepcopy(ro)
        ro2["time"] = ro["time"] + 1
        request2, errs = validate_request(ro2, graph)
        assert not errs
        v = verify_plan(sources, graph, request2, p, sources_obj=so, graph_obj=go, request_obj=ro2)
        check(v["verdict"] == "invalid", f"{name}: 串到另一请求 ⇒ 拒绝")
        # 2.5 串到另一来源资料（改 sources 快照 ⇒ 身份哈希不符）
        so2 = copy.deepcopy(so)
        so2["sources"][0]["snapshot"] = so2["sources"][0]["snapshot"] + "-other"
        v = verify_plan(sources, graph, request, p, sources_obj=so2, graph_obj=go, request_obj=ro)
        check(v["verdict"] == "invalid", f"{name}: 串到另一快照资料 ⇒ 拒绝")
        # 2.6 删除一个交付行
        if len(p["rows"]) > 1:
            q = copy.deepcopy(p)
            q["rows"] = q["rows"][:-1]
            v = verify_plan(sources, graph, request, q, sources_obj=so, graph_obj=go, request_obj=ro)
            check(v["verdict"] == "invalid", f"{name}: 删除交付行 ⇒ 拒绝")
        # 2.7 改动推导边（换一条 via_row）
        q = copy.deepcopy(p)
        q["rows"][0]["derivation"]["edges"][0]["via_row"] = "tampered-row"
        v = verify_plan(sources, graph, request, q, sources_obj=so, graph_obj=go, request_obj=ro)
        check(v["verdict"] == "invalid", f"{name}: 篡改推导边 ⇒ 拒绝")


# ---------------------------------------------------------------------------
# 3. 随机小例对照

def gen_random_case(rng):
    n_src = rng.randint(2, 4)
    sources = []
    for i in range(n_src):
        sid = f"S{i}"
        rows = [f"{sid}-r{j}" for j in range(rng.randint(1, 2))]
        opts = []
        for k in range(rng.randint(1, 3)):
            ops = sorted({op for op in ("raw", "aggregate", "join", "union", "project") if rng.random() < 0.6})
            opts.append({
                "option_id": f"o{k}", "ops": ops,
                "audiences": [a for a in ("a", "b") if rng.random() < 0.8] or ["a"],
                "geos": ["g"],
                "time": [0, rng.randint(3, 10)],
                "notices": [f"n-{sid}-{k}"],
                "classifications": [c for c in ("report", "dashboard") if rng.random() < 0.8] or ["report"],
                "mutex": [],
            })
        sources.append({"source_id": sid, "snapshot": "s1", "rows": rows, "options": opts})
    # 随机互斥
    for _ in range(rng.randint(0, 2)):
        a = rng.choice(sources)
        b = rng.choice(sources)
        if a is b:
            continue
        oa = rng.choice(a["options"])
        ob = rng.choice(b["options"])
        oa["mutex"].append({"source_id": b["source_id"], "snapshot": "s1", "option_id": ob["option_id"]})

    pnodes = []
    contribs = []
    node_rows = {}
    cnt = [0]

    def fresh(p):
        cnt[0] += 1
        return f"{p}{cnt[0]}"

    def add_node(op, inputs):
        nid = f"n{len(pnodes)}"
        pnodes.append({"node_id": nid, "op": op, "inputs": list(inputs)})
        return nid

    scan_ids = []
    for s in sources:
        nid = add_node("scan", [])
        scan_ids.append(nid)
        rows = []
        for r in s["rows"]:
            cid = fresh("a")
            contribs.append({"id": cid, "type": "source", "source_id": s["source_id"],
                             "snapshot": "s1", "row_id": r})
            rows.append((r, cid))
        node_rows[nid] = rows

    pool = list(scan_ids)
    for _ in range(rng.randint(1, 4)):
        op = rng.choice(["project", "union_all", "join", "aggregate", "deduplicate"])
        if op in ("project", "aggregate", "deduplicate"):
            inputs = [rng.choice(pool)]
        else:
            inputs = rng.sample(pool, 2) if len(pool) >= 2 else [pool[0], pool[0]]
        nid = add_node(op, inputs)
        rows = []
        if op == "project":
            for (r, cid) in node_rows[inputs[0]]:
                e = fresh("p")
                contribs.append({"id": e, "type": "all", "edges": [{"child": cid, "via_node": nid, "via_row": r}]})
                rows.append((r, e))
        elif op == "union_all":
            for src in inputs:
                for (r, cid) in node_rows[src]:
                    e = fresh("u")
                    contribs.append({"id": e, "type": "all", "edges": [{"child": cid, "via_node": nid, "via_row": r}]})
                    rows.append((r, e))
        elif op == "join":
            left, right = node_rows[inputs[0]], node_rows[inputs[1]]
            for (r1, c1) in left:
                for (r2, c2) in right:
                    e = fresh("j")
                    contribs.append({"id": e, "type": "all", "edges": [
                        {"child": c1, "via_node": nid, "via_row": f"{r1}#L"},
                        {"child": c2, "via_node": nid, "via_row": f"{r2}#R"}]})
                    rows.append((f"{r1}x{r2}", e))
        elif op == "aggregate":
            members = list(node_rows[inputs[0]])
            if members:
                e = fresh("g")
                contribs.append({"id": e, "type": "all", "edges": [
                    {"child": cid, "via_node": nid, "via_row": r} for (r, cid) in members]})
                rows.append(("g0", e))
        else:  # deduplicate
            members = list(node_rows[inputs[0]])
            if len(members) >= 2:
                e = fresh("d")
                contribs.append({"id": e, "type": "any", "edges": [
                    {"child": cid, "via_node": nid, "via_row": r} for (r, cid) in members[:3]]})
                rows.append(("d0", e))
            elif members:
                rows.append((members[0][0], members[0][1]))
        if rows:
            node_rows[nid] = rows
            pool.append(nid)

    terminals = [n for n in pool if n not in scan_ids] or scan_ids
    n_tables = rng.randint(1, 2)
    tables = []
    outputs = []
    req_tables = []
    for ti in range(n_tables):
        nid = terminals[ti % len(terminals)]
        tid = f"T{ti}"
        rows = [r for (r, _c) in node_rows[nid]][:4]
        tables.append({"table_id": tid, "node_id": nid, "rows": rows})
        for (r, cid) in node_rows[nid]:
            if r in rows:
                outputs.append({"table": tid, "row": r, "expr": cid})
        req_tables.append({"table_id": tid, "classification": rng.choice(["report", "dashboard"])})

    graph = {"kind": "graph", "version": 1, "graph_id": "rand",
             "nodes": pnodes, "tables": tables, "contribs": contribs, "outputs": outputs}
    request = {"kind": "request", "version": 1, "request_id": "rand",
               "time": rng.randint(0, 10), "audience": rng.choice(["a", "b"]), "geo": "g",
               "tables": req_tables}
    sources_doc = {"kind": "sources", "version": 1, "sources": sources}
    return sources_doc, graph, request


def test_random_small():
    print("== 3. 随机小例：求解器 vs 暴力枚举 ==")
    rng = random.Random(42)
    n_ok = 0
    for seed in range(150):
        so, go, ro = gen_random_case(rng)
        sources, errs = validate_sources(so)
        check(not errs, f"随机例 {seed}: sources 校验 {errs[:1]}")
        graph, errs, _w = validate_graph(go, sources)
        check(not errs, f"随机例 {seed}: graph 校验 {errs[:1]}")
        request, errs = validate_request(ro, graph)
        check(not errs, f"随机例 {seed}: request 校验 {errs[:1]}")
        if errs:
            continue
        p = solve_plan(sources, graph, request, sources_obj=so, graph_obj=go, request_obj=ro)
        bst, _ = brute_feasible(sources, graph, request)
        bverdict = {"feasible": "feasible", "infeasible": "infeasible", "missing": "infeasible"}.get(bst)
        check(bverdict == p["status"], f"随机例 {seed}: 求解器 {p['status']} == 暴力 {bst}")
        v = verify_plan(sources, graph, request, p, sources_obj=so, graph_obj=go, request_obj=ro)
        check(v["verdict"] == "valid", f"随机例 {seed}: verify {v['verdict']} {v['reasons'][:1]}")
        n_ok += 1
    print(f"  ok: {n_ok}/150 随机小例判定一致且核验通过")


# ---------------------------------------------------------------------------
# 4. 运行时大例

def test_large():
    print("== 4. 运行时大例 ==")
    sys.path.insert(0, os.path.join(EX))
    from gen_large import generate
    so, go, ro = generate(n_sources=24, n_rows=12000, n_tables=4, seed=7)
    sources, errs = validate_sources(so)
    check(not errs, "大例 sources 校验")
    graph, errs, warns = validate_graph(go, sources)
    check(not errs, "大例 graph 校验")
    request, errs = validate_request(ro, graph)
    check(not errs, "大例 request 校验")
    n_edges = sum(len(c.get("edges", [])) for c in go["contribs"])
    print(f"  规模: 源={len(so['sources'])} 选项/源={len(so['sources'][0]['options'])} "
          f"加工节点={len(go['nodes'])} 贡献节点={len(go['contribs'])} 贡献边={n_edges} "
          f"输出行={len(go['outputs'])}")
    check(len(so["sources"]) >= 20, "大例 ≥20 源")
    check(all(len(s["options"]) >= 4 for s in so["sources"]), "大例每源 ≥4 选项")
    check(len(go["outputs"]) >= 10000, "大例 ≥1 万行贡献输出")
    check(len(go["contribs"]) >= 10000, "大例 ≥1 万贡献节点")
    t0 = time.monotonic()
    p = solve_plan(sources, graph, request, sources_obj=so, graph_obj=go, request_obj=ro)
    t1 = time.monotonic()
    check(p["status"] == "feasible", "大例可行")
    v = verify_plan(sources, graph, request, p, sources_obj=so, graph_obj=go, request_obj=ro)
    t2 = time.monotonic()
    check(v["verdict"] == "valid", "大例 verify-plan 有效")
    print(f"  求解状态={p['status']} SAT变量={p['stats']['sat_vars']} 子句={p['stats']['sat_clauses']} "
          f"假设={p['stats']['sat_assumptions']} 冲突={p['stats']['conflicts']}")
    print(f"  耗时: 求解={t1 - t0:.2f}s 独立核验={t2 - t1:.2f}s")
    # 预算耗尽 ⇒ unknown
    p2 = solve_plan(sources, graph, request, budget_seconds=0,
                    sources_obj=so, graph_obj=go, request_obj=ro)
    check(p2["status"] == "unknown", "预算耗尽 ⇒ unknown（不谎报）")
    v2 = verify_plan(sources, graph, request, p2, sources_obj=so, graph_obj=go, request_obj=ro)
    check(v2["verdict"] == "unknown", "unknown 计划的核验结论为 unknown")


# ---------------------------------------------------------------------------
# 5. 结构校验拒绝

def test_broken():
    print("== 5. 结构校验拒绝坏输入 ==")
    so = load_json(os.path.join(EX, "bad_any", "sources.json"))
    go = load_json(os.path.join(EX, "bad_any", "graph_broken.json"))
    sources, errs = validate_sources(so)
    check(not errs, "broken 例 sources 本身合法")
    _g, errs, _w = validate_graph(go, sources)
    joined = "\n".join(errs)
    check("a-r9" in joined, "检出未登记原行引用")
    check("desensitized" in joined, "拒绝“已脱敏”免除来源")
    check("deduplicate" in joined, "检出 any 未由 deduplicate 产生")
    check("cMissing" in joined, "检出悬空贡献边")


def main():
    feasible = test_small_examples()
    test_tamper(feasible)
    test_random_small()
    test_broken()
    test_large()
    print()
    if FAILURES:
        print(f"验收失败 {len(FAILURES)} 项:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("全部验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
