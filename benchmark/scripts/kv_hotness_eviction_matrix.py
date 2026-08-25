#!/usr/bin/env python3
"""Qwen3.5-4B session-hotness victim matrix.

Each independent server run creates one old, frequently reused session and
one newer cold session, then submits a pressure request.  The selected victim
is compared across off/recency/lfu/cost.  This measures session-level eviction,
not token-level lossy compression.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from typing import Any

from kv_hotness_tiering_eval import (
    build_prompt,
    completion,
    grep_log,
    kv,
    start_server,
    stop_server,
    wait_health,
)


STRATEGIES = ("off", "recency", "lfu", "cost")
REPEATS = 20


def run_strategy(out_dir: str, strategy: str, run_index: int) -> dict[str, Any]:
    log_path = os.path.join(out_dir, f"{strategy}_r{run_index}.log")
    proc = start_server(
        log_path,
        parallel=3,
        ctx=2048,
        cache_ram=0,
        hotness=strategy,
        tiering="none",
        idle_ticks=8,
        pressure=0.90,
        extra=["--unified-idle-slot-policy", "default"],
    )
    if not wait_health(proc):
        stop_server(proc)
        return {"strategy": strategy, "pass": False, "error": "server_start_failed"}

    hot = build_prompt(700, "HOT_SESSION")
    cold = build_prompt(700, "COLD_SESSION", prefix_text="A distinct cold telemetry stream describes unrelated measurements.")
    pressure = build_prompt(900, "PRESSURE_SESSION", prefix_text="A third independent pressure workload contains unrelated records.")

    rows = []
    try:
        first = completion(hot, slot=0, n_predict=1)
        rows.append(first)
        for _ in range(REPEATS - 1):
            rows.append(completion(hot, slot=0, n_predict=1))
        cold_row = completion(cold, slot=1, n_predict=1)
        before = kv()
        pressure_row = completion(pressure, slot=2, n_predict=1)
        after = kv()
        purged = [int(x) for x in grep_log(log_path, r"purging slot (\d+) with")]
        return {
            "strategy": strategy,
            "pass": bool(pressure_row.get("status") == "ok" and purged),
            "purged_slots": purged,
            "selected_victim": purged[0] if purged else None,
            "hot_repeats": REPEATS,
            "hot_last": rows[-1],
            "cold": cold_row,
            "pressure": pressure_row,
            "metrics_before_pressure": before,
            "metrics_after_pressure": after,
            "hotness_policy": (after.get("hotness") or {}).get("policy"),
            "lookup_count": (after.get("hotness") or {}).get("lookup_count"),
            "lcp_hit_count": (after.get("hotness") or {}).get("lcp_hit_count"),
        }
    finally:
        stop_server(proc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be >= 1")
    os.makedirs(args.out_dir, exist_ok=True)
    report = {
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": "Qwen3.5-4B-Q4_K_M.gguf",
        "scenario": "session_hotness_victim_matrix",
        "repeats": REPEATS,
        "matrix_runs": args.runs,
        "strategies": {},
    }
    parser_runs = getattr(args, "runs", 1)
    for strategy in STRATEGIES:
        runs = [run_strategy(args.out_dir, strategy, index) for index in range(1, parser_runs + 1)]
        victims = [item.get("selected_victim") for item in runs]
        report["strategies"][strategy] = {
            "runs": runs,
            "victims": victims,
            "victim_mode": max(set(victims), key=victims.count) if victims else None,
            "all_pass": all(item.get("pass", False) for item in runs),
        }
    report["all_pass"] = all(item["all_pass"] for item in report["strategies"].values())
    path = os.path.join(args.out_dir, "report.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"wrote {path}")
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
