-- requires: 0004
-- Order history. Orders enter this table only through the Redis Stream consumer; order_id is the
-- checkout idempotency key, so replays and redeliveries upsert the same row.

CREATE TABLE orders (
    order_id            uuid PRIMARY KEY,
    customer_id         uuid REFERENCES customers (id) ON DELETE SET NULL,   -- NULL = guest order
    session_ref         text,                       -- opaque HMAC reference, never the raw session id
    idempotency_key     text,                       -- internal: links the row to the checkout state in the order queue
    email_hash          text NOT NULL,              -- sha256(lowercased email); guest-order claims only
    email_display       text,                       -- authenticated orders only
    channel             text NOT NULL CHECK (channel IN ('web', 'assistant')),
    prompt_version      text,
    record_state        text NOT NULL CHECK (record_state IN ('intent', 'confirmed')),
    intent_at           timestamptz,
    placed_at           timestamptz,
    status              text NOT NULL DEFAULT 'confirmed'
                        CHECK (status IN ('confirmed', 'shipped', 'delivered', 'cancelled')),
    currency            char(3) NOT NULL,
    subtotal_units      bigint,
    subtotal_nanos      integer,
    shipping_units      bigint,
    shipping_nanos      integer,
    total_units         bigint NOT NULL,
    total_nanos         integer NOT NULL,
    tracking_id         text,
    transaction_id      text,
    payment_method_id   uuid REFERENCES payment_methods (id) ON DELETE SET NULL,
    payment_method_type text,
    payment_brand       text,
    payment_last4       char(4),
    ship_line1          text,
    ship_line2          text,
    ship_city           text,
    ship_state          char(2),
    ship_zip            text,
    ship_country        char(2),
    ship_is_po_box      boolean,
    recorded_at         timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (record_state <> 'confirmed' OR placed_at IS NOT NULL)
);

CREATE TABLE order_items (
    order_id         uuid NOT NULL REFERENCES orders (order_id) ON DELETE CASCADE,
    line_no          integer NOT NULL,
    product_id       text NOT NULL,
    product_category text,
    name             text NOT NULL,
    quantity         integer NOT NULL CHECK (quantity > 0),
    unit_price_units bigint,
    unit_price_nanos integer,
    currency         char(3),
    PRIMARY KEY (order_id, line_no)
);

CREATE INDEX orders_customer_placed_idx ON orders (customer_id, placed_at DESC, order_id)
    WHERE customer_id IS NOT NULL;
CREATE INDEX orders_session_ref_idx ON orders (session_ref) WHERE customer_id IS NULL;
CREATE INDEX orders_guest_email_hash_idx ON orders (email_hash) WHERE customer_id IS NULL;

CREATE TABLE orders_reconciliation (
    order_id   uuid PRIMARY KEY,
    state      text NOT NULL CHECK (state IN ('intent_unresolved', 'payment_uncertain', 'dead_letter', 'resolved')),
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen  timestamptz NOT NULL DEFAULT now(),
    detail_code text
);
