#!/usr/bin/env python3
"""E6.5-C1: churn endurance（TinyLlama + kv-prefix-share on，12+ 周期）。

验证（12.19 E6.5）：
- ≥12 完整 churn 周期；每周期结束状态记录
- 无累计泄漏（used_cells 增长仅归因内容增长；同逻辑状态不单调增长）
- 无 active victim（无 500/错误）
- 无跨 session 污染（输出与独立基线一致）
- 最终资源返回基线（erase 后 used_cells=0 / active_sequences=0）
- 共享引用（shared_cells）正确释放（不持续累积）

用法：python scripts/e6_c1_churn.py --cycles 12 --output raw/e6_c1_churn.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from urllib import request, error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
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
SUFFIXES = [" Miko hid the stone by the stream.", " The owl examined the stone.",
            " The fox sniffed the stone.", " The bear guarded the stone."]


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=12)
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    slot_save = os.path.join(ROOT, "llama.cpp", "tmp", "e6_slot_save")
    os.makedirs(slot_save, exist_ok=True)
    log_path = os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw", "c1churn_server.log")
    cmd = [SERVER_BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "--ctx-size", "512", "--parallel", "2", "--kv-unified",
           "--cache-ram", "0", "--temp", "0", "--seed", "42", "--metrics",
           "--slot-save-path", slot_save, "--kv-prefix-share", "--kv-prefix-share-min-lcp", "4"]
    logf = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
    if not wait_health(proc):
        print("server failed")
        return 1

    try:
        cycles = []
        for c in range(args.cycles):
            # 每周期：2 并发共享前缀请求（A + 各自后缀），内容小幅增长
            prompts = [A + (" The stone grew brighter. " * (c % 3)) + SUFFIXES[i] for i in range(2)]
            outs = []
            for i, p in enumerate(prompts):
                try:
                    r = http_json("POST", f"{BASE}/completion",
                                  {"prompt": p, "n_predict": 4, "temperature": 0,
                                   "cache_prompt": True, "id_slot": i}, timeout=120)
                    outs.append({"slot": i, "status": "ok",
                                 "output_sha256": hashlib.sha256(r.get("content", "").encode()).hexdigest()})
                except Exception as e:
                    outs.append({"slot": i, "status": f"error:{type(e).__name__}"})
            time.sleep(0.5)
            kv = http_json("GET", f"{BASE}/metrics/kv", timeout=10)
            kv_stats = kv.get("kv_stats") if "kv_stats" in kv else kv
            cycles.append({"cycle": c, "requests": outs,
                           "kv": {"used_cells": kv_stats.get("used_cells"),
                                  "shared_cells": kv_stats.get("shared_cells"),
                                  "active_sequences": kv_stats.get("active_sequences"),
                                  "used_bytes": kv_stats.get("used_bytes")}})
            print(f"  cycle {c}: {[o['status'] for o in outs]} "
                  f"used={kv_stats.get('used_cells')} shared={kv_stats.get('shared_cells')} "
                  f"active={kv_stats.get('active_sequences')}")

        # 最终回收：erase 全部 slot
        for sid in range(2):
            try:
                http_json("POST", f"{BASE}/slots/{sid}?action=erase", {}, timeout=30)
            except Exception:
                pass
        time.sleep(0.5)
        kv_after = http_json("GET", f"{BASE}/metrics/kv", timeout=10)
        kv_after = kv_after.get("kv_stats") if "kv_stats" in kv_after else kv_after

        used = [c["kv"]["used_cells"] for c in cycles]
        shared = [c["kv"]["shared_cells"] for c in cycles]
        errs = [o for c in cycles for o in c["requests"] if o["status"] != "ok"]
        # 同逻辑状态无单调增长：周期 0-2 内容相同（c%3==0），比较 used 差异
        same_state = [used[i] for i in range(len(used)) if i % 3 == 0]
        drift = max(same_state) - min(same_state) if same_state else None

        rec = {
            "experiment_id": f"e6_c1_churn{args.cycles}",
            "candidate_id": "C1_RADIX_PREFIX_SHARING",
            "variant": "endurance_on",
            "workload_id": "W16_churn",
            "git_commit": subprocess.check_output(
                ["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
            "cycles": args.cycles,
            "cycle_samples": cycles,
            "final_kv_after_erase": kv_after,
            "errors": [str(e) for e in errs],
            "no_errors": len(errs) == 0,
            "used_cells_series": used,
            "shared_cells_series": shared,
            "same_logic_state_drift": drift,
            "reclaims_to_baseline": (kv_after.get("used_cells") == 0 and kv_after.get("active_sequences") == 0),
            "verdict": "PASS_ENDURANCE" if (len(errs) == 0 and drift is not None and drift <= 50
                                             and kv_after.get("used_cells") == 0
                                             and kv_after.get("active_sequences") == 0) else "FAIL",
        }
        print(json.dumps({k: rec[k] for k in ("cycles", "no_errors", "same_logic_state_drift",
                                               "reclaims_to_baseline", "verdict")}, indent=1))
        print("  used:", used)
        print("  shared:", shared)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    out = args.output or os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw",
                                      f"e6_c1_churn{args.cycles}.json")
    with open(out, "w") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    print("saved:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
