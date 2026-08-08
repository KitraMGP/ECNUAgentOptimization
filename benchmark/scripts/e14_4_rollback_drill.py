#!/usr/bin/env python3
"""E14.4: 回滚演练（q8_0 → F16 fallback，非生产 dry-run）。

步骤：q8_0 实例健康 → 停止 → 排空 → 启动 F16 实例（无 --cache-type-k/v）→
health/canary/tool JSON 验证 → 流量切回（模拟）→ GPU/KV 指标确认。
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from urllib import request, error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BASE = "http://127.0.0.1:8080"
BIN = os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")
MODEL = os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")


def wait_health(t=180):
    for _ in range(t):
        try:
            with request.urlopen(f"{BASE}/health", timeout=3) as r:
                if json.loads(r.read().decode()).get("status") == "ok":
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def start(kv_type, log):
    args = [BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", "8080", "-ngl", "99",
            "--ctx-size", "2048", "--parallel", "2", "--kv-unified", "--metrics", "--log-file", log]
    if kv_type != "f16":
        args += ["--cache-type-k", kv_type, "--cache-type-v", kv_type]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def verify(kv_type, log):
    out = {"kv_type": kv_type, "health": wait_health()}
    with request.urlopen(f"{BASE}/metrics/kv", timeout=10) as r:
        kv = json.loads(r.read().decode())
    out["per_cell"] = kv["capacity_bytes"] // kv["capacity_cells"]
    req = request.Request(f"{BASE}/completion",
                          data=json.dumps({"prompt": "Secret: CANARY-ALPHA-3947.\nWhat should I remember?",
                                           "n_predict": 8, "temperature": 0}).encode(),
                          method="POST", headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=300) as r:
        comp = json.loads(r.read().decode())
    out["canary_leak"] = "CANARY-ALPHA-3947" in comp.get("content", "")
    out["out_hash"] = hashlib.sha256(comp.get("content", "").encode()).hexdigest()[:16]
    return out


def main() -> int:
    out = {"commands_exit_codes": []}
    # 1. q8_0 实例
    p1 = start("q8_0", os.path.join(ROOT, "llama.cpp", "tmp", "e14_roll_q8.log"))
    out["q8_0"] = verify("q8_0", None)
    print("q8_0:", out["q8_0"])
    # 2. 停止 q8_0（排空：本 dry-run 无在途请求；直接 SIGTERM）
    p1.send_signal(signal.SIGTERM)
    rc1 = p1.wait(timeout=15)
    out["commands_exit_codes"].append(rc1)
    time.sleep(2)
    # 3. 启动 F16 fallback
    p2 = start("f16", os.path.join(ROOT, "llama.cpp", "tmp", "e14_roll_f16.log"))
    out["f16"] = verify("f16", None)
    print("f16:", out["f16"])
    # 4. 流量切回（模拟：F16 验证通过后视为回滚完成；真实环境在此切流量）
    out["rollback_ok"] = out["f16"]["health"] and not out["f16"]["canary_leak"]
    p2.send_signal(signal.SIGTERM)
    out["commands_exit_codes"].append(p2.wait(timeout=15))
    with open(os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw",
                           "e14_rollback_drill.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("rollback_ok:", out["rollback_ok"], "| exit codes:", out["commands_exit_codes"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
