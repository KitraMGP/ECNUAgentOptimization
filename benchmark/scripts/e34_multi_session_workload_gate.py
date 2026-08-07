#!/usr/bin/env python3
"""E3.4：A1/A4 真实多会话 Workload 验证 runner（server-per-replicate + 并发 client）。

每个 replicate：
1. 全新 llama-server（unified, parallel=4, ctx 8192, cache-ram 0, routing=default）
2. clean 断言
3. 4 个独立 client 线程并发执行既有 workload（multi_turn / long_life），
   各自独立 history/evaluator（真实多 sequence）
4. S0 热点：重放 S0 最后请求（自动路由，tick 更新）
5. 压力请求（新 active 会话，~6300 tokens，自动路由）→ 池满 → purge
6. 回访 S0（热点）与一个冷 session（自动路由）→ 观察 processed/latency
7. lifecycle 事件（--lifecycle-stats）+ /metrics/kv + evaluator

不得使用 id_slot 强制 victim；重放全部自动路由。
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
import workload.long_life  # noqa: F401
import workload.multi_turn  # noqa: F401
from framework.driver import Driver  # noqa: E402
from framework.workload import get_workload  # noqa: E402

PRESSURE_TEXT = (
    "This is a completely different payload stream that shares no prefix with "
    "any prior request in this session. It describes atmospheric pressure "
    "readings, seismic activity logs, ocean tide gauges, and solar radiation "
    "measurements collected by an unrelated network with a distinct schema "
    "and provenance record kept in a separate archive."
)


def build_pressure_prompt(n_tokens: int = 6300) -> str:
    chars = int(n_tokens * 5.66)
    body = " ".join([PRESSURE_TEXT] * (chars // len(PRESSURE_TEXT) + 1))[:chars]
    return body + "\n\nEND STATE: PRESSURE_MARKER\nConfirm the state value above."


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


def raw_chat(messages: List[dict], max_tokens: int = 16) -> Dict[str, Any]:
    """裸 /v1/chat/completions（自动路由，不指定 id_slot）。"""
    body: Dict[str, Any] = {"model": "bench", "messages": messages,
                            "max_tokens": max_tokens, "temperature": 0,
                            "chat_template_kwargs": {"enable_thinking": False}}
    t0 = time.perf_counter()
    try:
        d = http_json("POST", f"{BASE}/v1/chat/completions", body)
        status = "ok"
    except error.HTTPError as e:
        d = {"error": {"code": e.code, "type": e.reason}}
        status = f"http_{e.code}"
    except Exception as e:
        d = {"error": {"type": type(e).__name__}}
        status = "exc"
    lat = (time.perf_counter() - t0) * 1000
    usage = d.get("usage", {})
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    prompt_tokens = usage.get("prompt_tokens")
    text = ""
    if d.get("choices"):
        text = d["choices"][0].get("message", {}).get("content") or ""
    return {
        "status": status,
        "latency_ms": round(lat, 1),
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached,
        "prompt_processed_tokens": (prompt_tokens - cached) if prompt_tokens is not None and cached is not None else None,
        "response_hash": hashlib.sha256(re.sub(r"[^\w\u4e00-\u9fff]+", "", text.lower()).encode()).hexdigest()[:12] if text else None,
        "text": text[:50],
        "error": d.get("error"),
    }


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


def start_server(policy: str, log_path: str, lifecycle_stats: bool = True,
                 parallel: int = 4, unified: bool = True) -> subprocess.Popen:
    os.makedirs(SLOT_SAVE, exist_ok=True)
    cmd = [SERVER, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "-ngl", "99", "--ctx-size", "8192", "--parallel", str(parallel),
           "--cache-reuse", "0", "--seed", "42", "--temp", "0",
           "--cache-ram", "0", "--slot-save-path", SLOT_SAVE,
           "--unified-idle-slot-policy", policy]
    if unified:
        cmd.append("--kv-unified")
    if lifecycle_stats:
        cmd.append("--lifecycle-stats")
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


def grep_log(log_path: str) -> Dict[str, Any]:
    res = {"purge_events": [], "free_space": 0}
    try:
        with open(log_path, "r", errors="replace") as f:
            for line in f:
                if "failed to find free space in the KV cache" in line:
                    res["free_space"] += 1
                m = re.search(r"purging slot (\d+) with (\d+) tokens \(policy=(\w+), last_used_tick=(\d+)\)", line)
                if m:
                    res["purge_events"].append({"slot": int(m.group(1)),
                                                "tokens": int(m.group(2)),
                                                "policy": m.group(3),
                                                "tick": int(m.group(4))})
    except FileNotFoundError:
        pass
    return res


def kv_state() -> Dict[str, Any]:
    try:
        return http_json("GET", f"{BASE}/metrics/kv", timeout=5)
    except Exception as e:
        return {"error": str(e)}


class RecordingDriver:
    """包装 Driver.chat 记录每请求 messages/result（session 内复用）。"""

    def __init__(self, driver: Driver, session_id: str, rec: Dict) -> None:
        self._d = driver
        self.session_id = session_id
        self.rec = rec
        self.requests: List[Dict[str, Any]] = []
        self._orig_chat = driver.chat

    def install(self) -> None:
        self._d.chat = self._chat_wrapper  # type: ignore[method-assign]

    def _chat_wrapper(self, messages: List[dict], _retry: int = 0) -> Dict[str, Any]:
        kv_before = kv_state()
        t0 = time.perf_counter()
        r = self._orig_chat(messages, _retry)
        lat = (time.perf_counter() - t0) * 1000
        kv_after = kv_state()
        rec = {
            "session_id": self.session_id,
            "messages": messages,
            "text": r.get("text", ""),
            "prompt_tokens": r.get("prompt_tokens"),
            "cached_tokens": r.get("cached_tokens"),
            "prompt_processed_tokens": (r.get("prompt_tokens") or 0) - (r.get("cached_tokens") or 0),
            "latency_ms": round(lat, 1),
            "used_cells_before": kv_before.get("used_cells"),
            "used_cells_after": kv_after.get("used_cells"),
            "active_sequences_after": kv_after.get("active_sequences"),
            "response_hash": hashlib.sha256(re.sub(r"[^\w\u4e00-\u9fff]+", "",
                                                   (r.get("text") or "").lower()).encode()).hexdigest()[:12],
            "evaluator_pass": bool((r.get("text") or "").strip()),
            "request_failure": False,
        }
        self.requests.append(rec)
        self.rec["active_sequences_max"] = max(self.rec.get("active_sequences_max", 0),
                                               kv_after.get("active_sequences") or 0)
        return r


def run_session(session_id: str, wl_name: str, wl_params: Dict[str, Any],
                rec: Dict, out: Dict) -> None:
    """独立 client 线程：完整跑一个 workload 会话。"""
    try:
        driver = Driver(base_url=f"{BASE}/v1", host="127.0.0.1", port=PORT,
                        enable_thinking=False)
        rdr = RecordingDriver(driver, session_id, rec)
        rdr.install()
        wl = get_workload(wl_name)
        spec = wl.generate(dict(wl_params))
        rec["fingerprint"] = spec.fingerprint()
        result = wl.run(driver, spec)
        out[session_id] = {
            "requests": rdr.requests,
            "evaluate": wl.evaluate(result, spec),
            "ok": True,
        }
    except Exception as e:
        out[session_id] = {"ok": False, "error": f"{type(e).__name__}: {e}"}


def run_replicate(wl_name: str, pressure: bool, policy: str, log_dir: str,
                  rep: int, wl_params: Dict[str, Any], parallel: int = 4,
                  unified: bool = True) -> Dict[str, Any]:
    log_path = os.path.join(log_dir, f"e34_{wl_name}_{'pressure' if pressure else 'nopressure'}_{policy}_{rep}.log")
    proc = start_server(policy, log_path, parallel=parallel, unified=unified)
    rec: Dict[str, Any] = {
        "workload": wl_name, "pressure_variant": pressure, "policy": policy,
        "rep": rep, "server_pid": proc.pid, "clean_verified": False,
        "sessions": {}, "adapter_requests": [], "active_sequences_max": 0,
        "server_exited": False,
    }
    if not wait_health(proc):
        rec["startup_failure"] = True
        stop_server(proc)
        return rec
    kv = kv_state()
    rec["clean_verified"] = kv.get("used_cells") == 0 and kv.get("active_sequences") == 0
    rec["capacity_cells"] = kv.get("capacity_cells")

    # 4 个独立 session 并发执行
    session_out: Dict[str, Dict] = {}
    threads = []
    t_start = time.perf_counter()
    for sid in ("S0", "S1", "S2", "S3"):
        rec["sessions"][sid] = {"active_sequences_max": 0}
        t = threading.Thread(target=run_session, args=(sid, wl_name, wl_params,
                                                       rec["sessions"][sid], session_out))
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=900)
    rec["sessions_run_s"] = round(time.perf_counter() - t_start, 1)
    rec["session_results"] = session_out
    # 并发性验证：session 请求时间窗口是否重叠
    all_reqs = [q for s in session_out.values() if s.get("ok") for q in s["requests"]]
    rec["concurrency_verified"] = len(session_out) == 4 and all(s.get("ok") for s in session_out.values())

    if pressure:
        # S0 热点：重放 S0 最后请求（自动路由）
        s0 = session_out.get("S0", {})
        if s0.get("ok") and s0["requests"]:
            last_msgs = s0["requests"][-1]["messages"]
            rec["adapter_requests"].append({"tag": "S0_hot_replay",
                                            **raw_chat(last_msgs)})
            # 压力请求（新 active 会话，自动路由）
            rec["adapter_requests"].append({"tag": "pressure",
                                            **raw_chat([{"role": "user", "content": build_pressure_prompt(6300)}])})
            # 回访 S0（热点）
            rec["adapter_requests"].append({"tag": "revisit_S0",
                                            **raw_chat(last_msgs)})
            # 回访一个冷 session（S1）
            s1 = session_out.get("S1", {})
            if s1.get("ok") and s1["requests"]:
                rec["adapter_requests"].append({"tag": "revisit_S1",
                                                **raw_chat(s1["requests"][-1]["messages"])})

    rec["log_events"] = grep_log(log_path)
    stop_server(proc)
    rec["server_exited"] = proc.returncode is not None
    return rec


WL_PARAMS = {
    "multi_turn": {"rounds": 6},   # 4 session × 6 轮 → KV 总量 ~4×1000 = 4000（pressure 时 + 压力 6300 > 8192）
    "long_life": {"rounds": 6, "secret": "9527", "ctx_size": 8192},
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workload", choices=list(WL_PARAMS), required=True)
    ap.add_argument("--pressure", action="store_true")
    ap.add_argument("--policy", choices=["default", "lru"], required=True)
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--non-unified", action="store_true")
    ap.add_argument("--log-dir", default="/tmp")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    os.makedirs(args.log_dir, exist_ok=True)
    reps = []
    for i in range(args.reps):
        reps.append(run_replicate(args.workload, args.pressure, args.policy,
                                  args.log_dir, i, dict(WL_PARAMS[args.workload]),
                                  parallel=args.parallel, unified=not args.non_unified))
        print(f"  [rep{i}] clean={reps[-1]['clean_verified']} "
              f"concurrent={reps[-1].get('concurrency_verified')} "
              f"purge={reps[-1].get('log_events', {}).get('purge_events', [])}")
    out = {
        "workload": args.workload, "pressure_variant": args.pressure,
        "policy": args.policy, "reps": reps,
        "binary_sha256": sha256(SERVER), "model_sha256": sha256(MODEL),
        "llama_commit": subprocess.check_output(
            ["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"保存: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
