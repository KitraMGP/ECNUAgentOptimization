"""Metadata —— 实验环境信息采集与 ctx-size 语义检查（E0.6）。

采集内容（对应实现计划 E0.6 §3）：
- model：路径 / sha256 / 大小
- llama.cpp：git commit + dirty 标记（build_info 探测值）
- GPU：名称 / 显存总量 / 驱动版本
- server：base_url / total_slots（parallel）/ 实际 slot n_ctx
- experiment：temperature / seed / repeat / warmup

ctx-size 语义检查（E0.6 §4）：llama-server 的 ``--ctx-size`` 会被
``--parallel`` 平分到每个 slot（如 2048/4=512）。若探测到的 slot n_ctx 与
配置 ctx_size 不一致，打印 warning 并记入结果。

所有探测均容错降级（失败返回 None / 空并附加 warning），不阻塞实验。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import Any, Dict, List, Optional
from urllib import request

from .config import BenchmarkConfig

# 包位置：benchmark/framework/metadata.py → 目录层级
_BENCHMARK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # benchmark/
_PROJECT_ROOT = os.path.dirname(_BENCHMARK_ROOT)                                # workspace 根


# ---- HTTP 探测（不依赖 openai SDK，标准库） ----

def _http_get_json(url: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    try:
        with request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def probe_server(base_url: str) -> Dict[str, Any]:
    """探测 llama-server：/props 与 /slots，返回关键字段（探测失败时字段为空）。"""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    info: Dict[str, Any] = {}
    props = _http_get_json(f"{root}/props")
    if props:
        info["model_path"] = props.get("model_path")
        info["total_slots"] = props.get("total_slots")
        info["build_info"] = props.get("build_info")
        info["chat_template"] = props.get("chat_template")
    slots = _http_get_json(f"{root}/slots")
    if isinstance(slots, list) and slots:
        ctxs = [s.get("n_ctx") for s in slots if isinstance(s.get("n_ctx"), int)]
        if ctxs:
            info["slot_n_ctx"] = ctxs[0]
            info["slot_n_ctx_min"] = min(ctxs)
            info["slot_n_ctx_max"] = max(ctxs)
    info["probed"] = bool(info)
    return info


# ---- 环境信息 ----

def get_gpu_info() -> Dict[str, Any]:
    """nvidia-smi 查询 GPU 名称/显存/驱动；失败降级为 pynvml，再失败返回空。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if out:
            parts = [p.strip() for p in out.split(",")]
            if len(parts) >= 3:
                return {"name": parts[0], "memory_total_mb": int(float(parts[1])),
                        "driver_version": parts[2]}
    except Exception:
        pass
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(h)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        return {"name": name.decode() if isinstance(name, bytes) else name,
                "memory_total_mb": mem.total // (1024 * 1024),
                "driver_version": pynvml.nvmlSystemGetDriverVersion()}
    except Exception:
        return {}


def get_llama_commit(llama_dir: Optional[str] = None) -> Dict[str, Any]:
    """llama.cpp git commit + dirty 标记。llama_dir 为空时在常见位置探测。"""
    candidates = []
    if llama_dir:
        candidates.append(llama_dir)
    else:
        for base in (os.getcwd(), _BENCHMARK_ROOT, _PROJECT_ROOT):
            candidates.append(os.path.join(base, "llama.cpp"))
    for d in candidates:
        # .git 可能是目录（普通仓库）或文件（worktree gitfile）
        if not os.path.exists(os.path.join(d, ".git")):
            continue
        try:
            rev = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            dirty = subprocess.run(["git", "-C", d, "status", "--porcelain"],
                                   capture_output=True, text=True, timeout=5).stdout.strip()
            return {"commit": rev, "dirty": bool(dirty)}
        except Exception:
            continue
    return {"commit": None, "dirty": None}


def hash_file(path: str, chunk_size: int = 1024 * 1024) -> Dict[str, Any]:
    """sha256 文件哈希（分块，2.7GB 约 1–3s）。文件不存在返回空。"""
    if not path or not os.path.isfile(path):
        return {}
    h = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
                size += len(chunk)
        return {"sha256": h.hexdigest(), "size_bytes": size}
    except Exception:
        return {}


def _resolve_model_path(config: BenchmarkConfig, server_info: Dict[str, Any]) -> Optional[str]:
    """解析模型文件绝对路径：config.model_path > server /props 的 model_path。"""
    candidates = []
    if config.model_path:
        candidates.append(config.model_path)
    elif server_info.get("model_path"):
        candidates.append(server_info["model_path"])
    for cand in candidates:
        if os.path.isabs(cand) and os.path.isfile(cand):
            return cand
        for base in (os.getcwd(), _BENCHMARK_ROOT, _PROJECT_ROOT):
            p = os.path.join(base, cand)
            if os.path.isfile(p):
                return p
    return None


# ---- ctx-size 语义检查（E0.6 §4） ----

def check_ctx_semantics(config: BenchmarkConfig, server_info: Dict[str, Any]) -> List[str]:
    """对比配置 ctx_size 与 server 实际 slot n_ctx，返回 warning 列表。"""
    warnings: List[str] = []
    slot_ctx = server_info.get("slot_n_ctx")
    total_slots = server_info.get("total_slots")
    if slot_ctx is None or not server_info.get("probed"):
        warnings.append("无法探测 server slot n_ctx（/props 或 /slots 不可用），"
                        "ctx-size 语义检查跳过。")
        return warnings
    if slot_ctx != config.ctx_size:
        warnings.append(
            f"ctx-size 语义不匹配：配置 ctx_size={config.ctx_size}，"
            f"但 server 实际 slot n_ctx={slot_ctx}"
            f"{f'（total_slots={total_slots}，总 ctx 被 --parallel 平分）' if total_slots else ''}。"
            f"长上下文场景可能提前触发 400 兜底/截断，结果解释需谨慎。"
        )
    if total_slots and config.parallel and config.parallel != total_slots:
        warnings.append(f"配置 parallel={config.parallel} 与 server total_slots={total_slots} 不一致。")
    return warnings


# ---- 汇总 ----

def collect_metadata(config: BenchmarkConfig,
                     server_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """汇总完整实验 metadata。"""
    server_info = server_info or probe_server(config.base_url)
    warnings = check_ctx_semantics(config, server_info)

    model_path = _resolve_model_path(config, server_info)
    model_hash = hash_file(model_path) if model_path else {}

    return {
        "model": {
            "path": model_path,
            **model_hash,
        },
        "llama.cpp": {
            **get_llama_commit(),
            "build_info": server_info.get("build_info"),
        },
        "gpu": get_gpu_info(),
        "server": {
            "base_url": config.base_url,
            "total_slots": server_info.get("total_slots"),
            "slot_n_ctx": server_info.get("slot_n_ctx"),
            "ctx_size_config": config.ctx_size,
        },
        "experiment": {
            "temperature": config.temperature,
            "seed": config.seed,
            "repeat": config.repeat,
            "warmup": config.warmup,
        },
        "warnings": warnings,
    }
