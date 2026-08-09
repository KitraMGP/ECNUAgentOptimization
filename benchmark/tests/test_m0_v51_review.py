"""M0 v51/v52 复审修复测试——完整 run e2e 错误码与校准异常归因。

覆盖（v51 任务 1-5 / v52 任务 1-5）：
- 任务 1：ERROR_CODES 收录 erase_failed/after_erase_missing；**完整 run() e2e**：
  真实端点失败触发 after_erase_missing（/metrics/kv 500）与 erase_failed
  （clean_all_slots 异常防御分支）→ rc=0、phase=formal、FORMAL_INCOMPLETE、
  matrix_complete=false、ERROR group/rep error_type 正确、最终 envelope
  validator 通过（**禁止只测 _run_unit**）
- 任务 2：warmup clean_all_slots 返回 (0,0) → 主动 poll：dead → ServerCrash/
  group ERROR；healthy → note + 继续；真实端点不可达（/slots 空列表）healthy
  正例 + poll dead 反例
- 任务 3：校准 catch-all 只捕获明确基础设施异常；AssertionError/内部 bug →
  run_safe 归 INTERNAL_RUNNER_ERROR + exit 70
- 任务 4：warmup 工具轮异常与决策异常一致：dead → ServerCrash；healthy →
  记 note 不静默
- 任务 5：preflight envelope builder 支持 notes 参数并保留 runner 已有 notes
  （validator 仍 22 键 canonical meta 不变）
- v52 任务 1-2：**真实 openai SDK 异常（InternalServerError/APIConnectionError）
  从 Driver.chat 校准路径抛出 → PREFLIGHT_INFRA 合法落盘（rc=0、validator 通过）；
  禁止 patch run_calibration 抛 HTTPError 替代；AssertionError → run_safe 70**
- v52 任务 3：warmup 决策请求异常 healthy → note 落盘；dead → ServerCrash
- v52 任务 4：mock_server slots_malformed 修正为真实 (0,0) 路径（/slots 空列表）；
  erase_failed 保留为框架防御码（monkeypatch e2e），不宣称真实端点路径
- v52 任务 5：校准后 clean_all_slots 异常 / (0,0) + server 健康 → note；
  死 server → ServerCrash → PREFLIGHT_INFRA
"""
from __future__ import annotations

import json

