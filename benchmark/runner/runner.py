"""Runner —— 实验编排（场景 × repeat × warmup）与结果落盘。

行为与旧 agent_bench.py 的 main() 保持一致：
- ``--scenario all`` 只跑 multi_turn / tool_call / branch（long_life 需显式指定）；
- 结果 JSON 结构为 {"config": ..., "summary": ..., "scenarios": ...}；
- 输出文件 ``results/bench_<时间戳>.json``；
- repeat=1（默认）时 scenarios 与 summary 结构与旧脚本一致；
- repeat>1 时 scenarios[name] 展开为 {"runs": [...], "aggregate": {...}}，
  summary 追加跨 run 统计（新功能，默认关闭）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from typing import Any, Dict, List, Optional

from framework.config import BenchmarkConfig
from framework.driver import Driver
from framework.workload import Workload
from metrics.metrics import mean, p50, p95, std, summarize
from workload import all_workloads, get_workload

# 场景显示名（保持旧打印格式）
SCENARIO_TITLES = {
    "multi_turn": "场景1 多轮对话",
    "tool_call": "场景2 工具调用",
    "branch": "场景3 分支推理",
    "long_life": "场景4 长生命周期",
}

# all 的展开（与旧脚本一致：不含 long_life）
ALL_SCENARIOS = ["multi_turn", "tool_call", "branch"]


def select_workloads(scenario: str) -> List[Workload]:
    """按 --scenario 选择 workload（all = 前三个场景，与旧行为一致）。"""
    if scenario == "all":
        return [get_workload(n) for n in ALL_SCENARIOS]
    return [get_workload(scenario)]


class Runner:
    def __init__(self, config: BenchmarkConfig, driver: Optional[Driver] = None) -> None:
        self.config = config
        self.driver = driver or Driver(
            base_url=config.base_url,
            model=config.model,
            host=config.host,
            port=config.port,
            enable_thinking=config.enable_thinking,
            timings_per_token=config.timings_per_token,
        )

    # ---- 单场景执行 ----
    def _run_once(self, workload: Workload, spec) -> Dict[str, Any]:
        return workload.run(self.driver, spec)

    def run_scenario(self, workload: Workload) -> Dict[str, Any]:
        """执行一个场景（warmup + repeat），返回场景结果对象。"""
        params = workload.params_from_config(self.config)
        spec = workload.generate(params)
        print(f"== {SCENARIO_TITLES.get(workload.name, workload.name)} "
              f"(params={params}) ==")

        # warmup：结果丢弃（不计入统计）
        for _ in range(self.config.warmup):
            self._run_once(workload, spec)

        runs = [self._run_once(workload, spec) for _ in range(self.config.repeat)]
        if self.config.repeat == 1:
            return runs[0]
        return {"runs": runs}

    # ---- 汇总 ----
    def _build_summary(self, workload: Workload, result: Dict[str, Any]) -> Dict[str, Any]:
        if self.config.repeat == 1:
            rows = workload.rows_for_summary(result)
            summary = summarize(rows)
        else:
            run_summaries = [
                summarize(workload.rows_for_summary(r)) for r in result["runs"]
            ]
            summary = self._aggregate_summaries(run_summaries)
        # 任务判据（evaluate）：repeat>1 时对每个 run 分别判定并聚合数值键
        spec = workload.generate(workload.params_from_config(self.config))
        if self.config.repeat == 1:
            summary["evaluation"] = workload.evaluate(result, spec)
        else:
            evals = [workload.evaluate(r, spec) for r in result["runs"]]
            summary["evaluation"] = self._aggregate_evaluations(evals)
        # long_life 旧字段：task_success / cached_tokens_total / truncations（顶层，保持旧格式）
        if workload.name == "long_life":
            summary["task_success"] = summary["evaluation"]["task_success"]
            summary["truncations"] = summary["evaluation"].get("truncations", 0)
            if self.config.repeat == 1:
                rows = workload.rows_for_summary(result)
                summary["cached_tokens_total"] = sum(r.get("cached_tokens", 0) for r in rows)
        return summary

    @staticmethod
    def _aggregate_evaluations(evals: List[Dict[str, Any]]) -> Dict[str, Any]:
        """repeat>1 时：对多次运行的 evaluation 数值键聚合（mean/std/p50/p95）。

        用于 state_retention_rate 等 0/1 判据：跨 repeat 取均值即"率"。
        """
        if not evals:
            return {}
        agg: Dict[str, Any] = {}
        keys = list(evals[0].keys())
        for k in keys:
            vals = [e[k] for e in evals if e.get(k) is not None]
            if not vals:
                agg[k] = None
            elif all(isinstance(v, (int, float)) for v in vals):
                agg[k] = {
                    "mean": round(mean(vals), 4),
                    "std": round(std(vals), 4),
                    "p50": round(p50(vals), 4),
                    "p95": round(p95(vals), 4),
                }
            else:
                agg[k] = vals[0]
        return agg

    @staticmethod
    def _aggregate_summaries(run_summaries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """repeat>1 时：对每个数值键做 mean/std/p50/p95。"""
        agg: Dict[str, Any] = {}
        keys = list(run_summaries[0].keys()) if run_summaries else []
        for k in keys:
            vals = [s[k] for s in run_summaries if s.get(k) is not None]
            if not vals:
                agg[k] = None
            elif all(isinstance(v, (int, float)) for v in vals):
                agg[k] = {
                    "mean": round(mean(vals), 4) if len(vals) else None,
                    "std": round(std(vals), 4) if len(vals) else None,
                    "p50": round(p50(vals), 4) if len(vals) else None,
                    "p95": round(p95(vals), 4) if len(vals) else None,
                }
            else:
                agg[k] = vals[0]
        return agg

    # ---- 主流程 ----
    def run(self) -> Dict[str, Any]:
        results, summary = {}, {}
        for workload in select_workloads(self.config.scenario):
            result = self.run_scenario(workload)
            if self.config.repeat == 1:
                results[workload.name] = result["rows"]
            else:
                results[workload.name] = result
            summary[workload.name] = self._build_summary(workload, result)
        return {"config": self.config.to_dict(), "summary": summary, "scenarios": results}

    # ---- 落盘 ----
    def save(self, result: Dict[str, Any]) -> str:
        out_dir = self.config.output_dir
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out = os.path.join(out_dir, f"bench_{ts}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return out


# ---- CLI 入口（兼容旧 agent_bench.py 参数） ----
def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Agent 工作流内存/延迟基准测试（E0 框架版）")
    ap.add_argument("--config", default=None, help="JSON/YAML 配置文件路径（CLI 参数优先）")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--server-url", default=None, help="OpenAI 兼容 base_url（默认 http://host:port/v1）")
    ap.add_argument("--scenario",
                    choices=["multi_turn", "tool_call", "branch", "long_life", "all"],
                    default=None)
    ap.add_argument("--rounds", type=int, default=None)
    ap.add_argument("--tool-steps", type=int, default=None)
    ap.add_argument("--branch-rounds", type=int, default=None)
    ap.add_argument("--long-rounds", type=int, default=None)
    ap.add_argument("--long-secret", default=None)
    ap.add_argument("--ctx-size", type=int, default=None,
                    help="llama-server 的上下文长度（需与 server --ctx-size 一致）")
    ap.add_argument("--model-path", default=None, help="GGUF 模型文件路径（空则从 /props 探测）")
    ap.add_argument("--parallel", type=int, default=None,
                    help="server 并行 slot 数（0 = 探测；与 ctx-size 平分语义相关）")
    ap.add_argument("--repeat", type=int, default=None, help="正式重复次数（默认 1 = 旧行为）")
    ap.add_argument("--warmup", type=int, default=None, help="预热轮数（默认 0）")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--temperature", type=float, default=None, help="推理温度（正式实验默认 0）")
    ap.add_argument("--output-dir", default=None, help="结果输出目录（默认 results/）")
    ap.add_argument("--report", default=None, metavar="PATH",
                    help="生成 markdown 实验报告到指定路径")
    ap.add_argument("--timings-per-token", action="store_true", default=None,
                    help="请求 timings_per_token（llama-server 扩展字段）")
    return ap


def cli_main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.config:
        config = BenchmarkConfig.load(args.config)
    else:
        config = BenchmarkConfig()
    # CLI 显式参数覆盖配置文件
    cli_vals = {
        "host": args.host, "port": args.port, "server_url": args.server_url,
        "scenario": args.scenario, "rounds": args.rounds,
        "tool_steps": args.tool_steps, "branch_rounds": args.branch_rounds,
        "long_rounds": args.long_rounds, "long_secret": args.long_secret,
        "ctx_size": args.ctx_size, "repeat": args.repeat, "warmup": args.warmup,
        "seed": args.seed, "temperature": args.temperature,
        "output_dir": args.output_dir, "report_path": args.report,
        "timings_per_token": args.timings_per_token,
        "model_path": args.model_path, "parallel": args.parallel,
    }
    config = config.merge_cli(cli_vals)

    runner = Runner(config)
    result = runner.run()
    # E0.6：实验 metadata（模型哈希 / llama.cpp commit / GPU / ctx 语义检查）
    from framework.metadata import collect_metadata, probe_server
    server_info = probe_server(config.base_url)
    result["metadata"] = collect_metadata(config, server_info)
    for w in result["metadata"].get("warnings", []):
        print(f"  [WARNING] {w}")
    out = runner.save(result)
    print("\n== 汇总 ==")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"\n结果已保存: {out}")

    if config.report_path:
        from report.markdown import generate_markdown_report, write_report
        write_report(config.report_path, result)
        print(f"实验报告已保存: {config.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
