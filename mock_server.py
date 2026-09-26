"""
零依赖的 OpenAI 兼容假端点：没有 GPU、没有 API key 也能把整条链路跑通。

它的"智能"只有一层正则：看一眼传进来的 system prompt 属于哪一路，
然后按那一路的输出契约编一句回答。对验证 fan-out / fan-in / 路由来说够用了 ——
反正我们要测的是编排逻辑，不是模型能力。

用法：
    python3 mock_server.py [--port 8000] [--verbose]

然后把流程指过来：
    export VLLM_SERVER_URL="http://127.0.0.1:8000/v1/chat/completions"

分流靠的是三路提示里的关键字
（"price of a listing" / "schedule a call" / "question about a listing"），
这三个短语在 ORIGINAL_PROMPTS 和 STRICT_PROMPTS 里都存在，
所以**教程版（test_workflow.py / demo_routes.py）两套变体都能跑 mock**，不用切。

唯一要留意的是 prod 版：它多了个 token 白名单（STRICT_TOKENS=true），
而 mock 在"缺参数"分支上返回的是自由文本追问句（不是 `need_listing_id` 这种 token），
会被白名单判为非法输出 → 落到 fallback 且 degraded=True。
用 prod 版跑 mock 想看追问分支时加 `STRICT_TOKENS=false` 即可。

文件结构：
    fake_model_reply()         假的"模型"：按 system prompt 关键字分流，纯函数、可单测
    ChatCompletionsHandler     HTTP 层：收 POST、调上面那个函数、按 OpenAI 格式包回去
    main()                     起 http.server
"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

VERBOSE = False


def fake_model_reply(messages):
    """假的模型：按 system prompt 判断自己是哪一路，再按该路的输出契约编回答。

    多路分类之所以能并发，就是因为每路只看 system prompt + 会话历史。
    这里用关键字判断"我是哪一路"，等价于真模型读 system prompt 后的自我定位。
    """
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""

    # 分类只看"最近一条用户消息"，和真提示里写的一样
    last_user = ""
    for message in reversed(messages):
        if message["role"] == "user":
            last_user = message["content"]
            break

    # pricing_prompt -> 'listing_id: XXXXXX' | 追问句 | 'false'
    if "price of a listing" in system:
        match = re.search(r"\b(\d{6})\b", last_user)
        if match:
            return "listing_id: " + match.group(1)
        if any(word in last_user.lower() for word in ("price", "cost", "how much", "much")):
            return "Could you please provide the listing_id of the item you're asking about?"
        return "false"

    # scheduling_prompt -> 'date: YYYY-MM-DD, time: HH:MM' | 追问句 | 'false'
    if "schedule a call" in system:
        if not any(word in last_user.lower() for word in ("call", "schedule", "appointment", "meeting")):
            return "false"
        match = re.search(r"(\d{4}-\d{2}-\d{2})\D+(\d{1,2}:\d{2})", last_user)
        if match:
            return f"date: {match.group(1)}, time: {match.group(2)}"
        return "What day and time are you available for the call?"

    # listing_prompt -> 'true' | 'false'
    if "question about a listing" in system:
        return "true" if "listing" in last_user.lower() else "false"

    return "false"


class ChatCompletionsHandler(BaseHTTPRequestHandler):
    """只认 POST /v1/chat/completions（路径不看，端口上来的都当这个接口处理）。"""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}  # 坏请求也照常回一句，方便测客户端的容错

        content = fake_model_reply(body.get("messages", []))
        if VERBOSE:
            model = body.get("model", "-")
            print(f"[{model}] -> {content!r}", flush=True)

        # 严格按 OpenAI 的响应结构拼，客户端才能用同一套解析代码
        payload = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": body.get("model", "mock"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        data = json.dumps(payload).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        if VERBOSE:
            super().log_message(*args)


def main():
    parser = argparse.ArgumentParser(description="Mock OpenAI-compatible chat completions endpoint")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--verbose", action="store_true", help="log each request and the mocked reply")
    args = parser.parse_args()

    global VERBOSE
    VERBOSE = args.verbose

    server = HTTPServer(("127.0.0.1", args.port), ChatCompletionsHandler)
    print(f"Mock vLLM server listening on http://127.0.0.1:{args.port}/v1/chat/completions", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
