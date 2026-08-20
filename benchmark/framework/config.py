"""BenchmarkConfig —— 实验配置系统（JSON / YAML / dict / CLI 参数统一入口）。

设计要点：
- 字段名与旧 agent_bench.py 的 argparse dest 保持一致（host/port/scenario/rounds/
  tool_steps/branch_rounds/long_rounds/long_secret/ctx_size），保证结果 JSON 的
  ``config`` 部分向后兼容（新框架字段为其超集）。
- 支持 JSON / YAML 配置文件，CLI 参数优先级高于配置文件。
- YAML 依赖 pyyaml：可用则启用，不可用时报出清晰错误（JSON 不受影响）。
"""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# 合法场景名（与旧 CLI choices 一致 + all）
SCENARIOS = ("multi_turn", "tool_call", "branch", "long_life", "realistic_agent", "all")

# 兼容旧 CLI 的字段（旧结果 JSON 的 config 键集）
_LEGACY_KEYS = (
    "host", "port", "scenario", "rounds", "tool_steps",
    "branch_rounds", "long_rounds", "long_secret", "ctx_size",
)


@dataclass
class BenchmarkConfig:
    # ---- server ----
    host: str = "127.0.0.1"
    port: int = 8080
    server_url: str = ""            # 显式 base_url；为空则由 host:port 推导
    model: str = "bench"            # llama-server 不校验模型名
    ctx_size: int = 2048            # llama-server 的上下文长度（需与 server --ctx-size 一致）
    model_path: str = ""            # GGUF 模型文件路径（可覆盖；空则从 /props 探测）
    parallel: int = 0               # server 并行 slot 数（0 = 从 /props 探测；与 ctx 平分语义相关）
    # ---- 实验控制 ----
    scenario: str = "all"           # multi_turn / tool_call / branch / long_life / all
    repeat: int = 1                 # 正式重复次数（repeat=1 时输出结构与旧脚本一致）
    warmup: int = 0                 # 预热轮数（不计入统计）
    seed: int = 42                  # 记录用；temperature=0 下 workload 生成器确定性由 seed 控制
    temperature: float = 0.0        # 推理温度（正式实验默认 0，见 AGENTS.md / 实现计划）
    # ---- 场景参数（与旧 CLI 同名）----
    rounds: int = 20
    tool_steps: int = 6
    branch_rounds: int = 5
    long_rounds: int = 40
    long_secret: str = "9527"
    realistic_rounds: int = 10
    realistic_payload_chars: int = 12000
    # ---- 输出 ----
    output_dir: str = "results"
    report_path: Optional[str] = None   # markdown 报告输出路径（None = 不生成）
    # ---- driver ----
    enable_thinking: bool = False       # 固定 no-think，保证可比（旧脚本固定 False）
    timings_per_token: bool = False     # 请求 timings_per_token（旧脚本未开启）
    kv_probe_enabled: bool = False      # E1：启用 /metrics/kv 快照采集（默认关，不改变现有行为）
    kv_probe_interval: float = 0.0      # E1：KV 周期采样间隔秒（0 = 不周期采样，仅请求前后快照）
    # ---- E2.0.5：replicate 协议 ----
    # auto = 旧行为（repeat 共享 KV，不保证独立）；
    # independent = 每个正式 replicate 前清除所有 slot KV 并断言清洁（不清洁则标记 invalid，不计入独立统计）；
    # soak = 多个 cycle 共享 KV（记录 cycle_id 与初始 used_cells，不视为独立重复）
    replicate_mode: str = "auto"
    kv_clean: str = "erase"             # independent 模式清洁方式：erase（/slots/{id}?action=erase）
    extra: Dict[str, Any] = field(default_factory=dict)   # 保留未知配置键（向前兼容）

    # ---- 构造与校验 ----
    def __post_init__(self) -> None:
        self.host = str(self.host)
        self.port = int(self.port)
        self.ctx_size = int(self.ctx_size)
        self.parallel = int(self.parallel)
        self.model_path = str(self.model_path)
        self.repeat = int(self.repeat)
        self.warmup = int(self.warmup)
        self.seed = int(self.seed)
        self.temperature = float(self.temperature)
        self.rounds = int(self.rounds)
        self.tool_steps = int(self.tool_steps)
        self.branch_rounds = int(self.branch_rounds)
        self.long_rounds = int(self.long_rounds)
        self.long_secret = str(self.long_secret)
        self.realistic_rounds = int(self.realistic_rounds)
        self.realistic_payload_chars = int(self.realistic_payload_chars)
        if self.scenario not in SCENARIOS:
            raise ValueError(
                f"scenario={self.scenario!r} 非法，可选: {', '.join(SCENARIOS)}")
        if self.repeat < 1:
            raise ValueError(f"repeat 必须 >= 1，当前 {self.repeat}")
        if self.warmup < 0:
            raise ValueError(f"warmup 必须 >= 0，当前 {self.warmup}")
        if self.ctx_size <= 0:
            raise ValueError(f"ctx_size 必须 > 0，当前 {self.ctx_size}")
        if self.realistic_rounds < 7 or self.realistic_payload_chars < 256:
            raise ValueError("realistic_agent 参数不足：realistic_rounds>=7 且 payload_chars>=256")

    @property
    def base_url(self) -> str:
        """OpenAI 兼容 API base_url（/v1 结尾）。"""
        if self.server_url:
            return self.server_url.rstrip("/")
        return f"http://{self.host}:{self.port}/v1"

    # ---- 序列化 ----
    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d.pop("extra", None)
        # 派生字段：实际使用的 base_url（server_url 保留原始值，避免 merge 时固化）
        d["base_url"] = self.base_url
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BenchmarkConfig":
        """从 dict 构造；已知字段赋给对应成员，未知字段保留到 extra。"""
        known = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        extra = {k: v for k, v in data.items() if k not in known}
        cfg = cls(**kwargs)
        cfg.extra = extra
        return cfg

    # ---- 文件加载 ----
    @classmethod
    def load(cls, path: str) -> "BenchmarkConfig":
        ext = os.path.splitext(path)[1].lower()
        with open(path, "r", encoding="utf-8") as f:
            if ext in (".yaml", ".yml"):
                try:
                    import yaml  # pyyaml（可选依赖）
                except ImportError as e:  # pragma: no cover
                    raise RuntimeError(
                        "加载 YAML 配置需要 pyyaml：请先 `uv add pyyaml` 或改用 JSON 配置。"
                    ) from e
                data = yaml.safe_load(f) or {}
            elif ext == ".json":
                data = json.load(f)
            else:
                raise ValueError(f"不支持的配置文件类型: {ext}（支持 .json / .yaml / .yml）")
        if not isinstance(data, dict):
            raise ValueError(f"配置文件必须包含顶层对象，实际为 {type(data).__name__}")
        return cls.from_dict(data)

    def merge_cli(self, cli: Dict[str, Any]) -> "BenchmarkConfig":
        """将 CLI 显式传入的参数覆盖到配置上（None 表示未传，保持原值）。"""
        data = self.to_dict()
        for k, v in cli.items():
            if v is not None:
                data[k] = v
        merged = self.from_dict(data)
        # 保留当前配置的 extra 再合并 CLI 传入的未知键
        merged.extra = {**self.extra, **cli.get("extra", {})}
        return merged

    def legacy_keys(self) -> Dict[str, Any]:
        """旧脚本结果 JSON 中 ``config`` 的键集（用于兼容性对比）。"""
        d = self.to_dict()
        return {k: d[k] for k in _LEGACY_KEYS if k in d}
