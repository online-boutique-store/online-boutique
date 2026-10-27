"""OpenTelemetry wiring for the checkout state machine (spans, events, logs, metrics).

Attribute names are shared across traces, logs and metrics:
  app.session.ref (HMAC), app.customer.id (Postgres UUID, signed-in only), app.customer.authenticated,
  app.customer.type (new|returning|guest|unknown), app.checkout.id, app.checkout.stage,
  app.prompt.version, app.conversation.id.
"""
from opentelemetry import trace

import checkout
import incident
import telemetry as tel

tracer = tel.tracer


def corr(st):
    """Correlation attributes stamped on spans, log records and span events."""
    out = {
        "app.checkout.id": st.checkout_id,
        "app.checkout.stage": "done" if st.done else st.current_stage(),
        "app.customer.type": st.customer_type,
        "app.prompt.version": incident.prompt_version(),
        **tel.identity_attrs(getattr(st, "session_id", ""), getattr(st, "customer_id", None)),
    }
    return out


def _clean(d):
    return {k: v for k, v in d.items() if v is not None and v != ""}


class OtelCheckoutHooks(checkout.Hooks):
    def span(self, name, **attrs):
        return tracer.start_as_current_span(name, attributes=attrs)

    def event(self, name, st, **attrs):
        c = corr(st)
        sp = trace.get_current_span()
        if sp.is_recording():
            sp.add_event(name, _clean({**attrs, **c}))
            for k in ("app.checkout.id", "app.checkout.stage", "app.customer.type"):
                sp.set_attribute(k, c[k])
        outcome = attrs.get("outcome", "unknown")
        if name == "account.lookup":
            tel.account_lookups.add(1, {"outcome": outcome, "prompt.version": c["app.prompt.version"]})
        elif name == "account.create":
            tel.account_creations.add(1, {"outcome": outcome, "prompt.version": c["app.prompt.version"]})
        tel.event(name, **_clean({**c, **{k.replace("_", "."): v for k, v in attrs.items()}}))

    def stage(self, st, stage, prefilled):
        c = corr(st)
        tel.checkout_funnel_stage.add(1, {
            "stage": stage, "prompt.version": c["app.prompt.version"],
            "customer_type": st.customer_type, "prefilled": prefilled})
        fields = {**c, "checkout.stage": stage, "checkout.prefilled": prefilled}
        sp = trace.get_current_span()
        if sp.is_recording():
            sp.add_event("checkout.stage", _clean(fields))
        tel.event("checkout.stage", **_clean(fields))

    def terminal(self, st, outcome, error_type=None):
        c = corr(st)
        dims = {"outcome": outcome, "prompt.version": c["app.prompt.version"],
                "customer_type": st.customer_type}
        if error_type:
            dims["failure.reason" if outcome == "abandoned" else "error.type"] = error_type
        tel.checkout_terminal.add(1, dims)
        fields = _clean({**c, "checkout.outcome": outcome, "checkout.attempt": st.attempt,
                         "checkout.currency": str(st.currency) if st.currency else None,
                         "checkout.error_type": error_type})
        sp = trace.get_current_span()
        if outcome == "abandoned" or not sp.is_recording():
            # Sweeper thread has no request span: emit a small span carrying the outcome.
            with tracer.start_as_current_span(
                    "checkout.abandoned" if outcome == "abandoned" else "checkout.terminal",
                    attributes=fields):
                tel.event("checkout.terminal", **fields)
            return
        sp.add_event("checkout.terminal", fields)
        tel.event("checkout.terminal", **fields)


def install():
    checkout.hooks = OtelCheckoutHooks()
