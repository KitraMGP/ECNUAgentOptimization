"""tool_call —— 工具调用场景（工具返回大段 JSON 回填上下文 -> 上下文冗余膨胀）。

迁移自旧 agent_bench.py 的 scenario_tool_call / TOOL_SYSTEM / MOCK_TOOLS，
保持文本 ACTION 协议、mock 工具数据与结果行结构一致。

E15.2：ToolPayloadStore 最小端到端集成（本文件同时承载 tool_call / long_life
共用的集成组件 ``ToolPayloadRun``）：
- 每个 workload run 创建 thread-scoped ToolPayloadStore；
- ``tool_payload_mode=externalized``：工具返回先 put，tool_response 只注入
  deterministic projection + payload_ref + expected_hash（不含原文）；
  模型输出 ``ACTION: resolve_tool_payload(payload_ref=<ref>, expected_hash=<hash>)``
  时由 workload 拦截并严格校验，resolve 成功仅本轮 tool_response 注入完整 payload；
- ``tool_payload_mode=off``（默认）：完全旧行为（完整 payload 直接回填，
  不 put、不拦截、不加审计字段），可回滚。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from framework.config import BenchmarkConfig
from framework.driver import Driver
from framework.sampler import mem_str
from framework.tool_payload import (
    Ref,
    ToolPayloadError,
    ToolPayloadStore,
    canonical_json,
)
from framework.workload import Workload, WorkloadSpec, register

TOOL_SYSTEM = """你是智能客服助手，可调用以下工具：
- search_orders(customer_id)：查询客户订单列表（返回订单ID列表）
- get_order_detail(order_id)：查询单个订单的完整详情
处理流程（必须严格遵守）：
1. 先调用 search_orders(customer_id) 获取订单列表；
2. 然后对列表中的【每一个】订单依次调用 get_order_detail(order_id)；
3. 全部查询完成后，才输出最终汇总答案。
需要调用工具时，只输出一行：ACTION: 工具名(参数)。收到工具结果后继续下一步。
调用格式示例（参数必须填真实值，不要写参数名）：
- ACTION: search_orders(customer_id=C10086)
- ACTION: get_order_detail(order_id=SO00000)"""


def _search_orders(cid: str) -> str:
    return json.dumps(
        {"customer": cid, "order_ids": [f"SO{i:05d}" for i in range(8)]},
        ensure_ascii=False, indent=2,
    )


def _get_order_detail(oid: str) -> str:
    return json.dumps(
        {"order_id": oid,
         "items": [{"name": f"SKU{j}", "qty": j + 1, "price": round(25.5 * j, 2)}
                   for j in range(6)],
         "address": "湖南省长沙市开福区三一大道 500 号 8 栋 1203 室",
         "logistics": [{"time": f"2026-03-0{i+1} 10:0{i}", "location": "长沙转运中心",
                        "status": "已揽收"} for i in range(5)]},
        ensure_ascii=False, indent=2,
    )


MOCK_TOOLS = {
    "search_orders": _search_orders,
    "get_order_detail": _get_order_detail,
}


# ---- E15.2：ToolPayloadStore 集成组件（tool_call / long_life 共用）----------------
# resolve 协议（H3）：模型需要完整原文时输出的内部动作，由 workload 拦截，
# 绝不交给 MOCK_TOOLS；ref / expected_hash 均为 sha256 hex（64 位小写十六进制）。
RESOLVE_ACTION_RE = re.compile(
    r"ACTION:\s*resolve_tool_payload\(\s*payload_ref=([0-9a-f]{64})\s*,"
    r"\s*expected_hash=([0-9a-f]{64})\s*\)"
)
TOOL_PAYLOAD_MODES: Tuple[str, ...] = ("off", "externalized")
DEFAULT_TOOL_PAYLOAD_MODE = "off"


def normalize_tool_payload_mode(value: Any, default: str = DEFAULT_TOOL_PAYLOAD_MODE) -> str:
    """归一化 ``tool_payload_mode``；非法值 fail-fast（ValueError）。

    配置来源：config.extra["tool_payload_mode"]（配置文件未知键）或 spec.params
    （测试/直接调用）。off = 旧行为（可回滚），externalized = 外置 projection。
    """
    if value is None:
        return default
    if value not in TOOL_PAYLOAD_MODES:
        raise ValueError(
            f"tool_payload_mode={value!r} 非法，可选: {', '.join(TOOL_PAYLOAD_MODES)}（fail-fast）")
    return value


def _render_externalized_tool_response(ref: Any, entry: Any) -> str:
    """externalized tool_response 内容：只含 deterministic projection + payload_ref +
    expected_hash（不含 payload 原文；projection 由 ToolPayloadStore 生成，天然不含
    address/customer 等原文值）。"""
    proj = canonical_json(dict(entry.projection))
    return (
        "[tool_payload externalized]\n"
        f"payload_ref={ref.ref}\n"
        f"expected_hash={ref.expected_hash}\n"
        f"projection: {proj}\n"
        f"如需完整原文，请输出 ACTION: resolve_tool_payload("
        f"payload_ref={ref.ref}, expected_hash={ref.expected_hash})"
    )


class ToolPayloadRun:
    """单个 workload run 的 ToolPayloadStore 集成（thread-scoped + resolve 拦截 + 审计）。

    - 每个 run 创建一个实例（store 绑定 run 所在线程，thread-scoped）；
    - externalized：工具返回先 ``put`` → tool_response 只注入 projection + ref/hash；
      模型输出 resolve 动作由 workload 拦截（不交给 MOCK_TOOLS），严格校验 ref/hash，
      成功仅本轮 tool_response 注入完整 payload（下一轮自动恢复 projection 形态）；
    - off：完全旧行为（完整 payload 直接回填，不 put、不拦截、无审计字段）。
    """

    def __init__(self, mode: str) -> None:
        self.mode = normalize_tool_payload_mode(mode)
        self.store = ToolPayloadStore()
        self.externalized_puts = 0
        self.resolve_requests = 0
        self.resolve_successes = 0
        self.resolve_failures = 0
        self.projection_chars = 0
        self.full_payload_chars = 0
        # pending one-shot restore：resolve 注入完整 payload 后，记录其消息位置与
        # 同 ref/hash 的 projection 占位；下一次 after_chat 时原位替换回占位，
        # 确保完整原文仅在紧接的一次模型请求中可见（异常/fail-fast 时 run 终止，
        # 局部 messages 不再被任何后续请求使用，不会泄漏进历史）。
        self._pending_restore: Optional[Dict[str, Any]] = None

    def tool_response(self, action: str, args: str, text: str
                      ) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
        """根据模型输出的 ACTION 生成 tool_response 内容、行级审计与（可选）projection 占位。

        返回 ``(content, audit, projection_placeholder)``：
        - ``resolve_tool_payload``（仅 externalized）：workload 拦截；格式非法 →
          ``ToolPayloadError``；ref 缺失 / hash 不匹配 → 对应 ``ToolPayloadError``
          子类（fail-fast）；成功 → content=完整 payload（仅本轮注入），
          projection_placeholder=同 ref/hash 的 projection 占位（供 one-shot restore）。
        - 普通工具：保持原解析（参数原样传入），未知工具 KeyError fail-fast
          （无静默 fallback）；externalized 模式先 put，content=projection 占位
          （无需恢复，placeholder=None）。
        - off：content=完整 payload，audit/placeholder 均为 None（旧行为）。
        """
        if self.mode == "externalized" and action == "resolve_tool_payload":
            self.resolve_requests += 1
            m = RESOLVE_ACTION_RE.search(text)
            if m is None:
                self.resolve_failures += 1
                raise ToolPayloadError(
                    f"非法 resolve_tool_payload 动作（payload_ref/expected_hash 格式错误）: "
                    f"{text!r}（fail-fast）")
            ref, expected_hash = m.group(1), m.group(2)
            try:
                payload = self.store.resolve(ref, expected_hash)
            except ToolPayloadError:
                self.resolve_failures += 1
                raise
            self.resolve_successes += 1
            self.full_payload_chars += len(payload)
            placeholder = _render_externalized_tool_response(
                Ref(ref=ref, expected_hash=expected_hash), self.store.get(ref))
            return payload, {"kind": "resolve_full", "ref": ref, "chars": len(payload)}, placeholder
        payload = MOCK_TOOLS[action](args)   # 未知工具 KeyError（fail-fast，保持原语义）
        if self.mode == "externalized":
            ref = self.store.put(payload)
            self.externalized_puts += 1
            content = _render_externalized_tool_response(ref, self.store.get(ref.ref))
            self.projection_chars += len(content)
            return content, {"kind": "externalized_projection", "ref": ref.ref,
                             "chars": len(content)}, None
        return payload, None, None

    # ---- pending one-shot restore（完整 payload 仅紧接一次请求可见）----------------
    def mark_pending(self, index: int, projection: str,
                     expected_content: Optional[str] = None) -> None:
        """记录 pending restore：``messages[index]`` 为刚注入的完整 payload user 消息，
        下一次 ``after_chat`` 时原位替换回同 ref/hash 的 projection 占位。

        ``expected_content`` 为注入时的完整 tool_response 文本；``after_chat`` 恢复前
        校验当前位置内容与之匹配（不匹配 = 消息被改写，fail-fast，不静默恢复）。
        """
        self._pending_restore = {
            "index": index, "projection": projection, "expected_content": expected_content,
        }

    def after_chat(self, messages: List[dict]) -> None:
        """一次模型请求返回后调用：把 pending 的完整 payload 原位恢复为 projection 占位，
        保证后续所有 messages 历史不含完整原文（one-shot）。无 pending 时为 no-op。

        任何不一致（projection 缺失 / pending index 越界 / 位置不是 user 消息 /
        内容与注入时不符）→ 抛 ``ToolPayloadError``（fail-fast），**绝不静默清空
        pending**（异常时保留 pending 状态以便审计，不伪装恢复成功）。
        """
        if self._pending_restore is None:
            return
        idx = self._pending_restore["index"]
        projection = self._pending_restore["projection"]
        expected = self._pending_restore["expected_content"]
        if projection is None:
            raise ToolPayloadError(
                f"pending restore projection 缺失（index={idx}，fail-fast）")
        if idx >= len(messages):
            raise ToolPayloadError(
                f"pending restore index {idx} 越界（messages len={len(messages)}，fail-fast）")
        msg = messages[idx]
        if msg.get("role") != "user":
            raise ToolPayloadError(
                f"pending restore 位置 {idx} 不是 user 消息（role={msg.get('role')!r}，fail-fast）")
        if expected is not None and msg.get("content") != expected:
            raise ToolPayloadError(
                f"pending restore 内容不匹配（位置 {idx} 消息已被改写，fail-fast）")
        msg["content"] = projection
        self._pending_restore = None

    def stats(self) -> Dict[str, Any]:
        """可审计计数（meta 输出；只含计数与字节/字符统计，不含任何 payload 明文）。"""
        return {
            "mode": self.mode,
            "externalized_puts": self.externalized_puts,
            "unique_refs": len(self.store),
            "resolve_requests": self.resolve_requests,
            "resolve_successes": self.resolve_successes,
            "resolve_failures": self.resolve_failures,
            "projection_chars": self.projection_chars,
            "full_payload_chars": self.full_payload_chars,
        }


@register
class ToolCallWorkload(Workload):
    name = "tool_call"
    version = "1.0"
    description = "工具调用：文本 ACTION 协议 + 大段 JSON 回填，测上下文冗余膨胀。"

    def params_from_config(self, config: BenchmarkConfig) -> Dict[str, Any]:
        return {
            "steps": config.tool_steps,
            "tool_payload_mode": normalize_tool_payload_mode(
                config.extra.get("tool_payload_mode")),
        }

    def generate(self, params: Dict[str, Any]) -> WorkloadSpec:
        return WorkloadSpec(
            name=self.name,
            params=dict(params),
            prompts=[TOOL_SYSTEM],
            expected={"steps": params.get("steps", 6),
                      "tools": sorted(MOCK_TOOLS)},
            meta={"version": self.version, "description": self.description},
        )

    def run(self, driver: Driver, spec: WorkloadSpec) -> Dict[str, Any]:
        steps = spec.params["steps"]
        tp = ToolPayloadRun(spec.params.get("tool_payload_mode"))
        messages = [
            {"role": "system", "content": TOOL_SYSTEM},
            {"role": "user", "content": "请查询客户 C10086 的订单，汇总每个订单的金额与状态。"},
        ]
        rows: List[dict] = []
        for i in range(steps):
            r = driver.chat(messages)
            # one-shot restore：上一轮 resolve 注入的完整 payload 在该请求返回后
            # 立即原位恢复为 projection 占位（后续所有 messages 历史不含完整原文）
            tp.after_chat(messages)
            m = re.search(r"ACTION:\s*(\w+)\(([^)]*)\)", r["text"])
            action = m.group(1) if m else None
            row: dict = {"step": i + 1, "action": action, **r}
            if m:
                # E15.2：工具动作统一走 ToolPayloadRun（resolve 拦截 / put / projection），
                # off 模式返回完整 payload，行为与旧版一致；未知工具仍 KeyError fail-fast。
                content, audit, placeholder = tp.tool_response(action, m.group(2), r["text"])
                if audit is not None:
                    row["tool_payload"] = audit
                messages.append({"role": "assistant", "content": r["text"]})
                idx = len(messages)
                full_content = f"<tool_response>\n{content}\n</tool_response>"
                messages.append({"role": "user", "content": full_content})
                if placeholder is not None:
                    # 完整 payload 注入：登记 pending（含注入原文以校验不匹配），
                    # 下一次 after_chat 原位恢复为 projection 占位
                    tp.mark_pending(
                        idx, f"<tool_response>\n{placeholder}\n</tool_response>",
                        expected_content=full_content)
            rows.append(row)
            if not m:
                break  # 模型已给出最终答案
            print(
                f"  [工具步{i+1:>3}/{steps}] 调用={m.group(1)} "
                f"prompt={r['prompt_tokens']:>5} total={r['total_tokens']:>6} "
                f"延迟={r['latency_ms']:>7}ms {mem_str(r)}"
            )
        meta: Dict[str, Any] = {}
        if tp.mode == "externalized":
            meta["tool_payload"] = tp.stats()
        return {"rows": rows, "meta": meta}

    def evaluate(self, results: Dict[str, Any], spec: WorkloadSpec) -> Dict[str, Any]:
        rows = results["rows"]
        # E15.2：排除内部动作 resolve_tool_payload，只统计真实 MOCK_TOOLS 工具调用
        actions = [r["action"] for r in rows
                   if r.get("action") and r["action"] != "resolve_tool_payload"]
        # E0 从宽判据：至少成功发起一次真实工具调用
        return {
            "task_success": len(actions) >= 1,
            "tool_calls": len(actions),
            "tool_completion": round(len(actions) / max(1, spec.params.get("steps", 6)), 4),
        }
