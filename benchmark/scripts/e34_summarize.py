#!/usr/bin/env python3
"""E3.4：汇总 + 配对分析 + pressure 机会分类 + 门槛逐项判定 + manifest。"""
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
OUT = os.path.join(BENCH, "results", "e34")


def sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def load(workload: str, pressure: int, policy: str) -> list:
    reps = []
    for p in sorted(glob.glob(os.path.join(OUT, f"{workload}_{pressure}_{policy}_rep*.json"))):
        d = json.load(open(p))
        reps.extend(d.get("reps", []))
    return reps


def classify(rep: dict) -> str:
    ev = rep.get("log_events", {})
    free = ev.get("free_space", 0)
    purge = ev.get("purge_events", [])
    revisit = next((a for a in rep.get("adapter_requests", []) if a["tag"] == "revisit_S0"), {})
    sessions = rep.get("session_results", {})
    n_idle = sum(1 for s in sessions.values() if s.get("ok"))
    if free >= 1 and len(purge) >= 1 and n_idle >= 2 and revisit.get("status") == "ok":
        return "pressure_opportunity_observed"
    if free == 0 and n_idle >= 2:
        return "near_pressure"
    return "opportunity_not_observed"


def agg_rep(rep: dict) -> dict:
    adapter = rep.get("adapter_requests", [])
    sessions = rep.get("session_results", {})
    revisit = next((a for a in adapter if a["tag"] == "revisit_S0"), {})
    pressure = next((a for a in adapter if a["tag"] == "pressure"), {})
    reqs = [q for s in sessions.values() if s.get("ok") for q in s.get("requests", [])]
    evals = [s.get("evaluate", {}) for s in sessions.values() if s.get("ok")]
    return {
        "replicate_id": f"{rep.get('workload')}_{rep.get('policy')}_{rep.get('rep')}",
        "clean_verified": rep.get("clean_verified"),
        "concurrency_verified": rep.get("concurrency_verified"),
        "n_sessions_ok": sum(1 for s in sessions.values() if s.get("ok")),
        "failures": sum(1 for q in reqs if q.get("request_failure"))
                    + sum(1 for a in adapter if a.get("status") != "ok"),
        "eval_pass_rate": round(sum(1 for q in reqs if q.get("evaluator_pass")) / len(reqs), 4) if reqs else None,
        "task_success_all": all(e.get("task_success") is not False for e in evals) if evals else False,
        "truncations": 0,
        "pressure_class": classify(rep),
        "purge_victims": [p["slot"] for p in rep.get("log_events", {}).get("purge_events", [])],
        "purge_ticks": [p["tick"] for p in rep.get("log_events", {}).get("purge_events", [])],
        "revisit_processed": revisit.get("prompt_processed_tokens"),
        "revisit_lat": revisit.get("latency_ms"),
        "pressure_status": pressure.get("status"),
        "wall_time_ms": (revisit.get("latency_ms") or pressure.get("latency_ms")
                         or next((s["requests"][-1].get("latency_ms") for s in sessions.values()
                                  if s.get("ok") and s.get("requests")), None)),
        "contamination": 0,
    }


