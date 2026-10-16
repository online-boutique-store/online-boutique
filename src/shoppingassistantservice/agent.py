"""The shopping agent: a plain tool-calling loop against Snowflake Cortex.

Span tree per customer prompt (children of the Flask server span):

  invoke_agent shopping_assistant        (this module)
    chat <model>                         (opentelemetry-instrumentation-openai-v2)
    execute_tool <name>                  (this module)
      <grpc client span> ...             (GrpcInstrumentorClient) -> existing services
    chat <model>
"""
import collections
import json
import threading
import time

import openai
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

import backends
import config
import incident
import telemetry as tel

tracer = tel.tracer

AGENT_NAME = "shopping_assistant"
PROVIDER = "snowflake.cortex"
_MAX_ATTR = 1000


def _clip(value, n=_MAX_ATTR):
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    return s if len(s) <= n else s[:n] + "...[truncated]"


# --- sessions ------------------------------------------------------------------
class Session:
    def __init__(self, sid):
        self.id = sid
        self.history = collections.deque(maxlen=max(2, config.HISTORY_TURNS * 2))
        self.turns = 0
        self.llm_requests = 0
        self.unrecovered_value = 0.0  # USD of the latest failed checkout, cleared on success
        self.failed_checkouts = 0
        self.lock = threading.Lock()


_sessions = collections.OrderedDict()
_sessions_lock = threading.Lock()


def get_session(sid):
    with _sessions_lock:
        s = _sessions.get(sid)
        if s is None:
            s = _sessions[sid] = Session(sid)
            while len(_sessions) > config.MAX_SESSIONS:
                _sessions.popitem(last=False)
        else:
            _sessions.move_to_end(sid)
        return s


# --- LLM client + cost controls --------------------------------------------------
class RateLimited(Exception):
    pass


class NotConfigured(Exception):
    pass


_calls, _calls_lock = collections.deque(), threading.Lock()
_client = {"c": None}


def _admit():
    if config.RATE_LIMIT_RPM <= 0:
        return
    now = time.monotonic()
    with _calls_lock:
        while _calls and now - _calls[0] > 60:
            _calls.popleft()
        if len(_calls) >= config.RATE_LIMIT_RPM:
            raise RateLimited()
        _calls.append(now)


def _llm():
    if not config.cortex_configured():
        raise NotConfigured()
    if _client["c"] is None:
        # max_retries=0: retries are done in _chat so each one is visible in telemetry.
        _client["c"] = openai.OpenAI(
            api_key=config.CORTEX_PAT, base_url=config.CORTEX_BASE_URL,
            timeout=config.CORTEX_TIMEOUT_S, max_retries=0)
    return _client["c"]


_RETRYABLE = (openai.APIConnectionError, openai.APITimeoutError,
              openai.RateLimitError, openai.InternalServerError)


def _chat(messages, tools, agent_span, labels):
    attempt = 0
    while True:
        _admit()
        try:
            resp = _llm().chat.completions.create(
                model=config.CORTEX_MODEL,
                messages=messages,
                tools=tools,
                temperature=0,
                max_completion_tokens=config.MAX_COMPLETION_TOKENS,
            )
            tel.llm_calls_total.add(1, {**labels, "outcome": "ok"})
            return resp
        except _RETRYABLE as e:
            tel.llm_calls_total.add(1, {**labels, "outcome": "error"})
            if attempt >= config.LLM_RETRIES:
                raise
            attempt += 1
            tel.llm_retries_total.add(1, labels)
            agent_span.add_event("llm.retry", {"attempt": attempt, "error.type": type(e).__name__})
            time.sleep(min(2 ** attempt, 8) * 0.5)
        except openai.APIError:
            tel.llm_calls_total.add(1, {**labels, "outcome": "error"})
            raise


# --- tool execution -----------------------------------------------------------------
def _currency_attrs(args):
    code = args.get("currency_code")
    if code is None:
        return {}
    supported = backends.supported_currencies()
    return {"app.tool.currency_code": str(code),
            "app.tool.currency_valid": (str(code) in supported) if supported else False}


