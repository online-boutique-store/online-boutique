"""Run: python tests/test_agent_local.py   (from src/shoppingassistantservice)

Exercises the full agent path against FAKE Bedrock + FAKE gRPC backends. See
fakes.py for exactly what is and is not being validated.
"""
import json
import logging
import os
import sys

# The botocore Bedrock instrumentation only accepts the literal "true" (verified).
os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"
os.environ["TELEMETRY_REF_KEY"] = "test-ref-key"
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
fakes.start_fake_bedrock()

import config
import telemetry as tel
config.RATE_LIMIT_RPM = 0  # the guided checkout needs many LLM calls per minute
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

# ---- A: browse -> Bedrock -> catalog tool -> backend -> reply (the mandatory checkpoint)
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
check(a["prompt.version"] == "v1" and a["app.prompt.category"] == "browse" and a["app.session.ref"] == tel.session_ref(SID) and "app.session.id" not in a
      and "app.customer.id" not in a and a["app.customer.authenticated"] is False,
      "A agent span app.* + prompt.version; opaque session ref, no raw session id")
check("find me a mug" in a.get("gen_ai.input.messages", "").lower(), "A agent span captured standard input messages")
check(a.get("gen_ai.prompt.0.role") == "user" and "Find me a mug" in a.get("gen_ai.prompt.0.content", "")
      and "gen_ai.prompt.1.role" not in a,
      "A agent span captured indexed input content (user first)")
a_in = json.loads(a.get("gen_ai.input.messages", "[]"))
check(a_in and a_in[0]["role"] == "user" and all(m["role"] != "system" for m in a_in),
      "A agent span input.messages starts with the user and has no system entry")
import incident
check(incident.system_prompt()[:60] in a.get("gen_ai.system_instructions", "")
      and json.loads(a["gen_ai.system_instructions"])[0]["type"] == "text",
      "A agent span carries the system prompt in gen_ai.system_instructions")
check('"role": "assistant"' in a.get("gen_ai.output.messages", "") and "I found Mug" in a.get("gen_ai.output.messages", ""), "A agent span captured standard output messages")
check(a.get("gen_ai.completion.0.role") == "assistant" and "I found Mug" in a.get("gen_ai.completion.0.content", ""),
      "A agent span captured indexed output content")
check(a.get("traceloop.entity.input") == "Find me a mug under $50", "A agent span entity.input is the user's message")
check(incident.system_prompt()[:60] not in a.get("traceloop.entity.input", ""), "A agent span entity.input excludes the system prompt")
check("I found Mug" in a.get("traceloop.entity.output", ""), "A agent span entity.output is the assistant reply")
check(a["gen_ai.usage.input_tokens"] == 240 and a["gen_ai.usage.output_tokens"] == 40, "A tokens summed across both LLM calls")
c0 = chats[0].attributes
conv = a["gen_ai.conversation.id"]
check(all(s.attributes.get("gen_ai.conversation.id") == conv for s in [*chats, tool]),
      "A chat + execute_tool spans carry gen_ai.conversation.id")
check(all(s.attributes.get("user.id") == tel.session_ref(SID) for s in [agent, *chats, tool]),
      "A agent/chat/tool spans carry user.id = opaque session ref for guests")
check(c0.get("gen_ai.request.model") == config.BEDROCK_MODEL_ID and c0.get("gen_ai.usage.input_tokens") == 120, "A chat span model + usage")
check(c0.get("gen_ai.provider.name") == "aws.bedrock", "A chat span gen_ai.provider.name=aws.bedrock (hook)")
check(chats[0].kind == trace.SpanKind.CLIENT, "A chat span is a CLIENT span")
check("find me a mug" in c0.get("gen_ai.input.messages", "").lower(), "A chat span captured prompt content (hook)")
check(c0.get("gen_ai.prompt.0.role") == "user" and "Find me a mug" in c0.get("gen_ai.prompt.0.content", ""),
      "A chat span captured indexed input content, user first (hook)")
