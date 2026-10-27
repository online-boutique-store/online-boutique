#!/usr/bin/env python3
"""Find out which span shape fills Observe AI Agents > Conversations > Preview.

Sends six synthetic conversations (service conversation-preview-test, environment
preview-test) that mirror shoppingassistantservice's invoke_agent span, varying one
thing at a time:

  A-nested-string  today's production shape (control, expect blank)
  B-nested-native  messages as a native OTLP array instead of a JSON string
  C-nested-entity  plus traceloop.entity.input/output (candidate app fix)
  D-root-string    agent span is the trace's top-level span
  E-root-entity    D plus traceloop.entity.input/output
  F-root-native    D with native-array messages

OTLP/HTTP uses the standard library only. kubectl port-forward does NOT work against
observe-agent-forwarder (the collector refuses localhost connections on 4317/4318), so
run the script from inside the cluster:
  kubectl -n observe run preview-test --rm -i --restart=Never --image=python:3.11-slim \
    --env OTLP_ENDPOINT=http://observe-agent-forwarder.observe.svc.cluster.local:4318/v1/traces \
    -- python - < deploy/observe/ai-conversation-preview-test.py
OTLP_GRPC_ENDPOINT selects gRPC instead (needs grpcio + opentelemetry-proto).
"""
import json, os, secrets, time, urllib.request

ENDPOINT = os.environ.get("OTLP_ENDPOINT", "http://localhost:4318/v1/traces")
TOKEN = os.environ.get("OBSERVE_TOKEN")
RUN = time.strftime("%m%d-%H%M%S")
SYSTEM_PROMPT = "You are the shopping assistant for Online Boutique."

def any_value(x):
    if isinstance(x, bool): return {"boolValue": x}
    if isinstance(x, int): return {"intValue": str(x)}
    if isinstance(x, list): return {"arrayValue": {"values": [any_value(v) for v in x]}}
    if isinstance(x, dict): return {"kvlistValue": {"values": [{"key": k, "value": any_value(v)} for k, v in x.items()]}}
    return {"stringValue": str(x)}

def attrs(d): return [{"key": k, "value": any_value(v)} for k, v in d.items()]
def encode(msgs, native): return msgs if native else json.dumps(msgs)

def indexed(prefix, msgs):
    # Same shape telemetry.set_message_attributes writes in production.
    out = {}
    for i, m in enumerate(msgs):
        text = "\n".join(p["content"] for p in m["parts"] if p.get("type") == "text")
        if text:
            out[f"{prefix}.{i}.role"] = m["role"]
            out[f"{prefix}.{i}.content"] = text
    return out

def span(trace_id, span_id, parent_id, name, kind, start, end, attributes):
    s = {"traceId": trace_id, "spanId": span_id, "name": name, "kind": kind,
         "startTimeUnixNano": str(start), "endTimeUnixNano": str(end),
         "attributes": attrs(attributes), "status": {"code": 1}}
    if parent_id: s["parentSpanId"] = parent_id
    return s

def resource(service, spans):
    return {"resource": {"attributes": attrs({"service.name": service,
                                              "deployment.environment.name": "preview-test"})},
            "scopeSpans": [{"scope": {"name": "gen_ai", "version": "test"}, "spans": spans}]}

