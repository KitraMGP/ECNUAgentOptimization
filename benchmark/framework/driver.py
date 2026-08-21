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

import json
import time
import urllib.error
import urllib.request
import urllib.parse
from typing import Any, Dict, List, Optional

from openai import OpenAI

from . import sampler
from .kv_probe import KVProbe
from .prompt_preprocessor import Preprocessor


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
        kv_probe: Optional[KVProbe] = None,
        preprocessor: Optional[Preprocessor] = None,
        sdk_max_retries: Optional[int] = None,
    ) -> None:
        """封装 OpenAI 兼容客户端；建连时定位 server PID（按端口过滤）。

        ``kv_probe`` 可选：非 None 时在每次请求前后各采集一次 KV 快照
        （E1 可观测性，职责与进程采样分离）。

        ``preprocessor`` 可选（E15.3 B1）：非 None 时在**请求发送前**对
        messages 做 deterministic structured-lossless 压缩（默认 off = None，
        与旧行为完全一致）。压缩产生新消息列表，**不修改调用者 messages**；
        审计字典 ``row["preprocessor"]`` 仅在启用时出现。
        """
        self.base_url = base_url
        self.model = model
        self.enable_thinking = enable_thinking
        self.timings_per_token = timings_per_token
        self.max_retry = max_retry
        self.kv_probe = kv_probe
        self.preprocessor = preprocessor
        self._req_count = 0
        # M0（§3.8 retry 构造合同）：sdk_max_retries=None 时不传 max_retries，
        # 保持 OpenAI SDK 隐式默认（max_retries=2），不改变其他 workload；
        # M0 显式 0（SDK 层不重试，统一由外层 transient wrapper 控制）。
        if sdk_max_retries is None:
            self.client = OpenAI(base_url=base_url, api_key=api_key)
        else:
            self.client = OpenAI(base_url=base_url, api_key=api_key,
                                 max_retries=sdk_max_retries)
        sampler.set_server_pid(sampler.find_server_pid(host, port))

    # ---- 请求 ----
    def chat(
        self,
        messages: List[dict],
        _retry: int = 0,
        *,
        temperature: Optional[float] = None,
        seed: Optional[int] = None,
        max_tokens: Optional[int] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """调用 chat.completions 并记录指标；返回行与旧脚本一致并追加 timings。

        E15.3 B1（preprocessor on）：请求发送前对 messages 做确定性结构化压缩。
        - 压缩产生**新列表**（调用者 messages 零污染）；压缩幂等——400 重试路径
          基于压缩后消息裁剪后递归调用时再次压缩无变化；
        - 审计 ``row["preprocessor"]``（manifest.to_dict()，无原文）仅在启用时
          出现；off（None）时本方法行为与旧版逐字节一致。
        """
        # M0（§3.3 driver 最小兼容扩展）：_retry 保持位置参数（`*` 之前），
        # 新增参数全部 keyword-only 且默认 None——不传时行为与旧版逐字节一致。
        work: List[dict] = messages
        pp_audit: Optional[Dict[str, Any]] = None
        if self.preprocessor is not None:
            work, pp_audit = self.preprocessor.process(messages)
        if self.kv_probe is not None:
            self._req_count += 1
            self.kv_probe.snapshot(tag=f"req_{self._req_count}_start")
        try:
            t0 = time.perf_counter()
            kwargs: Dict[str, Any] = {}
            if temperature is not None:
                kwargs["temperature"] = temperature
            if seed is not None:
                kwargs["seed"] = seed
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=work,
                extra_body=self._extra_body(extra_body),
                **kwargs,
            )
            ms = (time.perf_counter() - t0) * 1000
            u = resp.usage
            ptd = getattr(u, "prompt_tokens_details", None)
            cached = getattr(ptd, "cached_tokens", 0) if ptd else 0
            row = {
                "text": resp.choices[0].message.content or "",
                # M0（Critical 2）：顶层 finish_reason = choices[0].finish_reason
                # （openai SDK 标准属性；runner 只从顶层判定，不依赖非标准 timings）
                "finish_reason": getattr(resp.choices[0], "finish_reason", None),
                "prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "total_tokens": u.total_tokens,
                "cached_tokens": cached or 0,
                "latency_ms": round(ms, 1),
                "rss_mb": sampler.find_server_rss_mb(sampler.get_server_pid()),
                "gpu_mb": sampler.find_server_gpu_mb(sampler.get_server_pid()),
                "timings": self._extract_timings(resp),
            }
            if pp_audit is not None:
                row["preprocessor"] = pp_audit
            if self.kv_probe is not None:
                self.kv_probe.snapshot(tag=f"req_{self._req_count}_end")
            return row
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
                dropped = rest[:drop]
                candidate = body + rest[drop:]
                # Qwen3.5 chat template（multi_step_tool 分支）要求消息中存在至少一条
                # "非 <tool_response> 的 user 消息"（真正的用户查询），否则 server 端
                # raise 'No user query found in messages'（500）。若丢弃的早期消息里
                # 包含真实用户查询，找回最早一条插入 system 之后，保证重试可渲染。
                if not any(self._is_real_user_query(m) for m in candidate):
                    for m in dropped:
                        if self._is_real_user_query(m):
                            candidate.insert(len(body), m)
                            break
                messages = candidate
                print(
                    f"    [chat 400 兜底] 丢弃 {drop} 条最早消息后重试 "
                    f"(第 {_retry + 1}/{self.max_retry} 次)"
                )
                return self.chat(
                    messages,
                    _retry + 1,
                    temperature=temperature,
                    seed=seed,
                    max_tokens=max_tokens,
                    extra_body=extra_body,
                )
            raise

    def _extra_body(self, override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """构造 extra_body；``override``（M0 扩展）与默认字典合并，调用方优先。"""
        body: Dict[str, Any] = {}
        if not self.enable_thinking:
            # 固定 no-think，保证可比（旧脚本固定 False）
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if self.timings_per_token:
            body["timings_per_token"] = True
        if override:
            body.update(override)
        return body

    # ---- M0 非标准端点（§4.2：/apply-template + /tokenize 精确 token 计数） ----

    def _post_json(self, path: str, payload: Dict[str, Any], timeout: float = 30.0) -> Any:
        """POST server-root 非标准端点并解析 JSON。

        OpenAI requests use ``base_url`` ending in ``/v1``.  llama-server's
        ``/apply-template`` and ``/tokenize`` endpoints are intentionally
        registered at the server root, not below ``/v1``.
        """
        parsed = urllib.parse.urlsplit(self.base_url)
        base_path = parsed.path.rstrip("/")
        if base_path.endswith("/v1"):
            base_path = base_path[:-3]
        root = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, base_path, "", "")).rstrip("/")
        url = f"{root}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise OSError(f"{path} HTTP {e.code}: {e.read().decode('utf-8', 'replace')}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise OSError(f"{path} 请求失败: {e}")

    def apply_template(
        self,
        messages: List[dict],
        add_generation_prompt: bool = True,
        chat_template_kwargs: Optional[Dict[str, Any]] = None,
    ) -> str:
        """POST /apply-template（server.cpp:263）→ 渲染后 prompt 文本。

        chat_template_kwargs 默认 ``{enable_thinking: false}``（M0 thinking
        合同 §3.3：不传时 server 渲染 ``<think>``）。
        """
        payload: Dict[str, Any] = {
            "messages": messages,
            "add_generation_prompt": add_generation_prompt,
            "chat_template_kwargs": chat_template_kwargs
            if chat_template_kwargs is not None
            else {"enable_thinking": False},
        }
        body = self._post_json("/apply-template", payload)
        if not isinstance(body, dict) or not isinstance(body.get("prompt"), str):
            raise OSError("/apply-template 响应缺 prompt 字段（schema 无法解析）")
        return body["prompt"]

    def tokenize(self, content: str, add_special: bool = False) -> List[int]:
        """POST /tokenize（server.cpp:261）→ token id 列表；P/B = len(tokens)。"""
        body = self._post_json("/tokenize", {"content": content, "add_special": add_special})
        if not isinstance(body, dict) or not isinstance(body.get("tokens"), list):
            raise OSError("/tokenize 响应缺 tokens 字段（schema 无法解析）")
        return list(body["tokens"])

    def count_tokens(self, content: str, add_special: bool = False) -> int:
        """精确 token 计数（§4.2）：len(/tokenize)。"""
        return len(self.tokenize(content, add_special))

    @staticmethod
    def _is_real_user_query(m: dict) -> bool:
        """判断是否为"真正的用户查询"（非 <tool_response> 回填）。

        Qwen3.5 chat template 的 multi_step_tool 分支要求消息中存在至少一条
        非 tool_response 的 user 消息，否则 server 端 500（'No user query
        found in messages'）。400 兜底丢弃早期消息时必须保证其存在。
        """
        if m.get("role") != "user":
            return False
        content = (m.get("content") or "").strip()
        return not (
            content.startswith("<tool_response>")
            and content.endswith("</tool_response>")
        )

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
