"""Fail-closed sanitization of telemetry copies (spans, events, OTLP logs).

Applied inside the OTel SDK pipeline - a LogRecordProcessor ahead of the batch
log processor and a wrapper around the span exporter - so it covers content
created by automatic instrumentation (botocore Bedrock chat spans and their
gen_ai.* log events) as well as our own hooks. The application's own objects
(Bedrock request/response, checkout args) are never modified.

Policy: shipping addresses, passwords/hashes, activation codes, session
tokens, cookies, credentials and card numbers never leave the process.
Synthetic name/email/customer id are kept. When content cannot be sanitized
with confidence it is replaced wholesale with a placeholder.
"""
import json
import re
import threading
from collections import OrderedDict

ADDRESS_PLACEHOLDER = "<shipping_address_redacted>"
SECRET_PLACEHOLDER = "<secret_redacted>"
CONTENT_PLACEHOLDER = "<content_redacted:unsanitizable>"

ADDRESS_KEYS = {"street_address", "city", "state", "zip_code", "postal_code",
                "country", "address", "shipping_address", "street", "address_line1",
                "address_line2", "zip", "zipcode"}
SECRET_KEYS = {"password", "password_hash", "claim_code", "raw_code", "code_hash",
               "session_id_hash", "session_token", "auth_token", "cookie",
               "set-cookie", "authorization", "credit_card_number",
               "credit_card_cvv", "cvv", "secret", "api_key", "database_url",
               "accounts_database_url", "dsn", "activation_code"}

_STREET_SUFFIX = (r"Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|"
                  r"Court|Ct|Place|Pl|Parkway|Pkwy|Terrace|Ter|Circle|Cir|Highway|Hwy|"
                  r"Square|Sq|Trail|Trl|Alley|Aly")
_STREET_RE = re.compile(
    r"\b\d{1,6}[A-Za-z]?\s+(?:[A-Za-z0-9.'\-]+\s+){0,4}?(?:" + _STREET_SUFFIX + r")\b\.?"
    r"(?:\s*,?\s*(?:apt|apartment|suite|ste|unit|#)\s*[\w\-]+)?", re.I)
_POBOX_RE = re.compile(r"\bP\.?\s?O\.?\s+Box\s+\d+\b", re.I)
_STATE_ZIP_RE = re.compile(r"\b[A-Z]{2}\.?,?\s+\d{5}(?:-\d{4})?\b")
_CITY_STATE_ZIP_RE = re.compile(r"[A-Za-z][A-Za-z .'\-]{1,30},\s*[A-Z]{2}\.?,?\s+\d{5}(?:-\d{4})?\b")
_ZIP_LABEL_RE = re.compile(r"\b(zip(?:\s?code)?|postal(?:\s?code)?|postcode)\b(\W{0,12})\d{4,5}(?:-\d{4})?", re.I)
_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_CVV_RE = re.compile(r'("(?:credit_card_)?cvv"\s*:\s*)"?\d{3,4}"?', re.I)
_ADDRESS_KEY_IN_TEXT = re.compile(
    r'["\']?(' + "|".join(sorted(ADDRESS_KEYS - {"address", "state", "country", "city", "street", "zip"}))
    + r')["\']?\s*[:=]', re.I)
_KV_KEYS = (r"street_address|shipping_address|address_line1|address_line2|address|street|city|state|"
            r"zip_code|zipcode|zip|postal_code|postcode|country")
_KV_RE = re.compile(
    r"(?P<q>\\*[\"'])(?P<k>" + _KV_KEYS + r")(?P=q)(?P<sep>\s*:\s*)"
    r"(?:(?P<vq>\\*[\"'])(?P<v>[^\"'\\]*)(?P=vq)|(?P<num>\d[\d\-]*))", re.I)
_KV_EQ_RE = re.compile(r"\b(street_address|city|zip_code|postal_code|zip)=[^\s,;&]+", re.I)
_CONN_RE = re.compile(r"\b(?:postgres(?:ql)?|redis)://[^\s\"']+", re.I)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.I)

_lock = threading.Lock()
_known = OrderedDict()   # lower-cased exact sensitive values seen by the app
_MAX_KNOWN = 1024


def register_sensitive(*values):
    """Remember exact values (street lines, secrets) so any telemetry text that
    echoes them - even free text - is scrubbed at export time."""
    with _lock:
        for v in values:
            v = str(v or "").strip()
            if len(v) < 4:
                continue
            _known.pop(v.lower(), None)
            _known[v.lower()] = None
            while len(_known) > _MAX_KNOWN:
                _known.popitem(last=False)


def register_address(address):
    if isinstance(address, dict):
        register_sensitive(address.get("street_address"), address.get("city"))
        zip_code = str(address.get("zip_code") or address.get("postal_code") or "").strip()
        if len(zip_code) >= 5:
            register_sensitive(zip_code)


def _known_values():
    with _lock:
        return sorted(_known, key=len, reverse=True)


def _is_secret_key(key):
    k = str(key).lower()
    last = k.rsplit(".", 1)[-1]
    if k in SECRET_KEYS or last in SECRET_KEYS:
        return True
    if "password" in k or "passwd" in k:
        return True
    if "token" in last and not (last.endswith("_tokens") or last.endswith(".tokens") or last == "tokens"):
        return True
    return False


