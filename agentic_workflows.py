"""
Parallel Agentic Workflows with Python
Source: DigitalOcean Community Tutorial
        "How to Build Parallel Agentic Workflows with Python" by Andrew Dugan
        https://www.digitalocean.com/community/tutorials/how-to-build-parallel-agentic-workflows-with-python

Structure:
    Step 1 - Environment / endpoint setup
    Step 2 - Asynchronous call logic
    Step 3 - System prompt configurations
    Step 4 - Processing user input through the models (fan-out / fan-in)
    Step 5 - Routing logic

通读顺序（不要按文件从上往下读，按"数据怎么流"读）：
    1. Step 3  SYSTEM_PROMPT_CONFIGURATIONS —— 先弄清楚模型被要求吐什么格式，
       后面所有路由判断都是围绕这份"输出契约"展开的。
    2. Step 2  _call_single_model / _call_models_async —— 再看这些提示怎么并发打出去。
    3. Step 4+5 run_agentic_workflow —— 最后看 fan-out/fan-in 与 if/elif 路由。

一句话数据流：
    一条用户消息 + 三个不同的 system prompt
      -> 三路并发各问一次模型（fan-out）
      -> 拿到三份"分类/抽取"结果（fan-in，顺序与入参一致）
      -> 按 pricing > scheduling > listing > fallback 的优先级挑一条
      -> 返回一句给用户的回复

核心设计取舍：三次调用之间没有依赖，所以能并发；分类结果被压缩成极短的
token（'false' / 'listing_id: XXXXXX' / 'date: ...'），所以路由层能用最土的
if/elif 搞定，不需要再调一次模型去判断意图。
"""

import asyncio
import os

import aiohttp

# Step 1 - Your droplet's vLLM server URL.
# Replace the default with the IP address of your GPU Droplet deployment,
# or point this at any OpenAI-compatible /v1/chat/completions endpoint
# (SiliconFlow, Zhipu, DashScope, OpenRouter ... all expose one).
VLLM_SERVER_URL = os.getenv(
    "VLLM_SERVER_URL", "http://your_server_ip:8000/v1/chat/completions"
)

# Bearer token for hosted providers. Leave empty for a local vLLM / mock
# server that needs no auth - in that case no Authorization header is sent.
LLM_API_KEY = os.getenv("LLM_API_KEY", "")

# Optional: override the model for every prompt at once. Handy when moving
# between providers without editing SYSTEM_PROMPT_CONFIGURATIONS.
LLM_MODEL_ID = os.getenv("LLM_MODEL_ID", "")

# "original" = verbatim tutorial prompts (written for Mistral-Small-24B).
# "strict"   = same three classifiers, but every branch answers with one
#              fixed token instead of free-form prose. See README
#              "排查：真实模型跑出非预期结果" for why this exists.
PROMPT_VARIANT = os.getenv("PROMPT_VARIANT", "original").strip().lower()


# Step 3 - Writing your prompts.
# Each entry is an independent, stateless classification / extraction task.
# The negative case for every prompt is the literal string "false" so the
# routing layer in Step 5 can short-circuit with plain if/elif.
ORIGINAL_PROMPTS = {
    "pricing_prompt": {
        "model_id": "mistralai/Mistral-Small-3.2-24B-Instruct-2506",
        "prompt": "You are a customer service agent. Determine if the user's most recent request is asking about the price of a listing. If they are asking about the price of a listing AND if they have included the listing_id, return only the listing_id of the item they are asking about in the following format: 'listing_id: XXXXXX'. \nIf they are asking about the pricing of a listing AND did NOT mention the specific listing_id number, ask them for the listing id number. If they are requesting something other than the price of a listing: return only the word 'false'."
    },
    "scheduling_prompt": {
        "model_id": "mistralai/Mistral-Small-3.2-24B-Instruct-2506",
        "prompt": "You are a customer service agent. Determine if the user's most recent request is asking to schedule a call with a real estate agent. If they are trying to schedule a call, return the date and time they would like to schedule the call in the following format: 'date: YYYY-MM-DD, time: HH:MM'. If they are trying to schedule a call but they did not mention the specific date and time they are available, ask them what day and time they are available. Do not provide any additional information, such as the agent's availability. If they are not asking to schedule a call, return only the word 'false'."
    },
    "listing_prompt": {
        "model_id": "mistralai/Mistral-Small-3.2-24B-Instruct-2506",
        "prompt": "You are a customer service agent. Determine if the user is asking a question about a listing. If they are asking a question about a listing, return only the word 'true'. Otherwise, return only the word 'false'."
    }
}

