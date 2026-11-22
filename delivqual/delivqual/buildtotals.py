"""build-totals：从两份原始明细一次性生成部门金额汇总表与匹配的行级贡献图。

加工语义（与命令文档一致，全部为精确整数运算）：
  1. 采购明细 P 行：(row_id, join_key, invoice_id, cents)
     归属明细 A 行：(row_id, join_key, department)
  2. 两侧按 join_key 做内连接，未匹配原行不计入；
  3. 连接结果投影为 (department, invoice_id, cents)，完全相同的三元组只计一次；
  4. 按 department 对 cents 求精确整数和（部门内事实相加为零仍输出该部门）。

贡献图沿用现有 graph.json 结构（source/all/any + via_node/via_row），可直接交给
`dq validate`。关键的证据压缩设计（图随原行数线性增长，不枚举物理配对）：

  - 同一侧、同一 join_key 且非行号业务字段完全相同的原行构成一个**逻辑等价类**。
    类内每行都是该类输出的充分替代凭据，用显式 deduplicate 节点 + any 表达式表达；
    类的输出只物化一次（all 包装节点）。
  - 一次真实连接是“采购类 × 归属类”两个类输出的共同贡献（一个 all 节点，
    两条边同指 join 节点），而不是 m*n 个物理行配对。任一路径展开得到的
    (P 行, A 行) 都是同键真实匹配，绝不会跨键误配。
  - 相同 (department, invoice_id, cents) 三元组经不同连接得出时，只能通过
    显式 fact 级 deduplicate + any 合并；等额但发票不同的事实各自独立。

via_row 解读（不使用统一占位行号，每个值都能回查到具体阶段的行或内容定义的等价类）：
  - 原始 row_id 字面值：scan 边直接指该输入行；deduplicate 的 any 边指类内一条
    真实候选输入行（该行在去重后存活）。
  - "C:<hash>"：等价类去重节点的输出行，<hash> 由类的业务内容（join_key 与
    非行号字段）确定性生成；沿子 any 可展开到全部成员原行。
  - "J:<hash>#L"/"J:<hash>#R"：连接输出行的左/右侧，<hash> 由两个类的内容确定；
    沿子树可展开到该侧类的任一成员原行。
  - "J:<hash>"：project 输出行（一对类连接投影出的三元组）。
  - "F:<hash>"：去重后的业务事实行（fact 级 deduplicate 的输出，或 aggregate 的
    输入行标签），<hash> 由 (department, invoice_id, cents) 确定。
  - 汇总表行号 "T0001" ...：aggregate 输出行，即 totals.jsonl 的 row_id。

仅改变输入文件行序时，所有内容哈希、排序与编号不变，表与图逐字节一致。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile

from .common import canonical, InputError
from .model import validate_sources

TABLE_ID = "totals"


class BuildError(Exception):
    """输入明细或绑定不满足 build-totals 的契约。"""


# ---------------------------------------------------------------------------
# 输入载入与逐条校验

def _is_nonempty_str(x):
    return isinstance(x, str) and x != ""


def load_jsonl(path, side):
    """载入 JSONL；返回原始对象列表。语法问题一律按坏记录拒绝（不静默跳过）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise InputError(f"无法读取文件 {path}: {e}")
    rows = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.strip() == "":
            raise BuildError(f"{side} 文件 {path} 第 {lineno} 行：空行不是合法记录")
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise BuildError(f"{side} 文件 {path} 第 {lineno} 行：不是合法 JSON（{e}）")
        rows.append((lineno, obj))
    return rows


def _check_fields(obj, side, lineno, required):
    if not isinstance(obj, dict):
        raise BuildError(f"{side} 第 {lineno} 行：记录必须是 JSON 对象")
    unknown = set(obj) - set(required)
    if unknown:
        raise BuildError(f"{side} 第 {lineno} 行：出现未知字段 {sorted(unknown)}，契约只允许 {required}")
    missing = [k for k in required if k not in obj]
    if missing:
        raise BuildError(f"{side} 第 {lineno} 行：缺少必填字段 {missing}")


