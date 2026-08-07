#!/usr/bin/env python3
"""E4：内存容量与并发承载基准（exploratory——Laptop GPU wall-time 不作正式性能证据）。

验证维度（与 wall-time 无关，容量指标不受 boost 抖动影响）：
- 可承载并发 session 数（unified 池共享）
- KV cache 使用量（used_cells/capacity 利用率）
- 分配失败率（400/truncation 计数）
- OOM 边界（池满时的最大可承载总 tokens + purge 恢复率）
- 显存峰值（GPU 显存）与 RSS 峰值

场景：
- A_并发承载：N=2/4/6/8 session 并发 × 4 轮 → 成功数/失败数/KV 利用率/显存
- B_OOM边界：6 session 并发 × 递增轮数（4/8/12）→ 边界 + purge 恢复
- C_策略对比（探索性）：default vs lru 的压力下 KV 利用率/失败率

usage: uv run python scripts/e4_memory_capacity_gate.py --scenario A|B|C
  --policy default|lru --n-sessions N --rounds R --reps 1 --output xxx.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import statistics
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List
from urllib import request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
SERVER = os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")
MODEL = os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")
SLOT_SAVE = os.path.join(ROOT, "llama.cpp", "tmp", "e20_slot_save")
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"

sys.path.insert(0, BENCH)
import workload  # noqa: F401
import workload.multi_turn  # noqa: F401
from framework.driver import Driver  # noqa: E402
from framework.workload import get_workload  # noqa: E402


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


def start_server(policy: str, log_path: str, parallel: int = 4) -> subprocess.Popen:
    os.makedirs(SLOT_SAVE, exist_ok=True)
    cmd = [SERVER, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "-ngl", "99", "--ctx-size", "8192", "--parallel", str(parallel),
           "--kv-unified", "--cache-reuse", "0", "--seed", "42", "--temp", "0",
           "--cache-ram", "0", "--slot-save-path", SLOT_SAVE,
           "--unified-idle-slot-policy", policy]
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


def gpu_memory_mb() -> float | None:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True).strip()
        return float(out)
    except Exception:
        return None


def rss_mb() -> float | None:
    try:
        import psutil
        p = psutil.Process(int(subprocess.check_output(
            ["pgrep", "-x", "llama-server"]).decode().strip()))
        return p.memory_info().rss / (1024 * 1024)
    except Exception:
        return None


class RecordingDriver:
    def __init__(self, driver: Driver, sid: int) -> None:
        self._d = driver
        self.sid = sid
        self.statuses: List[str] = []
        self.gen_counts: List[int] = []
        self._orig_chat = driver.chat

    def install(self) -> None:
        self._d.chat = self._chat_wrapper  # type: ignore[method-assign]

    def _chat_wrapper(self, messages: List[dict], _retry: int = 0) -> Dict[str, Any]:
        r = self._orig_chat(messages, _retry)
        self.statuses.append("ok")
        self.gen_counts.append(r.get("completion_tokens", 0))
        return r


def run_session(sid: int, rounds: int, out: Dict) -> None:
    try:
        driver = Driver(base_url=f"{BASE}/v1", host="127.0.0.1", port=PORT,
                        enable_thinking=False)
        rdr = RecordingDriver(driver, sid)
        rdr.install()
        wl = get_workload("multi_turn")
        spec = wl.generate({"rounds": rounds})
        result = wl.run(driver, spec)
        ev = wl.evaluate(result, spec)
        out[sid] = {"ok": True, "n_requests": len(rdr.statuses),
                    "task_success": ev.get("task_success")}
    except Exception as e:
        out[sid] = {"ok": False, "error": f"{type(e).__name__}: {e}"}


def run_replicate(scenario: str, policy: str, n_sessions: int, rounds: int,
                  rep: int, log_dir: str) -> Dict[str, Any]:
    log_path = os.path.join(log_dir, f"e4_{scenario}_{policy}_{n_sessions}_{rounds}_{rep}.log")
    proc = start_server(policy, log_path, parallel=max(4, n_sessions))
    rec: Dict[str, Any] = {"scenario": scenario, "policy": policy,
                           "n_sessions": n_sessions, "rounds": rounds, "rep": rep,
                           "server_pid": proc.pid, "clean_verified": False,
                           "capacity_cells": None, "kv_peak_used_cells": 0,
                           "kv_peak_utilization": None, "gpu_mem_peak_mb": 0.0,
                           "rss_peak_mb": 0.0, "failures": 0, "truncations": 0,
                           "session_results": {}, "server_exited": False}
    if not wait_health(proc):
        rec["startup_failure"] = True
        stop_server(proc)
        return rec
    kv0 = kv_state()
    rec["clean_verified"] = kv0.get("used_cells") == 0 and kv0.get("active_sequences") == 0
    rec["capacity_cells"] = kv0.get("capacity_cells")

    out: Dict[int, Dict] = {}
    threads = []
    for sid in range(n_sessions):
        t = threading.Thread(target=run_session, args=(sid, rounds, out))
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=900)
    rec["session_results"] = out
    ok = [s for s in out.values() if s.get("ok")]
    rec["sessions_ok"] = len(ok)
    rec["failures"] = n_sessions - len(ok)
    rec["task_success_all"] = all(s.get("task_success") is not False for s in ok) if ok else False
    # KV 峰值（并发中采样）
    peak_used = kv0.get("used_cells", 0)
    for _ in range(10):
        kv = kv_state()
        u = kv.get("used_cells", 0)
        if u and u > peak_used:
            peak_used = u
        time.sleep(0.3)
    rec["kv_peak_used_cells"] = peak_used
    cap = rec["capacity_cells"] or 8192
    rec["kv_peak_utilization"] = round(peak_used / cap, 4) if cap else None
    rec["gpu_mem_peak_mb"] = gpu_memory_mb() or 0.0
    rec["rss_peak_mb"] = rss_mb() or 0.0
    # truncation/400 检测（从日志）
    try:
        with open(log_path, "r", errors="replace") as f:
            log = f.read()
        rec["truncations"] = log.count("shift") + log.count("truncat")
        rec["http_400"] = log.count("exceeds the available context size")
    except FileNotFoundError:
        pass
    stop_server(proc)
    rec["server_exited"] = proc.returncode is not None
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["A", "B", "C"], required=True)
    ap.add_argument("--policy", choices=["default", "lru"], required=True)
    ap.add_argument("--n-sessions", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--log-dir", default="/tmp")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    os.makedirs(args.log_dir, exist_ok=True)
    reps = []
    for i in range(args.reps):
        reps.append(run_replicate(args.scenario, args.policy, args.n_sessions,
                                  args.rounds, i, args.log_dir))
        print(f"  [rep{i}] sessions_ok={reps[-1]['sessions_ok']}/{args.n_sessions} "
              f"kv_util={reps[-1]['kv_peak_utilization']} gpu={reps[-1]['gpu_mem_peak_mb']}MB "
              f"fails={reps[-1]['failures']}")
    out = {"scenario": args.scenario, "policy": args.policy, "reps": reps,
           "binary_sha256": sha256(SERVER), "model_sha256": sha256(MODEL),
           "exploratory": True,
           "note": "GPU 性能结果为 exploratory（RTX 4060 Laptop boost 抖动）；容量指标（KV/失败率/显存）为内存验证主体",
           "llama_commit": subprocess.check_output(
               ["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
           "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip()}
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"保存: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
