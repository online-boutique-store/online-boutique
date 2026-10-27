-- Customer accounts. Applied by accounts_db.Migrator inside ONE transaction that
-- also records this version in schema_migrations, so a failure leaves nothing behind.
-- Rules for migration files (enforced by tests/test_accounts_pg.py): no BEGIN/COMMIT,
-- no CREATE INDEX CONCURRENTLY, no VACUUM - everything must be transactional.
-- Only core Postgres features are used (no citext/pgcrypto): gen_random_uuid() is
-- built in since Postgres 13, and emails are stored already lower-cased.

CREATE TABLE accounts_meta (
    singleton   boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    instance_id uuid NOT NULL DEFAULT gen_random_uuid(),
    created_at  timestamptz NOT NULL DEFAULT now()
);
INSERT INTO accounts_meta DEFAULT VALUES;

CREATE TABLE customers (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email                     text NOT NULL UNIQUE CHECK (email = lower(email) AND email <> ''),
    name                      text NOT NULL,
    password_hash             text,
    status                    text NOT NULL CHECK (status IN ('pending_activation', 'active')),
    created_via               text NOT NULL CHECK (created_via IN ('web', 'assistant')),
    preferred_currency        char(3) NOT NULL DEFAULT 'USD',
    default_payment_method_id text,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CHECK (status = 'pending_activation' OR password_hash IS NOT NULL)
);

CREATE TABLE addresses (
    customer_id    uuid PRIMARY KEY REFERENCES customers (id) ON DELETE CASCADE,
    street_address text NOT NULL,
    city           text NOT NULL,
    state          text NOT NULL,
    zip_code       text NOT NULL,
    country        text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- One open claim per pending customer, bound to the browser session that created it.
-- Only hashes are stored; the raw code lives briefly in the Redis handoff.
CREATE TABLE activation_claims (
    customer_id     uuid PRIMARY KEY REFERENCES customers (id) ON DELETE CASCADE,
    session_id_hash text NOT NULL,
    code_hash       text NOT NULL,
    expires_at      timestamptz NOT NULL,
    used_at         timestamptz,
    rotations       integer NOT NULL DEFAULT 0
);
CREATE INDEX activation_claims_session_idx ON activation_claims (session_id_hash) WHERE used_at IS NULL;
CREATE INDEX activation_claims_expiry_idx ON activation_claims (expires_at) WHERE used_at IS NULL;

CREATE TABLE login_events (
    id          bigserial PRIMARY KEY,
    customer_id uuid REFERENCES customers (id) ON DELETE SET NULL,
    outcome     text NOT NULL,
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_events_customer_idx ON login_events (customer_id, at);
