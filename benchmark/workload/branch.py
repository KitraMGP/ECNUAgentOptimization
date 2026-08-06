"""branch —— 分支推理场景（同一前缀派生多个分支 -> 分支内存共享/COW 场景）。

迁移自旧 agent_bench.py 的 scenario_branch，保持公共前缀、分支 prompt、
打印格式与结果结构一致（{"common": r0, "branches": {"A": [...], "B": [...]}}）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from framework.config import BenchmarkConfig
from framework.driver import Driver
from framework.sampler import mem_str
from framework.workload import Workload, WorkloadSpec, register

BRANCH_QUESTIONS = [
    ("A", "请详细推演方案A的具体行程与预算。"),
    ("B", "请详细推演方案B的具体行程与预算。"),
]


@register
class BranchWorkload(Workload):
    name = "branch"
    version = "1.0"
    description = "分支推理：同一公共前缀派生 A/B 分支，测前缀缓存复用与分支内存。"

    def params_from_config(self, config: BenchmarkConfig) -> Dict[str, Any]:
        return {"branch_rounds": config.branch_rounds}

    def generate(self, params: Dict[str, Any]) -> WorkloadSpec:
        return WorkloadSpec(
            name=self.name,
            params=dict(params),
            prompts=[],
            expected={"branches": [q for q, _ in BRANCH_QUESTIONS],
                      "branch_rounds": params.get("branch_rounds", 5)},
            meta={"version": self.version, "description": self.description},
        )

    def run(self, driver: Driver, spec: WorkloadSpec) -> Dict[str, Any]:
        branch_rounds = spec.params["branch_rounds"]
        prefix = [
            {"role": "system", "content": "你是乐于助人的中文助手。回答简洁准确。"},
            {"role": "user", "content": "我计划周末去张家界旅游，请先给出总体思路。"},
        ]
        r0 = driver.chat(prefix)
        common = prefix + [{"role": "assistant", "content": r0["text"]}]
        print(
            f"  [公共前缀] prompt={r0['prompt_tokens']:>5} total={r0['total_tokens']:>6} "
            f"{mem_str(r0)}"
        )
        branches: Dict[str, List[dict]] = {}
        for name, q in BRANCH_QUESTIONS:
            msgs = list(common) + [{"role": "user", "content": q}]
            rows: List[dict] = []
            for i in range(branch_rounds):
                r = driver.chat(msgs)
                rows.append({"sub_round": i + 1, **r})
                msgs.append({"role": "assistant", "content": r["text"]})
                msgs.append({"role": "user", "content": f"继续完善方案{name}，下一步怎么做？"})
                print(
                    f"  [分支{name} 第{i+1:>3}/{branch_rounds}] prompt={r['prompt_tokens']:>5} "
                    f"total={r['total_tokens']:>6} {mem_str(r)}"
                )
            branches[name] = rows
        return {"rows": {"common": r0, "branches": branches}, "meta": {}}

    def rows_for_summary(self, results: Dict[str, Any]) -> List[dict]:
        """branch 的汇总行 = 分支 A + 分支 B + 公共前缀（与旧脚本一致）。"""
        data = results["rows"]
        return data["branches"]["A"] + data["branches"]["B"] + [data["common"]]

    def evaluate(self, results: Dict[str, Any], spec: WorkloadSpec) -> Dict[str, Any]:
        branches = results["rows"]["branches"]
        all_rows = self.rows_for_summary(results)
        # E0 从宽判据：A/B 分支均有输出且非空
        ok_a = any((r.get("text") or "").strip() for r in branches.get("A", []))
        ok_b = any((r.get("text") or "").strip() for r in branches.get("B", []))
        return {
            "task_success": ok_a and ok_b,
            "branch_consistency": 1.0 if (ok_a and ok_b) else 0.0,
            "branch_rows": len(all_rows),
        }
