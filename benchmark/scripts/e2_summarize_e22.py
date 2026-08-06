#!/usr/bin/env python3
"""E2.2：统一结果摘要生成。

- 复用 E2.1 正式 workload 结果（5 independent replicates，e21/）；
- 复用/新增 branch probe（e21 5 reps + e22 3 reps/smoke）；
- 派生 hybrid 校准字段（不改生产代码）：
    logical_prefix_reuse_tokens = OAI cached_tokens（= server 确认的 n_past 公共前缀）
    logical_prefix_reuse_ratio  = cached_tokens / prompt_tokens
    prompt_processed_tokens     = prompt_tokens - cached_tokens
    full_recompute              = cached_tokens == 0 的请求数
    attention_cached_tokens     = null（hybrid 无法分离 attention/recurrent，not_available）
- 输出 benchmark/results/e22/summary.json / summary.csv / manifest.json
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import statistics
from typing import Any, Dict, List


def norm_hash(text: str) -> str:
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", text.lower(), flags=re.UNICODE)
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]


def rows_of(run: Any) -> List[Dict[str, Any]]:
    if isinstance(run, dict) and "rows" in run:
        return run["rows"]
    return run if isinstance(run, list) else []


def analyze_bench_result(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    out: Dict[str, Any] = {"file": os.path.basename(path)}
    for wl in ("multi_turn", "long_life"):
        if wl not in d.get("scenarios", {}):
            continue
        sc = d["scenarios"][wl]
        runs = sc["runs"] if isinstance(sc, dict) and "runs" in sc else [sc]
        wl_out: Dict[str, Any] = {"runs": []}
        for i, run in enumerate(runs):
            rows = rows_of(run)
            tp = sum(r.get("prompt_tokens", 0) for r in rows)
            tc = sum(r.get("cached_tokens", 0) for r in rows)
            tpr = sum(r.get("prompt_tokens", 0) for r in rows) - tc
            full = sum(1 for r in rows if r.get("cached_tokens", 0) == 0)
            hashes = [norm_hash(str(r.get("text", ""))) for r in rows if r.get("text")]
            wl_out["runs"].append({
                "run_id": f"{wl}_{i}",
                "logical_prefix_reuse_tokens": tc,
                "logical_prefix_reuse_ratio": round(tc / tp, 4) if tp else None,
                "prompt_processed_tokens": tpr,
                "full_recompute": full,
                "attention_cached_tokens": None,  # hybrid not_available
                "prompt_tokens": tp,
                "cached_tokens": tc,
                "unique_response_hashes": len(set(hashes)),
            })
        sm = d["summary"].get(wl, {})
        out[wl] = {
            "runs": wl_out["runs"],
            "p50_latency_ms": sm.get("p50_latency_ms"),
            "p95_latency_ms": sm.get("p95_latency_ms"),
            "throughput_tps": sm.get("throughput_tps"),
            "task_success": sm.get("evaluation", {}).get("task_success"),
            "state_retention_rate": sm.get("evaluation", {}).get("state_retention_rate"),
            "truncations": sm.get("evaluation", {}).get("truncations"),
            "protocol": d.get("protocol", {}).get(wl, {}),
            "kv_runs": d.get("kv_observations", {}).get("runs", {}),
        }
    return out


def analyze_probe(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    reps = d.get("replicates", [])
    auto: Dict[str, List[Any]] = {"preserved": [], "overwritten": [], "reason": [],
                                  "active": [], "hash_first": [], "hash_revisit": []}
    for r in reps:
        a = r.get("auto", {})
        auto["preserved"].append(bool(a.get("branch_preserved")))
        auto["overwritten"].append(not bool(a.get("branch_preserved")))
        auto["reason"].append(a.get("revisit_reason"))
        auto["active"].append(a.get("active_sequences"))
        e = r.get("explicit", {})
        auto["hash_first"].append(e.get("hash_first"))
        auto["hash_revisit"].append(e.get("hash_revisit"))
    return {
        "policy": d.get("policy"),
        "replicates": len(reps),
        "preserved_count": sum(auto["preserved"]),
        "overwritten_count": sum(auto["overwritten"]),
        "revisit_reasons": auto["reason"],
        "revisit_active_sequences": auto["active"],
        "hash_first": auto["hash_first"],
        "hash_revisit": auto["hash_revisit"],
        "hash_mismatch": [i for i in range(len(auto["hash_first"]))
                          if auto["hash_first"][i] != auto["hash_revisit"][i]],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/e22")
    args = ap.parse_args()
    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    result: Dict[str, Any] = {"bench_workloads": {}, "branch_probes": {}}

    # 复用 E2.1 正式 workload（10 配置 × 5 reps）
    for tag in sorted(glob.glob("results/e21/*_p1/*.json")) + sorted(glob.glob("results/e21/*_p2/*.json")):
        rel = os.path.relpath(tag, "results/e21")
        cfg = os.path.dirname(rel)
        result["bench_workloads"][cfg] = analyze_bench_result(tag)

    # branch probes：E2.1 5 reps + E2.2 新增
    for pat in ("results/e21/branch_probe_cr0_*.json",
                "results/e22/branch_probe_*.json"):
        for f in sorted(glob.glob(pat)):
            key = os.path.basename(f).replace(".json", "")
            result["branch_probes"][key] = analyze_probe(f)

    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # CSV（branch probe 扁平化）
    rows: List[Dict[str, Any]] = []
    for key, p in result["branch_probes"].items():
        rows.append({"probe": key, **{k: v for k, v in p.items() if k != "revisit_reasons"}})
    if rows:
        import csv
        with open(os.path.join(out_dir, "summary.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(f"wrote {out_dir}/summary.json (bench={len(result['bench_workloads'])} configs, "
          f"probes={len(result['branch_probes'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
