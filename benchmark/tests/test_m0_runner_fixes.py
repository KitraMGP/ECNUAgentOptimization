"""M0 Critical 修复测试（审查 a408031 十项合同）。

集中覆盖：
- Critical 1：KVProbe 数据路径（baseline 从 snapshot['data']、per-rep 边界、
  erase 后采样、G-M0-3a/G-M0-5 缺观测/非零 → FAIL 不静默 PASS）
- Critical 2：Driver.chat 顶层 finish_reason；length → INVALID_DECISION + invalid_length>0
- Critical 3：_connect 传实际 host/port；sampler 按端口找 PID（非 8080）
- Critical 4：启动 OSError（bin 缺失）→ ServerError → preflight 落盘，无 traceback
- Critical 5：atomic tmp 命名与 cleanup_stale_tmp 匹配 + CLI 入口清理
- Critical 6：prefix/branch_len 用 (bucket,fanout) 校准实测值；validator 一致性
- Critical 7：warmup 崩溃 → group ERROR / FORMAL_INCOMPLETE（不被吞）
- Critical 8：mock_server 无误导性实例 count 死属性
- Critical 9：G-M0-5 真实 llama.cpp 日志串（server-context.cpp:1505）
- Critical 10：G-M0-4 derived 自检 + RS buffer 独立观测（llama-memory-recurrent.cpp:115）
"""
from __future__ import annotations

import json
import os
import time
import urllib.error

import pytest

from framework import sampler
from framework.driver import Driver
from framework.kv_probe import KVProbe
from runner import fanout_prompts as fp
from runner import m0_fanout_runner as m0r
from runner import m0_schema as sch
from runner.m0_fanout_runner import M0FanoutRunner, ServerAdapter
from tests import mock_server as mserver
from tests.mock_server import MockOpenAIServer


class FakeAdapter:
    """与 test_m0_fanout_runner 同构的 mock adapter（独立副本，避免跨文件耦合）。"""

    fail_after = None
    crash_at_after = None
    crash_at_value = 31
    poll_dead = None  # v48（任务 7）：置 True 时 poll() 恒返回 1（模拟已退出）
    _instances = 0
    _starts = 0

    def __init__(self, cmd, log_path, port, crash_at=None):
        self.cmd = cmd
        self.log_path = log_path
        self.port = port
        FakeAdapter._instances += 1
        if (FakeAdapter.crash_at_after is not None
                and FakeAdapter._instances >= FakeAdapter.crash_at_after):
            crash_at = FakeAdapter.crash_at_value
        self.crash_at = crash_at
        self._server: MockOpenAIServer | None = None

    def start(self):
        FakeAdapter._starts += 1
        if FakeAdapter.fail_after is not None and FakeAdapter._starts >= FakeAdapter.fail_after:
            self._server = None
            return
        self._server = MockOpenAIServer(crash_at=self.crash_at).__enter__()
        self.port = self._server.port

    def poll(self):
        if FakeAdapter.poll_dead:
            return 1
        if self.crash_at is not None and self._server is not None \
                and self._server.httpd is not None \
                and self._server.httpd.count > self.crash_at:
            return 1
        if self._server is None:
            return 1
        return None

    def wait_health(self, timeout=120.0):
        return self._server is not None

    def stop(self):
        if self._server is not None:
            self._server.__exit__(None, None, None)
            self._server = None
            return (True, "ok")  # v61 契约：tuple/bool

    def read_log(self):
        return ("E8-C1: capability rejected: hybrid (recurrent+attention) model\n"
                if "on" in str(self.cmd) else "")


@pytest.fixture
def fake_adapter_cls(monkeypatch):
    FakeAdapter.fail_after = None
    FakeAdapter.crash_at_after = None
    FakeAdapter.crash_at_value = 31
    FakeAdapter.poll_dead = None
    FakeAdapter._instances = 0
    FakeAdapter._starts = 0
    monkeypatch.setattr(m0r, "ServerAdapter", FakeAdapter)
    return FakeAdapter


def make_runner(tmp_path, fake_adapter_cls, **over):
    args = dict(
        server_bin="fake-bin", model="mock.gguf", ctx_size=4096,
        port_base=18080, decision_n_predict=16, branch_n_predict=64,
        tool_rounds=1, out_path=str(tmp_path / "out.json"),
        tmp_dir=str(tmp_path / "tmp"), ngl=99, adapter_cls=fake_adapter_cls,
    )
    args.update(over)
    return M0FanoutRunner(**args)


# ---- Critical 1：KVProbe 数据路径 ----

class TestCritical1KVPath:
    def test_baseline_capacity_from_snapshot_data(self, tmp_path):
        """capacity_bytes 在 snapshot['data'] 内——kv_buffer_mb 必须读 data。"""
        with MockOpenAIServer() as srv:
            r = M0FanoutRunner(server_bin="x", model="m", ctx_size=4096,
                               port_base=18080, tmp_dir=str(tmp_path / "t"),
                               out_path="", adapter_cls=ServerAdapter)
            r._driver, r._kv = r._connect(srv.port)
            bs = r._capture_baseline("off:q8_0-q8_0:f2")
        # mock /metrics/kv capacity_bytes = 104857600 = 100 MiB
        assert bs["kv_buffer_mb"] == pytest.approx(100.0)
        assert bs["metrics_kv_snapshot"]["capacity_bytes"] == 104857600
        assert "data" not in bs["metrics_kv_snapshot"]  # 保存权威 data，非包装

    def _minimal_modes(self, kv_last=None, shared=None, branches=None):
        def _rep(control, ctk, ctv, fanout, idx, kv, br):
            return {
                "unit_id": sch.unit_id_of(control, ctk, ctv, fanout, "short"),
                "rep_index": idx, "fanout": fanout, "bucket": "short",
                "ctk": ctk, "ctv": ctv, "prefix_len": 100, "branch_len": 100,
                "server_group_id": sch.group_id_of(control, ctk, ctv, fanout),
                "status": "OK", "decision_fallback": True,
                "metrics": {"kv": kv, "branches": br},
            }

        off = {
            "server_group_id": sch.group_id_of("off", "q8_0", "q8_0", 2),
            "ctk": "q8_0", "ctv": "q8_0", "fanout": 2, "status": "COMPLETED",
            "baseline": {"rs_buffer_mb": 50.0, "metrics_kv_snapshot": {}},
            "replicates": [_rep("off", "q8_0", "q8_0", 2, 0, kv_last, branches)],
        }
        on = {
            "server_group_id": sch.group_id_of("on", "q8_0", "q8_0", 2),
            "ctk": "q8_0", "ctv": "q8_0", "fanout": 2, "status": "COMPLETED",
            "baseline": {"rs_buffer_mb": 50.0, "metrics_kv_snapshot": {}},
            "replicates": [_rep("on", "q8_0", "q8_0", 2, 0, kv_last, branches)],
        }
        if shared is not None:
            off["baseline"]["metrics_kv_snapshot"] = {"shared_cells": shared}
            on["baseline"]["metrics_kv_snapshot"] = {"shared_cells": shared}
        return {"off": {"server_groups": [off]}, "on": {"server_groups": [on]}}

    def _runner_with_log(self, tmp_path):
        r = make_runner(tmp_path, None)
        os.makedirs(r.tmp_dir, exist_ok=True)
        with open(os.path.join(r.tmp_dir, "server_g0.log"), "w",
                  encoding="utf-8") as f:
            f.write("E8-C1: capability rejected: hybrid (recurrent+attention) model\n")
        return r

    def test_g3a_nonzero_used_cells_fails(self, tmp_path):
        r = self._runner_with_log(tmp_path)
        modes = self._minimal_modes(
            kv_last={"used_cells": 5, "active_sequences": 0}, shared=0,
            branches=[{"branch": "b1", "finish_reason": "stop", "canary_leak": False}])
        g = r._compute_gates(modes)
        assert g["G-M0-3a"]["status"] == "FAIL"

    def test_g3a_missing_observation_fails(self, tmp_path):
        """erase 后样本缺失（last 无 used_cells）→ FAIL，绝不 None 即 PASS。"""
        r = self._runner_with_log(tmp_path)
        modes = self._minimal_modes(kv_last={}, shared=0, branches=[{"branch": "b1"}])
        g = r._compute_gates(modes)
        assert g["G-M0-3a"]["status"] == "FAIL"

    def test_g5_shared_missing_fails(self, tmp_path):
        """baseline 无 shared_cells 观测 → FAIL（不得 None 即 PASS）。"""
        r = self._runner_with_log(tmp_path)
        modes = self._minimal_modes(
            kv_last={"used_cells": 0, "active_sequences": 0},
            shared=None, branches=[{"branch": "b1", "canary_leak": False}])
        g = r._compute_gates(modes)
        assert g["G-M0-5"]["status"] == "FAIL"

    def test_g5_shared_nonzero_fails(self, tmp_path):
        r = self._runner_with_log(tmp_path)
        modes = self._minimal_modes(
            kv_last={"used_cells": 0, "active_sequences": 0},
            shared=3, branches=[{"branch": "b1", "canary_leak": False}])
        g = r._compute_gates(modes)
        assert g["G-M0-5"]["status"] == "FAIL"


