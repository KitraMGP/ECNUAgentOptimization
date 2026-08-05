#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_bench.py — Agent 工作流内存/延迟基准测试（基线 & 优化后对比共用）

覆盖赛题点名的三类典型 agent 场景：
  1. multi_turn  多轮对话    : 上下文逐轮累积 -> KV Cache 持续增长
  2. tool_call   工具调用    : 工具返回大段 JSON 回填上下文 -> 上下文冗余膨胀
  3. branch      多路径决策  : 同一前缀派生多个分支 -> 分支内存共享/COW 场景

用法（在 benchmark/ 目录下，venv 内）:
  uv run python agent_bench.py --scenario multi_turn --rounds 20
  uv run python agent_bench.py --scenario tool_call --tool-steps 6
  uv run python agent_bench.py --scenario branch --branch-rounds 5
  uv run python agent_bench.py --scenario all
输出: benchmark/results/<scenario>_<时间戳>.json
"""
import argparse, json, os, re, time, datetime
from openai import OpenAI
import psutil

SYSTEM = "你是乐于助人的中文助手。回答简洁准确。"

# ---- 进程采样（CPU RSS + GPU 显存，自动适配 CPU/GPU 运行环境） ----
def find_server_pid(host="127.0.0.1", port=8080):
    """定位监听 host:port 的 llama-server 进程 PID（按 cmdline 匹配 + 端口过滤，避免多实例误匹配）。"""
    # 优先：ss -ltnp 找监听端口的 PID（最可靠）
    try:
        import subprocess
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            if f":{port}" in line and "llama-server" in line:
                pid = line.split("pid=")[1].split(",")[0]
                return int(pid)
    except Exception:
        pass
    # 兜底：遍历进程，按 cmdline 含 llama-server 匹配
    for p in psutil.process_iter(["cmdline"]):
        try:
            cl = p.info["cmdline"] or []
            if cl and any("llama-server" in c for c in cl):
                return p.pid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None

_server_pid = None

def get_server_pid():
    global _server_pid
    return _server_pid

def find_server_rss_mb(pid):
    if pid is None:
        return None
    try:
        return round(psutil.Process(pid).memory_info().rss / 1024 / 1024, 1)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None

def find_server_gpu_mb(pid):
    """nvidia-ml-py 按 PID 采样 llama-server 占用的显存；无 GPU/驱动时返回 None"""
    if pid is None:
        return None
    try:
        import pynvml  # nvidia-ml-py 提供顶层 pynvml 模块（非弃用旧包）
        pynvml.nvmlInit()
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            for proc in nvml.nvmlDeviceGetComputeRunningProcesses(h):
                if proc.pid == pid:
                    return round(proc.usedGpuMemory / 1024 / 1024, 1)
    except Exception:
        pass
    return None

def mem_str(r):
    parts = [f"RSS={r['rss_mb']}MB"]
    if r.get("gpu_mb") is not None:
        parts.append(f"GPU={r['gpu_mb']}MB")
    return " ".join(parts)

def make_client(host, port):
    # llama-server 提供 OpenAI 兼容 API；api_key 传任意值即可（server 默认不鉴权）
    global _server_pid
    _server_pid = find_server_pid(host, port)  # 建连时即定位 server PID（按端口过滤）
    return OpenAI(base_url=f"http://{host}:{port}/v1", api_key="EMPTY")

def chat(client, messages, _retry=0):
    """调用 chat.completions；若 400 超出 ctx，自动丢弃最早的非 system 消息后重试（最多 3 次）。"""
    try:
        t0 = time.perf_counter()
        resp = client.chat.completions.create(
            model="bench",  # llama-server 不校验模型名
            messages=messages,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},  # 固定 no-think，保证可比
        )
        ms = (time.perf_counter() - t0) * 1000
        u = resp.usage
        ptd = getattr(u, "prompt_tokens_details", None)
        cached = getattr(ptd, "cached_tokens", 0) if ptd else 0
        pid = get_server_pid()
        return {"text": resp.choices[0].message.content or "",
                "prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "total_tokens": u.total_tokens,
                "cached_tokens": cached or 0,
                "latency_ms": round(ms, 1),
                "rss_mb": find_server_rss_mb(pid),
                "gpu_mb": find_server_gpu_mb(pid)}
    except Exception as e:
        # 400: 请求超出 ctx —— 兜底：丢弃最早的非 system 消息后重试
        if "exceed_context_size_error" in str(e) and _retry < 3 and len(messages) > 2:
            # 保留 system（若有）与最近的消息，丢弃最早的一批
            body = [m for m in messages if m["role"] == "system"]
            rest = [m for m in messages if m["role"] != "system"]
            # 一次丢弃约 1/3 的非 system 消息，加速收敛
            drop = max(1, len(rest) // 3)
            messages = body + rest[drop:]
            print(f"    [chat 400 兜底] 丢弃 {drop} 条最早消息后重试 (第 {_retry + 1}/3 次)")
            return chat(client, messages, _retry + 1)
        raise

# ---- 场景 1：多轮对话 ----
POOL = ["解释一下什么是 KV Cache。",
        "写一句押韵的中文诗。",
        "把'今天天气很好'翻译成英文。",
        "3.14 乘以 2 等于多少？",
        "用一句话介绍长沙。",
        "什么是 Copy-on-Write？",
        "推荐一本编程入门书。",
        "计算 15 + 27 并解释过程。"]

def scenario_multi_turn(client, rounds):
    history = [{"role": "system", "content": SYSTEM}]
    rows = []
    for i in range(rounds):
        q = POOL[i % len(POOL)]
        history.append({"role": "user", "content": q})
        r = chat(client, history)
        history.append({"role": "assistant", "content": r["text"]})
        rows.append({"round": i + 1, **r})
        print(f"  [回合{i+1:>3}/{rounds}] prompt={r['prompt_tokens']:>5} "
              f"total={r['total_tokens']:>6} 延迟={r['latency_ms']:>7}ms {mem_str(r)}")
    return rows

# ---- 场景 2：工具调用（文本 ACTION 协议，最稳） ----
TOOL_SYSTEM = """你是智能客服助手，可调用以下工具：
- search_orders(customer_id)：查询客户订单列表（返回订单ID列表）
- get_order_detail(order_id)：查询单个订单的完整详情
处理流程（必须严格遵守）：
1. 先调用 search_orders(customer_id) 获取订单列表；
2. 然后对列表中的【每一个】订单依次调用 get_order_detail(order_id)；
3. 全部查询完成后，才输出最终汇总答案。
需要调用工具时，只输出一行：ACTION: 工具名(参数)。收到工具结果后继续下一步。
调用格式示例（参数必须填真实值，不要写参数名）：
- ACTION: search_orders(customer_id=C10086)
- ACTION: get_order_detail(order_id=SO00000)"""

MOCK_TOOLS = {
    "search_orders": lambda cid: json.dumps(
        {"customer": cid,
         "order_ids": [f"SO{i:05d}" for i in range(8)]},
        ensure_ascii=False, indent=2),
    "get_order_detail": lambda oid: json.dumps(
        {"order_id": oid,
         "items": [{"name": f"SKU{j}", "qty": j + 1, "price": round(25.5 * j, 2)}
                   for j in range(6)],
         "address": "湖南省长沙市开福区三一大道 500 号 8 栋 1203 室",
         "logistics": [{"time": f"2026-03-0{i+1} 10:0{i}", "location": "长沙转运中心",
                        "status": "已揽收"} for i in range(5)]},
        ensure_ascii=False, indent=2),
}

def scenario_tool_call(client, steps):
    messages = [{"role": "system", "content": TOOL_SYSTEM},
                {"role": "user", "content": "请查询客户 C10086 的订单，汇总每个订单的金额与状态。"}]
    rows = []
    for i in range(steps):
        r = chat(client, messages)
        m = re.search(r"ACTION:\s*(\w+)\(([^)]*)\)", r["text"])
        rows.append({"step": i + 1, "action": m.group(1) if m else None, **r})
        if not m:
            break  # 模型已给出最终答案
        messages.append({"role": "assistant", "content": r["text"]})
        messages.append({"role": "user",
                         "content": f"<tool_response>\n{MOCK_TOOLS[m.group(1)](m.group(2))}\n</tool_response>"})
        print(f"  [工具步{i+1:>3}/{steps}] 调用={m.group(1)} "
              f"prompt={r['prompt_tokens']:>5} total={r['total_tokens']:>6} "
              f"延迟={r['latency_ms']:>7}ms {mem_str(r)}")
    return rows

# ---- 场景 3：分支推理（同一前缀派生多分支） ----
def scenario_branch(client, branch_rounds):
    prefix = [{"role": "system", "content": SYSTEM},
              {"role": "user", "content": "我计划周末去张家界旅游，请先给出总体思路。"}]
    r0 = chat(client, prefix)
    common = prefix + [{"role": "assistant", "content": r0["text"]}]
    print(f"  [公共前缀] prompt={r0['prompt_tokens']:>5} total={r0['total_tokens']:>6} "
          f"{mem_str(r0)}")
    branches = {}
    for name, q in [("A", "请详细推演方案A的具体行程与预算。"),
                    ("B", "请详细推演方案B的具体行程与预算。")]:
        msgs = list(common) + [{"role": "user", "content": q}]
        rows = []
        for i in range(branch_rounds):
            r = chat(client, msgs)
            rows.append({"sub_round": i + 1, **r})
            msgs.append({"role": "assistant", "content": r["text"]})
            msgs.append({"role": "user", "content": f"继续完善方案{name}，下一步怎么做？"})
            print(f"  [分支{name} 第{i+1:>3}/{branch_rounds}] prompt={r['prompt_tokens']:>5} "
                  f"total={r['total_tokens']:>6} {mem_str(r)}")
        branches[name] = rows
    return {"common": r0, "branches": branches}

def scenario_long_life(client, rounds=40, secret="9527", ctx_size=2048, reserve=300):
    """长生命周期场景：多轮对话 + 工具调用穿插，验证早期上下文在上下文窗口管理下的保留情况。

    关键点：
    - 第 5 轮让模型记住一个秘密数字
    - 每 4 轮穿插一次大段工具结果回填（模拟 agent 工作负载）
    - 历史接近 ctx 上限时做应用层截断（模拟真实 agent 的上下文窗口管理）：
      截断会丢失早期 KV 前缀 -> cached_tokens 骤降 -> 触发重算（延迟上升）
    - 最后一轮提问秘密数字 -> 若早期关键上下文被截断丢弃，回答错误（任务成功率下降）
    - 需 server 以相同 ctx（--ctx-size，默认 2048）启动
    """
    threshold = ctx_size - 500
    keep_msgs = 6  # 截断后保留的最近消息条数（system 除外）
    history = [{"role": "system", "content": SYSTEM}]
    rows = []
    truncations = 0
    secret_round = min(5, max(1, rounds - 3))
    for i in range(rounds):
        n = i + 1

        # ---- 上下文窗口管理：历史接近 ctx 上限时截断早期消息 ----
        # 用上一轮 total（prompt+completion）近似当前 history 大小，比 prompt_tokens 更准确
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
            r = chat(client, tool_msgs)
            m = re.search(r"ACTION:\s*(\w+)\(([^)]*)\)", r["text"])
            if m:
                tool_msgs.append({"role": "assistant", "content": r["text"]})
                tool_msgs.append({"role": "user",
                                  "content": f"<tool_response>\n{MOCK_TOOLS[m.group(1)](m.group(2))}\n</tool_response>"})
                r2 = chat(client, tool_msgs)
                rows.append({"round": n, "kind": "tool", "tool": m.group(1), **r2})
                # 只追加本轮新增的交互，绝不复制旧 history（避免二次方膨胀）
                history.append({"role": "user", "content": "请查询客户 C10086 的最新一笔订单详情。"})
                history.append({"role": "assistant", "content": r2["text"]})
                print(f"  [轮{n:>3}/{rounds} 工具] 调用={m.group(1)} prompt={r2['prompt_tokens']:>5} "
                      f"cached={r2['cached_tokens']:>4} 延迟={r2['latency_ms']:>7}ms {mem_str(r2)}")
                continue
            q = POOL[i % len(POOL)]
        else:
            q = POOL[i % len(POOL)]

        history.append({"role": "user", "content": q})
        r = chat(client, history)
        rows.append({"round": n, **r})
        history.append({"role": "assistant", "content": r["text"]})
        print(f"  [轮{n:>3}/{rounds}] prompt={r['prompt_tokens']:>5} cached={r['cached_tokens']:>4} "
              f"total={r['total_tokens']:>6} 延迟={r['latency_ms']:>7}ms {mem_str(r)}")

    last_text = rows[-1].get("text", "") or ""
    success = secret in last_text
    print(f"  [任务成功率] 秘密数字 '{secret}' 是否在最终回答中: {success}")
    print(f"  [截断次数] {truncations}")
    return rows, success, truncations

# ---- 汇总与落盘 ----
def summarize(rows):
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for r in rows:
        for k in totals:
            totals[k] += r[k]
    return {**totals,
            "rounds": len(rows),
            "avg_latency_ms": round(sum(r["latency_ms"] for r in rows) / len(rows), 1),
            "max_latency_ms": max(r["latency_ms"] for r in rows),
            "peak_rss_mb": max((r["rss_mb"] for r in rows if r["rss_mb"]), default=None),
            "peak_gpu_mb": max((r["gpu_mb"] for r in rows if r.get("gpu_mb")), default=None)}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--scenario", choices=["multi_turn", "tool_call", "branch", "long_life", "all"], default="all")
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--tool-steps", type=int, default=6)
    ap.add_argument("--branch-rounds", type=int, default=5)
    ap.add_argument("--long-rounds", type=int, default=40, help="long_life 场景轮数（需 server 小 ctx 启动触发 KV 回收）")
    ap.add_argument("--long-secret", default="9527", help="long_life 场景的测试记忆数字")
    ap.add_argument("--ctx-size", type=int, default=2048, help="llama-server 的上下文长度（需与 server --ctx-size 一致）")
    args = ap.parse_args()
    client = make_client(args.host, args.port)
    results, summary = {}, {}

    if args.scenario in ("multi_turn", "all"):
        print(f"== 场景1 多轮对话 ({args.rounds} 轮) ==")
        rows = scenario_multi_turn(client, args.rounds)
        results["multi_turn"], summary["multi_turn"] = rows, summarize(rows)
    if args.scenario in ("tool_call", "all"):
        print(f"== 场景2 工具调用 ({args.tool_steps} 步) ==")
        rows = scenario_tool_call(client, args.tool_steps)
        results["tool_call"], summary["tool_call"] = rows, summarize(rows)
    if args.scenario in ("branch", "all"):
        print(f"== 场景3 分支推理 (每分支 {args.branch_rounds} 轮) ==")
        data = scenario_branch(client, args.branch_rounds)
        results["branch"] = data
        all_rows = data["branches"]["A"] + data["branches"]["B"] + [data["common"]]
        summary["branch"] = summarize(all_rows)

    if args.scenario == "long_life":
        print(f"== 场景4 长生命周期 ({args.long_rounds} 轮, 秘密数字={args.long_secret}, ctx={args.ctx_size}) ==")
        print("  (历史超过 ctx 上限时脚本做应用层截断，模拟真实 agent 的上下文窗口管理)")
        rows, success, truncations = scenario_long_life(client, args.long_rounds, args.long_secret, args.ctx_size)
        results["long_life"], summary["long_life"] = rows, summarize(rows)
        summary["long_life"]["task_success"] = success
        summary["long_life"]["cached_tokens_total"] = sum(r.get("cached_tokens", 0) for r in rows)
        summary["long_life"]["truncations"] = truncations

    os.makedirs("results", exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join("results", f"bench_{ts}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"config": vars(args), "summary": summary, "scenarios": results},
                  f, ensure_ascii=False, indent=2)
    print(f"\n== 汇总 ==")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n结果已保存: {out}")

if __name__ == "__main__":
    main()