"""multi_turn —— 多轮对话场景（上下文逐轮累积 -> KV Cache 持续增长）。

迁移自旧 agent_bench.py 的 scenario_multi_turn / SYSTEM / POOL，
保持 prompt 池、打印格式与结果行结构一致。
"""
from __future__ import annotations

from typing import Any, Dict, List

from framework.config import BenchmarkConfig
from framework.driver import Driver
from framework.sampler import mem_str
from framework.workload import Workload, WorkloadSpec, register

SYSTEM = "你是乐于助人的中文助手。回答简洁准确。"

POOL = [
    "解释一下什么是 KV Cache。",
    "写一句押韵的中文诗。",
    "把'今天天气很好'翻译成英文。",
    "3.14 乘以 2 等于多少？",
    "用一句话介绍长沙。",
    "什么是 Copy-on-Write？",
    "推荐一本编程入门书。",
    "计算 15 + 27 并解释过程。",
]


@register
class MultiTurnWorkload(Workload):
    name = "multi_turn"
    version = "1.0"
    description = "多轮对话：固定 prompt 池循环累积历史，测缓存复用与延迟增长。"

    def params_from_config(self, config: BenchmarkConfig) -> Dict[str, Any]:
        return {"rounds": config.rounds}

    def generate(self, params: Dict[str, Any]) -> WorkloadSpec:
        return WorkloadSpec(
            name=self.name,
            params=dict(params),
            prompts=list(POOL),
            expected={"rounds": params.get("rounds", 20)},
            meta={"version": self.version, "description": self.description},
        )

    def run(self, driver: Driver, spec: WorkloadSpec) -> Dict[str, Any]:
        rounds = spec.params["rounds"]
        history = [{"role": "system", "content": SYSTEM}]
        rows: List[dict] = []
        for i in range(rounds):
            q = POOL[i % len(POOL)]
            history.append({"role": "user", "content": q})
            r = driver.chat(history)
            history.append({"role": "assistant", "content": r["text"]})
            rows.append({"round": i + 1, **r})
            print(
                f"  [回合{i+1:>3}/{rounds}] prompt={r['prompt_tokens']:>5} "
                f"total={r['total_tokens']:>6} 延迟={r['latency_ms']:>7}ms {mem_str(r)}"
            )
        return {"rows": rows, "meta": {}}

    def evaluate(self, results: Dict[str, Any], spec: WorkloadSpec) -> Dict[str, Any]:
        rows = results["rows"]
        # E0 从宽判据：所有轮次均产生非空输出
        non_empty = [r for r in rows if (r.get("text") or "").strip()]
        return {
            "task_success": len(non_empty) == len(rows),
            "checked_rounds": len(rows),
        }
