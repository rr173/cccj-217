"""quarantine 验收套件（可独立复跑：python3 -I tests/quarantine.py）。

覆盖（从旧包核对新包，而不只看命令是否成功）：
  1. 共用凭据小例（examples/quarantine）：
     - 必需分支失效 ⇒ 记录撤下；多条健全替代 ⇒ 记录保留
     - 同一失效原子被多条撤下记录引用时，证据树不逐行复制（共享理由节点）
     - 隔离图恰好表达全部未失效推导；只剩一条的替代不被接到更上层
     - 表定义在全部行撤下后仍存在；撤下记录从图的表行与副本一起移除
     - 给定请求下只有一条替代可用于 dq plan；另一请求下可改选别的替代
     - 隔离图通过原 dq validate / plan / verify-plan
     - 撤下凭据可用原图 + 失效清单独立复查（独立活性求值 + 理由链核对）
  2. 同一快照其他原行、其他快照同名行不被误伤；已登记未参与交付的失效不影响结果
  3. 运行时层层共享贡献 + 多条输出：副本/凭据规模随图节点与记录线性，
     共享理由节点不按输出行复制；不枚举完整推导
  4. 流式：大载荷逐行复制，内存不随载荷总量线性增长
  5. 原子交付：最后一条输入记录出错时目录不发布；坏输入退出码与目标缺失
  6. 无撤下时空凭据；全部撤下时表定义仍在且 validate 通过
  7. 原始输入文件不被修改（哈希前后一致）
"""
from __future__ import annotations

import hashlib
import json
import os
import resource
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivqual.common import load_json, sha256_of
from delivqual.model import validate_graph, validate_sources
from delivqual.quarantine import (
    QuarantineError,
    build_reason_graph,
    evaluate_liveness,
    parse_invalidations,
    quarantine,
    stream_records,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "examples", "quarantine")
DQ = os.path.join(ROOT, "dq")

FAILURES = []


def check(cond, msg):
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILURES.append(msg)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# 独立参照实现：不调用 quarantine 模块的活性/理由逻辑，直接按题意重算

def independent_liveness(graph_obj, invalid_tris):
    """独立活性求值：source 命中失效三元组即死；all 任一分支死则死；any 全死才死。"""
    contribs = {c["id"]: c for c in graph_obj["contribs"]}
    alive = {}

    def eval_cid(cid):
        if cid in alive:
            return alive[cid]
        node = contribs[cid]
        if node["type"] == "source":
            tri = (node["source_id"], node["snapshot"], node["row_id"])
            alive[cid] = tri not in invalid_tris
        elif node["type"] == "all":
            alive[cid] = all(eval_cid(e["child"]) for e in node["edges"])
        else:
            alive[cid] = any(eval_cid(e["child"]) for e in node["edges"])
        return alive[cid]

    for cid in contribs:
        eval_cid(cid)
    return alive, contribs


def verify_withdrawal_credential(graph_obj, sources_obj, invalid_raw, wd, kept_alive):
    """只用原图 + 失效清单复查 withdrawals.json；返回 (ok, messages)。"""
    msgs = []
    contribs = {c["id"]: c for c in graph_obj["contribs"]}
    invalid_tris = {(x["source_id"], x["snapshot"], x["row_id"]) for x in invalid_raw}

    # 1. 绑定哈希
    b = wd["binding"]
    if b["origin_graph_sha256"] != sha256_of(graph_obj):
        msgs.append("origin_graph_sha256 与原图不符")
    if b["sources_sha256"] != sha256_of(sources_obj):
        msgs.append("sources_sha256 与源登记不符")
    if b["invalidation_sha256"] != sha256_of(sorted(invalid_tris)):
        msgs.append("invalidation_sha256 与失效清单（去重排序）不符")
    if wd["invalidation_entries_input"] != len(invalid_raw):
        msgs.append("invalidation_entries_input 未反映清单原始条目数（含重复）")
    if {(x["source_id"], x["snapshot"], x["row_id"]) for x in wd["invalidations"]} != invalid_tris:
        msgs.append("凭据内失效清单与输入去重结果不一致")

    reasons = {r["id"]: r for r in wd["reasons"]}

    def check_reason(rid, depth=0):
        if depth > 10000:
            msgs.append(f"理由链 {rid} 异常深（疑似环）")
            return False
        r = reasons.get(rid)
        if r is None:
            msgs.append(f"理由 {rid} 在 reasons 中缺失")
            return False
        node = contribs.get(r["contrib_id"])
        if node is None:
            msgs.append(f"理由 {rid} 引用原图不存在的贡献节点")
            return False
        if r["kind"] == "invalidated_source":
            tri = (r["source_id"], r["snapshot"], r["row_id"])
            if tri not in invalid_tris:
                msgs.append(f"理由 {rid}: 三元组 {tri} 不在失效清单中")
                return False
            if node["type"] != "source" or (node["source_id"], node["snapshot"],
                                            node["row_id"]) != tri:
                msgs.append(f"理由 {rid}: 与原图 source 原子内容不一致")
                return False
            return True
        if r["kind"] == "required_branch_failed":
            fe = r["failed_edge"]
            # 该边必须是原图中的真实边
            real = [e for e in node["edges"]
                    if e["child"] == fe["child"] and e["via_node"] == fe["via_node"]
                    and e["via_row"] == fe["via_row"]]
            if not real:
                msgs.append(f"理由 {rid}: failed_edge 不是原图中的真实边")
                return False
            return check_reason(fe["reason"], depth + 1)
        if r["kind"] == "all_alternatives_failed":
            # 必须逐条覆盖原图的每条替代边，且边信息完全一致
            real_edges = {(e["child"], e["via_node"], e["via_row"]) for e in node["edges"]}
            got_edges = {(e["child"], e["via_node"], e["via_row"]) for e in r["edges"]}
            if real_edges != got_edges or len(r["edges"]) != len(node["edges"]):
                msgs.append(f"理由 {rid}: 未逐条说明全部原替代边")
                return False
            return all(check_reason(e["reason"], depth + 1) for e in r["edges"])
        msgs.append(f"理由 {rid}: 未知 kind {r['kind']!r}")
        return False

    # 2. 每条撤下记录：原贡献根、理由链
    origin_outputs = {(o["table"], o["row"]): o["expr"] for o in graph_obj["outputs"]}
    withdrawn_idents = set()
    for w in wd["withdrawals"]:
        ident = (w["table"], w["row"])
        withdrawn_idents.add(ident)
        if origin_outputs.get(ident) != w["origin_root"]:
            msgs.append(f"撤下 {ident}: origin_root 与原图 outputs 不一致")
        if not check_reason(w["reason"]):
            msgs.append(f"撤下 {ident}: 理由链复查失败")

    # 3. 与独立活性结果逐条一致
    alive, _ = independent_liveness(graph_obj, invalid_tris)
    expect_withdrawn = {ident for ident, cid in origin_outputs.items() if not alive[cid]}
    if withdrawn_idents != expect_withdrawn:
        msgs.append(f"撤下集合 {sorted(withdrawn_idents)} 与独立活性求值 {sorted(expect_withdrawn)} 不一致")

    # 4. 理由节点最小且去重：每个失效贡献节点至多一个理由；
    #    all 只需指出一个失效必需分支即可充分解释，故理由 DAG 是失效闭包的子树
    #    （未选作解释的另一失效分支允许不出现在理由中）；any 必须覆盖全部替代边。
    cid_to_reasons = {}
    for r in wd["reasons"]:
        cid_to_reasons.setdefault(r["contrib_id"], []).append(r["id"])
    for cid, ids in cid_to_reasons.items():
        if len(set(ids)) != len(ids):
            msgs.append(f"贡献节点 {cid} 的理由 id 重复")
    # 从撤下根沿失效边求完整失效闭包（source 节点无边，加入后自然终止）
    reachable_dead = set()
    stack = [origin_outputs[i] for i in expect_withdrawn]
    while stack:
        x = stack.pop()
        if x in reachable_dead:
            continue
        reachable_dead.add(x)
        node = contribs[x]
        if node["type"] == "source":
            continue
        for e in node["edges"]:
            if not alive[e["child"]]:
                stack.append(e["child"])
    # 理由实际覆盖到的节点集合
    reason_cids = set(cid_to_reasons)
    # 4a. 理由只能解释失效闭包内的节点（不得附与撤下无关的节点）
    if not reason_cids <= reachable_dead:
        msgs.append(f"理由含失效闭包外的节点: {sorted(reason_cids - reachable_dead)[:3]}")
    # 4b. 每条撤下根都有理由；any 理由必须逐边覆盖全部原替代
    for w in wd["withdrawals"]:
        if w["origin_root"] not in reason_cids:
            msgs.append(f"撤下 {w['table']}/{w['row']} 的根没有理由节点")
    for r in wd["reasons"]:
        node = contribs[r["contrib_id"]]
        if r["kind"] == "all_alternatives_failed":
            if {e["child"] for e in r["edges"]} != {e["child"] for e in node["edges"]}:
                msgs.append(f"理由 {r['id']}: any 未覆盖全部替代边")
        elif r["kind"] == "required_branch_failed":
            fe = r["failed_edge"]
            if not any(e["child"] == fe["child"] and e["via_node"] == fe["via_node"]
                       and e["via_row"] == fe["via_row"] for e in node["edges"]):
                msgs.append(f"理由 {r['id']}: failed_edge 不是原图真实边")
    # 4c. 理由的 source 叶子必须全部是失效三元组
    for r in wd["reasons"]:
        if r["kind"] == "invalidated_source" \
                and (r["source_id"], r["snapshot"], r["row_id"]) not in invalid_tris:
            msgs.append(f"理由 {r['id']}: 叶子三元组不在失效清单中")
    return not msgs, msgs


