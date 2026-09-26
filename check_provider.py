"""
Connectivity / quota self-check before running the workflow.

Verifies three things in order:
    1. the endpoint is reachable and the API key is accepted (GET /models)
    2. the chosen model exists and is callable
    3. a real classification reply comes back in the expected shape

Usage:
    export VLLM_SERVER_URL="https://api.siliconflow.cn/v1/chat/completions"
    export LLM_API_KEY="sk-..."
    export LLM_MODEL_ID="Qwen/Qwen2.5-7B-Instruct"
    python3 check_provider.py
"""

import asyncio
import json
import os
import sys

import aiohttp

from env_loader import load_env_verbose

# 必须在读取下面三个配置之前执行：模块级常量只在 import 时求值一次
load_env_verbose()

VLLM_SERVER_URL = os.getenv("VLLM_SERVER_URL", "http://your_server_ip:8000/v1/chat/completions")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL_ID = os.getenv("LLM_MODEL_ID", "") or "Qwen/Qwen2.5-7B-Instruct"


def server_base(url):
    return url.replace("/chat/completions", "").rstrip("/")


def auth_headers():
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    return headers


async def list_models(session):
    url = f"{server_base(VLLM_SERVER_URL)}/models"
    try:
        async with session.get(url, headers=auth_headers()) as response:
            if response.status != 200:
                text = await response.text()
                # 不少网关（含本地 mock / vLLM 精简部署）不提供 /models，属可容忍情况
                print(f"[WARN] GET {url} 不可用 -> HTTP {response.status}: {text[:120]}")
                print("       跳过模型列表检查，直接验证模型调用")
                return None
            data = await response.json()
    except aiohttp.ClientError as exc:
        print(f"[WARN] 无法拉取模型列表（{exc}），跳过该检查")
        return None

    ids = sorted({item.get("id", "") for item in data.get("data", []) if item.get("id")})
    if not ids:
        print("[WARN] 模型列表为空，跳过该检查")
        return None

    print(f"[OK]   端点可达，共 {len(ids)} 个模型。前 20 个：")
    for model_id in ids[:20]:
        marker = "  <-- 当前使用" if model_id == LLM_MODEL_ID else ""
        print(f"       {model_id}{marker}")
    return ids


async def ping_model(session, model_id):
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": "Return only the word 'false'."},
            {"role": "user", "content": "Return only the word 'false'."},
        ],
        "max_tokens": 16,
        "temperature": 0.0,
    }
    async with session.post(VLLM_SERVER_URL, json=payload, headers=auth_headers()) as response:
        status = response.status
        text = await response.text()

    if status == 401:
        print(f"[FAIL] HTTP 401：API key 无效或未设置（LLM_API_KEY 当前长度 {len(LLM_API_KEY)}）")
        return False
    if status == 429:
        print(f"[FAIL] HTTP 429：触发限流（免费档限制较严）。稍后重试或换 Pro 档：{text[:200]}")
        return False
    if status == 402:
        print(f"[FAIL] HTTP 402：账户余额不足，key 本身有效。去控制台充值/领额度后重试：{text[:200]}")
        return False
    if status != 200:
        print(f"[FAIL] HTTP {status}: {text[:300]}")
        return False

    try:
        content = json.loads(text)["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError):
        print(f"[FAIL] 响应格式异常：{text[:300]}")
        return False

    print(f"[OK]   模型 {model_id} 可调用，返回：{content!r}")
    if "false" not in content.lower():
        print("[WARN] 模型没有严格遵循格式约定，路由解析可能不稳，考虑换更大的模型或改写提示")
    return True


async def main():
    print(f"endpoint : {VLLM_SERVER_URL}")
    print(f"api key  : {'已设置 (sk-***' + LLM_API_KEY[-4:] + ')' if LLM_API_KEY else '未设置（本地无鉴权模式）'}")
    print(f"model    : {LLM_MODEL_ID}")
    print()

    async with aiohttp.ClientSession() as session:
        ids = await list_models(session)
        if ids is not None and LLM_MODEL_ID not in ids:
            print(f"[WARN] {LLM_MODEL_ID} 不在模型列表中，可能已下架或 ID 写错")
        print()
        ok = await ping_model(session, LLM_MODEL_ID)

    print()
    print("自检通过，可以运行 test_workflow.py / demo_routes.py" if ok else "自检未通过，先修上面报出的问题")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
