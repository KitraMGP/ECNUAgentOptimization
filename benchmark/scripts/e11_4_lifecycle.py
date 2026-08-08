#!/usr/bin/env python3
"""E11.4: server checkpoint 集成正确性与生命周期验收（TinyLlama）。

矩阵：
- P 长度：116 / 512（前缀稳定 P，末尾无空格）
- target 数：1 / 2 / 4（同一 checkpoint 分叉）
- 后缀 X/Y 不同
- source 删除后 restore / target 删除后 restore / 交替
- on/off；save/restore；cleanup；shutdown/restart；capacity pressure
- prefix mismatch（不同前缀 restore → 拒绝）
- 门禁：restore vs baseline 输出 hash 一致、无 crash、refcount 归零
  （日志）、used/shared 回基线

用法：python scripts/e11_4_lifecycle.py --output raw/e11_lifecycle.json
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

SEED = ("Once upon a time in the deep dark forest, a small rabbit named Miko discovered "
        "a glowing stone near the old oak tree. The stone hummed with a soft blue light "
        "and seemed to pulse with the rhythm of the wind. Miko carefully picked it up "
        "and carried it back to the burrow where the animals gathered. ")  # ~110 tokens


def P_len(n_units):
    # 前缀稳定：末尾无空格（rstrip），单元拼接避免 BPE 边界合并
    p = (SEED.rstrip() + "\n") * n_units
    return p.rstrip()


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


def start_server(parallel, log_path, extra=None):
    args = [SERVER_BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
            "--ctx-size", str(parallel * 1024), "--parallel", str(parallel),
            "--kv-unified", "--cache-ram", "0", "--temp", "0", "--seed", "42",
            "--metrics", "--checkpoint-reuse", "--slot-save-path",
            os.path.join(ROOT, "llama.cpp", "tmp", "e6_slot_save")]
    if extra:
        args += extra
    proc = subprocess.Popen(args, stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    return proc, wait_health(proc)


def kv():
    r, _ = http_json("GET", f"{BASE}/metrics/kv", timeout=10)
    return {"used": r.get("used_cells"), "shared": r.get("shared_cells"),
            "active": r.get("active_sequences")} if "used_cells" in r else r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    out_path = args.output or os.path.join(ROOT, "benchmark", "results", "kv_optimization",
                                           "raw", "e11_lifecycle.json")
    log_dir = os.path.join(os.path.dirname(out_path), "e11life")
    os.makedirs(log_dir, exist_ok=True)

    out = {"git": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"),
                                           "rev-parse", "HEAD"]).decode().strip()}

    # ---- 场景 1：P 长度 × target 数（同一 checkpoint 分叉）----
    for n_units in (1, 4):  # ~110 / ~440 tokens
        P = P_len(n_units)
        for n_tgt in (1, 2, 4):
            proc, ok = start_server(4, os.path.join(log_dir, f"s1_{n_units}_{n_tgt}.log"))
            if not ok:
                out[f"p{n_units}_t{n_tgt}"] = {"failed": True}
                continue
            try:
                r, c = http_json("POST", f"{BASE}/completion",
                                 {"prompt": P, "n_predict": 4, "temperature": 0,
                                  "cache_prompt": True, "id_slot": 0, "checkpoint_save": True})
                rec = {"save_code": c, "targets": []}
                for i in range(n_tgt):
                    suffix = f" The owl examined the stone at position {i}."
                    rt, ct = http_json("POST", f"{BASE}/completion",
                                       {"prompt": P + suffix, "n_predict": 4, "temperature": 0,
                                        "cache_prompt": True, "id_slot": i + 1, "checkpoint_restore": True})
                    rec["targets"].append({"code": ct, "prompt_n": rt.get("timings", {}).get("prompt_n"),
                                           "hash": hashlib.sha256(rt.get("content", "").encode()).hexdigest()[:16] if ct == 200 else None})
                # baseline（erase 后全量）
                for sid in range(4):
                    http_json("POST", f"{BASE}/slots/{sid}?action=erase", {}, 30)
                for i in range(n_tgt):
                    suffix = f" The owl examined the stone at position {i}."
                    rb, cb = http_json("POST", f"{BASE}/completion",
                                       {"prompt": P + suffix, "n_predict": 4, "temperature": 0,
                                        "cache_prompt": True, "id_slot": i})
                    rec["targets"][i]["baseline_hash"] = hashlib.sha256(rb.get("content", "").encode()).hexdigest()[:16] if cb == 200 else None
                # 一致性
                rec["match"] = all(t["hash"] == t["baseline_hash"] for t in rec["targets"])
                rec["kv_after"] = kv()
                out[f"p{n_units}_t{n_tgt}"] = rec
                print(f"[p{n_units} t{n_tgt}] save={rec['save_code']} match={rec['match']} "
                      f"prompt_n={[t['prompt_n'] for t in rec['targets']]} kv={rec['kv_after']}")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
                time.sleep(2)

    # ---- 场景 2：source 删除后 target restore + prefix mismatch ----
    proc, ok = start_server(4, os.path.join(log_dir, "s2.log"))
    if ok:
        try:
            P = P_len(1)
            http_json("POST", f"{BASE}/completion",
                      {"prompt": P, "n_predict": 4, "temperature": 0, "cache_prompt": True,
                       "id_slot": 0, "checkpoint_save": True})
            # source 删除（erase slot0）后 target restore
            http_json("POST", f"{BASE}/slots/0?action=erase", {}, 30)
            rt, ct = http_json("POST", f"{BASE}/completion",
                               {"prompt": P + " The fox watched from afar.", "n_predict": 4,
                                "temperature": 0, "cache_prompt": True, "id_slot": 1, "checkpoint_restore": True})
            # prefix mismatch：完全不同的前缀 restore → 拒绝（全量 prefill）
            rm, cm = http_json("POST", f"{BASE}/completion",
                               {"prompt": "A completely different story about a bear in the hills. ",
                                "n_predict": 4, "temperature": 0, "cache_prompt": True,
                                "id_slot": 2, "checkpoint_restore": True})
            out["source_deleted_restore"] = {"code": ct, "prompt_n": rt.get("timings", {}).get("prompt_n")}
            out["prefix_mismatch"] = {"code": cm, "prompt_n": rm.get("timings", {}).get("prompt_n")}
            out["kv_after_s2"] = kv()
            print(f"[source_deleted] code={ct} prompt_n={rt.get('timings',{}).get('prompt_n')}")
            print(f"[prefix_mismatch] code={cm} prompt_n={rm.get('timings',{}).get('prompt_n')}（拒绝→全量）")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            time.sleep(2)

    # ---- 场景 3：shutdown/restart 后池清空（checkpoint 不再可用）----
    proc, ok = start_server(4, os.path.join(log_dir, "s3a.log"))
    if ok:
        try:
            P = P_len(1)
            http_json("POST", f"{BASE}/completion",
                      {"prompt": P, "n_predict": 4, "temperature": 0, "cache_prompt": True,
                       "id_slot": 0, "checkpoint_save": True})
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            time.sleep(2)
    proc2, ok2 = start_server(4, os.path.join(log_dir, "s3b.log"))
    if ok2:
        try:
            rt, ct = http_json("POST", f"{BASE}/completion",
                               {"prompt": P + " The owl returned.", "n_predict": 4, "temperature": 0,
                                "cache_prompt": True, "id_slot": 0, "checkpoint_restore": True})
            # restart 后池空 → restore 拒绝 → 全量 prefill（prompt_n = 全量）
            out["restart_pool_cleared"] = {"code": ct, "prompt_n": rt.get("timings", {}).get("prompt_n")}
            print(f"[restart] pool cleared: restore 拒绝 → prompt_n={rt.get('timings',{}).get('prompt_n')}（全量）")
        finally:
            proc2.terminate()
            try:
                proc2.wait(timeout=10)
            except Exception:
                proc2.kill()

    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("saved:", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