# ---------------------------------------------------------------------------
# 1. 共用凭据小例

def test_shared_credential_fixture():
    print("== 1. 共用凭据小例：撤下/保留/共享理由/恰好全部健全推导 ==")
    sources_obj = load_json(os.path.join(EX, "sources.json"))
    graph_obj = load_json(os.path.join(EX, "graph.json"))
    records = load_jsonl(os.path.join(EX, "records.jsonl"))
    invalid_raw = load_json(os.path.join(EX, "invalidations.json"))
    invalid_tris = {(x["source_id"], x["snapshot"], x["row_id"]) for x in invalid_raw}

    sources, errs = validate_sources(sources_obj)
    check(not errs, f"fixture sources 合法 {errs}")
    graph, errs, warns = validate_graph(graph_obj, sources)
    check(not errs and not warns, f"fixture graph 零 error 零 warning（{errs}, {warns}）")

    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "q")
        out2, summary = quarantine(sources_obj, graph_obj,
                                   os.path.join(EX, "records.jsonl"),
                                   invalid_raw, out)
        st = summary["stats"]
        check(st["records_total"] == 7 and st["records_kept"] == 5
              and st["records_withdrawn"] == 2,
              f"7 条记录：保留 5、撤下 2（实际 {st}）")
        check(st["invalidations_distinct"] == 2, "重复失效项去重为 2 条")
        wd = load_json(os.path.join(out, "withdrawals.json"))
        check(wd["invalidation_entries_input"] == 3, "原始清单 3 条（含 1 条重复）被记录")
        check(st["invalidations_unused"] == 1, "已登记未参与交付的失效行计入 unused 且不影响结果")

        rows = load_jsonl(os.path.join(out, "rows.jsonl"))
        kept_idents = [(r["table"], r["row"]) for r in rows]
        check(kept_idents == [("R", "R2"), ("R", "R3"), ("R", "R5"), ("Q", "Q1"), ("Q", "Q2")],
              "rows.jsonl 保持原输入顺序且只含存活记录")
        # 其他快照的同名 row_id 不误伤：R5 用 S/s2/s-r1，失效的是 S/s1/s-r1
        r5 = next(r for r in rows if (r["table"], r["row"]) == ("R", "R5"))
        check(r5["data"]["invoice"] == "INV-R5",
              "其他快照（s2）的同名原行 s-r1 不被 s1 的失效误伤，R5 原样保留")
        origin_by_ident = {(r["table"], r["row"]): r for r in records}
        for r in rows:
            ident = (r["table"], r["row"])
            check(r["data"] == origin_by_ident[ident]["data"],
                  f"保留记录 {ident} 的 data 原样未改")

        new_graph = load_json(os.path.join(out, "graph.json"))

        # 隔离图与原图可区分
        check(new_graph.get("quarantine", {}).get("origin_graph_sha256") == sha256_of(graph_obj),
              "新图带 quarantine 标记并绑定原图哈希（与原图可区分）")
        check(new_graph["graph_id"] != graph_obj["graph_id"], "graph_id 与原图不同")

        # 表定义保留、撤下行移除
        tables = {t["table_id"]: t["rows"] for t in new_graph["tables"]}
        check(set(tables) == {"R", "Q"}, "表定义全部保留")
        check(tables["R"] == ["R2", "R3", "R5"] and tables["Q"] == ["Q1", "Q2"],
              "撤下记录从表行中移除，保留行 id 不变")
        new_outputs = {(o["table"], o["row"]): o["expr"] for o in new_graph["outputs"]}
        check(set(new_outputs) == set(kept_idents), "图 outputs 与 rows.jsonl 身份一致")
        for ident, cid in new_outputs.items():
            check(cid == graph.outputs[ident], f"保留记录 {ident} 仍用原贡献根 {cid}")

        # 恰好全部健全推导：与独立活性求值逐一对照节点/边
        alive, contribs = independent_liveness(graph_obj, invalid_tris)
        kept_cids = {c["id"] for c in new_graph["contribs"]}
        expect_kept = set()
        stack = [graph.outputs[i] for i in new_outputs]
        while stack:
            x = stack.pop()
            if x in expect_kept:
                continue
            expect_kept.add(x)
            stack.extend(e["child"] for e in contribs[x].get("edges", [])
                         if alive[e["child"]])
        check(kept_cids == expect_kept, "隔离图贡献节点恰为从存活根可达的全部健全节点")
        # 边：保留节点的每条存活边必须原样存在（via_node/via_row 不重写）
        new_by_id = {c["id"]: c for c in new_graph["contribs"]}
        edge_ok = True
        for cid in expect_kept:
            if contribs[cid]["type"] == "source":
                continue
            old_edges = {(e["child"], e["via_node"], e["via_row"])
                         for e in contribs[cid]["edges"] if alive[e["child"]]}
            new_edges = {(e["child"], e["via_node"], e["via_row"])
                         for e in new_by_id[cid]["edges"]}
            if old_edges != new_edges:
                edge_ok = False
        check(edge_ok, "存活边的加工步骤与 via_row 原样保留，失效边只剪除不重写")
        # 不可达节点不存在
        reachable = set()
        stack = list(new_outputs.values())
        while stack:
            x = stack.pop()
            if x in reachable:
                continue
            reachable.add(x)
            stack.extend(e["child"] for e in new_by_id[x].get("edges", []))
        check(reachable == kept_cids, "结果中不留从保留记录到不了的贡献节点")

        # cR2 保留全部 3 条替代；cAnyQ 只剩 1 条且未被接到上层
        cR2 = new_by_id["cR2"]
        check(cR2["type"] == "any" and len(cR2["edges"]) == 3,
              "R2 的三条健全替代全部保留（不是每行只挑一条路径）")
        check({e["via_node"] for e in cR2["edges"]} == {"dedR"},
              "R2 替代边仍挂在原 deduplicate 节点 dedR 上")
        cAnyQ = new_by_id["cAnyQ"]
        check(cAnyQ["type"] == "any" and len(cAnyQ["edges"]) == 1
              and cAnyQ["edges"][0]["child"] == "cD1"
              and cAnyQ["edges"][0]["via_node"] == "dedA",
              "Q2 只剩一条替代时 any 节点保留，未直接接到更上层，via_row 原样")

        # 撤下凭据独立复查
        ok, msgs = verify_withdrawal_credential(graph_obj, sources_obj, invalid_raw, wd, alive)
        check(ok, f"撤下凭据可用原图+清单独立复查：{msgs}")
        # 共享失效原子 cS1 被 R1/R4 两条撤下引用，但 source 理由只有一份
        src_reasons = [r for r in wd["reasons"] if r["kind"] == "invalidated_source"]
        check(len(src_reasons) == 1 and src_reasons[0]["contrib_id"] == "cS1",
              "共享失效节点的证据只存一份（不逐行复制证据树）")
        withdrawn_rows = {(w["table"], w["row"]) for w in wd["withdrawals"]}
        check(withdrawn_rows == {("R", "R1"), ("R", "R4")}, "撤下清单为 R1、R4")
        check(st["reason_nodes"] == len(wd["reasons"]) == 6,
              "理由节点数 6（两条撤下链共享 1 个 source 理由），不含与撤下无关的节点")

        # 隔离图通过原 validate（含单条边 any 的隔离图规则）
        g2, gerrs, gwarns = validate_graph(new_graph, sources)
        check(not gerrs and not gwarns, f"隔离图通过 validate_graph（{gerrs}, {gwarns}）")