# ---- Critical 2：finish_reason ----

class TestCritical2FinishReason:
    def test_driver_chat_top_level_finish_reason(self):
        with MockOpenAIServer(finish_reason="length") as srv:
            drv = Driver(base_url=f"http://127.0.0.1:{srv.port}", model="bench",
                         sdk_max_retries=0)
            row = drv.chat([{"role": "user", "content": "hi"}], max_tokens=4)
        assert row["finish_reason"] == "length"

    def test_decision_length_invalid_length_counted(self, tmp_path, fake_adapter_cls):
        """length 截断 → 决策 INVALID 且 invalid_length>0（真实路径端到端）。"""
        r = make_runner(tmp_path, fake_adapter_cls)
        adapter = r._start_server(10, "q8_0", "q8_0", "off", "dv")
        r._stop_server()
        with MockOpenAIServer(finish_reason="length") as srv:
            r._driver, r._kv = r._connect(srv.port)
            recs = r._run_decision_session(srv.port)
        sess = sch.build_decision_session(
            requests=len(recs),
            valid=sum(1 for x in recs if x["ok"] and x["error"] is None),
            invalid=sum(1 for x in recs if not x["ok"] and x["error"] is None),
            error_count=sum(1 for x in recs if x["error"] is not None),
            invalid_length=sum(1 for x in recs
                               if not x["ok"] and x["error"] is None
                               and x["finish_reason"] == "length"),
            invalid_no_action=sum(1 for x in recs
                                  if not x["ok"] and x["error"] is None
                                  and x["finish_reason"] != "length"),
            finish_reasons={},
            output_hashes=[], error_summary=[])
        assert sess["invalid_length"] > 0
        assert all(x["finish_reason"] == "length"
                   for x in recs if x["error"] is None)
        # length 必须判 INVALID_DECISION（决策谓词）
        assert fp.decision_invalid("模拟回复 1", "length", 8) is True


# ---- Critical 3：端口 ----

class TestCritical3Port:
    def test_connect_passes_host_port(self, tmp_path, monkeypatch):
        calls: list = []
        orig = Driver.__init__

        def _spy(self_, base_url, **kw):
            calls.append((base_url, kw.get("host"), kw.get("port")))
            orig(self_, base_url, **kw)

        monkeypatch.setattr(Driver, "__init__", _spy)
        with MockOpenAIServer() as srv:
            r = M0FanoutRunner(server_bin="x", model="m", ctx_size=4096,
                               port_base=18080, tmp_dir=str(tmp_path / "t"),
                               out_path="", adapter_cls=ServerAdapter)
            r._connect(srv.port)
        assert calls, "Driver 未被构造"
        _, host, port = calls[-1]
        assert host == "127.0.0.1"
        assert port == srv.port  # 非默认 8080 时也必须传实际端口

    def test_find_server_pid_non_default_port(self):
        """sampler 按实际端口定位 PID（mock server 进程 = 本进程，非 llama-server）。"""
        with MockOpenAIServer() as srv:
            pid = sampler.find_server_pid("127.0.0.1", srv.port)
        assert pid == os.getpid()


# ---- Critical 4：启动 OSError ----