import httpx
import openai
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

    def test_empty_slots_endpoint_healthy(self, tmp_path, monkeypatch):
        """真实端点返回空列表（/slots 空列表 → list_slots 空 → clean_all_slots
        返回 (0,0)）+ 健康 → 记 note + 继续；矩阵完整完成（正例）。"""
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
    """v52（任务 2）：真实 openai SDK 异常从 Driver.chat 校准路径抛出（禁止 patch
    run_calibration 抛 HTTPError 替代）；内部 bug（AssertionError）→ run_safe 70。"""

    @staticmethod
    def _patch_chat_first(monkeypatch, exc_factory):
        """patch Driver.chat：第一次调用（= 校准 short-P 桶）抛指定异常，其余原逻辑。"""
        original = Driver.chat
        calls = {"n": 0}

        def chat(self, messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise exc_factory()
            return original(self, messages, **kwargs)

        monkeypatch.setattr(Driver, "chat", chat)
        return calls

    @staticmethod
    def _openai_req():
        return httpx.Request("POST", "http://calib")

    def _assert_preflight_infra(self, tmp_path, rc):
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        meta = doc["meta"]
        assert meta["phase"] == "preflight"
        assert meta["preflight_reason"] == "validation_incomplete"
        assert meta["parity_ok"] is None
        assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"

    def test_openai_internal_error_preflight_infra(self, tmp_path, monkeypatch):
        """校准 Driver.chat 抛真实 openai.InternalServerError（500）→ PREFLIGHT_INFRA
        合法落盘（rc=0、preflight、validator 通过）。"""
        req = self._openai_req()

        def factory():
            return openai.InternalServerError(
                "Internal Server Error",
                response=httpx.Response(500, request=req),
                body=None,
            )

        self._patch_chat_first(monkeypatch, factory)
        r = make_runner(tmp_path)
        self._assert_preflight_infra(tmp_path, r.run())

    def test_openai_connection_error_preflight_infra(self, tmp_path, monkeypatch):
        """校准 Driver.chat 抛真实 openai.APIConnectionError（连接失败）→ 同上。"""
        req = self._openai_req()
        self._patch_chat_first(monkeypatch, lambda: openai.APIConnectionError(request=req))
        r = make_runner(tmp_path)
        self._assert_preflight_infra(tmp_path, r.run())

    def test_internal_bug_goes_to_exit70(self, tmp_path, monkeypatch, capsys):
        """校准 Driver.chat 抛内部 bug（AssertionError）→ 不被吞 → run_safe 归
        INTERNAL_RUNNER_ERROR + EXIT_SOFTWARE(70)。"""
        self._patch_chat_first(monkeypatch, lambda: AssertionError("internal invariant broken"))
        r = make_runner(tmp_path)
        rc = r.run_safe()
        assert rc == sch.EXIT_SOFTWARE
        assert "INTERNAL_RUNNER_ERROR" in capsys.readouterr().err
        assert not (tmp_path / "out.json").exists(), "内部 bug 不落盘结果"


# ---- 任务 3：warmup 决策请求异常（与工具轮一致） ----


class TestWarmupDecisionError:
    """v52（任务 3）：warmup 决策请求异常——server 健康 → 记可诊断 note 不静默；
    server 已退出 → ServerCrash（group ERROR/FORMAL_INCOMPLETE）。"""

    @staticmethod
    def _patch_chat_warmup_decision(monkeypatch, exc_factory):
        """patch Driver.chat：decision validation 完成（armed）后的第一次调用
        = 首个 unit 的第一个 warmup 决策请求，抛指定异常；其余原逻辑。
        v52 review 修复：改用 patch_after_validation armed flag（替代脆弱
        的计数 27——不依赖校准 3 桶×2 + 验证 2×10 的 chat 次数）。"""
        original = Driver.chat
        armed = {"on": False}
        fired = {"y": False}
        patch_after_validation(monkeypatch, lambda: armed.__setitem__("on", True))

        def chat(self, messages, **kwargs):
            if armed["on"] and not fired["y"]:
                fired["y"] = True
                raise exc_factory()
            return original(self, messages, **kwargs)

        monkeypatch.setattr(Driver, "chat", chat)
        return armed

    @staticmethod
    def _conn_error():
        return openai.APIConnectionError(request=httpx.Request("POST", "http://warmup"))

    def test_healthy_records_note_and_continues(self, tmp_path, monkeypatch):
        """warmup 决策请求异常（server 健康）→ 记可诊断 note；矩阵完整完成。"""
        self._patch_chat_warmup_decision(monkeypatch, self._conn_error)
        r = make_runner(tmp_path)
        rc = r.run()
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        assert doc["meta"]["matrix_complete"] is True
        assert any("warmup 决策请求异常" in n for n in doc["notes"]), doc["notes"]

    def test_dead_raises_server_crash(self, tmp_path, monkeypatch):
        """warmup 决策请求异常 + server 已退出 → ServerCrash → group ERROR +
        FORMAL_INCOMPLETE（与工具轮一致）。"""
        self._patch_chat_warmup_decision(monkeypatch, self._conn_error)
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


# ---- 任务 5：校准后 clean_all_slots 异常 / (0,0) ----


class TestCalibrationCleanZeroZero:
    """v52（任务 5）：校准后 clean_all_slots 返回 (0,0) 且 server 健康 → 记可诊断
    note（区别于 warmup 的 (0,0) note）；死 server → ServerCrash → PREFLIGHT_INFRA。"""

    def test_healthy_records_note_and_continues(self, tmp_path, monkeypatch):
        """校准后 clean_all_slots 返回 (0,0) + server 健康 → note；矩阵完整完成。"""
        monkeypatch.setattr(KVProbe, "clean_all_slots", lambda self: (0, 0))
        r = make_runner(tmp_path)
        rc = r.run()
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        assert doc["meta"]["matrix_complete"] is True
        assert any("校准后 clean_all_slots 返回 (0,0)" in n for n in doc["notes"]), doc["notes"]

    def test_clean_exception_healthy_records_note(self, tmp_path, monkeypatch):
        """校准后 clean_all_slots 抛异常 + server 健康 → 记可诊断 note（防御分支）；
        仅第一次调用（= 校准后清理）抛，后续（warmup/formal erase）正常返回
        真实 slot 数 (2,2)，避免污染 formal rep 的严格 erase 检查。"""
        calls = {"n": 0}

        def boom(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("calib clean_all_slots failed")
            return (2, 2)

        monkeypatch.setattr(KVProbe, "clean_all_slots", boom)
        r = make_runner(tmp_path)
        rc = r.run()
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        assert doc["meta"]["matrix_complete"] is True
        assert any("校准后 clean_all_slots 异常" in n for n in doc["notes"]), doc["notes"]

    def test_partial_erase_healthy_records_note(self, tmp_path, monkeypatch):
        """v53（Medium 1）：校准后 clean_all_slots 返回 partial（attempt>0 且
        ok<attempt，如 (1,0)）+ server 健康 → 记可诊断 note（与 warmup v50
        任务 5 语义一致），不静默吞；矩阵完整完成。
        仅第一次调用（= 校准后清理）返回 (1,0)，后续（warmup/formal erase）
        正常返回真实 slot 数 (2,2)，避免污染 formal rep 的严格 erase 检查。"""
        calls = {"n": 0}

        def partial_first(self):
            calls["n"] += 1
            if calls["n"] == 1:
                return (1, 0)
            return (2, 2)

        monkeypatch.setattr(KVProbe, "clean_all_slots", partial_first)
        r = make_runner(tmp_path)
        rc = r.run()
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        assert doc["meta"]["matrix_complete"] is True
        assert any("校准后 clean_all_slots 部分失败 0/1" in n for n in doc["notes"]), doc["notes"]

    def test_zero_zero_dead_preflight_infra(self, tmp_path, monkeypatch):
        """校准后 clean_all_slots 返回 (0,0) + server 已退出 → ServerCrash →
        上层转 PREFLIGHT_INFRA 合法落盘（rc=0、preflight、validator 通过）。
        注（v52 review）：poll_dead 从校准 server 启动即生效，但 FakeAdapter.
        wait_health 不受其影响（直接 True）→ 校准正常启动，仅在清理段 poll
        判死，精确模拟"校准后清理时 server 已退出"。"""
        monkeypatch.setattr(KVProbe, "clean_all_slots", lambda self: (0, 0))
        # 校准 server 在校准后清理时已死（首次启动即崩溃标记）
        FakeAdapter.poll_dead = True
        try:
            r = make_runner(tmp_path)
            rc = r.run()
        finally:
            FakeAdapter.poll_dead = None
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        meta = doc["meta"]
        assert meta["phase"] == "preflight"
        assert meta["preflight_reason"] == "validation_incomplete"
        assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"


# ---- 任务 4：warmup 工具轮异常 ----


class TestWarmupToolRoundError:
    @staticmethod
    def _patch_chat_armed_tool(monkeypatch):
        """warmup 期间工具轮首个请求抛真实 openai SDK 状态异常
        （InternalServerError），触发后 disarm。

        v53（Medium 2）：真实 openai 异常路径替代旧 urllib.HTTPError mock；
        armed flag 触发一次后关闭（不依赖"前 N 次"计数时序）。
        """
        original = Driver.chat
        state = {"armed": True}

        def chat(self, messages, **kwargs):
            text = "\n".join(str(m.get("content", "")) for m in messages)
            if "m0_fanout_probe" in text and state["armed"]:
                state["armed"] = False  # 触发后 disarm
                req = httpx.Request("POST", "http://tool")
                resp = httpx.Response(500, request=req)
                raise openai.InternalServerError(
                    "Error code: 500 - Internal Server Error",
                    response=resp, body=None)
            return original(self, messages, **kwargs)

        monkeypatch.setattr(Driver, "chat", chat)
        return state

    def test_healthy_records_note_and_continues(self, tmp_path, monkeypatch):
        """warmup 工具轮请求异常（真实 openai InternalServerError，server 健康）
        → 记可诊断 note 不静默；warmup 后 formal rep 正常 → 矩阵完整完成。"""
        self._patch_chat_armed_tool(monkeypatch)
        r = make_runner(tmp_path)
        rc = r.run()
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        assert doc["meta"]["matrix_complete"] is True
        assert any("工具轮" in n for n in doc["notes"]), doc["notes"]

    def test_dead_raises_server_crash(self, tmp_path, monkeypatch):
        """warmup 工具轮异常（真实 openai InternalServerError）+ server 已退出 →
        ServerCrash → group ERROR + FORMAL_INCOMPLETE（与决策异常一致）。"""
        self._patch_chat_armed_tool(monkeypatch)
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


# ---- 任务 6：warmup 内部 bug 上抛（v53 Medium 3） ----


class TestWarmupInternalBug:
    """v53（Medium 3）：run_formal 的 warmup 异常只吞明确基础设施/请求异常
    （_INFRA_EXCEPTIONS = OSError/openai.OpenAIError 族）；内部 bug
    （AssertionError/KeyError/TypeError 等）必须上抛 run_safe → 70。

    触发点用 decision validation 完成后的首个 warmup 决策请求（patch_after_
    validation armed 模式，与 TestWarmupDecisionError 一致）——决策路径
    _run_unit 在 warmup 分支记 note 后 bare-raise，交由 run_formal 判定。
    """

    @staticmethod
    def _patch_chat_warmup_decision(monkeypatch, exc_factory):
        original = Driver.chat
        armed = {"on": False}
        fired = {"y": False}
        patch_after_validation(monkeypatch, lambda: armed.__setitem__("on", True))

        def chat(self, messages, **kwargs):
            if armed["on"] and not fired["y"]:
                fired["y"] = True
                raise exc_factory()
            return original(self, messages, **kwargs)

        monkeypatch.setattr(Driver, "chat", chat)
        return armed

    def test_assertion_error_goes_to_exit70(self, tmp_path, monkeypatch, capsys):
        """正例：warmup 决策请求抛内部 bug（AssertionError）→ 不被 run_formal
        的 warmup 异常处理吞掉（不属于 _INFRA_EXCEPTIONS）→ 上抛 run_safe 归
        INTERNAL_RUNNER_ERROR + EXIT_SOFTWARE(70)，不落盘结果。"""
        self._patch_chat_warmup_decision(
            monkeypatch, lambda: AssertionError("internal invariant broken in warmup"))
        r = make_runner(tmp_path)
        rc = r.run_safe()
        assert rc == sch.EXIT_SOFTWARE
        assert "INTERNAL_RUNNER_ERROR" in capsys.readouterr().err
        assert not (tmp_path / "out.json").exists(), "内部 bug 不落盘结果"

    def test_openai_infra_error_continues_not_70(self, tmp_path, monkeypatch):
        """反例：warmup 决策请求抛真实 openai SDK 基础设施异常（InternalServerError，
        属 _INFRA_EXCEPTIONS）+ server 健康 → 不被当成内部 bug 上抛 70，而是
        记可诊断 note 继续、矩阵完整完成（rc=0）。"""
        def _500():
            req = httpx.Request("POST", "http://warmup")
            resp = httpx.Response(500, request=req)
            return openai.InternalServerError(
                "Error code: 500 - Internal Server Error", response=resp, body=None)

        self._patch_chat_warmup_decision(monkeypatch, _500)
        r = make_runner(tmp_path)
        rc = r.run_safe()
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        assert doc["meta"]["matrix_complete"] is True
        assert any("warmup 决策请求异常" in n for n in doc["notes"]), doc["notes"]


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
        # v52（任务 2）：真实 openai SDK 异常从 Driver.chat 校准路径（patch
        # run_calibration 抛 HTTPError 的旧方式已删除）
        original = Driver.chat
        calls = {"n": 0}

        def chat(self, messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise openai.APIConnectionError(
                    request=httpx.Request("POST", "http://calib"))
            return original(self, messages, **kwargs)

        monkeypatch.setattr(Driver, "chat", chat)
        r = make_runner(tmp_path)
        r._add_note("runner-keep-me")
        rc = r.run()
        assert rc == 0, rc
        doc = load_doc(tmp_path)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        assert "runner-keep-me" in doc["notes"]