c0_in = json.loads(c0.get("gen_ai.input.messages", "[]"))
check(c0_in and c0_in[0]["role"] == "user" and all(m["role"] != "system" for m in c0_in)
      and incident.system_prompt()[:60] in c0.get("gen_ai.system_instructions", ""),
      "A chat span: no system message in inputs; system prompt in gen_ai.system_instructions (hook)")
check(fakes.FakeBedrock.requests and all(r.get("system") and incident.system_prompt()[:60] in r["system"][0]["text"]
                                          for r in fakes.FakeBedrock.requests),
      "A Bedrock requests still carry the system prompt (telemetry-only change)")
check('"tool_call"' in c0.get("gen_ai.output.messages", "") and "search_products" in c0.get("gen_ai.output.messages", ""), "A chat span captured tool_call selection")
check("tool_call_response" in chats[1].attributes.get("gen_ai.input.messages", ""), "A 2nd chat span carries the tool result in its input")
check(tool.attributes["gen_ai.tool.name"] == "search_products" and '"query": "mug"' in tool.attributes["gen_ai.tool.call.arguments"], "A tool span name + arguments")
check("6E92ZMYYFZ" in tool.attributes["gen_ai.tool.call.result"], "A tool span result")
check(any(b.get("app.session.ref") == tel.session_ref(SID) for b in fakes.SEEN_BAGGAGE)
      and not any(k in b for b in fakes.SEEN_BAGGAGE for k in ("app.session.id", "app.customer.email", "app.customer.name")),
      "A baggage reached backend carries the opaque session ref and no raw id / email / name")

# ---- A2: order-history tools are classified and dispatched; guests are told to log in (no error)
code, body, spans = ask("c1a1f2b0-00a2-4a4a-9c1a-1a2b3c4d5e0a", "show my orders", "account")
ot = [s for s in spans if s.name.startswith("execute_tool") and s.attributes.get("gen_ai.tool.name") == "list_orders"]
check(code == 200 and len(ot) == 1 and ot[0].attributes["traceloop.span.kind"] == "tool"
      and "login_required" in ot[0].attributes["gen_ai.tool.call.result"]
      and ot[0].status.status_code != trace.StatusCode.ERROR,
      "A2 list_orders runs as a tool span; guest gets login_required, not an error")
check("log in" in body["content"].lower(), "A2 reply tells the guest to log in")
check(not any(k in ot[0].attributes for k in ("app.customer.email", "app.customer.name", "app.session.id")),
      "A2 tool span has no email / name / raw session id")

# ---- B: guided checkout, NEW customer, v1 (healthy)
import accounts


import pgtest


accounts.store = accounts.AccountStore(pgtest.FakeRedis(), repo=pgtest.new_repo())
EMAIL = "new1@example.com"
EMAIL_H = accounts.email_hash(EMAIL)

ask(SID, "add two mugs to my cart", "cart")
code, body, spans = ask(SID, "I'd like to check out", "checkout_request")
check(body["checkout"]["stage"] == "identify" and by_name(spans, "execute_tool start_checkout"),
      "B start_checkout runs and the response carries the structured checkout stage")
code, body, spans = ask(SID, f"My email is {EMAIL}, please create an account", "checkout_request")
agent = by_name(spans, "invoke_agent")[0]
lk = by_name(spans, "execute_tool lookup_account")[0]
cr = by_name(spans, "execute_tool create_account")[0]
alk, acr = by_name(spans, "account.lookup")[0], by_name(spans, "account.create")[0]
check(lk.parent.span_id == agent.context.span_id and cr.parent.span_id == agent.context.span_id,
      "B lookup_account/create_account are execute_tool children of invoke_agent")
check(alk.parent.span_id == lk.context.span_id and acr.parent.span_id == cr.context.span_id,
      "B account.lookup / account.create spans nest under their execute_tool spans")
check(lk.attributes["traceloop.span.kind"] == "tool" and cr.attributes["traceloop.span.kind"] == "tool",
      "B new tools are classified traceloop.span.kind=tool")
check(EMAIL not in lk.attributes["gen_ai.tool.call.arguments"] and "<email_redacted>" in lk.attributes["gen_ai.tool.call.arguments"],
      "B tool-argument attributes redact the email")
