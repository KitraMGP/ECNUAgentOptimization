#!/usr/bin/env python3
"""E6：从 raw/ 原始 JSON 汇总实验数据（12.19 E6.1 条件：原始 JSON 能被汇总脚本读取）。"""
from __future__ import annotations

import glob
import json
import os
import statistics
from collections import defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RAW = os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw")


def summarize() -> dict:
    files = sorted(glob.glob(os.path.join(RAW, "*.json")))
    files = [f for f in files if not f.endswith("summary.json")]
    by_workload: dict = defaultdict(list)
    for f in files:
        try:
            rec = json.load(open(f))
        except Exception as e:
            print(f"  READ_FAIL {os.path.basename(f)}: {e}")
            continue
        by_workload[rec["workload_id"]].append(rec)

    out = {"raw_file_count": len(files), "workloads": {}}
    for wid, recs in sorted(by_workload.items()):
        entry = {"files": len(recs), "variants": {}}
        for rec in recs:
            v = rec["variant"]
            e = entry["variants"].setdefault(v, {"reps": [], "completed": [], "failed": [],
                                                  "rejected": [], "recompute": [],
                                                  "prefix_hit": [], "used_cells": [],
                                                  "purge": [], "latency": []})
            e["reps"].append(rec["repetition"])
            e["completed"].append(rec["completed_sessions"])
            e["failed"].append(rec["failed_sessions"])
            e["rejected"].append(rec["rejected_sessions"])
            e["recompute"].append(rec["recompute_tokens"])
            e["prefix_hit"].append(rec["prefix_hit_tokens"])
            kv = rec.get("kv_stats_snapshot") or {}
            e["used_cells"].append(kv.get("used_cells"))
            e["purge"].append(rec["purge_count"])
            e["latency"].append(rec["request_latency_ms"])
        for v, e in entry["variants"].items():
            for k in ("completed", "failed", "rejected", "recompute", "prefix_hit", "used_cells", "purge", "latency"):
                vals = [x for x in e[k] if x is not None]
                if vals:
                    e[k + "_median"] = round(statistics.median(vals), 2)
                    e[k + "_min"] = min(vals)
                    e[k + "_max"] = max(vals)
        out["workloads"][wid] = entry
    summary_path = os.path.join(RAW, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return out


if __name__ == "__main__":
    s = summarize()
    print(f"raw files: {s['raw_file_count']}")
    for wid, entry in s["workloads"].items():
        for v, e in entry["variants"].items():
            print(f"  {wid} [{v}] completed_median={e.get('completed_median')} "
                  f"failed={e.get('failed_median')} recompute_median={e.get('recompute_median')} "
                  f"prefix_hit_median={e.get('prefix_hit_median')} used_cells_median={e.get('used_cells_median')}")
