"""Order stream consumer + reconciler (runs inside accountservice).

Stream `orders:v1` (consumer group `accountservice`) is written atomically with checkout state by
checkoutservice. Delivery is at-least-once; every database write is idempotent. Messages that keep
failing are moved to `orders:dead` after MAX_DELIVERIES with a reason CODE only (never payload data);
the reconciler repairs intents whose completion never arrived.
"""
import json
import socket
import threading
import time

from opentelemetry import trace
from opentelemetry.trace import Link, SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

import accounts_db
import config
import orders_db
import telemetry as tel

STREAM = "orders:v1"
DEAD = "orders:dead"
GROUP = "accountservice"
MAX_DELIVERIES = 5
CLAIM_IDLE_MS = 30000
tracer = trace.get_tracer("accountservice.orders")


def _client():
    import redis
    host, _, port = config.ORDER_QUEUE_ADDR.partition(":")
    return redis.Redis(host=host, port=int(port or 6379), decode_responses=True, socket_connect_timeout=2, socket_timeout=5)


def _link_from(tp):
    if not tp:
        return []
    ctx = TraceContextTextMapPropagator().extract({"traceparent": tp})
    sc = trace.get_current_span(ctx).get_span_context()
    return [Link(sc)] if sc.is_valid else []


class OrderConsumer:
    def __init__(self, repo, client=None, name=None, idle_ms=CLAIM_IDLE_MS, block_ms=500):
        self.repo = repo
        self._client = client
        self.name = name or f"{socket.gethostname()}-{id(self) % 10000}"
        self.idle_ms, self.block_ms = idle_ms, block_ms
        self._stop = threading.Event()
        self._thread = None
        self.processed = 0
        self.fail_hook = None            # test hook: callable(msg) -> raises to simulate a DB failure

    @property
    def r(self):
        if self._client is None:
            self._client = _client()
        return self._client

    # lifecycle ---------------------------------------------------------------------------------
    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="order-consumer", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(5)

    def _ensure_group(self):
        import redis
        try:
            self.r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def _loop(self):
        delay, last_claim, last_recon = 1.0, 0.0, 0.0
        while not self._stop.is_set():
            try:
                self._ensure_group()
                self.poll_once()
                now = time.monotonic()
                if now - last_claim > max(self.idle_ms / 2000.0, 1.0):
                    last_claim = now
                    self.claim_idle()
                if now - last_recon > 60:
                    last_recon = now
                    try:
                        Reconciler(self.repo, self.r).run_once()
                    except Exception as e:  # noqa: BLE001
                        tel.warn("orders.reconcile.error", **{"error.type": type(e).__name__})
                delay = 1.0
            except Exception as e:  # noqa: BLE001 - Redis down, etc.: back off, never crash the service
                tel.warn("orders.consumer.error", **{"error.type": type(e).__name__})
                self._stop.wait(delay)
                delay = min(delay * 2, 15)

    # processing -------------------------------------------------------------------------------
    def poll_once(self):
        resp = self.r.xreadgroup(GROUP, self.name, {STREAM: ">"}, count=20, block=self.block_ms)
        for _, entries in resp or []:
            for mid, fields in entries:
                self.handle(mid, fields, deliveries=1)

    def claim_idle(self):
        """Take over messages another consumer (or a crashed us) left unacknowledged."""
        start = "0-0"
        while True:
            res = self.r.xautoclaim(STREAM, GROUP, self.name, min_idle_time=self.idle_ms, start_id=start, count=20)
            start, entries = res[0], res[1]
            for mid, fields in entries:
                if not fields:
                    self.r.xack(STREAM, GROUP, mid)
                    continue
                pend = self.r.xpending_range(STREAM, GROUP, min=mid, max=mid, count=1)
                self.handle(mid, fields, deliveries=(pend[0]["times_delivered"] if pend else 2))
            if start in ("0-0", b"0-0") or not entries:
                return

    def handle(self, mid, fields, deliveries):
        t0 = time.time()
        msg = None
        with tracer.start_as_current_span("orders.consume", kind=SpanKind.CONSUMER, links=_link_from(fields.get("tp"))) as span:
            try:
                msg = json.loads(fields["p"])
                tel.set_attrs(span, {"app.order.id": msg.get("order_id"), "app.order.record_phase": msg.get("phase"),
                                     "app.order.retry_count": max(deliveries - 1, 0)})
                with tracer.start_as_current_span("orders.persist"):
                    if self.fail_hook:
                        self.fail_hook(msg)
                    orders_db.apply_message(self.repo, msg)
                self.r.xack(STREAM, GROUP, mid)
                self.processed += 1
                tel.set_attrs(span, {"app.order.outbox_status": "consumed"})
                if msg.get("placed_ms"):
                    lat = max(int(time.time() * 1000) - int(msg["placed_ms"]), 0)
                    tel.set_attrs(span, {"app.order.persistence_latency_ms": lat})
                    tel.orders_persist_latency.record(lat)
                tel.orders_recorded.add(1, {"phase": msg.get("phase", "unknown"), "path": "stream"})
            except Exception as e:  # noqa: BLE001
                tel.record_exception(span, e)
                if isinstance(e, accounts_db.AccountStoreUnavailable):
                    # Infrastructure outage (Postgres): the message is fine. Leave it pending and retry
                    # indefinitely; it must never be dead-lettered because the database was down.
                    tel.orders_outbox_retries.add(1)
                elif deliveries >= MAX_DELIVERIES or isinstance(e, (KeyError, ValueError)):
                    self.dead_letter(mid, fields, msg, type(e).__name__)
                    tel.set_attrs(span, {"app.order.outbox_status": "dead"})
                else:
                    tel.orders_outbox_retries.add(1)

    def dead_letter(self, mid, fields, msg, reason):
        self.r.xadd(DEAD, {"order_id": (msg or {}).get("order_id", ""), "phase": (msg or {}).get("phase", ""), "reason": reason[:48],
                           "orig_id": mid, "p": fields.get("p", "")})
        self.r.xack(STREAM, GROUP, mid)
        tel.orders_outbox_dead.add(1, {"reason": reason[:32]})
        tel.warn("orders.dead_letter", **{"error.type": reason[:48], "app.order.outbox_status": "dead"})

    # status -----------------------------------------------------------------------------------
    def status(self):
        depth = oldest = dead = 0
        try:
            for g in self.r.xinfo_groups(STREAM):
                if g["name"] == GROUP:
                    depth = int(g.get("lag") or 0) + int(g.get("pending") or 0)
                    if g.get("pending"):
                        p = self.r.xpending(STREAM, GROUP)
                        if p and p.get("min"):
                            oldest = max(int(time.time()) - int(p["min"].split("-")[0]) // 1000, 0)
            dead = self.r.xlen(DEAD)
        except Exception:  # noqa: BLE001
            return -1, 0, 0
        return depth, oldest, dead


class Reconciler:
    """Repairs history from the checkout state (co:{key}) when a stream message was lost or never sent."""

    def __init__(self, repo, client, min_age_s=600):
        self.repo, self.r, self.min_age_s = repo, client, min_age_s

    def run_once(self):
        repaired = 0
        with self.repo._op("order.reconcile.scan") as op:
            rows = op.conn.execute(
                "SELECT order_id::text AS order_id, idempotency_key FROM orders WHERE record_state = 'intent' "
                "AND intent_at < now() - make_interval(secs => %s) LIMIT 200", (self.min_age_s,)).fetchall()
        for row in rows:
            key = row["idempotency_key"]
            h = self.r.hgetall(f"co:{key}") if key else {}
            state = h.get("state")
            if state in ("PAYMENT_DECLINED", "FAILED_PRE_PAYMENT"):
                orders_db.apply_message(self.repo, {"phase": "failed", "order_id": row["order_id"]})
                repaired += 1
            elif state == "COMPLETED" and not h.get("pub_completed"):
                msg = rebuild_completed(row["order_id"], key, h)
                if msg:
                    orders_db.apply_message(self.repo, msg)
                    repaired += 1
                    tel.orders_recorded.add(1, {"phase": "completed", "path": "reconciler"})
            elif state is None or int(h.get("updated_ms", 0) or 0) < int((time.time() - 3600) * 1000):
                self._mark(row["order_id"], "intent_unresolved")
        return repaired

    def _mark(self, order_id, state):
        with self.repo._op("order.reconcile.mark") as op:
            op.conn.execute("INSERT INTO orders_reconciliation (order_id, state) VALUES (%s, %s) ON CONFLICT (order_id) DO UPDATE SET "
                            "state = EXCLUDED.state, last_seen = now()", (order_id, state))


def rebuild_completed(order_id, key, h):
    """Rebuild the completed message from the stored plan (mirrors checkoutservice's plan.message)."""
    try:
        p = json.loads(h["plan"])
        import hashlib
        email = (p.get("email") or "").strip().lower()
        return {"v": 1, "phase": "completed", "order_id": order_id, "key": key, "customer_id": p.get("customer_id") or None,
                "session_ref": p.get("session_ref"), "email_hash": hashlib.sha256(email.encode()).hexdigest(),
                "email_display": p.get("email") if p.get("customer_id") else None, "channel": p.get("channel"), "currency": p.get("currency"),
                "intent_ms": p.get("intent_ms"), "placed_ms": int(h.get("updated_ms") or time.time() * 1000), "items": p.get("items", []),
                "subtotal": p.get("subtotal"), "shipping": p.get("shipping"), "total": p.get("total"), "tracking_id": h.get("tracking_id"),
                "txn_id": h.get("txn_id"), "payment": {"method_id": p.get("payment_method_id"), "type": p.get("payment_type"),
                                                       "brand": p.get("brand"), "last4": p.get("last4")},
                "ship": {"line1": p["address"]["line1"], "line2": p["address"].get("line2"), "city": p["address"]["city"],
                         "state": p["address"]["state"], "zip": p["address"]["zip"], "country": p["address"]["country"], "is_po_box": False}}
    except Exception:  # noqa: BLE001
        return None