def test_plan_branch_selection():
    print("== 2. 给定请求下只有一条替代可用于 plan；换请求可改选 ==")
    sources_obj = load_json(os.path.join(EX, "sources.json"))
    graph_obj = load_json(os.path.join(EX, "graph.json"))
    invalid_raw = load_json(os.path.join(EX, "invalidations.json"))
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "q")
        quarantine(sources_obj, graph_obj, os.path.join(EX, "records.jsonl"),
                   invalid_raw, out)
        env = dict(os.environ, PYTHONPATH=ROOT)

        def run_plan(req_name):
            rp = subprocess.run(
                [sys.executable, "-P", "-m", "delivqual", "plan",
                 "--sources", os.path.join(EX, "sources.json"),
                 "--graph", os.path.join(out, "graph.json"),
                 "--request", os.path.join(EX, req_name)],
                cwd=ROOT, env=env, capture_output=True, text=True)
            return json.loads(rp.stdout)

        pa = run_plan("req_a.json")
        check(pa["status"] == "feasible", "dept-a 请求：可行")
        r2a = next(r for r in pa["rows"] if (r["table"], r["row"]) == ("R", "R2"))
        root_edges = [e for e in r2a["derivation"]["edges"] if e["parent"] == "cR2"]
        check(len(root_edges) == 1 and root_edges[0]["child"] == "cAlt1"
              and root_edges[0]["via_node"] == "dedR",
              "dept-a 下 R2 只能选用第一条替代 cAlt1（join/aggregate 两个选项的受众被排除）")
        used_sources = {o["source_id"] for o in pa["options"]}
        check("J" not in used_sources and "W" not in used_sources,
              "dept-a 计划不选用仅 dept-b 可用的替代来源 J、W")
        # 全部替代仍在图中（plan 只选一条，图不丢其余健全路径）
        ng = load_json(os.path.join(out, "graph.json"))
        check(len(next(c for c in ng["contribs"] if c["id"] == "cR2")["edges"]) == 3,
              "plan 选路后图里仍保留 R2 的全部三条健全推导")

        pb = run_plan("req_b.json")
        check(pb["status"] == "feasible", "dept-b 请求：可行")
        r2b = next(r for r in pb["rows"] if (r["table"], r["row"]) == ("R", "R2"))
        root_edges_b = [e for e in r2b["derivation"]["edges"] if e["parent"] == "cR2"]
        # dept-b 下三条替代都合法；求解器按内部启发式任选其一（不保证具体哪条），
        # 故断言“选中的是原 any 的真实边且仍挂 dedR”，而不是绑定某一条分支。
        allowed = {("cAlt1", "dedR"), ("cAlt2", "dedR"), ("cAlt3", "dedR")}
        picked = (root_edges_b[0]["child"], root_edges_b[0]["via_node"])
        check(len(root_edges_b) == 1 and picked in allowed,
              f"dept-b 下 R2 可在健全替代中改选（实际 {picked}，另一条路径以后仍可用）")
        # 而 dept-a 唯一合法的 cAlt1 在 dept-b 也必须是合法候选（用暴力枚举独立确认
        # 两个请求各自的可行替代集合，不依赖 SAT 的选路 tie-break）
        from delivqual.brute import brute_feasible
        from delivqual.model import validate_request, validate_graph, validate_sources
        src = validate_sources(load_json(os.path.join(EX, "sources.json")))[0]
        qg = load_json(os.path.join(out, "graph.json"))
        g2 = validate_graph(qg, src)[0]
        feasible_children = {}
        for req_name in ("req_a.json", "req_b.json"):
            req = validate_request(load_json(os.path.join(EX, req_name)), g2)[0]
            # 逐替代单独构造请求行子实例，确认每条替代在该请求下是否可行
            cR2_node = next(c for c in qg["contribs"] if c["id"] == "cR2")
            kids = [e["child"] for e in cR2_node["edges"]]
            ok_set = set()
            for kid in kids:
                probe = json.loads(json.dumps(qg))
                for c in probe["contribs"]:
                    if c["id"] == "cR2":
                        c["edges"] = [e for e in c["edges"] if e["child"] == kid]
                pg = validate_graph(probe, src)[0]
                st, _ = brute_feasible(src, pg, req, max_combos=10000)
                if st == "feasible":
                    ok_set.add(kid)
            feasible_children[req_name] = ok_set
        check(feasible_children["req_a.json"] == {"cAlt1"},
              f"dept-a 下只有 cAlt1 一条替代可用于 plan（暴力枚举 {feasible_children['req_a.json']}）")
        # cAlt2 是 join 共同贡献，但其右支来源 T 的唯一选项不许可 join ⇒ 结构上恒不可行；
        # dept-b 放开受众后 cAlt1（project）与 cAlt3（aggregate）两条都可行。
        check(feasible_children["req_b.json"] == {"cAlt1", "cAlt3"},
              f"dept-b 下 cAlt1/cAlt3 两条替代可用、join 支因选项缺 join 权限恒不可行"
              f"（暴力枚举 {feasible_children['req_b.json']}），图保留全部路径供以后资格核验选择")

        # verify-plan 端到端
        for req_name, plan in (("req_a.json", pa), ("req_b.json", pb)):
            plan_path = os.path.join(td, f"plan-{req_name}.json")
            with open(plan_path, "w", encoding="utf-8") as f:
                json.dump(plan, f)
            rv = subprocess.run(
                [sys.executable, "-P", "-m", "delivqual", "verify-plan",
                 "--sources", os.path.join(EX, "sources.json"),
                 "--graph", os.path.join(out, "graph.json"),
                 "--request", os.path.join(EX, req_name),
                 "--plan", plan_path],
                cwd=ROOT, env=env, capture_output=True, text=True)
            check(json.loads(rv.stdout)["verdict"] == "valid",
                  f"{req_name} 的 plan 通过独立 verify-plan")


