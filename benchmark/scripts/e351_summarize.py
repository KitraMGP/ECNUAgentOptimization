#!/usr/bin/env python3
"""E3.5.1：契约闭环汇总 + request-level mapping/chain coverage/integrity/overhead 判定。"""
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
OUT = os.path.join(BENCH, "results", "e351")


def sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def load(scenario: str, policy: str) -> list:
    reps = []
    for p in sorted(glob.glob(os.path.join(OUT, f"{scenario}_{policy}_rep*.json"))):
        d = json.load(open(p))
        reps.extend(d.get("reps", []))
    return reps


def analyze_rep(rep: dict) -> dict:
    evs = rep.get("trace_events", [])
    sessions = rep.get("session_results", {})
    integrity = rep.get("integrity", {})
    # request-level mapping：session 内每请求的 rtrace 是否出现 assigned→active→idle 链
    req_traces = [q["trace_id"] for s in sessions.values() if s.get("ok") for q in s.get("requests", [])]
    assigned = {}
    for e in evs:
        if e.get("type") == "assigned":
            assigned.setdefault(e.get("rtrace"), []).append(e)
    mapped = sum(1 for t in req_traces if t in assigned)
    mapping_coverage = mapped / len(req_traces) if req_traces else 0.0
    # chain coverage：assigned→active→idle（同 rtrace）
    chain_ok = 0
    for t in set(req_traces):
        has_a = any(e.get("type") == "assigned" and e.get("rtrace") == t for e in evs)
        has_act = any(e.get("type") == "active" and e.get("rtrace") == t for e in evs)
        has_i = any(e.get("type") == "idle" and e.get("rtrace") == t for e in evs)
        if has_a and has_act and has_i:
            chain_ok += 1
    chain_coverage = chain_ok / len(set(req_traces)) if req_traces else 0.0
    # pressure 链：pressure → purge → retry/resume → idle
    pressure_events = [e for e in evs if e.get("type") == "pressure"]
    purge_events = [e for e in evs if e.get("type") == "purge"]
    retry_or_resume = [e for e in evs if e.get("type") in ("retry", "resume")]
    pressure_chain_ok = len(pressure_events) >= 1 and len(purge_events) >= 1 and len(retry_or_resume) >= 1
    # 错配：相邻 assigned→idle 对 slot 一致
    mismatch = 0
    prev = None
    for e in sorted(evs, key=lambda x: int(x.get("evseq", 0))):
        if e.get("rtrace") == "null":
            continue
        if e.get("type") == "assigned":
            prev = e
        elif e.get("type") == "idle" and prev is not None and prev.get("rtrace") == e.get("rtrace"):
            if prev.get("slot") != e.get("slot"):
                mismatch += 1
            prev = None
    # session latency（overhead 用）
    sess_lat = [q.get("latency_ms") for s in sessions.values() if s.get("ok")
                for q in s.get("requests", []) if q.get("latency_ms")]
    return {
        "scenario": rep.get("scenario"), "policy": rep.get("policy"), "rep": rep.get("rep"),
        "clean_verified": rep.get("clean_verified"), "concurrency_ok": rep.get("concurrency_ok"),
        "trace_enabled": rep.get("trace_enabled"),
        "n_requests": len(req_traces), "mapping_coverage": round(mapping_coverage, 4),
        "chain_coverage": round(chain_coverage, 4),
        "pressure_chain_ok": pressure_chain_ok,
        "mismatch": mismatch,
        "integrity_gap": integrity.get("gap", 0), "integrity_dup": integrity.get("duplicate", 0),
        "integrity_ooe": integrity.get("out_of_order", 0), "integrity_malformed": integrity.get("malformed", 0),
        "session_latency_median_ms": round(statistics.median(sess_lat), 1) if sess_lat else None,
    }


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    scenarios = ["request_level_mapping", "chain_pressure", "active_protection", "slot_reuse",
                 "malformed_trace", "diagnostics_off", "non_unified"]
    blocks = {}
    for sc in scenarios:
        blocks[sc] = {"default": [analyze_rep(r) for r in load(sc, "default")],
                      "lru": [analyze_rep(r) for r in load(sc, "lru")]}

    gates = {}
    trace_on = [blocks[sc][pol] for sc in ("request_level_mapping", "chain_pressure",
                                           "active_protection", "slot_reuse", "malformed_trace")
                for pol in ("default", "lru")]
    all_on = [r for grp in trace_on for r in grp]
    gates["request_mapping_100pct"] = all(r["mapping_coverage"] == 1.0 for r in all_on)
    gates["request_mapping_min"] = min(r["mapping_coverage"] for r in all_on)
    gates["chain_coverage_100pct"] = all(r["chain_coverage"] == 1.0 for r in all_on)
    gates["mismatch_zero"] = all(r["mismatch"] == 0 for r in all_on)
    gates["integrity_clean"] = all(r["integrity_gap"] == 0 and r["integrity_dup"] == 0
                                   and r["integrity_ooe"] == 0 and r["integrity_malformed"] == 0 for r in all_on)
    # pressure 链（chain_pressure 场景）
    cp = blocks["chain_pressure"]["default"] + blocks["chain_pressure"]["lru"]
    gates["pressure_chain_ok"] = all(r["pressure_chain_ok"] for r in cp)
    # diagnostics-off
    off = blocks["diagnostics_off"]["default"] + blocks["diagnostics_off"]["lru"]
    gates["diagnostics_off_zero"] = all(r["trace_enabled"] is False for r in off)
    gates["diagnostics_off_events"] = sum(len(load("diagnostics_off", p)[i].get("trace_events", []))
                                          for p in ("default", "lru") for i in range(len(load("diagnostics_off", p))))
    # overhead 配对（交替执行 overhead_on/off_repN：相邻对抵消时间漂移）
    on_lat, off_lat = [], []
    for i in range(100):
        op = os.path.join(OUT, f"overhead_on_rep{i}.json")
        fp = os.path.join(OUT, f"overhead_off_rep{i}.json")
        if not (os.path.exists(op) and os.path.exists(fp)):
            break
        on_rep = json.load(open(op))["reps"][0]
        off_rep = json.load(open(fp))["reps"][0]
        on_l = [q.get("latency_ms") for s in on_rep.get("session_results", {}).values() if s.get("ok")
                for q in s.get("requests", []) if q.get("latency_ms")]
        off_l = [q.get("latency_ms") for s in off_rep.get("session_results", {}).values() if s.get("ok")
                 for q in s.get("requests", []) if q.get("latency_ms")]
        if on_l and off_l:
            on_lat.append(statistics.median(on_l))
            off_lat.append(statistics.median(off_l))
    diffs = [((a - b) / b * 100) for a, b in zip(on_lat, off_lat) if b]
    gates["overhead_pairs"] = len(diffs)
    gates["overhead_median_pct"] = round(statistics.median(diffs), 2) if diffs else None
    gates["overhead_p95_pct"] = round(sorted(diffs)[int(0.95 * len(diffs)) - 1], 2) if diffs else None
    gates["overhead_median_lt_0.5"] = (gates["overhead_median_pct"] is not None and gates["overhead_median_pct"] < 0.5)
    gates["overhead_p95_lt_1"] = (gates["overhead_p95_pct"] is not None and gates["overhead_p95_pct"] < 1.0)
    gates["all_clean"] = all(r["clean_verified"] for sc in scenarios for pol in ("default", "lru") for r in blocks[sc][pol])
    gates["all_concurrency_ok"] = all(r["concurrency_ok"] for sc in scenarios for pol in ("default", "lru") for r in blocks[sc][pol])

    ready = (gates["request_mapping_100pct"] and gates["chain_coverage_100pct"]
             and gates["mismatch_zero"] and gates["integrity_clean"]
             and gates["pressure_chain_ok"] and gates["diagnostics_off_zero"]
             and gates["overhead_median_lt_0.5"] and gates["overhead_p95_lt_1"]
             and gates["all_clean"] and gates["all_concurrency_ok"])
    gates["verdict"] = "READY_TO_ACCEPT_FIELD_TRACE" if ready else "HOLD_FOR_ATTRIBUTION_GAPS"

    out = {"blocks": blocks, "gates": gates}
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    rows = [r for sc in scenarios for pol in ("default", "lru") for r in blocks[sc][pol]]
    with open(os.path.join(OUT, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "phase": "E3.5.1",
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "binary_sha256": sha(os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")),
        "model_sha256": sha(os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")),
        "gpu": "NVIDIA GeForce RTX 4060 Laptop GPU 8188 MiB",
        "field_trace_provided": False,
        "contract": {"schema": 2, "transport": "direct_log",
                     "events": ["assigned", "active", "idle", "pressure", "purge", "retry", "resume"],
                     "integrity": ["gap", "duplicate", "out_of_order", "malformed", "truncated"]},
        "result_files_sha256": {os.path.relpath(p, OUT): sha(p) for p in
                                sorted(glob.glob(os.path.join(OUT, "**", "*.json"), recursive=True))
                                if "manifest" not in p},
    }
    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"verdict = {gates['verdict']}")
    for k, v in gates.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
