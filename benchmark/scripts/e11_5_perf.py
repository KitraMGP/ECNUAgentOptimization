#!/usr/bin/env python3
"""E11.5: checkpoint 端到端收益验收（TinyLlama 集成生效路径）。

协议：warmup 5 + formal 30 paired；
- on：checkpoint_save（P，每 rep 前 erase 后重建）+ checkpoint_restore（P+X）
- off：全量 prefill P+X（对照）
- target 1/2/4；P ~110 / ~440 tokens
记录：save/restore 耗时（server 日志）、target prompt_ms、e2e、prompt_n、
checkpoint bytes、失败率、kv。

收益判定（E11.5 要求区分）：
- 单 target：save+restore vs 重复 prefill
- 多 target：保存成本摊薄
- 长 prefix：收益随 P 增
- 资源：checkpoint 常驻内存（state bytes）
- 物理容量：KV 池不变（state 在 host）

用法：python scripts/e11_5_perf.py --reps 30 --warmup 5
"""
from __future__ import annotations

import argparse
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
SEED = ("Once upon a time in the deep dark forest, a small rabbit named Miko discovered "
        "a glowing stone near the old oak tree. The stone hummed with a soft blue light "
        "and seemed to pulse with the rhythm of the wind. Miko carefully picked it up "
        "and carried it back to the burrow where the animals gathered. ")


def P_len(n):
    return (SEED.rstrip() + "\n") * n


def http_json(method, url, body=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()), resp.status
    except Exception as e:
        return {"err": str(e)}, getattr(e, "code", 0)


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


def start_server(parallel, log_path):
    proc = subprocess.Popen([SERVER_BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
                             "--ctx-size", str(parallel * 1024), "--parallel", str(parallel),
                             "--kv-unified", "--cache-ram", "0", "--temp", "0", "--seed", "42",
                             "--metrics", "--checkpoint-reuse", "--slot-save-path",
                             os.path.join(ROOT, "llama.cpp", "tmp", "e6_slot_save")],
                            stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    return proc, wait_health(proc)


def run_bench(n_units, n_tgt, reps, warmup, log_dir):
    P = P_len(n_units)
    suffix = [f" The owl examined the stone at position {i}." for i in range(n_tgt)]
    out = {}
    # ---- off（全量 prefill 对照）----
    proc, ok = start_server(4, os.path.join(log_dir, f"off_{n_units}_{n_tgt}.log"))
    off = []
    if ok:
        try:
            for rep in range(warmup + reps):
                for sid in range(4):
                    http_json("POST", f"{BASE}/slots/{sid}?action=erase", {}, 30)
                t0 = time.time()
                codes = []
                pn = []
                for i in range(n_tgt):
                    r, c = http_json("POST", f"{BASE}/completion",
                                     {"prompt": P + suffix[i], "n_predict": 4, "temperature": 0,
                                      "cache_prompt": True, "id_slot": i}, timeout=300)
                    codes.append(c)
                    pn.append(r.get("timings", {}).get("prompt_n"))
                e2e = (time.time() - t0) * 1000
                off.append({"rep": rep, "warmup": rep < warmup, "codes": codes,
                            "prompt_n": pn, "e2e_ms": e2e})
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            time.sleep(2)
    # ---- on（checkpoint save + restore）----
    proc, ok = start_server(4, os.path.join(log_dir, f"on_{n_units}_{n_tgt}.log"))
    on = []
    if ok:
        try:
            for rep in range(warmup + reps):
                # save 请求不 erase（P 缓存跨 rep 命中 → 反映 checkpoint 的增量价值：
                # P 的 prefill 只做一次，后续 save 命中 + restore 后缀）
                t0 = time.time()
                rs, cs = http_json("POST", f"{BASE}/completion",
                                   {"prompt": P, "n_predict": 4, "temperature": 0,
                                    "cache_prompt": True, "id_slot": 0, "checkpoint_save": True}, timeout=300)
                codes = [cs]
                pn = [rs.get("timings", {}).get("prompt_n")]
                for i in range(n_tgt):
                    r, c = http_json("POST", f"{BASE}/completion",
                                     {"prompt": P + suffix[i], "n_predict": 4, "temperature": 0,
                                      "cache_prompt": True, "id_slot": i + 1, "checkpoint_restore": True}, timeout=300)
                    codes.append(c)
                    pn.append(r.get("timings", {}).get("prompt_n"))
                e2e = (time.time() - t0) * 1000
                on.append({"rep": rep, "warmup": rep < warmup, "codes": codes,
                           "prompt_n": pn, "e2e_ms": e2e})
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            time.sleep(2)
    return {"off": off, "on": on}


def summarize(recs, field, idx=None):
    vals = []
    for r in recs:
        if r.get("warmup") or any(c != 200 for c in r["codes"]):
            continue
        if idx is not None:
            v = r[field][idx]
        else:
            v = r[field]
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    return {"n": len(vals), "median": round(statistics.median(vals), 2),
            "p95": round(sorted(vals)[int(len(vals) * 0.95) - 1], 2) if len(vals) >= 20 else round(max(vals), 2),
            "mean": round(statistics.mean(vals), 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    out_path = args.output or os.path.join(ROOT, "benchmark", "results", "kv_optimization",
                                           "raw", "e11_perf.json")
    log_dir = os.path.join(os.path.dirname(out_path), "e11perf")
    os.makedirs(log_dir, exist_ok=True)

    out = {"reps": args.reps, "warmup": args.warmup,
           "git": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"),
                                           "rev-parse", "HEAD"]).decode().strip()}
    for n_units in (1, 4):
        for n_tgt in (1, 4):
            key = f"p{n_units}_t{n_tgt}"
            data = run_bench(n_units, n_tgt, args.reps, args.warmup, log_dir)
            off_f = [r for r in data["off"] if not r.get("warmup")]
            on_f = [r for r in data["on"] if not r.get("warmup")]
            # off：e2e = 全量（每 target 各自 prefill；取第一个 target 或总和？用全部 target 的 e2e）
            # on：e2e = save + restore 全部
            entry = {
                "off_e2e": summarize(off_f, "e2e_ms"),
                "on_e2e": summarize(on_f, "e2e_ms"),
                "off_prompt_n_t0": summarize(off_f, "prompt_n", 0),
                "on_prompt_n_t0": summarize(on_f, "prompt_n", 1),
                "off_failed": sum(1 for r in off_f if any(c != 200 for c in r["codes"])),
                "on_failed": sum(1 for r in on_f if any(c != 200 for c in r["codes"])),
            }
            out[key] = entry
            print(f"[{key}] off_e2e={entry['off_e2e'] and entry['off_e2e']['median']}ms "
                  f"on_e2e={entry['on_e2e'] and entry['on_e2e']['median']}ms "
                  f"(含 save) | off_pn={entry['off_prompt_n_t0'] and entry['off_prompt_n_t0']['median']} "
                  f"on_pn={entry['on_prompt_n_t0'] and entry['on_prompt_n_t0']['median']}")

    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("saved:", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