# ---------------------------------------------------------------------------
# 3. 同名行/其他快照不误伤；空清单；全撤下

def test_snapshot_and_row_scoping():
    print("== 3. 失效定位按三元组：同名行/其他快照不误伤，空清单空凭据 ==")
    sources_obj = load_json(os.path.join(EX, "sources.json"))
    graph_obj = load_json(os.path.join(EX, "graph.json"))
    sources, _ = validate_sources(sources_obj)
    graph, _, _ = validate_graph(graph_obj, sources)

    # 3.1 其他快照的同名 row_id：清单里放一个不存在的快照组合 ⇒ 输入错误
    bad = [{"source_id": "S", "snapshot": "other-snapshot", "row_id": "s-r1"}]
    try:
        parse_invalidations(bad, sources)
        check(False, "未登记快照引用应报输入错误")
    except QuarantineError:
        check(True, "未登记快照引用是输入错误")
    # 未登记 row_id 同样拒绝
    try:
        parse_invalidations([{"source_id": "S", "snapshot": "s1", "row_id": "ghost"}], sources)
        check(False, "未登记原行引用应报输入错误")
    except QuarantineError:
        check(True, "未登记原行引用是输入错误")

    with tempfile.TemporaryDirectory() as td:
        # 3.2 空失效清单：全部保留，空凭据
        out0 = os.path.join(td, "q0")
        _, s0 = quarantine(sources_obj, graph_obj, os.path.join(EX, "records.jsonl"), [], out0)
        check(s0["stats"]["records_withdrawn"] == 0 and s0["stats"]["records_kept"] == 7,
              "空失效清单：无撤下")
        wd0 = load_json(os.path.join(out0, "withdrawals.json"))
        check(wd0["withdrawals"] == [] and wd0["reasons"] == []
              and wd0["binding"]["invalidation_sha256"] == sha256_of([]),
              "无撤下记录时仍交付空凭据（哈希绑定空清单）")
        rows0 = load_jsonl(os.path.join(out0, "rows.jsonl"))
        check(len(rows0) == 7, "空清单时 rows.jsonl 含全部 7 条记录")

        # 3.3 让 R2 的三条替代各自的必需原子全部失效（不同源），R1/R4 不受影响
        r2_inv = [
            {"source_id": "S", "snapshot": "s1", "row_id": "s-r2"},   # alt1
            {"source_id": "J", "snapshot": "j1", "row_id": "j-r1"},   # alt2 左
            {"source_id": "T", "snapshot": "t1", "row_id": "t-r1"},   # alt2 右
            {"source_id": "W", "snapshot": "w1", "row_id": "w-r1"},   # alt3
        ]
        out1 = os.path.join(td, "q1")
        _, s1 = quarantine(sources_obj, graph_obj, os.path.join(EX, "records.jsonl"),
                           r2_inv, out1)
        wd1 = load_json(os.path.join(out1, "withdrawals.json"))
        check({(w["table"], w["row"]) for w in wd1["withdrawals"]} == {("R", "R2")},
              "R2 全部替代失效才撤下；R1/R4 用 s-r1、R3/Q 用其他源，均不误伤")
        # R2 的理由必须是 all_alternatives_failed，逐条覆盖 3 条原替代边
        r2_reason = next(r for r in wd1["withdrawals"] if (r["table"], r["row"]) == ("R", "R2"))
        reason_objs = {r["id"]: r for r in wd1["reasons"]}
        top = reason_objs[r2_reason["reason"]]
        check(top["kind"] == "all_alternatives_failed" and len(top["edges"]) == 3,
              "充分替代必须逐条说明每条原替代边为何失效（3 条边全覆盖）")
        check(all(e["reason"] in reason_objs for e in top["edges"]),
              "每条替代边的理由都能在 reasons 中查到")
        # 独立复查该凭据
        ok, msgs = verify_withdrawal_credential(
            graph_obj, sources_obj, r2_inv, wd1,
            independent_liveness(graph_obj, {(x["source_id"], x["snapshot"], x["row_id"])
                                             for x in r2_inv})[0])
        check(ok, f"R2 全替代失效凭据独立复查通过：{msgs[:2]}")

        # 3.4 全部撤下：表定义仍在，rows.jsonl 为空，validate 通过
        all_inv = [
            {"source_id": "S", "snapshot": "s1", "row_id": "s-r1"},
            {"source_id": "S", "snapshot": "s1", "row_id": "s-r2"},
            {"source_id": "S", "snapshot": "s2", "row_id": "s-r1"},
            {"source_id": "T", "snapshot": "t1", "row_id": "t-r1"},
            {"source_id": "U", "snapshot": "u1", "row_id": "u-r1"},
            {"source_id": "U", "snapshot": "u1", "row_id": "u-r2"},
            {"source_id": "U", "snapshot": "u1", "row_id": "u-r3"},
            {"source_id": "V", "snapshot": "v1", "row_id": "v-r1"},
            {"source_id": "J", "snapshot": "j1", "row_id": "j-r1"},
            {"source_id": "W", "snapshot": "w1", "row_id": "w-r1"},
        ]
        out2 = os.path.join(td, "q2")
        _, s2 = quarantine(sources_obj, graph_obj, os.path.join(EX, "records.jsonl"),
                           all_inv, out2)
        check(s2["stats"]["records_kept"] == 0 and s2["stats"]["records_withdrawn"] == 7,
              "全部失效：7 条全撤")
        ng2 = load_json(os.path.join(out2, "graph.json"))
        check({t["table_id"] for t in ng2["tables"]} == {"R", "Q"}
              and all(t["rows"] == [] for t in ng2["tables"]),
              "全部撤下后表定义仍存在且行清单为空")
        check(ng2["contribs"] == [] and ng2["outputs"] == [],
              "全部撤下后无贡献节点、无 outputs")
        check(open(os.path.join(out2, "rows.jsonl"), encoding="utf-8").read() == "",
              "rows.jsonl 为空文件")
        g2, gerrs, gwarns = validate_graph(ng2, sources)
        check(not gerrs and not gwarns, f"全撤图通过 validate（{gerrs}, {gwarns}）")


