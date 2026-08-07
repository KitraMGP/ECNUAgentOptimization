#!/usr/bin/env python3
"""E3.5：真实流量 replay skeleton（dry-run）。

只复现生命周期分布（请求到达间隔 / prompt token 长度 / 生成 token 数 /
回访间隔），使用固定合成占位 token 构造等长请求；
**不**复现语义正确性或模型质量。

usage: uv run python scripts/e35_replay_trace.py <trace.json> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys

PLACEHOLDER = "The Corvus Delta monitoring network collects data across zones. "


def build_placeholder_prompt(n_tokens: int) -> str:
    chars = int(n_tokens * 6.0)
    return " ".join([PLACEHOLDER] * (chars // len(PLACEHOLDER) + 1))[:chars]


def plan(trace_path: str) -> dict:
    with open(trace_path, "r", encoding="utf-8") as f:
        trace = json.load(f)
    reqs = []
    for s in trace.get("sessions", []):
        for r in s.get("requests", []):
            reqs.append({
                "session_id": s["session_id"],
                "turn": r["turn"],
                "prompt_tokens": r["prompt_tokens"],
                "gen_tokens": r["gen_tokens"],
                "arrival_delta_ms": r.get("arrival_delta_ms", 0),
                "revisit_delta_ms": r.get("revisit_delta_ms"),
            })
    return {"n_sessions": len(trace.get("sessions", [])), "n_requests": len(reqs),
            "total_prompt_tokens": sum(r["prompt_tokens"] for r in reqs),
            "total_gen_tokens": sum(r["gen_tokens"] for r in reqs),
            "requests": reqs}


def dry_run(trace_path: str) -> dict:
    p = plan(trace_path)
    # dry-run：只验证 plan 可构造（不实际发请求）
    for r in p["requests"][:3]:
        prompt = build_placeholder_prompt(r["prompt_tokens"])
        assert len(prompt) > 0
    p["dry_run"] = True
    p["note"] = "replay 只复现生命周期分布（等长占位 token），不复现语义正确性/模型质量"
    return p


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", help="匿名 trace JSON")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    res = dry_run(args.trace) if args.dry_run else plan(args.trace)
    print(json.dumps(res, ensure_ascii=False, indent=2))
