"""M0 v51 复审阻断修复测试——完整 run e2e 错误码与校准异常归因。

覆盖（v51 任务 1-5）：
- 任务 1：ERROR_CODES 收录 erase_failed/after_erase_missing；**完整 run() e2e**：
  真实端点失败触发 after_erase_missing（/metrics/kv 500）与 erase_failed
  （clean_all_slots 异常防御分支）→ rc=0、phase=formal、FORMAL_INCOMPLETE、
  matrix_complete=false、ERROR group/rep error_type 正确、最终 envelope
  validator 通过（**禁止只测 _run_unit**）
- 任务 2：warmup clean_all_slots 返回 (0,0) → 主动 poll：dead → ServerCrash/
  group ERROR；healthy → note + 继续；真实端点不可达（/slots 畸形）healthy
  正例 + poll dead 反例
- 任务 3：校准 catch-all 只捕获明确基础设施异常；AssertionError/内部 bug →
  run_safe 归 INTERNAL_RUNNER_ERROR + exit 70
- 任务 4：warmup 工具轮异常与决策异常一致：dead → ServerCrash；healthy →
  记 note 不静默
- 任务 5：preflight envelope builder 支持 notes 参数并保留 runner 已有 notes
  （validator 仍 22 键 canonical meta 不变）
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from framework.driver import Driver
from framework.kv_probe import KVProbe
from runner import m0_fanout_runner as m0r
from runner import m0_schema as sch
from tests import mock_server as mserver


class FakeAdapter:
    """与 test_m0_v50_failures 同构的 mock adapter（独立副本，避免跨文件耦合）。"""

    poll_dead = None
    _instances = 0

    def __init__(self, cmd, log_path, port, crash_at=None):
        self.cmd = cmd
        self.log_path = log_path
        self.port = port
        FakeAdapter._instances += 1
        self.crash_at = crash_at
        self._server: mserver.MockOpenAIServer | None = None

    def start(self):
        self._server = mserver.MockOpenAIServer(crash_at=self.crash_at).__enter__()
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

    def read_log(self):
        return ""


def make_runner(tmp_path, **over):
    args = dict(
        server_bin="fake-bin", model="mock.gguf", ctx_size=4096,
        port_base=18080, decision_n_predict=16, branch_n_predict=64,
        tool_rounds=1, out_path=str(tmp_path / "out.json"),
        tmp_dir=str(tmp_path / "tmp"), ngl=99, adapter_cls=FakeAdapter,
    )
    args.update(over)
    r = m0r.M0FanoutRunner(**args)
    r.calibrated_lengths = {("short", 2): {"prefix": 100, "branch": 100}}
    return r


def patch_after_validation(monkeypatch, fn):
    """包装 run_decision_validation：验证完成后（formal 开始前）执行 fn()。

    用于在真实端点注入故障开关（校准+验证阶段干净、formal 阶段触发），
    使完整 run() 走真实失败归因链路（非只测 _run_unit）。
    """
    original = m0r.M0FanoutRunner.run_decision_validation

    def wrapper(self):
        result = original(self)
        fn()
        return result

    monkeypatch.setattr(m0r.M0FanoutRunner, "run_decision_validation", wrapper)


def load_doc(tmp_path):
    with open(tmp_path / "out.json", encoding="utf-8") as f:
        return json.load(f)


def find_error_group(doc):
    """遍历 modes.off/on server_groups，返回第一个 status=ERROR 的 group。"""
    for control in ("off", "on"):
        for g in doc["modes"].get(control, {}).get("server_groups", []):
            if g["status"] == "ERROR":
                return g
    return None


# ---- 任务 1：ERROR_CODES 收录 + 完整 run e2e ----


class TestErrorCodesV51:
    def test_new_codes_in_enum(self):
        assert "erase_failed" in sch.ERROR_CODES
        assert "after_erase_missing" in sch.ERROR_CODES

    def test_validator_accepts_new_codes(self):
        # rep/group error_type 的 enum 校验必须接受两个新码
        # （validate_modes_group_rep 为唯一权威路径，构造最小 ERROR group）
        uid = sch.unit_id_of("off", "q8_0", "q8_0", 2, "short")
        gid = sch.group_id_of("off", "q8_0", "q8_0", 2)
        rep1 = sch.build_rep(uid, 0, 2, "short", "q8_0", "q8_0", 100, 100,
                             gid, "ERROR", False, {}, error_type="erase_failed",
                             error_stage="formal", error_bucket="short")
        rep2 = sch.build_rep(uid, 1, 2, "short", "q8_0", "q8_0", 100, 100,
                             gid, "ERROR", False, {}, error_type="after_erase_missing",
                             error_stage="formal", error_bucket="short")
        g = sch.build_group(gid, "ERROR", "t0", "t1",
                            replicates=[rep1, rep2], error_type="erase_failed",
                            error_stage="formal")
        modes = {"off": {"server_groups": [g]}, "on": {"server_groups": []}}
        errs = sch.validate_modes_group_rep(modes, matrix_complete=False,
                                            rejections=[])
        assert not errs, errs
        # 非枚举值仍拒绝
        rep_bad = sch.build_rep(uid, 0, 2, "short", "q8_0", "q8_0", 100, 100,
                                gid, "ERROR", False, {}, error_type="bogus_code",
                                error_stage="formal", error_bucket="short")
        g_bad = sch.build_group(gid, "ERROR", "t0", "t1",
                                replicates=[rep_bad], error_type="bogus_code",
                                error_stage="formal")
        modes_bad = {"off": {"server_groups": [g_bad]}, "on": {"server_groups": []}}
        errs_bad = sch.validate_modes_group_rep(modes_bad, matrix_complete=False,
                                                rejections=[])
        assert errs_bad


class TestE2EAfterEraseMissing:
    def test_full_run_after_erase_missing(self, tmp_path, monkeypatch):
        """完整 run()：formal 阶段 /metrics/kv 端点真实 500 → after_erase 观测缺失
        → EraseFailure('after_erase_missing') → group ERROR + FORMAL_INCOMPLETE
        → 合法落盘（rc=0、phase=formal、matrix_complete=false、validator 通过）。"""
        prev = mserver._KV["metrics_fail"]
        mserver._KV["metrics_fail"] = False
        patch_after_validation(
            monkeypatch,
            lambda: mserver._KV.__setitem__("metrics_fail", True))
        try:
            r = make_runner(tmp_path)
            rc = r.run()
        finally:
            mserver._KV["metrics_fail"] = prev
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        meta = doc["meta"]
        assert meta["phase"] == "formal"
        # FORMAL_INCOMPLETE 落盘表现：matrix_complete=false → verdict=INVALID…
        assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"
        assert meta["matrix_complete"] is False
        g = find_error_group(doc)
        assert g is not None, "必须存在 ERROR group"
        assert g["error_type"] == "after_erase_missing"
        err_reps = [rep for rep in g["replicates"] if rep["status"] == "ERROR"]
        assert err_reps and all(rep["error_type"] == "after_erase_missing"
                                for rep in err_reps)


class TestE2EEraseFailed:
    def test_full_run_erase_failed(self, tmp_path, monkeypatch):
        """完整 run()：clean_all_slots 抛异常（server 健康）→ EraseFailure
        ('erase_failed') → group ERROR + FORMAL_INCOMPLETE → 合法落盘
        （rc=0、phase=formal、matrix_complete=false、validator 通过）。"""
        def boom(self):
            raise OSError("clean_all_slots network error")

        monkeypatch.setattr(KVProbe, "clean_all_slots", boom)
        r = make_runner(tmp_path)
        rc = r.run()
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        meta = doc["meta"]
        assert meta["phase"] == "formal"
        assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"
        assert meta["matrix_complete"] is False
        g = find_error_group(doc)
        assert g is not None
        assert g["error_type"] == "erase_failed"
        err_reps = [rep for rep in g["replicates"] if rep["status"] == "ERROR"]
        assert err_reps and all(rep["error_type"] == "erase_failed"
                                for rep in err_reps)


# ---- 任务 2：warmup clean_all_slots (0,0) → poll ----


class TestWarmupZeroZero:
    def test_healthy_continues_with_note(self, tmp_path, monkeypatch):
        """(0,0) + server 健康 → 允许继续（记可诊断 note）；完整矩阵仍完成。"""
        monkeypatch.setattr(KVProbe, "clean_all_slots",
                            lambda self: (0, 0))
        r = make_runner(tmp_path)
        rc = r.run()
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        assert doc["meta"]["matrix_complete"] is True
        assert any("(0,0)" in n for n in doc["notes"]), doc["notes"]

    def test_real_endpoint_unreachable_healthy(self, tmp_path, monkeypatch):
        """真实端点不可达（/slots 畸形响应 → list_slots 空 → (0,0)）+ 健康 →
        记 note + 继续；矩阵完整完成（正例）。"""
        prev = mserver._KV["slots_malformed"]
        mserver._KV["slots_malformed"] = False
        patch_after_validation(
            monkeypatch,
            lambda: mserver._KV.__setitem__("slots_malformed", True))
        try:
            r = make_runner(tmp_path)
            rc = r.run()
        finally:
            mserver._KV["slots_malformed"] = prev
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        assert doc["meta"]["matrix_complete"] is True
        assert any("(0,0)" in n for n in doc["notes"]), doc["notes"]

    def test_dead_raises_server_crash(self, tmp_path, monkeypatch):
        """(0,0) + server 已退出 → ServerCrash → group ERROR + FORMAL_INCOMPLETE
        （反例：不得静默继续）。"""
        monkeypatch.setattr(KVProbe, "clean_all_slots",
                            lambda self: (0, 0))
        patch_after_validation(monkeypatch,
                               lambda: monkeypatch.setattr(FakeAdapter, "poll_dead", True))
        r = make_runner(tmp_path)
        rc = r.run()
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        meta = doc["meta"]
        assert meta["phase"] == "formal"
        assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"
        assert meta["matrix_complete"] is False
        g = find_error_group(doc)
        assert g is not None
        assert g["error_type"] == "server_crash"


# ---- 任务 3：校准 catch-all 不吞内部 bug ----


class TestCalibrationCatchAll:
    def test_internal_bug_goes_to_exit70(self, tmp_path, monkeypatch, capsys):
        """校准阶段内部 bug（AssertionError）→ 不被吞 → run_safe 归
        INTERNAL_RUNNER_ERROR + EXIT_SOFTWARE(70)。"""

        def bug(self):
            raise AssertionError("internal invariant broken")

        monkeypatch.setattr(m0r.M0FanoutRunner, "run_calibration", bug)
        r = make_runner(tmp_path)
        rc = r.run_safe()
        assert rc == sch.EXIT_SOFTWARE
        assert "INTERNAL_RUNNER_ERROR" in capsys.readouterr().err
        assert not (tmp_path / "out.json").exists(), "内部 bug 不落盘结果"

    def test_infra_exception_preflight_infra(self, tmp_path, monkeypatch):
        """校准阶段明确基础设施异常（HTTP 500）→ PREFLIGHT_INFRA 合法落盘
        （rc=0、preflight、validator 通过）。"""

        def infra(self):
            raise urllib.error.HTTPError(
                "http://calib", 500, "Internal Server Error", {}, None)

        monkeypatch.setattr(m0r.M0FanoutRunner, "run_calibration", infra)
        r = make_runner(tmp_path)
        rc = r.run()
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        meta = doc["meta"]
        assert meta["phase"] == "preflight"
        assert meta["preflight_reason"] == "validation_incomplete"
        assert meta["parity_ok"] is None
        assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"


# ---- 任务 4：warmup 工具轮异常 ----


class TestWarmupToolRoundError:
    @staticmethod
    def _patch_chat_first_n_tool_calls(monkeypatch, n):
        original = Driver.chat
        calls = {"n": 0}

        def chat(self, messages, **kwargs):
            text = "\n".join(str(m.get("content", "")) for m in messages)
            if "m0_fanout_probe" in text:
                calls["n"] += 1
                if calls["n"] <= n:
                    raise urllib.error.HTTPError(
                        "http://tool", 500, "Internal Server Error", {}, None)
            return original(self, messages, **kwargs)

        monkeypatch.setattr(Driver, "chat", chat)
        return calls

    def test_healthy_records_note_and_continues(self, tmp_path, monkeypatch):
        """warmup 工具轮请求异常（server 健康）→ 记可诊断 note 不静默；
        warmup 后 formal rep 正常 → 矩阵完整完成。"""
        self._patch_chat_first_n_tool_calls(monkeypatch, sch.WARMUP_REPS)
        r = make_runner(tmp_path)
        rc = r.run()
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        assert doc["meta"]["matrix_complete"] is True
        assert any("工具轮" in n for n in doc["notes"]), doc["notes"]

    def test_dead_raises_server_crash(self, tmp_path, monkeypatch):
        """warmup 工具轮异常 + server 已退出 → ServerCrash → group ERROR +
        FORMAL_INCOMPLETE（与决策异常一致）。"""
        self._patch_chat_first_n_tool_calls(monkeypatch, sch.WARMUP_REPS)
        patch_after_validation(monkeypatch,
                               lambda: monkeypatch.setattr(FakeAdapter, "poll_dead", True))
        r = make_runner(tmp_path)
        rc = r.run()
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        meta = doc["meta"]
        assert meta["phase"] == "formal"
        assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"
        assert meta["matrix_complete"] is False
        g = find_error_group(doc)
        assert g is not None
        assert g["error_type"] == "server_crash"


# ---- 任务 5：preflight envelope notes ----


class TestPreflightNotes:
    def test_builder_notes_param(self, tmp_path):
        # runner._meta_context() 提供完整 22 键 canonical meta（validator 通过）
        r = make_runner(tmp_path)
        doc = sch.build_preflight_envelope(
            r._meta_context(), reason="validation_incomplete", source_phase="preflight",
            parity_ok=None, parity_progress={"completed": [], "errors": []},
            token_count_method=None,
            decision_validation=None, preflight_rejections=[],
            notes=["note-a", "note-b"])
        assert doc["notes"] == ["note-a", "note-b"]
        # validator 接受（22 键 canonical meta 不变）
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs

    def test_runner_notes_preserved_in_preflight(self, tmp_path, monkeypatch):
        """runner 已有 notes（_add_note）在 preflight 落盘时保留。"""
        def infra(self):
            raise urllib.error.HTTPError(
                "http://calib", 500, "Internal Server Error", {}, None)

        monkeypatch.setattr(m0r.M0FanoutRunner, "run_calibration", infra)
        r = make_runner(tmp_path)
        r._add_note("runner-keep-me")
        rc = r.run()
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        assert "runner-keep-me" in doc["notes"]