check(EMAIL not in agent.attributes["app.prompt.text"], "B prompt text attribute is email-redacted")
check(cr.attributes["app.customer.type"] == "new" and "app.customer.email_hash" not in cr.attributes
      and cr.attributes["app.checkout.id"], "B create_account span carries customer type + checkout id and NO email hash")
check(acr.attributes["app.account.created"] is True and alk.attributes["app.account.found"] is False,
      "B account spans record found/created")
ask(SID, "Use the Visa ending 0454", "checkout_request")
code, body, spans = ask(SID, "Japanese Yen please", "checkout_request")
cu = by_name(spans, "execute_tool set_currency")[0]
check(cu.attributes["gen_ai.tool.call.arguments"] == '{"currency_code": "JPY"}' and cu.attributes["app.tool.currency_valid"] is True,
      "B v1 model supplied ISO code via set_currency")
code, body, spans = ask(SID, "Yes, place it", "checkout_request")
co = by_name(spans, "execute_tool place_order")[0]
check(by_name(spans, "execute_tool confirm_order") and "ord-123" in body["content"]
      and body["details"]["outcome"] == "ok", "B confirm_order then place_order succeed; reply says so")
check(co.attributes["app.checkout.outcome"] == "success" and co.attributes["app.checkout.value_usd"] == 16.0
      and co.attributes["app.customer.type"] == "new", "B checkout outcome, USD value, customer type")
check(co.attributes["app.checkout.id"] and co.attributes["app.prompt.version"] == "v1"
      and co.attributes["app.conversation.id"], "B place_order carries checkout id, prompt version, conversation id")
check(any(b.get("app.checkout.id") == co.attributes["app.checkout.id"] and b.get("app.prompt.version") == "v1"
          for b in fakes.SEEN_BAGGAGE), "B checkout id + prompt version reached the backend as baggage")
check(len({s.context.trace_id for s in spans}) == 1, "B one trace from Flask to checkout backend")

# ---- C: prompt regression v2, RETURNING customer -> MODEL-chosen invalid currency -> backend error
ask(SID, "add two mugs to my cart", "cart")
ask(SID, "I'd like to check out", "checkout_request", prompt_version="v2")
code, body, spans = ask(SID, f"My email is {EMAIL}", "checkout_request", prompt_version="v2")
check(body["checkout"]["customer_type"] == "returning" and body["checkout"]["stage"] == "currency",
      "C returning customer is recognised; shipping + payment prefilled")
code, body, spans = ask(SID, "Japanese Yen please", "checkout_request", prompt_version="v2")
cu = by_name(spans, "execute_tool set_currency")[0]
check(cu.attributes["gen_ai.tool.call.arguments"] == '{"currency_code": "Japanese Yen"}', "C v2 model emitted the full currency name")
check(cu.attributes["app.tool.args_source"] == "model" and cu.attributes["fault.injected"] is False
      and cu.attributes["app.tool.currency_valid"] is False, "C bad arg labelled as MODEL decision and currency_valid=false")
code, body, spans = ask(SID, "Yes, place it", "checkout_request", prompt_version="v2")
co = by_name(spans, "execute_tool place_order")[0]
agent = by_name(spans, "invoke_agent")[0]
check(co.status.status_code == trace.StatusCode.ERROR and "unsupported currency" in co.status.description + co.attributes["gen_ai.tool.call.result"],
      "C place_order span ERROR with backend reason")
check("ord-" not in body["content"] and body["details"]["outcome"] == "tool_error", "C reply does not claim success; outcome=tool_error")
check(agent.attributes["prompt.version"] == "v2" and agent.attributes["agent.tool_errors"] == 1
      and agent.attributes["app.customer.type"] == "returning", "C agent span prompt.version=v2, tool_errors=1, returning customer")
check('"role": "assistant"' in agent.attributes.get("gen_ai.output.messages", "")
      and "unsupported currency" in agent.attributes["gen_ai.output.messages"],
      "C failed tool flow records the agent's honest assistant response")
