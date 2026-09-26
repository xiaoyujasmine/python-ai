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

SYSTEM_PROMPT_CONFIGURATIONS = (
    STRICT_PROMPTS if PROMPT_VARIANT == "strict" else ORIGINAL_PROMPTS
)


# Step 2 - Writing the asynchronous call logic.

async def _call_single_model(call_spec):
    """Make an async call to the Digital Ocean GPU droplet with a given model and messages"""
    model_id = call_spec["model_id"]
    messages = call_spec["messages"]

    payload = {
        "model": model_id,
        "messages": messages,
        "max_tokens": 100,
        "temperature": 0.1
    }

    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    async with aiohttp.ClientSession() as session:
        async with session.post(VLLM_SERVER_URL, json=payload, headers=headers) as response:
            result = await response.json()

            message = result["choices"][0]["message"]["content"]
            # Real models routinely emit a leading newline ("\nfalse") or wrap
            # output in markdown. Without this strip, "\nfalse" != "false" and
            # every route in Step 5 is treated as a hit.
            return message.strip()


async def _call_models_async(call_list):
    """Call multiple models asynchronously and return responses in the same order"""
    # Create tasks for all model calls
    tasks = [_call_single_model(call_spec) for call_spec in call_list]

    # Run all tasks concurrently and return responses in order
    responses = await asyncio.gather(*tasks)
    return responses


def call_models(call_list):
    """Synchronous wrapper around the async fan-out. Do not call this from
    inside a running event loop (use `_call_models_async` directly instead)."""
    return asyncio.run(_call_models_async(call_list))


# Step 4 + Step 5 - Processing user input through the models, then routing.

def run_agentic_workflow(conversation_history):
    model_calls_list = []
    prompt_names = []  # Keep track of prompt order for response mapping

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

    prompt_responses = call_models(model_calls_list)

    # Map responses to their respective prompts
    pricing_response = prompt_responses[prompt_names.index("pricing_prompt")]
    scheduling_response = prompt_responses[prompt_names.index("scheduling_prompt")]
    listing_response = prompt_responses[prompt_names.index("listing_prompt")]

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
