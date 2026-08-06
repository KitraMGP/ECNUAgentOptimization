#!/usr/bin/env python3
"""E2.5：生成 results/e25/manifest.json（冻结 hash + 预注册阈值 + 协议记录）。

预注册阈值在正式实验开始前写入本脚本，实验后运行本脚本生成 manifest；
阈值本身不得修改（本文件 git 历史可审计）。
"""
from __future__ import annotations

import csv
import glob
import hashlib
import json
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
sys.path.insert(0, BENCH)


def sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    out_dir = os.path.join(BENCH, "results", "e25")
    os.makedirs(out_dir, exist_ok=True)

    from workload.branch_pressure import build_prompts, SEQUENCE
    frozen = hashlib.sha256(json.dumps(build_prompts(), sort_keys=True).encode()).hexdigest()

    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "phase": "E2.5",
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "binary_sha256": sha(os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")),
        "model_sha256": sha(os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")),
        "gpu": "NVIDIA GeForce RTX 4060 Laptop GPU 8188 MiB",
        "workload": {
            "file": "benchmark/workload/branch_pressure.py",
            "file_sha256": sha(os.path.join(BENCH, "workload", "branch_pressure.py")),
            "prompts_sha256": frozen,
            "sequence": [t for t, *_ in SEQUENCE],
            "ctx": 8192, "parallel": 2, "cache_reuse": 0, "seed": 42, "temp": 0,
            "ctx_choice": "ctx=8192：prompt 总长 3281-3387 tokens（40-42% of 8192），满足 40-70% 目标",
        },
        "ram_conditions": {
            "off": {"cache_ram_mib": 0, "role": "正式性能条件"},
            "pressure": {"cache_ram_mib": 128, "role": "正式性能条件",
                         "calibration": "calib 128/256/512 MiB 均无 RAM restore 生效"
                                        "（4B 长 prompt 下 restore 不触发，见报告 7 节）；"
                                        "128 MiB 冻结为压力容量"},
            "default": {"cache_ram_mib": 8192, "role": "部署边界 smoke 2×2"},
        },
        "server_per_replicate_protocol": {
            "server_restarted": "每 replicate 全新 llama-server 进程",
            "fresh_process_verified": "启动后 /metrics/kv used_cells==0 && active_sequences==0",
            "warmup": "无关短请求（不进正式统计）",
            "post_warmup": "erase slots 清 KV",
            "termination": "SIGTERM + 确认进程退出",
        },
        "pre_registered_thresholds": {
            "correctness": {
                "request_failure": 0,
                "contamination": 0,
                "evaluator_pass_rate": "不低于同条件 default",
                "output_schema_failure": "不增加",
                "routing_deterministic": True,
                "revisit_must_select_original_branch_slot": "prefix-branch 回访必须 prefix_branch_revisit 且选中原分支 slot",
                "fallback_within_slot_limit": "分支数 > slot 数时的 fallback 无错误",
            },
            "performance": {
                "metrics": ["branch revisit latency p50", "branch revisit latency p95",
                            "workload total wall time", "prompt_processed_tokens",
                            "logical_prefix_reuse_tokens", "throughput"],
                "statistical_unit": "independent replicate（server-per-replicate）为唯一独立样本；"
                                    "请求仅作 replicate 内聚合；报告 paired difference + median + 方向一致性；"
                                    "5 reps 不宣称强统计显著性",
            },
            "benefit_gate": "RAM pressure 或 RAM off 至少一个正式条件须同时满足："
                            "1) 5/5 prefix-branch wall time 不劣于 default；"
                            "2) ≥4/5 replicate 的 branch revisit p50 更低；"
                            "3) paired median revisit p50 改善 ≥5%；"
                            "4) paired median wall time 改善 ≥3% 或 prompt_processed_tokens 降 ≥10%；"
                            "5) request failure 不增加；6) evaluator 不下降；7) contamination=0；"
                            "8) default RAM-on 行为无回归；9) routing overhead ≤2%",
        },
    }

    # 结果文件 sha256（含子目录 off_full/pressure_full）
    result_files = {}
    for p in sorted(glob.glob(os.path.join(out_dir, "**", "*.json"), recursive=True)):
        rel = os.path.relpath(p, out_dir)
        if os.path.basename(p) == "manifest.json":
            continue
        result_files[rel] = sha(p)
    manifest["result_files_sha256"] = result_files

    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("manifest.json written:", len(result_files), "result files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
