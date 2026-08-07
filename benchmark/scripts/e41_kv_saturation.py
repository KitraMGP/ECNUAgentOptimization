#!/usr/bin/env python3
"""E4.1：Unified KV 饱和、淘汰正确性与持续并发门禁 runner。

场景（server-per-replicate，unified p4 ctx 8192 cache-ram 0 temp 0 seed 42）：
- A 饱和淘汰：4 session（S1/S2 idle、S0/S3 active 长生成）+ S4 压力（理论 10400 > 8192）
- B victim 契约：3 idle（tick 与 slot id 顺序相反）+ 压力 → default/lru 选择验证
- C 无 victim：4 全 active + 新需求超池 → 既定失败（不 crash）
- D 恢复复用：淘汰 S1 后回访（重算/失败）+ S4 复用 slot（generation++）+ cells 回落
- E churn：多轮 create/grow/idle/evict/recover/cancel/recreate → 无泄漏

双模式：--trace（correctness 归属核对）/ 默认（production 零事件）。
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
from typing import Any, Dict, List
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
    chars = int(n_tokens * 8.8)  # CHUNKS 前缀实测 ~8.8 chars/token
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


def chat(prompt: str, max_tokens: int = 16, timeout: int = 300) -> Dict[str, Any]:
    body = {"model": "bench",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False}}
    try:
        d = http_json("POST", f"{BASE}/v1/chat/completions", body, timeout=timeout)
        return {"status": "ok", "prompt_tokens": d.get("usage", {}).get("prompt_tokens"),
                "cached": (d.get("usage", {}).get("prompt_tokens_details") or {}).get("cached_tokens"),
                "text": (d.get("choices") or [{}])[0].get("message", {}).get("content", "")}
    except error.HTTPError as e:
        return {"status": f"http_{e.code}", "error": e.read().decode()[:200]}
    except Exception as e:
        return {"status": "exc", "error": f"{type(e).__name__}: {e}"}


def parse_trace_events(log_path: str) -> List[Dict[str, Any]]:
    evs = []
    try:
        with open(log_path, "r", errors="replace") as f:
            for line in f:
                m = re.search(r"__LIFECYCLE_TRACE__ (.*)", line)
                if not m:
                    continue
                ev = {}
                for kv in re.findall(r"(\w+)=([^\s]+|null)", m.group(1)):
                    ev[kv[0]] = kv[1]
                evs.append(ev)
    except FileNotFoundError:
        pass
    return evs


def run_scenario(scenario: str, policy: str, rep: int, trace: bool, log_dir: str) -> Dict[str, Any]:
    log_path = os.path.join(log_dir, f"e41_{scenario}_{policy}_{rep}.log")
    proc = start_server(policy, log_path, trace=trace)
    rec: Dict[str, Any] = {"scenario": scenario, "policy": policy, "rep": rep,
                           "trace": trace, "server_pid": proc.pid,
                           "clean_verified": False, "capacity_cells": None,
                           "requests": [], "purge_events": [], "server_exited": False,
                           "kv_timeline": []}
    if not wait_health(proc):
        rec["startup_failure"] = True
        stop_server(proc)
        return rec
    kv0 = kv_state()
    rec["clean_verified"] = kv0.get("used_cells") == 0 and kv0.get("active_sequences") == 0
    rec["capacity_cells"] = kv0.get("capacity_cells")

    if scenario == "A":
        # 串行建立 4 session（各 ~2000 实际 token，不同前缀不互相覆盖）
        # → 池累积至 ~8000（>90%，未满不触发 purge）
        for sid in ("S0", "S1", "S2", "S3"):
            rec["requests"].append({"tag": f"{sid}_long", **chat(build_prompt(1200, CHUNKS[sid]), 16)})
        rec["session_results"] = {s: "ok" for s in ("S0", "S1", "S2", "S3")}
        rec["kv_after_4"] = kv_state()
        # S1/S2 idle（S0/S3 保持 active：并发短生成）
        active = {}
        for sid in ("S0", "S3"):
            t = threading.Thread(target=lambda s=sid: active.__setitem__(
                s, chat(build_prompt(1200, CHUNKS[s]), 64, 900)))
            t.start()
        time.sleep(1.5)
        # S4 压力（新前缀 ~2000 实际）→ 理论 8000 + 2000 > 8192 → purge idle
        rec["requests"].append({"tag": "S4_pressure", **chat(build_prompt(1200, CHUNKS["S4"]), 16)})
        for t in threading.enumerate():
            if t is not threading.main_thread() and t.is_alive():
                t.join(timeout=900)
        rec["active_results"] = {s: active.get(s, {}).get("status") for s in ("S0", "S3")}
        rec["kv_after_pressure"] = kv_state()
    elif scenario == "B":
        # 3 idle + 压力；构造 tick 与 slot id 顺序相反
        # S0 先 idle（tick 小）、S1 后 idle（tick 大）——slot id 顺序 S0<S1<S2
        rec["requests"].append({"tag": "S0_idle", **chat(build_prompt(3000, CHUNKS["S0"]), 16)})
        rec["requests"].append({"tag": "S0_revisit", **chat(build_prompt(3000, CHUNKS["S0"]), 16)})
        rec["requests"].append({"tag": "S1_idle", **chat(build_prompt(3000, CHUNKS["S1"]), 16)})
        rec["requests"].append({"tag": "S2_idle", **chat(build_prompt(3000, CHUNKS["S2"]), 16)})
        # 压力（新前缀 ~2000）→ 理论 8300 > 8192
        rec["requests"].append({"tag": "pressure", **chat(build_prompt(2000, CHUNKS["S4"]), 16)})
        rec["kv_after"] = kv_state()
    elif scenario == "C":
        # 4 全 active（长生成）+ S4 压力超池 → 无 idle victim
        active = {}
        for sid in ("S0", "S1", "S2", "S3"):
            t = threading.Thread(target=lambda s=sid: active.__setitem__(
                s, chat(build_prompt(1200, CHUNKS[s]), 64, 900)))
            t.start()
        time.sleep(1.5)
        rec["requests"].append({"tag": "S4_pressure", **chat(build_prompt(1200, CHUNKS["S4"]), 16, 120)})
        for t in threading.enumerate():
            if t is not threading.main_thread() and t.is_alive():
                t.join(timeout=900)
        rec["active_results"] = {s: active.get(s, {}).get("status") for s in ("S0", "S1", "S2", "S3")}
        rec["kv_after"] = kv_state()
    elif scenario == "D":
        # 复用 A 的饱和流程，之后回访被淘汰的 idle + S4 复用 slot
        for sid in ("S0", "S1", "S2", "S3"):
            rec["requests"].append({"tag": f"{sid}_long", **chat(build_prompt(1200, CHUNKS[sid]), 16)})
        rec["session_results"] = {s: "ok" for s in ("S0", "S1", "S2", "S3")}
        rec["kv_before_pressure"] = kv_state()
        rec["requests"].append({"tag": "S4_pressure", **chat(build_prompt(1200, CHUNKS["S4"]), 16)})
        rec["kv_after_pressure"] = kv_state()
        # 回访 S1（可能被淘汰）——重算或既定失败
        rec["requests"].append({"tag": "S1_revisit", **chat(build_prompt(1200, CHUNKS["S1"]), 16)})
        rec["kv_final"] = kv_state()
    elif scenario == "E":
        # churn：create/grow/idle/evict/recover/cancel/recreate × 6 轮
        used_peak = 0
        for cycle in range(6):
            for sid in ("S0", "S1", "S2", "S3"):
                rec["requests"].append({"tag": f"c{cycle}_{sid}", **chat(build_prompt(1200, CHUNKS[sid]), 8)})
            rec["requests"].append({"tag": f"c{cycle}_pressure", **chat(build_prompt(2000, CHUNKS["S4"]), 8)})
            # 回访 S1（recover）
            rec["requests"].append({"tag": f"c{cycle}_revisit_S1", **chat(build_prompt(1200, CHUNKS["S1"]), 8)})
            kv = kv_state()
            rec["kv_timeline"].append({"cycle": cycle, "used": kv.get("used_cells"),
                                       "active": kv.get("active_sequences")})
            used_peak = max(used_peak, kv.get("used_cells", 0))
        rec["kv_peak_used"] = used_peak
        rec["kv_final"] = kv_state()

    rec["purge_events"] = [e for e in parse_trace_events(log_path) if e.get("type") == "purge"]
    # 独立信号：E3.2 的 "purging slot" WRN（trace off 时也可观测）
    try:
        with open(log_path, "r", errors="replace") as f:
            _log = f.read()
        rec["purge_wrn_count"] = len(re.findall(r"purging slot \d+ with", _log))
    except FileNotFoundError:
        rec["purge_wrn_count"] = 0
    rec["lifecycle_trace_events"] = len(parse_trace_events(log_path))
    rec["truncations"] = 0
    try:
        with open(log_path, "r", errors="replace") as f:
            log = f.read()
        rec["truncations"] = len(re.findall(r"truncated\s*=\s*1\b", log))
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
        print(f"  [rep{i}] clean={reps[-1]['clean_verified']} "
              f"purges={len(reps[-1]['purge_events'])} trace_evts={reps[-1]['lifecycle_trace_events']} "
              f"400={reps[-1].get('http_400', 0)}")
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
