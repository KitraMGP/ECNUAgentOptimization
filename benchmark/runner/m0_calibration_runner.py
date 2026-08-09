#!/usr/bin/env python3
"""M0 短校准复现入口（v56，入库、可复现）。

执行内容（不跑 24-unit formal 矩阵）：
  1. 6 桶 parity 校准（short-P/B … long-P/B）+ 24 候选预算精确复核（parallel4 q8_0）
  2. decision-validation：off/on 两 control 各 ≥10 请求（parallel10 q8_0），
     每 session 含 validator 认可的 representative_output + sha256
  3. 可选 parallel10 q8_0 + --kv-prefix-share 探针（G-M0-5 / RS / /metrics/kv / GPU / RSS）

用法（仓库根相对路径，clone 后可用；无硬编码工作树绝对路径）：
  cd benchmark
  uv run python -m runner.m0_calibration_runner \
      --server-bin ../llama.cpp/build-cuda/bin/llama-server \
      --model ../models/qwen3-5-4B-Q4_K_M.gguf \
      --out ../benchmark/results/m0_cal_4b_run2.json \
      --tmp-dir ../benchmark/results/m0_cal_4b_run2_tmp

默认值（未显式传时）由仓库根推导：
  --server-bin  <repo>/llama.cpp/build-cuda/bin/llama-server
  --model       <repo>/models/qwen3-5-4B-Q4_K_M.gguf
  --out         <repo>/benchmark/results/m0_calibration_<model_id>_<ts>.json
  --tmp-dir     <repo>/benchmark/results/m0_cal_<ts>

退出码：0 = 运行完成（parity_ok 或验证结果如实落盘，无论是否通过门禁）；
意外异常 → 1。校准/验证失败按规则落盘（PREFLIGHT_INFRA 等），不虚构成功。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SERVER_BIN = str(REPO_ROOT / "llama.cpp/build-cuda/bin/llama-server")
DEFAULT_MODEL = str(REPO_ROOT / "models/qwen3-5-4B-Q4_K_M.gguf")
DEFAULT_RESULTS_DIR = str(REPO_ROOT / "benchmark/results")

sys.path.insert(0, str(REPO_ROOT / "benchmark"))

from framework import sampler  # noqa: E402
from runner import m0_fanout_runner as m0r  # noqa: E402
from runner.m0_fanout_runner import M0FanoutRunner  # noqa: E402


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="M0 短校准复现入口（calibration + decision-validation + 可选 p10 探针）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--server-bin", default=DEFAULT_SERVER_BIN,
                    help="llama-server 可执行文件路径")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="GGUF 模型路径")
    ap.add_argument("--out", default="", help="报告 JSON 输出路径（默认 results/ 下派生）")
    ap.add_argument("--tmp-dir", default="", help="server 日志/临时目录（默认 results/m0_cal_<ts>）")
    ap.add_argument("--ctx-size", type=int, default=4096)
    ap.add_argument("--ngl", type=int, default=99)
    ap.add_argument("--run-id", default="", help="run_id（默认 m0-cal-<model_id>-<ts>）")
    ap.add_argument("--no-probe", action="store_true",
                    help="跳过 parallel10 + --kv-prefix-share 探针（任务 1 的可选部分）")
    ap.add_argument("--keep-tmp", action="store_true",
                    help="保留 tmp-dir（默认 cleanup 时删除）")
    ap.add_argument("--decision-n-predict", type=int, default=16,
                    help="决策请求 max_tokens（与正式矩阵合同一致）")
    return ap.parse_args(argv)


def _derive_report_path(model: str, out: str, run_id: str) -> str:
    if out:
        return out
    model_id = os.path.basename(model).rsplit(".", 1)[0]
    return os.path.join(DEFAULT_RESULTS_DIR, f"m0_calibration_{model_id}_{run_id}.json")


def _grep_log(log_path: str, pattern: str) -> List[str]:
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            return [ln.strip() for ln in f if pattern.lower() in ln.lower()]
    except OSError:
        return []


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _PeakSampler:
    """校准/验证运行期间周期采样 GPU/RSS（按端口找 PID，失败静默 None）。

    与 p10 静态探针是不同 PID/阶段：本线程在 calibration + dv 各 server
    生命周期内采样（可能跨多个 PID）；p10 probe 是验证后的单次静态采样。
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self.peak_gpu: Optional[float] = None
        self.peak_rss: Optional[float] = None
        self.pids_seen: List[int] = []
        self.port_pids: Dict[int, int] = {}  # v56：端口→pid（server_logs_summary 用）

    def _run(self, runner: M0FanoutRunner) -> None:
        while not self._stop.is_set():
            for p in (runner._next_port - 1, runner._next_port - 2):
                pid = sampler.find_server_pid("127.0.0.1", p)
                if pid is not None:
                    break
            if pid is not None:
                if pid not in self.pids_seen:
                    self.pids_seen.append(pid)
                self.port_pids[p] = pid
                g = sampler.find_server_gpu_mb(pid)
                rs = sampler.find_server_rss_mb(pid)
                if g is not None and (self.peak_gpu is None or g > self.peak_gpu):
                    self.peak_gpu = g
                if rs is not None and (self.peak_rss is None or rs > self.peak_rss):
                    self.peak_rss = rs
            time.sleep(0.5)

    def start(self, runner: M0FanoutRunner) -> threading.Thread:
        th = threading.Thread(target=self._run, args=(runner,), daemon=True)
        th.start()
        return th

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        # join 由调用方执行（避免持锁）


