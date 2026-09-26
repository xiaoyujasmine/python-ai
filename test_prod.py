"""
test_prod.py - 验证 `agentic_workflows_prod.py` 的补强是否真的生效。

两组测试：

  python3 test_prod.py --only fault   # 离线，不需要 key（默认也跑）
    用本地故障端点注入 429 / 500 / 超时 / 脏输出 / 坏 JSON，
    验证：重试能救回、救不回时降级不崩、限流真的生效、输出归一化正确。

  python3 test_prod.py --only real    # 需要 .env 里的 key
    跑真实模型，校验 7 条路由用例的 route 分支。

  python3 test_prod.py                # 两组都跑
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from typing import Dict

import aiohttp
from aiohttp import web

from env_loader import load_env_verbose

load_env_verbose()

import agentic_workflows_prod as prod
from agentic_workflows import ORIGINAL_PROMPTS
from mock_server import fake_model_reply

# mock_server 只认教程原文提示里的关键字，所以离线组（run_case）强制用
# original 变体；真实模型组保持 .env 的 PROMPT_VARIANT，两者不要混用

# 故障注入会产生大量 error 日志，测试期间静音，断言失败时再打印 decision
logging.getLogger("agentic_workflows").setLevel(logging.CRITICAL)


# --------------------------------------------------------------------------
# 故障端点
# --------------------------------------------------------------------------


def classify_route(messages) -> str:
    """判断这条请求属于三路中的哪一路（按 system prompt 关键字）。"""
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    if "schedule" in system:
        return "scheduling"
    if "price of a listing" in system:
        return "pricing"
    if "question about a listing" in system:
        return "listing"
    return "unknown"


def make_app(state: Dict) -> web.Application:
    async def handler(request: web.Request) -> web.Response:
        mode = request.match_info["mode"]
        body = await request.json()
        messages = body.get("messages", [])
        route_name = classify_route(messages)
        key = (mode, route_name)
        state["hits"][key] = state["hits"].get(key, 0) + 1
        hits = state["hits"][key]

        # 并发峰值观测
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])

        try:
            if mode == "slow":
                await asyncio.sleep(float(request.query.get("delay", "2")))

            if mode == "retry" and hits <= 2:
                return web.json_response(
                    {"error": {"message": "rate limited"}},
                    status=429,
                    headers={"Retry-After": "0"},
                )

            if mode == "dead":
                return web.json_response({"error": {"message": "boom"}}, status=500)

            # 只有 pricing 路挂：验证"跳过失败路、继续走后面的分支"
            if mode == "halfdead" and route_name == "pricing":
                return web.json_response({"error": {"message": "pricing down"}}, status=500)

            if mode == "garbage":
                return web.json_response({"not_choices": True}, status=200)

            content = fake_model_reply(messages)

            # 模拟模型跑偏：scheduling 路吐一整段自由文本（真实模型实测过）
            if mode == "chatty" and route_name == "scheduling":
                content = ("I'm sorry, but I don't have access to real-time data or the "
                           "ability to book appointments. Please contact the office directly.")

            # 模拟真实模型的脏输出：前导换行 + markdown 强调 + 尾随解释
            if mode == "dirty":
                content = f"\n**{content}**\n\n(Reasoning: the user asked about it.)"

            return web.json_response({
                "choices": [{"message": {"role": "assistant", "content": content}}]
            })
        finally:
            state["inflight"] -= 1

    app = web.Application()
    app.router.add_post("/{mode}/v1/chat/completions", handler)
    return app


async def start_fault_server():
    state = {"hits": {}, "inflight": 0, "peak": 0}
    runner = web.AppRunner(make_app(state))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = runner.addresses[0][1]
    except Exception:
        port = site._server.sockets[0].getsockname()[1]
    return runner, state, f"http://127.0.0.1:{port}"


# --------------------------------------------------------------------------
# 断言小工具
# --------------------------------------------------------------------------

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    return ok


PRICE_HISTORY = [
    {"role": "user", "content": "Hi, can you tell me the price of one of your listings?"},
]
PRICE_WITH_ID = [
    {"role": "user", "content": "Hi, can you tell me the price of one of your listings?"},
    {"role": "assistant", "content": "Could you please provide the listing_id of the item you're asking about?"},
    {"role": "user", "content": "Yes, the listing ID is 123456"},
]
SCHEDULE_HISTORY = [
    {"role": "user", "content": "I would like to schedule a call on 2026-10-01 at 15:00"},
]
LISTING_HISTORY = [
    {"role": "user", "content": "Is there parking near the listing?"},
]


async def run_case(history, **overrides):
    """跑一次 workflow（离线 mock 用），临时覆盖若干模块级配置。"""
    overrides.setdefault("SYSTEM_PROMPT_CONFIGURATIONS", ORIGINAL_PROMPTS)
    overrides.setdefault("STRICT_TOKENS", False)  # mock 走 original，允许自由文本
    saved = {k: getattr(prod, k) for k in overrides}
    for key, value in overrides.items():
        setattr(prod, key, value)
    try:
        return await prod.run_agentic_workflow_async(history)
    finally:
        for key, value in saved.items():
            setattr(prod, key, value)


# --------------------------------------------------------------------------
# 故障注入组
# --------------------------------------------------------------------------


async def test_faults(base_url: str, state: Dict, slow_url: str):
    print("\n=== 故障注入（离线，不需要 API key）===")

    # 1) 429 抖动：每路前 2 次限流，第 3 次成功 -> 重试必须救回
    decision = await run_case(
        PRICE_HISTORY,
        VLLM_SERVER_URL=f"{base_url}/retry/v1/chat/completions",
        MAX_RETRIES=2,
        RETRY_BASE_DELAY=0.01,
        RETRY_JITTER=0.0,
    )
    check("429 限流后重试成功（不降级、不抛异常）",
          (not decision.degraded) and decision.route == "pricing",
          f"route={decision.route} degraded={decision.degraded} "
          f"hits={ {k[1]: v for k, v in state['hits'].items()} }")

    # 2) 端点持续 500 -> 三路全失败，降级到 fallback，绝不抛异常
    decision = await run_case(
        PRICE_HISTORY,
        VLLM_SERVER_URL=f"{base_url}/dead/v1/chat/completions",
        MAX_RETRIES=1,
        RETRY_BASE_DELAY=0.01,
        RETRY_JITTER=0.0,
    )
    check("持续 500 -> 全路降级到 fallback 且不崩溃",
          decision.degraded and decision.route == "fallback" and len(decision.errors) == 3,
          f"route={decision.route} errors={len(decision.errors)}")

    # 3) 只有 pricing 路挂 -> 跳过它，路由继续往下走
    decision = await run_case(
        SCHEDULE_HISTORY,
        VLLM_SERVER_URL=f"{base_url}/halfdead/v1/chat/completions",
        MAX_RETRIES=0,
    )
    check("单路失败时跳过该路，其余分支仍命中",
          decision.degraded and decision.route == "scheduling"
          and "pricing_prompt" in decision.errors
          and "scheduling_prompt" not in decision.errors,
          f"route={decision.route} failed={sorted(decision.errors)}")

    # 4) 端点挂起 -> 超时，而不是永久卡死。
    #    用独立端点：被客户端放弃的请求仍在 sleep，会污染并发峰值观测。
    started = time.perf_counter()
    decision = await run_case(
        PRICE_HISTORY,
        VLLM_SERVER_URL=slow_url,
        REQUEST_TIMEOUT_TOTAL=1.0,
        REQUEST_TIMEOUT_SOCK_READ=1.0,
        MAX_RETRIES=0,
    )
    elapsed = time.perf_counter() - started
    check("端点挂起 -> 按超时熔断（未永久卡死）",
          decision.degraded and decision.route == "fallback" and 0.9 <= elapsed < 10,
          f"{elapsed:.2f}s route={decision.route}")

    # 5) HTTP 200 但响应体坏掉 -> 不可重试错误，立即降级
    decision = await run_case(
        PRICE_HISTORY,
        VLLM_SERVER_URL=f"{base_url}/garbage/v1/chat/completions",
        MAX_RETRIES=2,
        RETRY_BASE_DELAY=0.01,
        RETRY_JITTER=0.0,
    )
    hits = state["hits"].get(("garbage", "pricing"), 0)
    check("坏 JSON 不重试（只打 1 次）并降级",
          decision.degraded and hits == 1,
          f"pricing 路请求次数={hits}")

    # 6) 脏输出：'\\n**false**' + 尾随解释，归一化后仍要正确
    decision = await run_case(
        LISTING_HISTORY,
        VLLM_SERVER_URL=f"{base_url}/dirty/v1/chat/completions",
    )
    check("脏输出归一化（markdown/换行/尾随解释）",
          (not decision.degraded) and decision.route == "listing",
          f"route={decision.route} raw={decision.raw}")

    # 7) 模型跑偏吐长文本：白名单关掉时会把那段话直接发给用户，
    #    打开后该路按未命中处理，落到真正命中的 listing 分支
    url = f"{base_url}/chatty/v1/chat/completions"
    off = await run_case(LISTING_HISTORY, VLLM_SERVER_URL=url, STRICT_TOKENS=False)
    on = await run_case(LISTING_HISTORY, VLLM_SERVER_URL=url, STRICT_TOKENS=True)
    check("白名单关闭：跑偏的长文本被当作回复透出（反面用例）",
          off.route == "scheduling" and len(off.reply) > 60,
          f"route={off.route} reply={off.reply[:60]}...")
    check("白名单开启：跑偏的长文本被拦下，落到 listing 分支",
          on.route == "listing" and on.degraded
          and "scheduling_prompt" in on.errors,
          f"route={on.route} reply={on.reply!r}")

    # 8) 并发上限：MAX_CONCURRENCY=1 时峰值必须为 1；=2 时允许 2 但不能是 3
    for limit, expected in ((1, 1), (2, 2), (3, 3)):
        state["peak"] = 0
        state["inflight"] = 0
        started = time.perf_counter()
        await run_case(
            PRICE_HISTORY,
            VLLM_SERVER_URL=f"{base_url}/slow/v1/chat/completions?delay=0.4",
            MAX_CONCURRENCY=limit,
            REQUEST_TIMEOUT_TOTAL=10.0,
            REQUEST_TIMEOUT_SOCK_READ=10.0,
            MAX_RETRIES=0,
        )
        elapsed = time.perf_counter() - started
        check(f"MAX_CONCURRENCY={limit} 时并发峰值 == {expected}",
              state["peak"] == expected,
              f"peak={state['peak']} 耗时={elapsed:.2f}s")


# --------------------------------------------------------------------------
# 正常路由组（与教程版输出逐字对齐）
# --------------------------------------------------------------------------

BASELINE_CASES = [
    ("pricing / 无 listing_id", PRICE_HISTORY, "pricing",
     "Could you please provide the listing_id of the item you're asking about?"),
    ("pricing / 给出 listing_id", PRICE_WITH_ID, "pricing",
     "The price for listing 123456 is $350,000."),
    ("pricing / 未知 listing_id",
     [{"role": "user", "content": "What is the price of listing 999999?"}], "pricing",
     "We are unable to find that listing ID in our records. Are you sure you have the correct listing ID?"),
    ("scheduling / 有日期时间", SCHEDULE_HISTORY, "scheduling",
     "Perfect! I've scheduled a call for you on that date and time. A sales representative will reach out to you at that time."),
    ("scheduling / 缺日期时间",
     [{"role": "user", "content": "Can I schedule a call with an agent?"}], "scheduling",
     "What day and time are you available for the call?"),
    ("listing / 通用问题", LISTING_HISTORY, "listing",
     "Please hold while I transfer you to a specialist for further assistance."),
    ("fallback / 超纲",
     [{"role": "user", "content": "What is the weather today?"}], "fallback",
     "I apologize, I'm not sure how I can help with that. Let me transfer you to a human representative who can better assist you."),
]


async def test_routes_offline(base_url: str):
    print("\n=== 路由回归（本地 mock 端点，与教程版输出逐字比对）===")
    for name, history, expected_route, expected_reply in BASELINE_CASES:
        decision = await run_case(
            history,
            VLLM_SERVER_URL=f"{base_url}/ok/v1/chat/completions",
        )
        check(name,
              decision.route == expected_route and decision.reply == expected_reply,
              f"route={decision.route} reply={decision.reply!r}")


async def test_routes_real():
    print("\n=== 真实模型路由回归（用 .env 里的端点）===")
    if not prod.LLM_API_KEY:
        print("  [SKIP] 未配置 LLM_API_KEY")
        return
    for name, history, expected_route, _ in BASELINE_CASES:
        try:
            # 已在 event loop 中，必须用 async 入口（同步入口内部是 asyncio.run）
            decision = await prod.run_agentic_workflow_async(history)
        except Exception as exc:  # 真实端点异常不该让整个测试挂掉
            check(name, False, f"{type(exc).__name__}: {exc}")
            continue
        # 真实模型偶有一路跑偏（被白名单拦下 -> degraded），只要最终路由对就行
        check(name, decision.route == expected_route,
              f"route={decision.route} degraded={decision.degraded} "
              f"raw={decision.raw} errors={decision.errors}")


# --------------------------------------------------------------------------


async def amain(run_fault: bool, run_real: bool) -> int:
    print(f"endpoint (real): {prod.VLLM_SERVER_URL}")
    print(f"model          : {prod.LLM_MODEL_ID or '(按 prompt 各自定义)'}")
    print(f"concurrency={prod.MAX_CONCURRENCY} timeout={prod.REQUEST_TIMEOUT_TOTAL}s "
          f"retries={prod.MAX_RETRIES}")

    if run_fault:
        runner, state, base_url = await start_fault_server()
        slow_runner, _, slow_base_url = await start_fault_server()
        try:
            await test_faults(base_url, state, f"{slow_base_url}/slow/v1/chat/completions?delay=5")
            await test_routes_offline(base_url)
        finally:
            await runner.cleanup()
            await slow_runner.cleanup()

    if run_real:
        await test_routes_real()

    return None


def smoke_sync_entry() -> None:
    """同步入口（asyncio.run 版）单独验一次：只能在 loop 外调用。"""
    if not prod.LLM_API_KEY:
        return
    print("\n=== 同步入口 smoke（loop 外）===")
    try:
        decision = prod.run_agentic_workflow(PRICE_HISTORY)
        check("run_agentic_workflow() 同步入口可用", not decision.degraded,
              f"route={decision.route} elapsed={decision.elapsed_ms}ms")
    except Exception as exc:
        check("run_agentic_workflow() 同步入口可用", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Test the production-hardened workflow")
    parser.add_argument("--only", choices=("fault", "real"), help="只跑某一组")
    args = parser.parse_args()
    run_fault = args.only in (None, "fault")
    run_real = args.only in (None, "real")

    asyncio.run(amain(run_fault, run_real))
    if run_real:
        smoke_sync_entry()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n汇总：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
