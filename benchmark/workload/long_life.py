"""long_life —— 长生命周期场景（多轮对话 + 工具穿插 + 应用层截断）。

迁移自旧 agent_bench.py 的 scenario_long_life，保持秘密数字埋点、工具穿插、
应用层截断逻辑、打印格式与结果行结构一致。

run() 返回 meta 携带旧 main() 单独使用的信息：
- task_success（秘密数字是否在最终回答中）
- truncations（截断次数）
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from framework.config import BenchmarkConfig
from framework.driver import Driver
from framework.sampler import mem_str
from framework.workload import Workload, WorkloadSpec, register

from .multi_turn import POOL, SYSTEM
from .tool_call import TOOL_SYSTEM, ToolPayloadRun, normalize_tool_payload_mode


@register
class LongLifeWorkload(Workload):
    name = "long_life"
    version = "1.0"
    description = "长生命周期：多轮对话+工具穿插+应用层截断，验证早期上下文保留。"

    def params_from_config(self, config: BenchmarkConfig) -> Dict[str, Any]:
        return {
            "rounds": config.long_rounds,
            "secret": config.long_secret,
            "ctx_size": config.ctx_size,
            "tool_payload_mode": normalize_tool_payload_mode(
                config.extra.get("tool_payload_mode")),
        }

    def generate(self, params: Dict[str, Any]) -> WorkloadSpec:
        return WorkloadSpec(
            name=self.name,
            params=dict(params),
            prompts=list(POOL),
            expected={"secret": params.get("secret", "9527")},
            meta={"version": self.version, "description": self.description},
        )

    def run(self, driver: Driver, spec: WorkloadSpec) -> Dict[str, Any]:
        rounds = spec.params["rounds"]
        secret = spec.params["secret"]
        ctx_size = spec.params.get("ctx_size", 2048)
        reserve = 300
        threshold = ctx_size - reserve
        keep_msgs = 6  # 截断后保留的最近消息条数（system 除外）
        history = [{"role": "system", "content": SYSTEM}]
        rows: List[dict] = []
        truncations = 0
        secret_round = min(5, max(1, rounds - 3))
        tp = ToolPayloadRun(spec.params.get("tool_payload_mode"))
        for i in range(rounds):
            n = i + 1

            # ---- 上下文窗口管理：历史接近 ctx 上限时截断早期消息 ----
            # 用上一轮 total（prompt+completion）近似当前 history 大小
            est = (rows[-1]["prompt_tokens"] + rows[-1]["completion_tokens"]) if rows else 0
            is_tool = (n % 4 == 0)
            # 工具轮额外预留：TOOL_SYSTEM 更长 + 工具查询消息
            budget = threshold - (500 if is_tool else 0)
            if est > budget and len(history) > keep_msgs + 1:
                dropped = len(history) - (keep_msgs + 1)
                history = history[:1] + history[-(keep_msgs):]
                truncations += 1
                print(f"  [截断 轮{n}] 历史约 {est} tokens 超过预算 {budget}，丢弃 {dropped} 条早期消息")

            if n == secret_round:
                q = f"请记住这个秘密数字：{secret}。只回答两个字：记住了。"
            elif n == rounds:
                q = "我之前让你记住的秘密数字是什么？请只回答数字本身。"
            elif n % 4 == 0:
                tool_msgs = [{"role": "system", "content": TOOL_SYSTEM}] + history[1:] + \
                            [{"role": "user", "content": "请查询客户 C10086 的最新一笔订单详情。"}]
                r = driver.chat(tool_msgs)
                # 局部工具消息同样遵守 one-shot restore 语义（r 请求返回后恢复 pending）
                tp.after_chat(tool_msgs)
                m = re.search(r"ACTION:\s*(\w+)\(([^)]*)\)", r["text"])
                if m:
                    # E15.2：工具轮与 tool_call 同一语义（resolve 拦截 / put / projection）
                    content, audit, placeholder = tp.tool_response(
                        m.group(1), m.group(2), r["text"])
                    tool_msgs.append({"role": "assistant", "content": r["text"]})
                    idx = len(tool_msgs)
                    full_content = f"<tool_response>\n{content}\n</tool_response>"
                    tool_msgs.append({"role": "user", "content": full_content})
                    if placeholder is not None:
                        tp.mark_pending(
                            idx, f"<tool_response>\n{placeholder}\n</tool_response>",
                            expected_content=full_content)
                    r2 = driver.chat(tool_msgs)
                    # 完整 payload 仅此一次局部请求可见；返回后原位恢复为 projection 占位
                    tp.after_chat(tool_msgs)
                    row: dict = {"round": n, "kind": "tool", "tool": m.group(1), **r2}
                    if audit is not None:
                        row["tool_payload"] = audit
                    rows.append(row)
                    # 只追加本轮新增的交互，绝不复制旧 history（避免二次方膨胀）
                    history.append({"role": "user", "content": "请查询客户 C10086 的最新一笔订单详情。"})
                    history.append({"role": "assistant", "content": r2["text"]})
                    print(
                        f"  [轮{n:>3}/{rounds} 工具] 调用={m.group(1)} prompt={r2['prompt_tokens']:>5} "
                        f"cached={r2['cached_tokens']:>4} 延迟={r2['latency_ms']:>7}ms {mem_str(r2)}"
                    )
                    continue
                q = POOL[i % len(POOL)]
            else:
                q = POOL[i % len(POOL)]

            history.append({"role": "user", "content": q})
            r = driver.chat(history)
            # 统一 one-shot 语义：普通轮也调用 after_chat(history)——
            # pending 不得跨轮残留（若有残留会在此 fail-fast，而非静默带入下一轮）
            tp.after_chat(history)
            rows.append({"round": n, **r})
            history.append({"role": "assistant", "content": r["text"]})
            print(
                f"  [轮{n:>3}/{rounds}] prompt={r['prompt_tokens']:>5} cached={r['cached_tokens']:>4} "
                f"total={r['total_tokens']:>6} 延迟={r['latency_ms']:>7}ms {mem_str(r)}"
            )

        last_text = rows[-1].get("text", "") or ""
        success = secret in last_text
        print(f"  [任务成功率] 秘密数字 '{secret}' 是否在最终回答中: {success}")
        print(f"  [截断次数] {truncations}")
        meta: Dict[str, Any] = {"task_success": success, "truncations": truncations}
        if tp.mode == "externalized":
            meta["tool_payload"] = tp.stats()
        return {"rows": rows, "meta": meta}

    def evaluate(self, results: Dict[str, Any], spec: WorkloadSpec) -> Dict[str, Any]:
        """长期 Agent 关键状态保持评估（E0.6）。

        - ``state_retention_rate``：主指标。会话注入的关键状态（secret）在
          最终询问中被正确召回的比例。单次运行取 0 或 1；跨 repeat 运行由
          runner 聚合为均值（即统计意义上的"保持率"）。
        - ``task_success``：仅作保真约束参考（不作为主指标），与
          state_retention_rate 等价（均基于 secret 召回）。
        - ``truncations``：应用层截断次数（上下文窗口管理压力）。
        """
        secret = spec.expected.get("secret", spec.params.get("secret", "9527"))
        meta = results.get("meta", {})
        recalled = bool(meta.get("task_success", False))
        retention = 1.0 if recalled else 0.0
        return {
            "state_retention_rate": retention,
            "task_success": recalled,
            "secret_recall": retention,
            "truncations": meta.get("truncations", 0),
        }
