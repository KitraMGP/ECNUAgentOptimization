#!/usr/bin/env python3
"""E2.5：汇总分析 —— 每 replicate 聚合 + paired 对比 + 预注册门槛判定。

- 独立样本 = replicate（server-per-replicate）；
- 每 replicate 聚合：revisit 延迟 p50/p95、wall time、prompt_processed 总量、
  logical_prefix_reuse 总量、evaluator pass rate、failure、contamination；
- paired（按配对区组序号）default vs prefix-branch；
- 输出 summary.json / summary.csv / paired_results.csv。
"""
from __future__ import annotations

import csv
import glob
import json
import os
import statistics
import sys

BENCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
OUT = os.path.join(BENCH, "results", "e25")


def load_reps(ram: str, policy: str, sub: str = "") -> list:
    # 正式矩阵优先读子目录（off_full / pressure_full）
    pat = os.path.join(OUT, f"{ram}_full", f"{ram}_{policy}.json")
    if not os.path.exists(pat):
        pat = os.path.join(OUT, f"{ram}_{policy}.json")
    if not os.path.exists(pat):
        return []
    d = json.load(open(pat))
    return d.get("reps", [])


def agg_replicate(r: dict) -> dict:
    reqs = r.get("requests", [])
    revisits = [q for q in reqs if "revisit" in q["turn_id"]]
    lat = [q["latency_total_ms"] for q in revisits]
    return {
        "replicate_id": r.get("replicate_id"),
        "server_pid": r.get("server_pid"),
        "clean_verified": r.get("clean_verified"),
        "fresh_process_verified": r.get("fresh_process_verified"),
        "failures": r.get("failures"),
        "wall_time_s": r.get("wall_time_s"),
        "revisit_p50_ms": round(statistics.median(lat), 1) if lat else None,
        "revisit_p95_ms": round(sorted(lat)[int(0.95 * len(lat)) - 1], 1) if lat else None,
        "revisit_latencies": lat,
        "prompt_processed_total": sum(q["prompt_processed_tokens"] or 0 for q in reqs),
        "prefix_reuse_total": sum(q["logical_prefix_reuse_tokens"] or 0 for q in reqs),
        "full_recompute": sum(1 for q in reqs if q.get("full_recompute")),
        "eval_pass_rate": round(sum(1 for q in reqs if q["evaluator_pass"]) / len(reqs), 4) if reqs else None,
        "contamination": sum(1 for q in reqs if q.get("contamination_detected")),
        "ram_restore_suspected": sum(1 for q in reqs if q.get("ram_restore_suspected")),
    }


def paired_block(ram: str, n_pairs: int, sub: str = "") -> dict:
    """配对区组顺序：rep0=D, rep1=PB, rep2=PB, rep3=D ... 按 replicate index 配对。"""
    ds = load_reps(ram, "default", sub)
    ps = load_reps(ram, "prefix-branch", sub)
    # 配对：按 replicate_id 的序号（_0.._9）配对
    dd = {r["replicate_id"].rsplit("_", 1)[-1]: agg_replicate(r) for r in ds}
    pp = {r["replicate_id"].rsplit("_", 1)[-1]: agg_replicate(r) for r in ps}
    pairs = []
    for k in sorted(set(dd) & set(pp)):
        pairs.append({"idx": int(k), "default": dd[k], "prefix_branch": pp[k]})
    pairs.sort(key=lambda p: p["idx"])
    return {"ram": ram, "n_pairs": len(pairs), "pairs": pairs}