def _server_logs_summary(runner: M0FanoutRunner, run_id: str,
                         port_pids: Optional[Dict[int, int]] = None) -> List[Dict[str, Any]]:
    """关键 server 日志结构化摘录（RS/KV/capability 行 + server tag/pid/阶段）。

    只归档 key lines（G-M0-4 RS buffer、G-M0-5 capability rejected、KV 分配行），
    不复制完整 -lv5 日志（正式矩阵日志策略同此）。pid 来自采样线程的端口→pid
    映射（server 停止后不可再探测）。
    """
    out: List[Dict[str, Any]] = []
    port_pids = port_pids or {}
    for idx, (tag, log) in enumerate(sorted(runner._server_logs.items())):
        entry: Dict[str, Any] = {
            "server_tag": tag,
            "run_id": run_id,
            "pid": port_pids.get(runner.port_base + idx),
            "phase": _phase_for_tag(tag),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "key_lines": {
                "rs_buffer": _grep_from_text(log, "RS buffer size"),
                "capability_rejected": _grep_from_text(log, "capability rejected"),
                "kv_alloc": _grep_from_text(log, "KV buffer size"),
            },
        }
        out.append(entry)
    return out


def _grep_from_text(text: str, pattern: str) -> List[str]:
    return [ln.strip() for ln in text.splitlines() if pattern.lower() in ln.lower()]


def _phase_for_tag(tag: str) -> str:
    if tag == "calib":
        return "calibration"
    if tag.startswith("dv"):
        return "decision_validation"
    if tag == "p10":
        return "probe_p10"
    return "unknown"


