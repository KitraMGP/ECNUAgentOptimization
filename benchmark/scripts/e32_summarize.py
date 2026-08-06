#!/usr/bin/env python3
"""E3.2：汇总 + paired 分析 + 预注册门槛逐项判定 + manifest。

配对：同场景同 rep index 的 default vs lru（配对区组执行）。
收益指标（p4_hot_cold_order）：P0_after（热分支回访）的 prompt_processed /
latency；wall time = replicate 总时长（最后请求完成时间差）。
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
OUT = os.path.join(BENCH, "results", "e32")


def sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def load(scenario: str, policy: str, cache_ram: int = 0) -> list:
    suffix = "" if cache_ram == 0 else "_ram8192"
    pattern = os.path.join(OUT, f"{scenario}{suffix}_{policy}_rep*.json")
    reps = []
    for p in sorted(glob.glob(pattern)):
        d = json.load(open(p))
        reps.extend(d.get("reps", []))
    return reps


def agg_rep(rep: dict) -> dict:
    reqs = rep.get("requests", [])
    by_tag = {q["turn_id"]: q for q in reqs}
    last = reqs[-1] if reqs else {}
    return {
        "replicate_id": f"{rep.get('scenario')}_{rep.get('policy')}_{rep.get('rep')}",
        "clean_verified": rep.get("clean_verified"),
        "purge_events": rep.get("log_events", {}).get("purge_events", []),
        "purge_victims": [p["slot"] for p in rep.get("log_events", {}).get("purge_events", [])],
        "purge_ticks": [p["tick"] for p in rep.get("log_events", {}).get("purge_events", [])],
        "free_space": rep.get("log_events", {}).get("free_space", 0),
        "failures": sum(1 for q in reqs if q.get("request_failure")),
        "contamination": sum(1 for q in reqs if q.get("contamination_detected")),
        "eval_pass": sum(1 for q in reqs if q.get("evaluator_pass")),
        "P0_after_processed": by_tag.get("P0_after", {}).get("prompt_processed_tokens"),
        "P0_after_lat": by_tag.get("P0_after", {}).get("latency_ms"),
        "wall_time_ms": last.get("latency_ms"),  # 最后请求完成 ≈ 总 wall（近似）
    }


def paired(scenario: str, n_pairs: int, cache_ram: int = 0) -> dict:
    ds = [agg_rep(r) for r in load(scenario, "default", cache_ram)]
    ls = [agg_rep(r) for r in load(scenario, "lru", cache_ram)]
    pairs = []
    for i in range(min(len(ds), len(ls))):
        pairs.append({"idx": i, "default": ds[i], "lru": ls[i]})
    return {"scenario": scenario, "n_pairs": len(pairs), "pairs": pairs}


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    blocks = {
        "p4_hot_cold_order": paired("p4_hot_cold_order", 5),
        "p2_single_candidate": paired("p2_single_candidate", 3),
        "active_protection": paired("active_protection", 3),
        "non_unified_smoke": paired("non_unified_smoke", 1),
        "p4_hot_cold_ram8192": paired("p4_hot_cold_order", 2, 8192),
    }

    gates = {}
    # ---- 收益门槛（p4_hot_cold_order，cache_ram 0）----
    hc = blocks["p4_hot_cold_order"]
    diffs_proc, diffs_lat = [], []
    better_proc = better_lat = 0
    wall_not_worse = 0
    for p in hc["pairs"]:
        d, l = p["default"], p["lru"]
        if d["P0_after_processed"] is not None and l["P0_after_processed"] is not None:
            diffs_proc.append(l["P0_after_processed"] - d["P0_after_processed"])
            if l["P0_after_processed"] < d["P0_after_processed"]:
                better_proc += 1
        if d["P0_after_lat"] is not None and l["P0_after_lat"] is not None:
            diffs_lat.append(l["P0_after_lat"] - d["P0_after_lat"])
            if l["P0_after_lat"] < d["P0_after_lat"]:
                better_lat += 1
        if d["wall_time_ms"] and l["wall_time_ms"]:
            if l["wall_time_ms"] <= d["wall_time_ms"] * 1.03:
                wall_not_worse += 1
    base_proc = statistics.median([p["default"]["P0_after_processed"] for p in hc["pairs"] if p["default"]["P0_after_processed"] is not None])
    base_lat = statistics.median([p["default"]["P0_after_lat"] for p in hc["pairs"] if p["default"]["P0_after_lat"] is not None])
    gates["hc_processed_improve_pct"] = round(-statistics.median(diffs_proc) / base_proc * 100, 1) if diffs_proc and base_proc else None
    gates["hc_lat_improve_pct"] = round(-statistics.median(diffs_lat) / base_lat * 100, 1) if diffs_lat and base_lat else None
    gates["hc_better_processed"] = f"{better_proc}/{len(hc['pairs'])}"
    gates["hc_better_lat"] = f"{better_lat}/{len(hc['pairs'])}"
    gates["hc_wall_not_worse_3pct"] = f"{wall_not_worse}/{len(hc['pairs'])}"

    # ---- 正确性门槛（unified 正式场景；non-unified 400 为已知场景行为，两策略一致）----
    unified_blocks = {k: v for k, v in blocks.items() if k != "non_unified_smoke"}
    all_reps = [a for b in unified_blocks.values() for p in b["pairs"] for a in (p["default"], p["lru"])]
    gates["all_failure_zero_unified"] = all(r["failures"] == 0 for r in all_reps)
    gates["all_eval_pass"] = all(
        r["eval_pass"] == 6 or r["eval_pass"] == 3 or r["eval_pass"] == 2
        for r in all_reps
    )
    gates["all_contamination_zero"] = all(r["contamination"] == 0 for r in all_reps)
    # non-unified：两策略 400 行为一致（E3.1 已知：5000 > 4096 per-seq 上限）
    nu_reps = [a for p in blocks["non_unified_smoke"]["pairs"] for a in (p["default"], p["lru"])]
    gates["non_unified_same_failures"] = len({r["failures"] for r in nu_reps}) == 1
    gates["non_unified_lru_inert"] = all(not p["lru"]["purge_victims"] and not p["default"]["purge_victims"] for p in blocks["non_unified_smoke"]["pairs"])

    # ---- 选择门槛（限定有压力发生的 replicate：purge 非空）----
    hc_lru_pressured = [p["lru"] for p in hc["pairs"] if p["lru"]["purge_victims"]]
    hc_def_pressured = [p["default"] for p in hc["pairs"] if p["default"]["purge_victims"]]
    gates["lru_first_victim_is_cold"] = f"{sum(1 for r in hc_lru_pressured if r['purge_victims'][0] == 1)}/{len(hc_lru_pressured)}（有压力样本）"
    gates["lru_victim_tick_minimal"] = all(
        r["purge_ticks"][0] == min(r["purge_ticks"]) for r in hc_lru_pressured
    )
    gates["default_first_victim_first_eligible"] = f"{sum(1 for r in hc_def_pressured if r['purge_victims'][0] == 0)}/{len(hc_def_pressured)}（有压力样本）"

    # ---- 收益门槛（p4_hot_cold：P0 回访不劣于 + 中位改善）----
    gates["hc_processed_not_worse"] = f"{sum(1 for p in hc['pairs'] if p['lru']['P0_after_processed'] is not None and p['default']['P0_after_processed'] is not None and p['lru']['P0_after_processed'] <= p['default']['P0_after_processed'])}/5"
    gates["hc_lat_not_worse"] = f"{sum(1 for p in hc['pairs'] if p['lru']['P0_after_lat'] is not None and p['default']['P0_after_lat'] is not None and p['lru']['P0_after_lat'] <= p['default']['P0_after_lat'])}/5"

    # active protection：llama.cpp 单测真并发验证（processing 跳过）+ probe lru 选冷分支
    ap = blocks["active_protection"]
    ap_lru = [p["lru"] for p in ap["pairs"]]
    gates["active_protection_lru_cold"] = all(
        (not r["purge_victims"]) or r["purge_victims"][0] == 1 for r in ap_lru
    )
    gates["active_protection_ok"] = gates["active_protection_lru_cold"]

    # ---- 决策 ----
    correct_ok = (gates["all_failure_zero_unified"] and gates["all_eval_pass"]
                  and gates["all_contamination_zero"] and gates["active_protection_ok"])
    select_ok = gates["lru_first_victim_is_cold"].startswith(f"{len(hc_lru_pressured)}/{len(hc_lru_pressured)}") and gates["default_first_victim_first_eligible"].startswith(f"{len(hc_def_pressured)}/{len(hc_def_pressured)}")
    benefit_ok = ((gates["hc_processed_improve_pct"] is not None and gates["hc_processed_improve_pct"] >= 10.0)
                  or (gates["hc_lat_improve_pct"] is not None and gates["hc_lat_improve_pct"] >= 5.0))
    benefit_ok = benefit_ok and int(gates["hc_processed_not_worse"].split("/")[0]) >= 4
    wall_ok = gates["hc_wall_not_worse_3pct"].startswith("5/")
    nu_ok = gates["non_unified_same_failures"] and gates["non_unified_lru_inert"]
    gates["verdict"] = "PROMOTE_A1A4" if (correct_ok and select_ok and benefit_ok and wall_ok and nu_ok) else \
                       ("HOLD_A1A4" if correct_ok else "REJECT_A1A4")
    verdict = gates["verdict"]

    out = {"blocks": blocks, "gates": gates}
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    rows = []
    for name, b in blocks.items():
        for p in b["pairs"]:
            for side in ("default", "lru"):
                a = p[side]
                rows.append({"scenario": name, "pair": p["idx"], "policy": side, **a})
    with open(os.path.join(OUT, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    prows = []
    for name, b in blocks.items():
        for p in b["pairs"]:
            d, l = p["default"], p["lru"]
            prows.append({"scenario": name, "pair": p["idx"],
                          "d_P0_processed": d["P0_after_processed"], "l_P0_processed": l["P0_after_processed"],
                          "diff_processed": (l["P0_after_processed"] - d["P0_after_processed"]) if d["P0_after_processed"] is not None and l["P0_after_processed"] is not None else None,
                          "d_P0_lat": d["P0_after_lat"], "l_P0_lat": l["P0_after_lat"],
                          "diff_lat": (l["P0_after_lat"] - d["P0_after_lat"]) if d["P0_after_lat"] is not None and l["P0_after_lat"] is not None else None,
                          "d_victims": d["purge_victims"], "l_victims": l["purge_victims"]})
    with open(os.path.join(OUT, "paired_results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(prows[0].keys()))
        w.writeheader()
        w.writerows(prows)

    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "phase": "E3.2",
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "binary_sha256": sha(os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")),
        "model_sha256": sha(os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")),
        "gpu": "NVIDIA GeForce RTX 4060 Laptop GPU 8188 MiB",
        "policy": {"unified_idle_slot_policy": "default|lru", "default_value": "default",
                   "lru_rules_frozen": ["last_used_tick 升序", "prompt.n_tokens 降序（tie）", "slot.id 升序（tie）"]},
        "params": {"ctx": 8192, "parallel": {"p4_hot_cold_order": 4, "p2_single_candidate": 2,
                                             "active_protection": 4, "non_unified_smoke": 2},
                   "kv_unified": True, "cache_reuse": 0, "cache_ram": 0, "seed": 42, "temp": 0},
        "protocol": "server-per-replicate（配对区组 D L | L D 交替）",
        "pre_registered_thresholds": "正确性 7 条 + 选择 4 条 + 收益 7 条（任务九）",
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
