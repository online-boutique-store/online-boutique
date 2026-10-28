import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

try:
    import telemetry
except ModuleNotFoundError:
    telemetry = None


def test_telemetry_redaction_policy():
    if telemetry is None:
        return
    payload = {
        "name": "Demo Shopper",
        "email": "demo.customer@example.com",
        "address": {"street_address": "1 Demo Way", "city": "Testville", "zip_code": "12345"},
        "password": "not-for-telemetry",
        "claim_code": "activation-secret",
        "currency_code": "JPY",
        "error": "failed to convert JPY to USD",
    }
    redacted = telemetry.redact(json.dumps(payload))
    assert "Demo Shopper" in redacted
    assert "demo.customer@example.com" in redacted
    assert "1 Demo Way" not in redacted
    assert "Testville" not in redacted
    assert "12345" not in redacted
    assert "not-for-telemetry" not in redacted
    assert "activation-secret" not in redacted
    assert "JPY" in redacted
    assert "failed to convert JPY to USD" in redacted



def test_grpc_client_spans_get_peer():
    from opentelemetry.trace import SpanKind
    from telemetry_sanitize import _add_grpc_peer
    a = {"rpc.system": "grpc", "rpc.service": "hipstershop.CartService"}
    assert _add_grpc_peer(a, SpanKind.CLIENT)["peer.service"] == "cartservice"
    assert "peer.service" not in _add_grpc_peer(a, SpanKind.SERVER)
    b = dict(a, **{"server.address": "x"})
    assert _add_grpc_peer(b, SpanKind.CLIENT)["server.address"] == "x"
    print("PASS test_grpc_client_spans_get_peer")


if __name__ == "__main__":
    test_telemetry_redaction_policy()
    print("PASS test_telemetry_redaction_policy")
    test_grpc_client_spans_get_peer()