def _is_address_key(key):
    k = str(key).lower()
    return k in ADDRESS_KEYS or k.rsplit(".", 1)[-1] in {"street_address", "zip_code", "postal_code",
                                                         "shipping_address"}


def _kv_replace(m):
    if m.group("num") is not None:
        return f"{m.group('q')}{m.group('k')}{m.group('q')}{m.group('sep')}{m.group('q')}{ADDRESS_PLACEHOLDER}{m.group('q')}"
    return f"{m.group('q')}{m.group('k')}{m.group('q')}{m.group('sep')}{m.group('vq')}{ADDRESS_PLACEHOLDER}{m.group('vq')}"


def _scrub_text(s):
    # Address fields serialized inside free text (tool-call JSON appended to a model
    # message, or JSON escaped inside another JSON string) are not parseable as a
    # whole, so mask their values in place.
    s = _KV_RE.sub(_kv_replace, s)
    s = _KV_EQ_RE.sub(lambda m: m.group(1) + "=" + ADDRESS_PLACEHOLDER, s)
    for v in _known_values():
        if v in s.lower():
            s = re.sub(r"(?<![\w])" + re.escape(v) + r"(?![\w])", ADDRESS_PLACEHOLDER, s, flags=re.I)
    s = _CITY_STATE_ZIP_RE.sub(ADDRESS_PLACEHOLDER, s)
    s = _STREET_RE.sub(ADDRESS_PLACEHOLDER, s)
    s = _POBOX_RE.sub(ADDRESS_PLACEHOLDER, s)
    s = _STATE_ZIP_RE.sub(ADDRESS_PLACEHOLDER, s)
    s = _ZIP_LABEL_RE.sub(lambda m: m.group(1) + m.group(2) + ADDRESS_PLACEHOLDER, s)
    s = _CONN_RE.sub(SECRET_PLACEHOLDER, s)
    s = _BEARER_RE.sub("Bearer " + SECRET_PLACEHOLDER, s)
    s = _CARD_RE.sub("<card>", s)
    return _CVV_RE.sub(r'\1"<cvv>"', s)


def sanitize_text(s):
    """Sanitize a string that may be plain text or serialized JSON."""
    stripped = s.strip()
    if stripped[:1] in ("{", "[", '"'):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            # Truncated / malformed JSON that still names address fields cannot be
            # sanitized structurally: drop the whole thing.
            if _ADDRESS_KEY_IN_TEXT.search(s):
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
            if _is_address_key(key):
                out[key] = ADDRESS_PLACEHOLDER
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


def sanitize_attribute(key, value):
    """Sanitize one span/log attribute (name-aware)."""
    if _is_address_key(key):
        return ADDRESS_PLACEHOLDER
    if _is_secret_key(key):
        return SECRET_PLACEHOLDER
    try:
        return sanitize_value(value)
    except Exception:
        return _fail_closed(value)


def sanitize_attributes(attrs):
    if not attrs:
        return {}
    return {k: sanitize_attribute(k, v) for k, v in dict(attrs).items()}


# --- span exporter wrapper ----------------------------------------------------
_DB_PORTS = {"postgresql": 5432, "redis": 6379}


def _add_db_peer(attrs):
    """Give database client spans the peer attributes APM uses to draw a DB node and edge
    (same shape as cartservice's Redis spans). The DB instrumentors set db.system and
    net.peer.name after the span starts, so this runs at export time."""
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
    from opentelemetry.trace import Status

    attrs = _add_db_peer(sanitize_attributes(span.attributes))
    events = [Event(e.name, sanitize_attributes(e.attributes), e.timestamp) for e in (span.events or ())]
    status = span.status
    if status is not None and status.description:
        status = Status(status.status_code, sanitize_text(status.description))
    return ReadableSpan(
        name=span.name, context=span.get_span_context(), parent=span.parent,
        resource=span.resource, attributes=attrs, events=events, links=span.links,
        kind=span.kind, status=status, start_time=span.start_time, end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope)


def _minimal_span(span):
    """Last resort: structure, timing and numeric attributes only."""
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import Status, StatusCode

    attrs = {k: v for k, v in dict(span.attributes or {}).items()
             if isinstance(v, (bool, int, float)) and not _is_secret_key(k)}
    code = span.status.status_code if span.status else StatusCode.UNSET
    return ReadableSpan(
        name=span.name, context=span.get_span_context(), parent=span.parent,
        resource=span.resource, attributes=attrs, events=[], links=span.links,
        kind=span.kind, status=Status(code), start_time=span.start_time,
        end_time=span.end_time, instrumentation_scope=span.instrumentation_scope)


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


# --- log record processor -----------------------------------------------------
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
                        attrs[key] = sanitize_attribute(key, attrs[key])
                    except Exception:
                        attrs[key] = CONTENT_PLACEHOLDER

        def shutdown(self):
            return None

        def force_flush(self, timeout_millis=30000):
            return True

    return SanitizingLogRecordProcessor()
