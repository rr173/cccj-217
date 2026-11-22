"""build-totals 验收套件（可独立复跑：python3 -I tests/build_totals.py）。

覆盖：
  1. 小例对照独立暴力计算：真实配对、三元组去重、部门精确整数和（含零和部门）
  2. 同一事实经不同 join_key 得出（充分替代只能走显式 deduplicate/any）
  3. 等额但发票不同的两个事实各计一次、不被合并
  4. 证据展开：图中每条可展开选择都对应同键真实物理配对，且集合与暴力结果逐对相等，
     不存在跨键误配；未匹配原行不出现在图中
  5. 只换输入行序：totals.jsonl 与 graph.json 逐字节一致
  6. 非法输入：重复行号、未登记行号、布尔/非整数 cents、空字段、坏 JSON、
     未知/缺失字段、绑定未登记、两份输入绑定同一快照
  7. 无匹配/空文件：空表 + 空图结果仍通过 validate
  8. 原子交付：目标目录已存在则拒绝且不改写；失败不留半份目录
  9. 运行时重复键大样例：图规模随原行数线性增长，不枚举物理配对（不落地大文件）
 10. 生成的图交给原有 validate_graph 结构校验（零 error 零 warning）
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import tempfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivqual.buildtotals import BuildError, _hid, build_totals, deliver, load_jsonl
from delivqual.common import load_json
from delivqual.model import validate_graph, validate_sources

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "examples", "build_totals")
DQ = os.path.join(ROOT, "dq")

FAILURES = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILURES.append(msg)


# ---------------------------------------------------------------------------
# 独立暴力参照实现（与生成器完全分开写，按题意逐字执行）

def naive_compute(purchase, attribution):
    """返回 (totals_dict, pairs_by_fact)。pairs_by_fact: fact -> {(p_row, a_row)}。"""
    pairs = defaultdict(set)
    for p in purchase:
        for a in attribution:
            if p["join_key"] == a["join_key"]:
                fact = (a["department"], p["invoice_id"], p["cents"])
                pairs[fact].add((p["row_id"], a["row_id"]))
    totals = defaultdict(int)
    for (dept, _inv, cents) in pairs:
        totals[dept] += cents
    return dict(totals), dict(pairs)


def expand_derivations(contribs, cid):
    """通用证据语义求值：返回 {frozenset(source 原子 (sid,snap,row))}。

    source：单原子推导；all：各子推导笛卡尔组合（共同贡献）；
    any：各子推导的并集（充分替代）。
    """
    node = contribs[cid]
    if node["type"] == "source":
        return {frozenset([(node["source_id"], node["snapshot"], node["row_id"])])}
    child_sets = [expand_derivations(contribs, e["child"]) for e in node["edges"]]
    if node["type"] == "any":
        return set().union(*child_sets)
    # all：笛卡尔积后合并原子
    out = {frozenset()}
    for ds in child_sets:
        out = {a | b for a in out for b in ds}
    return out


def graph_pairs_by_fact(graph_obj, p_sid, a_sid):
    """从图中按事实子树展开全部物理配对：fact -> {(p_row, a_row)}。"""
    contribs = {c["id"]: c for c in graph_obj["contribs"]}
    result = {}
    for out in graph_obj["outputs"]:
        root = contribs[out["expr"]]
        check(root["type"] == "all", f"部门根 {out['row']} 是 all（aggregate 分组输入）")
        for e in root["edges"]:
            check(e["via_node"] == "aggregate-T", f"{out['row']} 根边经由 aggregate 节点")
            fact_root = contribs[e["child"]]
            derivs = expand_derivations(contribs, e["child"])
            pairs = set()
            bad_shapes = []
            for d in derivs:
                p_rows = sorted(r for (sid, _s, r) in d if sid == p_sid)
                a_rows = sorted(r for (sid, _s, r) in d if sid == a_sid)
                if len(p_rows) != 1 or len(a_rows) != 1:
                    bad_shapes.append(sorted(d))
                else:
                    pairs.add((p_rows[0], a_rows[0]))
            check(not bad_shapes,
                  f"事实 {e['via_row']} 的每条推导恰为一对真实原行（{len(bad_shapes)} 条畸形，示例 {bad_shapes[:1]}）")
            result[e["via_row"]] = (e["child"], fact_root["type"], pairs)
    return result


def load_jsonl_raw(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# 1-5. 固定小例

def read_example():
    sources_obj = load_json(os.path.join(EX, "sources.json"))
    purchase = load_jsonl_raw(os.path.join(EX, "purchase.jsonl"))
    attribution = load_jsonl_raw(os.path.join(EX, "attribution.jsonl"))
    return sources_obj, purchase, attribution


def build_for(purchase, attribution, sources_obj=None, graph_id="g-test"):
    if sources_obj is None:
        sources_obj, _, _ = read_example()
    precs = list(enumerate([dict(r) for r in purchase], start=1))
    arecs = list(enumerate([dict(r) for r in attribution], start=1))
    return build_totals(sources_obj, precs, arecs,
                        ("P", "p2026"), ("A", "a2026"), graph_id)


def test_small_example():
    print("== 1. 小例：暴力对照（配对/去重/汇总/零和部门）==")
    sources_obj, purchase, attribution = read_example()
    totals_rows, graph_obj, stats = build_for(purchase, attribution, sources_obj)
    exp_totals, exp_pairs = naive_compute(purchase, attribution)

    got = {r["department"]: r["total_cents"] for r in totals_rows}
    check(got == exp_totals, f"部门汇总与暴力计算一致（{got}）")
    check(got == {"dept-a": 240, "dept-b": 200, "dept-c": 0},
          "固定期望值：dept-a=240、dept-b=200、dept-c=0（零和部门仍输出）")
    check([r["row_id"] for r in totals_rows] == ["T0001", "T0002", "T0003"],
          "汇总 row_id 稳定且按部门排序")

    print("== 2. 结构校验：生成图零 error 零 warning，每个汇总行恰好一个根 ==")
    sources, errs = validate_sources(sources_obj)
    check(not errs, f"sources 合法 {errs}")
    _g, gerrs, gwarns = validate_graph(graph_obj, sources)
    check(not gerrs, f"graph 校验无错误: {gerrs}")
    check(not gwarns, f"graph 校验无警告: {gwarns}")
    table_rows = graph_obj["tables"][0]["rows"]
    out_rows = [o["row"] for o in graph_obj["outputs"]]
    check(sorted(table_rows) == sorted(out_rows) and len(set(out_rows)) == len(out_rows),
          "汇总表每个 ID 在图中恰好对应一个根")

    print("== 3. 证据展开：逐事实对照真实物理配对，禁止跨键误配 ==")
    contribs = {c["id"]: c for c in graph_obj["contribs"]}
    key_of_p = {r["row_id"]: r["join_key"] for r in purchase}
    key_of_a = {r["row_id"]: r["join_key"] for r in attribution}
    gpairs = graph_pairs_by_fact(graph_obj, "P", "A")
    check(len(gpairs) == len(exp_pairs),
          f"图覆盖全部 {len(exp_pairs)} 个去重后事实（实际 {len(gpairs)}）")
    for fact, exp in exp_pairs.items():
        fhid = _hid(["fact", *fact])
        label = f"F:{fhid}"
        check(label in gpairs, f"事实 {fact} 在图中有独立子树（{label}）")
        froot_cid, ftype, got_pairs = gpairs[label]
        check(got_pairs == exp, f"事实 {fact} 的可展开配对集合与暴力结果逐对相等")
        cross_key = [(pr, ar) for (pr, ar) in got_pairs
                     if key_of_p[pr] != key_of_a[ar]]
        check(not cross_key, f"事实 {fact} 不存在跨键误配（{len(cross_key)} 条越键，示例 {cross_key[:1]}）")
        # 多物理配对事实：替代必须经 any（类级或事实级显式 deduplicate）；
        # 单物理配对事实的子树中不得出现 any
        subtree_ids = set()
        stack = [froot_cid]
        while stack:
            x = stack.pop()
            if x in subtree_ids:
                continue
            subtree_ids.add(x)
            stack.extend(ce["child"] for ce in contribs[x].get("edges", []))
        has_any = any(contribs[i]["type"] == "any" for i in subtree_ids)
        if len(exp) >= 2:
            check(has_any, f"事实 {fact} 的 {len(exp)} 个替代只经显式 any/deduplicate 表达")
        else:
            check(not has_any, f"单配对事实 {fact} 不构造替代节点")

    print("== 4. 专项：同事实跨键、等额不同发票、未匹配行 ==")
    f1 = ("dept-a", "INV-1", 100)
    _f1_root, f1_type, f1_pairs = gpairs[f"F:{_hid(['fact', *f1])}"]
    keys_used = {key_of_p[p] for (p, _a) in f1_pairs}
    check(f1_type == "any" and keys_used == {"k1", "k2"},
          "INV-1/100 经 k1、k2 两个不同键得出，替代只走 fact 级 deduplicate")
    f6 = ("dept-a", "INV-6", 70)
    f7 = ("dept-a", "INV-7", 70)
    check(f"F:{_hid(['fact', *f6])}" in gpairs and f"F:{_hid(['fact', *f7])}" in gpairs,
          "等额 70 但发票不同（INV-6/INV-7）是两个独立事实，不被合成一个")
    check(got["dept-a"] == 240, "两个等额事实各计一次（100+70+70=240）")
    referenced = {c.get("row_id") for c in graph_obj["contribs"] if c["type"] == "source"}
    check("p-r8" not in referenced and "a-r6" not in referenced,
          "未匹配原行（p-r8/k7、a-r6/k8）不计入也不出现在图中")
    # any 边只能指向 deduplicate 节点（加工节点层面复核）
    node_ops = {n["node_id"]: n["op"] for n in graph_obj["nodes"]}
    for c in graph_obj["contribs"]:
        if c["type"] == "any":
            check({node_ops[e["via_node"]] for e in c["edges"]} == {"deduplicate"},
                  f"any 节点 {c['id']} 的全部边指向同一个 deduplicate")

    print("== 5. 行序无关：洗牌后 totals 与 graph 逐字节一致 ==")
    rng = random.Random(20261122)
    p2, a2 = list(purchase), list(attribution)
    rng.shuffle(p2)
    rng.shuffle(a2)
    totals2, graph2, _ = build_for(p2, a2, sources_obj)
    check(json.dumps(graph2, sort_keys=True) == json.dumps(graph_obj, sort_keys=True),
          "换序后 graph 内容一致")
    check([json.dumps(r) for r in totals2] == [json.dumps(r) for r in totals_rows],
          "换序后 totals 行一致")
    return sources_obj, purchase, attribution


# ---------------------------------------------------------------------------
# 6. 非法输入

def _expect_build_error(name, sources_obj, precs, arecs, pb=("P", "p2026"), ab=("A", "a2026")):
    try:
        build_totals(sources_obj, precs, arecs, pb, ab, "g-bad")
    except BuildError as e:
        print(f"  ok: {name} 被拒绝：{e}")
        return True
    print(f"  FAIL: {name} 应当被拒绝")
    FAILURES.append(name)
    return False


def test_invalid():
    print("== 6. 非法输入拒绝 ==")
    sources_obj, purchase, attribution = read_example()
    base_p = list(enumerate([dict(r) for r in purchase], start=1))
    base_a = list(enumerate([dict(r) for r in attribution], start=1))

    bad_p = [(i, dict(r)) for (i, r) in base_p]
    bad_p[0] = (1, {**bad_p[0][1], "row_id": purchase[1]["row_id"]})
    _expect_build_error("采购重复 row_id", sources_obj, bad_p, base_a)

    bad_p = [(i, dict(r)) for (i, r) in base_p]
    bad_p[0] = (1, {**bad_p[0][1], "row_id": "p-ghost"})
    _expect_build_error("采购行号未登记", sources_obj, bad_p, base_a)

    for bad_val in (True, False, 1.5, "100", None):
        bad_p = [(i, dict(r)) for (i, r) in base_p]
        bad_p[0] = (1, {**bad_p[0][1], "cents": bad_val})
        _expect_build_error(f"cents 非法值 {bad_val!r}", sources_obj, bad_p, base_a)

    bad_p = [(i, dict(r)) for (i, r) in base_p]
    bad_p[0] = (1, {**bad_p[0][1], "invoice_id": ""})
    _expect_build_error("空字符串字段", sources_obj, bad_p, base_a)

    bad_a = [(i, dict(r)) for (i, r) in base_a]
    bad_a[0] = (1, {**bad_a[0][1], "department": 42})
    _expect_build_error("部门非字符串", sources_obj, base_p, bad_a)

    bad_p = [(i, dict(r)) for (i, r) in base_p]
    bad_p[0] = (1, {**bad_p[0][1], "extra": 1})
    _expect_build_error("未知字段", sources_obj, bad_p, base_a)

    bad_p = [(i, dict(r)) for (i, r) in base_p]
    rec = dict(bad_p[0][1])
    del rec["join_key"]
    bad_p[0] = (1, rec)
    _expect_build_error("缺失字段", sources_obj, bad_p, base_a)

    _expect_build_error("绑定未登记快照", sources_obj, base_p, base_a,
                        pb=("P", "nope"))
    _expect_build_error("两份输入绑定同一快照", sources_obj, base_p, base_a,
                        pb=("P", "p2026"), ab=("P", "p2026"))

    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "p.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"row_id": "p-r1", "join_key": "k", "invoice_id": "I", "cents": 1}\n')
            f.write("{not json}\n")
        try:
            load_jsonl(p, "采购明细")
            check(False, "坏 JSON 行应报错")
        except BuildError as e:
            check("第 2 行" in str(e), f"坏 JSON 带行号定位：{e}")


# ---------------------------------------------------------------------------
# 7. 空结果

def test_empty():
    print("== 7. 无匹配 / 空文件 ==")
    sources_obj, _, _ = read_example()
    p = [{"row_id": "p-r1", "join_key": "kx", "invoice_id": "I", "cents": 9}]
    a = [{"row_id": "a-r1", "join_key": "ky", "department": "D"}]
    totals_rows, graph_obj, stats = build_for(p, a, sources_obj)
    check(totals_rows == [], "无匹配时输出空表")
    check(graph_obj["outputs"] == [] and graph_obj["tables"][0]["rows"] == [],
          "无匹配时输出空图结果")
    sources, _ = validate_sources(sources_obj)
    _g, errs, warns = validate_graph(graph_obj, sources)
    check(not errs and not warns, f"空图通过 validate（errs={errs}）")

    totals_rows, graph_obj, _ = build_for([], [], sources_obj)
    check(totals_rows == [] and graph_obj["contribs"] == [], "空文件同样为空表空图")
    _g, errs, warns = validate_graph(graph_obj, sources)
    check(not errs and not warns, "空文件图通过 validate")


# ---------------------------------------------------------------------------
# 8. 原子交付（CLI 子进程，端到端）

def test_delivery_cli():
    print("== 8. 原子交付与退出码 ==")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "deliver")
        # -P：保留显式 PYTHONPATH（受信项目路径），但不把 cwd/脚本目录加入 sys.path，
        # 避免不可信目录下的同名模块（如 json.py）被加载
        env = dict(os.environ, PYTHONPATH=ROOT)
        cmd = [sys.executable, "-P", "-m", "delivqual", "build-totals",
               "--sources", os.path.join(EX, "sources.json"),
               "--purchase", os.path.join(EX, "purchase.jsonl"),
               "--attribution", os.path.join(EX, "attribution.jsonl"),
               "--purchase-source", "P", "--purchase-snapshot", "p2026",
               "--attribution-source", "A", "--attribution-snapshot", "a2026",
               "--out-dir", out]
        r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
        check(r.returncode == 0, f"成功退出码 0（得到 {r.returncode}）{r.stderr}")
        check(sorted(os.listdir(out)) == ["graph.json", "totals.jsonl"],
              "成功后目录恰好含两份交付物")
        before = open(os.path.join(out, "graph.json"), encoding="utf-8").read()
        # 目标已存在：拒绝且不改写
        r2 = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
        check(r2.returncode == 10, f"目录已存在退出码 10（得到 {r2.returncode}）")
        after = open(os.path.join(out, "graph.json"), encoding="utf-8").read()
        check(before == after, "拒绝时不改写已有目录")

        # 坏记录：退出 10，目标目录从未出现
        bad = os.path.join(td, "bad.jsonl")
        with open(bad, "w", encoding="utf-8") as f:
            f.write('{"row_id": "p-r1", "join_key": "k1", "invoice_id": "INV-1", "cents": true}\n')
        out2 = os.path.join(td, "never")
        cmd_bad = [sys.executable, "-P", "-m", "delivqual", "build-totals",
                   "--sources", os.path.join(EX, "sources.json"),
                   "--purchase", bad,
                   "--attribution", os.path.join(EX, "attribution.jsonl"),
                   "--purchase-source", "P", "--purchase-snapshot", "p2026",
                   "--attribution-source", "A", "--attribution-snapshot", "a2026",
                   "--out-dir", out2]
        r3 = subprocess.run(cmd_bad, cwd=ROOT, env=env, capture_output=True, text=True)
        check(r3.returncode == 10 and not os.path.exists(out2),
              "坏记录退出 10 且不留半份交付目录")

        # 生成图交给原有 dq validate（端到端 CLI）
        rv = subprocess.run([sys.executable, "-P", "-m", "delivqual", "validate",
                             "--sources", os.path.join(EX, "sources.json"),
                             "--graph", os.path.join(out, "graph.json")],
                            cwd=ROOT, env=env, capture_output=True, text=True)
        check(rv.returncode == 0 and '"valid": true' in rv.stdout,
              "交付的 graph.json 经 dq validate 端到端通过")


# ---------------------------------------------------------------------------
# 9. 运行时重复键大样例：线性规模

def _runtime_sources(td, n_p_rows, n_a_rows):
    p_rows = [f"p-r{i}" for i in range(n_p_rows)]
    a_rows = [f"a-r{i}" for i in range(n_a_rows)]
    sources_obj = {
        "kind": "sources", "version": 1,
        "sources": [
            {"source_id": "P", "snapshot": "p2026", "rows": p_rows, "options": [
                {"option_id": "full", "ops": ["raw", "aggregate", "join", "project"],
                 "audiences": ["D"], "geos": ["cn"], "time": [0, 10**9],
                 "notices": ["n-P"], "classifications": ["report"], "mutex": []}]},
            {"source_id": "A", "snapshot": "a2026", "rows": a_rows, "options": [
                {"option_id": "full", "ops": ["raw", "aggregate", "join", "project"],
                 "audiences": ["D"], "geos": ["cn"], "time": [0, 10**9],
                 "notices": ["n-A"], "classifications": ["report"], "mutex": []}]},
        ],
    }
    return sources_obj


def _dup_key_case(m, n_p, n_a):
    """m 个键；每键采购 n_p 行、归属 n_a 行，同类内非行号字段完全相同。"""
    purchase, attribution, p_idx, a_idx = [], [], 0, 0
    for ki in range(m):
        key = f"k{ki:06d}"
        for _ in range(n_p):
            purchase.append({"row_id": f"p-r{p_idx}", "join_key": key,
                             "invoice_id": "INV-X", "cents": 100})
            p_idx += 1
        for _ in range(n_a):
            attribution.append({"row_id": f"a-r{a_idx}", "join_key": key,
                                "department": "D"})
            a_idx += 1
    return purchase, attribution


def test_linearity():
    print("== 9. 重复键运行时样例：线性规模，不枚举物理配对 ==")
    sizes = [(50, 40, 1), (100, 80, 1), (30, 100, 100)]
    prev = None
    for (m, n_p, n_a) in sizes:
        sources_obj = _runtime_sources(tempfile.gettempdir(), m * n_p, m * n_a)
        purchase, attribution = _dup_key_case(m, n_p, n_a)
        totals_rows, graph_obj, stats = build_totals(
            sources_obj,
            list(enumerate(purchase, start=1)),
            list(enumerate(attribution, start=1)),
            ("P", "p2026"), ("A", "a2026"), "g-linear")
        n_input = len(purchase) + len(attribution)
        n_physical_pairs = m * n_p * n_a
        n_pair_nodes = sum(1 for c in graph_obj["contribs"] if c["id"].startswith("pair-"))
        n_contribs = len(graph_obj["contribs"])
        n_edges = sum(len(c.get("edges", [])) for c in graph_obj["contribs"])
        # 汇总：所有连接投影为同一三元组 → 去重后只剩一个事实，只计一次
        check(totals_rows == [{"row_id": "T0001", "department": "D", "total_cents": 100}],
              f"m={m},n_p={n_p},n_a={n_a}: 重复证明同一事实只计一次（100）")
        check(stats["distinct_facts"] == 1, "distinct_facts=1")
        # 连接节点数随键数线性，而不是物理配对数
        check(n_pair_nodes == m,
              f"连接贡献节点={n_pair_nodes}==键数 m={m}，物理配对={n_physical_pairs} 未被枚举")
        check(n_contribs <= 6 * n_input + 20,
              f"贡献节点 {n_contribs} 随输入行 {n_input} 线性（上界 6N+20）")
        check(n_edges <= 6 * n_input + 20,
              f"贡献边 {n_edges} 随输入行 {n_input} 线性（上界 6N+20）")
        # 证据仍完整：展开唯一事实得到全部物理配对
        _, _froot, pairs = graph_pairs_by_fact(graph_obj, "P", "A")[f"F:{_hid(['fact', 'D', 'INV-X', 100])}"]
        check(len(pairs) == n_physical_pairs,
              f"图可展开到全部 {n_physical_pairs} 个真实物理配对（实际 {len(pairs)}）")
        sources, _ = validate_sources(sources_obj)
        _g, errs, warns = validate_graph(graph_obj, sources)
        check(not errs and not warns, f"大样例图通过 validate（errs={errs[:1]}）")
        print(f"    规模 m={m} n_p={n_p} n_a={n_a}: 输入行={n_input} "
              f"物理配对={n_physical_pairs} 贡献节点={n_contribs} 边={n_edges} 加工节点={len(graph_obj['nodes'])}")
        if prev is not None:
            ratio_in = n_input / prev[0]
            ratio_c = n_contribs / prev[1]
            check(ratio_c <= ratio_in * 1.6,
                  f"输入放大 {ratio_in:.1f}x，贡献节点仅放大 {ratio_c:.1f}x（无线性以上增长）")
        prev = (n_input, n_contribs)


def main():
    test_small_example()
    test_invalid()
    test_empty()
    test_delivery_cli()
    test_linearity()
    print()
    if FAILURES:
        print(f"build-totals 验收失败 {len(FAILURES)} 项:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("build-totals 全部验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
