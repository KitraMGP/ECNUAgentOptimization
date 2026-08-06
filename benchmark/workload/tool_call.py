"""tool_call —— 工具调用场景（工具返回大段 JSON 回填上下文 -> 上下文冗余膨胀）。

迁移自旧 agent_bench.py 的 scenario_tool_call / TOOL_SYSTEM / MOCK_TOOLS，
保持文本 ACTION 协议、mock 工具数据与结果行结构一致。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from framework.config import BenchmarkConfig
from framework.driver import Driver
from framework.sampler import mem_str
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


@register
class ToolCallWorkload(Workload):
    name = "tool_call"
    version = "1.0"
    description = "工具调用：文本 ACTION 协议 + 大段 JSON 回填，测上下文冗余膨胀。"

    def params_from_config(self, config: BenchmarkConfig) -> Dict[str, Any]:
        return {"steps": config.tool_steps}

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
        messages = [
            {"role": "system", "content": TOOL_SYSTEM},
            {"role": "user", "content": "请查询客户 C10086 的订单，汇总每个订单的金额与状态。"},
        ]
        rows: List[dict] = []
        for i in range(steps):
            r = driver.chat(messages)
            m = re.search(r"ACTION:\s*(\w+)\(([^)]*)\)", r["text"])
            rows.append({"step": i + 1, "action": m.group(1) if m else None, **r})
            if not m:
                break  # 模型已给出最终答案
            messages.append({"role": "assistant", "content": r["text"]})
            messages.append({
                "role": "user",
                "content": f"<tool_response>\n{MOCK_TOOLS[m.group(1)](m.group(2))}\n</tool_response>",
            })
            print(
                f"  [工具步{i+1:>3}/{steps}] 调用={m.group(1)} "
                f"prompt={r['prompt_tokens']:>5} total={r['total_tokens']:>6} "
                f"延迟={r['latency_ms']:>7}ms {mem_str(r)}"
            )
        return {"rows": rows, "meta": {}}

    def evaluate(self, results: Dict[str, Any], spec: WorkloadSpec) -> Dict[str, Any]:
        rows = results["rows"]
        actions = [r["action"] for r in rows if r.get("action")]
        # E0 从宽判据：至少成功发起一次工具调用
        return {
            "task_success": len(actions) >= 1,
            "tool_calls": len(actions),
            "tool_completion": round(len(actions) / max(1, spec.params.get("steps", 6)), 4),
        }