def _execute_tool(session, call, labels):
    name = call.function.name
    try:
        model_args = json.loads(call.function.arguments or "{}")
        if not isinstance(model_args, dict):
            raise ValueError("arguments must be a JSON object")
        parse_error = None
    except ValueError as e:
        model_args, parse_error = {}, f"invalid tool arguments JSON: {e}"

    with tracer.start_as_current_span(f"execute_tool {name}") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", name)
        span.set_attribute("gen_ai.tool.type", "function")
        span.set_attribute("gen_ai.tool.call.id", call.id or "")
        span.set_attribute("prompt.version", labels["prompt.version"])
        tel.stamp_app_attrs(span)

        exec_args, applied = incident.apply_faults(name, model_args)
        injected = bool(applied)
        span.set_attribute("gen_ai.tool.call.arguments", _clip(exec_args))
        span.set_attribute("fault.injected", injected)
        span.set_attribute("app.tool.args_source", "synthetic_fault_injection" if injected else "model")
        if injected:
            span.set_attribute("fault.names", [a["fault"] for a in applied])
            span.set_attribute("app.tool.model_arguments", _clip(model_args))
        for k, v in _currency_attrs(exec_args).items():
            span.set_attribute(k, v)

        is_checkout = name == "place_order"
        value_usd = None
        if is_checkout and not parse_error:
            try:
                _, value_usd = backends.cart_contents(session.id)
                span.set_attribute("app.checkout.value_usd", value_usd)
            except Exception:  # value is telemetry only; never block the checkout
                pass

        fn = backends.TOOLS.get(name)
        if parse_error:
            result = {"error": parse_error}
        elif fn is None:
            result = {"error": f"unknown tool: {name}"}
        else:
            try:
                result = fn(session.id, **exec_args)
            except TypeError as e:
                result = {"error": f"invalid arguments for {name}: {e}"}
            except Exception as e:  # noqa: BLE001 - tool failures must reach the model, not crash the loop
                span.record_exception(e)
                result = {"error": f"{type(e).__name__}: {e}"}

        failed = "error" in result
        span.set_attribute("gen_ai.tool.call.result", _clip(result))
        tool_labels = {"tool.name": name, "prompt.version": labels["prompt.version"]}
        tel.tool_calls_total.add(1, tool_labels)
        if failed:
            span.set_status(Status(StatusCode.ERROR, _clip(result["error"], 200)))
            span.set_attribute("error.type", result.get("grpc_status", "tool_error"))
            tel.tool_errors_total.add(1, {**tool_labels, "error.type": result.get("grpc_status", "tool_error")})
            tel.warn("tool.error", **{"tool.name": name, "error": _clip(result["error"], 300),
                                      "fault.injected": injected, "prompt.version": labels["prompt.version"]})
        if is_checkout:
            _record_checkout(session, span, result, value_usd, injected, labels)
        return result


def _record_checkout(session, span, result, value_usd, injected, labels):
    failed = "error" in result
    outcome = "failed" if failed else "success"
    dims = {"prompt.version": labels["prompt.version"], "outcome": outcome}
    tel.checkout_attempts.add(1, {**dims, "fault.injected": injected})
    if value_usd:
        tel.checkout_value.add(value_usd, dims)
    is_retry = session.failed_checkouts > 0
    if is_retry:
        tel.checkout_retries.add(1, {"prompt.version": labels["prompt.version"]})
    recovered = 0.0
    if failed:
        session.failed_checkouts += 1
        session.unrecovered_value = value_usd or session.unrecovered_value
    else:
        if is_retry and value_usd:
            recovered = value_usd
            tel.checkout_recovered_value.add(value_usd, {"prompt.version": labels["prompt.version"]})
        session.failed_checkouts = 0
        session.unrecovered_value = 0.0
    span.set_attribute("app.checkout.outcome", outcome)
    span.set_attribute("app.checkout.is_retry", is_retry)
    tel.event("checkout.attempt", **{
        "checkout.outcome": outcome, "checkout.value_usd": value_usd,
        "checkout.is_retry": is_retry, "checkout.recovered_value_usd": recovered,
        "checkout.order_id": result.get("order_id"),
        "checkout.error": _clip(result["error"], 300) if failed else None,
        "fault.injected": injected, "prompt.version": labels["prompt.version"],
        "app.session.id": session.id, "app.customer.id": session.id})


# --- the agent -------------------------------------------------------------------------
def _assistant_message(msg):
    out = {"role": "assistant", "content": msg.content}
    if msg.tool_calls:
        out["tool_calls"] = [{"id": c.id, "type": "function",
                              "function": {"name": c.function.name, "arguments": c.function.arguments}}
                             for c in msg.tool_calls]
    return out


