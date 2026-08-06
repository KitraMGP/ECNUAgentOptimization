"""framework —— Benchmark 核心框架（配置 / 驱动 / 采样 / workload 抽象）。"""
from framework.config import BenchmarkConfig  # noqa: F401
from framework.driver import Driver  # noqa: F401
from framework.workload import Workload, WorkloadSpec  # noqa: F401

__all__ = ["BenchmarkConfig", "Driver", "Workload", "WorkloadSpec"]
