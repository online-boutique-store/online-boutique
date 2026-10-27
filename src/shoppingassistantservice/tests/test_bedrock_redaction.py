"""Run: python tests/test_bedrock_redaction.py   (from src/shoppingassistantservice)

End-to-end redaction proof for AUTOMATIC Bedrock instrumentation. Uses the real
telemetry.init() pipeline (sanitizing span exporter + sanitizing log processor +
real OTLP gRPC exporters) pointed at a local OTLP receiver that writes the
captured telemetry to files; those files - not sanitizer calls - are scanned.
"""
import json
import os
import pathlib
import sys
import tempfile
import time
from concurrent import futures

import grpc

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

from google.protobuf.json_format import MessageToJson
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2, logs_service_pb2_grpc
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2, metrics_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc

OUT = pathlib.Path(os.environ.get("REDACTION_CAPTURE_DIR") or tempfile.mkdtemp(prefix="redaction-"))
OUT.mkdir(parents=True, exist_ok=True)
for f in ("traces.jsonl", "logs.jsonl"):
    (OUT / f).write_text("")


class Traces(trace_service_pb2_grpc.TraceServiceServicer):
    def Export(self, req, ctx):
        with open(OUT / "traces.jsonl", "a") as fh:
            fh.write(MessageToJson(req, indent=None) + "\n")
        return trace_service_pb2.ExportTraceServiceResponse()


class Logs(logs_service_pb2_grpc.LogsServiceServicer):
    def Export(self, req, ctx):
        with open(OUT / "logs.jsonl", "a") as fh:
            fh.write(MessageToJson(req, indent=None) + "\n")
        return logs_service_pb2.ExportLogsServiceResponse()


class Metrics(metrics_service_pb2_grpc.MetricsServiceServicer):
    def Export(self, req, ctx):
        return metrics_service_pb2.ExportMetricsServiceResponse()


server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
trace_service_pb2_grpc.add_TraceServiceServicer_to_server(Traces(), server)
logs_service_pb2_grpc.add_LogsServiceServicer_to_server(Logs(), server)
metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(Metrics(), server)
port = server.add_insecure_port("127.0.0.1:0")
server.start()

os.environ.update({
    "ENABLE_TRACING": "1", "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{port}",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
    "OTEL_METRICS_EXPORTER": "none", "OTEL_BSP_SCHEDULE_DELAY": "200", "OTEL_BLRP_SCHEDULE_DELAY": "200",
})

import fakes

fakes.start_backends()
fakes.start_fake_bedrock()

import accounts
import config
import pgtest

config.RATE_LIMIT_RPM = 0
import shoppingassistantservice as svc
import telemetry_sanitize as san
from opentelemetry import _logs, trace

assert svc.exporting, "telemetry.init must have configured real OTLP export"
accounts.store = accounts.AccountStore(pgtest.FakeRedis(), repo=pgtest.new_repo())
client = svc.app.test_client()

failures = []


def check(cond, label):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def ask(sid, text, category="checkout_request"):
    config.PROMPT_VERSION = "v1"
    config.FAULT_INJECTION = set()
    r = client.post("/", json={"message": text, "session_id": sid, "prompt_category": category})
    return r.status_code, r.get_json()


# Unique values that must never appear in exported telemetry.
STREET = "7431 Quillfeather Lane"
CITY, ST, ZIP = "Zyxburg", "TX", "78945"
EMAIL = "redaction.shopper@example.com"
SID = "c1a1f2b0-00aa-4a4a-9c1a-1a2b3c4d5e99"
SECRET_PW = "Hunter2-NeverExport!"
SECRET_CODE = "ACT-9Z8Y7X-NEVER"

ask(SID, "add two mugs to my cart", "cart")
ask(SID, "I'd like to check out")
code, body = ask(SID, f"My email is {EMAIL}, please create an account and ship to {STREET}, {CITY}, {ST} {ZIP}")
ask(SID, "Use my visa please")                    # address now repeats in prompt history
ask(SID, "I want to pay in yen")                  # ... and again
ask(SID, f"my address is still {STREET}, {CITY}, {ST} {ZIP} - what are you holding?", "browse")

