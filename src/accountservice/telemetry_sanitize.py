"""Fail-closed sanitization of telemetry copies (spans, events, links, OTLP logs).

Layers (each can be disabled individually ONLY in test mode, to prove negative controls):
  allowlist   exported attribute keys must be known-safe; app.* keys must satisfy telemetry_schema
  structural  JSON key redaction (address/contact/secret keys) and unparseable address JSON dropped
  registry    exact values of the current request's PII (names, email, phone variants, address parts,
              tokens) registered at ingest and masked wherever they reappear, in any formatting
  patterns    last resort: street/ZIP/PO box/phone/email/card/CVV/bearer/DSN/demo-token patterns
  exceptions  exception messages and stacktraces never leave the process (type only)
Content that cannot be sanitized with confidence is replaced wholesale with a placeholder.
"""
import json
import os
import re
import threading
from collections import OrderedDict

import telemetry_schema

ADDRESS_PLACEHOLDER = "<address_redacted>"
CONTACT_PLACEHOLDER = "<contact_redacted>"
SECRET_PLACEHOLDER = "<secret_redacted>"
CONTENT_PLACEHOLDER = "<content_redacted:unsanitizable>"

ADDRESS_KEYS = {"street_address", "city", "state", "zip_code", "postal_code", "country", "address",
                "shipping_address", "street", "address_line1", "address_line2", "line1", "line2", "zip",
                "zipcode", "ship_line1", "ship_line2", "ship_city", "ship_state", "ship_zip"}
CONTACT_KEYS = {"first_name", "last_name", "name", "full_name", "email", "email_display", "email_hash",
                "phone", "phone_e164", "telephone", "tel", "mobile", "customer_email", "customer_name"}
SECRET_KEYS = {"password", "password_hash", "confirm_password", "claim_code", "raw_code", "code_hash",
               "session_id_hash", "session_token", "auth_token", "cookie", "set-cookie", "authorization",
               "credit_card_number", "credit_card_cvv", "cvv", "pan", "card_number", "secret", "api_key",
               "database_url", "accounts_database_url", "dsn", "activation_code", "provider_token",
               "payment_method_token", "token", "x-account-session", "x-service-secret", "csrf"}

_ALLOWED_PREFIXES = ("http.", "url.", "rpc.", "db.", "server.", "client.", "net.", "network.", "peer.",
                     "messaging.", "error.", "otel.", "thread.", "code.", "gen_ai.", "traceloop.", "llm.")
_ALWAYS_DROP = ("http.request.header", "http.response.header", "url.query", "url.full", "http.url",
                "db.query.parameter", "exception.message", "exception.stacktrace", "rpc.request.metadata",
                "rpc.response.metadata", "http.request.body", "http.response.body")
_QUERY_STRIP = ("http.target", "url.path", "http.route")

_STREET_SUFFIX = (r"Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Place|Pl|"
                  r"Parkway|Pkwy|Terrace|Ter|Circle|Cir|Highway|Hwy|Square|Sq|Trail|Trl|Alley|Aly|Mall")
_STREET_RE = re.compile(r"\b\d{1,6}[A-Za-z]?\s+(?:[A-Za-z0-9.'\-]+\s+){0,4}?(?:" + _STREET_SUFFIX + r")\b\.?"
                        r"(?:\s*,?\s*(?:apt|apartment|suite|ste|unit|#)\s*[\w\-]+)?", re.I)
_POBOX_RE = re.compile(r"\b(?:P\.?\s?O\.?\s*Box|Post\s+Office\s+Box)\s*\d+\b", re.I)
_STATE_ZIP_RE = re.compile(r"\b[A-Z]{2}\.?,?\s+\d{5}(?:-\d{4})?\b")
_CITY_STATE_ZIP_RE = re.compile(r"[A-Za-z][A-Za-z .'\-]{1,30},\s*[A-Z]{2}\.?,?\s+\d{5}(?:-\d{4})?\b")
_ZIP_LABEL_RE = re.compile(r"\b(zip(?:\s?code)?|postal(?:\s?code)?|postcode)\b(\W{0,12})\d{4,5}(?:-\d{4})?", re.I)
_PHONE_RE = re.compile(r"(?<![\w.])(?:\+?1[\s.\-]?)?\(?[2-9]\d{2}\)?[\s.\-]?[2-9]\d{2}[\s.\-]?\d{4}(?![\w])")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_CVV_RE = re.compile(r'("(?:credit_card_)?cvv"\s*:\s*)"?\d{3,4}"?', re.I)
_DEMO_TOKEN_RE = re.compile(r"\btok_demo_[A-Za-z0-9_\-]+")
_CONN_RE = re.compile(r"\b(?:postgres(?:ql)?|redis)://[^\s\"']+", re.I)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.I)
_ADDRESS_KEY_IN_TEXT = re.compile(
    r'["\']?(' + "|".join(sorted(ADDRESS_KEYS - {"address", "state", "country", "city", "street", "zip"}))
    + r')["\']?\s*[:=]', re.I)
