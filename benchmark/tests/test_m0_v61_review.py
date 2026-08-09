"""M0 v61 审查修复测试（唯一 Critical：run_formal group 生命周期 finally + 调用计数）。

任务 1（Critical）：从 _start_server 成功起，整个 _connect/_capture_baseline/
  warmup/formal/异常归因由外层 try/finally 无条件 _stop_server——任何异常
  （基础设施、FirstGroupStartFailed、FormalIncomplete、内部 bug）均不得泄漏
  server。v60 只在 unit 循环 finally stop，connect/baseline 异常路径泄漏。
任务 3：调用计数——首 group connect 异常、第 2+ baseline 异常、内部 KeyError
  三类均断言 stop 调用、adapter/driver/kv 清空；真实短进程端口释放测试。
任务 5：_stop_server 返回契约收紧（非 tuple/非 bool → 诊断失败；bool False 记录）。
任务 4：main 默认 tmp 父目录 = dirname(out)（避免系统 /tmp 永不清）。

全部不依赖 GPU/真实 llama-server（端口释放测试用 python -m http.server）。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import openai
import pytest

from runner import m0_fanout_runner as m0r
from runner import m0_schema as sch


class CountingStopAdapter:
    """stop 计数 + 返回 (True, "ok")；start/wait_health 最小 mock。"""

    stop_calls = 0  # 类级计数，每测试重置

    def __init__(self, cmd, log_path, port, crash_at=None):
        self.cmd = cmd
        self.log_path = log_path
        self.port = port

    def start(self):
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write("llama_memory_recurrent::init: RS buffer size = 502.50 MiB\n")

    def wait_health(self, timeout=90.0):
        return True

    def poll(self):
        return None  # 存活

    def read_log(self):
        with open(self.log_path, encoding="utf-8") as f:
            return f.read()

    def stop(self):
        CountingStopAdapter.stop_calls += 1
        return (True, "ok")


class OddStopAdapter:
    """stop 返回非预期契约（None / False / int）——任务 5 收紧用。"""

    def __init__(self, cmd, log_path, port, crash_at=None, stop_result=None):
        self.stop_result = stop_result

    def stop(self):
        return self.stop_result


class _FakeDrv:
    """fake driver：chat 返回合法决策行。"""

    def chat(self, messages, **kw):
        return {"text": "ACTION: branch(b1)", "finish_reason": "stop"}


def _make_runner(tmp_path, adapter_cls=CountingStopAdapter, **over):
    args = dict(
        server_bin="fake-bin", model="mock.gguf", ctx_size=4096,
        port_base=18080, decision_n_predict=16, branch_n_predict=64,
        tool_rounds=1, out_path=str(tmp_path / "out.json"),
        tmp_dir="", ngl=99, adapter_cls=adapter_cls,
    )
    args.update(over)
    return m0r.M0FanoutRunner(**args)


def _formal_ready(r):
    r.parity_progress = {"completed": list(sch.PARITY_BUCKETS), "errors": []}
    r.binary_version = "test-version"


def _install_fake_run_unit(r, monkeypatch, *, fail=None):
    """monkeypatch _run_unit：正常返回 build_rep；fail 为抛出的异常实例。"""

    def fake(unit_id, fanout, bucket, ctk, ctv, gid, rep_index):
        if fail is not None:
            raise fail
        r._last_prefix_len = 260
        r._last_branch_len = 344
        if rep_index is None:
            return {"unit_id": unit_id, "warmup": True}
        return sch.build_rep(
            unit_id, rep_index, fanout, bucket, ctk, ctv,
            260, 344, gid, "OK", False, {"kv": {}, "branches": []})

    monkeypatch.setattr(r, "_run_unit", fake)


class TestFormalFinallyStop:
    """任务 1+3：run_formal 任何异常路径均 finally stop + 清空。"""

    def test_first_group_connect_error_stops_server(self, tmp_path, monkeypatch):
        """首 group _connect 基础设施异常 → FirstGroupStartFailed，但 server
        必须 finally stop（v60 泄漏点修复）；adapter/driver/kv 清空。"""
        r = _make_runner(tmp_path)
        CountingStopAdapter.stop_calls = 0
        _formal_ready(r)

        def fake_connect(port):
            raise OSError("connection refused")

        monkeypatch.setattr(r, "_connect", fake_connect)
        with pytest.raises(m0r.FirstGroupStartFailed):
            r.run_formal()
        assert CountingStopAdapter.stop_calls == 1  # 该 group stop 一次
        assert r._adapter is None
        assert r._driver is None
        assert r._kv is None

    def test_first_group_baseline_error_stops_server(self, tmp_path, monkeypatch):
        """首 group _capture_baseline 基础设施异常 → FirstGroupStartFailed，
        同样 finally stop。"""
        r = _make_runner(tmp_path)
        CountingStopAdapter.stop_calls = 0
        _formal_ready(r)
        monkeypatch.setattr(r, "_connect", lambda port: (_FakeDrv(), None))

        def fake_baseline(gid):
            raise openai.APIConnectionError(request=None)

        monkeypatch.setattr(r, "_capture_baseline", fake_baseline)
        with pytest.raises(m0r.FirstGroupStartFailed):
            r.run_formal()
        assert CountingStopAdapter.stop_calls == 1
        assert r._adapter is None and r._driver is None and r._kv is None

    def test_second_group_baseline_error_stops_server(self, tmp_path, monkeypatch):
        """第 2+ group baseline 异常 → FormalIncomplete：两个 group 各 stop 一次
        （group1 正常 COMPLETED + group2 失败），adapter/driver/kv 清空。"""
        r = _make_runner(tmp_path)
        CountingStopAdapter.stop_calls = 0
        _formal_ready(r)
        monkeypatch.setattr(r, "_connect", lambda port: (_FakeDrv(), None))
        _install_fake_run_unit(r, monkeypatch)
        baseline_calls = {"n": 0}

        def fake_baseline(gid):
            baseline_calls["n"] += 1
            if baseline_calls["n"] >= 2:
                raise openai.APIConnectionError(request=None)
            return {"rss_mb": 100.0, "kv": {}}

        monkeypatch.setattr(r, "_capture_baseline", fake_baseline)
        with pytest.raises(m0r.FormalIncomplete):
            r.run_formal()
        assert CountingStopAdapter.stop_calls == 2  # group1 + group2 各一次
        assert r._adapter is None and r._driver is None and r._kv is None
        # group1 完整 COMPLETED、group2 失败 ERROR（endpoint_unavailable）
        assert len(r.groups_off) == 2
        assert r.groups_off[0]["status"] == "COMPLETED"
        assert r.groups_off[1]["status"] == "ERROR"

    def test_internal_keyerror_stops_server(self, tmp_path, monkeypatch):
        """内部 bug（KeyError）传播到 run_formal 之外，但 finally 仍 stop + 清空。"""
        r = _make_runner(tmp_path)
        CountingStopAdapter.stop_calls = 0
        _formal_ready(r)
        monkeypatch.setattr(r, "_connect", lambda port: (_FakeDrv(), None))
        monkeypatch.setattr(r, "_capture_baseline",
                            lambda gid: {"rss_mb": 100.0, "kv": {}})
        _install_fake_run_unit(r, monkeypatch, fail=KeyError("internal_bug"))
        with pytest.raises(KeyError):
            r.run_formal()
        assert CountingStopAdapter.stop_calls == 1
        assert r._adapter is None and r._driver is None and r._kv is None


class TestStopContractTightened:
    """任务 5：_stop_server 返回契约收紧——非 tuple/非 bool → 诊断失败；bool False 记录。"""

    def test_stop_none_diagnostic_failure(self, tmp_path):
        r = _make_runner(tmp_path, adapter_cls=OddStopAdapter)
        r._adapter = OddStopAdapter("x", "y", 1, stop_result=None)
        clean = r._stop_server()
        assert clean is False
        assert any("stop 返回异常类型 NoneType" in e for e in r._stop_errors)

    def test_stop_false_bool_recorded(self, tmp_path):
        r = _make_runner(tmp_path, adapter_cls=OddStopAdapter)
        r._adapter = OddStopAdapter("x", "y", 1, stop_result=False)
        clean = r._stop_server()
        assert clean is False
        assert any("stop 返回 False" in e for e in r._stop_errors)

    def test_stop_int_diagnostic_failure(self, tmp_path):
        r = _make_runner(tmp_path, adapter_cls=OddStopAdapter)
        r._adapter = OddStopAdapter("x", "y", 1, stop_result=42)
        clean = r._stop_server()
        assert clean is False
        assert any("stop 返回异常类型 int" in e for e in r._stop_errors)


class TestMainDefaultTmpParent:
    """任务 4：main 默认 tmp 父目录 = dirname(out)。"""

    def test_main_default_tmp_parent_is_dirname_out(self, tmp_path, monkeypatch):
        out = tmp_path / "results" / "bench.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        captured: dict = {}

        class FakeRunner:
            def __init__(self, **kw):
                captured.update(kw)

            def run_safe(self):
                return 0

        monkeypatch.setattr(m0r, "M0FanoutRunner", FakeRunner)
        monkeypatch.setattr(m0r.sch, "cleanup_stale_tmp", lambda *a, **k: None)
        monkeypatch.setattr(m0r.sch, "cleanup_stale_dirs", lambda *a, **k: None)
        monkeypatch.setattr(
            sys, "argv",
            ["m0_fanout_runner", "--server-bin", "fake-bin",
             "--model", "mock.gguf", "--out", str(out)])
        assert m0r.main() == 0
        assert captured["tmp_dir"] == str(out.parent)

    def test_main_explicit_tmp_dir_wins(self, tmp_path, monkeypatch):
        out = tmp_path / "results" / "bench.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        explicit = tmp_path / "custom-tmp"
        captured: dict = {}

        class FakeRunner:
            def __init__(self, **kw):
                captured.update(kw)

            def run_safe(self):
                return 0

        monkeypatch.setattr(m0r, "M0FanoutRunner", FakeRunner)
        monkeypatch.setattr(m0r.sch, "cleanup_stale_tmp", lambda *a, **k: None)
        monkeypatch.setattr(m0r.sch, "cleanup_stale_dirs", lambda *a, **k: None)
        monkeypatch.setattr(
            sys, "argv",
            ["m0_fanout_runner", "--server-bin", "fake-bin",
             "--model", "mock.gguf", "--out", str(out),
             "--tmp-dir", str(explicit)])
        assert m0r.main() == 0
        assert captured["tmp_dir"] == str(explicit)


class TestRealShortProcessPortRelease:
    """任务 3（可选）：真实短进程 kill+wait 后端口可立即重新 bind。"""

    def test_port_released_after_terminate_wait(self):
        port = 18441
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.1)
            proc.terminate()
            proc.wait(timeout=10)
            for _ in range(50):
                try:
                    s = socket.socket()
                    s.bind(("127.0.0.1", port))
                    s.close()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                pytest.fail("端口未释放")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
