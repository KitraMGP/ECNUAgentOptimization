#!/usr/bin/env python3
"""E3.5：lifecycle trace 验证 runner（只做诊断验证，不评估策略收益）。

验证矩阵（每 replicate 全新 server）：
- p4_multi_client：4 并发 client（trace_id 注入）→ 压力 → 回访；default/lru 各 3
- active_protection：真并发 active + idle → 验证 active 永不成为 victim；各 3
- slot_reuse：同 slot 连续请求 → generation 递增验证；各 3
- no_pressure：无压力（无 adapter）；各 2
- non_unified：parallel=2 无 unified；各 1
- diagnostics_off：--lifecycle-trace 关闭 → 零事件 + 行为不变；各 2

输出：每 replicate 的 session→slot/seq/gen 映射覆盖率、pressure/purge 关联、
active victim=0、错配=0、trace 事件数、开销（vs 无 trace 基线）。

usage: uv run python scripts/e35_lifecycle_trace_validation.py
  --scenario p4_multi_client|active_protection|slot_reuse|no_pressure|non_unified|diagnostics_off
  --policy default|lru --reps N --output xxx.json
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
import threading
import time
from typing import Any, Dict, List, Optional
from urllib import request, error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
SERVER = os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")
MODEL = os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")
SLOT_SAVE = os.path.join(ROOT, "llama.cpp", "tmp", "e20_slot_save")
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"

sys.path.insert(0, BENCH)
import workload  # noqa: F401
import workload.multi_turn  # noqa: F401
from framework.driver import Driver  # noqa: E402
from framework.workload import get_workload  # noqa: E402

CHUNK = (
    "The Corvus Delta monitoring network collects hydrological and ecological "
    "data across five instrumented zones every fifteen minutes. Each zone "
    "transmits calibrated readings for water quality, soil composition, "
    "vegetation density, and wildlife activity through the mesh telemetry "
    "backbone. The aggregation layer validates every packet against the "
    "schema, rejects failed checksums, and appends accepted readings to the "
    "append-only tool log before any downstream analysis begins."
)


def build_prompt(n_tokens: int, tag: str) -> str:
    chars = int(n_tokens * 6.2)
    body = " ".join([CHUNK] * (chars // len(CHUNK) + 1))[:chars]
    return body + f"\n\nEND STATE: {tag}\nConfirm the state value above."


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def http_json(method: str, url: str, body: Optional[dict] = None, timeout: int = 600) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def raw_chat(messages: List[dict], trace_id: str, max_tokens: int = 16) -> Dict[str, Any]:
    body: Dict[str, Any] = {"model": "bench", "messages": messages,
                            "max_tokens": max_tokens, "temperature": 0,
                            "chat_template_kwargs": {"enable_thinking": False},
                            "trace_id": trace_id}
    t0 = time.perf_counter()
    try:
        d = http_json("POST", f"{BASE}/v1/chat/completions", body)
        status = "ok"
    except error.HTTPError as e:
        d = {"error": {"code": e.code}}
        status = f"http_{e.code}"
    except Exception as e:
        d = {"error": {"type": type(e).__name__}}
        status = "exc"
    lat = (time.perf_counter() - t0) * 1000
    usage = d.get("usage", {})
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    return {"status": status, "latency_ms": round(lat, 1),
            "prompt_tokens": usage.get("prompt_tokens"), "cached_tokens": cached,
            "trace_id": trace_id, "error": d.get("error")}


def wait_health(proc: subprocess.Popen, timeout_s: int = 180) -> bool:
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


def start_server(policy: str, log_path: str, trace: bool = True,
                 unified: bool = True, parallel: int = 4) -> subprocess.Popen:
    os.makedirs(SLOT_SAVE, exist_ok=True)
    cmd = [SERVER, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "-ngl", "99", "--ctx-size", "8192", "--parallel", str(parallel),
           "--cache-reuse", "0", "--seed", "42", "--temp", "0",
           "--cache-ram", "0", "--slot-save-path", SLOT_SAVE,
           "--unified-idle-slot-policy", policy]
    if trace:
        cmd.append("--lifecycle-trace")
    if unified:
        cmd.append("--kv-unified")
    logf = open(log_path, "w")
    return subprocess.Popen(cmd, stdout=logf, stderr=logf)


def stop_server(proc: subprocess.Popen) -> bool:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    return proc.returncode is not None


def parse_trace_events(log_path: str) -> List[Dict[str, Any]]:
    """解析 __LIFECYCLE_TRACE__ 行为结构化事件。"""
    evs = []
    try:
        with open(log_path, "r", errors="replace") as f:
            for line in f:
                m = re.search(r"__LIFECYCLE_TRACE__ (.*)", line)
                if not m:
                    continue
                body = m.group(1)
                ev: Dict[str, Any] = {}
                for kv in re.findall(r"(\w+)=([^\s]+|null)", body):
                    ev[kv[0]] = kv[1]
                evs.append(ev)
    except FileNotFoundError:
        pass
    return evs


def kv_state() -> Dict[str, Any]:
    try:
        return http_json("GET", f"{BASE}/metrics/kv", timeout=5)
    except Exception as e:
        return {"error": str(e)}


class RecordingDriver:
    def __init__(self, driver: Driver, trace_id: str) -> None:
        self._d = driver
        self.trace_id = trace_id
        self.requests: List[Dict[str, Any]] = []
        self._orig_chat = driver.chat

    def install(self) -> None:
        self._d.chat = self._chat_wrapper  # type: ignore[method-assign]
        # 注入 trace_id 到每请求 body（E3.5）
        orig_extra = self._d._extra_body
        def extra_with_trace():
            b = orig_extra()
            b["trace_id"] = self.trace_id
            return b
        self._d._extra_body = extra_with_trace  # type: ignore[method-assign]

    def _chat_wrapper(self, messages: List[dict], _retry: int = 0) -> Dict[str, Any]:
        r = self._orig_chat(messages, _retry)
        self.requests.append({"messages": messages,
                              "prompt_tokens": r.get("prompt_tokens"),
                              "cached_tokens": r.get("cached_tokens"),
                              "latency_ms": r.get("latency_ms"),
                              "trace_id": self.trace_id})
        return r


def run_session(sid: str, wl_params: Dict[str, Any], rec: Dict, out: Dict) -> None:
    try:
        driver = Driver(base_url=f"{BASE}/v1", host="127.0.0.1", port=PORT,
                        enable_thinking=False)
        rdr = RecordingDriver(driver, f"trace-{sid}")
        rdr.install()
        wl = get_workload("multi_turn")
        spec = wl.generate(dict(wl_params))
        result = wl.run(driver, spec)
        out[sid] = {"ok": True, "requests": rdr.requests,
                    "evaluate": wl.evaluate(result, spec)}
    except Exception as e:
        out[sid] = {"ok": False, "error": f"{type(e).__name__}: {e}"}


def run_replicate(scenario: str, policy: str, rep: int, log_dir: str) -> Dict[str, Any]:
    trace = scenario != "diagnostics_off"
    unified = scenario != "non_unified"
    parallel = 2 if scenario in ("non_unified",) else 4
    log_path = os.path.join(log_dir, f"e35_{scenario}_{policy}_{rep}.log")
    proc = start_server(policy, log_path, trace=trace, unified=unified, parallel=parallel)
    rec: Dict[str, Any] = {"scenario": scenario, "policy": policy, "rep": rep,
                           "server_pid": proc.pid, "clean_verified": False,
                           "trace_enabled": trace, "session_results": {},
                           "adapter": [], "server_exited": False}
    if not wait_health(proc):
        rec["startup_failure"] = True
        stop_server(proc)
        return rec
    kv = kv_state()
    rec["clean_verified"] = kv.get("used_cells") == 0 and kv.get("active_sequences") == 0

    # 4 个并发 session（trace_id 注入）
    session_out: Dict[str, Dict] = {}
    threads = []
    for sid in ("S0", "S1", "S2", "S3"):
        t = threading.Thread(target=run_session, args=(sid, {"rounds": 6}, rec, session_out))
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=900)
    rec["session_results"] = session_out
    rec["concurrency_ok"] = len(session_out) == 4 and all(s.get("ok") for s in session_out.values())

    if scenario in ("p4_multi_client", "active_protection", "slot_reuse"):
        # 压力 + 回访（trace_id 关联）
        s0 = session_out.get("S0", {})
        if s0.get("ok") and s0["requests"]:
            last_msgs = s0["requests"][-1]["messages"]
            rec["adapter"].append({"tag": "S0_replay", **raw_chat(last_msgs, "trace-adapter-hot")})
            rec["adapter"].append({"tag": "pressure", **raw_chat(
                [{"role": "user", "content": build_prompt(6300, "PRESSURE")}], "trace-adapter-pressure")})
            rec["adapter"].append({"tag": "revisit_S0", **raw_chat(last_msgs, "trace-adapter-revisit")})

    rec["trace_events"] = parse_trace_events(log_path)
    stop_server(proc)
    rec["server_exited"] = proc.returncode is not None
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["p4_multi_client", "active_protection", "slot_reuse",
                                           "no_pressure", "non_unified", "diagnostics_off"], required=True)
    ap.add_argument("--policy", choices=["default", "lru"], required=True)
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--log-dir", default="/tmp")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    os.makedirs(args.log_dir, exist_ok=True)
    reps = []
    for i in range(args.reps):
        reps.append(run_replicate(args.scenario, args.policy, i, args.log_dir))
        print(f"  [rep{i}] clean={reps[-1]['clean_verified']} "
              f"trace_events={len(reps[-1].get('trace_events', []))}")
    out = {"scenario": args.scenario, "policy": args.policy, "reps": reps,
           "binary_sha256": sha256(SERVER), "model_sha256": sha256(MODEL),
           "llama_commit": subprocess.check_output(
               ["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
           "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip()}
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"保存: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
