"""Shared fixture: private Postgres + Redis + OTLP receiver + a real accountservice gRPC server.

Environment is set BEFORE the service modules are imported (config reads it at import time).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

import grpc  # noqa: E402

import otel_harness as oh  # noqa: E402
import pgtest  # noqa: E402

SECRETS = {"frontend": "fe-secret-test", "shoppingassistantservice": "sa-secret-test", "checkoutservice": "co-secret-test"}
_state = {}


def start(tracing=True):
    if _state:
        return _state
    cluster = pgtest.cluster()
    cluster.create_db("svc")
    redis_proc = oh.RedisProc()
    order_q = oh.RedisProc(persistence=True)
    receiver = oh.OtlpReceiver()
    os.environ.update({
        "ACCOUNTS_DATABASE_URL": cluster.dsn("svc"),
        "REDIS_ADDR": f"127.0.0.1:{redis_proc.port}",
        "ORDER_QUEUE_ADDR": f"127.0.0.1:{order_q.port}",
        "ACCOUNT_SERVICE_SECRETS": ",".join(f"{k}:{v}" for k, v in SECRETS.items()),
        "TELEMETRY_REF_KEY": "test-ref-key",
        "TELEMETRY_TEST_MODE": "1",
        "ENABLE_TRACING": "1" if tracing else "0",
        "OTEL_EXPORTER_OTLP_ENDPOINT": receiver.endpoint,
        "OTEL_BSP_SCHEDULE_DELAY": "150",
        "OTEL_METRIC_EXPORT_INTERVAL": "500",
        "OTEL_SERVICE_NAME": "accountservice",
        "OTEL_RESOURCE_ATTRIBUTES": "service.namespace=online-boutique,service.version=test-1,deployment.environment.name=test",
        "ACCOUNTS_MIGRATION_RETRY_MAX_S": "2",
    })
    import accounts
    import server
    import telemetry as tel

    tel.init()
    store = accounts.AccountStore()
    store.start()
    import orders
    consumer = orders.OrderConsumer(store.repo, idle_ms=int(os.environ.get("TEST_CLAIM_IDLE_MS", "1500")), block_ms=200)
    consumer.start()
    srv, port, health = server.build_server(store, "127.0.0.1:0", consumer=consumer)
    srv.start()
    import account_pb2_grpc as pbg
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    _state.update(cluster=cluster, redis=redis_proc, order_q=order_q, consumer=consumer, receiver=receiver, store=store, server=srv,
                  stub=pbg.AccountServiceStub(channel), channel=channel, port=port, dsn=cluster.dsn("svc"))
    import time
    for _ in range(200):
        if store.repo.schema.state == "ready":
            break
        time.sleep(0.1)
    assert store.repo.schema.state == "ready", store.repo.schema.state
    import atexit
    atexit.register(stop)
    return _state


def stop():
    s = dict(_state)
    _state.clear()
    for k in ("server",):
        try:
            s[k].stop(0)
        except Exception:
            pass
    try:
        s["consumer"].stop()
    except Exception:
        pass
    for k in ("redis", "order_q", "receiver"):
        try:
            s[k].stop() if k == "receiver" else s[k].close()
        except Exception:
            pass


def md(service="frontend", token=None, extra=None):
    items = [("x-service-name", service), ("x-service-secret", SECRETS[service])]
    if token:
        items.append(("x-account-session", token))
    items += list(extra or [])
    return tuple(items)


def call(fn, req, service="frontend", token=None, extra=None):
    """-> (response|None, grpc.RpcError|None)"""
    try:
        return fn(req, metadata=md(service, token, extra), timeout=20), None
    except grpc.RpcError as e:
        return None, e


def field_codes(err):
    """{field: code} from the BadRequest details."""
    from grpc_status import rpc_status
    from google.rpc import error_details_pb2
    st = rpc_status.from_call(err)
    out = {}
    for d in st.details if st else []:
        bad = error_details_pb2.BadRequest()
        if d.Unpack(bad):
            out.update({v.field: v.reason for v in bad.field_violations})
    return out


def error_reason(err):
    from grpc_status import rpc_status
    from google.rpc import error_details_pb2
    st = rpc_status.from_call(err)
    for d in st.details if st else []:
        info = error_details_pb2.ErrorInfo()
        if d.Unpack(info):
            return info.reason
    return None
