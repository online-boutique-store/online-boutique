-- requires: 0002
-- Saved payment methods: opaque demo tokens plus safe card metadata only. No PAN, no CVV, no
-- cardholder name, no billing address. This table MUST exist before orders (0005) because
-- orders.payment_method_id references it; the payment-method UI/RPCs ship after order history.

CREATE TABLE payment_methods (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id    uuid NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
    provider       text NOT NULL DEFAULT 'demo-psp',
    provider_token text NOT NULL,
    catalog_id     text NOT NULL,
    brand          text NOT NULL CHECK (brand IN ('visa', 'mastercard', 'amex', 'discover')),
    last4          char(4) NOT NULL CHECK (last4 ~ '^[0-9]{4}$'),
    exp_month      smallint NOT NULL CHECK (exp_month BETWEEN 1 AND 12),
    exp_year       smallint NOT NULL CHECK (exp_year BETWEEN 2000 AND 2100),
    label          text CHECK (label IS NULL OR char_length(label) <= 40),
    is_default     boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at     timestamptz,
    UNIQUE (provider, provider_token)
);
CREATE UNIQUE INDEX payment_methods_one_default ON payment_methods (customer_id)
    WHERE is_default AND deleted_at IS NULL;
CREATE INDEX payment_methods_customer_idx ON payment_methods (customer_id) WHERE deleted_at IS NULL;