# Same three tasks, but the model is only ever allowed to emit one token from
# a fixed set. Asking the model to BOTH free-write a follow-up question AND
# return exactly 'false' otherwise makes small models pick 'false' every time
# (measured: GLM-4-9B answered 'false' on an obvious pricing question, twice).
# The follow-up wording is moved into code, where it belongs.
STRICT_PROMPTS = {
    "pricing_prompt": {
        "model_id": "mistralai/Mistral-Small-3.2-24B-Instruct-2506",
        "prompt": (
            "Classify the user's most recent request. Answer with exactly one of "
            "these three tokens and nothing else - no punctuation, no explanation, "
            "no markdown.\n"
            "- 'listing_id: XXXXXX' if they are asking about the price of a listing "
            "AND gave a listing id (copy the id they gave)\n"
            "- 'need_listing_id' if they are asking about the price of a listing "
            "but gave no listing id\n"
            "- 'false' if they are not asking about the price of a listing"
        )
    },
    "scheduling_prompt": {
        "model_id": "mistralai/Mistral-Small-3.2-24B-Instruct-2506",
        "prompt": (
            "Classify the user's most recent request. Answer with exactly one of "
            "these three tokens and nothing else - no punctuation, no explanation, "
            "no markdown.\n"
            "Any request to schedule, book or arrange a call or appointment counts "
            "as a scheduling request, whether or not a date was given.\n"
            "- 'date: YYYY-MM-DD, time: HH:MM' if they want to schedule a call AND "
            "gave both a date and a time\n"
            "- 'need_datetime' if they want to schedule a call but gave no date or "
            "no time\n"
            "- 'false' only if the request has nothing to do with scheduling a call"
        )
    },
    "listing_prompt": {
        "model_id": "mistralai/Mistral-Small-3.2-24B-Instruct-2506",
        "prompt": (
            "Classify the user's most recent request. Answer with exactly one of "
            "these two tokens and nothing else - no punctuation, no explanation, "
            "no markdown.\n"
            "- 'true' if they are asking a question about a listing\n"
            "- 'false' otherwise"
        )
    }
}

# 上面两套提示的"开关"。教程原版写死用 ORIGINAL_PROMPTS；这里加了个开关，
# 是因为把原提示搬到 9B 级别的小模型上会误判（详见 STRICT_PROMPTS 上方的说明）。
# 注意：import 时就求值，所以 PROMPT_VARIANT 必须在 import 之前设好（见 env_loader）。
SYSTEM_PROMPT_CONFIGURATIONS = (
    STRICT_PROMPTS if PROMPT_VARIANT == "strict" else ORIGINAL_PROMPTS
)


# ---------------------------------------------------------------------------
# Step 2 - Writing the asynchronous call logic.
#
# 这一层的职责只有一个：把一条 messages 发到 OpenAI 兼容端点，取回模型那句回答。
# 它是"单次调用"，不知道路由、不知道并发，也刻意不做重试/超时 —— 那是
# agentic_workflows_prod.py 的事。先理解这个极简版，再看 prod 版补了什么。
# ---------------------------------------------------------------------------

async def _call_single_model(call_spec):
    """Make an async call to the Digital Ocean GPU droplet with a given model and messages"""
    model_id = call_spec["model_id"]
    messages = call_spec["messages"]

    # OpenAI 兼容的请求体。max_tokens=100 是因为分类结果本来就该很短；
    # temperature=0.1 是压低随机性，分类任务不想要"创意"。
    payload = {
        "model": model_id,
        "messages": messages,
        "max_tokens": 100,
        "temperature": 0.1
    }

    headers = {"Content-Type": "application/json"}
    # key 为空时（自建 vLLM / 本地 mock）不发 Authorization 头，否则部分网关会 401
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    # 每次调用都新建 session = 每次都重建 TCP 连接和连接池。
    # demo 无所谓，线上会白白多出一轮 TLS/握手开销（prod 版把 session 提到外层复用）。
    async with aiohttp.ClientSession() as session:
        # 没有 timeout：端点挂起时这里会一直等下去。prod 版补了 ClientTimeout。
        async with session.post(VLLM_SERVER_URL, json=payload, headers=headers) as response:
            result = await response.json()  # 非 2xx 时这里拿到的是错误体，会被当成正常解析

            # OpenAI 的响应结构：choices[0].message.content 才是模型说的话
            message = result["choices"][0]["message"]["content"]
            # Real models routinely emit a leading newline ("\nfalse") or wrap
            # output in markdown. Without this strip, "\nfalse" != "false" and
            # every route in Step 5 is treated as a hit.
            return message.strip()


async def _call_models_async(call_list):
    """Call multiple models asynchronously and return responses in the same order"""
    # 这里只是把协程"打包"成 Task 并排进事件循环，此刻还没有真正发起请求
    tasks = [_call_single_model(call_spec) for call_spec in call_list]

    # gather 的两个关键性质：
    #   1. 并发 —— 三路请求同时在飞，总耗时 ≈ 最慢的那一路，而不是三路之和
    #   2. 保序 —— 返回的 list 与 tasks 顺序严格一致，所以不需要按 prompt 名回查
    #              就能把结果对回各自的提示（Step 4 就是靠这一点做映射的）
    # 另：默认 return_exceptions=False，任意一路抛异常会直接把整个 gather 炸掉。
    responses = await asyncio.gather(*tasks)
    return responses


