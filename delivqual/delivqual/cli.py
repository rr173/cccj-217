"""命令行入口：validate / plan / explain-row / verify-plan / build-totals /
quarantine / stitch-lineage。

退出码：
  0  成功（validate 通过 / plan 可行 / verify 有效 / explain / build-totals 正常）
  10 输入校验错误（含 build-totals 的绑定不符、重复行号、坏记录；
     quarantine/stitch-lineage 的契约不符与无法接通的接缝）
  11 不可行（plan 给出冲突证书）
  12 unknown（预算耗尽）
  13 verify-plan 拒绝
  2  用法或 IO 错误
  （quarantine / stitch-lineage 同此约定：输入契约不符退出 10，IO 错误退出 2，成功退出 0）
"""
from __future__ import annotations

import argparse
import json
import sys

from .common import InputError, dump_json, load_json
from .model import validate_graph, validate_request, validate_sources


def _load_all(args, need_request=True):
    """载入并校验全部输入；出错时打印并退出。"""
    try:
        sources_obj = load_json(args.sources)
        graph_obj = load_json(args.graph)
        request_obj = load_json(args.request) if need_request else None
    except InputError as e:
        print(f"输入错误: {e}", file=sys.stderr)
        sys.exit(2)
    sources, errs = validate_sources(sources_obj)
    if errs:
        _print_validation("sources", errs, [])
        sys.exit(10)
    graph, errs, warns = validate_graph(graph_obj, sources)
    if errs:
        _print_validation("graph", errs, warns)
        sys.exit(10)
    request = None
    if need_request:
        request, errs = validate_request(request_obj, graph)
        if errs:
            _print_validation("request", errs, [])
            sys.exit(10)
    return sources_obj, graph_obj, request_obj, sources, graph, request


def _print_validation(what, errors, warnings):
    out = {"target": what, "valid": not errors, "errors": errors, "warnings": warnings}
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_validate(args):
    try:
        sources_obj = load_json(args.sources)
        graph_obj = load_json(args.graph)
        request_obj = load_json(args.request) if args.request else None
    except InputError as e:
        print(f"输入错误: {e}", file=sys.stderr)
        return 2
    sources, errs_s = validate_sources(sources_obj)
    graph = None
    errs_g, warns_g = [], []
    if not errs_s:
        graph, errs_g, warns_g = validate_graph(graph_obj, sources)
    errs_r = []
    if request_obj is not None and not errs_g:
        _request, errs_r = validate_request(request_obj, graph)
    result = {
        "valid": not (errs_s or errs_g or errs_r),
        "sources": {"errors": errs_s},
        "graph": {"errors": errs_g, "warnings": warns_g},
    }
    if request_obj is not None:
        result["request"] = {"errors": errs_r}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 10


def cmd_plan(args):
    sources_obj, graph_obj, request_obj, sources, graph, request = _load_all(args)
    from .solver import plan
    p = plan(sources, graph, request,
             budget_conflicts=args.budget_conflicts,
             budget_seconds=args.budget_seconds,
             sources_obj=sources_obj, graph_obj=graph_obj, request_obj=request_obj)
    text = dump_json(p, args.out)
    if not args.out:
        print(text)
    else:
        print(f"plan 已写入 {args.out}: status={p['status']}")
    return {"feasible": 0, "infeasible": 11, "unknown": 12}[p["status"]]


def cmd_verify(args):
    sources_obj, graph_obj, request_obj, sources, graph, request = _load_all(args)
    try:
        plan_obj = load_json(args.plan)
    except InputError as e:
        print(f"输入错误: {e}", file=sys.stderr)
        return 2
    from .verify import verify_plan
    verdict = verify_plan(sources, graph, request, plan_obj,
                          sources_obj=sources_obj, graph_obj=graph_obj,
                          request_obj=request_obj,
                          budget_seconds=args.budget_seconds or 30.0)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return {"valid": 0, "invalid": 13, "unknown": 12}[verdict["verdict"]]


def cmd_explain(args):
    sources_obj, graph_obj, request_obj, sources, graph, request = _load_all(args)
    from .explain import explain_row
    lines = explain_row(sources, graph, request, args.table, args.row,
                        budget_seconds=args.budget_seconds or 30.0)
    print("\n".join(lines))
    return 0


