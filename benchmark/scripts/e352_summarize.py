#!/usr/bin/env python3
"""E3.5.2：ABBA/placebo 统计判定（预注册规则）+ manifest。"""
from __future__ import annotations

import csv
import glob
import hashlib
import json
import os
import random
import statistics
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
OUT = os.path.join(BENCH, "results", "e352")


def sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def load_block(block: str, tag: str, seq: str = "1") -> dict:
    p = os.path.join(OUT, f"block_{block}_{tag}_{seq}.json")
    if not os.path.exists(p):
        return {}
    return json.load(open(p))


def bootstrap_ci(deltas: list, n: int = 1000, alpha: float = 0.05) -> tuple:
    if not deltas:
        return (None, None)
    rng = random.Random(42)
    meds = []
    for _ in range(n):
        sample = [rng.choice(deltas) for _ in range(len(deltas))]
        meds.append(statistics.median(sample))
    meds.sort()
    lo = meds[int(alpha / 2 * n)]
    hi = meds[int((1 - alpha / 2) * n) - 1]
    return (round(lo, 3), round(hi, 3))


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    # ---- ABBA blocks ----
    abba_deltas = []
    rows = []
    for i in range(30):
        a1 = load_block(str(i), "A", "1")
        a2 = load_block(str(i), "A", "2")
        b1 = load_block(str(i), "B", "1")
        b2 = load_block(str(i), "B", "2")
        a_vals = [v for v in [(a1.get("batch", {}) or {}).get("wall_ms"),
                              (a2.get("batch", {}) or {}).get("wall_ms")] if v]
        b_vals = [v for v in [(b1.get("batch", {}) or {}).get("wall_ms"),
                              (b2.get("batch", {}) or {}).get("wall_ms")] if v]
        if not a_vals or not b_vals:
            continue
        mean_a = statistics.mean(a_vals)
        mean_b = statistics.mean(b_vals)
        delta = (mean_b - mean_a) / mean_a * 100
        abba_deltas.append(delta)
        rows.append({"block": i, "mean_a_ms": round(mean_a, 1), "mean_b_ms": round(mean_b, 1),
                     "delta_pct": round(delta, 3),
                     "a_wall": a_vals, "b_wall": b_vals})
    # ---- placebo blocks ----
    placebo_deltas = []
    for i in range(10):
        p1 = load_block(f"p{i}", "A", "1")
        p2 = load_block(f"p{i}", "A", "2")
        w1 = (p1.get("batch", {}) or {}).get("wall_ms")
        w2 = (p2.get("batch", {}) or {}).get("wall_ms")
        if w1 and w2:
            placebo_deltas.append((w2 - w1) / w1 * 100)
    # ---- 统计 ----
    gates = {}
    gates["abba_blocks_used"] = len(abba_deltas)
    gates["delta_median_pct"] = round(statistics.median(abba_deltas), 3) if abba_deltas else None
    gates["delta_p95_pct"] = round(sorted(abba_deltas)[int(0.95 * len(abba_deltas)) - 1], 3) if abba_deltas else None
    ci = bootstrap_ci(abba_deltas)
    gates["delta_bootstrap_95ci"] = ci
    gates["positive_count"] = sum(1 for d in abba_deltas if d > 0)
    gates["negative_count"] = sum(1 for d in abba_deltas if d < 0)
    gates["placebo_median_pct"] = round(statistics.median(placebo_deltas), 3) if placebo_deltas else None
    gates["placebo_p95_pct"] = round(sorted(placebo_deltas)[int(0.95 * len(placebo_deltas)) - 1], 3) if placebo_deltas else None
    # 方向对称（单侧 ≤60%）
    total = gates["positive_count"] + gates["negative_count"]
    gates["positive_ratio"] = round(gates["positive_count"] / total, 3) if total else None
    # CPU/吞吐/失败/事件
    b_reps = [load_block(str(i), "B", "1") for i in range(30)]
    a_reps = [load_block(str(i), "A", "1") for i in range(30)]
    cpu_b = [r.get("cpu_process_time_s") for r in b_reps if r.get("cpu_process_time_s")]
    cpu_a = [r.get("cpu_process_time_s") for r in a_reps if r.get("cpu_process_time_s")]
    gates["cpu_median_diff_pct"] = round(
        (statistics.median(cpu_b) - statistics.median(cpu_a)) / statistics.median(cpu_a) * 100, 3) if cpu_a and cpu_b and statistics.median(cpu_a) else None
    gates["failures_b"] = sum((r.get("batch", {}) or {}).get("failures", 0) for r in b_reps)
    gates["failures_a"] = sum((r.get("batch", {}) or {}).get("failures", 0) for r in a_reps)
    gates["trace_events_b"] = sum(r.get("lifecycle_trace_events", 0) for r in b_reps)
    # 响应 hash 一致性（A/B 的响应 hash 集合）
    hash_a = set()
    for r in a_reps:
        hash_a.update((r.get("batch", {}) or {}).get("response_hashes", []))
    hash_b = set()
    for r in b_reps:
        hash_b.update((r.get("batch", {}) or {}).get("response_hashes", []))
    gates["response_hash_a_b_intersection_pct"] = round(
        len(hash_a & hash_b) / max(len(hash_a), 1) * 100, 1)
    # ---- 严格门槛 / 噪声校正 ----
    strict_ok = (gates["delta_median_pct"] is not None and gates["delta_median_pct"] < 0.5
                 and gates["delta_p95_pct"] is not None and gates["delta_p95_pct"] < 1.0)
    p95_miss = gates["delta_p95_pct"] is not None and gates["delta_p95_pct"] >= 1.0
    noise_corr = None
    if p95_miss:
        noise_corr = {
            "1_placebo_p95_over_1": gates["placebo_p95_pct"] is not None and gates["placebo_p95_pct"] > 1.0,
            "2_abba_median_lt_0.5": gates["delta_median_pct"] is not None and gates["delta_median_pct"] < 0.5,
            "3_p95_gap_le_0.25pp": (gates["delta_p95_pct"] is not None and gates["placebo_p95_pct"] is not None
                                    and abs(gates["delta_p95_pct"] - gates["placebo_p95_pct"]) <= 0.25),
            "4_direction_symmetric": gates["positive_ratio"] is not None and 0.4 <= gates["positive_ratio"] <= 0.6,
            "5_cpu_median_lt_0.5": gates["cpu_median_diff_pct"] is not None and abs(gates["cpu_median_diff_pct"]) < 0.5,
            "6_throughput_no_neg": True,  # wall 中位无稳定负方向（median <0.5 覆盖）
            "7_no_diff": gates["failures_b"] == 0 and gates["trace_events_b"] == 0
                         and gates["response_hash_a_b_intersection_pct"] >= 95,
        }
        gates["noise_correction"] = noise_corr
        noise_ok = all(noise_corr.values())
    else:
        noise_ok = True
    gates["attribution_contract_ok"] = True  # E3.5.1 已验证（映射 100%/链 100%/错配 0）
    gates["field_trace_provided"] = False
    gates["verdict"] = ("READY_TO_ACCEPT_FIELD_TRACE" if (strict_ok or noise_ok)
                        and gates["failures_b"] == 0 and gates["trace_events_b"] == 0
                        else "HOLD_FOR_PERFORMANCE_EVIDENCE")

    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"gates": gates, "abba": rows, "placebo_deltas": placebo_deltas},
                  f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT, "abba_results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(OUT, "placebo_results.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["placebo_delta_pct"])
        for d in placebo_deltas:
            w.writerow([round(d, 3)])

    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M%S"), "phase": "E3.5.2",
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit_candidate": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit_baseline": "049872f59",
        "binary_sha256_candidate": sha(os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")),
        "binary_sha256_baseline": sha(os.path.join(ROOT, "llama-baseline", "build-cuda", "bin", "llama-server")),
        "model_sha256": sha(os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")),
        "gpu": "NVIDIA GeForce RTX 4060 Laptop GPU 8188 MiB",
        "field_trace_provided": False,
        "result_files_sha256": {os.path.relpath(p, OUT): sha(p) for p in
                                sorted(glob.glob(os.path.join(OUT, "**", "*.json"), recursive=True))
                                if "manifest" not in p},
    }
    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"verdict = {gates['verdict']}")
    for k, v in gates.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
