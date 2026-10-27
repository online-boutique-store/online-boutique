"""Sanitizer layers + negative controls, through the REAL export pipeline (TracerProvider ->
BatchSpanProcessor -> SanitizingSpanExporter -> OTLP -> local receiver).

The service code never puts PII in telemetry by construction, so these probes inject leaky content
on purpose through every channel (attributes, events, links, span names, exception messages) and
prove: (1) with the sanitizer on, none of it is exported; (2) with a layer disabled (test mode
only), that layer's canary IS exported - i.e. the canary tests can fail - and (3) disabling every
layer leaks everything."""
import os
import sys
import time

import service_env as env

S = env.start()
import telemetry_sanitize as san  # noqa: E402
from opentelemetry import trace  # noqa: E402

recv = S["receiver"]
tracer = trace.get_tracer("probe")
FAILS = []

CAN = {
    "allowlist": ["canary-allowlist-note-91"],
    "structural": ["canary-json-name-77", "canary-json-phone-78"],
    "registry": ["Quillfeather", "Zephyrine Q"],
    "always": ["query-canary-1"],
    "patterns": ["4412 Hollowbrook Avenue", "probe.person@example.org", "(212) 555-0187", "tok_demo_canaryTOKEN123",
                 "4111 1111 1111 1111", "probe.name@example.net", "9001 Eventwood Road"],
    "exceptions": ["canary-exception-message-55"],
}
ALL = [c for v in CAN.values() for c in v]


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond:
        FAILS.append(name)


def probe():
    """Emit one span carrying every canary through a different channel."""
    from opentelemetry.trace import Link
    san.register_person("Zephyrine", "Quillfeather", None, None)
    san.register_sensitive("Zephyrine Q", min_len=3)
    link_ctx = trace.get_current_span().get_span_context()
    with tracer.start_as_current_span("probe lookup probe.name@example.net", links=[Link(link_ctx, {"db.statement": "link to 9001 Eventwood Road"})]) as sp:
        sp.set_attribute("custom.note", CAN["allowlist"][0])
        sp.set_attribute("db.statement", '{"first_name":"canary-json-name-77","phone":"canary-json-phone-78"}')
        sp.set_attribute("db.operation", "note for Quillfeather and Zephyrine Q")
        sp.set_attribute("http.target", "/lookup?email=query-canary-1")
        sp.set_attribute("rpc.method", "ship to 4412 Hollowbrook Avenue, call (212) 555-0187, mail probe.person@example.org")
        sp.set_attribute("error.type", "card 4111 1111 1111 1111 tok_demo_canaryTOKEN123")
        sp.add_event("probe-event", {"db.statement": "event: 9001 Eventwood Road"})
        try:
            raise ValueError(CAN["exceptions"][0])
        except ValueError as e:
            sp.record_exception(e)


def export_probe(env_vars):
    old = {k: os.environ.get(k) for k in ("TELEMETRY_SANITIZER_DISABLED", "TELEMETRY_DISABLE_LAYERS")}
    for k in old:
        os.environ.pop(k, None)
    os.environ.update(env_vars)
    try:
        recv.clear()
        probe()
        recv.wait_for(lambda r: any(s.name.startswith("probe") for s in r.spans), 6)
        time.sleep(0.5)
        return recv.canary_hits(ALL)
    finally:
        for k, v in old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def main():
    hits = export_probe({})
    check("sanitizer ON: no canary is exported through any channel", not hits, hits)
    check("sanitizer ON: the probe span itself was exported", any(s.name.startswith("probe") for s in recv.spans))
    sp = next(s for s in recv.spans if s.name.startswith("probe"))
    check("sanitizer ON: unknown attribute dropped; http.target query stripped; exception message dropped",
          "custom.note" not in sp.attrs and sp.attrs.get("http.target") == "/lookup" and
          all("exception.message" not in a for _, a in sp.events), sp.attrs)

    hits = export_probe({"TELEMETRY_SANITIZER_DISABLED": "1"})
    leaked = set(hits)
    check("negative control: sanitizer OFF leaks every layer canary", set(ALL) - set(CAN["always"]) <= leaked, set(ALL) - set(CAN["always"]) - leaked)
    check("query strings are stripped even with every layer off (unconditional)", not (set(CAN["always"]) & leaked))

    solo = {"allowlist": ["allowlist"], "structural": ["structural"], "registry": ["registry"],
            "patterns": ["patterns"], "exceptions": ["exceptions", "allowlist"]}
    for layer, off in solo.items():
        hits = set(export_probe({"TELEMETRY_DISABLE_LAYERS": ",".join(off)}))
        want = set(CAN[layer])
        check(f"negative control: disabling '{'+'.join(off)}' leaks the {layer} canaries", want <= hits, want - hits)
        others = hits - want
        # Redundant layers may also catch other canaries, but a disabled layer must never leak
        # canaries that belong to a layer that is still on.
        foreign = {c for c in others if c not in want and any(c in CAN[l] for l in CAN if l not in off)}
        check(f"disabling '{'+'.join(off)}' does not leak other layers' canaries", not foreign, foreign)

    hits = export_probe({})
    check("sanitizer back ON after toggles: clean again", not hits, hits)

    # Not honoured outside test mode
    os.environ["TELEMETRY_TEST_MODE"] = "0"
    os.environ["TELEMETRY_SANITIZER_DISABLED"] = "1"
    try:
        recv.clear()
        probe()
        recv.wait_for(lambda r: any(s.name.startswith("probe") for s in r.spans), 6)
        time.sleep(0.5)
        check("disable switches are ignored when TELEMETRY_TEST_MODE != 1", not recv.canary_hits(ALL), recv.canary_hits(ALL))
    finally:
        os.environ["TELEMETRY_TEST_MODE"] = "1"
        os.environ.pop("TELEMETRY_SANITIZER_DISABLED", None)

    # Sanitizer unit-level: fail closed on unparseable address JSON, metric attributes low-cardinality
    check("unparseable JSON that names address fields is dropped wholesale",
          san.sanitize_text('{"street_address": "1 Main St", "city": ') == san.CONTENT_PLACEHOLDER)
    check("metric attributes keep enums, drop free text / emails",
          san.sanitize_metric_attributes({"action": "login", "outcome": "ok", "who": "a@b.com", "note": "has spaces"}) == {"action": "login", "outcome": "ok"})
    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