def cmd_build_totals(args):
    """从两份明细构建部门汇总表 + 行级贡献图（原子交付到此前不存在的目录）。"""
    from .buildtotals import BuildError, build_totals, deliver, load_jsonl
    try:
        sources_obj = load_json(args.sources)
        purchase_records = load_jsonl(args.purchase, "采购明细")
        attribution_records = load_jsonl(args.attribution, "归属明细")
    except InputError as e:
        print(f"输入错误: {e}", file=sys.stderr)
        return 2
    graph_id = args.graph_id or f"totals-of-{args.purchase_source}-{args.attribution_source}"
    try:
        totals_rows, graph_obj, stats = build_totals(
            sources_obj, purchase_records, attribution_records,
            (args.purchase_source, args.purchase_snapshot),
            (args.attribution_source, args.attribution_snapshot),
            graph_id)
        out_dir = deliver(args.out_dir, totals_rows, graph_obj)
    except BuildError as e:
        print(json.dumps({"target": "build-totals", "valid": False, "errors": [str(e)]},
                         ensure_ascii=False, indent=2))
        return 10
    except OSError as e:
        print(f"写入失败: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"target": "build-totals", "valid": True, "out_dir": out_dir,
                      "files": ["totals.jsonl", "graph.json"], "stats": stats},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_quarantine(args):
    """隔离交付包：按失效原行清单撤下失去充分凭据的记录，原子发布可替换旧包的副本。"""
    from .quarantine import QuarantineError, quarantine
    try:
        sources_obj = load_json(args.sources)
        graph_obj = load_json(args.graph)
        invalidations_obj = load_json(args.invalidations)
    except InputError as e:
        print(f"输入错误: {e}", file=sys.stderr)
        return 2
    try:
        out_dir, summary = quarantine(sources_obj, graph_obj, args.records,
                                      invalidations_obj, args.out_dir)
    except QuarantineError as e:
        print(json.dumps({"target": "quarantine", "valid": False, "errors": [str(e)]},
                         ensure_ascii=False, indent=2))
        return 10
    except InputError as e:
        print(f"输入错误: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"写入失败: {e}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_stitch(args):
    """把分阶段落盘的加工图沿清单接成终端报表的一张行级贡献图（只处理血缘）。"""
    from .stitch import StitchError, stitch_lineage
    try:
        sources_obj = load_json(args.sources)
    except InputError as e:
        print(f"输入错误: {e}", file=sys.stderr)
        return 2
    try:
        out_path, summary = stitch_lineage(sources_obj, args.manifest, args.out)
    except StitchError as e:
        print(json.dumps({"target": "stitch-lineage", "valid": False, "errors": [str(e)]},
                         ensure_ascii=False, indent=2))
        return 10
    except InputError as e:
        print(f"输入错误: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"写入失败: {e}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="delivqual",
        description="离线数据交付资格核验器：全局一致的来源选项与义务，或可复查的冲突。")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, request=True):
        p.add_argument("--sources", required=True)
        p.add_argument("--graph", required=True)
        if request:
            p.add_argument("--request", required=True)

    p1 = sub.add_parser("validate", help="校验输入结构、引用、环与原子登记")
    p1.add_argument("--sources", required=True)
    p1.add_argument("--graph", required=True)
    p1.add_argument("--request")
    p1.set_defaults(fn=cmd_validate)

    p2 = sub.add_parser("plan", help="求解全局一致交付计划或冲突证书")
    common(p2)
    p2.add_argument("--out", help="plan JSON 输出路径（缺省打印到 stdout）")
    p2.add_argument("--budget-conflicts", type=int, default=None)
    p2.add_argument("--budget-seconds", type=float, default=None)
    p2.set_defaults(fn=cmd_plan)

    p3 = sub.add_parser("verify-plan", help="独立核实 plan 的全部合同")
    common(p3)
    p3.add_argument("--plan", required=True)
    p3.add_argument("--budget-seconds", type=float, default=30.0)
    p3.set_defaults(fn=cmd_verify)

    p4 = sub.add_parser("explain-row", help="解释单个输出行的交付情况")
    common(p4)
    p4.add_argument("--table", required=True)
    p4.add_argument("--row", required=True)
    p4.add_argument("--budget-seconds", type=float, default=30.0)
    p4.set_defaults(fn=cmd_explain)

    p5 = sub.add_parser(
        "build-totals",
        help="从采购/归属两份明细生成部门汇总 totals.jsonl 与匹配的 graph.json")
    p5.add_argument("--sources", required=True, help="已登记源快照的 sources.json")
    p5.add_argument("--purchase", required=True, help="采购明细 JSONL（row_id, join_key, invoice_id, cents）")
    p5.add_argument("--attribution", required=True, help="归属明细 JSONL（row_id, join_key, department）")
    p5.add_argument("--purchase-source", required=True)
    p5.add_argument("--purchase-snapshot", required=True)
    p5.add_argument("--attribution-source", required=True)
    p5.add_argument("--attribution-snapshot", required=True)
    p5.add_argument("--out-dir", required=True, help="目标输出目录（必须此前不存在）")
    p5.add_argument("--graph-id", help="生成图的 graph_id（缺省按绑定确定性生成）")
    p5.set_defaults(fn=cmd_build_totals)

    p6 = sub.add_parser(
        "quarantine",
        help="按失效原行清单隔离交付包：撤下失去充分凭据的记录，发布 rows.jsonl/graph.json/withdrawals.json")
    p6.add_argument("--sources", required=True, help="原 sources.json（只读，不修改）")
    p6.add_argument("--graph", required=True, help="原 graph.json（只读）")
    p6.add_argument("--records", required=True,
                    help="交付记录 JSONL，每行 {table, row, data}，与图输出行一一对应")
    p6.add_argument("--invalidations", required=True,
                    help="失效原行清单 JSON 数组，元素 {source_id, snapshot, row_id}")
    p6.add_argument("--out-dir", required=True, help="目标输出目录（必须此前不存在）")
    p6.set_defaults(fn=cmd_quarantine)

    p7 = sub.add_parser(
        "stitch-lineage",
        help="按接续清单把分阶段图接成终端报表的一张行级贡献图（不重跑业务计算、不碰报表载荷）")
    p7.add_argument("--sources", required=True, help="全阶段共用的真实原行登记 sources.json（只读）")
    p7.add_argument("--manifest", required=True,
                    help="接续清单 JSON：terminal/stages（图路径+SHA-256）/bindings（scan→生产阶段表）")
    p7.add_argument("--out", required=True, help="结果图输出路径（必须此前不存在；失败不留半份文件）")
    p7.set_defaults(fn=cmd_stitch)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
