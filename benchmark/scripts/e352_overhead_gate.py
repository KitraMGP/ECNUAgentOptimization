#!/usr/bin/env python3
"""E3.5.2：默认关闭开销复核 —— 单 server 测量 runner。

每个调用测量一个 server（A=baseline 或 B=candidate）的稳定请求批次：
- 全新 server → health → warmup（1 短请求）→ 固定批次（4 client × 6 轮
  multi_turn = 24 并发请求）→ 记录 wall/吞吐/p50/p95/CPU time/GPU 温度功耗/
  失败/响应 hash → SIGTERM。
排除 server 启动与模型加载时间（只统计请求批次）。

usage: uv run python scripts/e352_overhead_gate.py --binary <path> --tag A|B
  --block <n> --output xxx.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional
from urllib import request, error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
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


def http_json(method: str, url: str, body: Optional[dict] = None, timeout: int = 600) -> Any:
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


def start_server(binary: str, log_path: str) -> subprocess.Popen:
    os.makedirs(SLOT_SAVE, exist_ok=True)
    cmd = [binary, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "-ngl", "99", "--ctx-size", "8192", "--parallel", "4",
           "--kv-unified", "--cache-reuse", "0", "--seed", "42", "--temp", "0",
           "--cache-ram", "0", "--slot-save-path", SLOT_SAVE,
           "--unified-idle-slot-policy", "default"]
    # B（candidate）的 --lifecycle-trace 明确关闭（默认即关闭，显式不传）
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


def gpu_state() -> Dict[str, Any]:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=temperature.gpu,power.draw,utilization.gpu",
             "--format=csv,noheader,nounits"], text=True).strip()
        parts = out.split(",")
        return {"temp_c": float(parts[0]), "power_w": float(parts[1]), "util_pct": float(parts[2])}
    except Exception:
        return {}


class RecordingDriver:
    def __init__(self, driver: Driver) -> None:
        self._d = driver
        self.latencies: List[float] = []
        self.hashes: List[str] = []
        self._orig_chat = driver.chat

    def install(self) -> None:
        self._d.chat = self._chat_wrapper  # type: ignore[method-assign]

    def _chat_wrapper(self, messages: List[dict], _retry: int = 0) -> Dict[str, Any]:
        t0 = time.perf_counter()
        r = self._orig_chat(messages, _retry)
        self.latencies.append((time.perf_counter() - t0) * 1000)
        self.hashes.append(hashlib.sha256((r.get("text") or "").encode()).hexdigest()[:12])
        return r


def run_session(sid: int, wl_params: Dict[str, Any], out: Dict) -> None:
    try:
        driver = Driver(base_url=f"{BASE}/v1", host="127.0.0.1", port=PORT,
                        enable_thinking=False)
        rdr = RecordingDriver(driver)
        rdr.install()
        wl = get_workload("multi_turn")
        spec = wl.generate(dict(wl_params))
        result = wl.run(driver, spec)
        out[sid] = {"ok": True, "latencies": rdr.latencies, "hashes": rdr.hashes,
                    "eval": wl.evaluate(result, spec)}
    except Exception as e:
        out[sid] = {"ok": False, "error": f"{type(e).__name__}: {e}"}


def measure_batch() -> Dict[str, Any]:
    """固定批次：4 client × 6 轮 multi_turn = 24 并发请求。返回统计。"""
    out: Dict[int, Dict] = {}
    threads = []
    t0 = time.perf_counter()
    for sid in range(4):
        t = threading.Thread(target=run_session, args=(sid, {"rounds": 6}, out))
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=600)
    wall_ms = (time.perf_counter() - t0) * 1000
    lats = [l for s in out.values() if s.get("ok") for l in s["latencies"]]
    hashes = [h for s in out.values() if s.get("ok") for h in s["hashes"]]
    ok = sum(1 for s in out.values() if s.get("ok"))
    gen_tokens = sum(len(s.get("latencies", [])) for s in out.values())
    return {
        "wall_ms": round(wall_ms, 1),
        "n_requests": gen_tokens,
        "p50_ms": round(statistics.median(lats), 1) if lats else None,
        "p95_ms": round(sorted(lats)[int(0.95 * len(lats)) - 1], 1) if lats else None,
        "throughput_tps": round(gen_tokens / (wall_ms / 1000), 2) if wall_ms else None,
        "failures": 4 - ok,
        "response_hashes": sorted(set(hashes)),
        "sessions_ok": ok,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True)
    ap.add_argument("--tag", choices=["A", "B"], required=True)
    ap.add_argument("--block", type=str, required=True)
    ap.add_argument("--log-dir", default="/tmp")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    log_path = os.path.join(args.log_dir, f"e352_{args.tag}_{args.block}.log")
    proc = start_server(args.binary, log_path)
    rec: Dict[str, Any] = {"tag": args.tag, "block": args.block,
                           "server_pid": proc.pid, "binary_sha256": sha256(args.binary),
                           "lifecycle_trace": False}
    if not wait_health(proc):
        rec["startup_failure"] = True
        stop_server(proc)
        json.dump(rec, open(args.output, "w"))
        return 0
    # warmup（1 短请求，不计入统计）
    try:
        http_json("POST", f"{BASE}/v1/chat/completions",
                  {"model": "bench", "messages": [{"role": "user", "content": "warmup"}],
                   "max_tokens": 2, "temperature": 0,
                   "chat_template_kwargs": {"enable_thinking": False}}, timeout=120)
    except Exception:
        pass
    rec["gpu_pre"] = gpu_state()
    batch = measure_batch()
    rec["gpu_post"] = gpu_state()
    rec["batch"] = batch
    # CPU process time（server 进程）
    try:
        import psutil
        p = psutil.Process(proc.pid)
        rec["cpu_process_time_s"] = round(sum(p.cpu_times()[:2]), 2)
    except Exception:
        rec["cpu_process_time_s"] = None
    stop_server(proc)
    rec["server_exited"] = proc.returncode is not None
    # 默认关闭事件数（B 的日志应无 __LIFECYCLE_TRACE__）
    try:
        with open(log_path, "r", errors="replace") as f:
            rec["lifecycle_trace_events"] = sum(1 for line in f if "__LIFECYCLE_TRACE__" in line)
    except FileNotFoundError:
        rec["lifecycle_trace_events"] = None
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    print(f"[block {args.block} {args.tag}] wall={batch['wall_ms']}ms "
          f"fails={batch['failures']} trace_events={rec.get('lifecycle_trace_events')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