# --- behavior preserved: Bedrock saw the real address, checkout got it ----------
history_texts = json.dumps(fakes.FakeBedrock.requests)
check(STREET in history_texts, "Bedrock received the real address in conversation context (not redacted)")
tool_inputs = [c["toolUse"]["input"] for r in fakes.FakeBedrock.requests for m in r["messages"]
               for c in m["content"] if "toolUse" in c and c["toolUse"]["name"] == "create_account"]
check(tool_inputs and tool_inputs[0]["street_address"] == STREET and tool_inputs[0]["zip_code"] == ZIP,
      "create_account tool call carries the real address (app behavior unchanged)")

# --- direct Bedrock calls: nested tool-call args and tool results ----------------
import boto3

br = boto3.client("bedrock-runtime", endpoint_url=os.environ["BEDROCK_ENDPOINT_URL"], region_name="us-west-2")
NESTED_STREET = "99 Nestedwood Terrace"
br.converse(modelId="fake.model", messages=[
    {"role": "user", "content": [{"text": f"Ship my order to {NESTED_STREET}, Elsewhere, OH 43004"}]},
    {"role": "assistant", "content": [{"toolUse": {"toolUseId": "tu_x", "name": "set_shipping_address", "input": {
        "customer": {"name": "Demo Shopper", "shipping": {"street_address": NESTED_STREET, "city": "Elsewhere",
                                                          "zip_code": "43004"}},
        "note": json.dumps({"deliver_to": f"{NESTED_STREET}, Elsewhere, OH 43004"})}}}]},
    {"role": "user", "content": [{"toolResult": {"toolUseId": "tu_x", "content": [
        {"json": {"status": "ok", "address": {"street_address": NESTED_STREET, "city": "Elsewhere"},
                  "text": f"Saved {NESTED_STREET}", "password_hash": "pbkdf2$abc", "claim_code": SECRET_CODE}}]}}]},
    {"role": "user", "content": [{"text": "thanks"}]},
])
# Mixed text + tool call in ONE message (the live-EKS leak shape): the legacy
# prompt content joins text with the serialized tool-call JSON, so the whole
# string is not JSON. City/state/ZIP must still be masked.
br.converse(modelId="fake.model", messages=[
    {"role": "user", "content": [{"text": "create my account please"}]},
    {"role": "assistant", "content": [
        {"text": "Creating your account now."},
        {"toolUse": {"toolUseId": "tu_m", "name": "create_account", "input": {
            "name": "Demo Shopper", "email": "mixed@example.com", "street_address": "12 Mixedway Court",
            "city": "Mixedtown", "state": "IL", "zip_code": "60601", "country": "United States"}}}]},
    {"role": "user", "content": [{"toolResult": {"toolUseId": "tu_m", "content": [
        {"json": {"status": "account_created", "echo": json.dumps({"city": "Mixedtown", "zip_code": "60601"})}}]}}]},
])
# Serialized + truncated JSON that cannot be parsed: must be replaced wholesale.
br.converse(modelId="fake.model", messages=[
    {"role": "user", "content": [{"text": '{"street_address": "55 Brokenjson Blvd", "city": "Tru'}]}])

# Free text with no street suffix: only the app-learned value (registry) can catch it.
san.register_sensitive("Flat 3B Quillfeather House")
br.converse(modelId="fake.model", messages=[
    {"role": "user", "content": [{"text": "Deliver to Flat 3B Quillfeather House please"}]}])

trace.get_tracer_provider().force_flush()
_logs.get_logger_provider().force_flush()
time.sleep(1.5)
trace.get_tracer_provider().force_flush()
_logs.get_logger_provider().force_flush()
time.sleep(0.5)

traces_txt = (OUT / "traces.jsonl").read_text()
logs_txt = (OUT / "logs.jsonl").read_text()
check(len(traces_txt) > 1000 and len(logs_txt) > 1000, "captured trace and log files are non-empty")