_KV_KEYS = (r"street_address|shipping_address|address_line1|address_line2|address|street|city|state|zip_code|"
            r"zipcode|zip|postal_code|postcode|country|first_name|last_name|full_name|email|phone|phone_e164")
_KV_RE = re.compile(r"(?P<q>\\*[\"'])(?P<k>" + _KV_KEYS + r")(?P=q)(?P<sep>\s*:\s*)"
                    r"(?:(?P<vq>\\*[\"'])(?P<v>[^\"'\\]*)(?P=vq)|(?P<num>\d[\d\-]*))", re.I)
_KV_EQ_RE = re.compile(r"\b(street_address|city|zip_code|postal_code|zip|email|phone|first_name|last_name)=[^\s,;&]+", re.I)

_lock = threading.Lock()
_known = OrderedDict()          # lower-cased exact sensitive values seen by the app
_MAX_KNOWN = 4096


def _layer_off(name):
    """Test-only: TELEMETRY_TEST_MODE=1 plus TELEMETRY_SANITIZER_DISABLED=1 (all layers) or
    TELEMETRY_DISABLE_LAYERS=allowlist,registry,... Never honoured outside test mode."""
    if os.environ.get("TELEMETRY_TEST_MODE") != "1":
        return False
    if os.environ.get("TELEMETRY_SANITIZER_DISABLED") == "1":
        return True
    return name in {x.strip() for x in os.environ.get("TELEMETRY_DISABLE_LAYERS", "").split(",") if x.strip()}


# --- exact-value registry ------------------------------------------------------------------------
def register_sensitive(*values, min_len=4):
    """Remember exact values so any telemetry text that echoes them is scrubbed at export."""
    with _lock:
        for v in values:
            v = " ".join(str(v or "").split())
            if len(v) < min_len:
                continue
            _known.pop(v.lower(), None)
            _known[v.lower()] = None
            while len(_known) > _MAX_KNOWN:
                _known.popitem(last=False)


def register_address(address):
    if not isinstance(address, dict):
        return
    register_sensitive(address.get("line1"), address.get("line2"), address.get("street_address"),
                       address.get("address_line1"), address.get("address_line2"), address.get("city"))
    zip_code = str(address.get("zip") or address.get("zip_code") or address.get("postal_code") or "").strip()
    if len(zip_code) >= 5:
        register_sensitive(zip_code)


def register_person(first=None, last=None, email=None, phone_e164=None):
    """Names (min 3 chars), full name, email (+ local part), phone in every common format."""
    register_sensitive(first, last, min_len=3)
    if first and last:
        register_sensitive(f"{first} {last}", min_len=3)
    if email:
        register_sensitive(email)
        local = str(email).split("@", 1)[0]
        register_sensitive(local, min_len=5)
    if phone_e164 and len(phone_e164) == 12:
        d = phone_e164[2:]
        register_sensitive(phone_e164, d, f"1{d}", f"{d[:3]}-{d[3:6]}-{d[6:]}", f"({d[:3]}) {d[3:6]}-{d[6:]}",
                           f"{d[:3]}.{d[3:6]}.{d[6:]}", f"{d[:3]} {d[3:6]} {d[6:]}", f"+1 {d[:3]} {d[3:6]} {d[6:]}",
                           f"1-{d[:3]}-{d[3:6]}-{d[6:]}")


def _known_values():
    with _lock:
        return sorted(_known, key=len, reverse=True)


# --- key classification --------------------------------------------------------------------------
def _last(key):
    return str(key).lower().rsplit(".", 1)[-1]


def _is_secret_key(key):
    k = str(key).lower()
    last = _last(k)
    if k in SECRET_KEYS or last in SECRET_KEYS:
        return True
    if "password" in k or "passwd" in k:
        return True
    return "token" in last and not (last.endswith("_tokens") or last == "tokens")


def _is_address_key(key):
    k = str(key).lower()
    return k in ADDRESS_KEYS or _last(k) in {"street_address", "zip_code", "postal_code", "shipping_address",
                                            "address_line1", "address_line2", "ship_line1", "ship_zip"}


def _is_contact_key(key):
    k = str(key).lower()
    return k in CONTACT_KEYS or _last(k) in CONTACT_KEYS and not k.startswith(("app.", "gen_ai.", "rpc.", "db.", "http."))


