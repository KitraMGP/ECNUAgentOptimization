"""M0 v66 transient retry wrapper 测试（设计 §3.6/§3.8 v12 定稿的正式实现）。

集中覆盖：
- 任务 1/3（wrapper 单元）：
  - 首 attempt 失败（500）后成功 → 返回正常 row（重试吸收 transient）；
  - 连续 3 次 500（retry 耗尽）→ 抛最后一次异常，classify_error=http_5xx；
  - HTTP 400 一次 → 不重试（1 次尝试即抛，classify_error=http_4xx）；
  - HTTP 500 三次 → 恰好 3 次尝试后抛（http_5xx）；
  - 重试期间 server 退出（首次异常健康、第 2 次 poll 已退出）→ 立即
    ServerCrash（不再重试——ThreadPool branch barrier 不因 retry sleep 死锁）；
- 任务 3（所有阶段覆盖）：
  - calibration parity chat（_calibrate_template）、decision validation
    （_run_decision_session）、formal decision / ThreadPool branch / tool round
    （_run_unit 内三处调用点）在 transient 故障下经 wrapper 重试成功；
  - branch 阶段重试期间 barrier 正常放行（无死锁、无分支 error）；
- 任务 4（rep 级 GPU/RSS 峰值）：
  - 成功 rep：peak_rss_mb/peak_gpu_mb 取 decision/branch/tool 成功 row 的
    max（不再 0.0 占位）；
  - ERROR rep（工具轮失败）保留已有成功样本（decision+branch 的 max）。
"""
from __future__ import annotations

import threading
import urllib.error

import pytest

import openai

from framework import sampler
from framework.driver import Driver
from framework.kv_probe import KVProbe
from runner import m0_fanout_runner as m0r
from runner import m0_schema as sch
from tests import mock_server as mserver


class FakeAdapter:
    """与 test_m0_runner_fixes 同构的 mock adapter（独立副本）。"""

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
        if self._server is None:
            return 1
        return None

    def wait_health(self, timeout=120.0):
        return self._server is not None

    def stop(self):
        if self._server is not None:
            self._server.__exit__(None, None, None)
            self._server = None
            return (True, "ok")

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


# ---- 任务 1/3：wrapper 单元（_chat 直接路径） ----

