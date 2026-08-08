#!/usr/bin/env python3
"""E9.5: Qwen3.5-4B 内存组成和瓶颈测量（GPU）。

测量项（指令节七）：
- 模型权重 bytes（GGUF 文件大小 + CUDA buffer）
- attention KV bytes（/metrics/kv used_bytes，随 prompt/ctx 变化）
- recurrent state bytes（hybrid：固定开销，与 prompt 长度无关——验证）
- CUDA allocator reserved/used（nvidia-smi）
- parallel 1/2/4 的增量
- ctx 1024/2048/4096/8192 的容量
- KV type F16/Q8/Q4（-ctk/-ctv 组合）
- prefill/decode throughput（timings）
- OOM/拒绝边界

用法：python scripts/e9_5_memory_breakdown.py --output raw/e9_mem_breakdown.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from urllib import request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODEL = os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")
SERVER_BIN = os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"

P = ("You are a monitoring assistant. The river station reports flow data. "
     "Observations: flow 12.4 m3/s, temp 8.2 C, turbidity 3.1 NTU at 08:00. "
     "flow 12.6 m3/s, temp 8.1 C, turbidity 3.0 NTU at 09:00. ")  # ~55 tokens
X = "\nAppend observation X: flow 13.1 m3/s, temp 8.0 C."


def http_json(method, url, body=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()), resp.status
    except Exception as e:
        return {"err": str(e)}, getattr(e, "code", 0)


def wait_health(proc, timeout_s=180):
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
        time.sleep(1)
    return False


def gpu_mem():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"])
        used, total = out.decode().strip().split(",")
        return {"used_mb": int(used), "total_mb": int(total)}
    except Exception as e:
        return {"err": str(e)}


def run_config(ctx, parallel, kv_type, n_prompt_units, log_dir):
    slot_save = os.path.join(ROOT, "llama.cpp", "tmp", "e6_slot_save")
    os.makedirs(slot_save, exist_ok=True)
    log_path = os.path.join(log_dir, f"mem_c{ctx}_p{parallel}_{kv_type}.log")
    args = [SERVER_BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
            "--ctx-size", str(ctx), "--parallel", str(parallel), "-ngl", "99",
            "--kv-unified", "--cache-ram", "0", "--temp", "0", "--seed", "42",
            "--metrics", "--slot-save-path", slot_save]
    if kv_type != "f16":
        t = kv_type
        args += ["--ctk", t, "--ctv", t]
    proc = subprocess.Popen(args, stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    if not wait_health(proc):
        return {"failed": True, "reason": "server start failed"}
    try:
        mem_baseline = gpu_mem()
        kv_empty, _ = http_json("GET", f"{BASE}/metrics/kv", timeout=10)
        # 发请求（prompt 长度 = n_prompt_units × P）
        prompt = P * n_prompt_units + X
        r, code = http_json("POST", f"{BASE}/completion",
                            {"prompt": prompt, "n_predict": 8, "temperature": 0,
                             "cache_prompt": True, "id_slot": 0}, timeout=600)
        t = r.get("timings", {}) if code == 200 else {}
        kv_used, _ = http_json("GET", f"{BASE}/metrics/kv", timeout=10)
        mem_after = gpu_mem()
        log = open(log_path, encoding="utf-8", errors="replace").read()
        return {
            "config": {"ctx": ctx, "parallel": parallel, "kv_type": kv_type,
                       "n_prompt_units": n_prompt_units},
            "completion_code": code,
            "timings": {k: t.get(k) for k in ("prompt_n", "prompt_ms", "prompt_per_second",
                                              "predicted_ms", "predicted_per_second")},
            "kv_empty": kv_empty, "kv_used": kv_used,
            "gpu_mem_baseline_mb": mem_baseline.get("used_mb"),
            "gpu_mem_after_mb": mem_after.get("used_mb"),
            "model_file_bytes": os.path.getsize(MODEL),
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        time.sleep(3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    out_path = args.output or os.path.join(ROOT, "benchmark", "results", "kv_optimization",
                                           "raw", "e9_mem_breakdown.json")
    log_dir = os.path.join(os.path.dirname(out_path), "e9mem")
    os.makedirs(log_dir, exist_ok=True)

    results = {}
    # parallel × ctx 档（F16）
    for parallel in (1, 2, 4):
        for ctx in (1024, 2048, 4096):
            key = f"p{parallel}_c{ctx}"
            results[key] = run_config(ctx, parallel, "f16", 8, log_dir)
            r = results[key]
            print(f"[{key}] code={r.get('completion_code')} "
                  f"used_bytes={r.get('kv_used', {}).get('used_bytes')} "
                  f"prompt_n={r.get('timings', {}).get('prompt_n')}")
    # KV type 档（ctx=2048/p1）
    for kt in ("q8_0", "q4_0"):
        key = f"p1_c2048_{kt}"
        results[key] = run_config(2048, 1, kt, 8, log_dir)
        r = results[key]
        print(f"[{key}] code={r.get('completion_code')} "
              f"used_bytes={r.get('kv_used', {}).get('used_bytes')}")

    with open(out_path, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("saved:", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
