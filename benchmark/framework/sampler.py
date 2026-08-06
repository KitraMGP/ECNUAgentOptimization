"""Sampler —— llama-server 进程级资源采样（CPU RSS + GPU 显存）。

迁移自旧 agent_bench.py 的 find_server_pid / find_server_rss_mb /
find_server_gpu_mb / mem_str，保持函数语义一致，并修复一处笔误：
旧代码 ``find_server_gpu_mb`` 中误用未定义变量 ``nvml``（应为 ``pynvml``），
导致 GPU 采样总是被 except 吞掉而返回 None —— 已修正为 ``pynvml``。

GPU 显存采样仅宿主机有效：容器内 pynvml 报 NVMLError_DriverNotLoaded 时
返回 None 属正常降级（见 AGENTS.md）。
"""
from __future__ import annotations

import subprocess
from typing import Optional

import psutil

_server_pid: Optional[int] = None


def find_server_pid(host: str = "127.0.0.1", port: int = 8080) -> Optional[int]:
    """定位监听 host:port 的 llama-server 进程 PID（按 cmdline 匹配 + 端口过滤）。"""
    # 优先：ss -ltnp 找监听端口的 PID（最可靠）
    try:
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            if f":{port}" in line and "llama-server" in line:
                pid = line.split("pid=")[1].split(",")[0]
                return int(pid)
    except Exception:
        pass
    # 兜底：遍历进程，按 cmdline 含 llama-server 匹配
    for p in psutil.process_iter(["cmdline"]):
        try:
            cl = p.info["cmdline"] or []
            if cl and any("llama-server" in c for c in cl):
                return p.pid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def get_server_pid() -> Optional[int]:
    return _server_pid


def set_server_pid(pid: Optional[int]) -> None:
    global _server_pid
    _server_pid = pid


def find_server_rss_mb(pid: Optional[int]) -> Optional[float]:
    if pid is None:
        return None
    try:
        return round(psutil.Process(pid).memory_info().rss / 1024 / 1024, 1)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def find_server_gpu_mb(pid: Optional[int]) -> Optional[float]:
    """nvidia-ml-py 按 PID 采样 llama-server 占用的显存；无 GPU/驱动时返回 None。"""
    if pid is None:
        return None
    try:
        import pynvml  # nvidia-ml-py 提供顶层 pynvml 模块（非弃用旧包）
        pynvml.nvmlInit()
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            for proc in pynvml.nvmlDeviceGetComputeRunningProcesses(h):
                if proc.pid == pid:
                    return round(proc.usedGpuMemory / 1024 / 1024, 1)
    except Exception:
        pass
    return None


def sample(pid: Optional[int]) -> dict:
    """一次采样：返回 {'rss_mb': float|None, 'gpu_mb': float|None}。"""
    return {"rss_mb": find_server_rss_mb(pid), "gpu_mb": find_server_gpu_mb(pid)}


def mem_str(r: dict) -> str:
    """旧脚本的日志格式：RSS=xxMB [GPU=xxMB]。"""
    parts = [f"RSS={r['rss_mb']}MB"]
    if r.get("gpu_mb") is not None:
        parts.append(f"GPU={r['gpu_mb']}MB")
    return " ".join(parts)