# ---------------------------------------------------------------------------
# 4. 运行时层层共享贡献 + 多条输出：线性规模 + 共享理由不复制

def build_scale_case(n_out, seed=11):
    """构造层层共享的 DAG：死链金字塔（共享失效节点）+ 一个 any 死支 + 大量存活输出。"""
    rng = __import__("random").Random(seed)
    sources_doc = {
        "kind": "sources", "version": 1,
        "sources": [
            {"source_id": "D", "snapshot": "d1", "rows": [f"d-r{i}" for i in range(4)],
             "options": [{"option_id": "O", "ops": ["raw", "aggregate", "join", "union", "project"],
                          "audiences": ["a"], "geos": ["g"], "time": [0, 100],
                          "notices": ["n-D"], "classifications": ["report"], "mutex": []}]},
            {"source_id": "L", "snapshot": "l1", "rows": [f"l-r{i}" for i in range(n_out + 2)],
             "options": [{"option_id": "O", "ops": ["raw", "aggregate", "join", "union", "project"],
                          "audiences": ["a"], "geos": ["g"], "time": [0, 100],
                          "notices": ["n-L"], "classifications": ["report"], "mutex": []}]},
        ],
    }
    pnodes = [
        {"node_id": "scanD", "op": "scan", "inputs": []},
        {"node_id": "scanL", "op": "scan", "inputs": []},
        {"node_id": "aggD", "op": "aggregate", "inputs": ["scanD"]},
        {"node_id": "uni", "op": "union_all", "inputs": ["aggD", "scanL"]},
        {"node_id": "aggT", "op": "aggregate", "inputs": ["uni"]},
        {"node_id": "ded", "op": "deduplicate", "inputs": ["aggT"]},
    ]
    contribs = []
    # 4 个失效原子 + 两层 all 金字塔，全部输出共享同一条死链
    for i in range(4):
        contribs.append({"id": f"d-a{i}", "type": "source",
                         "source_id": "D", "snapshot": "d1", "row_id": f"d-r{i}"})
    layer = [f"d-a{i}" for i in range(4)]
    depth = 0
    while len(layer) > 1:
        nxt = []
        for i in range(0, len(layer) - 1, 2):
            cid = f"d-all-{depth}-{i}"
            contribs.append({"id": cid, "type": "all", "edges": [
                {"child": layer[i], "via_node": "aggD", "via_row": f"dead-{depth}-{i}#L"},
                {"child": layer[i + 1], "via_node": "aggD", "via_row": f"dead-{depth}-{i}#R"},
            ]})
            nxt.append(cid)
        if len(layer) % 2 == 1:
            nxt.append(layer[-1])
        layer = nxt
        depth += 1
    dead_root = layer[0]

    outputs = []
    tables = [
        {"table_id": "T", "node_id": "ded", "rows": []},
        # 活支镜像表：让每个活原子都从某输出可达（无冗余警告），与撤下表共享同一加工图
        {"table_id": "L", "node_id": "uni", "rows": []},
    ]
    # 模式 0（撤下）：输出根直接挂共享死链 ⇒ 所有撤下记录共用同一个根理由 id，
    #   理由 DAG 与输出数完全无关；其活支经镜像表 L 仍可达；
    # 模式 1（any 保留）：一支共享死链、一支本行活支 ⇒ any 剪为单边；
    # 模式 2（独立存活）：本行活支。
    for k in range(n_out):
        row = f"r{k:06d}"
        tables[0]["rows"].append(row)
        lat = f"l-a{k}"
        contribs.append({"id": lat, "type": "source",
                         "source_id": "L", "snapshot": "l1", "row_id": f"l-r{k}"})
        lall = f"l-all{k}"
        contribs.append({"id": lall, "type": "all", "edges": [
            {"child": lat, "via_node": "scanL", "via_row": f"live-{k}"}]})
        # 镜像表行：同一活支的独立输出根（保证 l-all{k} 总可达）
        lrow = f"l{k:06d}"
        tables[1]["rows"].append(lrow)
        lroot = f"out-mirror{k}"
        contribs.append({"id": lroot, "type": "all", "edges": [
            {"child": lall, "via_node": "uni", "via_row": f"m-{k}"}]})
        outputs.append({"table": "L", "row": lrow, "expr": lroot})
        mode = k % 3
        if mode == 0:
            # 直接挂共享死链根：撤下且根理由在所有同模输出间逐字共享
            outputs.append({"table": "T", "row": row, "expr": dead_root})
        elif mode == 1:
            root = f"out-any{k}"
            contribs.append({"id": root, "type": "any", "edges": [
                {"child": dead_root, "via_node": "ded", "via_row": f"any-dead-{k}"},
                {"child": lall, "via_node": "ded", "via_row": f"any-live-{k}"},
            ]})
            outputs.append({"table": "T", "row": row, "expr": root})
        else:
            root = f"out-live{k}"
            contribs.append({"id": root, "type": "all", "edges": [
                {"child": lall, "via_node": "uni", "via_row": f"u-live-{k}"}]})
            outputs.append({"table": "T", "row": row, "expr": root})

    graph_obj = {"kind": "graph", "version": 1, "graph_id": f"scale-{n_out}",
                 "nodes": pnodes, "tables": tables, "contribs": contribs, "outputs": outputs}
    invalid = [{"source_id": "D", "snapshot": "d1", "row_id": f"d-r{i}"} for i in range(4)]
    return sources_doc, graph_obj, invalid


