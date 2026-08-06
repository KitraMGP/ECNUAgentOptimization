"""Workload —— 统一场景抽象接口（generate / run / evaluate）。

设计意图（见 docs/benchmark_implementation_plan.md §2.1）：
- ``generate()``：确定性生成 workload 规格（prompt 模板、规模参数、期望结果），
  可版本化、可复现（同 seed 同输出）；
- ``run()``：通过 Driver 执行场景，返回与旧 agent_bench.py 一致的结果结构；
- ``evaluate()``：对运行结果做确定性自动判定（不依赖 LLM 主观评价），
  输出保真约束指标（task_success 等）。

E0 阶段判据从宽（仅作为"优化未损害 agent 能力"的约束门槛），
后续阶段按实现计划逐步收紧。
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import BenchmarkConfig
from .driver import Driver


@dataclass
class WorkloadSpec:
    """generate() 的产出：prompt 规格 + 期望结果 + 元信息。"""

    name: str
    params: Dict[str, Any] = field(default_factory=dict)      # 规模参数快照
    prompts: List[Any] = field(default_factory=list)          # prompt 模板/常量
    expected: Dict[str, Any] = field(default_factory=dict)    # evaluate 判据输入
    meta: Dict[str, Any] = field(default_factory=dict)        # 版本、描述等

    def fingerprint(self) -> str:
        """workload 内容指纹（冻结制度的依据：同指纹 = 同 workload）。"""
        payload = json.dumps(
            {"name": self.name, "params": self.params,
             "prompts": self.prompts, "expected": self.expected},
            ensure_ascii=False, sort_keys=True, default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class Workload(ABC):
    """场景基类：所有 workload 必须实现 generate / run / evaluate。"""

    name: str = ""
    version: str = "1.0"
    description: str = ""

    # ---- 参数提取（子类可覆盖） ----
    def params_from_config(self, config: BenchmarkConfig) -> Dict[str, Any]:
        """从 BenchmarkConfig 提取本场景规模参数（默认：全部标量参数）。"""
        return {}

    # ---- 抽象接口 ----
    @abstractmethod
    def generate(self, params: Dict[str, Any]) -> WorkloadSpec:
        """确定性生成场景规格。"""

    @abstractmethod
    def run(self, driver: Driver, spec: WorkloadSpec) -> Dict[str, Any]:
        """执行场景，返回 {'rows': [...], 'meta': {...}}。

        ``rows`` 结构与旧 agent_bench.py 对应场景一致；
        ``meta`` 携带场景特有结果（如 long_life 的 truncations）。
        """

    @abstractmethod
    def evaluate(self, results: Dict[str, Any], spec: WorkloadSpec) -> Dict[str, Any]:
        """对运行结果做任务判定，返回 {'task_success': bool, ...}。"""

    # ---- 汇总辅助（子类可覆盖） ----
    def rows_for_summary(self, results: Dict[str, Any]) -> List[dict]:
        """用于 summarize 的行列表；默认即 run() 返回的 rows。

        branch 等返回非扁平结构的场景需覆盖（如分支行拼接公共前缀）。
        """
        return results["rows"]


# ---- 注册表 ----
_REGISTRY: Dict[str, Workload] = {}


def register(cls: type) -> type:
    """类装饰器：实例化并注册 workload（registry 存实例，方法调用正常）。"""
    instance = cls()
    if instance.name in _REGISTRY:
        raise ValueError(f"workload 重复注册: {instance.name!r}")
    _REGISTRY[instance.name] = instance
    return cls


def get_workload(name: str) -> Workload:
    if name not in _REGISTRY:
        raise KeyError(f"未知 workload: {name!r}，已注册: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def all_workloads() -> List[Workload]:
    return list(_REGISTRY.values())
