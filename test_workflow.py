"""
Step 6 - Testing your workflow.

Run with:  python3 test_workflow.py

Both scenarios from the tutorial are executed in one pass:
    1. User asks about a price without giving a listing_id -> the pricing
       prompt should ask them for it.
    2. User supplies the listing ID in a follow-up turn -> the workflow should
       resolve it against the example price database.
"""

from env_loader import load_env_verbose

# 先加载 .env，再 import agentic_workflows（后者在模块级读取端点/模型配置）
load_env_verbose()

from agentic_workflows import run_agentic_workflow


def main():
    # Case 1 - no listing_id yet
    conversation_history = [
        {"role": "user", "content": "Hi, can you tell me the price of one of your listings?"},
    ]
    final_response = run_agentic_workflow(conversation_history)
    print(f"Response: {final_response}")

    # Case 2 - complete conversation, listing ID provided
    conversation_history = [
        {"role": "user", "content": "Hi, can you tell me the price of one of your listings?"},
        {"role": "assistant", "content": "Could you please provide the listing_id of the item you're asking about?"},
        {"role": "user", "content": "Yes, the listing ID is 123456"},
    ]
    final_response = run_agentic_workflow(conversation_history)
    print(f"Response: {final_response}")


if __name__ == "__main__":
    main()
