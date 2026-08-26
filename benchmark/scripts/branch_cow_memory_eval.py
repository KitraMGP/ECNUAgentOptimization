#!/usr/bin/env python3
"""Paired memory evaluation for in-memory conversation branch forks.

The control keeps the parent prompt and independently pre-fills every full
branch. The fork arm creates each target from the parent state, then only
processes the divergent suffix. This measures logical live-cell savings and
correctness; the current contiguous allocator's pre-allocated capacity is
reported separately and must not be described as reduced GPU reservation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import tempfile
import time
from typing import Any

import psutil

try:
    from .kv_hotness_tiering_eval import (
        MODEL,
        SERVER,
        build_prompt,
        completion,
        http_json,
        start_server,
        stop_server,
        wait_health,
        kv,
        BASE,
    )
except ImportError:  # direct `python scripts/branch_cow_memory_eval.py`
    from kv_hotness_tiering_eval import (
        MODEL,
        SERVER,
        build_prompt,
        completion,
        http_json,
        start_server,
        stop_server,
        wait_health,
        kv,
        BASE,
    )


SUFFIXES = (
    " Continue the branch with the drought mitigation plan.",
    " Continue the branch with the flood response plan.",
    " Continue the branch with the water quality investigation.",
    " Continue the branch with the emergency logistics plan.",
    " Continue the branch with the biodiversity recovery plan.",
    " Continue the branch with the reservoir operation plan.",
    " Continue the branch with the sensor maintenance plan.",
    " Continue the branch with the public communication plan.",
)
N_PREDICT = 1


def fork(target_slot: int) -> dict[str, Any]:
    return http_json("POST", f"{BASE}/slots/0?action=fork", {"target_slot": target_slot})


def rss_mb(proc) -> float | None:
    try:
        return psutil.Process(proc.pid).memory_info().rss / (1024 * 1024)
    except (OSError, psutil.Error):
        return None


def gpu_mb(proc) -> float | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            fields = [field.strip() for field in line.split(",")]
            if len(fields) == 2 and int(fields[0]) == proc.pid:
                return float(fields[1])
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def run_arm(out_dir: str, arm: str, fanout: int, ctx: int, run_index: int, reverse: bool = False) -> dict[str, Any]:
    log_path = os.path.join(out_dir, f"{arm}_r{run_index}.log")
    slot_dir = tempfile.mkdtemp(prefix=f"branch_cow_{arm}_")
    allocator = os.environ.get("BRANCH_KV_ALLOCATOR", "contiguous")
    block_size = int(os.environ.get("BRANCH_KV_BLOCK_SIZE", "16"))
    server_extra = ["--reasoning", "off", "--reasoning-format", "none", "--reasoning-budget", "0"]
    if allocator == "contiguous":
        server_extra.extend(["--slot-save-path", slot_dir])
    else:
        server_extra.append("--no-cache-idle-slots")
    proc = start_server(
        log_path,
        parallel=fanout + 1,
        ctx=ctx,
        cache_ram=0,
        hotness="off",
        tiering="none",
        idle_ticks=8,
        pressure=0.90,
        extra=server_extra + (["--kv-allocator", allocator, "--kv-block-size", str(block_size)] if allocator == "paged" else []),
    )
    if not wait_health(proc):
        stop_server(proc)
        shutil.rmtree(slot_dir, ignore_errors=True)
        return {"arm": arm, "run": run_index, "pass": False, "error": "server_start_failed"}

    root = build_prompt(int(os.environ.get("BRANCH_ROOT_TOKENS", "240")), "BRANCH_ROOT")
    rows_by_index: dict[int, dict[str, Any]] = {}
    forks: list[dict[str, Any]] = []
    try:
        parent = completion(root, slot=0, n_predict=N_PREDICT)
        # Hybrid recurrent state cannot arbitrarily roll back a long generated
        # suffix. A real conversation branch includes the parent's assistant
        # output in the next message history, so continue from that exact
        # state boundary instead of asking the recurrent cache to erase it.
        branch_root = root + (parent.get("content") or "")
        prompts = [branch_root + SUFFIXES[i] for i in range(fanout)]
        if arm == "fork":
            for slot in range(1, fanout + 1):
                forks.append(fork(slot))

        order = list(range(fanout - 1, -1, -1)) if reverse else list(range(fanout))
        for index in order:
            prompt = prompts[index]
            slot = index + 1 if arm == "fork" else index + 1
            rows_by_index[index] = completion(prompt, slot=slot, n_predict=N_PREDICT)

        rows = [rows_by_index[index] for index in range(fanout)]

        metrics = kv()
        return {
            "arm": arm,
            "run": run_index,
            "pass": (
                parent.get("status") == "ok"
                and len(rows) == fanout
                and all(row.get("status") == "ok" for row in rows)
                and (arm == "control" or all(item.get("target_slot") is not None for item in forks))
            ),
            "parent": parent,
            "branches": rows,
            "branch_hashes": [row.get("content_sha256") for row in rows],
            "forks": forks,
            "metrics": metrics,
            "rss_mb": rss_mb(proc),
            "gpu_mb": gpu_mb(proc),
        }
    finally:
        stop_server(proc)
        shutil.rmtree(slot_dir, ignore_errors=True)


def run_isolated_branch(out_dir: str, branch_index: int, ctx: int, run_index: int) -> dict[str, Any]:
    """Full-prefill reference for one branch in a fresh server lifetime."""
    log_path = os.path.join(out_dir, f"isolated_b{branch_index}_r{run_index}.log")
    slot_dir = tempfile.mkdtemp(prefix=f"branch_cow_isolated_b{branch_index}_")
    proc = start_server(
        log_path,
        parallel=2,
        ctx=ctx,
        cache_ram=0,
        hotness="off",
        tiering="none",
        idle_ticks=8,
        pressure=0.90,
        extra=["--reasoning", "off", "--reasoning-format", "none", "--reasoning-budget", "0", "--slot-save-path", slot_dir],
    )
    if not wait_health(proc):
        stop_server(proc)
        shutil.rmtree(slot_dir, ignore_errors=True)
        return {"branch_index": branch_index, "run": run_index, "pass": False, "error": "server_start_failed"}
    try:
        root = build_prompt(int(os.environ.get("BRANCH_ROOT_TOKENS", "240")), "BRANCH_ROOT")
        parent = completion(root, slot=0, n_predict=N_PREDICT)
        prompt = root + (parent.get("content") or "") + SUFFIXES[branch_index]
        branch = completion(prompt, slot=1, n_predict=N_PREDICT)
        return {
            "branch_index": branch_index,
            "run": run_index,
            "pass": parent.get("status") == "ok" and branch.get("status") == "ok",
            "parent": parent,
            "branch": branch,
            "hash": branch.get("content_sha256"),
        }
    finally:
        stop_server(proc)
        shutil.rmtree(slot_dir, ignore_errors=True)


def aggregate(arms: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for arm, rows in arms.items():
        valid = [row for row in rows if row.get("pass")]
        used = [row["metrics"].get("used_cells") for row in valid]
        shared = [row["metrics"].get("shared_cells") for row in valid]
        capacity = [row["metrics"].get("capacity_bytes") for row in valid]
        allocated = [
            (row["metrics"].get("allocator") or {}).get("allocated_bytes")
            for row in valid
        ]
        allocated = [value for value in allocated if value is not None]
        allocated_blocks = [
            (row["metrics"].get("allocator") or {}).get("allocated_blocks")
            for row in valid
        ]
        allocated_blocks = [value for value in allocated_blocks if value is not None]
        recurrent = [
            (row["metrics"].get("recurrent") or {}).get("capacity_bytes")
            for row in valid
        ]
        recurrent = [value for value in recurrent if value is not None]
        result[arm] = {
            "runs": len(rows),
            "passed_runs": len(valid),
            "used_cells": used,
            "used_cells_mean": statistics.mean(used) if used else None,
            "shared_cells": shared,
            "shared_cells_mean": statistics.mean(shared) if shared else None,
            "capacity_bytes": capacity,
            "capacity_bytes_mean": statistics.mean(capacity) if capacity else None,
            "allocated_bytes": allocated,
            "allocated_bytes_mean": statistics.mean(allocated) if allocated else None,
            "allocated_blocks": allocated_blocks,
            "allocated_blocks_mean": statistics.mean(allocated_blocks) if allocated_blocks else None,
            "recurrent_capacity_bytes": recurrent,
            "recurrent_capacity_bytes_mean": statistics.mean(recurrent) if recurrent else None,
            "rss_mb": [row.get("rss_mb") for row in valid],
            "gpu_mb": [row.get("gpu_mb") for row in valid],
            "hashes_match_within_arm": len({tuple(row.get("branch_hashes", [])) for row in valid}) <= 1,
        }
    if "control" in result and "fork" in result:
        c = result["control"]["used_cells_mean"]
        f = result["fork"]["used_cells_mean"]
        c_alloc = result["control"]["allocated_bytes_mean"]
        f_alloc = result["fork"]["allocated_bytes_mean"]
        result["comparison"] = {
            "used_cells_reduction_ratio": (c - f) / c if c else None,
            "allocated_bytes_reduction_ratio": (c_alloc - f_alloc) / c_alloc if c_alloc else None,
            "capacity_bytes_equal": result["control"]["capacity_bytes_mean"] == result["fork"]["capacity_bytes_mean"],
            "recurrent_capacity_bytes_equal": result["control"]["recurrent_capacity_bytes_mean"] == result["fork"]["recurrent_capacity_bytes_mean"],
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--fanout", type=int, default=4)
    parser.add_argument("--ctx", type=int, default=2048)
    parser.add_argument("--reverse", action="store_true", help="process target branches in reverse slot order")
    parser.add_argument("--allocator", choices=("contiguous", "paged"), default="contiguous")
    parser.add_argument("--block-size", choices=(8, 16, 32, 64), type=int, default=16)
    parser.add_argument("--root-tokens", type=int, default=240)
    args = parser.parse_args()
    if args.runs < 1 or args.fanout < 2 or args.fanout > len(SUFFIXES) or args.ctx < 256:
        parser.error("require runs>=1, 2<=fanout<=8, ctx>=256")
    os.makedirs(args.out_dir, exist_ok=True)
    os.environ["KV_ALLOCATOR"] = args.allocator
    os.environ["KV_BLOCK_SIZE"] = str(args.block_size)
    os.environ["BRANCH_ROOT_TOKENS"] = str(args.root_tokens)
    if args.allocator == "paged":
        os.environ["KV_NO_CACHE_RAM"] = "1"

    arms = {
        arm: [run_arm(args.out_dir, arm, args.fanout, args.ctx, i, args.reverse) for i in range(1, args.runs + 1)]
        for arm in ("control", "fork")
    }
    isolated = {
        run_index: [
            run_isolated_branch(args.out_dir, branch_index, args.ctx, run_index)
            for branch_index in range(args.fanout)
        ]
        for run_index in range(1, args.runs + 1)
    }
    report = {
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": os.path.basename(MODEL),
        "server": SERVER,
        "scenario": "conversation_branch_logical_cow_memory",
        "fanout": args.fanout,
        "ctx": args.ctx,
        "allocator": args.allocator,
        "block_size": args.block_size if args.allocator == "paged" else None,
        "arms": arms,
        "isolated_control": isolated,
        "aggregate": aggregate(arms),
    }
    report["all_pass"] = all(row.get("pass") for rows in arms.values() for row in rows)
    report["hashes_match"] = all(
        arms["fork"][run_index - 1].get("branch_hashes")
        == [row.get("hash") for row in isolated[run_index]]
        for run_index in range(1, args.runs + 1)
        if arms["fork"][run_index - 1].get("pass")
        and all(row.get("pass") for row in isolated[run_index])
    )
    path = os.path.join(args.out_dir, "report.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"wrote {path}")
    return 0 if report["all_pass"] and report["hashes_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
