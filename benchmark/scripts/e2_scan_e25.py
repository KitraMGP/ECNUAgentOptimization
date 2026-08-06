#!/usr/bin/env python3
"""E2.5：branch_pressure 正式实验运行器 —— server-per-replicate 协议。

每个 replicate 使用全新 llama-server 进程：
1. 启动 server（记录 PID/命令/启动时间）→ 2. health → 3. 断言 /metrics/kv
   used_cells==0 && active_sequences==0 → 4. warmup（无关短请求）→
   5. erase slots → 6. 正式 8 请求（branch_pressure 序列）→ 7. 保存 →
   8. SIGTERM 停 server → 9. 确认进程退出。

用法：
  uv run python scripts/e2_scan_e25.py --ram off --policy default --reps 5
  uv run python scripts/e2_scan_e25.py --ram pressure --ram-mib 128
  uv run python scripts/e2_scan_e25.py --ram pressure --policy prefix-branch --reps 5
  uv run python scripts/e2_scan_e25.py --ram default --policy default --reps 2
  uv run python scripts/e2_scan_e25.py --unified --ram pressure --policy prefix-branch --reps 1
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
import time
from typing import Any, Dict, List, Optional
from urllib import request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
SERVER = os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")
MODEL = os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")
SLOT_SAVE = os.path.join(ROOT, "llama.cpp", "tmp", "e20_slot_save")
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"

sys.path.insert(0, BENCH)
from workload.branch_pressure import (  # noqa: E402
    build_prompts, evaluate_response, evaluator_pass,
)

WARMUP_MSG = [{"role": "user", "content": "warmup: echo readiness."}]


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def http_json(method: str, url: str, body: Optional[dict] = None, timeout: int = 300) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(url, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_health(proc: subprocess.Popen, timeout_s: int = 180) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if proc.poll() is not None:
            return False
        try:
            with request.urlopen(f"{BASE}/health", timeout=3) as resp:
                body = resp.read().decode("utf-8").strip()
                try:
                    if json.loads(body).get("status") == "ok":
                        return True
                except json.JSONDecodeError:
                    if body.strip('"') == "ok":
                        return True
        except Exception:
            pass
        time.sleep(1)
    return False


def start_server(ram_mib: int, policy: str, unified: bool) -> subprocess.Popen:
    os.makedirs(SLOT_SAVE, exist_ok=True)
    cmd = [SERVER, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "-ngl", "99", "--ctx-size", "8192", "--parallel", "2",
           "--cache-reuse", "0", "--seed", "42", "--temp", "0",
           "--slot-routing-policy", policy, "--slot-routing-stats",
           "--slot-save-path", SLOT_SAVE, "--cache-ram", str(ram_mib)]
    if unified:
        cmd.append("--kv-unified")
    logf = open(os.path.join(os.environ.get("E25_LOG_DIR", "/tmp"), f"e25_{policy}_{ram_mib}.log"), "a")
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


def routing_events() -> List[Dict[str, Any]]:
    try:
        return http_json("GET", f"{BASE}/routing/events?limit=10000", timeout=5)["events"]
    except Exception as e:
        return [{"error": str(e)}]


def run_replicate(ram_mib: int, policy: str, replicate_id: str, unified: bool,
                  warmup: bool = True) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "replicate_id": replicate_id, "routing_policy": policy,
        "ram_cache_mib": ram_mib, "kv_unified": unified,
        "server_restarted": True, "fresh_process_verified": False,
        "clean_verified": False, "server_pid": None,
        "initial_used_cells": None, "initial_active_sequences": None,
        "requests": [], "failures": 0,
    }
    t_start = time.time()
    proc = start_server(ram_mib, policy, unified)
    rec["server_pid"] = proc.pid
    rec["server_start_cmd"] = " ".join(proc.args)
    rec["server_started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    ok = wait_health(proc)
    if not ok:
        rec["startup_failure"] = True
        stop_server(proc)
        return rec
    kv = kv_state()
    rec["initial_used_cells"] = kv.get("used_cells")
    rec["initial_active_sequences"] = kv.get("active_sequences")
    rec["clean_verified"] = (kv.get("used_cells") == 0 and kv.get("active_sequences") == 0)
    rec["fresh_process_verified"] = rec["clean_verified"]

    if warmup:
        try:
            http_json("POST", f"{BASE}/v1/chat/completions",
                      {"model": "bench", "messages": WARMUP_MSG, "max_tokens": 4,
                       "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}},
                      timeout=120)
        except Exception as e:
            rec["warmup_failure"] = str(e)
        try:
            slots = http_json("GET", f"{BASE}/slots", timeout=5)
            for s in slots:
                http_json("POST", f"{BASE}/slots/{s['id']}?action=erase", {}, timeout=10)
        except Exception as e:
            rec["post_warmup_erase_failure"] = str(e)

    for i, p in enumerate(build_prompts()):
        ev_before = len(routing_events())
        t0 = time.perf_counter()
        req_failure = None
        resp_data: Dict[str, Any] = {}
        try:
            resp_data = http_json("POST", f"{BASE}/v1/chat/completions",
                                  {"model": "bench", "messages": p["messages"],
                                   "max_tokens": 64, "temperature": 0,
                                   "chat_template_kwargs": {"enable_thinking": False}},
                                  timeout=300)
        except Exception as e:
            req_failure = f"{type(e).__name__}: {e}"
            rec["failures"] += 1
        latency_ms = (time.perf_counter() - t0) * 1000
        usage = resp_data.get("usage", {}) if resp_data else {}
        prompt_tokens = usage.get("prompt_tokens")
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        completion = usage.get("completion_tokens", 0)
        content = ""
        if resp_data:
            content = ((resp_data.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
        ev = evaluate_response(content, p["branch_id"], p["expected_state"])
        kv = kv_state()
        new_ev = routing_events()[ev_before:]
        route: Dict[str, Any] = {}
        if new_ev:
            e = new_ev[-1]
            route = {k: e.get(k) for k in (
                "selected_slot", "routing_reason", "candidate_slot_count",
                "selected_prefix_tokens", "preserved_branch",
                "fallback_to_default")}
        ram_restore_suspected = bool(
            not req_failure and cached and prompt_tokens
            and cached >= prompt_tokens
        )
        rec["requests"].append({
            "request_id": i, "turn_id": p["turn_id"], "branch_id": p["branch_id"],
            "expected_branch_id": p["branch_id"], "expected_state": p["expected_state"],
            **route,
            "prompt_tokens": prompt_tokens,
            "logical_prefix_reuse_tokens": cached if not req_failure else None,
            "logical_prefix_reuse_ratio": round(cached / prompt_tokens, 4) if prompt_tokens and not req_failure else None,
            "prompt_processed_tokens": (prompt_tokens - cached) if prompt_tokens is not None and not req_failure else None,
            "full_recompute": bool(prompt_tokens and cached == 0) if not req_failure else None,
            "attention_cached_tokens": None,  # hybrid 无 per-part 接口
            "latency_total_ms": round(latency_ms, 1),
            "tokens_per_second": round(completion / (latency_ms / 1000), 2) if latency_ms and completion else None,
            "used_cells": kv.get("used_cells"), "active_sequences": kv.get("active_sequences"),
            "response_hash": hashlib.sha256(re.sub(r"[^\w\u4e00-\u9fff]+", "", content.lower(), flags=re.UNICODE).encode()).hexdigest()[:16] if content else None,
            "evaluator": ev, "evaluator_pass": evaluator_pass(ev),
            "contamination_detected": not ev["no_foreign_state"] if ev["json_parsable"] else None,
            "request_failure": req_failure, "ram_restore_suspected": ram_restore_suspected,
            "server_pid": proc.pid,
        })
    rec["wall_time_s"] = round(time.time() - t_start, 1)
    stop_server(proc)
    rec["server_exited"] = proc.returncode is not None
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ram", choices=["off", "pressure", "default"], required=True)
    ap.add_argument("--ram-mib", type=int, default=0, help="pressure 容量（MiB）")
    ap.add_argument("--policy", choices=["default", "prefix-branch"],
                    required="--pairs" not in __import__("sys").argv,
                    help="--pairs 模式不需要")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--pairs", type=int, default=0,
                    help="配对区组：按 D P | P D | D P | ... 交错跑 pairs 个 default + pairs 个 prefix-branch")
    ap.add_argument("--unified", action="store_true")
    ap.add_argument("--output-dir", default=os.path.join(BENCH, "results", "e25"))
    ap.add_argument("--no-warmup", action="store_true")
    args = ap.parse_args()

    ram_mib = {"off": 0, "pressure": args.ram_mib, "default": 8192}[args.ram]
    os.makedirs(args.output_dir, exist_ok=True)
    reps: List[Dict[str, Any]] = []
    policies_done = {"default": [], "prefix-branch": []}

    def _run_one(pol: str, rid: str) -> None:
        rec = run_replicate(ram_mib, pol, rid, args.unified, warmup=not args.no_warmup)
        policies_done[pol].append(rec)
        print(f"[{rid}] done wall={rec.get('wall_time_s')}s "
              f"clean={rec.get('clean_verified')} failures={rec.get('failures')}")

    if args.pairs > 0:
        # 配对区组：replicate 1: D->PB；replicate 2: PB->D；交替
        for i in range(args.pairs):
            if i % 2 == 0:
                _run_one("default", f"{args.ram}_default_{i}")
                _run_one("prefix-branch", f"{args.ram}_prefix-branch_{i}")
            else:
                _run_one("prefix-branch", f"{args.ram}_prefix-branch_{i}")
                _run_one("default", f"{args.ram}_default_{i}")
        reps = policies_done["default"] + policies_done["prefix-branch"]
        out = {
            "ram": args.ram, "ram_mib": ram_mib, "unified": args.unified,
            "reps": reps, "default_reps": policies_done["default"],
            "prefix_branch_reps": policies_done["prefix-branch"],
            "binary_sha256": sha256(SERVER), "model_sha256": sha256(MODEL),
            "ctx": 8192, "parallel": 2, "cache_reuse": 0, "seed": 42, "temp": 0,
        }
        for pol in ("default", "prefix-branch"):
            path = os.path.join(args.output_dir, f"{args.ram}_{pol}.json")
            if args.unified:
                path = os.path.join(args.output_dir, f"unified_{args.ram}_{pol}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({**out, "policy": pol, "reps": policies_done[pol]},
                          f, ensure_ascii=False, indent=2)
            print(f"保存: {path} ({len(policies_done[pol])} reps)")
        return 0

    for i in range(args.reps):
        rid = f"{args.ram}_{args.policy}_{i}"
        reps.append(run_replicate(ram_mib, args.policy, rid, args.unified,
                                  warmup=not args.no_warmup))
        print(f"[{rid}] done wall={reps[-1].get('wall_time_s')}s "
              f"clean={reps[-1].get('clean_verified')} failures={reps[-1].get('failures')}")
    out = {
        "ram": args.ram, "ram_mib": ram_mib, "policy": args.policy,
        "unified": args.unified, "reps": reps,
        "binary_sha256": sha256(SERVER), "model_sha256": sha256(MODEL),
        "ctx": 8192, "parallel": 2, "cache_reuse": 0, "seed": 42, "temp": 0,
    }
    path = os.path.join(args.output_dir, f"{args.ram}_{args.policy}.json")
    if args.unified:
        path = os.path.join(args.output_dir, f"unified_{args.ram}_{args.policy}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"保存: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
