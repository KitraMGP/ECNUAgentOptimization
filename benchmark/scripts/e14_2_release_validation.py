#!/usr/bin/env python3
"""E14.2: 发布前验收（最终二进制，15 项）。

1 启动+健康 / 2 q8_0 生效 / 3 per-cell / 4 short QA / 5 tool JSON parse /
6 system retention / 7 canary / 8 multi-turn / 9 long ~1000 tokens /
10 parallel=4 并发 / 11 graceful shutdown / 12 异常+超长受控拒绝 /
13 checkpoint off 日志 0 / 14 clone_from 不存在或拒绝 / 15 重启健康恢复
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from urllib import request, error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BASE = "http://127.0.0.1:8080"
BIN = os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server")
MODEL = os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf")
LOG = os.path.join(ROOT, "llama.cpp", "tmp", "e14_rc.log")

ARGS = [BIN, "-m", MODEL, "--host", "127.0.0.1", "--port", "8080", "-ngl", "99",
        "--ctx-size", "4096", "--parallel", "4", "--kv-unified",
        "--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "--metrics",
        "--log-file", LOG]


def wait_health(t=180):
    for _ in range(t):
        try:
            with request.urlopen(f"{BASE}/health", timeout=3) as r:
                if json.loads(r.read().decode()).get("status") == "ok":
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def post(path, body, t=300):
    req = request.Request(BASE + path, data=json.dumps(body).encode(),
                          method="POST", headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=t) as r:
            return json.loads(r.read().decode()), r.status
    except error.HTTPError as e:
        return json.loads(e.read().decode()), e.code
    except Exception as e:
        return {"err": str(e)}, 0


def start():
    return subprocess.Popen(ARGS, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def stop(proc, sig=signal.SIGTERM):
    proc.send_signal(sig)
    try:
        proc.wait(timeout=15)
    except Exception:
        proc.kill()
    return proc.returncode


def main() -> int:
    out = {"git": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"),
                                           "rev-parse", "HEAD"]).decode().strip()}

    proc = start()
    out["1_health"] = wait_health()
    print("1. 启动+健康:", out["1_health"])

    with request.urlopen(f"{BASE}/metrics/kv", timeout=10) as r:
        kv = json.loads(r.read().decode())
    out["2_3_per_cell"] = kv["capacity_bytes"] // kv["capacity_cells"]
    print("2+3. per-cell:", out["2_3_per_cell"], "B（期望 17408）")

    def req(name, prompt, n=8):
        r, c = post("/completion", {"prompt": prompt, "n_predict": n, "temperature": 0,
                                    "cache_prompt": True, "id_slot": 0})
        return {"code": c, "hash": hashlib.sha256(r.get("content", "").encode()).hexdigest()[:16] if c == 200 else None,
                "prompt_n": r.get("timings", {}).get("prompt_n") if c == 200 else None, "err": r.get("err", "")[:80]}

    out["4_short_qa"] = req("short_qa", "What is the capital of France? Answer briefly.")
    print("4. short QA:", out["4_short_qa"]["code"])

    # tool JSON：用 chat/completions + tools（OpenAI 兼容，触发真实工具调用）
    tool_body = {
        "model": "qwen3-5-4b",
        "messages": [{"role": "user", "content": "What is the weather in Berlin? Use the tool."}],
        "tools": [{"type": "function",
                   "function": {"name": "get_weather", "description": "Get weather for a city",
                                "parameters": {"type": "object",
                                               "properties": {"city": {"type": "string"}},
                                               "required": ["city"]}}}],
        "temperature": 0,
    }
    tool_r, tool_c = post("/v1/chat/completions", tool_body)
    content = ""
    tool_calls = None
    try:
        msg = tool_r["choices"][0]["message"]
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            parse_ok = json.loads(tool_calls[0]["function"]["arguments"]) is not None
        else:
            parse_ok = False
    except Exception:
        parse_ok = False
    out["5_tool_json"] = {"code": tool_c, "parse_ok": parse_ok,
                          "has_tool_calls": bool(tool_calls),
                          "content": content[:60]}
    print("5. tool JSON parse:", parse_ok, "| tool_calls:", bool(tool_calls))

    out["6_system_retention"] = req("retention", "System: secret code 7391-XQZ.\nUser: repeat it.")
    print("6. system retention:", out["6_system_retention"]["code"])

    canary_r, canary_c = post("/completion", {"prompt": "Secret: CANARY-ALPHA-3947.\nWhat should I remember?",
                                              "n_predict": 8, "temperature": 0})
    leak = "CANARY-ALPHA-3947" in canary_r.get("content", "")
    out["7_canary"] = {"code": canary_c, "leak": leak}
    print("7. canary 泄漏:", leak)

    mt = []
    ctx_text = ""
    for turn in ["User: record flow 12.4.\nAssistant: ", "User: add temp 8.2.\nAssistant: "]:
        r, c = post("/completion", {"prompt": ctx_text + turn, "n_predict": 4, "temperature": 0})
        mt.append(c)
        if c == 200:
            ctx_text += turn + r.get("content", "")
    out["8_multi_turn"] = mt
    print("8. multi-turn:", mt)

    long_p = ("You are a long-context assistant. The following is a log of observations. "
              "flow 12.4 temp 8.2; flow 12.6 temp 8.1; flow 12.9 temp 8.3; flow 13.1 temp 8.0; " * 12)
    out["9_long_prompt"] = req("long", long_p + "\nSummarize.", n=8)
    print("9. long ~1000 tokens:", out["9_long_prompt"]["code"])

    # 10. parallel=4 并发
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(post, "/completion", {"prompt": f"Q{i}: summarize in one line.", "n_predict": 4,
                                                "temperature": 0, "id_slot": i}) for i in range(4)]
        results = [f.result() for f in futs]
    out["10_parallel4"] = [c for _, c in results]
    print("10. parallel=4 并发:", out["10_parallel4"])

    # 12. 异常请求 + 超长受控拒绝（server 运行期间）
    bad_r, bad_c = post("/completion", {"prompt": "", "n_predict": 4})
    out["12a_empty_prompt"] = bad_c
    long_req = "x" * 20000  # 超长（超 ctx）
    over_r, over_c = post("/completion", {"prompt": long_req, "n_predict": 4})
    out["12b_oversized"] = over_c
    out["12b_prompt_n"] = over_r.get("timings", {}).get("prompt_n") if over_c == 200 else None
    print("12. 空 prompt:", bad_c, "| 超长:", over_c, "| prompt_n:", out["12b_prompt_n"])

    # 11. graceful shutdown（SIGTERM，exit 0）
    rc = stop(proc)
    out["11_graceful_shutdown"] = rc
    print("11. graceful shutdown exit:", rc)

    # 13-15. 重启（checkpoint off 日志 0、clone_from 拒绝、健康恢复）
    proc2 = start()
    out["15_restart_health"] = wait_health()
    log = open(LOG, encoding="utf-8", errors="replace").read()
    out["13_checkpoint_log_count"] = log.count("E11-CKPT")
    try:
        req = request.Request(f"{BASE}/slots/0?action=clone_from",
                              data=json.dumps({"source_id": 1}).encode(),
                              method="POST", headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=10) as r:
            out["14_clone_from"] = r.status
    except error.HTTPError as e:
        out["14_clone_from"] = e.code
    except Exception as e:
        out["14_clone_from"] = str(e)[:40]
    print("13. checkpoint 日志:", out["13_checkpoint_log_count"], "| 14. clone_from:", out["14_clone_from"],
          "| 15. 重启健康:", out["15_restart_health"])
    stop(proc2)

    with open(os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw",
                           "e14_release_candidate_validation.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("saved e14_release_candidate_validation.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