def main() -> int:
    blocks = {
        "off": paired_block("off", 5),
        "pressure": paired_block("pressure", 5),
    }
    # RAM default smoke（根目录，各 1-2 个）
    smoke = {"default": {"default": [agg_replicate(r) for r in load_reps("default", "default")],
                          "prefix_branch": [agg_replicate(r) for r in load_reps("default", "prefix-branch")]}}
    unified = None
    up = os.path.join(OUT, "unified_pressure_prefix-branch.json")
    if os.path.exists(up):
        unified = [agg_replicate(r) for r in json.load(open(up)).get("reps", [])]

    # 门槛判定
    gates = {}
    for ram, blk in blocks.items():
        diffs_p50, diffs_wall, diffs_pp = [], [], []
        wall_not_worse = 0
        p50_lower = 0
        for p in blk["pairs"]:
            d, pb = p["default"], p["prefix_branch"]
            if d["revisit_p50_ms"] and pb["revisit_p50_ms"]:
                diffs_p50.append(pb["revisit_p50_ms"] - d["revisit_p50_ms"])
                if pb["revisit_p50_ms"] < d["revisit_p50_ms"]:
                    p50_lower += 1
            if d["wall_time_s"] and pb["wall_time_s"]:
                diffs_wall.append(pb["wall_time_s"] - d["wall_time_s"])
                if pb["wall_time_s"] <= d["wall_time_s"]:
                    wall_not_worse += 1
            diffs_pp.append(pb["prompt_processed_total"] - d["prompt_processed_total"])
        n = len(blk["pairs"])
        med_p50 = statistics.median(diffs_p50) if diffs_p50 else None
        med_wall = statistics.median(diffs_wall) if diffs_wall else None
        med_pp = statistics.median(diffs_pp) if diffs_pp else None
        base_p50 = statistics.median([p["default"]["revisit_p50_ms"] for p in blk["pairs"] if p["default"]["revisit_p50_ms"]])
        base_wall = statistics.median([p["default"]["wall_time_s"] for p in blk["pairs"] if p["default"]["wall_time_s"]])
        base_pp = statistics.median([p["default"]["prompt_processed_total"] for p in blk["pairs"]])
        gates[ram] = {
            "n_pairs": n,
            "wall_not_worse": f"{wall_not_worse}/{n}",
            "revisit_p50_lower": f"{p50_lower}/{n}",
            "med_revisit_p50_diff_ms": round(med_p50, 1) if med_p50 is not None else None,
            "med_revisit_p50_improve_pct": round(-med_p50 / base_p50 * 100, 1) if med_p50 is not None and base_p50 else None,
            "med_wall_diff_s": round(med_wall, 1) if med_wall is not None else None,
            "med_wall_improve_pct": round(-med_wall / base_wall * 100, 1) if med_wall is not None and base_wall else None,
            "med_prompt_processed_diff": round(med_pp, 1) if med_pp is not None else None,
            "med_prompt_processed_reduce_pct": round(-med_pp / base_pp * 100, 1) if med_pp is not None and base_pp else None,
        }
        gates[ram]["gate1_wall_not_worse_5of5"] = wall_not_worse == n and n >= 5
        gates[ram]["gate2_p50_lower_4of5"] = p50_lower >= 4
        gates[ram]["gate3_p50_improve_5pct"] = (gates[ram]["med_revisit_p50_improve_pct"] or 0) >= 5.0
        gates[ram]["gate4_wall3pct_or_pp10pct"] = (gates[ram]["med_wall_improve_pct"] or 0) >= 3.0 or (gates[ram]["med_prompt_processed_reduce_pct"] or 0) >= 10.0
        all_fail_zero = all(p["default"]["failures"] == 0 and p["prefix_branch"]["failures"] == 0 for p in blk["pairs"])
        all_eval_ok = all(p["default"]["eval_pass_rate"] == 1.0 and p["prefix_branch"]["eval_pass_rate"] == 1.0 for p in blk["pairs"])
        all_contam_zero = all(p["default"]["contamination"] == 0 and p["prefix_branch"]["contamination"] == 0 for p in blk["pairs"])
        gates[ram]["gate5_no_failure"] = all_fail_zero
        gates[ram]["gate6_eval_no_drop"] = all_eval_ok
        gates[ram]["gate7_no_contamination"] = all_contam_zero

    out = {"blocks": blocks, "smoke": smoke, "unified": unified, "gates": gates}
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # summary.csv（每 replicate 一行）
    rows = []
    for ram, blk in blocks.items():
        for p in blk["pairs"]:
            for side in ("default", "prefix_branch"):
                a = p[side]
                rows.append({"ram": ram, "pair": p["idx"], "policy": side,
                             **{k: a[k] for k in ("replicate_id", "server_pid", "clean_verified",
                                                   "fresh_process_verified", "failures", "wall_time_s",
                                                   "revisit_p50_ms", "revisit_p95_ms", "prompt_processed_total",
                                                   "prefix_reuse_total", "full_recompute", "eval_pass_rate",
                                                   "contamination", "ram_restore_suspected")}})
    with open(os.path.join(OUT, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

    # paired_results.csv（每对一行，diff）
    prows = []
    for ram, blk in blocks.items():
        for p in blk["pairs"]:
            d, pb = p["default"], p["prefix_branch"]
            prows.append({"ram": ram, "pair": p["idx"],
                          "d_p50": d["revisit_p50_ms"], "pb_p50": pb["revisit_p50_ms"],
                          "diff_p50": round(pb["revisit_p50_ms"] - d["revisit_p50_ms"], 1) if d["revisit_p50_ms"] and pb["revisit_p50_ms"] else None,
                          "d_wall": d["wall_time_s"], "pb_wall": pb["wall_time_s"],
                          "diff_wall": round(pb["wall_time_s"] - d["wall_time_s"], 1) if d["wall_time_s"] and pb["wall_time_s"] else None,
                          "d_pp": d["prompt_processed_total"], "pb_pp": pb["prompt_processed_total"],
                          "diff_pp": pb["prompt_processed_total"] - d["prompt_processed_total"]})
    with open(os.path.join(OUT, "paired_results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(prows[0].keys())); w.writeheader(); w.writerows(prows)

    print(json.dumps(gates, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