cur_client = [s for s in spans if s.kind == trace.SpanKind.CLIENT and "CurrencyService/Convert" in s.name]
check(cur_client and cur_client[0].status.status_code == trace.StatusCode.ERROR
      and cur_client[0].attributes.get("rpc.grpc.status_code") == 3,
      "C downstream Convert CLIENT span is ERROR with INVALID_ARGUMENT in the same trace")
check(len({s.context.trace_id for s in spans}) == 1, "C downstream error stays on the agent's trace")
check(any(e.name == "checkout.terminal" and e.attributes.get("checkout.outcome") == "failed" for e in co.events),
      "C place_order span has a checkout.terminal(failed) event")

# ---- E: after the prompt is rolled back to v1 the customer restates the currency and retries
code, body, spans = ask(SID, "try again please", "failed_checkout_followup", prompt_version="v1")
co = by_name(spans, "execute_tool place_order")[0]
check(co.attributes["app.checkout.outcome"] == "failed",
      "E a bare retry re-uses the stored bad currency and fails again (state is honest)")
ask(SID, "Japanese Yen please", "failed_checkout_followup", prompt_version="v1")
code, body, spans = ask(SID, "Yes, place it", "failed_checkout_followup", prompt_version="v1")
co = by_name(spans, "execute_tool place_order")[0]
check(co.attributes.get("app.checkout.is_retry") is True and co.attributes["app.checkout.outcome"] == "success",
      "E restating the currency under v1 then confirming succeeds and is flagged as retry")

# ---- D: synthetic fault injection under v1 (model correct, app rewrites), NEW guest-less customer
SID2 = "c1a1f2b0-0002-4a4a-9c1a-1a2b3c4d5e02"
ask(SID2, "add two mugs to my cart", "cart", prompt_version="v1")
ask(SID2, "I'd like to check out", "checkout_request", prompt_version="v1")
ask(SID2, "My email is new2@example.com, create an account", "checkout_request", prompt_version="v1")
ask(SID2, "Use the Visa", "checkout_request", prompt_version="v1")
code, body, spans = ask(SID2, "Japanese Yen please", "checkout_request", prompt_version="v1", fault=["force_malformed_currency"])
cu = by_name(spans, "execute_tool set_currency")[0]
check(cu.attributes["fault.injected"] is True and cu.attributes["app.tool.args_source"] == "synthetic_fault_injection",
      "D injected fault labelled synthetic")
check(cu.attributes["gen_ai.tool.call.arguments"] == '{"currency_code": "Japanese Yen"}'
      and json.loads(cu.attributes["app.tool.model_arguments"]) == {"currency_code": "JPY"},
      "D executed arg differs; original model arg preserved")
check(list(cu.attributes["fault.names"]) == ["force_malformed_currency"], "D fault name recorded")
check(by_name(spans, "invoke_agent")[0].attributes["fault.injection.enabled"] is True, "D agent span marks injection enabled")
ask(SID2, "Yes, place it", "checkout_request", prompt_version="v1", fault=["force_malformed_currency"])

# ---- metrics
def agg(name, key):
    out = {}
    for d, v in metric_points(name):
        k = key(d)
        out[k] = out.get(k, 0) + v
    return out


att = agg("ai.checkout.attempts", lambda d: (d["prompt.version"], d["outcome"], d["fault.injected"]))
check(att.get(("v1", "success", False)) == 2 and att.get(("v2", "failed", False)) == 1 and att.get(("v1", "failed", True)) == 1 and att.get(("v1", "failed", False)) == 1,
      f"metrics checkout attempts by version/outcome/injection {att}")