def call_models(call_list):
    """Synchronous wrapper around the async fan-out. Do not call this from
    inside a running event loop (use `_call_models_async` directly instead).

    asyncio.run() 会新建一个事件循环并在结束后关掉它，所以：
      - 脚本里可以直接用（本文件就是这么用的）
      - FastAPI / 常驻服务里不能用（那里已经有一个在跑的 loop，会 RuntimeError）
    """
    return asyncio.run(_call_models_async(call_list))


# ---------------------------------------------------------------------------
# Step 4 + Step 5 - Processing user input through the models, then routing.
#
# fan-out：把同一段会话历史分别套上三个不同的 system prompt，一次并发发出去。
# fan-in ：把三份结果按序收回，再用 if/elif 按优先级挑一条分支。
# ---------------------------------------------------------------------------

def run_agentic_workflow(conversation_history):
    model_calls_list = []
    prompt_names = []  # Keep track of prompt order for response mapping

    # fan-out 的组装阶段：每个 prompt 一路，消息体 = [system prompt] + 完整会话历史。
    # 注意三路拿到的是同一份历史，只是 system prompt 不同 —— 这是"并行分类"的全部秘密。
    for prompt_name, config in SYSTEM_PROMPT_CONFIGURATIONS.items():
        # LLM_MODEL_ID (if set) overrides the per-prompt model for all prompts
        model_id = LLM_MODEL_ID or config["model_id"]
        system_prompt = config["prompt"]

        # Construct the full message prompt: system prompt + conversation history
        full_messages = [{"role": "system", "content": system_prompt}] + conversation_history

        model_calls_list.append({
            "model_id": model_id,
            "messages": full_messages
        })
        prompt_names.append(prompt_name)

    # fan-in：一次并发拿回三份结果（阻塞直到全部完成）
    prompt_responses = call_models(model_calls_list)

    # 用名字回查下标，而不是写死 [0] [1] [2]：
    # 这样往 SYSTEM_PROMPT_CONFIGURATIONS 里加/删提示时不会静默错位。
    pricing_response = prompt_responses[prompt_names.index("pricing_prompt")]
    scheduling_response = prompt_responses[prompt_names.index("scheduling_prompt")]
    listing_response = prompt_responses[prompt_names.index("listing_prompt")]

    # 路由的通用约定：模型返回 "false" = "这条不是我的事"，于是落到下一路；
    # 返回别的任何东西 = "这条归我"，进入本路的处理分支。
    # 优先级是写死的：pricing > scheduling > listing > fallback，改顺序就改语义。
    #
    # ⚠️ 下面每路最后都有一个 else 兜底"原样透出模型输出"。教程里这是"让模型
    # 自己生成追问句"的设计；放到生产有风险 —— 模型跑偏时（实测 GLM-4-9B 在
    # scheduling 路吐过一整段 "I'm sorry, but I don't have access to..."），
    # 那段话会被原封不动发给用户。prod 版用 token 白名单把这条堵上了。

    # Route 1: Handle pricing inquiries
    if pricing_response.lower() != "false":
        if pricing_response == "need_listing_id":
            # STRICT_PROMPTS token: the classifier found a pricing intent with
            # no id. Wording stays in code instead of being sampled from the model.
            final_response = "Could you please provide the listing_id of the item you're asking about?"
        elif pricing_response.startswith("listing_id:"):
            # Extract listing ID from response
            listing_id = pricing_response.split("listing_id: ")[1].strip()

            # Simulate database/API lookup
            # You can add database querying logic here to look for a target listing ID. For our example, we will use a simple Python dictionary
            example_price_database = {
                "123456": "$350,000",
                "654321": "$450,000",
                "112233": "$550,000"
            }

            found_price = example_price_database.get(listing_id)
            if found_price:
                final_response = f"The price for listing {listing_id} is {found_price}."
            else:
                final_response = "We are unable to find that listing ID in our records. Are you sure you have the correct listing ID?"
        else:
            final_response = pricing_response  # Response asking for listing ID

    # Route 2: Handle scheduling requests
    elif scheduling_response.lower() != "false":
        if scheduling_response == "need_datetime":
            final_response = "What day and time are you available for the call?"
        elif scheduling_response.startswith("date:"):
            final_response = f"Perfect! I've scheduled a call for you on that date and time. A sales representative will reach out to you at that time."
            # In production: Add logic to actually book the appointment, and consider customizing the message to confirm the date and time selected.
        else:
            final_response = scheduling_response  # Response asking for specific time

    # Route 3: Handle general listing questions
    elif listing_response.lower() != "false":
        final_response = "Please hold while I transfer you to a specialist for further assistance."
        # In production: Add logic to transfer chat to human representative
        # You could alternatively add logic to access listing details and answer the user's specific question

    else:
        final_response = "I apologize, I'm not sure how I can help with that. Let me transfer you to a human representative who can better assist you."
        # In production: Add logic to transfer chat to human representative

    return final_response
