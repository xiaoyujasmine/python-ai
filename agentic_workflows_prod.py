"""
agentic_workflows_prod.py - 教程版 `agentic_workflows.py` 的生产化补强版。

教程原版刻意保持极简（每次调用新建一个 ClientSession、无超时、
无重试、无并发上限、单路异常即整体崩溃、只返回一段字符串）。这在
demo 里没问题，放到线上会有六个具体的失效模式，本文件逐个补掉：

    #  失效模式                          补强手段
    --  --------------------------------  ------------------------------------
    1  端点挂起 -> 整个请求永久卡死      ClientTimeout(connect/sock_read/total)
    2  429 / 5xx 抖动 -> 直接失败        指数退避重试（带 jitter，尊重 Retry-After）
    3  401/402/400 也跟着重试 -> 白烧钱  按状态码分诊，不可恢复的错误 fail fast
    4  三路并发打爆免费档 QPS            asyncio.Semaphore 限流
    5  一路失败 -> 整个 fan-out 崩       return_exceptions=True + 逐路降级
    6  只返回一段字符串，无法排障        结构化 RouteDecision（路由/原文/错误/耗时）

额外两点（线上真踩过的坑）：
    - 输出归一化：模型会返回 "\\nfalse"、"**false**"、"`false`"、
      "Answer: false"，教程里只做 strip() 是不够的。
    - 价格查询从函数里抽出来，可注入真实 DB 查询，不再硬编码字典。

怎么读这个文件（建议按调用链自底向上，或自顶向下各读一遍）：

    run_agentic_workflow()          <- 入口（同步包装，脚本/测试用）
      └─ run_agentic_workflow_async()   <- 真正的主流程，读这一个就懂全貌
           ├─ build_call_specs()    组装三路请求（= 教程 Step 4 的 fan-out 准备）
           ├─ fanout()              并发发出去，返回 {prompt名: 文本 or 异常}
           │    └─ call_model_once()  单路：限流 -> 超时 -> 分诊 -> 重试
           │         └─ _attempt()      真正的 HTTP POST
           ├─ normalize() + sanitize_token()   把模型输出压成可信 token
           └─ route()                按优先级挑分支（= 教程 Step 5）

    排障时看 RouteDecision 的四个字段就够了：
        route     最终走了哪条分支
        raw       三路各自归一化后的原文（看模型到底说了什么）
        errors    哪一路失败、为什么
        degraded  True 表示结果是"带病"产出的，不该当正常样本评估

用法（与教程版接口兼容，多一个结构化返回值）：
    result = run_agentic_workflow(history)
    print(result.reply)        # 教程版的那个字符串
    print(result.route)        # "pricing" / "scheduling" / "listing" / "fallback"
    print(result.degraded)     # True = 有分类路失败，结果是降级出来的

    # 已有 event loop 的场景（FastAPI / 常驻服务）用 async 版本，
    # 并复用一个 ClientSession，避免每条消息重建连接池：
    result = await run_agentic_workflow_async(history, session=session)

CLI:
    python3 agentic_workflows_prod.py "what is the price of listing 123456?"
    python3 agentic_workflows_prod.py "..." --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

import aiohttp

# .env 在模块级就要用到（见下方常量），所以先加载。
# env_loader 只补不覆盖：命令行 export 的值始终优先。
try:
    from env_loader import load_env

    load_env()
except Exception:  # pragma: no cover - 单文件拷走时也能跑
    pass

# prompts 单一来源：直接复用教程模块，避免两套提示各自漂移。
# 注意 agentic_workflows 在 import 时读取 PROMPT_VARIANT，故 load_env() 必须在前。
from agentic_workflows import PROMPT_VARIANT, SYSTEM_PROMPT_CONFIGURATIONS

logger = logging.getLogger("agentic_workflows")


# --------------------------------------------------------------------------
# Step 1 - 配置
# --------------------------------------------------------------------------

VLLM_SERVER_URL = os.getenv(
    "VLLM_SERVER_URL", "http://your_server_ip:8000/v1/chat/completions"
)
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL_ID = os.getenv("LLM_MODEL_ID", "")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("%s=%r 不是整数，回退为 %s", name, raw, default)
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        logger.warning("%s=%r 不是数字，回退为 %s", name, raw, default)
        return default


# 并发上限。免费档普遍只给 1~3 并发 / 3 req/s，默认 3 刚好等于本 demo 的
# 三路 fan-out；真上量时按供应商配额调，不要盲目调大。
MAX_CONCURRENCY = _env_int("MAX_CONCURRENCY", 3, minimum=1)

# 超时。total 是整条请求（含重试之外的一次尝试）的天花板。
REQUEST_TIMEOUT_TOTAL = _env_float("REQUEST_TIMEOUT_TOTAL", 20.0, minimum=0.1)
REQUEST_TIMEOUT_CONNECT = _env_float("REQUEST_TIMEOUT_CONNECT", 5.0, minimum=0.1)
REQUEST_TIMEOUT_SOCK_READ = _env_float("REQUEST_TIMEOUT_SOCK_READ", 15.0, minimum=0.1)

# 重试。MAX_RETRIES=2 表示最多 3 次尝试。只对可恢复错误重试。
MAX_RETRIES = _env_int("MAX_RETRIES", 2, minimum=0)
RETRY_BASE_DELAY = _env_float("RETRY_BASE_DELAY", 0.5, minimum=0.0)
RETRY_MAX_DELAY = _env_float("RETRY_MAX_DELAY", 8.0, minimum=0.0)
RETRY_JITTER = _env_float("RETRY_JITTER", 0.3, minimum=0.0)

# 采样参数
MAX_TOKENS = _env_int("MAX_TOKENS", 100, minimum=1)
TEMPERATURE = _env_float("TEMPERATURE", 0.1, minimum=0.0)

# 采样温度压到 0 可显著减少小模型的抖动（实测 temperature=0.1 时
# GLM-4-9B 偶尔把 "Can I schedule a call?" 判成 false）。需要多样性时调回。
# TEMPERATURE=0 即 greedy 解码。

# 可恢复状态码：限流 + 服务端错误 + 网关超时。
# 401/402/403/400/404 都是配置或账户问题，重试只会浪费时间和钱。
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# 状态码分诊文案。402 尤其重要：key 本身有效，重建 key 是无效动作。
STATUS_HINTS = {
    400: "请求体格式错误（多半是 model 名写错或 messages 结构不对）",
    401: "API key 无效/未传。检查 LLM_API_KEY 是否复制完整",
    402: "key 有效但余额不足 —— 重建 key 没用，去控制台充值或领额度",
    403: "key 无该模型权限，换模型或开通权限",
    404: "端点或模型不存在，检查 VLLM_SERVER_URL 与 LLM_MODEL_ID",
    422: "参数不合法（如 max_tokens 超限）",
    429: "触发限流。降低 MAX_CONCURRENCY，或换更高配额的档位",
}

# 教程里的示例价格库。生产环境请通过 price_lookup 注入真实查询。
EXAMPLE_PRICE_DATABASE = {
    "123456": "$350,000",
    "654321": "$450,000",
    "112233": "$550,000",
}

# 每路允许的合法输出（前缀 / 精确值）。strict 变体下命中不了白名单的输出
# 一律按"未命中"处理 —— 否则模型跑偏时长篇大论会被当成追问句直接发给用户
# （实测：GLM-4-9B 在 scheduling 路上返回过一整段
#  "I'm sorry, but I don't have access to real-time data ..."）。
TOKEN_SPECS = {
    "pricing_prompt": (("listing_id:",), {"false", "need_listing_id"}),
    "scheduling_prompt": (("date:",), {"false", "need_datetime"}),
    "listing_prompt": ((), {"true", "false"}),
}

# 是否启用白名单校验。默认跟随 PROMPT_VARIANT=strict；跑 original 变体时
# 模型被允许自由生成追问句，此时必须关掉，否则把合法追问误判为非法。
STRICT_TOKENS = (os.getenv("STRICT_TOKENS", "") or PROMPT_VARIANT).strip().lower() == "strict"


# --------------------------------------------------------------------------
# 结构化返回值
# --------------------------------------------------------------------------


@dataclass
class RouteDecision:
    """一次 fan-out + 路由的完整结果，便于日志/埋点/排障。"""

    reply: str  # 给用户的话（= 教程版的返回值）
    route: str  # pricing / scheduling / listing / fallback
    matched_prompt: str  # 命中的那一路，fallback 时为空串
    raw: Dict[str, str] = field(default_factory=dict)  # 每路归一化后的原文
    errors: Dict[str, str] = field(default_factory=dict)  # 每路的失败原因
    degraded: bool = False  # True = 至少一路失败，结果是降级出来的
    elapsed_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reply": self.reply,
            "route": self.route,
            "matched_prompt": self.matched_prompt,
            "raw": self.raw,
            "errors": self.errors,
            "degraded": self.degraded,
            "elapsed_ms": self.elapsed_ms,
        }


class LLMError(Exception):
    """一次模型调用失败。retryable=False 时应立即放弃。"""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 retryable: bool = False, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


# --------------------------------------------------------------------------
# 输出归一化
# --------------------------------------------------------------------------

# 模型偶尔在答案前加一句 "Answer:" / "Output:" 之类。
_LEADING_LABEL = re.compile(r"^(answer|output|result|response)\s*[:：]\s*", re.IGNORECASE)


def normalize(raw: Any) -> str:
    """把模型输出压成可直接比较的 token。

    实测见过的形态：'\\nfalse'、'**false**'、'`false`'、'"false"'、
    'Answer: false'、'false.（换行）解释...'。教程版只做 strip()，
    遇到前几种会全部被当成一次命中，路由直接跑偏。
    """
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    text = _LEADING_LABEL.sub("", text)
    # 去掉 markdown 强调/代码围栏/引号
    text = text.strip().strip("`*_\"' ")
    # 只取第一行：模型常在正确 token 后面追一段解释
    text = text.splitlines()[0].strip() if text else text
    return text.strip("`*_\"' ").strip()


def sanitize_token(prompt_name: str, value: str) -> Optional[str]:
    """白名单校验。合法返回原值，非法返回 None（调用方按"该路未命中"处理）。

    strict 变体下这条很重要：路由里 pricing/scheduling 都有一个"原样透出"
    的兜底分支，模型跑偏时那段自由文本会被直接发给用户。
    """
    if not STRICT_TOKENS:
        return value
    spec = TOKEN_SPECS.get(prompt_name)
    if spec is None:
        return value
    prefixes, exact = spec
    lowered = value.lower()
    if lowered in exact:
        return value
    if any(lowered.startswith(prefix) for prefix in prefixes):
        return value
    logger.warning("[%s] 输出不在白名单内，按未命中处理：%r", prompt_name, value[:120])
    return None


# --------------------------------------------------------------------------
# Step 2 - 单次调用（超时 + 分诊 + 重试）
# --------------------------------------------------------------------------


def _timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(
        total=REQUEST_TIMEOUT_TOTAL,
        connect=REQUEST_TIMEOUT_CONNECT,
        sock_connect=REQUEST_TIMEOUT_CONNECT,
        sock_read=REQUEST_TIMEOUT_SOCK_READ,
    )


def _auth_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    return headers


def _extract_content(data: Any) -> str:
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        snippet = json.dumps(data, ensure_ascii=False)[:300]
        raise LLMError(f"响应缺少 choices[0].message.content: {snippet}", retryable=False)


def _status_error(status: int, body: str) -> LLMError:
    hint = STATUS_HINTS.get(status, f"HTTP {status}")
    snippet = body.strip().replace("\n", " ")[:200]
    retryable = status in RETRYABLE_STATUS
    retry_after = None
    if status == 429 and snippet:
        # 有的网关把 Retry-After 放在 body 里
        match = re.search(r"retry[-_ ]?after\D{0,10}(\d+(?:\.\d+)?)", snippet, re.IGNORECASE)
        if match:
            retry_after = min(float(match.group(1)), 30.0)
    return LLMError(
        f"{hint} | HTTP {status}: {snippet}" if snippet else f"{hint} | HTTP {status}",
        status=status,
        retryable=retryable,
        retry_after=retry_after,
    )


def _backoff_delay(attempt: int, retry_after: Optional[float]) -> float:
    """指数退避 + 抖动。有 Retry-After 时以服务端要求为准。"""
    if retry_after is not None:
        return min(retry_after, RETRY_MAX_DELAY)
    delay = RETRY_BASE_DELAY * (2 ** attempt)
    delay = min(delay, RETRY_MAX_DELAY)
    if RETRY_JITTER:
        delay += random.uniform(0, RETRY_JITTER * max(delay, RETRY_BASE_DELAY))
    return delay


async def _attempt(session: aiohttp.ClientSession, url: str, payload: Dict[str, Any],
                   headers: Dict[str, str]) -> str:
    async with session.post(url, json=payload, headers=headers) as response:
        body = await response.text()
        if response.status >= 400:
            error = _status_error(response.status, body)
            if error.retry_after is None:
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        error.retry_after = min(float(retry_after), 30.0)
                    except ValueError:
                        pass
            raise error
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise LLMError(f"响应不是合法 JSON: {body[:200]}", retryable=False)
    return _extract_content(data)


async def call_model_once(
    session: aiohttp.ClientSession,
    model_id: str,
    messages: Sequence[Dict[str, str]],
    *,
    semaphore: asyncio.Semaphore,
    label: str = "",
) -> str:
    """调用一次模型，带限流、超时、分诊、重试。失败抛 LLMError。

    注意 semaphore 包住整个重试循环：否则重试会把并发再放大一倍。
    """
    url = VLLM_SERVER_URL
    payload = {
        "model": model_id,
        "messages": list(messages),
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
    }
    headers = _auth_headers()

    last_error: Optional[LLMError] = None
    async with semaphore:
        for attempt in range(MAX_RETRIES + 1):
            started = time.perf_counter()
            try:
                content = await _attempt(session, url, payload, headers)
                logger.debug("[%s] 第 %d 次尝试成功，%.0fms",
                             label, attempt + 1, (time.perf_counter() - started) * 1000)
                return content
            except LLMError as exc:
                last_error = exc
                if not exc.retryable or attempt == MAX_RETRIES:
                    break
                delay = _backoff_delay(attempt, exc.retry_after)
                logger.warning("[%s] HTTP %s，%.2fs 后第 %d 次重试（%s）",
                               label, exc.status, delay, attempt + 2, exc)
                await asyncio.sleep(delay)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                last_error = LLMError(f"请求超时（>{REQUEST_TIMEOUT_TOTAL}s）: {exc}",
                                      retryable=True)
                if attempt == MAX_RETRIES:
                    break
                delay = _backoff_delay(attempt, None)
                logger.warning("[%s] 超时，%.2fs 后第 %d 次重试", label, delay, attempt + 2)
                await asyncio.sleep(delay)
            except aiohttp.ClientError as exc:
                last_error = LLMError(f"网络错误 {type(exc).__name__}: {exc}", retryable=True)
                if attempt == MAX_RETRIES:
                    break
                delay = _backoff_delay(attempt, None)
                logger.warning("[%s] %s，%.2fs 后第 %d 次重试",
                               label, last_error, delay, attempt + 2)
                await asyncio.sleep(delay)

    assert last_error is not None
    raise last_error


# --------------------------------------------------------------------------
# Step 4 - fan-out / fan-in
# --------------------------------------------------------------------------


async def fanout(
    session: aiohttp.ClientSession,
    call_specs: List[Dict[str, Any]],
    prompt_names: List[str],
) -> Dict[str, Any]:
    """并发跑所有分类路，返回 {prompt_name: 文本 or 异常}。

    return_exceptions=True 是关键：一路挂掉不影响另外两路，
    由调用方决定降级策略（教程版这里会直接把异常抛穿到最上层）。
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    tasks = [
        call_model_once(
            session,
            spec["model_id"],
            spec["messages"],
            semaphore=semaphore,
            label=name,
        )
        for name, spec in zip(prompt_names, call_specs)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return dict(zip(prompt_names, results))


# --------------------------------------------------------------------------
# Step 5 - 路由（与教程同序的 if/elif，失败路自动跳过）
# --------------------------------------------------------------------------


def route(
    responses: Dict[str, str],
    *,
    price_lookup: Optional[Callable[[str], Optional[str]]] = None,
) -> tuple[str, str, str]:
    """返回 (reply, route, matched_prompt)。

    与教程一致的优先级：pricing -> scheduling -> listing -> fallback。
    差别：值为失败（None）的路直接跳过，不再短路到 fallback。
    """
    lookup = price_lookup or EXAMPLE_PRICE_DATABASE.get

    pricing = responses.get("pricing_prompt")
    if pricing is not None and pricing.lower() != "false":
        if pricing == "need_listing_id":
            return (
                "Could you please provide the listing_id of the item you're asking about?",
                "pricing",
                "pricing_prompt",
            )
        if pricing.lower().startswith("listing_id:"):
            # 大小写与空格都容错：'listing_id:123456' / 'Listing_ID: 123456'
            listing_id = pricing.split(":", 1)[1].strip()
            found_price = lookup(listing_id)
            if found_price:
                return (
                    f"The price for listing {listing_id} is {found_price}.",
                    "pricing",
                    "pricing_prompt",
                )
            return (
                "We are unable to find that listing ID in our records. "
                "Are you sure you have the correct listing ID?",
                "pricing",
                "pricing_prompt",
            )
        # original 变体：模型自由生成的追问句，原样透出
        return pricing, "pricing", "pricing_prompt"

    scheduling = responses.get("scheduling_prompt")
    if scheduling is not None and scheduling.lower() != "false":
        if scheduling == "need_datetime":
            return (
                "What day and time are you available for the call?",
                "scheduling",
                "scheduling_prompt",
            )
        if scheduling.lower().startswith("date:"):
            # 生产环境：这里去真正落库预约，并把日期时间回显给用户确认
            return (
                "Perfect! I've scheduled a call for you on that date and time. "
                "A sales representative will reach out to you at that time.",
                "scheduling",
                "scheduling_prompt",
            )
        return scheduling, "scheduling", "scheduling_prompt"

    listing = responses.get("listing_prompt")
    if listing is not None and listing.lower() != "false":
        # 生产环境：转人工，或查 listing 详情后直接回答
        return (
            "Please hold while I transfer you to a specialist for further assistance.",
            "listing",
            "listing_prompt",
        )

    return (
        "I apologize, I'm not sure how I can help with that. "
        "Let me transfer you to a human representative who can better assist you.",
        "fallback",
        "",
    )


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------


def build_call_specs(conversation_history: Sequence[Dict[str, str]]):
    """把会话历史展开成三路 call spec（与教程 Step 4 同构）。"""
    call_specs: List[Dict[str, Any]] = []
    prompt_names: List[str] = []
    for prompt_name, config in SYSTEM_PROMPT_CONFIGURATIONS.items():
        model_id = LLM_MODEL_ID or config["model_id"]
        call_specs.append({
            "model_id": model_id,
            "messages": [{"role": "system", "content": config["prompt"]}]
            + list(conversation_history),
        })
        prompt_names.append(prompt_name)
    return call_specs, prompt_names


async def run_agentic_workflow_async(
    conversation_history: Sequence[Dict[str, str]],
    *,
    session: Optional[aiohttp.ClientSession] = None,
    price_lookup: Optional[Callable[[str], Optional[str]]] = None,
) -> RouteDecision:
    """异步入口。常驻服务请复用 session，别每条消息新建连接池。"""
    started = time.perf_counter()
    call_specs, prompt_names = build_call_specs(conversation_history)

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession(timeout=_timeout())

    try:
        raw_results = await fanout(session, call_specs, prompt_names)
    finally:
        if owns_session:
            await session.close()

    # fan-in 归集：把每路结果分成"可用文本"和"失败原因"两堆。
    # 这里是整个 prod 版与教程版最本质的差别 —— 教程版假设三路必定都成功，
    # 这里承认任何一路都可能失败，并且失败时只是"这一路当作没命中"，
    # 而不是让整条请求崩掉。
    responses: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    degraded = False
    for name in prompt_names:
        value = raw_results.get(name)
        if isinstance(value, BaseException):
            errors[name] = f"{type(value).__name__}: {value}"
            degraded = True
            logger.error("[%s] 分类失败，本路降级为未命中：%s", name, value)
            continue
        normalized = normalize(value)
        if sanitize_token(name, normalized) is None:
            errors[name] = f"输出不在白名单内，按未命中处理：{normalized[:120]!r}"
            degraded = True
            continue
        responses[name] = normalized

    reply, route_name, matched = route(responses, price_lookup=price_lookup)

    decision = RouteDecision(
        reply=reply,
        route=route_name,
        matched_prompt=matched,
        raw=responses,
        errors=errors,
        degraded=degraded,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )
    logger.info("route=%s matched=%s degraded=%s elapsed=%dms errors=%s",
                decision.route, decision.matched_prompt or "-",
                decision.degraded, decision.elapsed_ms, decision.errors or "-")
    return decision


def run_agentic_workflow(
    conversation_history: Sequence[Dict[str, str]],
    *,
    price_lookup: Optional[Callable[[str], Optional[str]]] = None,
) -> RouteDecision:
    """同步入口，供脚本/测试使用。已在 event loop 中时请改用 async 版本。"""
    return asyncio.run(run_agentic_workflow_async(conversation_history, price_lookup=price_lookup))


def run_agentic_workflow_text(
    conversation_history: Sequence[Dict[str, str]],
    *,
    price_lookup: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """教程版 `run_agentic_workflow` 的直接替身：只返回那段回复文本。"""
    return run_agentic_workflow(conversation_history, price_lookup=price_lookup).reply


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the (production-hardened) agentic workflow on one user message."
    )
    parser.add_argument("message", nargs="?",
                        default="Hi, can you tell me the price of one of your listings?")
    parser.add_argument("--json", action="store_true", help="输出完整 RouteDecision")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印调试日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    history = [{"role": "user", "content": args.message}]
    decision = run_agentic_workflow(history)

    if args.json:
        print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(decision.reply)
        if decision.degraded:
            print(f"\n[降级] 失败的分类路：{decision.errors}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
