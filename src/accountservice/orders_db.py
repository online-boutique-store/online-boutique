"""Order history persistence: Postgres writes for stream messages, list/get/claim queries.

All writes are idempotent (order_id is the primary key; replays upsert the same row), so the stream
consumer can redeliver, crash and restart without duplicating or corrupting history.
"""
import base64
import json
import time

import config
import telemetry as tel
import telemetry_schema as schema

STATUS_OK = "confirmed"


def _minor(m):
    return schema.minor_units(m.get("u", 0), m.get("n", 0), m.get("c", "USD"))


def _sanitize_msg_for_telemetry(msg):
    return {"order_id": msg.get("order_id"), "phase": msg.get("phase")}


def apply_message(repo, msg):
    """Apply one stream message. phase: intent | completed | failed. Returns a short outcome string."""
    phase = msg["phase"]
    oid = msg["order_id"]
    with repo._op("order.apply") as op:
        c = op.conn
        cid = msg.get("customer_id")
        if cid and not c.execute("SELECT 1 FROM customers WHERE id = %s", (cid,)).fetchone():
            cid = None                      # account deleted since checkout: keep the order as a guest order
        pm = msg.get("payment", {}) or {}
        pm_id = pm.get("method_id") or None
        if pm_id and not c.execute("SELECT 1 FROM payment_methods WHERE id = %s", (pm_id,)).fetchone():
            pm_id = None
        ship = msg.get("ship", {}) or {}
        if phase == "failed":
            n = c.execute("DELETE FROM orders WHERE order_id = %s AND record_state = 'intent'", (oid,)).rowcount
            c.execute("UPDATE orders_reconciliation SET state='resolved', last_seen=now() WHERE order_id = %s", (oid,))
            return "failed_cleaned" if n else "failed_noop"
        sub, shp, tot = msg.get("subtotal", {}), msg.get("shipping", {}), msg.get("total", {})
        state = "confirmed" if phase == "completed" else "intent"
        placed = msg.get("placed_ms")
        cols = [
            ("order_id", "%s", oid), ("customer_id", "%s", cid), ("session_ref", "%s", msg.get("session_ref")),
            ("idempotency_key", "%s", msg.get("key")), ("email_hash", "%s", msg.get("email_hash") or ""),
            ("email_display", "%s", msg.get("email_display") if cid else None), ("channel", "%s", msg.get("channel", "web")),
            ("prompt_version", "%s", msg.get("prompt_version") or None), ("record_state", "%s", state),
            ("intent_at", "to_timestamp(%s/1000.0)", msg.get("intent_ms") or int(time.time() * 1000)),
            ("placed_at", "to_timestamp(%s::bigint/1000.0)", placed), ("status", "'confirmed'", None),
            ("currency", "%s", msg.get("currency", "USD")), ("subtotal_units", "%s", sub.get("u")), ("subtotal_nanos", "%s", sub.get("n")),
            ("shipping_units", "%s", shp.get("u")), ("shipping_nanos", "%s", shp.get("n")), ("total_units", "%s", tot.get("u", 0)),
            ("total_nanos", "%s", tot.get("n", 0)), ("tracking_id", "%s", msg.get("tracking_id") or None),
            ("transaction_id", "%s", msg.get("txn_id") or None), ("payment_method_id", "%s", pm_id),
            ("payment_method_type", "%s", pm.get("type") or None), ("payment_brand", "%s", pm.get("brand") or None),
            ("payment_last4", "%s", pm.get("last4") or None), ("ship_line1", "%s", ship.get("line1")),
            ("ship_line2", "%s", ship.get("line2") or None), ("ship_city", "%s", ship.get("city")),
            ("ship_state", "%s", (ship.get("state") or "")[:2] or None), ("ship_zip", "%s", ship.get("zip")),
            ("ship_country", "%s", (ship.get("country") or "US")[:2].upper()), ("ship_is_po_box", "%s", bool(ship.get("is_po_box"))),
        ]
        names = ", ".join(n for n, _, _ in cols)
        values = ", ".join(v for _, v, _ in cols)
        params = [p for _, v, p in cols if "%s" in v]
        c.execute(
            f"INSERT INTO orders ({names}) VALUES ({values}) "
            "ON CONFLICT (order_id) DO UPDATE SET "
            " customer_id = coalesce(EXCLUDED.customer_id, orders.customer_id), "
            " record_state = CASE WHEN orders.record_state = 'confirmed' THEN 'confirmed' ELSE EXCLUDED.record_state END, "
            " placed_at = coalesce(EXCLUDED.placed_at, orders.placed_at), tracking_id = coalesce(EXCLUDED.tracking_id, orders.tracking_id), "
            " transaction_id = coalesce(EXCLUDED.transaction_id, orders.transaction_id), updated_at = now()", params)
        if phase == "completed":
            c.execute("DELETE FROM order_items WHERE order_id = %s", (oid,))
            for i, it in enumerate(msg.get("items", []), 1):
                u = it.get("unit", {})
                c.execute("INSERT INTO order_items (order_id, line_no, product_id, product_category, name, quantity, unit_price_units, unit_price_nanos, currency) "
                          "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                          (oid, i, it["product_id"], it.get("category") or None, it.get("name") or it["product_id"], it["quantity"], u.get("u"), u.get("n"), u.get("c")))
            c.execute("UPDATE orders_reconciliation SET state='resolved', last_seen=now() WHERE order_id = %s", (oid,))
            tel.set_attrs(op.span, {"app.order.id": oid, "app.order.record_phase": "completed", "app.order.status": "confirmed",
                                    "app.order.currency": msg.get("currency", "USD"), "app.order.item_count": len(msg.get("items", [])),
                                    "app.order.total_minor_units": _minor(tot), "app.order.channel": msg.get("channel", "web"),
                                    "app.customer.id": cid})
        else:
            tel.set_attrs(op.span, {"app.order.id": oid, "app.order.record_phase": "intent", "app.customer.id": cid})
        return phase


def _cursor_encode(placed_ms, order_id):
    return base64.urlsafe_b64encode(f"{placed_ms}:{order_id}".encode()).decode()


def _cursor_decode(cur):
    try:
        ms, oid = base64.urlsafe_b64decode(cur.encode()).decode().split(":", 1)
        return int(ms), oid
    except Exception:  # noqa: BLE001
        return None


def list_orders(repo, customer_id, page_size=20, cursor=""):
    page_size = max(1, min(int(page_size or 20), 50))
    with repo._op("order.list", customer_id) as op:
        params = [customer_id]
        where = "customer_id = %s AND record_state = 'confirmed'"
        cur = _cursor_decode(cursor) if cursor else None
        if cur:
            where += " AND (placed_at, order_id) < (to_timestamp(%s/1000.0), %s::uuid)"
            params += [cur[0], cur[1]]
        rows = op.conn.execute(
            "SELECT o.order_id::text AS order_id, (extract(epoch FROM o.placed_at)*1000)::bigint AS placed_ms, o.status, o.currency, "
            "o.total_units, o.total_nanos, (SELECT coalesce(sum(quantity),0) FROM order_items i WHERE i.order_id = o.order_id)::int AS item_count, "
            "(SELECT coalesce(array_agg(name ORDER BY line_no), '{}') FROM order_items i WHERE i.order_id = o.order_id) AS item_names "
            f"FROM orders o WHERE {where} ORDER BY placed_at DESC, order_id DESC LIMIT %s", params + [page_size + 1]).fetchall()
        more = len(rows) > page_size
        rows = rows[:page_size]
        nxt = _cursor_encode(rows[-1]["placed_ms"], rows[-1]["order_id"]) if more and rows else ""
        tel.set_attrs(op.span, {"app.order.item_count": len(rows)})
        return rows, nxt


def get_order(repo, customer_id, order_id):
    with repo._op("order.get", customer_id) as op:
        try:
            row = op.conn.execute(
                "SELECT order_id::text AS order_id, (extract(epoch FROM placed_at)*1000)::bigint AS placed_ms, status, currency, "
                "subtotal_units, subtotal_nanos, shipping_units, shipping_nanos, total_units, total_nanos, tracking_id, channel, "
                "payment_brand, payment_last4, ship_line1, ship_line2, ship_city, ship_state, ship_zip, ship_country, ship_is_po_box "
                "FROM orders WHERE order_id = %s::uuid AND customer_id = %s AND record_state = 'confirmed'", (order_id, customer_id)).fetchone()
        except Exception as e:  # noqa: BLE001 - malformed id
            if type(e).__name__ in ("InvalidTextRepresentation", "DataError"):
                op.conn.rollback()
                op.outcome = "not_found"
                return None, []
            raise
        if not row:
            op.outcome = "not_found"
            return None, []
        items = op.conn.execute("SELECT product_id, name, quantity, unit_price_units, unit_price_nanos, currency FROM order_items "
                                "WHERE order_id = %s::uuid ORDER BY line_no", (order_id,)).fetchall()
        tel.set_attrs(op.span, {"app.order.id": row["order_id"]})
        return row, items


def claim_guest_orders(repo, customer_id, session_ref):
    """Attach guest orders placed from THIS browser session (same HMAC session reference) to the customer."""
    if not session_ref:
        return 0
    with repo._op("order.claim", customer_id) as op:
        n = op.conn.execute("UPDATE orders SET customer_id = %s, updated_at = now() WHERE customer_id IS NULL AND session_ref = %s "
                            "AND record_state = 'confirmed'", (customer_id, session_ref)).rowcount
        return n
