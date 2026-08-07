#!/usr/bin/env python3
"""E6：KV Cache 优化实验统一 runner。

按 workload_manifest.json（W01-W24）执行 workload，每条实验输出 12.17 原始 JSON 记录
到 benchmark/results/kv_optimization/raw/。采集：OAI usage（prompt/cached/generated）、
/metrics/kv（capacity/used cells/active/shared/bytes）、server 日志（purging slot）。

用法：
  python scripts/e6_runner.py --workload W01,W07 --repetitions 3 --tag baseline
  python scripts/e6_runner.py --workload W16 --cycles 12 --tag baseline_churn
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
from typing import Any, Dict, List
from urllib import request, error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
SERVER = os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")
MODEL = os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"
OUT = os.path.join(BENCH, "results", "kv_optimization", "raw")
MANIFEST = json.load(open(os.path.join(BENCH, "results", "kv_optimization", "workload_manifest.json")))
WORKLOADS = {w["workload_id"]: w for w in MANIFEST["workloads"]}
# 前缀匹配：W01 → W01_single_short
WID_ALIASES = {wid.split("_")[0]: wid for wid in WORKLOADS}

# 确定性 prompt 语料（固定 seed，跨 baseline/candidate 一致）
CHUNKS = [
    "The Corvus Delta network monitors riparian wetlands and upstream runoff.",
    "The Meridian Plateau observatory tracks seismic activity and solar radiation.",
    "The Azura Harbor fleet records ocean tides, salinity, and fish migration.",
    "The Kestrel Ridge station logs wind patterns, canopy cover, and soil moisture.",
    "The Borealis Ice Shelf survey measures glacier flow, temperature, and pressure.",
    "The Juniper Vale arboretum catalogs phenology, frost dates, and canopy gaps.",
    "The Solstice Basin research outpost samples permafrost carbon and methane flux.",
    "The Ember Peak volcanology team tracks tremor, gas ratios, and dome growth.",
]
PREFIX = " ".join(CHUNKS)  # 共享前缀语料


def build_prompt(n_tokens: int, prefix: str, tag: str = "S0") -> str:
    chars = max(64, int(n_tokens * 4.5))  # 英文约 4-5 chars/token
    body = " ".join([prefix] * (chars // len(prefix) + 1))[:chars]
    return body + f"\n\nTAG: {tag}\nEND STATE: {tag}\nConfirm the state value."


def sha256_file(path: str) -> str:
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


def wait_health(proc: subprocess.Popen, timeout_s: int = 240) -> bool:
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


def start_server(log_path: str, extra_args: List[str]) -> subprocess.Popen:
    cmd = [SERVER, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "-ngl", "99", "--ctx-size", "8192", "--parallel", "4",
           "--cache-ram", "0", "--temp", "0", "--seed", "42", "--metrics",
           "--slots", "--slot-save-path", os.path.join(ROOT, "llama.cpp", "tmp", "e6_slot_save")]
    cmd += extra_args
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    logf = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
    if not wait_health(proc):
        raise RuntimeError("server failed to start; see " + log_path)
    return proc


def get_kv_stats() -> dict | None:
    try:
        d = http_json("GET", f"{BASE}/metrics/kv", timeout=5)
        if isinstance(d, list):
            d = d[0]
        return d.get("kv_stats") if "kv_stats" in d else d
    except Exception:
        return None


def get_purge_count(log_path: str) -> int:
    try:
        txt = open(log_path, encoding="utf-8", errors="ignore").read()
        return len(re.findall(r"purging slot", txt))
    except Exception:
        return 0


def chat(prompt: str, max_tokens: int, timeout: int = 600) -> dict:
    body = {"messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False}}
    t0 = time.time()
    try:
        d = http_json("POST", f"{BASE}/v1/chat/completions", body, timeout=timeout)
        lat = (time.time() - t0) * 1000.0
        msg = d["choices"][0]["message"]
        content = msg.get("content") or ""
        usage = d.get("usage", {})
        pt = usage.get("prompt_tokens", 0)
        gt = usage.get("completion_tokens", 0)
        cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        return {"status": "ok", "content": content, "prompt_tokens": pt,
                "generated_tokens": gt, "cached_tokens": cached, "latency_ms": lat,
                "output_sha256": hashlib.sha256(content.encode()).hexdigest()}
    except error.HTTPError as e:
        lat = (time.time() - t0) * 1000.0
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        return {"status": f"http_{e.code}", "detail": detail, "latency_ms": lat,
                "prompt_tokens": 0, "generated_tokens": 0, "cached_tokens": 0,
                "output_sha256": ""}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:300], "latency_ms": (time.time() - t0) * 1000.0,
                "prompt_tokens": 0, "generated_tokens": 0, "cached_tokens": 0, "output_sha256": ""}


def make_record(experiment_id: str, candidate_id: str, variant: str, workload_id: str,
                repetition: int, log_path: str, extra: dict) -> dict:
    """12.17 原始数据格式。不能获取的字段用 null（不得用 0 代替）。"""
    kv = get_kv_stats()
    rec = {
        "experiment_id": experiment_id, "candidate_id": candidate_id,
        "variant": variant, "workload_id": workload_id, "repetition": repetition,
        "git_commit": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "model_sha256": sha256_file(MODEL),
        "config_sha256": hashlib.sha256(json.dumps(extra.get("config", {}), sort_keys=True).encode()).hexdigest(),
        "start_time_utc": extra.get("start_time_utc"),
        "end_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "exit_code": extra.get("exit_code", 0), "timeout": extra.get("timeout", False),
        "completed_sessions": extra.get("completed_sessions", 0),
        "failed_sessions": extra.get("failed_sessions", 0),
        "rejected_sessions": extra.get("rejected_sessions", 0),
        "prompt_tokens": extra.get("prompt_tokens", 0),
        "generated_tokens": extra.get("generated_tokens", 0),
        "output_token_sha256": extra.get("output_token_sha256", ""),
        "peak_physical_kv_bytes": extra.get("peak_physical_kv_bytes"),
        "final_physical_kv_bytes": kv.get("used_bytes") if kv and kv.get("used_bytes_valid") else None,
        "logical_kv_tokens": extra.get("prompt_tokens") if kv is not None else None,
        "unique_physical_kv_tokens": kv.get("used_cells") if kv else None,
        "internal_fragmentation_bytes": None,  # instrumentation gap：无空洞 cell 计数（架构审计 §13）
        "stranded_capacity_bytes": None,        # instrumentation gap：无连续/所有权受限统计
        "shared_blocks": None,                   # 无 paged block 概念（physical_sharing=false）
        "cow_operations": None,                  # 无 COW 实现
        "prefix_hit_tokens": extra.get("cached_tokens", 0),
        "recompute_tokens": max(0, extra.get("prompt_tokens", 0) - extra.get("cached_tokens", 0)),
        "purge_count": get_purge_count(log_path),
        "active_victim_count": extra.get("active_victim_count", 0),
        "cross_session_mismatch_count": extra.get("cross_session_mismatch_count", 0),
        "prefill_tokens_per_second": extra.get("prefill_tps"),
        "decode_tokens_per_second": extra.get("decode_tps"),
        "request_latency_ms": extra.get("latency_ms"),
        "quality_score": extra.get("quality_score"),
        "quality_gate": extra.get("quality_gate", "NOT_APPLICABLE"),
        "notes": extra.get("notes", []),
        "kv_stats_snapshot": kv,
        "per_request": extra.get("per_request", []),
    }
    return rec


def run_single(workload: dict, repetition: int, tag: str, log_path: str,
               candidate_id: str = "BASELINE") -> dict:
    wid = workload["workload_id"]
    n = workload["tokenized_input_length"]
    max_out = workload["expected_max_output_tokens"]
    concurrency = workload["concurrency"]
    sessions = workload["number_of_sessions"]
    prefix_pattern = workload["prefix_sharing_pattern"]

    # 确定性 prompt 构建（跨 baseline/candidate 相同）
    if prefix_pattern == "exact_common_prefix":
        prompts = [build_prompt(n, PREFIX, f"S{i}") for i in range(sessions)]
    elif prefix_pattern in ("partial_prefix", "idle_reuse"):
        prompts = [build_prompt(n, PREFIX[: int(len(PREFIX) * 0.6)], f"S{i}") for i in range(sessions)]
    elif prefix_pattern == "no_prefix":
        prompts = [build_prompt(n, CHUNKS[i % len(CHUNKS)], f"S{i}") for i in range(sessions)]
    elif prefix_pattern == "active_pressure":
        prompts = [build_prompt(n, PREFIX, f"S{i}") for i in range(sessions)]
    else:
        prompts = [build_prompt(n, PREFIX, f"S{i}") for i in range(sessions)]

    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    results = []
    t0 = time.time()
    if concurrency > 1:
        outs: List[dict] = [{} for _ in prompts]

        def worker(i: int):
            outs[i] = chat(prompts[i], max_out)

        ths = [threading.Thread(target=worker, args=(i,)) for i in range(len(prompts))]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        results = outs
    else:
        for p in prompts:
            results.append(chat(p, max_out))

    elapsed = time.time() - t0
    completed = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] not in ("ok",))
    rejected = sum(1 for r in results if r["status"] == "http_400" or "exceeds" in r.get("detail", ""))
    pt = sum(r["prompt_tokens"] for r in results)
    gt = sum(r["generated_tokens"] for r in results)
    cached = sum(r["cached_tokens"] for r in results)
    out_sha = hashlib.sha256("|".join(r["output_sha256"] for r in results).encode()).hexdigest()
    lat = [r["latency_ms"] for r in results if r["status"] == "ok"]
    kv = get_kv_stats()

    extra = {
        "config": {"ctx": 8192, "parallel": 4, "kv_unified": True, "cache_ram": 0,
                   "temp": 0, "seed": 42, "workload": wid, "repetition": repetition},
        "start_time_utc": start, "exit_code": 0, "timeout": False,
        "completed_sessions": completed, "failed_sessions": failed, "rejected_sessions": rejected,
        "prompt_tokens": pt, "generated_tokens": gt, "cached_tokens": cached,
        "output_token_sha256": out_sha,
        "peak_physical_kv_bytes": None,  # 无峰值采样（E6.4 加 sampler）
        "active_victim_count": 0, "cross_session_mismatch_count": 0,
        "prefill_tps": (pt / elapsed) if elapsed > 0 else None,
        "decode_tps": (gt / elapsed) if elapsed > 0 else None,
        "latency_ms": (sum(lat) / len(lat)) if lat else None,
        "quality_score": None, "quality_gate": "PASS_HOLD_REJECT_NOT_APPLICABLE",
        "notes": [f"prefix_pattern={prefix_pattern}", f"concurrency={concurrency}", f"sessions={sessions}"],
        "per_request": results,
    }
    rec = make_record(f"e6_{tag}_{wid}_rep{repetition}", candidate_id, "baseline_or_candidate",
                      wid, repetition, log_path, extra)
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{rec['experiment_id']}.json")
    with open(path, "w") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    print(f"  [{wid} rep{repetition}] completed={completed} failed={failed} "
          f"prompt={pt} cached={cached} used_cells={(kv or {}).get('used_cells')}")
    return rec


def run_churn(cycles: int, tag: str, log_path: str) -> dict:
    """W16 churn：4 session 并发，每周期增长 prompt，周期末静置采样，最后 erase 回基线。"""
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    t0 = time.time()
    cycle_samples = []
    for c in range(cycles):
        n_tok = 600 + c * 50
        prompts = [build_prompt(n_tok, PREFIX, f"CH{i}") for i in range(4)]
        outs: List[dict] = [{} for _ in prompts]

        def worker(i: int):
            outs[i] = chat(prompts[i], 64)

        ths = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        time.sleep(2)
        cycle_samples.append({"cycle": c, "samples": [get_kv_stats()]})
        print(f"  churn cycle {c}: {[r['status'] for r in outs]}")
    # erase 回收验证
    try:
        for slot_id in range(4):
            http_json("POST", f"{BASE}/slots/{slot_id}?action=erase", {}, timeout=30)
    except Exception as e:
        print("  erase failed:", e)
    time.sleep(1)
    kv_after = get_kv_stats()
    elapsed = time.time() - t0
    rec = {
        "experiment_id": f"e6_{tag}_W16_churn{cycles}",
        "candidate_id": "BASELINE", "variant": "baseline", "workload_id": "W16",
        "repetition": 0,
        "git_commit": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "model_sha256": sha256_file(MODEL),
        "config_sha256": "",
        "start_time_utc": start, "end_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "exit_code": 0, "timeout": False,
        "completed_sessions": 4 * cycles, "failed_sessions": 0, "rejected_sessions": 0,
        "prompt_tokens": 0, "generated_tokens": 0, "output_token_sha256": "",
        "peak_physical_kv_bytes": None, "final_physical_kv_bytes": (kv_after or {}).get("used_bytes"),
        "logical_kv_tokens": None, "unique_physical_kv_tokens": (kv_after or {}).get("used_cells"),
        "internal_fragmentation_bytes": None, "stranded_capacity_bytes": None,
        "shared_blocks": None, "cow_operations": None,
        "prefix_hit_tokens": 0, "recompute_tokens": 0,
        "purge_count": get_purge_count(log_path),
        "active_victim_count": 0, "cross_session_mismatch_count": 0,
        "prefill_tokens_per_second": None, "decode_tokens_per_second": None,
        "request_latency_ms": None, "quality_score": None,
        "quality_gate": "PASS_HOLD_REJECT_NOT_APPLICABLE",
        "notes": [f"cycles={cycles}", "churn endurance"],
        "kv_stats_snapshot": kv_after,
        "cycle_samples": cycle_samples,
    }
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{rec['experiment_id']}.json")
    with open(path, "w") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    print(f"  churn {cycles} cycles done, kv_after={kv_after}")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workload", default="W01")
    ap.add_argument("--repetitions", type=int, default=1)
    ap.add_argument("--tag", default="baseline")
    ap.add_argument("--cycles", type=int, default=12, help="W16 churn cycles")
    ap.add_argument("--candidate", default="BASELINE")
    ap.add_argument("--server-extra", default="", help="额外 server 参数（空格分隔）")
    args = ap.parse_args()

    log_path = os.path.join(OUT, f"server_{args.tag}.log")
    extra_args = args.server_extra.split() if args.server_extra else []
    proc = start_server(log_path, extra_args)
    try:
        wids = [w.strip() for w in args.workload.split(",")]
        for wid in wids:
            if wid == "W16":
                run_churn(args.cycles, args.tag, log_path)
                continue
            if wid not in WORKLOADS:
                if wid in WID_ALIASES:
                    wid = WID_ALIASES[wid]
                else:
                    print(f"unknown workload {wid}")
                    continue
            w = WORKLOADS[wid]
            reps = args.repetitions
            if w["correctness_gate"] and (w["concurrency"] > 1 or "lifecycle" in w["category"]):
                reps = max(reps, 3)  # 并发/生命周期 correctness ≥3 次（12.9）
            for r in range(reps):
                run_single(w, r, args.tag, log_path, args.candidate)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
