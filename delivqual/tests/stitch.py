"""stitch-lineage 验收套件（可独立复跑：python3 -I tests/stitch.py）。

覆盖（用手写的整体血缘作为对照，而不只看命令是否成功）：
  1. 小样（examples/stitch，三个阶段、跨阶段复用同名局部 id）：
     - 终端表/行/贡献根 id 不变；从根可穿过任意层物化中间表追到真实原行
     - 共同贡献（all）/充分替代（any）按实际被读取那行的凭据接通：不择一保留、
       不混凭据、不按下游路径拷贝上游树（同一上游根在结果中只有一份）
     - 同阶段重复引用共享；不同阶段的同名节点（scanS/pjS/cS1/...）必须分开
     - 加工依赖连通：每个读中间表的 scan 沿 stitch 到达实际生产该表的节点；
       不新增 project/join/aggregate；原 via_node/via_row 可回查且带阶段定位
     - validate / plan / verify-plan 全部接受；上游 aggregate 与下游 join 都不丢
     - 手写整体血缘对照：与独立逐段拼接的期望图逐节点/逐边相等
     - “错误地把中间表当原行”的请求得到不同结果（正确图可行，错误图不可行）
  2. 接缝错误必须区分且全部拒绝（非零退出、不发布、不改输入、不覆盖已有输出）：
     扫描未绑定 / 绑定找不到目标表 / 行不在目标表 / 行在表中但缺贡献根 /
     阶段文件被同名图替换（SHA-256 不符）/ 跨阶段依赖环
  3. quarantine 交付的图可直接作为上游阶段：只剩一条替代仍接通，不找回已撤下行/边
  4. 运行时生成的多层共享接续：图规模随实际阶段数与引用数线性增长，
     不随完整路径数（各层 any 的笛卡尔展开）增长
  5. 原子交付与输入只读：输出已存在拒绝改写；任何失败都不留半份文件；输入哈希不变
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivqual.common import load_json
from delivqual.model import validate_graph, validate_sources
from delivqual.stitch import StitchError, parse_manifest, stitch_lineage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "examples", "stitch")
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


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def run_cli(args):
    env = dict(os.environ, PYTHONPATH=ROOT)
    return subprocess.run([sys.executable, "-P", "-m", "delivqual", "stitch-lineage"] + args,
                          cwd=ROOT, env=env, capture_output=True, text=True)


def run_dq(sub, args):
    env = dict(os.environ, PYTHONPATH=ROOT)
    return subprocess.run([sys.executable, "-P", "-m", "delivqual", sub] + args,
                          cwd=ROOT, env=env, capture_output=True, text=True)


def load_example_bundle(td, tag="case"):
    """把小样三阶段复制到临时目录并返回 (manifest_obj, dir, sources_obj)。"""
    d = os.path.join(td, tag)
    os.makedirs(d)
    for fn in ("alpha.json", "beta.json", "terminal.json", "manifest.json", "sources.json"):
        write_json(os.path.join(d, fn), load_json(os.path.join(EX, fn)))
    return load_json(os.path.join(d, "manifest.json")), d, load_json(os.path.join(d, "sources.json"))


# ---------------------------------------------------------------------------
# 1. 小样：手写整体血缘对照 + 连通加工依赖 + 共同/替代 + 真实原行

# 手写的整体血缘期望（不调用 stitch 模块，按题意逐段拼接）：
#   终端表 Z/z1、W/w1 与原行 id 不变；所有内部 id 以“阶段::局部id”定位。
EXPECTED_OUTPUTS = {
    ("Z", "z1"): "terminal::cZ",
    ("W", "w1"): "terminal::cW1",
}
# z1 经 beta 的 any 选路后可到达的全部真实原行（两条替代分别展开）
EXPECTED_Z1_ATOMS = {
    ("S", "s1", "s-r1"),   # 经 beta cBr1 -> alpha M1（含上游 aggregate）
    ("T", "t1", "t-r1"),   # 经 beta cBr2 -> alpha M2；右支还有终端直读 alpha M2
}
EXPECTED_W1_ATOMS = {("S", "s1", "s-r1")}  # 终端另一支路再次引用同一上游行 M1


def atoms_of(contribs_index, cid, seen=None):
    """独立展开贡献 DAG 到 source 原子三元组集合。"""
    seen = seen if seen is not None else set()
    if cid in seen:
        return set()
    seen.add(cid)
    node = contribs_index[cid]
    if node["type"] == "source":
        return {(node["source_id"], node["snapshot"], node["row_id"])}
    out = set()
    for e in node["edges"]:
        out |= atoms_of(contribs_index, e["child"], seen)
    return out


def all_paths(contribs_index, cid):
    """枚举完整推导（all=笛卡尔组合，any=任选一支），每条为原子三元组 frozenset。"""
    node = contribs_index[cid]
    if node["type"] == "source":
        return {frozenset([(node["source_id"], node["snapshot"], node["row_id"])])}
    child_sets = [all_paths(contribs_index, e["child"]) for e in node["edges"]]
    if node["type"] == "any":
        return set().union(*child_sets)
    out = {frozenset()}
    for ds in child_sets:
        out = {a | b for a in out for b in ds}
    return out


def count_derivations(contribs_index, cid, memo=None):
    """计算完整推导条数（按边的选择计数，保留多重性）。

    同一上游行被两个 scan 读取时，两条替代的原子集合相同但仍是两次独立选择，
    故这里按边计数而不是去重——图节点线性、推导条数可随 any 层数指数增长。
    """
    memo = memo if memo is not None else {}
    if cid in memo:
        return memo[cid]
    node = contribs_index[cid]
    if node["type"] == "source":
        total = 1
    elif node["type"] == "any":
        total = sum(count_derivations(contribs_index, e["child"], memo) for e in node["edges"])
    else:  # all：各子推导笛卡尔组合
        total = 1
        for e in node["edges"]:
            total *= count_derivations(contribs_index, e["child"], memo)
    memo[cid] = total
    return total


def test_handwritten_oracle():
    print("== 1. 小样手写整体血缘对照：终端身份/真实原行/共同替代/共享/同名分开 ==")
    sources_obj = load_json(os.path.join(EX, "sources.json"))
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "stitched.json")
        rv = run_cli(["--sources", os.path.join(EX, "sources.json"),
                      "--manifest", os.path.join(EX, "manifest.json"), "--out", out])
        check(rv.returncode == 0, f"stitch-lineage 成功退出 0（实际 {rv.returncode} {rv.stderr}）")
        merged = load_json(out)
        cidx = {c["id"]: c for c in merged["contribs"]}
        nidx = {n["node_id"]: n for n in merged["nodes"]}

        # 1.1 终端表/行/根 id 不变（表 id 与行 id 原样，根只是加阶段前缀）
        tables = {t["table_id"]: t["rows"] for t in merged["tables"]}
        check(tables == {"Z": ["z1"], "W": ["w1"]}, "结果只含终端阶段原来的表与行且 id 不变")
        outputs = {(o["table"], o["row"]): o["expr"] for o in merged["outputs"]}
        check(outputs == EXPECTED_OUTPUTS, f"终端贡献根 id 符合手写对照 {outputs}")
        check(all(not c["type"] == "import" for c in merged["contribs"]),
              "结果图不含任何 import 叶（中间表未被登记成新来源）")

        # 1.2 从根穿过任意层中间表直到真实原行
        check(atoms_of(cidx, "terminal::cZ") == EXPECTED_Z1_ATOMS,
              "Z/z1 穿过 beta+alpha 两层物化表追到真实原行 S/s-r1 与 T/t-r1")
        check(atoms_of(cidx, "terminal::cW1") == EXPECTED_W1_ATOMS,
              "W/w1 另一支路回读 alpha M1 追到同一真实原行 S/s-r1")
        # 完整推导枚举：共同贡献不被拆、替代分支各自独立
        z_paths = all_paths(cidx, "terminal::cZ")
        check(z_paths == {
            frozenset([("S", "s1", "s-r1"), ("T", "t1", "t-r1")]),  # any 选 cBr1 后 join 共同贡献
            frozenset([("T", "t1", "t-r1")]),                        # any 选 cBr2（两支都是 M2=T 行）
        }, f"共同/替代语义与手写推导一致：{sorted(map(set, z_paths))}")

        # 1.3 共享：alpha 的贡献根在整张图里只有一份，却被 beta 与 terminal 多处引用
        parent_count = {cid: 0 for cid in cidx}
        for c in merged["contribs"]:
            for e in c.get("edges", []):
                parent_count[e["child"]] += 1
        check(parent_count["alpha::cN1"] == 2,
              "同一上游行 M1 的贡献根 alpha::cN1 被 beta 与 terminal 两条支路共享（不拷贝上游树）")
        check(parent_count["alpha::cT1w"] == 2,
              "同一上游行 M2 的贡献根 alpha::cT1w 被 beta 与终端 join 右支共享")
        check(len([c for c in cidx if c.endswith("::cS1")]) == 1,
              "上游 source 原子全图只有一份")

        # 1.4 不同阶段的同名局部 id 必须分开（小样故意让 scanS/pjS/cS1/scanM2 重名）
        for local in ("scanS", "pjS", "scanM2"):
            gids = [nid for nid in nidx if nid.endswith("::" + local)]
            check(len(gids) >= 2 and len(set(gids)) == len(gids),
                  f"同名加工节点 {local} 在不同阶段分开为 {sorted(gids)}")
        check({"alpha::cS1", "beta::cN1"} <= set(cidx),
              "alpha 的 cS1 与 beta 的 cN1 等同名/异名贡献 id 各自独立可定位")

        # 1.5 加工依赖连通：scan 经唯一 stitch 到实际生产表的节点
        seams = merged["stitch"]["seams"]
        check(len(seams) == 5, f"清单 5 条接缝全部落图（实际 {len(seams)}）")
        for s in seams:
            scan = nidx[f'{s["consumer_stage"]}::{s["scan"]}']
            check(len(scan["inputs"]) == 1 and nidx[scan["inputs"][0]]["op"] == "stitch",
                  f"scan {scan['node_id']} 唯一输入是 stitch 接续节点")
            stitch = nidx[scan["inputs"][0]]
            check(stitch["inputs"] == [s["producer_node"]],
                  f"stitch {stitch['node_id']} 直接连到实际生产表节点 {s['producer_node']}")
            check(s["rows_read"], f"接缝记录实际读到的上游行 {s['rows_read']}")
        # 不凭空增加投影/连接/聚合：新增节点只能是 stitch
        base_ops = []
        for fn in ("alpha.json", "beta.json", "terminal.json"):
            base_ops.extend(n["op"] for n in load_json(os.path.join(EX, fn))["nodes"])
        merged_ops = [n["op"] for n in merged["nodes"]]
        for op in ("project", "join", "aggregate", "union_all", "deduplicate"):
            check(merged_ops.count(op) == base_ops.count(op),
                  f"接续没有新增 {op} 节点（{base_ops.count(op)} 个原样保留）")
        check(merged_ops.count("stitch") == 5, "只新增无权限含义的 stitch 连接步骤")

        # 1.6 原加工步骤与 via_row 可回查：每条非 import 阶段的原边都在结果中且 via 带阶段定位
        for fn in ("alpha.json", "beta.json", "terminal.json"):
            stage = load_json(os.path.join(EX, fn))
            stage_name = stage["graph_id"].replace("stage-", "")
            for c in stage["contribs"]:
                if c["type"] in ("all", "any"):
                    g = cidx[f"{stage_name}::{c['id']}"]
                    got = {(e["via_node"], e["via_row"]) for e in g["edges"]}
                    want = {(f"{stage_name}::{e['via_node']}", e["via_row"]) for e in c["edges"]}
                    check(got == want, f"阶段 {stage_name} 贡献节点 {c['id']} 的 via_node/via_row 原样可回查")

        # 1.7 结果只依赖自己与 sources.json：标准 validate 零 error 零 warning
        sources, _ = validate_sources(sources_obj)
        g, errs, warns = validate_graph(merged, sources)
        check(not errs and not warns, f"接续结果通过标准 validate_graph（{errs}, {warns}）")

        # 1.8 计划与独立核验：上游 aggregate、下游 join 都不能丢
        plan_path = os.path.join(td, "plan.json")
        rp = run_dq("plan", ["--sources", os.path.join(EX, "sources.json"),
                             "--graph", out, "--request", os.path.join(EX, "request.json"),
                             "--out", plan_path])
        check(rp.returncode == 0, "dq plan 在接续图上可行")
        plan = load_json(plan_path)
        atoms = {a["row_id"]: a for r in plan["rows"] for a in r["atoms"]}
        check(set(atoms["s-r1"]["requires"]) == {"aggregate", "union", "project"},
              f"原行 s-r1 继承上游聚合与下游投影/并集（实际 {atoms['s-r1']['requires']}），没有被断成 raw")
        check("join" in atoms["t-r1"]["requires"],
              f"原行 t-r1 参加的下游 join 保留（实际 {atoms['t-r1']['requires']}）")
        check("raw" in atoms["t-r1"]["requires"] and "aggregate" not in atoms["t-r1"]["requires"],
              "未经聚合的 t-r1 仍是 raw 交付，不凭空要求 aggregate")
        rv = run_dq("verify-plan", ["--sources", os.path.join(EX, "sources.json"),
                                    "--graph", out, "--request", os.path.join(EX, "request.json"),
                                    "--plan", plan_path])
        check(json.loads(rv.stdout)["verdict"] == "valid", "plan 通过独立 verify-plan")

        # 1.9 接续标记含阶段摘要与绑定生产表
        marker = merged["stitch"]
        check({s["name"] for s in marker["stages"]} == {"alpha", "beta", "terminal"},
              "结果留下实际接续的阶段摘要")
        check(all(s["graph_sha256"] for s in marker["stages"]), "阶段摘要带文件字节 SHA-256")
        check(all(s["producer_table"] for s in marker["seams"]), "每条扫描接缝记录所绑定的生产表")


def test_wrong_source_request_differs():
    print("== 2. 错误地把中间表当原行登记：同一请求得到不同（不可行）结果 ==")
    with tempfile.TemporaryDirectory() as td:
        good = os.path.join(td, "good.json")
        run_cli(["--sources", os.path.join(EX, "sources.json"),
                 "--manifest", os.path.join(EX, "manifest.json"), "--out", good])
        merged = load_json(good)

        # 错误做法：把中间表 M/N 当成新的原始来源，用 source 叶替换掉接通的上游贡献根，
        # 且只给 raw 许可——这正是题目禁止的“为中间表登记一套替代原来源的许可”。
        wrong_sources = {
            "kind": "sources", "version": 1,
            "sources": [
                {"source_id": "M", "snapshot": "m1", "rows": ["M1", "M2"],
                 "options": [{"option_id": "R", "ops": ["raw"], "audiences": ["dept-a"],
                              "geos": ["cn"], "time": [0, 1000], "notices": ["n-MR"],
                              "classifications": ["report"], "mutex": []}]},
                {"source_id": "N", "snapshot": "n1", "rows": ["N1"],
                 "options": [{"option_id": "R", "ops": ["raw"], "audiences": ["dept-a"],
                              "geos": ["cn"], "time": [0, 1000], "notices": ["n-NR"],
                              "classifications": ["report"], "mutex": []}]},
            ],
        }
        # 干净地重建一张与接续图同外形的错误图：删掉所有上游阶段的贡献树，
        # 凡跨阶段接缝处的上游根一律替换为“中间表 raw 原子”。
        wrong = _wrong_intermediate_as_source_graph(merged)
        wsp = os.path.join(td, "wrong-sources.json")
        write_json(wsp, wrong_sources)
        wp = os.path.join(td, "wrong.json")
        write_json(wp, wrong)
        sources, _ = validate_sources(wrong_sources)
        _g, errs, warns = validate_graph(wrong, sources)
        check(not errs and not warns, f"错误图本身结构合法（才能对照出语义差异）{errs}")

        rp_good = run_dq("plan", ["--sources", os.path.join(EX, "sources.json"),
                                  "--graph", good, "--request", os.path.join(EX, "request.json")])
        rp_wrong = run_dq("plan", ["--sources", wsp, "--graph", wp,
                                   "--request", os.path.join(EX, "request.json")])
        check(json.loads(rp_good.stdout)["status"] == "feasible", "正确接续图：请求可行")
        wrong_plan = json.loads(rp_wrong.stdout)
        check(wrong_plan["status"] == "infeasible",
              f"把中间表当 raw 原行：聚合/连接许可丢失，同一请求不可行（实际 {wrong_plan['status']}）")
        cats = wrong_plan["conflict"]["categories"]
        check("操作未授权" in cats, f"冲突证书指向操作未授权（实际 {cats}），与正确图的可行结论不同")


def _wrong_intermediate_as_source_graph(merged):
    """构造对照错误图：终端外形不变，但每个真实原行叶被替换为中间表 raw 原子。

    只保留终端阶段的贡献节点（所有上游阶段贡献树删除），凡指向接缝上游根的边
    一律改指伪造的“中间表 source 原子”（只登记 raw 许可）。
    """
    # 建立“被跨阶段引用的全局贡献根 cid -> (生产表, 行)”映射（即接缝处的上游根）
    cross = {}
    for s in merged["stitch"]["seams"]:
        pg = load_json(os.path.join(EX, s["producer_stage"] + ".json"))
        for r in s["rows_read"]:
            root = next(o["expr"] for o in pg["outputs"]
                        if o["row"] == r and o["table"] == s["producer_table"])
            cross[f'{s["producer_stage"]}::{root}'] = (s["producer_table"], r)

    def fake_id_for(tbl, row):
        return f"fake::{tbl}::{row}"

    fake = {}
    new_contribs = []
    for c in merged["contribs"]:
        if not c["id"].startswith("terminal::"):
            continue  # 上游阶段贡献树全部删除
        if c["type"] == "source":
            continue  # 终端阶段本身不应有 source 叶（防御）
        nc = {"id": c["id"], "type": c["type"], "edges": []}
        for e in c["edges"]:
            if e["child"] in cross:
                tbl, row = cross[e["child"]]
                fid = fake_id_for(tbl, row)
                fake[fid] = {"id": fid, "type": "source",
                             "source_id": tbl, "snapshot": tbl.lower() + "1", "row_id": row}
                child = fid
            else:
                child = e["child"]
            nc["edges"].append({"child": child, "via_node": e["via_node"], "via_row": e["via_row"]})
        new_contribs.append(nc)
    new_contribs.extend(fake.values())
    return {
        "kind": "graph", "version": 1, "graph_id": "wrong-intermediate-as-source",
        # 加工图只留终端阶段：scan 去掉 stitch 输入，上游阶段与 stitch 全部删除
        "nodes": [
            {"node_id": n["node_id"], "op": n["op"],
             "inputs": [] if n["op"] == "scan" else n["inputs"]}
            for n in merged["nodes"]
            if n["node_id"].startswith("terminal::") and n["op"] != "stitch"
        ],
        "tables": merged["tables"],
        "contribs": new_contribs,
        "outputs": merged["outputs"],
    }


# ---------------------------------------------------------------------------
# 3. 接缝错误：每种失败都要能区分，且不发布/不覆盖/不改输入

def _stitch_api(manifest, d, sources):
    return stitch_lineage(sources, os.path.join(d, "manifest.json"), os.path.join(d, "never.json"))


def test_seam_errors():
    print("== 3. 接缝错误区分：未绑定/缺表/行不在表/缺根/换文件/依赖环 ==")
    sources_obj = load_json(os.path.join(EX, "sources.json"))
    n = 0

    def fresh():
        nonlocal n
        n += 1
        return load_example_bundle(td, f"case{n}")

    with tempfile.TemporaryDirectory() as td:
        # 3.1 扫描未绑定：删掉 terminal scanW 的绑定
        manifest, d, sources = fresh()
        manifest["bindings"] = [b for b in manifest["bindings"]
                                if not (b["stage"] == "terminal" and b["scan"] == "scanW")]
        _rehash(manifest, d)
        try:
            _stitch_api(manifest, d, sources)
            check(False, "扫描未绑定应拒绝")
        except StitchError as e:
            check("扫描未绑定" in str(e) and "scanW" in str(e), f"未绑定错误指出位置: {e}")

        # 3.2 绑定找不到目标表
        manifest, d, sources = fresh()
        manifest["bindings"][0]["producer_table"] = "GHOST"
        _rehash(manifest, d)
        try:
            _stitch_api(manifest, d, sources)
            check(False, "绑定目标表不存在应拒绝")
        except StitchError as e:
            check("找不到目标表" in str(e) and "GHOST" in str(e), f"缺表错误指出位置: {e}")

        # 3.3 行不在目标表：把 beta 的 import 行改成不存在的行
        manifest, d, sources = fresh()
        beta = load_json(os.path.join(d, "beta.json"))
        next(c for c in beta["contribs"] if c["id"] == "cM2")["row"] = "M9"
        write_json(os.path.join(d, "beta.json"), beta)
        _rehash(manifest, d)
        try:
            _stitch_api(manifest, d, sources)
            check(False, "上游行不在目标表应拒绝")
        except StitchError as e:
            check("不在生产阶段" in str(e) and "M9" in str(e), f"行越界错误指出位置: {e}")

        # 3.4 行在表中但没有贡献根：从 alpha outputs 删掉 M2 的根（行仍在 rows）
        manifest, d, sources = fresh()
        alpha = load_json(os.path.join(d, "alpha.json"))
        alpha["outputs"] = [o for o in alpha["outputs"] if o["row"] != "M2"]
        write_json(os.path.join(d, "alpha.json"), alpha)
        _rehash(manifest, d)
        try:
            _stitch_api(manifest, d, sources)
            check(False, "缺贡献根应拒绝")
        except StitchError as e:
            # 在阶段载入或接缝解析处拒绝都可以，但必须指出缺根与具体行 M2
            check(("没有已登记的贡献根" in str(e) or "没有登记的贡献表达式" in str(e))
                  and "M2" in str(e), f"缺根错误指出位置: {e}")

        # 3.5 已绑定的阶段文件被同名图替换：改内容但不改清单哈希
        manifest, d, sources = fresh()
        alpha = load_json(os.path.join(d, "alpha.json"))
        alpha["graph_id"] = "stage-alpha-tampered"
        write_json(os.path.join(d, "alpha.json"), alpha)  # manifest 的 sha256 故意不更新
        try:
            _stitch_api(manifest, d, sources)
            check(False, "摘要不符应拒绝")
        except StitchError as e:
            check("内容摘要不符" in str(e) and "alpha" in str(e), f"摘要错误指出阶段: {e}")

        # 3.5b 换成另一张同名（同 graph_id 也救不了）的合法图：字节哈希仍不符
        manifest, d, sources = fresh()
        other = load_json(os.path.join(d, "beta.json"))
        other["graph_id"] = load_json(os.path.join(d, "alpha.json"))["graph_id"]
        write_json(os.path.join(d, "alpha.json"), other)
        try:
            _stitch_api(manifest, d, sources)
            check(False, "同名图替换应被摘要拦截")
        except StitchError as e:
            check("内容摘要不符" in str(e), f"同名图替换被拦截: {e}")

        # 3.6 跨阶段依赖环：让 alpha 反向依赖 terminal（alpha 增加读 terminal 表的 scan）
        manifest, d, sources = fresh()
        alpha = load_json(os.path.join(d, "alpha.json"))
        alpha["nodes"].append({"node_id": "scanLoop", "op": "scan", "inputs": []})
        alpha["contribs"].append({"id": "cLoop", "type": "import", "scan": "scanLoop", "row": "z1"})
        write_json(os.path.join(d, "alpha.json"), alpha)
        manifest["bindings"].append(
            {"stage": "alpha", "scan": "scanLoop", "producer_stage": "terminal", "producer_table": "Z"})
        _rehash(manifest, d)
        try:
            _stitch_api(manifest, d, sources)
            check(False, "依赖环应拒绝")
        except StitchError as e:
            check("依赖存在环" in str(e), f"依赖环错误指出环节点: {e}")

        # 3.7 一个扫描节点绑定两个目标：清单层直接拒绝
        manifest, d, sources = fresh()
        manifest["bindings"].append(dict(manifest["bindings"][0]))
        _rehash(manifest, d)
        try:
            parse_manifest(manifest)
            check(False, "扫描重复绑定应拒绝")
        except StitchError as e:
            check("重复绑定" in str(e), f"重复绑定错误: {e}")


def _rehash(manifest, d):
    """按目录内实际文件重算清单 SHA-256（用于“内容改了、清单同步更新”的用例）。"""
    for s in manifest["stages"]:
        s["sha256"] = sha256_file(os.path.join(d, s["graph"]))
    write_json(os.path.join(d, "manifest.json"), manifest)


# ---------------------------------------------------------------------------
# 4. quarantine 图作为上游阶段：单边替代接通，不找回已撤下的行/边

def test_quarantine_upstream():
    print("== 4. quarantine 交付图直接作为上游：单边替代仍接通，不找回撤下项 ==")
    from delivqual.quarantine import quarantine
    qsources = load_json(os.path.join(ROOT, "examples", "quarantine", "sources.json"))
    qgraph = load_json(os.path.join(ROOT, "examples", "quarantine", "graph.json"))
    invalid = load_json(os.path.join(ROOT, "examples", "quarantine", "invalidations.json"))
    with tempfile.TemporaryDirectory() as td:
        qdir = os.path.join(td, "q")
        quarantine(qsources, qgraph,
                   os.path.join(ROOT, "examples", "quarantine", "records.jsonl"),
                   invalid, qdir)
        qg = load_json(os.path.join(qdir, "graph.json"))
        # 隔离图里 Q2 的 any 只剩一条边（单边替代）
        q2 = next(c for c in qg["contribs"] if c["id"] == "cAnyQ")
        check(q2["type"] == "any" and len(q2["edges"]) == 1, "前置：quarantine 图含单边 any")

        # 下游阶段回读隔离表 Q 的 Q2 行
        downstream = {
            "kind": "graph", "version": 1, "graph_id": "stage-down",
            "nodes": [
                {"node_id": "scanQ", "op": "scan", "inputs": []},
                {"node_id": "pj", "op": "project", "inputs": ["scanQ"]},
            ],
            "tables": [{"table_id": "D", "node_id": "pj", "rows": ["d1"]}],
            "contribs": [
                {"id": "i1", "type": "import", "scan": "scanQ", "row": "Q2"},
                {"id": "r1", "type": "all", "edges": [
                    {"child": "i1", "via_node": "pj", "via_row": "d1"}]},
            ],
            "outputs": [{"table": "D", "row": "d1", "expr": "r1"}],
        }
        write_json(os.path.join(td, "up.json"), qg)
        write_json(os.path.join(td, "down.json"), downstream)
        manifest = {
            "kind": "stitch-manifest", "version": 1, "terminal": "down",
            "stages": [
                {"name": "up", "graph": "up.json", "sha256": sha256_file(os.path.join(td, "up.json"))},
                {"name": "down", "graph": "down.json", "sha256": sha256_file(os.path.join(td, "down.json"))},
            ],
            "bindings": [
                {"stage": "down", "scan": "scanQ", "producer_stage": "up", "producer_table": "Q"},
            ],
        }
        write_json(os.path.join(td, "manifest.json"), manifest)
        out = os.path.join(td, "stitched.json")
        stitch_lineage(qsources, os.path.join(td, "manifest.json"), out)
        merged = load_json(out)
        cidx = {c["id"]: c for c in merged["contribs"]}
        # 单边 any 原样保留（不被折成直连，也不找回被撤下的另一条边）
        stitched_any = next(c for c in merged["contribs"] if c["id"] == "up::cAnyQ")
        check(stitched_any["type"] == "any" and len(stitched_any["edges"]) == 1,
              "仅剩一条健康选项的替代仍接通为单边 any（不找回已撤下的边）")
        check(merged.get("quarantine") is not None, "结果图带 quarantine 标记以允许单边 any")
        # 追到的真实原行只能是存活推导里的原子
        root = next(o["expr"] for o in merged["outputs"] if o["row"] == "d1")
        atoms = atoms_of(cidx, root)
        invalid_tris = {(x["source_id"], x["snapshot"], x["row_id"]) for x in invalid}
        check(not (atoms & invalid_tris), f"接续结果不含任何已撤下原行 {atoms & invalid_tris}")
        check(atoms == {("U", "u1", "u-r3")}, f"沿存活替代追到唯一健康原行（实际 {atoms}）")
        sources, _ = validate_sources(qsources)
        _g, errs, warns = validate_graph(merged, sources)
        check(not errs and not warns, f"quarantine 接续图通过标准 validate（{errs}, {warns}）")


# ---------------------------------------------------------------------------
# 5. 运行时多层共享接续：规模随阶段/引用线性，不随完整路径数增长

def build_chain_case(td, k):
    """k 个生产阶段串成链，每层 any 的两支都回读上一阶段的同一行（两个 scan）。

    完整端到端推导数随各层 any 呈 2^(k-1) 增长，但每阶段只贡献固定数量的节点/边，
    共享的上游子 DAG 只接一次 ⇒ 结果图规模必须关于 k 线性。
    """
    sources_obj = {
        "kind": "sources", "version": 1,
        "sources": [
            {"source_id": "B", "snapshot": "b1", "rows": [f"b-r{i}" for i in range(k + 1)],
             "options": [{"option_id": "O",
                          "ops": ["raw", "aggregate", "join", "union", "project"],
                          "audiences": ["a"], "geos": ["g"], "time": [0, 100],
                          "notices": ["n-B"], "classifications": ["report"], "mutex": []}]},
        ],
    }
    write_json(os.path.join(td, "sources.json"), sources_obj)
    stages, bindings = [], []
    prev_table = None
    for i in range(k):
        name = f"p{i:02d}"
        table = f"T{i:02d}"
        if i == 0:
            # 起点阶段：一个真实原行经 union/ded 物化出 r0
            nodes = [
                {"node_id": "scanB", "op": "scan", "inputs": []},
                {"node_id": "uni", "op": "project", "inputs": ["scanB"]},
                {"node_id": "ded", "op": "deduplicate", "inputs": ["uni"]},
            ]
            contribs = [
                {"id": "cB", "type": "source",
                 "source_id": "B", "snapshot": "b1", "row_id": "b-r0"},
                {"id": "a1", "type": "all", "edges": [
                    {"child": "cB", "via_node": "uni", "via_row": "r0#new"}]},
                {"id": "root", "type": "all", "edges": [
                    {"child": "a1", "via_node": "ded", "via_row": "r0"}]},
            ]
        else:
            # 同一张上游表被两个 scan 读取：两支充分替代都递归进上一阶段的同一行
            nodes = [
                {"node_id": "scanUp1", "op": "scan", "inputs": []},
                {"node_id": "scanUp2", "op": "scan", "inputs": []},
                {"node_id": "uni", "op": "union_all", "inputs": ["scanUp1", "scanUp2"]},
                {"node_id": "ded", "op": "deduplicate", "inputs": ["uni"]},
            ]
            contribs = [
                {"id": "i1", "type": "import", "scan": "scanUp1", "row": "r0"},
                {"id": "i2", "type": "import", "scan": "scanUp2", "row": "r0"},
                {"id": "a1", "type": "all", "edges": [
                    {"child": "i1", "via_node": "uni", "via_row": "r0#L"}]},
                {"id": "a2", "type": "all", "edges": [
                    {"child": "i2", "via_node": "uni", "via_row": "r0#R"}]},
                {"id": "root", "type": "any", "edges": [
                    {"child": "a1", "via_node": "ded", "via_row": "r0"},
                    {"child": "a2", "via_node": "ded", "via_row": "r0"}]},
            ]
            bindings.append({"stage": name, "scan": "scanUp1",
                             "producer_stage": f"p{i-1:02d}", "producer_table": prev_table})
            bindings.append({"stage": name, "scan": "scanUp2",
                             "producer_stage": f"p{i-1:02d}", "producer_table": prev_table})
        graph_obj = {
            "kind": "graph", "version": 1, "graph_id": f"chain-{name}",
            "nodes": nodes,
            "tables": [{"table_id": table, "node_id": "ded", "rows": ["r0"]}],
            "contribs": contribs,
            "outputs": [{"table": table, "row": "r0", "expr": "root"}],
        }
        fn = f"{name}.json"
        write_json(os.path.join(td, fn), graph_obj)
        stages.append({"name": name, "graph": fn,
                       "sha256": sha256_file(os.path.join(td, fn))})
        prev_table = table

    # 终端阶段：join 两个不同深度的上游阶段，验证跨层共享不复制
    term_nodes = [
        {"node_id": "scanLo", "op": "scan", "inputs": []},
        {"node_id": "scanHi", "op": "scan", "inputs": []},
        {"node_id": "jn", "op": "join", "inputs": ["scanLo", "scanHi"]},
    ]
    term_contribs = [
        {"id": "iLo", "type": "import", "scan": "scanLo", "row": "r0"},
        {"id": "iHi", "type": "import", "scan": "scanHi", "row": "r0"},
        {"id": "root", "type": "all", "edges": [
            {"child": "iLo", "via_node": "jn", "via_row": "z#L"},
            {"child": "iHi", "via_node": "jn", "via_row": "z#R"}]},
    ]
    term = {
        "kind": "graph", "version": 1, "graph_id": "chain-terminal",
        "nodes": term_nodes,
        "tables": [{"table_id": "Z", "node_id": "jn", "rows": ["z"]}],
        "contribs": term_contribs,
        "outputs": [{"table": "Z", "row": "z", "expr": "root"}],
    }
    write_json(os.path.join(td, "terminal.json"), term)
    stages.append({"name": "terminal", "graph": "terminal.json",
                   "sha256": sha256_file(os.path.join(td, "terminal.json"))})
    bindings.append({"stage": "terminal", "scan": "scanLo",
                     "producer_stage": "p00", "producer_table": "T00"})
    bindings.append({"stage": "terminal", "scan": "scanHi",
                     "producer_stage": f"p{k-1:02d}", "producer_table": prev_table})
    manifest = {"kind": "stitch-manifest", "version": 1, "terminal": "terminal",
                "stages": stages, "bindings": bindings}
    write_json(os.path.join(td, "manifest.json"), manifest)
    return sources_obj


def test_scale_linear():
    print("== 5. 多层共享接续：图规模随阶段/引用线性，不随完整路径数增长 ==")
    prev = None
    for k in (4, 8, 16):
        with tempfile.TemporaryDirectory() as td:
            sources_obj = build_chain_case(td, k)
            out = os.path.join(td, "out.json")
            stitch_lineage(sources_obj, os.path.join(td, "manifest.json"), out)
            merged = load_json(out)
            n_nodes = len(merged["nodes"])
            n_contribs = len(merged["contribs"])
            n_edges = sum(len(c.get("edges", [])) for c in merged["contribs"])
            # p00：3 个加工节点无 stitch；其余 k-1 阶段各 4 个原节点 + 2 个 stitch；
            # 终端：3 个原节点 + 2 个 stitch
            expect_nodes = 3 + 6 * (k - 1) + 5
            check(n_nodes == expect_nodes,
                  f"k={k}: 加工节点 {n_nodes} == 线性期望 {expect_nodes}（与 {2 ** (k - 1)} 条完整路径无关）")
            # 贡献节点：p00 3 个；其余每阶段 3 个（a1/a2/root，两个 import 叶不落结果图）；终端 1 个根
            expect_contribs = 3 + 3 * (k - 1) + 1
            check(n_contribs == expect_contribs,
                  f"k={k}: 贡献节点 {n_contribs} == 线性期望 {expect_contribs}")
            # 贡献边：p00 两条 all 各 1 边；其余每阶段两个 all + 一个 any 共 4 边；终端根 2 边
            expect_edges = 2 + 4 * (k - 1) + 2
            check(n_edges == expect_edges,
                  f"k={k}: 贡献边 {n_edges} == 线性期望 {expect_edges}（共享子图不复制）")
            # 最早阶段 p00 的根被终端直读与整条链共享，全图只出现一次
            check(sum(1 for c in merged["contribs"] if c["id"] == "p00::root") == 1,
                  f"k={k}: 最深层共享根全图唯一，未按 2^{k-1} 条路径复制")
            # 结果仍能通过标准校验，且沿 any 展开的完整推导确实指数级（语义不被压缩丢失）
            sources, _ = validate_sources(sources_obj)
            _g, errs, warns = validate_graph(merged, sources)
            check(not errs and not warns, f"k={k}: 多层接续图 validate 通过")
            cidx = {c["id"]: c for c in merged["contribs"]}
            n_paths = count_derivations(cidx, "terminal::root")
            # 每层 any 二选一（共 k-1 层），终端 join 两支 ⇒ 2^(k-1) 条独立推导
            check(n_paths == 2 ** (k - 1),
                  f"k={k}: 完整推导条数 {n_paths} = 2^{k-1}（图线性但语义完整可展开）")
            if prev is not None:
                k0, n0, c0 = prev
                check(n_contribs / c0 <= (k / k0) * 1.6,
                      f"k {k0}->{k}: 贡献节点 {c0}->{n_contribs} 随阶段线性增长")
            prev = (k, n_nodes, n_contribs)



# ---------------------------------------------------------------------------
# 6. 原子交付、退出码与输入只读

def test_atomic_and_readonly():
    print("== 6. 原子交付：失败非零退出、不覆盖已有输出、不改输入 ==")
    env_inputs = {
        "sources": sha256_file(os.path.join(EX, "sources.json")),
        "manifest": sha256_file(os.path.join(EX, "manifest.json")),
        "alpha": sha256_file(os.path.join(EX, "alpha.json")),
        "beta": sha256_file(os.path.join(EX, "beta.json")),
        "terminal": sha256_file(os.path.join(EX, "terminal.json")),
    }
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "exists.json")
        write_json(out, {"sentinel": True})
        rv = run_cli(["--sources", os.path.join(EX, "sources.json"),
                      "--manifest", os.path.join(EX, "manifest.json"), "--out", out])
        check(rv.returncode == 10, "输出已存在：退出 10")
        check(load_json(out) == {"sentinel": True}, "拒绝时不覆盖已有输出文件")

        # 坏清单（文件不存在）⇒ 退出 2，且目标路径从未出现
        rv = run_cli(["--sources", os.path.join(EX, "sources.json"),
                      "--manifest", os.path.join(td, "no-manifest.json"),
                      "--out", os.path.join(td, "x.json")])
        check(rv.returncode == 2 and not os.path.exists(os.path.join(td, "x.json")),
              "清单不可读：退出 2 且无半份结果文件")

        # 接缝失败 ⇒ 退出 10 且目标路径从未出现
        manifest, d, sources = load_example_bundle(td)
        manifest["bindings"] = [b for b in manifest["bindings"]
                                if not (b["stage"] == "terminal" and b["scan"] == "scanW")]
        write_json(os.path.join(d, "manifest.json"), manifest)
        target = os.path.join(td, "partial.json")
        rv = run_cli(["--sources", os.path.join(d, "sources.json"),
                      "--manifest", os.path.join(d, "manifest.json"), "--out", target])
        check(rv.returncode == 10 and not os.path.exists(target),
              "无法接通：退出 10 且读者看不到未生成完整的结果文件")
        check(not any(fn.startswith(".dq-stitch-") for fn in os.listdir(d)),
              "临时文件已清理，无残留半份交付")

    after = {
        "sources": sha256_file(os.path.join(EX, "sources.json")),
        "manifest": sha256_file(os.path.join(EX, "manifest.json")),
        "alpha": sha256_file(os.path.join(EX, "alpha.json")),
        "beta": sha256_file(os.path.join(EX, "beta.json")),
        "terminal": sha256_file(os.path.join(EX, "terminal.json")),
    }
    check(after == env_inputs, "全部原始输入文件未被修改")


def main():
    test_handwritten_oracle()
    test_wrong_source_request_differs()
    test_seam_errors()
    test_quarantine_upstream()
    test_scale_linear()
    test_atomic_and_readonly()
    print()
    if FAILURES:
        print(f"stitch-lineage 验收失败 {len(FAILURES)} 项:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("stitch-lineage 全部验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