def test_scale_and_sharing():
    print("== 4. 运行时层层共享 + 多输出：线性规模、共享理由不复制、不枚举推导 ==")
    sizes = [300, 900, 2400]
    prev = None
    for n_out in sizes:
        sources_obj, graph_obj, invalid = build_scale_case(n_out)
        sources, errs = validate_sources(sources_obj)
        check(not errs, f"n={n_out} sources 合法")
        graph, errs, warns = validate_graph(graph_obj, sources)
        check(not errs and not warns, f"n={n_out} graph 零 error 零 warning")
        with tempfile.TemporaryDirectory() as td:
            rec_path = os.path.join(td, "records.jsonl")
            with open(rec_path, "w", encoding="utf-8") as f:
                for o in graph_obj["outputs"]:
                    f.write(json.dumps({"table": o["table"], "row": o["row"],
                                        "data": {"k": o["row"], "v": n_out}},
                                       ensure_ascii=False) + "\n")
            out = os.path.join(td, "q")
            _, summary = quarantine(sources_obj, graph_obj, rec_path, invalid, out)
            st = summary["stats"]
            n_dead = sum(1 for k in range(n_out) if k % 3 == 0)
            n_any = sum(1 for k in range(n_out) if k % 3 == 1)
            n_live = sum(1 for k in range(n_out) if k % 3 == 2)
            n_mirror = n_out  # 镜像表 L 的行全部存活
            check(st["records_total"] == 2 * n_out
                  and st["records_withdrawn"] == n_dead
                  and st["records_kept"] == n_any + n_live + n_mirror,
                  f"n={n_out}: 总 {2 * n_out} 条，撤下 {n_dead}（死链共享），"
                  f"保留 {n_any + n_live + n_mirror}")
            wd = load_json(os.path.join(out, "withdrawals.json"))
            # 共享死链：理由节点数是金字塔大小（与 4 个原子同阶），不随撤下输出增长
            check(wd["stats"]["reason_nodes"] <= 20,
                  f"n={n_out}: 理由节点 {wd['stats']['reason_nodes']} 个，与输出数 {n_dead} 无关"
                  "（共享失效子图只存一份）")
            # 每条撤下的根理由必须指向同一个共享死链理由
            root_reasons = {w["reason"] for w in wd["withdrawals"]}
            check(len(root_reasons) == 1,
                  f"n={n_out}: {n_dead} 条撤下记录共享同一个根理由 id")
            # 独立复查凭据
            alive, _ = independent_liveness(graph_obj, {(x["source_id"], x["snapshot"], x["row_id"])
                                                        for x in invalid})
            ok, msgs = verify_withdrawal_credential(
                graph_obj, sources_obj, invalid, wd, alive)
            check(ok, f"n={n_out} 撤下凭据独立复查通过：{msgs[:2]}")
            # 副本规模随图节点线性（界：原图节点 + 每条输出 2 个固定节点）
            ng = load_json(os.path.join(out, "graph.json"))
            n_kept_nodes = len(ng["contribs"])
            check(n_kept_nodes <= st["origin_contrib_nodes"],
                  f"n={n_out}: 隔离图节点不超过原图")
            check(st["kept_contrib_edges"] <= st["origin_contrib_edges"],
                  f"n={n_out}: 隔离图边数不超过原图")
            # any 输出全部保留且被剪为单条边，仍挂 ded
            kept_any = [c for c in ng["contribs"] if c["type"] == "any"]
            check(len(kept_any) == n_any and all(len(c["edges"]) == 1
                  and all(e["via_node"] == "ded" for e in c["edges"]) for c in kept_any),
                  f"n={n_out}: {n_any} 个 any 输出保留为单边且 via_node 原样")
            # rows 内容逐行核对
            rows = load_jsonl(os.path.join(out, "rows.jsonl"))
            check(len(rows) == n_any + n_live + n_mirror
                  and all(r["data"]["v"] == n_out for r in rows),
                  f"n={n_out}: rows.jsonl 行数与载荷正确")
            # 撤下只发生在 T 表；镜像表 L 全保留，表定义都在
            kept_tables = {t["table_id"]: t["rows"] for t in ng["tables"]}
            check(len(kept_tables["L"]) == n_mirror
                  and len(kept_tables["T"]) == n_any + n_live,
                  f"n={n_out}: 镜像表 L 全保留，T 表仅撤死链行")
            # validate 隔离图
            _g2, gerrs, gwarns = validate_graph(ng, sources)
            check(not gerrs and not gwarns, f"n={n_out} 隔离图 validate 通过")
            if prev is not None:
                ratio_out = n_out / prev[0]
                ratio_node = n_kept_nodes / prev[1]
                check(ratio_node <= ratio_out * 1.6,
                      f"n={n_out}: 输出放大 {ratio_out:.1f}x，节点仅 {ratio_node:.1f}x（线性）")
            prev = (n_out, n_kept_nodes)


