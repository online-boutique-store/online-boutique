"""Run: python tests/test_agent_local.py   (from src/shoppingassistantservice)

Exercises the full agent path against FAKE Cortex + FAKE gRPC backends. See
fakes.py for exactly what is and is not being validated.
"""
import json
import logging
import os
import sys

os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "SPAN_ONLY"
os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "gen_ai_latest_experimental"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

exporter = InMemorySpanExporter()
tp = TracerProvider()
tp.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(tp)
reader = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))

import fakes

fakes.start_backends()
fakes.start_fake_cortex()

import config
import shoppingassistantservice as svc

records = []


class Capture(logging.Handler):
    def emit(self, record):
        records.append(record)


logging.getLogger("assistant").addHandler(Capture())

client = svc.app.test_client()
failures = []


def check(cond, label):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def ask(sid, text, category="test", prompt_version=None, fault=None):
    config.PROMPT_VERSION = prompt_version or "v1"
    config.FAULT_INJECTION = set(fault or [])
    exporter.clear()
    r = client.post("/", json={"message": text, "session_id": sid, "prompt_category": category})
    return r.status_code, r.get_json(), exporter.get_finished_spans()


def by_name(spans, prefix):
    return [s for s in spans if s.name.startswith(prefix)]


def metric_points(name):
    out = []
    data = reader.get_metrics_data()
    for rm in (data.resource_metrics if data else []):
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out += [(dict(p.attributes), p.value) for p in m.data.data_points]
    return out


SID = "c1a1f2b0-0001-4a4a-9c1a-1a2b3c4d5e01"

# ---- A: browse -> Cortex -> catalog tool -> backend -> reply (the mandatory checkpoint)
code, body, spans = ask(SID, "Find me a mug under $50", "browse")
agent = by_name(spans, "invoke_agent")[0]
tool = by_name(spans, "execute_tool search_products")[0]
chats = by_name(spans, "chat ")
grpc_client = [s for s in spans if s.kind == trace.SpanKind.CLIENT and "SearchProducts" in s.name]
grpc_server = [s for s in spans if s.kind == trace.SpanKind.SERVER and "SearchProducts" in s.name]
check(code == 200 and "6E92ZMYYFZ" in body["content"], "A reply returned with product id")
check(len({s.context.trace_id for s in spans}) == 1, "A single trace across flask/agent/chat/tool/grpc")
check(tool.parent.span_id == agent.context.span_id, "A execute_tool is child of invoke_agent")
check(all(c.parent.span_id == agent.context.span_id for c in chats) and len(chats) == 2, "A two chat spans, children of invoke_agent")
check(grpc_client and grpc_client[0].parent.span_id == tool.context.span_id, "A grpc client span is child of execute_tool")
check(grpc_server and grpc_server[0].parent.span_id == grpc_client[0].context.span_id, "A backend server span continues the client span")
check(body["trace_id"] == format(agent.context.trace_id, "032x"), "A response trace_id matches the trace")
a = agent.attributes
check(a["gen_ai.operation.name"] == "invoke_agent" and a["gen_ai.agent.name"] == "shopping_assistant", "A agent span gen_ai attrs")
check(a["prompt.version"] == "v1" and a["app.prompt.category"] == "browse" and a["app.session.id"] == SID and a["app.customer.id"] == SID, "A agent span app.* + prompt.version")
check(a["gen_ai.usage.input_tokens"] == 240 and a["gen_ai.usage.output_tokens"] == 40, "A tokens summed across both LLM calls")
c0 = chats[0].attributes
check(c0.get("gen_ai.request.model") == "claude-sonnet-4-5" and c0.get("gen_ai.usage.input_tokens") == 120, "A chat span model + usage")
check("find me a mug" in c0.get("gen_ai.input.messages", "").lower(), "A chat span captured prompt content (SPAN_ONLY)")
check("search_products" in chats[0].attributes.get("gen_ai.output.messages", ""), "A chat span captured tool_call selection")
check(tool.attributes["gen_ai.tool.name"] == "search_products" and '"query": "mug"' in tool.attributes["gen_ai.tool.call.arguments"], "A tool span name + arguments")
check("6E92ZMYYFZ" in tool.attributes["gen_ai.tool.call.result"], "A tool span result")
check(any(b.get("app.session.id") == SID and b.get("app.customer.id") == SID for b in fakes.SEEN_BAGGAGE), "A baggage reached backend")

# ---- B: healthy checkout (v1)
ask(SID, "add two mugs to my cart", "cart")
code, body, spans = ask(SID, "please check out in Japanese Yen", "checkout")
co = by_name(spans, "execute_tool place_order")[0]
check("ord-123" in body["content"] and body["details"]["outcome"] == "ok", "B v1 checkout succeeds and reply says so")
check(co.attributes["gen_ai.tool.call.arguments"] == '{"currency_code": "JPY"}', "B v1 model supplied ISO code")
check(co.attributes["app.tool.currency_valid"] is True and co.attributes["fault.injected"] is False, "B currency_valid true, not injected")
check(co.attributes["app.checkout.outcome"] == "success" and co.attributes["app.checkout.value_usd"] == 16.0, "B checkout outcome + USD value")

