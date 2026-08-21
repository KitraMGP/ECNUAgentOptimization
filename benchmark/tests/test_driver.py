"""Driver 单元测试：mock OpenAI 客户端（不连接真实 server）。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import json

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


def test_chat_retry_keeps_real_user_query(driver_with_mock_client):
    """400 兜底丢弃早期消息后，若剩余全是 <tool_response> user（Qwen3.5
    template 会 500 'No user query found'），必须找回真实的用户查询。"""
    drv, client = driver_with_mock_client
    # 消息结构：真实查询在最前，其后全是 tool_response 回填
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "请查询订单"},
            {"role": "assistant", "content": "ACTION: search_orders(cid)"},
            {"role": "user", "content": "<tool_response>\n{\"ids\": [1,2,3]}\n</tool_response>"},
            {"role": "assistant", "content": "ACTION: get_detail(id1)"},
            {"role": "user", "content": "<tool_response>\n{\"order\": 1}\n</tool_response>"},
            {"role": "assistant", "content": "ACTION: get_detail(id2)"},
            {"role": "user", "content": "<tool_response>\n{\"order\": 2}\n</tool_response>"}]
    client.chat.completions.create.side_effect = [
        Exception("exceed_context_size_error ..."),  # 触发兜底
        make_fake_resp(text="ok"),
    ]
    row = drv.chat(msgs)
    assert row["text"] == "ok"
    second_msgs = client.chat.completions.create.call_args.kwargs["messages"]
    # 兜底后仍存在真实用户查询（非 tool_response）
    real_users = [m for m in second_msgs
                  if m["role"] == "user"
                  and not (str(m["content"]).startswith("<tool_response>"))]
    assert len(real_users) >= 1, "兜底后丢失了真实用户查询"


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


def test_nonstandard_endpoints_use_server_root(driver_with_mock_client):
    drv, _ = driver_with_mock_client

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"prompt": "rendered"}).encode()

    with patch("framework.driver.urllib.request.urlopen", return_value=Response()) as urlopen:
        assert drv.apply_template([{"role": "user", "content": "hi"}]) == "rendered"
    assert urlopen.call_args.args[0].full_url == "http://127.0.0.1:8080/apply-template"
