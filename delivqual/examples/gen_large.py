"""运行时生成大规模交付核验实例（不落地预置大文件）。

用法：
  python3 examples/gen_large.py --out /tmp/large [--sources 24] [--rows 12000] \
      [--tables 4] [--seed 7]

保证：每源 4 个选项（含一个全权限 full 选项），实例必然可行；
互斥只出现在非 full 选项之间。贡献图为带大量共享的 DAG，节点数随 --rows 增长。
"""
from __future__ import annotations

import argparse
import json
import os
import random

ALL_OPS = ["raw", "aggregate", "join", "union", "project"]
CLASSES = ["report", "dashboard", "export"]


def generate(n_sources=24, n_rows=12000, n_tables=4, seed=7):
    rng = random.Random(seed)
    aud, geo, tmax = "analytics", "global", 10**6

    # ---------------- 源与选项 ----------------
    sources = []
    for i in range(n_sources):
        sid = f"S{i:02d}"
        snap = f"snap-{seed}"
        rows = [f"{sid}-r{j}" for j in range(120)]
        opts = [
            {"option_id": "full", "ops": list(ALL_OPS), "audiences": [aud], "geos": [geo],
             "time": [0, tmax], "notices": [f"n-{sid}-full"], "classifications": list(CLASSES), "mutex": []},
            {"option_id": "rawonly", "ops": ["raw"], "audiences": [aud], "geos": [geo],
             "time": [0, tmax], "notices": [f"n-{sid}-raw"], "classifications": ["report"], "mutex": []},
            {"option_id": "aggonly", "ops": ["aggregate"], "audiences": [aud], "geos": [geo],
             "time": [0, tmax], "notices": [f"n-{sid}-agg"], "classifications": ["report", "dashboard"], "mutex": []},
            {"option_id": "mid", "ops": ["raw", "join", "union", "project"], "audiences": [aud], "geos": [geo],
             "time": [0, tmax], "notices": [f"n-{sid}-mid"], "classifications": ["report", "export"], "mutex": []},
        ]
        sources.append({"source_id": sid, "snapshot": snap, "rows": rows, "options": opts})
    # 少量互斥（仅非 full 选项之间，保证实例仍可行）
    for i in range(0, n_sources - 1, 3):
        a = sources[i]["options"][1]  # rawonly
        b_id, b_snap = sources[i + 1]["source_id"], sources[i + 1]["snapshot"]
        a["mutex"].append({"source_id": b_id, "snapshot": b_snap, "option_id": "rawonly"})

    # ---------------- 加工图 + 贡献表达式（逐节点物化行） ----------------
    pnodes = []     # 加工节点
    contribs = []   # 贡献节点
    node_rows = {}  # node_id -> [(row_id, expr_cid)]
    expr_count = [0]

    def add_pnode(op, inputs):
        nid = f"n{len(pnodes)}"
        pnodes.append({"node_id": nid, "op": op, "inputs": list(inputs)})
        return nid

    def add_expr(expr):
        contribs.append(expr)
        return expr["id"]

    def fresh(prefix):
        expr_count[0] += 1
        return f"{prefix}{expr_count[0]}"

    # scan：每源一个
    scan_ids = []
    for s in sources:
        nid = add_pnode("scan", [])
        scan_ids.append(nid)
        rows = []
        for r in s["rows"][:100]:
            cid = add_expr({"id": fresh("a"), "type": "source",
                            "source_id": s["source_id"], "snapshot": s["snapshot"], "row_id": r})
            rows.append((r, cid))
        node_rows[nid] = rows

    def sample_rows(nid, k):
        rows = node_rows[nid]
        return [rows[rng.randrange(len(rows))] for _ in range(k)]

    # 中层：随机 DAG（密度使贡献节点规模与输出行同量级）
    # 先保证每个 scan 都进入加工图
    mid_ids = []
    for nid in scan_ids:
        pid = add_pnode("project", [nid])
        mid_ids.append(pid)
        rows = []
        for (r, cid) in sample_rows(nid, 150):
            e = add_expr({"id": fresh("p"), "type": "all", "edges": [
                {"child": cid, "via_node": pid, "via_row": r}]})
            rows.append((r, e))
        node_rows[pid] = rows

    n_mid = max(30, n_rows // 150)
    for _ in range(n_mid):
        op = rng.choices(["project", "union_all", "join", "aggregate", "deduplicate"],
                         weights=[30, 20, 20, 15, 15])[0]
        pool = scan_ids + mid_ids
        if op in ("project", "aggregate", "deduplicate"):
            inputs = [rng.choice(pool)]
        else:
            inputs = rng.sample(pool, 2)
        nid = add_pnode(op, inputs)
        mid_ids.append(nid)
        rows = []
        if op == "project":
            for (r, cid) in sample_rows(inputs[0], 150):
                e = add_expr({"id": fresh("p"), "type": "all", "edges": [
                    {"child": cid, "via_node": nid, "via_row": r}]})
                rows.append((r, e))
        elif op == "union_all":
            for src in inputs:
                for (r, cid) in sample_rows(src, 100):
                    e = add_expr({"id": fresh("u"), "type": "all", "edges": [
                        {"child": cid, "via_node": nid, "via_row": r}]})
                    rows.append((r, e))
        elif op == "join":
            left = node_rows[inputs[0]]
            right = node_rows[inputs[1]]
            for j in range(120):
                r1 = left[rng.randrange(len(left))]
                r2 = right[rng.randrange(len(right))]
                e = add_expr({"id": fresh("j"), "type": "all", "edges": [
                    {"child": r1[1], "via_node": nid, "via_row": f"{r1[0]}#L"},
                    {"child": r2[1], "via_node": nid, "via_row": f"{r2[0]}#R"}]})
                rows.append((f"{nid}-x{j}", e))
        elif op == "aggregate":
            for g in range(60):
                members = sample_rows(inputs[0], rng.randint(2, 4))
                e = add_expr({"id": fresh("g"), "type": "all", "edges": [
                    {"child": cid, "via_node": nid, "via_row": r} for (r, cid) in members]})
                rows.append((f"{nid}-g{g}", e))
        else:  # deduplicate
            for d in range(80):
                members = sample_rows(inputs[0], rng.randint(2, 3))
                e = add_expr({"id": fresh("d"), "type": "any", "edges": [
                    {"child": cid, "via_node": nid, "via_row": r} for (r, cid) in members]})
                rows.append((f"{nid}-d{d}", e))
        node_rows[nid] = rows

    # ---------------- 输出表 ----------------
    # 每个终端是覆盖多个中层节点的宽 union，使交付用到全部源
    terminals = []
    for _ in range(n_tables):
        inputs = rng.sample(mid_ids, min(10, len(mid_ids)))
        nid = add_pnode("union_all", inputs)
        terminals.append(nid)
        rows = []
        for src in inputs:
            for (r, cid) in sample_rows(src, 100):
                e = add_expr({"id": fresh("u"), "type": "all", "edges": [
                    {"child": cid, "via_node": nid, "via_row": r}]})
                rows.append((r, e))
        node_rows[nid] = rows
    tables = []
    outputs = []
    per_table = n_rows // n_tables
    for ti, nid in enumerate(terminals):
        tid = f"T{ti}"
        rows = node_rows[nid]
        trows = []
        for j in range(per_table):
            rid = f"{tid}-{j}"
            _r, cid = rows[rng.randrange(len(rows))]
            trows.append(rid)
            outputs.append({"table": tid, "row": rid, "expr": cid})
        tables.append({"table_id": tid, "node_id": nid, "rows": trows})

    graph = {
        "kind": "graph", "version": 1, "graph_id": f"large-{seed}",
        "nodes": pnodes, "tables": tables, "contribs": contribs, "outputs": outputs,
    }
    request = {
        "kind": "request", "version": 1, "request_id": f"req-large-{seed}",
        "time": 500000, "audience": aud, "geo": geo,
        "tables": [{"table_id": f"T{i}", "classification": CLASSES[i % len(CLASSES)]}
                   for i in range(n_tables)],
    }
    sources_doc = {"kind": "sources", "version": 1, "sources": sources}
    return sources_doc, graph, request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--sources", type=int, default=24)
    ap.add_argument("--rows", type=int, default=12000)
    ap.add_argument("--tables", type=int, default=4)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    sources, graph, request = generate(args.sources, args.rows, args.tables, args.seed)
    for name, obj in (("sources", sources), ("graph", graph), ("request", request)):
        with open(os.path.join(args.out, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    n_edges = sum(len(c.get("edges", [])) for c in graph["contribs"])
    print(json.dumps({
        "sources": len(sources["sources"]),
        "options_per_source": len(sources["sources"][0]["options"]),
        "processing_nodes": len(graph["nodes"]),
        "contrib_nodes": len(graph["contribs"]),
        "contrib_edges": n_edges,
        "output_rows": len(graph["outputs"]),
        "tables": len(graph["tables"]),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
