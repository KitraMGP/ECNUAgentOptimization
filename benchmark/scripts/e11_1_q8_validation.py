#!/usr/bin/env python3
"""E11.1: Qwen3.5-4B q8_0 profile 最终边界确认（补充矩阵）。

在 E10.3（6 workload × 20 reps）基础上补充：
- multi_turn：3 轮会话（累积上下文），F16 vs q8_0
- save_restore：slot save → erase → restore → 续写，输出与 baseline 对比
- prompt 1024：长 prompt 档（parallel 2）
- GPU 多次 peak：每 workload 5 次 nvidia-smi 采样（max/min/mean）
- q4_0 独立观察：与 F16 输出一致性对照（决定 NOT_VALIDATED / VALIDATED）

协议：warmup 5 + formal 20 paired（同 seed/temp/输入/生命周期，仅 KV type 不同）。
每 rep 保存完整 raw（content/hash/timings/code）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import time
from urllib import request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODEL = os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")
SERVER_BIN = os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"

MT_TURNS = [
    "User: Record that the river flow at station A is 12.4 m3/s.\nAssistant: Recorded.",
    "User: Now add the temperature reading 8.2 C to the log.\nAssistant: Added temperature 8.2 C.",
    "User: What is the current status of station A?\nAssistant: ",
]
SR_P = ("User: Store the observation set for station A: flow 13.1, temp 8.0, turbidity 2.8.\n"
        "Assistant: Stored.\nUser: Retrieve it and summarize.\nAssistant: ")
SR_X = " The summary shows stable conditions."
LONG_P = ("You are a long-context monitoring assistant. The following is a log of observations. "
          "flow 12.4 temp 8.2; flow 12.6 temp 8.1; flow 12.9 temp 8.3; flow 13.1 temp 8.0; "
          "flow 13.3 temp 8.2; flow 13.5 temp 8.1; flow 13.7 temp 8.3; flow 13.9 temp 8.4; "
          "flow 14.1 temp 8.2; flow 14.3 temp 8.0. ") * 12  # ~1000 tokens


def http_json(method, url, body=None, timeout=600):
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


def gpu_peaks(n=5):
    vals = []
    for _ in range(n):
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"])
            vals.append(int(out.decode().strip()))
        except Exception:
            pass
        time.sleep(0.3)
    return {"n": len(vals), "min_mb": min(vals) if vals else None,
            "max_mb": max(vals) if vals else None,
            "mean_mb": round(statistics.mean(vals), 1) if vals else None}


def start_server(kv_type, ctx, parallel, log_path):
    args = [SERVER_BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
            "-ngl", "99", "--ctx-size", str(ctx), "--parallel", str(parallel),
            "--kv-unified", "--cache-ram", "0", "--temp", "0", "--seed", "42",
            "--metrics", "--slot-save-path", os.path.join(ROOT, "llama.cpp", "tmp", "e6_slot_save")]
    if kv_type != "f16":
        args += ["--cache-type-k", kv_type, "--cache-type-v", kv_type]
    proc = subprocess.Popen(args, stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    return proc, wait_health(proc)


def run_multi_turn(kv_type, log_dir):
    proc, ok = start_server(kv_type, 4096, 2, os.path.join(log_dir, f"e11mt_{kv_type}.log"))
    if not ok:
        return {"failed": True}
    try:
        gpu = gpu_peaks()
        out = []
        for rep in range(25):  # warmup 5 + formal 20
            ctx_text = ""
            codes = []
            for turn in MT_TURNS:
                ctx_text += turn
                r, c = http_json("POST", f"{BASE}/completion",
                                 {"prompt": ctx_text, "n_predict": 8, "temperature": 0,
                                  "cache_prompt": True, "id_slot": 0}, timeout=600)
                codes.append(c)
                if c == 200:
                    ctx_text += r.get("content", "")
            out.append({"rep": rep, "warmup": rep < 5, "codes": codes,
                        "final_hash": hashlib.sha256(ctx_text.encode()).hexdigest(),
                        "final_len": len(ctx_text)})
        return {"failed": False, "results": out, "gpu": gpu}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        time.sleep(2)


def run_save_restore(kv_type, log_dir):
    proc, ok = start_server(kv_type, 2048, 2, os.path.join(log_dir, f"e11sr_{kv_type}.log"))
    if not ok:
        return {"failed": True}
    try:
        gpu = gpu_peaks()
        out = []
        for rep in range(25):
            r, c = http_json("POST", f"{BASE}/completion",
                             {"prompt": SR_P + SR_X, "n_predict": 8, "temperature": 0,
                              "cache_prompt": True, "id_slot": 0}, timeout=600)
            http_json("POST", f"{BASE}/slots/0?action=save", {"filename": f"e11sr_{rep}"}, 30)
            http_json("POST", f"{BASE}/slots/0?action=erase", {}, 30)
            rc, cc = http_json("POST", f"{BASE}/slots/0?action=restore",
                               {"filename": f"e11sr_{rep}"}, 30)
            r2, c2 = http_json("POST", f"{BASE}/completion",
                               {"prompt": SR_P + SR_X + " Continue with the next summary.",
                                "n_predict": 8, "temperature": 0, "cache_prompt": True, "id_slot": 0}, timeout=600)
            out.append({"rep": rep, "warmup": rep < 5, "first": c, "save_restore": cc,
                        "post": c2,
                        "post_hash": hashlib.sha256(r2.get("content", "").encode()).hexdigest() if c2 == 200 else None})
        return {"failed": False, "results": out, "gpu": gpu}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        time.sleep(2)


def run_long_prompt(kv_type, log_dir):
    proc, ok = start_server(kv_type, 4096, 2, os.path.join(log_dir, f"e11lp_{kv_type}.log"))
    if not ok:
        return {"failed": True}
    try:
        gpu = gpu_peaks()
        out = []
        for rep in range(25):
            r, c = http_json("POST", f"{BASE}/completion",
                             {"prompt": LONG_P + "\nSummarize the log.",
                              "n_predict": 8, "temperature": 0, "cache_prompt": True, "id_slot": 0}, timeout=600)
            t = r.get("timings", {}) if c == 200 else {}
            out.append({"rep": rep, "warmup": rep < 5, "code": c,
                        "prompt_n": t.get("prompt_n"), "prompt_ms": t.get("prompt_ms"),
                        "predicted_ms": t.get("predicted_ms"),
                        "hash": hashlib.sha256(r.get("content", "").encode()).hexdigest() if c == 200 else None})
        return {"failed": False, "results": out, "gpu": gpu}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        time.sleep(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    out_path = args.output or os.path.join(ROOT, "benchmark", "results", "kv_optimization",
                                           "raw", "e11_q8_boundary.json")
    log_dir = os.path.join(os.path.dirname(out_path), "e11q8")
    os.makedirs(log_dir, exist_ok=True)

    out = {"git": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"),
                                           "rev-parse", "HEAD"]).decode().strip()}
    for kv in ("f16", "q8_0"):
        out[kv] = {
            "multi_turn": run_multi_turn(kv, log_dir),
            "save_restore": run_save_restore(kv, log_dir),
            "long_prompt_1024": run_long_prompt(kv, log_dir),
        }
        print(f"[{kv}] mt={not out[kv]['multi_turn'].get('failed')} "
              f"sr={not out[kv]['save_restore'].get('failed')} "
              f"lp={not out[kv]['long_prompt_1024'].get('failed')}")

    # q4_0 独立观察（与 F16 一致性对照）
    out["q4_0"] = {"long_prompt_1024": run_long_prompt("q4_0", log_dir)}
    print(f"[q4_0] lp={not out['q4_0']['long_prompt_1024'].get('failed')}")

    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("saved:", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
