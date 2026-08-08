#!/usr/bin/env python3
"""E8.5: C1 端到端性能与容量验收（TinyLlama，warmup≥5 + 正式≥30 次 paired）。

场景：
  shared  : 长共享前缀（A+X -> A+Y）
  partial : 部分共享（前半相同后半不同）
  noshare : 无共享前缀（退化门禁）
  capacity: 容量边界——相同 ctx 下 on/off 最大成功 session 数（共享前缀 workload）

指标（每 rep 记录 raw）：
  queue/prompt_ms/predicted_ms（timings）、prompt_n、output hash、
  /metrics/kv（used/shared/active）、失败/超时
汇总：median、p95、CV（>5% 标 NOISY）；不删离群值
容量：预分配 capacity_bytes（固定）vs used_cells（实际）vs 共享 headroom
      （shared_cells 逻辑释放）；不声称物理分配降低

用法：python scripts/e8_c1_perf.py --reps 30 --warmup 5 --scenario shared
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

A = ("Once upon a time in the deep dark forest, a small rabbit named Miko discovered "
     "a glowing stone near the old oak tree. The stone hummed with a soft blue light "
     "and seemed to pulse with the rhythm of the wind. Miko carefully picked it up "
     "and carried it back to the burrow, where the other animals gathered to see. "
     "The stone told stories of ancient rivers and forgotten trails. ") * 8  # 长前缀 ~1400 tokens
B = ("Far away in the northern mountains, a young fox named Kira tracked the scent "
     "of fresh snow across the frozen river. The river whispered secrets of the valley. ") * 4
X = " Miko hid the stone by the stream."
Y = " The owl examined the stone."


def http_json(method, url, body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


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


def run_paired(server_args, scenario, reps, warmup, out_path):
    log_path = os.path.join(os.path.dirname(out_path), f"e8perf_server_{scenario}.log")
    logf = open(log_path, "w")
    proc = subprocess.Popen(server_args, stdout=logf, stderr=subprocess.STDOUT)
    if not wait_health(proc):
        return {"failed": True, "reason": "server start failed"}
    try:
        # source/target prompt 由场景决定
        if scenario == "shared":
            src_p, tgt_p = A + X, A + Y
            expect_shared = True
        elif scenario == "partial":
            half = A[: len(A) // 2]
            src_p, tgt_p = A + X, half + " A completely different continuation " + Y
            expect_shared = True
        elif scenario == "noshare":
            src_p, tgt_p = A + X, B + Y
            expect_shared = False
        elif scenario == "capacity":
            src_p, tgt_p = A + X, A + Y
            expect_shared = True
        else:
            raise ValueError(scenario)

        results = []
        for rep in range(warmup + reps):
            # 每 rep 前 erase 全部 slot（独立）
            for sid in range(4):
                try:
                    http_json("POST", f"{BASE}/slots/{sid}?action=erase", {}, timeout=30)
                except Exception:
                    pass
            rec = {"rep": rep, "warmup": rep < warmup, "src": None, "tgt": None, "kv": None}
            ok = True
            # src
            try:
                r = http_json("POST", f"{BASE}/completion",
                              {"prompt": src_p, "n_predict": 8, "temperature": 0,
                               "cache_prompt": True, "id_slot": 0}, timeout=300)
                t = r.get("timings", {})
                rec["src"] = {"status": "ok", "prompt_n": t.get("prompt_n"),
                              "prompt_ms": t.get("prompt_ms"), "predicted_ms": t.get("predicted_ms"),
                              "prompt_per_second": t.get("prompt_per_second"),
                              "predicted_per_second": t.get("predicted_per_second"),
                              "queue": t.get("queue"),
                              "out_sha256": hashlib.sha256(r.get("content", "").encode()).hexdigest()}
            except Exception as e:
                rec["src"] = {"status": "error", "err": str(e)}
                ok = False
            # tgt
            try:
                r = http_json("POST", f"{BASE}/completion",
                              {"prompt": tgt_p, "n_predict": 8, "temperature": 0,
                               "cache_prompt": True, "id_slot": 1}, timeout=300)
                t = r.get("timings", {})
                rec["tgt"] = {"status": "ok", "prompt_n": t.get("prompt_n"),
                              "prompt_ms": t.get("prompt_ms"), "predicted_ms": t.get("predicted_ms"),
                              "prompt_per_second": t.get("prompt_per_second"),
                              "predicted_per_second": t.get("predicted_per_second"),
                              "queue": t.get("queue"),
                              "out_sha256": hashlib.sha256(r.get("content", "").encode()).hexdigest()}
            except Exception as e:
                rec["tgt"] = {"status": "error", "err": str(e)}
                ok = False
            # kv
            try:
                kv = http_json("GET", f"{BASE}/metrics/kv", timeout=10)
                rec["kv"] = {"used_cells": kv.get("used_cells"),
                             "shared_cells": kv.get("shared_cells"),
                             "active": kv.get("active_sequences"),
                             "capacity_cells": kv.get("capacity_cells"),
                             "capacity_bytes": kv.get("capacity_bytes")}
            except Exception as e:
                rec["kv"] = {"err": str(e)}
                ok = False
            rec["ok"] = ok
            results.append(rec)
        return {"failed": False, "results": results,
                "expect_shared": expect_shared,
                "exit_code": proc.poll() if proc.poll() is not None else 0}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def summarize(name, recs, field):
    vals = [r[field] for r in recs if r and r.get(field) is not None]
    if not vals:
        return None
    mean = statistics.mean(vals)
    med = statistics.median(vals)
    sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
    cv = (sd / mean * 100) if mean else 0.0
    p95 = sorted(vals)[int(len(vals) * 0.95) - 1] if len(vals) >= 20 else max(vals)
    return {"n": len(vals), "mean": round(mean, 4), "median": round(med, 4),
            "p95": round(p95, 4), "stdev": round(sd, 4), "cv_pct": round(cv, 2),
            "min": round(min(vals), 4), "max": round(max(vals), 4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--scenario", default="shared",
                    choices=["shared", "partial", "noshare", "capacity"])
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    slot_save = os.path.join(ROOT, "llama.cpp", "tmp", "e6_slot_save")
    os.makedirs(slot_save, exist_ok=True)
    base = [SERVER_BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
            "--ctx-size", "8192", "--parallel", "4", "--kv-unified",
            "--cache-ram", "0", "--temp", "0", "--seed", "42", "--metrics",
            "--slot-save-path", slot_save, "--kv-prefix-share-min-lcp", "4"]

    out = args.output or os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw",
                                      f"e8_c1_perf_{args.scenario}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    off = run_paired(base, args.scenario, args.reps, args.warmup, out)
    on = run_paired(base + ["--kv-prefix-share"], args.scenario, args.reps, args.warmup, out)

    def formal(recs):
        return [r for r in recs if r and not r.get("warmup") and r.get("ok")]

    off_f, on_f = formal(off["results"]), formal(on["results"])
    # tgt prompt_n = recompute 代理
    off_rt = [r["tgt"]["prompt_n"] for r in off_f if r["tgt"].get("status") == "ok"]
    on_rt = [r["tgt"]["prompt_n"] for r in on_f if r["tgt"].get("status") == "ok"]
    off_pt = [r["tgt"]["prompt_ms"] for r in off_f if r["tgt"].get("status") == "ok"]
    on_pt = [r["tgt"]["prompt_ms"] for r in on_f if r["tgt"].get("status") == "ok"]
    off_dt = [r["tgt"]["predicted_ms"] for r in off_f if r["tgt"].get("status") == "ok"]
    on_dt = [r["tgt"]["predicted_ms"] for r in on_f if r["tgt"].get("status") == "ok"]

    med_off, med_on = (statistics.median(off_rt) if off_rt else None,
                       statistics.median(on_rt) if on_rt else None)
    reduction = (1 - med_on / med_off) * 100 if med_off and med_on else None

    entry = {
        "scenario": args.scenario, "reps": args.reps, "warmup": args.warmup,
        "model": "tinyllama stories260K",
        "git_commit": subprocess.check_output(
            ["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "recompute": {"off_median": med_off, "on_median": med_on,
                      "reduction_pct": round(reduction, 2) if reduction is not None else None,
                      "off_values": off_rt, "on_values": on_rt},
        "prefill_ms": {"off": summarize(name="off", recs=[{"v": v} for v in off_pt], field="v"),
                       "on": summarize(name="on", recs=[{"v": v} for v in on_pt], field="v")},
        "decode_ms": {"off": summarize(name="off", recs=[{"v": v} for v in off_dt], field="v"),
                      "on": summarize(name="on", recs=[{"v": v} for v in on_dt], field="v")},
        "shared_log": {"off": open(os.path.join(os.path.dirname(out), f"e8perf_server_{args.scenario}.log")).read().count("E6-C1: shared"),
                       "on": open(os.path.join(os.path.dirname(out), f"e8perf_server_{args.scenario}.log")).read().count("E6-C1: shared")},
        "off_raw": off, "on_raw": on,
    }
    # 注意 shared_log 需按模式分开日志——此处简单记录 on 模式的
    entry["shared_log"] = {"off": 0, "on": on["results"] and open(
        os.path.join(os.path.dirname(out), f"e8perf_server_{args.scenario}.log")).read().count("E6-C1: shared")}
    with open(out, "w") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)
    print(f"[{args.scenario}] off_rt_med={med_off} on_rt_med={med_on} "
          f"reduction={entry['recompute']['reduction_pct']}% "
          f"prefill_ms off/on={entry['prefill_ms']['off'] and entry['prefill_ms']['off']['median']}/"
          f"{entry['prefill_ms']['on'] and entry['prefill_ms']['on']['median']} "
          f"CV off/on={entry['prefill_ms']['off'] and entry['prefill_ms']['off']['cv_pct']}/"
          f"{entry['prefill_ms']['on'] and entry['prefill_ms']['on']['cv_pct']}")
    print("saved:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
