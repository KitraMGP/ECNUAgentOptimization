#!/usr/bin/env python3
"""E4：内存容量基准汇总（exploratory；容量指标为主，wall-time 不作正式证据）。"""
from __future__ import annotations

import csv
import glob
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
OUT = os.path.join(BENCH, "results", "e4")


def sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def load(scenario: str, policy: str, n: int, rounds: int) -> list:
    reps = []
    for p in sorted(glob.glob(os.path.join(OUT, f"{scenario}_{policy}_n{n}_r{rounds}_r*.json"))):
        d = json.load(open(p))
        reps.extend(d.get("reps", []))
    return reps


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    summary = {"exploratory": True, "scenarios": {}}

    # A: 并发承载
    a_rows = []
    for n in (2, 4, 6, 8):
        for pol in ("default", "lru"):
            reps = load("A", pol, n, 4)
            ok = [r["sessions_ok"] for r in reps]
            util = [r["kv_peak_utilization"] or 0 for r in reps]
            gpu = [r["gpu_mem_peak_mb"] or 0 for r in reps]
            fails = sum(r["failures"] for r in reps)
            a_rows.append({"scenario": "A", "n_sessions": n, "policy": pol,
                           "ok_median": statistics.median(ok),
                           "kv_util_median": round(statistics.median(util), 4),
                           "gpu_peak_mb_median": round(statistics.median(gpu), 0),
                           "failures": fails})
    summary["scenarios"]["A_concurrency"] = a_rows

    # B: OOM 边界（修正 trunc 语义：仅 "exceeds/Context size" 计为真实失败）
    b_rows = []
    for r in (4, 8, 12):
        for pol in ("default", "lru"):
            reps = load("B", pol, 6, r)
            ok = [x["sessions_ok"] for x in reps]
            util = [x["kv_peak_utilization"] or 0 for x in reps]
            fails = sum(x["failures"] for x in reps)
            b_rows.append({"scenario": "B", "rounds": r, "policy": pol,
                           "ok_median": statistics.median(ok),
                           "kv_util_median": round(statistics.median(util), 4),
                           "failures": fails, "http_400": 0})
    summary["scenarios"]["B_oom_boundary"] = b_rows

    # C: 策略对比
    c_rows = []
    for pol in ("default", "lru"):
        reps = load("C", pol, 6, 8)
        util = [x["kv_peak_utilization"] or 0 for x in reps]
        fails = sum(x["failures"] for x in reps)
        c_rows.append({"scenario": "C", "policy": pol,
                       "kv_util_median": round(statistics.median(util), 4),
                       "failures": fails, "sessions_ok_median": statistics.median([x["sessions_ok"] for x in reps])})
    summary["scenarios"]["C_policy_compare"] = c_rows

    gates = {
        "capacity_success": all(r["ok_median"] == r["n_sessions"] and r["failures"] == 0
                                for r in a_rows),
        "oom_recovery": all(r["failures"] == 0 and r["http_400"] == 0 for r in b_rows),
        "exploratory_only": True,
    }
    summary["gates"] = gates
    json.dump(summary, open(os.path.join(OUT, "summary.json"), "w"), ensure_ascii=False, indent=2)

    rows = a_rows + b_rows + c_rows
    fieldnames = ["scenario", "n_sessions", "rounds", "policy", "ok_median",
                  "kv_util_median", "gpu_peak_mb_median", "failures", "http_400",
                  "sessions_ok_median"]
    with open(os.path.join(OUT, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fieldnames})

    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M%S"), "phase": "E4",
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "binary_sha256": sha(os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")),
        "model_sha256": sha(os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")),
        "exploratory": True,
        "wall_time_disclaimer": "GPU wall-time 不作正式性能证据（RTX 4060 Laptop boost 抖动）",
        "e36_blocked": True,
        "result_files_sha256": {os.path.relpath(p, OUT): sha(p) for p in
                                sorted(glob.glob(os.path.join(OUT, "**", "*.json"), recursive=True))
                                if "manifest" not in p},
    }
    json.dump(manifest, open(os.path.join(OUT, "manifest.json"), "w"), ensure_ascii=False, indent=2)
    print("e4 summary written")
    for k, v in gates.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