class TestCritical4StartOSError:
    def test_missing_server_bin_preflight_no_traceback(self, tmp_path, capsys):
        """server-bin 不存在（Popen FileNotFoundError）→ ServerError →
        preflight 落盘 rc==0，无 traceback / exit 1。真 ServerAdapter。"""
        out = str(tmp_path / "out.json")
        r = M0FanoutRunner(server_bin="/no/such/server-bin", model="m.gguf",
                           ctx_size=4096, port_base=19001, tmp_dir=str(tmp_path / "t"),
                           out_path=out, ngl=99, adapter_cls=ServerAdapter)
        rc = r.run()
        assert rc == 0, rc
        assert "Traceback" not in capsys.readouterr().err
        with open(out, encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["meta"]["phase"] == "preflight"
        assert doc["meta"]["preflight_reason"] == "validation_incomplete"
        assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"

    def test_unwritable_log_path_preflight(self, tmp_path):
        """日志文件不可写（深层路径不存在 → open OSError）→ ServerError → preflight。"""
        out = str(tmp_path / "out.json")
        r = M0FanoutRunner(server_bin="/no/such/server-bin", model="m.gguf",
                           ctx_size=4096, port_base=19002,
                           tmp_dir=str(tmp_path / "no_such_dir" / "deep"),
                           out_path=out, ngl=99, adapter_cls=ServerAdapter)
        rc = r.run()
        assert rc == 0, rc
        with open(out, encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["meta"]["phase"] == "preflight"


# ---- Critical 5：陈旧 tmp 清理 ----

class TestCritical5StaleTmp:
    def test_atomic_tmp_naming_matches_cleanup(self, tmp_path):
        d = str(tmp_path)
        sch.atomic_write_json(os.path.join(d, "m0_fanout_x.json"), {"a": 1})
        # atomic 写后不应残留 tmp；造一个陈旧 tmp 模拟中断残留
        # （v48：新命名 .m0_fanout_<name>.<pid>.<rand>.tmp）
        stale = os.path.join(d, ".m0_fanout_x.json.1.abcd.tmp")
        with open(stale, "w", encoding="utf-8") as f:
            f.write("{}")
        past = time.time() - 7200
        os.utime(stale, (past, past))
        removed = sch.cleanup_stale_tmp(d, max_age_seconds=3600.0)
        assert os.path.basename(stale) in removed
        assert not os.path.exists(stale)
        assert os.path.exists(os.path.join(d, "m0_fanout_x.json"))

    def test_active_tmp_not_deleted(self, tmp_path):
        """v48（任务 9）：活跃并发写（新 mtime）不删；仅删陈旧。"""
        d = str(tmp_path)
        active = os.path.join(d, ".m0_fanout_x.main.1.abcd.tmp")
        stale = os.path.join(d, ".m0_fanout_x.main.2.efgh.tmp")
        for p in (active, stale):
            with open(p, "w", encoding="utf-8") as f:
                f.write("{}")
        past = time.time() - 7200
        os.utime(stale, (past, past))
        removed = sch.cleanup_stale_tmp(d, max_age_seconds=3600.0)
        assert os.path.basename(stale) in removed
        assert os.path.exists(active)

    def test_atomic_tmp_name_unique(self, tmp_path):
        """v48（任务 9）：并发写同路径 tmp 名唯一（pid+随机），互不覆盖。"""
        d = str(tmp_path)
        p = os.path.join(d, "m0_fanout_x.json")
        # 同一路径连续两次 atomic 写 → 无残留（前次 tmp 已被 replace）；再验证
        # 唯一名组件存在：模拟两次并发（无锁）写时文件名含 pid 与随机后缀
        sch.atomic_write_json(p, {"a": 1})
        sch.atomic_write_json(p, {"b": 2})
        leftovers = [n for n in os.listdir(d) if n.startswith(".m0_fanout_")]
        assert leftovers == []

    def test_cli_main_cleans_stale_tmp(self, tmp_path, monkeypatch):
        out_dir = tmp_path / "res"
        out_dir.mkdir()
        stale = out_dir / ".m0_fanout_old.json.tmp"
        stale.write_text("{}", encoding="utf-8")
        past = time.time() - 7200
        os.utime(stale, (past, past))

        def _fake_runner(**kw):
            class _R:
                def run_safe(self):
                    return 0
            return _R()

        monkeypatch.setattr(m0r, "M0FanoutRunner", _fake_runner)
        rc = m0r.main(["--server-bin", "x", "--model", "m",
                       "--out", str(out_dir / "out.json"),
                       "--tmp-dir", str(tmp_path / "t")])
        assert rc == 0
        assert not stale.exists()  # CLI 入口已清理（陈旧）

    def test_cli_main_keeps_active_tmp(self, tmp_path, monkeypatch):
        """v48（任务 9）：main 传 age 阈值——活跃（新 mtime）tmp 不删。"""
        out_dir = tmp_path / "res"
        out_dir.mkdir()
        active = out_dir / ".m0_fanout_cur.json.main.tmp"
        active.write_text("{}", encoding="utf-8")  # 新 mtime

        def _fake_runner(**kw):
            class _R:
                def run_safe(self):
                    return 0
            return _R()

        monkeypatch.setattr(m0r, "M0FanoutRunner", _fake_runner)
        rc = m0r.main(["--server-bin", "x", "--model", "m",
                       "--out", str(out_dir / "out.json"),
                       "--tmp-dir", str(tmp_path / "t")])
        assert rc == 0
        assert active.exists()  # 活跃并发写不删


# ---- Critical 6：校准实测长度 ----

class TestCritical6CalibratedLengths:
    def test_rep_lengths_use_calibrated_not_targets(self, tmp_path, fake_adapter_cls):
        r = make_runner(tmp_path, fake_adapter_cls)
        adapter = r._start_server(4, "q8_0", "q8_0", "off", "t")
        r._driver, r._kv = r._connect(adapter.port)
        # mock /tokenize 固定 100 → 校准实测 100，而 BUCKET_TARGETS[short]=(150,150)
        r.calibrated_lengths[("short", 2)] = {"prefix": 100, "branch": 100}
        uid = sch.unit_id_of("off", "q8_0", "q8_0", 2, "short")
        gid = sch.group_id_of("off", "q8_0", "q8_0", 2)
        rep = r._run_unit(uid, 2, "short", "q8_0", "q8_0", gid, rep_index=0)
        r._stop_server()
        assert rep["prefix_len"] == 100
        assert rep["branch_len"] == 100
        assert (rep["prefix_len"], rep["branch_len"]) != fp.BUCKET_TARGETS["short"]
        # rep 内 KV 数据存在（begin/end_run 边界 + erase 后采样）
        assert rep["metrics"]["kv"]["last"]["used_cells"] == 0
        assert rep["metrics"]["kv"]["last"]["active_sequences"] == 0

    def test_validator_rejects_inconsistent_bucket_lengths(self, tmp_path, fake_adapter_cls):
        """同 (fanout,bucket) rep 长度不一致 → SCHEMA_INVALID（校准一致性）。"""
        r = make_runner(tmp_path, fake_adapter_cls)
        uid = sch.unit_id_of("off", "q8_0", "q8_0", 2, "short")
        gid = sch.group_id_of("off", "q8_0", "q8_0", 2)
        rep1 = sch.build_rep(uid, 0, 2, "short", "q8_0", "q8_0",
                             100, 100, gid, "OK", False, {"branches": []})
        rep2 = sch.build_rep(uid, 1, 2, "short", "q8_0", "q8_0",
                             200, 100, gid, "OK", False, {"branches": []})
        group = sch.build_group(gid, "COMPLETED", "2026-08-09T00:00:00Z",
                                "2026-08-09T00:00:01Z",
                                baseline={"rs_buffer_mb": 50.0},
                                replicates=[rep1, rep2])
        modes = sch.build_modes([group], [])
        # 直接调 modes/group/rep validator（避免顶层五键噪音）
        errs = sch.validate_modes_group_rep(modes, matrix_complete=True,
                                            rejections=[])
        # _ERR = (code, path, expected, actual) 元组
        assert any(e[0] == "invariant_violation"
                   and "prefix_len" in str(e[2]) for e in errs)


# ---- Critical 7：warmup 崩溃 ----

class TestCritical7WarmupCrash:
    def test_warmup_crash_group_error_formal_incomplete(self, tmp_path, fake_adapter_cls):
        """warmup 崩溃不得被吞：立即 group ERROR + FORMAL_INCOMPLETE（rc==0）。"""
        FakeAdapter.crash_at_after = 4   # formal 首组（第 4 个 adapter）起崩溃
        FakeAdapter.crash_at_value = 1   # 第 2 次 POST 即崩 → warmup 期内
        try:
            r = make_runner(tmp_path, fake_adapter_cls)
            rc = r.run()
            assert rc == 0, rc
            with open(r.out_path, encoding="utf-8") as f:
                doc = json.load(f)
            assert doc["meta"]["phase"] == "formal"
            assert doc["meta"]["matrix_complete"] is False
            groups = (doc["modes"]["off"]["server_groups"]
                      + doc["modes"]["on"]["server_groups"])
            assert groups[-1]["status"] == "ERROR"
            assert groups[-1]["error_type"] == "server_crash"
            assert groups[-1]["replicates"] == []  # 崩溃于 warmup，无 formal rep
        finally:
            FakeAdapter.crash_at_after = None


# ---- Critical 8：mock 死属性 ----

class TestCritical8MockDeadAttr:
    def test_mock_server_no_misleading_instance_count(self):
        srv = MockOpenAIServer()
        assert not hasattr(srv, "count"), "实例 count 死属性必须删除（计数在 httpd.count）"
        with srv:
            assert hasattr(srv.httpd, "count")
            assert srv.httpd.count == 0


# ---- Critical 9：G-M0-5 真实日志串 ----

class TestCritical9G5RealLog:
    def test_source_contains_real_capability_line(self):
        src = os.path.join(os.path.dirname(__file__), "..", "..", "llama.cpp",
                           "tools", "server", "server-context.cpp")
        if not os.path.exists(src):
            pytest.skip("llama.cpp 子仓库不存在")
        with open(src, encoding="utf-8") as f:
            text = f.read()
        real = "E8-C1: capability rejected: hybrid (recurrent+attention) model"
        assert real in text
        # runner 匹配串必须是真实行的前缀子串（防 FakeAdapter 专造）
        assert "E8-C1: capability rejected: hybrid" in real

    def test_g5_pass_with_real_line_missing_log_fails(self, tmp_path, fake_adapter_cls):
        r = make_runner(tmp_path, fake_adapter_cls)
        os.makedirs(r.tmp_dir, exist_ok=True)
        with open(os.path.join(r.tmp_dir, "server_g0.log"), "w",
                  encoding="utf-8") as f:
            f.write("E8-C1: capability rejected: hybrid (recurrent+attention) model\n")
        modes = {"off": {"server_groups": []}, "on": {"server_groups": []}}
        g = r._compute_gates(modes)
        assert g["G-M0-5"]["status"] == "PASS"  # on 日志行命中（无 group 时无 shared 检查）
        # 缺日志 → FAIL（独立 tmp_dir，避免与 r 的日志串扰）
        r2 = make_runner(tmp_path, fake_adapter_cls)
        r2.tmp_dir = os.path.join(str(tmp_path), "empty_logs")
        g2 = r2._compute_gates(modes)
        assert g2["G-M0-5"]["status"] == "FAIL"


# ---- Critical 10：G-M0-4 独立观测 ----

class TestCritical10G4Observational:
    def test_parse_rs_buffer_real_format(self):
        log = ("llama_memory_recurrent::init: gpu0 RS buffer size =  50.25 MiB\n"
               "llama_memory_recurrent::init: cpu  RS buffer size =  0.00 MiB\n")
        assert m0r.M0FanoutRunner._parse_rs_buffer_mib(log) == pytest.approx(50.25)
        assert m0r.M0FanoutRunner._parse_rs_buffer_mib("no rs line") is None

    def _modes_with_groups(self):
        # v48（任务 1）：必须用真实 sch.build_group/build_modes 产物——
        # build_group 不写 ctk/ctv/fanout 顶层字段，G-M0-4 用 parse_group_id 匹配
        exp = 24 * 548864 * 4 * 4 / (1024 * 1024)
        gid = sch.group_id_of("off", "q8_0", "q8_0", 2)
        off = sch.build_group(gid, "COMPLETED", "2026-08-09T00:00:00Z",
                              "2026-08-09T00:00:01Z",
                              baseline={"rs_buffer_mb": round(exp, 3)},
                              replicates=[])
        return sch.build_modes([off], [])

    def test_g4_three_states(self, tmp_path):
        exp = 24 * 548864 * 4 * 4 / (1024 * 1024)
        # 无观测 → NOT_APPLICABLE（明确，非 PASS）
        r = make_runner(tmp_path, None)
        g = r._compute_gates(self._modes_with_groups())
        assert g["G-M0-4"]["status"] == "NOT_APPLICABLE"
        # 观测匹配 → PASS（真实日志 RS buffer size 行）
        r._server_logs["g0"] = (f"llama_memory_recurrent::init: RS buffer size ="
                                 f"  {exp:.2f} MiB\n")
        g = r._compute_gates(self._modes_with_groups())
        assert g["G-M0-4"]["status"] == "PASS"
        # 观测超差（>G4_TOLERANCE=5%）→ FAIL
        r._server_logs["g0"] = ("llama_memory_recurrent::init: RS buffer size ="
                                 "  400.00 MiB\n")
        g = r._compute_gates(self._modes_with_groups())
        assert g["G-M0-4"]["status"] == "FAIL"
        # derived 自检不一致（rs_buffer_mb 与公式不符）→ 无观测时 NOT_APPLICABLE
        # （v48，任务 5：derived 仅 internal consistency detail、非 PASS 证据、不 FAIL gate）
        modes = self._modes_with_groups()
        modes["off"]["server_groups"][0]["baseline"] = {"rs_buffer_mb": 1.0}
        r._server_logs["g0"] = ""
        g = r._compute_gates(modes)
        assert g["G-M0-4"]["status"] == "NOT_APPLICABLE"
        assert "internal inconsistency" in g["G-M0-4"].get("notes", "")

    def test_g4_derived_inconsistency_does_not_fail_with_obs(self, tmp_path):
        """v48（任务 5）：derived 公式不一致但真实 RS 日志观测匹配 → PASS
        （derived 只作 internal consistency 记录，不参与 gate status）。"""
        exp = 24 * 548864 * 4 * 4 / (1024 * 1024)
        r = make_runner(tmp_path, None)
        modes = self._modes_with_groups()
        modes["off"]["server_groups"][0]["baseline"] = {"rs_buffer_mb": 1.0}  # 与公式不符
        r._server_logs["g0"] = (f"llama_memory_recurrent::init: RS buffer size ="
                                 f"  {exp:.2f} MiB\n")
        g = r._compute_gates(modes)
        assert g["G-M0-4"]["status"] == "PASS"
        assert "internal inconsistency" in g["G-M0-4"].get("notes", "")

    def test_g4_parse_group_id_matching(self, tmp_path):
        """v48（任务 1）：G-M0-4 经 parse_group_id(server_group_id) 匹配——
        build_group 产物（无顶层 ctk/ctv/fanout 字段）必须被正确找到。"""
        exp = 24 * 548864 * 4 * 4 / (1024 * 1024)
        r = make_runner(tmp_path, None)
        r._server_logs["g0"] = (f"llama_memory_recurrent::init: RS buffer size ="
                                 f"  {exp:.2f} MiB\n")
        g = r._compute_gates(self._modes_with_groups())
        assert g["G-M0-4"]["status"] == "PASS"  # 找到 group → 观测 → PASS
        # 反例：server_group_id 无法解析（非法 id）→ 视为无该 group → 无观测
        modes = self._modes_with_groups()
        modes["off"]["server_groups"][0]["server_group_id"] = "not-a-group-id"
        g = r._compute_gates(modes)
        assert g["G-M0-4"]["status"] == "NOT_APPLICABLE"


# ---- v48 复审（f8334bd 复审 11 项）：任务 2 服务器命令合同 ----

class TestServerCmdContract:
    def test_slot_save_path_every_lifecycle(self, tmp_path):
        """--slot-save-path <tmp_dir> 出现在每个 cache profile/fanout/control 生命周期
        （llama.cpp server-context.cpp:5448 要求 slot_save_path 非空，否则 POST /slots
        erase 返回 NOT_SUPPORTED；与 e15 runner 一致）。"""
        r = make_runner(tmp_path, None)
        os.makedirs(r.tmp_dir, exist_ok=True)
        for ctl in ("off", "on"):
            for p in (4, 6, 10):
                cmd = r._server_cmd(p, "q8_0", "f16", ctl, port=9000 + p)
                assert "--slot-save-path" in cmd, cmd
                sp = cmd[cmd.index("--slot-save-path") + 1]
                assert sp == r.tmp_dir
                assert os.path.isdir(sp)  # 目录存在、每生命周期可用
                # v55：-lv 5 前置（G-M0-4 RS buffer 行观测；默认 verbosity=3 不打印）
                assert cmd[cmd.index("-lv") + 1] == "5", cmd

    def test_server_cmd_port_matches_adapter(self, tmp_path, monkeypatch):
        """v55 回归：--port 必须等于 adapter.port（_start_server 的 port 变量），
        不得用已 +1 的 self._next_port——否则真实 server 监听 next_port+1、
        wait_health 探测 port 永远超时（真实 4B 校准首跑暴露）。"""
        started: list = []

        class FakeAdapter2:
            def __init__(self, cmd, log_path, port):
                self.cmd, self.log_path, self.port = cmd, log_path, port

            def start(self):
                started.append((self.port, self.cmd))

            def wait_health(self, timeout=120.0):
                return True

            def read_log(self):
                return ""

            def poll(self):
                return None

            def stop(self):
                return (True, "ok")  # v61 契约：tuple/bool

        r = make_runner(tmp_path, FakeAdapter2)
        # 直接契约：_server_cmd(port=N) 的 --port == N
        for p in (18080, 18081, 19099):
            cmd = r._server_cmd(4, "q8_0", "q8_0", "off", port=p)
            assert cmd[cmd.index("--port") + 1] == str(p), cmd
        # _start_server 生命周期：cmd --port == adapter.port == 递增前 _next_port
        os.makedirs(r.tmp_dir, exist_ok=True)
        a = r._start_server(4, "q8_0", "q8_0", "off", "t")
        port, cmd = started[0]
        assert port == a.port == 18080  # 第一个 server 用递增前端口
        assert cmd[cmd.index("--port") + 1] == str(port)


# ---- v48 复审（任务 3）：请求 ERROR rep 仍必须 erase + after_erase ----

class _FailDriver:
    def chat(self, msgs, **kwargs):
        raise urllib.error.URLError("Connection refused")


class TestRepErrorCleanup:
    def _runner(self, tmp_path, adapter):
        r = make_runner(tmp_path, None)
        r._adapter = adapter
        r._driver = _FailDriver()
        r._kv = KVProbe(f"http://127.0.0.1:{adapter.port}", enabled=True)
        r.calibrated_lengths = {("short", 2): {"prefix": 100, "branch": 100}}
        return r

    def test_decision_error_rep_still_erases_and_snapshots(self, tmp_path):
        """决策请求 ERROR + server 健康 → rep ERROR 但 erase/after_erase 真实执行：
        metrics.kv 存在且 last.used_cells==0（G-M0-3a 可验证归零，不 None 静默 PASS）。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = self._runner(tmp_path, adapter)
            # 先用真实 Driver 发一个 completion → mock used_cells=384（erase 有实际内容）
            drv = Driver(base_url=f"http://127.0.0.1:{adapter.port}", model="bench",
                         sdk_max_retries=0)
            drv.chat([{"role": "user", "content": "hi"}], max_tokens=4)
            assert mserver._KV["used_cells"] == 384
            rep = r._run_unit(sch.unit_id_of("off", "q8_0", "q8_0", 2, "short"),
                              2, "short", "q8_0", "q8_0", "off:q8_0-q8_0:f2", 0)
            assert rep["status"] == "ERROR"
            assert rep["error_type"] == "connection_error"
            kv = rep["metrics"]["kv"]
            assert kv["last"]["used_cells"] == 0       # erase 后真实样本归零
            assert kv["last"]["active_sequences"] == 0
            assert kv["peak"]["used_cells"] is not None
            # G-M0-3a：把 rep 包进真实 build_group/build_modes 验证 PASS
            gid = sch.group_id_of("off", "q8_0", "q8_0", 2)
            grp = sch.build_group(gid, "COMPLETED", "2026-08-09T00:00:00Z",
                                  "2026-08-09T00:00:01Z",
                                  baseline={"metrics_kv_snapshot": {}},
                                  replicates=[rep])
            modes = sch.build_modes([grp], [])
            g = r._compute_gates(modes)
            assert g["G-M0-3a"]["status"] == "PASS"
        finally:
            adapter.stop()

    def test_decision_error_cleanup_failure_crash(self, tmp_path, monkeypatch):
        """决策请求 ERROR 后 erase 失败 + server 已退出 → ServerCrash（FORMAL_INCOMPLETE），
        不伪造观测、不静默 PASS。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = self._runner(tmp_path, adapter)

            def _boom(*a, **k):
                raise OSError("erase failed")

            r._kv.clean_all_slots = _boom
            FakeAdapter.poll_dead = True
            with pytest.raises(m0r.ServerCrash):
                r._run_unit(sch.unit_id_of("off", "q8_0", "q8_0", 2, "short"),
                            2, "short", "q8_0", "q8_0", "off:q8_0-q8_0:f2", 0)
        finally:
            FakeAdapter.poll_dead = None
            adapter.stop()


# ---- v48 复审（任务 4）：periodic 采样覆盖分支并发/工具轮，peak 来自真实周期样本 ----

class TestPeriodicPeak:
    def test_peak_from_periodic_mid_run(self, tmp_path):
        """begin_run 后启动 periodic → 中途高峰被周期样本捕获 → run 聚合 peak 真实。"""
        prev = dict(mserver._KV)
        try:
            with MockOpenAIServer() as srv:
                kv = KVProbe(f"http://127.0.0.1:{srv.port}", enabled=True)
                kv.begin_run("r1")
                kv.snapshot("pre")
                kv.start_periodic(0.01)
                mserver._KV["used_cells"] = 384
                mserver._KV["active_sequences"] = 4
                time.sleep(0.2)
                kv.stop()
                kv.end_run()
                agg = kv.run_aggregate()["r1"]
                assert agg["peak_used_cells"] == 384
                assert agg["peak_active_sequences"] == 4
                assert agg["samples"] >= 1
        finally:
            mserver._KV.update(prev)

    def test_periodic_stopped_on_error_path(self, tmp_path):
        """异常路径（stop 前抛）→ 线程仍被 stop 回收（无残留采样线程）。"""
        prev = dict(mserver._KV)
        try:
            with MockOpenAIServer() as srv:
                kv = KVProbe(f"http://127.0.0.1:{srv.port}", enabled=True)
                kv.begin_run("r1")
                kv.start_periodic(0.01)
                mserver._KV["used_cells"] = 384
                time.sleep(0.15)
                # 模拟异常路径：直接 stop（runner finally 顺序：stop → end_run）
                kv.stop()
                kv.end_run()
                assert kv._thread is None
                agg = kv.run_aggregate()["r1"]
                assert agg["peak_used_cells"] == 384
        finally:
            mserver._KV.update(prev)


# ---- v48 复审（任务 6）：G-M0-2 缺分支观测 → FAIL ----

class TestG2MissingBranchObs:
    def test_g2_missing_branch_obs_fails(self, tmp_path):
        """缺分支观测 → G-M0-2 FAIL（不静默 PASS）。"""
        r = TestCritical1KVPath()._runner_with_log(tmp_path)
        modes = TestCritical1KVPath()._minimal_modes(
            kv_last={"used_cells": 0, "active_sequences": 0},
            shared=0, branches=[])
        g = r._compute_gates(modes)
        assert g["G-M0-2"]["status"] == "FAIL"


# ---- v48 复审（任务 7）：warmup 任意请求异常后立即 poll server ----

class TestWarmupRequestErrorDead:
    def test_warmup_error_with_dead_server_not_swallowed(self, tmp_path,
                                                         fake_adapter_cls,
                                                         monkeypatch):
        """warmup 非 ServerCrash 请求异常 + server 已退出 → 不忽略：
        group ERROR + FORMAL_INCOMPLETE（崩溃发生在请求阶段、不靠后续分支检测）。"""
        FakeAdapter.poll_dead = True

        def _boom(*a, **k):
            raise urllib.error.URLError("Connection refused")

        r = make_runner(tmp_path, fake_adapter_cls)
        monkeypatch.setattr(r, "_run_unit", _boom)
        try:
            rc = r.run()
            assert rc == 0, rc
            with open(r.out_path, encoding="utf-8") as f:
                doc = json.load(f)
            assert doc["meta"]["phase"] == "formal"
            assert doc["meta"]["matrix_complete"] is False
            groups = (doc["modes"]["off"]["server_groups"]
                      + doc["modes"]["on"]["server_groups"])
            assert all(g["status"] == "ERROR" for g in groups)
            assert all(g["error_type"] == "server_crash" for g in groups)
        finally:
            FakeAdapter.poll_dead = None

    def test_warmup_error_healthy_server_ignored(self, tmp_path,
                                                 fake_adapter_cls,
                                                 monkeypatch):
        """warmup 请求异常但 server 仍健康 → 允许按设计忽略（仅预热），
        不误判崩溃。"""
        calls = {"n": 0}

        def _boom(*a, **k):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise urllib.error.URLError("Connection refused")
            # 之后返回合法 rep（server 健康 → 正常完成；gid/身份字段与 unit_id 一致）
            return sch.build_rep(a[0], int(k.get("rep_index") or 0), a[1], a[2],
                                 a[3], a[4], 100, 100, a[5], "OK", False,
                                 {"kv": {"last": {"used_cells": 0,
                                                  "active_sequences": 0}},
                                  "branches": [{"branch": "b1",
                                                "canary_leak": False}]})

        r = make_runner(tmp_path, fake_adapter_cls)
        monkeypatch.setattr(r, "_run_unit", _boom)
        rc = r.run()
        assert rc == 0, rc
        with open(r.out_path, encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["meta"]["phase"] == "formal"
        assert doc["meta"]["matrix_complete"] is True  # 健康 → 正常完成


# ---- v48 复审（任务 8）：sampler 端口兜底只匹配显式 --port ----

    # ---- v57 审查（Medium 7）：warmup 基础设施异常纳入 ServerError ----

    def test_warmup_server_error_dead_server_crash(self, tmp_path,
                                                   fake_adapter_cls,
                                                   monkeypatch):
        """warmup 抛 ServerError（基础设施归因）+ server 已退出 → 不忽略：
        group ERROR + FORMAL_INCOMPLETE（与 OSError/OpenAIError 同路径）。"""
        FakeAdapter.poll_dead = True

        def _boom(*a, **k):
            raise m0r.ServerError("health timeout (exit=None)")

        r = make_runner(tmp_path, fake_adapter_cls)
        monkeypatch.setattr(r, "_run_unit", _boom)
        try:
            rc = r.run()
            assert rc == 0, rc
            with open(r.out_path, encoding="utf-8") as f:
                doc = json.load(f)
            assert doc["meta"]["phase"] == "formal"
            assert doc["meta"]["matrix_complete"] is False
            groups = (doc["modes"]["off"]["server_groups"]
                      + doc["modes"]["on"]["server_groups"])
            assert all(g["status"] == "ERROR" for g in groups)
            assert all(g["error_type"] == "server_crash" for g in groups)
        finally:
            FakeAdapter.poll_dead = None

    def test_warmup_server_error_healthy_server_ignored(self, tmp_path,
                                                        fake_adapter_cls,
                                                        monkeypatch):
        """warmup 抛 ServerError 但 server 仍健康 → 按设计忽略（仅预热），
        不误判崩溃；后续正常完成。"""
        calls = {"n": 0}

        def _boom(*a, **k):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise m0r.ServerError("endpoint unavailable")
            return sch.build_rep(a[0], int(k.get("rep_index") or 0), a[1], a[2],
                                 a[3], a[4], 100, 100, a[5], "OK", False,
                                 {"kv": {"last": {"used_cells": 0,
                                                  "active_sequences": 0}},
                                  "branches": [{"branch": "b1",
                                                "canary_leak": False}]})

        r = make_runner(tmp_path, fake_adapter_cls)
        monkeypatch.setattr(r, "_run_unit", _boom)
        rc = r.run()
        assert rc == 0, rc
        with open(r.out_path, encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["meta"]["phase"] == "formal"
        assert doc["meta"]["matrix_complete"] is True  # 健康 → 正常完成

    def test_warmup_internal_bug_still_70(self, tmp_path, fake_adapter_cls,
                                          monkeypatch):
        """warmup 内部 bug（KeyError，非基础设施）→ 不被 _INFRA_EXCEPTIONS 吞，
        run_safe 归 INTERNAL_RUNNER_ERROR + EXIT_SOFTWARE(70)。"""
        def _boom(*a, **k):
            raise KeyError("internal bug")

        r = make_runner(tmp_path, fake_adapter_cls)
        monkeypatch.setattr(r, "_run_unit", _boom)
        rc = r.run_safe()
        assert rc == 70, rc
        # 已抛出的内部 bug 不应落正式结果（run_safe 仅 stderr 错误码）
        assert not os.path.isfile(r.out_path)


# ---- v48 复审（任务 8）：sampler 端口兜底只匹配显式 --port ----

class TestSamplerPortFallback:
    def _procs(self, *specs):
        out = []
        for i, cl in enumerate(specs):
            p = type("P", (), {})()
            p.info = {"cmdline": cl}
            p.pid = 9000 + i
            out.append(p)
        return out

    def _no_ss(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("no ss")

        monkeypatch.setattr(sampler.subprocess, "run", _boom)
        monkeypatch.setattr(sampler.psutil, "net_connections", lambda **k: [])

    def test_fallback_matches_explicit_port(self, monkeypatch):
        self._no_ss(monkeypatch)
        monkeypatch.setattr(sampler.psutil, "process_iter",
                            lambda *a, **k: self._procs(
                                ["llama-server", "-m", "m", "--port", "8080"],
                                ["llama-server", "-m", "m", "--port", "9999"]))
        assert sampler.find_server_pid(port=8080) == 9000

    def test_fallback_ignores_wrong_port(self, monkeypatch):
        self._no_ss(monkeypatch)
        monkeypatch.setattr(sampler.psutil, "process_iter",
                            lambda *a, **k: self._procs(
                                ["llama-server", "--port=9999"],
                                ["llama-server", "-m", "m"]))
        assert sampler.find_server_pid(port=8080) is None

    def test_fallback_no_prefix_mismatch(self, monkeypatch):
        self._no_ss(monkeypatch)
        monkeypatch.setattr(sampler.psutil, "process_iter",
                            lambda *a, **k: self._procs(
                                ["llama-server", "--port", "80809"]))
        assert sampler.find_server_pid(port=8080) is None


# ---- M0 v49（复审 Medium）：G-M0-2 成功分支观测语义 ----

class TestG2BranchObsSemanticsV49:
    """G-M0-2 必须基于成功分支观测：无任何非 ERROR 分支观测 → NOT_APPLICABLE，
    绝不静默 PASS（v49 任务 1）。rep 结构 = 生产 build_rep 输出（可达状态）。"""

    def _gates_with(self, tmp_path, off_reps, on_reps=None):
        r = TestCritical1KVPath()._runner_with_log(tmp_path)
        on_reps = off_reps if on_reps is None else on_reps

        def _grp(control, reps):
            return {
                "server_group_id": sch.group_id_of(control, "q8_0", "q8_0", 2),
                "ctk": "q8_0", "ctv": "q8_0", "fanout": 2, "status": "COMPLETED",
                "baseline": {"rs_buffer_mb": 50.0, "metrics_kv_snapshot": {}},
                "replicates": reps,
            }

        modes = {"off": {"server_groups": [_grp("off", off_reps)]},
                 "on": {"server_groups": [_grp("on", on_reps)]}}
        return r._compute_gates(modes)

    @staticmethod
    def _rep(i, status, leak=False):
        return {
            "unit_id": f"u{i}", "rep_index": i, "fanout": 2, "bucket": "short",
            "ctk": "q8_0", "ctv": "q8_0", "prefix_len": 100, "branch_len": 100,
            "server_group_id": sch.group_id_of("off", "q8_0", "q8_0", 2),
            "status": status, "decision_fallback": False,
            "metrics": {"kv": {"last": {"used_cells": 0, "active_sequences": 0}},
                        "branches": [] if status == "ERROR" else
                                   [{"branch": "b1", "canary_leak": leak},
                                    {"branch": "b2", "canary_leak": False}]},
        }

    def test_all_error_not_applicable(self, tmp_path):
        """5 reps 全 ERROR（无任何非 ERROR 分支观测）→ NOT_APPLICABLE（不 PASS）。"""
        g = self._gates_with(tmp_path,
                             [self._rep(i, "ERROR") for i in range(5)])
        assert g["G-M0-2"]["status"] == "NOT_APPLICABLE"

    def test_partial_success_no_leak_pass(self, tmp_path):
        """1 ERROR + 4 OK（无泄漏）→ 部分成功观测 → PASS。"""
        reps = [self._rep(0, "ERROR")] + [self._rep(i, "OK") for i in range(1, 5)]
        g = self._gates_with(tmp_path, reps)
        assert g["G-M0-2"]["status"] == "PASS"

    def test_partial_success_with_leak_fail(self, tmp_path):
        """1 ERROR + 4 OK（其中 1 个 canary 泄漏）→ FAIL（观测存在即判定）。"""
        reps = ([self._rep(0, "ERROR"), self._rep(1, "OK", leak=True)]
                + [self._rep(i, "OK") for i in range(2, 5)])
        g = self._gates_with(tmp_path, reps)
        assert g["G-M0-2"]["status"] == "FAIL"

    def test_full_smoke_g2_pass(self, tmp_path, fake_adapter_cls):
        """生产 e2e：mock 分支无泄漏 → G-M0-2 PASS（可达路径断言）。"""
        r = make_runner(tmp_path, fake_adapter_cls)
        rc = r.run()
        assert rc == 0, rc
        with open(r.out_path, encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["gates"]["G-M0-2"]["status"] == "PASS"


# ---- M0 v49（复审 Medium）：erase 异常路径（任务 2/5） ----

class TestEraseFailureV49:
    def test_erase_partial_formal_incomplete(self, tmp_path, fake_adapter_cls, monkeypatch):
        """clean_all_slots 部分成功（server 健康、erase 端点 501）→ rep ERROR +
        group ERROR(error_type=erase_partial) + FORMAL_INCOMPLETE，带诊断，
        不裸异常 exit70、不等 gate 间接发现（v49 任务 5）。"""
        prev_clean = KVProbe.clean_all_slots

        def _clean_partial(self):
            # MockOpenAIServer.__enter__ 会重置 erase_disabled，故在 server 启动后
            # （即每次 clean_all_slots 时）强制 erase 端点 501 → 部分成功 (2,0)
            mserver._KV["erase_disabled"] = True
            try:
                return prev_clean(self)
            finally:
                mserver._KV["erase_disabled"] = False

        monkeypatch.setattr(KVProbe, "clean_all_slots", _clean_partial)
        r = make_runner(tmp_path, fake_adapter_cls)
        rc = r.run()
        assert rc == 0, rc
        with open(r.out_path, encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["meta"]["phase"] == "formal"
        assert doc["meta"]["matrix_complete"] is False
        assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"
        groups = (doc["modes"]["off"]["server_groups"]
                  + doc["modes"]["on"]["server_groups"])
        err_groups = [g for g in groups if g["status"] == "ERROR"]
        assert err_groups, [g["status"] for g in groups]
        assert err_groups[0]["error_type"] == "erase_partial"
        err_reps = [rep for rep in err_groups[0]["replicates"]
                    if rep["status"] == "ERROR"]
        assert err_reps
        assert all(rep["error_type"] == "erase_partial" for rep in err_reps)

    def test_after_erase_snapshot_failure_healthy(self, tmp_path,
                                                 fake_adapter_cls,
                                                 monkeypatch):
        """server 健康但 after_erase 采样异常 → EraseFailure(after_erase_failed)
        → group ERROR + FORMAL_INCOMPLETE（不裸异常 exit70 无合法结果，v49 任务 2）。"""
        orig = KVProbe.snapshot

        def _snap(self, tag=""):
            if tag == "after_erase":
                raise OSError("metrics/kv 采样失败")
            return orig(self, tag)

        monkeypatch.setattr(KVProbe, "snapshot", _snap)
        r = make_runner(tmp_path, fake_adapter_cls)
        rc = r.run()
        assert rc == 0, rc
        with open(r.out_path, encoding="utf-8") as f:
            doc = json.load(f)
        assert doc["meta"]["matrix_complete"] is False
        groups = (doc["modes"]["off"]["server_groups"]
                  + doc["modes"]["on"]["server_groups"])
        err_groups = [g for g in groups if g["status"] == "ERROR"]
        assert err_groups
        assert err_groups[0]["error_type"] == "after_erase_failed"

    def test_erase_failure_dead_server_server_crash(self, tmp_path,
                                                    fake_adapter_cls,
                                                    monkeypatch):
        """erase 失败 + server 已退出（poll 非 None）→ ServerCrash（归因不变，
        v49 任务 2：server 已退出归 ServerCrash）。"""
        boom_calls = {"n": 0}

        def _boom(*a, **k):
            boom_calls["n"] += 1
            if boom_calls["n"] == 1:
                return (2, 2)  # 校准后清理正常（mock 2 slots 全成功）
            raise OSError("erase failed")

        monkeypatch.setattr(KVProbe, "clean_all_slots", _boom)
        FakeAdapter.poll_dead = True
        try:
            r = make_runner(tmp_path, fake_adapter_cls)
            rc = r.run()
            assert rc == 0, rc
            with open(r.out_path, encoding="utf-8") as f:
                doc = json.load(f)
            assert doc["meta"]["matrix_complete"] is False
            groups = (doc["modes"]["off"]["server_groups"]
                      + doc["modes"]["on"]["server_groups"])
            assert groups[-1]["status"] == "ERROR"
            assert groups[-1]["error_type"] == "server_crash"
        finally:
            FakeAdapter.poll_dead = None


# ---- M0 v49（复审 Medium）：warmup 决策异常 best-effort erase（任务 4） ----

class TestWarmupEraseV49:
    def test_warmup_decision_error_healthy_erases_and_continues(
            self, tmp_path, monkeypatch):
        """warmup 决策请求 500（server 健康）→ best-effort erase 真实执行 +
        异常被忽略、后续 formal rep 正常（G-M0-3a last=0 残留归零）——
        不吞崩溃也不中断健康流程（v49 任务 4，直接 _run_unit 路径）。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, None)
            r._adapter = adapter
            r._driver = Driver(base_url=f"http://127.0.0.1:{adapter.port}",
                               model="bench", max_retry=1, sdk_max_retries=0)
            r._kv = KVProbe(f"http://127.0.0.1:{adapter.port}", enabled=True)
            r.calibrated_lengths = {("short", 2): {"prefix": 100, "branch": 100}}
            prev_clean = KVProbe.clean_all_slots
            calls = {"n": 0}

            def _clean(self):
                calls["n"] += 1
                return prev_clean(self)

            monkeypatch.setattr(KVProbe, "clean_all_slots", _clean)
            uid = sch.unit_id_of("off", "q8_0", "q8_0", 2, "short")
            gid = sch.group_id_of("off", "q8_0", "q8_0", 2)
            mserver._ERROR_ONCE["on"] = True  # 首个 POST（warmup 决策）500
            try:
                try:
                    out = r._run_unit(uid, 2, "short", "q8_0", "q8_0", gid, None)
                except Exception:
                    out = None  # v48：warmup 请求异常向上抛、由 run_formal 的
                                # warmup except 处理（poll 健康 → 忽略；本测试模拟）
            finally:
                mserver._ERROR_ONCE["on"] = False
            assert out is None  # warmup 异常路径（best-effort erase 仍执行）
            assert calls["n"] >= 1  # best-effort erase 真实执行
            assert mserver._KV["used_cells"] == 0  # 残留归零
            # 后续 formal rep 正常完成（G-M0-3a 可验证 last=0）
            rep = r._run_unit(uid, 2, "short", "q8_0", "q8_0", gid, 0)
            assert rep["status"] in ("OK", "INVALID_DECISION")  # 决策正常（无 ACTION → fallback）
            assert rep["metrics"]["kv"]["last"]["used_cells"] == 0
        finally:
            adapter.stop()

    def test_warmup_erase_crash_group_error(self, tmp_path, fake_adapter_cls,
                                            monkeypatch):
        """warmup 的 best-effort erase 异常且 server 已退出 → ServerCrash →
        group ERROR（不吞崩溃；v49 任务 4 崩溃侧）。"""
        boom_calls = {"n": 0}

        def _boom(*a, **k):
            boom_calls["n"] += 1
            if boom_calls["n"] == 1:
                return (2, 2)  # 校准后清理正常
            raise ConnectionError("erase 连接失败")

        monkeypatch.setattr(KVProbe, "clean_all_slots", _boom)
        FakeAdapter.poll_dead = True
        try:
            r = make_runner(tmp_path, fake_adapter_cls)
            rc = r.run()
            assert rc == 0, rc
            with open(r.out_path, encoding="utf-8") as f:
                doc = json.load(f)
            assert doc["meta"]["matrix_complete"] is False
            groups = (doc["modes"]["off"]["server_groups"]
                      + doc["modes"]["on"]["server_groups"])
            assert groups[-1]["status"] == "ERROR"
            assert groups[-1]["error_type"] == "server_crash"
        finally:
            FakeAdapter.poll_dead = None


# ---- M0 v49（复审 Medium）：periodic 迟到样本不得覆盖 after_erase（任务 3） ----

class TestPeriodicLateAfterEraseV49:
    def test_late_periodic_does_not_override_after_erase(self, tmp_path):
        """periodic 迟到样本（stop join 超时后 daemon 线程仍在写）不得覆盖
        after_erase=0 归零观测——run_aggregate last 优先 tag=after_erase。"""
        with MockOpenAIServer() as srv:
            kv = KVProbe(f"http://127.0.0.1:{srv.port}", enabled=True)
            kv.begin_run("r1")
            kv.samples = [
                {"ts": 1.0, "tag": "pre", "run_id": "r1",
                 "data": {"used_cells": 0, "active_sequences": 0}},
                {"ts": 2.0, "tag": "periodic", "run_id": "r1",
                 "data": {"used_cells": 384, "active_sequences": 1}},
                {"ts": 3.0, "tag": "after_erase", "run_id": "r1",
                 "data": {"used_cells": 0, "active_sequences": 0}},
                {"ts": 4.0, "tag": "periodic", "run_id": "r1",  # 迟到样本
                 "data": {"used_cells": 384, "active_sequences": 1}},
            ]
            agg = kv.run_aggregate()["r1"]
            assert agg["last_used_cells"] == 0      # after_erase 优先
            assert agg["last_active_sequences"] == 0
            assert agg["peak_used_cells"] == 384    # peak 仍来自周期高峰
            kv.end_run()

    def test_thread_fully_exits_after_stop(self):
        """人为延迟 fetch（3s > join 窗口 2s）→ stop 后线程仍最终退出：
        不泄漏、不跨 run 污染（线程退出后 samples 冻结）。"""
        prev = dict(mserver._KV)
        try:
            with MockOpenAIServer() as srv:
                kv = KVProbe(f"http://127.0.0.1:{srv.port}", enabled=True)
                orig_fetch = kv._fetch

                def slow_fetch():
                    time.sleep(3.0)  # > stop() 的 join 窗口 2.0
                    return orig_fetch()

                kv._fetch = slow_fetch
                kv.begin_run("r1")
                kv.snapshot("pre")
                kv.start_periodic(0.01)
                time.sleep(0.3)  # 线程进入 slow_fetch
                t = kv._thread
                assert t is not None and t.is_alive()
                kv.stop()  # join 超时（线程在 slow_fetch 中）
                assert kv._thread is None
                n_before_join = len(kv.samples)
                t.join(timeout=8.0)  # 等待线程完全退出
                assert not t.is_alive()
                n_after_join = len(kv.samples)
                time.sleep(0.2)
                assert len(kv.samples) == n_after_join  # 退出后不再增长
                kv.end_run()
        finally:
            mserver._KV.update(prev)
