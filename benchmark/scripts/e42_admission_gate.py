#!/usr/bin/env python3
"""E4.2：全活跃压力下的准入、背压与资源回收验证 runner。

场景（unified p4 ctx 8192 cache-ram 0 temp 0 seed 42）：
- A_active_only：4 active（各 ~1200 prompt + 64 生成），无 S4 → 全部完成？
- B_late_arrival：同 A，1.5s 后提交 S4 → active 是否继续完成、S4 被拒/排队
- C_simultaneous：5 请求同时 → admission 次序与结果
- D_recovery：B 后等 active 完成，重试 S4 → 成功且不读错误 KV
- E_ctx_limit：单请求 prompt 超 per-seq ctx → 400（校验阶段错误，与池饱和区分）

每请求记录精确时间线：start/admitted/end + prompt/cached/generated tokens + status。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict
from urllib import request, error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
SERVER = os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")
MODEL = os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")
SLOT_SAVE = os.path.join(ROOT, "llama.cpp", "tmp", "e20_slot_save")
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"

CHUNKS = {
    "S0": "The Corvus Delta network monitors riparian wetlands and upstream runoff.",
    "S1": "The Meridian Plateau observatory tracks seismic activity and solar radiation.",
    "S2": "The Azura Harbor fleet records ocean tides, salinity, and fish migration.",
    "S3": "The Kestrel Ridge station logs wind patterns, canopy cover, and soil moisture.",
    "S4": "The Borealis Ice Shelf survey measures glacier flow, temperature, and pressure.",
}


def build_prompt(n_tokens: int, prefix: str) -> str:
    chars = int(n_tokens * 8.8)
    body = " ".join([prefix] * (chars // len(prefix) + 1))[:chars]
    return body + f"\n\nEND STATE: {prefix[:8]}\nConfirm the state value."


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def http_json(method: str, url: str, body: dict | None = None, timeout: int = 600) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def wait_health(proc: subprocess.Popen, timeout_s: int = 180) -> bool:
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


def start_server(policy: str, log_path: str, trace: bool = False) -> subprocess.Popen:
    os.makedirs(SLOT_SAVE, exist_ok=True)
    cmd = [SERVER, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "-ngl", "99", "--ctx-size", "8192", "--parallel", "4",
           "--kv-unified", "--cache-reuse", "0", "--seed", "42", "--temp", "0",
           "--cache-ram", "0", "--slot-save-path", SLOT_SAVE,
           "--unified-idle-slot-policy", policy]
    if trace:
        cmd.append("--lifecycle-trace")
    logf = open(log_path, "w")
    return subprocess.Popen(cmd, stdout=logf, stderr=logf)


def stop_server(proc: subprocess.Popen) -> bool:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    return proc.returncode is not None


def kv_state() -> Dict[str, Any]:
    try:
        return http_json("GET", f"{BASE}/metrics/kv", timeout=5)
    except Exception as e:
        return {"error": str(e)}


def chat(prompt: str, max_tokens: int, tag: str, timeout: int = 300) -> Dict[str, Any]:
    body = {"model": "bench",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False}}
    t_start = time.perf_counter()
    try:
        d = http_json("POST", f"{BASE}/v1/chat/completions", body, timeout=timeout)
        u = d.get("usage", {})
        return {"tag": tag, "status": "ok",
                "start_s": round(t_start, 3), "end_s": round(time.perf_counter(), 3),
                "prompt_tokens": u.get("prompt_tokens"),
                "cached": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                "completion_tokens": u.get("completion_tokens")}
    except error.HTTPError as e:
        return {"tag": tag, "status": f"http_{e.code}", "start_s": round(t_start, 3),
                "end_s": round(time.perf_counter(), 3),
                "error": e.read().decode()[:300]}
    except Exception as e:
        return {"tag": tag, "status": "exc", "start_s": round(t_start, 3),
                "end_s": round(time.perf_counter(), 3),
                "error": f"{type(e).__name__}: {e}"}


def run_scenario(scenario: str, policy: str, rep: int, trace: bool, log_dir: str) -> Dict[str, Any]:
    log_path = os.path.join(log_dir, f"e42_{scenario}_{policy}_{rep}.log")
    proc = start_server(policy, log_path, trace=trace)
    rec: Dict[str, Any] = {"scenario": scenario, "policy": policy, "rep": rep,
                           "trace": trace, "server_pid": proc.pid,
                           "clean_verified": False, "requests": [], "server_exited": False}
    if not wait_health(proc):
        rec["startup_failure"] = True
        stop_server(proc)
        return rec
    kv0 = kv_state()
    rec["clean_verified"] = kv0.get("used_cells") == 0 and kv0.get("active_sequences") == 0
    rec["capacity_cells"] = kv0.get("capacity_cells")
    rec["kv_baseline"] = kv0.get("used_cells")

    if scenario == "A":
        results = {}
        threads = []
        for sid in ("S0", "S1", "S2", "S3"):
            t = threading.Thread(target=lambda s=sid: results.__setitem__(
                s, chat(build_prompt(800, CHUNKS[s]), 256, s, 300)))
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=300)
        rec["requests"] = [results[s] for s in ("S0", "S1", "S2", "S3")]
        rec["kv_after"] = kv_state()
    elif scenario == "B":
        results = {}
        threads = []
        for sid in ("S0", "S1", "S2", "S3"):
            t = threading.Thread(target=lambda s=sid: results.__setitem__(
                s, chat(build_prompt(800, CHUNKS[s]), 256, s, 300)))
            threads.append(t)
            t.start()
        time.sleep(1.5)  # active 进入 decode 后提交 S4
        results["S4"] = chat(build_prompt(3000, CHUNKS["S4"]), 16, "S4", 300)
        for t in threads:
            t.join(timeout=300)
        rec["requests"] = [results[s] for s in ("S0", "S1", "S2", "S3", "S4")]
        rec["kv_after"] = kv_state()
    elif scenario == "C":
        results = {}
        barrier = threading.Barrier(5)
        def _sim(s):
            barrier.wait()
            results[s] = chat(build_prompt(3000, CHUNKS[s]) if s == "S4" else build_prompt(800, CHUNKS[s]),
                                16 if s == "S4" else 256, s, 300)
        threads = [threading.Thread(target=_sim, args=(s,)) for s in ("S0", "S1", "S2", "S3", "S4")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=300)
        rec["requests"] = [results[s] for s in ("S0", "S1", "S2", "S3", "S4")]
        rec["kv_after"] = kv_state()
    elif scenario == "D":
        results = {}
        threads = []
        for sid in ("S0", "S1", "S2", "S3"):
            t = threading.Thread(target=lambda s=sid: results.__setitem__(
                s, chat(build_prompt(800, CHUNKS[s]), 256, s, 300)))
            threads.append(t)
            t.start()
        time.sleep(1.5)
        results["S4_first"] = chat(build_prompt(3000, CHUNKS["S4"]), 16, "S4", 120)
        for t in threads:
            t.join(timeout=300)
        # 等 active 完成释放资源后重试 S4
        results["S4_retry"] = chat(build_prompt(1000, CHUNKS["S4"]), 16, "S4_retry", 300)
        rec["requests"] = [results[s] for s in ("S0", "S1", "S2", "S3", "S4_first", "S4_retry")]
        rec["kv_after"] = kv_state()
    elif scenario == "E":
        # 单请求 prompt 超 per-seq ctx（8192）→ 校验阶段 400
        big = build_prompt(15000, CHUNKS["S0"])
        rec["requests"].append(chat(big, 8, "ctx_limit", 120))
        rec["kv_after"] = kv_state()

    rec["purge_events"] = []
    rec["lifecycle_trace_events"] = 0
    try:
        with open(log_path, "r", errors="replace") as f:
            log = f.read()
        rec["lifecycle_trace_events"] = len(re.findall(r"__LIFECYCLE_TRACE__", log))
        rec["purge_wrn_count"] = len(re.findall(r"purging slot \d+ with", log))
        rec["http_400"] = log.count("exceeds the available context size")
    except FileNotFoundError:
        pass
    stop_server(proc)
    rec["server_exited"] = proc.returncode is not None
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["A", "B", "C", "D", "E"], required=True)
    ap.add_argument("--policy", choices=["default", "lru"], required=True)
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--log-dir", default="/tmp")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    os.makedirs(args.log_dir, exist_ok=True)
    reps = []
    for i in range(args.reps):
        reps.append(run_scenario(args.scenario, args.policy, i, args.trace, args.log_dir))
        statuses = [r["status"] for r in reps[-1]["requests"]]
        print(f"  [rep{i}] {args.scenario}: statuses={statuses}")
    out = {"scenario": args.scenario, "policy": args.policy, "trace": args.trace, "reps": reps,
           "binary_sha256": sha256(SERVER), "model_sha256": sha256(MODEL),
           "exploratory_wall_time": True,
           "llama_commit": subprocess.check_output(
               ["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
           "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip()}
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"保存: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
