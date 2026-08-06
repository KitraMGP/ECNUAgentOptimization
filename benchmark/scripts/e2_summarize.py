#!/usr/bin/env python3
"""E2.0.5：实验汇总工具 —— 从 results/e205/ 的原始 JSON 生成紧凑 summary。

- summary JSON：每配置 × 每 independent replicate 的关键原始指标
  （prompt_tokens / cached_tokens / cache_hit_rate / recompute / latency p50/p95 /
  used_cells first/peak / truncations / evaluation）；
- 每 replicate 的逐请求 text 规范化 hash（sha256），用于 B0/B2 输出等价性门禁；
- 输出 CSV 便于人工核对。

用法：uv run python scripts/e2_summarize.py results/e205 [-o out_dir]
"""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import os
import re
import statistics
from typing import Any, Dict, List


def normalize_text(text: str) -> str:
    """规范化：小写、压缩空白、去标点（输出等价性门禁用）。"""
    t = text.lower()
    t = re.sub(r"[\s\p{P}\p{S}]+", "", t, flags=re.UNICODE) if False else re.sub(r"\s+", "", t)
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t, flags=re.UNICODE)
    return t


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()[:16]


def _rows_of(run: Any) -> List[Dict[str, Any]]:
    if isinstance(run, dict) and "rows" in run:
        return run["rows"]
    return run if isinstance(run, list) else []


def analyze_result(path: str, cfg_label: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    wl_names = [k for k in d["scenarios"] if k in ("multi_turn", "long_life")]
    out: Dict[str, Any] = {"file": os.path.basename(path), "config": cfg_label,
                           "workloads": {}}
    # text hash（B0/B2 输出等价性门禁：逐请求规范化 hash 列表）
    hashes: Dict[str, List[str]] = {}
    for wl in wl_names:
        sc = d["scenarios"][wl]
        runs = sc["runs"] if isinstance(sc, dict) and "runs" in sc else [sc]
        wl_rows = []
        for run in runs:
            rows = _rows_of(run)
            for r in rows:
                if isinstance(r, dict) and r.get("text"):
                    wl_rows.append(text_hash(str(r["text"])))
        hashes[wl] = wl_rows
        sm = d["summary"].get(wl, {})
        kv = (d.get("kv_observations", {}).get("runs", {}) or {})
        # protocol 记录
        proto = (d.get("protocol", {}).get(wl, {}) or {})
        reps = proto.get("replicates", [])
        rep_records = []
        for i, rec in enumerate(reps):
            rid = rec.get("run_id", f"{wl}_{i}")
            agg = kv.get(rid, {})
            if i < len(runs):
                rows = _rows_of(runs[i])
            else:
                rows = []
            total_p = sum(r.get("prompt_tokens", 0) for r in rows)
            total_c = sum(r.get("cached_tokens", 0) for r in rows)
            ev = None
            rep_records.append({
                "run_id": rid,
                "valid": rec.get("valid"),
                "clean_verified": rec.get("clean_verified"),
                "initial_used_cells": rec.get("initial_used_cells"),
                "first_used_cells": agg.get("first_used_cells"),
                "peak_used_cells": agg.get("peak_used_cells"),
                "last_used_cells": agg.get("last_used_cells"),
                "prompt_tokens": total_p,
                "cached_tokens": total_c,
                "cache_hit_rate": round(total_c / total_p, 4) if total_p else None,
                "recompute_tokens": total_p - total_c,
            })
        out["workloads"][wl] = {
            "replicates": rep_records,
            "summary": sm,
            "protocol": proto,
        }
    out["text_hashes"] = hashes
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.results_dir, "*", "*.json")))
    if not files:
        files = sorted(glob.glob(os.path.join(args.results_dir, "*.json")))
    if not files:
        print(f"no results in {args.results_dir}")
        return 1
    out_dir = args.out or args.results_dir
    os.makedirs(out_dir, exist_ok=True)
    all_rows: List[Dict[str, Any]] = []
    for path in files:
        rel = os.path.relpath(path, args.results_dir)
        cfg_label = os.path.dirname(rel) or "root"
        a = analyze_result(path, cfg_label)
        for wl, wd in a["workloads"].items():
            for r in wd["replicates"]:
                all_rows.append({"cfg": cfg_label, "workload": wl, **r})
        # text hash 追加写入（每个结果文件一个 hash JSON）
        with open(os.path.join(out_dir, f"text_hashes_{os.path.basename(path)}"),
                  "w", encoding="utf-8") as f:
            json.dump(a["text_hashes"], f, ensure_ascii=False, indent=2)

    summary_path = os.path.join(out_dir, "e205_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=2)
    csv_path = os.path.join(out_dir, "e205_summary.csv")
    if all_rows:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)
    print(f"wrote {summary_path} ({len(all_rows)} rows) and {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