# --- text / value sanitization -------------------------------------------------------------------
def _kv_replace(m):
    ph = CONTACT_PLACEHOLDER if m.group("k").lower() in ("first_name", "last_name", "full_name", "email", "phone", "phone_e164") else ADDRESS_PLACEHOLDER
    q = m.group("q")
    if m.group("num") is not None:
        return f"{q}{m.group('k')}{q}{m.group('sep')}{q}{ph}{q}"
    return f"{q}{m.group('k')}{q}{m.group('sep')}{m.group('vq')}{ph}{m.group('vq')}"


def _scrub_text(s):
    if not _layer_off("structural"):
        s = _KV_RE.sub(_kv_replace, s)
        s = _KV_EQ_RE.sub(lambda m: m.group(1) + "=" + ADDRESS_PLACEHOLDER, s)
    if not _layer_off("registry"):
        low = s.lower()
        for v in _known_values():
            if v in low:
                s = re.sub(r"(?<![\w])" + re.escape(v) + r"(?![\w])", ADDRESS_PLACEHOLDER, s, flags=re.I)
                low = s.lower()
    if not _layer_off("patterns"):
        s = _CITY_STATE_ZIP_RE.sub(ADDRESS_PLACEHOLDER, s)
        s = _STREET_RE.sub(ADDRESS_PLACEHOLDER, s)
        s = _POBOX_RE.sub(ADDRESS_PLACEHOLDER, s)
        s = _STATE_ZIP_RE.sub(ADDRESS_PLACEHOLDER, s)
        s = _ZIP_LABEL_RE.sub(lambda m: m.group(1) + m.group(2) + ADDRESS_PLACEHOLDER, s)
        s = _EMAIL_RE.sub(CONTACT_PLACEHOLDER, s)
        s = _PHONE_RE.sub(CONTACT_PLACEHOLDER, s)
        s = _DEMO_TOKEN_RE.sub(SECRET_PLACEHOLDER, s)
        s = _CONN_RE.sub(SECRET_PLACEHOLDER, s)
        s = _BEARER_RE.sub("Bearer " + SECRET_PLACEHOLDER, s)
        s = _CARD_RE.sub("<card>", s)
        s = _CVV_RE.sub(r'\1"<cvv>"', s)
    return s


def sanitize_text(s):
    """Sanitize a string that may be plain text or serialized JSON."""
    if _layer_off("structural") and _layer_off("registry") and _layer_off("patterns"):
        return s
    stripped = s.strip()
    if stripped[:1] in ("{", "[", '"'):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            if _ADDRESS_KEY_IN_TEXT.search(s) and not _layer_off("structural"):
                return CONTENT_PLACEHOLDER
            return _scrub_text(s)
        return json.dumps(sanitize_value(parsed), default=str)
    return _scrub_text(s)


def sanitize_value(value):
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _layer_off("structural"):
                out[key] = sanitize_value(item)
            elif _is_address_key(key):
                out[key] = ADDRESS_PLACEHOLDER
            elif _is_contact_key(key):
                out[key] = CONTACT_PLACEHOLDER
            elif _is_secret_key(key):
                out[key] = SECRET_PLACEHOLDER
            else:
                out[key] = sanitize_value(item)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return sanitize_text(bytes(value).decode("utf-8", "replace"))
    return value


def _fail_closed(value):
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(v, (bool, int, float)) for v in value):
        return value
    return CONTENT_PLACEHOLDER


_DROP = object()


def sanitize_attribute(key, value):
    """Sanitize one span/event/link/log attribute (name-aware). Returns _DROP to omit it."""
    key = str(key)
    if not _layer_off("allowlist"):
        if key.startswith("app."):
            try:
                return telemetry_schema.coerce(key, value)
            except ValueError:
                return _DROP
        if key.startswith(_ALWAYS_DROP):
            return _DROP
        if not key.startswith(_ALLOWED_PREFIXES) and key not in ("exception.type", "exception.escaped") \
                and not isinstance(value, (bool, int, float)):
            return _DROP
    if key == "exception.stacktrace" or key == "exception.message":
        return _DROP if not _layer_off("exceptions") else value
    if key in _QUERY_STRIP and isinstance(value, str):
        value = value.split("?", 1)[0]
    standard = key.startswith(_ALLOWED_PREFIXES) or key.startswith("app.") or key in ("exception.type", "exception.escaped")
    if not _layer_off("structural") and not standard:
        if _is_address_key(key):
            return ADDRESS_PLACEHOLDER
        if _is_contact_key(key):
            return CONTACT_PLACEHOLDER
        if _is_secret_key(key):
            return SECRET_PLACEHOLDER
    try:
        return sanitize_value(value)
    except Exception:
        return _fail_closed(value)


