"""M0 v60 审查修复测试（4 项任务）：

1. _stop_server 检查 adapter.stop() 返回 (ok, detail)——ok=False 视为 stop
   failure（进程可能残留），记录 _stop_errors 与 detail；抛异常路径保留；
   非 tuple 返回（旧 mock 契约 None）视为干净。补真实返回 False 而非抛异常
   测试（dv 路径 break / formal 路径 FormalIncomplete 均按基础设施处理）。
2. formal _capture_baseline / _connect 的基础设施异常（OSError / openai /
   ServerError，含 TimeoutError）→ 首 group（尚无 completed group）按首
   group preflight/PREFLIGHT_INFRA 规则（FirstGroupStartFailed → 合法 preflight
   落盘、保留完整 decision_validation）；第 2+ group → 保留已完成 groups、
   失败 group ERROR（endpoint_unavailable）、FormalIncomplete 合法落盘
   （phase=formal、rule=FORMAL_INCOMPLETE、exit 0），绝不 exit70 丢结果；
   内部 bug（AssertionError/KeyError）仍 70。
3. cleanup_stale_dirs 并发安全：活跃 marker（pid 存活）不删；死 pid marker
   按 age 删；keep=true 永不删；marker 文件不计入最新 mtime（控制元数据）；
   _dir_newest_mtime 含递归子目录 mtime；CLEANUP_AGE_MIN == 300.0。
4. _cleanup_all（无调用点冗余 stop try/except 死代码）已删除。

全部不依赖 GPU/真实 llama-server。
"""
from __future__ import annotations

import json
import os
import time

import openai
import pytest

from runner import m0_fanout_runner as m0r
from runner import m0_schema as sch
from tests.mock_server import MockOpenAIServer


# ---- 1. _stop_server 检查 adapter.stop() 返回 (ok, detail) ----

class _FakeDrv:
    """fake driver：chat 返回合法决策行。"""

    def chat(self, messages, **kw):
        return {"text": "ACTION: branch(b1)", "finish_reason": "stop"}


class ReturningFalseStopAdapter:
    """stop() **真实返回 (False, detail) 而非抛异常**（如 SIGTERM 超时转 kill）。"""

    _instances = 0

    def __init__(self, cmd, log_path, port, crash_at=None):
        self.cmd = cmd
        self.log_path = log_path
        self.port = port
        self._server = None
        ReturningFalseStopAdapter._instances += 1

    def start(self):
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write("llama_memory_recurrent::init: RS buffer size = 502.50 MiB\n")
        self._server = MockOpenAIServer().__enter__()
        self.port = self._server.port

    def poll(self):
        return None

    def wait_health(self, timeout=120.0):
        return self._server is not None

    def stop(self):
        if self._server is not None:
            self._server.__exit__(None, None, None)
            self._server = None
        return (False, "SIGTERM timeout, killed")

    def read_log(self):
        return ""


class ReturningTrueStopAdapter(ReturningFalseStopAdapter):
    def stop(self):
        if self._server is not None:
            self._server.__exit__(None, None, None)
            self._server = None
        return (True, "no process")


def _make_runner(tmp_path, adapter_cls=None, **over):
    args = dict(
        server_bin="fake-bin", model="mock.gguf", ctx_size=4096,
        port_base=18080, decision_n_predict=16, branch_n_predict=64,
        tool_rounds=1, out_path=str(tmp_path / "out.json"),
        tmp_dir="", ngl=99,
    )
    if adapter_cls is not None:
        args["adapter_cls"] = adapter_cls
    args.update(over)
    return m0r.M0FanoutRunner(**args)


