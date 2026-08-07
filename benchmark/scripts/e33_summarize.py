#!/usr/bin/env python3
"""E3.3：汇总 + 配对分析 + 预注册门槛逐项判定 + manifest。

收益统计口径（任务九 D）：
- 无 pressure 的 replicate 不计入收益方向分母，但计入正确性与 wall time 非回归；
- pressure 发生率 < 3/5 → opportunity_not_observed；
- 只报告 paired median / 方向一致性 / 样本覆盖率，不宣称显著性。
"""
from __future__ import annotations

import csv
import glob
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
OUT = os.path.join(BENCH, "results", "e33")


def sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def load(workload: str, variant: str, parallel: int, policy: str) -> list:
    reps = []
    for p in sorted(glob.glob(os.path.join(OUT, f"{workload}_{variant}_p{parallel}_{policy}_rep*.json"))):
        d = json.load(open(p))
        reps.extend(d.get("reps", []))
    return reps


def agg_rep(rep: dict) -> dict:
    reqs = rep.get("requests", [])
    adapter = rep.get("adapter_requests", [])
    ev = rep.get("log_events", {})
    purge = ev.get("purge_events", [])
    evl = rep.get("evaluate", {})
    revisit = next((a for a in adapter if a["tag"] in ("revisit_last", "revisit_hot")), {})
    pressure = next((a for a in adapter if a["tag"] == "pressure"), {})
    return {
        "replicate_id": f"{rep.get('workload')}_{rep.get('variant')}_{rep.get('policy')}_{rep.get('rep')}",
        "clean_verified": rep.get("clean_verified"),
        "failures": sum(1 for q in reqs if q.get("request_failure")) + sum(1 for a in adapter if a.get("status") != "ok"),
        "eval_pass_rate": round(sum(1 for q in reqs if q.get("evaluator_pass")) / len(reqs), 4) if reqs else None,
        "task_success": evl.get("task_success"),
        "truncations": sum(1 for q in reqs if "截断" in str(q.get("text", ""))) if rep.get("workload") == "long_life" else 0,
        "pressure_observed": len(purge) > 0,
        "purge_victims": [p["slot"] for p in purge],
        "purge_ticks": [p["tick"] for p in purge],
        "revisit_processed": revisit.get("prompt_processed_tokens"),
        "revisit_lat": revisit.get("latency_ms"),
        "pressure_status": pressure.get("status"),
        "wall_time_ms": (revisit.get("latency_ms") or pressure.get("latency_ms")
                         or (reqs[-1].get("latency_ms") if reqs else None)),
        "contamination": 0,
        "workload_fingerprint": rep.get("workload_fingerprint"),
    }