def sanitize_attributes(attrs):
    if not attrs:
        return {}
    out = {}
    for k, v in dict(attrs).items():
        r = sanitize_attribute(k, v)
        if r is not _DROP:
            out[k] = r
    return out


# --- span exporter wrapper -----------------------------------------------------------------------
_DB_PORTS = {"postgresql": 5432, "redis": 6379}


def _add_db_peer(attrs):
    """DB client spans get the peer attributes APM uses to draw a DB node and edge."""
    system, host = attrs.get("db.system"), attrs.get("net.peer.name") or attrs.get("server.address")
    if not system or not host:
        return attrs
    attrs = dict(attrs)
    attrs.setdefault("server.address", str(host))
    if system in _DB_PORTS:
        attrs.setdefault("server.port", _DB_PORTS[system])
    attrs.setdefault("peer.service", str(host).split(".")[0])
    return attrs


def _sanitize_span(span):
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.trace import Link, Status

    attrs = _add_db_peer(sanitize_attributes(span.attributes))
    events = []
    for e in (span.events or ()):
        ea = sanitize_attributes(e.attributes)
        if e.name == "exception" and not _layer_off("exceptions"):
            ea = {k: v for k, v in ea.items() if k in ("exception.type", "exception.escaped")}
        events.append(Event(e.name, ea, e.timestamp))
    links = [Link(l.context, sanitize_attributes(l.attributes)) for l in (span.links or ())]
    status = span.status
    if status is not None and status.description:
        status = Status(status.status_code, sanitize_text(status.description)[:64])
    return ReadableSpan(
        name=sanitize_text(span.name) if not span.name.replace(".", "").replace("_", "").replace(" ", "").isalnum() else span.name,
        context=span.get_span_context(), parent=span.parent, resource=span.resource, attributes=attrs,
        events=events, links=links, kind=span.kind, status=status, start_time=span.start_time,
        end_time=span.end_time, instrumentation_scope=span.instrumentation_scope)


def _minimal_span(span):
    """Last resort: structure, timing and numeric attributes only."""
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import Status, StatusCode

    attrs = {k: v for k, v in dict(span.attributes or {}).items()
             if isinstance(v, (bool, int, float)) and not _is_secret_key(k)}
    code = span.status.status_code if span.status else StatusCode.UNSET
    return ReadableSpan(
        name=re.sub(r"[^A-Za-z0-9_. /:-]", "", span.name)[:64], context=span.get_span_context(),
        parent=span.parent, resource=span.resource, attributes=attrs, events=[], links=[],
        kind=span.kind, status=Status(code), start_time=span.start_time, end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope)


def make_span_exporter(inner):
    from opentelemetry.sdk.trace.export import SpanExporter

    class SanitizingSpanExporter(SpanExporter):
        def __init__(self):
            self.inner = inner

        def export(self, spans):
            out = []
            for span in spans:
                try:
                    out.append(_sanitize_span(span))
                except Exception:
                    try:
                        out.append(_minimal_span(span))
                    except Exception:
                        continue   # never export what we could not sanitize
            return self.inner.export(out)

        def shutdown(self):
            return self.inner.shutdown()

        def force_flush(self, timeout_millis=30000):
            return self.inner.force_flush(timeout_millis)

    return SanitizingSpanExporter()


def make_log_processor():
    from opentelemetry.sdk._logs import LogRecordProcessor

    class SanitizingLogRecordProcessor(LogRecordProcessor):
        def on_emit(self, log_record):
            rec = log_record.log_record
            try:
                rec.body = sanitize_value(rec.body)
            except Exception:
                rec.body = CONTENT_PLACEHOLDER
            attrs = rec.attributes
            if attrs:
                for key in list(attrs.keys()):
                    try:
                        r = sanitize_attribute(key, attrs[key])
                    except Exception:
                        r = CONTENT_PLACEHOLDER
                    if r is _DROP:
                        del attrs[key]
                    else:
                        attrs[key] = r

        def shutdown(self):
            return None

        def force_flush(self, timeout_millis=30000):
            return True

    return SanitizingLogRecordProcessor()


def sanitize_metric_attributes(attrs):
    """Metric attribute sets: low-cardinality enums only, never free text."""
    return {k: v for k, v in dict(attrs or {}).items()
            if isinstance(v, (bool, int, float)) or (isinstance(v, str) and re.match(r"^[A-Za-z0-9_.:-]{1,48}$", v)
                                                    and not _EMAIL_RE.search(v) and not _PHONE_RE.search(v))}