byc = agg("ai.checkout.attempts", lambda d: (d["customer_type"], d["outcome"]))
check(byc.get(("returning", "failed")) == 2 and byc.get(("returning", "success")) == 1 and byc.get(("new", "success")) == 1 and byc.get(("new", "failed")) == 1, f"metrics attempts by customer_type {byc}")
val = agg("ai.checkout.value_usd", lambda d: (d["prompt.version"], d["outcome"]))
check(val.get(("v2", "failed")) == 16.0, f"metrics failed checkout value at risk {val}")
check(sum(v for _, v in metric_points("ai.checkout.retries")) == 2, "metrics customer retries (bare retry + restated retry)")
check(sum(v for _, v in metric_points("ai.checkout.recovered_value_usd")) == 16.0, "metrics recovered value recorded")
check(any(d["tool.name"] == "place_order" for d, _ in metric_points("ai.tool.errors")), "metrics tool error counter")
check(sum(v for d, v in metric_points("ai.assistant.tokens") if d["direction"] == "input") > 0, "metrics token counter")
stages = agg("ai.checkout.funnel.stage", lambda d: d["stage"])
check(stages.get("identify") == 3 and stages.get("placing") == 3 and stages.get("confirm") == 3,
      f"funnel counts each stage ONCE per flow (3 flows, retry not recounted) {stages}")
