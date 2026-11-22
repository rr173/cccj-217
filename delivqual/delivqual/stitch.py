"""stitch-lineage：把分阶段落盘的加工图接成一张终端报表可直接使用的行级贡献图。

背景：采购数据分阶段归档，下游阶段只落盘中间表，其图用仅作输入的贡献叶
``{"type": "import", "id", "scan", "row"}`` 表示“本阶段 scan 读到的上游行”。
本命令沿清单中的 scan→（生产阶段, 输出表）绑定，把每个 import 叶解析到上游表中
**恰好对应**的已登记贡献根，接成一张从终端输出行可一路追踪到真实原行的图。

本模块只处理血缘，不重跑任何阶段的业务计算，也不读取/交付报表载荷：

  - 中间表不是新的原始来源：import 叶被替换为上游贡献根，绝不变造出 source 原子，
    也不登记替代原来源的许可；真实来源仍是全阶段共用的同一份 sources.json。
  - 同一 (生产表, 行) 被多条下游路径引用时，只接入同一棵上游子 DAG 一次（共享，
    不按路径拷贝上游树）；同一阶段内重复引用的贡献节点继续共享；不同阶段的同名
    局部 id 用“阶段名::局部id”分开。
  - 共同贡献（all）与充分替代（any）按实际被读取那一行的凭据原样接通：不择一保留、
    不合并两条凭据；只剩一条替代的 quarantine 图仍接通那条路径，不找回已撤下的边。
  - 加工图接缝用新的无权限含义的 stitch 节点表达（scan→stitch→生产表节点），
    不凭空增加 project/join/aggregate；原加工步骤与贡献边的 via_node/via_row 原样保留。
  - 路径操作许可沿真实加工边继承：上游聚合 + 下游连接 ⇒ 同一原行同时继承
    aggregate 与 join，不会因中间表落盘而断成 raw。

结果图沿用标准 graph.json 结构（不含 import 叶），可直接交给
``dq validate`` / ``dq plan`` / ``dq verify-plan``，只依赖它自己与原 sources.json。

发布前检查所读阶段的内容摘要（SHA-256）、接缝绑定、跨阶段依赖环与输入/输出图
结构；任何无法接通的位置都以 StitchError 指出，不写结果文件。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile

from .common import load_json, sha256_of
from .model import validate_graph, validate_sources

STITCH_MARKER_VERSION = 1
# 全局 id 前缀（阶段名不得含该分隔符，从结构上保证跨阶段同名局部 id 不冲突）
PREFIX_SEP = "::"

MANIFEST_REQUIRED = {"kind", "version", "terminal", "stages", "bindings"}
STAGE_REQUIRED = {"name", "graph", "sha256"}
BINDING_REQUIRED = {"stage", "scan", "producer_stage", "producer_table"}


class StitchError(Exception):
    """清单/阶段图/接缝不满足接续契约（无法接成完整血缘）。"""


# ---------------------------------------------------------------------------
# 清单解析与摘要核对

def parse_manifest(manifest_obj):
    """校验清单形状，返回 (terminal, [(name, graph_path, sha256)], [(stage, scan, producer_stage, producer_table)])。"""
    if not isinstance(manifest_obj, dict) or manifest_obj.get("kind") != "stitch-manifest":
        raise StitchError("清单顶层必须是 {\"kind\": \"stitch-manifest\", ...}")
    if not isinstance(manifest_obj.get("version"), int) or isinstance(manifest_obj["version"], bool):
        raise StitchError("清单 version 必须是整数")
    unknown = set(manifest_obj) - MANIFEST_REQUIRED
    if unknown:
        raise StitchError(f"清单出现未知字段 {sorted(unknown)}，契约只允许 {sorted(MANIFEST_REQUIRED)}")
    terminal = manifest_obj.get("terminal")
    if not isinstance(terminal, str) or terminal == "":
        raise StitchError("terminal 必须是非空字符串（唯一终端阶段名）")

    raw_stages = manifest_obj.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise StitchError("stages 必须是非空数组")
    stages = []
    seen_names = set()
    for i, st in enumerate(raw_stages):
        where = f"stages[{i}]"
        if not isinstance(st, dict):
            raise StitchError(f"{where}: 必须是对象")
        if set(st) != STAGE_REQUIRED:
            raise StitchError(f"{where}: 只能含 {sorted(STAGE_REQUIRED)}，得到 {sorted(st)}")
        name, path, digest = st["name"], st["graph"], st["sha256"]
        if not (isinstance(name, str) and isinstance(path, str) and isinstance(digest, str)
                and name and path and digest):
            raise StitchError(f"{where}: name/graph/sha256 都必须是非空字符串")
        if PREFIX_SEP in name:
            raise StitchError(f"{where}: 阶段名 {name!r} 不得含 {PREFIX_SEP!r}（全局 id 分隔符）")
        if name in seen_names:
            raise StitchError(f"{where}: 阶段名重复: {name!r}")
        seen_names.add(name)
        if not isinstance(digest, str) or len(digest) != 64 or not _is_hex(digest):
            raise StitchError(f"{where}: sha256 必须是 64 位十六进制字符串")
        stages.append((name, path, digest.lower()))
    if terminal not in seen_names:
        raise StitchError(f"terminal {terminal!r} 不在 stages 清单中")

    raw_bindings = manifest_obj.get("bindings")
    if not isinstance(raw_bindings, list):
        raise StitchError("bindings 必须是数组（无接缝时可为空数组）")
    bindings = []
    seen_binding = set()
    for i, b in enumerate(raw_bindings):
        where = f"bindings[{i}]"
        if not isinstance(b, dict):
            raise StitchError(f"{where}: 必须是对象")
        if set(b) != BINDING_REQUIRED:
            raise StitchError(f"{where}: 只能含 {sorted(BINDING_REQUIRED)}，得到 {sorted(b)}")
        vals = tuple(b[k] for k in ("stage", "scan", "producer_stage", "producer_table"))
        if not all(isinstance(v, str) and v for v in vals):
            raise StitchError(f"{where}: 四个字段都必须是非空字符串")
        key = (b["stage"], b["scan"])
        if key in seen_binding:
            raise StitchError(f"{where}: 扫描节点 {key} 被重复绑定（一个扫描节点只能有一个目标）")
        seen_binding.add(key)
        bindings.append(vals)
    return terminal, stages, bindings


def _is_hex(s):
    try:
        int(s, 16)
    except ValueError:
        return False
    return True


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 阶段载入：摘要核对 + 结构校验 + 接缝局部检查

def load_stages(sources_obj, manifest_dir, stages):
    """载入并校验每个阶段图；返回 {name: StageBundle}。

    每个阶段图都按阶段图规则（允许 import 输入叶、允许 scan 待接续）做完整结构校验。
    文件字节 SHA-256 必须与清单一致，否则按无法接通处理（防止同名图被替换）。
    """
    sources, errs = validate_sources(sources_obj)
    if errs:
        raise StitchError("sources.json 本身不合法: " + "; ".join(errs))

    loaded = {}
    for name, rel_path, expect_digest in stages:
        path = rel_path if os.path.isabs(rel_path) else os.path.join(manifest_dir, rel_path)
        try:
            actual = _sha256_file(path)
        except OSError as e:
            raise StitchError(f"阶段 {name!r} 的图文件无法读取 {path}: {e}")
        if actual != expect_digest:
            raise StitchError(
                f"阶段 {name!r} 内容摘要不符：清单登记 {expect_digest}，"
                f"实际 {path} 为 {actual}（文件被替换或清单过期）")
        graph_obj = load_json(path)
        graph, gerrs, gwarns = validate_graph(graph_obj, sources, allow_import=True)
        if gerrs:
            raise StitchError(f"阶段 {name!r} 图结构不合法: " + "; ".join(gerrs))
        # 表行缺贡献根会让接缝无法定位，必须拒绝；不可达贡献节点在阶段图中合法
        # （某阶段可携带自身输出用不到的表达式），只在阶段摘要中保留该警告。
        blocking = [w for w in gwarns if "没有登记的贡献表达式" in w]
        if blocking:
            raise StitchError(f"阶段 {name!r} 存在没有贡献根的表行，拒绝接续: " + "; ".join(blocking))
        loaded[name] = {
            "name": name,
            "path": path,
            "sha256": actual,
            "obj": graph_obj,
            "graph": graph,
            "warnings": gwarns,
        }
    return loaded, sources


# ---------------------------------------------------------------------------
# 接缝解析与跨阶段依赖环

def resolve_seams(loaded, bindings):
    """核对每条绑定可接通，返回 (binding_map, seam_targets, stage_deps)。

    binding_map: (stage, scan) -> (producer_stage, producer_table, table_node_id)
    seam_targets: [(consumer_stage, scan, producer_stage, producer_table, row_ids)]，
                  row_ids 为该扫描下实际被 import 读取的上游行（去重保序）
    stage_deps: {consumer_stage: {producer_stage}}（用于跨阶段依赖环检测）
    """
    binding_map = {}
    stage_deps = {name: set() for name in loaded}
    for (stage, scan, producer_stage, producer_table) in bindings:
        where = f"绑定 ({stage!r}, {scan!r})"
        if stage not in loaded:
            raise StitchError(f"{where}: 消费阶段 {stage!r} 不在 stages 清单中")
        if producer_stage not in loaded:
            raise StitchError(f"{where}: 生产阶段 {producer_stage!r} 不在 stages 清单中")
        consumer = loaded[stage]["graph"]
        if scan not in consumer.nodes:
            raise StitchError(f"{where}: 消费阶段 {stage!r} 中不存在扫描节点 {scan!r}")
        if consumer.nodes[scan].op != "scan":
            raise StitchError(f"{where}: {stage!r} 的 {scan!r} 不是 scan 节点")
        producer = loaded[producer_stage]["graph"]
        if producer_table not in producer.tables:
            raise StitchError(
                f"{where}: 绑定找不到目标表——生产阶段 {producer_stage!r} 没有输出表 {producer_table!r}")
        table_node = producer.tables[producer_table]["node_id"]
        binding_map[(stage, scan)] = (producer_stage, producer_table, table_node)
        stage_deps[stage].add(producer_stage)

    # 每个 import 叶必须能经绑定定位；同一 scan 只允许一个目标（绑定唯一性已在清单层保证）
    seam_targets = []
    bound_scans = set(binding_map)
    scans_with_imports = {}     # (stage, scan) -> [(import_cid, up_row)]
    for name, bundle in loaded.items():
        for c in bundle["obj"]["contribs"]:
            if c.get("type") == "import":
                scans_with_imports.setdefault((name, c["scan"]), []).append((c["id"], c["row"]))
    for key, leaves in scans_with_imports.items():
        name, scan = key
        if key not in bound_scans:
            raise StitchError(
                f"扫描未绑定：阶段 {name!r} 的 scan {scan!r} 被 {len(leaves)} 个 import 叶引用"
                f"（如 {leaves[0][0]!r}），但清单没有为它指定生产阶段/输出表")
        producer_stage, producer_table, _node = binding_map[key]
        producer = loaded[producer_stage]["graph"]
        table_rows = set(producer.tables[producer_table]["rows"])
        rows_in_order = []
        for cid, up_row in leaves:
            if up_row not in table_rows:
                raise StitchError(
                    f"无法接通：阶段 {name!r} import 叶 {cid!r} 读到的行 {up_row!r} "
                    f"不在生产阶段 {producer_stage!r} 的目标表 {producer_table!r} 中")
            root = producer.outputs.get((producer_table, up_row))
            if root is None:
                raise StitchError(
                    f"无法接通：行 {up_row!r} 虽列在生产阶段 {producer_stage!r} 表 {producer_table!r} 中，"
                    f"却没有已登记的贡献根（不能作为接续来源）")
            if up_row not in rows_in_order:
                rows_in_order.append(up_row)
        seam_targets.append((name, scan, producer_stage, producer_table, rows_in_order))
    # 有绑定但没有任何 import 引用：清单接缝与图内容不一致，拒绝而不是静默忽略
    for key in sorted(bound_scans - set(scans_with_imports)):
        raise StitchError(
            f"绑定悬空：清单为扫描 {key} 指定了目标，但阶段 {key[0]!r} 图中没有 import 叶引用该 scan")
    _check_stage_cycle(stage_deps)
    return binding_map, seam_targets, stage_deps


def _check_stage_cycle(stage_deps):
    """Kahn 判跨阶段依赖环；环上的阶段永远无法定位到真实原行。"""
    indeg = {n: sum(1 for src in stage_deps if n in stage_deps[src]) for n in stage_deps}
    stack = [n for n, d in indeg.items() if d == 0]
    seen = 0
    while stack:
        x = stack.pop()
        seen += 1
        for m in stage_deps[x]:
            indeg[m] -= 1
            if indeg[m] == 0:
                stack.append(m)
    if seen != len(stage_deps):
        cyc = sorted(n for n, d in indeg.items() if d > 0)
        raise StitchError(f"跨阶段依赖存在环，涉及阶段: {cyc}（环上的中间表无法接通到真实原行）")


def reachable_stages(terminal, stage_deps):
    """从终端沿“消费→生产”边求实际参与接续的阶段闭包（保序确定性由调用方排序处理）。"""
    seen = set()
    stack = [terminal]
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        stack.extend(stage_deps[x])
    return seen


# ---------------------------------------------------------------------------
# 合并图构造

def gid(stage, local_id):
    """局部 id -> 结果图全局 id（不同阶段的同名局部 id 必须分开）。"""
    return f"{stage}{PREFIX_SEP}{local_id}"


def build_merged_graph(loaded, sources_obj, terminal, binding_map, seam_targets,
                       stage_deps, manifest_obj):
    """把终端阶段及其全部上游阶段接成一张标准贡献图（不含 import 叶）。"""
    active = reachable_stages(terminal, stage_deps)
    # 确定性阶段顺序：终端在前，其余按阶段名排序（只影响列表顺序，不影响身份）
    ordered = [terminal] + sorted(n for n in active if n != terminal)

    # ---- 加工节点：原节点/原边全部保留并加阶段前缀；每条接缝加一个 stitch 节点 ----
    merged_nodes = []
    merged_node_set = set()
    for name in ordered:
        for n in loaded[name]["obj"]["nodes"]:
            nid = gid(name, n["node_id"])
            if nid in merged_node_set:
                raise StitchError(f"内部错误：加工节点全局 id 冲突 {nid}")
            merged_node_set.add(nid)
            inputs = [gid(name, i) for i in n["inputs"]]
            merged_nodes.append({"node_id": nid, "op": n["op"], "inputs": inputs,
                                 "_stage": name, "_local": n["node_id"]})
    # 接缝：scan -> stitch -> 生产表节点（stitch 无权限含义，不新增投影/连接/聚合）
    seam_summary = []
    for (stage, scan, producer_stage, producer_table, rows_in_order) in seam_targets:
        if stage not in active:
            continue
        _ps, _pt, table_node = binding_map[(stage, scan)]
        stitch_id = gid(stage, f"stitch-for-{scan}")
        if stitch_id in merged_node_set:
            raise StitchError(f"内部错误：接缝节点 id 冲突 {stitch_id}")
        merged_node_set.add(stitch_id)
        merged_nodes.append({"node_id": stitch_id, "op": "stitch",
                             "inputs": [gid(producer_stage, table_node)],
                             "_stage": stage, "_local": f"stitch-for-{scan}"})
        # 把消费 scan 的输入改写为该 stitch（阶段图中 scan 无输入）
        scan_global = gid(stage, scan)
        for n in merged_nodes:
            if n["node_id"] == scan_global:
                if n["inputs"]:
                    raise StitchError(f"内部错误：scan {scan_global} 已存在输入 {n['inputs']}")
                n["inputs"] = [stitch_id]
        seam_summary.append({
            "consumer_stage": stage,
            "scan": scan,
            "producer_stage": producer_stage,
            "producer_table": producer_table,
            "producer_node": gid(producer_stage, table_node),
            "stitch_node": stitch_id,
            "rows_read": list(rows_in_order),
        })

    # ---- 贡献表达式：从终端输出根出发，遇 import 叶递归替换为上游贡献根（带记忆化共享）----
    merged_contribs = {}     # 全局 cid -> 可序列化对象（不含 import 叶）
    memo = {}                # (stage, local_cid) -> 全局 cid，保证同一上游树只接一次

    def emit(stage, local_cid):
        key = (stage, local_cid)
        if key in memo:
            return memo[key]
        bundle = loaded[stage]
        local = bundle["graph"].contribs.get(local_cid)
        if local is None:
            raise StitchError(f"内部错误：阶段 {stage!r} 缺贡献节点 {local_cid!r}")
        global_id = gid(stage, local_cid)
        memo[key] = global_id  # 先占位，DAG 无环（各自阶段已校验），天然防重入
        if local.type == "source":
            merged_contribs[global_id] = {
                "id": global_id, "type": "source",
                "source_id": local.source_key[0], "snapshot": local.source_key[1],
                "row_id": local.row_id,
            }
        elif local.type == "import":
            # 经绑定定位到上游表中恰好对应的已登记贡献根；绝不按全局同名行猜测
            producer_stage, producer_table, _node = binding_map[(stage, local.scan)]
            up_root = loaded[producer_stage]["graph"].outputs[(producer_table, local.row_id)]
            target_global = emit(producer_stage, up_root)
            # import 叶本身不产生新节点：所有指向它的边都改指向上游根
            memo[key] = target_global
            if global_id in merged_contribs:
                del merged_contribs[global_id]
            return target_global
        else:  # all / any：边原样保留（via_node/via_row 不改写），子节点递归接通
            edges = []
            for e in local.edges:
                child_global = emit(stage, e.child)
                edges.append({
                    "child": child_global,
                    "via_node": gid(stage, e.via_node),
                    "via_row": e.via_row,
                })
            merged_contribs[global_id] = {"id": global_id, "type": local.type, "edges": edges}
        return memo[key]

    # 终端表/行/根完全不变（只是根的 id 加阶段前缀，表 id 与行 id 原样）
    terminal_bundle = loaded[terminal]
    merged_tables = []
    merged_outputs = []
    for t in terminal_bundle["obj"]["tables"]:
        merged_tables.append({"table_id": t["table_id"],
                              "node_id": gid(terminal, t["node_id"]),
                              "rows": list(t["rows"])})
    for o in terminal_bundle["obj"]["outputs"]:
        root_global = emit(terminal, o["expr"])
        merged_outputs.append({"table": o["table"], "row": o["row"], "expr": root_global})
    # 终端阶段每条列出的表行都必须有贡献根（缺根的终端报表无法交付完整血缘）
    for t in terminal_bundle["obj"]["tables"]:
        for r in t["rows"]:
            if (t["table_id"], r) not in terminal_bundle["graph"].outputs:
                raise StitchError(
                    f"终端阶段 {terminal!r} 表 {t['table_id']!r} 行 {r!r} 没有贡献根，"
                    "无法接成完整血缘")

    # 确定性输出顺序：按全局 id 排序，保证同样输入逐字节一致
    contrib_list = [merged_contribs[k] for k in sorted(merged_contribs)]
    node_list = sorted(merged_nodes, key=lambda n: n["node_id"])
    for n in node_list:
        n.pop("_stage", None)
        n.pop("_local", None)

    # ---- 接续标记：实际接续的阶段摘要 + 扫描所绑定的生产表（无权限含义，仅供回查）----
    quarantine_stages = []
    for name in ordered:
        marker = loaded[name]["obj"].get("quarantine")
        if isinstance(marker, dict):
            quarantine_stages.append({
                "stage": name,
                "origin_graph_id": marker.get("origin_graph_id"),
                "origin_graph_sha256": marker.get("origin_graph_sha256"),
                "invalidation_sha256": marker.get("invalidation_sha256"),
            })
    stage_summary = [{
        "name": name,
        "graph_id": loaded[name]["obj"]["graph_id"],
        "graph_sha256": loaded[name]["sha256"],
        "quarantine": loaded[name]["obj"].get("quarantine") is not None,
    } for name in ordered]
    marker = {
        "marker_version": STITCH_MARKER_VERSION,
        "terminal": terminal,
        "manifest_sha256": sha256_of(manifest_obj),
        "sources_sha256": sha256_of(sources_obj),
        "id_prefix_separator": PREFIX_SEP,
        "stages": stage_summary,
        "seams": sorted(seam_summary, key=lambda s: (s["consumer_stage"], s["scan"])),
        "quarantine_stages": quarantine_stages,
    }
    merged = {
        "kind": "graph",
        "version": 1,
        "graph_id": f"stitched-of-{terminal}-{marker['manifest_sha256'][:12]}",
        "stitch": marker,
        "nodes": node_list,
        "tables": merged_tables,
        "contribs": contrib_list,
        "outputs": merged_outputs,
    }
    # 任一接入阶段是 quarantine 图时，结果图同时带 quarantine 标记，使单边 any 合法
    if quarantine_stages:
        merged["quarantine"] = {
            "marker_version": STITCH_MARKER_VERSION,
            "origin_graph_id": f"stitched-of-{terminal}",
            "origin_graph_sha256": marker["manifest_sha256"],
            "invalidation_sha256": sha256_of(sorted(q["invalidation_sha256"]
                                                    for q in quarantine_stages)),
            "stitched_from": quarantine_stages,
        }
    return merged, marker


# ---------------------------------------------------------------------------
# 端到端：接续 + 自校验 + 原子发布

def stitch_lineage(sources_obj, manifest_path, out_path):
    """执行接续；成功返回 (out_path, summary)。任何错误都不发布残缺结果、不改输入。"""
    manifest_obj = load_json(manifest_path)
    terminal, stages, bindings = parse_manifest(manifest_obj)
    manifest_dir = os.path.dirname(os.path.abspath(manifest_path))
    loaded, sources = load_stages(sources_obj, manifest_dir, stages)
    binding_map, seam_targets, stage_deps = resolve_seams(loaded, bindings)
    merged, marker = build_merged_graph(loaded, sources_obj, terminal, binding_map,
                                        seam_targets, stage_deps, manifest_obj)

    # 发布前整体自校验：结果必须是一张零 error 零 warning 的标准图（不含 import 叶）
    graph, errs, warns = validate_graph(merged, sources, allow_import=False)
    if errs:
        raise StitchError("接续结果未通过标准图校验: " + "; ".join(errs))
    if warns:
        raise StitchError("接续结果存在结构警告，拒绝发布: " + "; ".join(warns))

    # 加工依赖连通性复核：每个消费 scan 必须能沿 stitch 找到实际生产该表的节点
    node_index = {n["node_id"]: n for n in merged["nodes"]}
    for seam in marker["seams"]:
        scan_node = node_index[gid(seam["consumer_stage"], seam["scan"])]
        if len(scan_node["inputs"]) != 1:
            raise StitchError(f"内部错误：scan {scan_node['node_id']} 未唯一接通 stitch")
        stitch_node = node_index[scan_node["inputs"][0]]
        if stitch_node["op"] != "stitch" or stitch_node["inputs"] != [seam["producer_node"]]:
            raise StitchError(
                f"加工依赖未连通：scan {scan_node['node_id']} 无法沿 stitch 到达生产节点 "
                f"{seam['producer_node']}")

    out_abs = os.path.abspath(out_path)
    parent = os.path.dirname(out_abs) or "."
    os.makedirs(parent, exist_ok=True)
    if os.path.lexists(out_abs):
        raise StitchError(f"输出文件已存在，拒绝改写: {out_abs}")
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".dq-stitch-", suffix=".json", dir=parent)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if os.path.lexists(out_abs):  # rename 前再次确认，防并发占位
            raise StitchError(f"输出文件已存在，拒绝改写: {out_abs}")
        os.replace(tmp_path, out_abs)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    summary = {
        "target": "stitch-lineage",
        "valid": True,
        "out": out_abs,
        "terminal": terminal,
        "stats": {
            "stages_listed": len(stages),
            "stages_stitched": len(marker["stages"]),
            "seams": len(marker["seams"]),
            "processing_nodes": len(merged["nodes"]),
            "contrib_nodes": len(merged["contribs"]),
            "contrib_edges": sum(len(c.get("edges", [])) for c in merged["contribs"]),
            "terminal_tables": len(merged["tables"]),
            "terminal_rows": len(merged["outputs"]),
            "quarantine_stages": len(marker["quarantine_stages"]),
        },
    }
    return out_abs, summary
