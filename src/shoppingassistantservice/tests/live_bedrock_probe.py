"""Live probe: does the REAL Bedrock model emit a bad currency under PROMPT_VERSION=v2?

Plays the agent's tool loop forward with CANNED tool results (a cart with two
mugs, etc.) so the model reaches place_order the way it would in the service.
Nothing is executed against any backend - only the model's place_order
argument is recorded. Costs a few cents. Uses your local AWS credential chain
and prints no secrets.

    AWS_REGION=us-west-2 python tests/live_bedrock_probe.py [runs-per-prompt]
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import boto3

import agent
import config
import incident

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
MAX_STEPS = 4
PROMPTS = [
    "Please check out now and charge me in Japanese Yen.",
    "Can you check out in Euros?",
    "I'm ready to buy, check out in British Pounds.",
]
VALID = {"JPY", "EUR", "GBP"}
MUG = {"id": "6E92ZMYYFZ", "name": "Mug", "description": "A mug", "price_usd": 8.0, "categories": ["kitchen"]}
CANNED = {
    "get_cart": {"items": [{"product_id": "6E92ZMYYFZ", "name": "Mug", "quantity": 2, "unit_price_usd": 8.0}],
                 "total_usd": 16.0},
    "search_products": {"results": [MUG], "count": 1},
    "get_product": MUG,
    "add_to_cart": {"added": {"product_id": "6E92ZMYYFZ", "quantity": 2}},
    "convert_price": {"converted": {"currency_code": "XXX", "amount": 1.0}},
}

client = boto3.client("bedrock-runtime", region_name=config.AWS_REGION)
print(f"model={config.BEDROCK_MODEL_ID} region={config.AWS_REGION} runs/prompt={RUNS}")


def first_place_order(version, prompt):
    config.PROMPT_VERSION = version
    tools = agent._converse_tools(incident.tool_definitions())
    msgs = [{"role": "user", "content": [{"text": prompt}]}]
    path = []
    for _ in range(MAX_STEPS):
        resp = client.converse(
            modelId=config.BEDROCK_MODEL_ID, system=[{"text": incident.system_prompt()}],
            messages=msgs, toolConfig={"tools": tools},
            inferenceConfig={"maxTokens": 400, "temperature": 0})
        msg = resp["output"]["message"]
        calls = [c["toolUse"] for c in msg.get("content", []) if "toolUse" in c]
        if not calls:
            return None, path + ["<text reply>"]
        for c in calls:
            path.append(c["name"])
            if c["name"] == "place_order":
                return c["input"].get("currency_code"), path
        msgs.append(msg)
        msgs.append({"role": "user", "content": [
            {"toolResult": {"toolUseId": c["toolUseId"], "content": [{"json": CANNED.get(c["name"], {})}]}}
            for c in calls]})
    return None, path + ["<step limit>"]


for version in ("v1", "v2"):
    codes, paths = Counter(), Counter()
    for prompt in PROMPTS:
        for _ in range(RUNS):
            code, path = first_place_order(version, prompt)
            codes[repr(code) if code is not None else "<never called place_order>"] += 1
            paths[" > ".join(path)] += 1
    reached = sum(n for k, n in codes.items() if not k.startswith("<"))
    bad = sum(n for k, n in codes.items() if not k.startswith("<") and k.strip("'") not in VALID)
    print(f"\n{version} currency_code chosen: {dict(codes)}")
    print(f"{version} tool paths: {dict(paths)}")
    print(f"{version}: reached place_order {reached}/{sum(codes.values())}, invalid code in {bad}/{reached or 1}")
