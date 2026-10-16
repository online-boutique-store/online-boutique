"""Live probe: does the REAL Bedrock model emit a bad currency under PROMPT_VERSION=v2?

Read-only apart from a handful of small Converse calls (a few cents at most).
Uses your local AWS credentials via the standard chain; prints no secrets.
Executes no tools and touches no backend - it only records the model's choice.

    AWS_REGION=us-west-2 python tests/live_bedrock_probe.py [runs-per-version]
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
PROMPTS = [
    "Please check out now and charge me in Japanese Yen.",
    "Can you check out in Euros?",
    "I'm ready to buy, check out in British Pounds.",
]
client = boto3.client("bedrock-runtime", region_name=config.AWS_REGION)
print(f"model={config.BEDROCK_MODEL_ID} region={config.AWS_REGION} runs/version/prompt={RUNS}")

for version in ("v1", "v2"):
    config.PROMPT_VERSION = version
    tools = agent._converse_tools(incident.tool_definitions())
    seen = Counter()
    for prompt in PROMPTS:
        for _ in range(RUNS):
            resp = client.converse(
                modelId=config.BEDROCK_MODEL_ID,
                system=[{"text": incident.system_prompt()}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                toolConfig={"tools": tools},
                inferenceConfig={"maxTokens": 300, "temperature": 0},
            )
            calls = [c["toolUse"] for c in resp["output"]["message"]["content"] if "toolUse" in c]
            order = [c for c in calls if c["name"] == "place_order"]
            if order:
                seen[repr(order[0]["input"].get("currency_code"))] += 1
            else:
                seen["<no place_order: " + (",".join(c["name"] for c in calls) or "text reply") + ">"] += 1
    total = sum(seen.values())
    bad = sum(n for k, n in seen.items() if k.strip("'").upper() not in {"JPY", "EUR", "GBP"} and not k.startswith("<"))
    print(f"\n{version}: {dict(seen)}")
    print(f"{version}: invalid currency_code in {bad}/{total} calls")
