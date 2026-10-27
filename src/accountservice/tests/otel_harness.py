"""Local OTLP collector + helpers for instrumentation tests.

OtlpReceiver is an in-process OTLP/gRPC server (traces, metrics, logs). It keeps decoded spans for
structural assertions AND the raw serialized requests, so canary scans cover every byte that was
exported: span names, attributes, events, links, resource attributes, metric attributes, log bodies.
"""
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from concurrent import futures

import grpc
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2, logs_service_pb2_grpc
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2, metrics_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc

KIND = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _val(v):
    which = v.WhichOneof("value")
    if which is None:
        return None
    if which == "array_value":
        return [_val(x) for x in v.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _val(kv.value) for kv in v.kvlist_value.values}
    return getattr(v, which)


def attrs(kvs):
    return {kv.key: _val(kv.value) for kv in kvs}


class Span:
    def __init__(self, s, resource, scope):
        self.name = s.name
        self.kind = KIND.get(s.kind, str(s.kind))
        self.trace_id = s.trace_id.hex()
        self.span_id = s.span_id.hex()
        self.parent_id = s.parent_span_id.hex()
        self.attrs = attrs(s.attributes)
        self.events = [(e.name, attrs(e.attributes)) for e in s.events]
        self.links = [(l.trace_id.hex(), l.span_id.hex(), attrs(l.attributes)) for l in s.links]
        self.status = s.status.code
        self.resource = resource
        self.scope = scope
        self.service = resource.get("service.name", "")

    def __repr__(self):
        return f"<{self.service}:{self.name} {self.kind}>"


class OtlpReceiver:
    def __init__(self):
        self.lock = threading.Lock()
        self.spans, self.raw, self.metric_points, self.logs = [], [], [], []
        outer = self

        class T(trace_service_pb2_grpc.TraceServiceServicer):
            def Export(self, request, context):
                with outer.lock:
                    outer.raw.append(request.SerializeToString())
                    for rs in request.resource_spans:
                        res = attrs(rs.resource.attributes)
                        for ss in rs.scope_spans:
                            for s in ss.spans:
                                outer.spans.append(Span(s, res, ss.scope.name))
                return trace_service_pb2.ExportTraceServiceResponse()

        class M(metrics_service_pb2_grpc.MetricsServiceServicer):
            def Export(self, request, context):
                with outer.lock:
                    outer.raw.append(request.SerializeToString())
                    for rm in request.resource_metrics:
                        for sm in rm.scope_metrics:
                            for m in sm.metrics:
                                outer.metric_points.append((m.name, attrs(rm.resource.attributes)))
                return metrics_service_pb2.ExportMetricsServiceResponse()

        class L(logs_service_pb2_grpc.LogsServiceServicer):
            def Export(self, request, context):
                with outer.lock:
                    outer.raw.append(request.SerializeToString())
                return logs_service_pb2.ExportLogsServiceResponse()

        self.server = grpc.server(futures.ThreadPoolExecutor(4))
        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(T(), self.server)
        metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(M(), self.server)
        logs_service_pb2_grpc.add_LogsServiceServicer_to_server(L(), self.server)
        self.port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()
        self.endpoint = f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.stop(0)

    # queries ------------------------------------------------------------------------------
    def find(self, name=None, service=None, kind=None, **attr_equals):
        with self.lock:
            out = list(self.spans)
        res = []
        for s in out:
            if name is not None and not (s.name == name or (hasattr(name, "match") and name.match(s.name))):
                continue
            if service and s.service != service:
                continue
            if kind and s.kind != kind:
                continue
            if any(s.attrs.get(k.replace("__", ".")) != v for k, v in attr_equals.items()):
                continue
            res.append(s)
        return res

    def wait_for(self, predicate, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            if predicate(self):
                return True
            time.sleep(0.1)
        return predicate(self)

    def raw_bytes(self):
        with self.lock:
            return b"\n".join(self.raw)

    def clear(self):
        with self.lock:
            self.spans.clear()
            self.raw.clear()
            self.metric_points.clear()

    def canary_hits(self, canaries):
        """Every canary string found anywhere in the exported bytes (case-insensitive)."""
        blob = self.raw_bytes().lower()
        return [c for c in canaries if c.lower().encode() in blob]

    def trace_spans(self, trace_id):
        with self.lock:
            return [s for s in self.spans if s.trace_id == trace_id]


class RedisProc:
    """A real redis-server on a private port. persistence=True => AOF with appendfsync always."""

    def __init__(self, persistence=False, directory=None, port=None):
        self.dir = directory or tempfile.mkdtemp(prefix="redis")
        self.owned = directory is None
        self.port = port or free_port()
        self.persistence = persistence
        self.proc = None
        self.start()

    def start(self):
        args = ["redis-server", "--port", str(self.port), "--bind", "127.0.0.1", "--dir", self.dir, "--save", ""]
        if self.persistence:
            args += ["--appendonly", "yes", "--appendfsync", "always", "--aof-use-rdb-preamble", "yes",
                     "--no-appendfsync-on-rewrite", "no", "--maxmemory-policy", "noeviction"]
        self.log = open(os.path.join(self.dir, "redis.log"), "ab")
        self.proc = subprocess.Popen(args, stdout=self.log, stderr=self.log, stdin=subprocess.DEVNULL)
        import redis
        for _ in range(100):
            try:
                redis.Redis(port=self.port, socket_connect_timeout=0.2).ping()
                return
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("redis did not start")

    def kill9(self):
        self.proc.kill()
        self.proc.wait()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def client(self, **kw):
        import redis
        return redis.Redis(port=self.port, decode_responses=kw.pop("decode_responses", True), **kw)

    def close(self):
        self.stop()
        if self.owned:
            shutil.rmtree(self.dir, ignore_errors=True)
