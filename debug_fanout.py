"""
Dump the raw fan-out replies behind every routing decision.

When a route looks wrong, the routing code is almost never the culprit -
one of the three classifiers returned something unexpected. This prints
them verbatim so you can see which prompt drifted.

Usage:
    python3 debug_fanout.py              # default case
    python3 debug_fanout.py --case 2     # pick a built-in case
    python3 debug_fanout.py --repeat 3   # sample N times (temperature > 0)
    python3 debug_fanout.py --ask "..."  # custom single user turn
"""

import argparse
import sys

from env_loader import load_env_verbose

load_env_verbose()

from agentic_workflows import SYSTEM_PROMPT_CONFIGURATIONS, LLM_MODEL_ID, call_models

CASES = {
    1: [{"role": "user", "content": "Hi, can you tell me the price of one of your listings?"}],
    2: [
        {"role": "user", "content": "Hi, can you tell me the price of one of your listings?"},
        {"role": "assistant", "content": "Could you please provide the listing_id of the item you're asking about?"},
        {"role": "user", "content": "Yes, the listing ID is 123456"},
    ],
    3: [{"role": "user", "content": "What is the price of listing 999999?"}],
    4: [{"role": "user", "content": "I would like to schedule a call on 2026-10-01 at 15:00"}],
    5: [{"role": "user", "content": "Can I schedule a call with an agent?"}],
    6: [{"role": "user", "content": "Is there parking near the listing?"}],
    7: [{"role": "user", "content": "What is the weather today?"}],
}


def fanout(conversation_history):
    call_list = []
    names = []
    for prompt_name, config in SYSTEM_PROMPT_CONFIGURATIONS.items():
        call_list.append({
            "model_id": LLM_MODEL_ID or config["model_id"],
            "messages": [{"role": "system", "content": config["prompt"]}] + conversation_history,
        })
        names.append(prompt_name)
    return names, call_models(call_list)


def render(name, value):
    """Show invisible characters that break startswith() checks."""
    return f"{value!r}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=int, default=1, choices=sorted(CASES))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--ask", type=str, default=None)
    args = parser.parse_args()

    history = [{"role": "user", "content": args.ask}] if args.ask else CASES[args.case]
    print(f"model  : {LLM_MODEL_ID or '(per-prompt default)'}")
    print(f"history: {len(history)} turn(s)")
    for turn in history:
        print(f"         [{turn['role']}] {turn['content']}")
    print()

    for i in range(args.repeat):
        if args.repeat > 1:
            print(f"--- sample {i + 1}/{args.repeat} ---")
        names, responses = fanout(history)
        for name, value in zip(names, responses):
            flag = ""
            if name == "pricing_prompt" and not value.startswith("listing_id:"):
                flag = "   (不匹配 listing_id: 前缀 -> 走追问分支)"
            if name == "scheduling_prompt" and not value.startswith("date:"):
                flag = ""
            print(f"  {name:<18} {render(name, value)}{flag}")
        print()


if __name__ == "__main__":
    sys.exit(main())
