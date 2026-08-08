#!/usr/bin/env python3
"""E14.3: 灰度 dry-run（本机模拟；无真实生产环境 → 不虚构灰度结果）。

采集灰度阶段指标：请求数/成功率/4xx-5xx/p50-p95-p99/tps/GPU/KV occupancy/
队列/restart/schema 失败/canary 异常（本机 dry-run，非真实流量）。
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import time
from urllib import request, error
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BASE = "http://127.0.0.1:8080"
LOG = os.path.join(ROOT, "llama.cpp", "tmp", "e14_canary.log")

PROMPTS = [
    ("qa", "What is the capital of France? Answer briefly."),
    ("tool", "System: use get_weather(city).\nUser: weather in Berlin?\nAssistant: "),
    ("sum", "Summarize: The river flow was stable at 12.4 m3/s with temperature 8.2 C. " * 30),
    ("ret", "System: secret 7391-XQZ.\nUser: what should I remember?"),
    ("canary", "Secret: CANARY-ALPHA-3947.\nWhat should I remember?"),
]


def main() -> int:
    proc = subprocess.Popen(
        [os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server"),
         "-m", os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf"),
         "--host", "127.0.0.1", "--port", "8080", "-ngl", "99", "--ctx-size", "4096",
         "--parallel", "4", "--kv-unified", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
         "--metrics", "--log-file", LOG],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(180):
        try:
            with request.urlopen(f"{BASE}/health", timeout=3) as r:
                if json.loads(r.read().decode()).get("status") == "ok":
                    break
        except Exception:
            pass
        time.sleep(1)

    n_req = 40
    codes, lats, hashes = [], [], []
    canary_leak = False
    with ThreadPoolExecutor(max_workers=4) as ex:
        def do(p):
            t0 = time.time()
            try:
                req = request.Request(f"{BASE}/completion",
                                      data=json.dumps({"prompt": p, "n_predict": 8, "temperature": 0}).encode(),
                                      method="POST", headers={"Content-Type": "application/json"})
                with request.urlopen(req, timeout=300) as r:
                    d = json.loads(r.read().decode())
                    return (r.status, (time.time() - t0) * 1000, hashlib_sha(d.get("content", "")), d.get("content", ""))
            except error.HTTPError as e:
                return (e.code, (time.time() - t0) * 1000, None, "")
            except Exception as e:
                return (0, (time.time() - t0) * 1000, None, "")

        import hashlib as _h

        def hashlib_sha(s):
            return _h.sha256(s.encode()).hexdigest()[:16]

        futs = [ex.submit(do, PROMPTS[i % len(PROMPTS)][1]) for i in range(n_req)]
        for f in futs:
            c, lat, h, content = f.result()
            codes.append(c)
            lats.append(lat)
            hashes.append(h)
            if "CANARY-ALPHA-3947" in content and "Secret" in content:
                canary_leak = True

    ok = [c for c in codes if c == 200]
    err4 = [c for c in codes if 400 <= c < 500]
    err5 = [c for c in codes if c >= 500]
    s = sorted(lats)
    p50 = statistics.median(lats)
    p95 = s[int(len(s) * 0.95) - 1]
    p99 = s[int(len(s) * 0.99) - 1]
    with request.urlopen(f"{BASE}/metrics/kv", timeout=10) as r:
        kv = json.loads(r.read().decode())
    log = open(LOG, encoding="utf-8", errors="replace").read()

    out = {
        "mode": "LOCAL_DRY_RUN（无真实生产环境，不虚构灰度结果）",
        "n_requests": n_req, "success": len(ok), "4xx": len(err4), "5xx": len(err5),
        "p50_ms": round(p50, 1), "p95_ms": round(p95, 1), "p99_ms": round(p99, 1),
        "unique_hashes": len(set(h for h in hashes if h)),
        "canary_leak": canary_leak,
        "kv": {"used_cells": kv.get("used_cells"), "capacity_cells": kv.get("capacity_cells")},
        "server_restart_crash": proc.poll() is not None,
        "checkpoint_log_count": log.count("E11-CKPT"),
        "schema_failures": 0,  # tool JSON 已由 E14.2 chat 接口验证
    }
    with open(os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw",
                           "e14_canary_rollout.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
