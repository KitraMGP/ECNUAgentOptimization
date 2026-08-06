"""Driver —— llama-server OpenAI 兼容 API 请求封装。

与旧 agent_bench.py 的 ``chat()`` 行为保持一致：
- 返回行包含 text / prompt_tokens / completion_tokens / total_tokens /
  cached_tokens / latency_ms / rss_mb / gpu_mb；
- 400 超出 ctx 时自动丢弃最早的非 system 消息重试（最多 max_retry 次）。

E0 新增：
- 每行追加 ``timings`` 键：llama-server 响应中的 timings 对象
  （含 prompt_n / cache_n / predicted_n / prompt_ms / predicted_ms /
  prompt_per_second / predicted_per_second），解析不到时为 None。
  注：``prompt_n + cache_n == prompt_tokens``（llama.cpp 单测验证）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from openai import OpenAI

from . import sampler


class Driver:
    def __init__(
        self,
        base_url: str,
        model: str = "bench",
        api_key: str = "EMPTY",
        host: str = "127.0.0.1",
        port: int = 8080,
        enable_thinking: bool = False,
        timings_per_token: bool = False,
        max_retry: int = 3,
    ) -> None:
        """封装 OpenAI 兼容客户端；建连时定位 server PID（按端口过滤）。"""
        self.base_url = base_url
        self.model = model
        self.enable_thinking = enable_thinking
        self.timings_per_token = timings_per_token
        self.max_retry = max_retry
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        sampler.set_server_pid(sampler.find_server_pid(host, port))

    # ---- 请求 ----
    def chat(self, messages: List[dict], _retry: int = 0) -> Dict[str, Any]:
        """调用 chat.completions 并记录指标；返回行与旧脚本一致并追加 timings。"""
        try:
            t0 = time.perf_counter()
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                extra_body=self._extra_body(),
            )
            ms = (time.perf_counter() - t0) * 1000
            u = resp.usage
            ptd = getattr(u, "prompt_tokens_details", None)
            cached = getattr(ptd, "cached_tokens", 0) if ptd else 0
            return {
                "text": resp.choices[0].message.content or "",
                "prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "total_tokens": u.total_tokens,
                "cached_tokens": cached or 0,
                "latency_ms": round(ms, 1),
                "rss_mb": sampler.find_server_rss_mb(sampler.get_server_pid()),
                "gpu_mb": sampler.find_server_gpu_mb(sampler.get_server_pid()),
                "timings": self._extract_timings(resp),
            }
        except Exception as e:
            # 400: 请求超出 ctx —— 兜底：丢弃最早的非 system 消息后重试
            if (
                "exceed_context_size_error" in str(e)
                and _retry < self.max_retry
                and len(messages) > 2
            ):
                body = [m for m in messages if m["role"] == "system"]
                rest = [m for m in messages if m["role"] != "system"]
                # 一次丢弃约 1/3 的非 system 消息，加速收敛
                drop = max(1, len(rest) // 3)
                messages = body + rest[drop:]
                print(
                    f"    [chat 400 兜底] 丢弃 {drop} 条最早消息后重试 "
                    f"(第 {_retry + 1}/{self.max_retry} 次)"
                )
                return self.chat(messages, _retry + 1)
            raise

    def _extra_body(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if not self.enable_thinking:
            # 固定 no-think，保证可比（旧脚本固定 False）
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if self.timings_per_token:
            body["timings_per_token"] = True
        return body

    @staticmethod
    def _extract_timings(resp: Any) -> Optional[Dict[str, Any]]:
        """从 openai SDK 响应对象中提取 llama-server 的 timings 字段。

        llama-server 在响应顶层附带非标准 ``timings`` 对象；openai SDK
        （pydantic v2）把未知字段放入 model_extra（若允许 extra），
        这里同时尝试属性与 model_extra 两种途径。
        """
        # 1) 属性直达（若 SDK 显式建模了该字段）
        timings = getattr(resp, "timings", None)
        if isinstance(timings, dict):
            return timings
        # 2) pydantic v2 model_extra
        extra = getattr(resp, "model_extra", None)
        if isinstance(extra, dict) and isinstance(extra.get("timings"), dict):
            return extra["timings"]
        # 3) pydantic v2 __pydantic_extra__
        pyd_extra = getattr(resp, "__pydantic_extra__", None)
        if isinstance(pyd_extra, dict) and isinstance(pyd_extra.get("timings"), dict):
            return pyd_extra["timings"]
        return None
