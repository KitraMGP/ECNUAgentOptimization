"""Runner 编排测试：FakeDriver 全链路（结果 JSON 结构 = 旧格式超集）。"""
from __future__ import annotations

from tests.conftest import FakeDriver
import workload  # noqa: F401  (触发注册)
from framework.config import BenchmarkConfig
from runner.runner import Runner


def test_run_all_legacy_shape(fake_driver):
    cfg = BenchmarkConfig(scenario="all", rounds=3, tool_steps=3,
                          branch_rounds=2, repeat=1, warmup=0)
    result = Runner(cfg, driver=fake_driver).run()
    assert set(result) == {"config", "summary", "scenarios"}
    # all 只含前三个场景（与旧脚本一致，不含 long_life）
    assert set(result["scenarios"]) == {"multi_turn", "tool_call", "branch"}

    sm = result["summary"]["multi_turn"]
    for k in ["prompt_tokens", "completion_tokens", "total_tokens", "rounds",
              "avg_latency_ms", "max_latency_ms", "peak_rss_mb", "peak_gpu_mb"]:
        assert k in sm, f"summary 缺旧字段 {k}"
    assert sm["rounds"] == 3
    assert "evaluation" in sm

    # config 保留旧 CLI 键（超集）
    cfg_keys = result["config"]
    for k in ["host", "port", "scenario", "rounds", "tool_steps",
              "branch_rounds", "long_rounds", "long_secret", "ctx_size"]:
        assert k in cfg_keys, f"config 缺旧键 {k}"


def test_run_long_life_legacy_fields(fake_driver):
    cfg = BenchmarkConfig(scenario="long_life", long_rounds=6, ctx_size=2048)
    result = Runner(cfg, driver=fake_driver).run()
    assert "long_life" in result["scenarios"]
    sm = result["summary"]["long_life"]
    assert sm["task_success"] in (True, False)
    assert "truncations" in sm
    assert "cached_tokens_total" in sm
    assert sm["rounds"] == 6


def test_run_repeat_and_warmup(fake_driver):
    cfg = BenchmarkConfig(scenario="multi_turn", rounds=2, repeat=2, warmup=1)
    result = Runner(cfg, driver=fake_driver).run()
    sc = result["scenarios"]["multi_turn"]
    assert isinstance(sc, dict) and "runs" in sc
    assert len(sc["runs"]) == 2
    # warmup(1 次场景 × 2 轮) + repeat(2 次 × 2 轮) = 6 次 chat
    assert fake_driver.n_calls == 6
    sm = result["summary"]["multi_turn"]
    # 跨 run 聚合：数值键变为 {"mean","std","p50","p95"}
    assert isinstance(sm["total_tokens"], dict)
    assert "mean" in sm["total_tokens"]


def test_save_writes_json(tmp_path, fake_driver):
    cfg = BenchmarkConfig(scenario="multi_turn", rounds=2, output_dir=str(tmp_path))
    runner = Runner(cfg, driver=fake_driver)
    result = runner.run()
    path = runner.save(result)
    import json, os
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    assert set(saved) == {"config", "summary", "scenarios"}


def test_cli_all_scenario_selection():
    from runner.runner import select_workloads
    names = [w.name for w in select_workloads("all")]
    assert names == ["multi_turn", "tool_call", "branch"]
    assert [w.name for w in select_workloads("long_life")] == ["long_life"]
