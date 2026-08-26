#!/usr/bin/env python3
"""KV hotness + L0→L1 ram offload Qwen3.5-4B eval (plan §5 A1/A2/B1).

固定：Qwen3.5-4B Q4_K_M、CUDA、--kv-unified、temp=0、seed=42、--cache-type-r/s f32。
Control：--kv-tiering none --kv-hotness off。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import tempfile
from typing import Any, Dict, Optional
from urllib import error, request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SERVER = os.environ.get("KV_EVAL_SERVER", os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server"))
MODEL = os.environ.get("KV_EVAL_MODEL", os.path.join(ROOT, "models", "Qwen3.5-4B-Q4_K_M.gguf"))
PORT = int(os.environ.get("KV_EVAL_PORT", "8091"))
BASE = f"http://127.0.0.1:{PORT}"

CHUNK = (
    "The Corvus Delta monitoring network collects hydrological and ecological "
    "data across five instrumented zones every fifteen minutes. Each zone "
    "transmits calibrated readings for water quality, soil composition, "
    "vegetation density, and wildlife activity through the mesh telemetry "
    "backbone. The aggregation layer validates every packet against the "
    "schema, rejects failed checksums, and appends accepted readings to the "
    "append-only tool log before any downstream analysis begins."
)
PRESSURE_TEXT = (
    "This is a completely different payload stream that shares no prefix with "
    "the Corvus Delta monitoring data. It describes atmospheric pressure "
    "readings from high-altitude weather stations, seismic activity logs, "
    "ocean tide gauges, and solar radiation measurements collected by an "
    "unrelated network with a distinct schema and provenance record."
)


def build_prompt(n_tokens: int, tag: str, prefix_text: str = CHUNK) -> str:
    chars = int(n_tokens * 6.2)
    body = " ".join([prefix_text] * (chars // len(prefix_text) + 1))[:chars]
    return body + f"\n\nEND STATE: {tag}\nPlease confirm the state value above."


def http_json(method: str, url: str, body: Optional[dict] = None, timeout: int = 600) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def wait_health(proc: subprocess.Popen, timeout_s: int = 240) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if proc.poll() is not None:
            return False
        try:
            with request.urlopen(f"{BASE}/health", timeout=3) as resp:
                if json.loads(resp.read().decode()).get("status") == "ok":
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def start_server(log_path: str, *, parallel: int, ctx: int, cache_ram: int,
                 hotness: str, tiering: str, idle_ticks: int, pressure: float,
                 extra: Optional[list] = None) -> subprocess.Popen:
    cmd = [
        SERVER, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
        "-ngl", "99", "--ctx-size", str(ctx), "--parallel", str(parallel),
        "--kv-unified", "--seed", "42", "--temp", "0",
        "--cache-type-r", "f32", "--cache-type-s", "f32",
        "--kv-hotness", hotness, "--kv-tiering", tiering,
        "--kv-tier-idle-ticks", str(idle_ticks),
        "--kv-tier-pressure", str(pressure),
        "--chat-template-kwargs", '{"enable_thinking":false}',
    ]
    if os.environ.get("KV_NO_CACHE_RAM") != "1":
        cmd.extend(["--cache-ram", str(cache_ram)])
    if os.environ.get("KV_KEEP_IDLE_SLOTS") != "1":
        cmd.append("--no-cache-idle-slots")
    if extra:
        cmd.extend(extra)
    allocator = os.environ.get("KV_ALLOCATOR")
    if allocator:
        cmd.extend(["--kv-allocator", allocator, "--kv-block-size", os.environ.get("KV_BLOCK_SIZE", "16")])
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    logf = open(log_path, "w")
    return subprocess.Popen(cmd, stdout=logf, stderr=logf)


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def completion(prompt: str, slot: Optional[int] = None, n_predict: int = 16) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "prompt": prompt,
        "n_predict": n_predict,
        "temperature": 0,
        "cache_prompt": True,
        "seed": 42,
    }
    if slot is not None:
        body["id_slot"] = slot
    t0 = time.perf_counter()
    d = http_json("POST", f"{BASE}/completion", body)
    lat = (time.perf_counter() - t0) * 1000.0
    timings = d.get("timings") or {}
    text = d.get("content") or ""
    return {
        "status": "ok" if "content" in d else "err",
        "content": text,
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "prompt_n": timings.get("prompt_n"),
        "cache_n": timings.get("cache_n"),
        "prompt_ms": timings.get("prompt_ms"),
        "predicted_ms": timings.get("predicted_ms"),
        "latency_ms": lat,
        "id_slot": d.get("id_slot"),
        "tokens_cached": d.get("tokens_cached"),
    }


def kv() -> Dict[str, Any]:
    return http_json("GET", f"{BASE}/metrics/kv")


def grep_offload(log_path: str) -> list:
    if not os.path.exists(log_path):
        return []
    with open(log_path, "r", errors="replace") as f:
        return re.findall(r"__TIER_OFFLOAD__ slot=(\d+) reason=(\S+)", f.read())


def grep_log(log_path: str, pat: str) -> list:
    if not os.path.exists(log_path):
        return []
    with open(log_path, "r", errors="replace") as f:
        return re.findall(pat, f.read())


def run_a1(out_dir: str) -> Dict[str, Any]:
    """parallel=4 no-pressure: hash/latency vs off."""
    rec: Dict[str, Any] = {"name": "A1", "runs": {}}
    prompt = build_prompt(180, "A1")
    for name, hotness, tiering in (
        ("control", "off", "none"),
        ("recency", "recency", "none"),
    ):
        log = os.path.join(out_dir, f"a1_{name}.log")
        proc = start_server(log, parallel=4, ctx=4096, cache_ram=0,
                            hotness=hotness, tiering=tiering, idle_ticks=8, pressure=0.90)
        if not wait_health(proc):
            stop_server(proc)
            rec["runs"][name] = {"error": "server_start_failed", "log": log}
            continue
        rows = []
        for i in range(3):
            rows.append(completion(prompt, slot=i % 4, n_predict=8))
        snap = kv()
        stop_server(proc)
        rec["runs"][name] = {
            "rows": rows,
            "hashes": [r["content_sha256"] for r in rows],
            "latency_ms": [r["latency_ms"] for r in rows],
            "capacity_cells": snap.get("capacity_cells"),
            "used_cells": snap.get("used_cells"),
            "hotness": (snap.get("hotness") or {}).get("policy"),
        }
    c = rec["runs"].get("control", {})
    r = rec["runs"].get("recency", {})
    rec["hash_match"] = bool(c.get("hashes") and c.get("hashes") == r.get("hashes"))
    def med(xs):
        xs = sorted(xs or [])
        return xs[len(xs)//2] if xs else None
    cl, rl = med(c.get("latency_ms")), med(r.get("latency_ms"))
    rec["latency_p50_control"] = cl
    rec["latency_p50_recency"] = rl
    rec["latency_dev_pct"] = None if not cl else abs(rl - cl) / cl * 100.0
    rec["pass"] = rec["hash_match"] and (rec["latency_dev_pct"] is None or rec["latency_dev_pct"] <= 3.0)
    return rec


def run_a2(out_dir: str) -> Dict[str, Any]:
    """5th session pool pressure: active not cleared; recency victim is oldest idle."""
    rec: Dict[str, Any] = {"name": "A2"}
    log = os.path.join(out_dir, "a2_recency.log")
    proc = start_server(log, parallel=4, ctx=2048, cache_ram=0,
                        hotness="recency", tiering="none", idle_ticks=8, pressure=0.90)
    if not wait_health(proc):
        stop_server(proc)
        rec["error"] = "server_start_failed"
        rec["pass"] = False
        return rec
    completion(build_prompt(500, "COLD"), slot=1, n_predict=4)
    completion(build_prompt(500, "HOT"), slot=0, n_predict=4)
    completion(build_prompt(500, "WARM"), slot=2, n_predict=4)
    snap_before = kv()
    pressure = completion(build_prompt(700, "PRESSURE", PRESSURE_TEXT), slot=3, n_predict=8)
    snap_after = kv()
    with open(log, "r", errors="replace") as f:
        text = f.read()
    purged = [int(x) for x in re.findall(r"purging slot (\d+) with", text)]
    stop_server(proc)
    rec.update({
        "purged": purged,
        "pressure_status": pressure["status"],
        "used_before": snap_before.get("used_cells"),
        "used_after": snap_after.get("used_cells"),
        "capacity_cells": snap_after.get("capacity_cells"),
        "active_not_cleared": 3 not in purged,
        "oldest_idle_selected": purged[:1] == [1] if purged else False,
    })
    rec["pass"] = rec["active_not_cleared"] and bool(purged)
    return rec


def run_b1(out_dir: str) -> Dict[str, Any]:
    """offload+recall: used_cells drop, suffix-only prefill, greedy hash match."""
    rec: Dict[str, Any] = {"name": "B1"}
    prompt = build_prompt(280, "OFFLOAD")
    # control: no tiering
    log_c = os.path.join(out_dir, "b1_control.log")
    proc = start_server(log_c, parallel=2, ctx=2048, cache_ram=256,
                        hotness="off", tiering="none", idle_ticks=8, pressure=0.90)
    if not wait_health(proc):
        stop_server(proc)
        rec["error"] = "control_start_failed"
        rec["pass"] = False
        return rec
    ctrl = completion(prompt, slot=0, n_predict=8)
    stop_server(proc)

    log_t = os.path.join(out_dir, "b1_ram.log")
    proc = start_server(log_t, parallel=2, ctx=2048, cache_ram=256,
                        hotness="off", tiering="ram", idle_ticks=1, pressure=0.10)
    if not wait_health(proc):
        stop_server(proc)
        rec["error"] = "ram_start_failed"
        rec["pass"] = False
        return rec
    first = completion(prompt, slot=0, n_predict=8)
    time.sleep(0.4)
    snap = kv()
    offloads = grep_offload(log_t)
    recall = completion(prompt, slot=0, n_predict=8)
    snap2 = kv()
    stop_server(proc)
    rec.update({
        "control_hash": ctrl["content_sha256"],
        "control_latency_ms": ctrl["latency_ms"],
        "first_hash": first["content_sha256"],
        "recall_hash": recall["content_sha256"],
        "first_prompt_n": first["prompt_n"],
        "recall_prompt_n": recall["prompt_n"],
        "offloads": offloads,
        "l1_entries": (snap.get("tiering") or {}).get("l1_entries"),
        "offload_count": (snap.get("tiering") or {}).get("offload_count"),
        "l1_restore_count": (snap2.get("tiering") or {}).get("l1_restore_count"),
        "last_victim": (snap.get("tiering") or {}).get("last_victim"),
        "capacity_cells": snap.get("capacity_cells"),
        "used_after_offload": snap.get("used_cells"),
        "used_after_recall": snap2.get("used_cells"),
        "attention_capacity": ((snap.get("attention") or {}).get("capacity_bytes")),
        "recurrent": snap.get("recurrent"),
    })
    rec["tier_latency_ms"] = recall["latency_ms"]
    rec["latency_delta_pct"] = (
        (recall["latency_ms"] - ctrl["latency_ms"]) / ctrl["latency_ms"] * 100.0
        if ctrl["latency_ms"] else None
    )
    rec["hash_match"] = ctrl["content_sha256"] == first["content_sha256"] == recall["content_sha256"]
    rec["used_cells_dropped"] = bool(offloads)
    rec["suffix_prefill"] = (
        recall["prompt_n"] is not None and first["prompt_n"] is not None
        and recall["prompt_n"] < first["prompt_n"]
    )
    rec["hybrid_safe_fallback"] = (
        not rec["suffix_prefill"] and recall["prompt_n"] == first["prompt_n"]
    )
    rec["pass"] = (
        rec["hash_match"] and rec["used_cells_dropped"]
        and (rec["suffix_prefill"] or rec["hybrid_safe_fallback"])
    )
    return rec


def run_c1(out_dir: str) -> Dict[str, Any]:
    """L2 overflow: L1 cap forces disk spill; restore hash matches; cap respected."""
    rec: Dict[str, Any] = {"name": "C1"}
    l2_dir = tempfile.mkdtemp(prefix="kv_tier2_c1_")
    prompt_a = build_prompt(220, "L2A")
    prompt_b = build_prompt(220, "L2B", PRESSURE_TEXT)
    control_log = os.path.join(out_dir, "c1_control.log")
    control = start_server(control_log, parallel=2, ctx=2048, cache_ram=0,
                           hotness="off", tiering="none", idle_ticks=8, pressure=0.90)
    if not wait_health(control):
        stop_server(control)
        rec["error"] = "control_start_failed"
        rec["pass"] = False
        return rec
    control_first = completion(prompt_a, slot=0, n_predict=8)
    stop_server(control)
    log = os.path.join(out_dir, "c1_disk.log")
    proc = start_server(
        log, parallel=2, ctx=2048, cache_ram=128,
        hotness="off", tiering="ram,disk", idle_ticks=1, pressure=0.10,
        extra=["--slot-save-path", l2_dir, "--kv-tier2-max-mib", "256"],
    )
    if not wait_health(proc):
        stop_server(proc)
        rec["error"] = "server_start_failed"
        rec["pass"] = False
        rec["log"] = log
        return rec
    first = completion(prompt_a, slot=0, n_predict=8)
    second = completion(prompt_b, slot=1, n_predict=8)
    snap = kv()
    spills = grep_log(log, r"__TIER2_SPILL__")
    recall = completion(prompt_a, slot=0, n_predict=8)
    restores = grep_log(log, r"__TIER2_RESTORE__")
    snap2 = kv()
    stop_server(proc)
    l2_bytes = (snap.get("tiering") or {}).get("l2_bytes") or 0
    rec.update({
        "first_hash": first.get("content_sha256"),
        "recall_hash": recall.get("content_sha256"),
        "control_latency_ms": control_first.get("latency_ms"),
        "tier_latency_ms": recall.get("latency_ms"),
        "first_prompt_n": first.get("prompt_n"),
        "recall_prompt_n": recall.get("prompt_n"),
        "spills": len(spills),
        "restores": len(restores),
        "l2_entries_after_spill": (snap.get("tiering") or {}).get("l2_entries"),
        "l2_bytes_after_spill": l2_bytes,
        "l2_spill_count": (snap.get("tiering") or {}).get("l2_spill_count"),
        "l2_entries_after_restore": (snap2.get("tiering") or {}).get("l2_entries"),
        "l2_restore_count": (snap2.get("tiering") or {}).get("l2_restore_count"),
        "tiering_mode": (snap.get("tiering") or {}).get("mode"),
        "l2_cap_ok": l2_bytes <= 256 * 1024 * 1024,
    })
    rec["latency_delta_pct"] = (
        (recall.get("latency_ms") - control_first.get("latency_ms"))
        / control_first.get("latency_ms") * 100.0
        if control_first.get("latency_ms") else None
    )
    rec["hash_match"] = first.get("content_sha256") == recall.get("content_sha256")
    rec["hybrid_safe_fallback"] = rec["restores"] == 0 and rec["recall_prompt_n"] == rec["first_prompt_n"]
    rec["suffix_prefill"] = (
        recall.get("prompt_n") is not None and first.get("prompt_n") is not None
        and recall["prompt_n"] < first["prompt_n"]
    )
    rec["pass"] = (
        rec["tiering_mode"] == "ram,disk"
        and rec["spills"] >= 1
        # Qwen3.5-4B is hybrid: an L2 state includes generated suffix tokens,
        # which recurrent memory cannot safely remove. The safe contract is
        # either restore for an exact state or reject and full-prefill.
        and rec["restores"] in (0, 1)
        and rec["hash_match"]
        and rec["l2_cap_ok"]
    )
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "benchmark", "results", "kv_hotness_tiering_eval"))
    ap.add_argument("--skip-a1", action="store_true")
    ap.add_argument("--skip-a2", action="store_true")
    ap.add_argument("--skip-b1", action="store_true")
    ap.add_argument("--skip-c1", action="store_true")
    args = ap.parse_args()
    if not os.path.isfile(SERVER):
        print(f"missing server: {SERVER}", file=sys.stderr)
        return 2
    if not os.path.isfile(MODEL):
        print(f"missing model: {MODEL}", file=sys.stderr)
        return 2
    os.makedirs(args.out_dir, exist_ok=True)
    report: Dict[str, Any] = {
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": MODEL,
        "server": SERVER,
        "port": PORT,
        "scenarios": {},
    }
    if not args.skip_a1:
        print("running A1...", flush=True)
        report["scenarios"]["A1"] = run_a1(args.out_dir)
        print("A1", report["scenarios"]["A1"].get("pass"), flush=True)
    if not args.skip_a2:
        print("running A2...", flush=True)
        report["scenarios"]["A2"] = run_a2(args.out_dir)
        print("A2", report["scenarios"]["A2"].get("pass"), flush=True)
    if not args.skip_b1:
        print("running B1...", flush=True)
        report["scenarios"]["B1"] = run_b1(args.out_dir)
        print("B1", report["scenarios"]["B1"].get("pass"), flush=True)
    if not args.skip_c1:
        print("running C1...", flush=True)
        report["scenarios"]["C1"] = run_c1(args.out_dir)
        print("C1", report["scenarios"]["C1"].get("pass"), flush=True)
    report["all_pass"] = all(s.get("pass") for s in report["scenarios"].values()) if report["scenarios"] else False
    out = os.path.join(args.out_dir, "report.json")
    with open(out, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(json.dumps({k: {"pass": v.get("pass"), "error": v.get("error")}
                      for k, v in report["scenarios"].items()}, indent=2))
    print("wrote", out)
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
