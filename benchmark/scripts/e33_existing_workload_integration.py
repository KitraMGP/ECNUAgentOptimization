#!/usr/bin/env python3
"""E3.3：A1/A4 在既有 workload 上的集成验证 runner（server-per-replicate）。

复用 benchmark/workload/ 的既有 workload（get_workload + generate + run），
不修改 workload 文件。adapter 变体（仅 benchmark 侧组合请求）：
- baseline：纯 workload.run（无压力场景：lru=default 等价 + 成本验证）
- pressure：workload 跑完 → 压力请求（新前缀长 prompt，新 slot）→ 观察 purge
  → 重放 workload 最后请求（观察 processed/回访成本）
- integration_pressure：workload（seq0）→ 重放最后请求到 slot1（冷）与
  slot0（热）→ 压力（slot2）→ purge（lru 应清冷分支）→ 回访热分支

用法：uv run python scripts/e33_existing_workload_integration.py
  --workload multi_turn|long_life|branch|branch_pressure
  --variant baseline|pressure|integration_pressure --policy default|lru
  --parallel N --reps 1 --output results/e33/xxx.json
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
import workload  # noqa: F401  （导入触发 @register 注册）
import workload.branch  # noqa: F401
import workload.branch_pressure  # noqa: F401
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
    chars = int(n_tokens * 5.66)  # PRESSURE_TEXT 实测 ~5.66 chars/token（6.2 高估导致实际 6905）
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


def raw_chat(messages: List[dict], slot: Optional[int] = None,
             max_tokens: int = 16) -> Dict[str, Any]:
    """裸 /v1/chat/completions（adapter 用，可显式 id_slot）。"""
    body: Dict[str, Any] = {"model": "bench", "messages": messages,
                            "max_tokens": max_tokens, "temperature": 0,
                            "chat_template_kwargs": {"enable_thinking": False}}
    if slot is not None:
        body["id_slot"] = slot
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
        "response_hash": hashlib.sha256(re.sub(r"[^\w]+", "", text.lower()).encode()).hexdigest()[:12] if text else None,
        "text": text[:50],
        "error": d.get("error"),
        "slot_id": slot,
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


def start_server(policy: str, parallel: int, ctx: int, unified: bool,
                 log_path: str, cache_ram: int = 0) -> subprocess.Popen:
    os.makedirs(SLOT_SAVE, exist_ok=True)
    cmd = [SERVER, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
           "-ngl", "99", "--ctx-size", str(ctx), "--parallel", str(parallel),
           "--cache-reuse", "0", "--seed", "42", "--temp", "0",
           "--cache-ram", str(cache_ram), "--slot-save-path", SLOT_SAVE,
           "--unified-idle-slot-policy", policy, "--lifecycle-stats"]
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
    """包装 Driver.chat，记录每请求 messages/result + /metrics/kv 快照。"""

    def __init__(self, driver: Driver) -> None:
        self._d = driver
        self.requests: List[Dict[str, Any]] = []
        self._orig_chat = driver.chat

    def install(self) -> None:
        self._d.chat = self._chat_wrapper  # type: ignore[method-assign]

    def _chat_wrapper(self, messages: List[dict], _retry: int = 0) -> Dict[str, Any]:
        kv_before = kv_state()
        r = self._orig_chat(messages, _retry)
        kv_after = kv_state()
        rec = {
            "messages": messages,
            "text": r.get("text", ""),
            "prompt_tokens": r.get("prompt_tokens"),
            "cached_tokens": r.get("cached_tokens"),
            "prompt_processed_tokens": (r.get("prompt_tokens") or 0) - (r.get("cached_tokens") or 0),
            "latency_ms": r.get("latency_ms"),
            "used_cells_before": kv_before.get("used_cells"),
            "used_cells_after": kv_after.get("used_cells"),
            "active_sequences_after": kv_after.get("active_sequences"),
            "response_hash": hashlib.sha256(re.sub(r"[^\w\u4e00-\u9fff]+", "",
                                                   (r.get("text") or "").lower()).encode()).hexdigest()[:12],
            "evaluator_pass": bool((r.get("text") or "").strip()),
            "request_failure": False,
        }
        self.requests.append(rec)
        return r


def run_replicate(workload_name: str, variant: str, policy: str, parallel: int,
                  unified: bool, ctx: int, log_dir: str, rep: int,
                  wl_params: Dict[str, Any], cache_ram: int = 0) -> Dict[str, Any]:
    log_path = os.path.join(log_dir, f"e33_{workload_name}_{variant}_{policy}_{rep}.log")
    proc = start_server(policy, parallel, ctx, unified, log_path, cache_ram)
    rec: Dict[str, Any] = {
        "workload": workload_name, "variant": variant, "policy": policy,
        "parallel": parallel, "unified": unified, "ctx": ctx, "rep": rep,
        "server_pid": proc.pid, "clean_verified": False, "requests": [],
        "adapter_requests": [], "evaluate": {}, "server_exited": False,
    }
    if not wait_health(proc):
        rec["startup_failure"] = True
        stop_server(proc)
        return rec
    kv = kv_state()
    rec["clean_verified"] = kv.get("used_cells") == 0 and kv.get("active_sequences") == 0
    rec["capacity_cells"] = kv.get("capacity_cells")

    driver = Driver(base_url=f"{BASE}/v1", host="127.0.0.1", port=PORT,
                    enable_thinking=False)
    rdr = RecordingDriver(driver)
    rdr.install()
    wl = get_workload(workload_name)
    spec = wl.generate(dict(wl_params))
    rec["workload_fingerprint"] = spec.fingerprint()
    result = wl.run(driver, spec)
    rec["requests"] = rdr.requests
    rec["evaluate"] = wl.evaluate(result, spec)

    if variant == "pressure":
        # 压力请求（新前缀 + 新 slot → 全量写入 → 池竞争 workload 的 idle seq）
        pres = raw_chat([{"role": "user", "content": build_pressure_prompt(6500)}],
                        slot=parallel - 1)
        rec["adapter_requests"].append({"tag": "pressure", **pres})
        # 回访 workload 最后请求（id_slot=0 原会话）
        last_msgs = rdr.requests[-1]["messages"]
        revisit = raw_chat(last_msgs, slot=0)
        rec["adapter_requests"].append({"tag": "revisit_last", **revisit})
    elif variant == "integration_pressure":
        # 冷分支：重放最后请求到 slot1；热分支：重放最后请求到 slot0（tick 更新）
        last_msgs = rdr.requests[-1]["messages"]
        rec["adapter_requests"].append({"tag": "cold_slot1", **raw_chat(last_msgs, slot=1)})
        rec["adapter_requests"].append({"tag": "hot_slot0", **raw_chat(last_msgs, slot=0)})
        # 压力（slot2/3 新 seq）
        pres_slot = 2 if parallel > 2 else 1
        rec["adapter_requests"].append({"tag": "pressure",
                                        **raw_chat([{"role": "user", "content": build_pressure_prompt(6500)}],
                                                   slot=pres_slot)})
        # 回访热分支（slot0）
        rec["adapter_requests"].append({"tag": "revisit_hot",
                                        **raw_chat(last_msgs, slot=0)})

    rec["log_events"] = grep_log(log_path)
    stop_server(proc)
    rec["server_exited"] = proc.returncode is not None
    return rec


WL_PARAMS = {
    "multi_turn": {"rounds": 16},
    "long_life": {"rounds": 10, "secret": "9527", "ctx_size": 8192},
    "branch": {"branch_rounds": 3},
    "branch_pressure": {},
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workload", choices=list(WL_PARAMS), required=True)
    ap.add_argument("--variant", choices=["baseline", "pressure", "integration_pressure"], required=True)
    ap.add_argument("--policy", choices=["default", "lru"], required=True)
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--unified", action="store_true")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--cache-ram", type=int, default=0)
    ap.add_argument("--log-dir", default="/tmp")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    os.makedirs(args.log_dir, exist_ok=True)
    reps = []
    for i in range(args.reps):
        reps.append(run_replicate(args.workload, args.variant, args.policy,
                                  args.parallel, args.unified, args.ctx,
                                  args.log_dir, i, dict(WL_PARAMS[args.workload]),
                                  cache_ram=args.cache_ram))
        print(f"  [rep{i}] clean={reps[-1]['clean_verified']} "
              f"purge={reps[-1].get('log_events', {}).get('purge_events', [])} "
              f"fails={sum(1 for q in reps[-1]['requests'] if q.get('request_failure'))}")
    out = {
        "workload": args.workload, "variant": args.variant, "policy": args.policy,
        "parallel": args.parallel, "unified": args.unified, "ctx": args.ctx,
        "reps": reps, "cache_ram_mib": args.cache_ram, "binary_sha256": sha256(SERVER),
        "model_sha256": sha256(MODEL),
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