def parse_purchase(records, registered_rows):
    """校验采购明细，返回 {row_id: (join_key, invoice_id, cents)}（按 row_id 排序）。"""
    out = {}
    for lineno, obj in records:
        side = "采购明细"
        _check_fields(obj, side, lineno, ("row_id", "join_key", "invoice_id", "cents"))
        rid, key, invoice, cents = obj["row_id"], obj["join_key"], obj["invoice_id"], obj["cents"]
        if not _is_nonempty_str(rid):
            raise BuildError(f"{side} 第 {lineno} 行：row_id 必须是非空字符串")
        if not _is_nonempty_str(key):
            raise BuildError(f"{side} 第 {lineno} 行：join_key 必须是非空字符串")
        if not _is_nonempty_str(invoice):
            raise BuildError(f"{side} 第 {lineno} 行：invoice_id 必须是非空字符串")
        if isinstance(cents, bool) or not isinstance(cents, int):
            raise BuildError(f"{side} 第 {lineno} 行：cents 必须是有符号整数（不接受布尔值），得到 {cents!r}")
        if rid in out:
            raise BuildError(f"{side} 第 {lineno} 行：row_id {rid!r} 在同一输入内重复出现")
        if rid not in registered_rows:
            raise BuildError(f"{side} 第 {lineno} 行：row_id {rid!r} 不在所绑定快照的登记行清单中")
        out[rid] = (key, invoice, cents)
    return out


def parse_attribution(records, registered_rows):
    """校验归属明细，返回 {row_id: (join_key, department)}（按 row_id 排序）。"""
    out = {}
    for lineno, obj in records:
        side = "归属明细"
        _check_fields(obj, side, lineno, ("row_id", "join_key", "department"))
        rid, key, dept = obj["row_id"], obj["join_key"], obj["department"]
        if not _is_nonempty_str(rid):
            raise BuildError(f"{side} 第 {lineno} 行：row_id 必须是非空字符串")
        if not _is_nonempty_str(key):
            raise BuildError(f"{side} 第 {lineno} 行：join_key 必须是非空字符串")
        if not _is_nonempty_str(dept):
            raise BuildError(f"{side} 第 {lineno} 行：department 必须是非空字符串")
        if rid in out:
            raise BuildError(f"{side} 第 {lineno} 行：row_id {rid!r} 在同一输入内重复出现")
        if rid not in registered_rows:
            raise BuildError(f"{side} 第 {lineno} 行：row_id {rid!r} 不在所绑定快照的登记行清单中")
        out[rid] = (key, dept)
    return out


# ---------------------------------------------------------------------------
# 核心计算

def _hid(content) -> str:
    """内容寻址哈希：只与业务内容有关，与行序无关。"""
    return hashlib.sha256(canonical(content)).hexdigest()[:16]


def build_totals(sources_obj, purchase_records, attribution_records,
                 purchase_binding, attribution_binding, graph_id):
    """从已载入的原始记录计算 (totals_rows, graph_obj, stats)。

    purchase_binding / attribution_binding: (source_id, snapshot)，必须是 sources_obj
    中两个不同的已登记源快照。records 为 load_jsonl 的 [(lineno, obj), ...]。
    """
    sources, errs = validate_sources(sources_obj)
    if errs:
        raise BuildError("sources.json 本身不合法: " + "; ".join(errs))

    p_key = tuple(purchase_binding)
    a_key = tuple(attribution_binding)
    for name, key in (("采购", p_key), ("归属", a_key)):
        if key not in sources.by_key:
            raise BuildError(f"{name}输入绑定的源快照 {key} 未在 sources.json 登记")
    if p_key == a_key:
        raise BuildError(f"两份输入必须绑定到不同的已登记源快照，实际都是 {p_key}")

    purchases = parse_purchase(purchase_records, sources.by_key[p_key].rows)
    attributions = parse_attribution(attribution_records, sources.by_key[a_key].rows)

    # ---- 等价类（只含有连接命中的键；未匹配原行不计入）----
    p_classes = {}   # (join_key, invoice_id, cents) -> [row_id,...]
    a_classes = {}   # (join_key, department)          -> [row_id,...]
    p_keys = {v[0] for v in purchases.values()}
    a_keys = {v[0] for v in attributions.values()}
    matched_keys = p_keys & a_keys
    for rid in sorted(purchases):
        key, invoice, cents = purchases[rid]
        if key in matched_keys:
            p_classes.setdefault((key, invoice, cents), []).append(rid)
    for rid in sorted(attributions):
        key, dept = attributions[rid]
        if key in matched_keys:
            a_classes.setdefault((key, dept), []).append(rid)

    # ---- 真实类配对 → 投影事实；事实 -> 支撑配对集合 ----
    # 归属类按 join_key 建索引：配对工作量与真实同键类对数成正比，不扫跨键组合
    a_by_key = {}
    for content in a_classes:                       # content = (join_key, department)
        a_by_key.setdefault(content[0], []).append(content)
    # fact = (department, invoice_id, cents)
    fact_pairs = {}
    for pc in sorted(p_classes):                    # pc = (join_key, invoice_id, cents)
        p_key_v, invoice, cents = pc
        for ac in a_by_key.get(p_key_v, ()):
            fact = (ac[1], invoice, cents)          # ac[1] = department
            fact_pairs.setdefault(fact, []).append((pc, ac))

    # ---- 部门精确整数和 ----
    dept_total = {}
    for (dept, _inv, cents) in fact_pairs:
        dept_total[dept] = dept_total.get(dept, 0) + cents

    totals_rows = []
    for idx, dept in enumerate(sorted(dept_total), start=1):
        totals_rows.append({
            "row_id": f"T{idx:04d}",
            "department": dept,
            "total_cents": dept_total[dept],
        })

    graph_obj, stats = _build_graph(
        p_key, a_key, p_classes, a_classes, fact_pairs, totals_rows, graph_id)
    stats.update({
        "purchase_rows": len(purchases),
        "attribution_rows": len(attributions),
        "matched_join_keys": len(matched_keys),
        "distinct_facts": len(fact_pairs),
        "departments": len(dept_total),
    })
    return totals_rows, graph_obj, stats