# ---- C: prompt regression v2 -> MODEL-chosen invalid currency -> genuine backend error
ask(SID, "add two mugs to my cart", "cart")
code, body, spans = ask(SID, "please check out in Japanese Yen", "checkout", prompt_version="v2")
co = by_name(spans, "execute_tool place_order")[0]
agent = by_name(spans, "invoke_agent")[0]
check(co.attributes["gen_ai.tool.call.arguments"] == '{"currency_code": "Japanese Yen"}', "C v2 model emitted the full currency name")
check(co.attributes["app.tool.args_source"] == "model" and co.attributes["fault.injected"] is False, "C bad arg labelled as MODEL decision, not injection")
check(co.attributes["app.tool.currency_valid"] is False, "C currency_valid false")
check(co.status.status_code == trace.StatusCode.ERROR and "unsupported currency" in co.status.description + co.attributes["gen_ai.tool.call.result"], "C tool span ERROR with backend reason")
check("ord-" not in body["content"] and body["details"]["outcome"] == "tool_error", "C reply does not claim success; outcome=tool_error")
check(agent.attributes["prompt.version"] == "v2" and agent.attributes["agent.tool_errors"] == 1, "C agent span prompt.version=v2, tool_errors=1")
cur_client = [s for s in spans if s.kind == trace.SpanKind.CLIENT and "CurrencyService/Convert" in s.name]
check(cur_client and cur_client[0].status.status_code == trace.StatusCode.ERROR
      and cur_client[0].attributes.get("rpc.grpc.status_code") == 3,
      "C downstream Convert CLIENT span is ERROR with INVALID_ARGUMENT in the same trace")
# NOTE: the SERVER-side Convert span is intentionally not asserted here. The Python
# instrumentation leaves INVALID_ARGUMENT server spans UNSET (client-fault semantics),
# but the REAL currencyservice is Node + @opentelemetry/instrumentation-grpc@0.52.1,
# which marks every non-zero status ERROR (utils.js:_grpcStatusCodeToOpenTelemetryStatusCode).
# That is verified from source only; confirm live after deploy (docs/ai-demo.md).
check(len({s.context.trace_id for s in spans}) == 1, "C downstream error stays on the agent's trace")

# ---- E: customer retry succeeds -> recovered value
code, body, spans = ask(SID, "try again, check out in Japanese Yen", "checkout_followup", prompt_version="v1")
# cart was NOT emptied by the failed attempt, so the retry has value
co = by_name(spans, "execute_tool place_order")[0]
check(co.attributes.get("app.checkout.is_retry") is True and co.attributes["app.checkout.outcome"] == "success", "E retry after failure succeeds and is flagged as retry")

# ---- D: synthetic fault injection under v1 (model correct, app rewrites)
SID2 = "c1a1f2b0-0002-4a4a-9c1a-1a2b3c4d5e02"
ask(SID2, "add two mugs to my cart", "cart", prompt_version="v1")
code, body, spans = ask(SID2, "please check out in Japanese Yen", "checkout", prompt_version="v1", fault=["force_malformed_currency"])
co = by_name(spans, "execute_tool place_order")[0]
check(co.attributes["fault.injected"] is True and co.attributes["app.tool.args_source"] == "synthetic_fault_injection", "D injected fault labelled synthetic")
check(co.attributes["gen_ai.tool.call.arguments"] == '{"currency_code": "Japanese Yen"}' and json.loads(co.attributes["app.tool.model_arguments"]) == {"currency_code": "JPY"}, "D executed arg differs; original model arg preserved")
check(list(co.attributes["fault.names"]) == ["force_malformed_currency"], "D fault name recorded")
check(by_name(spans, "invoke_agent")[0].attributes["fault.injection.enabled"] is True, "D agent span marks injection enabled")

# ---- metrics
att = {(d["prompt.version"], d["outcome"], d["fault.injected"]): v for d, v in metric_points("ai.checkout.attempts")}
check(att.get(("v1", "success", False)) == 2 and att.get(("v2", "failed", False)) == 1 and att.get(("v1", "failed", True)) == 1, f"metrics checkout attempts by version/outcome/injection {att}")
val = {(d["prompt.version"], d["outcome"]): v for d, v in metric_points("ai.checkout.value_usd")}
check(val.get(("v2", "failed")) == 32.0 or val.get(("v2", "failed")) == 16.0, f"metrics failed checkout value present {val}")
check(sum(v for _, v in metric_points("ai.checkout.retries")) == 1, "metrics one customer retry")
check(sum(v for _, v in metric_points("ai.checkout.recovered_value_usd")) > 0, "metrics recovered value recorded")
check(any(d["tool.name"] == "place_order" for d, _ in metric_points("ai.tool.errors")), "metrics tool error counter")
check(sum(v for d, v in metric_points("ai.assistant.tokens") if d["direction"] == "input") > 0, "metrics token counter")

# ---- logs
done = [r for r in records if r.getMessage() == "assistant.request.completed"]
fields = done[-1].fields
check(bool(done) and fields["app.session.id"] == SID2 and "prompt" not in {k.lower() for k in fields}, "logs completed event w/o prompt text duplication")
check(any(r.getMessage() == "checkout.attempt" and r.fields["checkout.outcome"] == "failed" for r in records), "logs structured checkout.attempt failed event")

# ---- guard rails
code, body, _ = ask("x", "")
check(code == 400, "empty message -> 400")
config.PROMPT_VERSION = "v1"
os.environ["CORTEX_PAT"] = ""
config.CORTEX_PAT = ""
code, body, _ = ask("y", "hello")
check(code == 503 and body["details"]["outcome"] == "not_configured", "missing PAT -> 503 not_configured")

print()
print("ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
