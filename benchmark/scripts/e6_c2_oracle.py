#!/usr/bin/env python3
"""E6.3-C2: SnapKV 式 prompt KV 压缩离线 oracle（Qwen3.5-4B）。

目的：评估"prefill 后压缩 prompt KV"在 hybrid 主模型上的收益上界与可行性。
方法：
  1. 真实 4B server 跑长 prompt 请求，采集 /metrics/kv（attention KV 占用）；
  2. 离线模拟 SnapKV 式压缩预算 K（保留最近 observation window + 选中的 prefix token），
     计算 attention KV bytes 下降上界（recurrent state 不可压缩，单独报告）；
  3. 质量：离线无法测量（在线路径未实现）→ quality_gate = NOT_APPLICABLE；
  4. 给出"是否值得进入在线路径"的工程/架构判断。

用法：python scripts/e6_c2_oracle.py --output results/kv_optimization/raw/e6_c2_oracle.json
"""
from __future__ import annotations

import argparse
import json
import os
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


def build_prompt(n_tokens: int) -> str:
    chars = int(n_tokens * 4.5)
    body = " ".join([CHUNK] * (chars // len(CHUNK) + 1))[:chars]
    return body + "\n\nSummarize the monitoring findings in one sentence."


def http_json(method, url, body=None, timeout=900):
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
    ap.add_argument("--output", default="")
    ap.add_argument("--prompt-tokens", type=int, default=4000)
    ap.add_argument("--budgets", default="1024,2048,3072")
    args = ap.parse_args()

    log_path = os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw", "c2_server.log")
    cmd = [SERVER, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "-ngl", "99", "--ctx-size", "8192", "--parallel", "4", "--kv-unified",
           "--cache-ram", "0", "--temp", "0", "--seed", "42", "--metrics"]
    logf = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
    if not wait_health(proc):
        print("server failed to start")
        return 1

    try:
        prompt = build_prompt(args.prompt_tokens)
        body = {"messages": [{"role": "user", "content": prompt}],
                "max_tokens": 16, "temperature": 0.0,
                "chat_template_kwargs": {"enable_thinking": False}}
        d = http_json("POST", f"{BASE}/v1/chat/completions", body, timeout=1200)
        usage = d.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        kv = http_json("GET", f"{BASE}/metrics/kv", timeout=10)
        kv_stats = kv.get("kv_stats") if "kv_stats" in kv else kv

        used_cells = kv_stats.get("used_cells", 0)
        used_bytes = kv_stats.get("used_bytes", 0)
        capacity_cells = kv_stats.get("capacity_cells", 0)
        capacity_bytes = kv_stats.get("capacity_bytes", 0)
        active_seqs = kv_stats.get("active_sequences", 0)

        # SnapKV 式压缩模拟：预算 K（保留最近 observation window 32 + 从 prefix 选 K-32）
        obs_window = 32  # SnapKV 默认 observation window（LongBench 32）
        budgets = [int(x) for x in args.budgets.split(",")]
        scenarios = []
        for K in budgets:
            keep = min(K, used_cells)
            # attention KV 部分按 keep/used 比例下降（recurrent state 不可压缩，单独说明）
            attn_bytes_after = used_bytes * keep / max(1, used_cells)
            attn_bytes_saved = used_bytes - attn_bytes_after
            # hybrid 总内存：attention KV + recurrent state + 权重（权重不随 KV 压缩变）
            scenarios.append({
                "budget_tokens": K,
                "keep_tokens": keep,
                "used_cells": used_cells,
                "attn_kv_bytes_before": used_bytes,
                "attn_kv_bytes_after_estimate": attn_bytes_after,
                "attn_kv_bytes_saved": attn_bytes_saved,
                "attn_kv_reduction_pct": round(100.0 * attn_bytes_saved / max(1, used_bytes), 2),
                "note": "attention-only 估算；recurrent state 不可压缩（hybrid 架构限制）",
            })

        rec = {
            "experiment_id": "e6_c2_oracle",
            "candidate_id": "C2_SNAPKV_PROMPT_COMPRESSION",
            "variant": "offline_oracle", "workload_id": "C2_ORACLE",
            "repetition": 0,
            "git_commit": subprocess.check_output(
                ["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
            "prompt_tokens": prompt_tokens,
            "kv_stats": kv_stats,
            "scenarios": scenarios,
            "architecture_analysis": {
                "model_is_hybrid": True,
                "recurrent_state_compressible": False,
                "reason": "Qwen3.5-4B 为 hybrid（attention+recurrent）：recurrent state 是连续不可分割状态，"
                          "token 级压缩（删 attention token）会使 recurrent/attention 位置错位（E6.2 已证同源限制）；"
                          "SnapKV 完整实现需 FA 注意力分数输出通道 + KV 紧凑重排 + recurrent 兼容改造",
                "quality_gate": "NOT_APPLICABLE",
                "quality_note": "离线 oracle 无法测量生成质量（在线路径未实现）；不做任何质量声明",
            },
            "judgement": {
                "enter_online_path": False,
                "reason": "主模型 Qwen3.5-4B 为 hybrid：① recurrent state 不可 token 级压缩，收益上限受限；"
                          "② FA 分数输出通道 + KV 紧凑重排工程量与收益不匹配；"
                          "③ 标准架构模型上可作未来工作（SnapKV 论文在 LLaMA/Mistral 上验证）",
                "deferred_to": "标准 attention-only 架构模型（如 LLaMA/Mistral 系列 GGUF）",
            },
        }
        print(json.dumps({"prompt_tokens": prompt_tokens, "kv_stats": kv_stats,
                          "scenarios": scenarios, "judgement": rec["judgement"]}, indent=1))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    out = args.output or os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw", "e6_c2_oracle.json")
    with open(out, "w") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    print("saved:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
