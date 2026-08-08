#!/usr/bin/env python3
"""E14.1: 生产配置启动日志断言验证（K/V type、per-cell、checkpoint off）。"""
import json
import os
import subprocess
import time
from urllib import request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BASE = "http://127.0.0.1:8080"

proc = subprocess.Popen(
    [os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server"),
     "-m", os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf"),
     "--host", "127.0.0.1", "--port", "8080", "-ngl", "99", "--ctx-size", "4096",
     "--parallel", "4", "--kv-unified", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
     "--metrics", "--log-file", os.path.join(ROOT, "llama.cpp", "tmp", "e14_prod.log")],
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
for _ in range(180):
    try:
        with request.urlopen(f"{BASE}/health", timeout=3) as r:
            if json.loads(r.read().decode()).get("status") == "ok":
                break
    except Exception:
        pass
    time.sleep(1)
log = open(os.path.join(ROOT, "llama.cpp", "tmp", "e14_prod.log"), encoding="utf-8", errors="replace").read()
print("=== 启动日志断言 ===")
for line in log.splitlines():
    if "cache_type" in line.lower() or "cache type" in line.lower() or "kv_unified" in line or "n_ctx" in line:
        print(" ", line.strip()[:110])
with request.urlopen(f"{BASE}/metrics/kv", timeout=10) as r:
    kv = json.loads(r.read().decode())
print("capacity_cells:", kv["capacity_cells"], "| per-cell:", kv["capacity_bytes"] // kv["capacity_cells"], "B")
print("checkpoint 相关日志计数（期望 0）:", log.count("E11-CKPT") + log.count("checkpoint_reuse"))
proc.terminate()
try:
    proc.wait(timeout=10)
except Exception:
    proc.kill()