FORBIDDEN = [STREET, NESTED_STREET, "55 Brokenjson", "Flat 3B Quillfeather", f"{ST} {ZIP}", ZIP, "43004",
             CITY, "Elsewhere", "Mixedway", "Mixedtown", "60601", SECRET_PW, SECRET_CODE, "pbkdf2$abc"]
for label, text in (("captured traces", traces_txt), ("captured OTLP logs", logs_txt)):
    leaked = [v for v in FORBIDDEN if v in text]
    check(not leaked, f"{label}: no address/secret values (leaked: {leaked})")

# --- the captured files still carry the useful GenAI observability fields ---------
spans = []
for line in traces_txt.splitlines():
    for rs in json.loads(line).get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            spans += ss.get("spans", [])


def attrs(span):
    out = {}
    for a in span.get("attributes", []):
        v = a["value"]
        out[a["key"]] = next(iter(v.values()))
    return out


chat = [s for s in spans if s["name"].startswith("chat ")]
agent = [s for s in spans if s["name"].startswith("invoke_agent")]
tools = [s for s in spans if s["name"].startswith("execute_tool")]
check(chat and agent and tools, "chat, invoke_agent and execute_tool spans exported")
ca = attrs(chat[0])
check(ca.get("gen_ai.request.model") and ca.get("gen_ai.operation.name") == "chat", "chat span keeps model + operation")
check(any(attrs(s).get("gen_ai.usage.input_tokens") for s in chat), "chat span keeps token usage")
check(any(attrs(s).get("gen_ai.input.messages") and attrs(s).get("gen_ai.prompt.0.content") for s in chat),
      "chat spans keep conversation content (prompt/completion)")
check(any("<shipping_address_redacted>" in json.dumps(attrs(s)) for s in chat), "chat content shows redaction placeholder")
check(any("mugs" in json.dumps(attrs(s)) for s in chat), "non-sensitive conversation content preserved")
check(all(attrs(s).get("prompt.version") == "v1" for s in agent), "agent spans keep prompt.version")
check({attrs(s).get("gen_ai.tool.name") for s in tools} >= {"create_account", "start_checkout"}, "tool names preserved")
ids = {s["traceId"] for s in spans}
check(any(s.get("parentSpanId") for s in chat) and all(s.get("parentSpanId") for s in tools) and len(ids) >= 5, "span hierarchy / trace ids preserved")
check(any(EMAIL in json.dumps(attrs(s)) or "app.customer.email" in attrs(s) for s in spans + tools),
      "synthetic customer email still present on spans")

records = []
for line in logs_txt.splitlines():
    for rl in json.loads(line).get("resourceLogs", []):
        for sl in rl.get("scopeLogs", []):
            records += sl.get("logRecords", [])
bedrock_logs = [r for r in records if str(r.get("eventName", "")).startswith("gen_ai.")]
check(bedrock_logs, "Bedrock-generated gen_ai.* OTLP log records exported")
check(any("<shipping_address_redacted>" in json.dumps(r) for r in bedrock_logs), "log bodies show redaction placeholder")
check(any("mugs" in json.dumps(r) for r in bedrock_logs), "non-sensitive log content preserved")
check(all(r.get("traceId") for r in bedrock_logs if r.get("eventName") == "gen_ai.choice"),
      "log records keep trace correlation")

# --- sanitizer fail-closed behaviour ------------------------------------------------
check(san.sanitize_text('{"street_address": "1 X St", "ci') == san.CONTENT_PLACEHOLDER,
      "unparseable address JSON is replaced wholesale")
check(san._fail_closed({"a": 1}) == san.CONTENT_PLACEHOLDER and san._fail_closed(7) == 7,
      "sanitizer error path drops non-numeric content")
check("input_tokens" not in str(san.sanitize_value({"input_tokens": 5, "auth_token": "t"}).get("auth_token")),
      "token-named secrets masked")
check(san.sanitize_value({"input_tokens": 5})["input_tokens"] == 5, "token counters preserved")

print(f"captured files: {OUT}")
if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nALL BEDROCK REDACTION CHECKS PASSED")
server.stop(0)
