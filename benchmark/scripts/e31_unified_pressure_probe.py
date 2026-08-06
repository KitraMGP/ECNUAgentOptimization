#!/usr/bin/env python3
"""E3.1：unified idle-sequence 生命周期候选 —— 压力机会 probe。

核心问题：unified KV 下是否真实存在
  1) idle slot 保留大量 cells；2) active 请求遭遇 cell 压力；
  3) idle cells 在关键路径上被回收（现有 try_clear_idle_slots）。

场景（prompt 固定、temperature=0、seed=42、cache-ram 0）：
- p2_single：slot0 长请求 → idle；slot1 长请求（合计超 capacity）
- p4_multi：4 slots 依次长请求 → 3 idle；最后 1 个大请求
- active_protection：p4 下 2 active + 2 idle；第 3 个请求大 prompt
- non_unified：p2 同序列（证明机会是否 unified 特有）

观测：每请求 slot/prompt_tokens/cached/latency/status/used_cells/
active_sequences/response_hash/保真标记；server 日志 purge/retry 事件。
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
from urllib import request, error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SERVER = os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")
MODEL = os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")
SLOT_SAVE = os.path.join(ROOT, "llama.cpp", "tmp", "e20_slot_save")
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"

# 固定长文本块（~250 chars ≈ 60-65 tokens）
CHUNK = (
    "The Corvus Delta monitoring network collects hydrological and ecological "
    "data across five instrumented zones every fifteen minutes. Each zone "
    "transmits calibrated readings for water quality, soil composition, "
    "vegetation density, and wildlife activity through the mesh telemetry "
    "backbone. The aggregation layer validates every packet against the "
    "schema, rejects failed checksums, and appends accepted readings to the "
    "append-only tool log before any downstream analysis begins."
)


def build_prompt(n_tokens: int, tag: str) -> str:
    """按目标 token 数构造固定 prompt（Qwen tokenizer 实测 ~6.2 chars/token）。"""
    chars = int(n_tokens * 6.2)
    body = " ".join([CHUNK] * (chars // len(CHUNK) + 1))
    body = body[:chars]
    return body + f"\n\nEND STATE: {tag}\nPlease confirm the state value above."


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def http_json(method: str, url: str, body: Optional[dict] = None, timeout: int = 300) -> Any:
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


def start_server(unified: bool, parallel: int, ctx: int, log_path: str) -> subprocess.Popen:
    os.makedirs(SLOT_SAVE, exist_ok=True)
    cmd = [SERVER, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "-ngl", "99", "--ctx-size", str(ctx), "--parallel", str(parallel),
           "--cache-reuse", "0", "--seed", "42", "--temp", "0",
           "--cache-ram", "0", "--slot-save-path", SLOT_SAVE]
    if unified:
        cmd.append("--kv-unified")
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
    body: Dict[str, Any] = {"prompt": prompt, "n_predict": max_tokens,
                            "temperature": 0, "seed": 42, "cache_prompt": True}
    if slot is not None:
        body["id_slot"] = slot
    t0 = time.perf_counter()
    try:
        d = http_json("POST", f"{BASE}/completion", body, timeout=600)
        status = "ok"
    except error.HTTPError as e:
        d = {"error": {"type": e.reason, "code": e.code}}
        status = f"http_{e.code}"
    except Exception as e:
        d = {"error": {"type": type(e).__name__}}
        status = "exc"
    lat = (time.perf_counter() - t0) * 1000
    text = d.get("content", "")
    timings = d.get("timings", {})
    return {
        "status": status,
        "latency_ms": round(lat, 1),
        "prompt_tokens": d.get("usage", {}).get("prompt_tokens") if "usage" in d else timings.get("prompt_n"),
        "cached_tokens": (d.get("usage", {}).get("prompt_tokens_details") or {}).get("cached_tokens") if "usage" in d else None,
        "response_hash": hashlib.sha256(re.sub(r"[^\w]+", "", text.lower()).encode()).hexdigest()[:12] if text else None,
        "text": text[:60],
        "error": d.get("error"),
        "timings_prompt_ms": timings.get("prompt_ms"),
        "timings_predicted_ms": timings.get("predicted_ms"),
    }


def kv_state() -> Dict[str, Any]:
    try:
        return http_json("GET", f"{BASE}/metrics/kv", timeout=5)
    except Exception as e:
        return {"error": str(e)}


def grep_log(log_path: str, patterns: List[str]) -> Dict[str, int]:
    counts = {p: 0 for p in patterns}
    try:
        with open(log_path, "r", errors="replace") as f:
            for line in f:
                for p in patterns:
                    if p in line:
                        counts[p] += 1
    except FileNotFoundError:
        pass
    return counts


def run_scenario(scenario: str, unified: bool, ctx: int, reps: int, log_dir: str) -> List[Dict]:
    out = []
    for ri in range(reps):
        log_path = os.path.join(log_dir, f"e31_{scenario}_{ri}.log")
        proc = start_server(unified, {"p2_single": 2, "p4_multi": 4,
                                      "active_protection": 4, "non_unified": 2}[scenario],
                            ctx, log_path)
        rec: Dict[str, Any] = {
            "scenario": scenario, "unified": unified, "ctx": ctx, "rep": ri,
            "server_pid": proc.pid, "clean_verified": False,
            "requests": [], "log_events": {}, "server_exited": False,
        }
        if not wait_health(proc):
            rec["startup_failure"] = True
            stop_server(proc)
            out.append(rec)
            continue
        kv = kv_state()
        rec["clean_verified"] = kv.get("used_cells") == 0 and kv.get("active_sequences") == 0
        rec["capacity_cells"] = kv.get("capacity_cells")

        def _req(prompt: str, slot: Optional[int], tag: str) -> Dict:
            r = completion(prompt, slot)
            r["tag"] = tag
            r["kv_before"] = kv_state()
            # 保真：请求成功完成且非空响应（压力场景下 400/截断即保真失败）
            r["fidelity_ok"] = r["status"] == "ok" and bool(r["text"])
            r["used_cells_after"] = kv_state().get("used_cells")
            r["active_sequences_after"] = kv_state().get("active_sequences")
            rec["requests"].append(r)
            print(f"    [{tag}] slot={slot} status={r['status']} "
                  f"prompt={r['prompt_tokens']} cached={r['cached_tokens']} "
                  f"lat={r['latency_ms']}ms used={r['used_cells_after']} "
                  f"active={r['active_sequences_after']}")
            return r

        if scenario == "p2_single":
            _req(build_prompt(5000, "STATE_A"), 0, "A_long")
            _req(build_prompt(5000, "STATE_B"), 1, "B_long")
        elif scenario == "p4_multi":
            for s in range(3):
                _req(build_prompt(2000, f"STATE_P{s}"), s, f"P{s}_long")
            _req(build_prompt(5000, "STATE_P3"), 3, "P3_long")
        elif scenario == "active_protection":
            _req(build_prompt(2000, "STATE_AP0"), 0, "AP0_long")
            _req(build_prompt(2000, "STATE_AP1"), 1, "AP1_long")
            # slot2 保持 active（大请求）的同时 slot3 先 idle 一个短请求
            _req(build_prompt(200, "STATE_AP3"), 3, "AP3_short")
            _req(build_prompt(5000, "STATE_AP2"), 2, "AP2_long")
        elif scenario == "non_unified":
            _req(build_prompt(5000, "STATE_A"), 0, "A_long")
            _req(build_prompt(5000, "STATE_B"), 1, "B_long")

        rec["log_events"] = grep_log(log_path, [
            "failed to find free space in the KV cache",
            "purging slot",
            "Context size has been exceeded",
            "retrying with smaller batch size",
        ])
        stop_server(proc)
        rec["server_exited"] = proc.returncode is not None
        out.append(rec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["p2_single", "p4_multi",
                                           "active_protection", "non_unified"], required=True)
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--log-dir", default="/tmp")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    unified = args.scenario != "non_unified"
    os.makedirs(args.log_dir, exist_ok=True)
    reps = run_scenario(args.scenario, unified, args.ctx, args.reps, args.log_dir)
    out = {
        "scenario": args.scenario, "unified": unified, "ctx": args.ctx,
        "reps": reps, "binary_sha256": sha256(SERVER),
        "model_sha256": sha256(MODEL), "llama_commit": subprocess.check_output(
            ["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"保存: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