class TestTransientClass:
    """_transient_class 分类纯单元（不依赖 server）——覆盖 openai SDK 全类型
    与 should-fix 回归（未知内部异常不重试）。"""

    @staticmethod
    def _status(status: int):
        req = __import__("httpx").Request("POST", "http://t")
        return __import__("httpx").Response(status, request=req)

    def test_timeout_classified_retryable(self):
        assert m0r.M0FanoutRunner._transient_class(
            openai.APITimeoutError(request=None)) == "timeout"

    def test_connection_classified_retryable(self):
        assert m0r.M0FanoutRunner._transient_class(
            openai.APIConnectionError(request=None)) == "connection_error"

    def test_5xx_retryable(self):
        assert m0r.M0FanoutRunner._transient_class(
            openai.InternalServerError("e", response=self._status(500), body=None)
        ) == "http_5xx"

    def test_4xx_not_retryable(self):
        assert m0r.M0FanoutRunner._transient_class(
            openai.BadRequestError("e", response=self._status(400), body=None)
        ) is None

    def test_malformed_not_retryable(self):
        # APIResponseValidationError 构造需 body/response；用子类化或直接实例
        err = openai.APIResponseValidationError(
            message="bad", body=b"{}", response=self._status(200))
        assert m0r.M0FanoutRunner._transient_class(err) is None

    def test_internal_bug_not_retryable(self):
        for exc in (AssertionError("x"), KeyError("x"), TypeError("x"),
                    ValueError("x")):
            assert m0r.M0FanoutRunner._transient_class(exc) is None

    def test_unknown_internal_error_not_retryable(self):
        """should-fix 回归：未知内部异常（IndexError/RuntimeError，含
        driver.py 空 choices 路径）不得文本兜底为 connection_error 重试。"""
        assert m0r.M0FanoutRunner._transient_class(
            IndexError("choices index out of range")) is None
        assert m0r.M0FanoutRunner._transient_class(
            RuntimeError("boom")) is None

    def test_urllib_5xx_retryable_4xx_not(self):
        assert m0r.M0FanoutRunner._transient_class(
            urllib.error.HTTPError(
                "http://t", 503, "svc", None, None)) == "http_5xx"
        assert m0r.M0FanoutRunner._transient_class(
            urllib.error.HTTPError(
                "http://t", 400, "bad", None, None)) is None

    def test_text_connection_patterns_retryable(self):
        """v68：明确 connection 文本模式（非 openai 类型，走 _text_transient）
        → connection_error 可重试；URLError refused 同。"""
        for msg in ("Connection refused", "Connection reset by peer",
                    "Connection error.", "Connection lost.",
                    "Failed to connect to host", "Unable to connect",
                    "Cannot connect to server"):
            assert m0r.M0FanoutRunner._transient_class(
                Exception(msg)) == "connection_error", msg
        assert m0r.M0FanoutRunner._transient_class(
            urllib.error.URLError("Connection refused")) == "connection_error"

    def test_text_disconnect_reconnect_not_misclassified(self):
        """v68 误匹配反例：裸 connect/disconnect/reconnect/connectivity 等
        内部消息不得被文本 fallback 归为 connection_error（收紧，不再重试）。"""
        for msg in ("Disconnected from server", "disconnected",
                    "Reconnecting in 5s", "reconnect attempt",
                    "connectivity issue", "no route to connect",
                    "index out of range"):
            assert m0r.M0FanoutRunner._transient_class(
                Exception(msg)) is None, msg

    def test_text_timeout_retryable(self):
        """v68：文本 timeout 模式（非 openai 类型）→ timeout 可重试。"""
        assert m0r.M0FanoutRunner._transient_class(
            Exception("Request timed out")) == "timeout"
        assert m0r.M0FanoutRunner._transient_class(
            TimeoutError("socket timed out")) == "timeout"

    def test_classify_error_shared_text_helper(self):
        """v68：classify_error 与 _transient_class 共用 _text_transient——
        同一文本在两者下分类一致（connection 正例；disconnect 反例归类默认值
        而非 connection_error——classify_error 无 None 语义时落默认 connection_error，
        但 _transient_class 必须 None 不重试）。"""
        # 正例：明确 connection 文本两者一致
        e = urllib.error.URLError("Connection refused")
        assert m0r._text_transient(e) == "connection_error"
        assert m0r.classify_error(e) == "connection_error"
        assert m0r.M0FanoutRunner._transient_class(e) == "connection_error"
        # 反例：disconnect 文本 _transient_class 不重试
        d = Exception("Disconnected from server")
        assert m0r._text_transient(d) is None
        assert m0r.M0FanoutRunner._transient_class(d) is None