def build(label, nested, native, entity, user_first=False):
    trace_id = secrets.token_hex(16)
    front_id, agent_id, chat_id = secrets.token_hex(8), secrets.token_hex(8), secrets.token_hex(8)
    conv_id = f"preview-test-{RUN}-{label}"
    user_text = f"[{label}] Find me a salt shaker under $100."
    bot_text = f"[{label}] Here is a salt shaker for $18.99."
    user_msg = {"role": "user", "parts": [{"type": "text", "content": user_text}]}
    # user_first: OTel GenAI semconv shape -- system prompt in gen_ai.system_instructions,
    # gen_ai.input.messages / gen_ai.prompt.N start at the user turn.
    inp = [user_msg] if user_first else [
        {"role": "system", "parts": [{"type": "text", "content": SYSTEM_PROMPT}]}, user_msg]
    out = [{"role": "assistant", "parts": [{"type": "text", "content": bot_text}], "finish_reason": "stop"}]
    now = time.time_ns()
    a_start, a_end = now - 3_000_000_000, now
    c_start, c_end = a_start + 200_000_000, a_end - 200_000_000

    content = {"gen_ai.input.messages": encode(inp, native),
               "gen_ai.output.messages": encode(out, native),
               **indexed("gen_ai.prompt", inp), **indexed("gen_ai.completion", out)}
    if user_first:
        content["gen_ai.system_instructions"] = json.dumps([{"type": "text", "content": SYSTEM_PROMPT}])
    agent_attrs = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "preview-test-agent",
                   "gen_ai.provider.name": "aws.bedrock", "gen_ai.request.model": "test-model",
                   "gen_ai.conversation.id": conv_id, "gen_ai.is_llm_root": "true",
                   "traceloop.span.kind": "agent", **content}
    if entity:
        agent_attrs["traceloop.entity.input"] = user_text
        agent_attrs["traceloop.entity.output"] = bot_text
    chat_attrs = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "aws.bedrock",
                  "gen_ai.request.model": "test-model", "gen_ai.usage.input_tokens": 25,
                  "gen_ai.usage.output_tokens": 12, "traceloop.span.kind": "chat",
                  "llm.request.type": "chat", **content}

    agent_spans = [
        span(trace_id, agent_id, front_id if nested else None,
             "invoke_agent preview-test-agent", 1, a_start, a_end, agent_attrs),
        span(trace_id, chat_id, agent_id, "chat test-model", 3, c_start, c_end, chat_attrs),
    ]
    payload = {"resourceSpans": [resource("conversation-preview-test", agent_spans)]}
    if nested:  # mimic online-boutique: frontend span is the trace's top-level span
        front = span(trace_id, front_id, None, "POST /assistant", 2,
                     a_start - 100_000_000, a_end + 100_000_000, {})
        payload["resourceSpans"].append(resource("conversation-preview-test-frontend", [front]))
    return payload, trace_id, conv_id

GRPC_ENDPOINT = os.environ.get("OTLP_GRPC_ENDPOINT")  # e.g. localhost:4317


def send_grpc(payload):
    # Needs grpcio + opentelemetry-proto; the HTTP path stays stdlib-only.
    import base64, copy
    import grpc
    from google.protobuf import json_format
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc
    p = copy.deepcopy(payload)
    for rs in p["resourceSpans"]:
        for ss in rs["scopeSpans"]:
            for s in ss["spans"]:
                for k in ("traceId", "spanId", "parentSpanId"):
                    if k in s:  # proto JSON encodes bytes as base64, OTLP/HTTP JSON as hex
                        s[k] = base64.b64encode(bytes.fromhex(s[k])).decode()
    req = json_format.ParseDict(p, trace_service_pb2.ExportTraceServiceRequest())
    with grpc.insecure_channel(GRPC_ENDPOINT) as ch:
        trace_service_pb2_grpc.TraceServiceStub(ch).Export(req, timeout=10)
    return "gRPC OK"


def send(payload):
    if GRPC_ENDPOINT:
        return send_grpc(payload)
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
        headers["x-observe-target-package"] = "Tracing"
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status

TESTS = [  # label, nested under frontend, native array, entity input, user first
    ("A-nested-string",    True,  False, False, False),  # today's shape (control, expect blank)
    ("B-nested-native",    True,  True,  False, False),
    ("C-nested-entity",    True,  False, True,  False),
    ("D-root-string",      False, False, False, False),
    ("E-root-entity",      False, False, True,  False),
    ("F-root-native",      False, True,  False, False),
    ("G-nested-userfirst", True,  False, False, True),   # production shape + system_instructions
    ("H-root-userfirst",   False, False, False, True),
]

if __name__ == "__main__":
    # PREVIEW_CASES=G-nested-userfirst,H-root-userfirst sends only those labels.
    only = {c for c in os.environ.get("PREVIEW_CASES", "").split(",") if c}
    for label, nested, native, entity, user_first in TESTS:
        if only and label not in only:
            continue
        payload, trace_id, conv_id = build(label, nested, native, entity, user_first)
        print(f"{label:18} HTTP {send(payload)}  conversation={conv_id}  trace={trace_id}")
        time.sleep(1)
