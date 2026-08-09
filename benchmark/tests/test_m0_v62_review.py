"""M0 v62 stop 语义最终复审测试（v61 复审修复）。

任务 1：ServerAdapter.stop——SIGTERM 2s 超时后 SIGKILL+wait 成功 = 进程已清理，
  返回 (True, "SIGTERM timeout, killed")（clean kill 兜底），不得 FormalIncomplete；
  只有 kill/wait 后仍存活、stop 抛异常或无法确认退出才返回 False。
任务 2：_stop_server 保留 detail；非 clean 原因与 kill 兜底 warning 写 self.notes
  （具体文本），不只 _stop_errors；clean kill 兜底不阻断后续 group。
任务 3：stop SIGTERM 超时但 kill 成功 → 矩阵继续；kill 后仍失败 → FormalIncomplete；
  detail 落 notes；start 失败不重复 stop。
任务 4：FakeAdapter.stop 无 server 也返回 (True,'ok')；纯文件名 --out 默认 tmp
  父目录 = cwd（避免 /tmp）。

全部不依赖 GPU/真实 llama-server。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

from runner import m0_fanout_runner as m0r
from runner import m0_schema as sch


class ConfigurableStopAdapter:
    """stop 返回可配置 (ok, detail)；start/wait_health 最小 mock。"""

    stop_calls = 0

    def __init__(self, cmd, log_path, port, crash_at=None, stop_result=(True, "ok")):
        self.cmd = cmd
        self.log_path = log_path
        self.port = port
        self.stop_result = stop_result

    def start(self):
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write("llama_memory_recurrent::init: RS buffer size = 502.50 MiB\n")

    def wait_health(self, timeout=90.0):
        return True

    def poll(self):
        return None

    def read_log(self):
        with open(self.log_path, encoding="utf-8") as f:
            return f.read()

    def stop(self):
        ConfigurableStopAdapter.stop_calls += 1
        return self.stop_result


class KillFailAdapter(ConfigurableStopAdapter):
    """stop 返回 (False, detail) —— kill/wait 后仍存活。"""

    def stop(self):
        return (False, "SIGKILL timeout, still alive")


class KillFallbackAdapter(ConfigurableStopAdapter):
    """首个 group stop 返回 (True, 'SIGTERM timeout, killed')（kill 兜底），
    其余返回 (True, 'ok')。"""

    def __init__(self, cmd, log_path, port, crash_at=None, stop_result=(True, "ok")):
        super().__init__(cmd, log_path, port, crash_at, stop_result)
        self._first = True

    def stop(self):
        if self._first:
            self._first = False
            return (True, "SIGTERM timeout, killed")
        return (True, "ok")


class StartFailAdapter:
    """start 抛 OSError（模拟 spawn 失败）——start 失败不重复 stop。"""

    stop_calls = 0

    def __init__(self, cmd, log_path, port, crash_at=None):
        self.cmd = cmd
        self.log_path = log_path
        self.port = port

    def start(self):
        raise OSError("cannot spawn server")

    def wait_health(self, timeout=90.0):
        return False

    def poll(self):
        return None

    def read_log(self):
        return ""

    def stop(self):
        StartFailAdapter.stop_calls += 1
        return (True, "ok")


class _FakeDrv:
    def chat(self, messages, **kw):
        return {"text": "ACTION: branch(b1)", "finish_reason": "stop"}


def _make_runner(tmp_path, adapter_cls=ConfigurableStopAdapter, **over):
    args = dict(
        server_bin="fake-bin", model="mock.gguf", ctx_size=4096,
        port_base=18080, decision_n_predict=16, branch_n_predict=64,
        tool_rounds=1, out_path=str(tmp_path / "out.json"),
        tmp_dir="", ngl=99, adapter_cls=adapter_cls,
    )
    args.update(over)
    return m0r.M0FanoutRunner(**args)


def _ready_runner(r):
    r.parity_progress = {"completed": list(sch.PARITY_BUCKETS), "errors": []}
    r.binary_version = "test-version"
    import types
    r.run_calibration = types.MethodType(lambda self: True, r)
    r.run_budget_check = types.MethodType(lambda self: None, r)


def _install_fake_run_unit(r, monkeypatch):
    def fake(unit_id, fanout, bucket, ctk, ctv, gid, rep_index):
        r._last_prefix_len = 260
        r._last_branch_len = 344
        if rep_index is None:
            return {"unit_id": unit_id, "warmup": True}
        return sch.build_rep(unit_id, rep_index, fanout, bucket, ctk, ctv,
                             260, 344, gid, "OK", False, {"kv": {}, "branches": []})

    monkeypatch.setattr(r, "_run_unit", fake)


class TestStopKillSemantics:
    """任务 1/3：kill 兜底 clean（矩阵继续）；kill 仍失败 → FormalIncomplete。"""

    def test_sigterm_timeout_killed_continues_matrix(self, tmp_path, monkeypatch):
        """stop 返回 (True, 'SIGTERM timeout, killed') → clean kill 兜底：
        矩阵继续（phase=formal）、notes 记 warning、不 FormalIncomplete。"""
        r = _make_runner(tmp_path, adapter_cls=KillFallbackAdapter)
        _ready_runner(r)
        monkeypatch.setattr(r, "_connect", lambda port: (_FakeDrv(), None))
        monkeypatch.setattr(r, "_capture_baseline",
                            lambda gid: {"rss_mb": 100.0, "kv": {}})
        _install_fake_run_unit(r, monkeypatch)
        # 直接跑 run_formal（聚焦 stop 语义，跳过 calibration/budget/dv 全流程）
        r.run_formal()  # kill 兜底 clean → 不抛（矩阵继续）
        assert any("SIGTERM timeout, killed" in n for n in r.notes)
        assert r._adapter is None and r._driver is None and r._kv is None
        assert len(r.groups_off) + len(r.groups_on) == 12  # 12 groups 均完成

    def test_kill_still_alive_formal_incomplete(self, tmp_path, monkeypatch):
        """stop 返回 (False, 'SIGKILL timeout, still alive') → FormalIncomplete，
        notes 含具体 detail。"""
        r = _make_runner(tmp_path, adapter_cls=ConfigurableStopAdapter)
        ConfigurableStopAdapter.stop_calls = 0
        _ready_runner(r)
        monkeypatch.setattr(r, "_connect", lambda port: (_FakeDrv(), None))
        monkeypatch.setattr(r, "_capture_baseline",
                            lambda gid: {"rss_mb": 100.0, "kv": {}})
        _install_fake_run_unit(r, monkeypatch)
        # 替换 adapter_cls：首个 group 用 KillFailAdapter（stop 返回 False）
        r._adapter = None
        r2 = _make_runner(tmp_path, adapter_cls=KillFailAdapter)
        _ready_runner(r2)
        monkeypatch.setattr(r2, "_connect", lambda port: (_FakeDrv(), None))
        monkeypatch.setattr(r2, "_capture_baseline",
                            lambda gid: {"rss_mb": 100.0, "kv": {}})
        _install_fake_run_unit(r2, monkeypatch)
        r = r2
        with pytest.raises(m0r.FormalIncomplete):
            r.run_formal()
        assert any("SIGKILL timeout, still alive" in n for n in r.notes)
        assert r._adapter is None and r._driver is None and r._kv is None

    def test_stop_detail_written_to_notes(self, tmp_path, monkeypatch):
        """_stop_server：clean 但带 detail（kill 兜底 warning）写 notes 具体文本。"""
        r = _make_runner(tmp_path, adapter_cls=ConfigurableStopAdapter)
        r._adapter = ConfigurableStopAdapter("x", "y", 1,
                                             stop_result=(True, "SIGTERM timeout, killed"))
        clean = r._stop_server()
        assert clean is True
        assert any("stop clean 但带 warning: SIGTERM timeout, killed" in n
                   for n in r.notes)

    def test_start_failure_no_duplicate_stop(self, tmp_path, monkeypatch):
        """start 失败（ServerError）→ FirstGroupStartFailed；stop 仅 _start_server
        内部一次（run_formal 外层 finally 不重复：adapter=None）。"""
        r = _make_runner(tmp_path, adapter_cls=StartFailAdapter)
        StartFailAdapter.stop_calls = 0
        _ready_runner(r)
        with pytest.raises(m0r.FirstGroupStartFailed):
            r.run_formal()
        assert StartFailAdapter.stop_calls == 1  # 仅 _start_server 内部（OSError 分支）
        assert r._adapter is None

    def test_stop_no_process_returns_clean(self, tmp_path):
        """FakeAdapter.stop 无 server（proc None）也返回 (True, 'ok')。"""
        r = _make_runner(tmp_path, adapter_cls=ConfigurableStopAdapter)
        assert r._adapter is None
        clean = r._stop_server()
        assert clean is True
        assert r.notes == []

    def test_stop_normal_detail_not_recorded_as_warning(self, tmp_path):
        """v64：clean 但正常 detail（clean stop/already exited/no process/process
        disappeared）不记 notes/warning（v62 曾把 'clean stop' 误记——完整 4B
        矩阵 run1/run2 的 notes 污染根因）。"""
        for detail in ("clean stop", "already exited", "no process",
                       "process disappeared"):
            r = _make_runner(tmp_path, adapter_cls=ConfigurableStopAdapter)
            r._adapter = ConfigurableStopAdapter("x", "y", 1,
                                                 stop_result=(True, detail))
            clean = r._stop_server()
            assert clean is True
            assert r.notes == [], f"detail={detail} 不应记 notes"
        # 非正常 detail（kill 兜底）仍记 warning
        r = _make_runner(tmp_path, adapter_cls=ConfigurableStopAdapter)
        r._adapter = ConfigurableStopAdapter(
            "x", "y", 1, stop_result=(True, "SIGTERM timeout, killed"))
        clean = r._stop_server()
        assert clean is True
        assert any("stop clean 但带 warning: SIGTERM timeout, killed" in n
                   for n in r.notes)


class TestMainTmpParentBasename:
    """任务 4：纯文件名 --out out.json 默认 tmp 父目录 = cwd（避免 /tmp）。"""

    def test_out_basename_default_tmp_parent_cwd(self, tmp_path, monkeypatch):
        captured: dict = {}

        class FakeRunner:
            def __init__(self, **kw):
                captured.update(kw)

            def run_safe(self):
                return 0

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(m0r, "M0FanoutRunner", FakeRunner)
        monkeypatch.setattr(m0r.sch, "cleanup_stale_tmp", lambda *a, **k: None)
        monkeypatch.setattr(m0r.sch, "cleanup_stale_dirs", lambda *a, **k: None)
        monkeypatch.setattr(
            sys, "argv",
            ["m0_fanout_runner", "--server-bin", "fake-bin",
             "--model", "mock.gguf", "--out", "out.json"])
        assert m0r.main() == 0
        assert captured["tmp_dir"] == str(tmp_path)  # dirname(abspath("out.json")) = cwd
