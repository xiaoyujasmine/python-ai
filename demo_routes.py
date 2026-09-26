"""
Exercises every branch of the routing layer in `agentic_workflows.py`.

Requires the mock server (or any OpenAI-compatible endpoint) to be running:

    python3 mock_server.py --port 8000
    export VLLM_SERVER_URL="http://127.0.0.1:8000/v1/chat/completions"
    python3 demo_routes.py
"""

from env_loader import load_env_verbose

# 先加载 .env，再 import agentic_workflows（后者在模块级读取端点/模型配置）
load_env_verbose()

from agentic_workflows import run_agentic_workflow

CASES = [
    ("pricing / no listing_id", [
        {"role": "user", "content": "Hi, can you tell me the price of one of your listings?"},
    ]),
    ("pricing / listing_id given", [
        {"role": "user", "content": "Hi, can you tell me the price of one of your listings?"},
        {"role": "assistant", "content": "Could you please provide the listing_id of the item you're asking about?"},
        {"role": "user", "content": "Yes, the listing ID is 123456"},
    ]),
    ("pricing / unknown listing_id", [
        {"role": "user", "content": "What is the price of listing 999999?"},
    ]),
    ("scheduling / date and time given", [
        {"role": "user", "content": "I would like to schedule a call on 2026-10-01 at 15:00"},
    ]),
    ("scheduling / missing date and time", [
        {"role": "user", "content": "Can I schedule a call with an agent?"},
    ]),
    ("listing / general question", [
        {"role": "user", "content": "Is there parking near the listing?"},
    ]),
    ("fallback / out of scope", [
        {"role": "user", "content": "What is the weather today?"},
    ]),
]


def main():
    for name, history in CASES:
        print(f"[{name}]")
        print(f"  -> {run_agentic_workflow(history)}")


if __name__ == "__main__":
    main()
