#!/usr/bin/env python3
"""E6.4-C1: paired capacity/reuse benchmark（TinyLlama 标准架构，真实 KV 路径）。

场景（跨 slot 前缀共享）：
- req1: A+X（id_slot=0）→ 完成后 idle 保留
- req2: A+Y（id_slot=1）→ kv-prefix-share on 时共享 A 前缀（recompute 下降），off 全量
配对：on/off 相同输入、seed、budget；输出必须一致（无损）。
重复：性能 gate ≥5 次（12.9）；正确性 gate ≥3 次。

指标（12.17）：recompute_tokens = prompt_n - cache_n_past；used_cells；shared_cells；
输出 hash（无损比对）。

用法：python scripts/e6_c1_bench.py --reps 5 --output raw/e6_c1_bench.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import time
from urllib import request, error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
# TinyLlama 标准架构模型（真实 KV 路径；4B hybrid 上 C1 自动禁用）
MODEL_CACHE = os.path.join(ROOT, "llama.cpp", "tmp", "models--ggml-org--test-model-stories260K")
MODEL = os.path.join(MODEL_CACHE, "snapshots",
                     "479896ec924af6d40fd419ab8f4d1eb2101de00d", "stories260K-f32.gguf")
SERVER_BIN = os.path.join(ROOT, "llama.cpp", "build", "bin", "llama-server")
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"

A = ("Once upon a time in the deep dark forest, a small rabbit named Miko discovered "
     "a glowing stone near the old oak tree. The stone hummed with a soft blue light "
     "and seemed to pulse with the rhythm of the wind. Miko carefully picked it up "
     "and carried it back to the burrow, where the other animals gathered to see. "
     "The stone told stories of ancient rivers and forgotten trails. ")
X = " Miko decided to hide the stone in the hollow log by the stream."
Y = " Miko decided to show the stone to the wise old owl at dusk."


def http_json(method, url, body=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def wait_health(proc, timeout_s=120):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if proc.poll() is not None:
            return False
        try:
            with request.urlopen(f"{BASE}/health", timeout=3) as resp:
                if json.loads(resp.read().decode()).get("status") == "ok":
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def run_pair(proc_args: list, mode: str, reps: int) -> dict:
    log_path = os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw",
                            f"c1bench_server_{mode}.log")
    logf = open(log_path, "w")
    proc = subprocess.Popen(proc_args, stdout=logf, stderr=subprocess.STDOUT)
    if not wait_health(proc):
        print(f"[{mode}] server failed")
        return {"failed": True}
    try:
        results = []
        for rep in range(reps):
            # 独立 replicate：每 rep 前 erase 两个 slot（清 KV，避免状态累积）
            for sid in (0, 1):
                try:
                    http_json("POST", f"{BASE}/slots/{sid}?action=erase", {}, timeout=30)
                except Exception:
                    pass
            # req1: A+X -> slot0
            r1 = http_json("POST", f"{BASE}/completion",
                           {"prompt": A + X, "n_predict": 8, "temperature": 0,
                            "cache_prompt": True, "id_slot": 0}, timeout=300)
            r2 = http_json("POST", f"{BASE}/completion",
                           {"prompt": A + Y, "n_predict": 8, "temperature": 0,
                            "cache_prompt": True, "id_slot": 1}, timeout=300)
            kv = http_json("GET", f"{BASE}/metrics/kv", timeout=10)
            kv_stats = kv.get("kv_stats") if "kv_stats" in kv else kv
            t1 = r1.get("timings", {})
            t2 = r2.get("timings", {})
            results.append({
                "repetition": rep,
                "req1_prompt_n": t1.get("prompt_n", 0),
                "req1_cache_n_past": t1.get("cache_n", 0),
                "req2_prompt_n": t2.get("prompt_n", 0),
                "req2_cache_n_past": t2.get("cache_n", 0),  # 参考（E2.2: cache_n 语义不可靠）
                "req2_recompute_tokens": t2.get("prompt_n", 0),  # 本次 prefill 处理 token 数（共享后大幅下降）
                "req2_output": r2.get("content", ""),
                "req2_output_sha256": hashlib.sha256(r2.get("content", "").encode()).hexdigest(),
                "kv_used_cells": kv_stats.get("used_cells"),
                "kv_shared_cells": kv_stats.get("shared_cells"),
                "kv_active_sequences": kv_stats.get("active_sequences"),
            })
        return {"failed": False, "results": results}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--output", default="")
    ap.add_argument("--prefix-tokens", type=int, default=0, help="0=固定 A 前缀")
    args = ap.parse_args()

    slot_save = os.path.join(ROOT, "llama.cpp", "tmp", "e6_slot_save")
    os.makedirs(slot_save, exist_ok=True)
    base = [SERVER_BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
            "--ctx-size", "512", "--parallel", "2", "--kv-unified",
            "--cache-ram", "0", "--temp", "0", "--seed", "42", "--metrics",
            "--slot-save-path", slot_save,
            "--kv-prefix-share-min-lcp", "4"]
    off_args = base + []
    on_args = base + ["--kv-prefix-share"]

    off = run_pair(off_args, "off", args.reps)
    on = run_pair(on_args, "on", args.reps)

    # 汇总
    def med(vals):
        vals = [v for v in vals if v is not None]
        return round(statistics.median(vals), 2) if vals else None

    off_rec = off.get("results", [])
    on_rec = on.get("results", [])
    recompute_off = [r["req2_recompute_tokens"] for r in off_rec]
    recompute_on = [r["req2_recompute_tokens"] for r in on_rec]
    # 无损：on/off 的 req2 输出一致（每 rep 对齐）
    off_out = [r["req2_output_sha256"] for r in off_rec]
    on_out = [r["req2_output_sha256"] for r in on_rec]
    lossless = (off_out == on_out) and len(off_out) > 0
    # recompute 下降（12.11 门槛：≥25%）
    m_off, m_on = med(recompute_off), med(recompute_on)
    reduction = (1 - m_on / m_off) * 100 if m_off else None

    summary = {
        "experiment_id": "e6_c1_paired_bench",
        "candidate_id": "C1_RADIX_PREFIX_SHARING",
        "variant": "paired",
        "workload_id": "C1_PAIRED_bench",
        "git_commit": subprocess.check_output(
            ["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "model": "tinyllama stories260K (standard attention-only, real KV path)",
        "reps": args.reps,
        "off": {"results": off_rec, "req2_recompute_median": m_off},
        "on": {"results": on_rec, "req2_recompute_median": m_on},
        "lossless_output_identical": lossless,
        "recompute_reduction_pct": round(reduction, 2) if reduction is not None else None,
        "threshold_12_11": "exact-prefix recompute tokens >=25% reduction",
        "verdict": "PASS_OPTIMIZATION_GAIN" if (lossless and reduction is not None and reduction >= 25) else "FAIL",
        "notes": ["TinyLlama 标准架构（4B hybrid 上 C1 自动禁用）",
                  "on/off 相同输入/seed/budget，仅 kv-prefix-share 开关不同"],
    }
    print(json.dumps({k: summary[k] for k in ("reps", "lossless_output_identical",
                                               "recompute_reduction_pct", "verdict")}, indent=1))
    for i, (o, n) in enumerate(zip(off_rec, on_rec)):
        print(f"  rep{i}: off_recompute={o['req2_recompute_tokens']} on_recompute={n['req2_recompute_tokens']} "
              f"on_shared_cells={n['kv_shared_cells']}")

    out = args.output or os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw",
                                      "e6_c1_bench_summary.json")
    with open(out, "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("saved:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
