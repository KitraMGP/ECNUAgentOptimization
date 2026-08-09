"""M0 v50 真实失败归因测试（4B 短校准前收口）。

集中覆盖：
- 任务 1：after_erase 观测缺失必须走真实可达分支——/metrics/kv 端点失败
  （真实 fetch 返回 None，非 mock 抛异常）→ EraseFailure('after_erase_missing')
  → FORMAL_INCOMPLETE；补 _fetch 失败/返回 None 的生产路径测试
- 任务 2：校准阶段清理异常 + server 已退出 → ServerCrash → PREFLIGHT_INFRA
  合法落盘（exit 0），不得 run_safe exit70
- 任务 3：formal 工具轮请求在 server 健康但 HTTP 异常时标 rep ERROR 并继续
  严格 erase/after_erase（不裸逃逸 exit70）；server 已退出才 ServerCrash
- 任务 5：warmup best-effort erase 异常（partial erase，真实可达）记录可诊断
  note，不静默吞；used_cells 先非 0 再证明 erase 归零
"""
from __future__ import annotations

import json
import urllib.error

import httpx
import openai
import pytest

from framework.driver import Driver
from framework.kv_probe import KVProbe
from runner import m0_fanout_runner as m0r
from runner import m0_schema as sch
from tests import mock_server as mserver


class FakeAdapter:
    """与 test_m0_runner_fixes 同构的 mock adapter（独立副本，避免跨文件耦合）。"""

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
            return (True, "ok")  # v61 契约：tuple/bool

    def read_log(self):
        return ""


def make_runner(tmp_path, adapter, **over):
    args = dict(
        server_bin="fake-bin", model="mock.gguf", ctx_size=4096,
        port_base=18080, decision_n_predict=16, branch_n_predict=64,
        tool_rounds=1, out_path=str(tmp_path / "out.json"),
        tmp_dir=str(tmp_path / "tmp"), ngl=99, adapter_cls=FakeAdapter,
    )
    args.update(over)
    r = m0r.M0FanoutRunner(**args)
    if adapter is not None:
        r._adapter = adapter
        r._driver = Driver(base_url=f"http://127.0.0.1:{adapter.port}",
                           model="bench", max_retry=1, sdk_max_retries=0)
        r._kv = KVProbe(f"http://127.0.0.1:{adapter.port}", enabled=True)
    r.calibrated_lengths = {("short", 2): {"prefix": 100, "branch": 100}}
    return r


UID = sch.unit_id_of("off", "q8_0", "q8_0", 2, "short")
GID = sch.group_id_of("off", "q8_0", "q8_0", 2)


class TestAfterEraseMissing:
    def test_after_erase_missing_production_path(self, tmp_path):
        """任务 1：/metrics/kv 端点失败（真实 fetch 失败 → snapshot 返回 None，
        非 mock 抛异常）→ 显式 EraseFailure('after_erase_missing')。
        决策/分支/erase 均成功、仅观测缺失——不伪造归零观测。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            prev = mserver._KV["metrics_fail"]
            mserver._KV["metrics_fail"] = True  # 生产路径：_fetch 返回 None
            try:
                # 先发真实 completion → mock used_cells=384（erase 有实际内容可清）
                r._driver.chat([{"role": "user", "content": "hi"}], max_tokens=4)
                assert mserver._KV["used_cells"] == 384
                with pytest.raises(m0r.EraseFailure) as ei:
                    r._run_unit(UID, 2, "short", "q8_0", "q8_0", GID, 0)
                assert ei.value.error_type == "after_erase_missing"
                assert "after_erase_missing" in str(ei.value)
                # fetch 失败已记入 last_error（生产可诊断）
                assert "500" in (r._kv.last_error or "")
            finally:
                mserver._KV["metrics_fail"] = prev
        finally:
            adapter.stop()

    def test_fetch_failure_sets_last_error_and_none(self, tmp_path):
        """_fetch 失败/返回 None 的生产路径（端点 500）→ kv_state/snapshot None。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            kv = KVProbe(f"http://127.0.0.1:{adapter.port}", enabled=True)
            assert kv.kv_state() is not None
            prev = mserver._KV["metrics_fail"]
            mserver._KV["metrics_fail"] = True
            try:
                assert kv.kv_state() is None
                assert "500" in (kv.last_error or "")
                assert kv.snapshot("t") is None  # 记录失败样本
                assert "500" in (kv.last_error or "")
            finally:
                mserver._KV["metrics_fail"] = prev
        finally:
            adapter.stop()