# ---------------------------------------------------------------------------
# 贡献图生成（线性压缩，不展开物理配对）

def _build_graph(p_key, a_key, p_classes, a_classes, fact_pairs,
                 totals_rows, graph_id):
    p_sid, p_snap = p_key
    a_sid, a_snap = a_key

    pnodes = []
    contribs = []
    outputs = []

    def add_pnode(node_id, op, inputs):
        pnodes.append({"node_id": node_id, "op": op, "inputs": list(inputs)})

    def add_contrib(c):
        contribs.append(c)

    # 固定主干加工节点
    add_pnode("scan-P", "scan", [])
    add_pnode("scan-A", "scan", [])

    # ---- 单侧等价类：原子 → (any) → 去重 all 包装；每类一个 deduplicate 节点 ----
    def build_side(classes, sid, snap, scan_node, prefix):
        all_of = {}       # 类内容 -> all 包装 cid
        for content in sorted(classes):
            members = sorted(classes[content])          # 已按 row_id 排序
            hid = _hid([prefix, sid, snap, *content])
            dedup_node = f"d{prefix}-{hid}"
            add_pnode(dedup_node, "deduplicate", [scan_node])
            for rid in members:
                atom = f"src-{prefix}-{hid}-{rid}"
                add_contrib({"id": atom, "type": "source",
                             "source_id": sid, "snapshot": snap, "row_id": rid})
                # scan 边：via_row 就是真实输入行号
                add_contrib({"id": f"scan-{prefix}-{hid}-{rid}", "type": "all", "edges": [
                    {"child": atom, "via_node": scan_node, "via_row": rid}]})
            wrapper = f"cls-{prefix}-{hid}"
            if len(members) == 1:
                # 单成员类：去重节点单行输入=输出；仍经由 scan 包装保持路径完整可回查
                add_contrib({"id": wrapper, "type": "all", "edges": [
                    {"child": f"scan-{prefix}-{hid}-{members[0]}", "via_node": dedup_node,
                     "via_row": f"C:{hid}"}]})
            else:
                # 多成员类：any 表达“任一成員行都是该类输出的充分替代凭据”，
                # 每条边的 via_row 是该成员在去重节点上的真实候选行
                any_cid = f"cls-{prefix}-any-{hid}"
                add_contrib({"id": any_cid, "type": "any", "edges": [
                    {"child": f"scan-{prefix}-{hid}-{rid}",
                     "via_node": dedup_node, "via_row": rid}
                    for rid in members]})
                add_contrib({"id": wrapper, "type": "all", "edges": [
                    {"child": any_cid, "via_node": dedup_node,
                     "via_row": f"C:{hid}"}]})
            all_of[content] = (wrapper, hid)
        return all_of

    p_all = build_side(p_classes, p_sid, p_snap, "scan-P", "P")
    a_all = build_side(a_classes, a_sid, a_snap, "scan-A", "A")

    add_pnode("join-J", "join", ["scan-P", "scan-A"])
    add_pnode("project-R", "project", ["join-J"])

    # ---- 事实级去重节点（只有多配对事实才真正建 deduplicate 节点）----
    multi_facts = {f for f, pairs in fact_pairs.items() if len(pairs) >= 2}
    for fact in sorted(multi_facts):
        fhid = _hid(["fact", *fact])
        add_pnode(f"df-{fhid}", "deduplicate", ["project-R"])
    # aggregate 元数恰为 1：多份事实流先汇入一个 union_all（单份则直接接入）
    agg_inputs = [f"df-{_hid(['fact', *f])}" for f in sorted(multi_facts)] + ["project-R"]
    if len(agg_inputs) >= 2:
        add_pnode("union-T", "union_all", agg_inputs)
        add_pnode("aggregate-T", "aggregate", ["union-T"])
    else:
        add_pnode("aggregate-T", "aggregate", agg_inputs)

    # ---- 每个类配对：一个 join all（共同贡献）+ 一个 project all ----
    fact_root = {}   # fact -> 挂到 aggregate 的贡献 cid
    for fact in sorted(fact_pairs):
        dept, invoice, cents = fact
        pair_contribs = []
        for (pc, ac) in sorted(fact_pairs[fact]):
            p_wrap, p_hid = p_all[pc]
            a_wrap, a_hid = a_all[ac]
            jhid = _hid(["pair", ["P", p_sid, p_snap, *pc],
                         ["A", a_sid, a_snap, *ac]])
            join_cid = f"pair-{jhid}"
            add_contrib({"id": join_cid, "type": "all", "edges": [
                {"child": p_wrap, "via_node": "join-J", "via_row": f"J:{jhid}#L"},
                {"child": a_wrap, "via_node": "join-J", "via_row": f"J:{jhid}#R"},
            ]})
            proj_cid = f"proj-{jhid}"
            add_contrib({"id": proj_cid, "type": "all", "edges": [
                {"child": join_cid, "via_node": "project-R", "via_row": f"J:{jhid}"}]})
            pair_contribs.append((proj_cid, jhid))
        fhid = _hid(["fact", *fact])
        if len(pair_contribs) == 1:
            fact_root[fact] = (pair_contribs[0][0], fhid)
        else:
            # 相同三元组的多个来源只能经显式 deduplicate 合成充分替代
            any_cid = f"fact-any-{fhid}"
            add_contrib({"id": any_cid, "type": "any", "edges": [
                {"child": proj_cid, "via_node": f"df-{fhid}", "via_row": f"J:{jhid}"}
                for proj_cid, jhid in pair_contribs]})
            fact_root[fact] = (any_cid, fhid)

    # ---- 部门根：aggregate 的分组输入（all，多条边同指 aggregate 节点）----
    for row in totals_rows:
        dept = row["department"]
        members = sorted(f for f in fact_pairs if f[0] == dept)
        root_cid = f"total-{_hid(['dept', dept])}"
        add_contrib({"id": root_cid, "type": "all", "edges": [
            {"child": fact_root[f][0], "via_node": "aggregate-T",
             "via_row": f"F:{fact_root[f][1]}"}
            for f in members]})
        outputs.append({"table": TABLE_ID, "row": row["row_id"], "expr": root_cid})

    graph_obj = {
        "kind": "graph",
        "version": 1,
        "graph_id": graph_id,
        "nodes": pnodes,
        "tables": [{
            "table_id": TABLE_ID,
            "node_id": "aggregate-T",
            "rows": [r["row_id"] for r in totals_rows],
        }],
        "contribs": contribs,
        "outputs": outputs,
    }
    n_edges = sum(len(c.get("edges", [])) for c in contribs)
    stats = {
        "graph_id": graph_id,
        "processing_nodes": len(pnodes),
        "contrib_nodes": len(contribs),
        "contrib_edges": n_edges,
        "output_rows": len(totals_rows),
    }
    return graph_obj, stats


# ---------------------------------------------------------------------------
# 原子交付：先写临时目录，成功后一次性 rename；绝不留下半份交付

def deliver(output_dir, totals_rows, graph_obj):
    out = os.path.abspath(output_dir)
    parent = os.path.dirname(out) or "."
    os.makedirs(parent, exist_ok=True)
    if os.path.lexists(out):
        raise BuildError(f"输出目录已存在，拒绝改写: {out}")
    tmp = tempfile.mkdtemp(prefix=".dq-build-totals-", dir=parent)
    try:
        totals_path = os.path.join(tmp, "totals.jsonl")
        graph_path = os.path.join(tmp, "graph.json")
        with open(totals_path, "w", encoding="utf-8") as f:
            for row in totals_rows:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n")
        with open(graph_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(graph_obj, ensure_ascii=False, indent=2) + "\n")
        if os.path.lexists(out):  # rename 前再次确认，防止并发占位
            raise BuildError(f"输出目录已存在，拒绝改写: {out}")
        os.replace(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return out