# ---------------------------------------------------------------------------
# 5. 流式大载荷：内存不随载荷总量线性增长

def test_streaming_memory():
    print("== 5. 流式：大载荷逐行复制，峰值内存不随载荷总量累积 ==")
    sources_obj, graph_obj, invalid = build_scale_case(400, seed=23)
    sources, _ = validate_sources(sources_obj)
    with tempfile.TemporaryDirectory() as td:
        rec_path = os.path.join(td, "records.jsonl")
        payload = "x" * 40000  # 每行约 40KB，400 行 ≈ 16MB 载荷
        with open(rec_path, "w", encoding="utf-8") as f:
            for o in graph_obj["outputs"]:
                f.write(json.dumps({"table": o["table"], "row": o["row"],
                                    "data": {"blob": payload, "row": o["row"]}}) + "\n")
        size_mb = os.path.getsize(rec_path) / (1 << 20)
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        out = os.path.join(td, "q")
        _, summary = quarantine(sources_obj, graph_obj, rec_path, invalid, out)
        st = summary["stats"]
        after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss 单位 KB（Linux）
        growth_mb = max(0, (after - before)) / 1024
        print(f"    输入 {size_mb:.1f}MB，RSS 峰值增长 {growth_mb:.2f}MB")
        check(growth_mb < size_mb * 0.5,
              f"内存增长 {growth_mb:.2f}MB 远小于载荷 {size_mb:.1f}MB（载荷未在内存累积）")
        # 输出载荷逐字节等价（用解析后比较，避免序列化空白差异）
        in_rows = {(r["table"], r["row"]): r for r in load_jsonl(rec_path)}
        out_rows = {(r["table"], r["row"]): r for r in load_jsonl(os.path.join(out, "rows.jsonl"))}
        withdrawn_T = {("T", f"r{k:06d}") for k in range(400) if k % 3 == 0}
        check(set(out_rows) == set(in_rows) - withdrawn_T,
              "流式复制的存活行身份正确（T 表死链行撤下，镜像 L 表全保留）")
        check(all(out_rows[k]["data"] == in_rows[k]["data"] for k in out_rows),
              "大载荷 data 逐行原样复制")
        check(st["records_kept"] == len(out_rows), "stats 与实际复制行数一致")


# ---------------------------------------------------------------------------
# 6. 原子交付与坏输入：最后一条记录出错不发布；退出码；原文件不变

