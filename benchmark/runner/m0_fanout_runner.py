"""M0 fan-out 矩阵 runner —— 正式 CLI 入口（一次运行完整 24-unit 矩阵）。

唯一权威：docs/M0_BRANCH_MEMORY_BASELINE_DESIGN.md v47。
- 15 个顺序 server 生命周期：校准 1 + decision validation 2 + formal 12 groups；
- 首 formal group 启动失败 → preflight/PREFLIGHT_INFRA；第 2+ group 启动失败
  或任一 group 运行崩溃 → formal/FORMAL_INCOMPLETE；
- warmup 不落 replicates；每 unit 5 formal reps（FORMAL_REPS）；
- q8_0/q8_0 与 f16/f16、off/on 全矩阵（2×3×2 = 12 groups）；
- 未知 CLI 参数（8 项）→ EXIT_USAGE 64；原子落盘 + SCHEMA_INVALID envelope/sidecar。

生产路径真实可运行（复用 e15 server 生命周期骨架 + framework/driver）；
server 进程通过 adapter 抽象以便 mock 测试，但生产路径不得 mock/no-op。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import openai  # v52：校准路径显式捕获 openai.OpenAIError（与 framework/driver 同依赖风格）

from framework import sampler
from framework.driver import Driver
from framework.kv_probe import KVProbe
from runner import fanout_prompts as fp
from runner import m0_schema as sch
from runner.e15_branch_concurrent import _http_get_json, http_request

# v52（任务 1）：校准阶段允许归入 PREFLIGHT_INFRA 的明确基础设施异常——
# ① OSError 族（网络/IO；HTTPError/URLError/TimeoutError/ConnectionError 均为其子类，
#   不再冗余列出）② openai SDK 异常（OpenAIError 覆盖 APIStatusError/APIConnectionError/
#   APITimeoutError）。其余（AssertionError/KeyError/TypeError 等内部 bug）不捕获 →
# run_safe 归 INTERNAL_RUNNER_ERROR + EXIT_SOFTWARE(70)。
# v57（审查 Medium 7）：③ ServerError（server 启动/health/端点失败，基础设施归因）
#   ——warmup 路径亦视为基础设施异常（健康则忽略继续、死则转崩溃），
#   AssertionError/KeyError/TypeError 等内部 bug 仍不被捕获 → 70。
class ServerError(Exception):
    """server 启动/health/端点失败（基础设施归因）。"""


class CalibrationLengthMissing(Exception):
    """校准长度缺失：配置/内部不变量（v58）。

    独立于 ServerError——不进入 _INFRA_EXCEPTIONS（warmup 不吞），任何路径
    （warmup/formal）均不可恢复，上层 run_safe 归 INTERNAL_RUNNER_ERROR +
    EXIT_SOFTWARE(70)。
    """


_INFRA_EXCEPTIONS = (OSError, openai.OpenAIError, ServerError)

SEED = 42
TEMPERATURE = 0.0
VALIDATION_REQUESTS = 10        # decision validation 每 session ≥10
VALIDATION_SESSIONS = 2
VALIDATION_CONTROLS = ("off", "on")  # v54：G-M0-1 验证覆盖两个 control 开关（off/on 各一 session）
# G-M0-4：RS buffer 对账公式（llama-memory-recurrent.cpp，探针实测）：
#   RS = 24 (k/v/r/s) × 548864 (dim) × 4 (bytes) × parallel
#   v55 实测（2026-08-09 真实 4B GPU）：RS buffer = 201.00 MiB @ parallel4 /
#   502.50 MiB @ parallel10 → 50.25 MiB/parallel，与公式 24×548864×4/1048576
#   完全一致（qwen35 hybrid：24 recurrent 层 × (n_embd_r 24576 + n_embd_s 524288)）
RS_BYTES_PER_ROW = 24 * 548864 * 4
G4_TOLERANCE = 0.05             # ≤5%
# v48（任务 4）：KVProbe periodic 采样间隔（s）——覆盖分支并发与工具轮，
# peak_used_cells/peak_active_sequences 来自真实周期样本；测试可用更小值
PERIODIC_INTERVAL = 0.05
# v66（任务 1）：transient retry（§3.6/§3.8 v12 设计定稿的正式实现）——
# 连接错误（APIConnectionError）/请求超时（APITimeoutError）/HTTP status>=500
# → 最多 3 次逻辑调用（首次 + 重试 2），确定性退避 0.25s/0.5s；4xx、malformed
# 成功响应（APIResponseValidationError）、内部 bug（AssertionError/KeyError/
# TypeError/ValueError）不重试；每次异常后立即 poll server——已退出 →
# ServerCrash（不再重试，ThreadPool branch 的 barrier 不因重试 sleep 死锁）。
TRANSIENT_MAX_ATTEMPTS = 3
TRANSIENT_BACKOFF = (0.25, 0.5)


class ServerAdapter:
    """llama-server 进程生命周期封装（可替换为 mock 以测试，生产为真实 Popen）。"""

    def __init__(self, cmd: List[str], log_path: str, port: int) -> None:
        self.cmd = cmd
        self.log_path = log_path
        self.port = port
        self.proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        with open(self.log_path, "w", encoding="utf-8") as lf:
            self.proc = subprocess.Popen(self.cmd, stdout=lf, stderr=subprocess.STDOUT)

    def poll(self) -> Optional[int]:
        if self.proc is None:
            return None
        return self.proc.poll()

    def wait_health(self, timeout: float = 120.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False
            status, _ = _http_get_json(f"http://127.0.0.1:{self.port}/health", timeout=5.0)
            if status == 200:
                return True
            time.sleep(0.5)
        return False

    def stop(self) -> Tuple[bool, str]:
        """固定清理序列（v62 语义）：poll → 存活 terminate → wait 2s → kill → wait 5s。

        v62：SIGTERM 超时后 SIGKILL+wait **成功** = 进程已清理 →
        返回 `(True, "SIGTERM timeout, killed")`（clean kill 兜底），
        **不得 FormalIncomplete**；只有 kill/wait 后仍存活（wait 再次
        TimeoutExpired）、stop 抛异常或无法确认退出才返回 False。
        """
        proc = self.proc
        if proc is None:
            return True, "no process"
        clean = True
        detail = ""
        try:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=2.0)
                    detail = "clean stop"
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        clean = False
                        detail = "SIGKILL timeout, still alive"
                    else:
                        # v62：kill 兜底成功 = 进程已清理 → clean=True，
                        # detail 供 _stop_server 记 notes/warning
                        detail = "SIGTERM timeout, killed"
            else:
                proc.wait(timeout=5.0)  # 已退出：直接 reap，不 send_signal
                detail = "already exited"
        except subprocess.TimeoutExpired:
            # 已退出路径的 wait(5.0) 超时：无法确认退出
            clean = False
            detail = "wait timeout, cannot confirm exit"
        except ProcessLookupError:
            detail = "process disappeared"  # 进程已消失：clean（无残留）
        except Exception as e:  # stop 抛异常（如 kill 失败）→ False
            clean = False
            detail = f"{type(e).__name__}: {e}"
        self.proc = None
        return clean, detail

    def read_log(self) -> str:
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            # v53（Medium 5）：OSError 含文件/权限类前置错误（FileNotFoundError /
            # PermissionError 均属 OSError 子类）——按既定设计归基础设施，日志缺失
            # 只返回 ""，绝不 traceback 传播
            return ""


class M0FanoutRunner:
    def __init__(
        self,
        server_bin: str,
        model: str,
        ctx_size: int = 4096,
        port_base: int = 8080,
        decision_n_predict: int = 16,
        branch_n_predict: int = 64,
        tool_rounds: int = 2,
        out_path: str = "",
        tmp_dir: str = "",
        ngl: int = 99,
        adapter_cls: type = ServerAdapter,
    ) -> None:
        self.server_bin = server_bin
        self.model = model
        self.ctx_size = ctx_size
        self.port_base = port_base
        self.decision_n_predict = decision_n_predict
        self.branch_n_predict = branch_n_predict
        self.tool_rounds = tool_rounds
        self.out_path = out_path
        if tmp_dir:
            # v58：--tmp-dir 视为父目录，创建本 run 专用唯一子目录（不污染、
            # 绝不递归删除用户提供的既有共享目录；cleanup 只删自己创建的专用目录）
            self.tmp_dir = os.path.join(
                tmp_dir, f"m0_fanout_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}")
            os.makedirs(self.tmp_dir, exist_ok=True)
        else:
            self.tmp_dir = tempfile.mkdtemp(prefix="m0_fanout_")
        # v60（任务 3）：写活跃 run marker（pid/create_ts/keep）——cleanup_stale_dirs
        # 据此识别活跃 run 不删；keep=false（无 --keep-tmp 参数，文档明确生命周期：
        # pid 退出 + age 超龄后由 cleanup 删除；用户可改 marker keep=true 或改名目录
        # 避免意外清理）
        try:
            with open(os.path.join(self.tmp_dir, sch.RUN_MARKER_NAME),
                      "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "create_ts": time.time(), "keep": False}, f)
        except OSError:
            # marker 写失败不致命（cleanup 退化为无 marker 的 age 判定）
            pass
        self.ngl = ngl
        self.adapter_cls = adapter_cls
        self._next_port = port_base
        self._adapter: Optional[ServerAdapter] = None
        self._driver: Optional[Driver] = None
        self._kv: Optional[KVProbe] = None
        self.model_id = os.path.basename(model)
        # 运行期状态（落盘数据源）
        self.parity_progress: Dict[str, Any] = {"completed": [], "errors": []}
        self.preflight_rejections: List[Dict[str, Any]] = []
        self.decision_validation: Optional[Dict[str, Any]] = None
        self.decision_sessions: List[Dict[str, Any]] = []
        self.groups_off: List[Dict[str, Any]] = []
        self.groups_on: List[Dict[str, Any]] = []
        self.any_fallback = False
        self.any_rep_error = False
        self.notes: List[str] = []  # v50：可诊断 note（warmup erase 异常等），落盘 doc.notes
        self._stop_errors: List[str] = []  # v59：_stop_server 捕获的 stop 异常诊断（不传播，不掩盖 primary）
        self.phase = "preflight"
        self.rule = "PREFLIGHT_INFRA"
        self.binary_version = ""
        self.formal_started = False
        # Critical 9/10：tag → server 启动日志（G-M0-4 RS buffer 独立观测 /
        # G-M0-5 hybrid 拒绝行证据；真实 llama.cpp 日志行见对应注释）
        self._server_logs: Dict[str, str] = {}
        # v57（审查 Medium 2）：tag → 真实启动元数据 {port, pid, started_at,
        # log_path}——_start_server 成功时记录；server_logs_summary / formal
        # 日志摘要只用该映射（禁止 sorted index 推断 tag→port/pid）
        self._server_meta: Dict[str, Dict[str, Any]] = {}
        # Critical 6：桶校准实测长度（(bucket, fanout) → {prefix, branch}），
        # 来自真实 apply-template+tokenize，禁止用 BUCKET_TARGETS 近似值落结果
        self.calibrated_lengths: Dict[Tuple[str, int], Dict[str, int]] = {}

    # ---- server 生命周期 ----

    def _server_cmd(self, parallel: int, ctk: str, ctv: str, control: str,
                    port: int) -> List[str]:
        # v55 修复：--port 必须等于 adapter.port（_start_server 里 port 变量，
        # 不是已 +1 的 self._next_port）——原实现用 self._next_port 导致真实
        # server 监听 next_port+1、wait_health 探测 port 永远超时（mock 测试
        # 不启动真实进程未暴露；真实 4B 校准首跑暴露，2026-08-09）
        cmd = [self.server_bin, "-m", self.model,
               "--host", "127.0.0.1", "--port", str(port),
               "-ngl", str(self.ngl),
               "--ctx-size", str(self.ctx_size),
               "--kv-unified",
               "--parallel", str(parallel),
               "--cache-type-k", ctk,
               "--cache-type-v", ctv,
               "--no-webui",
               # v55：日志级别 -lv 5 —— G-M0-4 的 RS buffer 独立观测数据源
               # （llama-memory-recurrent.cpp:115 "RS buffer size"）与
               # llama_kv_cache 分配行默认 verbosity=3 不打印，实测确认
               # （2026-08-09 真实 4B 校准）；-lv 5 同时覆盖 G-M0-5
               # capability-rejected 行（INFO 级，默认即有）
               "-lv", "5",
               # v48 复审：slot erase（KVProbe 恢复观测）前置——llama.cpp
               # server-context.cpp:5448 要求 slot_save_path 非空，否则 POST /slots
               # 返回 ERROR_TYPE_NOT_SUPPORTED；与 e15 runner（e15_branch_concurrent.py
               # :556）一致。tmp_dir 由 CLI/__init__ 创建、runner 生命周期内持久，
               # 每个 server group 复用同一目录（每生命周期可用）。
               "--slot-save-path", self.tmp_dir]
        if control == "on":
            cmd += ["--kv-prefix-share"]
        return cmd

    def _start_server(self, parallel: int, ctk: str, ctv: str, control: str,
                      tag: str) -> ServerAdapter:
        """启动 server（启动/health 失败重试 3 次后抛 ServerError）。"""
        port = self._next_port
        self._next_port += 1
        cmd = self._server_cmd(parallel, ctk, ctv, control, port)
        log_path = os.path.join(self.tmp_dir, f"server_{tag}.log")
        adapter = self.adapter_cls(cmd, log_path, port)
        last_err = "启动失败"
        for attempt in range(3):
            try:
                adapter.start()
            except OSError as e:
                # Critical 4：启动路径（打开日志文件 / Popen server-bin）OSError
                # → 基础设施归因 ServerError，绝不 traceback 传播（含
                # FileNotFoundError：server-bin 不存在；PermissionError：tmp 不可写）
                try:
                    adapter.stop()
                except Exception:
                    pass
                last_err = f"启动失败: {e}"
                break
            if adapter.wait_health(timeout=90.0):
                self._adapter = adapter
                # Critical 9/10：记录该 group 日志（G-M0-4 RS 观测 / G-M0-5 日志证据）
                self._server_logs[tag] = adapter.read_log()
                # v57（审查 Medium 2）：真实元数据映射——pid 取真实子进程 PID
                # （mock adapter 无 proc 时为 None），started_at 为启动成功时刻
                pid = None
                if getattr(adapter, "proc", None) is not None:
                    pid = adapter.proc.pid
                self._server_meta[tag] = {
                    "port": port,
                    "pid": pid,
                    "started_at": sch.now_utc(),  # v58：RFC3339 UTC（Z 后缀）
                    "log_path": log_path,
                }
                return adapter
            code = adapter.poll()
            adapter.stop()
            last_err = f"health 超时（exit={code}）" if code is None else f"早退（exit={code}）"
            time.sleep(0.5)
        raise ServerError(last_err)

    def _stop_server(self) -> bool:
        """停止当前 server。

        v59：stop 异常不传播——finally 中调用绝不掩盖 try 块 primary 异常；
        错误记入 self._stop_errors（调用方在无 primary 异常时按基础设施处理）。
        v60（任务 1）：检查 adapter.stop() 返回 `(ok, detail)`——`ok=False`
        视为 stop failure（进程可能残留，如 SIGTERM 超时转 kill），记录
        `_stop_errors` 与 detail；抛异常路径保留（同记 _stop_errors 不传播）；
        非 tuple 返回（旧 mock 契约 None）→ 视为干净。
        返回 True=stop 干净、False=stop 出错（进程可能残留）。
        """
        clean = True
        if self._adapter is not None:
            try:
                result = self._adapter.stop()
            except Exception as e:  # noqa: BLE001
                clean = False
                msg = f"{type(e).__name__}: {e}"
                self._stop_errors.append(msg)
                self.notes.append(f"stop 异常: {msg}")  # v62：notes 具体文本
            else:
                if isinstance(result, tuple) and len(result) >= 2:
                    ok, detail = result[0], result[1]
                    if not ok:
                        clean = False
                        msg = f"stop ok=False: {detail}"
                        self._stop_errors.append(msg)
                        self.notes.append(msg)  # v62：非 clean 原因写 notes
                    elif detail and detail not in (
                            "clean stop", "already exited",
                            "no process", "process disappeared"):
                        # v64：仅**非正常** detail（如 SIGTERM timeout, killed 兜底）
                        # 记 notes/warning；正常 detail（clean stop/already exited/
                        # no process/process disappeared）是常规返回、不污染 notes
                        self.notes.append(f"stop clean 但带 warning: {detail}")
                elif isinstance(result, bool):
                    # v61（任务 5）：bool False 也记录（进程可能残留）
                    if not result:
                        clean = False
                        msg = "stop 返回 False（进程可能残留）"
                        self._stop_errors.append(msg)
                        self.notes.append(msg)  # v62
                else:
                    # v61（任务 5）：非 tuple/非 bool 返回 → 按诊断失败处理
                    clean = False
                    msg = f"stop 返回异常类型 {type(result).__name__}（期望 tuple/bool）"
                    self._stop_errors.append(msg)
                    self.notes.append(msg)  # v62
            self._adapter = None
        self._driver = None
        self._kv = None
        return clean

    # ---- driver/kv ----

    def _connect(self, port: int) -> Tuple[Driver, KVProbe]:
        base_url = f"http://127.0.0.1:{port}"
        # Critical 3：显式传实际 host/port —— Driver 内部 sampler 按端口定位 PID，
        # 不传会用默认 8080，非默认端口时 PID 定位错配（ss 找不到 → 兜底错进程）。
        drv = Driver(base_url=base_url, model="bench", max_retry=1,
                     sdk_max_retries=0, host="127.0.0.1", port=port)
        kv = KVProbe(base_url=base_url)
        # server version（/props build_info 或启动日志，尽力而为）
        try:
            status, body = _http_get_json(f"{base_url}/props", timeout=5.0)
            if status == 200 and isinstance(body, dict):
                self.binary_version = str(body.get("build_info", ""))
        except Exception:
            pass
        return drv, kv

    # ---- v66：transient retry wrapper（§3.6/§3.8 v12 设计定稿的正式实现） ----

    def _chat(self, messages: List[dict], **kwargs: Any) -> Dict[str, Any]:
        """单一 chat wrapper：transient 失败最多 3 次逻辑调用（退避 0.25/0.5）。

        - 可重试：APIConnectionError / APITimeoutError / HTTP status>=500
          （按异常类型 + status_code 精确分类，见 ``_transient_class``）；
        - 不重试：HTTP 4xx（含 driver 内部 400 context 兜底耗尽后的 400）、
          malformed 成功响应（APIResponseValidationError）、内部 bug
          （AssertionError/KeyError/TypeError/ValueError 等）；
        - **每次异常后立即 poll server**（三路归因 §3.6）：已退出 →
          ``ServerCrash``（不再重试——ThreadPool branch 的 barrier 不因
          重试 sleep 死锁）；健康 → 按剩余尝试次数 sleep 退避后重试，
          耗尽则抛最后一次异常（外层 ``classify_error`` 归因
          connection_error/timeout/http_5xx）。
        """
        assert self._driver is not None
        for attempt in range(TRANSIENT_MAX_ATTEMPTS):
            try:
                return self._driver.chat(messages, **kwargs)
            except Exception as e:  # noqa: BLE001 —— 分类后决定是否重试
                if self._adapter is not None and self._adapter.poll() is not None:
                    # 首次异常进程存活、重试期间退出（三路③）→ 立即 server_crash
                    raise ServerCrash(
                        f"transient retry: server 已退出 ({classify_error(e)})") from e
                if self._transient_class(e) is None:
                    # 4xx / malformed / 内部 bug → 不外层重试（三路①已排除，直接抛）
                    raise
                if attempt < TRANSIENT_MAX_ATTEMPTS - 1:
                    time.sleep(TRANSIENT_BACKOFF[attempt])
                    continue
                # 重试耗尽（第 3 次失败）→ 抛最后一次异常（分类 = 该异常类型）
                raise

    @staticmethod
    def _transient_class(e: Exception) -> Optional[str]:
        """transient 可重试分类；None = 不可重试（4xx/malformed/内部 bug）。

        v66：按异常类型 + status_code 精确分类（替代纯文本启发式）——
        - APITimeoutError → timeout；APIConnectionError → connection_error
          （openai 2.x 中 APITimeoutError 是 APIConnectionError 子类，先判 timeout）；
        - APIStatusError：status>=500 → http_5xx（可重试）；400<=status<500 →
          http_4xx（不可重试）；
        - APIResponseValidationError → malformed_response（不可重试）；
        - 内部 bug（AssertionError/KeyError/TypeError/ValueError）→ None；
        - 其余（urllib.HTTPError 等旧路径）→ 文本 fallback 分类。
        """
        if isinstance(e, (AssertionError, KeyError, TypeError, ValueError)):
            return None
        if isinstance(e, openai.APITimeoutError):
            return "timeout"
        if isinstance(e, openai.APIConnectionError):
            return "connection_error"
        if isinstance(e, openai.APIStatusError):
            code = getattr(e, "status_code", 0) or 0
            return "http_5xx" if code >= 500 else None
        if isinstance(e, openai.APIResponseValidationError):
            return None  # malformed 成功响应不重试
        # 其余（urllib.HTTPError 等旧路径）：**仅明确 transient 文本**才重试——
        # 未知异常（IndexError/RuntimeError 等内部 bug，如 driver.py 空 choices）
        # 一律不重试（与"内部 bug 不重试"一致：宁可标 ERROR 也不重试内部缺陷）
        if isinstance(e, urllib.error.HTTPError):
            code = getattr(e, "code", 0)
            return "http_5xx" if 500 <= code < 600 else None
        low = str(e).lower()
        if "timed out" in low or "timeout" in low:
            return "timeout"
        if "refused" in low or "connection reset" in low:
            return "connection_error"
        return None

    # ---- 校准（§3.5 步骤 1-3：apply-template + tokenize + parity） ----

    def _calibrate_template(self, messages: List[Dict[str, str]]) -> Tuple[int, int]:
        """渲染 + tokenize + max_tokens=1 parity 校准请求 → (P/B tokens, chat prompt_tokens)。

        返回 (tokenize_len, chat_prompt_tokens)；端点失败抛 ServerError（基础设施归因）。
        """
        assert self._driver is not None
        try:
            prompt = self._driver.apply_template(messages)
            n = self._driver.count_tokens(prompt, add_special=False)
            # v66：parity 校准 chat 走 transient wrapper（§3.6 校准请求同 wrapper）
            row = self._chat(messages, temperature=0.0, seed=SEED, max_tokens=1)
            chat_prompt = int(row["prompt_tokens"])
            return n, chat_prompt
        except (OSError, openai.OpenAIError) as e:
            # v52（任务 1）：显式捕获 openai SDK 异常（APIStatusError/APIConnectionError/
            # APITimeoutError 均属 OpenAIError）+ OSError 族 → 基础设施归因 PREFLIGHT_INFRA；
            # 内部 bug（AssertionError/KeyError 等）不捕获 → run_safe 70。
            raise ServerError(f"校准端点失败: {type(e).__name__}: {e}")

    def run_calibration(self) -> bool:
        """6 桶校准（short-P/B … long-P/B）+ 预算精确复核；返回 parity_ok。

        失败（端点异常/基础设施）→ 抛 ServerError → 上层落盘 PREFLIGHT_INFRA。
        预算复核（run_budget_check）在校准 server 存活期间执行（§3.5 步骤 3）。
        """
        adapter = self._start_server(parallel=4, ctk="q8_0", ctv="q8_0", control="off",
                                     tag="calib")
        try:
            self._driver, self._kv = self._connect(adapter.port)
            for bucket in fp.BUCKETS:
                for tmpl in ("P", "B"):
                    key = f"{bucket}-{tmpl}"
                    if tmpl == "P":
                        msgs = fp.decision_messages(fp.build_context(bucket), fanout=2)
                    else:
                        action = "ACTION: branch(b1)"
                        canary = fp.canary_for(2, "b1")
                        msgs = fp.branch_messages(
                            fp.decision_messages(fp.build_context(bucket), fanout=2),
                            action, "b1", fp.build_branch_task(bucket, "b1"), canary)
                    tok, chat_tok = self._calibrate_template(msgs)
                    delta = tok - chat_tok
                    # completed 语义（v37）：桶流程完成即计入 completed，即使 mismatch
                    self.parity_progress["completed"].append(key)
                    if delta != 0:
                        self.parity_progress["errors"].append({
                            "code": "parity_mismatch", "stage": "calibration",
                            "bucket": bucket, "template": tmpl,
                        })
            # 预算精确复核（24 候选，同一校准 server）
            self.run_budget_check()
            # erase 全部 slot 回基线（校准后清理协议；v49：best-effort——
            # 异常后 poll：崩溃 → ServerCrash（校准 server 已退出，不吞），健康 → 继续；
            # v52（任务 5）：返回 (0,0) 时同样 poll——死 → ServerCrash（上层转
            # PREFLIGHT_INFRA），健康 → 记可诊断 note）
            if self._kv is not None:
                try:
                    attempt, ok = self._kv.clean_all_slots()
                    if attempt > 0 and ok < attempt:
                        # v53（Medium 1）：与 warmup（v50 任务 5）一致——partial
                        # erase（attempt>0 且 ok<attempt）→ 记可诊断 note，不静默吞
                        self._add_note(
                            f"校准后 clean_all_slots 部分失败 {ok}/{attempt} "
                            f"（server 健康，继续）")
                    elif attempt == 0:
                        # v52（任务 5）：返回 (0,0) 时 poll——死 → ServerCrash
                        # （上层转 PREFLIGHT_INFRA），健康 → 记可诊断 note
                        if (self._adapter is not None
                                and self._adapter.poll() is not None):
                            raise ServerCrash("校准后清理: server 已退出") from None
                        self._add_note(
                            "校准后 clean_all_slots 返回 (0,0)"
                            "（无 slot 或端点不可用；server 健康，继续）")
                except ServerCrash:
                    raise
                except Exception as e:
                    if (self._adapter is not None
                            and self._adapter.poll() is not None):
                        raise ServerCrash("校准后清理: server 已退出") from None
                    # 防御分支（kv_probe 行为改变才可达）：健康 → 记可诊断 note
                    self._add_note(
                        f"校准后 clean_all_slots 异常（server 健康，继续）: "
                        f"{type(e).__name__}: {e}")
            mismatched = any(e["code"] == "parity_mismatch"
                             for e in self.parity_progress["errors"])
            return not mismatched
        finally:
            self._stop_server()

    # ---- 预算（§3.5 步骤 3：24 候选精确复核） ----

    def run_budget_check(self) -> None:
        """对 24 候选做真实 apply-template+tokenize 预算复核；超限 → preflight_rejections。

        必须在校准 server 存活期间调用（self._driver 已连接）。
        """
        assert self._driver is not None
        try:
            for unit_id in sch.EXPECTED_UNIT_IDS:
                parsed = sch.parse_unit_id(unit_id)
                assert parsed is not None
                fanout = int(parsed["fanout"])
                bucket = parsed["bucket"]
                msgs = fp.decision_messages(fp.build_context(bucket), fanout=fanout)
                p_tok = self._driver.count_tokens(
                    self._driver.apply_template(msgs), add_special=False)
                action = "ACTION: branch(b1)"
                canary = fp.canary_for(fanout, "b1")
                bmsgs = fp.branch_messages(msgs, action, "b1",
                                           fp.build_branch_task(bucket, "b1"), canary)
                b_tok = self._driver.count_tokens(
                    self._driver.apply_template(bmsgs), add_special=False)
                # Critical 6：按 (bucket, fanout) 保存校准实测长度（真实
                # apply-template+tokenize），formal rep 的 prefix_len/branch_len
                # 与预算复核同源；同 (bucket,fanout) 的 4 个 unit（off/on ×
                # 2 profile）共享同一实测值（设计 v42「同桶 4 unit 一致」）。
                self.calibrated_lengths[(bucket, fanout)] = {
                    "prefix": int(p_tok), "branch": int(b_tok)}
                b_branch = b_tok - p_tok  # 分支后缀增量
                total = fp.budget(p_tok, b_branch, fanout)
                if total > fp.BUDGET_THRESHOLD:
                    self.preflight_rejections.append({
                        "unit_id": unit_id, "budget": total,
                        "threshold": fp.BUDGET_THRESHOLD, "reason": "budget_rejected",
                    })
        except OSError as e:
            # v53（Medium 5）：OSError 族（连接类 + FileNotFoundError/PermissionError
            # 等文件/权限类前置错误）→ 按既定设计归基础设施 ServerError
            raise ServerError(f"预算复核端点失败: {e}")

    # ---- decision validation（G-M0-1 数据源，2 sessions × ≥10） ----

    def _run_decision_session(self, port: int) -> List[Dict[str, Any]]:
        """单次验证 session：≥10 次决策点请求，逐条记录 valid/invalid/error。"""
        drv, kv = self._connect(port)
        self._driver, self._kv = drv, kv
        recs: List[Dict[str, Any]] = []
        target = VALIDATION_REQUESTS
        for _ in range(target):
            msgs = fp.decision_messages(fp.build_context("short"), fanout=8)
            try:
                # v66：验证请求走 transient wrapper（§3.6 验证请求 retry 三路）
                row = self._chat(msgs, temperature=TEMPERATURE, seed=SEED,
                                 max_tokens=self.decision_n_predict)
                text = row["text"]
                # Critical 2：只从顶层 finish_reason 判定（mock 可配置 length），
                # 不依赖非标准 timings 字段
                fr = row.get("finish_reason") or "stop"
                invalid = fp.decision_invalid(text, str(fr), 8)
                recs.append({"ok": not invalid, "error": None, "text": text,
                             "finish_reason": str(fr)})
            except Exception as e:
                recs.append({"ok": False, "error": classify_error(e), "text": "",
                             "finish_reason": ""})
        return recs

    def run_decision_validation(self) -> Tuple[bool, List[Dict[str, Any]]]:
        """2 次独立 server 会话验证；返回 (证据完整, sessions)。

        启动失败（重试 3 次后）→ 立即停止验证阶段，返回不完整（上层归因
        PREFLIGHT_INFRA / partial）。
        v58：函数开始清空 self.decision_sessions；每完成一个 session 立即同步
        （异常中断时已完成 session 保留在 self.decision_sessions，供上层构造
        partial 证据）。
        """
        self.decision_sessions = []
        for s in range(VALIDATION_SESSIONS):
            # v54：session 0 → control off、session 1 → control on（G-M0-1 验证
            # 不依赖 control 开关，两开关各验证一次更完整）
            control = VALIDATION_CONTROLS[s]
            try:
                adapter = self._start_server(parallel=10, ctk="q8_0", ctv="q8_0",
                                             control=control, tag=f"dv{s}")
            except ServerError:
                break
            before_stop = len(self._stop_errors)
            try:
                recs = self._run_decision_session(adapter.port)
            except _INFRA_EXCEPTIONS:
                # v59：session 级基础设施异常（连接失败等）→ 停止验证阶段
                # （本 session 不 append），已完成 session 保留 → 上层构造
                # partial dv 落盘；AssertionError/KeyError 等内部 bug 不在此
                # 捕获，传播 → run_safe 归 INTERNAL_RUNNER_ERROR + 70
                break
            finally:
                self._stop_server()
            if len(self._stop_errors) > before_stop:
                # v59：try 块无 primary 异常但 stop 失败（进程可能残留）→
                # 按基础设施处理（session 不完整）
                break
            # v58：session 完成立即同步（异常时本 session 不 append，已完成保留）
            self.decision_sessions.append(self._session_from_recs(recs, control))
        complete = len(self.decision_sessions) == VALIDATION_SESSIONS and all(
            s["requests"] >= VALIDATION_REQUESTS and s["error_count"] == 0
            for s in self.decision_sessions)
        return complete, self.decision_sessions

    @staticmethod
    def _session_from_recs(recs: List[Dict[str, Any]], control: str) -> Dict[str, Any]:
        valid = sum(1 for r in recs if r["ok"] and r["error"] is None)
        errors = [r for r in recs if r["error"] is not None]
        invalids = [r for r in recs if not r["ok"] and r["error"] is None]
        invalid_length = sum(1 for r in invalids if r["finish_reason"] == "length")
        invalid_no_action = len(invalids) - invalid_length
        fr: Dict[str, int] = {}
        for r in recs:
            if r["error"] is None:
                fr[r["finish_reason"] or "stop"] = fr.get(r["finish_reason"] or "stop", 0) + 1
        err_summary: Dict[Tuple, int] = {}
        for e in errors:
            key = (e["error"], None, None)
            err_summary[key] = err_summary.get(key, 0) + 1
        # v56：可审计代表输出——第一条 valid 请求的原始输出文本（必须通过
        # parse_action 才计入 valid；哈希由 build_decision_session 计算）
        rep_text = next((r["text"] for r in recs
                         if r["ok"] and r["error"] is None), "")
        sess = sch.build_decision_session(
            requests=len(recs), valid=valid, invalid=len(invalids),
            error_count=len(errors), invalid_length=invalid_length,
            invalid_no_action=invalid_no_action, finish_reasons=fr,
            output_hashes=[sch.content_sha256(r["text"]) for r in recs
                           if r["error"] is None],
            error_summary=[{"code": k[0], "count": v}
                           for k, v in err_summary.items()],
            representative_output=rep_text,
        )
        sess["control"] = control  # v54：记录验证 session 的 control 开关
        return sess

    # ---- formal 矩阵（12 groups） ----

    def run_formal(self) -> None:
        """12 个 group（cache profile × fanout × control），每组 parallel=fanout+2。

        首 group 启动失败 → 抛 FirstGroupStartFailed（上层落盘 preflight）；
        第 2+ group 启动失败 / 任一 group 运行中崩溃 → 抛 FormalIncomplete。
        """
        self.formal_started = True
        group_seq: List[Tuple[str, str, str, int]] = []  # (control, ctk, ctv, fanout)
        for control in fp.CONTROLS:
            for (ctk, ctv) in fp.CACHE_PROFILES:
                for fanout in fp.FANOUTS:
                    if fanout not in fp.legal_matrix_plan():
                        continue
                    group_seq.append((control, ctk, ctv, fanout))
        for gi, (control, ctk, ctv, fanout) in enumerate(group_seq):
            gid = sch.group_id_of(control, ctk, ctv, fanout)
            start_ts = sch.now_utc()
            adapter: Optional[ServerAdapter] = None  # v61：结构化 finally 统一 stop
            try:
                try:
                    adapter = self._start_server(parallel=fanout + 2, ctk=ctk, ctv=ctv,
                                                 control=control, tag=f"g{gi}")
                except ServerError as e:
                    if gi == 0:
                        raise FirstGroupStartFailed(str(e))
                    self._record_group_error(gid, control, start_ts, "server_crash", str(e))
                    raise FormalIncomplete(str(e))
                # v61（Critical 修复）：server 已启动 → 以下全部路径（connect/baseline/
                # warmup/formal/异常归因）由外层 finally 无条件 _stop_server，
                # 任何异常（基础设施、FirstGroupStartFailed、FormalIncomplete、内部 bug）
                # 均不得泄漏 server。
                try:
                    self._driver, self._kv = self._connect(adapter.port)
                except _INFRA_EXCEPTIONS as e:
                    # v60（任务 2）：连接基础设施异常（OSError/openai/ServerError，
                    # 含 TimeoutError）→ 首 group（尚无任何 completed group）按首
                    # group preflight/PREFLIGHT_INFRA 规则（FirstGroupStartFailed，
                    # 上层保留完整 decision_validation 落盘 preflight）；第 2+ group
                    # → 保留已完成 groups、失败 group ERROR（endpoint_unavailable）、
                    # FormalIncomplete 合法落盘，绝不 exit70 丢结果。内部 bug
                    # （AssertionError/KeyError/TypeError）不在此捕获 → run_safe 70。
                    if not self.groups_off and not self.groups_on:
                        raise FirstGroupStartFailed(
                            f"connect 基础设施失败: {type(e).__name__}: {e}") from e
                    self._record_group_error(gid, control, start_ts,
                                             "endpoint_unavailable", f"connect: {e}")
                    raise FormalIncomplete(
                        f"connect 基础设施失败: {type(e).__name__}: {e}") from e
                try:
                    baseline = self._capture_baseline(gid)
                except _INFRA_EXCEPTIONS as e:
                    # v60（任务 2）：baseline KV 快照 / 采样的基础设施异常（OSError /
                    # openai 连接失败等）→ 同 connect 归因（首 group preflight；
                    # 第 2+ group 保留已完成 + ERROR + FormalIncomplete）。
                    if not self.groups_off and not self.groups_on:
                        raise FirstGroupStartFailed(
                            f"baseline 基础设施失败: {type(e).__name__}: {e}") from e
                    self._record_group_error(gid, control, start_ts,
                                             "endpoint_unavailable", f"baseline: {e}")
                    raise FormalIncomplete(
                        f"baseline 基础设施失败: {type(e).__name__}: {e}") from e
                reps: List[Dict[str, Any]] = []
                crashed = False
                crash_error_type = "server_crash"  # v49：ServerCrash 默认；EraseFailure 覆盖
                for bucket in fp.legal_matrix_plan().get(fanout, []):
                    unit_id = sch.unit_id_of(control, ctk, ctv, fanout, bucket)
                    if unit_id in self._rejected_ids():
                        continue
                    # warmup 2 次（不落 replicates）
                    for _ in range(sch.WARMUP_REPS):
                        try:
                            self._run_unit(unit_id, fanout, bucket, ctk, ctv,
                                           gid, rep_index=None)
                        except ServerCrash:
                            # Critical 7：warmup 崩溃不得被吞——立即进入 group
                            # ERROR / FORMAL_INCOMPLETE（进程已退出，后续请求必败）
                            crashed = True
                            break
                        # v50（任务 4）：warmup（rep_index=None）不执行严格 erase
                        # 路径（严格 partial 检查只对 formal rep）——EraseFailure
                        # 不可能从 _run_unit(warmup) 抛出，删除 v49 不可达分支；
                        # warmup 的 best-effort erase 异常经 poll 判定：已退出 →
                        # ServerCrash（上层 group ERROR）；健康 → 记 note（见 finally）
                        except _INFRA_EXCEPTIONS:
                            # v53（Medium 3）：warmup 只吞明确基础设施/请求异常
                            # （OSError / openai.OpenAIError 族）——v48 任务 7 语义
                            # 保留：异常后立即 poll server——已退出 → 视同崩溃
                            # （转 group ERROR/FORMAL_INCOMPLETE）；仍健康才允许
                            # 按设计忽略 warmup 请求级误差（仅预热）
                            if (self._adapter is not None
                                    and self._adapter.poll() is not None):
                                crashed = True
                                break
                        except Exception:
                            # v53（Medium 3）：内部 bug（AssertionError/KeyError/
                            # TypeError 等）不属于基础设施/请求异常——不得静默吞，
                            # 上抛 → run_safe 归 INTERNAL_RUNNER_ERROR + 70
                            raise
                    if crashed:
                        break
                    # formal 5 reps
                    for ri in range(sch.FORMAL_REPS):
                        try:
                            rep = self._run_unit(unit_id, fanout, bucket, ctk, ctv,
                                                 gid, rep_index=ri)
                            reps.append(rep)
                        except ServerCrash as e:
                            crashed = True
                            reps.append(sch.build_rep(
                                unit_id, ri, fanout, bucket, ctk, ctv,
                                self._last_prefix_len, self._last_branch_len, gid,
                                "ERROR", False, {"error": str(e)},
                                error_type="server_crash", error_stage="formal",
                                error_bucket=bucket))
                            break
                        except EraseFailure as e:
                            # v49（任务 2/5）：erase 阶段失败（server 健康）——
                            # 标 rep ERROR（保留可诊断 error_type）+ group
                            # ERROR/FORMAL_INCOMPLETE（优先后者以免污染后续 rep）
                            crashed = True
                            crash_error_type = e.error_type
                            reps.append(sch.build_rep(
                                unit_id, ri, fanout, bucket, ctk, ctv,
                                self._last_prefix_len, self._last_branch_len, gid,
                                "ERROR", False,
                                {"error": f"{e.error_type}: {e.detail}"},
                                error_type=e.error_type, error_stage="formal",
                                error_bucket=bucket))
                            break
                stop_ts = sch.now_utc()
                if crashed:
                    # 崩溃/erase 失败 group → status=ERROR + error_type 必填
                    # （v42/v47：modes 只含已启动 groups；失败 group 以 ERROR 出现；
                    #  v49：error_type 取实际失败类型，EraseFailure 时非 server_crash）
                    group = sch.build_group(gid, "ERROR", start_ts, stop_ts,
                                            baseline=baseline, replicates=reps,
                                            error_type=crash_error_type,
                                            error_stage="formal")
                    self._append_group(control, group)
                    raise FormalIncomplete("运行中崩溃/erase 失败")
                group = sch.build_group(gid, "COMPLETED", start_ts, stop_ts,
                                        baseline=baseline, replicates=reps)
                self._append_group(control, group)
            finally:
                if adapter is not None:
                    stop_clean = self._stop_server()
                    if not stop_clean:
                        # v61（任务 2）：stop 失败——无 primary 异常 → FormalIncomplete；
                        # 有 primary 异常 → 记 notes 不覆盖（异常优先传播）
                        if sys.exc_info()[0] is None:
                            raise FormalIncomplete("group 停止失败（进程可能残留）")
                        self.notes.append(
                            "group 停止失败（进程可能残留；primary 异常优先）")
    def _rejected_ids(self) -> List[str]:
        return [r.get("unit_id") for r in self.preflight_rejections]

    def _append_group(self, control: str, group: Dict[str, Any]) -> None:
        if control == "off":
            self.groups_off.append(group)
        else:
            self.groups_on.append(group)

    def _record_group_error(self, gid: str, control: str, start_ts: str,
                            error_type: str, detail: str) -> None:
        group = sch.build_group(gid, "ERROR", start_ts, sch.now_utc(),
                                error_type=error_type, error_stage="formal")
        self._append_group(control, group)

    def _capture_baseline(self, gid: str) -> Dict[str, Any]:
        assert self._kv is not None
        try:
            rss = sampler.find_server_rss_mb(sampler.get_server_pid())
        except Exception:
            rss = 0.0
        try:
            gpu = sampler.find_server_gpu_mb(sampler.get_server_pid())
        except Exception:
            gpu = 0.0
        kv_snap = self._kv.snapshot("baseline") or {}
        # Critical 1：snapshot 返回 {ts, tag, run_id, data} 包装——capacity_bytes
        # 与全部 /metrics/kv 字段在 data 内；从 data 读取并保存权威 data（而非
        # 包装对象），修复 kv_buffer_mb 恒 0 与 metrics_kv_snapshot 结构错配。
        kv_data = kv_snap.get("data") if isinstance(kv_snap.get("data"), dict) else {}
        parsed = sch.parse_group_id(gid)
        assert parsed is not None
        parallel = int(parsed["fanout"]) + 2
        rs_mb = RS_BYTES_PER_ROW * parallel / (1024 * 1024)
        return sch.build_baseline(
            server_version=self.binary_version or "unknown",
            gpu_used_mb=float(gpu or 0.0), rss_mb=float(rss or 0.0),
            rs_buffer_mb=rs_mb,
            kv_buffer_mb=float(kv_data.get("capacity_bytes", 0) or 0) / (1024 * 1024),
            metrics_kv_snapshot=kv_data,
        )

    _last_prefix_len = 150
    _last_branch_len = 150

    def _run_unit(self, unit_id: str, fanout: int, bucket: str, ctk: str, ctv: str,
                  gid: str, rep_index: Optional[int]) -> Dict[str, Any]:
        """单个 unit 执行（决策点 → fallback → 分支并发 → 工具轮 → 回收）。

        rep_index=None 表示 warmup（不落 replicates、不参与指标）。
        """
        assert self._driver is not None and self._kv is not None
        # Critical 6：prefix/branch 长度必须用 (bucket, fanout) 校准实测值
        # （apply-template+tokenize），禁止 BUCKET_TARGETS 近似值落结果。
        cal = self.calibrated_lengths.get((bucket, fanout))
        if cal is None:
            raise CalibrationLengthMissing(
                f"校准长度缺失: {bucket}/fanout={fanout}（校准阶段必须先行，v58）")
        self._last_prefix_len = int(cal["prefix"])
        self._last_branch_len = int(cal["branch"])
        # v65：收集本 rep 各请求 latency/TTFT（driver row 顶层 latency_ms +
        # timings.prompt_ms 代理）——OK/INVALID_DECISION rep 落 avg 供 off/on
        # 延迟对比（§5.2 定义但此前未落，完整 4B 矩阵暴露缺口）
        latencies: List[float] = []
        ttfts: List[float] = []
        # v66（任务 4）：rep 级 GPU/RSS 峰值——decision/branch/tool **成功 row**
        # 的 rss_mb/gpu_mb 样本（driver row 顶层字段，None 过滤）；ERROR rep
        # 保留已有成功样本（部分成功也有真实峰值，不再 0.0 占位）。
        # 线程安全：branch worker（ThreadPool）跨线程 append 依赖 CPython GIL
        # 列表 append 原子性；读取（_peak_mem）发生在所有 future 完成之后。
        mem_samples: List[Tuple[Optional[float], Optional[float]]] = []
        # Critical 1：每个 formal rep 独立 begin_run/end_run 边界（KV 快照按
        # run_id 归组）；warmup 不进入 run 作用域。
        rep_run_id = f"{unit_id}#r{rep_index}" if rep_index is not None else None
        if rep_index is not None:
            self._kv.begin_run(rep_run_id)
            self._kv.snapshot("pre")
            # v48 复审（任务 4）：periodic 采样覆盖分支并发与工具轮——
            # peak_used_cells/peak_active_sequences 必须来自真实周期样本
            # （begin_run 之后启动 → 周期样本带 run_id、进入 run 聚合）
            self._kv.start_periodic(PERIODIC_INTERVAL)
        decision_error: Optional[BaseException] = None
        tool_error: Optional[BaseException] = None  # v50（任务 3）
        try:
            context = fp.build_context(bucket)
            msgs = fp.decision_messages(context, fanout)
            # 1) 决策请求（v66：transient wrapper——§3.6 三路归因）
            try:
                row = self._chat(msgs, temperature=TEMPERATURE, seed=SEED,
                                 max_tokens=self.decision_n_predict)
                latencies.append(row.get("latency_ms") or 0.0)
                _t = row.get("timings") or {}
                ttfts.append(_t.get("prompt_ms") or 0.0)
                # v66（任务 4）：rep 级 GPU/RSS 峰值样本（决策成功 row）
                mem_samples.append((row.get("rss_mb"), row.get("gpu_mb")))
                text = row["text"]
                # Critical 2：只从顶层 finish_reason 判定（choice.finish_reason）
                fr = row.get("finish_reason") or "stop"
                invalid = fp.decision_invalid(text, str(fr), fanout)
                if invalid:
                    action = f"ACTION: branch({fp.fallback_branch(fanout, rep_index or 0)})"
                    fallback = True
                else:
                    action = f"ACTION: branch(b{fp.parse_action(text)})"
                    fallback = False
            except ServerCrash:
                raise
            except Exception as e:
                # rep ERROR（v48 复审，任务 3）：不再提前 return——决策异常
                # 记入 decision_error，仍走统一 erase + after_erase 路径；
                # server 已退出 → ServerCrash → 上层 FORMAL_INCOMPLETE（不伪造观测）。
                if rep_index is None:
                    # v52（任务 3）：warmup 决策请求异常——先 poll 判定：已退出 →
                    # ServerCrash（group ERROR/FORMAL_INCOMPLETE，与工具轮一致）；
                    # 健康 → 记可诊断 note 后上抛（上层 poll 判定后继续，不静默）
                    if (self._adapter is not None
                            and self._adapter.poll() is not None):
                        raise ServerCrash(str(e)) from e
                    self._add_note(
                        f"warmup 决策请求异常（server 健康，继续）: "
                        f"{type(e).__name__}: {e}")
                    raise
                if self._adapter is not None and self._adapter.poll() is not None:
                    raise ServerCrash(str(e)) from e
                decision_error = e
                fallback = False  # v48：决策异常 rep 无决策 fallback 标记
                self.any_rep_error = True
            if decision_error is not None:
                branches = []
            else:
                if fallback:
                    self.any_fallback = True
                # 2) 分支并发（barrier + ThreadPoolExecutor）
                branches = []
                bar = threading.Barrier(fanout)

                def _branch(bx: int) -> Dict[str, Any]:
                    bname = f"b{bx}"
                    canary = fp.canary_for(fanout, bname)
                    bmsgs = fp.branch_messages(msgs, action, bname,
                                               fp.build_branch_task(bucket, bname), canary)
                    bar.wait()
                    brow = self._chat(bmsgs, temperature=TEMPERATURE, seed=SEED,
                                      max_tokens=self.branch_n_predict)
                    latencies.append(brow.get("latency_ms") or 0.0)
                    _t2 = brow.get("timings") or {}
                    ttfts.append(_t2.get("prompt_ms") or 0.0)
                    # v66（任务 4）：分支成功 row 的 GPU/RSS 峰值样本
                    mem_samples.append((brow.get("rss_mb"), brow.get("gpu_mb")))
                    return {"branch": bname, "row": brow, "canary": canary}

                with ThreadPoolExecutor(max_workers=fanout) as ex:
                    futures = [ex.submit(_branch, bx) for bx in range(1, fanout + 1)]
                    for f in futures:
                        try:
                            branches.append(f.result())
                        except Exception as e:
                            # 进程崩溃检测：并发请求失败且 server 已退出 → ServerCrash
                            # （该 rep 归因 server_crash，而非请求级 connection_error）
                            if self._adapter is not None and self._adapter.poll() is not None:
                                raise ServerCrash(str(e)) from e
                            branches.append({"branch": "?", "row": None, "canary": "",
                                             "error": classify_error(e)})
                # 3) 工具轮（M 轮固定注入）；崩溃检测（进程退出 → ServerCrash）
                try:
                    for tround in range(1, self.tool_rounds + 1):
                        for b in branches:
                            if b.get("row") is not None and b.get("error") is None:
                                trow = self._chat(
                                    msgs + [{"role": "assistant", "content": action}] +
                                    [fp.tool_round_message(b["branch"], tround)],
                                    temperature=TEMPERATURE, seed=SEED,
                                    max_tokens=self.branch_n_predict)
                                latencies.append(trow.get("latency_ms") or 0.0)
                                _t3 = trow.get("timings") or {}
                                ttfts.append(_t3.get("prompt_ms") or 0.0)
                                # v66（任务 4）：工具轮成功 row 的 GPU/RSS 峰值样本
                                mem_samples.append((trow.get("rss_mb"), trow.get("gpu_mb")))
                except Exception as e:
                    if self._adapter is not None and self._adapter.poll() is not None:
                        raise ServerCrash(str(e)) from e
                    if rep_index is None:
                        # v51（任务 4）：warmup 工具轮异常（server 健康）→ 记可诊断
                        # note 而非静默忽略；best-effort erase 由 finally 承担
                        self._add_note(
                            f"warmup 工具轮请求异常（server 健康，继续）: "
                            f"{type(e).__name__}: {e}")
                    else:
                        # v50（任务 3）：server 健康但工具轮请求 HTTP 异常——不裸逃逸
                        # exit70：标当前 rep ERROR（废弃工具轮结果），继续统一
                        # erase + after_erase 路径；server 已退出 → ServerCrash
                        # （上一分支）→ group ERROR/FORMAL_INCOMPLETE
                        tool_error = e
        finally:
            if rep_index is not None:
                # v48（任务 4）：异常路径同样 stop periodic——先 stop 采样
                # （end_run 必须等 erase+after_erase 之后，否则 after_erase
                #  样本 run_id=None 不进 rep 聚合——v48 复审根因）
                self._kv.stop()
            else:
                # v49（任务 4）：warmup（rep_index=None）即使 server 健康也执行
                # best-effort erase——决策/分支请求留下的 slot 占位不清会串扰
                # 下一个 warmup/formal rep 的 KV 基线；异常后 poll 判断：
                # 崩溃 → ServerCrash（上层 group ERROR）；健康 → 可继续（仅预热）
                try:
                    attempt, ok = self._kv.clean_all_slots()
                    if attempt > 0 and ok < attempt:
                        # v50（任务 5）：clean_all_slots 不抛异常（list_slots/
                        # erase_slot 内部吞错），真实可达的 erase 异常 = partial
                        # erase（501/部分 slot 失败）→ 记可诊断 note，不静默吞
                        self._add_note(
                            f"warmup best-effort erase 部分失败 {ok}/{attempt} "
                            f"（server 健康，继续）")
                    elif attempt == 0:
                        # v51（任务 2）：(0,0)（无 slot 或 list_slots 端点不可用）
                        # 主动 poll——server 已退出 → ServerCrash（group ERROR）；
                        # 健康且确实无 slot → 允许继续（记可诊断 note）
                        if self._adapter is not None and self._adapter.poll() is not None:
                            raise ServerCrash(
                                "warmup clean_all_slots: server 已退出") from None
                        self._add_note(
                            "warmup clean_all_slots 返回 (0,0)"
                            "（无 slot 或端点不可用；server 健康，继续）")
                except Exception as e:
                    if self._adapter is not None and self._adapter.poll() is not None:
                        raise ServerCrash("warmup erase: server 已退出") from None
                    # v50（任务 5）：防御分支（kv_probe 行为改变才可达）——记录 note
                    self._add_note(
                        f"warmup best-effort erase 异常（server 健康，继续）: "
                        f"{type(e).__name__}: {e}")
        # 4) 回收（erase 全部 slot）+ after_erase（v48：决策 ERROR 与正常统一路径；
        #    任务 3：ERROR rep 不跳过 erase/after_erase——server 健康则 metrics.kv
        #    存在并由 G-M0-3a 验证归零；崩溃 → ServerCrash → FORMAL_INCOMPLETE；
        #    v49：仅 formal rep（rep_index is not None）走严格检查——warmup 的
        #    erase 已由 finally best-effort 承担，严格 partial 检查只对 formal）
        if rep_index is not None:
            try:
                # v49（任务 5）：clean_all_slots 返回 (attempt, ok)——partial erase
                # （attempt>0 且 ok<attempt）→ 立即 EraseFailure（FORMAL_INCOMPLETE，
                # 带诊断，不等 gate 间接发现；G-M0-3a 已无归零观测可依）
                attempt, ok = self._kv.clean_all_slots()
                if attempt > 0 and ok < attempt:
                    raise EraseFailure("erase_partial",
                                       f"clean_all_slots 部分成功 {ok}/{attempt}", ok, attempt)
            except EraseFailure:
                raise
            except ServerCrash:
                raise
            except Exception as e:
                # v50（任务 4）：clean_all_slots 异常（非 partial）与 after_erase
                # 异常分开归因——这里归 erase_failed
                if self._adapter is not None and self._adapter.poll() is not None:
                    raise ServerCrash(str(e)) from e
                raise EraseFailure("erase_failed", str(e)) from e
            try:
                # Critical 1：erase 后采样实际 /metrics/kv——G-M0-3a 从该
                # 样本验证 used_cells==0 && active_sequences==0（last=erase 后值）
                snap = self._kv.snapshot("after_erase")
                if snap is None:
                    # v50（任务 1）：after_erase 观测缺失（fetch 失败/端点不可用/
                    # 非收集阶段）→ 显式 EraseFailure，真实可达分支（不靠 mock
                    # 抛异常）；group ERROR + FORMAL_INCOMPLETE
                    raise EraseFailure(
                        "after_erase_missing",
                        "after_erase 观测缺失：/metrics/kv fetch 失败或端点不可用 "
                        f"(last_error={self._kv.last_error!r})")
            except ServerCrash:
                raise
            except EraseFailure:
                raise
            except Exception as e:
                if self._adapter is not None and self._adapter.poll() is not None:
                    raise ServerCrash(str(e)) from e
                raise EraseFailure("after_erase_failed", str(e)) from e
            finally:
                # end_run 在 erase+after_erase 之后：保证 after_erase 样本归组
                self._kv.end_run()
        # 5) 指标 + rep 对象
        if rep_index is None:
            return {}
        # Critical 1：按当前 rep 的 run_id 取聚合（不含其他 rep / 全局样本），
        # 并包装成 gate/validator 消费的一致结构 {first,last,peak}
        # （G-M0-3a 读 last.used_cells / last.active_sequences）
        kv_agg = self._kv.run_aggregate().get(rep_run_id, {})
        kv_metrics: Dict[str, Any] = {}
        if kv_agg:
            kv_metrics = {
                "first": {"used_cells": kv_agg.get("first_used_cells"),
                          "active_sequences": kv_agg.get("first_active_sequences")},
                "last": {"used_cells": kv_agg.get("last_used_cells"),
                         "active_sequences": kv_agg.get("last_active_sequences")},
                "peak": {"used_cells": kv_agg.get("peak_used_cells"),
                         "active_sequences": kv_agg.get("peak_active_sequences")},
            }
        status = "OK" if not fallback else "INVALID_DECISION"
        has_error = any(b.get("error") for b in branches) or tool_error is not None
        if tool_error is not None:
            # v50（任务 3）：工具轮失败（server 健康）→ rep ERROR，与决策异常同语义
            status = "ERROR"
            fallback = False
            self.any_rep_error = True
        if decision_error is not None:
            # v48（任务 3）：决策请求异常 → rep ERROR（error_type=决策异常分类）
            status = "ERROR"
            fallback = False
            has_error = True
            self.any_rep_error = True
        if has_error:
            # 状态机（v10/v16）：ERROR 必须 decision_fallback=false（错误优先，
            # 清除决策级 fallback 标记——崩溃/请求错误时该 rep 不标记 fallback）
            status = "ERROR"
            if fallback:
                fallback = False
                # 若决策确实 fallback 但 rep 最终 ERROR，不计入 any_fallback
                # （meta.decision_fallback 仅由 rep 级 decision_fallback=true 聚合）
                # 由 _compute_gates 从 rep 字段重算，此处无需扣减——见 _finalize
            self.any_rep_error = True
        # v66（任务 4）：rep 级 GPU/RSS 峰值 = 成功 row 样本取 max（None 过滤）；
        # ERROR rep（decision/tool/分支错误）保留已有成功样本（非 0.0 占位）
        def _peak_mem(idx: int) -> float:
            vals = [s[idx] for s in mem_samples if s[idx] is not None]
            return round(max(vals), 1) if vals else 0.0
        metrics: Dict[str, Any] = {
            "peak_gpu_mb": _peak_mem(1), "peak_rss_mb": _peak_mem(0),
            "kv": kv_metrics,
            "branches": [{"branch": b["branch"],
                          # Critical 2：分支行顶层 finish_reason
                          "finish_reason": (b["row"].get("finish_reason") or "stop")
                          if b.get("row") else "stop",
                          "canary_leak": self._canary_leak(b, branches)}
                         for b in branches],
        }
        avg_lat = (sum(latencies) / len(latencies)) if latencies else None
        avg_ttft = (sum(ttfts) / len(ttfts)) if ttfts else None
        rep = sch.build_rep(unit_id, rep_index, fanout, bucket, ctk, ctv,
                            self._last_prefix_len, self._last_branch_len, gid,
                            status, fallback, metrics,
                            latency_ms=(avg_lat if not has_error else None),
                            ttft_ms=(avg_ttft if not has_error else None))
        if has_error:
            if decision_error is not None:
                rep["error_type"] = classify_error(decision_error)
            elif tool_error is not None:
                # v50（任务 3）：工具轮失败（server 健康）→ error_type 取工具轮
                # 异常分类（如 http_5xx）——原 next(...) 在工具轮失败时 StopIteration
                # 裸逃逸（branches 无 error），已修复
                rep["error_type"] = classify_error(tool_error)
            else:
                rep["error_type"] = next(b["error"] for b in branches if b.get("error"))
            rep["error_stage"] = "formal"
            rep["error_bucket"] = bucket
        return rep

    @staticmethod
    def _canary_leak(b: Dict[str, Any], branches: List[Dict[str, Any]]) -> bool:
        """该分支输出是否含其他分支 canary（G-M0-2 隔离）。"""
        row = b.get("row")
        if not row:
            return False
        text = row.get("text") or ""
        for other in branches:
            oc = other.get("canary")
            if oc and other["branch"] != b["branch"] and oc in text:
                return True
        return False


    # ---- 主流程 ----

    def _add_note(self, text: str) -> None:
        """v50（任务 5）：记录可诊断 note（落盘 doc.notes；不改变 verdict）。"""
        self.notes.append(text)

    def run(self) -> int:
        """完整 24-unit 矩阵一次运行；返回进程退出码（0/64/65/70/74）。"""
        # 1) 校准 + 预算精确复核（run_calibration 内在校准 server 存活期间完成）
        try:
            parity_ok = self.run_calibration()
        except (ServerError, ServerCrash, EraseFailure) as e:
            # v50（任务 2）：校准阶段 ServerCrash/清理异常 → PREFLIGHT_INFRA 落盘
            # v50（review 修复）：保留已收集的 preflight_rejections（校准失败
            # 时预算复核可能已部分完成），不传空列表丢数据
            return self._fail_preflight("validation_incomplete", None,
                                        self.preflight_rejections,
                                        f"{type(e).__name__}: {e}")
        except _INFRA_EXCEPTIONS as e:
            # v51（任务 3）：仅明确的网络/OS 基础设施异常归 PREFLIGHT_INFRA；
            # 内部 bug（AssertionError/KeyError/TypeError 等）不再被吞——
            # 上抛 → run_safe 归 INTERNAL_RUNNER_ERROR + EXIT_SOFTWARE(70)
            return self._fail_preflight("validation_incomplete", None,
                                        self.preflight_rejections,
                                        f"{type(e).__name__}: {e}")
        if len(self.preflight_rejections) == len(sch.EXPECTED_UNIT_IDS):
            # ALL_BUDGET_REJECTED
            return self._fail_preflight("budget_rejected", False, self.preflight_rejections, "")
        if not parity_ok:
            # PARITY_MISMATCH（统一判定：parity_ok=false + completed 全集）
            return self._fail_preflight("parity_mismatch", False, self.preflight_rejections, "")
        # 3) decision validation（G-M0-1 数据源，2×≥10）
        complete, sessions = self.run_decision_validation()
        if not complete:
            # v59：partial dv（保留已完成 session 证据），以合法 preflight 结果落盘；
            # 不丢 decision_sessions 中已完成部分
            self.decision_validation = sch.build_decision_validation(
                self.decision_sessions, partial=True)
            return self._fail_preflight("validation_incomplete", True, self.preflight_rejections, "")
        self.decision_validation = sch.build_decision_validation(sessions, partial=False)
        # 4) formal 矩阵（12 groups）
        try:
            self.run_formal()
            self.phase = "formal"
        except FirstGroupStartFailed as e:
            # 首 group 启动失败 → preflight/PREFLIGHT_INFRA（保留完整 decision_validation）
            return self._fail_preflight("validation_incomplete", True,
                                        self.preflight_rejections, str(e))
        except FormalIncomplete:
            self.phase = "formal"
            self.rule = "FORMAL_INCOMPLETE"
        # 5) gates + 落盘
        return self._finalize()

    def run_safe(self) -> int:
        """run() 的兜底入口（Critical 4）：任何未预期异常 → 固定 stderr 错误码 +
        EXIT_SOFTWARE(70)，绝不 traceback / exit 1（OSError 已在 _start_server
        转 ServerError；此处只捕获内部 bug 与遗漏路径）。"""
        try:
            return self.run()
        except Exception as e:
            print(f"INTERNAL_RUNNER_ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            return sch.EXIT_SOFTWARE

    def _fail_preflight(self, reason: str, parity_ok: Optional[bool],
                        rejections: List[Dict[str, Any]], detail: str) -> int:
        """落盘 preflight envelope（PREFLIGHT_INFRA / PARITY_MISMATCH / ALL_BUDGET_REJECTED）。"""
        tcm = sch.TOKEN_COUNT_METHOD if parity_ok is not None else None
        # parity_ok 总规则（v35）：全部桶完成且偏差=0 → true；完成但偏差≠0 → false；
        # 未完整完成 → null。reason=validation_incomplete 且 parity 已通过 → true。
        if reason == "validation_incomplete":
            mismatched = any(e["code"] == "parity_mismatch"
                             for e in self.parity_progress["errors"])
            completed_all = len(self.parity_progress["completed"]) == len(sch.PARITY_BUCKETS)
            if completed_all and not mismatched:
                parity_ok = True
                tcm = sch.TOKEN_COUNT_METHOD
            elif completed_all and mismatched:
                parity_ok = False
                tcm = sch.TOKEN_COUNT_METHOD
            else:
                parity_ok = None
                tcm = None
        dv = self.decision_validation
        context = self._meta_context()
        doc = sch.build_preflight_envelope(
            context, reason=reason, source_phase=self.phase,
            parity_ok=parity_ok, parity_progress=self.parity_progress,
            token_count_method=tcm, decision_validation=dv,
            preflight_rejections=rejections,
            notes=self.notes)  # v51：保留 runner 已有 notes（warmup erase 等诊断）
        ok, errs = sch.validate_result_envelope(doc)
        if not ok:
            return self._write_schema_invalid(errs, "preflight")
        return self._write_result(doc)

    def _meta_context(self) -> Dict[str, Any]:
        return {"meta": {
            "workload": "fanout",
            "design_ref": "M0_BRANCH_MEMORY_BASELINE_DESIGN.md",
            "model_id": self.model_id,
            "binary_version": self.binary_version,
            "ctx_size": self.ctx_size,
            "cache_profiles": [{"ctk": "q8_0", "ctv": "q8_0"}, {"ctk": "f16", "ctv": "f16"}],
            "protocol": "OAI /v1/chat/completions, temp=0, seed=42, no-think",
        }}

    def _finalize(self) -> int:
        """formal 结果：构造 doc → gates → verdict → 校验 → 原子落盘。"""
        modes = sch.build_modes(self.groups_off, self.groups_on)
        if not self.groups_off and not self.groups_on:
            # 防御性兜底（v60：0 group 理论不可达——首 group 任何失败转
            # FirstGroupStartFailed → preflight 落盘；FormalIncomplete 仅在
            # 已有 ≥1 completed group 后抛出）
            return self._fail_preflight("validation_incomplete", True,
                                        self.preflight_rejections, "formal 无 group")
        gates = self._compute_gates(modes)
        # meta.decision_fallback（v34）：仅 formal 任一 rep decision_fallback=true
        # 时 true——从落盘 rep 字段重算（ERROR rep 的 fallback 标记已清除）
        any_fallback = False
        for control in ("off", "on"):
            for grp in modes.get(control, {}).get("server_groups", []):
                for rep in grp.get("replicates", []):
                    if rep.get("decision_fallback"):
                        any_fallback = True
        self.any_fallback = any_fallback
        doc = self._meta_context()
        doc["meta"].update({
            "source_phase": "formal", "phase": "formal",
            "preflight_status": "N/A", "preflight_reason": None,
            "parity_ok": True,
            "parity_progress": self.parity_progress,
            "parity_compensation": None,
            "token_count_method": sch.TOKEN_COUNT_METHOD,
            "decision_validation": self.decision_validation,
            "decision_validated": (gates["G-M0-1"]["status"] == "PASS"),
            "decision_fallback": self.any_fallback,
            "preflight_rejections": self.preflight_rejections,
            "planned_units": sch.planned_units_of(),
            "executed_units": self._executed_units(modes),
            "matrix_complete": self._matrix_complete(modes),
        })
        doc["modes"] = modes
        doc["gates"] = gates
        doc["notes"] = list(self.notes)  # v50：含 warmup erase 等可诊断 note
        any_rej = len(self.preflight_rejections) > 0
        doc["verdict"] = sch.aggregate_verdict(
            doc, self.any_fallback, self.any_rep_error, any_rej)
        ok, errs = sch.validate_result_envelope(doc)
        if not ok:
            return self._write_schema_invalid(errs, "formal")
        return self._write_result(doc)

    def _executed_units(self, modes: Dict[str, Any]) -> int:
        complete = self._complete_units(modes)
        return len(complete)

    def _matrix_complete(self, modes: Dict[str, Any]) -> bool:
        rej = set(self._rejected_ids())
        expected = set(sch.EXPECTED_UNIT_IDS) - rej
        complete = self._complete_units(modes)
        return complete == expected

    @staticmethod
    def _complete_units(modes: Dict[str, Any]) -> set:
        by_unit: Dict[str, set] = {}
        for control in ("off", "on"):
            for grp in modes.get(control, {}).get("server_groups", []):
                for rep in grp.get("replicates", []):
                    by_unit.setdefault(rep["unit_id"], set()).add(rep.get("rep_index"))
        return {u for u, idxs in by_unit.items()
                if idxs == set(range(sch.FORMAL_REPS))}

    @staticmethod
    def _parse_rs_buffer_mib(log: str) -> Optional[float]:
        """从 server 启动日志解析 RS buffer 总大小（MiB）。

        真实 llama.cpp 行（llama-memory-recurrent.cpp:115，LLAMA_LOG_INFO）：
        "llama_memory_recurrent::init: <buf> RS buffer size =  50.25 MiB"
        v63：真实 llama.cpp 可能多次打印同一总行（每 buffer 构建阶段重复输出
        201.00 MiB 等）——取**首个匹配值**而非求和（求和会把重复打印的总行
        误加成 2 倍导致 G-M0-4 超差，2026-08-09 完整 4B 矩阵实测暴露）；
        无匹配 → None（观测缺失）。
        """
        m = re.search(r"RS buffer size =\s*([\d.]+) MiB", log)
        return round(float(m.group(1)), 3) if m else None

    def _compute_gates(self, modes: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        gates: Dict[str, Dict[str, Any]] = {}
        # G-M0-1：专用验证合法率 100%
        dv = self.decision_validation
        g1 = "FAIL"
        if dv is not None and not dv.get("partial"):
            total_req = sum(s["requests"] for s in dv["sessions"])
            total_valid = sum(s["valid"] for s in dv["sessions"])
            if total_req >= 2 * VALIDATION_REQUESTS and total_valid == total_req:
                g1 = "PASS"
        gates["G-M0-1"] = {"status": g1}
        # G-M0-2：canary 跨分支泄漏率 0（Critical 1：观测缺失 → FAIL，不静默 PASS；
        # v49 任务 1：无任何非 ERROR 分支观测 → NOT_APPLICABLE，绝不 PASS）
        leak = False
        leak_na = False
        any_branch_obs = False
        for control in ("off", "on"):
            for grp in modes.get(control, {}).get("server_groups", []):
                for rep in grp.get("replicates", []):
                    if rep.get("status") == "ERROR":
                        continue  # 分支无输出，隔离性不可测（由 REP_ERROR 顶层处理）
                    branches = rep.get("metrics", {}).get("branches")
                    if not isinstance(branches, list) or not branches:
                        leak_na = True  # 缺分支观测
                        continue
                    any_branch_obs = True
                    for b in branches:
                        if b.get("canary_leak"):
                            leak = True
        if leak:
            gates["G-M0-2"] = {"status": "FAIL"}
        elif leak_na:
            gates["G-M0-2"] = {"status": "FAIL"}
        elif not any_branch_obs:
            # v49 任务 1：全矩阵无任何非 ERROR 分支观测（如 5 reps 全 ERROR）——
            # 隔离性不可测，NOT_APPLICABLE（顶层 verdict 由 REP_ERROR/FORMAL_INCOMPLETE
            # 处理，不得静默 PASS）
            gates["G-M0-2"] = {"status": "NOT_APPLICABLE"}
        else:
            gates["G-M0-2"] = {"status": "PASS"}
        # G-M0-3a：每 rep 末 erase 后 used_cells==0 && active_sequences==0
        # （Critical 1：after_erase 样本缺失/字段非 int → FAIL，绝不 None 即 PASS）
        rec_ok = True
        for control in ("off", "on"):
            for grp in modes.get(control, {}).get("server_groups", []):
                for rep in grp.get("replicates", []):
                    kv = rep.get("metrics", {}).get("kv", {})
                    last = kv.get("last", {}) if isinstance(kv, dict) else {}
                    if (not isinstance(last, dict)
                            or not isinstance(last.get("used_cells"), int)
                            or not isinstance(last.get("active_sequences"), int)
                            or last["used_cells"] != 0
                            or last["active_sequences"] != 0):
                        rec_ok = False
        gates["G-M0-3a"] = {"status": "PASS" if rec_ok else "FAIL"}
        gates["G-M0-3b"] = {"status": "NOT_APPLICABLE"}  # smoke 观测，非门禁
        # G-M0-4（Critical 10，v48 复审）：RS buffer 对账——derived 与独立观测分离。
        #   - derived 自检：baseline.rs_buffer_mb 与公式 RS_BYTES_PER_ROW*parallel 一致
        #     （防内部公式 bug；**仅 internal consistency detail，不参与 gate status**——
        #     RS_BYTES_PER_ROW=24*548864*4 经 v55 真实 4B GPU 短校准实测一致
        #     （201.00 MiB@p4 / 502.50 MiB@p10 = 50.25 MiB/parallel，2026-08-09），
        #     不宣称已验证）；
        #   - gate 仅由真实 server 启动日志独立观测决定（llama-memory-recurrent.cpp:115
        #     "RS buffer size = X MiB"）：无任何观测 → NOT_APPLICABLE（明确，非 PASS）；
        #     有观测且超差（>G4_TOLERANCE）→ FAIL；容差内 → PASS。
        #   - group 匹配用 sch.parse_group_id(server_group_id)（build_group 不写
        #     ctk/ctv/fanout 字段，不得 grp.get("ctk") 读取）。
        g4_derived_consistent = True
        g4_obs_ok = True
        g4_obs_found = False
        # 重建与 run_formal 相同的 group 序号（tag=f"g{gi}" → 日志 key）
        seq: List[Tuple[str, str, str, str, int]] = []
        for control in fp.CONTROLS:
            for (ctk, ctv) in fp.CACHE_PROFILES:
                for fanout in fp.FANOUTS:
                    if fanout not in fp.legal_matrix_plan():
                        continue
                    seq.append((f"g{len(seq)}", control, ctk, ctv, fanout))
        for gi, (tag, control, ctk, ctv, fanout) in enumerate(seq):
            def _g4_match(g: Dict[str, Any]) -> bool:
                pid = sch.parse_group_id(str(g.get("server_group_id", "")))
                if pid is None:
                    return False
                return (pid["control"] == control and pid["ctk"] == ctk
                        and pid["ctv"] == ctv and pid["fanout"] == str(fanout))
            grp = next((g for g in modes.get(control, {}).get("server_groups", [])
                        if _g4_match(g)), None)
            if grp is None:
                continue
            bs = grp.get("baseline")
            if not isinstance(bs, dict) or not isinstance(bs.get("rs_buffer_mb"), (int, float)):
                g4_derived_consistent = False
                continue
            expected_mb = RS_BYTES_PER_ROW * (fanout + 2) / (1024 * 1024)
            if expected_mb and abs(bs["rs_buffer_mb"] - expected_mb) / expected_mb > 0.001:
                g4_derived_consistent = False
            # 独立观测（该 group 的 server 启动日志）
            log = self._server_logs.get(tag, "")
            obs = self._parse_rs_buffer_mib(log)
            if obs is None:
                continue
            g4_obs_found = True
            if expected_mb and abs(obs - expected_mb) / expected_mb > G4_TOLERANCE:
                g4_obs_ok = False
        g4_status = ("FAIL" if (g4_obs_found and not g4_obs_ok)
                     else "NOT_APPLICABLE" if not g4_obs_found else "PASS")
        g4_gate: Dict[str, Any] = {"status": g4_status}
        if not g4_derived_consistent:
            g4_gate["notes"] = ("derived formula internal inconsistency "
                                "(non-gating detail); gate decided by RS log observation only")
        gates["G-M0-4"] = g4_gate
        # G-M0-5：对照有效性（off/on 均 shared_cells==0；on 日志含 hybrid 拒绝）
        # Critical 9：真实 llama.cpp 行（server-context.cpp:1505，SRV_WRN，仅
        # --kv-prefix-share 且 hybrid 模型时输出）：
        #   "E8-C1: capability rejected: hybrid (recurrent+attention) model"
        # 匹配其稳定前缀；FakeAdapter 不再专造行（返回真实格式）。
        g5 = "PASS"
        on_log = self._on_session_log()
        if "E8-C1: capability rejected:" not in on_log:  # v63：真实 4B 行为 "memory implementation does not support cross-slot prefix metadata sharing"（同前缀）
            g5 = "FAIL"
        for control in ("off", "on"):
            for grp in modes.get(control, {}).get("server_groups", []):
                kv = grp.get("baseline", {}).get("metrics_kv_snapshot", {}) if isinstance(
                    grp.get("baseline"), dict) else {}
                # Critical 1：shared_cells 必须存在且 == 0；缺观测 → FAIL
                shared = kv.get("shared_cells") if isinstance(kv, dict) else None
                if not isinstance(shared, int) or shared != 0:
                    g5 = "FAIL"
        gates["G-M0-5"] = {"status": g5}
        # G-M0-6：复现（同配置两次独立 run 对比——单次运行无法判定 → NOT_APPLICABLE）
        gates["G-M0-6"] = {"status": "NOT_APPLICABLE"}
        # G-M0-7：落盘完整性（validator 已执行；此处由外层校验通过 → PASS）
        gates["G-M0-7"] = {"status": "PASS"}
        return gates

    def _on_session_log(self) -> str:
        # 取全部 server 日志（尽力而为）；目录不存在/无日志 → ""（G-M0-5 依赖日志，
        # mock 环境通过 FakeAdapter.read_log 提供 hybrid 拒绝行）
        if not os.path.isdir(self.tmp_dir):
            return ""
        log = ""
        for fn in sorted(os.listdir(self.tmp_dir)):
            if fn.startswith("server_") and fn.endswith(".log"):
                try:
                    with open(os.path.join(self.tmp_dir, fn), "r", encoding="utf-8") as f:
                        log += f.read()
                except OSError:
                    # v53（Medium 5）：OSError 含文件/权限类前置错误（目录被清理 /
                    # 权限变化）——尽力而为，缺失日志不阻断 G-M0-5 归因
                    pass
        return log

    # ---- 落盘 ----

    def _write_result(self, doc: Dict[str, Any]) -> int:
        """原子写主结果；写失败 → IO_WRITE_FAILED（74）。"""
        if not self.out_path:
            return 0  # 无 --out 时仅构造不落盘（测试用）
        try:
            sch.write_result_pair(self.out_path, doc)
            return 0
        except OSError as e:
            print(f"IO_WRITE_FAILED: {e}", file=sys.stderr)
            return sch.EXIT_IO_WRITE_FAILED

    def _write_schema_invalid(self, errs: List[Dict[str, Any]], source_phase: str) -> int:
        """SCHEMA_INVALID：合法 envelope + 结构化 sidecar（v30/v31），exit 65。"""
        context = self._meta_context()
        envelope = sch.build_preflight_envelope(
            context, reason="schema_invalid", source_phase=source_phase,
            parity_ok=None, parity_progress={"completed": [], "errors": []},
            token_count_method=None, decision_validation=None,
            preflight_rejections=[])
        ok, env_errs = sch.validate_result_envelope(envelope)
        if not ok:
            # envelope 自身校验失败 → 内部 bug，不得递归包装
            print(f"INTERNAL_SCHEMA_ENVELOPE_BUG: {env_errs}", file=sys.stderr)
            return sch.EXIT_SOFTWARE
        sidecar = {"errors": [
            {"path": sch.to_json_pointer(e.get("path", "$")), "code": e.get("code", "invalid_value"),
             "expected_type": e.get("expected_type", "object"),
             "actual_type": e.get("actual_type", "null")}
            for e in errs]}
        s_ok, s_errs = sch.validate_schema_sidecar(sidecar)
        if not s_ok:
            print(f"INTERNAL_SCHEMA_ENVELOPE_BUG: sidecar {s_errs}", file=sys.stderr)
            return sch.EXIT_SOFTWARE
        if not self.out_path:
            return sch.EXIT_SCHEMA_INVALID
        sidecar_path = os.path.join(
            os.path.dirname(os.path.abspath(self.out_path)),
            f"{os.path.basename(self.out_path).rsplit('.', 1)[0]}.schema_invalid.json")
        try:
            sch.write_result_pair(self.out_path, envelope, sidecar_path, sidecar)
            return sch.EXIT_SCHEMA_INVALID
        except OSError as e:
            print(f"IO_WRITE_FAILED: {e}", file=sys.stderr)
            return sch.EXIT_IO_WRITE_FAILED


class FirstGroupStartFailed(Exception):
    """第 1 个 formal group 启动失败 → preflight/PREFLIGHT_INFRA（测试㉖）。"""


class FormalIncomplete(Exception):
    """第 2+ group 启动失败 / 运行中崩溃 → formal/FORMAL_INCOMPLETE（测试㉗）。"""


class EraseFailure(Exception):
    """v49（任务 2/5）：erase 阶段失败（server 健康但 clean_all_slots 部分成功/
    after_erase 采样异常）→ 立即 group ERROR/FORMAL_INCOMPLETE 并带可诊断 error_type
    （不裸异常 exit70、不等 gate 间接发现；优先后者以免污染后续 rep）。
    server 已退出 → 仍由上层归 ServerCrash。"""

    def __init__(self, error_type: str, detail: str, ok: int = 0, attempt: int = 0):
        super().__init__(f"{error_type}: {detail}")
        self.error_type = error_type
        self.detail = detail
        self.ok = ok
        self.attempt = attempt


class ServerCrash(Exception):
    """server 运行中崩溃（进程退出）。"""


def classify_error(e: Exception) -> str:
    # v66：按异常类型 + status_code 精确分类（openai SDK 2.x，优先于文本启发式）；
    # 文本 fallback 保留（urllib.HTTPError / 旧路径）。
    if isinstance(e, openai.APITimeoutError):
        return "timeout"
    if isinstance(e, openai.APIConnectionError):
        return "connection_error"
    if isinstance(e, openai.APIResponseValidationError):
        return "malformed_response"
    if isinstance(e, openai.APIStatusError):
        code = getattr(e, "status_code", 0) or 0
        if 400 <= code < 500:
            return "http_4xx"
        if 500 <= code < 600:
            return "http_5xx"
        # 其他 status（3xx/构造不全）→ 落入下方文本 fallback
    text = str(e)
    if isinstance(e, urllib.error.HTTPError):
        code = getattr(e, "code", 0)
        if 400 <= code < 500:
            return "http_4xx"
        if 500 <= code < 600:
            return "http_5xx"
    low = text.lower()
    # openai SDK 状态异常（如 InternalServerError "Error code: 500"）
    m = __import__("re").search(r"error code[: ](\d+)", low)
    if m:
        code = int(m.group(1))
        return "http_4xx" if 400 <= code < 500 else "http_5xx"
    if isinstance(e, (TimeoutError,)):
        return "timeout"
    if "timed out" in low:
        return "timeout"
    if "connection" in low or "refused" in low or "connect" in low:
        return "connection_error"
    return "connection_error"


# ---- CLI（v44：未知参数 → EXIT_USAGE 64；--warmup/--reps 等 8 项未知） ----

UNKNOWN_ARGS = ("--fanout", "--prefix-len", "--branch-len", "--ctk",
                "--ctv", "--parallel", "--warmup", "--reps")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"INVALID_CONFIGURATION: {message}", file=sys.stderr)
        raise SystemExit(sch.EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="m0_fanout_runner", description="M0 fan-out 矩阵 runner")
    parser.add_argument("--server-bin", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--ctx-size", type=int, default=4096)
    parser.add_argument("--decision-n-predict", type=int, default=16)
    parser.add_argument("--branch-n-predict", type=int, default=64)
    parser.add_argument("--tool-rounds", type=int, default=2)
    parser.add_argument("--out", default="")
    parser.add_argument("--tmp-dir", default="")
    parser.add_argument("--port-base", type=int, default=8080)
    parser.add_argument("--ngl", type=int, default=99)
    parser.add_argument("--cleanup-tmp-age", type=float, default=3600.0,
                        help="陈旧 .m0_fanout_*.tmp 清理 age 阈值（秒，v48；v60："
                             "下限 300s 钳制——低于 300 自动提到 300，防误删活跃 run）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 未知参数预检（argparse 会拦截，此处显式保证 8 项清单语义与 stderr 文案）
    for a in argv:
        if a in UNKNOWN_ARGS or a.startswith("--warmup") or a.startswith("--reps"):
            print(f"INVALID_CONFIGURATION: 未知参数 {a}", file=sys.stderr)
            return sch.EXIT_USAGE
    args = build_parser().parse_args(argv)
    if args.ctx_size <= 0:
        print("INVALID_CONFIGURATION: --ctx-size 必须为正整数", file=sys.stderr)
        return sch.EXIT_USAGE
    # v60（任务 3）：--cleanup-tmp-age 合理最小值钳制（<300s 提到 300s）——
    # 共享父目录并发清理禁止；marker 未写入的目录按 age 判定，过低阈值有
    # 误删刚结束 run 目录的风险（文档 v60 修订注记明确）
    if args.cleanup_tmp_age < sch.CLEANUP_AGE_MIN:
        print(f"cleanup-tmp-age {args.cleanup_tmp_age}s 低于下限 "
              f"{sch.CLEANUP_AGE_MIN}s，钳制为 {sch.CLEANUP_AGE_MIN}s", file=sys.stderr)
        args.cleanup_tmp_age = sch.CLEANUP_AGE_MIN
    # Critical 5：CLI 入口清理 results 目录陈旧 atomic tmp 残留
    # （v48，任务 9：仅删超过 age 阈值的陈旧文件，不删活跃并发写；
    #   atomic_write_json 命名 .m0_fanout_<name>.<kind>.<pid>.<rand>.tmp 与 cleanup 匹配）
    if args.out:
        sch.cleanup_stale_tmp(os.path.dirname(os.path.abspath(args.out)),
                              args.cleanup_tmp_age)
        # v59（任务 6）：清理 results 目录陈旧 M0 专属子目录（-lv5 完整日志保留
        # 在 results 供排障；此处仅清理超过 age 的陈旧目录，不删活跃/父目录）
        sch.cleanup_stale_dirs(os.path.dirname(os.path.abspath(args.out)),
                               args.cleanup_tmp_age)
    # v61（任务 4）：--tmp-dir 未提供时默认父目录 = results 目录（dirname(out)），
    # 使 ACTIVE.marker cleanup 扫描域一致（cleanup_stale_dirs 与 runner tmp 同域），
    # 避免系统 /tmp 目录永不清；显式 --tmp-dir 优先。
    tmp_parent = args.tmp_dir
    if not tmp_parent and args.out:
        tmp_parent = os.path.dirname(os.path.abspath(args.out))
    runner = M0FanoutRunner(
        server_bin=args.server_bin, model=args.model, ctx_size=args.ctx_size,
        port_base=args.port_base, decision_n_predict=args.decision_n_predict,
        branch_n_predict=args.branch_n_predict, tool_rounds=args.tool_rounds,
        out_path=args.out, tmp_dir=tmp_parent, ngl=args.ngl,
    )
    # Critical 4：兜底入口（无 traceback / exit 1）
    return runner.run_safe()


if __name__ == "__main__":
    sys.exit(main())
