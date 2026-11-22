"""quarantine：把一份已归档交付包隔离成可替换旧包的副本。

输入：
  sources.json      源登记（只读，不修改、不作任何授权决定）
  graph.json        原加工图 + 贡献表达式（只读）
  交付记录 JSONL      每行 {"table", "row", "data"}，与图的输出表行一一对应
  失效原行清单       JSON 数组，元素为 {"source_id", "snapshot", "row_id"}，
                    以三项原值共同定位；重复项视为同一次失效

规则（只做结构性的凭据隔离，不重算业务值）：
  - 叶子 source 原子命中失效三元组即失效；
  - all（共同贡献）：任一必需分支失效即失效，少一条都不能成立；
  - any（充分替代）：所有替代边全部失效才失效。
  记录行只要还存在至少一条完全不使用失效原行的完整推导就原样保留（行 id、
  data 均不动）；否则从副本与图的表行中一起撤下。

产出目录（必须此前不存在；三份文件一起原子发布）：
  rows.jsonl         保留记录，原输入顺序、原载荷逐行复制（流式，不在内存累积载荷）
  graph.json         隔离图：贡献表达式恰好是全部仍然健全的推导（共享子 DAG 原样
                     保留，不把只剩一条的替代直接接到上层），表定义保留，撤下行移除
  withdrawals.json   撤下凭据：绑定原图内容与失效清单，给出每条撤下记录的原贡献根
                     和沿原图具体边可复查的失效理由（共享失效节点只存一份）

复杂度以实际图节点/边与记录数为基础：活性求值是一次 DAG 线性扫描，共享子图只
遍历一次；不枚举任何可能的完整推导，撤下证据也不按输出行重复复制。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile

from .common import canonical, InputError, sha256_of
from .model import validate_graph, validate_sources

QUARANTINE_MARKER_VERSION = 1


class QuarantineError(Exception):
    """隔离输入不满足契约（记录与图不对应、引用未登记等）。"""


# ---------------------------------------------------------------------------
# 失效清单

def parse_invalidations(obj, sources):
    """校验失效清单；返回去重后的失效三元组集合。

    重复项视为同一次失效；未登记的引用是输入错误（三项原值共同定位）。
    """
    if not isinstance(obj, list):
        raise QuarantineError("失效原行清单必须是 JSON 数组")
    invalid = set()
    for ii, item in enumerate(obj):
        where = f"失效清单[{ii}]"
        if not isinstance(item, dict):
            raise QuarantineError(f"{where}: 必须是对象")
        if set(item) != {"source_id", "snapshot", "row_id"}:
            raise QuarantineError(
                f"{where}: 只能含 source_id/snapshot/row_id 三个字段，得到 {sorted(item)}")
        sid, snap, row_id = item["source_id"], item["snapshot"], item["row_id"]
        if not (isinstance(sid, str) and isinstance(snap, str) and isinstance(row_id, str)
                and sid != "" and snap != "" and row_id != ""):
            raise QuarantineError(f"{where}: 三个字段都必须是非空字符串")
        key = (sid, snap)
        src = sources.by_key.get(key)
        if src is None:
            raise QuarantineError(f"{where}: 引用未登记的源快照 {key}")
        if row_id not in src.rows:
            raise QuarantineError(f"{where}: 引用源 {key} 未登记的原行 {row_id!r}")
        invalid.add((sid, snap, row_id))
    return invalid


# ---------------------------------------------------------------------------
# 活性求值（一次 DAG 线性扫描；共享节点只算一次）

def evaluate_liveness(graph, invalid_atoms):
    """返回 cid -> bool（True=仍健全）。

    invalid_atoms: 命中失效清单的 source 贡献节点 id 集合。
    贡献图已由 validate_graph 保证无环，这里按拓扑序自底向上求值。
    """
    alive = {}
    # Kahn 拓扑（贡献边 parent -> child），queue 先出根；反转后得到自底向上的求值序
    indeg = {cid: 0 for cid in graph.contribs}
    for cid, node in graph.contribs.items():
        for e in node.edges:
            indeg[e.child] += 1
    queue = [cid for cid, d in indeg.items() if d == 0]
    root_first = []
    while queue:
        x = queue.pop()
        root_first.append(x)
        for e in graph.contribs[x].edges:
            indeg[e.child] -= 1
            if indeg[e.child] == 0:
                queue.append(e.child)
    if len(root_first) != len(graph.contribs):  # 校验阶段已挡环，防御性处理
        raise QuarantineError("贡献表达式图存在环（输入校验应当已经拒绝）")

    for cid in reversed(root_first):
        node = graph.contribs[cid]
        if node.type == "source":
            alive[cid] = cid not in invalid_atoms
        elif node.type == "all":
            # 共同贡献：少一条必需分支都不能成立
            alive[cid] = all(alive[e.child] for e in node.edges)
        else:  # any：只有全部替代都失效才不能成立
            alive[cid] = any(alive[e.child] for e in node.edges)
    return alive


# ---------------------------------------------------------------------------
# 撤下理由 DAG（共享失效节点只存一份；只收集从撤下根可达的节点）

def _short_hash(parts) -> str:
    return hashlib.sha256(canonical(parts)).hexdigest()[:16]


def build_reason_graph(graph, alive, withdrawn_roots):
    """构造撤下理由 DAG。

    返回 (reasons_list, root_reason_of)：
      reasons_list 按理由 id 排序，理由只覆盖从撤下根可达的失效节点（不含与撤下
      无关的节点）；同一失效贡献节点在所有撤下记录间共享同一个理由 id。
    复查方式：source 理由的三元组必在失效清单中；all 理由指出一条已失效必需边；
    any 理由逐条说明每个原替代边为何失效（child 指向另一理由）。
    """
    reason_id = {}        # cid -> 理由 id（按需登记，天然去重）
    reason_obj = {}       # 理由 id -> 可序列化对象

    def rid_of(cid):
        if cid not in reason_id:
            node = graph.contribs[cid]
            if node.type == "source":
                rid = f"r-src-{_short_hash(['reason', cid, node.source_key[0], node.source_key[1], node.row_id])}"
                reason_id[cid] = rid
                reason_obj[rid] = {
                    "id": rid,
                    "kind": "invalidated_source",
                    "contrib_id": cid,
                    "source_id": node.source_key[0],
                    "snapshot": node.source_key[1],
                    "row_id": node.row_id,
                }
            elif node.type == "all":
                # 共同贡献只需指出一个已经失效的必需分支（取原边序中的第一条）
                dead_edge = next(e for e in node.edges if not alive[e.child])
                child_rid = rid_of(dead_edge.child)
                rid = f"r-all-{_short_hash(['reason', cid, dead_edge.child, dead_edge.via_node, dead_edge.via_row])}"
                reason_id[cid] = rid
                reason_obj[rid] = {
                    "id": rid,
                    "kind": "required_branch_failed",
                    "contrib_id": cid,
                    "failed_edge": {
                        "child": dead_edge.child,
                        "via_node": dead_edge.via_node,
                        "via_row": dead_edge.via_row,
                        "reason": child_rid,
                    },
                }
            else:  # any：充分替代必须说明每条原替代边为什么都失效
                edge_reasons = []
                for e in node.edges:
                    edge_reasons.append({
                        "child": e.child,
                        "via_node": e.via_node,
                        "via_row": e.via_row,
                        "reason": rid_of(e.child),
                    })
                rid = f"r-any-{_short_hash(['reason', cid] + [er['child'] for er in edge_reasons])}"
                reason_id[cid] = rid
                reason_obj[rid] = {
                    "id": rid,
                    "kind": "all_alternatives_failed",
                    "contrib_id": cid,
                    "edges": edge_reasons,
                }
        return reason_id[cid]

    root_reason_of = {}
    for cid in withdrawn_roots:
        root_reason_of[cid] = rid_of(cid)
    return [reason_obj[k] for k in sorted(reason_obj)], root_reason_of


# ---------------------------------------------------------------------------
# 隔离图构造：恰好保留全部健全推导，不重写任何边的加工步骤/via_row

def build_quarantine_graph(graph_obj, graph, alive, invalid_set):
    """从原图派生隔离图对象。

    - 加工节点全部保留（表定义与加工步骤可回查）；
    - 贡献节点只保留从存活输出根可达的健全节点；
    - 存活节点的边只剪掉失效分支，其余边（含 via_node/via_row）原样保留，
      因此只剩一条替代的 any 不会被直接接到上层；
    - 表定义保留（即使行全部撤下），撤下行从 rows 与 outputs 中一起移除。
    """
    # 存活输出根（按原图 outputs 顺序）
    surviving_roots = []
    withdrawn = []          # [(table, row, root_cid)]，保持原图 outputs 顺序
    for out in graph_obj["outputs"]:
        cid = out["expr"]
        if alive[cid]:
            surviving_roots.append(cid)
        else:
            withdrawn.append((out["table"], out["row"], cid))

    # 从存活根沿健全边求可达闭包（结果中不留到不了的贡献节点）
    kept = set()
    stack = list(surviving_roots)
    while stack:
        x = stack.pop()
        if x in kept:
            continue
        kept.add(x)
        node = graph.contribs[x]
        for e in node.edges:
            if alive[e.child]:
                stack.append(e.child)

    new_contribs = []
    for c in graph_obj["contribs"]:
        if c["id"] not in kept:
            continue
        if c["type"] == "source":
            new_contribs.append(dict(c))
        else:
            new_contribs.append({
                "id": c["id"],
                "type": c["type"],
                "edges": [dict(e) for e in c["edges"] if e["child"] in kept],
            })

    surviving_ids = {(t, r) for (t, r) in graph.outputs if alive[graph.outputs[(t, r)]]}
    new_tables = []
    for t in graph_obj["tables"]:
        new_tables.append({
            "table_id": t["table_id"],
            "node_id": t["node_id"],
            "rows": [r for r in t["rows"] if (t["table_id"], r) in surviving_ids],
        })
    new_outputs = [dict(o) for o in graph_obj["outputs"]
                   if (o["table"], o["row"]) in surviving_ids]

    invalidation_hash = sha256_of(sorted(invalid_set))
    new_graph = {
        "kind": "graph",
        "version": 1,
        "graph_id": f"{graph_obj['graph_id']}--quarantine-{invalidation_hash[:12]}",
        "quarantine": {
            "marker_version": QUARANTINE_MARKER_VERSION,
            "origin_graph_id": graph_obj["graph_id"],
            "origin_graph_sha256": sha256_of(graph_obj),
            "invalidation_sha256": invalidation_hash,
            "withdrawn_outputs": [{"table": t, "row": r} for (t, r, _c) in withdrawn],
        },
        "nodes": [dict(n) for n in graph_obj["nodes"]],
        "tables": new_tables,
        "contribs": new_contribs,
        "outputs": new_outputs,
    }
    return new_graph, withdrawn, kept


# ---------------------------------------------------------------------------
# 交付记录流式校验/复制

def stream_records(records_path, graph, copy_sink):
    """逐行校验交付记录并把存活记录交给 copy_sink(lineno, obj, raw_line)。

    只在内存索引记录身份（table/row 字符串），不累积 data 载荷。
    与图所列 (table, row) 必须一一对应：缺、多、重复、顺序外的身份都是输入错误。
    """
    expected = list(graph.outputs)          # 原图 outputs 顺序即交付顺序基准
    expected_set = set(expected)
    seen = set()
    count = 0
    try:
        f = open(records_path, "r", encoding="utf-8")
    except OSError as e:
        raise InputError(f"无法读取交付记录 {records_path}: {e}")
    with f:
        for lineno, line in enumerate(f, start=1):
            if line.strip() == "":
                raise QuarantineError(f"交付记录 {records_path} 第 {lineno} 行：空行不是合法记录")
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise QuarantineError(f"交付记录 {records_path} 第 {lineno} 行：不是合法 JSON（{e}）")
            if not isinstance(obj, dict):
                raise QuarantineError(f"交付记录 {records_path} 第 {lineno} 行：记录必须是 JSON 对象")
            if set(obj) != {"table", "row", "data"}:
                raise QuarantineError(
                    f"交付记录 {records_path} 第 {lineno} 行：只能含 table/row/data 三个字段，"
                    f"得到 {sorted(obj)}")
            table, row = obj["table"], obj["row"]
            if not (isinstance(table, str) and isinstance(row, str) and table != "" and row != ""):
                raise QuarantineError(f"交付记录 {records_path} 第 {lineno} 行：table/row 必须是非空字符串")
            ident = (table, row)
            if ident not in expected_set:
                raise QuarantineError(
                    f"交付记录 {records_path} 第 {lineno} 行：身份 {ident} 不在原图输出行中")
            if ident in seen:
                raise QuarantineError(
                    f"交付记录 {records_path} 第 {lineno} 行：身份 {ident} 重复出现")
            seen.add(ident)
            count += 1
            copy_sink(lineno, obj, line)
    if seen != expected_set:
        missing = sorted(expected_set - seen)
        extra_note = f"；记录数 {count}，图输出行数 {len(expected_set)}"
        raise QuarantineError(
            f"交付记录与原图输出行未一一对应：缺少 {missing[:5]}{'...' if len(missing) > 5 else ''}{extra_note}")
    return count


# ---------------------------------------------------------------------------
# 撤下凭据

def build_withdrawals(graph_obj, sources_obj, invalid_set, invalid_raw, withdrawn,
                      reasons, root_reason_of, stats):
    """withdrawals.json：收件人可只凭原图 + 失效清单复查的撤下凭据。"""
    return {
        "kind": "quarantine-withdrawals",
        "version": 1,
        "binding": {
            "origin_graph_id": graph_obj["graph_id"],
            "origin_graph_sha256": sha256_of(graph_obj),
            "sources_sha256": sha256_of(sources_obj),
            "invalidation_sha256": sha256_of(sorted(invalid_set)),
        },
        "invalidations": [
            {"source_id": sid, "snapshot": snap, "row_id": row_id}
            for (sid, snap, row_id) in sorted(invalid_set)
        ],
        "invalidation_entries_input": len(invalid_raw),
        "withdrawals": [
            {
                "table": table,
                "row": row,
                "origin_root": root_cid,
                "reason": root_reason_of[root_cid],
            }
            for (table, row, root_cid) in withdrawn
        ],
        "reasons": reasons,
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# 端到端隔离与原子发布

def quarantine(sources_obj, graph_obj, records_path, invalidations_obj, out_dir):
    """执行隔离；返回 (out_dir, summary)。任何输入/写入错误都不发布残缺副本。"""
    sources, errs = validate_sources(sources_obj)
    if errs:
        raise QuarantineError("sources.json 本身不合法: " + "; ".join(errs))
    graph, errs, _warns = validate_graph(graph_obj, sources)
    if errs:
        raise QuarantineError("graph.json 本身不合法: " + "; ".join(errs))
    # 隔离要求每条图列输出行都有贡献根（含最终会被撤下的记录）
    missing = [(t, r) for t in graph.tables for r in graph.tables[t]["rows"]
               if (t, r) not in graph.outputs]
    if missing:
        raise QuarantineError(
            f"原图存在没有贡献根的表行，无法隔离（每条交付记录都必须有贡献根）: {missing[:5]}")

    invalid_set = parse_invalidations(invalidations_obj, sources)

    # 失效叶子（同一三元组可能被多个 source 贡献节点引用，全部命中）
    invalid_atoms = {
        cid for cid, node in graph.contribs.items()
        if node.type == "source"
        and (node.source_key[0], node.source_key[1], node.row_id) in invalid_set
    }
    alive = evaluate_liveness(graph, invalid_atoms)

    new_graph, withdrawn, kept = build_quarantine_graph(graph_obj, graph, alive, invalid_set)

    # 撤下理由 DAG（懒收集：只覆盖从撤下根可达的失效节点，共享节点只存一份）
    withdrawn_roots = [cid for (_t, _r, cid) in withdrawn]
    reasons, root_reason_of = build_reason_graph(graph, alive, withdrawn_roots)

    surviving_idents = {(o["table"], o["row"]) for o in new_graph["outputs"]}

    out = os.path.abspath(out_dir)
    parent = os.path.dirname(out) or "."
    os.makedirs(parent, exist_ok=True)
    if os.path.lexists(out):
        raise QuarantineError(f"输出目录已存在，拒绝改写: {out}")
    tmp = tempfile.mkdtemp(prefix=".dq-quarantine-", dir=parent)
    n_records = 0
    n_kept_rows = 0
    try:
        rows_path = os.path.join(tmp, "rows.jsonl")
        with open(rows_path, "w", encoding="utf-8") as rows_f:
            def sink(lineno, obj, raw_line):
                nonlocal n_records, n_kept_rows
                n_records += 1
                if (obj["table"], obj["row"]) in surviving_idents:
                    # 原样复制载荷：用已解析对象重序列化（键序/内容等价），
                    # 并保证规范的单行 JSON + 换行；data 字段不解释、不改写
                    rows_f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    n_kept_rows += 1
            stream_records(records_path, graph, sink)
        if n_kept_rows != len(surviving_idents):
            raise QuarantineError(
                f"内部一致性错误：存活图输出 {len(surviving_idents)} 行，实际复制 {n_kept_rows} 行")

        used_tris = {
            (graph.contribs[c].source_key[0], graph.contribs[c].source_key[1],
             graph.contribs[c].row_id)
            for c in invalid_atoms
        }
        stats = {
            "records_total": n_records,
            "records_withdrawn": len(withdrawn),
            "records_kept": n_kept_rows,
            "invalidations_distinct": len(invalid_set),
            "invalidations_unused": len(invalid_set - used_tris),
            "origin_contrib_nodes": len(graph_obj["contribs"]),
            "kept_contrib_nodes": len(kept),
            "origin_contrib_edges": sum(len(c.get("edges", [])) for c in graph_obj["contribs"]),
            "kept_contrib_edges": sum(len(c.get("edges", [])) for c in new_graph["contribs"]),
            "reason_nodes": len(reasons),
        }
        withdrawals = build_withdrawals(
            graph_obj, sources_obj, invalid_set, invalidations_obj, withdrawn,
            reasons, root_reason_of, stats)

        with open(os.path.join(tmp, "graph.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(new_graph, ensure_ascii=False, indent=2) + "\n")
        with open(os.path.join(tmp, "withdrawals.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(withdrawals, ensure_ascii=False, indent=2) + "\n")

        if os.path.lexists(out):  # rename 前再次确认，防止并发占位
            raise QuarantineError(f"输出目录已存在，拒绝改写: {out}")
        os.replace(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    summary = {
        "target": "quarantine",
        "valid": True,
        "out_dir": out,
        "files": ["rows.jsonl", "graph.json", "withdrawals.json"],
        "stats": stats,
    }
    return out, summary