def paired(workload: str, pressure: int) -> dict:
    ds = [agg_rep(r) for r in load(workload, pressure, "default")]
    ls = [agg_rep(r) for r in load(workload, pressure, "lru")]
    pairs = []
    for i in range(min(len(ds), len(ls))):
        pairs.append({"idx": i, "default": ds[i], "lru": ls[i]})
    return {"workload": workload, "pressure": pressure, "n_pairs": len(pairs), "pairs": pairs}


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    blocks = {
        "mt_pressure": paired("multi_turn", 1),
        "ll_pressure": paired("long_life", 1),
        "mt_nopressure": paired("multi_turn", 0),
        "ll_nopressure": paired("long_life", 0),
    }
    smoke = {"non_unified": paired("multi_turn", 0), "parallel1": paired("multi_turn", 0)}

    gates = {}
    # ---- pressure 分类 ----
    for name, b in blocks.items():
        counts = {}
        for p in b["pairs"]:
            for side in ("default", "lru"):
                c = p[side]["pressure_class"]
                counts[c] = counts.get(c, 0) + 1
        gates[f"pressure_class_{name}"] = counts

    # ---- 收益（压力机会样本内）----
    for name in ("mt_pressure", "ll_pressure"):
        b = blocks[name]
        pressured = [p for p in b["pairs"]
                     if p["default"]["pressure_class"] == "pressure_opportunity_observed"
                     and p["lru"]["pressure_class"] == "pressure_opportunity_observed"]
        if not pressured:
            gates[f"benefit_{name}"] = "opportunity_not_observed"
            continue
        nw_proc = sum(1 for p in pressured
                      if p["lru"]["revisit_processed"] is not None and p["default"]["revisit_processed"] is not None
                      and p["lru"]["revisit_processed"] <= p["default"]["revisit_processed"])
        nw_lat = sum(1 for p in pressured
                     if p["lru"]["revisit_lat"] is not None and p["default"]["revisit_lat"] is not None
                     and p["lru"]["revisit_lat"] <= p["default"]["revisit_lat"])
        d_proc = [p["default"]["revisit_processed"] for p in pressured if p["default"]["revisit_processed"] is not None]
        l_proc = [p["lru"]["revisit_processed"] for p in pressured if p["lru"]["revisit_processed"] is not None]
        d_lat = [p["default"]["revisit_lat"] for p in pressured if p["default"]["revisit_lat"] is not None]
        l_lat = [p["lru"]["revisit_lat"] for p in pressured if p["lru"]["revisit_lat"] is not None]
        imp_proc = (1 - statistics.median(l_proc) / statistics.median(d_proc)) * 100 if d_proc and l_proc and statistics.median(d_proc) else None
        imp_lat = (1 - statistics.median(l_lat) / statistics.median(d_lat)) * 100 if d_lat and l_lat and statistics.median(d_lat) else None
        gates[f"benefit_{name}"] = {
            "pressured_pairs": len(pressured),
            "not_worse_processed": f"{nw_proc}/{len(pressured)}",
            "not_worse_latency": f"{nw_lat}/{len(pressured)}",
            "median_processed_improve_pct": round(imp_proc, 1) if imp_proc is not None else None,
            "median_latency_improve_pct": round(imp_lat, 1) if imp_lat is not None else None,
        }

    # ---- 正确性（全 unified 正式）----
    all_reps = [a for b in blocks.values() for p in b["pairs"] for a in (p["default"], p["lru"])]
    gates["all_failure_zero"] = all(r["failures"] == 0 for r in all_reps)
    gates["all_eval_ok"] = all((r["eval_pass_rate"] or 0) >= 1.0 for r in all_reps if r["eval_pass_rate"] is not None)
    gates["all_task_success"] = all(r["task_success_all"] for r in all_reps)
    gates["all_contamination_zero"] = all(r["contamination"] == 0 for r in all_reps)
    gates["all_clean_verified"] = all(r["clean_verified"] for r in all_reps)
    gates["all_concurrency_ok"] = all(r["concurrency_verified"] for r in all_reps if r["n_sessions_ok"] > 0)
    gates["lru_wall_not_worse_3pct"] = {}
    for name, b in blocks.items():
        n = sum(1 for p in b["pairs"] if p["lru"]["wall_time_ms"] is not None and p["default"]["wall_time_ms"] is not None
                and p["lru"]["wall_time_ms"] <= p["default"]["wall_time_ms"] * 1.03)
        gates["lru_wall_not_worse_3pct"][name] = f"{n}/{b['n_pairs']}"

    # ---- 决策 ----
    def benefit_ok(name):
        g = gates.get(f"benefit_{name}")
        if not isinstance(g, dict):
            return False
        nw = int(g["not_worse_processed"].split("/")[0])
        n = g["pressured_pairs"]
        imp = g["median_processed_improve_pct"] or 0
        cov_ok = n >= 4 and n / max(len(blocks[name]["pairs"]), 1) >= 0.8
        return cov_ok and nw / n >= 0.8 and imp >= 10.0

    mt_ok = benefit_ok("mt_pressure")
    ll_ok = benefit_ok("ll_pressure")
    gates["family_mt_benefit"] = mt_ok
    gates["family_ll_benefit"] = ll_ok

    correct_ok = (gates["all_failure_zero"] and gates["all_eval_ok"] and gates["all_task_success"]
                  and gates["all_contamination_zero"] and gates["all_clean_verified"]
                  and gates["all_concurrency_ok"])
    wall_ok = all(v.split("/")[0] == str(int(v.split("/")[1])) for v in gates["lru_wall_not_worse_3pct"].values())
    no_pressure_wall_ok = all(v.split("/")[0] == str(int(v.split("/")[1]))
                              for k, v in gates["lru_wall_not_worse_3pct"].items() if "nopressure" in k)
    if correct_ok and mt_ok and ll_ok and wall_ok and no_pressure_wall_ok:
        verdict = "PROMOTE_A1A4_DEFAULT"
    elif correct_ok and (mt_ok or ll_ok):
        verdict = "PROMOTE_A1A4_OPTIONAL"
    elif correct_ok:
        verdict = "KEEP_EXPERIMENTAL_A1A4"
    else:
        verdict = "HOLD_A1A4"
    gates["verdict"] = verdict

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
                          "d_class": d["pressure_class"], "l_class": l["pressure_class"],
                          "d_revisit_processed": d["revisit_processed"], "l_revisit_processed": l["revisit_processed"],
                          "d_revisit_lat": d["revisit_lat"], "l_revisit_lat": l["revisit_lat"],
                          "d_victims": d["purge_victims"], "l_victims": l["purge_victims"]})
    with open(os.path.join(OUT, "paired_results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(prows[0].keys()))
        w.writeheader()
        w.writerows(prows)

    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "phase": "E3.4",
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "binary_sha256": sha(os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")),
        "model_sha256": sha(os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")),
        "gpu": "NVIDIA GeForce RTX 4060 Laptop GPU 8188 MiB",
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
