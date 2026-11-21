"""纯 Python CDCL SAT 求解器（MiniSat 风格）。

特性：
  -  watched-literal 传播、1-UIP 学习、VSIDS 活动度、阶段保存、Luby 重启
  -  假设求解 solve(assumptions)：UNSAT 时给出（非最小）假设冲突核
  -  冲突预算与墙上时间预算：耗尽返回 "unknown"，绝不谎报 sat/unsat

接口：
  s = CDCL(nvars); s.add_clause([...]); ...
  status = s.solve(assumptions=[...])   # "sat" | "unsat" | "unknown"
  s.model()  -> {var: bool}             # status == "sat"
  s.core     -> [assumption literals]   # status == "unsat"
"""
from __future__ import annotations

import heapq
import time


class CDCL:
    def __init__(self, nvars):
        self.n = nvars
        self.assigns = bytearray(nvars + 1)      # 0=未指派 1=True 2=False
        self.level = [0] * (nvars + 1)
        self.reason = [None] * (nvars + 1)       # 子句 id(int) / "A" 假设 / "D" 决策 / None(层0单元)
        self.clauses = []                        # [ [lit,...] ]，前两个字面量是被监视字面量
        self.watches = {}                        # lit -> [clause_id]
        self.trail = []
        self.trail_lim = []
        self.qhead = 0
        self.activity = [0.0] * (nvars + 1)
        self.var_inc = 1.0
        self.var_decay = 0.95
        self.heap = []                           # (-activity, var)
        self.phase = bytearray(nvars + 1)        # 0=未保存 1=True 2=False
        self.ok = True
        # 求解状态
        self.assump_lits = []
        self.assump_vars = set()
        self.core = None
        self.stats = {"conflicts": 0, "decisions": 0, "propagations": 0, "restarts": 0}
        # 预算
        self.conf_budget = None
        self.deadline = None

    # ------------------------------------------------------------------ 基础

    def add_clause(self, lits):
        if not self.ok:
            return
        # 去重 + 重言式检查
        seen = set()
        out = []
        for l in lits:
            if -l in seen:
                return  # 重言式
            if l not in seen:
                seen.add(l)
                out.append(l)
        cid = len(self.clauses)
        self.clauses.append(out)
        if len(out) == 0:
            self.ok = False
        elif len(out) == 1:
            if not self._enqueue(out[0], None):
                self.ok = False
        else:
            self.watches.setdefault(out[0], []).append(cid)
            self.watches.setdefault(out[1], []).append(cid)

    def _value(self, lit):
        a = self.assigns[abs(lit)]
        if a == 0:
            return 0
        # a: 1=True 2=False
        if lit > 0:
            return 1 if a == 1 else -1
        return -1 if a == 1 else 1

    def _enqueue(self, lit, reason):
        v = abs(lit)
        val = 1 if lit > 0 else 2
        cur = self.assigns[v]
        if cur != 0:
            return cur == val
        self.assigns[v] = val
        self.level[v] = len(self.trail_lim)
        self.reason[v] = reason
        self.trail.append(lit)
        return True

    def _propagate(self):
        clauses = self.clauses
        assigns_value = self._value
        while self.qhead < len(self.trail):
            p = self.trail[self.qhead]
            self.qhead += 1
            self.stats["propagations"] += 1
            if (self.stats["propagations"] & 0x1FFF) == 0 and self.deadline is not None:
                if time.monotonic() > self.deadline:
                    raise _Budget()
            ws = self.watches.get(-p)
            if not ws:
                continue
            i = 0
            j = 0
            n = len(ws)
            while i < n:
                cid = ws[i]
                i += 1
                c = clauses[cid]
                if c[0] == -p:
                    c[0], c[1] = c[1], c[0]
                # 现在 c[1] 是被赋假的被监视字面量（若该子句确实监视 -p）
                if c[1] != -p:
                    # 防御：不应发生
                    ws[j] = cid
                    j += 1
                    continue
                first = c[0]
                if assigns_value(first) == 1:
                    ws[j] = cid
                    j += 1
                    continue
                # 寻找新的被监视字面量
                found = False
                for k in range(2, len(c)):
                    if assigns_value(c[k]) != -1:
                        c[1], c[k] = c[k], c[1]
                        self.watches.setdefault(c[1], []).append(cid)
                        found = True
                        break
                if found:
                    continue  # cid 不再监视 -p，不写回
                ws[j] = cid
                j += 1
                fv = assigns_value(first)
                if fv == -1:
                    # 冲突：把剩余未处理的监视搬回去
                    while i < n:
                        ws[j] = ws[i]
                        i += 1
                        j += 1
                    del ws[j:]
                    return cid
                self._enqueue(first, cid)
            del ws[j:]
        return None

    # ------------------------------------------------------------------ 分析

    def _bump(self, v):
        self.activity[v] += self.var_inc
        if self.activity[v] > 1e100:
            for i in range(len(self.activity)):
                self.activity[i] *= 1e-100
            self.var_inc *= 1e-100
            # 堆中活动度全部失效，重建
            self.heap = [(-self.activity[x], x) for x in range(1, self.n + 1) if self.assigns[x] == 0]
            heapq.heapify(self.heap)
        heapq.heappush(self.heap, (-self.activity[v], v))

    def _analyze(self, confl):
        learnt = [0]
        seen = bytearray(self.n + 1)
        path_c = 0
        idx = len(self.trail) - 1
        p = 0
        c = self.clauses[confl]
        cur_level = len(self.trail_lim)
        while True:
            start = 0 if p == 0 else 1
            for k in range(start, len(c)):
                q = c[k]
                v = abs(q)
                if not seen[v] and self.level[v] > 0:
                    seen[v] = 1
                    self._bump(v)
                    if self.level[v] >= cur_level:
                        path_c += 1
                    else:
                        learnt.append(q)
            while not seen[abs(self.trail[idx])]:
                idx -= 1
            p = self.trail[idx]
            idx -= 1
            seen[abs(p)] = 0
            path_c -= 1
            if path_c <= 0:
                break
            r = self.reason[abs(p)]
            assert isinstance(r, int), "analyze 遇到无推理子句的非层0字面量"
            c = self.clauses[r]
        learnt[0] = -p
        # 回跳层 = learnt[1:] 中的最大层
        bt = 0
        for l in learnt[1:]:
            lv = self.level[abs(l)]
            if lv > bt:
                bt = lv
        self.var_inc /= self.var_decay
        return learnt, bt

    def _cancel_until(self, lvl):
        if len(self.trail_lim) <= lvl:
            return
        for i in range(len(self.trail) - 1, self.trail_lim[lvl] - 1, -1):
            lit = self.trail[i]
            v = abs(lit)
            self.assigns[v] = 0
            self.reason[v] = None
            self.phase[v] = 1 if lit > 0 else 2
            heapq.heappush(self.heap, (-self.activity[v], v))
        del self.trail[self.trail_lim[lvl]:]
        del self.trail_lim[lvl:]
        self.qhead = len(self.trail)

    def _pick_branch_lit(self):
        while True:
            while self.heap:
                neg_act, v = heapq.heappop(self.heap)
                if self.assigns[v] != 0:
                    continue
                if -neg_act != self.activity[v]:
                    # 陈旧条目：以当前活动度重新入堆，不能丢弃该变量
                    heapq.heappush(self.heap, (-self.activity[v], v))
                    continue
                ph = self.phase[v]
                return v if ph == 1 else -v
            # 堆空：可能仍有未指派变量（陈旧条目等原因），重建一次
            if all(self.assigns[v] != 0 for v in range(1, self.n + 1)):
                return 0
            for v in range(1, self.n + 1):
                if self.assigns[v] == 0:
                    heapq.heappush(self.heap, (-self.activity[v], v))

    # ------------------------------------------------------------------ 假设核

    def _trace_core(self, start_cid, also_lit):
        """从冲突子句沿推理链回溯，收集涉及的假设字面量。"""
        core = set()
        if also_lit is not None:
            core.add(also_lit)
        seen = bytearray(self.n + 1)
        stack = [start_cid] if start_cid is not None else []
        while stack:
            cid = stack.pop()
            for q in self.clauses[cid]:
                v = abs(q)
                if seen[v] or self.level[v] == 0:
                    continue
                seen[v] = 1
                r = self.reason[v]
                if r == "A":
                    core.add(v if self.assigns[v] == 1 else -v)
                elif isinstance(r, int):
                    stack.append(r)
                # "D" 决策或 None：不应出现在假设层冲突中；忽略
        return sorted(core)

    # ------------------------------------------------------------------ 求解

    def solve(self, assumptions=(), conf_budget=None, seconds=None):
        self.assump_lits = list(assumptions)
        self.assump_vars = {abs(a) for a in assumptions}
        self.core = None
        if conf_budget is not None:
            self.conf_budget = conf_budget
        if seconds is not None:
            self.deadline = time.monotonic() + seconds
        try:
            return self._solve()
        except _Budget:
            return "unknown"

    def _solve(self):
        st = self.stats
        if not self.ok:
            self.core = []
            return "unsat"
        confl = self._propagate()
        if confl is not None:
            self.ok = False
            self.core = []
            return "unsat"
        # 假设作为前 k 个“决策层”入队
        n_assump_levels = 0
        for a in self.assump_lits:
            val = self._value(a)
            if val == -1:
                v = abs(a)
                r = self.reason[v]
                if r == "A":
                    # 与已入队的假设互为否定
                    self.core = sorted({a, v if self.assigns[v] == 1 else -v})
                elif isinstance(r, int):
                    self.core = self._trace_core(r, a)
                else:
                    self.core = [a]
                self.ok = False
                return "unsat"
            elif val == 1:
                continue
            else:
                self.trail_lim.append(len(self.trail))
                n_assump_levels += 1
                self._enqueue(a, "A")
                confl = self._propagate()
                if confl is not None:
                    # 该假设与先前假设冲突
                    self.core = self._trace_core(confl, None)
                    self.ok = False
                    return "unsat"
        # 主循环
        for v in range(1, self.n + 1):
            if self.assigns[v] == 0:
                heapq.heappush(self.heap, (-self.activity[v], v))
        restart_base = 100
        restart_count = 0
        conflicts_at_restart = 0
        while True:
            confl = self._propagate()
            if confl is not None:
                st["conflicts"] += 1
                conflicts_at_restart += 1
                if self.conf_budget is not None and st["conflicts"] >= self.conf_budget:
                    raise _Budget()
                if len(self.trail_lim) <= n_assump_levels:
                    # 冲突发生在假设层（尚无真决策）：假设本身不可满足
                    self.ok = False
                    self.core = self._trace_core(confl, None)
                    return "unsat"
                learnt, bt = self._analyze(confl)
                # 仅由假设构成的学习子句 => 假设不可满足
                if all(abs(l) in self.assump_vars for l in learnt):
                    self.ok = False
                    self.core = sorted(-l for l in learnt)
                    return "unsat"
                # 回跳永不低于假设层：假设在整个 solve 期间保持为真。
                # 此时 learnt[0] 必然未被指派（它处于更高的决策层）。
                self._cancel_until(max(bt, n_assump_levels))
                cid = len(self.clauses)
                self.clauses.append(learnt)
                if len(learnt) == 1:
                    self._enqueue(learnt[0], cid)
                else:
                    self.watches.setdefault(learnt[0], []).append(cid)
                    self.watches.setdefault(learnt[1], []).append(cid)
                    self._enqueue(learnt[0], cid)
                # Luby 重启
                if conflicts_at_restart >= restart_base * _luby(restart_count):
                    restart_count += 1
                    conflicts_at_restart = 0
                    st["restarts"] += 1
                    self._cancel_until(n_assump_levels)
            else:
                lit = self._pick_branch_lit()
                if lit == 0:
                    # 返回前整体复核模型，宁可内部报错也不谎报 SAT
                    assert self._check_model(), "内部错误：SAT 模型未通过子句复核"
                    return "sat"
                st["decisions"] += 1
                self.trail_lim.append(len(self.trail))
                self._enqueue(lit, "D")

    def _check_model(self):
        if any(self.assigns[v] == 0 for v in range(1, self.n + 1)):
            return False
        for c in self.clauses:
            if not any(self._value(l) == 1 for l in c):
                return False
        return True

    def model(self):
        return {v: (self.assigns[v] == 1) for v in range(1, self.n + 1)}


class _Budget(Exception):
    pass


def _luby(i):
    """Luby 序列（从 1 开始）。"""
    k = 1
    while (1 << k) - 1 < i + 1:
        k += 1
    if i + 1 == (1 << k) - 1:
        return 1 << (k - 1)
    return _luby(i + 1 - (1 << (k - 1)) + 1)
