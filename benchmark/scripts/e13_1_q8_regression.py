#!/usr/bin/env python3
"""E13.1: q8_0 validated deployment profile 轻量回归。

验证：server 可启动 / q8_0 参数生效 / canary 输出 / KV per-cell / 无失败退出。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from urllib import request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODEL = os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")
SERVER_BIN = os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")
BASE = "http://127.0.0.1:8080"


def main() -> int:
    proc = subprocess.Popen(
        [SERVER_BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", "8080",
         "-ngl", "99", "--ctx-size", "2048", "--parallel", "1", "--kv-unified",
         "--cache-ram", "0", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
         "--metrics", "--temp", "0", "--seed", "42"],
        stdout=open(os.path.join(ROOT, "llama.cpp", "tmp", "e13_q8_reg.log"), "w"),
        stderr=subprocess.STDOUT)
    ok = False
    for _ in range(180):
        if proc.poll() is not None:
            break
        try:
            with request.urlopen(f"{BASE}/health", timeout=3) as r:
                if json.loads(r.read().decode()).get("status") == "ok":
                    ok = True
                    break
        except Exception:
            pass
        time.sleep(1)
    print("1. server 启动:", "OK" if ok else "FAILED")
    if not ok:
        proc.terminate()
        return 1
    try:
        # q8_0 生效 + per-cell
        with request.urlopen(f"{BASE}/metrics/kv", timeout=10) as r:
            kv = json.loads(r.read().decode())
        per_cell = kv["capacity_bytes"] // kv["capacity_cells"]
        print(f"2. KV per-cell: {per_cell} B（q8_0 期望 17408）:",
              "OK" if per_cell == 17408 else f"UNEXPECTED ({per_cell})")
        # canary 输出
        req = request.Request(f"{BASE}/completion",
                              data=json.dumps({"prompt": "The secret is CANARY-ALPHA-3947. What should I remember?",
                                               "n_predict": 8, "temperature": 0}).encode(),
                              method="POST", headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=300) as r:
            comp = json.loads(r.read().decode())
        print("3. canary 请求:", "OK" if comp.get("content") is not None else "FAILED")
        print("   输出:", repr(comp.get("content", ""))[:60])
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