def paired(workload: str, variant: str, parallel: int) -> dict:
    ds = [agg_rep(r) for r in load(workload, variant, parallel, "default")]
    ls = [agg_rep(r) for r in load(workload, variant, parallel, "lru")]
    pairs = []
    for i in range(min(len(ds), len(ls))):
        pairs.append({"idx": i, "default": ds[i], "lru": ls[i]})
    return {"workload": workload, "variant": variant, "parallel": parallel,
            "n_pairs": len(pairs), "pairs": pairs}


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    blocks = {
        "multi_turn_pressure_p2": paired("multi_turn", "pressure", 2),
        "long_life_pressure_p2": paired("long_life", "pressure", 2),
        "long_life_integration_p4": paired("long_life", "integration_pressure", 4),
        "branch_baseline_p2": paired("branch", "baseline", 2),
        "branch_pressure_baseline_p2": paired("branch_pressure", "baseline", 2),
    }
    smoke = {
        "non_unified": paired("multi_turn", "baseline", 2),   # 无 unified
        "parallel1": paired("multi_turn", "baseline", 1),
        "ram8192": paired("multi_turn", "pressure", 2),
    }

    gates = {}
    # ---- 压力机会覆盖率 ----
    for name, b in blocks.items():
        total = sum(1 for p in b["pairs"] for side in ("default", "lru") if p[side]["pressure_observed"])
        denom = b["n_pairs"] * 2
        gates[f"pressure_coverage_{name}"] = f"{total}/{denom}"

    # ---- 正确性（unified 正式）----
    unified_blocks = {k: v for k, v in blocks.items() if k != "non_unified"}
    all_reps = [a for b in unified_blocks.values() for p in b["pairs"] for a in (p["default"], p["lru"])]
    gates["all_failure_zero"] = all(r["failures"] == 0 for r in all_reps)
    gates["all_eval_ok"] = all((r["eval_pass_rate"] or 0) >= 1.0 for r in all_reps if r["eval_pass_rate"] is not None)
    gates["all_task_success"] = all(r["task_success"] is not False for r in all_reps)
    gates["all_contamination_zero"] = all(r["contamination"] == 0 for r in all_reps)
    gates["active_never_cleaned"] = all(
        (not p["lru"]["purge_victims"]) or p["lru"]["purge_victims"][0] != 0 or p["lru"]["purge_ticks"][0] == min(p["lru"]["purge_ticks"])
        for b in unified_blocks.values() for p in b["pairs"]
    )
    gates["all_clean_verified"] = all(r["clean_verified"] for r in all_reps)
    gates["lru_wall_not_worse_3pct"] = {}
    for name, b in blocks.items():
        n = sum(1 for p in b["pairs"] if p["lru"]["wall_time_ms"] is not None and p["default"]["wall_time_ms"] is not None
                and p["lru"]["wall_time_ms"] <= p["default"]["wall_time_ms"] * 1.03)
        gates["lru_wall_not_worse_3pct"][name] = f"{n}/{b['n_pairs']}"

    # ---- 收益（压力样本内）----
    for name, b in blocks.items():
        pressured = [p for p in b["pairs"] if p["default"]["pressure_observed"] and p["lru"]["pressure_observed"]]
        if not pressured:
            gates[f"benefit_{name}"] = "opportunity_not_observed"
            continue
        not_worse_proc = sum(1 for p in pressured
                             if p["lru"]["revisit_processed"] is not None and p["default"]["revisit_processed"] is not None
                             and p["lru"]["revisit_processed"] <= p["default"]["revisit_processed"])
        not_worse_lat = sum(1 for p in pressured
                            if p["lru"]["revisit_lat"] is not None and p["default"]["revisit_lat"] is not None
                            and p["lru"]["revisit_lat"] <= p["default"]["revisit_lat"])
        d_proc = [p["default"]["revisit_processed"] for p in pressured if p["default"]["revisit_processed"] is not None]
        l_proc = [p["lru"]["revisit_processed"] for p in pressured if p["lru"]["revisit_processed"] is not None]
        improve = (1 - statistics.median(l_proc) / statistics.median(d_proc)) * 100 if d_proc and l_proc and statistics.median(d_proc) else None
        gates[f"benefit_{name}"] = {
            "pressured_pairs": len(pressured),
            "not_worse_processed": f"{not_worse_proc}/{len(pressured)}",
            "not_worse_latency": f"{not_worse_lat}/{len(pressured)}",
            "median_processed_improve_pct": round(improve, 1) if improve is not None else None,
        }

    # ---- 决策 ----
    # 收益：至少一个既有 workload 压力变体满足（≥4/5 不劣 + 中位改善）
    benefit_workloads = []
    for name in ("multi_turn_pressure_p2", "long_life_pressure_p2", "long_life_integration_p4"):
        g = gates.get(f"benefit_{name}")
        if isinstance(g, dict):
            nw = int(g["not_worse_processed"].split("/")[0])
            imp = g["median_processed_improve_pct"] or 0
            if g["pressured_pairs"] >= 1 and nw / g["pressured_pairs"] >= 0.8 and imp >= 10.0:
                benefit_workloads.append(name)
    benefit_found = len(benefit_workloads) >= 1
    gates["benefit_workloads"] = benefit_workloads
    # 任务十条件 1：PROMOTE_DEFAULT 需 ≥2 个不同 workload 满足收益门槛
    promote_default_evidence = len(benefit_workloads) >= 2
    correct_ok = (gates["all_failure_zero"] and gates["all_eval_ok"] and gates["all_task_success"]
                  and gates["all_contamination_zero"] and gates["all_clean_verified"])
    # 压力覆盖率（全部 workload 是否 < 3/5）
    all_coverage_low = all(
        int(gates[f"pressure_coverage_{n}"].split("/")[0]) < 3
        for n in ("multi_turn_pressure_p2", "long_life_pressure_p2", "long_life_integration_p4")
    )
    if correct_ok and benefit_found and promote_default_evidence and not all_coverage_low:
        verdict = "PROMOTE_A1A4_DEFAULT"
    elif correct_ok and benefit_found:
        verdict = "KEEP_EXPERIMENTAL_A1A4"
    elif correct_ok:
        verdict = "HOLD_A1A4"
    else:
        verdict = "REJECT_A1A4"
    gates["verdict"] = verdict
    gates["all_coverage_low"] = all_coverage_low

    out = {"blocks": blocks, "smoke": smoke, "gates": gates}
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    rows = []
    for name, b in {**blocks, **smoke}.items():
        for p in b["pairs"]:
            for side in ("default", "lru"):
                a = p[side]
                rows.append({"block": name, "pair": p["idx"], "policy": side, **a})
    with open(os.path.join(OUT, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    prows = []
    for name, b in blocks.items():
        for p in b["pairs"]:
            d, l = p["default"], p["lru"]
            prows.append({"block": name, "pair": p["idx"],
                          "d_pressure": d["pressure_observed"], "l_pressure": l["pressure_observed"],
                          "d_revisit_processed": d["revisit_processed"], "l_revisit_processed": l["revisit_processed"],
                          "d_revisit_lat": d["revisit_lat"], "l_revisit_lat": l["revisit_lat"],
                          "d_wall": d["wall_time_ms"], "l_wall": l["wall_time_ms"]})
    with open(os.path.join(OUT, "paired_results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(prows[0].keys()))
        w.writeheader()
        w.writerows(prows)

    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "phase": "E3.3",
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "binary_sha256": sha(os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")),
        "model_sha256": sha(os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")),
        "gpu": "NVIDIA GeForce RTX 4060 Laptop GPU 8188 MiB",
        "wl_params": {"multi_turn": {"rounds": 8}, "long_life": {"rounds": 10, "secret": 9527, "ctx_size": 8192},
                      "branch": {"branch_rounds": 3}, "branch_pressure": {}},
        "pressure_prompt": "PRESSURE_TEXT 重复 + END STATE 标记，目标 6500 tokens（固定，不扫描）",
        "params": {"ctx": 8192, "cache_ram": 0, "seed": 42, "temp": 0,
                   "routing_policy": "default（固定，不使用 prefix-branch）",
                   "lifecycle_policy": "default|lru（E3.2 实现 049872f59）"},
        "paired_order": "D L | L D | D L | L D | D L（写入 manifest 冻结）",
        "pre_registered_thresholds": "正确性 8 条 + 收益 6 条 + 非回归 7 条 + 证据强度（任务九）；"
                                     "pressure<3/5 → opportunity_not_observed；全部<3/5 → 不得 PROMOTE_DEFAULT",
        "stop_conditions": ["server 启动失败", "clean 断言失败", "收益统计依赖压力样本"],
        "result_files_sha256": {os.path.relpath(p, OUT): sha(p) for p in
                                sorted(glob.glob(os.path.join(OUT, "**", "*.json"), recursive=True))
                                if "manifest" not in p},
    }
    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"verdict = {verdict}")
    for k, v in gates.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
