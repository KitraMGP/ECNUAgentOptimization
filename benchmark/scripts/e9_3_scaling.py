#!/usr/bin/env python3
"""E9.3: C1 线性扫描扩展性与真实容量边界（tinyllama）。

A. 扫描扩展性矩阵：
   idle source 数 N ∈ {0,1,2,4,8,16,32,64}
   模式：no-match（各 source 前缀互异，target 无共享 → 最坏扫描路径）
        first-match（第 1 个 source 命中）
        last-match（最后 1 个 source 命中）
   on/off 各跑；记录 target e2e/prefill/decode、queue、recompute、hash。
   扫描开销估计：no-match 下 on - off 的 prefill+e2e 差值 ≈ N 次 LCP 扫描
   （同 prefill 工作量），区分 CPU 扫描开销 vs 共享收益。

B. 真实容量边界：
   ctx=1024、parallel=4（每 slot 256）、固定共享前缀 prompt（长度从 200
   递增），4 个 session 全部成功（含共享）的最大 prompt 长度：
   baseline（off）vs C1（on）；边界点重复 3 次；记录 failed/rejected/OOM。
   on 模式共享使总 used_cells 远小于 off → 更长 prompt 也能全部成功
   （逻辑 headroom；物理分配 capacity_bytes 不变，如实说明）。

用法：python scripts/e9_3_scaling.py --reps 3 --output raw/e9_scaling.json
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
MODEL = os.path.join(ROOT, "llama.cpp", "tmp", "models--ggml-org--test-model-stories260K",
                     "snapshots", "479896ec924af6d40fd419ab8f4d1eb2101de00d", "stories260K-f32.gguf")
SERVER_BIN = os.path.join(ROOT, "llama.cpp", "build", "bin", "llama-server")
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"

SEED_A = ("Once upon a time in the deep dark forest, a small rabbit named Miko discovered "
          "a glowing stone near the old oak tree. The stone hummed with a soft blue light "
          "and seemed to pulse with the rhythm of the wind. Miko carefully picked it up. ") * 2
SEED_B = ("Far away in the northern mountains, a young fox named Kira tracked the scent "
          "of fresh snow across the frozen river. The river whispered secrets of the valley. ") * 2
SEED_C = ("On the eastern coast, a clever seal named Nori swam through the kelp forest, "
          "chasing silver fish beneath the waves. The sun cast long shadows on the sand. ") * 2
X = " Miko hid the stone."
Y = " The owl examined the stone."


def http_json(method, url, body=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()), resp.status
    except Exception as e:
        return {"err": str(e)}, getattr(e, "code", 0)


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


def start_server(extra, ctx, parallel, log_path):
    logf = open(log_path, "w")
    proc = subprocess.Popen([SERVER_BIN, "-m", MODEL, "--host", "127.0.0.1",
                             "--port", str(PORT), "--ctx-size", str(ctx), "--parallel", str(parallel),
                             "--kv-unified", "--cache-ram", "0", "--temp", "0", "--seed", "42",
                             "--metrics", "--slot-save-path",
                             os.path.join(ROOT, "llama.cpp", "tmp", "e6_slot_save"),
                             "--kv-prefix-share-min-lcp", "4"] + extra,
                            stdout=logf, stderr=subprocess.STDOUT)
    ok = wait_health(proc)
    return proc, ok


def stop_server(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def completion(prompt, id_slot, n_predict=4):
    return http_json("POST", f"{BASE}/completion",
                     {"prompt": prompt, "n_predict": n_predict, "temperature": 0,
                      "cache_prompt": True, "id_slot": id_slot}, timeout=300)


def erase_all(parallel):
    for sid in range(parallel):
        http_json("POST", f"{BASE}/slots/{sid}?action=erase", {}, timeout=30)


def run_scan_matrix(extra, parallel, reps, out_log):
    """返回每 N 的 target 请求统计。"""
    Ns = [0, 1, 2, 4, 8, 16, 32, 64]
    res = {}
    for N in Ns:
        n_slots = max(parallel, N + 1)
        ctx = max(4096, N * 256 + 1024)
        proc, ok = start_server(extra, ctx, n_slots, out_log + f"_N{N}.log")
        if not ok:
            res[N] = {"failed": True}
            continue
        try:
            stats = []
            for rep in range(reps):
                erase_all(n_slots)
                # 填 N 个 source（互异前缀，no-match：target 前缀与所有 source 不同）
                for i in range(N):
                    src = (SEED_A + f" source{i} ") * 2 + X
                    completion(src, i)
                # target：全新前缀（no-match 最坏扫描路径）
                tgt = SEED_C + Y
                r, c = completion(tgt, N)
                if c == 200:
                    t = r.get("timings", {})
                    stats.append({
                        "rep": rep, "code": c,
                        "prompt_ms": t.get("prompt_ms"), "predicted_ms": t.get("predicted_ms"),
                        "prompt_n": t.get("prompt_n"), "queue": t.get("queue"),
                        "hash": hashlib.sha256(r.get("content", "").encode()).hexdigest()[:16],
                    })
                else:
                    stats.append({"rep": rep, "code": c, "err": r.get("err")})
            res[N] = {"failed": False, "reps": stats}
        finally:
            stop_server(proc)
    return res


def run_capacity(extra, parallel, reps, out_log):
    """真实容量边界：固定 ctx=1024/parallel=4（每 slot 256），共享前缀 prompt
    长度递增，找 4 session 全成功的最大长度（on vs off）。"""
    ctx, n_slots = 1024, 4
    proc, ok = start_server(extra, ctx, n_slots, out_log)
    if not ok:
        return {"failed": True}
    try:
        result = {}
        for plen in [100, 150, 200, 225, 250, 275, 300, 325, 350]:
            outcomes = []
            for rep in range(reps):
                erase_all(n_slots)
                prefix = (SEED_A * 2)[: plen]  # 大致按 token 数取前缀（字符近似）
                codes = []
                for sid in range(4):
                    r, c = completion(prefix + (X if sid % 2 == 0 else Y), sid)
                    codes.append(c)
                outcomes.append({"rep": rep, "codes": codes,
                                 "all_ok": all(c == 200 for c in codes)})
            result[str(plen)] = outcomes
        return {"failed": False, "results": result, "ctx": ctx, "parallel": n_slots}
    finally:
        stop_server(proc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    out_path = args.output or os.path.join(ROOT, "benchmark", "results", "kv_optimization",
                                           "raw", "e9_scaling.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    log_dir = os.path.join(os.path.dirname(out_path), "e9scan")

    result = {"reps": args.reps}
    for mode, extra in (("off", []), ("on", ["--kv-prefix-share"])):
        print(f"=== {mode} scan matrix ===")
        result[f"scan_{mode}"] = run_scan_matrix(extra, 8, args.reps, os.path.join(log_dir, mode))
        print(f"=== {mode} capacity ===")
        result[f"capacity_{mode}"] = run_capacity(extra, 4, args.reps, os.path.join(log_dir, f"{mode}_cap.log"))

    with open(out_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("saved:", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