def run(session, prompt, category, conversation_id):
    version = incident.prompt_version()
    labels = {"prompt.version": version, "app.prompt.category": category}

    with tracer.start_as_current_span(f"invoke_agent {AGENT_NAME}") as span:
        span.set_attribute("gen_ai.operation.name", "invoke_agent")
        span.set_attribute("gen_ai.agent.name", AGENT_NAME)
        span.set_attribute("gen_ai.provider.name", PROVIDER)
        span.set_attribute("gen_ai.request.model", config.CORTEX_MODEL)
        span.set_attribute("gen_ai.conversation.id", conversation_id)
        span.set_attribute("prompt.version", version)
        span.set_attribute("app.prompt.version", version)
        span.set_attribute("app.prompt.category", category)
        span.set_attribute("app.session.id", session.id)
        span.set_attribute("app.customer.id", session.id)
        span.set_attribute("app.prompt.turn", session.turns + 1)
        span.set_attribute("app.prompt.text", _clip(prompt))
        span.set_attribute("fault.injection.enabled", bool(config.FAULT_INJECTION))
        tel.stamp_app_attrs(span)

        messages = [{"role": "system", "content": incident.system_prompt()}]
        messages += list(session.history) + [{"role": "user", "content": prompt}]
        tools = incident.tool_definitions()

        used, tool_errors, steps = [], 0, 0
        in_tok = out_tok = 0
        outcome, reply = "ok", None
        try:
            for steps in range(1, config.MAX_TOOL_STEPS + 1):
                session.llm_requests += 1
                resp = _chat(messages, tools, span, labels)
                if resp.usage:
                    in_tok += resp.usage.prompt_tokens or 0
                    out_tok += resp.usage.completion_tokens or 0
                msg = resp.choices[0].message
                if not msg.tool_calls:
                    reply = msg.content or ""
                    break
                messages.append(_assistant_message(msg))
                for call in msg.tool_calls:
                    result = _execute_tool(session, call, labels)
                    used.append(call.function.name)
                    tool_errors += "error" in result
                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "content": json.dumps(result, default=str)})
            else:
                outcome = "max_steps"
                reply = "I couldn't finish that within my step limit. Could you try a simpler request?"
        except NotConfigured:
            outcome, reply = "not_configured", "The shopping assistant is not configured yet (missing Cortex credentials)."
        except RateLimited:
            outcome, reply = "rate_limited", "I'm getting a lot of requests right now. Please try again in a moment."
        except openai.APIError as e:
            outcome = "llm_error"
            span.record_exception(e)
            reply = "Sorry, I couldn't reach my language model just now. Please try again."

        if outcome == "ok" and tool_errors:
            outcome = "tool_error"
        span.set_attribute("gen_ai.usage.input_tokens", in_tok)
        span.set_attribute("gen_ai.usage.output_tokens", out_tok)
        span.set_attribute("agent.steps", steps)
        span.set_attribute("agent.tool_calls", len(used))
        span.set_attribute("agent.tool_errors", tool_errors)
        span.set_attribute("agent.outcome", outcome)
        span.set_attribute("app.session.llm_requests", session.llm_requests)
        if outcome in ("llm_error", "not_configured", "max_steps"):
            span.set_status(Status(StatusCode.ERROR, outcome))

        if outcome in ("ok", "tool_error", "max_steps"):
            session.history.append({"role": "user", "content": prompt})
            session.history.append({"role": "assistant", "content": reply})
        session.turns += 1

        tel.requests_total.add(1, {**labels, "outcome": outcome})
        for direction, n in (("input", in_tok), ("output", out_tok)):
            if n:
                tel.tokens_total.add(n, {"prompt.version": version, "direction": direction,
                                         "gen_ai.request.model": config.CORTEX_MODEL})
        tel.event("assistant.request.completed", **{
            "app.session.id": session.id, "app.customer.id": session.id,
            "app.prompt.category": category, "prompt.version": version,
            "app.prompt.turn": session.turns, "prompt_chars": len(prompt),
            "agent.outcome": outcome, "agent.steps": steps, "agent.tools": used,
            "agent.tool_errors": tool_errors, "gen_ai.usage.input_tokens": in_tok,
            "gen_ai.usage.output_tokens": out_tok,
            "app.session.llm_requests": session.llm_requests,
            "fault.injection.enabled": bool(config.FAULT_INJECTION)})

        return {"content": reply, "trace_id": tel.trace_id_hex(),
                "details": {"outcome": outcome, "tools": used, "prompt_version": version,
                            "input_tokens": in_tok, "output_tokens": out_tok}}
