#!/usr/bin/env python3
"""E4.2：churn 完整回收验证（≥12 周期，完整时间序列）。"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from urllib import request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
SERVER = os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")
MODEL = os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")
SLOT_SAVE = os.path.join(ROOT, "llama.cpp", "tmp", "e20_slot_save")
BASE = "http://127.0.0.1:8080"

CHUNKS = {
    "S0": "The Corvus Delta network monitors riparian wetlands and upstream runoff.",
    "S1": "The Meridian Plateau observatory tracks seismic activity and solar radiation.",
    "S2": "The Azura Harbor fleet records ocean tides, salinity, and fish migration.",
    "S3": "The Kestrel Ridge station logs wind patterns, canopy cover, and soil moisture.",
}


def build_prompt(n_tokens: int, prefix: str) -> str:
    chars = int(n_tokens * 8.8)
    body = " ".join([prefix] * (chars // len(prefix) + 1))[:chars]
    return body + f"\n\nEND STATE: {prefix[:8]}\nConfirm the state value."


def http_json(method: str, url: str, body=None, timeout: int = 300):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def kv_state():
    try:
        return http_json("GET", f"{BASE}/metrics/kv", timeout=5)
    except Exception as e:
        return {"error": str(e)}


def rss_mb(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        return None


def gpu_mb() -> float | None:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True)
        return float(out.strip().splitlines()[0])
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default="lru")
    ap.add_argument("--cycles", type=int, default=12)
    ap.add_argument("--fixed", action="store_true", help="固定 prompt（相同逻辑状态验证）")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    os.makedirs(SLOT_SAVE, exist_ok=True)
    log_path = f"/tmp/e42_churn_{args.policy}.log"
    proc = subprocess.Popen(
        [SERVER, "-m", MODEL, "--host", "127.0.0.1", "--port", "8080",
         "-ngl", "99", "--ctx-size", "8192", "--parallel", "4",
         "--kv-unified", "--cache-reuse", "0", "--seed", "42", "--temp", "0",
         "--cache-ram", "0", "--slot-save-path", SLOT_SAVE,
         "--unified-idle-slot-policy", args.policy],
        stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    t0 = time.time()
    while time.time() - t0 < 180:
        try:
            if json.loads(http_json("GET", f"{BASE}/health", timeout=3).decode())["status"] == "ok":
                break
        except Exception:
            pass
        time.sleep(1)

    timeline = []
    for c in range(args.cycles):
        cycle = {"cycle": c, "samples": []}
        for sid in ("S0", "S1", "S2", "S3"):
            p = build_prompt(600 if args.fixed else 600 + c * 50, CHUNKS[sid])  # --fixed 时内容不变
            b = {"model": "bench",
                 "messages": [{"role": "user", "content": p}],
                 "max_tokens": 8, "temperature": 0,
                 "chat_template_kwargs": {"enable_thinking": False}}
            http_json("POST", f"{BASE}/v1/chat/completions", b, timeout=300)
        # 周期结束静置采样 3 次（相同逻辑状态 = 4 session 保留，context 已演进）
        for i in range(3):
            k = kv_state()
            cycle["samples"].append({
                "checkpoint": i,
                "used_cells": k.get("used_cells"),
                "capacity_cells": k.get("capacity_cells"),
                "active_sequences": k.get("active_sequences"),
                "shared_cells": k.get("shared_cells"),
                "rss_mb": rss_mb(proc.pid),
                "gpu_mb": gpu_mb(),
            })
            time.sleep(0.5)
        timeline.append(cycle)
        print(f"  cycle {c}: used={cycle['samples'][-1]['used_cells']}")

    # 清空：erase 所有 slot → 回空闲基线
    for slot_id in range(4):
        try:
            http_json("POST", f"{BASE}/slots/{slot_id}?action=erase", {}, timeout=30)
        except Exception:
            pass
    k = kv_state()
    baseline = {"used_cells": k.get("used_cells"), "active_sequences": k.get("active_sequences")}
    print(f"  after erase: {baseline}")

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=15)

    # 解析日志事件计数
    log = open(log_path).read()
    counts = {
        "purge": len(re.findall(r"purging slot", log)),
        "lifecycle_trace_events": len(re.findall(r"__LIFECYCLE_TRACE__", log)),
    }
    out = {"policy": args.policy, "cycles": timeline, "post_erase": baseline,
           "event_counts": counts}
    with open(args.output, "w") as f:
        json.dump(out, f, indent=2)
    print(f"保存: {args.output} counts={counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
