"""端到端冒烟：mock OpenAI server + 真实 HTTP 链路（Driver / cli_main）。

验证 E0 关键开放点：llama-server 风格的 timings 字段能否经 openai SDK
从 OAI 兼容响应中解析出来（_extract_timings 的 model_extra 路径）。
"""
from __future__ import annotations

import json
import os

from tests.mock_server import MockOpenAIServer
from framework.driver import Driver
from runner.runner import cli_main


def test_driver_http_timings_extraction():
    with MockOpenAIServer() as srv:
        drv = Driver(base_url=f"http://127.0.0.1:{srv.port}/v1")
        row = drv.chat([{"role": "user", "content": "hi"}])
    # 旧字段
    assert row["prompt_tokens"] == 100
    assert row["cached_tokens"] == 40
    assert row["completion_tokens"] == 10
    # E0 新字段：timings 经真实 HTTP + SDK 解析成功（开放问题 1 的验证）
    assert row["timings"] is not None, "openai SDK 未透出 timings 字段"
    assert row["timings"]["prompt_n"] == 60
    assert row["timings"]["cache_n"] == 40
    assert row["timings"]["predicted_per_second"] == 100.0
    # prompt_n + cache_n == prompt_tokens（llama.cpp 语义）
    assert row["timings"]["prompt_n"] + row["timings"]["cache_n"] == row["prompt_tokens"]


def test_cli_main_end_to_end(tmp_path):
    with MockOpenAIServer() as srv:
        out_dir = str(tmp_path / "results")
        report_path = str(tmp_path / "report.md")
        rc = cli_main([
            "--scenario", "multi_turn",
            "--rounds", "3",
            "--port", str(srv.port),
            "--host", "127.0.0.1",
            "--output-dir", out_dir,
            "--report", report_path,
        ])
    assert rc == 0
    # 结果 JSON 结构与旧脚本一致
    files = [f for f in os.listdir(out_dir) if f.startswith("bench_")]
    assert len(files) == 1
    with open(os.path.join(out_dir, files[0]), encoding="utf-8") as f:
        result = json.load(f)
    # E0.6：结果含 metadata 顶层键（模型哈希 / llama.cpp commit / GPU / ctx 检查）
    assert set(result) == {"config", "metadata", "summary", "scenarios"}
    assert set(result["scenarios"]) == {"multi_turn"}
    assert result["summary"]["multi_turn"]["rounds"] == 3
    # metadata：mock server 探测到 slot n_ctx=512，且 config ctx=2048 → warning
    meta = result["metadata"]
    assert meta["server"]["slot_n_ctx"] == 512
    assert meta["server"]["total_slots"] == 4
    assert any("slot n_ctx" in w for w in meta["warnings"])
    # config 含旧 CLI 键
    for k in ["host", "port", "scenario", "rounds", "ctx_size"]:
        assert k in result["config"]
    # markdown 报告生成
    assert os.path.exists(report_path)
    with open(report_path, encoding="utf-8") as f:
        md = f.read()
    assert "# Benchmark 实验报告" in md
    assert "cache_hit_rate" in md


def test_cli_main_yaml_config(tmp_path):
    import yaml
    cfg_path = tmp_path / "cfg.yaml"
    cfg = {"scenario": "branch", "branch_rounds": 2, "output_dir": str(tmp_path / "r2")}
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    with MockOpenAIServer() as srv:
        rc = cli_main(["--config", str(cfg_path), "--port", str(srv.port)])
    assert rc == 0
    files = os.listdir(str(tmp_path / "r2"))
    assert any(f.startswith("bench_") for f in files)
