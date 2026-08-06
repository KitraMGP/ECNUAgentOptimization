#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_bench.py — Agent 工作流内存/延迟基准测试（E0 框架兼容入口）

E0 重构后本文件为薄壳：全部逻辑迁移至 benchmark/ 下的框架模块
（framework / workload / metrics / runner / report），本入口保持
CLI 参数与输出格式向后兼容。

用法（在 benchmark/ 目录下，venv 内）:
  uv run python agent_bench.py --scenario multi_turn --rounds 20
  uv run python agent_bench.py --scenario tool_call --tool-steps 6
  uv run python agent_bench.py --scenario branch --branch-rounds 5
  uv run python agent_bench.py --scenario all
  uv run python agent_bench.py --scenario long_life --long-rounds 40 --ctx-size 2048
  uv run python agent_bench.py --config configs/example.yaml --scenario multi_turn

输出: results/bench_<时间戳>.json（结构与旧脚本一致；行内追加 timings 键）
"""
from runner.runner import cli_main

if __name__ == "__main__":
    raise SystemExit(cli_main())
