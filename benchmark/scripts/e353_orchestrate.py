#!/usr/bin/env python3
"""E3.5.3：编排（预热 → placebo 20 对 → 资格检查 → crossover 30 对）。

相邻配对：每对两个 measurement 紧邻（中间仅 pkill+2s）。
资格检查（预注册门槛）失败 → 不执行 crossover，写 environment_not_qualified。
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
OUT = os.path.join(BENCH, "results", "e353")
A = os.path.join(ROOT, "llama-baseline", "build-cuda", "bin", "llama-server")
B = os.path.join(ROOT, "llama-candidate", "build-cuda", "bin", "llama-server")
BIN = {"A": A, "B": B}


def run_one(binary: str, tag: str, pair: str, pos: str) -> None:
    subprocess.run(
        ["uv", "run", "python", "scripts/e353_stable_pair.py", "--binary", binary,
         "--tag", tag, "--pair", pair, "--pos", pos,
         "--output", os.path.join(OUT, f"pair_{pair}_{tag}_{pos}.json")],
        cwd=BENCH, capture_output=True)
    subprocess.run(["pkill", "-x", "llama-server"], capture_output=True)
    subprocess.run(["sleep", "2"])


def load(pair: str, tag: str, pos: str) -> dict:
    p = os.path.join(OUT, f"pair_{pair}_{tag}_{pos}.json")
    if os.path.exists(p):
        return json.load(open(p))
    return {}


def gpu_pair_ok(r1: dict, r2: dict) -> tuple:
    t1 = (r1.get("gpu_post") or {}).get("temp_c")
    t2 = (r2.get("gpu_post") or {}).get("temp_c")
    c1 = (r1.get("gpu_post") or {}).get("clock_mhz")
    c2 = (r2.get("gpu_post") or {}).get("clock_mhz")
    temp_ok = t1 is not None and t2 is not None and abs(t1 - t2) <= 2.0
    clock_ok = c1 is not None and c2 is not None and c1 and abs(c1 - c2) / c1 <= 0.01
    return temp_ok, clock_ok, (t1, t2, c1, c2)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    prereg = json.load(open(os.path.join(OUT, "prereg_manifest.json")))
    env = {"clock_lock": "unavailable（无 root 权限）", "power_lock": "unsupported"}

    # 1) 预热（4 个批次，丢弃）
    print("=== 预热 ===")
    for i in range(4):
        run_one(A, "A", f"warm{i}", "first")

    # 2) placebo 20 对（预注册顺序）
    print("=== placebo ===")
    for idx, (ta, tb) in enumerate(prereg["placebo"]["order"]):
        assert ta == tb, f"placebo pair 应为同 binary: {ta}{tb}"
        run_one(BIN[ta], ta, f"p{idx}", "first")
        run_one(BIN[ta], ta, f"p{idx}", "second")
        print(f"  [placebo {idx} {ta}/{ta} done]")

    # 3) 资格检查
    deltas, temp_fail, clock_fail, events, fails = [], 0, 0, 0, 0
    for idx in range(20):
        tag = prereg["placebo"]["order"][idx][0]
        r1 = load(f"p{idx}", tag, "first")
        r2 = load(f"p{idx}", tag, "second")
        w1 = (r1.get("batch") or {}).get("wall_ms")
        w2 = (r2.get("batch") or {}).get("wall_ms")
        if w1 and w2:
            deltas.append((w2 - w1) / w1 * 100)
        tok, cok, g = gpu_pair_ok(r1, r2)
        temp_fail += 0 if tok else 1
        clock_fail += 0 if cok else 1
        events += (r1.get("lifecycle_trace_events") or 0) + (r2.get("lifecycle_trace_events") or 0)
        fails += (r1.get("batch") or {}).get("failures", 0) + (r2.get("batch") or {}).get("failures", 0)
    abs_deltas = [abs(d) for d in deltas]
    p95 = sorted(abs_deltas)[int(0.95 * len(abs_deltas)) - 1] if abs_deltas else None
    aa = [d for i, d in enumerate(deltas) if prereg["placebo"]["order"][i][0] == "A"]
    bb = [d for i, d in enumerate(deltas) if prereg["placebo"]["order"][i][0] == "B"]
    qual = {
        "failure_zero": fails == 0,
        "events_zero": events == 0,
        "placebo_p95_abs_le_1pct": p95 is not None and p95 <= 1.0,
        "placebo_p95_abs": round(p95, 3) if p95 is not None else None,
        "aa_abs_median_lt_0.25": abs(statistics.median(aa)) < 0.25 if aa else False,
        "bb_abs_median_lt_0.25": abs(statistics.median(bb)) < 0.25 if bb else False,
        "pair_temp_diff_le_2c": temp_fail == 0,
        "pair_clock_diff_le_1pct": clock_fail == 0,
        "throttling_count": 0,  # 无 throttling 查询字段；温度/clock 差覆盖
        "placebo_deltas": [round(d, 3) for d in deltas],
    }
    qualified = all(qual[k] for k in ("failure_zero", "events_zero", "placebo_p95_abs_le_1pct",
                                      "aa_abs_median_lt_0.25", "bb_abs_median_lt_0.25",
                                      "pair_temp_diff_le_2c", "pair_clock_diff_le_1pct"))
    qual["qualified"] = qualified
    json.dump(qual, open(os.path.join(OUT, "environment_qualification.json"), "w"), indent=2)
    print(f"=== 资格检查: {'PASS' if qualified else 'FAIL'} p95={qual['placebo_p95_abs']} "
          f"temp_fail={temp_fail} clock_fail={clock_fail} ===")
    if not qualified:
        print("=== environment_not_qualified：不执行 crossover ===")
        return 0

    # 4) crossover 30 对（预注册顺序）
    print("=== crossover ===")
    for idx, (ta, tb) in enumerate(prereg["crossover"]["order"]):
        assert {ta, tb} == {"A", "B"}
        run_one(BIN[ta], ta, f"c{idx}", "first")
        run_one(BIN[tb], tb, f"c{idx}", "second")
        print(f"  [crossover {idx} {ta}->{tb} done]")
    print("=== E3.5.3 DONE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
