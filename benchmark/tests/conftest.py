"""pytest 共享夹具：sys.path 注入 + FakeDriver（不依赖 GPU / 真实 llama-server）。"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # benchmark/
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.sampler import set_server_pid  # noqa: E402


def default_row(text: str = "测试回复", **over) -> dict:
    """模拟 Driver.chat 的返回行（旧 8 键 + timings）。"""
    row = {
        "text": text,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "cached_tokens": 50,
        "latency_ms": 123.4,
        "rss_mb": 100.0,
        "gpu_mb": 200.0,
        "timings": None,
    }
    row.update(over)
    return row


class FakeDriver:
    """脚本化响应驱动的假 Driver：记录每次调用消息，按序/循环返回响应。"""

    def __init__(self, responses=None):
        self._responses = list(responses or [])
        self._idx = 0
        self.calls = []

    def chat(self, messages, _retry=0):
        self.calls.append([dict(m) for m in messages])
        if self._responses:
            r = self._responses[self._idx % len(self._responses)]
            self._idx += 1
        else:
            r = default_row()
        return dict(r)

    @property
    def n_calls(self):
        return len(self.calls)


@pytest.fixture(autouse=True)
def _no_server_pid():
    """测试环境没有 llama-server：采样函数直接返回 None，避免系统调用。"""
    set_server_pid(None)
    yield
    set_server_pid(None)


@pytest.fixture
def fake_driver():
    return FakeDriver()
