#!/usr/bin/env python3
"""E3.5.1：lifecycle attribution 契约闭环验证 runner（server-per-replicate）。

验证矩阵：
- request_level_mapping：每请求唯一 request trace id → assigned/active/idle 链；default/lru 各 3
- chain_pressure：pressure→purge→retry/resume→idle 完整链；各 3
- active_protection：active session 永不成为 victim；各 3
- slot_reuse：同 slot 连续请求 generation 递增；各 3
- malformed_trace：非法 trace id 忽略；各 2
- diagnostics_off：--lifecycle-trace 关闭零事件（overhead 配对基线）；各 2
- non_unified：lru 惰性；各 1

事件完整性（integrity parser）：evseq gap/duplicate/out-of-order/malformed/
truncated 计数；transport=direct_log。
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


def raw_chat(messages: List[dict], rtrace: str, strace: str, max_tokens: int = 16) -> Dict[str, Any]:
    body: Dict[str, Any] = {"model": "bench", "messages": messages,
                            "max_tokens": max_tokens, "temperature": 0,
                            "chat_template_kwargs": {"enable_thinking": False},
                            "trace_id": rtrace, "session_trace_id": strace}
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
    return {"status": status, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "trace_id": rtrace, "session_trace_id": strace, "error": d.get("error")}


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
    evs = []
    try:
        with open(log_path, "r", errors="replace") as f:
            for line in f:
                m = re.search(r"__LIFECYCLE_TRACE__ (.*)", line)
                if not m:
                    continue
                ev: Dict[str, Any] = {}
                for kv in re.findall(r"(\w+)=([^\s]+|null)", m.group(1)):
                    ev[kv[0]] = kv[1]
                evs.append(ev)
    except FileNotFoundError:
        pass
    return evs


def integrity_check(evs: List[Dict[str, Any]]) -> Dict[str, int]:
    """evseq gap/duplicate/out-of-order/malformed/truncated 计数。"""
    seqs = []
    for e in evs:
        try:
            seqs.append(int(e.get("evseq", -1)))
        except ValueError:
            seqs.append(-1)
    counts = {"total": len(seqs), "gap": 0, "duplicate": 0, "out_of_order": 0,
              "malformed": 0, "truncated": 0}
    seen = set()
    prev = 0
    for s in seqs:
        if s == -1:
            counts["malformed"] += 1
            continue
        if s in seen:
            counts["duplicate"] += 1
        seen.add(s)
        if prev and s != prev + 1 and s > prev:
            counts["gap"] += (s - prev - 1)
        elif prev and s < prev:
            counts["out_of_order"] += 1
        prev = s
    return counts


def kv_state() -> Dict[str, Any]:
    try:
        return http_json("GET", f"{BASE}/metrics/kv", timeout=5)
    except Exception as e:
        return {"error": str(e)}


class RecordingDriver:
    """每请求唯一 request trace id（trace-<session>-<seq>）+ session trace id。"""

    def __init__(self, driver: Driver, session_id: str) -> None:
        self._d = driver
        self.session_id = session_id
        self.req_counter = 0
        self.requests: List[Dict[str, Any]] = []
        self._orig_chat = driver.chat

    def install(self) -> None:
        self._d.chat = self._chat_wrapper  # type: ignore[method-assign]
        orig_extra = self._d._extra_body
        def extra_with_trace():
            self.req_counter += 1
            b = orig_extra()
            b["trace_id"] = f"trace-{self.session_id}-{self.req_counter:04d}"
            b["session_trace_id"] = f"sess-{self.session_id}"
            return b
        self._d._extra_body = extra_with_trace  # type: ignore[method-assign]

    def _chat_wrapper(self, messages: List[dict], _retry: int = 0) -> Dict[str, Any]:
        r = self._orig_chat(messages, _retry)
        self.requests.append({"messages": messages,
                              "trace_id": f"trace-{self.session_id}-{self.req_counter:04d}",
                              "session_trace_id": f"sess-{self.session_id}",
                              "latency_ms": r.get("latency_ms")})
        return r


def run_session(sid: str, wl_params: Dict[str, Any], rec: Dict, out: Dict) -> None:
    try:
        driver = Driver(base_url=f"{BASE}/v1", host="127.0.0.1", port=PORT,
                        enable_thinking=False)
        rdr = RecordingDriver(driver, sid)
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
    log_path = os.path.join(log_dir, f"e351_{scenario}_{policy}_{rep}.log")
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

    if scenario in ("request_level_mapping", "chain_pressure", "active_protection", "slot_reuse"):
        s0 = session_out.get("S0", {})
        if s0.get("ok") and s0["requests"]:
            last_msgs = s0["requests"][-1]["messages"]
            rec["adapter"].append({"tag": "S0_replay", **raw_chat(last_msgs, "trace-adp-hot-01", "sess-adp-hot")})
            rec["adapter"].append({"tag": "pressure", **raw_chat(
                [{"role": "user", "content": build_prompt(6300, "PRESSURE")}], "trace-adp-pres-02", "sess-adp-pres")})
            rec["adapter"].append({"tag": "revisit_S0", **raw_chat(last_msgs, "trace-adp-rev-03", "sess-adp-rev")})

    rec["trace_events"] = parse_trace_events(log_path)
    rec["integrity"] = integrity_check(rec["trace_events"])
    stop_server(proc)
    rec["server_exited"] = proc.returncode is not None
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["request_level_mapping", "chain_pressure",
                                           "active_protection", "slot_reuse", "malformed_trace",
                                           "diagnostics_off", "non_unified"], required=True)
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
              f"events={len(reps[-1].get('trace_events', []))} "
              f"integrity={reps[-1].get('integrity', {}).get('gap', 'n/a')}")
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
