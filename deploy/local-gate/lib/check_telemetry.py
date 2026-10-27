#!/usr/bin/env python3
"""Summarize local collector output and enforce basic local redaction invariants."""
import json
import os
import re
import sys
from collections import Counter

trace_file, metric_file, log_file = sys.argv[1:4]
spans=[]; services=Counter(); names=Counter(); trace_ids=set()
for line in open(trace_file):
    if not line.strip(): continue
    try:
        doc=json.loads(line)
    except ValueError:
        continue  # collector may be mid-write on the last line
    for rs in doc.get("resourceSpans", []):
        resource={a["key"]: next(iter(a["value"].values())) for a in rs.get("resource", {}).get("attributes", [])}
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                attrs={a["key"]: next(iter(a["value"].values())) for a in span.get("attributes", [])}
                service=resource.get("service.name", "")
                services[service]+=1; names[span.get("name", "")]+=1; trace_ids.add(span.get("traceId"))
                spans.append((service, span, attrs))
required=["invoke_agent shopping_assistant", "chat us.anthropic.claude-sonnet-4-6"]
for name in required:
    if not any(span[1].get("name") == name for span in spans):
        raise SystemExit(f"missing required span: {name}")
if not any(n.startswith("execute_tool") for n in names):
    raise SystemExit("missing execute_tool spans")
if not any(n.startswith("accounts.db.") for n in names):
    raise SystemExit("missing accounts.db spans")
if not any(service == "frontend" for service, _, _ in spans):
    raise SystemExit("missing frontend spans")

blob = open(trace_file).read() + open(metric_file).read() + open(log_file).read()
for value in os.environ.get("LOCAL_GATE_REDACTION_VALUES", "").split("|"):
    if value and value in blob:
        raise SystemExit("sensitive value found in local telemetry")
print(json.dumps({"traces": len(trace_ids), "spans": len(spans), "services": services, "top_names": names.most_common(20), "local_export": "pass", "observe_ingestion": "not_tested"}, default=dict, indent=2))
