"""realistic_agent —— 可复现的多阶段 Agent 任务。

覆盖规划、检索、详情查询、一次失败重试、状态更新、校验和总结。
工具结果是真实消息的一部分，payload 大小由参数控制；不调用外部服务。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from framework.config import BenchmarkConfig
from framework.driver import Driver
from framework.workload import Workload, WorkloadSpec, register


TASK_ID = "TASK-AGENT-20260821"
ORDER_ID = "SO-7319"
CUSTOMER_ID = "C-10086"
TOTAL = "598.00"
ACTION_RE = re.compile(r"ACTION:\s*(\w+)\(([^)]*)\)")

SYSTEM = (
    "你是订单运营 Agent。必须按阶段完成任务，只能调用给定工具，"
    "不得编造工具结果。最终总结必须包含 task_id、order_id、total 和 status。"
)


def _tool_call(name: str, args: str) -> str:
    return f"ACTION: {name}({args})"


def _payload(size: int) -> str:
    base = {
        "order_id": ORDER_ID,
        "customer_id": CUSTOMER_ID,
        "total": TOTAL,
        "status": "ready",
        "items": [{"sku": f"SKU-{i:04d}", "quantity": i % 4 + 1,
                   "price": f"{(i + 1) * 3.25:.2f}"} for i in range(32)],
    }
    text = json.dumps(base, ensure_ascii=False, separators=(",", ":"))
    return (text + "|audit=" + "x" * max(0, size - len(text)))[:size]


def _response(name: str, args: str, *, failed: bool, payload_size: int) -> str:
    if name == "search_orders":
        return json.dumps({"orders": [ORDER_ID], "customer_id": CUSTOMER_ID}, ensure_ascii=False)
    if name == "get_order_detail":
        if failed:
            return json.dumps({"ok": False, "error": "temporary_timeout", "retryable": True})
        return _payload(payload_size)
    if name == "update_order":
        return json.dumps({"ok": True, "order_id": ORDER_ID, "status": "approved"})
    if name == "verify_order":
        return json.dumps({"ok": True, "order_id": ORDER_ID, "total": TOTAL})
    return json.dumps({"ok": False, "error": "unsupported_tool", "retryable": False})


@register
class RealisticAgentWorkload(Workload):
    name = "realistic_agent"
    version = "1.0"
    description = "多阶段 Agent：规划、工具链、失败重试、状态更新与最终总结。"

    def params_from_config(self, config: BenchmarkConfig) -> Dict[str, Any]:
        return {
            "rounds": config.realistic_rounds,
            "payload_chars": config.realistic_payload_chars,
            "failure_round": 3,
        }

    def generate(self, params: Dict[str, Any]) -> WorkloadSpec:
        return WorkloadSpec(
            name=self.name,
            params=dict(params),
            prompts=["plan", "search", "detail", "retry", "update", "verify", "summarize"],
            expected={"task_id": TASK_ID, "order_id": ORDER_ID, "total": TOTAL,
                      "status": "approved"},
            meta={"version": self.version, "description": self.description,
                  "trace": "plan>search>detail_failure>detail_retry>update>verify>summary"},
        )

    def run(self, driver: Driver, spec: WorkloadSpec) -> Dict[str, Any]:
        rounds = max(7, int(spec.params.get("rounds", 10)))
        payload_size = max(256, int(spec.params.get("payload_chars", 12000)))
        history: List[dict] = [{"role": "system", "content": SYSTEM}]
        rows: List[dict] = []
        state = {"searched": False, "detail": False, "retried": False,
                 "updated": False, "verified": False}
        phase_prompts = [
            "规划任务 TASK-AGENT-20260821，先说明下一步。",
            f"查询客户 {CUSTOMER_ID} 的订单，必须调用 search_orders。",
            f"查询订单 {ORDER_ID} 详情，必须调用 get_order_detail。",
            "上一次详情查询发生 temporary_timeout。请只重试 get_order_detail。",
            f"详情已获得。请调用 update_order(order_id={ORDER_ID}, status=approved)。",
            f"请调用 verify_order(order_id={ORDER_ID}) 校验总额 {TOTAL}。",
            "请输出最终 JSON 总结，包含 task_id、order_id、total、status。",
        ]
        for i in range(rounds):
            phase = min(i, len(phase_prompts) - 1)
            history.append({"role": "user", "content": phase_prompts[phase]})
            result = driver.chat(history)
            text = result.get("text", "") or ""
            match = ACTION_RE.search(text)
            action = match.group(1) if match else None
            args = match.group(2) if match else ""
            tool_error = None
            payload_tokens = 0
            if action:
                failed = action == "get_order_detail" and not state["retried"]
                tool_text = _response(action, args, failed=failed, payload_size=payload_size)
                tool_error = "temporary_timeout" if failed else None
                if action == "search_orders":
                    state["searched"] = True
                elif action == "get_order_detail":
                    state["retried"] = state["retried"] or failed
                    if not failed:
                        state["detail"] = True
                elif action == "update_order":
                    state["updated"] = True
                elif action == "verify_order":
                    state["verified"] = True
                payload_tokens = len(tool_text)
                history.extend([
                    {"role": "assistant", "content": text},
                    {"role": "tool", "content": tool_text},
                ])
            else:
                history.append({"role": "assistant", "content": text})
            row = {"round": i + 1, "phase": phase_prompts[phase],
                   "action": action, "tool_error": tool_error,
                   "payload_chars": payload_tokens, **result}
            rows.append(row)
        final_text = rows[-1].get("text", "") if rows else ""
        state["summary"] = all(v in final_text for v in (TASK_ID, ORDER_ID, TOTAL, "approved"))
        return {"rows": rows, "meta": {"state": state, "payload_chars": payload_size,
                                         "task_id": TASK_ID}}

    def evaluate(self, results: Dict[str, Any], spec: WorkloadSpec) -> Dict[str, Any]:
        rows = results.get("rows", [])
        state = results.get("meta", {}).get("state", {})
        actions = [r.get("action") for r in rows if r.get("action")]
        return {
            "task_success": bool(state.get("summary")),
            "plan_completed": bool(rows),
            "tool_calls": len(actions),
            "retry_recovered": bool(state.get("retried") and state.get("detail")),
            "order_updated": bool(state.get("updated")),
            "order_verified": bool(state.get("verified")),
            "summary_facts": bool(state.get("summary")),
            "tool_errors": sum(1 for r in rows if r.get("tool_error")),
        }