pref = agg("ai.checkout.funnel.stage", lambda d: (d["stage"], d["prefilled"]))
check(pref.get(("shipping", True)) == 1 and pref.get(("payment", True)) == 1, f"funnel marks returning-customer prefilled stages {pref}")
term = agg("ai.checkout.terminal", lambda d: d["outcome"])
check(term == {"completed": 2, "failed": 3}, f"exactly one terminal outcome per attempt (C fail, E fail, D fail, B+E ok) {term}")
lk_m = agg("ai.account.lookups", lambda d: d["outcome"])
cr_m = agg("ai.account.creations", lambda d: d["outcome"])
check(lk_m == {"not_found": 2, "found": 1} and cr_m == {"created": 2}, f"account lookup/creation metrics {lk_m} {cr_m}")
cur_m = agg("ai.checkout.currency_selections", lambda d: d["currency.valid"])
check(cur_m == {True: 2, False: 2}, f"currency selection metrics by validity {cur_m}")
names = {m.name for rm in reader.get_metrics_data().resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
check({"ai.assistant.request.duration", "ai.llm.call.duration", "ai.tool.duration"} <= names, "metrics latency histograms exist")

# ---- classification attributes the AI Agents tab depends on
chat = by_name(spans, "chat ")[0]
check(chat.attributes.get("traceloop.span.kind") == "chat" and chat.attributes.get("llm.request.type") == "chat"
      and chat.attributes.get("llm.request.model") == config.BEDROCK_MODEL_ID
      and chat.attributes.get("llm.response.model") == config.BEDROCK_MODEL_ID,
      "AI Agents attrs preserved on chat spans (task/chat/request+response model)")
check(by_name(spans, "invoke_agent")[0].attributes["traceloop.span.kind"] == "agent"
      and by_name(spans, "invoke_agent")[0].attributes["gen_ai.is_llm_root"] == "true", "AI Agents attrs preserved on agent span")

# ---- logs
done = [r for r in records if r.getMessage() == "assistant.request.completed"]
fields = done[-1].fields
check(bool(done) and fields["app.session.ref"] == tel.session_ref(SID2) and "app.session.id" not in fields
                    and fields["UserId"] == tel.session_ref(SID2) and "prompt" not in {k.lower() for k in fields}, "logs completed event w/o prompt text duplication")
check(any(r.getMessage() == "checkout.attempt" and r.fields["checkout.outcome"] == "failed" and r.fields["app.checkout.id"] for r in records),
      "logs structured checkout.attempt failed event with checkout id")
for evt in ("account.lookup", "account.create", "checkout.stage", "checkout.terminal", "checkout.currency_selected"):
    check(any(r.getMessage() == evt and r.fields.get("app.session.ref") for r in records), f"logs {evt} event with session correlation")
check(not any(EMAIL in json.dumps(r.fields, default=str) for r in records), "logs never contain the raw customer email")
check(not any(SID in json.dumps(r.fields, default=str) or SID2 in json.dumps(r.fields, default=str) for r in records),
      "logs never contain a raw session id")

import agent  # noqa: E402

# ---- F: Postgres outage mid-conversation for a LOGGED-IN customer, then recovery
os.environ["ACCOUNT_API_SECRET"] = "internal-test-secret"
_internal = {"X-Account-Api-Secret": "internal-test-secret"}
_out_email = "outage.shopper@example.com"
_reg = client.post("/account/register", headers=_internal, json={
    "first_name": "Outage", "last_name": "Shopper", "email": _out_email, "password": "Sup3rPassw0rd!", "confirm_password": "Sup3rPassw0rd!",
    "street_address": "1 Main St", "city": "Austin", "state": "TX", "zip_code": "73301", "country": "United States"})
check(_reg.status_code == 201, "F register logged-in customer (Postgres up)")
_tok = (_reg.get_json() or {}).get("session_token", "")
SIDF = "c1a1f2b0-000f-4a4a-9c1a-1a2b3c4d5e0f"


def ask_auth(text, category="checkout_request"):
    config.PROMPT_VERSION = "v1"
    config.FAULT_INJECTION = set()
    exporter.clear()
    r = client.post("/", json={"message": text, "session_id": SIDF, "prompt_category": category},
                    headers={"X-Account-Session": _tok})
    return r.status_code, r.get_json(), exporter.get_finished_spans()


def agent_span(spans):
    return [s for s in spans if s.name.startswith("invoke_agent")][-1]


pgtest.cluster().stop()
try:
    code, body, spans = ask_auth("I'd like to check out")
    a = agent_span(spans)
    # baggage stamping exports this as the string "True" (pre-existing behaviour, kept unchanged)
    check(code == 200 and a.attributes.get("app.account.authenticated") in (True, "True")
          and "app.customer.email" not in a.attributes and "app.customer.name" not in a.attributes,
          "F outage: authenticated identity preserved on invoke_agent span (from Redis session), without email/name")
    check(a.attributes.get("app.account.profile_available") is False, "F outage: app.account.profile_available=false")
    code, body, spans = ask_auth(f"my email is {_out_email}")
    tools = [s for s in spans if s.name.startswith("execute_tool")]
    names = [s.attributes.get("gen_ai.tool.name") for s in tools]
    st = agent.get_session(SIDF).checkout
    check("lookup_account" in names and "continue_as_guest" not in names, "F outage: lookup attempted, no guest tool call")
    check(st.customer_type == "returning" and st.email == _out_email, "F outage: checkout kept as returning customer, not guest")
    check(not any(s.status.status_code == trace.StatusCode.ERROR for s in tools if s.attributes.get("gen_ai.tool.name") == "lookup_account"),
          "F outage: lookup_account reports account_unavailable (not a tool error)")
    check("unavailable" in (body or {}).get("content", "").lower(), "F outage: reply tells the customer details are unavailable")
    check(agent_span(spans).attributes.get("gen_ai.agent.name") and agent_span(spans).attributes.get("traceloop.span.kind") == "agent",
          "F outage: AI Agents classification unchanged")
finally:
    pgtest.cluster().start()
recovered = None
for _ in range(6):
    _, _, spans = ask_auth("show me mugs", "browse")
    if agent_span(spans).attributes.get("app.account.profile_available") is True:
        recovered = agent_span(spans)
        break
check(recovered is not None and recovered.attributes.get("app.account.authenticated") in (True, "True"),
      "F recovery: profile available again with the same session, no re-login")
prof = client.get("/account/profile", headers={**_internal, "X-Account-Session": _tok})
check(prof.status_code == 200 and prof.get_json()["account"]["email"] == _out_email, "F recovery: /account/profile 200")

# ---- guard rails
code, body, _ = ask("x", "")
check(code == 400, "empty message -> 400")
config.PROMPT_VERSION = "v1"
import agent
import boto3
agent._client["c"] = None
boto3.DEFAULT_SESSION = None  # boto3 caches resolved credentials on its default session
for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
    os.environ.pop(k, None)
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull  # isolate from the developer's real AWS config
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
code, body, _ = ask("y", "hello")
check(code == 503 and body["details"]["outcome"] == "not_configured", "no AWS credentials -> 503 not_configured")

print()
print("ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