class TestCalibrationCleanupCrashPreflight:
    def test_calibration_cleanup_crash_preflight(self, tmp_path, monkeypatch):
        """任务 2：校准阶段 clean_all_slots 异常 + server 已退出 → ServerCrash →
        run() 捕获 → PREFLIGHT_INFRA 合法 preflight 落盘（exit 0），
        不得 run_safe exit70 / traceback。"""
        FakeAdapter.poll_dead = True

        def _boom(self):
            raise OSError("clean boom")

        monkeypatch.setattr(KVProbe, "clean_all_slots", _boom)
        r = make_runner(tmp_path, None)  # 校准用 fake_adapter_cls 全链路
        try:
            rc = r.run()
            assert rc == 0, rc
            with open(r.out_path, encoding="utf-8") as f:
                doc = json.load(f)
            assert doc["meta"]["phase"] == "preflight"
            assert doc["meta"]["preflight_status"] == "FAILED"
            assert doc["meta"]["preflight_reason"] == "validation_incomplete"
            # v34 语义：清理失败发生在 6 桶全部完成且无 mismatch 之后 →
            # parity_ok=true + token_count_method（非 null）
            assert doc["meta"]["parity_ok"] is True
            assert doc["meta"]["token_count_method"] == "apply-template+tokenize"
            assert len(doc["meta"]["parity_progress"]["completed"]) == 6
            assert doc["meta"]["matrix_complete"] is False
            assert doc["meta"]["executed_units"] == 0
            assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"
        finally:
            FakeAdapter.poll_dead = None


class _ToolFailDriver:
    """决策/分支请求正常；工具轮请求抛真实 openai SDK 状态异常
    （InternalServerError "Error code: 500"）——server 进程健康（mock 不退出）。

    v53（Medium 2）：改用真实 openai.APIStatusError/InternalServerError 路径
    （替代旧 urllib.HTTPError mock）。时序：工具轮位置由单元协议决定
    （决策 1 + 分支 fanout 次 → 工具轮首个）。
    v66：transient wrapper 会重试 500——工具轮首条起**连续 3 次** 500
    （armed=3 计数）使 retry 耗尽才落 rep ERROR；反例（server 已退出）由
    wrapper 首次异常 poll 立即 ServerCrash（不重试，仍 1 次即触发）。
    """

    def __init__(self, inner):
        self.inner = inner
        self.armed = 3
        self.n = 0

    def chat(self, msgs, **kw):
        self.n += 1
        if self.armed > 0 and 1 + 2 + 1 <= self.n <= 1 + 2 + 3:
            # 工具轮首条起连续 3 次 500（wrapper retry 耗尽）
            self.armed -= 1
            req = httpx.Request("POST", "http://tool")
            resp = httpx.Response(500, request=req)
            raise openai.InternalServerError(
                "Error code: 500 - Internal Server Error", response=resp, body=None)
        return self.inner.chat(msgs, **kw)


class TestToolRoundHttpError:
    def test_tool_round_500_healthy_marks_rep_error(self, tmp_path):
        """任务 3（正）：server 健康但工具轮请求 HTTP 500 → 标当前 rep ERROR、
        继续统一 erase/after_erase（不裸逃逸 exit70）。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            r._driver = _ToolFailDriver(r._driver)
            rep = r._run_unit(UID, 2, "short", "q8_0", "q8_0", GID, 0)
            assert rep["status"] == "ERROR"  # 不抛异常
            kv = rep["metrics"]["kv"]
            assert kv["last"]["used_cells"] == 0       # erase 仍真实执行
            assert kv["last"]["active_sequences"] == 0
            assert kv["peak"]["used_cells"] is not None
        finally:
            adapter.stop()

    def test_tool_round_500_dead_raises_crash(self, tmp_path):
        """任务 3（反）：工具轮请求失败 + server 已退出 → ServerCrash
        （group ERROR，不是 rep ERROR）。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            r._driver = _ToolFailDriver(r._driver)
            FakeAdapter.poll_dead = True
            with pytest.raises(m0r.ServerCrash):
                r._run_unit(UID, 2, "short", "q8_0", "q8_0", GID, 0)
        finally:
            FakeAdapter.poll_dead = None
            adapter.stop()


class TestWarmupEraseNote:
    def test_warmup_erase_zeroes_and_partial_notes(self, tmp_path):
        """任务 5：warmup 请求后 used_cells 先非 0 → best-effort erase 归零；
        erase 部分失败（真实可达：erase_disabled → 501）→ 记可诊断 note。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            # 正常路径：warmup 请求使 used_cells=384（非 0）→ erase 后归零
            out = r._run_unit(UID, 2, "short", "q8_0", "q8_0", GID, None)
            assert out == {}  # warmup 不落 replicates
            assert mserver._KV["used_cells"] == 0

            # 异常路径：erase 部分失败（501）→ note 记录，不静默吞
            prev_disable = mserver._KV["erase_disabled"]
            mserver._KV["erase_disabled"] = True
            try:
                r._driver.chat([{"role": "user", "content": "hi"}], max_tokens=4)
                assert mserver._KV["used_cells"] == 384  # erase 前非 0
                r.notes.clear()
                r._run_unit(UID, 2, "short", "q8_0", "q8_0", GID, None)
                assert mserver._KV["used_cells"] == 384  # partial：未清（留证）
                assert any("warmup best-effort erase 部分失败" in n
                           for n in r.notes), r.notes
            finally:
                mserver._KV["erase_disabled"] = prev_disable
        finally:
            adapter.stop()
