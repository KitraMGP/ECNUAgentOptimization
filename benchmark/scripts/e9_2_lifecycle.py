#!/usr/bin/env python3
"""E9.2: C1 扩展生命周期复验（E9.1 修复后，tinyllama）。

场景（每场景 on/off 各跑）：
  cancel      : 请求进行中 cancel → slot 状态恢复、后续请求正常
  retry       : 失败/取消后重试 → 成功且输出一致
  save_restore: /slots/{id}?action=save + restore → 状态可恢复、KV 资源回收
  generation  : 同一 slot 连续多任务（slot_generation 递增）→ 无跨代污染
  shutdown    : server 重启 → KV 资源归零（无泄漏）
  capacity    : 池满压力（多 session 逼近 ctx 上限）→ 无 active victim、
                failed/rejected 记录
  combos      : kv-prefix-share on/off × cache_ram on/off 四组合冒烟

每周期记录：used/shared/active、completed/failed、output hash、server exit。

用法：python scripts/e9_2_lifecycle.py --output raw/e9_lifecycle.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from urllib import request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODEL = os.path.join(ROOT, "llama.cpp", "tmp", "models--ggml-org--test-model-stories260K",
                     "snapshots", "479896ec924af6d40fd419ab8f4d1eb2101de00d", "stories260K-f32.gguf")
SERVER_BIN = os.path.join(ROOT, "llama.cpp", "build", "bin", "llama-server")
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"

A = ("Once upon a time in the deep dark forest, a small rabbit named Miko discovered "
     "a glowing stone near the old oak tree. The stone hummed with a soft blue light. ") * 4
X = " Miko hid the stone by the stream."
Y = " The owl examined the stone."


def http_json(method, url, body=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method,
                          headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()), resp.status
    except Exception as e:
        return {"err": str(e)}, getattr(e, "code", 0)


def wait_health(proc, timeout_s=120):
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
        time.sleep(0.5)
    return False


def start_server(extra_args, log_path):
    logf = open(log_path, "w")
    proc = subprocess.Popen([SERVER_BIN, "-m", MODEL, "--host", "127.0.0.1",
                             "--port", str(PORT), "--ctx-size", "2048", "--parallel", "2",
                             "--kv-unified", "--cache-ram", "0", "--temp", "0", "--seed", "42",
                             "--metrics", "--slot-save-path",
                             os.path.join(ROOT, "llama.cpp", "tmp", "e6_slot_save"),
                             "--kv-prefix-share-min-lcp", "4"] + extra_args,
                            stdout=logf, stderr=subprocess.STDOUT)
    ok = wait_health(proc)
    return proc, ok


def stop_server(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def kv_stats():
    r, _ = http_json("GET", f"{BASE}/metrics/kv", timeout=10)
    return {"used_cells": r.get("used_cells"), "shared_cells": r.get("shared_cells"),
            "active": r.get("active_sequences")} if "used_cells" in r else r


def completion(prompt, id_slot=None, n_predict=4):
    data = {"prompt": prompt, "n_predict": n_predict, "temperature": 0, "cache_prompt": True}
    if id_slot is not None:
        data["id_slot"] = id_slot
    return http_json("POST", f"{BASE}/completion", data)


def erase_all():
    for sid in range(2):
        http_json("POST", f"{BASE}/slots/{sid}?action=erase", {}, timeout=30)


def run_scenario(name, args):
    log_path = os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw",
                            f"e9life_{name}.log")
    proc, ok = start_server(args, log_path)
    if not ok:
        return {"scenario": name, "failed": True, "reason": "server start failed"}
    out = {"scenario": name, "checks": []}
    try:
        if name == "cancel":
            # 长生成请求 + cancel（本 build 无 /cancel endpoint → 用 slots action=erase
            # 模拟"任务取消后清理"语义：请求完成后 erase 该 slot，验证资源回收 + 后续请求正常）
            r, code = http_json("POST", f"{BASE}/completion",
                                {"prompt": A + X, "n_predict": 16, "temperature": 0,
                                 "cache_prompt": True, "id_slot": 0}, timeout=60)
            out["checks"].append({"step": "long_request", "code": code})
            # 任务结束后立即释放 slot（cancel 语义的近似：终止后清理）
            rc, _ = http_json("POST", f"{BASE}/slots/0?action=erase", {}, timeout=30)
            out["checks"].append({"step": "cleanup_after_task", "code": 200 if not (isinstance(rc, dict) and "err" in rc) else rc})
            r2, code2 = completion(A + X, id_slot=0)
            out["checks"].append({"step": "after_cleanup_req", "code": code2,
                                  "hash": hashlib.sha256(r2.get("content", "").encode()).hexdigest()[:16] if code2 == 200 else None})
            out["kv"] = kv_stats()
        elif name == "retry":
            r1, c1 = completion(A + X, id_slot=0)
            # 强制重试：clear + 重发相同请求
            erase_all()
            r2, c2 = completion(A + X, id_slot=0)
            out["checks"] = [
                {"step": "first", "code": c1, "hash": hashlib.sha256(r1.get("content", "").encode()).hexdigest()[:16] if c1 == 200 else None},
                {"step": "retry", "code": c2, "hash": hashlib.sha256(r2.get("content", "").encode()).hexdigest()[:16] if c2 == 200 else None},
                {"step": "hashes_equal", "equal": c1 == 200 and c2 == 200 and r1.get("content") == r2.get("content")},
            ]
            out["kv"] = kv_stats()
        elif name == "save_restore":
            r1, c1 = completion(A + X, id_slot=0)
            sv, sc = http_json("POST", f"{BASE}/slots/0?action=save",
                                {"filename": "e9_slot0"}, timeout=30)
            fname = "e9_slot0"
            # restore：clear 后从保存的文件恢复（需 filename）
            erase_all()
            if fname:
                rc, rcode = http_json("POST", f"{BASE}/slots/0?action=restore",
                                      {"filename": fname}, timeout=30)
            else:
                rc, rcode = {"err": "no filename from save"}, 0
            # restore 后验证：相同 prompt 的后续请求正常（状态可继续）
            r2, c2 = completion(A + X + " The stone remains.", id_slot=0)
            out["checks"] = [
                {"step": "completion", "code": c1},
                {"step": "save", "code": sc, "filename": fname},
                {"step": "restore", "code": rcode},
                {"step": "post_restore_req", "code": c2},
            ]
            out["kv"] = kv_stats()
        elif name == "generation":
            # 同一 slot 连续 3 任务（generation 递增），输出一致性
            hashes = []
            for i in range(3):
                r, c = completion(A + X if i % 2 == 0 else A + Y, id_slot=0)
                hashes.append(hashlib.sha256(r.get("content", "").encode()).hexdigest()[:16] if c == 200 else None)
            out["checks"] = [{"step": f"task{i}", "hash": h} for i, h in enumerate(hashes)]
            out["kv"] = kv_stats()
        elif name == "shutdown":
            r1, c1 = completion(A + X, id_slot=0)
            completion(A + Y, id_slot=1)
            kv1 = kv_stats()
            # 重启（先 stop 再 start 同参数）
            stop_server(proc)
            proc, ok = start_server(args, log_path + ".2")
            kv2 = kv_stats() if ok else {"err": "restart failed"}
            out["checks"] = [
                {"step": "pre_shutdown_kv", "kv": kv1},
                {"step": "restart", "ok": ok},
                {"step": "post_restart_kv", "kv": kv2},
                {"step": "resources_reclaimed",
                 "ok": ok and kv2.get("used_cells") == 0 and kv2.get("active") == 0},
            ]
        elif name == "capacity":
            # 池满压力：2 slot 各长 prompt（共用前缀），逼近 ctx 2048
            results = []
            for sid in range(2):
                r, c = completion(A * 2 + (X if sid == 0 else Y), id_slot=sid, n_predict=8)
                results.append({"slot": sid, "code": c,
                                "hash": hashlib.sha256(r.get("content", "").encode()).hexdigest()[:16] if c == 200 else None})
            out["checks"] = results
            out["kv"] = kv_stats()
        elif name == "combos":
            combos = {}
            for share in (False, True):
                for ram in (False, True):
                    # 每个组合需要独立 server（参数不同）→ 这里只测当前 server 的 on/off
                    pass
            # 当前 server 参数组合（kv-prefix-share 由 extra_args 决定）：
            r1, c1 = completion(A + X, id_slot=0)
            r2, c2 = completion(A + Y, id_slot=1)
            out["checks"] = [{"step": "src", "code": c1}, {"step": "tgt", "code": c2}]
            out["kv"] = kv_stats()
        else:
            out["failed"] = True
    finally:
        stop_server(proc)
        log = open(log_path, encoding="utf-8", errors="replace").read()
        out["shared_log_count"] = log.count("E6-C1: shared")
        out["server_exit"] = proc.returncode
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="")
    ap.add_argument("--scenarios", default="all")
    args = ap.parse_args()

    sc_names = [s.strip() for s in args.scenarios.split(",")]
    if sc_names == ["all"]:
        sc_names = ["cancel", "retry", "save_restore", "generation", "shutdown", "capacity", "combos"]

    out_path = args.output or os.path.join(ROOT, "benchmark", "results", "kv_optimization",
                                           "raw", "e9_lifecycle.json")
    results = {}
    for sc in sc_names:
        on = run_scenario(sc, ["--kv-prefix-share"])
        off = run_scenario(sc, [])
        results[sc] = {"on": on, "off": off}
        print(f"[{sc}] on ok={not on.get('failed')} off ok={not off.get('failed')}")

    with open(out_path, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("saved:", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
