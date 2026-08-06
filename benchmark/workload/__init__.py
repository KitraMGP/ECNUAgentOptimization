"""workload 包：导入即注册全部场景（register 装饰器）。"""
from __future__ import annotations

# 导入触发 @register 注册（顺序即 all 场景的默认顺序）
from . import branch, long_life, multi_turn, tool_call  # noqa: F401
from framework.workload import all_workloads, get_workload  # noqa: F401

__all__ = ["all_workloads", "get_workload"]