def run_short_calibration(
    server_bin: str, model: str, ctx_size: int, ngl: int,
    tmp_dir: str, run_id: str, decision_n_predict: int,
    do_probe: bool,
    adapter_cls: Optional[type] = None,
) -> Dict[str, Any]:
    """执行短校准全流程并返回报告 dict（不落盘）。

    保证 cleanup：任何路径（含异常）都会停 server、join sampler、检查残留。
    adapter_cls：测试注入 mock（默认 M0FanoutRunner 的 ServerAdapter）。
    """
    report: Dict[str, Any] = {
        "run_id": run_id,
        "run_mode": "short-calibration (no formal matrix)",
        "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "model": os.path.basename(model),
        "model_path": model,
        "server_bin": server_bin,
        "binary_version": None,
        "ctx_size": ctx_size,
        "ngl": ngl,
        "protocol": "OAI /v1/chat/completions, temp=0, seed=42, no-think",
        "parity_ok": None,
        "parity_progress": None,
        "calibrated_lengths": None,
        "preflight_rejections": None,
        "decision_validation": None,
        "probe_p10": None,
        "rs_observations": {},
        "gpu_rss_samples": {"peak_gpu_mb": None, "peak_rss_mb": None,
                            "source": ("periodic sampler across calibration/dv servers "
                                       "(multiple PIDs); p10 probe is a separate static "
                                       "sample (different PID/phase)")},
        "metrics_kv": None,
        "server_logs_summary": [],
        "errors": [],
        "cleanup": None,
    }

    r = M0FanoutRunner(server_bin=server_bin, model=model, ctx_size=ctx_size,
                       tmp_dir=tmp_dir, out_path="", ngl=ngl,
                       decision_n_predict=decision_n_predict,
                       adapter_cls=adapter_cls or m0r.ServerAdapter)

    sampler_th: Optional[threading.Thread] = None
    peak = _PeakSampler()

    def _stop_all() -> None:
        try:
            r._stop_server()
        except Exception:  # noqa: BLE001
            pass
        peak.stop()
        if sampler_th is not None:
            sampler_th.join(timeout=3.0)

    try:
        sampler_th = peak.start(r)

        # ---- 步骤 1：calibration（parallel4 q8_0，6 桶 + 24 候选预算复核） ----
        try:
            parity_ok = r.run_calibration()
            report["parity_ok"] = bool(parity_ok)
            report["parity_progress"] = r.parity_progress
            report["calibrated_lengths"] = {
                f"{k[0]}/fanout{k[1]}": v for k, v in r.calibrated_lengths.items()}
            report["preflight_rejections"] = r.preflight_rejections
            report["binary_version"] = r.binary_version
            calib_log = os.path.join(tmp_dir, "server_calib.log")
            report["rs_observations"]["calib_p4"] = _grep_log(calib_log, "RS buffer size")
            report["rs_observations"]["calib_capability"] = _grep_log(
                calib_log, "capability rejected")
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"calibration: {type(e).__name__}: {e}")

        # ---- 步骤 2：decision-validation（off/on 各一 session，每 session ≥10） ----
        try:
            complete, sessions = r.run_decision_validation()
            report["decision_validation"] = {"complete": complete, "sessions": sessions}
            report["rs_observations"]["dv"] = {}
            for i, s in enumerate(sessions):
                ctl = s.get("control", f"dv{i}")
                dv_log = os.path.join(tmp_dir, f"server_dv{i}.log")
                report["rs_observations"]["dv"][f"{ctl}_p10"] = _grep_log(
                    dv_log, "RS buffer size")
                report["rs_observations"]["dv"][f"{ctl}_capability"] = _grep_log(
                    dv_log, "capability rejected")
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"decision_validation: {type(e).__name__}: {e}")

        # ---- 步骤 3：parallel10 q8_0 + --kv-prefix-share 探针（可选） ----
        if do_probe:
            try:
                adapter = r._start_server(parallel=10, ctk="q8_0", ctv="q8_0",
                                          control="on", tag="p10")
                drv, kv = r._connect(adapter.port)
                try:
                    snap = kv.snapshot(tag="p10_probe")
                    report["metrics_kv"] = {"snapshot": snap}
                except Exception as e:  # noqa: BLE001
                    report["metrics_kv"] = {"error": f"{type(e).__name__}: {e}"}
                pid = sampler.find_server_pid("127.0.0.1", adapter.port)
                g = sampler.find_server_gpu_mb(pid) if pid else None
                rs = sampler.find_server_rss_mb(pid) if pid else None
                p10_log = os.path.join(tmp_dir, "server_p10.log")
                report["probe_p10"] = {
                    "pid": pid,
                    "phase": "post-validation static probe (p10, kv-prefix-share on)",
                    "gpu_mb": g, "rss_mb": rs,
                    "rs_buffer_lines": _grep_log(p10_log, "RS buffer size"),
                    "capability_rejected_lines": _grep_log(p10_log, "capability rejected"),
                    "health": adapter.wait_health(timeout=30.0),
                }
                report["rs_observations"]["p10"] = report["probe_p10"]["rs_buffer_lines"]
                report["rs_observations"]["p10_capability"] = (
                    report["probe_p10"]["capability_rejected_lines"])
            except Exception as e:  # noqa: BLE001
                report["errors"].append(f"probe_p10: {type(e).__name__}: {e}")
            finally:
                try:
                    r._stop_server()
                except Exception:  # noqa: BLE001
                    pass
    finally:
        _stop_all()

    report["server_logs_summary"] = _server_logs_summary(r, run_id, port_pids=peak.port_pids)
    report["gpu_rss_samples"]["peak_gpu_mb"] = peak.peak_gpu
    report["gpu_rss_samples"]["peak_rss_mb"] = peak.peak_rss
    report["gpu_rss_samples"]["pids_seen"] = peak.pids_seen

    try:
        out = subprocess.run(["pgrep", "-x", "llama-server"],
                             capture_output=True, text=True)
        report["cleanup"] = {"leftover_pids": out.stdout.split()}
    except Exception:  # noqa: BLE001
        report["cleanup"] = {"leftover_pids": None}
    return report


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    for p, name in ((args.server_bin, "--server-bin"), (args.model, "--model")):
        if not os.path.isfile(p):
            print(f"错误：{name} 文件不存在：{p}", file=sys.stderr)
            return 1
    model_id = os.path.basename(args.model).rsplit(".", 1)[0]
    run_id = args.run_id or f"m0-cal-{model_id}-{time.strftime('%Y%m%d_%H%M%S')}"
    tmp_dir = args.tmp_dir or os.path.join(DEFAULT_RESULTS_DIR, f"m0_cal_{run_id}")
    os.makedirs(tmp_dir, exist_ok=True)
    out_path = _derive_report_path(args.model, args.out, run_id)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    try:
        report = run_short_calibration(
            server_bin=args.server_bin, model=args.model, ctx_size=args.ctx_size,
            ngl=args.ngl, tmp_dir=tmp_dir, run_id=run_id,
            decision_n_predict=args.decision_n_predict, do_probe=not args.no_probe)
    except Exception as e:  # noqa: BLE001
        print(f"未捕获异常：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        if not args.keep_tmp and os.path.isdir(tmp_dir):
            try:
                shutil_rmtree(tmp_dir)
            except Exception:  # noqa: BLE001
                pass

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"WROTE {out_path}")
    except OSError as e:
        print(f"写报告失败：{e}", file=sys.stderr)
        return 1

    keys = ("parity_ok", "parity_progress", "calibrated_lengths", "preflight_rejections",
            "decision_validation", "probe_p10", "gpu_rss_samples", "metrics_kv",
            "rs_observations", "server_logs_summary", "errors", "cleanup")
    print(json.dumps({k: report.get(k) for k in keys}, ensure_ascii=False, indent=2)[:6000])
    return 0


def shutil_rmtree(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