class TestStopServerReturnValue:
    def test_stop_returns_false_is_stop_failure(self, tmp_path):
        """stop() 返回 (False, detail) 而非抛异常 → _stop_server 视为 stop failure。"""
        r = _make_runner(tmp_path, adapter_cls=ReturningFalseStopAdapter)
        adapter = r._start_server(parallel=4, ctk="q8_0", ctv="q8_0",
                                  control="off", tag="calib")
        assert adapter is not None
        clean = r._stop_server()
        assert clean is False
        assert any("stop ok=False: SIGTERM timeout, killed" in e
                   for e in r._stop_errors)

    def test_stop_returns_true_is_clean(self, tmp_path):
        """stop() 返回 (True, detail) → 干净、无 stop 错误记录。"""
        r = _make_runner(tmp_path, adapter_cls=ReturningTrueStopAdapter)
        r._start_server(parallel=4, ctk="q8_0", ctv="q8_0",
                        control="off", tag="calib")
        clean = r._stop_server()
        assert clean is True
        assert r._stop_errors == []

    def test_stop_false_breaks_dv_session(self, tmp_path, monkeypatch):
        """dv：try 块无 primary 异常但 stop 返回 False → session 不完整。"""
        r = _make_runner(tmp_path, adapter_cls=ReturningFalseStopAdapter)
        monkeypatch.setattr(r, "_connect", lambda port: (_FakeDrv(), None))
        complete, sessions = r.run_decision_validation()
        assert complete is False
        assert sessions == []
        assert any("stop ok=False" in e for e in r._stop_errors)

    def test_stop_false_formal_group_incomplete(self, tmp_path, monkeypatch):
        """formal：group try 块正常完成但 stop 返回 False → FormalIncomplete。"""
        r = _make_runner(tmp_path, adapter_cls=ReturningFalseStopAdapter)
        import types

        def fake_run_unit(self, unit_id, fanout, bucket, ctk, ctv, gid, rep_index):
            self._last_prefix_len = 260
            self._last_branch_len = 344
            if rep_index is None:  # warmup：不产出 formal rep
                return {"unit_id": unit_id, "warmup": True}
            return sch.build_rep(
                unit_id, rep_index, fanout, bucket, ctk, ctv,
                self._last_prefix_len, self._last_branch_len, gid,
                "OK", False, {"kv": {}, "branches": []})

        monkeypatch.setattr(r, "_run_unit", types.MethodType(fake_run_unit, r))
        monkeypatch.setattr(r, "_connect", lambda port: (_FakeDrv(), None))
        monkeypatch.setattr(r, "_capture_baseline",
                            lambda gid: sch.build_baseline(
                                server_version="test", gpu_used_mb=100.0,
                                rss_mb=200.0, rs_buffer_mb=50.25,
                                kv_buffer_mb=68.0,
                                metrics_kv_snapshot={"capacity_bytes": 71303168}))
        r.calibrated_lengths = {
            ("short", 2): {"prefix": 260, "branch": 344},
            ("medium", 2): {"prefix": 420, "branch": 504},
            ("long", 2): {"prefix": 551, "branch": 635},
        }
        r.parity_progress = {"completed": list(sch.PARITY_BUCKETS), "errors": []}
        with pytest.raises(m0r.FormalIncomplete):
            r.run_formal()
        assert any("stop ok=False" in e for e in r._stop_errors)


# ---- 2. formal _connect/_capture_baseline 基础设施异常 → FormalIncomplete ----