class TestChatWrapperUnit:
    def test_first_attempt_fails_then_success(self, tmp_path):
        """首 attempt 失败（500 一次）→ wrapper 重试成功 → 返回正常 row。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            mserver.set_fail(1)  # 首个 chat 500，之后复位
            row = r._chat([{"role": "user", "content": "hi"}], max_tokens=4)
            assert row["text"].startswith("模拟回复")
            assert mserver._FAIL["remaining"] == 0  # 注入恰好消费 1 次
        finally:
            adapter.stop()

    def test_consecutive_3_fails_raises_http5xx(self, tmp_path):
        """连续 3 次 500（retry 耗尽，总尝试 3）→ 抛最后一次异常（http_5xx）。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            mserver.set_fail(3)  # 3 次尝试全 500
            with pytest.raises(openai.InternalServerError) as ei:
                r._chat([{"role": "user", "content": "hi"}], max_tokens=4)
            assert m0r.classify_error(ei.value) == "http_5xx"
            assert mserver._FAIL["remaining"] == 0  # 恰好 3 次尝试
        finally:
            adapter.stop()

    def test_http_400_no_retry(self, tmp_path):
        """HTTP 400 一次 → 不重试（若重试会成功返回 row；此处 1 次即抛 http_4xx）。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            mserver.set_fail(1, status=400)
            with pytest.raises(openai.BadRequestError) as ei:
                r._chat([{"role": "user", "content": "hi"}], max_tokens=4)
            assert m0r.classify_error(ei.value) == "http_4xx"
        finally:
            adapter.stop()

    def test_http_500_three_times(self, tmp_path):
        """HTTP 500 三次 → 恰好 3 次尝试后抛（http_5xx），第 4 次不触发。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            mserver.set_fail(3, status=500)
            with pytest.raises(openai.InternalServerError) as ei:
                r._chat([{"role": "user", "content": "hi"}], max_tokens=4)
            assert m0r.classify_error(ei.value) == "http_5xx"
            # 注入已耗尽（remaining=0）——若 wrapper 错误地第 4 次尝试会成功返回
            assert mserver._FAIL["remaining"] == 0
        finally:
            adapter.stop()

    def test_server_exit_during_retry(self, tmp_path):
        """重试期间 server 退出（首次异常健康、第 2 次 poll 已退出）→
        立即 ServerCrash，不再继续重试（barrier 不因 retry sleep 死锁）。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            polls = {"n": 0}
            orig_poll = adapter.poll

            def flaky_poll():
                polls["n"] += 1
                if polls["n"] >= 2:
                    return 1  # 第 2 次 poll 起已退出（重试期间进程退出）
                return orig_poll()

            adapter.poll = flaky_poll  # 实例属性遮蔽（仅本测试）
            mserver.set_fail(3)
            with pytest.raises(m0r.ServerCrash) as ei:
                r._chat([{"role": "user", "content": "hi"}], max_tokens=4)
            assert polls["n"] == 2  # 第 2 次异常后 poll 发现退出 → 立即抛（不再重试）
            assert "server 已退出" in str(ei.value)
        finally:
            adapter.stop()


# ---- 任务 3：所有阶段覆盖（调用点确实经由 wrapper） ----

class TestStageCoverage:
    def test_calibration_parity_chat_uses_wrapper(self, tmp_path):
        """calibration parity chat（_calibrate_template 内 _chat）：transient
        500 → 重试成功 → 返回 (tokenize_len, chat_prompt_tokens)。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            mserver.set_fail(1)  # parity chat 首次 500（apply-template/tokenize 不受注入）
            n, chat_tok = r._calibrate_template(
                [{"role": "user", "content": "校准"}])
            assert n == 100 and chat_tok == 100  # mock 固定值（parity 偏差 0）
            assert mserver._FAIL["remaining"] == 0
        finally:
            adapter.stop()

    def test_decision_validation_uses_wrapper(self, tmp_path):
        """decision validation（_run_decision_session 内 _chat）：首条验证请求
        transient 500 → 重试成功 → 10 条全 OK（session error_count==0）。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            mserver.set_fail(1)
            recs, _ = r._run_decision_session(adapter.port)
            assert len(recs) == m0r.VALIDATION_REQUESTS
            assert all(rec["error"] is None for rec in recs)  # 无 ERROR 记录
        finally:
            adapter.stop()

    def test_formal_decision_uses_wrapper(self, tmp_path):
        """formal 决策 chat：transient 500 → 重试成功 → rep 非 ERROR。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            mserver.set_fail(1)  # 首个 chat = 决策请求
            rep = r._run_unit(UID, 2, "short", "q8_0", "q8_0", GID, 0)
            assert rep["status"] in ("OK", "INVALID_DECISION")  # 无 ERROR
        finally:
            adapter.stop()

    def test_branch_threadpool_uses_wrapper_no_deadlock(self, tmp_path, monkeypatch):
        """ThreadPool branch：分支阶段恰好一次 transient 500 → wrapper 重试成功 →
        所有分支无 error、barrier 正常放行（重试 sleep 不阻塞其他线程）。

        v67：分支线程并发，计数/注入用 Lock 保护——只有**一个**分支线程触发
        set_fail(1)（inject.done 保证恰好一次）；断言注入被消费
        （_FAIL.remaining==0 = 某分支 chat 收到 500 后 wrapper 重试成功，
        即 retry 确实发生，而非注入未命中）。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            calls = {"n": 0}
            inject = {"done": False}
            lock = threading.Lock()
            orig_chat = r._chat

            def counting_chat(msgs, **kw):
                with lock:
                    calls["n"] += 1
                    # 决策（1 次）成功后，分支阶段（fanout=2 并发）首个进入的
                    # 线程注入恰好一次 500；其余线程（inject.done）不再注入
                    if not inject["done"] and 2 <= calls["n"] <= 3:
                        inject["done"] = True
                        mserver.set_fail(1)
                return orig_chat(msgs, **kw)

            monkeypatch.setattr(r, "_chat", counting_chat)
            rep = r._run_unit(UID, 2, "short", "q8_0", "q8_0", GID, 0)
            assert rep["status"] in ("OK", "INVALID_DECISION")
            assert not any(b.get("error") for b in rep["metrics"]["branches"])
            # barrier 正常放行：分支观测齐（fanout=2）
            assert len(rep["metrics"]["branches"]) == 2
            # v67：注入确实发生且被 wrapper 重试吸收（retry 发生）
            assert inject["done"] is True
            assert mserver._FAIL["remaining"] == 0
        finally:
            adapter.stop()

    def test_tool_round_uses_wrapper(self, tmp_path, monkeypatch):
        """工具轮 chat：工具轮第一条 transient 500 → wrapper 重试成功 → rep OK。

        v67：补 retry 发生断言——注入被消费（_FAIL.remaining==0）。"""
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            calls = {"n": 0}
            orig_chat = r._chat

            def counting_chat(msgs, **kw):
                calls["n"] += 1
                if calls["n"] == 4:  # 工具轮第一条（决策 1 + 分支 2 后）
                    mserver.set_fail(1)
                return orig_chat(msgs, **kw)

            monkeypatch.setattr(r, "_chat", counting_chat)
            rep = r._run_unit(UID, 2, "short", "q8_0", "q8_0", GID, 0)
            assert rep["status"] in ("OK", "INVALID_DECISION")  # 工具轮恢复，无 ERROR
            assert mserver._FAIL["remaining"] == 0  # v67：注入被消费 = retry 发生
        finally:
            adapter.stop()


# ---- 任务 4：rep 级 GPU/RSS 峰值（成功 row 取 max；ERROR rep 保留样本） ----

class TestRepPeakMem:
    def test_peak_mem_from_success_rows(self, tmp_path, monkeypatch):
        """成功 rep：peak_rss_mb/peak_gpu_mb = 成功 row 样本（不再 0.0 占位）。"""
        monkeypatch.setattr("framework.sampler.find_server_rss_mb", lambda pid: 123.4)
        monkeypatch.setattr("framework.sampler.find_server_gpu_mb", lambda pid: 56.7)
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            rep = r._run_unit(UID, 2, "short", "q8_0", "q8_0", GID, 0)
            assert rep["status"] in ("OK", "INVALID_DECISION")
            assert rep["metrics"]["peak_rss_mb"] == 123.4
            assert rep["metrics"]["peak_gpu_mb"] == 56.7
        finally:
            adapter.stop()

    def test_peak_mem_takes_max(self, tmp_path, monkeypatch):
        """peak 取 decision/branch/tool 多成功 row 的 max（非末次/首样本）。"""
        # chat 顺序（fanout=2, tool_rounds=1）：决策 1 + 分支 2 + 工具轮 2 = 5 次
        vals = iter([100.0, 300.0, 200.0, 250.0, 150.0])

        def fake_rss(pid):
            try:
                return next(vals)
            except StopIteration:
                return 100.0  # 兜底（不应触发）

        monkeypatch.setattr("framework.sampler.find_server_rss_mb", fake_rss)
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            rep = r._run_unit(UID, 2, "short", "q8_0", "q8_0", GID, 0)
            assert rep["metrics"]["peak_rss_mb"] == 300.0  # max(100,300,200,250,150)
        finally:
            adapter.stop()

    def test_error_rep_keeps_existing_samples(self, tmp_path, monkeypatch):
        """ERROR rep（工具轮 transient 耗尽）保留 decision+branch 成功样本。"""
        monkeypatch.setattr("framework.sampler.find_server_rss_mb", lambda pid: 99.0)
        monkeypatch.setattr("framework.sampler.find_server_gpu_mb", lambda pid: 44.0)
        adapter = FakeAdapter([], "", 0)
        adapter.start()
        try:
            r = make_runner(tmp_path, adapter)
            calls = {"n": 0}
            orig_chat = r._chat

            def counting_chat(msgs, **kw):
                calls["n"] += 1
                if calls["n"] == 4:  # 工具轮第一条：连续 3 次 500 → retry 耗尽
                    mserver.set_fail(3)
                return orig_chat(msgs, **kw)

            monkeypatch.setattr(r, "_chat", counting_chat)
            rep = r._run_unit(UID, 2, "short", "q8_0", "q8_0", GID, 0)
            assert rep["status"] == "ERROR"
            assert rep["error_type"] == "http_5xx"
            # 已有成功样本（决策 + 分支）保留，非 0.0 占位
            assert rep["metrics"]["peak_rss_mb"] == 99.0
            assert rep["metrics"]["peak_gpu_mb"] == 44.0
            assert mserver._FAIL["remaining"] == 0  # v67：3 次注入全消费（retry 耗尽）
        finally:
            adapter.stop()
