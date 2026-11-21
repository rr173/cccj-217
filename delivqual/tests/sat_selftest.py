"""CDCL 求解器自测：随机 CNF 与暴力枚举对照，含假设核 sanity 检查。"""
import itertools
import random
import sys

sys.path.insert(0, "/workspace/delivqual")
from delivqual.sat_cdcl import CDCL


def brute_sat(n, clauses, assumptions=()):
    for vals in itertools.product([False, True], repeat=n):
        ass = {i + 1: vals[i] for i in range(n)}
        ok = True
        for a in assumptions:
            if ass[abs(a)] != (a > 0):
                ok = False
                break
        if not ok:
            continue
        for cl in clauses:
            if not any(ass[abs(l)] == (l > 0) for l in cl):
                ok = False
                break
        if ok:
            return True
    return False


def main():
    rng = random.Random(20261121)
    cases = 0
    for trial in range(4000):
        n = rng.randint(1, 9)
        m = rng.randint(0, 30)
        clauses = []
        for _ in range(m):
            k = rng.randint(1, 3)
            cl = []
            for _ in range(k):
                v = rng.randint(1, n)
                cl.append(v if rng.random() < 0.5 else -v)
            clauses.append(cl)
        assumptions = []
        for _ in range(rng.randint(0, 3)):
            v = rng.randint(1, n)
            assumptions.append(v if rng.random() < 0.5 else -v)
        if check_case(n, clauses, assumptions, trial) != 0:
            return 1
        cases += 1
    # 假设密集：假设数接近变量数，专门压迫“回跳低于假设层”的情形
    for trial in range(4000, 8000):
        n = rng.randint(3, 10)
        m = rng.randint(5, 40)
        clauses = []
        for _ in range(m):
            k = rng.randint(1, 3)
            cl = []
            for _ in range(k):
                v = rng.randint(1, n)
                cl.append(v if rng.random() < 0.5 else -v)
            clauses.append(cl)
        assumptions = []
        for v in rng.sample(range(1, n + 1), rng.randint(1, n)):
            assumptions.append(v if rng.random() < 0.5 else -v)
        if check_case(n, clauses, assumptions, trial) != 0:
            return 1
        cases += 1
    print(f"OK: {cases} 个随机 CNF 用例与暴力枚举一致（含假设核检查）")
    return 0


def check_case(n, clauses, assumptions, trial):
    s = CDCL(n)
    for cl in clauses:
        s.add_clause(cl)
    st = s.solve(assumptions=assumptions)
    expect = brute_sat(n, clauses, assumptions)
    got = st == "sat"
    if got != expect:
        print(f"MISMATCH trial={trial} n={n} clauses={clauses} assumptions={assumptions} got={st} expect={expect}")
        return 1
    if st == "unsat":
        core = s.core
        assert core is not None
        if not set(core) <= set(assumptions):
            print(f"CORE NOT SUBSET trial={trial} core={core} assumptions={assumptions}")
            return 1
        if brute_sat(n, clauses, core):
            print(f"CORE NOT UNSAT trial={trial} core={core} clauses={clauses}")
            return 1
    else:
        # 模型必须满足全部子句与假设，且所有变量都已指派
        model = s.model()
        assert all(v in model for v in range(1, n + 1))
        for cl in clauses:
            assert any(model[abs(l)] == (l > 0) for l in cl), f"model violates {cl}"
        for a in assumptions:
            assert model[abs(a)] == (a > 0), f"assumption {a} violated"
    return 0


if __name__ == "__main__":
    sys.exit(main())
