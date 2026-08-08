#!/usr/bin/env python3
"""E13.3: 默认路径回归结果生成（checkpoint off 默认路径 vs E12 baseline）。"""
import json, os, subprocess, time, hashlib
from urllib import request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BASE = "http://127.0.0.1:8080"

out = {"git_root": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
       "git_llama": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip()}

# 4B 默认请求（checkpoint off）输出 hash
proc = subprocess.Popen(
    [os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server"),
     "-m", os.path.join(ROOT, "models", "qwen3-5-4B-Q4_K_M.gguf"),
     "--host", "127.0.0.1", "--port", "8080", "-ngl", "99", "--ctx-size", "2048",
     "--parallel", "2", "--kv-unified", "--cache-ram", "0", "--temp", "0", "--seed", "42",
     "--metrics", "--log-file", os.path.join(ROOT, "llama.cpp", "tmp", "e13_default2.log")],
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
for _ in range(180):
    try:
        with request.urlopen(f"{BASE}/health", timeout=3) as r:
            if json.loads(r.read().decode()).get("status") == "ok":
                break
    except Exception:
        pass
    time.sleep(1)
req = request.Request(f"{BASE}/completion",
                      data=json.dumps({"prompt": ("You are a monitoring assistant. The river station reports flow data. "
                                                   "Observations: flow 12.4 m3/s, temp 8.2 C. " * 6) + "\nSummarize.",
                                       "n_predict": 8, "temperature": 0, "cache_prompt": True, "id_slot": 0}).encode(),
                      method="POST", headers={"Content-Type": "application/json"})
with request.urlopen(req, timeout=300) as r:
    comp = json.loads(r.read().decode())
h = hashlib.sha256(comp.get("content", "").encode()).hexdigest()
out["default_4b"] = {
    "code": 200,
    "prompt_n": comp.get("timings", {}).get("prompt_n"),
    "hash": h,
    "e12_baseline_hash_match": h.startswith("126357be2c69be7d"),
}
log = open(os.path.join(ROOT, "llama.cpp", "tmp", "e13_default2.log"), encoding="utf-8", errors="replace").read()
out["checkpoint_off_no_save_restore"] = log.count("E11-CKPT") == 0
proc.terminate()
try:
    proc.wait(timeout=10)
except Exception:
    proc.kill()

# clone_from 残留
res = subprocess.run(["grep", "-rc", "clone_from\\|SLOT_CLONE",
                       os.path.join(ROOT, "llama.cpp", "tools", "server")],
                      capture_output=True, text=True)
out["clone_from_residue"] = res.stdout.strip() or "0 (no match)"
out["clone_from_clean"] = res.returncode != 0  # grep 无匹配 → 0 残留

with open(os.path.join(ROOT, "benchmark", "results", "kv_optimization", "raw", "e13_default_path_regression.json"), "w") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(json.dumps(out, ensure_ascii=False, indent=1))
