"""Report —— 生成 markdown 实验报告。

E0 基础版：环境/配置表 + 每场景四层指标表（性能/缓存/资源/任务）。
对比功能（baseline diff）预留参数，E0 仅输出当前结果。
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, Optional


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4f}" if abs(v) < 1 else f"{v:.2f}"
    return str(v)


def _config_table(config: Dict[str, Any]) -> str:
    rows = "".join(
        f"| `{k}` | {_fmt(v)} |\n" for k, v in sorted(config.items())
    )
    return "| 配置项 | 值 |\n|---|---|\n" + rows


def _scenario_table(name: str, summary: Dict[str, Any]) -> str:
    ev = summary.get("evaluation") or {}
    lines = [
        f"### {name}",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| rounds | {summary.get('rounds', '-')} |",
        f"| prompt_tokens | {_fmt(summary.get('prompt_tokens'))} |",
        f"| completion_tokens | {_fmt(summary.get('completion_tokens'))} |",
        f"| total_tokens | {_fmt(summary.get('total_tokens'))} |",
        f"| cached_tokens | {_fmt(summary.get('cached_tokens'))} |",
        f"| cache_hit_rate | {_fmt(summary.get('cache_hit_rate'))} |",
        f"| recompute_tokens | {_fmt(summary.get('recompute_tokens'))} |",
        f"| avg_latency_ms | {_fmt(summary.get('avg_latency_ms'))} |",
        f"| p50_latency_ms | {_fmt(summary.get('p50_latency_ms'))} |",
        f"| p95_latency_ms | {_fmt(summary.get('p95_latency_ms'))} |",
        f"| std_latency_ms | {_fmt(summary.get('std_latency_ms'))} |",
        f"| max_latency_ms | {_fmt(summary.get('max_latency_ms'))} |",
        f"| peak_rss_mb | {_fmt(summary.get('peak_rss_mb'))} |",
        f"| peak_gpu_mb | {_fmt(summary.get('peak_gpu_mb'))} |",
    ]
    if ev:
        lines.append("")
        lines.append("**任务判据（保真约束）**")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("|---|---|")
        for k, v in sorted(ev.items()):
            lines.append(f"| {k} | {_fmt(v)} |")
    lines.append("")
    return "\n".join(lines)


def generate_markdown_report(result: Dict[str, Any],
                             baseline: Optional[Dict[str, Any]] = None) -> str:
    """生成 markdown 报告文本。``baseline`` 预留用于 diff（E0 未启用）。"""
    config = result.get("config", {})
    summary = result.get("summary", {})
    lines = [
        "# Benchmark 实验报告",
        "",
        f"- 生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 场景: `{config.get('scenario', '?')}`",
        f"- repeat: `{config.get('repeat', 1)}` / warmup: `{config.get('warmup', 0)}`",
        f"- server: `{config.get('base_url') or config.get('server_url', '?')}` / ctx: `{config.get('ctx_size', '?')}`",
        f"- temperature: `{config.get('temperature', 0.0)}` / seed: `{config.get('seed', '?')}`",
        "",
        "## 1. 运行配置",
        "",
        _config_table(config),
        "",
        "## 2. 场景指标",
        "",
    ]
    for name, sm in summary.items():
        lines.append(_scenario_table(name, sm))
    if baseline is not None:  # pragma: no cover — E0 预留
        lines.append("## 3. Baseline 对比（预留）")
        lines.append("_对比功能将在后续阶段实现（--compare）。_")
        lines.append("")
    return "\n".join(lines)


def write_report(path: str, result: Dict[str, Any],
                 baseline: Optional[Dict[str, Any]] = None) -> str:
    import os
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(generate_markdown_report(result, baseline))
    return path
