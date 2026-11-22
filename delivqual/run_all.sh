#!/bin/sh
# 一键复跑：结构校验 + 主演练 + 求解器自测 + 验收套件 + 大例基准
set -e
cd "$(dirname "$0")"
DQ="./dq"
OUT=/tmp/delivqual_out
mkdir -p "$OUT" /tmp/large

echo "========== 1. 结构校验（全部小样例） =========="
for d in main_drill mutex diamond bad_any mismatch missing; do
  for g in examples/$d/graph*.json; do
    for r in examples/$d/req*.json; do
      [ -f "$r" ] || continue
      $DQ validate --sources examples/$d/sources.json --graph "$g" --request "$r" > /dev/null \
        && echo "  ok: $d/$(basename $g) + $(basename $r)" \
        || echo "  (预期内拒绝) $d/$(basename $g) + $(basename $r)"
    done
  done
done
echo "  -- 坏输入应被拒绝 --"
$DQ validate --sources examples/bad_any/sources.json --graph examples/bad_any/graph_broken.json || echo "  ok: graph_broken.json 被拒绝 (exit=$?)"

echo "========== 2. 主演练 =========="
S=examples/main_drill/sources.json
$DQ plan --sources $S --graph examples/main_drill/graph_v1.json --request examples/main_drill/req_R.json  --out $OUT/v1_R.json  >/dev/null && echo "  v1 R 单独交付: feasible"
$DQ plan --sources $S --graph examples/main_drill/graph_v1.json --request examples/main_drill/req_G.json  --out $OUT/v1_G.json  >/dev/null && echo "  v1 G 单独交付: feasible"
if $DQ plan --sources $S --graph examples/main_drill/graph_v1.json --request examples/main_drill/req_RG.json --out $OUT/v1_RG.json >/dev/null; then
  echo "  !! v1 R+G 应不可行"; exit 1
else
  echo "  v1 R+G 同时交付: infeasible（X=raw 与 Y=aggregate 不能同选）"
fi
$DQ plan --sources $S --graph examples/main_drill/graph_v2.json --request examples/main_drill/req_RG.json --out $OUT/v2_RG.json >/dev/null \
  && echo "  v2 R+G（G 增加 T 的充分替代推导）: feasible（S/X + T/A，notice 不含 S/Y）"
$DQ verify-plan --sources $S --graph examples/main_drill/graph_v2.json --request examples/main_drill/req_RG.json --plan $OUT/v2_RG.json >/dev/null \
  && echo "  v2_RG 计划独立核验: valid"
$DQ verify-plan --sources $S --graph examples/main_drill/graph_v1.json --request examples/main_drill/req_RG.json --plan $OUT/v1_RG.json >/dev/null \
  && echo "  v1_RG 冲突证书独立核验: valid"
$DQ explain-row --sources $S --graph examples/main_drill/graph_v2.json --request examples/main_drill/req_RG.json --table G --row G1 | sed 's/^/  /'

echo "========== 3. 求解器自测（随机 CNF 对照暴力） =========="
python3 -I tests/sat_selftest.py

echo "========== 4. 验收套件（小例/篡改/随机/大例/预算） =========="
python3 tests/acceptance.py

echo "========== 4b. build-totals 验收（配对/去重/汇总/证据展开/线性压缩/原子交付） =========="
python3 -I tests/build_totals.py
# 从随库小例端到端生成一份图，再交给原有 validate/plan/verify-plan
BT=$OUT/build_totals
rm -rf "$BT"
$DQ build-totals \
  --sources examples/build_totals/sources.json \
  --purchase examples/build_totals/purchase.jsonl \
  --attribution examples/build_totals/attribution.jsonl \
  --purchase-source P --purchase-snapshot p2026 \
  --attribution-source A --attribution-snapshot a2026 \
  --out-dir "$BT"
$DQ validate --sources examples/build_totals/sources.json --graph "$BT/graph.json" \
  --request examples/build_totals/request.json > /dev/null && echo "  生成图 validate: 通过"
$DQ plan --sources examples/build_totals/sources.json --graph "$BT/graph.json" \
  --request examples/build_totals/request.json --out "$OUT/bt_plan.json" >/dev/null
$DQ verify-plan --sources examples/build_totals/sources.json --graph "$BT/graph.json" \
  --request examples/build_totals/request.json --plan "$OUT/bt_plan.json" >/dev/null \
  && echo "  生成图 plan + verify-plan: valid"
cat "$BT/totals.jsonl" | sed 's/^/  /'

echo "========== 4c. quarantine 验收（隔离副本/撤下凭据/共享理由/线性规模） =========="
python3 -I tests/quarantine.py
# 端到端：从共用凭据小例生成隔离副本，再交给原有 validate/plan/verify-plan
Q=$OUT/quarantine
rm -rf "$Q"
$DQ quarantine \
  --sources examples/quarantine/sources.json \
  --graph examples/quarantine/graph.json \
  --records examples/quarantine/records.jsonl \
  --invalidations examples/quarantine/invalidations.json \
  --out-dir "$Q"
$DQ validate --sources examples/quarantine/sources.json --graph "$Q/graph.json" \
  --request examples/quarantine/req_a.json > /dev/null && echo "  隔离图 validate: 通过"
$DQ plan --sources examples/quarantine/sources.json --graph "$Q/graph.json" \
  --request examples/quarantine/req_a.json --out "$Q/plan_a.json" >/dev/null
$DQ verify-plan --sources examples/quarantine/sources.json --graph "$Q/graph.json" \
  --request examples/quarantine/req_a.json --plan "$Q/plan_a.json" >/dev/null \
  && echo "  隔离图 plan + verify-plan: valid"
echo "  withdrawals.json 撤下条目:"
python3 -c "import json; w=json.load(open('$Q/withdrawals.json')); [print('   撤下', x['table'], x['row'], '根', x['origin_root']) for x in w['withdrawals']]"

echo "========== 5. 大例基准（运行时生成） =========="
python3 examples/gen_large.py --out /tmp/large --sources 24 --rows 12000 --tables 4 --seed 7
$DQ plan --sources /tmp/large/sources.json --graph /tmp/large/graph.json --request /tmp/large/request.json --out $OUT/large_plan.json >/dev/null
python3 -c "
import json
p = json.load(open('$OUT/large_plan.json'))
s = p['stats']
print(f\"  状态={p['status']} 变量={s['sat_vars']} 子句={s['sat_clauses']} 假设={s['sat_assumptions']} 求解={s['solve_seconds']}s 自校验={s.get('self_verify_seconds')}s\")"
$DQ verify-plan --sources /tmp/large/sources.json --graph /tmp/large/graph.json --request /tmp/large/request.json --plan $OUT/large_plan.json | python3 -c "
import json,sys
d = json.load(sys.stdin)
print(f\"  独立核验={d['verdict']} 耗时={d['seconds']}s\")"

echo "========== 全部复跑完成 =========="