class TestFormalInfraErrors:
    def _ready_runner(self, r):
        """run() 前置：parity 成功 + 完整 6 桶证据 + 无预算拒绝。"""
        r.parity_progress = {"completed": list(sch.PARITY_BUCKETS), "errors": []}
        r.binary_version = "test-version"
        import types
        r.run_calibration = types.MethodType(lambda self: True, r)
        r.run_budget_check = types.MethodType(lambda self: None, r)

    def _cal_lengths(self):
        return {
            ("short", 2): {"prefix": 260, "branch": 344},
            ("medium", 2): {"prefix": 420, "branch": 504},
            ("long", 2): {"prefix": 551, "branch": 635},
        }

    def _fake_run_unit(self, unit_id, fanout, bucket, ctk, ctv, gid, rep_index):
        self._last_prefix_len = 260
        self._last_branch_len = 344
        if rep_index is None:
            return {"unit_id": unit_id, "warmup": True}
        return sch.build_rep(
            unit_id, rep_index, fanout, bucket, ctk, ctv,
            self._last_prefix_len, self._last_branch_len, gid,
            "OK", False, {"kv": {}, "branches": []})

    def test_first_group_connect_oserror_preflight(self, tmp_path, monkeypatch):
        """首 group _connect 基础设施异常（OSError）→ FirstGroupStartFailed →
        合法 preflight 落盘（phase=preflight）、保留完整 decision_validation、
        exit 0（不 exit70 丢结果）。"""
        r = _make_runner(tmp_path, adapter_cls=ReturningTrueStopAdapter)
        self._ready_runner(r)
        # dv 阶段（2 sessions）各 1 次 connect 成功；formal 首 group 第 3 次失败
        connect_calls = {"n": 0}

        def fake_connect(port):
            connect_calls["n"] += 1
            if connect_calls["n"] >= 3:
                raise OSError("connection refused")
            return (_FakeDrv(), None)

        monkeypatch.setattr(r, "_connect", fake_connect)
        rc = r.run()
        assert rc == 0
        assert r.phase == "preflight"
        with open(r.out_path, encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["meta"]["phase"] == "preflight"
        assert doc["meta"]["preflight_status"] == "FAILED"
        dv = doc["meta"]["decision_validation"]
        assert dv["partial"] is False
        assert len(dv["sessions"]) == 2
        assert doc["modes"] == {}
        assert doc["gates"] == {}

    def test_first_group_baseline_openai_error_preflight(self, tmp_path, monkeypatch):
        """首 group _capture_baseline openai 基础设施异常 → 同样 preflight 落盘。"""
        r = _make_runner(tmp_path, adapter_cls=ReturningTrueStopAdapter)
        self._ready_runner(r)
        monkeypatch.setattr(r, "_connect", lambda port: (_FakeDrv(), None))

        def fake_baseline(gid):
            raise openai.APIConnectionError(request=None)

        monkeypatch.setattr(r, "_capture_baseline", fake_baseline)
        rc = r.run()
        assert rc == 0
        with open(r.out_path, encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["meta"]["phase"] == "preflight"
        assert len(doc["meta"]["decision_validation"]["sessions"]) == 2

    def test_second_group_connect_oserror_formal_incomplete(self, tmp_path, monkeypatch):
        """第 2+ group _connect 基础设施异常 → FormalIncomplete：保留已完成
        group（COMPLETED）、失败 group ERROR（endpoint_unavailable）、
        phase=formal、rule=FORMAL_INCOMPLETE、合法落盘 exit 0（不 exit70）。"""
        r = _make_runner(tmp_path, adapter_cls=ReturningTrueStopAdapter)
        self._ready_runner(r)

        def fake_run_unit(unit_id, fanout, bucket, ctk, ctv, gid, rep_index):
            r._last_prefix_len = 260
            r._last_branch_len = 344
            if rep_index is None:  # warmup：不产出 formal rep
                return {"unit_id": unit_id, "warmup": True}
            return sch.build_rep(
                unit_id, rep_index, fanout, bucket, ctk, ctv,
                r._last_prefix_len, r._last_branch_len, gid,
                "OK", False, {"kv": {}, "branches": []})

        monkeypatch.setattr(r, "_run_unit", fake_run_unit)
        r.calibrated_lengths = self._cal_lengths()
        connect_calls = {"n": 0}

        def fake_connect(port):
            connect_calls["n"] += 1
            # dv 2 次成功 + formal group0 第 3 次成功；group1 第 4 次失败
            if connect_calls["n"] >= 4:
                raise OSError("connection refused (group 2)")
            return (_FakeDrv(), None)

        monkeypatch.setattr(r, "_connect", fake_connect)
        monkeypatch.setattr(r, "_capture_baseline",
                            lambda gid: sch.build_baseline(
                                server_version="test", gpu_used_mb=100.0,
                                rss_mb=200.0, rs_buffer_mb=50.25,
                                kv_buffer_mb=68.0,
                                metrics_kv_snapshot={"capacity_bytes": 71303168}))
        rc = r.run()
        assert rc == 0
        assert r.phase == "formal"
        assert r.rule == "FORMAL_INCOMPLETE"
        with open(r.out_path, encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["meta"]["phase"] == "formal"
        off = doc["modes"]["off"]["server_groups"]
        # 第 1 group COMPLETED；第 2 group ERROR（endpoint_unavailable）
        assert off[0]["status"] == "COMPLETED"
        assert off[1]["status"] == "ERROR"
        assert off[1]["error_type"] == "endpoint_unavailable"
        assert doc["meta"]["matrix_complete"] is False
        assert doc["gates"] != {}

    def test_second_group_connect_keyerror_still_exit70(self, tmp_path, monkeypatch):
        """内部 bug（KeyError，非基础设施）不得转 FormalIncomplete → 70。"""
        r = _make_runner(tmp_path, adapter_cls=ReturningTrueStopAdapter)
        self._ready_runner(r)

        def fake_run_unit(unit_id, fanout, bucket, ctk, ctv, gid, rep_index):
            r._last_prefix_len = 260
            r._last_branch_len = 344
            if rep_index is None:
                return {"unit_id": unit_id, "warmup": True}
            return sch.build_rep(
                unit_id, rep_index, fanout, bucket, ctk, ctv,
                r._last_prefix_len, r._last_branch_len, gid,
                "OK", False, {"kv": {}, "branches": []})

        monkeypatch.setattr(r, "_run_unit", fake_run_unit)
        r.calibrated_lengths = self._cal_lengths()
        connect_calls = {"n": 0}

        def fake_connect(port):
            connect_calls["n"] += 1
            if connect_calls["n"] >= 4:
                raise KeyError("internal bug")
            return (_FakeDrv(), None)

        monkeypatch.setattr(r, "_connect", fake_connect)
        monkeypatch.setattr(r, "_capture_baseline",
                            lambda gid: sch.build_baseline(
                                server_version="test", gpu_used_mb=100.0,
                                rss_mb=200.0, rs_buffer_mb=50.25,
                                kv_buffer_mb=68.0,
                                metrics_kv_snapshot={"capacity_bytes": 71303168}))
        assert r.run_safe() == sch.EXIT_SOFTWARE
        assert not os.path.isfile(r.out_path)


# ---- 3. cleanup_stale_dirs 并发安全（marker） ----

class TestCleanupMarkerSafety:
    def _mk_run_dir(self, d, name, mtime_old=True):
        run_dir = d / name
        run_dir.mkdir()
        (run_dir / "server_g0.log").write_text("x", encoding="utf-8")
        if mtime_old:
            old_t = time.time() - 7200.0
            os.utime(run_dir, (old_t, old_t))
            os.utime(run_dir / "server_g0.log", (old_t, old_t))
        return run_dir

    def _write_marker(self, run_dir, pid, keep=False):
        with open(run_dir / sch.RUN_MARKER_NAME, "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "create_ts": time.time(), "keep": keep}, f)

    def test_active_marker_pid_alive_not_removed(self, tmp_path):
        """marker pid 存活（本进程）→ 活跃 run 不删（目录全旧也保留）。"""
        d = tmp_path / "r"
        d.mkdir()
        run_dir = self._mk_run_dir(d, "m0_fanout_active")
        self._write_marker(run_dir, os.getpid())
        assert sch.cleanup_stale_dirs(str(d), max_age_seconds=3600.0) == []
        assert run_dir.exists()

    def test_dead_marker_pid_removed_when_stale(self, tmp_path):
        """marker pid 已死（999999）+ 目录全旧 → 按 age 删。
        （marker 写入会更新目录 mtime——目录 mtime 回退到创建时刻模拟真实时序：
        marker 写于目录创建瞬间。）"""
        d = tmp_path / "r"
        d.mkdir()
        run_dir = self._mk_run_dir(d, "m0_fanout_dead")
        self._write_marker(run_dir, 999999)
        old_t = time.time() - 7200.0
        os.utime(run_dir, (old_t, old_t))  # marker 写入后目录 mtime 回退创建时刻
        removed = sch.cleanup_stale_dirs(str(d), max_age_seconds=3600.0)
        assert removed == ["m0_fanout_dead"]
        assert not run_dir.exists()

    def test_keep_marker_never_removed(self, tmp_path):
        """marker keep=true → 永不自动删除（无论 age/pid 存活与否）。"""
        d = tmp_path / "r"
        d.mkdir()
        run_dir = self._mk_run_dir(d, "m0_fanout_keep")
        self._write_marker(run_dir, 999999, keep=True)
        assert sch.cleanup_stale_dirs(str(d), max_age_seconds=3600.0) == []
        assert run_dir.exists()
        # 再验证更严格：age 极小也保留
        assert sch.cleanup_stale_dirs(str(d), max_age_seconds=0.001) == []

    def test_marker_file_not_counted_as_fresh(self, tmp_path):
        """marker 是控制元数据（写入即新 mtime），不得计入最新 mtime——
        否则带 marker 的陈旧目录永不超龄。目录 mtime 回退创建时刻、文件全旧、
        marker 文件 mtime 最新（now）→ 仍删（marker 不计入）。"""
        d = tmp_path / "r"
        d.mkdir()
        run_dir = self._mk_run_dir(d, "m0_fanout_mark")
        self._write_marker(run_dir, 999999)  # marker mtime = now（"最新"）
        old_t = time.time() - 7200.0
        os.utime(run_dir, (old_t, old_t))  # 目录 mtime 回退创建时刻（marker 不算）
        removed = sch.cleanup_stale_dirs(str(d), max_age_seconds=3600.0)
        assert removed == ["m0_fanout_mark"]

    def test_dir_newest_mtime_includes_subdirs(self, tmp_path):
        """_dir_newest_mtime 含递归子目录 mtime（子目录新建 → 最新 mtime 是子目录）。"""
        root = tmp_path / "t"
        root.mkdir()
        old_t = time.time() - 7200.0
        os.utime(root, (old_t, old_t))
        sub = root / "sub"
        sub.mkdir()
        (sub / "log").write_text("x", encoding="utf-8")
        os.utime(sub / "log", (old_t, old_t))
        fresh_t = time.time() - 2.0
        os.utime(sub, (fresh_t, fresh_t))  # 子目录 mtime 新（如新建子目录）
        # sub.mkdir 会更新 root 目录 mtime → 回退 root 到 old，排除干扰
        os.utime(root, (old_t, old_t))
        newest = sch._dir_newest_mtime(str(root))
        assert abs(newest - fresh_t) < 1.0

    def test_cleanup_age_min_constant(self):
        assert sch.CLEANUP_AGE_MIN == 300.0

    def test_cleanup_stale_dirs_docstring_mentions_concurrency_guard(self):
        """docstring 明确并发禁止与 marker 语义（可人工核对的契约）。"""
        doc = sch.cleanup_stale_dirs.__doc__ or ""
        assert "并发禁止" in doc
        assert "ACTIVE.marker" in doc or "marker" in doc


# ---- 4. _cleanup_all 冗余 stop try/except 死代码已删除 ----

class TestCleanupAllRemoved:
    def test_cleanup_all_deleted(self):
        """无调用点冗余 stop try/except（_cleanup_all）已删除——不再存在该属性，
        避免与 _stop_server 双份 stop 逻辑漂移。"""
        assert not hasattr(m0r.M0FanoutRunner, "_cleanup_all")
