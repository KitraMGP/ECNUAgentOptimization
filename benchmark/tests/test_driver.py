"""Driver 单元测试：mock OpenAI 客户端（不连接真实 server）。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from framework.driver import Driver


def make_fake_resp(text="hi", prompt=10, completion=5, cached=3, timings=None,
                   model_extra=None):
    usage = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )
    resp = SimpleNamespace(
        usage=usage,
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
    )
    if model_extra is not None:
        resp.model_extra = model_extra
    return resp


@pytest.fixture
def driver_with_mock_client():
    fake_client = MagicMock()
    with patch("framework.driver.OpenAI", return_value=fake_client), \
         patch("framework.sampler.find_server_pid", return_value=None):
        drv = Driver(base_url="http://127.0.0.1:8080/v1")
    return drv, fake_client


def test_chat_returns_legacy_fields(driver_with_mock_client):
    drv, client = driver_with_mock_client
    client.chat.completions.create.return_value = make_fake_resp(
        text="答案", prompt=10, completion=5, cached=3)
    row = drv.chat([{"role": "user", "content": "hi"}])
    assert row["text"] == "答案"
    assert row["prompt_tokens"] == 10
    assert row["completion_tokens"] == 5
    assert row["total_tokens"] == 15
    assert row["cached_tokens"] == 3
    assert isinstance(row["latency_ms"], float)
    # 无 timings 时返回 None
    assert row["timings"] is None


def test_chat_extracts_timings_from_model_extra(driver_with_mock_client):
    drv, client = driver_with_mock_client
    timings = {"prompt_n": 7, "cache_n": 3, "predicted_n": 5}
    client.chat.completions.create.return_value = make_fake_resp(
        model_extra={"timings": timings})
    row = drv.chat([{"role": "user", "content": "hi"}])
    assert row["timings"] == timings


def test_chat_extracts_timings_from_attribute(driver_with_mock_client):
    drv, client = driver_with_mock_client
    timings = {"prompt_n": 8, "cache_n": 2}
    resp = make_fake_resp()
    resp.timings = timings
    client.chat.completions.create.return_value = resp
    row = drv.chat([{"role": "user", "content": "hi"}])
    assert row["timings"] == timings


def test_chat_retry_on_ctx_overflow(driver_with_mock_client):
    """400 超出 ctx：丢弃最早的非 system 消息后重试（最多 3 次）。"""
    drv, client = driver_with_mock_client
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"}]
    client.chat.completions.create.side_effect = [
        Exception("exceed_context_size_error ..."),  # 第一次触发兜底
        make_fake_resp(text="ok"),
    ]
    row = drv.chat(msgs)
    assert row["text"] == "ok"
    # 第二次调用时消息变短（丢 1/3 非 system 消息）
    second_msgs = client.chat.completions.create.call_args.kwargs["messages"]
    assert len(second_msgs) < len(msgs)
    assert second_msgs[0]["role"] == "system"


def test_chat_gives_up_after_max_retry(driver_with_mock_client):
    drv, client = driver_with_mock_client
    client.chat.completions.create.side_effect = [
        Exception("exceed_context_size_error ...") for _ in range(4)
    ]
    # 6 条消息：len(messages) > 2 约束不会先于 max_retry 触发
    msgs = ([{"role": "system", "content": "s"}] +
            [{"role": "user", "content": f"u{i}"} for i in range(5)])
    with pytest.raises(Exception):
        drv.chat(msgs)
    # 初始 1 次 + 3 次重试（max_retry=3）
    assert client.chat.completions.create.call_count == 4


def test_chat_raises_other_errors(driver_with_mock_client):
    drv, client = driver_with_mock_client
    client.chat.completions.create.side_effect = Exception("connection refused")
    with pytest.raises(Exception, match="connection refused"):
        drv.chat([{"role": "user", "content": "hi"}])
