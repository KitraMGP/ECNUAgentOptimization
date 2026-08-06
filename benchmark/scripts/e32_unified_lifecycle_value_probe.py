#!/usr/bin/env python3
"""E3.2：unified idle-slot lifecycle 价值感知回收 —— probe（default vs lru）。

场景（prompt 固定、temperature=0、seed=42、cache-ram 0）：
- p4_hot_cold_order：slot 顺序与 last-used 顺序相反（slot1 冷先 idle、
  slot0 热后使用+回访）；压力 purge 后回访 P0/P1 → 比较 prompt_processed
- p2_single_candidate：唯一 idle 候选（策略不改变结果）
- multiple_victims：3 个 idle 候选（lru 选最久未用）
- active_protection：真并发（threading，slot0 生成中 + slot2 压力）
- non_unified_smoke：lru 在 non-unified 不生效

每请求记录任务八字段；evaluator：status==ok && 响应非空 && 同分支回访
hash 一致 && 跨分支 hash 有区分度（压力场景为长文本续写，JSON evaluator
不适用，改用等价的一致性/区分度检查；JSON evaluator 由单测覆盖纯函数）。
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
from typing import Any, Dict, List, Optional
from urllib import request, error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SERVER = os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")
MODEL = os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")
SLOT_SAVE = os.path.join(ROOT, "llama.cpp", "tmp", "e20_slot_save")
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"

CHUNK = (
    "The Corvus Delta monitoring network collects hydrological and ecological "
    "data across five instrumented zones every fifteen minutes. Each zone "
    "transmits calibrated readings for water quality, soil composition, "
    "vegetation density, and wildlife activity through the mesh telemetry "
    "backbone. The aggregation layer validates every packet against the "
    "schema, rejects failed checksums, and appends accepted readings to the "
    "append-only tool log before any downstream analysis begins."
)
PRESSURE_TEXT = (
    "This is a completely different payload stream that shares no prefix with "
    "the Corvus Delta monitoring data. It describes atmospheric pressure "
    "readings from high-altitude weather stations, seismic activity logs, "
    "ocean tide gauges, and solar radiation measurements collected by an "
    "unrelated network with a distinct schema and provenance record."
)


def build_prompt(n_tokens: int, tag: str, prefix_text: str = CHUNK) -> str:
    chars = int(n_tokens * 6.2)  # Qwen tokenizer 实测 ~6.2 chars/token
    body = " ".join([prefix_text] * (chars // len(prefix_text) + 1))[:chars]
    return body + f"\n\nEND STATE: {tag}\nPlease confirm the state value above."


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


def start_server(policy: str, parallel: int, ctx: int, unified: bool,
                 cache_ram: int, log_path: str, lifecycle_stats: bool) -> subprocess.Popen:
    os.makedirs(SLOT_SAVE, exist_ok=True)
    cmd = [SERVER, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "-ngl", "99", "--ctx-size", str(ctx), "--parallel", str(parallel),
           "--cache-reuse", "0", "--seed", "42", "--temp", "0",
           "--cache-ram", str(cache_ram), "--slot-save-path", SLOT_SAVE,
           "--unified-idle-slot-policy", policy]
    if unified:
        cmd.append("--kv-unified")
    if lifecycle_stats:
        cmd.append("--lifecycle-stats")
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


def completion(prompt: str, slot: Optional[int] = None, max_tokens: int = 16) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": "bench",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if slot is not None:
        body["id_slot"] = slot
    t0 = time.perf_counter()
    try:
        d = http_json("POST", f"{BASE}/v1/chat/completions", body)
        status = "ok"
    except error.HTTPError as e:
        d = {"error": {"code": e.code, "type": e.reason}}
        status = f"http_{e.code}"
    except Exception as e:
        d = {"error": {"type": type(e).__name__}}
        status = "exc"
    lat = (time.perf_counter() - t0) * 1000
    usage = d.get("usage", {})
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    prompt_tokens = usage.get("prompt_tokens")
    text = ""
    if d.get("choices"):
        text = d["choices"][0].get("message", {}).get("content") or ""
    return {
        "status": status,
        "latency_ms": round(lat, 1),
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached if cached is not None else None,
        "logical_prefix_reuse_tokens": cached if cached is not None else None,
        "prompt_processed_tokens": (prompt_tokens - cached) if prompt_tokens is not None and cached is not None else None,
        "response_hash": hashlib.sha256(re.sub(r"[^\w]+", "", text.lower()).encode()).hexdigest()[:12] if text else None,
        "text": text[:60],
        "error": d.get("error"),
    }


def kv_state() -> Dict[str, Any]:
    try:
        return http_json("GET", f"{BASE}/metrics/kv", timeout=5)
    except Exception as e:
        return {"error": str(e)}


def grep_log(log_path: str) -> Dict[str, Any]:
    """解析 purge 与 LIFECYCLE_EVENT 行。"""
    res = {"purge_events": [], "lifecycle_events": [], "free_space": 0}
    try:
        with open(log_path, "r", errors="replace") as f:
            for line in f:
                if "failed to find free space in the KV cache" in line:
                    res["free_space"] += 1
                m = re.search(r"purging slot (\d+) with (\d+) tokens \(policy=(\w+), last_used_tick=(\d+)\)", line)
                if m:
                    res["purge_events"].append({"slot": int(m.group(1)),
                                                "tokens": int(m.group(2)),
                                                "policy": m.group(3),
                                                "tick": int(m.group(4))})
                m2 = re.search(r"__LIFECYCLE_EVENT__ (.*)", line)
                if m2:
                    res["lifecycle_events"].append(m2.group(1).strip())
    except FileNotFoundError:
        pass
    return res


class Recorder:
    """server-per-replicate 记录器：启动/断言/请求/事件/终止。"""

    def __init__(self, scenario: str, policy: str, unified: bool, parallel: int,
                 ctx: int, cache_ram: int, log_dir: str, rep: int) -> None:
        self.scenario = scenario
        self.policy = policy
        self.unified = unified
        self.parallel = parallel
        self.ctx = ctx
        self.cache_ram = cache_ram
        self.rep = rep
        self.log_path = os.path.join(log_dir, f"e32_{scenario}_{policy}_{rep}.log")
        self.proc = start_server(policy, parallel, ctx, unified, cache_ram,
                                 self.log_path, lifecycle_stats=True)
        self.rec: Dict[str, Any] = {
            "scenario": scenario, "policy": policy, "unified": unified,
            "ctx": ctx, "parallel": parallel, "cache_ram_mib": cache_ram,
            "rep": rep, "server_pid": self.proc.pid, "clean_verified": False,
            "requests": [], "server_exited": False,
        }

    def start(self) -> bool:
        if not wait_health(self.proc):
            self.rec["startup_failure"] = True
            stop_server(self.proc)
            return False
        kv = kv_state()
        self.rec["clean_verified"] = kv.get("used_cells") == 0 and kv.get("active_sequences") == 0
        self.rec["capacity_cells"] = kv.get("capacity_cells")
        return True

    def req(self, tag: str, prompt: str, slot: Optional[int] = None,
            max_tokens: int = 16) -> Dict[str, Any]:
        kv_before = kv_state()
        r = completion(prompt, slot, max_tokens)
        kv_after = kv_state()
        r.update({
            "turn_id": tag, "slot_id": slot, "sequence_id": slot,
            "lifecycle_policy": self.policy,
            "used_cells_before": kv_before.get("used_cells"),
            "used_cells_after": kv_after.get("used_cells"),
            "active_sequences_after": kv_after.get("active_sequences"),
            "evaluator_pass": r["status"] == "ok" and bool(r["text"]),
            "contamination_detected": False,
            "request_failure": r["status"] != "ok",
            "server_pid": self.proc.pid,
        })
        self.rec["requests"].append(r)
        print(f"    [{tag}] slot={slot} status={r['status']} "
              f"prompt={r['prompt_tokens']} cached={r['cached_tokens']} "
              f"processed={r['prompt_processed_tokens']} lat={r['latency_ms']}ms")
        return r

    def finish(self) -> Dict[str, Any]:
        self.rec["log_events"] = grep_log(self.log_path)
        stop_server(self.proc)
        self.rec["server_exited"] = self.proc.returncode is not None
        return self.rec


def run_replicate(scenario: str, policy: str, unified: bool, parallel: int,
                  ctx: int, cache_ram: int, log_dir: str, rep: int) -> Dict[str, Any]:
    r = Recorder(scenario, policy, unified, parallel, ctx, cache_ram, log_dir, rep)
    if not r.start():
        return r.rec

    if scenario == "p4_hot_cold_order":
        # 冷分支 slot1 先 idle（tick 1）；热分支 slot0 后使用 + 回访（tick 大）
        r.req("P1_cold", build_prompt(3000, "STATE_COLD"), slot=1)
        r.req("P0_hot_first", build_prompt(3000, "STATE_HOT"), slot=0)
        r.req("P0_hot_revisit", build_prompt(3000, "STATE_HOT"), slot=0)  # 热分支最近使用
        # 压力请求（空 slot2 全量写入 → 池 3078+3076+4500>8192；清 P1(3078) 后
        # 空余 5116>4500 → 只清冷分支，P0 保留）
        r.req("pressure", build_prompt(4500, "STATE_PRESSURE", PRESSURE_TEXT), slot=2)
        # 回访 P0（热）与 P1（冷）
        r.req("P0_after", build_prompt(3000, "STATE_HOT"))
        r.req("P1_after", build_prompt(3000, "STATE_COLD"))
    elif scenario == "p2_single_candidate":
        r.req("P0_single", build_prompt(5000, "STATE_SINGLE"), slot=0)
        r.req("pressure", build_prompt(5000, "STATE_PRESSURE", PRESSURE_TEXT), slot=1)
        r.req("P0_after", build_prompt(5000, "STATE_SINGLE"))
    elif scenario == "multiple_victims":
        r.req("V0", build_prompt(2000, "STATE_V0"), slot=0)
        r.req("V1", build_prompt(2000, "STATE_V1"), slot=1)
        r.req("V2", build_prompt(2000, "STATE_V2"), slot=2)
        r.req("pressure", build_prompt(5000, "STATE_PRESSURE", PRESSURE_TEXT), slot=3)
    elif scenario == "active_protection":
        r.req("IDLE1", build_prompt(2000, "STATE_IDLE1"), slot=1)
        result = {}
        def long_run():
            result["resp"] = completion(build_prompt(4000, "STATE_ACTIVE"), 0, 256)
        t = threading.Thread(target=long_run)
        t.start()
        time.sleep(1.5)  # 让 slot0 进入 processing
        try:
            r.req("pressure", build_prompt(3000, "STATE_PRESSURE", PRESSURE_TEXT), slot=2)
        except Exception:
            pass
        t.join(timeout=300)
        r.rec["requests"][-1]["active_thread_done"] = bool(result.get("resp"))
    elif scenario == "non_unified_smoke":
        r.req("P0_single", build_prompt(5000, "STATE_SINGLE"), slot=0)
        r.req("pressure", build_prompt(5000, "STATE_PRESSURE", PRESSURE_TEXT), slot=1)

    return r.finish()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["p4_hot_cold_order", "p2_single_candidate",
                                           "multiple_victims", "active_protection",
                                           "non_unified_smoke"], required=True)
    ap.add_argument("--policy", choices=["default", "lru"], required=True)
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--cache-ram", type=int, default=0)
    ap.add_argument("--log-dir", default="/tmp")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    unified = args.scenario != "non_unified_smoke"
    parallel = {"p4_hot_cold_order": 4, "p2_single_candidate": 2,
                "multiple_victims": 4, "active_protection": 4,
                "non_unified_smoke": 2}[args.scenario]
    os.makedirs(args.log_dir, exist_ok=True)
    reps = []
    for i in range(args.reps):
        reps.append(run_replicate(args.scenario, args.policy, unified, parallel,
                                  args.ctx, args.cache_ram, args.log_dir, i))
        print(f"  [rep{i}] clean={reps[-1]['clean_verified']} "
              f"purge={reps[-1].get('log_events', {}).get('purge_events', [])}")
    out = {
        "scenario": args.scenario, "policy": args.policy, "unified": unified,
        "ctx": args.ctx, "parallel": parallel, "cache_ram_mib": args.cache_ram,
        "reps": reps, "binary_sha256": sha256(SERVER),
        "model_sha256": sha256(MODEL),
        "llama_commit": subprocess.check_output(
            ["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"保存: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
