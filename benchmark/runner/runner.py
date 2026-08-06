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
        self._last_protocol: Optional[Dict[str, Any]] = None

    # ---- 单场景执行 ----
    def _run_once(self, workload: Workload, spec) -> Dict[str, Any]:
        return workload.run(self.driver, spec)

    # ---- E2.0.5：replicate 协议 ----
    def _protocol_clean(self, probe) -> Dict[str, Any]:
        """independent 模式：清除所有 slot KV 并断言 /metrics/kv 清洁。

        返回协议记录 dict：clean_verified / initial_used_cells / attempts / erased。
        """
        rec: Dict[str, Any] = {"clean_verified": False, "initial_used_cells": None,
                               "attempts": 0, "erased": 0}
        if self.config.kv_clean == "erase":
            n_attempt, n_ok = probe.clean_all_slots()
            rec["attempts"] = n_attempt
            rec["erased"] = n_ok
        state = probe.kv_state()
        if state is not None:
            rec["initial_used_cells"] = state.get("used_cells")
            rec["clean_verified"] = (
                state.get("used_cells") == 0 and state.get("active_sequences") == 0)
        return rec

    def run_scenario(self, workload: Workload) -> Dict[str, Any]:
        """执行一个场景（warmup + repeat），返回场景结果对象。

        repeat 语义（E2.0.5 起区分两套协议）：
        - replicate_mode=independent：每个正式 replicate 前清除 KV 并断言 used_cells=0 /
          active_sequences=0；断言失败则该 replicate 标记 valid=False（数据保留，
          但不计入独立统计聚合）；
        - replicate_mode=soak：多个 cycle 共享 KV（记录初始 used_cells，不视为独立重复）；
        - replicate_mode=auto：旧行为（repeat 共享 KV）。
        """
        params = workload.params_from_config(self.config)
        spec = workload.generate(params)
        print(f"== {SCENARIO_TITLES.get(workload.name, workload.name)} "
              f"(params={params}) ==")

        # KV probe（若启用）：warmup 阶段暂停采集，正式 run 打 run_id 边界
        probe = getattr(self.driver, "kv_probe", None)

        if probe is not None:
            probe.set_collecting(False)
        # warmup：结果丢弃（不计入统计）；KV 预热残留由协议层在下个 replicate 前清除
        for _ in range(self.config.warmup):
            self._run_once(workload, spec)
        if probe is not None:
            probe.set_collecting(True)

        runs = []
        protocol: Dict[str, Any] = {"mode": self.config.replicate_mode}
        if self.config.replicate_mode == "soak":
            protocol["note"] = ("cycles share KV across repeats; "
                                "initial_used_cells recorded per cycle; "
                                "NOT independent replicates")
        records = []
        for i in range(self.config.repeat):
            rec: Dict[str, Any] = {"run_id": f"{workload.name}_{i}",
                                   "cycle_id": i,
                                   "independent": self.config.replicate_mode == "independent",
                                   "clean_verified": None,
                                   "initial_used_cells": None,
                                   "valid": True}
            if self.config.replicate_mode == "independent" and probe is not None:
                rec.update(self._protocol_clean(probe))
                rec["valid"] = bool(rec["clean_verified"])
            elif self.config.replicate_mode == "soak" and probe is not None:
                state = probe.kv_state()
                rec["independent"] = False
                if state is not None:
                    rec["initial_used_cells"] = state.get("used_cells")
            if probe is not None:
                probe.begin_run(rec["run_id"])
            runs.append(self._run_once(workload, spec))
            if probe is not None:
                probe.end_run()
            records.append(rec)
        protocol["replicates"] = records
        protocol["valid_count"] = sum(1 for r in records if r["valid"])
        if self.config.repeat == 1:
            # 保持旧返回结构（兼容 run()/_build_summary 解包）
            self._last_protocol = protocol
            return runs[0]
        return {"runs": runs, "protocol": protocol}

    # ---- 汇总 ----
    def _build_summary(self, workload: Workload, result: Dict[str, Any]) -> Dict[str, Any]:
        # E2.0.5：independent 协议下 clean 失败的 replicate（valid=False）不计入独立统计
        protocol = result.get("protocol") if self.config.repeat > 1 else self._last_protocol
        valid_indices: Optional[List[int]] = None
        if protocol and protocol.get("mode") == "independent" and self.config.repeat > 1:
            valid_indices = [i for i, r in enumerate(protocol.get("replicates", []))
                             if r.get("valid")]
            if len(valid_indices) != self.config.repeat:
                print(f"  [WARNING] {workload.name}: {self.config.repeat - len(valid_indices)} "
                      f"replicate(s) 未通过 KV 清洁断言，已从独立统计中排除 "
                      f"(valid={len(valid_indices)}/{self.config.repeat})")
        if self.config.repeat == 1:
            rows = workload.rows_for_summary(result)
            summary = summarize(rows)
        else:
            runs = result["runs"]
            chosen = runs if valid_indices is None else [runs[i] for i in valid_indices]
            run_summaries = [
                summarize(workload.rows_for_summary(r)) for r in chosen
            ]
            summary = self._aggregate_summaries(run_summaries)
        # 任务判据（evaluate）：repeat>1 时对每个 run 分别判定并聚合数值键
        spec = workload.generate(workload.params_from_config(self.config))
        if self.config.repeat == 1:
            summary["evaluation"] = workload.evaluate(result, spec)
        else:
            runs = result["runs"]
            chosen = runs if valid_indices is None else [runs[i] for i in valid_indices]
            evals = [workload.evaluate(r, spec) for r in chosen]
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
        protocols: Dict[str, Any] = {}
        for workload in select_workloads(self.config.scenario):
            result = self.run_scenario(workload)
            if self.config.repeat == 1:
                results[workload.name] = result["rows"]
            else:
                results[workload.name] = result
            summary[workload.name] = self._build_summary(workload, result)
            if self.config.repeat > 1:
                protocols[workload.name] = result.get("protocol")
            elif self._last_protocol is not None:
                protocols[workload.name] = self._last_protocol
        out: Dict[str, Any] = {"config": self.config.to_dict(),
                               "summary": summary, "scenarios": results}
        # 仅显式协议（independent/soak）或 repeat>1 时挂载 protocol；auto+repeat=1 保持旧结构
        if protocols and (self.config.replicate_mode != "auto" or self.config.repeat > 1):
            out["protocol"] = protocols
        return out

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
    ap.add_argument("--kv-probe", action="store_true", default=None,
                    help="E1：启用 /metrics/kv 快照采集（请求前后 + 开始/结束）")
    ap.add_argument("--kv-probe-interval", type=float, default=None,
                    help="E1：KV 周期采样间隔秒（0 = 不周期采样，默认）")
    ap.add_argument("--replicate-mode", choices=["auto", "independent", "soak"], default=None,
                    help="E2.0.5：repeat 协议（independent=每 replicate 前清 KV 并断言清洁；"
                         "soak=cycle 共享 KV 记录初始 used_cells；auto=旧行为）")
    ap.add_argument("--kv-clean", choices=["erase", "none"], default=None,
                    help="E2.0.5：independent 模式 KV 清洁方式（erase=POST /slots/{id}?action=erase）")
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
        "kv_probe_enabled": args.kv_probe, "kv_probe_interval": args.kv_probe_interval,
        "replicate_mode": args.replicate_mode, "kv_clean": args.kv_clean,
    }
    config = config.merge_cli(cli_vals)

    # E1：KV probe（可选，endpoint 不可用时自动降级，不阻塞实验）
    probe = None
    if config.kv_probe_enabled:
        from framework.kv_probe import KVProbe
        probe = KVProbe(config.base_url)
        probe.snapshot(tag="start")
        probe.start_periodic(config.kv_probe_interval)
        driver = Driver(
            base_url=config.base_url,
            model=config.model,
            host=config.host,
            port=config.port,
            enable_thinking=config.enable_thinking,
            timings_per_token=config.timings_per_token,
            kv_probe=probe,
        )
        runner = Runner(config, driver=driver)
    else:
        runner = Runner(config)

    result = runner.run()
    if probe is not None:
        probe.snapshot(tag="end")
        probe.stop()
        result["kv_observations"] = probe.to_dict()
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