def test_atomic_delivery_and_errors():
    print("== 6. 原子交付：最后一条出错目录不发布；坏输入退出码；原文件不改 ==")
    sources_obj = load_json(os.path.join(EX, "sources.json"))
    graph_obj = load_json(os.path.join(EX, "graph.json"))
    invalid_raw = load_json(os.path.join(EX, "invalidations.json"))
    env = dict(os.environ, PYTHONPATH=ROOT)
    origin_hashes = {
        "sources": sha256_file(os.path.join(EX, "sources.json")),
        "graph": sha256_file(os.path.join(EX, "graph.json")),
        "records": sha256_file(os.path.join(EX, "records.jsonl")),
        "invalid": sha256_file(os.path.join(EX, "invalidations.json")),
    }

    def cli(args):
        return subprocess.run([sys.executable, "-P", "-m", "delivqual", "quarantine"] + args,
                              cwd=ROOT, env=env, capture_output=True, text=True)

    with tempfile.TemporaryDirectory() as td:
        inv_path = os.path.join(td, "inv.json")
        with open(inv_path, "w", encoding="utf-8") as f:
            json.dump(invalid_raw, f)

        # 6.1 最后一条记录身份重复 ⇒ 退出 10，目录从未出现
        rec_dup = os.path.join(td, "dup.jsonl")
        lines = load_jsonl(os.path.join(EX, "records.jsonl"))
        with open(rec_dup, "w", encoding="utf-8") as f:
            for r in lines[:-1]:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.write(json.dumps(lines[0], ensure_ascii=False) + "\n")  # 末行重复 R1
        out_dup = os.path.join(td, "never-dup")
        rv = cli(["--sources", os.path.join(EX, "sources.json"),
                  "--graph", os.path.join(EX, "graph.json"),
                  "--records", rec_dup, "--invalidations", inv_path,
                  "--out-dir", out_dup])
        check(rv.returncode == 10 and not os.path.exists(out_dup),
              "最后一条记录重复：退出 10 且目录未发布")
        check('"valid": false' in rv.stdout, "失败输出为结构化错误")

        # 6.2 最后一条记录是图中不存在的身份 ⇒ 退出 10，目录未出现
        rec_bad = os.path.join(td, "badident.jsonl")
        with open(rec_bad, "w", encoding="utf-8") as f:
            for r in lines[:-1]:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.write(json.dumps({"table": "R", "row": "GHOST", "data": {}}, ensure_ascii=False) + "\n")
        out_bad = os.path.join(td, "never-bad")
        rv = cli(["--sources", os.path.join(EX, "sources.json"),
                  "--graph", os.path.join(EX, "graph.json"),
                  "--records", rec_bad, "--invalidations", inv_path,
                  "--out-dir", out_bad])
        check(rv.returncode == 10 and not os.path.exists(out_bad),
              "最后一条记录身份无效：退出 10 且目录未发布")

        # 6.3 缺一条记录（一一对应不满足）⇒ 退出 10
        rec_miss = os.path.join(td, "miss.jsonl")
        with open(rec_miss, "w", encoding="utf-8") as f:
            for r in lines[:-1]:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        out_miss = os.path.join(td, "never-miss")
        rv = cli(["--sources", os.path.join(EX, "sources.json"),
                  "--graph", os.path.join(EX, "graph.json"),
                  "--records", rec_miss, "--invalidations", inv_path,
                  "--out-dir", out_miss])
        check(rv.returncode == 10 and not os.path.exists(out_miss),
              "记录缺行：退出 10 且目录未发布")

        # 6.4 坏 JSON / 空行 / 字段不符 / table 非字符串
        for name, content in (
            ("badjson", '{"table": "R", "row": "R1", "data": {}}\n{not json}\n'),
            ("blank", '{"table": "R", "row": "R1", "data": {}}\n\n'),
            ("fields", '{"table": "R", "row": "R1", "data": {}, "extra": 1}\n'),
            ("nonstr", '{"table": 7, "row": "R1", "data": {}}\n'),
        ):
            p = os.path.join(td, f"{name}.jsonl")
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            rv = cli(["--sources", os.path.join(EX, "sources.json"),
                      "--graph", os.path.join(EX, "graph.json"),
                      "--records", p, "--invalidations", inv_path,
                      "--out-dir", os.path.join(td, f"never-{name}")])
            check(rv.returncode == 10 and not os.path.exists(os.path.join(td, f"never-{name}")),
                  f"坏记录 {name}：退出 10 且无目录")

        # 6.5 失效清单引用未登记原行 ⇒ 退出 10
        bad_inv = os.path.join(td, "bad-inv.json")
        with open(bad_inv, "w", encoding="utf-8") as f:
            json.dump([{"source_id": "S", "snapshot": "s1", "row_id": "nope"}], f)
        out_bi = os.path.join(td, "never-bi")
        rv = cli(["--sources", os.path.join(EX, "sources.json"),
                  "--graph", os.path.join(EX, "graph.json"),
                  "--records", os.path.join(EX, "records.jsonl"),
                  "--invalidations", bad_inv, "--out-dir", out_bi])
        check(rv.returncode == 10 and not os.path.exists(out_bi),
              "未登记失效引用：退出 10 且无目录")

        # 6.6 目标已存在 ⇒ 拒绝且不改写
        out_ok = os.path.join(td, "ok")
        rv1 = cli(["--sources", os.path.join(EX, "sources.json"),
                   "--graph", os.path.join(EX, "graph.json"),
                   "--records", os.path.join(EX, "records.jsonl"),
                   "--invalidations", inv_path, "--out-dir", out_ok])
        check(rv1.returncode == 0 and sorted(os.listdir(out_ok)) ==
              ["graph.json", "rows.jsonl", "withdrawals.json"],
              "成功发布恰好三份文件")
        marker = open(os.path.join(out_ok, "rows.jsonl"), encoding="utf-8").read()
        rv2 = cli(["--sources", os.path.join(EX, "sources.json"),
                   "--graph", os.path.join(EX, "graph.json"),
                   "--records", os.path.join(EX, "records.jsonl"),
                   "--invalidations", inv_path, "--out-dir", out_ok])
        check(rv2.returncode == 10, "目标已存在：退出 10")
        check(open(os.path.join(out_ok, "rows.jsonl"), encoding="utf-8").read() == marker,
              "拒绝时不改写已有目录")

        # 6.7 输入文件不存在 ⇒ 退出 2
        rv = cli(["--sources", os.path.join(td, "missing.json"),
                  "--graph", os.path.join(EX, "graph.json"),
                  "--records", os.path.join(EX, "records.jsonl"),
                  "--invalidations", inv_path, "--out-dir", os.path.join(td, "x")])
        check(rv.returncode == 2, "输入文件不可读：退出 2")

        # 6.8 原图本身有行缺贡献根 ⇒ 拒绝
        broken = json.loads(json.dumps(graph_obj))
        broken["tables"].append({"table_id": "Z", "node_id": "dedR", "rows": ["z1"]})
        broken_path = os.path.join(td, "broken-graph.json")
        with open(broken_path, "w", encoding="utf-8") as f:
            json.dump(broken, f)
        # validate 会警告缺贡献；quarantine 应直接按输入错误拒绝
        try:
            quarantine(sources_obj, broken, os.path.join(EX, "records.jsonl"),
                       invalid_raw, os.path.join(td, "never-broken"))
            check(False, "缺贡献根的图应被拒绝")
        except QuarantineError:
            check(True, "每条交付记录必须有贡献根（含将被撤下的）：缺根拒绝")

    # 6.9 原始输入文件哈希前后一致
    after_hashes = {
        "sources": sha256_file(os.path.join(EX, "sources.json")),
        "graph": sha256_file(os.path.join(EX, "graph.json")),
        "records": sha256_file(os.path.join(EX, "records.jsonl")),
        "invalid": sha256_file(os.path.join(EX, "invalidations.json")),
    }
    check(after_hashes == origin_hashes, "全部原始输入文件未被修改")


# ---------------------------------------------------------------------------
# 7. stream_records 单元行为：顺序外重复/空载荷 data 允许（不解释 data）

def test_stream_contract():
    print("== 7. 流式契约：data 不解释、身份索引、重复检测 ==")
    sources_obj = load_json(os.path.join(EX, "sources.json"))
    sources, _ = validate_sources(sources_obj)
    graph, _, _ = validate_graph(load_json(os.path.join(EX, "graph.json")), sources)
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "r.jsonl")
        idents = list(graph.outputs)
        with open(p, "w", encoding="utf-8") as f:
            for (t, r) in idents:
                f.write(json.dumps({"table": t, "row": r, "data": None}) + "\n")  # data=null 合法
        got = []
        n = stream_records(p, graph, lambda ln, obj, raw: got.append((obj["table"], obj["row"])))
        check(n == len(idents) and got == idents, "data 字段不解释（null 也原样流过）且顺序保留")

        # 乱序身份仍须一一对应（顺序不要求等于 outputs，但集合必须相等且不重复）
        p2 = os.path.join(td, "r2.jsonl")
        shuffled = list(reversed(idents))
        with open(p2, "w", encoding="utf-8") as f:
            for (t, r) in shuffled:
                f.write(json.dumps({"table": t, "row": r, "data": 1}) + "\n")
        got2 = []
        stream_records(p2, graph, lambda ln, obj, raw: got2.append((obj["table"], obj["row"])))
        check(sorted(got2) == sorted(idents), "乱序记录只要一一对应即接受")


def main():
    test_shared_credential_fixture()
    test_plan_branch_selection()
    test_snapshot_and_row_scoping()
    test_scale_and_sharing()
    test_streaming_memory()
    test_atomic_delivery_and_errors()
    test_stream_contract()
    print()
    if FAILURES:
        print(f"quarantine 验收失败 {len(FAILURES)} 项:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("quarantine 全部验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
