#!/usr/bin/env python3
"""E7.4: C1 扩展 paired benchmark 矩阵（TinyLlama 标准架构）。

场景（on/off 配对，相同输入/seed/budget，仅 --kv-prefix-share 不同）：
  M01_2slot_shared_prefix    : 2 slot 共享前缀（A+X -> A+Y）基准
  M02_3slot_multi_target     : 3 slot，2 target 共享同一 source
  M03_partial_prefix         : 部分前缀共享（prefix 后半不同）
  M04_no_shared_prefix       : 无共享前缀（退化门禁）
  M05_long_code              : 长代码 prompt（共享前缀 + 代码后缀）
  M06_long_summary           : 长摘要 prompt
  M07_tool_call_json         : tool-call JSON prompt
  M08_canary                 : session-specific canary 隔离（on 模式输出不含对方 canary）

每个场景每模式 ≥5 次（性能）；记录 recompute/prompt_n/latency/shared_cells/输出 hash。
TinyLlama 上 latency 为 ms 级（噪声大），不作为正式性能门槛依据（12.9 wall-time
仅辅助）；正式指标 = recompute tokens + shared_cells + 输出一致 + 无错误。

用法：python scripts/e7_c1_bench_matrix.py --reps 5 --output raw/e7_c1_matrix.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import time
from urllib import request, error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODEL_CACHE = os.path.join(ROOT, "llama.cpp", "tmp", "models--ggml-org--test-model-stories260K")
MODEL = os.path.join(MODEL_CACHE, "snapshots",
                     "479896ec924af6d40fd419ab8f4d1eb2101de00d", "stories260K-f32.gguf")
SERVER_BIN = os.path.join(ROOT, "llama.cpp", "build", "bin", "llama-server")
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"

A = ("Once upon a time in the deep dark forest, a small rabbit named Miko discovered "
     "a glowing stone near the old oak tree. The stone hummed with a soft blue light "
     "and seemed to pulse with the rhythm of the wind. Miko carefully picked it up "
     "and carried it back to the burrow, where the other animals gathered to see. "
     "The stone told stories of ancient rivers and forgotten trails. ")
B = ("Far away in the northern mountains, a young fox named Kira tracked the scent "
     "of fresh snow across the frozen river. The river whispered secrets of the "
     "valley below, where the pines stood tall against the winter wind. ")
X = " Miko hid the stone by the stream."
Y = " The owl examined the stone."
Z = " The bear guarded the stone."
CODE_PREFIX = "def process_sensor_data(readings):\n    # normalize and filter readings\n    results = []\n    for r in readings:\n        if r > 0:\n            results.append(r * 1.5)\n    return results\n\n"
CODE_X = "\n\ndef apply_calibration(data, factor):\n    return [d * factor for d in data]\n"
CODE_Y = "\n\ndef compute_anomaly_score(data, baseline):\n    return sum(abs(d - b) for d, b in zip(data, baseline))\n"
SUMMARY_PREFIX = "Document: The annual report of the Corvus Delta monitoring program covers water quality, biodiversity, and climate trends across the region. " * 4
SUMMARY_X = "\nSummary: The report documents stable water quality with minor seasonal variation."
SUMMARY_Y = "\nSummary: Biodiversity indicators show a modest recovery in riparian zones."
TOOL_PREFIX = "System: You have access to tools: get_weather(city), search_news(query), calculate(expr). " + A
TOOL_X = "\nUser: What is the weather in Berlin? Call the tool.\nAssistant: get_weather(city=\"Berlin\")"
TOOL_Y = "\nUser: Calculate 15*4. Call the tool.\nAssistant: calculate(expr=\"15*4\")"
CANARY_X = "CANARY-ALPHA-3947"
CANARY_Y = "CANARY-BETA-8812"

SCENARIOS = {
    "M01_2slot_shared_prefix": {"src": A + X, "tgt": A + Y, "slots": 2, "expect_shared": True},
    "M02_3slot_multi_target": {"src": A + X, "tgt": [A + Y, A + Z], "slots": 3, "expect_shared": True},
    "M03_partial_prefix": {"src": A + X, "tgt": A[: len(A) // 2] + " different continuation " + Y, "slots": 2, "expect_shared": True},
    "M04_no_shared_prefix": {"src": A + X, "tgt": B + Y, "slots": 2, "expect_shared": False},
    "M05_long_code": {"src": CODE_PREFIX + CODE_X, "tgt": CODE_PREFIX + CODE_Y, "slots": 2, "expect_shared": True},
    "M06_long_summary": {"src": SUMMARY_PREFIX + SUMMARY_X, "tgt": SUMMARY_PREFIX + SUMMARY_Y, "slots": 2, "expect_shared": True},
    "M07_tool_call_json": {"src": TOOL_PREFIX + TOOL_X, "tgt": TOOL_PREFIX + TOOL_Y, "slots": 2, "expect_shared": True},
    "M08_canary": {"src": A + "\nSECRET: " + CANARY_X + "\n" + X, "tgt": A + "\nSECRET: " + CANARY_Y + "\n" + Y, "slots": 2, "expect_shared": True},
}


def http_json(method, url, body=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    t0 = time.time()
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode()), (time.time() - t0) * 1000.0


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


def run_scenario(server_args, scenario_id, spec, reps):
    log_path = os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw",
                            f"e7matrix_server_{scenario_id}.log")
    logf = open(log_path, "w")
    proc = subprocess.Popen(server_args, stdout=logf, stderr=subprocess.STDOUT)
    if not wait_health(proc):
        print(f"  [{scenario_id}] server failed")
        return {"failed": True}
    try:
        out = []
        for rep in range(reps):
            for sid in range(spec["slots"]):
                try:
                    http_json("POST", f"{BASE}/slots/{sid}?action=erase", {}, timeout=30)
                except Exception:
                    pass
            src_p = spec["src"]
            tgts = spec["tgt"] if isinstance(spec["tgt"], list) else [spec["tgt"]]
            r_src, lat_src = http_json("POST", f"{BASE}/completion",
                                       {"prompt": src_p, "n_predict": 4, "temperature": 0,
                                        "cache_prompt": True, "id_slot": 0}, timeout=120)
            rec = {"repetition": rep,
                   "src_status": "ok", "src_prompt_n": r_src.get("timings", {}).get("prompt_n"),
                   "src_latency_ms": lat_src, "targets": []}
            for i, tgt_p in enumerate(tgts):
                r_tgt, lat_tgt = http_json("POST", f"{BASE}/completion",
                                           {"prompt": tgt_p, "n_predict": 4, "temperature": 0,
                                            "cache_prompt": True, "id_slot": i + 1}, timeout=120)
                tim = r_tgt.get("timings", {})
                rec["targets"].append({
                    "slot": i + 1, "status": "ok",
                    "prompt_n": tim.get("prompt_n", 0),  # 本次 prefill 处理 token（recompute 代理）
                    "latency_ms": lat_tgt,
                    "output": r_tgt.get("content", ""),
                    "output_sha256": hashlib.sha256(r_tgt.get("content", "").encode()).hexdigest(),
                })
            kv, _ = http_json("GET", f"{BASE}/metrics/kv", timeout=10)
            kv = kv.get("kv_stats") if "kv_stats" in kv else kv
            rec["kv_used_cells"] = kv.get("used_cells")
            rec["kv_shared_cells"] = kv.get("shared_cells")
            rec["kv_active_sequences"] = kv.get("active_sequences")
            out.append(rec)
        log = open(log_path, encoding="utf-8", errors="replace").read()
        return {"failed": False, "results": out,
                "shared_log_count": log.count("E6-C1: shared"),
                "exit_code": proc.poll() if proc.poll() is not None else 0}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--output", default="")
    ap.add_argument("--scenarios", default="all")
    args = ap.parse_args()

    slot_save = os.path.join(ROOT, "llama.cpp", "tmp", "e6_slot_save")
    os.makedirs(slot_save, exist_ok=True)
    base = [SERVER_BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
            "--ctx-size", "512", "--parallel", "4", "--kv-unified",
            "--cache-ram", "0", "--temp", "0", "--seed", "42", "--metrics",
            "--slot-save-path", slot_save, "--kv-prefix-share-min-lcp", "4"]

    sc_ids = [s.strip() for s in args.scenarios.split(",")]
    if sc_ids == ["all"]:
        sc_ids = list(SCENARIOS.keys())

    matrix = {"git_commit": subprocess.check_output(
        ["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "model": "tinyllama stories260K", "reps": args.reps, "scenarios": {}}
    for sc_id in sc_ids:
        spec = SCENARIOS[sc_id]
        off = run_scenario(base, f"{sc_id}_off", spec, args.reps)
        on = run_scenario(base + ["--kv-prefix-share"], f"{sc_id}_on", spec, args.reps)
        # 汇总
        def med(vals):
            vals = [v for v in vals if v is not None]
            return round(statistics.median(vals), 2) if vals else None

        def p95(vals):
            vals = sorted(v for v in vals if v is not None)
            return round(vals[int(len(vals) * 0.95) - 1], 2) if vals else None

        off_rt = [t["prompt_n"] for r in off["results"] for t in r["targets"]]
        on_rt = [t["prompt_n"] for r in on["results"] for t in r["targets"]]
        off_lat = [t["latency_ms"] for r in off["results"] for t in r["targets"]]
        on_lat = [t["latency_ms"] for r in on["results"] for t in r["targets"]]
        m_off, m_on = med(off_rt), med(on_rt)
        reduction = (1 - m_on / m_off) * 100 if m_off else None
        # 输出一致（on/off 按 rep 对齐）
        off_out = [t["output_sha256"] for r in off["results"] for t in r["targets"]]
        on_out = [t["output_sha256"] for r in on["results"] for t in r["targets"]]
        lossless = (off_out == on_out) and len(off_out) > 0
        # canary 检查（M08）
        canary_leak = False
        if sc_id == "M08_canary":
            for r in on["results"]:
                for t in r["targets"]:
                    if CANARY_X in t["output"]:
                        canary_leak = True
        entry = {
            "off": {"recompute_median": m_off, "recompute_values": off_rt,
                    "latency_median_ms": med(off_lat), "latency_p95_ms": p95(off_lat),
                    "shared_log_count": off["shared_log_count"]},
            "on": {"recompute_median": m_on, "recompute_values": on_rt,
                   "latency_median_ms": med(on_lat), "latency_p95_ms": p95(on_lat),
                   "shared_log_count": on["shared_log_count"],
                   "shared_cells_samples": [r["kv_shared_cells"] for r in on["results"]]},
            "recompute_reduction_pct": round(reduction, 2) if reduction is not None else None,
            "lossless_output_identical": lossless,
            "canary_leak": canary_leak,
            "expect_shared": spec["expect_shared"],
            "off_raw": off, "on_raw": on,
        }
        matrix["scenarios"][sc_id] = entry
        print(f"  [{sc_id}] off_rt={m_off} on_rt={m_on} reduction={entry['recompute_reduction_pct']}% "
              f"lossless={lossless} shared_log(on)={on['shared_log_count']}")

    out = args.output or os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw",
                                      "e7_c1_matrix.json")
    with open(out, "w") as f:
        json.dump(matrix, f, ensure_ascii=False, indent=2)
    print("saved:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
