#!/usr/bin/env python3
"""E4.1：汇总 + 门禁判定（PASS/HOLD/REJECT）+ manifest。"""
from __future__ import annotations

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
OUT = os.path.join(BENCH, "results", "e41")


def sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def load(scenario: str, policy: str, trace: int = 1) -> list:
    reps = []
    for p in sorted(glob.glob(os.path.join(OUT, f"{scenario}_{policy}_r*_trace{trace}.json"))):
        d = json.load(open(p))
        reps.extend(d.get("reps", []))
    return reps


def analyze(scenario: str, policy: str, rep: dict) -> dict:
    reqs = rep.get("requests", [])
    purges = rep.get("purge_events", [])
    active = rep.get("active_results", {})
    return {
        "scenario": scenario, "policy": policy, "rep": rep.get("rep"),
        "clean": rep.get("clean_verified"),
        "kv_after_4": (rep.get("kv_after_4") or {}).get("used_cells"),
        "kv_after_pressure": (rep.get("kv_after_pressure") or {}).get("used_cells"),
        "capacity": rep.get("capacity_cells"),
        "purge_count": len(purges),
        "purge_wrn_count": rep.get("purge_wrn_count", 0),
        "purge_ticks_ascending": all(
            int(purges[i].get("tick", 0)) <= int(purges[i + 1].get("tick", 0))
            for i in range(len(purges) - 1)),
        "active_ok": all(v == "ok" for v in active.values()) if active else None,
        "s4_status": next((q["status"] for q in reqs if q.get("tag") == "S4_pressure"), None),
        "s1_revisit_status": next((q["status"] for q in reqs if q.get("tag") == "S1_revisit"), None),
        "http_400": rep.get("http_400", 0),
        "truncations": rep.get("truncations", 0),
        "trace_events": rep.get("lifecycle_trace_events", 0),
        "server_exited": rep.get("server_exited"),
    }


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    scenarios = ["A", "B", "C", "D", "E"]
    rows = []
    gates = {}
    for sc in scenarios:
        for pol in ("default", "lru"):
            for r in load(sc, pol, 1):
                rows.append(analyze(sc, pol, r))
    # production（trace off）A
    prod = []
    for pol in ("default", "lru"):
        for r in load("A", pol, 0):
            prod.append(analyze("A_prod", pol, r))
    rows += prod

    # ---- 门禁判定 ----
    A = [r for r in rows if r["scenario"] == "A"]
    B = [r for r in rows if r["scenario"] == "B"]
    C = [r for r in rows if r["scenario"] == "C"]
    D = [r for r in rows if r["scenario"] == "D"]
    E = [r for r in rows if r["scenario"] == "E"]
    prod_r = [r for r in rows if r["scenario"] == "A_prod"]

    gates["A_saturation_purge_occurred"] = all(r["purge_count"] >= 1 for r in A) and any(
        r["kv_after_4"] and r["kv_after_4"] >= 0.75 * (r["capacity"] or 8192) for r in A)
    gates["A_active_protected"] = all(r["active_ok"] for r in A if r["active_ok"] is not None)
    gates["A_no_failure"] = all(r["http_400"] == 0 and r["truncations"] == 0 for r in A)
    # B：victim 契约（lru tick 升序）
    gates["B_victim_contract"] = all(r["purge_ticks_ascending"] for r in B if r["policy"] == "lru" and r["purge_count"] > 0)
    # C：无 victim 失败（既定失败或完成；不 crash）
    gates["C_no_crash"] = all(r["server_exited"] for r in C)
    gates["C_defined_outcome"] = all(
        (r["s4_status"] in ("ok", "http_400", "http_500")) for r in C)
    # D：恢复（S1 回访 ok 或既定失败）
    gates["D_recovery_defined"] = all(r["s1_revisit_status"] in ("ok", "http_400") for r in D if r["s1_revisit_status"])
    # E：churn 无泄漏（kv_final < 容量；无 400）
    gates["E_no_leak"] = all(
        (r.get("kv_after_pressure") or 0) <= (r["capacity"] or 8192) for r in E)
    gates["E_no_failure"] = all(r["http_400"] == 0 for r in E)
    # production 零事件
    gates["production_zero_events"] = all(r["trace_events"] == 0 for r in prod_r)
    gates["production_behavior_same"] = all(
        r["purge_wrn_count"] >= 1 and r["http_400"] == 0 for r in prod_r)

    all_pass = (gates["A_saturation_purge_occurred"] and gates["A_active_protected"]
                and gates["A_no_failure"] and gates["B_victim_contract"]
                and gates["C_no_crash"] and gates["C_defined_outcome"]
                and gates["D_recovery_defined"] and gates["E_no_leak"]
                and gates["E_no_failure"] and gates["production_zero_events"]
                and gates["production_behavior_same"])
    gates["verdict"] = "PASS_KV_SATURATION_CORRECTNESS" if all_pass else "HOLD_FOR_INCOMPLETE_CAPACITY_EVIDENCE"

    json.dump({"gates": gates, "rows": rows}, open(os.path.join(OUT, "summary.json"), "w"),
              ensure_ascii=False, indent=2)
    import csv
    with open(os.path.join(OUT, "saturation_results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    # eviction_events.jsonl
    with open(os.path.join(OUT, "eviction_events.jsonl"), "w") as f:
        for sc in scenarios:
            for pol in ("default", "lru"):
                for r in load(sc, pol, 1):
                    for e in r.get("purge_events", []):
                        f.write(json.dumps({"scenario": sc, "policy": pol, **e}) + "\n")

    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M%S"), "phase": "E4.1",
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "binary_sha256": sha(os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")),
        "model_sha256": sha(os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")),
        "exploratory_wall_time": True,
        "result_files_sha256": {os.path.relpath(p, OUT): sha(p) for p in
                                sorted(glob.glob(os.path.join(OUT, "**", "*.json"), recursive=True))
                                if "manifest" not in p and "jsonl" not in p},
    }
    json.dump(manifest, open(os.path.join(OUT, "manifest.json"), "w"), ensure_ascii=False, indent=2)
    print(f"verdict = {gates['verdict']}")
    for k, v in gates.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
