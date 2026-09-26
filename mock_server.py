"""
Minimal OpenAI-compatible mock server so the workflow can be run locally
without a GPU Droplet or a third-party LLM API.

It inspects the incoming system prompt and returns the kind of reply the
real model is instructed to produce, which is enough to exercise the
fan-out / fan-in and routing logic end to end.

Usage:
    python3 mock_server.py [--port 8000] [--verbose]

Then point the workflow at it:
    export VLLM_SERVER_URL="http://127.0.0.1:8000/v1/chat/completions"
"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

VERBOSE = False


def fake_model_reply(messages):
    """Stand-in for the real model: classify + extract, following each prompt's contract."""
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""

    last_user = ""
    for message in reversed(messages):
        if message["role"] == "user":
            last_user = message["content"]
            break

    # pricing_prompt -> 'listing_id: XXXXXX' | clarifying question | 'false'
    if "price of a listing" in system:
        match = re.search(r"\b(\d{6})\b", last_user)
        if match:
            return "listing_id: " + match.group(1)
        if any(word in last_user.lower() for word in ("price", "cost", "how much", "much")):
            return "Could you please provide the listing_id of the item you're asking about?"
        return "false"

    # scheduling_prompt -> 'date: YYYY-MM-DD, time: HH:MM' | clarifying question | 'false'
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
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}

        content = fake_model_reply(body.get("messages", []))
        if VERBOSE:
            model = body.get("model", "-")
            print(f"[{model}] -> {content!r}", flush=True)

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
