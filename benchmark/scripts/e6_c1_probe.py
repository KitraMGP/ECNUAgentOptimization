#!/usr/bin/env python3
"""E6.2-C1: 跨 slot 前缀共享真实模型验证（4B GPU）。

场景：请求1 A+X（id_slot=0）完成后 idle 保留；请求2 A+Y（id_slot=1）
在 kv_prefix_share on 时应共享 A 前缀（cached_tokens 提升、used_cells 减少），
off 时全量重算。两模式下输出必须完全一致（无损）。

用法：python scripts/e6_c1_probe.py --mode on|off --output results/kv_optimization/raw/c1_<mode>.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from urllib import request, error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SERVER = os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")
MODEL = os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"

CHUNK = ("The Corvus Delta network monitors riparian wetlands and upstream runoff while "
         "seasonal sensors record turbidity, pH, dissolved oxygen, nitrate, and phosphate "
         "levels across the main channel and its tributaries. Data from the network informs "
         "regional water resource planning and flood mitigation strategies. ")


def build_prompt(n_tokens: int, suffix: str) -> str:
    chars = int(n_tokens * 4.5)
    body = " ".join([CHUNK] * (chars // len(CHUNK) + 1))[:chars]
    return body + f"\n\n{suffix}\nConfirm the station status."


def http_json(method: str, url: str, body: dict | None = None, timeout: int = 600):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def wait_health(proc, timeout_s=240):
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["on", "off"], default="on")
    ap.add_argument("--output", default="")
    ap.add_argument("--prefix-tokens", type=int, default=1500)
    args = ap.parse_args()

    log_path = os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw", f"c1_server_{args.mode}.log")
    cmd = [SERVER, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "-ngl", "99", "--ctx-size", "8192", "--parallel", "4", "--kv-unified",
           "--cache-ram", "0", "--temp", "0", "--seed", "42", "--metrics"]
    if args.mode == "on":
        cmd.append("--kv-prefix-share")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    logf = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
    if not wait_health(proc):
        print("server failed to start")
        return 1

    A = build_prompt(args.prefix_tokens, "Station ALPHA baseline")
    X = "\nAppend observation X: flow 12.4 m3/s, temp 8.1 C."
    Y = "\nAppend observation Y: flow 11.9 m3/s, temp 9.2 C."

    def comp(prompt, slot):
        body = {"messages": [{"role": "user", "content": prompt}],
                "max_tokens": 16, "temperature": 0.0,
                "chat_template_kwargs": {"enable_thinking": False}}
        try:
            d = http_json("POST", f"{BASE}/v1/chat/completions", body, timeout=900)
            usage = d.get("usage", {})
            return {"status": "ok",
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "cached_tokens": usage.get("prompt_tokens_details", {}).get("cached_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "output": d["choices"][0]["message"].get("content", ""),
                    "output_sha256": hashlib.sha256(
                        d["choices"][0]["message"].get("content", "").encode()).hexdigest()}
        except error.HTTPError as e:
            return {"status": f"http_{e.code}", "prompt_tokens": 0, "cached_tokens": 0,
                    "completion_tokens": 0, "output": "", "output_sha256": ""}

    # 注意：id_slot 参数在 chat 中不可用——用 /completion 接口（支持 id_slot）
    def comp_legacy(prompt, slot):
        body = {"prompt": prompt, "n_predict": 16, "temperature": 0, "cache_prompt": True,
                "id_slot": slot}
        d = http_json("POST", f"{BASE}/completion", body, timeout=900)
        tim = d.get("timings", {})
        return {"status": "ok",
                "prompt_tokens": d.get("tokens_cached", 0) + tim.get("prompt_n", 0),
                "cache_n_past": tim.get("cache_n", 0),  # = n_past（本次复用前缀 token 数，E6.2 命中证据）
                "prompt_n": tim.get("prompt_n", 0),
                "completion_tokens": tim.get("predicted_n", 0),
                "output": d.get("content", ""),
                "output_sha256": hashlib.sha256(d.get("content", "").encode()).hexdigest()}

    try:
        r1 = comp_legacy(A + X, 0)
        r2 = comp_legacy(A + Y, 1)
        kv = http_json("GET", f"{BASE}/metrics/kv", timeout=10)
        kv_stats = kv.get("kv_stats") if "kv_stats" in kv else kv
        rec = {
            "experiment_id": f"e6_c1_probe_{args.mode}",
            "candidate_id": "C1_RADIX_PREFIX_SHARING",
            "variant": args.mode, "workload_id": "C1_PROBE",
            "repetition": 0,
            "git_commit": subprocess.check_output(
                ["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
            "mode": args.mode,
            "req1": r1, "req2": r2,
            "kv_stats": kv_stats,
            "lossless_match": None,  # 由汇总脚本跨 mode 比对
        }
        print(json.dumps({k: rec[k] for k in ("variant", "req1", "req2")}, indent=1))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    out = args.output or os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw",
                                      f"e6_c1_probe_{args.mode}.json")
    with open(out, "w") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    print("saved:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
